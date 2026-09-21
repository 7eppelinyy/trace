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
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from trace.ai.budget import LLMBudgetExceededError
from trace.ai.schemas import LLMUnavailableError, SchemaValidationError
from trace.alerts.engine import AlertDecision, DeliveryReceipt
from trace.app import AppContext
from trace.collectors.market_data.confirmation import (
    MODE_MARKET_NOT_OPENED,
    MODE_NO_QUOTE,
)
from trace.common.ids import revision_id
from trace.common.modes import (
    STATUS_OK,
    STOP_LLM_BUDGET_EXCEEDED,
    STOP_LLM_KEY_MISSING,
    STOP_SOURCE_UNAVAILABLE,
    STOP_TELEGRAM_CREDENTIALS_MISSING,
    STOP_TELEGRAM_DELIVERY_FAILED,
    STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING,
    TraceMode,
)
from trace.common.observability import new_run_id
from trace.db.health import HumanReviewRepo
from trace.db.repositories import ProcessingJobRepo, RawItemRepo
from trace.domain.models import Event
from trace.event_engine.exact_dedup import ExactDedup
from trace.event_engine.normalize import normalize_raw_item

logger = logging.getLogger(__name__)

# 这些 market_data_mode 表示"当时拿不到可用的市场确认"，开盘后值得重算
_PENDING_CONFIRMATION_MODES = [
    MODE_MARKET_NOT_OPENED,
    MODE_NO_QUOTE,
]

AlertSender = Callable[[str, str, str | None], DeliveryReceipt]
"""(user_id, text, url) -> DeliveryReceipt。

投递方必须解析 Telegram 返回结果并构造回执；
失败时返回 status='failed' 的回执，不得抛异常。
"""


@dataclass
class RunSummary:
    """一轮流水线的结构化摘要（任务书 §15）。"""
    run_id: str = ""
    started_at: str = ""                 # UTC ISO（运行历史落库用）
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
    stage_b_skipped: int = 0             # 仅补充佐证的合并，跳过的 Stage B 次数
    rescored_events: int = 0             # 开盘后补算市场确认导致分数实质变化的事件数
    cursors_committed: int = 0           # 本轮提交的采集游标数（0 = 中途退出未提交）
    llm_stage_a_calls: int = 0
    llm_stage_b_calls: int = 0
    llm_verifier_calls: int = 0
    failed_sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
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
            "stage_b_skipped": self.stage_b_skipped,
            "rescored_events": self.rescored_events,
            "cursors_committed": self.cursors_committed,
            "llm_stage_a_calls": self.llm_stage_a_calls,
            "llm_stage_b_calls": self.llm_stage_b_calls,
            "llm_verifier_calls": self.llm_verifier_calls,
            "failed_sources": self.failed_sources,
            "notes": self.notes,
        }


@dataclass
class _PendingEvent:
    """本轮待处理事件的聚合状态（同一事件可能被多条 item 命中）。"""
    event: Event
    is_update: bool = False          # 有实质更新 → alert_type=event_update
    needs_analysis: bool = False     # 是否需要跑 Stage B
    resend_allowed: bool = True      # 修订原因是否在复推白名单内


class Pipeline:
    def __init__(self, app: AppContext):
        self.app = app
        self.alert_sender: AlertSender | None = None
        # 实体别名词典（每轮 run 开始时失效重建，/watch 注册下一轮生效）
        self._entity_index_cache: list | None = None
        # 仅补充佐证的合并（action="merged"）默认不重跑 Stage B：
        # 事件 version 未变，重算出的 impact 会被 Alert 幂等键拦下，
        # 用户看不到任何变化，却实打实消耗一次 LLM 调用（任务书 §17）。
        self._reanalyze_on_merge = bool(
            app.config.get("ai.reanalyze_on_non_material_merge", False))

    def _llm_stop_status(self) -> str:
        """缺 LLM Key 的停止码按当前 Provider 区分：
        默认 Gemini → STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING；
        显式切到 OpenAI → 保持 STOP_LLM_KEY_MISSING 兼容。"""
        if self.app.config.llm.provider == "gemini":
            return STOP_TRACE_MVP_V1_GEMINI_KEY_MISSING
        return STOP_LLM_KEY_MISSING

    # ------------------------------------------------------------------
    def run_once(self, *, enforce_intervals: bool = False) -> RunSummary:
        """执行一轮完整的采集→事件→分析→提醒流程，返回运行摘要。

        enforce_intervals：True 时按 settings.yaml collectors.interval_seconds
        跳过未到期的采集器（长驻 run/bot 循环用）；run-once 验收默认
        False，保证每次都真实采集。结束后摘要落库到 run_history。
        """
        from trace.alerts.delivery_worker import drain_outbox
        before = drain_outbox(self.app.db, self.alert_sender, is_production=TraceMode.is_production())
        summary = self._run_once(enforce_intervals=enforce_intervals)
        summary.alerts_sent += before['sent']
        summary.alerts_failed += before['failed']
        summary.alerts_suppressed += before['suppressed']
        try:
            from trace.db.repositories import RunHistoryRepo
            history = RunHistoryRepo(self.app.db)
            history.insert(summary)
            history.prune(keep=int(self.app.config.get("ops.run_history_keep", 500)))
        except Exception:
            logger.exception("run history persistence failed")
        return summary

    def _run_once(self, *, enforce_intervals: bool) -> RunSummary:
        ctx = self.app
        summary = RunSummary(run_id=new_run_id(),
                             started_at=datetime.now(timezone.utc).isoformat(),
                             trace_mode=TraceMode.current())

        # 每轮失效缓存：来源/证券主数据、聚类时间窗口可能在两轮之间变化
        self._entity_index_cache = None
        ctx.pipeline.refresh_caches()
        ctx.event_engine.refresh_caches()
        ctx.confirmer.invalidate_cache()
        # 上一轮若中途退出，采集器上还挂着未提交的游标。必须丢弃后再采：
        # 否则本轮 get() 读到的是"已跳过"的暂存值，那批条目照样丢。
        dropped = ctx.collectors.discard_cursors()
        if dropped:
            logger.warning("discarded %d uncommitted collector cursor(s) from "
                           "an aborted round: their items will be re-collected",
                           dropped)

        # 默认接收人：TELEGRAM_DEFAULT_CHAT_ID（仅当该用户从未注册时初始化，
        # 之后用户的 /watch /alert /timezone 设置不会被覆盖）
        self._ensure_default_chat(ctx)

        # 1. 采集（失败分类写入 source_health，不吞异常）
        items, results = ctx.collectors.run_all(respect_intervals=enforce_intervals)
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

        # Persist every raw version and its extraction intent before advancing cursors.
        dedup = ExactDedup(ctx.db)
        job_repo = ProcessingJobRepo(ctx.db)
        raw_repo = RawItemRepo(ctx.db)
        stage_a_before = ctx.pipeline.extractor.real_llm_calls
        stage_b_before = ctx.pipeline.analyzer.real_llm_calls
        verifier_before = getattr(ctx.event_engine.verifier, 'real_llm_calls', 0)
        impact_repo = ctx.pipeline.impact_repo
        pending = {}
        for item in items:
            normalize_raw_item(item)
            from trace.common.source_policy import permitted
            if not permitted(ctx.db, item.source_id, 'store'):
                summary.notes.append('source_storage_not_permitted:' + item.source_id)
                continue
            with ctx.db.transaction(mode='IMMEDIATE'):
                match = dedup.check(item)
                if match.is_duplicate:
                    summary.raw_items_duplicate += 1
                    # A duplicate input can still have unfinished durable work.
                    existing = raw_repo.get(match.existing_raw_item_id) if match.existing_raw_item_id else None
                    if existing and not existing.event_id:
                        job_repo.create_or_update('stage_a_extract', existing.raw_item_id)
                    continue
                raw_repo.insert(item)
                job_repo.create_or_update('stage_a_extract', item.raw_item_id,
                    input_json={'revision_event_id': match.existing_event_id if match.is_revision else None})
                summary.raw_items_new += 1
        summary.cursors_committed = ctx.collectors.commit_cursors()

        for candidate in job_repo.list_pending('stage_a_extract', limit=500):
            job = job_repo.claim(candidate.job_id)
            if job is None:
                continue
            item = raw_repo.get(job.target_id)
            if item is None:
                job_repo.finish(job, 'dead_letter', 'raw_payload_missing')
                continue
            try:
                extracted = ctx.pipeline.extractor.extract(item)
                decision = ctx.event_engine.ingest(item, extracted, skip_exact_dedup=True,
                    target_event_id=job.input_json.get('revision_event_id'),
                    on_commit=lambda: job_repo.finish(job, 'completed'))
                if decision.action == 'created':
                    summary.events_created += 1
                elif decision.action == 'revised':
                    summary.events_revised += 1
                elif decision.action == 'merged':
                    done = job_repo.get_by_target('stage_b_analyze', decision.event.event_id)
                    if done and done.status in ('completed', 'succeeded_empty'):
                        summary.stage_b_skipped += 1
            except SchemaValidationError as exc:
                with ctx.db.transaction():
                    job_repo.finish(job, 'human_review', str(exc))
                    HumanReviewRepo(ctx.db).add(revision_id(), raw_item_id=item.raw_item_id,
                        reason=f'stage_a_schema_validation_failed: {exc}')
                summary.human_review += 1
            except LLMBudgetExceededError as exc:
                job_repo.finish(job, 'blocked_budget', str(exc))
                summary.status = STOP_LLM_BUDGET_EXCEEDED
                summary.notes.append(str(exc))
                break
            except LLMUnavailableError as exc:
                job_repo.finish(job, 'blocked_budget', str(exc))
                summary.status = self._llm_stop_status()
                summary.notes.append(str(exc))
                break
            except Exception as exc:
                if job_repo.owns(job):
                    job_repo.finish(job, 'failed', type(exc).__name__)
                logger.exception('Stage A job failed: %s', job.job_id)
                summary.notes.append('stage_a_job_failed:' + job.job_id)

        from trace.db.jobs import event_from_job
        for candidate in job_repo.list_pending('stage_b_analyze', limit=500):
            if summary.status != STATUS_OK:
                break
            job = job_repo.claim(candidate.job_id)
            if job is None:
                continue
            current = ctx.event_engine.event_repo.get(job.target_id)
            if current is None:
                job_repo.finish(job, 'dead_letter', 'event_missing')
                continue
            if current.version != job.input_version and job.input_json:
                job_repo.finish(job, 'superseded', 'newer_event_version_available')
                continue
            event = event_from_job(job, current)
            previous_impacts = impact_repo.list_by_event(event.event_id)
            is_update = job.input_json.get('is_update', event.version > 1)
            resend_allowed = job.input_json.get('resend_allowed', True)
            try:
                job_repo.renew_lease(job, 600)
                # All model/quote work occurs outside the write transaction.
                impacts = ctx.pipeline.analyze_event(event,
                    extra_entities=self.entity_names_for_event(event), persist=False,
                    evidence_ids=job.input_json.get('evidence_ids'))
                with ctx.db.transaction(mode='IMMEDIATE'):
                    if not job_repo.owns(job):
                        raise RuntimeError('Processing lease lost')
                    latest = ctx.event_engine.event_repo.get(event.event_id)
                    if latest.version != event.version:
                        job_repo.finish(job, 'superseded', 'event_revised_during_analysis')
                        continue
                    ctx.pipeline.persist_results(event, impacts)
                    from trace.db.repositories import ResearchQuestionRepo
                    for impact in impacts:
                        for raw_id in job.input_json.get('evidence_ids', []):
                            ResearchQuestionRepo(ctx.db).match_new_evidence(raw_id, event_id=event.event_id, security_id=impact.security_id)
                    analysis_id = 'AN-' + job.job_id
                    ctx.db.execute('''INSERT OR IGNORE INTO analysis_run
                        (analysis_id,event_id,event_version,processor_version,model,created_at,evidence_ids_json,impacts_json,status)
                        VALUES (?,?,?,?,?,?,?,?,?)''',
                        (analysis_id,event.event_id,event.version,job.processor_version,
                         ctx.config.llm.model_impact_analyzer,datetime.now(timezone.utc).isoformat(),
                         json.dumps(job.input_json.get('evidence_ids', [])),
                         json.dumps([asdict(i) for i in impacts],default=str,ensure_ascii=False),
                         'completed' if impacts else 'succeeded_empty'))
                    batch = ctx.alert_engine.evaluate(event, impacts, is_update=is_update, resend_allowed=resend_allowed)
                    summary.alerts_eligible += len(batch.decisions)
                    summary.alerts_suppressed += batch.suppressed
                    for suppression in batch.bootstrap_suppressions:
                        ctx.alert_engine.mark_bootstrap_suppressed(suppression)
                        summary.alerts_suppressed += 1
                    for decision in batch.decisions:
                        self._deliver(decision, summary, analysis_id=analysis_id)
                    job_repo.finish(job, 'completed' if impacts else 'succeeded_empty')
                summary.events_analyzed += 1
                pending[event.event_id] = event
            except SchemaValidationError as exc:
                with ctx.db.transaction():
                    job_repo.finish(job, 'human_review', str(exc))
                    event.needs_human_review = True
                    ctx.event_engine.event_repo.update(event)
                    HumanReviewRepo(ctx.db).add(revision_id(), event_id=event.event_id,
                        reason=f'stage_b_schema_validation_failed: {exc}')
                summary.human_review += 1
            except LLMBudgetExceededError as exc:
                job_repo.finish(job, 'blocked_budget', str(exc))
                summary.status = STOP_LLM_BUDGET_EXCEEDED
                summary.notes.append(str(exc))
            except LLMUnavailableError as exc:
                job_repo.finish(job, 'blocked_budget', str(exc))
                summary.status = self._llm_stop_status()
                summary.notes.append(str(exc))
            except Exception as exc:
                if job_repo.owns(job):
                    job_repo.finish(job, 'failed', type(exc).__name__)
                logger.exception('Stage B job failed: %s', job.job_id)
                summary.notes.append('stage_b_job_failed:' + job.job_id)

        # 4. 开盘后补算此前拿不到的市场确认（零 LLM 成本）
        self._rescore_market_confirmations(summary, set(pending))

        # 4.5 消费并排空 Outbox 投递队列 (T06 / F06)
        from trace.alerts.delivery_worker import drain_outbox
        drain_stats = drain_outbox(
            ctx.db,
            self.alert_sender,
            limit=100,
            is_production=TraceMode.is_production(),
        )
        summary.alerts_sent += drain_stats["sent"]
        summary.alerts_failed += drain_stats["failed"]
        summary.alerts_suppressed += drain_stats["suppressed"]
        if TraceMode.is_production() and drain_stats["failed"] > 0:
            if self.alert_sender is None:
                summary.status = STOP_TELEGRAM_CREDENTIALS_MISSING
            else:
                summary.status = STOP_TELEGRAM_DELIVERY_FAILED

        # LLM 成本统计（任务书 §17）：本轮真实 API 调用次数（含重试）
        summary.llm_stage_a_calls = ctx.pipeline.extractor.real_llm_calls - stage_a_before
        summary.llm_stage_b_calls = ctx.pipeline.analyzer.real_llm_calls - stage_b_before
        summary.llm_verifier_calls = (
            getattr(ctx.event_engine.verifier, "real_llm_calls", 0) - verifier_before)

        # 全部来源失败且无任何数据 → 明确状态（不伪装成功）
        if summary.sources_failed and summary.sources_succeeded == 0 \
                and summary.raw_items_new == 0:
            summary.status = STOP_SOURCE_UNAVAILABLE
            summary.notes.append("all enabled sources failed")
        logger.info("run summary: %s", summary.as_dict())
        return summary

    # ------------------------------------------------------------------
    def _rescore_market_confirmations(self, summary: RunSummary,
                                      analyzed_ids: set[str]) -> None:
        """对此前因休市／无行情而只能取中性确认的事件重新计分。

        盘后公告在分析当时拿不到市场确认（见行情时段门禁），
        market_confirmation 只能是中性 5.0。等次日开盘，这个分数才真正可得 ——
        没有这一步，score_delta_ge_1 与 first_cross_threshold 两条复推规则
        在默认配置下永远不会触发（佐证性合并跳过 Stage B，状态类修订又已升版）。

        零 LLM 成本：base_score 已落库，供需信号是确定性词表函数，
        只有行情是新的。分数实质变化才升版本并再次评估投递。
        """
        ctx = self.app
        if not ctx.config.get("scoring.rescore_on_market_open", True):
            return

        # 先清理已超过反应窗口截止时刻的过期记录 (F17)
        ctx.pipeline.impact_repo.expire_stale_pending_confirmations()

        impacts = ctx.pipeline.impact_repo.list_pending_confirmation(
            _PENDING_CONFIRMATION_MODES,
            since_hours=float(ctx.config.get("markets.confirmation_max_age_hours", 24)) * 2)
        if not impacts:
            return

        score_delta = float(ctx.config.get("alerts.score_resend_delta", 1.0))
        by_event: dict[str, list] = {}
        for imp in impacts:
            if imp.event_id in analyzed_ids:
                continue          # 本轮刚分析过，用的就是最新行情
            by_event.setdefault(imp.event_id, []).append(imp)
        if not by_event:
            return

        events = ctx.event_engine.event_repo.get_many(list(by_event))
        for event_id, group in by_event.items():
            event = events.get(event_id)
            if event is None:
                continue
            try:
                rescored, materially_changed = self._rescore_event(
                    event, group, score_delta)
            except Exception:
                logger.exception("rescore failed for event %s", event_id)
                continue
            if not rescored:
                continue          # 仍然拿不到确认：下轮再试

            summary.rescored_events += 1
            if materially_changed:
                # 分数跳变达到复推阈值：升版本，已推送过的也允许作为更新再推
                result = ctx.event_engine.reviser.apply_analysis_change(
                    event, ["score_delta_ge_1"])
                event, is_update = result.event, True
                resend_allowed = result.resend_allowed
            else:
                # 更常见的情况：这是该事件**第一次**拿到真实市场确认，
                # 不是内容更新。按 new_event 评估即可 —— 之前因低于阈值
                # 而没推过的，此刻跨过阈值会自然推送（first_cross_threshold）；
                # 已经推过的会被幂等键拦下，不会重复打扰。
                is_update, resend_allowed = False, True

            batch = ctx.alert_engine.evaluate(
                event, rescored, is_update=is_update,
                resend_allowed=resend_allowed)
            summary.alerts_eligible += len(batch.decisions)
            summary.alerts_suppressed += batch.suppressed
            for suppression in batch.bootstrap_suppressions:
                ctx.alert_engine.mark_bootstrap_suppressed(suppression)
                summary.alerts_suppressed += 1
            for dec in batch.decisions:
                self._deliver(dec, summary)

    def _rescore_event(self, event: Event, impacts: list,
                       score_delta: float) -> tuple[list, bool]:
        """用当前行情重算该事件各 impact 的 final_score。

        返回 (成功重算的 impact 列表, 是否有分数跳变达到 score_resend_delta)。

        注意量级：市场确认对分数的影响上限是
        market_confirmation_weight × 5（默认 0.15×5 = 0.75），**低于**默认的
        score_resend_delta = 1.0。也就是说单靠行情确认几乎不可能触发
        score_delta_ge_1 —— 重算的真正价值在于让此前低于阈值的事件跨过阈值
        （first_cross_threshold），而那条路径不需要升版本。
        """
        from trace.scoring.signals import detect_supply_demand

        ctx = self.app
        # 供需信号是确定性词表函数：对同一事件重算必然得到同一结果
        sd_score = detect_supply_demand(event.title, event.summary).score
        rescored: list = []
        materially_changed = False
        for imp in impacts:
            security = ctx.pipeline.security_repo.get(imp.security_id)
            if security is None:
                continue
            confirmation = ctx.confirmer.confirm(
                security.market, security.ticker, imp.direction,
                event_time=event.event_time, security_id=security.security_id)
            if confirmation.mode in _PENDING_CONFIRMATION_MODES:
                # 更新可能计算得出的调度窗口，下轮继续按时补算
                imp.next_eligible_at = confirmation.next_eligible_at
                imp.expires_at = confirmation.expires_at
                ctx.pipeline.impact_repo.upsert(imp)
                continue          # 仍然拿不到：保持原样，下轮再试

            new_score = ctx.pipeline.scoring.final_score(
                imp.base_score, confirmation.score, sd_score)
            old_score = imp.final_score
            imp.final_score = new_score
            imp.market_confirmation = confirmation.score
            imp.market_data_mode = confirmation.mode
            imp.next_eligible_at = None
            imp.expires_at = None
            ctx.pipeline.impact_repo.upsert(imp)
            logger.info("rescored %s: %.2f → %.2f (market_confirmation=%.1f, %s)",
                        imp.impact_id, old_score, new_score,
                        confirmation.score, confirmation.mode)
            rescored.append(imp)
            if abs(new_score - old_score) >= score_delta:
                materially_changed = True
        return rescored, materially_changed

    # ------------------------------------------------------------------
    def _ensure_default_chat(self, ctx: AppContext) -> None:
        default_id = ctx.config.telegram.default_chat_id
        if not default_id:
            return
        from trace.db.repositories import ChannelBindingRepo
        if ctx.alert_engine.user_repo.get(default_id) is not None:
            return  # 用户已存在：不覆盖其设置
        ctx.alert_engine.user_repo.ensure(
            default_id, ctx.config.telegram.default_user_timezone)
        ChannelBindingRepo(ctx.db).bind(default_id, "telegram", default_id)
        ctx.alert_engine.rule_repo.set_threshold(
            default_id, None, ctx.alert_engine.default_threshold)
        logger.info("default chat %s registered (all-threshold=%.1f)",
                    default_id, ctx.alert_engine.default_threshold)

    # ------------------------------------------------------------------
    def _deliver(self, decision: AlertDecision, summary: RunSummary, *, analysis_id: str | None = None) -> None:
        ctx = self.app
        user = ctx.alert_engine.user_repo.get(decision.user_id)
        tz = user.timezone if user else ctx.config.telegram.default_user_timezone
        text, url = ctx.alert_renderer.render(
            decision.event, decision.impact, tz,
            is_update=(decision.alert_type == "event_update"))

        from trace.db.repositories import AlertOutboxRepo, ChannelBindingRepo
        from trace.domain.models import AlertOutbox
        import secrets

        binding_repo = ChannelBindingRepo(ctx.db)
        bindings = binding_repo.list_active(decision.user_id)
        if not bindings:
            logger.info("user %s has no active notification channels, skipping direct push",
                        decision.user_id)
            return

        outbox_repo = AlertOutboxRepo(ctx.db)
        for b in bindings:
            idem_key = (f"{decision.user_id}:{decision.event.event_id}:"
                        f"{decision.impact.security_id}:{decision.event.version}:"
                        f"{decision.alert_type}:{b.channel_type}:{b.channel_target}")
            outbox_item = AlertOutbox(
                outbox_id=f"out_{secrets.token_hex(8)}",
                user_id=decision.user_id,
                channel_type=b.channel_type,
                channel_target=b.channel_target,
                event_id=decision.event.event_id,
                impact_id=decision.impact.impact_id,
                event_version=decision.event.version,
                alert_type=decision.alert_type,
                idempotency_key=idem_key,
                content_text=text,
                content_url=url,
                security_id=decision.impact.security_id,
                final_score=decision.impact.final_score,
                analysis_id=analysis_id,
            )
            outbox_repo.enqueue(outbox_item)

    # ------------------------------------------------------------------
    def _entity_index(self) -> list[tuple[list[str], str]]:
        """实体别名词典 [(候选名列表, 展示名)]，每轮 run 开始时失效重建。

        同时覆盖：
            - 非上市实体/概念节点（entity_alias 表：Samsung、NAND、HBM…）
            - 上市证券名称（security 表：中芯国际、Micron…）
        A股公告标题通常包含公司名，只查 entity_alias 会导致 A股事件
        找不到任何候选证券。
        """
        if self._entity_index_cache is None:
            from trace.db.repositories import EntityAliasRepo, SecurityRepo
            index: list[tuple[list[str], str]] = []
            for entity in EntityAliasRepo(self.app.db).all_with_aliases():
                index.append(([entity.name, *entity.aliases], entity.name))
            for sec in SecurityRepo(self.app.db).list_all():
                candidates = [sec.company_name_zh, sec.company_name_en,
                              sec.ticker, *sec.aliases]
                index.append(
                    ([c for c in candidates if c],
                     sec.company_name_zh or sec.company_name_en or sec.ticker))
            self._entity_index_cache = index
        return self._entity_index_cache

    def entity_names_for_event(self, event: Event) -> list[str]:
        """从事件标题/摘要中用别名表粗提取实体名（供图谱映射）。"""
        def in_text(name: str) -> bool:
            n = (name or "").strip().lower()
            if not n:
                return False
            if re.fullmatch(r"[a-z][a-z0-9.\-]{0,5}", n):
                # 短代码（ticker）：词边界匹配，避免 "MU" 误配英文单词
                return re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])",
                                 text) is not None
            return n in text

        text = f"{event.title} {event.summary}".lower()
        names: list[str] = []
        for candidates, display in self._entity_index():
            if any(in_text(c) for c in candidates):
                names.append(display)
        return names

    # ------------------------------------------------------------------
    def run_forever(self, poll_seconds: int = 60,
                    enforce_intervals: bool = True) -> None:
        """长驻轮询循环：按 collectors.interval_seconds 调度采集器，
        每轮后执行运营维护（预测回测 / 每日摘要 / 每日备份）。"""
        from trace.common.process_lock import SingleInstanceLock
        lock_path = Path(self.app.config.db_path).parent / "runner.lock"
        with SingleInstanceLock(lock_path, name="pipeline_runner"):
            logger.info("pipeline started (poll=%ss, enforce_intervals=%s)",
                        poll_seconds, enforce_intervals)
            from trace.alerts.delivery_worker import start_delivery_worker
            stop_delivery, delivery_thread = start_delivery_worker(self.app.db, self.alert_sender, production=TraceMode.is_production())
            try:
                while True:
                    try:
                        self.run_once(enforce_intervals=enforce_intervals)
                    except Exception:
                        logger.exception("pipeline round failed")
                    try:
                        self._maintenance()
                    except Exception:
                        logger.exception("pipeline maintenance failed")
                    time.sleep(poll_seconds)
            except KeyboardInterrupt:
                logger.info("pipeline stopped (keyboard interrupt)")
            finally:
                stop_delivery.set()
                delivery_thread.join(timeout=5)

    # ------------------------------------------------------------------
    def _maintenance(self) -> None:
        """长驻循环每轮的运营维护：预测回测 → 每日摘要 → 每日备份。

        全部幂等：每天/每个 impact 只执行一次，重复触发是空操作。
        """
        self._run_forecast_checks()
        self._maybe_send_digest()
        self._maybe_backup()

    def _run_forecast_checks(self) -> None:
        """预测回测：对到期的方向预测用真实行情核对并落账。"""
        if not self.app.config.get("forecast.enabled", True):
            return
        try:
            self.app.ledger.run_due_checks()
        except Exception:
            logger.exception("forecast ledger check failed")

    def _maybe_send_digest(self) -> None:
        """每日摘要：到达配置时间（用户时区）后生成并推送，每天一次。"""
        import pytz

        ctx = self.app
        if not ctx.config.get("digest.enabled", True):
            return
        tz = pytz.timezone(ctx.config.telegram.default_user_timezone)
        now_local = datetime.now(timezone.utc).astimezone(tz)
        if now_local.strftime("%H:%M") < str(ctx.config.get("digest.send_time", "21:00")):
            return
        today = now_local.date().isoformat()
        if ctx.digest_builder.digest_repo.get(today):
            return          # 今日摘要已生成（幂等）

        # 生产模式无投递通道：不生成（避免"已推送"的伪装记录）
        if self.alert_sender is None and TraceMode.is_production():
            logger.error("production mode without telegram channel: "
                         "daily digest skipped (refuse fake delivery)")
            return

        digest = ctx.digest_builder.build(today)
        logger.info("daily digest built for %s", today)
        if self.alert_sender is None:
            logger.info("[DIGEST]\n%s", digest.content_markdown)
            return
        receipt = self.alert_sender(ctx.config.telegram.default_chat_id,
                                    digest.content_markdown, None)
        logger.info("daily digest sent: status=%s response=%s",
                    receipt.status, receipt.response)

    def _maybe_backup(self) -> None:
        """每日 SQLite 备份（幂等：同一天只备一份），带轮换。"""
        from trace.db.backup import create_backup, latest_backup

        ctx = self.app
        if not ctx.config.get("backup.enabled", True):
            return
        backup_dir = Path(ctx.config.get("backup.directory", Path(ctx.config.db_path).parent / "backups"))
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        latest = latest_backup(backup_dir)
        if latest is not None and latest.name.startswith(f"trace-{today}"):
            return
        try:
            create_backup(ctx.config.db_path, backup_dir,
                          keep=int(ctx.config.get("backup.keep", 7)))
        except Exception:
            logger.exception("daily backup failed")
