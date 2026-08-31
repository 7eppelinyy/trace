"""集中评分引擎。

禁止让 LLM 凭感觉直接打 1–10 分。
LLM 只输出基础维度（directness / magnitude / persistence），
来源可靠度由系统（source_registry）提供。

第一版公式（权重可配置，集中管理，有测试，可追溯）：

    base_score =
        0.22 * source_reliability
      + 0.28 * directness
      + 0.30 * magnitude
      + 0.20 * persistence

    final_score = clamp(
        base_score + 0.15 * (market_confirmation - 5),
        1, 10)

所有输入维度取值 1–10。market_confirmation 无行情时取 5（中性，不加减分）。
Confidence 与 Importance 分离：本引擎不产出 confidence，
confidence 来自 Impact Analyzer 的证据充分度，二者独立展示。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScoreInput:
    source_reliability: float     # 1-10，来自 source_registry
    directness: float             # 1-10，LLM 输出
    magnitude: float              # 1-10，LLM 输出
    persistence: float            # 1-10，LLM 输出
    market_confirmation: float = 5.0   # 1-10，行情确认；缺省 5


@dataclass
class ScoreOutput:
    base_score: float
    final_score: float


class ScoringEngine:
    def __init__(self, config):
        self._weights = config.get("scoring.base_weights", {})
        self._market_weight = float(config.get("scoring.market_confirmation_weight", 0.15))
        self._min = float(config.get("scoring.score_min", 1))
        self._max = float(config.get("scoring.score_max", 10))

    def base_score(self, source_reliability: float, directness: float,
                   magnitude: float, persistence: float) -> float:
        w = self._weights
        score = (
            float(w.get("source_reliability", 0.22)) * self._clamp_dim(source_reliability)
            + float(w.get("directness", 0.28)) * self._clamp_dim(directness)
            + float(w.get("magnitude", 0.30)) * self._clamp_dim(magnitude)
            + float(w.get("persistence", 0.20)) * self._clamp_dim(persistence)
        )
        return round(score, 2)

    def final_score(self, base: float, market_confirmation: float = 5.0) -> float:
        mc = self._clamp_dim(market_confirmation)
        score = base + self._market_weight * (mc - 5.0)
        return round(max(self._min, min(self._max, score)), 2)

    def score(self, inp: ScoreInput) -> ScoreOutput:
        base = self.base_score(inp.source_reliability, inp.directness,
                               inp.magnitude, inp.persistence)
        final = self.final_score(base, inp.market_confirmation)
        return ScoreOutput(base_score=base, final_score=final)

    @staticmethod
    def _clamp_dim(v: float) -> float:
        return max(1.0, min(10.0, float(v)))
