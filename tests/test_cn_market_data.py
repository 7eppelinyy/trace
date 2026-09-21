"""A股腾讯行情 Provider 单元测试。"""

from __future__ import annotations

import httpx
import pytest

from trace.collectors.market_data.cn import (
    MockCNMarketProvider,
    TencentCNMarketProvider,
    build_cn_provider,
)
from trace.common.modes import TraceMode

_SAMPLE_RESP = (
    'v_sh688981="1~中芯国际~688981~120.90~114.86~114.86~39436472~22611824~16824648~'
    '120.89~69~120.88~125~120.87~80~120.86~26~120.85~33~120.90~452~120.91~40~'
    '120.92~203~120.93~6~120.94~171~~20260916161441~6.04~5.26~120.99~114.60";\n'
    'v_sz000001="1~平安银行~000001~11.50~11.40~11.42~500000~250000~250000~'
    '11.49~100~11.48~200~11.47~300~11.46~400~11.45~500~11.50~600~11.51~700~'
    '11.52~800~11.53~900~11.54~1000~~20260916150000~0.10~0.88~11.60~11.38";'
)


class _MockClient:
    def __init__(self, text: str = _SAMPLE_RESP, status_code: int = 200, error: Exception | None = None):
        self.text = text
        self.status_code = status_code
        self.error = error
        self.called_urls: list[str] = []

    def get(self, url: str, **kwargs):
        self.called_urls.append(url)
        if self.error:
            raise self.error
        resp = httpx.Response(status_code=self.status_code, text=self.text, request=httpx.Request("GET", url))
        return resp

    def close(self):
        pass


def test_symbol_conversion():
    provider = TencentCNMarketProvider()
    assert provider._to_tencent_symbol("688981.SH") == "sh688981"
    assert provider._to_tencent_symbol("688981.SS") == "sh688981"
    assert provider._to_tencent_symbol("000001.SZ") == "sz000001"
    assert provider._to_tencent_symbol("sh600519") == "sh600519"
    assert provider._to_tencent_symbol("SZ000002") == "sz000002"
    assert provider._to_tencent_symbol("600519") == "sh600519"
    assert provider._to_tencent_symbol("002415") == "sz002415"
    assert provider._to_tencent_symbol("INVALID") is None
    assert provider._to_tencent_symbol("") is None


def test_parse_quotes_success():
    client = _MockClient()
    provider = TencentCNMarketProvider(client=client)

    quotes = provider.get_quotes(["688981.SH", "000001.SZ"])
    assert len(quotes) == 2
    assert "https://qt.gtimg.cn/q=sh688981,sz000001" in client.called_urls[0]

    q1 = quotes["688981.SH"]
    assert q1.ticker == "688981.SH"
    assert q1.last_price == 120.90
    assert q1.prev_close == 114.86
    assert q1.change_pct_day == 5.26
    assert q1.volume == 39436472
    assert q1.ts.year == 2026
    assert q1.ts.month == 9
    assert q1.ts.day == 16
    assert q1.session == "regular"

    q2 = quotes["000001.SZ"]
    assert q2.ticker == "000001.SZ"
    assert q2.last_price == 11.50
    assert q2.prev_close == 11.40
    assert q2.change_pct_day == 0.88


def test_get_quote_single():
    client = _MockClient()
    provider = TencentCNMarketProvider(client=client)
    q = provider.get_quote("688981.SH")
    assert q is not None
    assert q.last_price == 120.90


def test_network_failure_graceful():
    client = _MockClient(error=httpx.ConnectTimeout("connection timed out"))
    provider = TencentCNMarketProvider(client=client)
    assert provider.get_quote("688981.SH") is None
    assert provider.get_quotes(["688981.SH"]) == {}


def test_malformed_response_graceful():
    client = _MockClient(text="random html or empty response")
    provider = TencentCNMarketProvider(client=client)
    assert provider.get_quote("688981.SH") is None


def test_build_cn_provider_factory(monkeypatch):
    # 显式指定 mock
    monkeypatch.setenv("CN_MARKET_PROVIDER", "mock")
    p = build_cn_provider()
    assert isinstance(p, MockCNMarketProvider)

    # 显式指定 tencent
    monkeypatch.setenv("CN_MARKET_PROVIDER", "tencent")
    p = build_cn_provider()
    assert isinstance(p, TencentCNMarketProvider)

    # 显式指定 unavailable
    monkeypatch.setenv("CN_MARKET_PROVIDER", "unavailable")
    p = build_cn_provider()
    assert p.data_mode == "unavailable"
