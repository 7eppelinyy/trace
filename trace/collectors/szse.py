"""深圳证券交易所公告 Collector（A股 P0，路线B）。

与 SSECollector 相同的路线B定义：
    数据权威链：上市公司 → 深交所法定披露义务 → 巨潮（统一披露平台）
              → SZSECollector（column=szse 过滤）→ RawItem

默认 seed 中 src_szse 关闭，由 src_cninfo 统一入口覆盖；
需要按市场独立健康跟踪时可在 source_registry 中显式启用，
跨来源去重由 canonical_url / title_hash 保证。
"""

from __future__ import annotations

from trace.collectors.cninfo import CNINFOCollector


class SZSECollector(CNINFOCollector):
    """巨潮公告的深市（SZSE）市场过滤适配器。"""

    collector_type = "exchange_cn"
    market_filter = frozenset({"SZSE"})

    @property
    def cursor_key(self) -> str:
        return "src_szse"

    @property
    def handled_source_ids(self) -> set[str]:
        return {"src_szse"}
