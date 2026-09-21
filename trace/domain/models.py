"""核心领域模型。

系统中心对象是 Event（而不是 News）。
所有对象都是纯数据 dataclass，不依赖具体数据库实现，
保证后续从 SQLite 迁移 PostgreSQL 时不需要重写 Domain Model。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class Market(str, Enum):
    US = "US"
    CN = "CN"


class SecurityStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    DELISTED = "delisted"


class EventStatus(str, Enum):
    RUMOR = "rumor"
    REPORTED = "reported"
    PARTIALLY_CONFIRMED = "partially_confirmed"
    OFFICIAL_CONFIRMED = "official_confirmed"
    CONTRADICTED = "contradicted"  # 存在矛盾 / 官方否认
    RETRACTED = "retracted"        # 已撤回
    SUPERSEDED = "superseded"


class EventType(str, Enum):
    """事件类型（第一版集合，可扩展）。"""
    REGULATION = "regulation"            # 监管/出口管制/政策
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    PRODUCT = "product"                  # 产品发布/延期/取消
    SUPPLY_CHAIN = "supply_chain"        # 供应链变动
    PRICING = "pricing"                  # 合约价/现货价变化
    CAPACITY = "capacity"                # 产能/利用率
    MA_CUSTOMER = "major_customer"       # 大客户变动
    MA = "ma"                            # 并购
    MANAGEMENT = "management"
    LAWSUIT = "lawsuit"
    MACRO = "macro"                      # 宏观/利率/汇率
    GEOPOLITICAL = "geopolitical"
    OTHER = "other"


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class Directness(str, Enum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    CONDITIONAL = "conditional"


class EdgeType(str, Enum):
    SUPPLIER_OF = "supplier_of"
    CUSTOMER_OF = "customer_of"
    COMPETES_WITH = "competes_with"
    SUBSTITUTES_FOR = "substitutes_for"
    PRODUCES = "produces"
    CONSUMES = "consumes"
    DEPENDS_ON = "depends_on"
    BENEFITS_FROM = "benefits_from"
    EXPOSED_TO = "exposed_to"
    REGULATED_BY = "regulated_by"


class LicenseMode(str, Enum):
    PUBLIC = "public"
    INTERNAL_ONLY = "internal_only"
    LICENSED_AI_ANALYSIS = "licensed_ai_analysis"
    LICENSED_DISPLAY = "licensed_display"
    LICENSED_REDISTRIBUTION = "licensed_redistribution"
    UNKNOWN = "unknown"


class AlertType(str, Enum):
    NEW_EVENT = "new_event"
    EVENT_UPDATE = "event_update"


class AuthorityLevel(str, Enum):
    """来源权威等级（Source Completion Gate §7）。"""
    OFFICIAL_COMPANY = "official_company"          # 公司官方公告/新闻稿/财报
    OFFICIAL_GOVERNMENT = "official_government"    # 政府正式规则/公告
    OFFICIAL_REGULATOR = "official_regulator"      # 监管正式文件（SEC/证监会）
    INDUSTRY_MEDIA = "industry_media"
    FINANCIAL_MEDIA = "financial_media"


# 官方权威等级集合：合并进既有事件时触发官方确认修订（官方确认链）
OFFICIAL_AUTHORITIES = {
    AuthorityLevel.OFFICIAL_COMPANY.value,
    AuthorityLevel.OFFICIAL_GOVERNMENT.value,
    AuthorityLevel.OFFICIAL_REGULATOR.value,
}


# 来源健康状态（派生，不存库；见 trace.db.health.derive_status）
HEALTH_HEALTHY = "HEALTHY"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_RATE_LIMITED = "RATE_LIMITED"
HEALTH_BROKEN = "BROKEN"
HEALTH_DISABLED = "DISABLED"


# ---------------------------------------------------------------------------
# 数据源与原始条目
# ---------------------------------------------------------------------------

@dataclass
class Source:
    """source_registry：任何来源必须先登记。"""
    source_id: str
    source_name: str
    source_type: str                       # official / ir / industry_media / financial_media / rss / market_data
    priority: int = 5                      # 1(最高)-10
    base_reliability: float = 5.0          # 1-10，进入评分公式
    license_mode: LicenseMode = LicenseMode.UNKNOWN
    retention_policy: str = "default"
    enabled: bool = False                  # 未授权默认关闭
    # Source Completion Gate 扩展（集中配置，禁止散落硬编码）
    authority_level: str = ""              # AuthorityLevel.value
    poll_interval_seconds: int = 600       # 建议轮询间隔
    security_map: list[str] = field(default_factory=list)
    # 公司官方源直接关联的 security_id（官方公告不依赖 LLM 猜股票代码）
    # Governance & Licensing (F29, F30)
    can_fetch: bool = True
    can_store: bool = True
    can_display: bool = True
    can_forward: bool = True
    verified_at: str | None = None
    verified_by: str | None = None
    operational_override: bool | None = None
    override_reason: str = ""
    override_updated_at: str | None = None
    override_updated_by: str | None = None
    seed_enabled: bool = False

    @property
    def is_effective_enabled(self) -> bool:
        if self.operational_override is not None:
            return bool(self.operational_override)
        return bool(self.enabled)


@dataclass
class SourceAuthorizationRecord:
    """正式授权证据登记格式（Source Governance & Legal Clearance）。

    区别于系统的技术配置权限（can_fetch/store/display/forward），
    本记录用于登记经过法律/合规核验的第三方数据真实授权或许可协议。
    """
    auth_id: str
    source_id: str
    scope: list[str]                  # e.g. ["fetch", "store", "display", "forward"]
    evidence_url_or_file: str         # 证据链接、许可协议编号或文件路径
    verified_by: str                  # 真实核验人姓名或工号（禁止虚构合规团队）
    verified_at: str                  # ISO8601 时间戳
    expires_at: str | None = None     # 授权期限（若永久为 None）
    status: str = "pending"           # pending | verified | expired | rejected
    notes: str = ""
    created_at: str = ""


@dataclass
class RawItem:
    raw_item_id: str
    source_id: str
    source_item_id: str | None = None      # 来源侧唯一 ID（如 accession number）
    title: str = ""
    url: str = ""
    published_at: datetime | None = None
    fetched_at: datetime | None = None
    language: str = ""                     # en / zh / ...
    content: str | None = None
    reference: str | None = None           # 无正文时的引用信息
    content_hash: str = ""
    title_hash: str = ""
    canonical_url: str = ""
    # 处理状态
    event_id: str | None = None


# ---------------------------------------------------------------------------
# Event 中心对象
# ---------------------------------------------------------------------------

@dataclass
class Event:
    event_id: str
    title: str = ""
    summary: str = ""
    event_type: str = EventType.OTHER.value
    status: str = EventStatus.REPORTED.value
    version: int = 1
    first_seen_at: datetime | None = None
    last_updated_at: datetime | None = None
    event_time: datetime | None = None     # 事件实际发生时间
    language: str = ""
    first_source_id: str | None = None     # 最早发现事件的来源
    primary_source_id: str | None = None   # 当前最权威证据
    all_source_ids: list[str] = field(default_factory=list)
    material_update: bool = False
    needs_human_review: bool = False
    # Stage A 抽取的关键数字（金额/产能/份额…）。修订时用于判定
    # key_number_changed —— 关键数字变了属于实质更新，允许再次推送。
    key_numbers: list[str] = field(default_factory=list)
    # 聚类辅助字段
    title_embedding: bytes | None = None
    summary_embedding: bytes | None = None
    embedding_model: str | None = None


@dataclass
class EventSource:
    """Event 的 Evidence（每个来源一条）。

    角色（任务书 §8）：
        first       最早发现事件的来源（first_source）
        primary     当前最权威证据（通常是官方来源，primary_source）
        confirming  后续来源对既有事件的确认（confirming_source）
        supporting  一般佐证
    """
    event_id: str
    raw_item_id: str
    role: str = "supporting"


@dataclass
class EventRevision:
    revision_id: str
    event_id: str
    version: int
    revision_type: str = ""                # rumor_to_confirmed / official_source_appeared / direction_changed / key_number_changed / score_changed / threshold_crossed
    material_update: bool = False
    note: str = ""
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# 证券与产业图谱
# ---------------------------------------------------------------------------

@dataclass
class Security:
    """Security Master：统一管理美股 + A股。"""
    security_id: str
    market: str                            # US / CN
    exchange: str = ""                     # NASDAQ / NYSE / SSE / SZSE / BSE / ""
    ticker: str = ""
    company_name_zh: str = ""
    company_name_en: str = ""
    cik: str | None = None
    aliases: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    industry_tags: list[str] = field(default_factory=list)
    graph_node_ids: list[str] = field(default_factory=list)
    is_watchlist_default: bool = False     # 初始核心 Watchlist
    is_context_universe: bool = False      # Context Universe（不一定触发通知）
    status: str = SecurityStatus.VERIFIED.value  # verified / unverified / delisted


@dataclass
class EntityAlias:
    """非上市实体（Samsung、SK hynix、YMTC、CXMT、TSMC 等）与别名。"""
    entity_id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    entity_type: str = "company"           # company / agency / concept
    security_id: str | None = None         # 可关联到 Security（如 TSMC -> TSM）


@dataclass
class IndustryEdge:
    edge_id: str
    from_node: str                         # entity_id / security_id / 概念节点 id
    to_node: str
    edge_type: str                         # EdgeType.value
    confidence: float = 0.8
    evidence_source: str = ""
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    direction_rule: str = ""               # 如 "positive" / "negative" / "contextual"


# ---------------------------------------------------------------------------
# 影响分析与评分
# ---------------------------------------------------------------------------

@dataclass
class EventImpact:
    """Stage B Impact Analyzer 的结构化输出（每个受影响证券一条）。"""
    impact_id: str
    event_id: str
    security_id: str
    direction: str = Direction.UNCERTAIN.value      # bullish/bearish/neutral/mixed/uncertain
    directness: str = Directness.INDIRECT.value     # direct/indirect/conditional
    magnitude: float = 5.0                          # 1-10
    persistence: float = 5.0                        # 1-10
    directness_score: float = 5.0                   # 1-10（directness 的数值化）
    confidence: float = 0.5                         # 0-1，与 importance 分离
    reason: str = ""
    industry_path: str = ""                         # 产业传导路径描述
    evidence_ids: list[str] = field(default_factory=list)
    # 评分结果（集中评分引擎写入）
    source_reliability: float = 5.0
    base_score: float = 5.0
    market_confirmation: float = 5.0                # 1-10，无行情时默认 5（不加减分）
    final_score: float = 5.0
    # 生产门禁降级标记（不得把降级包装成完整真实分析）
    analysis_mode: str = "llm"                      # llm / rule_based_degraded
    # real / mock / none / unavailable / no_quote /
    # market_not_opened_since_event / reaction_window_expired
    # （后三者来自行情时段门禁，见 collectors/market_data/confirmation.py；
    #  每个取值都必须在 alerts/template.py 有对应说明文案）
    market_data_mode: str = "real"
    next_eligible_at: datetime | None = None        # 预计可补算的最早时刻（下一次开盘）
    expires_at: datetime | None = None              # 补算反应窗口截止时刻
    created_at: datetime | None = None


@dataclass
class MarketSnapshot:
    security_id: str
    ts: datetime
    last_price: float | None = None
    prev_close: float | None = None
    change_pct_day: float | None = None             # 当日涨跌幅（与 15m 严格分离，F15）
    change_pct_1m: float | None = None
    change_pct_5m: float | None = None
    change_pct_15m: float | None = None             # 严格 15 分钟区间变动
    volume: int | None = None
    volume_ratio: float | None = None
    session: str = ""                               # regular / pre / post
    currency: str = "USD"
    source: str = ""                                # alpaca / tencent / mock / unavailable
    is_delayed: bool = False                        # 是否延时行情
    market_ts: datetime | None = None               # 交易所成交时间戳


# ---------------------------------------------------------------------------
# 用户与提醒
# ---------------------------------------------------------------------------

@dataclass
class User:
    user_id: str                             # telegram chat id / internal user id
    timezone: str = "Asia/Taipei"
    muted_until: datetime | None = None
    created_at: datetime | None = None
    # 首次同步保护：即时推送只覆盖该时间点之后发布/更新的事件。
    # 历史事件保留在 Event DB（/event、/digest 可查），但不作为即时 Alert 推送。
    alert_activation_at: datetime | None = None


@dataclass
class UserSession:
    session_token: str
    user_id: str
    created_at: datetime
    expires_at: datetime
    is_revoked: bool = False
    family_id: str | None = None


@dataclass
class UserRefreshToken:
    token_hash: str
    user_id: str
    family_id: str
    expires_at: datetime
    created_at: datetime
    session_token: str | None = None
    used_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass
class WatchlistEntry:
    user_id: str
    security_id: str
    added_at: datetime | None = None
    user_alias: str = ""


@dataclass
class AlertRule:
    user_id: str
    security_id: str | None = None           # None 表示 all
    threshold: float = 7.0
    updated_at: datetime | None = None


@dataclass
class AlertDelivery:
    """幂等投递记录。idempotency key = (user_id, event_id, security_id, event_version, alert_type)。"""
    delivery_id: str
    user_id: str
    event_id: str
    security_id: str
    event_version: int
    alert_type: str
    final_score: float = 0.0
    sent_at: datetime | None = None
    status: str = "sent"                     # sent / failed / suppressed
    # Telegram 投递回执（不得仅以 HTTP 200 判断，必须解析返回体）
    telegram_chat_id: str | None = None
    telegram_message_id: str | None = None
    response_status: str | None = None       # ok / api_error / network_error
    run_id: str | None = None
    # 首次同步保护：因事件早于用户 alert_activation_at 而被抑制（显式标记）
    bootstrap_suppressed: bool = False


@dataclass
class DailyDigest:
    digest_id: str
    date_str: str                            # YYYY-MM-DD
    content_markdown: str = ""
    sent_at: datetime | None = None
    cache_key: str | None = None


@dataclass
class NotificationPreference:
    preference_id: str
    user_id: str
    security_id: str | None = None           # None = 全局默认偏好；非 None = 单标的覆盖
    threshold: float = 7.0
    enabled: bool = True
    quiet_start: str | None = None           # HH:MM 本地时区
    quiet_end: str | None = None             # HH:MM 本地时区
    channel: str = "all"                     # all / telegram / wechat
    revision: int = 1
    updated_at: datetime | None = None


@dataclass
class ForecastSnapshot:
    """不可变预测快照 (T13/F19/F27)。

    在 Stage B 生成预测时定格快照，以预测生成时刻 (analysis_created_at) 作为唯一基准锚点，
    消除前视偏差。修订事件版本生成新快照，禁止覆盖历史快照。
    """
    snapshot_id: str
    impact_id: str
    event_id: str
    security_id: str
    event_version: int = 1
    predicted_direction: str = "uncertain"   # bullish / bearish / uncertain
    predicted_score: float = 0.0
    confidence: float = 0.0
    model_version: str = "v1"
    market: str = "US"                       # US / CN
    analysis_created_at: datetime | None = None
    published_at: datetime | None = None
    anchor_price: float | None = None
    anchor_ts: datetime | None = None
    horizon_hours: float = 24.0
    due_at: datetime | None = None
    benchmark_code: str = "SPX"              # SPX (US) / STAR50 (CN)
    benchmark_anchor_price: float | None = None
    status: str = "pending"                  # pending / evaluated / unmeasurable / expired
    created_at: datetime | None = None


@dataclass
class ForecastCheck:
    """预测回测账本单条记录：影响预测 vs 事后真实行情。

    方向核对基准（确定性规则，不走 LLM）：
        actual_direction 由实际涨跌幅度与 move_threshold_pct 决定；
        outcome = hit（方向一致）/ miss（方向相反）/ neutral（实际无方向）。
    """
    check_id: str
    impact_id: str
    event_id: str
    security_id: str
    predicted_direction: str                 # bullish / bearish
    predicted_score: float = 0.0
    confidence: float = 0.0
    actual_change_pct: float | None = None
    actual_direction: str = ""               # bullish / bearish / neutral
    outcome: str = ""                        # hit / miss / neutral / unmeasurable
    horizon_hours: float = 24.0
    evaluated_at: datetime | None = None
    # 事件锚定区间收益的可复算依据（0009）
    anchor_price: float | None = None        # 预测生成时刻附近的快照价
    anchor_ts: datetime | None = None
    exit_price: float | None = None          # 核对时刻价格
    elapsed_hours: float | None = None       # 预测生成到核对的真实间隔
    note: str = ""                           # unmeasurable 的原因
    snapshot_id: str | None = None           # 关联不可变快照 (0020)
    event_version: int = 1
    model_version: str = "v1"
    market: str = "US"
    benchmark_code: str = "SPX"
    benchmark_change_pct: float | None = None
    excess_return_pct: float | None = None
    excluded_reason: str = ""



@dataclass
class ProcessingJob:
    """持久化处理作业 (T03/F03)。"""
    job_id: str
    job_type: str                            # stage_a_extract / stage_b_analyze
    target_id: str                           # raw_item_id / event_id
    status: str                              # pending / processing / completed / failed / blocked_budget / human_review / succeeded_empty
    processor_version: str = "v1"
    input_version: int = 1
    lease_until: datetime | None = None
    retry_count: int = 0
    max_retries: int = 3
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    lease_owner: str | None = None
    input_json: dict = field(default_factory=dict)


@dataclass
class ChannelBinding:
    """用户通知渠道绑定 (F06/T06)。"""
    binding_id: str
    user_id: str
    channel_type: str                         # telegram / wechat / webhook
    channel_target: str                       # chat_id / openid / url
    is_active: bool = True
    verified_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class AlertOutbox:
    """待投递提醒任务 (F06/T06)。"""
    outbox_id: str
    user_id: str
    channel_type: str
    channel_target: str
    event_id: str
    impact_id: str | None
    event_version: int
    alert_type: str
    idempotency_key: str
    content_text: str
    content_url: str | None = None
    status: str = "pending"                   # pending / sending / sent / failed / suppressed
    retry_count: int = 0
    max_retries: int = 3
    retry_after: datetime | None = None
    lease_until: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    lease_owner: str | None = None
    security_id: str | None = None
    final_score: float | None = None
    analysis_id: str | None = None
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# 假设跟踪与用户反馈 (T16)
# ---------------------------------------------------------------------------

class ResearchQuestionState(str, Enum):
    TRACKING = "tracking"
    CONFIRMED = "confirmed"
    FALSIFIED = "falsified"
    ARCHIVED = "archived"


@dataclass
class ResearchQuestion:
    """用户假设跟踪模型 (F31/T16)。"""
    question_id: str
    user_id: str
    title: str
    hypothesis: str
    event_id: str | None = None
    security_id: str | None = None
    supporting_conditions: list[str] = field(default_factory=list)
    contradicting_conditions: list[str] = field(default_factory=list)
    next_check_at: datetime | None = None
    state: str = ResearchQuestionState.TRACKING.value
    user_notes: str = ""
    matched_evidence_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    revision: int = 1


@dataclass
class AlertFeedback:
    """提醒有效性与原因反馈模型 (F32/T16)。"""
    feedback_id: str
    user_id: str
    event_id: str
    rating: str                                # useful / not_useful / irrelevant / too_late / incorrect_analysis / duplicate
    security_id: str | None = None
    reason: str = ""
    created_at: datetime | None = None


