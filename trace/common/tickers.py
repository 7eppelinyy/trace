"""证券代码解析与规范化。

A股证券格式必须统一，不允许用户输入格式造成多个重复 Security。
支持输入形式：
    美股：SNDK / MU / NVDA（大小写不敏感）
    A股：600519 / 600519.SH / 600519.sh / sh600519 / 688981.SS
输出标准形式：
    美股：大写 ticker（如 SNDK）
    A股：6 位代码 + 交易所后缀（如 600519.SH）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

A_SHARE_RE = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class NormalizedTicker:
    market: str          # "US" | "CN"
    exchange: str        # NASDAQ / NYSE / SSE / SZSE / BSE / ""
    ticker: str          # 标准形式


class TickerParseError(ValueError):
    pass


def _a_share_exchange(code: str) -> str:
    """按代码段推断交易所（6 位数字代码）。

    必须正确处理：
        600/601/603/605/688 → SSE
        000/001/002/003/300/301 → SZSE
    """
    prefix3 = code[:3]
    if prefix3 in ("600", "601", "603", "605", "688"):
        return "SSE"
    if prefix3 in ("000", "001", "002", "003", "300", "301"):
        return "SZSE"
    # 其余代码段（沪市基金/债券/科创板外扩、深市基金/债券、北交所等）
    if code.startswith(("60", "68", "90", "5", "11", "13")):
        return "SSE"
    if code.startswith(("00", "30", "20", "12", "15", "16", "18")):
        return "SZSE"
    if code.startswith(("43", "83", "87", "88", "92")):
        return "BSE"
    # 默认按沪市处理，后续可由人工纠正
    return "SSE"


def normalize_ticker(raw: str) -> NormalizedTicker:
    s = (raw or "").strip().upper()
    if not s:
        raise TickerParseError("empty ticker")

    # 600519.SH / 688981.SS
    m = re.fullmatch(r"(\d{6})\.(SH|SS|SZ)", s)
    if m:
        code, suffix = m.group(1), m.group(2)
        exchange = "SSE" if suffix in ("SH", "SS") else "SZSE"
        return NormalizedTicker(market="CN", exchange=exchange, ticker=f"{code}.SH" if exchange == "SSE" else f"{code}.SZ")

    # SH600519 / SZ000001
    m = re.fullmatch(r"(SH|SZ)(\d{6})", s)
    if m:
        prefix, code = m.group(1), m.group(2)
        return NormalizedTicker(
            market="CN", exchange="SSE" if prefix == "SH" else "SZSE",
            ticker=f"{code}.SH" if prefix == "SH" else f"{code}.SZ",
        )

    # 纯 6 位数字
    if A_SHARE_RE.fullmatch(s):
        exchange = _a_share_exchange(s)
        suffix = "SH" if exchange == "SSE" else "SZ"
        return NormalizedTicker(market="CN", exchange=exchange, ticker=f"{s}.{suffix}")

    # 美股：允许字母与 . - 组合（如 BRK.B / BRK-B、GOOGL）
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
        norm_sym = s.replace("-", ".")
        known_exchanges = {
            "TSM": "NYSE",
            "IBM": "NYSE",
        }
        exchange = known_exchanges.get(norm_sym, "")
        return NormalizedTicker(market="US", exchange=exchange, ticker=norm_sym)

    raise TickerParseError(f"unrecognized ticker format: {raw}")


def is_valid_ticker_format(raw: str) -> bool:
    """快速检查输入字符串是否符合合法证券代码格式。"""
    try:
        normalize_ticker(raw)
        return True
    except (TickerParseError, Exception):
        return False
