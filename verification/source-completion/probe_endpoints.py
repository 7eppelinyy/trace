"""Source Completion Gate：新源端点真实探测（第一轮）。

只读探测候选入口的可达性/结构，不做任何写库操作。
结果写入 verification/source-completion/probe-round1.json。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import trace.config  # noqa: F402,E402  触发 .env 加载（代理等）
import httpx  # noqa: E402

OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
POLITE_UA = "TraceEventRadar/0.2 (research use; contact research@example.com)"

CANDIDATES = [
    # ---- SanDisk IR（Q4 平台）----
    {"name": "sandisk_ir_news_page", "url": "https://investor.sandisk.com/news-events/news-releases", "ua": BROWSER_UA},
    {"name": "sandisk_ir_rss_feeds", "url": "https://investor.sandisk.com/feeds/news-releases.rss", "ua": BROWSER_UA},
    {"name": "sandisk_ir_rss_xml", "url": "https://investor.sandisk.com/rss.xml", "ua": BROWSER_UA},
    {"name": "sandisk_ir_events", "url": "https://investor.sandisk.com/news-events/events-and-presentations", "ua": BROWSER_UA},
    # ---- Micron IR ----
    {"name": "micron_ir_news_page", "url": "https://investors.micron.com/news-releases", "ua": BROWSER_UA},
    {"name": "micron_ir_rss", "url": "https://investors.micron.com/rss.xml", "ua": BROWSER_UA},
    {"name": "micron_ir_press", "url": "https://investors.micron.com/news-releases?field_news_type=All", "ua": BROWSER_UA},
    # ---- U.S. Commerce ----
    {"name": "commerce_news", "url": "https://www.commerce.gov/news", "ua": BROWSER_UA},
    {"name": "commerce_press_rss", "url": "https://www.commerce.gov/news/press-releases/rss", "ua": BROWSER_UA},
    {"name": "commerce_news_rss", "url": "https://www.commerce.gov/news/rss", "ua": BROWSER_UA},
    # ---- 工信部 ----
    {"name": "miit_sjdt", "url": "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html", "ua": BROWSER_UA},
    {"name": "miit_ldhd", "url": "https://www.miit.gov.cn/xwdt/gxdt/ldhd/index.html", "ua": BROWSER_UA},
    {"name": "miit_zcwj", "url": "https://www.miit.gov.cn/zwgk/zcwj/wjfb/index.html", "ua": BROWSER_UA},
    # ---- 商务部 ----
    {"name": "mofcom_xwfb", "url": "http://www.mofcom.gov.cn/article/xwfb/", "ua": BROWSER_UA},
    {"name": "mofcom_xwfb_https", "url": "https://www.mofcom.gov.cn/article/xwfb/", "ua": BROWSER_UA},
]


def probe(client: httpx.Client, c: dict) -> dict:
    t0 = time.time()
    rec = {"name": c["name"], "url": c["url"], "status": "", "http_status": 0,
           "latency_ms": 0, "bytes": 0, "notes": ""}
    try:
        resp = client.get(c["url"], headers={"User-Agent": c["ua"]}, timeout=20)
        rec["http_status"] = resp.status_code
        rec["latency_ms"] = int((time.time() - t0) * 1000)
        rec["bytes"] = len(resp.content)
        body = resp.text[:4000]
        # 结构线索
        hints = []
        if "<rss" in body[:500] or "<feed" in body[:500]:
            hints.append("rss/feed")
        if "href=" in body:
            hints.append(f"links~{body.count('href=')}")
        etag = resp.headers.get("etag")
        lm = resp.headers.get("last-modified")
        if etag:
            hints.append("etag")
        if lm:
            hints.append("last-modified")
        rec["notes"] = ", ".join(hints)
        rec["status"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception as exc:
        rec["status"] = "error"
        rec["latency_ms"] = int((time.time() - t0) * 1000)
        rec["notes"] = f"{type(exc).__name__}: {str(exc)[:150]}"
    print(f"[{rec['status']:>8}] {c['name']:24} {rec['http_status']} "
          f"{rec['latency_ms']}ms {rec['bytes']}B {rec['notes'][:80]}")
    return rec


def main() -> None:
    results = []
    with httpx.Client(follow_redirects=True) as client:
        for c in CANDIDATES:
            results.append(probe(client, c))
            time.sleep(1)  # 礼貌间隔
    (OUT / "probe-round1.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[saved] {OUT / 'probe-round1.json'}")


if __name__ == "__main__":
    main()
