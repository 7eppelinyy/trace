"""第七轮探测：Micron AEM model.json / 商务部首页真实条目 / 工信部首页。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import trace.config  # noqa: F402,E402
import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

OUT = Path(__file__).resolve().parent
H = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
report: dict = {}

with httpx.Client(follow_redirects=True, timeout=30) as client:
    # 1) Micron AEM model JSON（AEM 标准数据接口）
    for name, url in [
        ("micron_model_json", "https://www.micron.com/about/newsroom/press-releases.model.json"),
        ("micron_model_json2", "https://www.micron.com/about/newsroom.model.json"),
    ]:
        try:
            r = client.get(url, headers=H)
            txt = r.text[:600]
            report[name] = {"status": r.status_code, "bytes": len(r.content), "sample": txt}
            print(f"[{r.status_code}] {name}: {txt[:250]}\n")
        except Exception as exc:
            report[name] = {"error": str(exc)[:150]}
            print(f"[err] {name}: {exc}\n")

    # 2) 商务部首页：解析真实新闻条目（art_ 链接 + 日期）
    r = client.get("https://www.mofcom.gov.cn/", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for a in soup.select("a[href*='art_']"):
        t = a.get_text(strip=True)
        if t and len(t) > 8:
            items.append({"title": t[:90], "url": a["href"]})
    # 查找日期
    dates = re.findall(r"20\d{2}[-/]\d{2}[-/]\d{2}", r.text)
    report["mofcom_home"] = {"status": r.status_code, "items": len(items),
                             "sample": items[:8], "dates_found": dates[:8]}
    print(f"mofcom_home: {len(items)} items, dates={dates[:5]}")
    for it in items[:6]:
        print("  -", it["title"])

    # 3) 工信部首页
    r = client.get("https://www.miit.gov.cn/", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    items = []
    for a in soup.select("a[href]"):
        t = a.get_text(strip=True)
        href = a.get("href", "")
        if t and len(t) > 10 and ("/art/" in href or "/gzdt/" in href or "index" in href):
            items.append({"title": t[:90], "url": href[:110]})
    report["miit_home"] = {"status": r.status_code, "bytes": len(r.content),
                           "items": len(items), "sample": items[:10]}
    print(f"\nmiit_home: {len(items)} items")
    for it in items[:8]:
        print("  -", it["title"], "|", it["url"])

    # 4) GovDelivery USDOC feed 内容抽样
    r = client.get("https://public.govdelivery.com/accounts/USDOC/feed.rss", headers=H)
    import feedparser
    p = feedparser.parse(r.content)
    report["commerce_govdelivery"] = {
        "status": r.status_code, "entries": len(p.entries),
        "titles": [e.get("title", "")[:90] for e in p.entries[:6]],
        "dates": [e.get("published", "") for e in p.entries[:3]],
    }
    print(f"\ncommerce_govdelivery: {len(p.entries)} entries")
    for e in p.entries[:5]:
        print("  -", e.get("title", "")[:90])

(OUT / "probe-round7.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[saved] {OUT / 'probe-round7.json'}")
