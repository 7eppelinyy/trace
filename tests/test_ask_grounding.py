"""测试证据约束问答、情景推演与真实图谱拓扑 (F05/F08/F25/F28 & A13)。

验收原则：
1. 证据约束模式 (evidence_answer)：无证据时明确拒答 (insufficient_evidence)，提供可执行核验步骤，不编造业务/行情事实。
2. 情景推演模式 (scenario)：显式标识假设前提，标记 status=scenario_simulation，不进入事实流。
3. 动态传导图谱：按真实拓扑 hop 展开，无假四级套话，无证据时为空列表。
4. 事件锚定 (event_id/version)：直接基于指定事件与版本提取证据并生成事实 Claims。
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from trace.alerts.ask import AskEngine
from trace.api.app import create_api_app
from trace.common.ids import event_id as make_event_id, raw_item_id as make_raw_id
from trace.db.repositories import (
    EventImpactRepo,
    EventRepo,
    EventSourceRepo,
    RawItemRepo,
    SecurityRepo,
)
from trace.domain.models import Event, EventImpact, EventSource, RawItem, Security
from trace.graph.industry_graph import IndustryGraph


@pytest.fixture
def client(app):
    """FastAPI 测试客户端，附带合法会话凭据。"""
    api_app = create_api_app(ctx=app)
    with TestClient(api_app) as c:
        auth_resp = c.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "test_user"})
        assert auth_resp.status_code == 200
        token = auth_resp.json()["session_token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def test_ask_no_evidence_refusal(app):
    """A13: 无相关事实证据时返回 insufficient_evidence，不生成虚构事实。"""
    engine = app.ask_engine
    res = engine.ask(ticker="NVDA", question="评估近期突发停电对英伟达工厂出货的具体影响", mode="evidence_answer")
    assert res is not None
    assert res.status == "insufficient_evidence"
    assert "【证据不足】" in res.text
    assert "建议核验步骤" in res.text
    assert len(res.candidates) == 0
    assert len(res.graph_chain) == 0
    assert len(res.claims) == 0


def test_ask_scenario_mode_with_assumptions(app):
    """A13: 用户显式选择 scenario 模式时，即使无事实证据，也允许基于假设推演，并明确标出情景标记。"""
    engine = app.ask_engine
    res = engine.ask(ticker="NVDA", question="若美国进一步收紧芯片出口限制，对英伟达算力供应链的冲击几何？", mode="scenario")
    assert res is not None
    assert res.status == "scenario_simulation"
    assert "【情景假设推演（非已发生事实）】" in res.text
    assert len(res.graph_chain) > 0
    # 验证图谱节点标记为假设
    assert any("假设" in node.get("title", "") or "假设" in node.get("desc", "") for node in res.graph_chain)
    # 验证 Claims 为 scenario 类型
    assert len(res.claims) > 0
    assert all(c.get("kind") == "scenario" for c in res.claims)


def test_ask_pinned_event_grounding(app):
    """测试指定 event_id / event_version 时的证据锚定与结构化 Claims。"""
    db = app.db
    sec_repo = SecurityRepo(db)
    ev_repo = EventRepo(db)
    imp_repo = EventImpactRepo(db)
    raw_repo = RawItemRepo(db)
    src_repo = EventSourceRepo(db)

    # 1. 获取或注入证券
    sec = sec_repo.get_by_ticker("MU")
    if sec is None:
        sec = Security(
            security_id="SEC-US-MU",
            ticker="MU",
            market="US",
            company_name_zh="美光科技",
            company_name_en="Micron Technology",
            graph_node_ids=["mu", "nand_flash"],
        )
        sec_repo.upsert(sec)

    # 2. 注入真实事件与 RawItem
    r_id = make_raw_id()
    raw = RawItem(
        raw_item_id=r_id,
        source_id="src_sec_edgar",
        source_item_id="sec-edgar-mu-001",
        title="Micron raises Q3 revenue guidance",
        content="Micron Technology raises its revenue guidance due to surging demand for HBM3E memory.",
        url="https://sec.gov/micron-hbm-guidance",
        fetched_at=datetime.now(timezone.utc),
    )
    raw_repo.insert(raw)

    e_id = make_event_id()
    ev = Event(
        event_id=e_id,
        title="美光上调第三季度业绩指引，HBM 需求超预期",
        summary="美光科技因 HBM3E 内存芯片供不应求，官方上调季度营收指引。",
        event_time=datetime.now(timezone.utc),
        status="confirmed",
        first_source_id="src_sec_edgar",
        version=1,
    )
    ev_repo.insert(ev)
    src_repo.add(EventSource(event_id=e_id, raw_item_id=r_id, role="primary"))

    imp = EventImpact(
        impact_id=f"imp_{e_id}",
        event_id=e_id,
        security_id=sec.security_id,
        direction="bullish",
        confidence=0.92,
        final_score=8.8,
        reason="HBM3E 产线满载并获得战略客户长单，直接增厚当季营业利润。",
    )
    imp_repo.upsert(imp)

    engine = app.ask_engine
    # 测试指定该事件
    res = engine.ask(ticker="MU", question="解读美光最新财报指引变动", event_id=e_id, event_version=1, mode="evidence_answer")
    assert res is not None
    assert res.status == "ok" or res.status == "degraded"
    assert len(res.candidates) == 1
    assert res.candidates[0].event.event_id == e_id
    assert len(res.citations) > 0
    assert "https://sec.gov/micron-hbm-guidance" in res.citations

    # 检查 Claims 包含事实与推断
    kinds = [c.get("kind") for c in res.claims]
    assert "fact" in kinds
    assert "inference" in kinds

    # 测试错误版本
    res_wrong_ver = engine.ask(ticker="MU", question="解读美光最新财报指引变动", event_id=e_id, event_version=999)
    assert res_wrong_ver.status == "insufficient_evidence"


def test_dynamic_graph_chain_hops(app):
    """测试传导图谱忠实于真实拓扑深度（不强凑四级）。"""
    db = app.db
    sec_repo = SecurityRepo(db)
    ev_repo = EventRepo(db)
    imp_repo = EventImpactRepo(db)
    raw_repo = RawItemRepo(db)
    src_repo = EventSourceRepo(db)

    # 注入一个孤立标的（无图谱邻居节点）
    sec_isolated = Security(
        security_id="sec_iso_01",
        ticker="ISO",
        market="US",
        company_name_zh="孤立科技",
        company_name_en="Isolated Inc",
        graph_node_ids=["iso_isolated"],
    )
    sec_repo.upsert(sec_isolated)

    r_id = make_raw_id()
    raw = RawItem(
        raw_item_id=r_id,
        source_id="src_sec_edgar",
        source_item_id="iso-001",
        title="Isolated Inc releases new product",
        content="Product released.",
        url="https://sec.gov/iso-001",
        fetched_at=datetime.now(timezone.utc),
    )
    raw_repo.insert(raw)

    e_id = make_event_id()
    ev = Event(
        event_id=e_id,
        title="孤立科技发布自研新品",
        summary="发布公告",
        event_time=datetime.now(timezone.utc),
        status="reported",
        first_source_id="src_sec_edgar",
        version=1,
    )
    ev_repo.insert(ev)
    src_repo.add(EventSource(event_id=e_id, raw_item_id=r_id, role="primary"))

    imp = EventImpact(
        impact_id=f"imp_{e_id}",
        event_id=e_id,
        security_id=sec_isolated.security_id,
        direction="bullish",
        confidence=0.8,
        final_score=6.0,
        reason="新品提升产品线单价。",
    )
    imp_repo.upsert(imp)

    engine = app.ask_engine
    res = engine.ask(ticker="ISO", question="评估孤立科技新品", event_id=e_id)
    assert res is not None
    # 因为该标的没有拓扑邻居，图谱必须严格为 2 级（L1: 事件来源, L2: 直接冲击），不得强行造出 L3/L4
    assert len(res.graph_chain) == 2
    assert res.graph_chain[0]["level"] == 1
    assert res.graph_chain[1]["level"] == 2



def test_api_ask_modes_and_grounding(client, app):
    """测试通过 FastAPI 接口调用 /ask：包含 mode、event_id、claims、citations 返回。"""
    # 1. 证据不足时
    resp = client.post(
        "/api/v1/ask",
        json={"ticker": "NVDA", "question": "请说明英伟达最近收购传闻的核验细节", "mode": "evidence_answer"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "insufficient_evidence"
    assert "【证据不足】" in data["answer"]
    assert data["graph_chain"] == []

    # 2. 情景模拟时
    resp2 = client.post(
        "/api/v1/ask",
        json={"ticker": "NVDA", "question": "推演下一代架构采用玻璃基板对供应链的影响", "mode": "scenario"},
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "scenario_simulation"
    assert "【情景假设推演" in data2["answer"]
    assert len(data2["graph_chain"]) > 0


def test_broadened_financial_scope_auto_scenario(client, app):
    """验证放宽金融推演问答范围：标的无事件时自动切入情景推演，无标的金融宏观议题亦顺利推演。"""
    # 1. 博通股价（已建立标的，但无即时突发事实）
    resp_avgo = client.post(
        "/api/v1/ask",
        json={"question": "怎么看待目前博通的股价？", "mode": "auto"},
    )
    assert resp_avgo.status_code == 200
    data_avgo = resp_avgo.json()
    assert data_avgo["status"] == "scenario_simulation"
    assert data_avgo["ticker"] == "AVGO"
    assert "AVGO" in data_avgo["answer"] or "博通" in data_avgo["answer"]
    assert len(data_avgo["graph_chain"]) > 0

    # 2. 宏观/Web3金融议题（美股链上交易）
    resp_onchain = client.post(
        "/api/v1/ask",
        json={"question": "评估美股链上交易的影响", "mode": "auto"},
    )
    assert resp_onchain.status_code == 200
    data_onchain = resp_onchain.json()
    assert data_onchain["status"] == "scenario_simulation"
    assert "【情景假设推演" in data_onchain["answer"]
    assert len(data_onchain["claims"]) > 0


