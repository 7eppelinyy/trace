"""服务端身份、对象授权与安全边界测试 (T02 / F02 / Probe 11.4)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.common.modes import TraceMode


@pytest.fixture
def api_client(app):
    """裸测试客户端（默认无 Authorization Header）。"""
    api_app = create_api_app(ctx=app)
    with TestClient(api_app) as c:
        yield c


def test_unauthenticated_requests_rejected(api_client):
    """受保护接口未携带有效凭据必须返回 401 Unauthorized。"""
    # 自选
    resp_wl = api_client.get("/api/v1/watchlist")
    assert resp_wl.status_code == 401
    assert "WWW-Authenticate" in resp_wl.headers

    resp_wl_post = api_client.post("/api/v1/watchlist", json={"ticker": "AAPL"})
    assert resp_wl_post.status_code == 401

    resp_wl_del = api_client.delete("/api/v1/watchlist/AAPL")
    assert resp_wl_del.status_code == 401

    # 智能问答
    resp_ask = api_client.post("/api/v1/ask", json={"ticker": "NVDA", "question": "test"})
    assert resp_ask.status_code == 401

    # 早报
    resp_digest = api_client.get("/api/v1/digest/today")
    assert resp_digest.status_code == 401


def test_public_endpoints_accessible_anonymously(api_client):
    """公开接口允许匿名访问。"""
    assert api_client.get("/").status_code == 200
    assert api_client.get("/api/v1/health").status_code == 200
    assert api_client.get("/api/v1/events").status_code == 200
    assert api_client.get("/api/v1/market/indices").status_code == 200


def test_status_endpoint_desensitized(api_client, monkeypatch):
    """系统状态接口中的敏感路径必须脱敏（仅返回文件名）。"""
    assert api_client.get('/api/v1/status').status_code == 401
    session = api_client.post('/api/v1/auth/session', json={'grant_type':'dev','user_id':'audit-admin'}).json()
    api_client.headers['Authorization'] = 'Bearer ' + session['session_token']
    assert api_client.get('/api/v1/status').status_code == 403
    monkeypatch.setenv('TRACE_ADMIN_USER_IDS','audit-admin')
    resp = api_client.get("/api/v1/status")
    assert resp.status_code == 200
    data = resp.json()
    db_path = data.get("db_path", "")
    assert db_path
    # 路径必须是单纯的文件名，不能包含盘符或绝对路径分隔符
    assert "/" not in db_path
    assert "\\" not in db_path
    assert ":" not in db_path


def test_session_lifecycle_and_revocation(api_client):
    """测试会话创建、鉴权访问、撤销与撤销后 401 拦截。"""
    # 1. 签发会话
    resp = api_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "alice_test"})
    assert resp.status_code == 200
    token = resp.json()["session_token"]
    user_id = resp.json()["user_id"]
    assert user_id == "alice_test"

    headers = {"Authorization": f"Bearer {token}"}

    # 2. 用有效 Token 访问 /auth/me
    resp_me = api_client.get("/api/v1/auth/me", headers=headers)
    assert resp_me.status_code == 200
    assert resp_me.json()["user_id"] == "alice_test"

    # 3. 撤销会话
    resp_revoke = api_client.delete("/api/v1/auth/session", headers=headers)
    assert resp_revoke.status_code == 200
    assert resp_revoke.json()["ok"] is True

    # 4. 再次访问已被撤销的会话 -> 401
    resp_after = api_client.get("/api/v1/auth/me", headers=headers)
    assert resp_after.status_code == 401


def test_expired_session_rejected(api_client, app):
    """已过期的 Session Token 必须返回 401。"""
    resp = api_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "expired_user"})
    token = resp.json()["session_token"]

    # 手动在 DB 中将过期时间修改为昨天
    past_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    app.db.execute("UPDATE user_session SET expires_at = ? WHERE session_token = ?", (past_time, token))

    headers = {"Authorization": f"Bearer {token}"}
    resp_access = api_client.get("/api/v1/watchlist", headers=headers)
    assert resp_access.status_code == 401
    assert "expired" in resp_access.json()["detail"].lower()


def test_object_authorization_rejects_identity_tampering(api_client):
    """对象级授权测试 (Probe 11.4)：A 凭据冒用 B 身份必须返回 403 Forbidden。"""
    # 签发 Alice 和 Bob 的会话
    resp_a = api_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "alice"})
    token_a = resp_a.json()["session_token"]

    resp_b = api_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "bob"})
    token_b = resp_b.json()["session_token"]

    # 1. Alice 凭据携带 Alice Header -> 正常通过
    resp_ok = api_client.get(
        "/api/v1/watchlist",
        headers={"Authorization": f"Bearer {token_a}", "X-User-Id": "alice"}
    )
    assert resp_ok.status_code == 200

    # 2. Alice 凭据篡改 X-User-Id 为 bob -> 403 严格拒绝
    resp_forbidden_header = api_client.get(
        "/api/v1/watchlist",
        headers={"Authorization": f"Bearer {token_a}", "X-User-Id": "bob"}
    )
    assert resp_forbidden_header.status_code == 403
    assert "Forbidden" in resp_forbidden_header.json()["detail"]

    # 3. Alice 凭据篡改 URL Query 为 ?user_id=bob -> 403 严格拒绝
    resp_forbidden_query = api_client.get(
        "/api/v1/watchlist?user_id=bob",
        headers={"Authorization": f"Bearer {token_a}"}
    )
    assert resp_forbidden_query.status_code == 403
    assert "Forbidden" in resp_forbidden_query.json()["detail"]


def test_production_mode_prohibits_dev_grant(api_client, monkeypatch):
    """生产模式下严禁使用 grant_type=dev 自声明用户。"""
    monkeypatch.setenv("TRACE_MODE", "production")
    assert TraceMode.is_production() is True

    # 尝试 dev grant -> 403
    resp_dev = api_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "hacker"})
    assert resp_dev.status_code == 403

    # 访客 guest grant -> 正常签发受控的 usr_ 前缀安全账户
    resp_guest = api_client.post("/api/v1/auth/session", json={"grant_type": "guest"})
    assert resp_guest.status_code == 200
    guest_uid = resp_guest.json()["user_id"]
    assert guest_uid.startswith("usr_")
