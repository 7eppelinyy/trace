"""探测 SanDisk 两条路线：IR RSS（经代理） vs 主站 sitemap（直连）。"""
import json
import time

import httpx

OUT = "verification/source-completion/probe-sndk-routes.json"
result = {}

PROXIES = "http://127.0.0.1:10808"

for name, url, use_proxy in [
    ("ir_rss_via_proxy", "https://investor.sandisk.com/rss.xml", True),
    ("sitemap_direct", "https://www.sandisk.com/sitemap.xml", False),
]:
    t0 = time.time()
    entry = {"url": url}
    try:
        kwargs = {"timeout": 25, "follow_redirects": True,
                  "headers": {"User-Agent": "Mozilla/5.0"}}
        if use_proxy:
            kwargs["proxy"] = PROXIES
        with httpx.Client(**kwargs) as c:
            r = c.get(url)
        entry.update({
            "http_status": r.status_code,
            "latency_ms": int((time.time() - t0) * 1000),
            "bytes": len(r.content),
            "etag": r.headers.get("ETag"),
            "last_modified": r.headers.get("Last-Modified"),
            "sample": r.text[:500],
        })
    except Exception as exc:
        entry.update({"error": f"{type(exc).__name__}: {exc}",
                      "latency_ms": int((time.time() - t0) * 1000)})
    result[name] = entry

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
