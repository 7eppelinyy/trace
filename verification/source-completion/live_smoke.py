"""逐源真实 Smoke Test（任务书 §13）。

对每个关键来源做真实网络验证：直接请求官方入口 + 用生产采集器同款
解析函数提取条目。不写游标、不写数据库（避免与 run-once 冲突）。

输出：verification/source-completion/live-source-smoke.json
字段：source / url / status / http_status / items_found /
      latest_published_at / latency_ms / notes
不得包含任何 Token / API Key。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx

from trace.collectors.micron import parse_newsroom_html
from trace.collectors.policy import PolicyCollector
from trace.collectors.rss import RSSCollector
from trace.collectors.sandisk import parse_sitemap_press_releases

OUT = Path("verification/source-completion/live-source-smoke.json")

UA = "Mozilla/5.0 (compatible; TraceEventRadar/0.2; research)"
SEC_UA = "TraceEventRadar research@example.com"

results = []


def smoke(source: str, url: str, fetch_and_parse, notes: str,
          extra_headers: dict | None = None):
    t0 = time.time()
    entry = {"source": source, "url": url, "status": "ERROR",
             "http_status": None, "items_found": 0,
             "latest_published_at": None, "latency_ms": 0, "notes": notes}
    try:
        with httpx.Client(timeout=30, follow_redirects=True,
                          headers={"User-Agent": UA,
                                   **(extra_headers or {})}) as c:
            r = c.get(url)
        entry["http_status"] = r.status_code
        if r.status_code == 200:
            items = fetch_and_parse(r.text)
            entry["items_found"] = len(items)
            if items:
                latest = max((i for i in items if i is not None), default=None)
                entry["latest_published_at"] = (
                    latest.isoformat() if latest else None)
            entry["status"] = "PASS" if items else "FAIL_NO_ITEMS"
        else:
            entry["status"] = "FAIL_HTTP"
            entry["notes"] += f" (HTTP {r.status_code})"
    except Exception as exc:
        entry["status"] = "ERROR"
        entry["notes"] += f" ({type(exc).__name__}: {str(exc)[:150]})"
    entry["latency_ms"] = int((time.time() - t0) * 1000)
    results.append(entry)
    print(f"{source:22} {entry['status']:12} http={entry['http_status']} "
          f"items={entry['items_found']} {entry['latency_ms']}ms")
    return entry


# ---- G1: SEC EDGAR（合规 UA；8-K atom feed） ----
def _sec_parse(text: str):
    import re
    # Atom feed：条目日期在 <updated> 标签（也可能有 <published>）
    dates = re.findall(r"<updated>([^<]+)</updated>", text) or \
        re.findall(r"<published>([^<]+)</published>", text)
    out = []
    for d in dates:
        try:
            out.append(datetime.fromisoformat(d.replace("Z", "+00:00")))
        except ValueError:
            pass
    return out

smoke("src_sec_edgar",
      "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=NVDA&type=8-K&dateb=&owner=include&count=10&output=atom",
      _sec_parse, "SEC EDGAR full-text/8-K atom，合规 UA",
      extra_headers={"User-Agent": SEC_UA})

# ---- G2: NVIDIA IR 官方 RSS ----
def _rss_parse(text: str):
    import feedparser
    from time import mktime
    parsed = feedparser.parse(text)
    out = []
    for e in parsed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        out.append(datetime.fromtimestamp(mktime(t), tz=timezone.utc) if t else None)
    return out

smoke("src_nvidia_ir", "https://nvidianews.nvidia.com/rss.xml",
      _rss_parse, "NVIDIA 官方新闻室 RSS")

# ---- G3: SanDisk IR（官方主站 sitemap 路线，直连） ----
def _sandisk_parse(text: str):
    return [p for _, _, p in parse_sitemap_press_releases(text)]

smoke("src_sndk_ir", "https://www.sandisk.com/sitemap.xml",
      _sandisk_parse, "SanDisk 官方主站 sitemap（press-releases 53 条）；"
                      "IR RSS 大陆直连超时，官方内容不依赖第三方")

# ---- G4: Micron IR（官方新闻室页面回退路线） ----
def _micron_parse(text: str):
    return [p for _, _, p in parse_newsroom_html(text)]

smoke("src_micron_ir", "https://www.micron.com/about/newsroom/press-releases",
      _micron_parse, "Micron 官方新闻室页面（官方 RSS 429 限流的回退路线）")

# ---- G4b: Micron RSS 429 现状记录 ----
t0 = time.time()
micron_rss = {"source": "src_micron_ir (official RSS)", 
              "url": "https://investors.micron.com/rss.xml",
              "status": "", "http_status": None, "items_found": 0,
              "latest_published_at": None, "latency_ms": 0,
              "notes": "官方 RSS 限流现状记录（不作 Gate 依据）"}
try:
    with httpx.Client(timeout=20, follow_redirects=True,
                      headers={"User-Agent": UA}) as c:
        r = c.get(micron_rss["url"])
    micron_rss["http_status"] = r.status_code
    micron_rss["status"] = "RATE_LIMITED" if r.status_code == 429 else \
        ("PASS" if r.status_code == 200 else "FAIL_HTTP")
except Exception as exc:
    micron_rss["status"] = "ERROR"
    micron_rss["notes"] += f" ({type(exc).__name__})"
micron_rss["latency_ms"] = int((time.time() - t0) * 1000)
results.append(micron_rss)
print(f"src_micron_ir (rss)    {micron_rss['status']:12} "
      f"http={micron_rss['http_status']} {micron_rss['latency_ms']}ms")

# ---- G6: BIS / Federal Register ----
class _FakePolicy(PolicyCollector):
    def __init__(self):
        pass  # smoke 不需要 db/config，只用解析方法

pol = _FakePolicy()
# 生产同款页面配置（含 /press-release/ 链接过滤，过滤导航噪声）
from trace.collectors.policy import _POLICY_PAGES
_BIS_PAGE = next(p for p in _POLICY_PAGES if p["source_id"] == "src_bis")


def _bis_parse(text: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "lxml")
    entries = pol._parse_links(soup, _BIS_PAGE)
    return [None for _ in entries]

smoke("src_bis", "https://www.bis.doc.gov/index.php/policy-focus/press-releases",
      _bis_parse, "BIS press releases 官方页面")

smoke("src_federal_register",
      "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bterm%5D=semiconductor",
      _rss_parse, "Federal Register 官方 RSS（semiconductor 条件）")

# ---- G7: U.S. Commerce（GovDelivery 官方 RSS） ----
smoke("src_commerce", "https://public.govdelivery.com/accounts/USDOC/feed.rss",
      _rss_parse, "U.S. Commerce 官方站点 403（Cloudflare），"
                  "GovDelivery 官方订阅 RSS 为真实可用入口")

# ---- G8: 中国政策源（工信部 / 商务部） ----
from trace.collectors.filters import matches_keywords

def _miit_parse(text: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "lxml")
    entries = pol._parse_list_items(soup, {"link_pattern": "/art/",
                                           "base": "https://www.miit.gov.cn"})
    return [p for t, _, p in entries
            if matches_keywords(t, None, "cn_policy")]

def _mofcom_parse(text: str):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(text, "lxml")
    entries = pol._parse_list_items(soup, {"link_pattern": "art_",
                                           "base": "https://www.mofcom.gov.cn"})
    return [p for t, _, p in entries
            if matches_keywords(t, None, "cn_policy")]

smoke("src_miit", "https://www.miit.gov.cn/", _miit_parse,
      "工信部首页要闻；经 cn_policy Level 1 关键词初筛后的条目数")
smoke("src_mofcom", "https://www.mofcom.gov.cn/", _mofcom_parse,
      "商务部首页要闻；经 cn_policy Level 1 关键词初筛后的条目数")

# ---- G5: CNINFO ----
def _cninfo_probe():
    t0 = time.time()
    entry = {"source": "src_cninfo",
             "url": "http://www.cninfo.com.cn/new/information/topSearch/query",
             "status": "ERROR", "http_status": None, "items_found": 0,
             "latest_published_at": None, "latency_ms": 0,
             "notes": "巨潮 topSearch 接口（证监会指定法定披露平台）"}
    try:
        r = httpx.post(entry["url"],
                       data={"keyWord": "688981", "maxSecNum": 10, "maxListNum": 5},
                       headers={"User-Agent": "Mozilla/5.0",
                                "X-Requested-With": "XMLHttpRequest"},
                       timeout=20)
        entry["http_status"] = r.status_code
        if r.status_code == 200:
            data = r.json()
            entry["items_found"] = len(data) if isinstance(data, list) else 1
            entry["status"] = "PASS" if entry["items_found"] else "FAIL_NO_ITEMS"
        else:
            entry["status"] = "FAIL_HTTP"
    except Exception as exc:
        entry["notes"] += f" ({type(exc).__name__}: {str(exc)[:120]})"
    entry["latency_ms"] = int((time.time() - t0) * 1000)
    results.append(entry)
    print(f"src_cninfo             {entry['status']:12} "
          f"http={entry['http_status']} {entry['latency_ms']}ms")

_cninfo_probe()

OUT.parent.mkdir(parents=True, exist_ok=True)
summary = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "note": "逐源真实网络 smoke test；items_found 为解析函数实际提取条目数；"
            "中国政策源为关键词初筛后的候选数。不含任何 Token/Key。",
    "sources": results,
}
OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
passed = sum(1 for r in results if r["status"] == "PASS")
print(f"\n== smoke summary: {passed} PASS / {len(results)} entries ==")
