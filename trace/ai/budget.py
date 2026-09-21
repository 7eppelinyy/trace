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

    def __init__(self, db: Database, daily_limit: int = 0, *, ask_limit: int = 300,
                 ask_user_limit: int = 30, pipeline_reserve: int | None = None):
        self.db = db
        self.daily_limit = int(daily_limit)
        self.ask_limit = ask_limit
        self.ask_user_limit = ask_user_limit
        self.pipeline_reserve = max(0, daily_limit // 3 if pipeline_reserve is None else pipeline_reserve)

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
    def allow_call(self, n: int = 1) -> bool:
        """非破坏性检查今日剩余配额是否充足（不增加调用计数）。"""
        if not self.enabled:
            return True
        return (self.used() + n) <= self.daily_limit

    def check(self) -> None:
        """调用前检查。超限抛 LLMBudgetExceededError。"""
        if not self.enabled:
            return
        used = self.used()
        if used >= self.daily_limit:
            raise LLMBudgetExceededError(
                f"daily LLM call budget exhausted: {used}/{self.daily_limit} "
                f"(UTC {self._today()}); raise llm.daily_call_budget or wait for reset")

    def try_consume(self, n: int = 1, *, usage_type: str = "pipeline", user_id: str | None = None) -> bool:
        """原子检查并扣减预算。若超限返回 False（不扣减），否则扣减成功并返回 True。"""
        if type(n) is not int or n <= 0:
            raise ValueError("Budget reservation must be a positive integer")
        if usage_type not in ("pipeline", "ask") or (usage_type == "ask" and not user_id):
            raise ValueError("Ask reservations require an authenticated user")
        today = self._today()
        with self.db.transaction(mode="IMMEDIATE"):
            row = self.db.query_one("SELECT calls FROM llm_usage WHERE date_str=?", (today,))
            current = int(row["calls"]) if row else 0
            if self.enabled and current + n > self.daily_limit:
                return False
            scopes = [usage_type]
            if usage_type == "ask":
                import hashlib
                scopes.append("ask-user:" + hashlib.sha256(user_id.encode()).hexdigest())
                # Interactive calls cannot consume the background reserve.
                if self.enabled and current + n > max(0, self.daily_limit - self.pipeline_reserve):
                    return False
                for scope, limit in zip(scopes, (self.ask_limit, self.ask_user_limit)):
                    used = self.db.query_one("SELECT calls FROM llm_usage_scope WHERE date_str=? AND scope=?", (today, scope))
                    if limit <= 0 or (int(used['calls']) if used else 0) + n > limit:
                        return False
            self.db.execute(
                """INSERT INTO llm_usage (date_str, calls, updated_at) VALUES (?,?,?)
                   ON CONFLICT(date_str) DO UPDATE SET
                     calls = calls + excluded.calls, updated_at = excluded.updated_at""",
                (today, int(n), datetime.now(timezone.utc).isoformat()),
            )
            for scope in scopes:
                self.db.execute("""INSERT INTO llm_usage_scope VALUES (?,?,?)
                    ON CONFLICT(date_str,scope) DO UPDATE SET calls=calls+excluded.calls""", (today, scope, n))
            return True

    def consume(self, n: int = 1) -> int:
        """记录 n 次真实 API 调用（含重试消耗的每一次），返回当日累计。"""
        if n <= 0:
            return self.used()
        today = self._today()
        with self.db.transaction(mode="IMMEDIATE"):
            self.db.execute(
                """INSERT INTO llm_usage (date_str, calls, updated_at) VALUES (?,?,?)
                   ON CONFLICT(date_str) DO UPDATE SET
                     calls = calls + excluded.calls, updated_at = excluded.updated_at""",
                (today, int(n), datetime.now(timezone.utc).isoformat()))
        used = self.used(today)
        if self.enabled and used >= self.daily_limit:
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
