"""第二轮探测：SanDisk RSS 结构验证 + Micron/Commerce/商务部替代入口。"""

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
FULL_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CANDIDATES = [
    # ---- SanDisk RSS 内容验证 ----
    {"name": "sndk_rss_check", "url": "https://investor.sandisk.com/rss.xml", "headers": FULL_HEADERS},
    # ---- Micron 替代入口 ----
    {"name": "micron_full_headers", "url": "https://investors.micron.com/news-releases", "headers": FULL_HEADERS},
    {"name": "micron_main_newsroom", "url": "https://www.micron.com/about/newsroom", "headers": FULL_HEADERS},
    {"name": "micron_main_rss", "url": "https://www.micron.com/about/newsroom/rss", "headers": FULL_HEADERS},
    {"name": "micron_ir_rss_full", "url": "https://investors.micron.com/rss.xml", "headers": FULL_HEADERS},
    # ---- Commerce 替代 ----
    {"name": "commerce_press_full", "url": "https://www.commerce.gov/news/press-releases", "headers": FULL_HEADERS},
    {"name": "commerce_sitemap", "url": "https://www.commerce.gov/sitemap.xml", "headers": FULL_HEADERS},
    {"name": "commerce_search_json", "url": "https://www.commerce.gov/api/v1/search?keys=semiconductor&type=news", "headers": FULL_HEADERS},
    # ---- 商务部替代 ----
    {"name": "mofcom_home", "url": "https://www.mofcom.gov.cn/", "headers": FULL_HEADERS},
    {"name": "mofcom_xwrcjd", "url": "http://www.mofcom.gov.cn/article/xwfb/xwrcjd/", "headers": FULL_HEADERS},
    {"name": "mofcom_xwsj", "url": "http://www.mofcom.gov.cn/article/xwfb/xwsj/", "headers": FULL_HEADERS},
    {"name": "mofcom_xxgb", "url": "http://www.mofcom.gov.cn/article/xwfb/xxgzbd/", "headers": FULL_HEADERS},
]


def probe(client: httpx.Client, c: dict) -> dict:
    t0 = time.time()
    rec = {"name": c["name"], "url": c["url"], "status": "", "http_status": 0,
           "latency_ms": 0, "bytes": 0, "sample": ""}
    try:
        resp = client.get(c["url"], headers=c["headers"], timeout=25)
        rec["http_status"] = resp.status_code
        rec["latency_ms"] = int((time.time() - t0) * 1000)
        rec["bytes"] = len(resp.content)
        txt = resp.text
        if c["name"] == "sndk_rss_check":
            # 打印前 3 个 item 标题
            import feedparser
            p = feedparser.parse(resp.content)
            titles = [e.get("title", "")[:80] for e in p.entries[:3]]
            rec["sample"] = f"entries={len(p.entries)} | " + " || ".join(titles)
        else:
            rec["sample"] = txt[:200].replace("\n", " ")
        rec["status"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception as exc:
        rec["status"] = "error"
        rec["latency_ms"] = int((time.time() - t0) * 1000)
        rec["sample"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    print(f"[{rec['status']:>8}] {c['name']:22} {rec['http_status']} "
          f"{rec['latency_ms']}ms {rec['bytes']}B")
    if rec["sample"]:
        print(f"           ↳ {rec['sample'][:180]}")
    return rec


def main() -> None:
    results = []
    with httpx.Client(follow_redirects=True) as client:
        for c in CANDIDATES:
            results.append(probe(client, c))
            time.sleep(1.5)
    (OUT / "probe-round2.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[saved] {OUT / 'probe-round2.json'}")


if __name__ == "__main__":
    main()
