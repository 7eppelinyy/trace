"""SEC ticker/CIK Authority 核验测试（离线：模拟官方权威表）。

任务书 §3：
    - SEC company ticker authority → 自动匹配 ticker → 核验本地 seed
    - 不一致时给出明确错误（不允许静默通过）
    - 不得在运行中偷偷覆盖用户数据（本模块只核验与报告）
"""

import httpx
import pytest

from trace.domain.models import Security
from trace.verification import SECAuthority, verify_securities


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _authority(monkeypatch, rows):
    data = {
        str(i): {"cik_str": r["cik"], "ticker": r["ticker"], "title": r["title"]}
        for i, r in enumerate(rows)
    }
    monkeypatch.setattr("trace.verification.httpx.get",
                        lambda url, **kw: _FakeResp(data))
    auth = SECAuthority()
    auth.fetch()
    return auth


def test_authority_fetch_zero_pads_cik(monkeypatch):
    auth = _authority(monkeypatch, [
        {"cik": 2023554, "ticker": "SNDK", "title": "Sandisk Corp"},
        {"cik": 723125, "ticker": "MU", "title": "Micron Technology Inc"},
    ])
    assert auth.get("sndk").cik == "0002023554"
    assert auth.get("MU").title == "Micron Technology Inc"


def test_authority_fetch_failure_not_silent(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr("trace.verification.httpx.get", boom)
    with pytest.raises(RuntimeError):
        SECAuthority().fetch()


def test_authority_empty_table_rejected(monkeypatch):
    monkeypatch.setattr("trace.verification.httpx.get",
                        lambda url, **kw: _FakeResp({}))
    with pytest.raises(RuntimeError):
        SECAuthority().fetch()


def test_verify_match(monkeypatch):
    auth = _authority(monkeypatch, [
        {"cik": 2023554, "ticker": "SNDK", "title": "Sandisk Corp"},
    ])
    local = [Security(security_id="s1", market="US", ticker="SNDK",
                      cik="0002023554", company_name_en="Sandisk Corporation")]
    results = verify_securities(local, auth)
    assert results[0].status == "MATCH"
    assert results[0].ok


def test_verify_mismatch_reported(monkeypatch):
    auth = _authority(monkeypatch, [
        {"cik": 2023554, "ticker": "SNDK", "title": "Sandisk Corp"},
    ])
    local = [Security(security_id="s1", market="US", ticker="SNDK",
                      cik="0002023443", company_name_en="Sandisk Corporation")]
    results = verify_securities(local, auth)
    assert results[0].status == "MISMATCH"
    assert not results[0].ok
    assert "0002023554" in results[0].detail


def test_verify_not_found_official(monkeypatch):
    auth = _authority(monkeypatch, [
        {"cik": 2023554, "ticker": "SNDK", "title": "Sandisk Corp"},
    ])
    local = [Security(security_id="s1", market="US", ticker="NVDA",
                      cik="0001045810", company_name_en="NVIDIA")]
    results = verify_securities(local, auth)
    assert results[0].status == "NOT_FOUND_OFFICIAL"


def test_verify_skips_cn_securities(monkeypatch):
    auth = _authority(monkeypatch, [
        {"cik": 2023554, "ticker": "SNDK", "title": "Sandisk Corp"},
    ])
    local = [Security(security_id="c1", market="CN", ticker="688981.SH",
                      company_name_zh="中芯国际")]
    assert verify_securities(local, auth) == []


def test_verify_seed_securities_offline(db, monkeypatch):
    """用模拟权威表核验种子数据：SNDK/MU/NVDA 的 CIK 与官方一致。"""
    from trace.db.repositories import SecurityRepo
    auth = _authority(monkeypatch, [
        {"cik": 2023554, "ticker": "SNDK", "title": "Sandisk Corp"},
        {"cik": 723125, "ticker": "MU", "title": "Micron Technology Inc"},
        {"cik": 1045810, "ticker": "NVDA", "title": "NVIDIA Corp"},
    ])
    # 只核验登记了 CIK 的核心 Watchlist 证券（context universe 无 CIK）
    local = [s for s in SecurityRepo(db).list_all() if s.cik]
    results = verify_securities(local, auth)
    assert {r.ticker for r in results} == {"SNDK", "MU", "NVDA"}
    assert all(r.ok for r in results), [r for r in results if not r.ok]
