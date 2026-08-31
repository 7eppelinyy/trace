"""代码规范化完备性 + 敏感信息脱敏测试。

任务书 §3/§14/§15：
    - A股各代码段正确归一化（600/601/603/605/688→SH；000/001/002/003/300/301→SZ）
    - 同一证券不同写法只对应一个 Security
    - 时区转换（已在 test_alerts.py 覆盖）
    - 敏感信息不进入日志
"""

from __future__ import annotations

import logging

import pytest

from trace.common.tickers import TickerParseError, normalize_ticker
from trace.db.repositories import SecurityRepo
from trace.domain.models import Security


@pytest.mark.parametrize("raw,expected,exchange", [
    ("600519", "600519.SH", "SSE"),
    ("601138", "601138.SH", "SSE"),
    ("603986", "603986.SH", "SSE"),
    ("605499", "605499.SH", "SSE"),
    ("688981", "688981.SH", "SSE"),
    ("000021", "000021.SZ", "SZSE"),
    ("001979", "001979.SZ", "SZSE"),
    ("002156", "002156.SZ", "SZSE"),
    ("003023", "003023.SZ", "SZSE"),
    ("300308", "300308.SZ", "SZSE"),
    ("301308", "301308.SZ", "SZSE"),
])
def test_all_a_share_prefixes(raw, expected, exchange):
    out = normalize_ticker(raw)
    assert out.market == "CN"
    assert out.exchange == exchange
    assert out.ticker == expected


def test_user_input_variants_single_security(db):
    """688981 / 688981.SH / SH688981 不允许产生三个 Security。"""
    repo = SecurityRepo(db)
    variants = ["688981", "688981.SH", "SH688981", "688981.sh", "688981.SS"]
    normalized = {normalize_ticker(v).ticker for v in variants}
    assert normalized == {"688981.SH"}

    # upsert 按 ticker 幂等：重复写入不会产生多条记录
    for _ in variants:
        repo.upsert(Security(security_id="sec-smic", market="CN",
                             exchange="SSE", ticker="688981.SH",
                             company_name_zh="中芯国际"))
    rows = repo.list_all()
    assert len([r for r in rows if r.ticker == "688981.SH"]) == 1


def test_watch_command_normalization(db):
    """bot /watch 场景：任何合法写法都解析到同一 security。"""
    repo = SecurityRepo(db)
    existing = repo.get_by_ticker(normalize_ticker("SH688981").ticker)
    assert existing is not None and existing.company_name_zh == "中芯国际"


def test_us_ticker_case_insensitive():
    assert normalize_ticker("nvda").ticker == "NVDA"
    assert normalize_ticker("Mu").ticker == "MU"


def test_invalid_a_share_length():
    with pytest.raises(TickerParseError):
        normalize_ticker("68898")                    # 5 位
    with pytest.raises(TickerParseError):
        normalize_ticker("6889810")                  # 7 位


# ---------------------------------------------------------------------------
# 敏感信息不进入日志（任务书 §15）
# ---------------------------------------------------------------------------

def test_secret_redaction_filter_masks_tokens():
    from trace.common.observability import SecretRedactionFilter
    f = SecretRedactionFilter()

    cases = [
        "bot token is 1234567890:AA1234567890abcdefghijklmnopqrst",
        "using api_key=sk-abcdefghijklmnop1234",
        "TELEGRAM_BOT_TOKEN: 1111111111:AAzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
        "openai token=sk-proj-xyz1234567890abcdef",
    ]
    for msg in cases:
        record = logging.LogRecord("t", logging.INFO, "", 0, msg, None, None)
        f.filter(record)
        out = record.getMessage()
        assert "1234567890:AA" not in out
        assert "sk-abcdefghijklmnop1234" not in out
        assert "[REDACTED]" in out


def test_plain_message_not_redacted():
    from trace.common.observability import SecretRedactionFilter
    f = SecretRedactionFilter()
    record = logging.LogRecord("t", logging.INFO, "", 0,
                               "run-abc123 collected 5 raw items", None, None)
    f.filter(record)
    assert record.getMessage() == "run-abc123 collected 5 raw items"


def test_run_id_filter_attaches_run_id():
    from trace.common.observability import RunIdFilter, new_run_id
    rid = new_run_id()
    record = logging.LogRecord("t", logging.INFO, "", 0, "x", None, None)
    RunIdFilter().filter(record)
    assert record.run_id == rid
