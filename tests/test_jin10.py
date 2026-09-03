"""金十采集器纯函数测试（离线，不依赖网络 / MCP / token）。

只覆盖 get_quote 之外的纯逻辑：时间解析与 list_flash 响应的 JSON 解析。
collect() 走真实 MCP，属于 live 链路，不在离线套件内。
"""

from __future__ import annotations

from datetime import timezone

from trace.collectors.jin10 import _parse_time, parse_flash_json


def test_parse_time_iso_with_offset():
    dt = _parse_time("2026-09-03T21:11:59+08:00")
    assert dt is not None
    assert dt.tzinfo is not None
    # +08:00 -> UTC 偏移 8 小时
    assert dt.astimezone(timezone.utc).hour == 13


def test_parse_time_naive_treated_utc():
    dt = _parse_time("2026-09-03 07:00")
    assert dt is not None
    assert dt.astimezone(timezone.utc).hour == 7


def test_parse_time_empty_or_none():
    assert _parse_time("") is None
    assert _parse_time(None) is None


def test_parse_flash_json():
    text = ('{"status":200,"data":{"has_more":true,"next_cursor":"1788440723394",'
            '"items":[{"content":"快讯内容","time":"2026-09-03T21:11:59+08:00",'
            '"url":"https://flash.jin10.com/detail/20260903211159272800"}]}}')
    items = parse_flash_json(text)
    assert len(items) == 1
    assert items[0]["url"].endswith("20260903211159272800")
