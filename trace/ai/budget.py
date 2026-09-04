"""LLM 每日调用预算（成本熔断）。

任务书 §17 只要求统计成本，但统计是**事后**的：run 摘要告诉你昨晚花了多少，
拦不住今晚再花一次。真实的失控场景是静默的 ——

    某个来源改版 → canonical_url / source_item_id 变了
    → Level 1 确定性去重全线穿透
    → 同一批新闻每轮都被当成新条目
    → Stage A + Stage B 每轮重跑

功能测试全绿、日志正常，只有账单会变。本模块提供事前护栏：

    每次真实 API 调用前检查当日累计次数，超限抛 LLMBudgetExceededError，
    由流水线转为 STOP_LLM_BUDGET_EXCEEDED 结束本轮。

计数落库（llm_usage 表）而不是放进程内存：长驻服务会重启，run-once 每次都是
新进程，内存计数器等于没有护栏。按 UTC 日切，与 run 摘要口径一致。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from trace.ai.schemas import LLMUnavailableError
from trace.db.connection import Database

logger = logging.getLogger(__name__)


class LLMBudgetExceededError(LLMUnavailableError):
    """当日 LLM 调用预算已用尽：停止本轮，不得继续消耗配额。

    刻意继承 LLMUnavailableError：Stage A/B 里对 LLMUnavailableError 是
    "向上抛、不降级"，而对普通 Exception 是"落到规则兜底"。预算耗尽必须走
    前者 —— 否则会静默地把整批事件降级成 rule_based_degraded 分析，
    正是任务书 §7 禁止的伪装。
    """


class LLMBudget:
    """当日 LLM 真实调用次数的持久化计数与上限。

    daily_limit <= 0 表示不限制（计数照常累计，只是不熔断）——
    这样即使关闭护栏也能看到用量，便于给上限选一个合适的值。
    """

    def __init__(self, db: Database, daily_limit: int = 0):
        self.db = db
        self.daily_limit = int(daily_limit)

    # ------------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def used(self, date_str: str | None = None) -> int:
        row = self.db.query_one(
            "SELECT calls FROM llm_usage WHERE date_str=?",
            (date_str or self._today(),))
        return int(row["calls"]) if row else 0

    def remaining(self) -> int | None:
        """剩余可用次数；未设上限时返回 None。"""
        if self.daily_limit <= 0:
            return None
        return max(0, self.daily_limit - self.used())

    @property
    def enabled(self) -> bool:
        return self.daily_limit > 0

    # ------------------------------------------------------------------
    def check(self) -> None:
        """调用前检查。超限抛 LLMBudgetExceededError。"""
        if not self.enabled:
            return
        used = self.used()
        if used >= self.daily_limit:
            raise LLMBudgetExceededError(
                f"daily LLM call budget exhausted: {used}/{self.daily_limit} "
                f"(UTC {self._today()}); raise llm.daily_call_budget or wait for reset")

    def consume(self, n: int = 1) -> int:
        """记录 n 次真实 API 调用（含重试消耗的每一次），返回当日累计。"""
        today = self._today()
        self.db.execute(
            """INSERT INTO llm_usage (date_str, calls, updated_at) VALUES (?,?,?)
               ON CONFLICT(date_str) DO UPDATE SET
                 calls = calls + excluded.calls, updated_at = excluded.updated_at""",
            (today, int(n), datetime.now(timezone.utc).isoformat()))
        used = self.used(today)
        if self.enabled and used == self.daily_limit:
            logger.warning("daily LLM call budget reached: %d/%d",
                           used, self.daily_limit)
        return used

    # ------------------------------------------------------------------
    def render(self) -> str:
        """给 doctor / /status 用的一行摘要。"""
        used = self.used()
        if not self.enabled:
            return f"今日 LLM 调用 {used} 次（未设上限）"
        return f"今日 LLM 调用 {used}/{self.daily_limit} 次（剩余 {self.remaining()}）"
