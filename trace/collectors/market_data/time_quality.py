"""Market timestamps and freshness are independent of fetch success."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def parse_market_timestamp(value: str, market: str):
    if not value:
        return None
    try:
        value = value.replace('/', '-').strip()
        dt = datetime.strptime(value, '%Y%m%d%H%M%S') if value.isdigit() and len(value) == 14 else datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo('America/New_York' if market == 'US' else 'Asia/Shanghai'))
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def quote_quality(quote, now=None):
    if quote is None or quote.last_price is None:
        return 'unavailable'
    if quote.source == 'mock':
        return 'mock'
    timestamp = quote.market_timestamp
    if timestamp is None or timestamp.tzinfo is None:
        return 'unknown_timestamp'
    age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
    if age < -30:
        return 'invalid_timestamp'
    if age > (1200 if quote.is_delayed else 300):
        return 'stale'
    return 'delayed' if quote.is_delayed else 'real'
