"""第三轮探测：SanDisk RSS 重试 / Micron newsroom 结构 / Commerce 替代 / 商务部真实列表 / 工信部内容。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import trace.config  # noqa: F402,E402
import httpx  # noqa: E402

OUT = Path(__file__).resolve().parent

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
H = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}

CANDIDATES = [
    {"name": "sndk_rss_retry1", "url": "https://investor.sandisk.com/rss.xml", "timeout": 45},
    {"name": "sndk_rss_retry2", "url": "https://investor.sandisk.com/rss.xml", "timeout": 45},
    {"name": "micron_newsroom_news", "url": "https://www.micron.com/about/newsroom/news", "timeout": 25},
    {"name": "micron_newsroom_press", "url": "https://www.micron.com/about/newsroom/press-releases", "timeout": 25},
    {"name": "micron_newsroom_api", "url": "https://www.micron.com/api/newsroom/news?limit=10", "timeout": 25},
    {"name": "commerce_news_feed", "url": "https://www.commerce.gov/news/feed", "timeout": 25},
    {"name": "commerce_govdelivery", "url": "https://public.govdelivery.com/accounts/USDOC/feed.rss", "timeout": 25},
    {"name": "commerce_bis_govdelivery", "url": "https://public.govdelivery.com/accounts/USBIS/feed.rss", "timeout": 25},
    {"name": "mofcom_news_a", "url": "http://www.mofcom.gov.cn/article/xwfb/xwrcjd/", "timeout": 25},
    {"name": "mofcom_news_b", "url": "http://www.mofcom.gov.cn/xwfb/xwrcjd/", "timeout": 25},
    {"name": "mofcom_news_c", "url": "https://www.mofcom.gov.cn/article/a/", "timeout": 25},
    {"name": "miit_sjdt_content", "url": "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html", "timeout": 25},
]


def probe(client: httpx.Client, c: dict) -> dict:
    t0 = time.time()
    rec = {"name": c["name"], "url": c["url"], "status": "", "http_status": 0,
           "latency_ms": 0, "bytes": 0, "sample": ""}
    try:
        resp = client.get(c["url"], headers=H, timeout=c.get("timeout", 25))
        rec["http_status"] = resp.status_code
        rec["latency_ms"] = int((time.time() - t0) * 1000)
        rec["bytes"] = len(resp.content)
        txt = resp.text
        if c["name"].startswith("sndk_rss"):
            if "<rss" in txt[:500] or "<item" in txt:
                import feedparser
                p = feedparser.parse(resp.content)
                rec["sample"] = (f"entries={len(p.entries)} | "
                                 + " || ".join(e.get("title", "")[:70] for e in p.entries[:3]))
            else:
                rec["sample"] = txt[:200]
        elif c["name"] == "miit_sjdt_content":
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(txt, "lxml")
            links = [(a.get_text(strip=True), a.get("href")) for a in soup.select("a")
                     if len(a.get_text(strip=True)) > 10]
            rec["sample"] = " || ".join(f"{t[:40]}@{u}" for t, u in links[:4])
        else:
            rec["sample"] = txt[:250].replace("\n", " ")
        rec["status"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception as exc:
        rec["status"] = "error"
        rec["latency_ms"] = int((time.time() - t0) * 1000)
        rec["sample"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    print(f"[{rec['status']:>8}] {c['name']:22} {rec['http_status']} "
          f"{rec['latency_ms']}ms {rec['bytes']}B")
    print(f"           ↳ {rec['sample'][:220]}")
    return rec


def main() -> None:
    results = []
    with httpx.Client(follow_redirects=True) as client:
        for c in CANDIDATES:
            results.append(probe(client, c))
            time.sleep(2)
    (OUT / "probe-round3.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[saved] {OUT / 'probe-round3.json'}")


if __name__ == "__main__":
    main()
