from datetime import datetime, timezone
import logging
import time
from fastapi import APIRouter
import httpx

from trace.api.schemas import IndexQuoteItem
from trace.common.modes import TraceMode
from trace.collectors.market_data.time_quality import parse_market_timestamp

router = APIRouter(prefix="/market", tags=["market"])
logger = logging.getLogger(__name__)


_cached_indices: list[IndexQuoteItem] = []
_last_fetch_ts: float = 0.0


_CACHE_FRESH_SECONDS = 20.0
_CACHE_STALE_SECONDS = 300.0       # 5 分钟后标记为 stale
_CACHE_MAX_AGE_SECONDS = 1800.0    # 30 分钟后超出最大展示年龄，置为 unavailable 且隐藏数值


@router.get("/indices", response_model=list[IndexQuoteItem])
def get_market_indices():
    """获取美股三大指数及 A 股科创 50 实时行情（严格区分 real、cached、stale、unavailable、mock）。"""
    global _cached_indices, _last_fetch_ts
    now = time.monotonic()
    if _cached_indices and (now - _last_fetch_ts < _CACHE_FRESH_SECONDS):
        return _cached_indices

    # 腾讯行情接口支持跨市场指数快照：
    # us.INX (标普500), us.IXIC (纳斯达克), us.DJI (道琼斯), sh000688 (科创50)
    url = "https://qt.gtimg.cn/q=us.INX,us.IXIC,us.DJI,sh000688"
    fetch_time = datetime.now(timezone.utc)
    try:
        with httpx.Client(timeout=4.0, trust_env=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="ignore")

            parsed: dict[str, IndexQuoteItem] = {}
            for line in text.strip().split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                left, right = line.split("=", 1)
                sym = left.replace("v_", "").strip()
                parts = right.strip('"\n; ').split("~")
                if len(parts) < 33:
                    continue

                # 时间戳解析 (F15)
                ts_str = parts[30] if len(parts) > 30 else ""
                is_us = sym.startswith('us.')
                market_ts = parse_market_timestamp(ts_str, 'US' if is_us else 'CN')
                as_of_dt = market_ts
                age = (fetch_time - market_ts).total_seconds() if market_ts else None
                quality = 'unknown_timestamp' if age is None else ('invalid_timestamp' if age < -30 else (
                    'stale' if age > (1200 if is_us else 300) else ('delayed' if is_us else 'real')))

                source_name = "tencent"
                curr = "USD" if is_us else "CNY"
                if "us.INX" in sym:
                    parsed["SPX"] = IndexQuoteItem(
                        name="标普 500", code="SPX",
                        price=round(float(parts[3]), 2),
                        change_pct=round(float(parts[32]), 2),
                        status=quality,
                        as_of=as_of_dt,
                        market_timestamp=market_ts,
                        fetched_at=fetch_time,
                        source=source_name,
                        currency=curr,
                        is_delayed=is_us,
                        change_basis="prev_close",
                        quality=quality,
                    )
                elif "us.IXIC" in sym:
                    parsed["IXIC"] = IndexQuoteItem(
                        name="纳斯达克", code="IXIC",
                        price=round(float(parts[3]), 2),
                        change_pct=round(float(parts[32]), 2),
                        status=quality,
                        as_of=as_of_dt,
                        market_timestamp=market_ts,
                        fetched_at=fetch_time,
                        source=source_name,
                        currency=curr,
                        is_delayed=is_us,
                        change_basis="prev_close",
                        quality=quality,
                    )
                elif "us.DJI" in sym:
                    parsed["DJI"] = IndexQuoteItem(
                        name="道琼斯", code="DJI",
                        price=round(float(parts[3]), 2),
                        change_pct=round(float(parts[32]), 2),
                        status=quality,
                        as_of=as_of_dt,
                        market_timestamp=market_ts,
                        fetched_at=fetch_time,
                        source=source_name,
                        currency=curr,
                        is_delayed=is_us,
                        change_basis="prev_close",
                        quality=quality,
                    )
                elif "sh000688" in sym:
                    parsed["STAR50"] = IndexQuoteItem(
                        name="科创 50", code="STAR50",
                        price=round(float(parts[3]), 2),
                        change_pct=round(float(parts[32]), 2),
                        status=quality,
                        as_of=as_of_dt,
                        market_timestamp=market_ts,
                        fetched_at=fetch_time,
                        source=source_name,
                        currency=curr,
                        is_delayed=False,
                        change_basis="prev_close",
                        quality=quality,
                    )

            ordered_codes = ["SPX", "IXIC", "DJI", "STAR50"]
            items = [parsed[c] for c in ordered_codes if c in parsed]
            if len(items) == 4:
                _cached_indices = items
                _last_fetch_ts = now
                return items
    except Exception as exc:
        logger.warning("fetch market indices failed: %s", exc)

    # 若抓取失败但有历史缓存，根据缓存年龄平滑降级 (F15 / N05)
    # 严格规则：缓存命中/失败回退不能把 stale/delayed/unknown_timestamp 升级成 cached/real
    if _cached_indices:
        cache_age = now - _last_fetch_ts
        if cache_age < _CACHE_STALE_SECONDS:
            return [
                IndexQuoteItem(
                    name=c.name, code=c.code,
                    price=c.price, change_pct=c.change_pct,
                    status=c.status if c.status not in ("real", "ok") else "cached",
                    as_of=c.as_of,
                    market_timestamp=c.market_timestamp,
                    fetched_at=c.fetched_at,
                    source=c.source,
                    currency=c.currency,
                    is_delayed=c.is_delayed,
                    change_basis=c.change_basis,
                    quality=c.quality if c.quality not in ("real", "ok") else "cached",
                )
                for c in _cached_indices
            ]
        elif cache_age < _CACHE_MAX_AGE_SECONDS:
            return [
                IndexQuoteItem(
                    name=c.name, code=c.code,
                    price=c.price, change_pct=c.change_pct,
                    status="stale", as_of=c.as_of,
                    market_timestamp=c.market_timestamp,
                    fetched_at=c.fetched_at,
                    source=c.source,
                    currency=c.currency,
                    is_delayed=c.is_delayed,
                    change_basis=c.change_basis,
                    quality="stale",
                )
                for c in _cached_indices
            ]
        else:
            # 超过最大可展示年龄：置为 unavailable，隐藏价格与涨跌幅数值，不把远古陈旧报价当参考
            return [
                IndexQuoteItem(
                    name=c.name, code=c.code,
                    price=None, change_pct=None,
                    status="unavailable", as_of=c.as_of,
                    market_timestamp=c.market_timestamp,
                    fetched_at=c.fetched_at,
                    source=c.source,
                    currency=c.currency,
                    is_delayed=c.is_delayed,
                    change_basis=c.change_basis,
                    quality="unavailable",
                )
                for c in _cached_indices
            ]

    # 开发/测试模式且非生产模式下，若明确启用模拟演示数据，显式标记 status="mock"
    if not TraceMode.is_production() and TraceMode.is_test():
        return [
            IndexQuoteItem(name="标普 500", code="SPX", price=5618.24, change_pct=1.23, status="mock", as_of=None),
            IndexQuoteItem(name="纳斯达克", code="IXIC", price=17628.39, change_pct=1.56, status="mock", as_of=None),
            IndexQuoteItem(name="道琼斯", code="DJI", price=41503.10, change_pct=0.89, status="mock", as_of=None),
            IndexQuoteItem(name="科创 50", code="STAR50", price=1609.87, change_pct=-0.39, status="mock", as_of=None),
        ]

    # 无可用行情时明确返回 unavailable，严禁返回未标明的虚假数字
    return [
        IndexQuoteItem(name="标普 500", code="SPX", price=None, change_pct=None, status="unavailable", as_of=None),
        IndexQuoteItem(name="纳斯达克", code="IXIC", price=None, change_pct=None, status="unavailable", as_of=None),
        IndexQuoteItem(name="道琼斯", code="DJI", price=None, change_pct=None, status="unavailable", as_of=None),
        IndexQuoteItem(name="科创 50", code="STAR50", price=None, change_pct=None, status="unavailable", as_of=None),
    ]
