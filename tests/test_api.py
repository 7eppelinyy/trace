"""FastAPI RESTful API 端点单元测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.app import AppContext, create_app
from trace.common.ids import event_id, raw_item_id, revision_id
from trace.db.repositories import (
    EventImpactRepo,
    EventRepo,
    EventRevisionRepo,
    EventSourceRepo,
    RawItemRepo,
    SecurityRepo,
)
from trace.domain.models import Event, EventImpact, EventRevision, EventSource, RawItem, Security


@pytest.fixture
def test_app_ctx(db, config):
    """装配用于测试的隔离 AppContext。"""
    return create_app()


@pytest.fixture
def client(test_app_ctx):
    """FastAPI 测试客户端。"""
    api_app = create_api_app(ctx=test_app_ctx)
    with TestClient(api_app) as c:
        yield c


def test_root_endpoint(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "online"
    assert data["docs_url"] == "/docs"


def test_health_endpoint(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert "sources" in data
    assert len(data["sources"]) > 0


def test_status_endpoint(client):
    resp = client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "OK"
    assert "mode" in data
    assert "db_path" in data


def test_events_list_and_detail(client, test_app_ctx):
    db = test_app_ctx.db
    ev_id = event_id()
    now = datetime.now(timezone.utc)

    # 插入测试事件
    ev = Event(
        event_id=ev_id,
        title="测试半导体重大投资",
        summary="晶圆厂扩大先进产能投资",
        event_type="capacity",
        status="reported",
        version=1,
        first_seen_at=now,
        last_updated_at=now,
    )
    EventRepo(db).insert(ev)

    # 插入测试影响
    imp = EventImpact(
        impact_id=f"IMP-{ev_id}",
        event_id=ev_id,
        security_id="SEC-US-NVDA",

        direction="bullish",
        directness="direct",
        magnitude=8.0,
        persistence=7.0,
        confidence=0.9,
        reason="算力需求拉动",
        industry_path="AI -> GPU -> NVDA",
        final_score=8.5,
        base_score=8.0,
        market_confirmation=5.0,
    )
    EventImpactRepo(db).upsert(imp)


    # 插入证据与来源
    raw_id = raw_item_id()
    raw = RawItem(
        raw_item_id=raw_id,
        source_id="src_sec_edgar",
        title="SEC 8-K Filing",
        url="https://sec.gov/test",
        published_at=now,
    )
    RawItemRepo(db).insert(raw)
    EventSourceRepo(db).add(EventSource(event_id=ev_id, raw_item_id=raw_id, role="primary"))

    # 插入版本修订
    rev = EventRevision(
        revision_id=revision_id(),
        event_id=ev_id,
        version=1,
        revision_type="created",
        material_update=True,
        note="initial version",
        created_at=now,
    )
    EventRevisionRepo(db).add(rev)


    # 1) 测试列表
    resp = client.get("/api/v1/events?min_score=7.0")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) >= 1
    found = [item for item in data["items"] if item["event_id"] == ev_id]
    assert len(found) == 1
    assert found[0]["title"] == "测试半导体重大投资"
    assert found[0]["max_score"] == 8.5
    assert "SEC-US-NVDA" in found[0]["impacted_securities"]

    # 2) 测试详情
    resp_detail = client.get(f"/api/v1/events/{ev_id}")
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["event_id"] == ev_id
    assert len(detail["impacts"]) == 1
    assert detail["impacts"][0]["security_id"] == "SEC-US-NVDA"
    assert len(detail["evidences"]) == 1
    assert detail["evidences"][0]["url"] == "https://sec.gov/test"
    assert len(detail["revisions"]) == 1

    # 3) 测试不存在事件 404
    resp_404 = client.get("/api/v1/events/EV-nonexistent")
    assert resp_404.status_code == 404


def test_watchlist_flow(client):
    # 1) 添加合规美股代码
    resp = client.post("/api/v1/watchlist", json={"ticker": "NVDA"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "NVDA"
    assert data["market"] == "US"

    # 2) 添加合规 A 股代码
    resp_cn = client.post("/api/v1/watchlist", json={"ticker": "688981.SH"})
    assert resp_cn.status_code == 200
    data_cn = resp_cn.json()
    assert data_cn["ticker"] == "688981.SH"
    assert data_cn["market"] == "CN"

    # 3) 查询列表
    resp_list = client.get("/api/v1/watchlist")
    assert resp_list.status_code == 200
    items = resp_list.json()["items"]
    tickers = [i["ticker"] for i in items]
    assert "NVDA" in tickers
    assert "688981.SH" in tickers

    # 4) 移除
    resp_del = client.delete("/api/v1/watchlist/NVDA")
    assert resp_del.status_code == 200
    assert resp_del.json()["ok"] is True

    # 5) 再次查询确认已移除
    resp_after = client.get("/api/v1/watchlist")
    tickers_after = [i["ticker"] for i in resp_after.json()["items"]]
    assert "NVDA" not in tickers_after
    assert "688981.SH" in tickers_after

    # 6) 测试非法代码 400
    resp_invalid = client.post("/api/v1/watchlist", json={"ticker": "INVALID$$$"})
    assert resp_invalid.status_code == 400


def test_ask_flow(client):
    # 提问已存在标的
    resp = client.post("/api/v1/ask", json={"ticker": "NVDA", "question": "为什么涨？"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "NVDA"
    assert "answer" in data

    # 提问不存在标的 -> 404
    resp_404 = client.post("/api/v1/ask", json={"ticker": "XYZ", "question": "为什么？"})
    assert resp_404.status_code == 404


    # 测试流式响应
    resp_stream = client.post("/api/v1/ask?stream=true", json={"ticker": "NVDA", "question": "为什么？"})
    assert resp_stream.status_code == 200
    assert "text/event-stream" in resp_stream.headers["content-type"]
    assert "data:" in resp_stream.text


def test_digest_today(client):
    resp = client.get("/api/v1/digest/today")
    assert resp.status_code == 200
    data = resp.json()
    assert "date_str" in data
    assert "content_markdown" in data
    assert len(data["content_markdown"]) > 0
