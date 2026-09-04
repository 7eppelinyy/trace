"""LLM same-event verifier。

merge_score 落在 [0.72, 0.82) 区间时调用：
判断两条候选是否为同一事件。LLM 不可用时保守返回 False（创建新 Event）。
"""

from __future__ import annotations

import logging

from trace.ai.llm_client import LLMClient
from trace.ai.schemas import LLMUnavailableError
from trace.domain.models import Event
from trace.event_engine.engine import ExtractedEvent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """判断两条事件描述是否为同一个现实事件（同一件事的不同报道/语言版本）。
只输出 JSON：{"same_event": true} 或 {"same_event": false}。
判断依据：核心实体、动作、对象是否一致；时间接近。仅仅是同一主题不算同一事件。"""


class LLMSameEventVerifier:
    def __init__(self, llm: LLMClient, llm_config):
        self.llm = llm
        self.model = llm_config.model_same_event_verifier
        # LLM 成本统计（任务书 §17）：llm_calls 为成功业务次数，
        # real_llm_calls 为真实 API 调用次数（含重试消耗）
        self.llm_calls: int = 0
        self.real_llm_calls: int = 0

    def is_same_event(self, new: ExtractedEvent, existing: Event) -> bool:
        if not self.llm.available:
            return False
        calls_before = self.llm.call_count
        try:
            user_prompt = (
                f"事件A 标题: {new.title}\n事件A 摘要: {new.summary}\n"
                f"事件B 标题: {existing.title}\n事件B 摘要: {existing.summary}"
            )
            data = self.llm.complete_json(self.model, SYSTEM_PROMPT, user_prompt)
            self.llm_calls += 1
            self.real_llm_calls += self.llm.call_count - calls_before
            return bool(data.get("same_event", False))
        except LLMUnavailableError:
            # 含预算耗尽：向上抛而不是"当作不同事件"。静默返回 False 会在
            # 配额用尽时把同一事件拆成一堆重复 Event，污染事件库。
            self.real_llm_calls += self.llm.call_count - calls_before
            raise
        except Exception as exc:
            self.real_llm_calls += self.llm.call_count - calls_before
            logger.warning("same-event verifier failed: %s", exc)
            return False
