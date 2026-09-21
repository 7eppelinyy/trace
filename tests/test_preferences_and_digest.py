"""T10 偏好与时区日报闭环测试 (F10, F27)。

验证：
1. 偏好优先级：单标的覆盖 > 全局偏好 > 默认回退；
2. 乐观并发版本控制 (expected_revision conflict 409)；
3. AlertEngine 退订抑制、阈值过滤与免打扰时段判定；
4. 日报跨时区 cache_key 隔离，杜绝台北/纽约用户互相覆盖缓存；
5. REST API 契约与鉴权。
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from trace.alerts.engine import AlertEngine, is_in_quiet_hours
from trace.api.app import create_api_app
from trace.config import load_config
from trace.db.connection import Database
from trace.db.repositories import (
    DailyDigestRepo,
    EventImpactRepo,
    EventRepo,
    NotificationPreferenceRepo,
    SecurityRepo,
    UserRepo,
    UserSessionRepo,
    WatchlistRepo,
)
from trace.domain.models import (
    DailyDigest,
    Direction,
    Directness,
    Event,
    EventImpact,
    EventStatus,
    EventType,
    Security,
    User,
    WatchlistEntry,
)


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_pref.db"
    db = Database(db_file)
    from trace.db.migration import apply_migrations
    apply_migrations(db)
    return db


@pytest.fixture
def sample_data(test_db):
    user_repo = UserRepo(test_db)
    sec_repo = SecurityRepo(test_db)
    wl_repo = WatchlistRepo(test_db)

    # 创建两个处于不同时区的用户
    user_repo.ensure("user_taipei", timezone="Asia/Taipei")
    user_repo.ensure("user_ny", timezone="America/New_York")
    u1 = user_repo.get("user_taipei")
    u2 = user_repo.get("user_ny")

    # 证券
    s1 = Security(security_id="SEC_NVDA", ticker="NVDA", market="US", company_name_en="NVIDIA")
    s2 = Security(security_id="SEC_MU", ticker="MU", market="US", company_name_en="Micron")
    sec_repo.upsert(s1)
    sec_repo.upsert(s2)

    wl_repo.add(WatchlistEntry(user_id="user_taipei", security_id="SEC_NVDA"))
    wl_repo.add(WatchlistEntry(user_id="user_taipei", security_id="SEC_MU"))
    wl_repo.add(WatchlistEntry(user_id="user_ny", security_id="SEC_NVDA"))

    return {"u1": u1, "u2": u2, "s1": s1, "s2": s2}


def test_preference_hierarchy_and_concurrency(test_db, sample_data):
    repo = NotificationPreferenceRepo(test_db)

    # 1. 默认回退
    def_pref = repo.get_effective_preference("user_taipei", "SEC_NVDA")
    assert def_pref.threshold == 7.0
    assert def_pref.enabled is True
    assert def_pref.channel == "all"

    # 2. 设置全局偏好
    p_global = repo.set_preference("user_taipei", None, threshold=8.0, enabled=True)
    assert p_global.revision == 1
    assert p_global.threshold == 8.0

    # 此时查询标的偏好继承全局
    eff_pref = repo.get_effective_preference("user_taipei", "SEC_NVDA")
    assert eff_pref.threshold == 8.0

    # 3. 设置单标的覆盖
    p_sec = repo.set_preference("user_taipei", "SEC_NVDA", threshold=6.5, enabled=False)
    assert p_sec.revision == 1
    assert p_sec.threshold == 6.5
    assert p_sec.enabled is False

    # 优先级：单标的覆盖优先
    eff_nvda = repo.get_effective_preference("user_taipei", "SEC_NVDA")
    assert eff_nvda.threshold == 6.5
    assert eff_nvda.enabled is False

    # 未覆盖的标的依然走全局
    eff_mu = repo.get_effective_preference("user_taipei", "SEC_MU")
    assert eff_mu.threshold == 8.0
    assert eff_mu.enabled is True

    # 4. 乐观并发锁测试
    p_global_updated = repo.set_preference(
        "user_taipei", None, threshold=8.5, expected_revision=1
    )
    assert p_global_updated.revision == 2
    assert p_global_updated.threshold == 8.5

    # 传入过期的 expected_revision 必须抛出冲突异常
    with pytest.raises(ValueError, match="Revision conflict"):
        repo.set_preference("user_taipei", None, threshold=9.0, expected_revision=1)


def test_is_in_quiet_hours():
    # 测试跨夜免打扰：22:00 至 08:00
    # 构造 UTC 15:00 = Asia/Taipei 23:00 (在免打扰内)
    dt_in = datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(dt_in, "Asia/Taipei", "22:00", "08:00") is True

    # 构造 UTC 02:00 = Asia/Taipei 10:00 (不在免打扰内)
    dt_out = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc)
    assert is_in_quiet_hours(dt_out, "Asia/Taipei", "22:00", "08:00") is False

    # 测试日间免打扰：13:00 至 14:00 (午休)
    dt_lunch = datetime(2026, 9, 19, 5, 30, tzinfo=timezone.utc)  # 台北 13:30
    assert is_in_quiet_hours(dt_lunch, "Asia/Taipei", "13:00", "14:00") is True


def test_alert_engine_suppression(test_db, sample_data):
    config = load_config()
    engine = AlertEngine(test_db, config)
    pref_repo = NotificationPreferenceRepo(test_db)

    now = datetime.now(timezone.utc)
    ev = Event(
        event_id="EVT-T10-TEST",
        title="Test T10 Event",
        summary="Testing alert suppression",
        event_type=EventType.PRODUCT.value,
        status=EventStatus.OFFICIAL_CONFIRMED.value,
        first_seen_at=now,
        last_updated_at=now,
        version=1,
    )
    imp_nvda = EventImpact(
        impact_id="IMP-NVDA-1",
        event_id="EVT-T10-TEST",
        security_id="SEC_NVDA",
        direction=Direction.BULLISH.value,
        directness=Directness.DIRECT.value,
        magnitude=0.8,
        persistence=0.8,
        confidence=0.9,
        reason="Positive",
        final_score=7.5,
    )

    # 1. 默认情况下，7.5 分 > 7.0 默认阈值，应该触发推送
    batch1 = engine.evaluate(ev, [imp_nvda], bypass_freshness=True)
    assert any(d.user_id == "user_taipei" and d.should_send for d in batch1.decisions)

    # 2. 用户退订 SEC_NVDA (enabled=False) -> 必须被抑制
    pref_repo.set_preference("user_taipei", "SEC_NVDA", enabled=False)
    batch2 = engine.evaluate(ev, [imp_nvda], bypass_freshness=True)
    assert not any(d.user_id == "user_taipei" for d in batch2.decisions)
    assert batch2.suppressed >= 1

    # 3. 恢复 enabled，但提高阈值到 8.0 -> 7.5 < 8.0 必须被抑制
    pref_repo.set_preference("user_taipei", "SEC_NVDA", enabled=True, threshold=8.0)
    batch3 = engine.evaluate(ev, [imp_nvda], bypass_freshness=True)
    assert not any(d.user_id == "user_taipei" for d in batch3.decisions)


def test_daily_digest_timezone_cache_isolation(test_db):
    digest_repo = DailyDigestRepo(test_db)

    date_str = "2026-09-19"
    key_taipei = f"{date_str}:Asia/Taipei:default:v1"
    key_ny = f"{date_str}:America/New_York:default:v1"

    d_taipei = DailyDigest(
        digest_id="dig_taipei",
        date_str=date_str,
        content_markdown="Taipei Morning Digest Content",
        cache_key=key_taipei,
    )
    d_ny = DailyDigest(
        digest_id="dig_ny",
        date_str=date_str,
        content_markdown="New York Morning Digest Content",
        cache_key=key_ny,
    )

    digest_repo.upsert(d_taipei)
    digest_repo.upsert(d_ny)

    # 验证两个时区的日报互相独立，没有被同日期的 ON CONFLICT(date_str) 互相覆盖
    loaded_taipei = digest_repo.get_by_cache_key(key_taipei)
    loaded_ny = digest_repo.get_by_cache_key(key_ny)

    assert loaded_taipei is not None
    assert loaded_taipei.digest_id == "dig_taipei"
    assert loaded_taipei.content_markdown == "Taipei Morning Digest Content"

    assert loaded_ny is not None
    assert loaded_ny.digest_id == "dig_ny"
    assert loaded_ny.content_markdown == "New York Morning Digest Content"


def test_preferences_api_endpoints(app):
    from trace.db.repositories import UserRepo
    user_repo = UserRepo(app.db)
    user_repo.ensure("user_taipei", timezone="Asia/Taipei")

    api_app = create_api_app(app)
    with TestClient(api_app) as client:
        session_repo = UserSessionRepo(app.db)
        sess = session_repo.create_session("user_taipei")
        headers = {"Authorization": f"Bearer {sess.session_token}"}

        # 1. GET /preferences (默认)
        res = client.get("/api/v1/preferences", headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["global_preference"]["threshold"] == 7.0
        assert data["global_preference"]["enabled"] is True
        assert data["overrides"] == []

        # 2. PUT /preferences (更新全局)
        res_put = client.put(
            "/api/v1/preferences",
            headers=headers,
            json={
                "security_id": None,
                "threshold": 7.5,
                "enabled": True,
                "quiet_start": "23:00",
                "quiet_end": "07:30",
            },
        )
        assert res_put.status_code == 200
        p_data = res_put.json()
        assert p_data["threshold"] == 7.5
        assert p_data["revision"] == 1

        # 3. PUT /preferences 冲突检测 (传入错误 expected_revision)
        res_conflict = client.put(
            "/api/v1/preferences",
            headers=headers,
            json={
                "security_id": None,
                "threshold": 8.0,
                "enabled": True,
                "expected_revision": 99,
            },
        )
        assert res_conflict.status_code == 409
        assert "Revision conflict" in res_conflict.json()["detail"]

        # 4. PUT /preferences 单标的覆盖
        res_ov = client.put(
            "/api/v1/preferences",
            headers=headers,
            json={
                "security_id": "SEC_NVDA",
                "threshold": 6.0,
                "enabled": False,
            },
        )
        assert res_ov.status_code == 200

        # 5. GET /preferences 包含 overrides
        res_get = client.get("/api/v1/preferences", headers=headers)
        assert res_get.status_code == 200
        data2 = res_get.json()
        assert data2["global_preference"]["threshold"] == 7.5
        assert len(data2["overrides"]) == 1
        assert data2["overrides"][0]["security_id"] == "SEC_NVDA"
        assert data2["overrides"][0]["enabled"] is False
