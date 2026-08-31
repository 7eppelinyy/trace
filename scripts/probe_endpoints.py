"""Source Completion Gate 端点探测脚本（一次性，证据保存到 verification）。"""
import json, re, time, sys
import httpx

OUT = {}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TraceEventRadar/0.2"}

def probe(name, url, method="GET", **kw):
    t0 = time.time()
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=UA) as c:
            resp = c.request(method, url, **kw)
        body = resp.text
        rec = {
            "url": str(resp.url), "status": resp.status_code,
            "latency_ms": int((time.time() - t0) * 1000),
            "bytes": len(body),
            "etag": resp.headers.get("etag"),
            "last_modified": resp.headers.get("last-modified"),
            "content_type": resp.headers.get("content-type"),
        }
        rec["snippet"] = body[:1500]
        OUT[name] = rec
        print(f"[{name}] {resp.status_code} {len(body)}B {resp.headers.get('content-type','')}", flush=True)
    except Exception as exc:
        OUT[name] = {"url": url, "error": f"{type(exc).__name__}: {exc}",
                     "latency_ms": int((time.time() - t0) * 1000)}
        print(f"[{name}] ERROR {exc}", flush=True)

probe("sandisk_rss", "https://investor.sandisk.com/rss.xml")
probe("micron_rss", "https://investors.micron.com/rss.xml")
probe("micron_newsroom", "https://www.micron.com/about/newsroom/press-releases")
probe("commerce_govdelivery", "https://public.govdelivery.com/accounts/USDOC/feed.rss")
probe("miit_home", "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html")
probe("mofcom_xwfb", "https://www.mofcom.gov.cn/article/xwfb/")
probe("bis_press", "https://www.bis.doc.gov/index.php/policy-focus/press-releases")

with open("verification/_probe_endpoints.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print("saved verification/_probe_endpoints.json")
