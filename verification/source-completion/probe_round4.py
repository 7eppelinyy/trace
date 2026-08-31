"""第四轮探测：Micron press page 结构 / 工信部内容 / 商务部首页栏目发现。"""

from __future__ import annotations

import json
import re
import sys
import time
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

report: dict = {}

with httpx.Client(follow_redirects=True, timeout=30) as client:
    # 1) Micron press releases 页面结构
    r = client.get("https://www.micron.com/about/newsroom/press-releases", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    # 找 JSON-LD 或 __NEXT_DATA__ 类数据
    scripts = [s.string or "" for s in soup.find_all("script") if s.string]
    json_like = [s[:300] for s in scripts if "press" in s.lower() and len(s) > 500][:2]
    links = []
    for a in soup.select("a[href*='/about/newsroom/']"):
        href = a.get("href", "")
        title = a.get_text(strip=True)
        if title and len(title) > 15:
            links.append({"title": title[:100], "href": href})
    report["micron_press_page"] = {
        "http_status": r.status_code, "bytes": len(r.content),
        "links_count": len(links), "links_sample": links[:6],
        "json_scripts_sample": json_like,
    }
    print(f"micron_press_page: {len(links)} links")
    for l in links[:5]:
        print("  -", l["title"][:80], l["href"][:60])

    # 2) 工信部：检查页面结构（可能是 iframe 或 JS）
    r = client.get("https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    iframes = [f.get("src") for f in soup.find_all("iframe")]
    links = [{"title": a.get_text(strip=True)[:80], "href": a.get("href")}
             for a in soup.select("a") if len(a.get_text(strip=True)) > 12]
    report["miit_sjdt"] = {
        "http_status": r.status_code, "bytes": len(r.content),
        "iframes": iframes, "links_sample": links[:6],
        "raw_head": r.text[:600],
    }
    print(f"\nmiit_sjdt: iframes={iframes} links={len(links)}")

    # 3) 商务部首页：发现栏目链接
    r = client.get("https://www.mofcom.gov.cn/", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    seen = set()
    sections = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if re.search(r"/article/", href) or re.search(r"/xwfb|news|xw", href):
            t = a.get_text(strip=True)
            if t and len(t) > 4 and href not in seen:
                seen.add(href)
                sections.append({"title": t[:60], "href": href[:120]})
    report["mofcom_home_links"] = sections[:15]
    print(f"\nmofcom_home: {len(sections)} candidate links")
    for s in sections[:12]:
        print("  -", s["title"], s["href"])

(OUT / "probe-round4.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[saved] {OUT / 'probe-round4.json'}")
