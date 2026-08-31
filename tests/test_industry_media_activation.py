"""产业媒体启用（Industry Media Activation, 2026-08-31）回归测试。

背景：TrendForce / DIGITIMES / EE Times 原本因 `license_mode=unknown`
被任务书 §6 冻结（enabled=false）。2026-08-31 从生产 VM 实测确认三者
均提供**官方公开 RSS feed**（站点自行发布 feed = 明示授权程序化访问）
且 robots.txt 允许该路径，故升级为 `license_mode=public` 并启用。

本文件锁定四类不变量，防止后续回归：
    1. 三家已启用且 license_mode=public；
    2. 无官方 feed / robots 禁止的来源必须保持 enabled=false
       （尤其 CNBC——技术上能取到但 robots.txt Disallow）；
    3. 同一来源不得被两个采集器重复采集
       （RSSCollector 与 IndustryMediaCollector 的 handled_source_ids 必须互斥）；
    4. feeds.yaml 自身一致性：source_id 必须在 seed 中登记、
       keyword_filter 必须在 source_filters.yaml 中有定义、
       启用中的源必须有采集器认领。

全部为离线断言，不发起任何网络请求。
"""

from __future__ import annotations

from trace.collectors.filters import load_filters, matches_keywords
from trace.collectors.industry_media import IndustryMediaCollector
from trace.collectors.ir import IRCollector
from trace.collectors.rss import RSSCollector, load_feeds
from trace.db.repositories import SourceRepo
from trace.domain.models import LicenseMode

# 本次启用的产业媒体（2026-08-31 实测确认有**实时**官方公开 feed）
ACTIVATED = ("src_digitimes", "src_eetimes")

# 实测无实时官方 feed 或 robots 禁止 → 必须保持关闭
MUST_STAY_DISABLED = (
    "src_trendforce",      # 官方 feed 停更 61 天（最后条目 2026-07-01）
    "src_eetimes_china",   # /rss/news.xml、/rss.xml、/feed 均 404
    "src_jw_insights",     # 集微网：feed 0 条 / 连接超时
    "src_chipwise",        # 芯智讯：服务端断开 / 超时
    "src_cnbc",            # 官方 RSS 可用但 robots.txt Disallow
    "src_reuters",         # 无授权
    "src_bloomberg",
    "src_dowjones",
    "src_ft",
    "src_cls",
    "src_eastmoney",
)


# ---------------------------------------------------------------------------
# 1. 三家已启用且授权模式正确
# ---------------------------------------------------------------------------

def test_activated_industry_media_enabled_and_public(db):
    """TrendForce / DIGITIMES / EE Times：enabled=True 且 license_mode=public。"""
    repo = SourceRepo(db)
    for source_id in ACTIVATED:
        src = repo.get(source_id)
        assert src is not None, f"{source_id} 未在 seed 中登记"
        assert src.enabled is True, f"{source_id} 应已启用"
        assert src.license_mode is LicenseMode.PUBLIC, (
            f"{source_id} 的 license_mode 应为 public（官方 feed = 明示授权），"
            f"实际 {src.license_mode}")
        assert src.source_type == "industry_media"
        # 产业媒体不得被当作官方权威源（影响 primary_source 升级与评分）
        assert src.authority_level == "industry_media"


# ---------------------------------------------------------------------------
# 2. 未获授权 / 无 feed 的来源必须保持关闭
# ---------------------------------------------------------------------------

def test_unauthorized_sources_stay_disabled(db):
    """无官方 feed 或 robots 禁止的来源一律 enabled=false（任务书 §6 保守策略）。"""
    repo = SourceRepo(db)
    for source_id in MUST_STAY_DISABLED:
        src = repo.get(source_id)
        assert src is not None, f"{source_id} 未在 seed 中登记"
        assert src.enabled is False, (
            f"{source_id} 必须保持关闭：无官方 feed 或 robots.txt 禁止抓取")


def test_cnbc_disabled_despite_working_feed(db):
    """CNBC 专项：feed 技术可用不等于获授权，robots.txt Disallow → 必须关闭。

    这条单独立测，因为它最容易被「反正能抓到」的理由误开。
    """
    cnbc = SourceRepo(db).get("src_cnbc")
    assert cnbc is not None
    assert cnbc.enabled is False
    assert cnbc.license_mode is not LicenseMode.PUBLIC
    # 也不得偷偷登记进 feeds.yaml
    assert "src_cnbc" not in {f["source_id"] for f in load_feeds()}


# ---------------------------------------------------------------------------
# 3. 采集器职责互斥：同一来源不得被重复采集
# ---------------------------------------------------------------------------

def test_collector_source_ids_are_disjoint():
    """RSS / IndustryMedia / IR 三个采集器的 handled_source_ids 必须互不相交。

    TrendForce / DIGITIMES 改走 RSS 路线后，若仍留在 IndustryMediaCollector
    的 _MEDIA_PAGES 中，会导致同一来源每轮被采两次（重复请求 + 事件时间被
    now() 污染）。
    """
    rss = RSSCollector.handled_source_ids.fget(RSSCollector)  # type: ignore[attr-defined]
    media = IndustryMediaCollector.handled_source_ids.fget(  # type: ignore[attr-defined]
        IndustryMediaCollector)
    ir = IRCollector.handled_source_ids.fget(IRCollector)  # type: ignore[attr-defined]

    assert not (rss & media), f"RSS 与 IndustryMedia 重叠：{rss & media}"
    assert not (rss & ir), f"RSS 与 IR 重叠：{rss & ir}"
    assert not (media & ir), f"IndustryMedia 与 IR 重叠：{media & ir}"

    # 三家已启用的产业媒体必须归 RSS 采集器
    for source_id in ACTIVATED:
        assert source_id in rss, f"{source_id} 应由 RSSCollector 采集"
        assert source_id not in media, (
            f"{source_id} 已改走 RSS，必须从 IndustryMediaCollector 移除")


# ---------------------------------------------------------------------------
# 4. feeds.yaml 一致性
# ---------------------------------------------------------------------------

def test_activated_media_have_feed_entries():
    """三家启用源都必须在 feeds.yaml 中有 feed 配置，且带关键词初筛与回填窗口。"""
    feeds = {f["source_id"]: f for f in load_feeds()}
    for source_id in ACTIVATED:
        assert source_id in feeds, f"{source_id} 已启用但 feeds.yaml 无配置"
        feed = feeds[source_id]
        assert feed["url"].startswith("https://")
        # 产业媒体条目多，必须挂关键词初筛，否则会无差别灌进 LLM
        assert feed.get("keyword_filter") == "industry_media", (
            f"{source_id} 必须挂 keyword_filter=industry_media 以控 LLM 成本")
        # 首次接入只回填最近 N 天，避免 bootstrap 灌入大量历史
        assert 1 <= int(feed.get("bootstrap_days", 30)) <= 30


def test_dead_feed_urls_not_registered():
    """实测 404 的 EE Times China feed 不得留在 feeds.yaml（否则每轮污染健康状态）。"""
    registered = {f["source_id"] for f in load_feeds()}
    assert "src_eetimes_china" not in registered


def test_every_feed_source_is_seeded(db):
    """feeds.yaml 中每个 source_id 都必须在 source 表中登记（防拼写错误静默失效）。"""
    repo = SourceRepo(db)
    for feed in load_feeds():
        assert repo.get(feed["source_id"]) is not None, (
            f"feeds.yaml 中的 {feed['source_id']} 未在 seed_sources.yaml 登记")


def test_every_keyword_filter_is_defined():
    """feeds.yaml 引用的 keyword_filter 必须在 source_filters.yaml 中存在。

    matches_keywords() 在 filter_name 不存在时**返回 True 放行全部条目**，
    所以过滤器名写错不会报错，只会让该源无差别灌进 LLM —— 属静默故障，
    必须靠这条测试兜住。
    """
    defined = set(load_filters())
    for feed in load_feeds():
        name = feed.get("keyword_filter")
        if name:
            assert name in defined, (
                f"{feed['source_id']} 引用了未定义的 keyword_filter={name}，"
                f"会导致该源全部条目放行进 LLM")


# ---------------------------------------------------------------------------
# 5. industry_media 词表覆盖真实标题
# ---------------------------------------------------------------------------

def test_industry_media_filter_matches_real_titles():
    """用 2026-08-31 实测抓到的真实标题验证词表放行/拦截行为。"""
    # 应放行：存储价格信号（TrendForce 实测标题）
    assert matches_keywords(
        "[Insights] Memory Spot Price Update: DRAM Spot Prices See Gains in "
        "Low-Density DDR4 and DDR3 Amid Sideways Market",
        None, "industry_media")
    # 应放行：先进封装涨价（供给侧信号，DIGITIMES/TrendForce 类标题）
    assert matches_keywords(
        "ASE Reportedly Raises Advanced Packaging Quotes by More Than 20% in "
        "Latest AI-Driven Price Hike", None, "industry_media")
    # 应放行：中文供需信号
    assert matches_keywords("存储器合约价连续三个季度上涨", None, "industry_media")
    assert matches_keywords("原厂减产致NAND现货价走高", None, "industry_media")

    # 应拦截：与存储/半导体供需无关（DIGITIMES 实测标题）
    assert not matches_keywords(
        "Taiwan drone bill dispute leaves NT$240 billion procurement plan in limbo",
        None, "industry_media")
    assert not matches_keywords(
        "Advanced Cooling Technologies Address the Automotive Heat Challenge",
        None, "industry_media")
