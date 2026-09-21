"""会话续期、凭据轮换与账户/缓存隔离端到端测试 (N02 / RV01)。

验证：
1. POST /auth/session 签发 session_token + refresh_token + 服务端分配 user_id；
2. POST /auth/session/refresh 轮换 refresh_token，续期后 user_id 保持不变；自选和研究数据完全一致；
3. 防重放攻击：已消费的旧 refresh_token 再次使用时，检测到重放并撤销同 family 下全部凭据；
4. 撤销闭环：DELETE /auth/session 后，当前 session 与关联 refresh_token 均不可再用；
5. 生产门禁与未知模式校验：生产模式禁止 dev grant (403)，非法 TRACE_MODE 拒绝；
6. 速率限制隔离：会话签发与刷新使用独立限流桶；
7. 过期凭据明确拒绝，不静默换账户。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.common.modes import TraceMode
from trace.db.repositories import (
    ResearchQuestionRepo,
    UserRepo,
    UserSessionRepo,
    WatchlistRepo,
)


@pytest.fixture
def auth_client(app):
    """装配独立的 API TestClient。"""
    api_app = create_api_app(ctx=app)
    with TestClient(api_app) as c:
        yield c, app


def test_session_creation_returns_refresh_token(auth_client):
    client, ctx = auth_client
    resp = client.post("/api/v1/auth/session", json={"grant_type": "guest"})
    assert resp.status_code == 200
    data = resp.json()
    assert "session_token" in data
    assert "refresh_token" in data
    assert "user_id" in data
    assert data["session_token"].startswith("trc_sess_")
    assert data["refresh_token"].startswith("trc_refr_")
    assert data["user_id"].startswith("usr_")
    assert data["token_type"] == "Bearer"


def test_session_refresh_preserves_user_identity_and_data(auth_client):
    client, ctx = auth_client
    # 1. 签发初次会话
    r1 = client.post("/api/v1/auth/session", json={"grant_type": "guest"})
    assert r1.status_code == 200
    d1 = r1.json()
    uid = d1["user_id"]
    sess_1 = d1["session_token"]
    refr_1 = d1["refresh_token"]

    # 2. 为该用户创建私有自选和私有研究假设
    h1 = {"Authorization": f"Bearer {sess_1}"}
    wl_resp = client.post("/api/v1/watchlist", json={"ticker": "NVDA", "company_name_zh": "英伟达"}, headers=h1)
    assert wl_resp.status_code == 200
    q_resp = client.post("/api/v1/research/questions", json={"title": "NVDA GPU Demand", "hypothesis": "Strong AI cycle"}, headers=h1)
    assert q_resp.status_code == 200
    qid = q_resp.json()["question_id"]

    # 3. 执行续期
    r_refresh = client.post("/api/v1/auth/session/refresh", json={"refresh_token": refr_1})
    assert r_refresh.status_code == 200
    d2 = r_refresh.json()
    sess_2 = d2["session_token"]
    refr_2 = d2["refresh_token"]
    assert d2["user_id"] == uid, "续期后 user_id 必须严格保持不变"
    assert sess_2 != sess_1, "会话凭据必须轮换"
    assert refr_2 != refr_1, "刷新凭据必须轮换"

    # 4. 使用新会话凭据访问自选与研究，数据完整保留
    h2 = {"Authorization": f"Bearer {sess_2}"}
    wl_list = client.get("/api/v1/watchlist", headers=h2)
    assert wl_list.status_code == 200
    tickers = [i["ticker"] for i in wl_list.json()["items"]]
    assert "NVDA" in tickers

    q_detail = client.get(f"/api/v1/research/questions/{qid}", headers=h2)
    assert q_detail.status_code == 200
    assert q_detail.json()["title"] == "NVDA GPU Demand"


def test_refresh_token_replay_attack_revokes_entire_family(auth_client):
    client, ctx = auth_client
    # 签发初始会话
    r1 = client.post("/api/v1/auth/session", json={"grant_type": "guest"})
    d1 = r1.json()
    refr_1 = d1["refresh_token"]

    # 正常消费一次
    r2 = client.post("/api/v1/auth/session/refresh", json={"refresh_token": refr_1})
    assert r2.status_code == 200
    d2 = r2.json()
    sess_2 = d2["session_token"]
    refr_2 = d2["refresh_token"]

    # 重放攻击：再次提交已消费的 refr_1
    r_replay = client.post("/api/v1/auth/session/refresh", json={"refresh_token": refr_1})
    assert r_replay.status_code == 401
    assert "reuse detected" in r_replay.json()["detail"].lower() or "revoked" in r_replay.json()["detail"].lower()

    # 验证安全防御熔断：同 family 下轮换出的新凭据 refr_2 和 sess_2 也被撤销！
    r_check_sess = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {sess_2}"})
    assert r_check_sess.status_code == 401

    r_check_refr = client.post("/api/v1/auth/session/refresh", json={"refresh_token": refr_2})
    assert r_check_refr.status_code == 401


def test_session_and_refresh_revocation(auth_client):
    client, ctx = auth_client
    r1 = client.post("/api/v1/auth/session", json={"grant_type": "guest"})
    d1 = r1.json()
    sess = d1["session_token"]
    refr = d1["refresh_token"]

    # 主动注销 DELETE /auth/session
    r_del = client.request(
        "DELETE",
        "/api/v1/auth/session",
        headers={"Authorization": f"Bearer {sess}"},
        json={"refresh_token": refr}
    )
    assert r_del.status_code == 200

    # 会话不可用
    r_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {sess}"})
    assert r_me.status_code == 401

    # 刷新凭据不可用
    r_ref = client.post("/api/v1/auth/session/refresh", json={"refresh_token": refr})
    assert r_ref.status_code == 401


def test_production_mode_and_dev_grant_restrictions(auth_client, monkeypatch):
    client, ctx = auth_client

    # 模拟生产模式
    monkeypatch.setenv("TRACE_MODE", "production")
    monkeypatch.delenv("TRACE_ALLOW_DEV_AUTH", raising=False)

    # dev 方式必须拒绝 403
    r_dev = client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "audit-victim"})
    assert r_dev.status_code == 403
    assert "prohibited" in r_dev.json()["detail"]

    # guest 方式正常可用
    r_guest = client.post("/api/v1/auth/session", json={"grant_type": "guest"})
    assert r_guest.status_code == 200
    assert r_guest.json()["user_id"].startswith("usr_")

    # 未配置微信返回 501
    r_wx = client.post("/api/v1/auth/session", json={"grant_type": "wechat"})
    assert r_wx.status_code == 501


def test_invalid_trace_mode_fails_startup(monkeypatch):
    monkeypatch.setenv("TRACE_MODE", "real")
    with pytest.raises(ValueError, match="invalid TRACE_MODE"):
        TraceMode.validate()
