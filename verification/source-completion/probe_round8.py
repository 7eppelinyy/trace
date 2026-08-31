"""第八轮探测：Micron 页面真实内容定位 / 商务部+工信部首页日期配对。"""

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
    # 1) Micron Edge Delivery JSON / plain.html
    for name, url in [
        ("micron_helix_json", "https://www.micron.com/about/newsroom/press-releases.json"),
        ("micron_plain_html", "https://www.micron.com/about/newsroom/press-releases.plain.html"),
    ]:
        try:
            r = client.get(url, headers=H)
            report[name] = {"status": r.status_code, "bytes": len(r.content),
                            "sample": r.text[:300]}
            print(f"[{r.status_code}] {name}: {r.text[:200]}\n")
        except Exception as exc:
            print(f"[err] {name}: {exc}\n")

    # 2) Micron 页面 HTML 里是否已含新闻列表文本
    r = client.get("https://www.micron.com/about/newsroom/press-releases", headers=H)
    text = r.text
    # 找包含典型新闻稿标题的文本块
    hits = re.findall(r'<(h[1-4]|a)[^>]*>([^<]{20,150}?)</\1>', text)
    news_hits = [h for h in hits if any(k in h[1].lower() for k in
                 ("micron", "fiscal", "quarter", "announces", "reports", "hbm", "dram"))][:10]
    report["micron_html_headings"] = news_hits
    print(f"micron headings with news keywords: {len(news_hits)}")
    for h in news_hits[:8]:
        print("  -", h[0], h[1][:100])
    # 也搜索 "2026" 日期块
    date_hits = re.findall(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, 20\d{2}', text)[:8]
    report["micron_dates"] = date_hits
    print("micron dates:", date_hits[:6])

    # 3) 商务部首页：条目-日期配对结构
    r = client.get("https://www.mofcom.gov.cn/", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    pairs = []
    # 找带日期的列表项（li）
    for li in soup.select("li"):
        a = li.select_one("a[href*='art_']")
        date = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", li.get_text())
        if a and date:
            pairs.append({"title": a.get_text(strip=True)[:80],
                          "url": a["href"][:110], "date": date.group()})
    report["mofcom_pairs"] = pairs[:8]
    print(f"\nmofcom title-date pairs: {len(pairs)}")
    for p in pairs[:6]:
        print(f"  [{p['date']}] {p['title']}")

    # 4) 工信部首页：条目-日期配对结构
    r = client.get("https://www.miit.gov.cn/", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    pairs = []
    for li in soup.select("li"):
        a = li.select_one("a[href*='/art/']")
        date = re.search(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", li.get_text())
        if a and date:
            pairs.append({"title": a.get_text(strip=True)[:80],
                          "url": a["href"][:110], "date": date.group()})
    report["miit_pairs"] = pairs[:8]
    print(f"\nmiit title-date pairs: {len(pairs)}")
    for p in pairs[:6]:
        print(f"  [{p['date']}] {p['title']}")

(OUT / "probe-round8.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[saved] {OUT / 'probe-round8.json'}")
