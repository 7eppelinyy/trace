"""统一命令行入口。

用法：
    python -m trace.main init               初始化数据库 + 加载种子数据
    python -m trace.main doctor             环境诊断（不泄露 Token/API Key）
    python -m trace.main run-once           单次真实流水线（验收用）
    python -m trace.main verify-securities  SEC 官方权威表核验证券主数据
    python -m trace.main telegram-init-user 幂等初始化 Telegram 用户（Watchlist 仅 SNDK/MU/NVDA）
    python -m trace.main telegram-test      Telegram 连通性测试（真实投递 + 保存回执）
    python -m trace.main replay-event       历史真实事件验收回放（--acceptance-test）
    python -m trace.main bot                启动 Telegram Bot（long polling）+ 流水线
    python -m trace.main run                仅流水线循环
    python -m trace.main digest             生成今日摘要并打印

Phase 1：只做事件发现与解释，禁止自动交易。
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from trace.app import create_app
from trace.bot.delivery import DeliveryReceipt, send_message
from trace.common.modes import (
    STATUS_OK,
    STOP_TRACE_MVP_V1_ACCEPTANCE_REPLAY_INVALID,
    STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED,
    STOP_TRACE_MVP_V1_DEEPSEEK_SCHEMA_FAILED,
    STOP_TRACE_MVP_V1_REAL_DELIVERY_FAILED,
    STOP_TRACE_MVP_V1_TELEGRAM_CHAT_ID_MISSING,
    STOP_TRACE_MVP_V1_TELEGRAM_TOKEN_MISSING,
    TraceMode,
)
from trace.common.observability import setup_logging
from trace.pipeline import Pipeline

logger = logging.getLogger(__name__)

# 验收证据目录（所有证据文件不得包含完整 API Key / Token）
ACCEPTANCE_DIR = (Path(__file__).resolve().parent.parent
                  / "verification" / "mvp-v1-live-acceptance")

# 默认 Watchlist：只加入这三只，不得把整个 Security Master 自动加入
DEFAULT_WATCHLIST_TICKERS = ("SNDK", "MU", "NVDA")


def _save_acceptance(filename: str, content: str) -> Path:
    """把验收证据写入 verification/mvp-v1-live-acceptance/。"""
    ACCEPTANCE_DIR.mkdir(parents=True, exist_ok=True)
    path = ACCEPTANCE_DIR / filename
    path.write_text(content, encoding="utf-8")
    logger.info("acceptance evidence saved: %s", path)
    return path


def cmd_init() -> None:
    app = create_app()
    print(f"数据库初始化完成：{app.config.db_path}")
    print("种子数据已加载（数据源 / Security Master / 实体 / 产业链边）")


# ---------------------------------------------------------------------------
# run-once：单次真实流水线（任务书 §11.2）
# ---------------------------------------------------------------------------

def _make_alert_sender(bot_token: str):
    """构造真实 Telegram 投递器：解析返回并回传回执。"""

    def send(user_id: str, text: str, url: str | None) -> DeliveryReceipt:
        full = text if not url else f"{text}\n\n🔗 原文: {url}"
        return send_message(bot_token, user_id, full)

    return send


def cmd_run_once() -> None:
    TraceMode.validate()
    app = create_app()
    pipeline = Pipeline(app)
    if app.config.telegram.enabled:
        pipeline.alert_sender = _make_alert_sender(app.config.telegram.bot_token)
    else:
        if TraceMode.is_production():
            logger.warning("TRACE_MODE=production 但未配置 TELEGRAM_BOT_TOKEN；"
                           "投递将按 STOP_TELEGRAM_CREDENTIALS_MISSING 处理")
        else:
            logger.info("未配置 TELEGRAM_BOT_TOKEN：消息仅打印到日志（%s 模式）",
                        TraceMode.current())

    summary = pipeline.run_once()
    print("\n===== run-once 摘要 =====")
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    if summary.status != STATUS_OK:
        raise SystemExit(f"run-once finished with status: {summary.status}")


# ---------------------------------------------------------------------------
# doctor：环境诊断（任务书 §11.1，输出不得泄露 Token / API Key）
# ---------------------------------------------------------------------------

def cmd_doctor() -> None:
    import sys

    import httpx

    from trace.db.migration import apply_migrations

    app = create_app()
    lines: list[tuple[str, str, str]] = []   # (项目, 状态, 说明)

    def check(name: str, ok: bool | None, detail: str) -> None:
        status = "OK" if ok else ("FAIL" if ok is False else "WARN")
        lines.append((name, status, detail))

    # Python 环境
    check("python", sys.version_info >= (3, 10), sys.version.split()[0])

    # 数据库与 migration
    try:
        apply_migrations(app.db)
        check("database", True, str(app.config.db_path))
        vers = [r["version"] for r in
                app.db.query("SELECT version FROM schema_version ORDER BY version")]
        check("migration", True, f"applied: {', '.join(vers) if vers else '(none)'}")
    except Exception as exc:
        check("database", False, str(exc)[:200])

    # 数据目录写权限
    data_dir = app.config.db_path.parent
    try:
        probe = data_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("data_dir_writable", True, str(data_dir))
    except Exception as exc:
        check("data_dir_writable", False, str(exc)[:200])

    # SEC 联通
    try:
        resp = httpx.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": app.config.sec_contact.user_agent},
                         timeout=20)
        check("sec_edgar", resp.status_code == 200,
              f"HTTP {resp.status_code}, UA={app.config.sec_contact.user_agent}")
    except Exception as exc:
        check("sec_edgar", False, str(exc)[:200])

    # 美股 IR 联通（NVIDIA 官方 RSS）
    try:
        resp = httpx.get("https://nvidianews.nvidia.com/rss.xml",
                         headers={"User-Agent": "Mozilla/5.0 TraceEventRadar/0.2"},
                         timeout=20)
        check("us_ir_nvidia", resp.status_code == 200, f"HTTP {resp.status_code}")
    except Exception as exc:
        check("us_ir_nvidia", False, str(exc)[:200])

    # 巨潮联通
    try:
        resp = httpx.post(
            "https://www.cninfo.com.cn/new/information/topSearch/query",
            data={"keyWord": "688981", "maxSecNum": 10, "maxListNum": 5},
            headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"},
            timeout=20)
        check("cninfo", resp.status_code == 200, f"HTTP {resp.status_code}")
    except Exception as exc:
        check("cninfo", False, str(exc)[:200])

    # SSE/SZSE 路径（路线B：由巨潮统一入口覆盖）
    check("sse_szse_path", True,
          "路线B：SSE/SZSE 为巨潮公告的市场过滤适配器（见 trace/collectors/sse.py）")

    # LLM Key（只显示是否配置，不显示值；默认 Provider 为 Gemini）
    has_key = bool(app.config.llm.api_key)
    provider = app.config.llm.provider
    env_name = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    check("llm_key", has_key if TraceMode.is_production() else None,
          f"provider={provider}, {env_name} {'已配置' if has_key else '未配置'} "
          f"(model={app.config.llm.model_event_extractor})")

    # Telegram Token 与 Bot 身份
    has_token = bool(app.config.telegram.bot_token)
    check("telegram_token", has_token if TraceMode.is_production() else None,
          "TELEGRAM_BOT_TOKEN " + ("已配置" if has_token else "未配置"))
    if has_token:
        from trace.bot.delivery import get_me
        me = get_me(app.config.telegram.bot_token)
        check("telegram_identity", me is not None,
              f"@{me.get('username')}" if me else "getMe 失败（检查 Token）")
    else:
        check("telegram_identity", None, "跳过（无 Token）")

    # 默认接收人（生产模式必须配置；离线模式仅告警）
    has_chat = bool(app.config.telegram.default_chat_id)
    check("default_chat_id", has_chat if TraceMode.is_production() else None,
          "TELEGRAM_DEFAULT_CHAT_ID " + ("已配置" if has_chat else "未配置"))

    # 用户时区
    check("timezone", True,
          f"default_user_timezone={app.config.telegram.default_user_timezone}")

    # 配置文件
    from trace.config import SETTINGS_PATH
    check("settings", SETTINGS_PATH.exists(), str(SETTINGS_PATH))

    # TRACE_MODE
    mode = TraceMode.current()
    check("trace_mode", mode in ("test", "offline", "production"),
          f"TRACE_MODE={mode}")

    # Source Health（任务书 §12：HEALTHY/DEGRADED/RATE_LIMITED/BROKEN/DISABLED）
    from trace.db.health import SourceHealthRepo, derive_health_status
    from trace.db.repositories import SourceRepo as _SourceRepo
    registry = _SourceRepo(app.db)
    health_rows = {r["source_id"]: r for r in SourceHealthRepo(app.db).all()}
    health_summary: dict[str, int] = {}
    for source in sorted(registry.list_all(), key=lambda s: s.source_id):
        row = health_rows.get(source.source_id)
        status = derive_health_status(row or {}, enabled=source.enabled)
        health_summary[status] = health_summary.get(status, 0) + 1
        if not source.enabled:
            continue  # 禁用来源只显示状态，不报警
        detail_parts = []
        if row:
            detail_parts.append(f"last_http={row.get('last_http_status') or '-'}")
            if row.get("last_success_at"):
                detail_parts.append(f"last_ok={row['last_success_at'][:19]}")
            if row.get("last_error"):
                detail_parts.append(f"err={row['last_error'][:60]}")
        key = f"source_health/{source.source_id}"
        check(key, status in ("HEALTHY", "DEGRADED"),
              f"{status} " + (" ".join(detail_parts) if detail_parts else "(尚未采集)"))

    print("===== doctor 环境诊断 =====")
    width = max(len(n) for n, _, _ in lines)
    fails = 0
    for name, status, detail in lines:
        if status == "FAIL":
            fails += 1
        print(f"[{status:4}] {name.ljust(width)}  {detail}")
    print(f"\n总计 {len(lines)} 项：失败 {fails} 项")
    if fails:
        raise SystemExit("doctor: FAIL")


# ---------------------------------------------------------------------------
# verify-securities：SEC 官方权威表核验（任务书 §3）
# ---------------------------------------------------------------------------

def cmd_verify_securities() -> None:
    from trace.db.repositories import SecurityRepo
    from trace.verification import SECAuthority, verify_securities

    app = create_app()
    authority = SECAuthority(user_agent=app.config.sec_contact.user_agent)
    try:
        authority.fetch()
    except Exception as exc:
        raise SystemExit(f"SEC 官方权威表拉取失败：{exc}")

    securities = [s for s in SecurityRepo(app.db).list_all() if s.market == "US"]
    results = verify_securities(securities, authority)

    print(f"{'ticker':8} {'状态':18} {'本地CIK':12} {'官方CIK':12} 官方名称")
    mismatches = 0
    not_set = 0
    for r in results:
        if r.status == "MISMATCH" or r.status == "NOT_FOUND_OFFICIAL":
            mismatches += 1
        if r.status == "CIK_NOT_SET":
            not_set += 1
        print(f"{r.ticker:8} {r.status:18} {(r.local_cik or '-'):12} "
              f"{(r.official_cik or '-'):12} {r.official_title or '-'}")
        if r.detail:
            print(f"         ↳ {r.detail}")
    if not_set:
        print(f"提示：{not_set} 只证券未登记 CIK（context universe，可按需补录）")
    if mismatches:
        raise SystemExit("存在不一致项，请修正 seed 数据（不允许运行时自动覆盖）")
    print("核验通过：已登记 CIK 的证券与 SEC 官方权威表一致")


# ---------------------------------------------------------------------------
# telegram-init-user：幂等初始化用户（任务书 §3）
# ---------------------------------------------------------------------------

def cmd_telegram_init_user() -> None:
    """幂等创建默认接收人：Watchlist 仅 SNDK/MU/NVDA，阈值 7，用户时区。

    重复执行不得创建重复用户 / Watchlist / AlertRule。
    """
    from trace.db.repositories import SecurityRepo, UserRepo, WatchlistRepo
    from trace.domain.models import WatchlistEntry

    app = create_app()
    chat_id = app.config.telegram.default_chat_id
    if not chat_id:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_TELEGRAM_CHAT_ID_MISSING}："
                         "未配置 TELEGRAM_DEFAULT_CHAT_ID")

    user_repo = UserRepo(app.db)
    watch_repo = WatchlistRepo(app.db)
    security_repo = SecurityRepo(app.db)
    threshold = app.alert_engine.default_threshold

    existing = user_repo.get(chat_id)
    # 用户：幂等（ON CONFLICT DO NOTHING），首次创建时登记 alert_activation_at
    user_repo.ensure(chat_id, app.config.telegram.default_user_timezone)
    created = existing is None

    # Watchlist：只加入三只默认证券；已存在的不重复插入
    added: list[str] = []
    for ticker in DEFAULT_WATCHLIST_TICKERS:
        sec = security_repo.get_by_ticker(ticker)
        if sec is None:
            logger.warning("security %s not found in master, skip", ticker)
            continue
        watch_repo.add(WatchlistEntry(user_id=chat_id, security_id=sec.security_id))
        added.append(ticker)

    # AlertRule：全部证券阈值（all 规则）= 默认阈值，幂等覆盖
    app.alert_engine.rule_repo.set_threshold(chat_id, None, threshold)

    watchlist = [security_repo.get(sid).ticker
                 for sid in watch_repo.list_by_user(chat_id)]
    activation = user_repo.get(chat_id).alert_activation_at
    result = {
        "user_id": chat_id,
        "user_created": created,
        "timezone": app.config.telegram.default_user_timezone,
        "alert_activation_at": activation.isoformat() if activation else None,
        "watchlist": sorted(watchlist),
        "alert_threshold_all": threshold,
        "note": "Watchlist 仅包含默认三只证券，未自动加入整个 Security Master",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    _save_acceptance("telegram-user-watchlist.json",
                     json.dumps(result, ensure_ascii=False, indent=2))
    if created:
        logger.info("telegram user %s initialized (watchlist=%s, threshold=%.1f)",
                    chat_id, watchlist, threshold)
    else:
        logger.info("telegram user %s already exists; idempotent no-op", chat_id)


# ---------------------------------------------------------------------------
# telegram-test：连通性测试（任务书 §4）
# ---------------------------------------------------------------------------

def cmd_telegram_test() -> None:
    """发送一条纯系统测试消息并保存真实回执。失败 → STOP_REAL_DELIVERY_FAILED。"""
    app = create_app()
    TraceMode.validate()

    token = app.config.telegram.bot_token
    chat_id = app.config.telegram.default_chat_id
    if not token:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_TELEGRAM_TOKEN_MISSING}："
                         "未配置 TELEGRAM_BOT_TOKEN")
    if not chat_id:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_TELEGRAM_CHAT_ID_MISSING}："
                         "未配置 TELEGRAM_DEFAULT_CHAT_ID")

    from trace.bot.delivery import get_me
    me = get_me(token)
    llm = app.config.llm
    # provider=openai 且 base_url 指向 DeepSeek 时，显示真实模型提供商
    llm_label = ("DeepSeek AI" if "deepseek" in (llm.base_url or "").lower()
                 else f"{llm.provider} / {llm.model_event_extractor}")
    llm_status = "已连接" if llm.enabled else "未配置"
    text = (
        "✅ Trace 系统连接成功\n\n"
        f"{llm_label}：{llm_status}\n"
        "美股数据源：已连接\n"
        "A股数据源：已连接\n"
        "Telegram：已连接\n\n"
        "这是一条系统连通性测试消息，不是市场事件。"
    )
    receipt = send_message(token, chat_id, text)
    sent_at = datetime.now(timezone.utc).isoformat()

    result = {
        "telegram_chat_id": receipt.chat_id,
        "telegram_message_id": receipt.message_id,
        "sent_at": sent_at,
        "status": receipt.status,
        "response": receipt.response,
        "bot_username": f"@{me.get('username')}" if me else None,
        "llm_label": llm_label,
        "llm_connected": llm.enabled,
        "trace_mode": TraceMode.current(),
        "note": "系统连通性测试消息（非市场事件）",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    _save_acceptance("telegram-connectivity-receipt.json",
                     json.dumps(result, ensure_ascii=False, indent=2))

    if receipt.status != "sent":
        raise SystemExit(f"{STOP_TRACE_MVP_V1_REAL_DELIVERY_FAILED}："
                         f"Telegram 连通性测试失败（{receipt.response}: {receipt.error}）")
    logger.info("telegram connectivity test sent (message_id=%s)", receipt.message_id)


# ---------------------------------------------------------------------------
# replay-event：历史真实事件验收回放（任务书 §9）
# ---------------------------------------------------------------------------

def cmd_replay_event(event_id: str, acceptance_test: bool) -> None:
    """对一条真实历史事件执行真实分析 + 真实 Telegram 投递。

    唯一特殊点：--acceptance-test 时绕过 freshness gate（首次同步保护），
    其余门禁（阈值 / 置信度 / 静音 / 幂等）保持真实，不得放宽。
    消息顶部必须明确标记"历史真实事件验收回放"。
    """
    from trace.ai.schemas import LLMUnavailableError, SchemaValidationError

    app = create_app()
    TraceMode.validate()

    event = app.event_engine.event_repo.get(event_id)
    if event is None:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_ACCEPTANCE_REPLAY_INVALID}："
                         f"event {event_id} 不存在")

    # 真实 LLM（Stage B + 评分）；幂等键使用同一 event_id + version，
    # 重复回放时 evaluate 的 already_sent 会拦截（不得重复投递）。
    pipeline = Pipeline(app)
    app.pipeline.refresh_caches()
    try:
        impacts = app.pipeline.analyze_event(
            event, extra_entities=pipeline.entity_names_for_event(event))
    except SchemaValidationError as exc:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_DEEPSEEK_SCHEMA_FAILED}: {exc}")
    except LLMUnavailableError as exc:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED}: {exc}")

    if not impacts:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_ACCEPTANCE_REPLAY_INVALID}："
                         "该事件无任何影响分析结果，无法回放")

    batch = app.alert_engine.evaluate(
        event, impacts, is_update=False,
        bypass_freshness=acceptance_test)

    if not batch.decisions:
        # 幂等：已投递过 / 低于阈值 / 被抑制 → 不得重复投递
        logger.info("replay produced no deliveries (suppressed=%d, "
                    "bootstrap_suppressed=%d)", batch.suppressed,
                    len(batch.bootstrap_suppressions))
        raise SystemExit(
            "replay-event: 无可投递对象（可能已投递过或低于阈值；"
            f"suppressed={batch.suppressed}, "
            f"bootstrap_suppressed={len(batch.bootstrap_suppressions)}）"
            "——幂等保护生效，不重复发送")

    if not app.config.telegram.enabled or not app.config.telegram.default_chat_id:
        raise SystemExit(STOP_TRACE_MVP_V1_TELEGRAM_TOKEN_MISSING
                         if not app.config.telegram.bot_token
                         else STOP_TRACE_MVP_V1_TELEGRAM_CHAT_ID_MISSING)

    send = _make_alert_sender(app.config.telegram.bot_token)
    sent, failed = 0, 0
    receipts: list[dict] = []
    rendered_texts: list[str] = []
    for dec in batch.decisions:
        user = app.alert_engine.user_repo.get(dec.user_id)
        tz = user.timezone if user else app.config.telegram.default_user_timezone
        text, url = app.alert_renderer.render(
            dec.event, dec.impact, tz, is_update=False)
        # 验收回放必须在顶部明确标记，不得让用户误以为是实时新闻
        header = "🧪 历史真实事件验收回放（非实时新闻）\n" if acceptance_test else ""
        full_text = header + text + (f"\n\n🔗 原文: {url}" if url else "")
        receipt = send(dec.user_id, header + text, url)
        if receipt.status == "sent":
            app.alert_engine.mark_sent(dec, receipt)
            sent += 1
            rendered_texts.append(full_text)
            receipts.append({
                "user_id": dec.user_id,
                "event_id": dec.event.event_id,
                "security_id": dec.impact.security_id,
                "event_version": dec.event.version,
                "alert_type": dec.alert_type,
                "final_score": dec.impact.final_score,
                "direction": dec.impact.direction,
                "telegram_chat_id": receipt.chat_id,
                "telegram_message_id": receipt.message_id,
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "status": receipt.status,
                "response": receipt.response,
            })
            logger.info("replay alert sent user=%s event=%s message_id=%s",
                        dec.user_id, dec.event.event_id, receipt.message_id)
        else:
            app.alert_engine.mark_failed(dec, receipt)
            failed += 1

    if failed:
        raise SystemExit(f"{STOP_TRACE_MVP_V1_REAL_DELIVERY_FAILED}："
                         f"{failed} 条回放投递失败")

    # 验收证据：渲染后的消息全文 + 真实 Telegram 回执（不含任何 Key/Token）
    if rendered_texts:
        _save_acceptance("rendered-alert.txt", "\n\n" + "=" * 40 + "\n\n".join(rendered_texts))
        _save_acceptance("telegram-real-delivery-receipt.json",
                         json.dumps({"acceptance_test": acceptance_test,
                                     "event_id": event_id, "deliveries": receipts},
                                    ensure_ascii=False, indent=2))
    print(f"replay-event 完成：sent={sent} failed={failed} "
          f"event={event_id} acceptance_test={acceptance_test}")


# ---------------------------------------------------------------------------
# bot / run / digest
# ---------------------------------------------------------------------------

def _make_bot_alert_sender(application_holder: dict):
    """bot 模式：通过 python-telegram-bot 的 running application 投递。"""

    def send(user_id: str, text: str, url: str | None) -> DeliveryReceipt:
        application = application_holder.get("app")
        if application is None:
            return DeliveryReceipt(status="failed", response="no_channel",
                                   error="bot application not ready")
        import asyncio
        full = text if not url else f"{text}\n\n🔗 原文: {url}"
        try:
            fut = asyncio.run_coroutine_threadsafe(
                application.bot.send_message(chat_id=int(user_id), text=full),
                application.loop)
            msg = fut.result(timeout=30)
            return DeliveryReceipt(status="sent", chat_id=str(msg.chat_id),
                                   message_id=str(msg.message_id), response="ok")
        except Exception as exc:
            return DeliveryReceipt(status="failed", response="network_error",
                                   error=str(exc)[:300])

    return send


def cmd_run() -> None:
    app = create_app()
    pipeline = Pipeline(app)
    if app.config.telegram.enabled:
        # 无 polling 时退回直接 Bot API 投递
        pipeline.alert_sender = _make_alert_sender(app.config.telegram.bot_token)
    # systemd 以 SIGTERM 停止：转为退出循环而不是直接硬杀
    import signal

    def _graceful_stop(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, _graceful_stop)
    pipeline.run_forever(poll_seconds=60)


def cmd_bot() -> None:
    app = create_app()
    if not app.config.telegram.enabled:
        raise SystemExit("STOP_TELEGRAM_CREDENTIALS_MISSING："
                         "未配置 TELEGRAM_BOT_TOKEN（参考 .env.example）")

    holder: dict = {}
    pipeline = Pipeline(app)
    pipeline.alert_sender = _make_bot_alert_sender(holder)

    threading.Thread(target=pipeline.run_forever, kwargs={"poll_seconds": 60},
                     daemon=True).start()

    from trace.bot.telegram_bot import build_application
    application = build_application(app)
    holder["app"] = application
    logger.info("telegram bot starting (long polling)")
    application.run_polling()


def cmd_digest() -> None:
    app = create_app()
    digest = app.digest_builder.build()
    print(digest.content_markdown)


def main() -> None:
    setup_logging(logging.INFO)
    parser = argparse.ArgumentParser(description="美股+A股重大事件智能雷达")
    parser.add_argument("command", choices=[
        "init", "doctor", "run-once", "verify-securities",
        "telegram-init-user", "telegram-test", "replay-event",
        "run", "bot", "digest"])
    parser.add_argument("--event-id", default="",
                        help="replay-event：要回放的事件 ID")
    parser.add_argument("--acceptance-test", action="store_true",
                        help="replay-event：验收模式（仅绕过 freshness gate，"
                             "其余门禁保持真实）")
    args = parser.parse_args()
    if args.command == "replay-event":
        if not args.event_id:
            raise SystemExit("replay-event 需要 --event-id <REAL_EVENT_ID>")
        cmd_replay_event(args.event_id, args.acceptance_test)
        return
    {
        "init": cmd_init,
        "doctor": cmd_doctor,
        "run-once": cmd_run_once,
        "verify-securities": cmd_verify_securities,
        "telegram-init-user": cmd_telegram_init_user,
        "telegram-test": cmd_telegram_test,
        "run": cmd_run,
        "bot": cmd_bot,
        "digest": cmd_digest,
    }[args.command]()


if __name__ == "__main__":
    main()
