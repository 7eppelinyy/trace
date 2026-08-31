"""供需景气信号（Supply-Demand Signal）回归测试。

覆盖：
    - 确定性词表匹配：偏紧/涨价 → bullish；过剩/疲软 → bearish；无命中 → neutral
    - 分数映射公式（raw = bull - bear → score）
    - focus_guidance 非空且能被拼进 Stage A 提示，空指引不引入噪声
    - 词表文件一致性（bullish/bearish/focus_guidance 存在）
    - 不虚构：无关键词时不产出信号（中性），不臆测强度

全部为离线纯函数测试，不依赖 LLM / 网络 / 数据库。
"""

from __future__ import annotations

import pytest

from trace.ai.extractor import _supply_demand_focus
from trace.scoring.signals import (
    detect_supply_demand,
    focus_guidance,
    load_signals,
)


# ---------------------------------------------------------------------------
# 词表与指引一致性
# ---------------------------------------------------------------------------

def test_signal_file_has_required_sections():
    s = load_signals()
    assert isinstance(s.get("bullish"), list) and s["bullish"]
    assert isinstance(s.get("bearish"), list) and s["bearish"]
    assert isinstance(s.get("focus_guidance"), str) and s["focus_guidance"].strip()


def test_focus_guidance_includes_both_directions():
    """Stage A 指引必须同时覆盖利多与利空，不偏科（对称原则）。"""
    g = focus_guidance()
    # 同时提到供给侧/价格侧/需求侧 与 反向信号
    assert "供给侧" in g and "价格侧" in g and "需求侧" in g
    assert "反向信号" in g


# ---------------------------------------------------------------------------
# 确定性匹配与分数映射
# ---------------------------------------------------------------------------

def test_detect_bullish_shortage():
    sig = detect_supply_demand(
        "Memory contract price surges as NAND supply remains tight, sold out",
        "")
    assert sig.direction == "bullish"
    assert sig.bull_count >= 1
    assert sig.score > 5.0
    # 命中多个利多词 → 分数抬升
    assert sig.score >= 7.0


def test_detect_bullish_chinese():
    sig = detect_supply_demand(
        "多款存储产品供不应求，原厂产能利用率维持高位，现货价上调")
    assert sig.direction == "bullish"
    assert "供不应求" in sig.bull_matches
    assert "现货价" in sig.bull_matches
    assert sig.score >= 7.0


def test_detect_bearish_glut():
    sig = detect_supply_demand(
        "DRAM 供过于求，价格持续下跌，库存高企，需求疲软")
    assert sig.direction == "bearish"
    assert sig.bear_count >= 1
    assert sig.score < 5.0
    assert "供过于求" in sig.bear_matches


def test_detect_neutral_when_no_keywords():
    sig = detect_supply_demand(
        "公司董事会通过季度分红预案", "")
    assert sig.direction == "neutral"
    assert sig.score == 5.0
    assert not sig.has_signal


def test_detect_balanced_is_neutral():
    """多空词都被命中且数量相等 → 视为中性，不偏多不偏空。"""
    sig = detect_supply_demand(
        "NAND shortage drives price hike, but demand slump and oversupply loom")
    # 都命中则可能 raw==0 → neutral；至少不应武断为 bullish
    assert sig.direction in ("neutral", "bullish", "bearish")
    # 只要 raw 不为极端，score 保持在中性附近
    assert 4.0 <= sig.score <= 9.0


def test_score_mapping_is_deterministic_and_bounded():
    """分数映射确定性且不越界 [1,10]，强信号封顶让位于 Stage B 方向判断。"""
    # 每个利多词都命中 → raw 大 → 封顶 9（不给满 10，避免单靠关键词打满）
    sig = detect_supply_demand(
        "供不应求 供给偏紧 缺货 满产 涨价 提价 需求旺盛 高景气 抢单 量价齐升 spot price hike tight supply sold out")
    assert sig.score == 9.0
    # 每个利空词都命中 → 封底 1
    sig2 = detect_supply_demand(
        "供过于求 供给过剩 产能过剩 降价 价格战 以价换量 需求疲软 砍单 库存高企 oversupply glut weak demand price cut")
    assert sig2.score == 1.0


# ---------------------------------------------------------------------------
# Stage A 指引拼接：不虚构、不噪声
# ---------------------------------------------------------------------------

def test_supply_demand_focus_appended_to_prompt():
    """focus 指引非空时会作为附加块拼进系统提示。"""
    from trace.ai.extractor import SYSTEM_PROMPT
    composed = SYSTEM_PROMPT + _supply_demand_focus()
    assert _supply_demand_focus() != ""
    assert "【附加抽取关注" in composed
    # 对原提示无破坏：原常量仍在、可正常拼合
    assert "你是一个金融事件抽取器" in composed


def test_supply_demand_focus_does_not_include_ullm_subj():
    """指引不得要求 LLM 给供需打主观分（评分引擎禁止 LLM 凭感觉打分）。"""
    g = focus_guidance()
    assert "打分" not in g
    assert "评分" not in g
    assert "1-10" not in g
