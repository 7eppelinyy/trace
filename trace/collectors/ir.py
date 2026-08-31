"""IR Collector：公司官方投资者关系入口（RSS/Newsroom，真实）。

任务书 §4.2：至少接通以下公司的一个真实官方入口（官方 RSS / newsroom /
press release / earnings release / 官方公开 JSON 接口），
不得使用未经授权的第三方内容冒充公司官方公告。

当前状态（2026-08-26 真实探测）：
    - NVIDIA:   https://nvidianews.nvidia.com/rss.xml  ✅ 官方新闻室 RSS
    - SanDisk:  IR RSS 依赖代理（大陆直连超时）→ 由 SanDiskCollector 的
                官方主站 sitemap 路线覆盖（见 trace/collectors/sandisk.py，
                直连 200，53 条官方 press-releases）
    - Micron:   官方 RSS 429 限流 → 由 MicronCollector 的
                新闻室页面回退路线覆盖（见 trace/collectors/micron.py）

每条采集结果保留：
    source_name / source_item_id / title / published_at / fetched_at /
    canonical_url / content_excerpt(reference) / raw_hash(title_hash+content_hash)
"""

from __future__ import annotations

import logging

from trace.collectors.rss import RSSCollector

logger = logging.getLogger(__name__)


class IRCollector(RSSCollector):
    """公司官方 IR 采集器：复用 RSS 管道，来源仅限官方入口。

    Micron 由 MicronCollector 专门处理（429 回退），
    SanDisk 由 SanDiskCollector 专门处理（sitemap 路线），
    不在本集合内，避免同一 source_id 被两个采集器重复覆盖。
    """

    collector_type = "ir"

    @property
    def handled_source_ids(self) -> set[str]:
        return {"src_nvidia_ir"}
