"""Alpaca 行情 Provider（美股首版）。

无 API Key 时自动降级为 MockUSMarketProvider，保证开发环境可运行。
未来可替换为 Massive 或其他授权 Provider，业务代码不变。

真实行情必须产出可用于市场确认的字段：
    - prev_close：snapshot.prevDailyBar（日线级确认的兜底）
    - change_pct_15m：最近两根 15Min bar（reaction_window 内的方向/幅度）
任一字段缺失时保持 None，由 MarketConfirmer 退回中性 5 分，
不得用 Mock 数据补位。
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

logger = logging.getLogger(__name__)


class AlpacaProvider(MarketDataProvider):
    market = "US"

    def __init__(self):
        self.base_url = os.environ.get("ALPACA_BASE_URL", "https://data.alpaca.markets")
        self._http = httpx.Client(
            timeout=15,
            headers={
                "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY", ""),
                "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
            },
        )

    def get_quote(self, ticker: str) -> Quote | None:
        try:
            resp = self._http.get(
                f"{self.base_url}/v2/stocks/{ticker}/snapshot",
                params={"feed": "iex"})
            resp.raise_for_status()
            snap = resp.json()
        except Exception as exc:
            logger.warning("alpaca snapshot %s failed: %s", ticker, exc)
            return None

        last_trade = (snap.get("latestTrade") or {}).get("p")
        trade_t_str = (snap.get("latestTrade") or {}).get("t")
        daily_close = (snap.get("dailyBar") or {}).get("c")
        bar_t_str = (snap.get("dailyBar") or {}).get("t")
        prev_close = (snap.get("prevDailyBar") or {}).get("c")
        price = last_trade or daily_close

        market_ts = None
        raw_t = trade_t_str or bar_t_str
        if raw_t:
            try:
                # Alpaca RFC3339 timestamps (e.g. 2026-09-18T19:59:59.123456789Z)
                clean_t = str(raw_t).rstrip("Z").split(".")[0]
                market_ts = datetime.fromisoformat(clean_t).replace(tzinfo=timezone.utc)
            except Exception:
                market_ts = None

        fetched_at = datetime.now(timezone.utc)
        px_val = float(price) if price else None
        prev_val = float(prev_close) if prev_close else None
        day_chg = round((px_val - prev_val) / prev_val * 100, 2) if (px_val and prev_val) else None

        quote = Quote(
            ticker=ticker,
            ts=market_ts or fetched_at,
            last_price=px_val,
            prev_close=prev_val,
            change_pct_day=day_chg,
            market_timestamp=market_ts,
            fetched_at=fetched_at,
            currency="USD",
            source="alpaca",
            is_delayed=False,
        )
        # 15 分钟变动失败不影响整条 Quote（日线 prev_close 仍可确认）
        quote.change_pct_15m = self._change_pct_15m(ticker)
        return quote

    def _change_pct_15m(self, ticker: str) -> float | None:
        """最近两根 15Min bar 的变动 (F16 修复：使用 sort=desc 取最新的两根，杜绝旧 bar 混淆)。"""
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=2)
        try:
            resp = self._http.get(
                f"{self.base_url}/v2/stocks/{ticker}/bars",
                params={
                    "feed": "iex", "timeframe": "15Min", "limit": 2,
                    "sort": "desc",
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            resp.raise_for_status()
            bars = resp.json().get("bars") or []
        except Exception as exc:
            logger.debug("alpaca bars %s failed: %s", ticker, exc)
            return None
        if len(bars) < 2:
            return None
        # 确保按时间先后正序排列 (F16)
        if bars[0].get("t") and bars[-1].get("t"):
            chronological = sorted(bars, key=lambda b: str(b.get("t", "")))
        else:
            chronological = bars
        from trace.collectors.market_data.time_quality import parse_market_timestamp
        first_ts = parse_market_timestamp(str(chronological[0].get('t') or ''), 'US')
        last_ts = parse_market_timestamp(str(chronological[-1].get('t') or ''), 'US')
        if not first_ts or not last_ts or (last_ts-first_ts).total_seconds() != 900:
            return None
        open_px = chronological[0].get("c")
        close_px = chronological[-1].get("c")
        if not open_px or not close_px:
            return None
        return round((close_px - open_px) / open_px * 100, 2)


class MockUSMarketProvider(MarketDataProvider):
    """开发阶段 Mock：基于确定种子的随机行情（显式标记 data_mode=mock）。"""

    market = "US"
    data_mode = "mock"

    def __init__(self, seed: int = 42):
        self._rng = random.Random(seed)

    def get_quote(self, ticker: str) -> Quote | None:
        base = 50 + hash(ticker) % 400
        last = base * (1 + self._rng.uniform(-0.03, 0.03))
        prev = base
        now_dt = datetime.now(timezone.utc)
        day_chg = round((last - prev) / prev * 100, 2)
        return Quote(
            ticker=ticker,
            ts=now_dt,
            last_price=round(last, 2),
            prev_close=round(prev, 2),
            change_pct_day=day_chg,
            change_pct_15m=round(day_chg * 0.3, 2),
            volume=self._rng.randint(100_000, 5_000_000),
            volume_ratio=round(self._rng.uniform(0.5, 3.0), 2),
            market_timestamp=now_dt,
            fetched_at=now_dt,
            currency="USD",
            source="mock",
            is_delayed=False,
        )


class TencentUSMarketProvider(MarketDataProvider):
    """基于公开合规快照行情的美股实时 Provider。"""

    market = "US"
    data_mode = "real"

    def __init__(self, timeout: float = 5.0, client: httpx.Client | None = None):
        self.timeout = timeout
        self._client = client

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            timeout=self.timeout,
            trust_env=False,
            headers={"User-Agent": "TraceUSMarket/0.1"},
        )

    def get_quote(self, ticker: str) -> Quote | None:
        raw = (ticker or "").strip().upper()
        quotes = self.get_quotes([raw])
        return quotes.get(raw)

    def get_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        valid_tickers = [t.strip().upper() for t in tickers if (t or "").strip()]
        if not valid_tickers:
            return {}
        sym_param = ",".join(f"us{t}" for t in valid_tickers)
        url = f"https://qt.gtimg.cn/q={sym_param}"
        results: dict[str, Quote] = {}
        client = self._get_client()
        owns_client = self._client is None
        fetched_at = datetime.now(timezone.utc)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            for line in resp.text.strip().split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                left, right = line.split("=", 1)
                sym = left.replace("v_us", "").replace("v_", "").strip().upper()
                payload = right.strip('";\n')
                parts = payload.split("~")
                if len(parts) < 34:
                    continue
                try:
                    last_price = float(parts[3])
                    prev_close = float(parts[4])
                    change_pct = float(parts[32]) if len(parts) > 32 and parts[32] else None
                    # parts[30] 时间戳 (格式通常如 '2026-09-18 16:00:00')
                    ts_str = parts[30] if len(parts) > 30 else ""
                    from trace.collectors.market_data.time_quality import parse_market_timestamp
                    market_ts = parse_market_timestamp(ts_str, 'US')

                    # F15: 日涨跌与 15m 严格分离，腾讯快照为日涨跌，不可替代 15m
                    results[sym] = Quote(
                        ticker=sym,
                        ts=market_ts or fetched_at,
                        last_price=round(last_price, 2),
                        prev_close=round(prev_close, 2),
                        change_pct_day=round(change_pct, 2) if change_pct is not None else None,
                        change_pct_15m=None,
                        market_timestamp=market_ts,
                        fetched_at=fetched_at,
                        currency="USD",
                        source="tencent",
                        is_delayed=True,
                    )
                except (ValueError, IndexError):
                    continue
        except Exception as exc:
            logger.warning("batch tencent us quote fetch failed for %s: %s", tickers, exc)
        finally:
            if owns_client:
                client.close()
        return results


def build_us_provider() -> MarketDataProvider:
    if os.environ.get("ALPACA_API_KEY"):
        return AlpacaProvider()
    try:
        return TencentUSMarketProvider()
    except Exception:
        pass
    # 生产模式禁止 Mock 行情：未真实接入时显式标记 unavailable
    if TraceMode.is_production():
        logger.info("ALPACA_API_KEY not set (production): market data unavailable "
                    "(mock forbidden in production)")
        return UnavailableMarketProvider("US")
    logger.info("ALPACA_API_KEY not set, using MockUSMarketProvider")
    return MockUSMarketProvider()
