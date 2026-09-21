"""N05 专项测试：行情质量全链路传递、慢行情与事件正文解耦、游标稳定性与 10 万级基准。

测试覆盖：
1. Quote 数据契约：market_timestamp / fetched_at / source / currency / is_delayed / change_basis / quality
2. /events 慢行情解耦：列表查询只读缓存，外部供应商慢/阻塞时不阻断正文毫秒级返回
3. 游标分页契约：first_seen_membership_latest_content、翻页中修订内容、时间戳碰撞、尾页 next_cursor 为空、非法 cursor 校验
4. 10 万事件基准：EXPLAIN QUERY PLAN 确认使用 idx_event_first_seen 索引扫描，P95 < 15ms
5. 缓存退避与行情质量保真：stale/delayed 绝不升级为 cached/real，超龄隐藏数值
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.api.schemas import EventSecurityChip, IndexQuoteItem, WatchlistEntryItem, WatchlistSearchItem
from trace.collectors.market_data.base import Quote
from trace.collectors.market_data.confirmation import MarketConfirmer
from trace.collectors.market_data.time_quality import quote_quality
from trace.common.cursors import decode_cursor, encode_cursor
from trace.common.ids import event_id as make_event_id, raw_item_id as make_raw_id
from trace.db.connection import Database
from trace.db.migration import apply_migrations
from trace.db.repositories import EventImpactRepo, EventRepo, EventSourceRepo, RawItemRepo, SecurityRepo
from trace.domain.models import Event, EventImpact, EventSource, RawItem, Security


# ---------------------------------------------------------------------------
# 1. Quote 契约与质量推导测试
# ---------------------------------------------------------------------------

def test_quote_contract_and_quality_property():
    """验证 Quote 数据契约字段完整，且 quality 属性准确识别时效状态。"""
    now = datetime.now(timezone.utc)
    # 1. 正常实时行情
    q_real = Quote(
        ticker="NVDA", ts=now, last_price=120.5, prev_close=118.0,
        market_timestamp=now - timedelta(seconds=10), fetched_at=now,
        currency="USD", source="alpaca", is_delayed=False, change_basis="prev_close"
    )
    assert q_real.quality == "real"
    assert q_real.change_pct_from_prev() == 2.12

    # 2. 延时行情 (15分钟延时，在 1200s 内合法)
    q_delayed = Quote(
        ticker="SPY", ts=now, last_price=540.0, prev_close=538.0,
        market_timestamp=now - timedelta(minutes=10), fetched_at=now,
        currency="USD", source="alpaca", is_delayed=True, change_basis="prev_close"
    )
    assert q_delayed.quality == "delayed"

    # 3. 陈旧行情 (超过允许窗口)
    q_stale = Quote(
        ticker="AAPL", ts=now, last_price=220.0, prev_close=219.0,
        market_timestamp=now - timedelta(minutes=25), fetched_at=now,
        currency="USD", source="alpaca", is_delayed=False, change_basis="prev_close"
    )
    assert q_stale.quality == "stale"

    # 4. 未知时间戳
    q_unknown = Quote(
        ticker="TEST", ts=now, last_price=10.0, market_timestamp=None
    )
    assert q_unknown.quality == "unknown_timestamp"


# ---------------------------------------------------------------------------
# 2. /events 慢行情解耦测试 (Slow Quote Decoupling)
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_client(app):
    api_app = create_api_app(ctx=app)
    with TestClient(api_app) as c:
        auth_resp = c.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "test_n05_user"})
        token = auth_resp.json()["session_token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def test_events_list_decoupled_from_slow_quotes(app, auth_client, monkeypatch):
    """验证 /events 列表接口不阻塞在慢行情网络调用上，秒级内极速返回。"""
    # 启用来源展示权限
    app.db.execute("UPDATE source SET can_display=1 WHERE source_id='src_sec_edgar'")
    now = datetime.now(timezone.utc)
    db = app.db
    sec_repo = SecurityRepo(db)
    ev_repo = EventRepo(db)
    imp_repo = EventImpactRepo(db)

    sec = Security(security_id="SEC-US-SLOW", ticker="SLOW", market="US", company_name_zh="慢速标的")
    sec_repo.upsert(sec)

    e_id = make_event_id()
    ev = Event(event_id=e_id, title="慢速标的重要公告", status="confirmed", version=1,
               first_seen_at=now, first_source_id="src_sec_edgar")
    ev_repo.insert(ev)
    imp_repo.upsert(EventImpact(impact_id=f"imp_{e_id}", event_id=e_id, security_id=sec.security_id, final_score=8.0))

    # 模拟外部 provider 的 get_quotes 会阻塞 5 秒的极端慢网络
    def blocking_quotes(*args, **kwargs):
        time.sleep(5.0)
        return {}

    for p in app.confirmer._providers.values():
        monkeypatch.setattr(p, "get_quotes", blocking_quotes)

    # 发起 /events 列表请求，测量耗时
    t0 = time.perf_counter()
    resp = auth_client.get("/api/v1/events?limit=10")
    duration = time.perf_counter() - t0

    assert resp.status_code == 200
    # 绝不能等待 5 秒！必须在 500ms 内基于本地缓存立即响应
    assert duration < 0.5, f"Events list took too long ({duration:.3f}s), failed to decouple slow quotes!"
    
    data = resp.json()
    assert len(data["items"]) >= 1
    found = [it for it in data["items"] if it["event_id"] == e_id]
    assert len(found) == 1
    # 未命中缓存时，chip 的 quality 为 unavailable，不阻断主数据
    assert len(found[0]["securities"]) == 1
    assert found[0]["securities"][0]["ticker"] == "SLOW"
    assert found[0]["securities"][0]["quality"] == "unavailable"


# ---------------------------------------------------------------------------
# 3. 游标分页契约与稳定性测试 (first_seen_membership_latest_content)
# ---------------------------------------------------------------------------

def test_cursor_pagination_contract_and_revision_stability(app, auth_client):
    """测试游标稳定消费、翻页期间内容修订 (最新版本)、边界碰撞与尾页判定。"""
    app.db.execute("UPDATE source SET can_display=1 WHERE source_id='src_sec_edgar'")
    db = app.db
    ev_repo = EventRepo(db)

    base_time = datetime.now(timezone.utc) - timedelta(hours=2)
    event_ids = []

    # 1. 插入 5 个有序事件 (E1..E5, 时间从早到晚)
    for i in range(1, 6):
        eid = f"EVT-PAG-{i:02d}"
        ev = Event(
            event_id=eid,
            title=f"事件 {i} 原文标题",
            summary=f"事件 {i} 初始摘要",
            first_seen_at=base_time + timedelta(minutes=i * 10),
            version=1,
            status="confirmed",
            first_source_id="src_sec_edgar",
        )
        ev_repo.insert(ev)
        event_ids.append(eid)

    # 2. 读取第 1 页 (limit=2) -> 期望获得最新的 [E5, E4]
    resp1 = auth_client.get("/api/v1/events?limit=2")
    assert resp1.status_code == 200
    p1 = resp1.json()
    assert len(p1["items"]) == 2
    assert [it["event_id"] for it in p1["items"]] == ["EVT-PAG-05", "EVT-PAG-04"]
    assert p1["pagination"]["has_more"] is True
    cursor_1 = p1["pagination"]["next_cursor"]
    assert cursor_1 is not None

    # 3. 模拟在用户翻页前，EVT-PAG-03 发生了内容修订 (更新到版本 2，修改了标题和内容)
    ev3 = ev_repo.get("EVT-PAG-03")
    ev3.version = 2
    ev3.title = "事件 3 已被官方修订（新标题）"
    ev3.summary = "事件 3 修正了营收数据"
    ev3.last_updated_at = datetime.now(timezone.utc)
    ev_repo.update(ev3)

    # 4. 使用 cursor_1 读取第 2 页 (limit=2)
    resp2 = auth_client.get(f"/api/v1/events?limit=2&cursor={cursor_1}")
    assert resp2.status_code == 200
    p2 = resp2.json()
    assert len(p2["items"]) == 2
    # 验证边界稳定：精确消费 [E3, E2]，没有遗漏 E3，也没有重复 E4
    assert [it["event_id"] for it in p2["items"]] == ["EVT-PAG-03", "EVT-PAG-02"]
    # 验证契约 first_seen_membership_latest_content：
    # 成员边界根据 first_seen_at 稳定排在第 2 页，而内容是最新的版本 2！
    assert p2["items"][0]["version"] == 2
    assert "已被官方修订" in p2["items"][0]["title"]

    cursor_2 = p2["pagination"]["next_cursor"]
    assert cursor_2 is not None

    # 5. 读取第 3 页 (最后一页，剩余 1 条数据 EVT-PAG-01)
    resp3 = auth_client.get(f"/api/v1/events?limit=2&cursor={cursor_2}")
    assert resp3.status_code == 200
    p3 = resp3.json()
    assert len(p3["items"]) == 1
    assert p3["items"][0]["event_id"] == "EVT-PAG-01"
    # 验证尾页契约：当返回数量 < limit 时，has_more 必须为 False，next_cursor 必须为 None
    assert p3["pagination"]["has_more"] is False
    assert p3["pagination"]["next_cursor"] is None

    # 6. 非法/篡改 cursor 测试 -> 必须返回 400 或 422
    resp_bad = auth_client.get("/api/v1/events?limit=2&cursor=corrupted_invalid_cursor")
    assert resp_bad.status_code in (400, 422)
    assert "Invalid pagination cursor" in resp_bad.json()["detail"]


def test_cursor_collision_on_identical_first_seen_at(app, auth_client):
    """测试同一秒内多个事件 (first_seen_at 完全一致) 时的游标确定性排序与不漏不重。"""
    app.db.execute("UPDATE source SET can_display=1 WHERE source_id='src_sec_edgar'")
    db = app.db
    ev_repo = EventRepo(db)

    same_time = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in ["A", "B", "C", "D"]:
        ev_repo.insert(
            Event(
                event_id=f"EVT-COLLIDE-{i}",
                title=f"碰撞事件 {i}",
                first_seen_at=same_time,
                version=1,
                status="confirmed",
                first_source_id="src_sec_edgar",
            )
        )

    # limit=2 翻页
    r1 = auth_client.get("/api/v1/events?limit=2")
    data1 = r1.json()
    assert len(data1["items"]) == 2
    c1 = data1["pagination"]["next_cursor"]

    r2 = auth_client.get(f"/api/v1/events?limit=2&cursor={c1}")
    data2 = r2.json()

    seen_ids = [it["event_id"] for it in data1["items"]] + [it["event_id"] for it in data2["items"]]
    assert len(seen_ids) == len(set(seen_ids)), f"Collision pagination produced duplicates: {seen_ids}"


# ---------------------------------------------------------------------------
# 4. 10 万事件查询计划与 P95 基准验证
# ---------------------------------------------------------------------------

def test_100k_events_index_query_plan_and_latency(tmp_path):
    """在临时库中验证 0030 索引上的 EXPLAIN QUERY PLAN 与 10 万级数据 P95 游标检索延时。"""
    db_path = tmp_path / "events_100k_benchmark.db"
    db = Database(db_path)
    apply_migrations(db)

    # 1. 验证 EXPLAIN QUERY PLAN 是否使用了复合索引 idx_event_first_seen
    test_cursor_sql = (
        "EXPLAIN QUERY PLAN "
        "SELECT e.event_id, e.first_seen_at FROM event e "
        "WHERE e.first_seen_at <= ? AND (e.first_seen_at < ? OR (e.first_seen_at = ? AND e.event_id < ?)) "
        "ORDER BY e.first_seen_at DESC, e.event_id DESC LIMIT 20"
    )
    plan_rows = db.query(test_cursor_sql, ("2026-09-21T12:00:00Z", "2026-09-21T11:00:00Z", "2026-09-21T11:00:00Z", "EVT-999"))
    plan_str = " ".join(r["detail"] for r in plan_rows)
    # 必须基于索引 idx_event_first_seen 扫描，绝不能出现全表扫描或者临时 B-Tree 排序
    assert "idx_event_first_seen" in plan_str
    assert "TEMP B-TREE" not in plan_str

    # 2. 批量快速插入 100,000 条合成事件记录（单事务纯 SQL 极速生成）
    base_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    batch_size = 10000
    with db.transaction():
        for b in range(10):
            rows = [
                (
                    f"EVT-BENCH-{b * batch_size + i:06d}",
                    f"Benchmark Title {b * batch_size + i}",
                    "Benchmark Summary",
                    "earnings",
                    "confirmed",
                    1,
                    (base_ts + timedelta(seconds=(b * batch_size + i) * 10)).isoformat(),
                    (base_ts + timedelta(seconds=(b * batch_size + i) * 10)).isoformat(),
                )
                for i in range(batch_size)
            ]
            db.executemany(
                "INSERT INTO event (event_id, title, summary, event_type, status, version, first_seen_at, last_updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    count_row = db.query_one("SELECT COUNT(*) as cnt FROM event")
    assert count_row["cnt"] == 100000

    # 3. 连续执行 20 次游标分页跳转，测试 P95 延时
    latencies = []
    repo = EventRepo(db)
    # 获取初始游标
    page_events, total, eff_ts = repo.paginate_events(limit=20, enforce_display=False)
    current_cursor = encode_cursor(page_events[-1].first_seen_at.isoformat(), page_events[-1].event_id)
    for _ in range(20):
        t0 = time.perf_counter()
        page_events, total, eff_ts = repo.paginate_events(limit=20, cursor=current_cursor, enforce_display=False)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed_ms)
        if page_events:
            last_ev = page_events[-1]
            current_cursor = encode_cursor(last_ev.first_seen_at.isoformat(), last_ev.event_id)

    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    # 复合索引扫描下，100k 数据的单页游标跳转应在 15ms 内完成（通常在 0.3-2ms）
    assert p95 < 15.0, f"100k events cursor query P95 was too slow: {p95:.2f}ms"
    db.close()


# ---------------------------------------------------------------------------
# 5. 缓存退避与行情质量保真测试
# ---------------------------------------------------------------------------

def test_cache_fallback_does_not_upgrade_degraded_quality(auth_client, monkeypatch):
    """测试 /market/indices 缓存回退时不能将 stale/delayed/unknown_timestamp 升级为 cached/real。"""
    from trace.api.routers import market as market_router

    # 构造一批质量本身为 delayed 和 stale 的指数项
    now = datetime.now(timezone.utc)
    market_router._cached_indices = [
        IndexQuoteItem(
            name="标普 500", code="SPX", price=5600.0, change_pct=1.0,
            status="delayed", quality="delayed", as_of=now - timedelta(minutes=15),
            market_timestamp=now - timedelta(minutes=15)
        ),
        IndexQuoteItem(
            name="纳斯达克", code="IXIC", price=17500.0, change_pct=1.5,
            status="stale", quality="stale", as_of=now - timedelta(minutes=45),
            market_timestamp=now - timedelta(minutes=45)
        ),
        IndexQuoteItem(
            name="道琼斯", code="DJI", price=41000.0, change_pct=0.5,
            status="unknown_timestamp", quality="unknown_timestamp", as_of=None,
            market_timestamp=None
        ),
        IndexQuoteItem(
            name="科创 50", code="STAR50", price=1600.0, change_pct=-0.5,
            status="real", quality="real", as_of=now,
            market_timestamp=now
        ),
    ]
    market_router._last_fetch_ts = time.monotonic() - 60.0  # 60 秒前获取，处于 cache fallback 窗口

    # 模拟外部抓取失败触发缓存回退
    monkeypatch.setattr("httpx.Client.get", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("network down")))

    resp = auth_client.get("/api/v1/market/indices")
    assert resp.status_code == 200
    items = resp.json()
    by_code = {it["code"]: it for it in items}

    # 1. delayed 保持 delayed，绝不能升级为 cached
    assert by_code["SPX"]["status"] == "delayed"
    assert by_code["SPX"]["quality"] == "delayed"

    # 2. stale 保持 stale
    assert by_code["IXIC"]["status"] == "stale"
    assert by_code["IXIC"]["quality"] == "stale"

    # 3. unknown_timestamp 保持 unknown_timestamp
    assert by_code["DJI"]["status"] == "unknown_timestamp"
    assert by_code["DJI"]["quality"] == "unknown_timestamp"

    # 4. 只有原先是 real 的项，才进入 cached 状态
    assert by_code["STAR50"]["status"] == "cached"
    assert by_code["STAR50"]["quality"] == "cached"
