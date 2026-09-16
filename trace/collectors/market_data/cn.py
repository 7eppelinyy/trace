"""A股行情 Provider。

优先使用腾讯财经等合规公开快照行情服务或正式授权行情服务。
开发阶段允许 Mock / 历史数据 / 合规开发数据。
实现 CNMarketDataProvider 接口，业务代码保持不变。
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta, timezone

import httpx

from trace.collectors.market_data.base import (
    MarketDataProvider,
    Quote,
    UnavailableMarketProvider,
)
from trace.common.modes import TraceMode
from trace.common.tickers import normalize_ticker

logger = logging.getLogger(__name__)


class MockCNMarketProvider(MarketDataProvider):
    """开发阶段 Mock（显式标记 data_mode=mock）。正式接入时替换为真实 Adapter。"""

    market = "CN"
    data_mode = "mock"

    def __init__(self, seed: int = 7):
        self._rng = random.Random(seed)

    def get_quote(self, ticker: str) -> Quote | None:
        base = 10 + abs(hash(ticker)) % 200
        last = base * (1 + self._rng.uniform(-0.05, 0.05))
        return Quote(
            ticker=ticker, ts=datetime.now(timezone.utc),
            last_price=round(last, 2), prev_close=round(base, 2),
            change_pct_15m=round((last - base) / base * 100, 2),
            volume=self._rng.randint(500_000, 20_000_000),
            volume_ratio=round(self._rng.uniform(0.5, 4.0), 2),
        )


class TencentCNMarketProvider(MarketDataProvider):
    """基于腾讯公开快照行情接口的 A 股实时行情 Provider（合规且支持批量抓取）。"""

    market = "CN"
    data_mode = "real"

    def __init__(self, timeout: float = 5.0, client: httpx.Client | None = None):
        self.timeout = timeout
        self._client = client

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": "TraceEventRadar/0.1 (research use)"},
        )

    def _to_tencent_symbol(self, ticker: str) -> str | None:
        raw = (ticker or "").strip().upper()
        if not raw:
            return None
        # 600519.SH / 000001.SZ / 688981.SS
        if "." in raw:
            parts = raw.split(".", 1)
            code, ext = parts[0], parts[1]
            if ext in ("SH", "SS"):
                return f"sh{code}"
            elif ext in ("SZ",):
                return f"sz{code}"
            elif ext in ("BJ",):
                return f"bj{code}"
        # SH600519 / SZ000001
        if raw.startswith(("SH", "SZ", "BJ")) and len(raw) >= 8:
            return raw.lower()
        # 纯 6 位数字代码通过 normalize_ticker 转换
        try:
            norm = normalize_ticker(raw)
            if norm.market == "CN":
                prefix = "sh" if norm.exchange == "SSE" else ("sz" if norm.exchange == "SZSE" else "bj")
                code = norm.ticker.split(".")[0]
                return f"{prefix}{code}"
        except Exception:
            pass
        return None

    def get_quote(self, ticker: str) -> Quote | None:
        quotes = self.get_quotes([ticker])
        return quotes.get(ticker)

    def get_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        sym_map: dict[str, str] = {}  # sym (sh688981) -> original ticker (688981.SH)
        for t in tickers:
            sym = self._to_tencent_symbol(t)
            if sym:
                sym_map[sym] = t

        if not sym_map:
            return {}

        url = f"https://qt.gtimg.cn/q={','.join(sym_map.keys())}"
        client = self._get_client()
        owns_client = self._client is None
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return self._parse_response(resp.text, sym_map)
        except Exception as exc:
            logger.warning("tencent quote fetch failed for %s: %s", tickers, exc)
            return {}
        finally:
            if owns_client:
                client.close()

    def _parse_response(self, text: str, sym_map: dict[str, str]) -> dict[str, Quote]:
        results: dict[str, Quote] = {}
        for line in text.strip().split(";"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            left, right = line.split("=", 1)
            sym = left.replace("v_", "").strip()
            orig_ticker = sym_map.get(sym)
            if not orig_ticker:
                continue
            payload = right.strip('"')
            parts = payload.split("~")
            if len(parts) < 32:
                continue
            try:
                last_price = float(parts[3])
                prev_close = float(parts[4])
                volume = int(parts[6]) if parts[6] else None
                # timestamp: parts[30] = 'YYYYMMDDHHMMSS' (CST, UTC+8)
                ts_str = parts[30] if len(parts) > 30 else ""
                if len(ts_str) == 14:
                    ts = datetime.strptime(ts_str, "%Y%m%d%H%M%S").replace(
                        tzinfo=timezone(timedelta(hours=8))
                    )
                else:
                    ts = datetime.now(timezone.utc)

                change_pct = float(parts[32]) if len(parts) > 32 and parts[32] else None

                results[orig_ticker] = Quote(
                    ticker=orig_ticker,
                    ts=ts,
                    last_price=last_price,
                    prev_close=prev_close,
                    change_pct_15m=change_pct,
                    volume=volume,
                    session="regular",
                )
            except (ValueError, IndexError) as exc:
                logger.debug("error parsing tencent quote for %s: %s", sym, exc)
                continue
        return results


def build_cn_provider() -> MarketDataProvider:
    """构建 A 股行情 Provider 工厂。"""
    choice = os.environ.get("CN_MARKET_PROVIDER", "").lower()
    if choice == "mock":
        return MockCNMarketProvider()
    if choice == "unavailable":
        return UnavailableMarketProvider("CN")
    if choice == "tencent":
        return TencentCNMarketProvider()

    # 测试模式默认使用 Mock 保证离线 CI 稳定性
    if TraceMode.is_test():
        return MockCNMarketProvider()

    logger.info("using TencentCNMarketProvider for real CN market quotes")
    return TencentCNMarketProvider()

