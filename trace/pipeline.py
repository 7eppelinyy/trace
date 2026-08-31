"""主数据流水线编排。

run-once 流程（任务书 §11.2）：
    采集一次 → 持久化 RawItem → 去重 → 生成/更新 Event → AI 分析
    → 评分 → 匹配 Watchlist → Telegram 投递 → 保存投递结果 → 退出

生产门禁（任务书 §7）：
    - TRACE_MODE=production 且无 LLM Key：不生成伪分析，返回
      DEGRADED_NO_LLM / STOP_LLM_KEY_MISSING 状态
    - 无 Telegram Token：不得宣称投递成功（STOP_TELEGRAM_CREDENTIALS_MISSING）
    - Schema 校验失败：进入人工检查状态，不进入 Alert Engine

可观测性（任务书 §15）：
    每轮运行生成 run_id，输出运行摘要
    （sources_checked / sources_succeeded / sources_failed /
      raw_items_new / raw_items_duplicate / events_created /
      events_revised / events_analyzed / alerts_eligible /
      alerts_sent / alerts_suppressed / alerts_failed）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from trace.ai.schemas import LLMUnavailableError, SchemaValidationError
from trace.alerts.engine import AlertDecision, DeliveryReceipt
from trace.app import AppContext
from trace.common.ids import revision_id
from trace.common.modes import (
    STATUS_OK,
    STOP_LLM_KEY_MISSING,
    STOP_SOURCE_UNAVAILABLE,
    STOP_TELEGRAM_CREDENTIALS_MISSING,
    STOP_TELEGRAM_DELIVERY_FAILED,
    STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING,
    TraceMode,
)
from trace.common.observability import new_run_id
from trace.db.health import HumanReviewRepo
from trace.domain.models import Event
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.normalize import normalize_raw_item

logger = logging.getLogger(__name__)

AlertSender = Callable[[str, str, str | None], DeliveryReceipt]
"""(user_id, text, url) -> DeliveryReceipt。

投递方必须解析 Telegram 返回结果并构造回执；
失败时返回 status='failed' 的回执，不得抛异常。
"""


@dataclass
class RunSummary:
    """一轮流水线的结构化摘要（任务书 §15）。"""
    run_id: str = ""
    trace_mode: str = ""
    status: str = STATUS_OK
    sources_checked: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    sources_disabled: int = 0
    raw_items_new: int = 0
    raw_items_duplicate: int = 0
    events_created: int = 0
    events_revised: int = 0
    events_analyzed: int = 0
    alerts_eligible: int = 0
    alerts_sent: int = 0
    alerts_suppressed: int = 0
    alerts_failed: int = 0
    human_review: int = 0
    # DeepSeek 成本控制（任务书 §17）：新增来源不得导致 LLM 调用线性暴增
    keyword_filtered: int = 0            # Level 1 关键词初筛拦截数
    llm_stage_a_calls: int = 0
    llm_stage_b_calls: int = 0
    llm_verifier_calls: int = 0
    failed_sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "trace_mode": self.trace_mode,
            "status": self.status,
            "sources_checked": self.sources_checked,
            "sources_succeeded": self.sources_succeeded,
            "sources_failed": self.sources_failed,
            "sources_disabled": self.sources_disabled,
            "raw_items_new": self.raw_items_new,
            "raw_items_duplicate": self.raw_items_duplicate,
            "events_created": self.events_created,
            "events_revised": self.events_revised,
            "events_analyzed": self.events_analyzed,
            "alerts_eligible": self.alerts_eligible,
            "alerts_sent": self.alerts_sent,
            "alerts_suppressed": self.alerts_suppressed,
            "alerts_failed": self.alerts_failed,
            "human_review": self.human_review,
            "keyword_filtered": self.keyword_filtered,
            "llm_stage_a_calls": self.llm_stage_a_calls,
            "llm_stage_b_calls": self.llm_stage_b_calls,
            "llm_verifier_calls": self.llm_verifier_calls,
            "failed_sources": self.failed_sources,
            "notes": self.notes,
        }


class Pipeline:
    def __init__(self, app: AppContext):
        self.app = app
        self.alert_sender: AlertSender | None = None

    def _llm_stop_status(self) -> str:
        """缺 LLM Key 的停止码按当前 Provider 区分：
        默认 Gemini → STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING；
        显式切到 OpenAI → 保持 STOP_LLM_KEY_MISSING 兼容。"""
        if self.app.config.llm.provider == "gemini":
            return STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING
        return STOP_LLM_KEY_MISSING

    # ------------------------------------------------------------------
    def run_once(self) -> RunSummary:
        """执行一轮完整的采集→事件→分析→提醒流程，返回运行摘要。"""
        ctx = self.app
        summary = RunSummary(run_id=new_run_id(), trace_mode=TraceMode.current())

        # 默认接收人：TELEGRAM_DEFAULT_CHAT_ID（仅当该用户从未注册时初始化，
        # 之后用户的 /watch /alert /timezone 设置不会被覆盖）
        self._ensure_default_chat(ctx)

        # 1. 采集（失败分类写入 source_health，不吞异常）
        items, results = ctx.collectors.run_all()
        summary.sources_checked = len(results)
        for res in results:
            summary.keyword_filtered += res.keyword_filtered
            if res.status == "disabled":
                summary.sources_disabled += 1
            elif res.status == "failed":
                summary.sources_failed += 1
                summary.failed_sources.extend(res.source_ids)
            else:
                summary.sources_succeeded += 1
        logger.info("collected %d raw items (sources ok=%d failed=%d disabled=%d, "
                    "keyword_filtered=%d)",
                    len(items), summary.sources_succeeded,
                    summary.sources_failed, summary.sources_disabled,
                    summary.keyword_filtered)

        # 2. 逐条：Level 1 确定性去重（零成本，必须先于 DeepSeek）
        #    → Stage A 抽取 → 语义去重 → Event 创建/修订
        #    任务书 §17：重复条目不得消耗 LLM 调用
        dedup = ExactDedup(ctx.db)
        stage_a_before = ctx.pipeline.extractor.llm_calls
        verifier_before = getattr(ctx.event_engine.verifier, "llm_calls", 0)
        pending: dict[str, tuple[Event, bool]] = {}   # event_id -> (event, is_update)
        for item in items:
            normalize_raw_item(item)
            if dedup.check(item).is_duplicate:
                summary.raw_items_duplicate += 1
                continue
            try:
                extracted = ctx.pipeline.extractor.extract(item)
            except SchemaValidationError as exc:
                # 人工检查：不创建 Event，不进入 Alert 链路
                summary.human_review += 1
                HumanReviewRepo(ctx.db).add(
                    revision_id(), event_id=None, raw_item_id=item.raw_item_id,
                    reason=f"stage_a_schema_validation_failed: {exc}")
                logger.warning("stage A schema validation failed for %s → human review",
                               item.raw_item_id)
                continue
            except LLMUnavailableError as exc:
                # 生产模式无 LLM：整轮停止，不得生成伪分析
                summary.status = self._llm_stop_status()
                summary.notes.append(f"STOP: {exc}")
                logger.error("%s", exc)
                return summary

            decision = ctx.event_engine.ingest(item, extracted)
            if decision.action == "duplicate" or decision.event is None:
                summary.raw_items_duplicate += 1
                continue
            summary.raw_items_new += 1
            event, was_revised = pending.get(
                decision.event.event_id, (decision.event, False))
            if decision.action == "created":
                summary.events_created += 1
            elif decision.action == "revised":
                was_revised = True
                summary.events_revised += 1
            pending[event.event_id] = (decision.event, was_revised)

        # 3. 逐事件：Stage B 分析 → 评分 → Alert 评估 → 投递
        stage_b_before = ctx.pipeline.analyzer.llm_calls
        for event, is_update in pending.values():
            try:
                impacts = ctx.pipeline.analyze_event(
                    event, extra_entities=self._event_entity_names(event))
            except SchemaValidationError as exc:
                # Schema 校验失败：进入人工检查状态，不允许进入 Alert Engine
                event.needs_human_review = True
                ctx.event_engine.event_repo.update(event)
                summary.human_review += 1
                HumanReviewRepo(ctx.db).add(
                    revision_id(), event_id=event.event_id, raw_item_id=None,
                    reason=f"stage_b_schema_validation_failed: {exc}")
                logger.warning("stage B schema validation failed for %s → human review",
                               event.event_id)
                continue
            except LLMUnavailableError as exc:
                summary.status = self._llm_stop_status()
                summary.notes.append(f"STOP: {exc}")
                logger.error("%s", exc)
                return summary

            summary.events_analyzed += 1
            batch = ctx.alert_engine.evaluate(event, impacts, is_update=is_update)
            summary.alerts_eligible += len(batch.decisions)
            summary.alerts_suppressed += batch.suppressed

            # 首次同步保护：历史事件显式标记抑制（不集中推送，但可查询）
            for suppression in batch.bootstrap_suppressions:
                ctx.alert_engine.mark_bootstrap_suppressed(suppression)
                summary.alerts_suppressed += 1

            for dec in batch.decisions:
                self._deliver(dec, summary)

        # LLM 成本统计（任务书 §17）：本轮真实调用次数
        summary.llm_stage_a_calls = ctx.pipeline.extractor.llm_calls - stage_a_before
        summary.llm_stage_b_calls = ctx.pipeline.analyzer.llm_calls - stage_b_before
        summary.llm_verifier_calls = (
            getattr(ctx.event_engine.verifier, "llm_calls", 0) - verifier_before)

        # 全部来源失败且无任何数据 → 明确状态（不伪装成功）
        if summary.sources_failed and summary.sources_succeeded == 0 \
                and summary.raw_items_new == 0:
            summary.status = STOP_SOURCE_UNAVAILABLE
            summary.notes.append("all enabled sources failed")
        logger.info("run summary: %s", summary.as_dict())
        return summary

    # ------------------------------------------------------------------
    def _ensure_default_chat(self, ctx: AppContext) -> None:
        default_id = ctx.config.telegram.default_chat_id
        if not default_id:
            return
        if ctx.alert_engine.user_repo.get(default_id) is not None:
            return  # 用户已存在：不覆盖其设置
        ctx.alert_engine.user_repo.ensure(
            default_id, ctx.config.telegram.default_user_timezone)
        ctx.alert_engine.rule_repo.set_threshold(
            default_id, None, ctx.alert_engine.default_threshold)
        logger.info("default chat %s registered (all-threshold=%.1f)",
                    default_id, ctx.alert_engine.default_threshold)

    # ------------------------------------------------------------------
    def _deliver(self, decision: AlertDecision, summary: RunSummary) -> None:
        ctx = self.app
        user = ctx.alert_engine.user_repo.get(decision.user_id)
        tz = user.timezone if user else ctx.config.telegram.default_user_timezone
        text, url = ctx.alert_renderer.render(
            decision.event, decision.impact, tz,
            is_update=(decision.alert_type == "event_update"))

        if self.alert_sender is None:
            if TraceMode.is_production():
                # 生产模式：没有 Telegram Token 不得宣称投递成功
                summary.status = STOP_TELEGRAM_CREDENTIALS_MISSING
                summary.alerts_failed += 1
                logger.error("production mode without telegram channel: "
                             "refuse to claim delivery success (%s/%s)",
                             decision.event.event_id, decision.user_id)
                return
            # test / offline：只打印，明确标记 log_only
            logger.info("[ALERT -> %s]\n%s", decision.user_id, text)
            ctx.alert_engine.mark_sent(
                decision, DeliveryReceipt(status="sent", response="log_only"))
            summary.alerts_sent += 1
            return

        receipt = self.alert_sender(decision.user_id, text, url)
        if receipt.status == "sent":
            ctx.alert_engine.mark_sent(decision, receipt)
            summary.alerts_sent += 1
            logger.info("alert sent: user=%s event=%s message_id=%s",
                        decision.user_id, decision.event.event_id,
                        receipt.message_id)
        else:
            ctx.alert_engine.mark_failed(decision, receipt)
            summary.alerts_failed += 1
            if TraceMode.is_production():
                summary.status = STOP_TELEGRAM_DELIVERY_FAILED

    # ------------------------------------------------------------------
    def _event_entity_names(self, event: Event) -> list[str]:
        """从事件标题/摘要中用别名表粗提取实体名（供图谱映射）。

        同时覆盖：
            - 非上市实体/概念节点（entity_alias 表：Samsung、NAND、HBM…）
            - 上市证券名称（security 表：中芯国际、Micron…）
        A股公告标题通常包含公司名，只查 entity_alias 会导致 A股事件
        找不到任何候选证券。
        """
        import re

        from trace.db.repositories import EntityAliasRepo, SecurityRepo

        def in_text(name: str) -> bool:
            n = (name or "").strip().lower()
            if not n:
                return False
            if re.fullmatch(r"[a-z][a-z0-9.\-]{0,5}", n):
                # 短代码（ticker）：词边界匹配，避免 "MU" 误配英文单词
                return re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])",
                                 text) is not None
            return n in text

        names: list[str] = []
        text = f"{event.title} {event.summary}".lower()
        for entity in EntityAliasRepo(self.app.db).all_with_aliases():
            if in_text(entity.name) or any(in_text(a) for a in entity.aliases):
                names.append(entity.name)
        for sec in SecurityRepo(self.app.db).list_all():
            candidates = [sec.company_name_zh, sec.company_name_en,
                          sec.ticker, *sec.aliases]
            if any(in_text(c) for c in candidates):
                names.append(sec.company_name_zh or sec.company_name_en or sec.ticker)
        return names

    # ------------------------------------------------------------------
    def run_forever(self, poll_seconds: int = 60) -> None:
        logger.info("pipeline started (poll=%ss)", poll_seconds)
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("pipeline round failed")
            time.sleep(poll_seconds)
