"""Telegram Bot 命令层（python-telegram-bot v21+）。

支持命令（规划书 §18）：
    /watch <ticker>        加入 Watchlist（支持美股与 A股标准格式）
    /remove <ticker>       移出 Watchlist
    /watchlist             查看 Watchlist
    /alert <ticker|all> <阈值>   调整提醒阈值（默认 7）
    /mute <时长>           静音（如 60m / 2h）
    /unmute                取消静音
    /event <EVENT_ID>      查看事件详情
    /sources <EVENT_ID>    查看事件证据来源
    /digest                立即生成每日摘要
    /timezone <tz>         设置用户时区
    /ask <ticker> <问题>   基于本地证据的解释问答
    /help                  帮助

A股证券格式统一规范化，不允许用户输入格式造成多个重复 Security。
访问控制：配置了 TELEGRAM_ALLOWED_CHAT_IDS 时拒绝其他用户。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from trace.app import AppContext
from trace.common.modes import TraceMode
from trace.common.tickers import TickerParseError, normalize_ticker
from trace.db.health import SourceHealthRepo, derive_health_status
from trace.db.repositories import (
    AlertRuleRepo,
    DailyDigestRepo,
    EventRepo,
    EventSourceRepo,
    RawItemRepo,
    RunHistoryRepo,
    SecurityRepo,
    SourceRepo,
    UserRepo,
    WatchlistRepo,
)

logger = logging.getLogger(__name__)


def _allowed(update: Update, ctx: AppContext) -> bool:
    allowed = ctx.config.telegram.allowed_chat_ids
    if not allowed:
        return True
    return str(update.effective_chat.id) in allowed


def _ensure_user(ctx: AppContext, chat_id: int) -> None:
    UserRepo(ctx.db).ensure(str(chat_id), ctx.config.telegram.default_user_timezone)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    chat_id = update.effective_chat.id
    UserRepo(app.db).ensure(str(chat_id), app.config.telegram.default_user_timezone)
    await update.message.reply_text(
        "欢迎使用美股 + A股重大事件雷达。\n"
        "系统只基于官方来源（SEC / 公司 IR / 巨潮等）生成事件分析，"
        "不提供任何交易建议。\n\n"
        "快速开始：\n"
        "/watch SNDK 或 /watch 688981.SH 加入关注\n"
        "/alert all 7 设置提醒阈值\n"
        "/help 查看全部命令")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "命令列表：\n"
        "/watch SNDK | /watch 688981.SH — 加入 Watchlist\n"
        "/remove SNDK — 移出\n"
        "/watchlist — 查看\n"
        "/alert SNDK 8 | /alert all 7 — 设置阈值（默认 7）\n"
        "/mute 60m | /unmute — 静音控制\n"
        "/event <EVENT_ID> — 事件详情\n"
        "/sources <EVENT_ID> — 事件来源\n"
        "/digest — 每日摘要\n"
        "/accuracy — 预测回测命中率\n"
        "/status — 系统运行状态\n"
        "/timezone Asia/Tokyo — 设置时区\n"
        "/ask SNDK 今天为什么跌 — 事件解释"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app):
        return
    if not context.args:
        await update.message.reply_text("用法：/watch <ticker>")
        return
    try:
        norm = normalize_ticker(context.args[0])
    except TickerParseError as exc:
        await update.message.reply_text(f"无法识别的代码：{exc}")
        return
    _ensure_user(app, update.effective_chat.id)
    repo = SecurityRepo(app.db)
    sec = repo.get_by_ticker(norm.ticker)
    if sec is None:
        # 允许任意合法代码加入：动态注册（graph_node_ids 用 ticker 小写）
        from trace.domain.models import Security
        sec = Security(
            security_id=f"SEC-{norm.market}-{norm.ticker}",
            market=norm.market, exchange=norm.exchange, ticker=norm.ticker,
            graph_node_ids=[norm.ticker.lower()],
        )
        repo.upsert(sec)
        # IndustryGraph 的 node→证券映射只在启动时构建；不刷新的话
        # 长驻 bot 进程中新 /watch 的证券永远无法被图谱命中
        app.graph.reload()
    from trace.domain.models import WatchlistEntry
    WatchlistRepo(app.db).add(WatchlistEntry(
        user_id=str(update.effective_chat.id), security_id=sec.security_id))
    await update.message.reply_text(
        f"已加入 Watchlist：{sec.ticker}（{sec.company_name_zh or sec.company_name_en}）")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app) or not context.args:
        await update.message.reply_text("用法：/remove <ticker>")
        return
    try:
        norm = normalize_ticker(context.args[0])
    except TickerParseError as exc:
        await update.message.reply_text(f"无法识别的代码：{exc}")
        return
    sec = SecurityRepo(app.db).get_by_ticker(norm.ticker)
    if sec:
        WatchlistRepo(app.db).remove(str(update.effective_chat.id), sec.security_id)
    await update.message.reply_text(f"已移出：{norm.ticker}")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app):
        return
    ids = WatchlistRepo(app.db).list_by_user(str(update.effective_chat.id))
    repo = SecurityRepo(app.db)
    lines = [repo.get(sid).ticker for sid in ids if repo.get(sid)]
    await update.message.reply_text(
        "Watchlist：\n" + ("\n".join(f"- {t}" for t in lines) if lines else "（空）"))


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app) or len(context.args) < 2:
        await update.message.reply_text("用法：/alert <ticker|all> <阈值>")
        return
    target, threshold = context.args[0], context.args[1]
    try:
        value = float(threshold)
        if not 1 <= value <= 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("阈值必须是 1–10 的数字")
        return
    _ensure_user(app, update.effective_chat.id)
    rules = AlertRuleRepo(app.db)
    if target.lower() == "all":
        rules.set_threshold(str(update.effective_chat.id), None, value)
        await update.message.reply_text(f"全部证券提醒阈值设为 {value}")
        return
    try:
        norm = normalize_ticker(target)
    except TickerParseError as exc:
        await update.message.reply_text(f"无法识别的代码：{exc}")
        return
    sec = SecurityRepo(app.db).get_by_ticker(norm.ticker)
    if sec is None:
        await update.message.reply_text(f"{norm.ticker} 不在证券库中，请先 /watch")
        return
    rules.set_threshold(str(update.effective_chat.id), sec.security_id, value)
    await update.message.reply_text(f"{sec.ticker} 提醒阈值设为 {value}")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app):
        return
    _ensure_user(app, update.effective_chat.id)
    duration = context.args[0] if context.args else "60m"
    m = re.fullmatch(r"(\d+)(m|h|d)", duration)
    if not m:
        await update.message.reply_text("用法：/mute 60m （支持 m/h/d）")
        return
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    UserRepo(app.db).set_mute(str(update.effective_chat.id),
                              datetime.now(timezone.utc) + delta)
    await update.message.reply_text(f"已静音 {duration}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app):
        return
    _ensure_user(app, update.effective_chat.id)
    UserRepo(app.db).set_mute(str(update.effective_chat.id), None)
    await update.message.reply_text("已取消静音")


async def cmd_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app) or not context.args:
        await update.message.reply_text("用法：/event <EVENT_ID>")
        return
    ev = EventRepo(app.db).get(context.args[0])
    if ev is None:
        await update.message.reply_text("未找到该事件")
        return
    await update.message.reply_text(
        f"{ev.title}\n\n{ev.summary}\n\n"
        f"类型: {ev.event_type} | 状态: {ev.status} | 版本: v{ev.version}\n"
        f"首次发现: {ev.first_seen_at}\n最近更新: {ev.last_updated_at}")


async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app) or not context.args:
        await update.message.reply_text("用法：/sources <EVENT_ID>")
        return
    raw_repo = RawItemRepo(app.db)
    lines = []
    for es in EventSourceRepo(app.db).list_by_event(context.args[0]):
        item = raw_repo.get(es.raw_item_id)
        if item:
            lines.append(f"[{es.role}] {item.title}\n{item.url}")
    await update.message.reply_text(
        "\n\n".join(lines) if lines else "该事件暂无证据记录")


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app):
        return
    digest = app.digest_builder.build()
    await update.message.reply_text(digest.content_markdown)


async def cmd_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app) or not context.args:
        await update.message.reply_text("用法：/timezone Asia/Tokyo")
        return
    import pytz
    tz = context.args[0]
    if tz not in pytz.all_timezones_set:
        await update.message.reply_text(f"无效时区：{tz}")
        return
    _ensure_user(app, update.effective_chat.id)
    UserRepo(app.db).set_timezone(str(update.effective_chat.id), tz)
    await update.message.reply_text(f"时区已设为 {tz}")


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app) or len(context.args) < 2:
        await update.message.reply_text("用法：/ask SNDK 今天为什么跌")
        return
    try:
        norm = normalize_ticker(context.args[0])
    except TickerParseError as exc:
        await update.message.reply_text(f"无法识别的代码：{exc}")
        return
    question = " ".join(context.args[1:])
    answer = app.ask_engine.ask(norm.ticker, question)
    if answer is None:
        await update.message.reply_text(f"{norm.ticker} 不在证券库中")
        return
    await update.message.reply_text(answer.text)


async def cmd_accuracy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/accuracy：预测回测账本（方向预测 vs 事后真实行情）。"""
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app):
        return
    await update.message.reply_text(app.ledger.render_summary())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status：最近一轮运行 + 来源健康 + 回测/摘要/备份一览。"""
    app: AppContext = context.bot_data["app"]
    if not _allowed(update, app):
        return
    lines = ["🩺 系统状态", f"TRACE_MODE: {TraceMode.current()}"]

    runs = RunHistoryRepo(app.db).recent(1)
    if runs:
        r = runs[0]
        lines.append(f"最近一轮: {r['run_id']}（{(r['started_at'] or '')[:19]}Z）{r['status']}")
        lines.append(f"来源 {r['sources_succeeded']}/{r['sources_checked']} 成功 ｜ "
                     f"新事件 {r['events_created']} ｜ 投递 {r['alerts_sent']} ｜ "
                     f"初筛拦截 {r['keyword_filtered']} ｜ "
                     f"省下 Stage B {r['stage_b_skipped']}")
        if r["failed_sources"]:
            lines.append("失败来源: " + ", ".join(r["failed_sources"][:5]))
    else:
        lines.append("最近一轮: 尚无记录（等待长驻循环落库）")

    health_rows = {row["source_id"]: row for row in SourceHealthRepo(app.db).all()}
    counts: dict[str, int] = {}
    for source in SourceRepo(app.db).list_all():
        status = derive_health_status(health_rows.get(source.source_id) or {},
                                      enabled=source.enabled)
        counts[status] = counts.get(status, 0) + 1
    lines.append("来源健康: " + " ｜ ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    s = app.ledger.summary()
    if s["hits"] + s["misses"] > 0:
        rate = f"{s['hit_rate'] * 100:.0f}%" if s["hit_rate"] is not None else "-"
        lines.append(f"预测回测: 命中率 {rate}（{s['hits']}/{s['hits'] + s['misses']}）｜ "
                     f"待核对 {s['pending']}")
    else:
        lines.append(f"预测回测: 暂无样本 ｜ 待核对 {s['pending']}")

    import pytz
    tz = pytz.timezone(app.config.telegram.default_user_timezone)
    today = datetime.now(timezone.utc).astimezone(tz).date().isoformat()
    digest_sent = DailyDigestRepo(app.db).get(today) is not None
    lines.append(f"今日摘要: {'已推送' if digest_sent else '未推送'}")

    from trace.db.backup import latest_backup
    backup = latest_backup(Path(app.config.db_path).parent / "backups")
    lines.append(f"最近备份: {backup.name if backup else '尚无'}")

    await update.message.reply_text("\n".join(lines))


def build_application(app: AppContext) -> Application:
    application = Application.builder().token(
        app.config.telegram.bot_token).build()
    application.bot_data["app"] = app
    for name, handler in [
        ("start", cmd_start), ("help", cmd_help),
        ("watch", cmd_watch), ("remove", cmd_remove),
        ("watchlist", cmd_watchlist), ("alert", cmd_alert),
        ("mute", cmd_mute), ("unmute", cmd_unmute),
        ("event", cmd_event), ("sources", cmd_sources),
        ("digest", cmd_digest), ("timezone", cmd_timezone), ("ask", cmd_ask),
        ("accuracy", cmd_accuracy), ("status", cmd_status),
    ]:
        application.add_handler(CommandHandler(name, handler))
    return application


def run_bot(app: AppContext) -> None:
    application = build_application(app)
    logger.info("telegram bot starting (polling)")
    application.run_polling()
