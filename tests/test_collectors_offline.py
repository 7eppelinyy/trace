"""Collector 离线测试：官方数据解析 fixture + cursor + retry + 限流 + 失败暴露。

任务书 §6/§14：
    - 美股官方数据解析 fixture（SEC EDGAR submissions JSON）
    - A股公告解析 fixture（巨潮 query/topSearch）
    - Collector cursor（增量：重复采集不产生重复 RawItem）
    - Collector 重试 / 限流
    - 来源失败不伪装无新数据
"""

from __future__ import annotations

import httpx
import pytest

from trace.collectors.base import BaseCollector, CollectorRegistry
from trace.collectors.cninfo import CNINFOCollector
from trace.collectors.sec import SECCollector
from trace.collectors.sse import SSECollector
from trace.collectors.szse import SZSECollector
from trace.common.http_client import (
    AuthError,
    HttpClient,
    NetworkError,
    SourceError,
    SourceUnavailableError,
)
from trace.db.health import SourceHealthRepo
from trace.db.repositories import SourceRepo
from trace.domain.models import Source


# ---------------------------------------------------------------------------
# Fake HTTP
# ---------------------------------------------------------------------------

class _JsonResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeHttp:
    """按 URL 关键字返回预制 payload 的假 HttpClient。"""

    def __init__(self, payloads: dict):
        self.payloads = payloads          # url 关键字 -> payload | Exception
        self.calls: list = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return self._match(url)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw.get("data")))
        return self._match(url)

    def _match(self, url):
        for key, val in self.payloads.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                if callable(val):
                    return val(url, self.calls[-1])
                return _JsonResp(val)
        raise AssertionError(f"unexpected url: {url}")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# SEC EDGAR 解析 fixture
# ---------------------------------------------------------------------------

EDGAR_NVDA_JSON = {
    "cik": "1045810",
    "filings": {
        "recent": {
            "form": ["8-K", "DEF 14A", "10-Q"],
            "accessionNumber": [
                "0001045810-26-000010",
                "0001045810-26-000009",
                "0001045810-26-000008",
            ],
            "filingDate": ["2026-08-20", "2026-08-15", "2026-06-25"],
            "primaryDocument": ["nvda-8k.htm", "def14a.htm", "nvda-10q.htm"],
            "primaryDocDescription": ["CURRENT REPORT", "PROXY", "QUARTERLY REPORT"],
        }
    },
}


def test_sec_edgar_parse_fixture(db, config, monkeypatch):
    """真实结构 fixture：只保留关注类型，保存 accession/URL/filed_at/form。"""
    fake = _FakeHttp({"data.sec.gov/submissions": EDGAR_NVDA_JSON})
    collector = SECCollector(db, config)
    monkeypatch.setattr(collector, "http_client", lambda *a, **k: fake)

    items = collector.collect()
    # 3 条 filing 中只有 8-K 与 10-Q 属于关注类型（DEF 14A 被过滤）
    assert len(items) == 2
    forms = " ".join(i.reference for i in items)
    assert "form=8-K" in forms and "form=10-Q" in forms
    assert all(i.source_id == "src_sec_edgar" for i in items)
    assert all(i.source_item_id.startswith("edgar:") for i in items)
    item8k = next(i for i in items if "8-K" in i.reference)
    assert item8k.published_at is not None and item8k.published_at.year == 2026
    assert "/Archives/edgar/data/1045810/000104581026000010/nvda-8k.htm" in item8k.url
    assert "accession=0001045810-26-000010" in item8k.reference


def test_sec_cursor_prevents_recollect(db, config, monkeypatch):
    """增量游标：第二次采集同一来源不产生重复条目。"""
    fake = _FakeHttp({"data.sec.gov/submissions": EDGAR_NVDA_JSON})
    collector = SECCollector(db, config)
    monkeypatch.setattr(collector, "http_client", lambda *a, **k: fake)

    first = collector.collect()
    assert len(first) == 2
    second = collector.collect()
    assert second == []
    seen = collector.cursor_repo.get("src_sec_edgar")["seen_accessions"]
    assert "0001045810-26-000010" in seen


# ---------------------------------------------------------------------------
# 巨潮（A股）解析 fixture
# ---------------------------------------------------------------------------

_TS_2025_08_20 = 1755648000000   # 2025-08-20T00:00:00Z（毫秒）


def _cninfo_fake(fail_all_queries: bool = False) -> _FakeHttp:
    def topsearch(url, call):
        code = call[2]["keyWord"]
        return _JsonResp([{"code": code, "orgId": f"org{code}", "zwjc": "测试公司"}])

    def query(url, call):
        code = call[2]["stock"].split(",")[0]
        if fail_all_queries:
            return _JsonResp({"announcements": None})   # 结构变化
        return _JsonResp({"announcements": [{
            "announcementId": f"AN{code}",
            "announcementTitle": "<em>测试公告</em>：关于有关事项的说明",
            "adjunctUrl": f"finalpage/2025-08-20/{code}.PDF",
            "announcementTime": _TS_2025_08_20,
            "secName": "测试公司",
            "announcementType": "年度报告",
        }]})

    return _FakeHttp({"topSearch": topsearch, "hisAnnouncement": query})


def test_cninfo_parse_fixture(db, config, monkeypatch):
    """A股公告解析：标题去 <em>、静态原文地址、毫秒时间戳、公告唯一标识。"""
    collector = CNINFOCollector(db, config)
    monkeypatch.setattr(collector, "http_client", lambda *a, **k: _cninfo_fake())

    items = collector.collect()
    # 默认 Watchlist 中的 5 只 A股：688981/603986/688008/301308/000977
    assert len(items) == 5
    item = next(i for i in items if "688981" in i.source_item_id)
    assert "<em>" not in item.title
    assert item.url.startswith("http://static.cninfo.com.cn/")
    assert item.published_at.year == 2025 and item.published_at.month == 8
    assert item.language == "zh"
    assert item.source_id == "src_cninfo"
    assert "公告ID=AN688981" in item.reference


def test_cninfo_cursor_and_org_cache(db, config, monkeypatch):
    """增量：重复采集不产生重复 RawItem；orgId 解析结果缓存复用。"""
    fake = _cninfo_fake()
    collector = CNINFOCollector(db, config)
    monkeypatch.setattr(collector, "http_client", lambda *a, **k: fake)

    assert len(collector.collect()) == 5
    assert collector.collect() == []
    cursor = collector.cursor_repo.get("src_cninfo")
    assert "AN688981" in cursor["seen_announcement_ids"]
    assert cursor["org_cache"].get("688981") == "org688981"
    # 第二轮不应再调用 topSearch（orgId 已缓存）
    topsearch_calls = [c for c in fake.calls if "topSearch" in c[1]]
    assert len(topsearch_calls) == 5


def test_cninfo_structure_error_not_silent(db, config, monkeypatch):
    """来源结构变化必须暴露为失败，不得伪装成无新数据。"""
    collector = CNINFOCollector(db, config)
    monkeypatch.setattr(collector, "http_client",
                        lambda *a, **k: _cninfo_fake(fail_all_queries=True))
    res = collector.run()
    assert res.status == "failed"
    assert res.error_category == "parse_failed"
    health = SourceHealthRepo(db).get("src_cninfo")
    assert health["consecutive_failures"] == 1
    assert health["last_error"]


def test_sse_szse_adapters_market_filter(db, config, monkeypatch):
    """路线B：SSE/SZSE 适配器只采集各自市场，游标互相独立。"""
    SourceRepo(db).upsert(Source(source_id="src_sse", source_name="上交所",
                                 source_type="official", enabled=True))
    SourceRepo(db).upsert(Source(source_id="src_szse", source_name="深交所",
                                 source_type="official", enabled=True))

    sse = SSECollector(db, config)
    monkeypatch.setattr(sse, "http_client", lambda *a, **k: _cninfo_fake())
    sse_items = sse.collect()
    assert len(sse_items) == 3                       # 688981/603986/688008
    assert all(i.source_id == "src_sse" for i in sse_items)

    szse = SZSECollector(db, config)
    monkeypatch.setattr(szse, "http_client", lambda *a, **k: _cninfo_fake())
    szse_items = szse.collect()
    assert len(szse_items) == 2                      # 301308/000977
    assert all(i.source_id == "src_szse" for i in szse_items)

    assert sse.cursor_repo.get("src_sse")["seen_announcement_ids"]
    assert szse.cursor_repo.get("src_szse")["seen_announcement_ids"]


def test_disabled_source_is_disabled(db, config):
    """默认禁用的来源（路线B适配器）必须明确返回 disabled，不静默运行。"""
    res = SSECollector(db, config).run()
    assert res.status == "disabled"


# ---------------------------------------------------------------------------
# HttpClient：重试 / 退避 / 限流 / 错误分类
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def json(self):
        return {"ok": True}


class _StubClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    def request(self, method, url, **kw):
        out = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(out, Exception):
            raise out
        return out

    def close(self):
        pass


def _http(outcomes, max_retries: int = 3) -> tuple[HttpClient, _StubClient]:
    client = HttpClient("src_test", timeout=1.0, max_retries=max_retries,
                        backoff_base=0.001, rate_limit_qps=1000.0)
    stub = _StubClient(outcomes)
    client._client = stub
    return client, stub


def test_http_retry_then_success():
    client, stub = _http([httpx.ConnectError("boom"),
                          httpx.ConnectError("boom"), _Resp(200)])
    resp = client.get("https://x.test/a")
    assert resp.status_code == 200
    assert stub.calls == 3


def test_http_network_error_after_retries():
    client, stub = _http([httpx.ConnectError("boom")] * 10, max_retries=2)
    with pytest.raises(NetworkError):
        client.get("https://x.test/a")
    assert stub.calls == 3                     # 1 + 2 次重试


def test_http_429_rate_limited_backoff_then_success():
    client, stub = _http([_Resp(429), _Resp(200)])
    resp = client.get("https://x.test/a")
    assert resp.status_code == 200
    assert stub.calls == 2


def test_http_429_exhausted_raises():
    client, stub = _http([_Resp(429)] * 10, max_retries=1)
    with pytest.raises(SourceError):
        client.get("https://x.test/a")
    assert stub.calls == 2


def test_http_5xx_retried_as_unavailable():
    client, stub = _http([_Resp(503), _Resp(200)])
    assert client.get("https://x.test/a").status_code == 200
    assert stub.calls == 2


def test_http_auth_error_no_retry():
    client, stub = _http([_Resp(403)] * 5, max_retries=3)
    with pytest.raises(AuthError):
        client.get("https://x.test/a")
    assert stub.calls == 1                     # 鉴权失败不重试


def test_http_5xx_exhausted_raises_unavailable():
    client, stub = _http([_Resp(500)] * 10, max_retries=1)
    with pytest.raises(SourceUnavailableError):
        client.get("https://x.test/a")


def test_http_rate_limit_spacing(monkeypatch):
    """rate limit：连续请求之间必须等待最小间隔。"""
    sleeps: list[float] = []
    monkeypatch.setattr("trace.common.http_client.time.sleep",
                        lambda s: sleeps.append(s))
    client = HttpClient("src_test", timeout=1.0, max_retries=0,
                        backoff_base=0.001, rate_limit_qps=2.0)  # 间隔 0.5s
    stub = _StubClient([_Resp(200), _Resp(200)])
    client._client = stub
    client.get("https://x.test/a")
    client.get("https://x.test/b")
    assert any(s > 0.3 for s in sleeps)        # 第二次请求前被限流等待


# ---------------------------------------------------------------------------
# 来源失败不伪装无新数据
# ---------------------------------------------------------------------------

class _BoomCollector(BaseCollector):
    collector_type = "boom"

    def __init__(self, db, config, exc):
        super().__init__(db, config)
        self.exc = exc

    @property
    def handled_source_ids(self):
        return {"src_boom"}

    def collect(self):
        raise self.exc


def _enable_boom(db):
    SourceRepo(db).upsert(Source(source_id="src_boom", source_name="Boom",
                                 source_type="official", enabled=True))


def test_network_failure_reported_not_empty(db, config):
    _enable_boom(db)
    res = _BoomCollector(db, config, NetworkError("down")).run()
    assert res.status == "failed"
    assert res.error_category == "network_error"
    assert res.items == []
    health = SourceHealthRepo(db).get("src_boom")
    assert health["consecutive_failures"] == 1
    assert health["last_failure_at"]


def test_unexpected_error_reported_not_empty(db, config):
    _enable_boom(db)
    res = _BoomCollector(db, config, ValueError("weird")).run()
    assert res.status == "failed"
    assert res.error_category == "structure_changed"


def test_success_no_new_data_distinct_from_failure(db, config):
    """成功但无新数据 != 失败：状态必须区分。"""
    _enable_boom(db)

    class _Empty(_BoomCollector):
        def collect(self):
            return []

    res = _Empty(db, config, None).run()
    assert res.status == "no_new_data"
    health = SourceHealthRepo(db).get("src_boom")
    assert health["consecutive_failures"] == 0
    assert health["last_success_at"]


def test_registry_run_all_aggregates_failures(db, config):
    _enable_boom(db)
    registry = CollectorRegistry()
    registry.register(_BoomCollector(db, config, NetworkError("down")))
    items, results = registry.run_all()
    assert items == []
    assert results[0].status == "failed"
    assert results[0].source_ids == ["src_boom"]
