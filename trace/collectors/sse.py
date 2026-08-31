"""上海证券交易所公告 Collector（A股 P0，路线B）。

任务书 §5.2 路线B：
    巨潮资讯（cninfo）是证监会指定的沪深两市统一法定信息披露平台。
    本采集器不直接抓取上交所网站（其公开查询接口不稳定且无公告全文
    保障），而是定义为"巨潮公告的沪市过滤适配器"：

    数据权威链：上市公司 → 上交所法定披露义务 → 巨潮（统一披露平台）
              → SSECollector（column=sse 过滤）→ RawItem

    与 SZSECollector 一起，保证不存在三个名字不同、实际重复抓取
    同一数据且无法去重的 Collector。默认 seed 中 src_sse 关闭，
    由 src_cninfo 统一入口覆盖沪深两市；需要按市场独立健康跟踪时
    可在 source_registry 中显式启用（跨来源去重由 canonical_url /
    title_hash 保证，不会产生重复 RawItem）。
"""

from __future__ import annotations

from trace.collectors.cninfo import CNINFOCollector


class SSECollector(CNINFOCollector):
    """巨潮公告的沪市（SSE）市场过滤适配器。"""

    collector_type = "exchange_cn"
    market_filter = frozenset({"SSE"})

    @property
    def cursor_key(self) -> str:
        return "src_sse"

    @property
    def handled_source_ids(self) -> set[str]:
        return {"src_sse"}
