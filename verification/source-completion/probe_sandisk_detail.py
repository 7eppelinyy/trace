"""SanDisk press-release 详情页解析验证（直连，无代理）。"""
import httpx
import json
import re
from bs4 import BeautifulSoup

H = {"User-Agent": "TraceEventRadar/0.2 research@example.com"}
OUT = {}

urls = [
    "https://www.sandisk.com/company/newsroom/press-releases/2026/2026-08-13-sandisk-investor-day-2026",
    "https://www.sandisk.com/company/newsroom/press-releases/2026/2026-08-12-kioxia-and-sandisk-unveil-new-high-performance-qlc-3d-flash-memory-for-ai-and-data-intensive-apps",
]

for u in urls:
    try:
        r = httpx.get(u, timeout=20, follow_redirects=True, headers=H)
        soup = BeautifulSoup(r.text, "lxml")
        title = soup.title.get_text(strip=True) if soup.title else ""
        h1 = soup.find("h1")
        # 日期：meta tag / og / JSON-LD
        og_pub = soup.find("meta", property="article:published_time")
        jsonld = soup.find("script", type="application/ld+json")
        date = og_pub["content"] if og_pub and og_pub.get("content") else None
        # 正文摘要：找前几段
        paras = [p.get_text(strip=True) for p in soup.select("p") if len(p.get_text(strip=True)) > 40][:3]
        OUT[u] = {
            "status": r.status_code,
            "title": title,
            "h1": h1.get_text(strip=True) if h1 else None,
            "published_time": date,
            "has_jsonld": jsonld is not None,
            "paras_sample": paras,
        }
    except Exception as e:
        OUT[u] = {"error": f"{type(e).__name__}: {e}"}

with open("verification/source-completion/probe-sandisk-detail.json", "w",
          encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print(json.dumps(OUT, ensure_ascii=False, indent=2)[:3500])
