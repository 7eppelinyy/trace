"""Stage A：Event Extractor。

负责：发生了什么 / 涉及什么实体 / 关键数字 / event_type /
event_status / 时间 / 事实 / 证据引用。

任务书 §7/§8 生产门禁：
    production 模式没有 LLM Key 时，不得生成伪 AI 分析 ——
    抛出 LLMUnavailableError（上层转为 DEGRADED_NO_LLM / STOP）。
    test / offline 模式允许规则兜底，但必须显式标记
    analysis_mode=rule_based_degraded，不得包装成完整真实分析。

模型只能根据传入的 Evidence 抽取事实；禁止编造实体/数字/关系。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from trace.ai.llm_client import LLMClient
from trace.ai.schemas import (
    EVENT_EXTRACT_SCHEMA,
    LLMUnavailableError,
    SchemaValidationError,
    check_evidence_ids,
    parse_occurred_at,
    validate,
)
from trace.common.modes import ANALYSIS_MODE_LLM, ANALYSIS_MODE_RULE_DEGRADED, TraceMode
from trace.domain.models import RawItem
from trace.event_engine.engine import ExtractedEvent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个金融事件抽取器。从给定的新闻/公告（Evidence）中抽取结构化事件信息。
严格要求：
1. 只能使用提供的 Evidence 内容，禁止虚构任何实体、数字、订单、客户关系、收入占比、政策内容。
2. entities 用 Evidence 中出现的公司/机构/产品名（中英文皆可）。
3. facts 是逐条来自原文的事实陈述；每条事实必须能对应 Evidence。
4. event_status 判断：官方发布=official_confirmed；官方公告=official_confirmed；多家媒体报道=reported；单一匿名消息=rumor。
5. evidence_ids 只能填提供的 Evidence ID。
6. 无法确定的字段：字符串填空字符串，列表填空数组，时间填 null，并写入 uncertainties。

只输出一个 JSON 对象，必须严格包含以下全部字段（不得遗漏、不得增删）：
{
  "title": "一句话事件标题（必填，非空）",
  "summary": "事件摘要（必填，非空）",
  "summary_zh": "事件中文摘要",
  "entities": ["公司/机构/产品名"],
  "facts": ["逐条来自原文的事实"],
  "event_type": "regulation|earnings|guidance|product|supply_chain|pricing|capacity|major_customer|ma|management|lawsuit|macro|geopolitical|other 之一",
  "event_status": "rumor|reported|partially_confirmed|official_confirmed 之一",
  "source_claim_type": "official_filing|official_press_release|company_disclosure|media_report|regulatory_announcement|unknown 之一",
  "key_numbers": ["关键数字（如 $500B、30%）"],
  "occurred_at": "ISO8601 时间或 null",
  "uncertainties": ["不确定点"],
  "evidence_ids": ["提供的 Evidence ID"]
}
event_type 必须从上述枚举中选择，不得自创类型（如合作/融资类归入 supply_chain 或 other）。"""

_TYPE_KEYWORDS = {
    "regulation": ["export control", "restriction", "ban", "制裁", "管制", "限制", "出口管制", "法规", "监管"],
    "pricing": ["contract price", "spot price", "合约价", "现货价", "涨价", "跌价", "价格"],
    "capacity": ["capacity", "utilization", "产能", "利用率", "扩产", "减产"],
    "earnings": ["earnings", "revenue", "财报", "营收", "业绩"],
    "guidance": ["guidance", "outlook", "指引", "展望"],
    "product": ["launch", "unveil", "发布", "推出", "新品"],
    "supply_chain": ["supplier", "采购", "供应", "订单", "订单转移"],
    "major_customer": ["customer", "客户", "大客户"],
    "ma": ["acquire", "acquisition", "merger", "并购", "收购", "合并"],
    "macro": ["rate", "fed", "cpi", "利率", "降息", "加息", "通胀"],
    "geopolitical": ["tariff", "关税", "地缘", "冲突"],
}

# 来源类型 → source_claim_type 的确定性映射（官方来源不给模型编造空间）
_SOURCE_CLAIM_TYPE = {
    "src_sec_edgar": "official_filing",
    "src_cninfo": "company_disclosure",
    "src_sse": "company_disclosure",
    "src_szse": "company_disclosure",
    "src_nvidia_ir": "official_press_release",
    "src_micron_ir": "official_press_release",
    "src_sndk_ir": "official_press_release",
    "src_bis": "regulatory_announcement",
    "src_federal_register": "regulatory_announcement",
    "src_fed": "regulatory_announcement",
    "src_csrc": "regulatory_announcement",
    "src_miit": "regulatory_announcement",
    "src_mofcom": "regulatory_announcement",
    "src_stats_cn": "regulatory_announcement",
}

# 官方来源的公告/披露在规则兜底下即为官方确认（不给模型编造空间，
# 也不得把官方公告包装成"媒体报道"）。非官方来源保持 reported。
_OFFICIAL_SOURCES = {sid for sid in _SOURCE_CLAIM_TYPE}


def _rule_event_status(source_id: str) -> str:
    return "official_confirmed" if source_id in _OFFICIAL_SOURCES else "reported"


class EventExtractor:
    def __init__(self, llm: LLMClient, llm_config):
        self.llm = llm
        self.model = llm_config.model_event_extractor
        # 最近一次抽取使用的模式（llm / rule_based_degraded），供流水线落库标记
        self.last_mode: str = ANALYSIS_MODE_LLM
        # LLM 成本统计（任务书 §17）：Stage A 真实调用次数
        self.llm_calls: int = 0

    def extract(self, item: RawItem) -> ExtractedEvent:
        if self.llm.available:
            try:
                result = self._extract_with_llm(item)
                self.last_mode = ANALYSIS_MODE_LLM
                self.llm_calls += 1
                return result
            except SchemaValidationError:
                # Schema 校验重试后仍失败：不得进入 Alert 链路，向上暴露
                self.last_mode = ANALYSIS_MODE_RULE_DEGRADED
                raise
            except LLMUnavailableError:
                raise
            except Exception as exc:
                logger.warning("Stage A LLM failed: %s", exc)
        # 生产模式：没有 LLM 不得生成伪 AI 分析
        if TraceMode.is_production() and not self.llm.available:
            raise LLMUnavailableError(
                "TRACE_MODE=production but no LLM key: refuse pseudo AI extraction")
        self.last_mode = ANALYSIS_MODE_RULE_DEGRADED
        return self._extract_with_rules(item)

    # ------------------------------------------------------------------
    def _extract_with_llm(self, item: RawItem) -> ExtractedEvent:
        evidence_ids = [item.raw_item_id]
        user_prompt = (
            f"Evidence（ID: {item.raw_item_id}）\n"
            f"来源: {item.source_id}\n标题: {item.title}\n"
            f"内容: {(item.content or item.reference or '')[:4000]}\n"
            f"可用 Evidence ID 列表: {evidence_ids}"
        )
        data = self.llm.complete_json_validated(
            self.model, SYSTEM_PROMPT, user_prompt, EVENT_EXTRACT_SCHEMA)
        validate(data, EVENT_EXTRACT_SCHEMA)

        event_time = parse_occurred_at(data.get("occurred_at") or data.get("event_time"))
        return ExtractedEvent(
            title=data["title"],
            summary=data["summary"],
            summary_zh=data.get("summary_zh", ""),
            entities=data.get("entities", []),
            facts=data.get("facts", []),
            uncertainties=data.get("uncertainties", []),
            source_claim_type=data.get("source_claim_type")
            or _SOURCE_CLAIM_TYPE.get(item.source_id, "unknown"),
            event_type=data["event_type"],
            event_status=data["event_status"],
            event_time=event_time or item.published_at,
            key_numbers=data.get("key_numbers", []),
            evidence_ids=check_evidence_ids(data.get("evidence_ids", []),
                                            set(evidence_ids)),
        )

    # ------------------------------------------------------------------
    def _extract_with_rules(self, item: RawItem) -> ExtractedEvent:
        """规则兜底（仅 test/offline）：标题即事实，不做任何虚构。"""
        text = f"{item.title} {item.content or ''}".lower()
        event_type = "other"
        for etype, kws in _TYPE_KEYWORDS.items():
            if any(k in text for k in kws):
                event_type = etype
                break
        return ExtractedEvent(
            title=item.title,
            summary=item.title,
            summary_zh="",
            entities=[],
            facts=[item.title],
            uncertainties=["rule_based_degraded: 无 LLM，仅按标题抽取"],
            source_claim_type=_SOURCE_CLAIM_TYPE.get(item.source_id, "unknown"),
            event_type=event_type,
            event_status=_rule_event_status(item.source_id),
            event_time=item.published_at,
            key_numbers=re.findall(r"\d+(?:\.\d+)?%|\$\d[\d,.]*[BbMm]?", item.title),
            evidence_ids=[item.raw_item_id],
        )
