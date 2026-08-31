"""探测所有 enabled 源在无代理情况下的可达性（直连）。"""
import json
import time

import httpx

OUT = "verification/source-completion/probe-direct-reachability.json"

TARGETS = [
    ("src_sec_edgar", "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=NVDA&type=8-K&dateb=&owner=include&count=5&output=atom"),
    ("src_nvidia_ir", "https://nvidianews.nvidia.com/rss.xml"),
    ("src_sndk_ir_sitemap", "https://www.sandisk.com/sitemap.xml"),
    ("src_micron_ir_newsroom", "https://www.micron.com/about/newsroom/press-releases"),
    ("src_bis", "https://www.bis.doc.gov/index.php/policy-focus/press-releases"),
    ("src_federal_register", "https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bterm%5D=semiconductor"),
    ("src_fed", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("src_commerce", "https://public.govdelivery.com/accounts/USDOC/feed.rss"),
    ("src_miit", "https://www.miit.gov.cn/"),
    ("src_mofcom", "https://www.mofcom.gov.cn/"),
    ("src_cninfo", "http://www.cninfo.com.cn/new/index"),
]

result = {}
for name, url in TARGETS:
    t0 = time.time()
    entry = {"url": url}
    try:
        with httpx.Client(timeout=20, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; TraceEventRadar/0.2)"}) as c:
            r = c.get(url)
        entry.update({"http_status": r.status_code,
                      "latency_ms": int((time.time() - t0) * 1000),
                      "bytes": len(r.content)})
    except Exception as exc:
        entry.update({"error": f"{type(exc).__name__}: {exc}",
                      "latency_ms": int((time.time() - t0) * 1000)})
    result[name] = entry
    print(name, entry.get("http_status", entry.get("error")),
          entry.get("latency_ms"), "ms")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
