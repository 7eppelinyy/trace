"""Market Confirmation：把行情变化映射为 1–10 确认分。

Phase 1 行情主要用于 Market Confirmation，不做无新闻异动报警。
规则（集中在本模块，可调）：
    - 无行情数据：5.0（中性，不加减分）
    - 使用事件后 reaction_window 内的方向与幅度
    - 方向与 impact direction 一致才加分，反向扣分

交易时段门禁（必须，否则会污染 final_score）：
    change_pct_from_prev() 只是"当前价 vs 上一收盘"，它是否算作对某个事件的
    市场反应，取决于事件之后市场有没有开过盘：

        事件 04:00 ET（盘前）→ 现在还没开盘
            → 当前报价反映的是**事件之前**那个交易日的全天涨跌，与事件无关
        事件周五 18:00 ET（收盘后）→ 周六查
            → 同上；要到下周一开盘之后才谈得上"市场确认"
        事件三天前 → 现在
            → 今天的日内涨跌早已不是对该事件的反应

    这三种情况一律返回中性 5.0 并标记原因，而不是把无关涨跌以
    markets confirmation_weight（默认 0.15）的权重掺进 final_score。
    参考时刻取「事件时刻本身（若当时在交易时段）或事件之后的首次开盘」，
    这样盘后事件不会因为跨了一个周末就被误判为过期。

行情缓存与快照：
    - 同一轮里同一 ticker 会被 Stage B（每个 impact 一次）、日报（每只自选股
      一次）、回测账本（每个待核对 impact 一次）反复请求，加 TTL 缓存。
    - 取到的报价写入 market_snapshot，为事件锚定回测与事后审计
      （"当时那条 alert 的市场确认基于哪个价格"）提供价格历史。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from trace.collectors.market_data.base import MarketDataProvider, Quote
from trace.domain.models import MarketSnapshot

logger = logging.getLogger(__name__)

# 市场确认的数据模式（写入 event_impact.market_data_mode）
MODE_NO_QUOTE = "no_quote"
MODE_MARKET_NOT_OPENED = "market_not_opened_since_event"
MODE_WINDOW_EXPIRED = "reaction_window_expired"

NEUTRAL_SCORE = 5.0


def confirmation_score(direction: str, change_pct: float | None) -> float:
    """direction: bullish/bearish/neutral/mixed/uncertain。返回 1–10。"""
    if change_pct is None:
        return 5.0
    if direction in ("neutral", "mixed", "uncertain"):
        return 5.0
    magnitude = min(abs(change_pct) / 5.0, 1.0) * 5.0  # ±5% 映射到 ±5 分
    if direction == "bullish":
        return max(1.0, min(10.0, 5.0 + magnitude))
    if direction == "bearish":
        return max(1.0, min(10.0, 5.0 - magnitude))
    return 5.0


@dataclass
class MarketConfirmation:
    """一次市场确认的完整结果（分数 + 依据 + 数据模式）。"""
    score: float
    quote: Quote | None
    mode: str                       # real / mock / unavailable / no_quote / 门禁原因
    change_pct: float | None = None

    @property
    def is_neutral(self) -> bool:
        return self.score == NEUTRAL_SCORE


class MarketConfirmer:
    def __init__(self, us_provider: MarketDataProvider, cn_provider: MarketDataProvider,
                 *, calendar=None, snapshot_repo=None,
                 cache_ttl_seconds: float = 60.0,
                 max_reaction_hours: float = 24.0):
        self._providers = {us_provider.market: us_provider, cn_provider.market: cn_provider}
        self._calendar = calendar
        self._snapshots = snapshot_repo
        self._cache_ttl = float(cache_ttl_seconds)
        self._max_reaction_hours = float(max_reaction_hours)
        self._cache: dict[tuple[str, str], tuple[float, Quote | None]] = {}

    # ------------------------------------------------------------------
    def invalidate_cache(self) -> None:
        """清空行情缓存（每轮 run 开始调用；测试里也用来强制重取）。"""
        self._cache.clear()

    def quote(self, market: str, ticker: str, *,
              security_id: str | None = None) -> Quote | None:
        """取报价（TTL 缓存 + 快照落库）。

        缓存命中时不重复写快照：快照代表"我们在某时刻真实观测到的价格"，
        重复写同一观测没有意义（PK 是 security_id+ts，也会被覆盖）。
        """
        provider = self._providers.get(market)
        if provider is None:
            return None

        key = (market, ticker)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._cache_ttl:
            return cached[1]

        quote = provider.get_quote(ticker)
        self._cache[key] = (now, quote)
        if quote is not None and security_id:
            self._record_snapshot(security_id, quote)
        return quote

    def _record_snapshot(self, security_id: str, quote: Quote) -> None:
        """落库价格历史。失败不得影响主流程（行情是旁路信号）。"""
        if self._snapshots is None:
            return
        try:
            self._snapshots.insert(MarketSnapshot(
                security_id=security_id, ts=quote.ts,
                last_price=quote.last_price, prev_close=quote.prev_close,
                change_pct_1m=quote.change_pct_1m,
                change_pct_5m=quote.change_pct_5m,
                change_pct_15m=quote.change_pct_15m,
                volume=quote.volume, volume_ratio=quote.volume_ratio,
                session=quote.session))
        except Exception:
            logger.exception("market snapshot persist failed: %s", security_id)

    # ------------------------------------------------------------------
    def confirm(self, market: str, ticker: str, direction: str, *,
                event_time: datetime | None = None,
                security_id: str | None = None,
                now: datetime | None = None) -> MarketConfirmation:
        """对某证券做一次市场确认。

        event_time 缺省时不做时段门禁（保持旧行为）；传入时按交易日历判断
        当前报价是否谈得上"对该事件的反应"，谈不上就返回中性 5.0。
        """
        provider = self._providers.get(market)
        if provider is None:
            return MarketConfirmation(NEUTRAL_SCORE, None, "unavailable")

        # 先取报价再判门禁：即使这次不能用作市场确认，价格本身仍要留进
        # market_snapshot —— 盘后事件的锚点价正是"事件之前那个收盘价"，
        # 门禁一挡就不取的话，恰恰是最重要的盘后公告永远拿不到回测锚点。
        # TTL 缓存保证每轮每个 ticker 至多一次真实请求。
        quote = self.quote(market, ticker, security_id=security_id)
        if quote is None:
            mode = getattr(provider, "data_mode", "real")
            return MarketConfirmation(
                NEUTRAL_SCORE, None,
                MODE_NO_QUOTE if mode == "real" else mode)

        blocked = self._reaction_gate(market, event_time, now)
        if blocked:
            logger.info("market confirmation suppressed for %s (%s)", ticker, blocked)
            return MarketConfirmation(NEUTRAL_SCORE, quote, blocked)

        change = (quote.change_pct_15m if quote.change_pct_15m is not None
                  else quote.change_pct_from_prev())
        return MarketConfirmation(
            score=confirmation_score(direction, change),
            quote=quote,
            mode=getattr(provider, "data_mode", "real"),
            change_pct=change)

    # ------------------------------------------------------------------
    def _reaction_gate(self, market: str, event_time: datetime | None,
                       now: datetime | None) -> str:
        """返回 "" 表示当前报价可用作该事件的市场确认；否则返回抑制原因。"""
        if event_time is None or self._calendar is None:
            return ""
        if not getattr(self._calendar, "knows_market", lambda _m: False)(market):
            return ""       # 该市场没有日历配置：不做门禁，保持旧行为

        now = now or datetime.now(timezone.utc)
        event_time = _aware(event_time)
        now = _aware(now)
        if now < event_time:
            return MODE_MARKET_NOT_OPENED       # 事件时间在未来：无从确认

        # 参考时刻：事件当时若在交易时段就用事件时刻，否则用事件后首次开盘
        try:
            if self._calendar.is_market_open(market, event_time):
                reference = event_time
            else:
                reference = _aware(self._calendar.next_open(market, event_time))
        except Exception:
            logger.exception("market calendar lookup failed for %s", market)
            return ""

        if now < reference:
            return MODE_MARKET_NOT_OPENED       # 事件之后市场还没开过盘
        if (now - reference).total_seconds() > self._max_reaction_hours * 3600:
            return MODE_WINDOW_EXPIRED          # 早已不是对该事件的反应
        return ""

    # ------------------------------------------------------------------
    def data_mode(self, market: str) -> str:
        """返回该市场的行情数据模式：real / mock / unavailable。

        任务书 §7：使用 Mock 行情必须明确标记 market_data_mode=mock；
        没有行情标记 none，不得生成虚构市场确认。
        逐次确认的更精确模式见 MarketConfirmation.mode。
        """
        provider = self._providers.get(market)
        if provider is None:
            return "none"
        return getattr(provider, "data_mode", "real")


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def build_confirmer(us_provider: MarketDataProvider, cn_provider: MarketDataProvider,
                    config, calendar=None, snapshot_repo=None) -> MarketConfirmer:
    """按 settings.yaml 装配（阈值集中配置，禁止散落硬编码）。"""
    return MarketConfirmer(
        us_provider, cn_provider,
        calendar=calendar, snapshot_repo=snapshot_repo,
        cache_ttl_seconds=float(config.get("markets.quote_cache_ttl_seconds", 60)),
        max_reaction_hours=float(config.get("markets.confirmation_max_age_hours", 24)))
