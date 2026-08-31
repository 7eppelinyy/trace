"""从生产环境探测产业/财经媒体的官方 RSS 可用性 + robots.txt 授权情况。

目的：确定哪些 license_mode=unknown 的媒体源提供**官方公开 feed**
（官方 feed = 明示授权程序化访问 → 可合法升级为 license_mode=public 并启用）。

只做只读探测，不写库、不产生事件。
"""

from __future__ import annotations

import json
import sys
import urllib.robotparser
from datetime import datetime, timezone

import feedparser
import httpx

UA = "TraceRadar/1.0 (+market event monitor; contact: see SEC_CONTACT_EMAIL)"

CANDIDATES = [
    # (source_id, 名称, 候选 feed URL 列表)
    ("src_digitimes", "DIGITIMES", [
        "https://www.digitimes.com/rss/",
        "https://www.digitimes.com/rss/daily.xml",
        "https://www.digitimes.com.tw/rss/",
    ]),
    ("src_eetimes", "EE Times", [
        "https://www.eetimes.com/feed/",
        "https://www.eetimes.com/rss",
    ]),
    ("src_eetimes_china", "EE Times China", [
        "https://www.eet-china.com/rss/news.xml",
        "https://www.eet-china.com/rss.xml",
        "https://www.eet-china.com/feed",
    ]),
    ("src_trendforce", "TrendForce", [
        "https://www.trendforce.com/rss",
        "https://www.trendforce.com/news/rss",
        "https://www.trendforce.com/presscenter/rss",
        "https://press.trendforce.com/rss",
    ]),
    ("src_jw_insights", "集微网", [
        "https://www.laoyaoba.com/rss",
        "https://www.laoyaoba.com/feed",
        "https://www.jiweinet.com/rss",
        "https://www.jiweinet.com/feed",
    ]),
    ("src_chipwise", "芯智讯", [
        "https://www.icsmart.cn/feed/",
        "https://www.icsmart.cn/rss",
        "https://www.icsmart.cn/feed",
    ]),
    ("src_cnbc", "CNBC (Technology 官方 RSS)", [
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    ]),
    ("src_cls", "财联社", [
        "https://www.cls.cn/rss",
        "https://www.cls.cn/feed",
    ]),
    ("src_eastmoney", "东方财富", [
        "https://feed.eastmoney.com/rss",
        "https://finance.eastmoney.com/rss",
    ]),
]

MEMORY_KEYWORDS = ["nand", "dram", "hbm", "ssd", "memory", "存储", "内存",
                   "合约价", "现货", "产能", "ai server", "服务器", "半导体", "chip"]


def check_robots(feed_url: str) -> dict:
    from urllib.parse import urlsplit
    parts = urlsplit(feed_url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    out = {"robots_url": robots_url}
    try:
        r = httpx.get(robots_url, timeout=15, headers={"User-Agent": UA},
                      follow_redirects=True)
        out["robots_status"] = r.status_code
        if r.status_code == 200:
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(r.text.splitlines())
            out["allowed_for_ua"] = rp.can_fetch(UA, feed_url)
            out["allowed_for_star"] = rp.can_fetch("*", feed_url)
            body = r.text.lower()
            out["has_crawl_delay"] = "crawl-delay" in body
    except Exception as exc:  # noqa: BLE001
        out["robots_error"] = f"{type(exc).__name__}: {exc}"
    return out


def probe_feed(url: str) -> dict:
    res = {"url": url}
    try:
        r = httpx.get(url, timeout=25, headers={"User-Agent": UA},
                      follow_redirects=True)
        res["status"] = r.status_code
        res["final_url"] = str(r.url)
        res["content_type"] = r.headers.get("content-type", "")
        res["bytes"] = len(r.content)
        if r.status_code == 200:
            fp = feedparser.parse(r.content)
            res["bozo"] = bool(fp.bozo)
            res["entries"] = len(fp.entries)
            res["feed_title"] = (fp.feed.get("title") or "")[:80]
            has_dates = sum(1 for e in fp.entries if e.get("published_parsed"))
            res["entries_with_date"] = has_dates
            titles = [(e.get("title") or "")[:110] for e in fp.entries[:5]]
            res["sample_titles"] = titles
            hits = [t for e in fp.entries
                    for t in [(e.get("title") or "")]
                    if any(k in t.lower() for k in MEMORY_KEYWORDS)]
            res["keyword_hits"] = len(hits)
            res["keyword_sample"] = hits[:3]
    except Exception as exc:  # noqa: BLE001
        res["error"] = f"{type(exc).__name__}: {exc}"
    return res


def main() -> int:
    report = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "user_agent": UA,
        "results": [],
    }
    for source_id, name, urls in CANDIDATES:
        entry = {"source_id": source_id, "name": name, "feeds": []}
        verdict = "NO_OFFICIAL_FEED"
        for u in urls:
            fr = probe_feed(u)
            if fr.get("status") == 200 and fr.get("entries", 0) > 0:
                fr["robots"] = check_robots(u)
                verdict = "OFFICIAL_FEED_OK"
            entry["feeds"].append(fr)
            if verdict == "OFFICIAL_FEED_OK":
                break
        entry["verdict"] = verdict
        report["results"].append(entry)
        print(f"[{verdict:18s}] {source_id:20s} {name}", flush=True)
        for fr in entry["feeds"]:
            line = (f"    {fr.get('status', fr.get('error', '?'))!s:>28s}  "
                    f"entries={fr.get('entries', '-')}  "
                    f"kw={fr.get('keyword_hits', '-')}  {fr['url']}")
            print(line, flush=True)
            if fr.get("sample_titles"):
                for t in fr["sample_titles"][:3]:
                    print(f"        · {t}", flush=True)
            if fr.get("robots"):
                rb = fr["robots"]
                print(f"        robots: status={rb.get('robots_status')} "
                      f"allow_ua={rb.get('allowed_for_ua')} "
                      f"allow_*={rb.get('allowed_for_star')}", flush=True)
    out_path = sys.argv[1] if len(sys.argv) > 1 else "media-feed-probe.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\nwritten: {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
