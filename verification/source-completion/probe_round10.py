"""第十轮：Micron 新闻条目日期元素定位 + TrendForce 授权/入口核查。"""

from __future__ import annotations

import json
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
report = {}

with httpx.Client(follow_redirects=True, timeout=30) as client:
    # 1) Micron：找含 "Read article" 链接的卡片容器，打印其完整文本与子元素
    r = client.get("https://www.micron.com/about/newsroom/press-releases", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    cards = []
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == "Read article":
            container = a
            for _ in range(8):
                if container.parent is None:
                    break
                container = container.parent
                if container.find("h2"):
                    break
            h2 = container.find("h2")
            if not h2:
                continue
            # 收集容器内所有文本节点与子标签
            texts = [t.strip() for t in container.stripped_strings]
            cards.append({
                "title": h2.get_text(strip=True)[:110],
                "href": a["href"][:140],
                "container_tag": container.name,
                "container_class": container.get("class"),
                "texts": texts[:12],
            })
            if len(cards) >= 3:
                break
    report["micron_cards"] = cards
    for c in cards:
        print(c["container_tag"], c["container_class"], "|", c["title"][:60])
        print("  texts:", c["texts"])

    # 2) TrendForce：RSS / robots 政策
    for name, url in [
        ("trendforce_rss", "https://www.trendforce.com/rss"),
        ("trendforce_press", "https://www.trendforce.com/presscenter"),
        ("trendforce_news", "https://www.trendforce.com/news/"),
        ("trendforce_robots", "https://www.trendforce.com/robots.txt"),
    ]:
        try:
            r = client.get(url, headers=H)
            txt = r.text[:500]
            report[name] = {"status": r.status_code, "bytes": len(r.content), "sample": txt}
            print(f"[{r.status_code}] {name}: {txt[:180]}\n")
        except Exception as exc:
            report[name] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
            print(f"[err] {name}: {exc}\n")

(OUT / "probe-round10.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[saved] {OUT / 'probe-round10.json'}")
