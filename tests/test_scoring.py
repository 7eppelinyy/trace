"""评分引擎测试：公式集中管理、可配置、可追溯。"""

from trace.scoring.engine import ScoreInput, ScoringEngine


def test_base_score_formula(config):
    engine = ScoringEngine(config)
    # base = 0.22*10 + 0.28*9 + 0.30*8 + 0.20*7 = 2.2+2.52+2.4+1.4 = 8.52
    assert engine.base_score(10, 9, 8, 7) == 8.52


def test_final_score_with_market_confirmation(config):
    engine = ScoringEngine(config)
    base = 8.0
    # market_confirmation=8 → +0.15*(8-5)=+0.45
    assert engine.final_score(base, 8.0) == 8.45
    # 中性行情不加减分
    assert engine.final_score(base, 5.0) == 8.0
    # 反向行情扣分
    assert engine.final_score(base, 2.0) == 7.55


def test_final_score_clamped(config):
    engine = ScoringEngine(config)
    assert engine.final_score(12.0, 10.0) == 10.0
    assert engine.final_score(0.0, 1.0) == 1.0


def test_dimensions_clamped_to_1_10(config):
    engine = ScoringEngine(config)
    out = engine.score(ScoreInput(source_reliability=99, directness=-5,
                                  magnitude=50, persistence=0))
    # 维度被夹到 [1,10]：0.22*10+0.28*1+0.30*10+0.20*1 = 2.2+0.28+3+0.2=5.68
    assert out.base_score == 5.68


def test_no_market_data_is_neutral(config):
    engine = ScoringEngine(config)
    out = engine.score(ScoreInput(source_reliability=5, directness=5,
                                  magnitude=5, persistence=5))
    assert out.final_score == out.base_score == 5.0


# ---------------------------------------------------------------------------
# 供需景气信号维度（Supply-Demand Signal，财报季方法论）
# ---------------------------------------------------------------------------

def test_final_score_with_supply_demand(config):
    engine = ScoringEngine(config)
    base = 8.0
    # supply_demand=8（偏紧/涨价）→ +0.10*(8-5)=+0.30
    assert engine.final_score(base, 5.0, supply_demand=8.0) == 8.30
    # supply_demand=2（过剩/疲软）→ -0.30
    assert engine.final_score(base, 5.0, supply_demand=2.0) == 7.70
    # supply_demand=5 中性：不加减分
    assert engine.final_score(base, 5.0, supply_demand=5.0) == 8.0


def test_score_with_supply_demand_in_score_input(config):
    engine = ScoringEngine(config)
    out = engine.score(ScoreInput(source_reliability=8, directness=8,
                                  magnitude=8, persistence=8,
                                  market_confirmation=5.0, supply_demand=9.0))
    # base = 0.22*8+0.28*8+0.30*8+0.20*8 = 8.0；sd=9 → +0.10*4=+0.40
    assert out.base_score == 8.0
    assert out.final_score == 8.40


def test_supply_demand_default_is_neutral(config):
    engine = ScoringEngine(config)
    # 未显式提供 supply_demand 时取 5，行为与以往一致（不改变既有结果）
    out = engine.score(ScoreInput(source_reliability=5, directness=5,
                                  magnitude=5, persistence=5))
    assert out.final_score == 5.0
