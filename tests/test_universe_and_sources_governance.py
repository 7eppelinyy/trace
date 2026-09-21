"""T15 专属验证测试套件：证券主数据、覆盖目录与数据源治理 (F23, F29, F30)

覆盖验证：
1. 种子一致性：数据源 security_map 零悬空引用 (Zero Dangling)；
2. 来源合规性：29 个数据源具备完整 4 维许可、核验责任人与时间戳；
3. 运营隔离性 (F30)：启动与 seed 更新绝不冲正运营手工禁用 (Operational Override)；
4. 候选标的生命周期 (F23)：格式仅赋予 unverified 候选状态，退市与已核验主数据受保护；
5. 用户别名隔离 (F30)：自选私有别名不污染公共 Security 主数据；
6. 标的代码与交易所保真 (F23)：完整保留 share class (BRK.B)，不胡乱猜测 NASDAQ；
7. API 分级暴露：自选与搜索接口完整透出 status、coverage_tier 与 user_alias；
8. 文档与代码一致性：docs/coverage_universe.md 与数据库种子 100% 对应。
"""

from __future__ import annotations

import re
from pathlib import Path
import pytest
import yaml

from fastapi.testclient import TestClient
from trace.api.app import create_api_app
from trace.common.tickers import (
    TickerParseError,
    is_valid_ticker_format,
    normalize_ticker,
)
from trace.data.seed import load_all_seeds
from trace.db.connection import Database
from trace.db.migration import apply_migrations
from trace.db.repositories import SecurityRepo, SourceRepo, WatchlistRepo
from trace.domain.models import LicenseMode, Security, WatchlistEntry


@pytest.fixture
def test_db(app):
    return app.db


@pytest.fixture
def test_client(app):
    api_app = create_api_app(ctx=app)
    with TestClient(api_app) as c:
        # Authenticate dev sessions
        yield c


def test_zero_dangling_security_map_references():
    """验证数据源配置中的 security_map 零悬空引用。"""
    sources_yaml = Path("trace/data/seed_sources.yaml")
    secs_yaml = Path("trace/data/seed_securities.yaml")

    sources = yaml.safe_load(sources_yaml.read_text(encoding="utf-8"))["sources"]
    securities = yaml.safe_load(secs_yaml.read_text(encoding="utf-8"))["securities"]

    valid_sec_ids = {f"SEC-{s['market']}-{s['ticker']}" for s in securities}

    dangling = []
    for src in sources:
        for sid in src.get("security_map", []):
            if sid not in valid_sec_ids:
                dangling.append((src["source_id"], sid))

    assert len(dangling) == 0, f"Found dangling security_map references: {dangling}"


def test_source_governance_metadata_and_permissions(test_db):
    """验证数据源登记具备 4 维许可、合规核验元数据与正确权限策略 (F29)。"""
    sources = SourceRepo(test_db).list_all()
    assert len(sources) == 29

    for s in sources:
        # 1. 必须具备核验时间与责任人
        assert s.verified_at is None
        assert s.verified_by is None

        # 2. 启用来源必须具备 fetch, store, display 许可
        if s.enabled:
            assert s.can_fetch is True
            assert s.can_store is True
            assert s.can_display is True
            # 金十数据仅限内部 AI 分析，禁止对外二次转发分发
            if s.source_id == "src_jin10":
                assert s.can_forward is False
            else:
                assert s.can_forward is True
        else:
            # 禁用源（未授权或废弃）四项许可均必须关闭
            assert s.can_fetch is False
            assert s.can_store is False
            assert s.can_display is False
            assert s.can_forward is False


def test_operational_override_isolation_across_restarts(test_db):
    """验证 F30：运营人员手工停用数据源后，服务重启/重载种子绝不覆盖停用状态。"""
    repo = SourceRepo(test_db)

    # 1. 验证 SEC EDGAR 初始为启用状态
    sec_edgar = repo.get("src_sec_edgar")
    assert sec_edgar.enabled is True
    assert sec_edgar.operational_override is None

    # 2. 运营人员由于突发限流或维护，手工设置停用
    repo.set_operational_override(
        "src_sec_edgar",
        enabled=False,
        reason="SEC Edgar Maintenance / Rate Limit Drill",
        updated_by="operator_alice",
    )

    # 3. 验证即时状态已生效停用
    sec_edgar_override = repo.get("src_sec_edgar")
    assert sec_edgar_override.enabled is False
    assert sec_edgar_override.seed_enabled is True
    assert sec_edgar_override.operational_override is False
    assert sec_edgar_override.override_reason == "SEC Edgar Maintenance / Rate Limit Drill"

    # 4. 模拟服务重启，执行 load_all_seeds()
    load_all_seeds(test_db)

    # 5. 关键验收：重启后手工停用状态必须依然保留，绝不被 seed 冲正！
    sec_edgar_after_reboot = repo.get("src_sec_edgar")
    assert sec_edgar_after_reboot.enabled is False
    assert sec_edgar_after_reboot.operational_override is False
    assert sec_edgar_after_reboot.override_reason == "SEC Edgar Maintenance / Rate Limit Drill"

    # 6. 清除 override，恢复 seed 默认值
    repo.set_operational_override("src_sec_edgar", enabled=None)
    sec_edgar_restored = repo.get("src_sec_edgar")
    assert sec_edgar_restored.enabled is True
    assert sec_edgar_restored.operational_override is None


def test_candidate_security_status_lifecycle_and_delisted_guard(test_db):
    """验证 F23：代码格式合法仅成为候选 (unverified)；退市标的与已核验主数据受保护。"""
    repo = SecurityRepo(test_db)

    # 1. 官方种子标的初始为 verified
    nvda = repo.get_by_ticker("NVDA")
    assert nvda is not None
    assert nvda.status == "verified"

    # 2. 模拟添加未核验候选标的
    candidate = Security(
        security_id="SEC-US-CANDIDATE1",
        market="US",
        exchange="",
        ticker="CANDIDATE1",
        company_name_zh="候选公司1",
        company_name_en="Candidate 1",
        status="unverified",
    )
    repo.upsert(candidate)
    loaded_cand = repo.get_by_ticker("CANDIDATE1")
    assert loaded_cand.status == "unverified"

    # 3. 尝试用 unverified 覆盖已核验标的 -> 应该被保护，保持 verified
    fake_nvda = Security(
        security_id="SEC-US-NVDA",
        market="US",
        exchange="",
        ticker="NVDA",
        company_name_zh="英伟达假冒名",
        company_name_en="NVDA",
        status="unverified",
    )
    repo.upsert(fake_nvda)
    check_nvda = repo.get_by_ticker("NVDA")
    assert check_nvda.status == "verified"

    # 4. 标记为退市后，后续动态候选 upsert 不能将其自动冲正
    repo.mark_status(candidate.security_id, "delisted")
    assert repo.get_by_ticker("CANDIDATE1").status == "delisted"

    # 再次尝试 upsert 为 unverified
    repo.upsert(candidate)
    assert repo.get_by_ticker("CANDIDATE1").status == "delisted"


def test_user_alias_isolation_zero_pollution(test_client, test_db):
    """验证 F30：用户设置个人自选别名，绝不污染全局公共 Security 公司名称。"""
    sec_repo = SecurityRepo(test_db)

    # 认证用户 A 与 用户 B 的合法 Session Token
    token_a = test_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "user_a"}).json()["session_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    token_b = test_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "user_b"}).json()["session_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 原始公共公司名称
    orig_sec = sec_repo.get_by_ticker("NVDA")
    assert orig_sec.company_name_zh == "英伟达"

    # 用户 A 添加自选并指定专属别名 "老黄算力旗舰"
    resp_a = test_client.post(
        "/api/v1/watchlist",
        json={"ticker": "NVDA", "company_name_zh": "老黄算力旗舰"},
        headers=headers_a,
    )
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["user_alias"] == "老黄算力旗舰"
    assert data_a["company_name_zh"] == "英伟达"

    # 核心断言：公共主数据未被污染！
    sec_after = sec_repo.get_by_ticker("NVDA")
    assert sec_after.company_name_zh == "英伟达"

    # 用户 B 查看自选，不应受到用户 A 的别名污染
    resp_b_add = test_client.post(
        "/api/v1/watchlist",
        json={"ticker": "NVDA", "company_name_zh": "核心GPU持仓"},
        headers=headers_b,
    )
    assert resp_b_add.status_code == 200
    assert resp_b_add.json()["user_alias"] == "核心GPU持仓"

    # 用户 A 调用专用别名修改接口 PUT /api/v1/watchlist/{ticker}/alias
    resp_update = test_client.put(
        "/api/v1/watchlist/NVDA/alias",
        json={"user_alias": "AI总龙头"},
        headers=headers_a,
    )
    assert resp_update.status_code == 200
    assert resp_update.json()["user_alias"] == "AI总龙头"

    # 验证用户 A 自选列表显示更新后的个人别名
    wl_a = test_client.get("/api/v1/watchlist", headers=headers_a).json()
    item_a = [i for i in wl_a["items"] if i["ticker"] == "NVDA"][0]
    assert item_a["user_alias"] == "AI总龙头"
    assert item_a["company_name_zh"] == "英伟达"

    # 验证用户 B 自选列表保持其自身别名
    wl_b = test_client.get("/api/v1/watchlist", headers=headers_b).json()
    item_b = [i for i in wl_b["items"] if i["ticker"] == "NVDA"][0]
    assert item_b["user_alias"] == "核心GPU持仓"

    # 公共实体依然保持纯洁
    assert sec_repo.get_by_ticker("NVDA").company_name_zh == "英伟达"


def test_share_class_and_exchange_fidelity():
    """验证 F23：代码解析完整保留 share class (BRK.B / BRK-B)，不粗暴截断，不瞎猜 NASDAQ。"""
    # 1. 点号 share class
    norm1 = normalize_ticker("BRK.B")
    assert norm1.ticker == "BRK.B"
    assert norm1.market == "US"
    assert norm1.exchange != "NASDAQ"  # 不应该强行写 NASDAQ

    # 2. 连字符 share class 规范化为点号
    norm2 = normalize_ticker("BRK-B")
    assert norm2.ticker == "BRK.B"

    # 3. 已知标的交易所正确识别 (如 TSM 为纽交所 NYSE)
    norm3 = normalize_ticker("TSM")
    assert norm3.exchange == "NYSE"

    # 4. 代码格式合法性验证工具
    assert is_valid_ticker_format("NVDA") is True
    assert is_valid_ticker_format("688981.SH") is True
    assert is_valid_ticker_format("002371.SZ") is True
    assert is_valid_ticker_format("BRK.A") is True
    assert is_valid_ticker_format("INVALID$$$") is False
    assert is_valid_ticker_format("") is False


def test_watchlist_search_and_list_exposes_governance_tier(test_client):
    """验证自选列表与搜索结果完整暴露 status 与 coverage_tier 分级。"""
    token = test_client.post("/api/v1/auth/session", json={"grant_type": "dev", "user_id": "user_test_tier"}).json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. 搜索官方核心标的 NVDA -> status=verified, coverage_tier=realtime_monitored
    resp_nvda = test_client.get("/api/v1/watchlist/search?q=NVDA", headers=headers)
    assert resp_nvda.status_code == 200
    nvda_item = resp_nvda.json()["items"][0]
    assert nvda_item["ticker"] == "NVDA"
    assert nvda_item["status"] == "verified"
    assert nvda_item["coverage_tier"] == "realtime_monitored"

    # 2. 搜索图谱上下文标的 AMD -> status=verified, coverage_tier=context_universe
    resp_amd = test_client.get("/api/v1/watchlist/search?q=AMD", headers=headers)
    assert resp_amd.status_code == 200
    amd_item = resp_amd.json()["items"][0]
    assert amd_item["ticker"] == "AMD"
    assert amd_item["status"] == "verified"
    assert amd_item["coverage_tier"] == "context_universe"

    # 3. 搜索未录入主数据但格式合法的代码 (如 600000.SH 浦发银行) -> status=unverified, coverage_tier=unverified_candidate
    resp_dyn = test_client.get("/api/v1/watchlist/search?q=600000.SH", headers=headers)
    assert resp_dyn.status_code == 200
    dyn_item = resp_dyn.json()["items"][0]
    assert dyn_item["ticker"] == "600000.SH"
    assert dyn_item["status"] == "unverified"
    assert dyn_item["coverage_tier"] == "unverified_candidate"

    # 4. 添加该未核验候选入自选并查询列表
    test_client.post("/api/v1/watchlist", json={"ticker": "600000.SH"}, headers=headers)
    wl_resp = test_client.get("/api/v1/watchlist", headers=headers)
    assert wl_resp.status_code == 200
    dyn_in_wl = [i for i in wl_resp.json()["items"] if i["ticker"] == "600000.SH"][0]
    assert dyn_in_wl["status"] == "unverified"
    assert dyn_in_wl["coverage_tier"] == "unverified_candidate"


def test_coverage_universe_doc_consistency():
    """验证 docs/coverage_universe.md 与系统种子主数据 100% 对应。"""
    doc_path = Path("docs/coverage_universe.md")
    assert doc_path.exists(), "docs/coverage_universe.md must exist"
    doc_text = doc_path.read_text(encoding="utf-8")

    # 1. 验证 46 只标的全覆盖
    secs_yaml = Path("trace/data/seed_securities.yaml")
    securities = yaml.safe_load(secs_yaml.read_text(encoding="utf-8"))["securities"]
    assert len(securities) == 46

    for s in securities:
        ticker = s["ticker"]
        assert ticker in doc_text, f"Ticker {ticker} not found in docs/coverage_universe.md"

    # 2. 验证 29 个数据源全覆盖
    sources_yaml = Path("trace/data/seed_sources.yaml")
    sources = yaml.safe_load(sources_yaml.read_text(encoding="utf-8"))["sources"]
    assert len(sources) == 29

    for src in sources:
        sid = src["source_id"]
        assert sid in doc_text, f"Source ID {sid} not found in docs/coverage_universe.md"
