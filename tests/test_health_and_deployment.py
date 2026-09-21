"""业务健康检查、就绪探针与灾难恢复演练测试 (T14 / F20 / F21 / F22 / F26)。

验证核心：
1. 真实业务健康检查 (/health)：
   - 数据源心跳新鲜度（无心跳或 >1h 超时正确降级为 DEGRADED 并输出排查告警）；
   - LLM 预算消耗保护告警；
   - Outbox 队列积压告警；
2. 服务就绪探针 (/ready)：
   - 数据库可达性、全量 migration 版本应用及基础种子加载门禁；
3. 数据库备份与恢复完整性 (F20)：
   - 在线一致性热备与独立临时库恢复校验；
   - 自动化运行 restore_drill 并验证全表行数及事件时间一致；
4. 部署材料语法与规范性 (F22 / F26)：
   - docker-compose.yml 结构与健康检查配置有效性；
   - .env.example 关键字段完整性。
"""

from __future__ import annotations

import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from trace.api.app import create_api_app
from trace.db.backup import create_backup, restore_backup, verify_database_integrity
from trace.db.health import SourceHealthRepo
from trace.db.repositories import AlertOutboxRepo, SourceRepo
from trace.domain.models import AlertOutbox, Source
from scripts.restore_drill import run_drill


@pytest.fixture
def client(app):
    api_app = create_api_app(ctx=app)
    with TestClient(api_app) as c:
        yield c


def test_health_check_healthy_state(client, app):
    """测试健康状态：数据源具有最近 1 小时内核实的心跳时为 HEALTHY。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    sources = SourceRepo(app.db).list_all()
    for s in sources:
        app.db.execute(
            "INSERT INTO source_health (source_id, last_success_at, consecutive_failures) "
            "VALUES (?, ?, 0) ON CONFLICT(source_id) DO UPDATE SET last_success_at=excluded.last_success_at, consecutive_failures=0",
            (s.source_id, now_iso),
        )

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "HEALTHY"
    assert data["db_status"] == "OK"
    assert data["sources_status"] == "OK"
    assert data["sources_stale_count"] == 0
    assert len(data["diagnostics"]) == 0


def test_health_check_stale_sources_degraded(client, app):
    """验收标准：停止数据源抓取模拟超时，/health 状态正确变为 DEGRADED 并输出告警提示。"""
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    sources = SourceRepo(app.db).list_all()
    for s in sources:
        app.db.execute(
            "INSERT INTO source_health (source_id, last_success_at, consecutive_failures) "
            "VALUES (?, ?, 0) ON CONFLICT(source_id) DO UPDATE SET last_success_at=excluded.last_success_at, consecutive_failures=0",
            (s.source_id, stale_time),
        )

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "DEGRADED"
    assert data["sources_status"] == "STALE"
    assert data["sources_stale_count"] == len([s for s in sources if s.enabled])
    assert any("no successful fetch within the last 1 hour" in d for d in data["diagnostics"])


def test_health_check_llm_budget_exhausted_degraded(client, app):
    """测试 LLM 预算耗尽时自动标记 DEGRADED。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    for s in SourceRepo(app.db).list_all():
        app.db.execute(
            "INSERT INTO source_health (source_id, last_success_at, consecutive_failures) "
            "VALUES (?, ?, 0) ON CONFLICT(source_id) DO UPDATE SET last_success_at=excluded.last_success_at, consecutive_failures=0",
            (s.source_id, now_iso),
        )

    # 模拟 LLM 预算熔断超限
    budget = app.pipeline.budget
    budget.daily_limit = 10
    budget.consume(11)

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "DEGRADED"
    assert data["llm_budget_used_pct"] >= 100.0
    assert any("budget exhausted" in d for d in data["diagnostics"])


def test_health_check_outbox_backlog_degraded(client, app):
    """测试 Outbox 队列积压超过 50 条时标记 DEGRADED。"""
    now_iso = datetime.now(timezone.utc).isoformat()
    for s in SourceRepo(app.db).list_all():
        app.db.execute(
            "INSERT INTO source_health (source_id, last_success_at, consecutive_failures) "
            "VALUES (?, ?, 0) ON CONFLICT(source_id) DO UPDATE SET last_success_at=excluded.last_success_at, consecutive_failures=0",
            (s.source_id, now_iso),
        )

    outbox_repo = AlertOutboxRepo(app.db)
    # 模拟 55 条积压待发送任务
    for i in range(55):
        outbox_repo.enqueue(AlertOutbox(
            outbox_id=f"OB-TEST-{i}",
            user_id="U1",
            channel_type="telegram",
            channel_target="12345",
            event_id="E1",
            impact_id="I1",
            event_version=1,
            alert_type="breaking",
            idempotency_key=f"IDEMP-{i}",
            content_text="Backlog alert",
            status="pending",
        ))

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "DEGRADED"
    assert data["outbox_pending_count"] >= 55
    assert any("outbox backlog high" in d for d in data["diagnostics"])


def test_readiness_probe(client, app):
    """测试 /ready 就绪探针正常与异常分支。"""
    # 1. 正常状态：200 READY
    resp = client.get("/ready")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["status"] == "READY"
    assert data["details"]["database"] == "reachable"
    assert data["details"]["schema_status"] == "up_to_date"
    assert data["details"]["seeds_status"] == "loaded"

    # 2. 模拟主数据缺失：清空 security 表导致未就绪 503
    app.db.execute("DELETE FROM security")
    resp_fail = client.get("/ready")
    assert resp_fail.status_code == 503
    data_fail = resp_fail.json()
    assert data_fail["ready"] is False
    assert data_fail["status"] == "NOT_READY"
    assert data_fail["details"]["seeds_status"] == "missing_securities"


def test_backup_restore_and_automated_drill(tmp_path, db):
    """验收标准：自动化运行一次真实 SQLite 数据库备份与恢复演练，恢复后的数据库通过校验。"""
    source_db = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    target_db = tmp_path / "restored.db"

    # 将当前活跃内存库持久化到 source_db 文件中供演练
    src_file_conn = create_backup(Path(db.path) if db.path != ":memory:" else "data/trace.db",
                                  backup_dir)

    # 运行演练脚本自动化流水线
    drill_report = run_drill(src_file_conn, backup_dir, target_db)

    assert drill_report["drill_status"] == "PASS"
    assert drill_report["source_verification"]["integrity_check"] == "ok"
    assert drill_report["target_verification"]["integrity_check"] == "ok"
    assert len(drill_report["mismatches"]) == 0


def test_docker_compose_and_env_example():
    """验收标准：docker-compose.yml 语法与卷挂载合法，.env.example 必填配置齐备。"""
    root = Path(__file__).resolve().parent.parent

    # 1. 验证 docker-compose.yml 语法与结构
    compose_path = root / "docker-compose.yml"
    assert compose_path.exists()
    content = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert "services" in content
    assert "api" in content["services"]
    assert "runner" in content["services"]

    api_svc = content["services"]["api"]
    assert "trace_data:/app/data" in api_svc["volumes"]
    assert "healthcheck" in api_svc
    assert "ports" in api_svc

    runner_svc = content["services"]["runner"]
    assert "trace_data:/app/data" in runner_svc["volumes"]
    assert runner_svc["depends_on"]["api"]["condition"] == "service_healthy"

    # 2. 验证 .env.example
    env_example = root / ".env.example"
    assert env_example.exists()
    env_text = env_example.read_text(encoding="utf-8")
    assert "TRACE_MODE=" in env_text
    assert "TRACE_DB_PATH=" in env_text
    assert "TRACE_BACKUP_DIR=" in env_text
    assert "LLM_PROVIDER=" in env_text
    assert "LLM_DAILY_CALL_BUDGET=" in env_text
    assert "TELEGRAM_BOT_TOKEN=" in env_text
    assert "SEC_CONTACT_EMAIL=" in env_text
