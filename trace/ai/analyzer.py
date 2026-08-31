"""Stage B：Impact Analyzer。

负责：影响哪些证券 / 直接还是间接 / 产业传导路径 /
direction / directness / magnitude / persistence / reason / confidence。

候选证券来自产业链图谱（Event → Industry Graph → Security）。
图谱路径只代表"候选影响对象"，最终方向/幅度必须由本次事件
Evidence 决定；LLM 只对候选集合打分，不允许凭空引入证券。
Schema 校验失败的输出不得进入 Alert Engine。

任务书 §7/§8 生产门禁：
    production 模式没有 LLM Key 时，不得生成伪分析 —— 抛
    LLMUnavailableError；test/offline 允许规则兜底但显式标记
    analysis_mode=rule_based_degraded。

不得把二跳、三跳影响包装成确定直接影响：
direct 只允许 1 跳，2 跳为 indirect，>=3 跳为 conditional。
"""

from __future__ import annotations

import logging

from trace.ai.llm_client import LLMClient
from trace.ai.schemas import (
    IMPACT_ANALYZER_SCHEMA,
    LLMUnavailableError,
    SchemaValidationError,
    check_evidence_ids,
    validate,
)
from trace.common.modes import ANALYSIS_MODE_LLM, ANALYSIS_MODE_RULE_DEGRADED, TraceMode
from trace.domain.models import Event, RawItem
from trace.graph.industry_graph import GraphHit

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个事件影响分析器。给定一个事件、证据（Evidence）和一组候选证券（含产业传导路径），
判断事件对每个候选证券的影响。
严格要求：
1. 只评估候选列表中的证券，禁止引入列表外的证券。
2. 只能基于提供的 Evidence 判断；禁止编造新闻、订单、客户关系、收入占比、政策内容。
3. impact_relation/directness 必须与给定跳数一致：1 跳=direct，2 跳=indirect，>=3 跳=conditional。
   图谱中存在路径不等于本次事件已确定影响该证券；证据不足时 direction=uncertain。
4. magnitude/persistence 取 1-10；confidence 取 0-1，证据不足时必须取低值。
5. reason 必须基于事件事实；每条核心结论在 supporting_evidence_ids 中引用 Evidence ID。
6. 区分：直接提及 / 一跳业务关联 / 二跳产业关联 / 仅主题相关，不得夸大。
7. 不输出任何交易建议。

只输出一个 JSON 对象，结构必须严格为：
{
  "impacts": [
    {
      "security_ticker": "候选列表中的证券代码",
      "direction": "bullish|bearish|neutral|mixed|uncertain 之一",
      "impact_relation": "direct|indirect|conditional 之一（与给定跳数一致）",
      "directness": "direct|indirect|conditional 之一",
      "magnitude": 1-10 数值,
      "persistence": 1-10 数值,
      "reason": "基于事件事实的理由（英文可）",
      "reason_zh": "基于事件事实的理由（中文）",
      "industry_path": "产业传导路径",
      "confidence": 0-1 数值,
      "supporting_evidence_ids": ["支持该结论的 Evidence ID"],
      "counter_evidence_ids": ["反例 Evidence ID"],
      "assumptions": ["所做假设"]
    }
  ]
}
对每个候选证券都要给出一条 impact。若 Evidence 不足以判断方向，direction 必须为 neutral 或 uncertain，且 confidence 取低值；
禁止仅凭"提交了某类 filing"这类元数据就输出 bullish 或 bearish。"""

# 跳数 → directness 的硬约束（防止 LLM 夸大直接影响）
_HOPS_DIRECTNESS = {1: "direct", 2: "indirect"}


class ImpactAnalyzer:
    def __init__(self, llm: LLMClient, llm_config):
        self.llm = llm
        self.model = llm_config.model_impact_analyzer
        # 最近一次分析使用的模式（供流水线落库标记）
        self.last_mode: str = ANALYSIS_MODE_LLM
        # LLM 成本统计（任务书 §17）：Stage B 真实调用次数
        self.llm_calls: int = 0

    def analyze(self, event: Event, hits: list[GraphHit],
                evidence_items: list[RawItem]) -> list[dict]:
        """返回通过 schema 校验的 impact 字典列表（含 _hit 元数据）。"""
        if not hits:
            self.last_mode = ANALYSIS_MODE_LLM
            return []
        evidence_text = "\n".join(
            f"- [ID: {it.raw_item_id}] {it.title}"
            + (f": {it.content[:300]}" if it.content else "")
            + (f" | {it.reference}" if it.reference else "")
            for it in evidence_items[:5])
        if self.llm.available:
            try:
                results = self._analyze_with_llm(event, hits, evidence_text,
                                                 evidence_items)
                self.last_mode = ANALYSIS_MODE_LLM
                self.llm_calls += 1
                return results
            except SchemaValidationError:
                # 校验失败（重试后仍失败）：不得进入 Alert Engine，向上暴露
                self.last_mode = ANALYSIS_MODE_RULE_DEGRADED
                raise
            except LLMUnavailableError:
                raise
            except Exception as exc:
                logger.warning("Stage B LLM failed: %s", exc)
        # 生产模式：没有 LLM 不得生成伪分析
        if TraceMode.is_production() and not self.llm.available:
            raise LLMUnavailableError(
                "TRACE_MODE=production but no LLM key: refuse pseudo impact analysis")
        self.last_mode = ANALYSIS_MODE_RULE_DEGRADED
        return self._analyze_with_rules(event, hits, evidence_items)

    # ------------------------------------------------------------------
    def _analyze_with_llm(self, event: Event, hits: list[GraphHit],
                          evidence_text: str,
                          evidence_items: list[RawItem]) -> list[dict]:
        known_ids = {it.raw_item_id for it in evidence_items[:5]}
        candidates = [
            {
                "security_ticker": h.security.ticker,
                "market": h.security.market,
                "name": h.security.company_name_zh or h.security.company_name_en,
                "industry_path": " → ".join(h.path),
                "hops": h.hops,
            }
            for h in hits
        ]
        user_prompt = (
            f"事件标题: {event.title}\n事件摘要: {event.summary}\n"
            f"Evidence:\n{evidence_text[:3000]}\n"
            f"可用 Evidence ID 列表: {sorted(known_ids)}\n"
            f"候选证券: {candidates}"
        )
        data = self.llm.complete_json_validated(
            self.model, SYSTEM_PROMPT, user_prompt, IMPACT_ANALYZER_SCHEMA)
        validate(data, IMPACT_ANALYZER_SCHEMA)

        hits_by_ticker = {h.security.ticker: h for h in hits}
        results: list[dict] = []
        for imp in data.get("impacts", []):
            hit = hits_by_ticker.get(imp.get("security_ticker"))
            if hit is None:
                continue  # LLM 引入列表外证券 → 丢弃
            # 强制 directness 与跳数一致，防止夸大
            forced = _HOPS_DIRECTNESS.get(hit.hops, "conditional")
            imp["directness"] = forced
            imp["impact_relation"] = forced
            # 证据 ID 完整性：编造的引用必须被丢弃
            imp["supporting_evidence_ids"] = check_evidence_ids(
                imp.get("supporting_evidence_ids", []), known_ids)
            imp["counter_evidence_ids"] = check_evidence_ids(
                imp.get("counter_evidence_ids", []), known_ids)
            # 没有可用证据引用且方向确定 → 降级为 uncertain（不得为完整性补造事实）
            if not imp["supporting_evidence_ids"] and imp.get("direction") in (
                    "bullish", "bearish"):
                imp["direction"] = "uncertain"
            imp["_hit"] = hit
            results.append(imp)
        return results

    # ------------------------------------------------------------------
    def _analyze_with_rules(self, event: Event, hits: list[GraphHit],
                            evidence_items: list[RawItem]) -> list[dict]:
        """无 LLM 时的保守规则：方向不确定，幅度适中，低置信度（显式降级）。"""
        evidence = list(evidence_items[:5])
        results: list[dict] = []
        for h in hits:
            results.append({
                "security_ticker": h.security.ticker,
                "direction": "uncertain",
                "impact_relation": _HOPS_DIRECTNESS.get(h.hops, "conditional"),
                "directness": _HOPS_DIRECTNESS.get(h.hops, "conditional"),
                "magnitude": max(3.0, 7.0 - h.hops),
                "persistence": 5.0,
                "reason": (f"规则降级分析（rule_based_degraded）：产业关联（{h.hops} 跳）："
                           f"{' → '.join(h.path)}"),
                "reason_zh": "",
                "industry_path": " → ".join(h.path),
                "confidence": max(0.1, 0.5 - 0.1 * h.hops),
                "supporting_evidence_ids": [it.raw_item_id for it in evidence],
                "counter_evidence_ids": [],
                "assumptions": ["rule_based_degraded: 无 LLM，仅按图谱跳数保守估计"],
                "_hit": h,
            })
        return results


def directness_score(directness: str) -> float:
    """directness → 1-10 数值（进入评分公式）。"""
    return {"direct": 9.0, "indirect": 6.0, "conditional": 3.5}.get(directness, 3.0)
