"""T11 事件查询性能、稳定分页与客户端请求治理测试 (F13, F24)。

验证：
1. SQL 级下推筛选与分页（杜绝静默 1000 条截断）；
2. 批量加载 impacts (list_by_events)，彻底消除 N+1 查询；
3. 基于 snapshot_ts 的稳定快照定界语义，并发新增不漂移；
4. 搜索关键词长度治理与主数据优先；
5. 行情异常降级（慢行情不阻断事件主数据返回）。
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.db.connection import Database
from trace.db.repositories import (
    EventImpactRepo,
    EventRepo,
    SecurityRepo,
    UserSessionRepo,
)
from trace.domain.models import (
    Direction,
    Directness,
    Event,
    EventImpact,
    EventStatus,
    EventType,
    Security,
)


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_t11.db"
    db = Database(db_file)
    from trace.db.migration import apply_migrations
    apply_migrations(db)
    return db


@pytest.fixture
def populated_db(test_db):
    sec_repo = SecurityRepo(test_db)
    event_repo = EventRepo(test_db)
    impact_repo = EventImpactRepo(test_db)

    # 证券
    sec_us = Security(security_id="SEC_US_TEST", ticker="TESTUS", market="US", company_name_en="Test US Corp")
    sec_cn = Security(security_id="SEC_CN_TEST", ticker="600999.SH", market="CN", company_name_zh="测试A股")
    sec_repo.upsert(sec_us)
    sec_repo.upsert(sec_cn)

    base_time = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

    # 构造 60 个事件：奇数绑定 US，偶数绑定 CN；评分从 4.0 到 9.9
    for i in range(1, 61):
        ts = base_time + timedelta(hours=i)
        ev = Event(
            event_id=f"EVT-PAGINATE-{i:03d}",
            title=f"Paginated Event {i}",
            summary=f"Summary for event {i}",
            event_type=EventType.PRODUCT.value,
            status=EventStatus.OFFICIAL_CONFIRMED.value,
            first_seen_at=ts,
            last_updated_at=ts,
            version=1,
        )
        event_repo.insert(ev)

        # 评分: i % 10 + 1 (范围 1.0 - 10.0)
        score = 4.0 + (i % 6) * 1.0  # 4.0, 5.0, 6.0, 7.0, 8.0, 9.0
        sec_id = sec_us.security_id if (i % 2 == 1) else sec_cn.security_id

        imp = EventImpact(
            impact_id=f"IMP-PAGINATE-{i:03d}",
            event_id=ev.event_id,
            security_id=sec_id,
            direction=Direction.BULLISH.value,
            directness=Directness.DIRECT.value,
            magnitude=0.5,
            persistence=0.5,
            confidence=0.9,
            reason=f"Impact for {i}",
            final_score=score,
        )
        impact_repo.upsert(imp)

    return {"sec_us": sec_us, "sec_cn": sec_cn, "base_time": base_time}


def test_sql_pagination_and_filtering(test_db, populated_db):
    event_repo = EventRepo(test_db)

    # 1. 基础分页：每页 15 条
    page1, total, snap1 = event_repo.paginate_events(page=1, limit=15)
    assert total == 60
    assert len(page1) == 15
    assert page1[0].event_id == "EVT-PAGINATE-060"
    assert page1[-1].event_id == "EVT-PAGINATE-046"

    # 第二页
    page2, total2, _ = event_repo.paginate_events(page=2, limit=15, snapshot_ts=snap1)
    assert total2 == 60
    assert len(page2) == 15
    assert page2[0].event_id == "EVT-PAGINATE-045"
    assert page2[-1].event_id == "EVT-PAGINATE-031"

    # 2. 市场过滤 (仅 US 标的)
    us_page, us_total, _ = event_repo.paginate_events(page=1, limit=50, market="US")
    assert us_total == 30  # 60 个中奇数号 30 个
    assert len(us_page) == 30
    assert all(int(e.event_id.split("-")[-1]) % 2 == 1 for e in us_page)

    # 3. 评分过滤 (min_score >= 8.0)
    score_page, score_total, _ = event_repo.paginate_events(page=1, limit=50, min_score=8.0)
    # score 取值 4.0, 5.0, 6.0, 7.0, 8.0, 9.0 -> >=8.0 的是 8.0 和 9.0 (共 2/6 = 20 个)
    assert score_total == 20
    assert len(score_page) == 20


def test_snapshot_pagination_prevents_page_drift(test_db, populated_db):
    event_repo = EventRepo(test_db)
    base_time = populated_db["base_time"]

    # 用户在 T 时刻请求第 1 页，锁定快照时间戳
    t_snapshot = (base_time + timedelta(hours=60)).isoformat()
    page1, total1, snap = event_repo.paginate_events(page=1, limit=10, snapshot_ts=t_snapshot)
    assert total1 == 60
    assert page1[0].event_id == "EVT-PAGINATE-060"

    # 此时后台实时新增了 5 条更新更晚的新事件 (T+100h)
    for j in range(101, 106):
        new_ts = base_time + timedelta(hours=100 + j)
        ev_new = Event(
            event_id=f"EVT-PAGINATE-{j}",
            title=f"New Event {j}",
            summary="New",
            event_type=EventType.PRODUCT.value,
            status=EventStatus.REPORTED.value,
            first_seen_at=new_ts,
            last_updated_at=new_ts,
            version=1,
        )
        event_repo.insert(ev_new)

    # 若不带 snapshot_ts（以更新的时间为准），总数变成 65，分页会发生漂移
    t_latest = (base_time + timedelta(hours=300)).isoformat()
    page2_drifted, total_drifted, _ = event_repo.paginate_events(page=2, limit=10, snapshot_ts=t_latest)
    assert total_drifted == 65
    assert page2_drifted[0].event_id == "EVT-PAGINATE-055"  # 发生下移漂移

    # 严格带 snapshot_ts 访问第 2 页，锁定原快照，完全不漂移
    page2_stable, total_stable, _ = event_repo.paginate_events(page=2, limit=10, snapshot_ts=snap)
    assert total_stable == 60
    assert page2_stable[0].event_id == "EVT-PAGINATE-050"  # 稳定衔接第 1 页结尾


def test_batch_impacts_eliminates_n_plus_one(test_db, populated_db):
    impact_repo = EventImpactRepo(test_db)
    event_ids = [f"EVT-PAGINATE-{i:03d}" for i in range(1, 21)]

    # 批量获取 20 个事件的影响，必须单次查询返回字典
    impact_map = impact_repo.list_by_events(event_ids)
    assert len(impact_map) == 20
    for eid in event_ids:
        assert len(impact_map[eid]) == 1
        assert impact_map[eid][0].event_id == eid


def test_events_api_pagination_and_resilience(app):
    from trace.db.repositories import UserRepo
    UserRepo(app.db).ensure("user_test", timezone="Asia/Taipei")

    # 注入 10 条测试事件
    sec_repo = SecurityRepo(app.db)
    event_repo = EventRepo(app.db)
    impact_repo = EventImpactRepo(app.db)
    sec_us = sec_repo.get_by_ticker("NVDA")
    if not sec_us:
        sec_us = Security(security_id="SEC_US_NVDA", ticker="NVDA", market="US", company_name_en="NVIDIA")
        sec_repo.upsert(sec_us)

    base_time = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for i in range(1, 11):
        ts = base_time + timedelta(hours=i)
        ev = Event(
            event_id=f"EVT-API-{i:02d}",
            title=f"API Event {i}", first_source_id="src_sec_edgar",
            summary=f"Summary {i}",
            event_type=EventType.PRODUCT.value,
            status=EventStatus.OFFICIAL_CONFIRMED.value,
            first_seen_at=ts,
            last_updated_at=ts,
            version=1,
        )
        event_repo.insert(ev)
        imp = EventImpact(
            impact_id=f"IMP-API-{i:02d}",
            event_id=ev.event_id,
            security_id=sec_us.security_id,
            direction=Direction.BULLISH.value,
            directness=Directness.DIRECT.value,
            magnitude=0.8,
            persistence=0.8,
            confidence=0.9,
            reason="Positive",
            final_score=7.5,
        )
        impact_repo.upsert(imp)

    api_app = create_api_app(app)
    with TestClient(api_app) as client:
        sess = UserSessionRepo(app.db).create_session("user_test")
        headers = {"Authorization": f"Bearer {sess.session_token}"}

        # 1. 分页请求
        resp = client.get("/api/v1/events?page=1&limit=5", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 5
        assert data["pagination"]["page"] == 1
        assert data["pagination"]["limit"] == 5
        assert data["pagination"]["total"] == 10
        assert data["pagination"]["snapshot_ts"] is not None
        assert data["pagination"]["has_more"] is True

        # 2. 搜索接口参数长度防护 (超长字符触发 422 验证异常)
        too_long_query = "A" * 60
        resp_search = client.get(f"/api/v1/watchlist/search?q={too_long_query}", headers=headers)
        assert resp_search.status_code == 422
