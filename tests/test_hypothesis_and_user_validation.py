"""假设跟踪与真实用户验证测试 (T16 / F31 / F32 / Gate G3)。

验证核心：
1. 假设草案智能提取与生成 (POST /research/questions/draft)：
   - 基于事件摘要与传导影响自动推导支持条件、反证条件与核验时间窗口；
2. 假设全生命周期管理与新证据关联 (F31)：
   - 创建跟踪 -> 列表筛选 -> 新增关联证据 ("可能相关，需要复核") -> 状态推进 (confirmed/falsified/archived) -> 删除；
3. 多租户严格授权隔离与公开分享脱敏：
   - 用户 B 无法读取、更新或删除用户 A 的假设 (403 Forbidden)；
   - 公开分享端点 (/questions/{id}/share) 严格剥离个人私有备注 (user_notes 绝不泄露)；
4. 提醒有效性与原因反馈闭环 (F32)：
   - 记录 "这条提醒没用" / too_late / irrelevant / duplicate 反馈；
   - 个人与系统级别聚合统计 (useful_rate, 细分原因分布)；
5. 可移植研究资产导出 (GET /research/export)：
   - 满足用户随时导出自有研究记录与反馈明细。
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.common.ids import event_id, raw_item_id
from trace.db.repositories import (
    AlertFeedbackRepo,
    EventImpactRepo,
    EventRepo,
    ResearchQuestionRepo,
)
from trace.domain.models import AlertFeedback, Event, EventImpact, ResearchQuestion


@pytest.fixture
def client(app):
    api_app = create_api_app(ctx=app)
    with TestClient(api_app) as c:
        yield c


@pytest.fixture
def alice_headers(client):
    resp = client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "alice_t16"})
    assert resp.status_code == 200
    token = resp.json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bob_headers(client):
    resp = client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "bob_t16"})
    assert resp.status_code == 200
    token = resp.json()["session_token"]
    return {"Authorization": f"Bearer {token}"}


def _seed_test_event(app, eid: str = "EVT-T16-NVDA"):
    now = datetime.now(timezone.utc)
    ev = Event(
        event_id=eid,
        title="英伟达发布新一代 AI 超级芯片架构",
        summary="英伟达正式宣布新一代 Blackwell Ultra 芯片，大幅提高能效比，锁定大厂首批订单。",
        event_type="product_breakthrough",
        status="verified",
        first_source_id="src_sec_edgar",
        first_seen_at=now,
        last_updated_at=now,
    )
    app.db.execute("UPDATE source SET can_display=1, can_forward=1 WHERE source_id='src_sec_edgar'")
    EventRepo(app.db).insert(ev)

    imp = EventImpact(
        impact_id=f"IMP-{eid}-NVDA",
        event_id=eid,
        security_id="SEC-US-NVDA",
        direction="bullish",
        directness="direct",
        magnitude=0.85,
        persistence=0.9,
        confidence=0.88,
        reason="算力龙头地位强化，先进制程高毛利贡献增加",
        industry_path="半导体设计 -> GPU/AI加速器",
        base_score=8.5,
        final_score=8.5,
    )
    EventImpactRepo(app.db).upsert(imp)
    return ev, imp


def test_unauthenticated_research_endpoints_rejected(client):
    """受保护的研究假设与反馈接口未携带凭据必须返回 401。"""
    assert client.get("/api/v1/research/questions").status_code == 401
    assert client.post("/api/v1/research/questions", json={"title": "t", "hypothesis": "h"}).status_code == 401
    assert client.post("/api/v1/research/feedback", json={"event_id": "e", "rating": "useful"}).status_code == 401
    assert client.get("/api/v1/research/export").status_code == 401


def test_hypothesis_draft_generation(client, app, alice_headers):
    """测试基于事件与影响自动生成研究假设草案 (POST /research/questions/draft)。"""
    ev, imp = _seed_test_event(app, "EVT-DRAFT-01")

    # 1. 成功生成草案
    resp = client.post(
        "/api/v1/research/questions/draft",
        json={"event_id": ev.event_id, "security_id": "SEC-US-NVDA"},
        headers=alice_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_id"] == ev.event_id
    assert data["security_id"] == "SEC-US-NVDA"
    assert "英伟达" in data["title"]
    assert "Blackwell Ultra" in data["hypothesis"]
    assert len(data["supporting_conditions"]) >= 1
    assert any("SEC-US-NVDA" in c and "bullish" in c for c in data["supporting_conditions"])
    assert len(data["contradicting_conditions"]) == 1
    assert "请填写" in data["contradicting_conditions"][0]
    assert data["next_check_at"] is not None

    # 2. 事件不存在返回 404
    resp_404 = client.post(
        "/api/v1/research/questions/draft",
        json={"event_id": "EVT-NON-EXISTENT"},
        headers=alice_headers,
    )
    assert resp_404.status_code == 404


def test_hypothesis_lifecycle_and_evidence_matching(client, app, alice_headers):
    """测试假设创建、查询、新证据关联 ('可能相关，需要复核') 与状态推进。"""
    ev, _ = _seed_test_event(app, "EVT-LIFECYCLE-01")

    # 1. 创建跟踪
    create_payload = {
        "event_id": ev.event_id,
        "security_id": "SEC-US-NVDA",
        "title": "验证英伟达新一代芯片订单持续放量",
        "hypothesis": "Blackwell Ultra 将在下半年贡献超过 20% 营收",
        "supporting_conditions": ["头部云厂商全量追加投片", "产业链交付指引上调"],
        "contradicting_conditions": ["供应链封装产能受限推迟交付", "竞对方案低价抢占市场"],
        "user_notes": "私人研究备忘：重点盯下季度财报电话会的 Capex 调整指引",
    }
    resp_create = client.post(
        "/api/v1/research/questions",
        json=create_payload,
        headers=alice_headers,
    )
    assert resp_create.status_code == 200
    q_data = resp_create.json()
    qid = q_data["question_id"]
    assert qid.startswith("RQ-")
    assert q_data["user_id"] == "alice_t16"
    assert q_data["state"] == "tracking"
    assert q_data["user_notes"] == create_payload["user_notes"]
    assert q_data["matched_evidence_ids"] == []

    # 2. 列表查询
    resp_list = client.get("/api/v1/research/questions?state=tracking", headers=alice_headers)
    assert resp_list.status_code == 200
    assert resp_list.json()["total"] >= 1
    assert any(item["question_id"] == qid for item in resp_list.json()["items"])

    # 3. 单条详情
    resp_get = client.get(f"/api/v1/research/questions/{qid}", headers=alice_headers)
    assert resp_get.status_code == 200
    assert resp_get.json()["question_id"] == qid

    from trace.db.repositories import RawItemRepo
    from trace.domain.models import RawItem
    for rid in ('RAW-ITEM-NEW-01','RAW-ITEM-AUTO-02'):
        RawItemRepo(app.db).insert(RawItem(raw_item_id=rid,source_id='src_sec_edgar',event_id=ev.event_id,title='New evidence',content='Evidence'))
    # 4. 手动关联新证据 ('可能相关，需要复核')
    resp_evid = client.post(
        f"/api/v1/research/questions/{qid}/evidence",
        json={"raw_item_id": "RAW-ITEM-NEW-01"},
        headers=alice_headers,
    )
    assert resp_evid.status_code == 200
    assert "RAW-ITEM-NEW-01" in resp_evid.json()["matched_evidence_ids"]

    # 5. 仓储层自动匹配新证据测试 (match_new_evidence)
    repo = ResearchQuestionRepo(app.db)
    matched = repo.match_new_evidence(
        raw_item_id="RAW-ITEM-AUTO-02",
        event_id=ev.event_id,
        security_id="SEC-US-NVDA",
    )
    assert len(matched) >= 1
    assert any(m.question_id == qid for m in matched)

    # 刷新确认
    resp_get2 = client.get(f"/api/v1/research/questions/{qid}", headers=alice_headers)
    assert "RAW-ITEM-AUTO-02" in resp_get2.json()["matched_evidence_ids"]

    # 6. 更新状态与私有备注 (推进至 confirmed)
    patch_payload = {
        "state": "confirmed",
        "user_notes": "最新证据验证：苹果已锁定 100% 首发产能，假设成立！",
    }
    resp_patch = client.patch(
        f"/api/v1/research/questions/{qid}",
        json=patch_payload,
        headers=alice_headers,
    )
    assert resp_patch.status_code == 200
    assert resp_patch.json()["state"] == "confirmed"
    assert resp_patch.json()["user_notes"] == patch_payload["user_notes"]

    # 7. 非法状态测试
    resp_invalid = client.patch(
        f"/api/v1/research/questions/{qid}",
        json={"state": "invalid_status"},
        headers=alice_headers,
    )
    assert resp_invalid.status_code == 400

    # 8. 删除假设
    resp_del = client.delete(f"/api/v1/research/questions/{qid}", headers=alice_headers)
    assert resp_del.status_code == 200
    assert resp_del.json()["ok"] is True

    resp_after_del = client.get(f"/api/v1/research/questions/{qid}", headers=alice_headers)
    assert resp_after_del.status_code == 404


def test_tenant_isolation_and_public_share_desensitization(client, alice_headers, bob_headers):
    """测试多租户对象授权隔离与公开分享脱敏 (私有备注绝不泄露)。"""
    # 1. Alice 创建包含私密研究备注的假设
    resp_create = client.post(
        "/api/v1/research/questions",
        json={
            "title": "绝密：某龙头并购推演",
            "hypothesis": "A 公司将在下月收购 B 公司",
            "user_notes": "顶级私密操作策略：已建立看涨期权头寸，目标价 $250",
        },
        headers=alice_headers,
    )
    assert resp_create.status_code == 200
    alice_qid = resp_create.json()["question_id"]

    # 2. Bob 尝试直接越权获取 Alice 的假设详情 -> 403 Forbidden
    resp_bob_get = client.get(f"/api/v1/research/questions/{alice_qid}", headers=bob_headers)
    assert resp_bob_get.status_code == 403
    assert "Forbidden" in resp_bob_get.json()["detail"]

    # 3. Bob 尝试越权修改 Alice 的假设 -> 403 Forbidden
    resp_bob_patch = client.patch(
        f"/api/v1/research/questions/{alice_qid}",
        json={"state": "falsified"},
        headers=bob_headers,
    )
    assert resp_bob_patch.status_code == 403

    # 4. Bob 尝试越权删除 Alice 的假设 -> 403 Forbidden
    resp_bob_del = client.delete(f"/api/v1/research/questions/{alice_qid}", headers=bob_headers)
    assert resp_bob_del.status_code == 403

    # 5. Bob 的列表中完全不包含 Alice 的假设
    resp_bob_list = client.get("/api/v1/research/questions", headers=bob_headers)
    assert resp_bob_list.status_code == 200
    assert not any(item["question_id"] == alice_qid for item in resp_bob_list.json()["items"])

    # 6. 公开分享视图 (GET /questions/{id}/share)：
    # 允许公开展示结构化推断，但严格脱敏剥离个人私有备注 (user_notes 必须不存在)
    path = f"/api/v1/research/questions/{alice_qid}/share"
    assert client.get(path).status_code == 404
    assert client.post(path,headers=bob_headers,json={'expires_in_days':1}).status_code == 404
    capability = client.post(path,headers=alice_headers,json={'expires_in_days':1}).json()
    resp_share = client.get(path,params={'token':capability['share_token']})
    assert resp_share.status_code == 200
    share_data = resp_share.json()
    assert share_data["question_id"] == alice_qid
    assert share_data["title"] == "绝密：某龙头并购推演"
    assert share_data["hypothesis"] == "A 公司将在下月收购 B 公司"
    # 严格校验：公开分享响应体中绝不能有 user_notes 键，防止私有信息外泄
    assert "user_notes" not in share_data


def test_alert_feedback_and_summary_aggregation(client, app, alice_headers, bob_headers, monkeypatch):
    """测试提醒反馈记录 ('这条提醒没用' / 细分原因) 与多维度聚合统计。"""
    ev1, _ = _seed_test_event(app, "EVT-FB-01")
    ev2, _ = _seed_test_event(app, "EVT-FB-02")
    ev3, _ = _seed_test_event(app, "EVT-FB-03")

    # 1. Alice 提交正向反馈
    r1 = client.post(
        "/api/v1/research/feedback",
        json={"event_id": ev1.event_id, "security_id": "SEC-US-NVDA", "rating": "useful", "reason": "预警及时，抓住了盘前变动"},
        headers=alice_headers,
    )
    assert r1.status_code == 200
    assert r1.json()["rating"] == "useful"

    # 2. Alice 提交负向反馈：太晚
    r2 = client.post(
        "/api/v1/research/feedback",
        json={"event_id": ev2.event_id, "security_id": "SEC-US-NVDA", "rating": "too_late", "reason": "市场已开盘 20 分钟后才推送"},
        headers=alice_headers,
    )
    assert r2.status_code == 200

    # 3. Bob 提交负向反馈：无关
    r3 = client.post(
        "/api/v1/research/feedback",
        json={"event_id": ev3.event_id, "rating": "irrelevant", "reason": "未持有该标的也不关注该子行业"},
        headers=bob_headers,
    )
    assert r3.status_code == 200

    # 4. 非法评分值
    r_bad = client.post(
        "/api/v1/research/feedback",
        json={"event_id": ev1.event_id, "rating": "unknown_rating"},
        headers=alice_headers,
    )
    assert r_bad.status_code == 400

    # 5. Alice 查看个人反馈汇总 (scope=my)
    resp_alice_summary = client.get("/api/v1/research/feedback/summary?scope=my", headers=alice_headers)
    assert resp_alice_summary.status_code == 200
    s_alice = resp_alice_summary.json()
    assert s_alice["total"] == 2
    assert s_alice["useful_count"] == 1
    assert s_alice["not_useful_count"] == 1
    assert s_alice["useful_rate"] == 0.5
    assert s_alice["by_rating"]["useful"] == 1
    assert s_alice["by_rating"]["too_late"] == 1

    # 6. Bob 查看个人反馈汇总 (scope=my)
    resp_bob_summary = client.get("/api/v1/research/feedback/summary?scope=my", headers=bob_headers)
    assert resp_bob_summary.status_code == 200
    s_bob = resp_bob_summary.json()
    assert s_bob["total"] == 1
    assert s_bob["useful_count"] == 0
    assert s_bob["by_rating"]["irrelevant"] == 1

    # 7. 查看全系统反馈汇总 (scope=all)
    assert client.get('/api/v1/research/feedback/summary?scope=all',headers=alice_headers).status_code == 403
    monkeypatch.setenv('TRACE_ADMIN_USER_IDS','alice_t16')
    resp_all_summary = client.get("/api/v1/research/feedback/summary?scope=all", headers=alice_headers)
    assert resp_all_summary.status_code == 200
    s_all = resp_all_summary.json()
    assert s_all["total"] == 3
    assert s_all["useful_count"] == 1
    assert s_all["not_useful_count"] == 2
    assert s_all["useful_rate"] == 0.3333


def test_portable_research_export(client, app, alice_headers, bob_headers):
    """测试可移植研究资产导出 (GET /research/export)，验证资产归属完整性与用户边界。"""
    _seed_test_event(app, "EVT-EXP-01")
    _seed_test_event(app, "EVT-EXP-02")

    # 1. Alice 创建 1 个假设与 1 个反馈
    client.post(
        "/api/v1/research/questions",
        json={"title": "Alice 假设", "hypothesis": "测试导出假设", "user_notes": "私密 note"},
        headers=alice_headers,
    )
    client.post(
        "/api/v1/research/feedback",
        json={"event_id": "EVT-EXP-01", "rating": "useful", "reason": "好评"},
        headers=alice_headers,
    )

    # 2. Bob 创建 1 个假设与 1 个反馈
    client.post(
        "/api/v1/research/questions",
        json={"title": "Bob 假设", "hypothesis": "Bob 的专属假设"},
        headers=bob_headers,
    )
    client.post(
        "/api/v1/research/feedback",
        json={"event_id": "EVT-EXP-02", "rating": "duplicate", "reason": "重复内容"},
        headers=bob_headers,
    )

    # 3. Alice 导出
    resp_exp = client.get("/api/v1/research/export", headers=alice_headers)
    assert resp_exp.status_code == 200
    data = resp_exp.json()
    assert data["user_id"] == "alice_t16"
    assert len(data["questions"]) >= 1
    assert any(q["title"] == "Alice 假设" for q in data["questions"])
    assert not any(q["title"] == "Bob 假设" for q in data["questions"])

    assert len(data["feedbacks"]) >= 1
    assert any(f["event_id"] == "EVT-EXP-01" for f in data["feedbacks"])
    assert not any(f["event_id"] == "EVT-EXP-02" for f in data["feedbacks"])
    assert "exported_at" in data
