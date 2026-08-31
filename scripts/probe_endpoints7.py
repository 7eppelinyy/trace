"""第七轮：Micron teaser 结构 + SanDisk RSS 字段 + MOFCOM 首页结构确认。"""
import json, re, time
import httpx
from bs4 import BeautifulSoup
import feedparser

OUT = {}
CHROME = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# ---- Micron teaser 结构 ----
try:
    with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
        resp = c.get("https://www.micron.com/about/press/news")
    soup = BeautifulSoup(resp.text, "lxml")
    teasers = soup.select("div.cmp-teaser")
    OUT["micron_teaser_count"] = len(teasers)
    samples = []
    for t in teasers[:3]:
        info = {"html_snippet": str(t)[:1200]}
        h = t.find(["h1", "h2", "h3", "h4"])
        info["heading"] = (h.get_text().strip() if h else None)
        info["heading_tag"] = h.name if h else None
        title_div = t.select_one(".cmp-teaser__title")
        info["title_div"] = (title_div.get_text().strip() if title_div else None)
        desc = t.select_one(".cmp-teaser__description")
        info["desc"] = (desc.get_text(" ", strip=True)[:200] if desc else None)
        a = t.find("a", href=True)
        info["link"] = a["href"] if a else None
        samples.append(info)
    OUT["micron_teasers"] = samples
    print("[micron] teasers:", len(teasers), flush=True)
except Exception as exc:
    OUT["micron_error"] = str(exc)
    print("[micron] ERROR", exc, flush=True)

# ---- SanDisk RSS 字段 ----
try:
    with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
        resp = c.get("https://investor.sandisk.com/rss.xml")
    parsed = feedparser.parse(resp.content)
    OUT["sandisk_entry_count"] = len(parsed.entries)
    OUT["sandisk_feed_etag"] = getattr(parsed, "etag", None)
    OUT["sandisk_feed_modified"] = getattr(parsed, "modified", None)
    if parsed.entries:
        e = parsed.entries[0]
        OUT["sandisk_entry0"] = {k: str(v)[:300] for k, v in e.items()
                                 if k in ("title", "link", "id", "published", "published_parsed",
                                          "updated", "summary", "guidislink")}
        dates = [en.get("published") for en in parsed.entries[:10]]
        OUT["sandisk_dates_first10"] = dates
    print("[sandisk] entries:", len(parsed.entries), flush=True)
except Exception as exc:
    OUT["sandisk_error"] = str(exc)
    print("[sandisk] ERROR", exc, flush=True)

# ---- MOFCOM 首页 li 结构（日期配对） ----
try:
    with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
        resp = c.get("https://www.mofcom.gov.cn/")
    soup = BeautifulSoup(resp.text, "lxml")
    samples = []
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        txt = (a.get_text() or "").strip()
        if len(txt) >= 8 and "/art/20" in a["href"]:
            samples.append({"li_html": str(li)[:400], "href": a["href"], "text": txt[:60]})
            if len(samples) >= 3:
                break
    OUT["mofcom_li_samples"] = samples
    print("[mofcom] samples:", len(samples), flush=True)
except Exception as exc:
    OUT["mofcom_error"] = str(exc)
    print("[mofcom] ERROR", exc, flush=True)

with open("verification/_probe_endpoints7.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2, default=str)
print("saved verification/_probe_endpoints7.json")
