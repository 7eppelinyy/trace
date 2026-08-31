"""SanDisk 直连入口探测（第二轮）：sitemap 新闻条目 + api.sandisk.com。"""
import httpx
import json
import re

H = {"User-Agent": "TraceEventRadar/0.2 research@example.com"}
OUT = {}

def get(url, timeout=15):
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True, headers=H)
        return r.status_code, r.text, str(r.url)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", url

# 1. sitemap 中的新闻类条目
st, body, _ = get("https://www.sandisk.com/sitemap.xml")
news_urls = []
if st == 200:
    for m in re.finditer(r"<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]*)</lastmod>)?", body):
        loc, lastmod = m.group(1), m.group(2) or ""
        if any(k in loc.lower() for k in ("newsroom", "press", "news", "media")):
            news_urls.append({"url": loc, "lastmod": lastmod})
OUT["sitemap_news_count"] = len(news_urls)
OUT["sitemap_news_sample"] = news_urls[:20]

# 2. 子 sitemap（常见 sitemap index 结构）
if st == 200 and "<sitemapindex" in body:
    OUT["sitemap_index"] = re.findall(r"<loc>([^<]+)</loc>", body)

# 3. api.sandisk.com 根路径与常见新闻端点
for u in ["https://api.sandisk.com/",
          "https://api.sandisk.com/v1/news",
          "https://api.sandisk.com/newsroom",
          "https://api.sandisk.com/api/newsroom/articles"]:
    st3, body3, final3 = get(u, timeout=10)
    OUT.setdefault("api_probe", []).append(
        {"url": u, "status": st3,
         "sample": (body3[:200] if st3 else body3)})

# 4. Next.js RSC：直接请求 newsroom 页面的 JSON flight 数据
rsc_headers = dict(H)
rsc_headers["RSC"] = "1"
try:
    r = httpx.get("https://www.sandisk.com/company/newsroom",
                  timeout=15, follow_redirects=True, headers=rsc_headers)
    OUT["rsc"] = {"status": r.status_code, "len": len(r.text),
                  "sample": r.text[:500]}
except Exception as e:
    OUT["rsc"] = {"error": str(e)}

with open("verification/source-completion/probe-sandisk-direct2.json", "w",
          encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print(json.dumps(OUT, ensure_ascii=False, indent=2)[:3000])
