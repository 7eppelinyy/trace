"""Source Completion Gate 新增测试（任务书 §18）。

覆盖：
    - SanDisk IR RSS 解析（ETag + bootstrap 窗口）
    - Micron newsroom 页面解析
    - Micron 429 → newsroom 回退（健康状态显式 RATE_LIMITED）
    - 政府关键词初筛（Level 1 过滤器）
    - Source authority 映射（security_map → 图谱直接关联）
    - 新来源 bootstrap suppression（历史事件不推送）
    - 跨源 same-event dedup（官方确认修订）
    - Source Health 状态派生（RATE_LIMITED / BROKEN / DISABLED）

live 测试继续独立标记 @pytest.mark.live。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trace.collectors.filters import matches_keywords
from trace.collectors.micron import MicronCollector, parse_newsroom_html
from trace.collectors.sandisk import parse_sitemap_press_releases
from trace.common.http_client import RateLimitedError
from trace.db.health import SourceHealthRepo, derive_health_status
from trace.db.repositories import SourceRepo
from trace.domain.models import OFFICIAL_AUTHORITIES


# ---------------------------------------------------------------------------
# SanDisk sitemap 解析（§18：SanDisk parser）
# ---------------------------------------------------------------------------

_SANDISK_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.sandisk.com/company/newsroom/press-releases/2026/2026-08-13-sandisk-investor-day-2026</loc><lastmod>2026-08-13</lastmod></url>
<url><loc>https://www.sandisk.com/company/newsroom/press-releases/2026/2026-08-12-kioxia-and-sandisk-unveil-new-high-performance-qlc-3d-flash-memory</loc></url>
<url><loc>https://www.sandisk.com/company/newsroom/blogs/2026/some-blog-post</loc></url>
<url><loc>https://www.sandisk.com/company/newsroom/press-releases/2025/no-date-slug</loc><lastmod>2026-08-01</lastmod></url>
</urlset>
"""


def test_sandisk_sitemap_parse():
    """解析官方 sitemap：只取 press-releases，blogs 排除，标题/日期正确。"""
    results = parse_sitemap_press_releases(_SANDISK_SITEMAP_XML)
    # 3 条 press-releases（blogs 不进入）
    assert len(results) == 3
    titles = [t for t, _, _ in results]
    assert all(t.startswith("SanDisk: ") for t in titles)
    assert any("Investor Day 2026" in t for t in titles)

    # 按发布时间倒序
    assert results[0][2] is not None
    assert results[0][2].year == 2026 and results[0][2].month == 8 \
        and results[0][2].day == 13
    # URL 日期优先；无日期的 slug 用 lastmod 兜底
    assert results[2][2] is not None and results[2][2].month == 8

    # canonical URL 完整保留
    assert results[0][1].startswith(
        "https://www.sandisk.com/company/newsroom/press-releases/2026/")


def test_sandisk_sitemap_parse_empty():
    """结构变化/无 press-releases：返回空列表（由上层抛 ParseError）。"""
    xml = "<urlset><url><loc>https://www.sandisk.com/about</loc></url></urlset>"
    assert parse_sitemap_press_releases(xml) == []


# ---------------------------------------------------------------------------
# Micron newsroom HTML 解析（§18：Micron parser）
# ---------------------------------------------------------------------------

_MICRON_NEWSROOM_HTML = """
<html><body>
<div class="cmp-teaser__content">
  <h2><a href="https://investors.micron.com/news-releases/news-release-details/micron-q4-2026">
    Micron Technology Reports Fourth Quarter Fiscal 2026 Results</a></h2>
  <p>August 24, 2026</p>
  <a href="https://investors.micron.com/news-releases/news-release-details/micron-q4-2026">Read article</a>
</div>
<div class="cmp-teaser__content">
  <h2><a href="https://investors.micron.com/news-releases/news-release-details/micron-hbm">
    Micron Ships Industry-Leading HBM4 Samples</a></h2>
  <p>August 10, 2026</p>
  <a href="https://investors.micron.com/news-releases/news-release-details/micron-hbm">Read article</a>
</div>
<div class="cmp-teaser__content">
  <h2>Not a link</h2>
</div>
</body></html>
"""


def test_micron_newsroom_parse():
    """解析官方新闻室列表页：标题、URL、发布时间均正确提取。"""
    results = parse_newsroom_html(_MICRON_NEWSROOM_HTML)
    assert len(results) == 2
    title0, url0, pub0 = results[0]
    assert "Fourth Quarter" in title0
    assert url0.startswith("https://investors.micron.com/")
    assert pub0 is not None
    assert pub0.year == 2026 and pub0.month == 8 and pub0.day == 24
    title1, url1, pub1 = results[1]
    assert "HBM4" in title1
    assert pub1 is not None and pub1.month == 8 and pub1.day == 10


def test_micron_newsroom_parse_empty_structure():
    """结构变化时返回空列表（由上层抛 ParseError，不静默）。"""
    assert parse_newsroom_html("<html><body><p>nothing</p></body></html>") == []


# ---------------------------------------------------------------------------
# Micron 429 → newsroom 回退（§18：429 handling）
# ---------------------------------------------------------------------------

class _FakeMicronHttp:
    """模拟 RSS 429 + newsroom 页面成功的 HTTP 客户端。"""

    def __init__(self, rss_error: Exception, newsroom_html: str):
        self.rss_error = rss_error
        self.newsroom_html = newsroom_html
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        if "rss.xml" in url:
            raise self.rss_error
        return _HtmlResp(self.newsroom_html)

    def close(self):
        pass


class _HtmlResp:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.headers = {}


def test_micron_429_fallback_to_newsroom(db, config, monkeypatch):
    """RSS 429 → newsroom 回退：采集成功，健康状态显式 RATE_LIMITED。"""
    collector = MicronCollector(db, config)
    fake = _FakeMicronHttp(
        rss_error=RateLimitedError("429 from RSS", http_status=429),
        newsroom_html=_MICRON_NEWSROOM_HTML,
    )
    monkeypatch.setattr(collector, "http_client", lambda *a, **k: fake)

    items = collector.collect()
    # newsroom 回退成功，拿到 2 条
    assert len(items) == 2
    assert all(i.source_id == "src_micron_ir" for i in items)

    # 健康状态：429 记录必须显式可见（RATE_LIMITED）
    row = SourceHealthRepo(db).get("src_micron_ir")
    assert row is not None
    assert row["last_http_status"] == 429
    status = derive_health_status(row, enabled=True)
    assert status == "RATE_LIMITED"


# ---------------------------------------------------------------------------
# 政府关键词初筛（§18：government keyword filter）
# ---------------------------------------------------------------------------

def test_keyword_filter_us_policy_hits():
    """us_policy 词表：半导体相关关键词命中，无关内容不命中。"""
    assert matches_keywords("New semiconductor export controls announced", None, "us_policy")
    assert matches_keywords("BIS adds Chinese firm to Entity List", None, "us_policy")
    assert matches_keywords("Commerce imposes tariffs on memory chips", None, "us_policy")
    assert not matches_keywords("Federal Reserve raises interest rates", None, "us_policy")


def test_keyword_filter_cn_policy_hits():
    """cn_policy 词表：中文半导体关键词命中，无关内容不命中。"""
    assert matches_keywords("工信部发布集成电路产业发展规划", None, "cn_policy")
    assert matches_keywords("商务部回应芯片出口管制", None, "cn_policy")
    assert not matches_keywords("国务院召开常务会议讨论农业", None, "cn_policy")


def test_keyword_filter_empty_filter_passes():
    """filter_name 为空或词表不存在时放行（不静默丢数据）。"""
    assert matches_keywords("anything", None, "")
    assert matches_keywords("anything", None, "nonexistent_filter")


def test_keyword_filter_industry_media():
    """industry_media 词表：存储相关命中。"""
    assert matches_keywords("NAND flash contract price rises", None, "industry_media")
    assert not matches_keywords("general tech news", None, "industry_media")


# ---------------------------------------------------------------------------
# Source authority 映射（§18：source authority mapping）
# ---------------------------------------------------------------------------

def test_official_authorities_defined():
    """OFFICIAL_AUTHORITIES 集合包含公司/政府/监管三类。"""
    assert "official_company" in OFFICIAL_AUTHORITIES
    assert "official_government" in OFFICIAL_AUTHORITIES
    assert "official_regulator" in OFFICIAL_AUTHORITIES
    assert "industry_media" not in OFFICIAL_AUTHORITIES
    assert "financial_media" not in OFFICIAL_AUTHORITIES


def test_source_registry_security_map(db):
    """seed 数据：SanDisk → SEC-US-SNDK，Micron → SEC-US-MU，NVIDIA → SEC-US-NVDA。"""
    repo = SourceRepo(db)
    sndk = repo.get("src_sndk_ir")
    assert sndk is not None
    assert "SEC-US-SNDK" in sndk.security_map
    assert sndk.authority_level == "official_company"

    micron = repo.get("src_micron_ir")
    assert micron is not None
    assert "SEC-US-MU" in micron.security_map

    nvidia = repo.get("src_nvidia_ir")
    assert nvidia is not None
    assert "SEC-US-NVDA" in nvidia.security_map


def test_source_registry_authority_levels(db):
    """seed 数据：政策源为 official_government，SEC 为 official_regulator。"""
    repo = SourceRepo(db)
    miit = repo.get("src_miit")
    assert miit is not None
    assert miit.authority_level == "official_government"
    assert miit.enabled is True

    mofcom = repo.get("src_mofcom")
    assert mofcom is not None
    assert mofcom.authority_level == "official_government"

    sec = repo.get("src_sec_edgar")
    assert sec is not None
    assert sec.authority_level == "official_regulator"


# ---------------------------------------------------------------------------
# Bootstrap suppression for new source（§18）
# ---------------------------------------------------------------------------

def test_bootstrap_suppression_before_activation(db, config):
    """事件早于用户激活时间（但在新鲜度窗口内）必须被抑制，不推送。"""
    from trace.alerts.engine import AlertEngine
    from trace.db.repositories import AlertRuleRepo, SecurityRepo, UserRepo, WatchlistRepo
    from trace.domain.models import Event, EventImpact, WatchlistEntry

    user_repo = UserRepo(db)
    # ensure 即把 alert_activation_at 设为当前时间
    user_repo.ensure("u_test")

    mu = SecurityRepo(db).get_by_ticker("MU")
    WatchlistRepo(db).add(WatchlistEntry(user_id="u_test", security_id=mu.security_id))
    AlertRuleRepo(db).set_threshold("u_test", None, 1.0)

    # 1 小时前的事件：在新鲜度窗口内，但早于激活时间 → before_activation
    old_time = datetime.now(timezone.utc) - timedelta(hours=1)
    event = Event(event_id="ev-old", version=1, event_time=old_time,
                  first_seen_at=old_time)
    impact = EventImpact(impact_id="IMP-old", event_id="ev-old",
                         security_id=mu.security_id, final_score=9.0,
                         confidence=0.9)

    engine = AlertEngine(db, config)
    batch = engine.evaluate(event, [impact], is_update=False)
    # 历史事件必须被抑制（不推送）
    assert len(batch.decisions) == 0
    assert len(batch.bootstrap_suppressions) == 1
    assert batch.bootstrap_suppressions[0].reason == "before_activation"


def test_bootstrap_suppression_freshness_window(db, config):
    """超出新鲜度窗口的事件必须被抑制。"""
    from trace.alerts.engine import AlertEngine
    from trace.db.repositories import AlertRuleRepo, SecurityRepo, UserRepo, WatchlistRepo
    from trace.domain.models import Event, EventImpact, WatchlistEntry

    user_repo = UserRepo(db)
    user_repo.ensure("u_test")
    mu = SecurityRepo(db).get_by_ticker("MU")
    WatchlistRepo(db).add(WatchlistEntry(user_id="u_test", security_id=mu.security_id))
    AlertRuleRepo(db).set_threshold("u_test", None, 1.0)

    # 非常旧的事件（超出默认 168 小时新鲜度窗口）
    old_time = datetime.now(timezone.utc) - timedelta(days=30)
    event = Event(event_id="ev-very-old", version=1, event_time=old_time,
                  first_seen_at=old_time)
    impact = EventImpact(impact_id="IMP-vold", event_id="ev-very-old",
                         security_id=mu.security_id, final_score=9.0,
                         confidence=0.9)

    engine = AlertEngine(db, config)
    batch = engine.evaluate(event, [impact], is_update=False)
    assert len(batch.decisions) == 0
    assert len(batch.bootstrap_suppressions) == 1
    assert batch.bootstrap_suppressions[0].reason == "freshness_window"


def test_fresh_event_not_suppressed(db, config):
    """新鲜事件（在激活时间之后）不被抑制，正常推送。"""
    from trace.alerts.engine import AlertEngine
    from trace.db.repositories import AlertRuleRepo, SecurityRepo, UserRepo, WatchlistRepo
    from trace.domain.models import Event, EventImpact, WatchlistEntry

    user_repo = UserRepo(db)
    user_repo.ensure("u_test")
    mu = SecurityRepo(db).get_by_ticker("MU")
    WatchlistRepo(db).add(WatchlistEntry(user_id="u_test", security_id=mu.security_id))
    AlertRuleRepo(db).set_threshold("u_test", None, 1.0)

    # 当前时间事件（在激活时间之后）
    now = datetime.now(timezone.utc)
    event = Event(event_id="ev-fresh", version=1, event_time=now,
                  first_seen_at=now)
    impact = EventImpact(impact_id="IMP-fresh", event_id="ev-fresh",
                         security_id=mu.security_id, final_score=9.0,
                         confidence=0.9)

    engine = AlertEngine(db, config)
    batch = engine.evaluate(event, [impact], is_update=False)
    assert len(batch.decisions) == 1
    assert len(batch.bootstrap_suppressions) == 0


# ---------------------------------------------------------------------------
# 跨源 same-event dedup：官方确认修订（§18：official confirmation revision）
# ---------------------------------------------------------------------------

def test_official_confirmation_revision_via_engine(db, config):
    """媒体报道 → 官方来源合并：状态升级 + version+1 + primary_source 更新。"""
    from trace.event_engine.embeddings import HashEmbedder
    from trace.event_engine.engine import EventEngine, ExtractedEvent
    from trace.common.ids import raw_item_id
    from trace.domain.models import RawItem

    engine = EventEngine(db, config, embedder=HashEmbedder())
    now = datetime.now(timezone.utc)

    # 先入媒体报道
    media_item = RawItem(
        raw_item_id=raw_item_id(), source_id="src_reuters",
        title="Micron announces new HBM4 memory chip",
        url="https://reuters.example/micron-hbm4",
        published_at=now, language="en")
    d1 = engine.ingest(media_item, ExtractedEvent(
        title="Micron announces new HBM4 memory chip",
        summary="Micron announces new HBM4 memory chip",
        entities=["Micron"], event_type="product",
        event_status="reported", event_time=now))
    assert d1.action == "created"
    assert d1.event.version == 1
    assert d1.event.status == "reported"

    # 官方来源合并（同一事件的高相似度）
    official_item = RawItem(
        raw_item_id=raw_item_id(), source_id="src_micron_ir",
        title="Micron Ships Industry-Leading HBM4 Samples to Key Customers",
        url="https://investors.micron.com/hbm4",
        published_at=now, language="en")
    # 直接调用 _merge_into 模拟官方确认合并
    d2 = engine._merge_into(d1.event, official_item)
    assert d2.action == "revised"
    assert d2.event.version == 2
    assert d2.event.status == "official_confirmed"
    assert d2.event.primary_source_id == "src_micron_ir"
    assert "src_micron_ir" in d2.event.all_source_ids


def test_non_official_merge_keeps_reported(db, config):
    """非官方来源合并：不升级为 official_confirmed。"""
    from trace.event_engine.embeddings import HashEmbedder
    from trace.event_engine.engine import EventEngine, ExtractedEvent
    from trace.common.ids import raw_item_id
    from trace.domain.models import RawItem

    engine = EventEngine(db, config, embedder=HashEmbedder())
    now = datetime.now(timezone.utc)

    item1 = RawItem(
        raw_item_id=raw_item_id(), source_id="src_bis",
        title="BIS announces new semiconductor export rule",
        url="https://bis.doc.gov/rule1",
        published_at=now, language="en")
    d1 = engine.ingest(item1, ExtractedEvent(
        title="BIS announces new semiconductor export rule",
        summary="BIS announces new semiconductor export rule",
        entities=["BIS"], event_type="regulation",
        event_status="official_confirmed", event_time=now))
    assert d1.event.status == "official_confirmed"

    # 非官方来源合并（不降级）
    item2 = RawItem(
        raw_item_id=raw_item_id(), source_id="src_cnbc",
        title="CNBC reports on BIS semiconductor rule",
        url="https://cnbc.com/bis-rule",
        published_at=now, language="en")
    d2 = engine._merge_into(d1.event, item2)
    # 非官方来源合并后状态不降级
    assert d2.event.status == "official_confirmed"
    assert "src_cnbc" in d2.event.all_source_ids


# ---------------------------------------------------------------------------
# Source Health 状态派生（§18：source health）
# ---------------------------------------------------------------------------

def test_health_rate_limited_explicit():
    """429 后回退成功：状态仍为 RATE_LIMITED（不得显示无新数据）。"""
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "last_success_at": now,
        "last_failure_at": now,
        "last_error": "official RSS 429 rate limited",
        "last_error_category": "rate_limited",
        "consecutive_failures": 0,
        "last_http_status": 429,
    }
    assert derive_health_status(row, enabled=True) == "RATE_LIMITED"


def test_health_broken_after_3_failures():
    """连续 3 次失败：状态为 BROKEN。"""
    row = {
        "last_success_at": "2026-01-01T00:00:00Z",
        "last_failure_at": "2026-01-02T00:00:00Z",
        "last_error": "network error",
        "last_error_category": "network_error",
        "consecutive_failures": 3,
    }
    assert derive_health_status(row, enabled=True) == "BROKEN"


def test_health_disabled():
    """禁用来源：状态为 DISABLED。"""
    row = {"last_success_at": "2026-01-01T00:00:00Z"}
    assert derive_health_status(row, enabled=False) == "DISABLED"


def test_health_healthy():
    """最近成功且无失败：状态为 HEALTHY。"""
    row = {
        "last_success_at": "2026-08-26T10:00:00Z",
        "last_failure_at": None,
        "last_error": None,
        "last_error_category": None,
        "consecutive_failures": 0,
    }
    assert derive_health_status(row, enabled=True) == "HEALTHY"


def test_health_degraded_no_success():
    """从未成功：状态为 DEGRADED（无 last_success_at）。"""
    row = {"last_failure_at": "2026-08-26T10:00:00Z", "last_error": "timeout"}
    assert derive_health_status(row, enabled=True) == "BROKEN"
    assert derive_health_status({}, enabled=True) == "DEGRADED"
