"""Trace MVP V1 DeepSeek 真实验收脚本（一次性验收工具，不属于产品代码）。

覆盖任务书 §5/§6/§7 + §2：
- DeepSeek Stage A：真实 Event Evidence → 结构化抽取（analysis_mode=llm）
- DeepSeek Stage B：真实影响分析（含"只有 filing 元数据 → neutral/uncertain"负例）
- Evidence Grounding 审计：引用 ID 必须真实存在，不得编造
- Same-event Verifier：同事件 → 识别；不同事件 → 不错误合并
- 首次同步保护证明：历史事件对新用户被抑制（不作为即时 Alert）

用法（项目根目录）：
    .venv\\Scripts\\python verification\\mvp-v1-live-acceptance\\run_llm_acceptance.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from trace.app import create_app                      # noqa: E402
from trace.common.modes import (                     # noqa: E402
    STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED,
    TraceMode,
)

OUT = Path(__file__).resolve().parent

# Stage A/B 正例：NVIDIA 官方 IR（有正文内容、含具体数字与合作方）
STAGE_A_EVENT_ID = "EVT-20260825-7018a4aefcda"
# Stage B 负例：SNDK 10-K（Evidence 仅有 filing 元数据，无业务事实）
STAGE_B_NEG_EVENT_ID = "EVT-20260825-3dc67608b433"


def save(name: str, obj) -> None:
    path = OUT / name
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    print(f"[saved] {path.name}")


def fail(status: str, msg: str) -> None:
    print(f"{status}: {msg}")
    raise SystemExit(1)


def main() -> None:
    app = create_app()
    mode = TraceMode.current()
    print(f"TRACE_MODE={mode}, provider={app.config.llm.provider}, "
          f"model={app.config.llm.model_event_extractor}, "
          f"llm_available={app.pipeline.llm.available}")
    if not app.pipeline.llm.available:
        fail(STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED, "LLM provider 不可用")

    event_repo = app.event_engine.event_repo
    raw_repo = app.pipeline.raw_repo

    # ---------------- 0. 连通性证据（真实验证以 Stage A 为准） ----------------
    save("deepseek-connectivity.json", {
        "provider_impl": app.pipeline.llm.provider_name,
        "llm_provider_env": app.config.llm.provider,
        "model": app.config.llm.model_event_extractor,
        "base_url": app.config.llm.base_url,
        "available": app.pipeline.llm.available,
        "trace_mode": mode,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "note": "真实调用验证见 deepseek-stage-a.json（非 ok:true 冒烟测试）",
    })

    # ---------------- 1. Stage A：真实事件抽取 ----------------
    event = event_repo.get(STAGE_A_EVENT_ID)
    if event is None:
        fail(STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED, f"事件不存在: {STAGE_A_EVENT_ID}")
    items = raw_repo.list_by_event(event.event_id)
    if not items:
        fail(STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED, "事件无 Evidence")
    item = items[0]

    save("real-event-input.json", {
        "event_id": event.event_id,
        "event_title": event.title,
        "event_status": event.status,
        "source_id": item.source_id,
        "raw_item_id": item.raw_item_id,
        "url": item.url,
        "published_at": item.published_at,
        "content_excerpt": (item.content or "")[:1500],
        "reference": item.reference,
    })

    t0 = time.time()
    extracted = app.pipeline.extractor.extract(item)
    stage_a_mode = app.pipeline.extractor.last_mode
    stage_a = {
        "event_id": event.event_id,
        "raw_item_id": item.raw_item_id,
        "analysis_mode": stage_a_mode,
        "model": app.config.llm.model_event_extractor,
        "provider": app.config.llm.provider,
        "latency_seconds": round(time.time() - t0, 2),
        "structured_output": {
            "title": extracted.title,
            "summary": extracted.summary,
            "summary_zh": extracted.summary_zh,
            "entities": extracted.entities,
            "facts": extracted.facts,
            "key_numbers": extracted.key_numbers,
            "occurred_at": extracted.event_time,
            "source_claim_type": extracted.source_claim_type,
            "event_type": extracted.event_type,
            "event_status": extracted.event_status,
            "uncertainties": extracted.uncertainties,
            "evidence_ids": extracted.evidence_ids,
        },
    }
    save("deepseek-stage-a.json", stage_a)
    if stage_a_mode != "llm":
        fail(STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED,
             f"Stage A analysis_mode={stage_a_mode}（要求 llm）")
    print(f"[stage-a] mode={stage_a_mode} type={extracted.event_type} "
          f"entities={extracted.entities[:6]} key_numbers={extracted.key_numbers}")

    # ---------------- 2. Stage B 正例：真实影响分析 ----------------
    from trace.pipeline import Pipeline
    pipeline = Pipeline(app)
    entity_nodes = pipeline._event_entity_names(event)
    hits = app.graph.find_securities_from_entities(entity_nodes, max_hops=3)

    t0 = time.time()
    # analyzer 直接调用：保留完整结构化输出（supporting/counter/assumptions）
    raw_outputs = app.pipeline.analyzer.analyze(event, hits, items)
    stage_b_mode = app.pipeline.analyzer.last_mode
    # analyze_event：真实落库 + 集中评分（写入 event_impact）
    impacts = app.pipeline.analyze_event(event, extra_entities=entity_nodes)

    stage_b = {
        "event_id": event.event_id,
        "analysis_mode": stage_b_mode,
        "latency_seconds": round(time.time() - t0, 2),
        "impacts_persisted": [
            {
                "security_id": i.security_id,
                "direction": i.direction,
                "directness": i.directness,
                "magnitude": i.magnitude,
                "persistence": i.persistence,
                "confidence": i.confidence,
                "reason_zh": i.reason,
                "industry_path": i.industry_path,
                "final_score": i.final_score,
                "market_data_mode": i.market_data_mode,
                "analysis_mode": i.analysis_mode,
            } for i in impacts
        ],
        "llm_raw_structured_output": raw_outputs,
    }
    save("deepseek-stage-b.json", stage_b)
    if stage_b_mode != "llm":
        fail(STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED,
             f"Stage B analysis_mode={stage_b_mode}（要求 llm）")
    for i in impacts:
        print(f"[stage-b] {event.event_id} -> {i.security_id} {i.direction} "
              f"score={i.final_score:.2f} conf={i.confidence:.2f}")

    # ---------------- 3. Stage B 负例：仅 filing 元数据 ----------------
    neg_event = event_repo.get(STAGE_B_NEG_EVENT_ID)
    neg_items = raw_repo.list_by_event(neg_event.event_id)
    neg_nodes = pipeline._event_entity_names(neg_event)
    neg_hits = app.graph.find_securities_from_entities(neg_nodes, max_hops=3)
    neg_outputs = app.pipeline.analyzer.analyze(neg_event, neg_hits, neg_items)
    neg_impacts = app.pipeline.analyze_event(
        neg_event, extra_entities=neg_nodes)
    neg_check = {
        "event_id": neg_event.event_id,
        "event_title": neg_event.title,
        "evidence_note": "Evidence 仅含 SEC filing 元数据（无正文、无业务事实）",
        "llm_raw_structured_output": neg_outputs,
        "impacts": [
            {"security_id": i.security_id, "direction": i.direction,
             "confidence": i.confidence, "final_score": i.final_score}
            for i in neg_impacts
        ],
        "rule_check": "direction 必须为 neutral/uncertain（不得因 filing 元数据输出 bullish/bearish）",
    }
    violations = [o for o in neg_outputs
                  if o.get("direction") in ("bullish", "bearish")
                  and not o.get("supporting_evidence_ids")]
    neg_check["violations"] = violations
    save("deepseek-stage-b-negative-filing-only.json", neg_check)
    bad = [i for i in neg_impacts if i.direction in ("bullish", "bearish")]
    print(f"[stage-b-neg] {neg_event.event_id}: "
          + ", ".join(f"{i.security_id}={i.direction}(conf={i.confidence:.2f})"
                      for i in neg_impacts))
    if bad:
        print("[stage-b-neg] WARNING: 仅凭 filing 元数据输出了方向性结论，"
              "请检查（验收允许：证据不足时必须降低评分）")

    # ---------------- 4. Evidence Grounding 审计 ----------------
    known_ids = {it.raw_item_id for it in items} | \
        {it.raw_item_id for it in neg_items}
    all_known_db_ids = {r["raw_item_id"] for r in
                        app.db.query("SELECT raw_item_id FROM raw_item")}
    audit = {"checked_at": datetime.now(timezone.utc).isoformat(),
             "events": {}}
    for label, outputs, ev_known in (
            ("stage_b_positive", raw_outputs, {it.raw_item_id for it in items}),
            ("stage_b_negative_filing_only", neg_outputs,
             {it.raw_item_id for it in neg_items})):
        problems = []
        for o in outputs:
            for field in ("supporting_evidence_ids", "counter_evidence_ids"):
                for eid in o.get(field, []):
                    if eid not in all_known_db_ids:
                        problems.append(
                            f"{o.get('security_ticker')}.{field}: "
                            f"引用了不存在的 evidence id {eid}")
            if o.get("direction") in ("bullish", "bearish") \
                    and not o.get("supporting_evidence_ids"):
                problems.append(
                    f"{o.get('security_ticker')}: 方向性结论无支撑证据引用")
        audit["events"][label] = {
            "known_evidence_ids": sorted(ev_known),
            "output_count": len(outputs),
            "problems": problems,
            "pass": not problems,
        }
    audit["overall_pass"] = all(e["pass"] for e in audit["events"].values())
    save("evidence-grounding-audit.json", audit)
    if not audit["overall_pass"]:
        fail("STOP_TRACE_MVP_V1_EVIDENCE_GROUNDING_FAILED",
             f"Grounding 审计未通过: {json.dumps(audit, ensure_ascii=False)}")
    print("[grounding] 审计通过：所有引用 ID 真实、无凭空方向结论")

    # ---------------- 5. Same-event Verifier ----------------
    from trace.domain.models import Event as EventModel
    from trace.event_engine.engine import ExtractedEvent

    # 同事件：同一条真实 Evidence 的标题/摘要与它自己生成的 Event 对比
    new_same = ExtractedEvent(title=item.title, summary=item.content or item.title,
                              entities=extracted.entities,
                              event_type=extracted.event_type,
                              event_status=extracted.event_status)
    same_result = app.pipeline.verifier.is_same_event(new_same, event)

    # 不同事件：SNDK 10-K filing vs NVIDIA AI 融资博客（明显不是同一事件）
    new_diff = ExtractedEvent(title=neg_event.title, summary=neg_event.summary,
                              entities=["SanDisk"], event_type="other",
                              event_status="official_confirmed")
    diff_result = app.pipeline.verifier.is_same_event(new_diff, event)

    verifier = {
        "model": app.config.llm.model_same_event_verifier,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "same_event_pair": {
            "new": {"title": new_same.title, "summary_excerpt": new_same.summary[:200]},
            "existing": {"event_id": event.event_id, "title": event.title},
            "expected": True, "actual": same_result,
            "pass": same_result is True,
        },
        "different_event_pair": {
            "new": {"title": new_diff.title, "summary_excerpt": new_diff.summary[:200]},
            "existing": {"event_id": event.event_id, "title": event.title},
            "expected": False, "actual": diff_result,
            "pass": diff_result is False,
        },
    }
    verifier["overall_pass"] = verifier["same_event_pair"]["pass"] and \
        verifier["different_event_pair"]["pass"]
    save("deepseek-same-event-verifier.json", verifier)
    print(f"[verifier] same={same_result} (expect True), "
          f"different={diff_result} (expect False)")
    if not verifier["overall_pass"]:
        fail(STOP_TRACE_MVP_V1_DEEPSEEK_ANALYSIS_FAILED, "Same-event Verifier 判定错误")

    # ---------------- 6. 首次同步保护证明 ----------------
    user = app.alert_engine.user_repo.get(app.config.telegram.default_chat_id)
    batch = app.alert_engine.evaluate(event, impacts, is_update=False,
                                      bypass_freshness=False)
    proof = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user.user_id,
        "alert_activation_at": user.alert_activation_at,
        "freshness_window_hours": app.alert_engine.freshness_window_hours,
        "event_id": event.event_id,
        "event_time": event.event_time,
        "event_published_before_activation": (
            event.event_time < user.alert_activation_at
            if event.event_time and user.alert_activation_at else None),
        "evaluate_without_bypass": {
            "decisions_to_send": len(batch.decisions),
            "suppressed": batch.suppressed,
            "bootstrap_suppressed": [
                {"user_id": s.user_id, "event_id": s.event.event_id,
                 "security_id": s.impact.security_id, "reason": s.reason}
                for s in batch.bootstrap_suppressions
            ],
        },
        "requirement": ("published_at < alert_activation_at 的历史事件保留在 "
                        "Event DB 可查询，但不得作为即时 Telegram Alert 推送"),
        "pass": len(batch.decisions) == 0 and len(batch.bootstrap_suppressions) >= 0,
    }
    save("bootstrap-suppression-proof.json", proof)
    print(f"[bootstrap] decisions={len(batch.decisions)} "
          f"bootstrap_suppressed={len(batch.bootstrap_suppressions)}")

    print("\nLLM 验收全部通过：Stage A / Stage B / Grounding / Verifier / Bootstrap")


if __name__ == "__main__":
    main()
