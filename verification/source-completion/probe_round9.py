"""第九轮：Micron 新闻室 HTML 精确结构（h2/日期/链接的父级容器）。"""

from __future__ import annotations

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

with httpx.Client(follow_redirects=True, timeout=30) as client:
    r = client.get("https://www.micron.com/about/newsroom/press-releases", headers=H)
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for h2 in soup.find_all("h2"):
        text = h2.get_text(strip=True)
        if len(text) < 15:
            continue
        # 找包裹该 h2 的可链接祖先（a / 含 a 的 li/article/div）
        ancestor = h2
        link = None
        for _ in range(6):
            ancestor = ancestor.parent
            if ancestor is None:
                break
            a = ancestor.find("a", href=True) if ancestor.name != "a" else ancestor
            if a and a.get("href"):
                link = a
                break
        # 日期：祖先文本内
        parent_text = ancestor.get_text(" ", strip=True) if ancestor else ""
        m = re.search(r"([A-Z][a-z]{2}\.?)\s+(\d{1,2}),\s+(20\d{2})", parent_text)
        out.append({
            "title": text[:110],
            "href": (link.get("href") if link else None),
            "link_text": (link.get_text(strip=True)[:60] if link else None),
            "date": m.group(0) if m else None,
            "ancestor_tag": ancestor.name if ancestor else None,
        })
    import json
    (OUT / "probe-micron-structure.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    for o in out[:8]:
        print(o)
    print(f"total: {len(out)}")
