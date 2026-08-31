"""AI 分析架构。

不得：新闻全文 → LLM → 随便生成一段分析。
必须拆成两个阶段：

    Stage A：Event Extractor
        发生了什么 / 涉及什么实体 / 关键数字 / event_type /
        event_status / 时间 / 事实 / 证据引用

    Stage B：Impact Analyzer
        影响哪些证券 / 直接还是间接 / 产业传导路径 /
        direction / directness / magnitude / persistence /
        reason / confidence / 证据引用

LLM 必须 Structured Output；Schema 校验失败不得进入 Alert Engine。
Confidence 与 Importance 分离：confidence 表达证据充分度，
不是上涨/下跌概率。

任务书 §8 字段对齐：
    Stage A: event_type / event_status / summary_zh / entities / facts /
             key_numbers / occurred_at / source_claim_type /
             uncertainties / evidence_ids
    Stage B: security_ticker(=security_id) / direction / impact_relation /
             directness / magnitude / persistence / confidence / reason_zh /
             industry_path / supporting_evidence_ids / counter_evidence_ids /
             assumptions
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class SchemaValidationError(Exception):
    """LLM 输出不符合 Schema：该事件不得进入 Alert Engine。"""


class LLMUnavailableError(RuntimeError):
    """生产模式下缺少 LLM Key：不得生成伪 AI 分析。"""


# ---------------------------------------------------------------------------
# Stage A：Event Extractor 输出 Schema
# ---------------------------------------------------------------------------

EVENT_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1},
        "summary": {"type": "string", "minLength": 1},
        "summary_zh": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
        "facts": {"type": "array", "items": {"type": "string"}},
        "event_type": {
            "type": "string",
            "enum": ["regulation", "earnings", "guidance", "product", "supply_chain",
                     "pricing", "capacity", "major_customer", "ma", "management",
                     "lawsuit", "macro", "geopolitical", "other"],
        },
        "event_status": {
            "type": "string",
            "enum": ["rumor", "reported", "partially_confirmed", "official_confirmed"],
        },
        "source_claim_type": {
            "type": "string",
            "enum": ["official_filing", "official_press_release", "company_disclosure",
                     "media_report", "regulatory_announcement", "unknown"],
        },
        "key_numbers": {"type": "array", "items": {"type": "string"}},
        "occurred_at": {"type": ["string", "null"]},
        "event_time": {"type": ["string", "null"]},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "summary", "entities", "event_type", "event_status",
                 "facts", "evidence_ids"],
}


# ---------------------------------------------------------------------------
# Stage B：Impact Analyzer 输出 Schema（针对单个证券）
# ---------------------------------------------------------------------------

IMPACT_ANALYZER_SCHEMA = {
    "type": "object",
    "properties": {
        "impacts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # security_id：本系统 Security Master 中的 ticker（如 SNDK / 688981.SH）
                    "security_ticker": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["bullish", "bearish", "neutral", "mixed", "uncertain"],
                    },
                    "impact_relation": {
                        "type": "string",
                        "enum": ["direct", "indirect", "conditional"],
                    },
                    "directness": {
                        "type": "string",
                        "enum": ["direct", "indirect", "conditional"],
                    },
                    "directness_score": {"type": "number", "minimum": 0, "maximum": 10},
                    "magnitude": {"type": "number", "minimum": 1, "maximum": 10},
                    "persistence": {"type": "number", "minimum": 1, "maximum": 10},
                    "reason": {"type": "string"},
                    "reason_zh": {"type": "string"},
                    "industry_path": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "supporting_evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "counter_evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["security_ticker", "direction", "impact_relation",
                             "directness", "magnitude", "persistence", "reason",
                             "confidence", "supporting_evidence_ids"],
            },
        },
    },
    "required": ["impacts"],
}


def validate(data: dict, schema: dict) -> None:
    """轻量 JSON Schema 校验（不引入 jsonschema 依赖）。校验失败抛异常。"""
    _validate_node(data, schema, path="$")


def _validate_node(data, schema: dict, path: str) -> None:
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            raise SchemaValidationError(f"{path}: expected object, got {type(data).__name__}")
        for req in schema.get("required", []):
            if req not in data:
                raise SchemaValidationError(f"{path}: missing required field '{req}'")
        for key, subschema in schema.get("properties", {}).items():
            if key in data:
                _validate_node(data[key], subschema, f"{path}.{key}")
    elif t == "array":
        if not isinstance(data, list):
            raise SchemaValidationError(f"{path}: expected array")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(data):
                _validate_node(item, item_schema, f"{path}[{i}]")
    elif isinstance(t, list):
        ok = any(_type_matches(data, x) for x in t)
        if not ok:
            raise SchemaValidationError(f"{path}: expected one of {t}")
    elif t == "string":
        if not isinstance(data, str):
            raise SchemaValidationError(f"{path}: expected string")
        if data is not None and "minLength" in schema and len(data) < schema["minLength"]:
            raise SchemaValidationError(f"{path}: string too short")
        if "enum" in schema and data not in schema["enum"]:
            raise SchemaValidationError(f"{path}: '{data}' not in enum {schema['enum']}")
    elif t == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            raise SchemaValidationError(f"{path}: expected number")
        if "minimum" in schema and data < schema["minimum"]:
            raise SchemaValidationError(f"{path}: {data} < minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            raise SchemaValidationError(f"{path}: {data} > maximum {schema['maximum']}")


def _type_matches(data, t: str) -> bool:
    if t == "null":
        return data is None
    if t == "string":
        return isinstance(data, str)
    if t == "number":
        return isinstance(data, (int, float)) and not isinstance(data, bool)
    if t == "object":
        return isinstance(data, dict)
    if t == "array":
        return isinstance(data, list)
    return False


# ---------------------------------------------------------------------------
# Evidence 完整性：每条结论必须能回到 Evidence ID
# ---------------------------------------------------------------------------

def check_evidence_ids(ids: list, known_ids: set[str]) -> list[str]:
    """过滤掉不在已知证据集合中的 evidence id。

    模型只能引用传入的 Evidence；编造的 id 必须被丢弃，
    不得为了消息完整而保留虚假引用。
    """
    return [str(i) for i in (ids or []) if str(i) in known_ids]


def parse_occurred_at(value) -> datetime | None:
    """occurred_at / event_time 宽松解析；解析失败返回 None（不虚构时间）。"""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
