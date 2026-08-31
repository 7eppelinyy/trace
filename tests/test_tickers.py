"""A股/美股代码规范化测试：不允许用户输入格式造成多个重复 Security。"""

import pytest

from trace.common.tickers import TickerParseError, normalize_ticker


def test_us_ticker():
    assert normalize_ticker("sndk").ticker == "SNDK"
    assert normalize_ticker("MU").market == "US"
    assert normalize_ticker("BRK.B").ticker == "BRK.B"


def test_a_share_formats_all_normalize_to_same():
    variants = ["600519", "600519.SH", "600519.sh", "sh600519", "600519.SS"]
    results = {normalize_ticker(v).ticker for v in variants}
    assert results == {"600519.SH"}
    assert normalize_ticker("600519").exchange == "SSE"


def test_a_share_szse():
    assert normalize_ticker("000021").ticker == "000021.SZ"
    assert normalize_ticker("sz300308").ticker == "300308.SZ"
    assert normalize_ticker("688981.SH").exchange == "SSE"


def test_invalid():
    with pytest.raises(TickerParseError):
        normalize_ticker("")
    with pytest.raises(TickerParseError):
        normalize_ticker("12345")          # 5 位数字
    with pytest.raises(TickerParseError):
        normalize_ticker("hello world!")
