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


def test_jin10_fetches_from_feed_head_each_run(db, config, monkeypatch):
    """回归：每轮必须从 feed 头部(cursor=None)取最新页，而不得拿存下的
    next_cursor 续翻 —— 续翻会一路翻进历史深处、永远回不到头部的新条目，
    导致"采集器健康但零产出"。见 jin10.py collect() 的注释。"""
    from trace.collectors.jin10 import Jin10Collector

    monkeypatch.setenv("JIN10_BEARER_TOKEN", "test-token")
    collector = Jin10Collector(db, config)
    # 预置一个"上轮存下的位置"游标；若采集器拿它当续传起点就是 bug
    collector.cursor_repo.set(
        "src_jin10", {"seen_ids": ["old"], "next_cursor": "1788307413690"})

    captured = {}

    async def fake_fetch(token, cursor, max_pages):
        captured["cursor"] = cursor
        return [], None

    monkeypatch.setattr(collector, "_fetch_flash", fake_fetch)

    items = collector.collect()
    assert items == []
    assert captured["cursor"] is None, "必须从 feed 头部取，忽略 stored next_cursor"
    # 持久化的游标不再保存 next_cursor（续传位已废弃）
    persisted = collector.cursor_repo.get("src_jin10")
    assert "next_cursor" not in persisted
    assert "seen_ids" in persisted
