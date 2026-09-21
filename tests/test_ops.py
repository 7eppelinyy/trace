"""运营能力测试：备份轮换 / 运行历史 / 摘要自动推送 / 矩阵余弦。

    - 备份：一致性快照可打开查询；按 keep 轮换；长驻循环同天幂等
    - 运行历史：run_once 摘要落库、recent、prune
    - 摘要：到点推送一次，幂等；enabled=false 关闭；
      production 无通道不生成（不得伪装已推送）
    - pairwise_cosines：numpy 矩阵路径与纯 Python 回退路径结果一致
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from trace.alerts.engine import DeliveryReceipt
from trace.db.backup import create_backup, latest_backup, rotate
from trace.db.repositories import RunHistoryRepo
from trace.event_engine.embeddings import HashEmbedder, pairwise_cosines
from trace.pipeline import Pipeline


# ---------------------------------------------------------------------------
# 备份与轮换
# ---------------------------------------------------------------------------

def _make_db(path) -> None:
    from trace.db.connection import Database
    from trace.db.migration import apply_migrations
    database = Database(path)
    apply_migrations(database)
    database.close()
    conn = sqlite3.connect(str(path))
    with conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
    conn.close()


def test_backup_snapshot_readable(tmp_path):
    src = tmp_path / "trace.db"
    _make_db(src)
    backup_dir = tmp_path / "backups"
    dest = create_backup(src, backup_dir, keep=3,
                         now=datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc))
    assert dest.exists()
    conn = sqlite3.connect(str(dest))
    assert conn.execute("SELECT x FROM t").fetchone()[0] == 42
    conn.close()


def test_backup_rotation_keeps_newest(tmp_path):
    src = tmp_path / "trace.db"
    _make_db(src)
    backup_dir = tmp_path / "backups"
    for h in range(5):
        create_backup(src, backup_dir, keep=2,
                      now=datetime(2026, 9, 3, 12, h, 0, tzinfo=timezone.utc))
    kept = rotate(backup_dir, keep=2)
    assert len(kept) == 2
    # 字典序即时间序：保留的是最新两份
    names = [p.name for p in kept]
    assert names == sorted(names, reverse=True)
    assert latest_backup(backup_dir).name == names[0]


# ---------------------------------------------------------------------------
# 运行历史
# ---------------------------------------------------------------------------

def test_run_history_roundtrip_and_prune(db, config):
    from trace.pipeline import RunSummary

    repo = RunHistoryRepo(db)
    for i in range(3):
        summary = RunSummary(run_id=f"run-{i}",
                             started_at=f"2026-09-0{i + 1}T00:00:00+00:00",
                             trace_mode="production", status="OK")
        summary.sources_checked = 10
        summary.raw_items_new = i
        repo.insert(summary)
    rows = repo.recent(2)
    assert [r["run_id"] for r in rows] == ["run-2", "run-1"]
    assert rows[0]["raw_items_new"] == 2
    assert rows[0]["failed_sources"] == []

    deleted = repo.prune(keep=2)
    assert deleted == 1
    assert len(repo.recent(10)) == 2


def test_run_once_persists_history(app, monkeypatch):
    """pipeline.run_once 的摘要必须落库（可观测性闭环）。"""
    monkeypatch.setenv("TRACE_MODE", "offline")
    from trace.collectors.base import BaseCollector, CollectorRegistry
    from trace.domain.models import RawItem
    from trace.common.ids import raw_item_id

    class _Empty(BaseCollector):
        collector_type = "scripted"
        @property
        def handled_source_ids(self):
            return {"src_sec_edgar"}
        def collect(self):
            return [RawItem(raw_item_id=raw_item_id(), source_id="src_sec_edgar",
                            source_item_id=f"edgar:{i}", title=f"t{i}",
                            url=f"https://sec.gov/{i}", language="en")
                    for i in range(2)]

    registry = CollectorRegistry()
    registry.register(_Empty(app.db, app.config, ))
    app.collectors = registry
    pipeline = Pipeline(app)
    summary = pipeline.run_once()

    rows = RunHistoryRepo(app.db).recent(1)
    assert rows and rows[0]["run_id"] == summary.run_id
    assert rows[0]["started_at"]
    assert rows[0]["raw_items_new"] == summary.raw_items_new


# ---------------------------------------------------------------------------
# 每日摘要自动推送（长驻循环维护任务）
# ---------------------------------------------------------------------------

def _wire_digest(app, sent_box):
    pipeline = Pipeline(app)

    def fake_send(user_id, text, url):
        sent_box.append((user_id, text))
        return DeliveryReceipt(status="sent", chat_id=user_id,
                               message_id=str(len(sent_box)), response="ok")

    pipeline.alert_sender = fake_send
    return pipeline


def test_digest_sent_once_per_day(app, monkeypatch):
    monkeypatch.setenv("TRACE_MODE", "offline")
    app.config.raw["digest"] = {"enabled": True, "send_time": "00:00"}
    sent: list = []
    pipeline = _wire_digest(app, sent)

    pipeline._maybe_send_digest()
    assert len(sent) == 1
    assert "每日事件摘要" in sent[0][1]

    # 同一天重复触发：幂等，不重复推送
    pipeline._maybe_send_digest()
    assert len(sent) == 1


def test_digest_disabled_or_before_time(app, monkeypatch):
    monkeypatch.setenv("TRACE_MODE", "offline")
    sent: list = []
    pipeline = _wire_digest(app, sent)

    app.config.raw["digest"] = {"enabled": False, "send_time": "00:00"}
    pipeline._maybe_send_digest()
    assert sent == []

    app.config.raw["digest"] = {"enabled": True, "send_time": "23:59"}
    pipeline._maybe_send_digest()
    assert sent == []                        # 未到推送时间


def test_digest_production_without_channel_skipped(app, monkeypatch):
    """production 无投递通道：不生成摘要记录（不得伪装已推送）。"""
    monkeypatch.setenv("TRACE_MODE", "production")
    app.config.raw["digest"] = {"enabled": True, "send_time": "00:00"}
    pipeline = Pipeline(app)                 # 无 alert_sender
    pipeline._maybe_send_digest()
    assert app.digest_builder.digest_repo.get(
        datetime.now(timezone.utc).astimezone(
            __import__("pytz").timezone(
                app.config.telegram.default_user_timezone)).date().isoformat()) is None


# ---------------------------------------------------------------------------
# pairwise_cosines：numpy 矩阵路径 vs 纯 Python 回退
# ---------------------------------------------------------------------------

def test_pairwise_cosines_matches_pure_python():
    import math

    def pure_cosine(a, b):
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return max(0.0, min(1.0, dot / (na * nb))) if na and nb else 0.0

    embedder = HashEmbedder(dim=64)
    vecs = embedder.encode(["micron dram price", "nand supply", "无关内容", ""])
    q = embedder.encode(["dram price increase"])[0]

    fast = pairwise_cosines(q, vecs)

    import trace.event_engine.embeddings as emb
    original = emb._np
    emb._np = None                          # 强制纯 Python 回退路径
    try:
        slow = pairwise_cosines(q, vecs)
    finally:
        emb._np = original

    assert len(fast) == len(slow) == 4
    for f, s in zip(fast, slow):
        assert f == pytest.approx(s, abs=1e-9)
    assert fast[0] > fast[2]                # 相关 > 无关（语义可分）
