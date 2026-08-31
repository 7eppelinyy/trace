"""第二轮端点探测：SanDisk / MIIT / MOFCOM / BIS 真实入口。"""
import json, time
import httpx

OUT = {}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

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
            "snippet": body[:2000],
        }
        OUT[name] = rec
        print(f"[{name}] {resp.status_code} {len(body)}B final={resp.url}", flush=True)
    except Exception as exc:
        OUT[name] = {"url": url, "error": f"{type(exc).__name__}: {exc}",
                     "latency_ms": int((time.time() - t0) * 1000)}
        print(f"[{name}] ERROR {type(exc).__name__}: {exc}", flush=True)

# ---- SanDisk IR 候选入口 ----
probe("sandisk_rss_retry", "https://investor.sandisk.com/rss.xml")
probe("sandisk_news", "https://investor.sandisk.com/news/default.aspx")
probe("sandisk_news2", "https://investor.sandisk.com/news-releases")
probe("sandisk_home", "https://investor.sandisk.com/")
probe("sandisk_8k", "https://investor.sandisk.com/financial-information/sec-filings")

# ---- MOFCOM 候选入口 ----
probe("mofcom_home", "https://www.mofcom.gov.cn/")
probe("mofcom_xwfb2", "https://www.mofcom.gov.cn/article/xwfb/xwrcjd/")
probe("mofcom_sjfz", "https://www.mofcom.gov.cn/article/sjfz/")

# ---- MIIT 候选入口 ----
probe("miit_sjdt", "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html")
probe("miit_ldhd", "https://www.miit.gov.cn/xwdt/gxdt/ldhd/index.html")
probe("miit_home2", "https://www.miit.gov.cn/xwdt/gxdt/index.html")

# ---- BIS 新站 ----
probe("bis_media", "https://media.bis.gov/")
probe("bis_media_press", "https://media.bis.gov/press-releases")
probe("bis_doc_legacy", "https://www.bis.doc.gov/index.php/policy-focus/press-releases")

with open("verification/_probe_endpoints2.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print("saved verification/_probe_endpoints2.json")
