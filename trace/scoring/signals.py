"""供需景气确定性信号（Supply-Demand Signal）。

设计动机（财报季方法论，任务书相关）：
    把「业绩大增公司财报里出现 供不应求/涨价/高景气 等措辞 → 重点关注」
    的启发式固化成一个**确定性、可测试、脱离 LLM 主观打分**的信号维度，
    与"市场确认"并列作为 final_score 的对称微调。

哲学约束（对齐评分引擎）：
    - 禁止让 LLM 凭感觉打 1–10 分；本模块只用**词表匹配**产出客观信号。
    - 它表达"文字里供需拐点的强度"，不是上涨/下跌概率。
      方向（bullish/bearish 每个证券）仍由 Stage B 独立判定。
    - 对周期股必须克制：词表里「价格中枢上涨/供给偏紧/缺货」常出现在景气
      顶部。因此本信号是**对称**的（同时识别供需偏紧与供需过剩），且权重小，
      只微调 final_score，绝不单独反转方向。详见 supply_demand_signals.yaml。

分数映射（确定性，测试锁定）：
    raw = (#bullish 命中) - (#bearish 命中)
    raw > 0 : score = 6 + min(3, raw)   → 7 / 8 / 9 / 9…
    raw < 0 : score = 4 - min(3, -raw)  → 3 / 2 / 1 / 1…
    raw = 0 : score = 5（中性，不加减分）
    取值范围 [1, 10]，中心 5=中性。direction 由 raw 符号决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_SIGNALS_PATH = Path(__file__).parent.parent / "data" / "supply_demand_signals.yaml"
_cache: dict | None = None


@dataclass
class SupplyDemandSignal:
    """单个事件的供需信号。"""
    score: float = 5.0                      # 1–10，5=中性
    direction: str = "neutral"              # bullish / bearish / neutral
    bull_matches: list[str] = field(default_factory=list)
    bear_matches: list[str] = field(default_factory=list)
    bull_count: int = 0
    bear_count: int = 0

    @property
    def has_signal(self) -> bool:
        return self.bull_count > 0 or self.bear_count > 0


def load_signals(path: Path | None = None) -> dict:
    """加载词表（进程内缓存）。"""
    global _cache
    actual = path or _SIGNALS_PATH
    if path is None and _cache is not None:
        return _cache
    with open(actual, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if path is None:
        _cache = data
    return data


def focus_guidance() -> str:
    """Stage A 抽取指引文本（来自 yaml，与词表同源）。"""
    return str(load_signals().get("focus_guidance", "")).strip()


def _to_lower_list(items) -> list[str]:
    return [str(x).strip().lower() for x in (items or []) if str(x).strip()]


def detect_supply_demand(title: str, summary: str = "") -> SupplyDemandSignal:
    """对事件的标题+摘要扫描供需词表，输出确定性信号。

    只扫文本不做任何推断；匹配数量即信号强度。
    """
    signals = load_signals()
    bull = _to_lower_list(signals.get("bullish"))
    bear = _to_lower_list(signals.get("bearish"))

    text = f"{title or ''} {summary or ''}".lower()
    bull_hits = sorted({k for k in bull if k in text})
    bear_hits = sorted({k for k in bear if k in text})
    b, d = len(bull_hits), len(bear_hits)

    raw = b - d
    if raw > 0:
        score = 6 + min(3, raw)          # 7/8/9
        direction = "bullish"
    elif raw < 0:
        score = 4 - min(3, -raw)         # 3/2/1
        direction = "bearish"
    else:
        score = 5.0
        direction = "neutral"

    return SupplyDemandSignal(
        score=float(max(1, min(10, score))),
        direction=direction,
        bull_matches=bull_hits,
        bear_matches=bear_hits,
        bull_count=b,
        bear_count=d,
    )
