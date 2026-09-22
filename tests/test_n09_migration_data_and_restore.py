"""N09 验证：有历史数据的迁移、预测口径、备份恢复与隔离演练。

验证要点：
1. 0022 -> 0031 有关联数据的平滑迁移，验证 0025 raw_item & processing_job 表重建无数据丢失或外键断裂；
2. 迁移中途失败的 DDL 事务原子性回滚与并发安全；
3. 旧预测 legacy_unverified 隔离，新预测严格行情时间与基准同口径规则；
4. db_admin.py backup/restore 支持包含空格/中文路径，拒绝覆盖已有库；
5. 恢复演练遇到外键破坏、缺表、损坏文件、不存在路径时必须明确失败；
6. 恢复后 sending 状态隔离为 ambiguous，杜绝旧库回滚后的突发重复告警；
7. restore_drill.py 演练输出真实 RTO/RPO 并比对所有业务表 SHA256 哈希。
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trace.db.backup import (
    create_backup,
    restore_backup,
    quarantine_restored_outbox,
    verify_database_integrity,
)
from trace.db.connection import Database
from trace.db.migration import apply_migrations, MIGRATIONS_DIR
from trace.feedback.ledger import ForecastLedger
from trace.domain.models import ForecastSnapshot, Event, EventImpact, MarketSnapshot
from trace.db.repositories import (
    EventRepo,
    EventImpactRepo,
    ForecastSnapshotRepo,
    ForecastCheckRepo,
    MarketSnapshotRepo,
    UserRepo,
)
from scripts.restore_drill import run_drill

PYTHON_EXE = sys.executable


def _apply_migrations_up_to(db: Database, up_to_version: str) -> None:
    """仅应用到指定版本（包含）的迁移脚本。"""
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = sql_file.stem
        if version > up_to_version:
            break
        with db.transaction(mode="IMMEDIATE"):
            if db.query_one("SELECT 1 FROM schema_version WHERE version=?", (version,)):
                continue
            pending = ""
            for char in sql_file.read_text(encoding="utf-8"):
                pending += char
                if char == ";" and sqlite3.complete_statement(pending):
                    db.conn.execute(pending)
                    pending = ""
            if pending.strip():
                db.conn.execute(pending)
            db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def test_migration_0022_to_latest_preserves_interconnected_data(tmp_path):
    """测试具有完整业务关联外键的历史 0022 数据库安全迁移至最新版本。"""
    db_path = tmp_path / "legacy_v22.db"
    db = Database(db_path)

    # 1. 迁移至 0022
    _apply_migrations_up_to(db, "0022_hypothesis_tracking_and_feedback")

    # 2. 写入具有相互外键引用的代表性历史数据
    with db.transaction():
        # 用户与会话
        db.execute("INSERT INTO user (user_id, created_at) VALUES ('usr_old_1', '2026-01-01T00:00:00Z')")
        db.execute(
            "INSERT INTO user_session (session_token, user_id, created_at, expires_at, is_revoked) "
            "VALUES ('sess_tok_1', 'usr_old_1', '2026-01-01T00:00:00Z', '2026-12-31T00:00:00Z', 0)"
        )
        # 标的与自选
        db.execute("INSERT OR IGNORE INTO security (security_id, market, ticker) VALUES ('SEC-US-AAPL', 'US', 'AAPL')")
        db.execute("INSERT INTO watchlist (user_id, security_id, added_at) VALUES ('usr_old_1', 'SEC-US-AAPL', '2026-01-01T00:00:00Z')")

        # 来源与原材料
        db.execute("INSERT INTO source (source_id, source_name, source_type, enabled) VALUES ('src_reuters', 'Reuters News', 'rss', 1)")
        db.execute(
            "INSERT INTO raw_item (raw_item_id, source_id, source_item_id, title, url, canonical_url, published_at, fetched_at, language, content, reference, title_hash, content_hash, event_id) "
            "VALUES ('raw_item_1', 'src_reuters', 'item_101', 'Apple supply chain update', 'http://news.example.com/1', 'http://news.example.com/1', '2026-01-01T01:00:00Z', '2026-01-01T01:05:00Z', 'en', 'Content text', '', 'th1', 'ch1', 'evt_101')"
        )

        # 事件、来源关联与影响
        db.execute(
            "INSERT INTO event (event_id, title, summary, event_type, status, version, first_seen_at, event_time) "
            "VALUES ('evt_101', 'Apple supply chain event', 'Summary', 'supply_chain', 'reported', 1, '2026-01-01T01:05:00Z', '2026-01-01T01:00:00Z')"
        )
        db.execute(
            "INSERT INTO event_source (event_id, raw_item_id, role) "
            "VALUES ('evt_101', 'raw_item_1', 'primary')"
        )
        db.execute(
            "INSERT INTO event_impact (impact_id, event_id, security_id, direction, confidence, final_score, reason, created_at) "
            "VALUES ('imp_101', 'evt_101', 'SEC-US-AAPL', 'bullish', 0.85, 7.5, 'Supplier ramping', '2026-01-01T01:10:00Z')"
        )

        # 研究问题
        db.execute(
            "INSERT INTO research_question (question_id, user_id, event_id, security_id, title, hypothesis, state, created_at, updated_at) "
            "VALUES ('rq_101', 'usr_old_1', 'evt_101', 'SEC-US-AAPL', 'Apple Q1 ramp', 'Shipments will beat', 'tracking', '2026-01-01T02:00:00Z', '2026-01-01T02:00:00Z')"
        )

        # 任务与投递队列
        db.execute(
            "INSERT INTO processing_job (job_id, job_type, target_id, status, retry_count, max_retries, created_at, updated_at) "
            "VALUES ('job_101', 'event_analysis', 'evt_101', 'completed', 0, 3, '2026-01-01T01:05:00Z', '2026-01-01T01:10:00Z')"
        )
        db.execute(
            "INSERT INTO alert_outbox (outbox_id, user_id, channel_type, channel_target, event_id, impact_id, event_version, alert_type, status, idempotency_key, content_text, created_at, updated_at) "
            "VALUES ('out_sending', 'usr_old_1', 'telegram', '12345', 'evt_101', 'imp_101', 1, 'urgent', 'sending', 'key_send', 'Alert sending text', '2026-01-01T01:10:00Z', '2026-01-01T01:10:00Z')"
        )
        db.execute(
            "INSERT INTO alert_outbox (outbox_id, user_id, channel_type, channel_target, event_id, impact_id, event_version, alert_type, status, idempotency_key, content_text, created_at, updated_at) "
            "VALUES ('out_sent', 'usr_old_1', 'telegram', '12345', 'evt_101', 'imp_101', 1, 'urgent', 'sent', 'key_sent', 'Alert sent text', '2026-01-01T01:10:00Z', '2026-01-01T01:11:00Z')"
        )

        # 旧预测快照与核对记录
        db.execute(
            "INSERT INTO forecast_snapshot (snapshot_id, impact_id, event_id, security_id, event_version, predicted_direction, predicted_score, confidence, model_version, market, analysis_created_at, horizon_hours, due_at, status, created_at) "
            "VALUES ('snap_old_1', 'imp_101', 'evt_101', 'SEC-US-AAPL', 1, 'bullish', 7.5, 0.85, 'v1', 'US', '2026-01-01T01:10:00Z', 24.0, '2026-01-02T01:10:00Z', 'pending', '2026-01-01T01:10:00Z')"
        )
        db.execute(
            "INSERT INTO forecast_check (check_id, snapshot_id, impact_id, event_id, security_id, event_version, predicted_direction, predicted_score, confidence, outcome, evaluated_at, elapsed_hours, model_version, market) "
            "VALUES ('chk_old_1', 'snap_old_1', 'imp_101', 'evt_101', 'SEC-US-AAPL', 1, 'bullish', 7.5, 0.85, 'hit', '2026-01-02T01:10:00Z', 24.0, 'v1', 'US')"
        )

    # 3. 执行最新迁移（包含 0025 表重建、0026 预测来源口径隔离、0028-0031 会话与来源治理）
    apply_migrations(db)

    # 4. 验证完整性与数据保全
    integrity = verify_database_integrity(db_path)
    assert integrity["integrity_check"] == "ok"
    assert integrity["foreign_key_check"] == "ok"
    assert not integrity["missing_core_tables"]

    # 验证 0025 raw_item 表重建未丢数据，索引完整
    raw_row = db.query_one("SELECT * FROM raw_item WHERE raw_item_id='raw_item_1'")
    assert raw_row is not None
    assert raw_row["title"] == "Apple supply chain update"
    assert raw_row["event_id"] == "evt_101"

    # 验证 0025 processing_job 表重建未丢数据
    job_row = db.query_one("SELECT * FROM processing_job WHERE job_id='job_101'")
    assert job_row is not None
    assert job_row["status"] == "completed"

    # 验证 0025 将旧库处于 sending 状态的孤立项转为 ambiguous，防止回滚后突发告警
    outbox_sending = db.query_one("SELECT * FROM alert_outbox WHERE outbox_id='out_sending'")
    assert outbox_sending["status"] == "ambiguous"
    assert "legacy sending state" in outbox_sending["last_error"]
    assert outbox_sending["lease_until"] is None

    outbox_sent = db.query_one("SELECT * FROM alert_outbox WHERE outbox_id='out_sent'")
    assert outbox_sent["status"] == "sent"

    # 验证 0026 将旧预测快照与核对标记为 legacy_unverified
    snap_old = db.query_one("SELECT * FROM forecast_snapshot WHERE snapshot_id='snap_old_1'")
    assert snap_old["time_basis"] == "legacy_unverified"

    chk_old = db.query_one("SELECT * FROM forecast_check WHERE check_id='chk_old_1'")
    assert chk_old["time_basis"] == "legacy_unverified"

    # 验证最新迁移全部登记
    applied_versions = [r["version"] for r in db.query("SELECT version FROM schema_version ORDER BY version")]
    assert "0025_durable_versions_and_leases" in applied_versions
    assert "0026_forecast_provenance" in applied_versions
    assert "0031_source_governance_authorization" in applied_versions


def test_migration_atomic_rollback_on_failure(tmp_path):
    """测试迁移过程中遇到异常时 DDL 事务原子回滚，不产生残留 schema 也不写入版本回执。"""
    db_path = tmp_path / "rollback_test.db"
    db = Database(db_path)
    apply_migrations(db)

    # 记录当前迁移版本数与表数量
    v_count_before = db.query_one("SELECT COUNT(*) AS n FROM schema_version")["n"]
    tables_before = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}

    # 构造一个含致命语法错误和建表操作的伪迁移
    fake_sql = "CREATE TABLE temp_canary (id INT PRIMARY KEY); SYNTAX_ERROR_ABORT;"
    fake_version = "0999_failing_migration"

    with pytest.raises(sqlite3.OperationalError):
        with db.transaction(mode="IMMEDIATE"):
            db.execute("INSERT INTO schema_version (version) VALUES (?)", (fake_version,))
            for stmt in fake_sql.split(";"):
                if stmt.strip():
                    db.conn.execute(stmt)

    # 验证回滚：版本表未增加，金丝雀表不存在
    v_count_after = db.query_one("SELECT COUNT(*) AS n FROM schema_version")["n"]
    assert v_count_after == v_count_before

    tables_after = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables_after == tables_before
    assert "temp_canary" not in tables_after


def test_legacy_forecast_isolation_and_strict_benchmark_rules(db, config):
    """测试 legacy_unverified 隔离且基准指数与个股同样遵守新鲜度与可靠性检验。"""
    now = datetime.now(timezone.utc)
    UserRepo(db).ensure("usr_fc")

    # 1. 插入一个带有 legacy_unverified 的历史快照
    db.execute(
        "INSERT INTO forecast_snapshot (snapshot_id, impact_id, event_id, security_id, event_version, "
        "predicted_direction, predicted_score, confidence, model_version, market, horizon_hours, due_at, "
        "analysis_created_at, time_basis, status, created_at) "
        "VALUES ('snp_legacy', 'imp_leg', 'evt_leg', 'SEC-US-AAPL', 1, 'bullish', 8.0, 0.8, 'v1', 'US', 24.0, "
        "'2026-01-01T00:00:00Z', '2025-12-31T00:00:00Z', 'legacy_unverified', 'pending', '2025-12-31T00:00:00Z')"
    )

    # 2. 插入一个新的 analysis_recorded 快照
    db.execute(
        "INSERT INTO forecast_snapshot (snapshot_id, impact_id, event_id, security_id, event_version, "
        "predicted_direction, predicted_score, confidence, model_version, market, horizon_hours, due_at, "
        "analysis_created_at, anchor_price, anchor_ts, benchmark_code, benchmark_anchor_price, "
        "time_basis, status, created_at) "
        "VALUES ('snp_new', 'imp_new', 'evt_new', 'SEC-US-AAPL', 1, 'bullish', 8.0, 0.8, 'v1', 'US', 24.0, "
        f"'{ (now - timedelta(hours=1)).isoformat() }', "
        f"'{ (now - timedelta(hours=25)).isoformat() }', 100.0, "
        f"'{ (now - timedelta(hours=25)).isoformat() }', 'SPX', 5000.0, "
        "'analysis_recorded', 'pending', "
        f"'{ (now - timedelta(hours=25)).isoformat() }')"
    )

    # 验证 summary() 与 pending_count() 统计隔离
    check_repo = ForecastCheckRepo(db)
    summary = check_repo.summary()
    assert summary["legacy_unverified"] == 1
    assert summary["total_snapshots"] == 1  # 仅包含 analysis_recorded
    assert check_repo.pending_count() == 1  # 仅包含新快照

    # 3. 构造行情 Confirmer：个股行情有效，但大盘基准行情为 mock 或陈旧数据
    class _MockBenchmarkConfirmer:
        def quote(self, market: str, ticker: str, *, security_id: str | None = None):
            from trace.collectors.market_data.base import Quote
            if ticker == "AAPL":
                return Quote(ticker="AAPL", ts=now, market_timestamp=now, last_price=105.0)
            elif ticker == "SPX":
                # 模拟 mock 来源或陈旧基准行情，不能用来计算超额收益
                return Quote(ticker="SPX", ts=now, market_timestamp=now, last_price=5100.0, source="mock")
            return None

    ledger = ForecastLedger(db, config, _MockBenchmarkConfirmer())
    snap = ForecastSnapshotRepo(db).get("snp_new")
    check = ledger._check_snapshot(snap, now)
    assert check is not None
    assert check.outcome == "hit"  # 100 -> 105 (+5%) bullish 命中
    # 基准行情为 mock，因此超额收益不能被虚构计算
    assert check.excess_return_pct is None
    assert check.benchmark_change_pct is None


def test_db_admin_backup_restore_cli_with_special_paths(tmp_path):
    """测试 db_admin.py 备份恢复命令行工具，支持含中文和空格的复杂路径，拒绝覆盖已有库。"""
    # 构造含空格与中文的源目录和备份目录
    work_dir = tmp_path / "测试 空间 2026"
    work_dir.mkdir(parents=True)
    source_db = work_dir / "源 业务 库.db"
    backup_dir = work_dir / "备份 仓库"

    # 初始化源数据库
    db = Database(source_db)
    apply_migrations(db)
    UserRepo(db).ensure("usr_chinese_path")

    # 1. 运行 db_admin.py backup
    cmd_backup = [
        PYTHON_EXE, "scripts/db_admin.py", "backup",
        "--source", str(source_db),
        "--directory", str(backup_dir),
        "--keep", "5"
    ]
    res_b = subprocess.run(cmd_backup, capture_output=True, text=True, encoding="utf-8", check=True)
    backup_file = Path(res_b.stdout.strip())
    assert backup_file.is_file()

    # 2. 运行 db_admin.py restore 到新路径（含中文与空格）
    restored_db = work_dir / "恢复 目标 库.db"
    cmd_restore = [
        PYTHON_EXE, "scripts/db_admin.py", "restore",
        "--source", str(backup_file),
        "--target", str(restored_db),
        "--quarantine-outbox"
    ]
    res_r = subprocess.run(cmd_restore, capture_output=True, text=True, encoding="utf-8", check=True)
    restore_info = json.loads(res_r.stdout)
    assert restore_info["status"] == "success"
    assert restored_db.is_file()

    # 3. 验证业务表哈希一致
    src_v = verify_database_integrity(source_db)
    dst_v = verify_database_integrity(restored_db)
    assert src_v["table_hashes"] == dst_v["table_hashes"]

    # 4. 再次 restore 到同一已存在目标，必须报错拒绝
    res_fail = subprocess.run(cmd_restore, capture_output=True, text=True, encoding="utf-8")
    assert res_fail.returncode != 0
    assert "Restore requires a new target path" in res_fail.stderr


def test_negative_drill_cases_fail_explicitly(tmp_path):
    """验证外键损坏、核心表丢失、文件损坏、不存在路径时恢复演练明确报错，杜绝伪造 PASS。"""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)

    # 1. 外键损坏库
    fk_db_path = tmp_path / "fk_corrupt.db"
    db_fk = Database(fk_db_path)
    apply_migrations(db_fk)
    db_fk.execute("PRAGMA foreign_keys=OFF")
    db_fk.execute("INSERT INTO research_question (question_id, user_id, title, hypothesis, created_at, updated_at) "
                  "VALUES ('rq_bad', 'usr_non_existent', 'title', 'hyp', '2026-01-01', '2026-01-01')")
    db_fk.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(ValueError, match="validation failed"):
        run_drill(fk_db_path, backup_dir, tmp_path / "target_fk.db")

    # 2. 缺失核心业务表库
    missing_table_db = tmp_path / "missing_core.db"
    conn = sqlite3.connect(str(missing_table_db))
    conn.execute("CREATE TABLE schema_version (version TEXT PRIMARY KEY);")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="validation failed"):
        run_drill(missing_table_db, backup_dir, tmp_path / "target_missing.db")

    # 3. 截断/损坏的备份文件
    corrupt_backup = backup_dir / "trace-corrupt.db"
    corrupt_backup.write_bytes(b"SQLite format 3\x00corrupted-partial-payload")
    with pytest.raises(ValueError, match="validation failed"):
        restore_backup(corrupt_backup, tmp_path / "target_corrupt.db")

    # 4. 源文件不存在
    with pytest.raises(FileNotFoundError):
        run_drill(tmp_path / "non_existent.db", backup_dir, tmp_path / "target_none.db")


def test_outbox_quarantine_on_restore(tmp_path):
    """测试恢复数据库后将 sending 状态安全隔离为 ambiguous，防止重复告警爆发。"""
    source_db_path = tmp_path / "source_outbox.db"
    db = Database(source_db_path)
    apply_migrations(db)

    UserRepo(db).ensure("usr_q")
    with db.transaction():
        db.execute(
            "INSERT INTO alert_outbox (outbox_id, user_id, channel_type, channel_target, event_id, event_version, alert_type, status, idempotency_key, content_text, created_at, updated_at) "
            "VALUES ('out_in_flight', 'usr_q', 'telegram', '123', 'evt_1', 1, 'urgent', 'sending', 'key1', 'Hello in flight', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        db.execute(
            "INSERT INTO alert_outbox (outbox_id, user_id, channel_type, channel_target, event_id, event_version, alert_type, status, idempotency_key, content_text, created_at, updated_at) "
            "VALUES ('out_sent_ok', 'usr_q', 'telegram', '123', 'evt_1', 1, 'urgent', 'sent', 'key2', 'Hello sent', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )

    backup_file = create_backup(source_db_path, tmp_path / "backups")
    restored_db_path = tmp_path / "restored_quarantine.db"

    # 执行带隔离的恢复
    restore_backup(backup_file, restored_db_path, quarantine_outbox=True)

    # 验证 restored 库中的 sending 记录转为 ambiguous
    db_restored = Database(restored_db_path)
    item_in_flight = db_restored.query_one("SELECT * FROM alert_outbox WHERE outbox_id='out_in_flight'")
    assert item_in_flight["status"] == "ambiguous"
    assert item_in_flight["last_error"] == "restored_from_backup_quarantine"
    assert item_in_flight["lease_until"] is None

    # 已发送记录不受影响
    item_sent = db_restored.query_one("SELECT * FROM alert_outbox WHERE outbox_id='out_sent_ok'")
    assert item_sent["status"] == "sent"


def test_restore_drill_cli_produces_full_report(tmp_path):
    """测试 restore_drill.py 脚本运行，输出报告包含 RTO/RPO 与全量哈希匹配。"""
    source_db_path = tmp_path / "drill_source.db"
    db = Database(source_db_path)
    apply_migrations(db)
    UserRepo(db).ensure("usr_drill")

    event_time = datetime.now(timezone.utc)
    EventRepo(db).insert(Event(
        event_id="evt_drill",
        title="Drill Event Title",
        version=1,
        event_time=event_time,
        first_seen_at=event_time
    ))

    backup_dir = tmp_path / "drill_backups"
    drill_target = tmp_path / "drill_target.db"
    report_json_path = tmp_path / "drill_report.json"

    cmd = [
        PYTHON_EXE, "scripts/restore_drill.py",
        "--source", str(source_db_path),
        "--backup-dir", str(backup_dir),
        "--target", str(drill_target),
        "--output-json", str(report_json_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
    assert report_json_path.is_file()

    report = json.loads(report_json_path.read_text(encoding="utf-8"))
    assert report["drill_status"] == "PASS"
    assert report["rto_seconds"] > 0
    assert report["rpo_basis"] == "snapshot_at_backup_creation"
    assert report["latest_event_time"] is not None
    assert report["drill_mode"] == "local_isolated_recovery_verification"
    assert not report["mismatches"]
    assert report["source_verification"]["table_hashes"] == report["target_verification"]["table_hashes"]
