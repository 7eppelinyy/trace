"""SanDisk 直连探测（第三轮）：全量 sitemap 分类 + RSC flight 数据。"""
import httpx
import json
import re

H = {"User-Agent": "TraceEventRadar/0.2 research@example.com"}
OUT = {}

# 1. 全量 sitemap 新闻条目分类
r = httpx.get("https://www.sandisk.com/sitemap.xml", timeout=20,
              follow_redirects=True, headers=H)
entries = []
for m in re.finditer(r"<url>\s*<loc>([^<]+)</loc>\s*<lastmod>([^<]*)</lastmod>", r.text):
    entries.append({"url": m.group(1), "lastmod": m.group(2)})
news = [e for e in entries if "/company/newsroom" in e["url"]]
OUT["total_urls"] = len(entries)
OUT["newsroom_urls"] = len(news)
from collections import Counter
segs = Counter()
for e in news:
    tail = e["url"].split("/newsroom/")[-1]
    segs[tail.split("/")[0] if "/" in tail else "(page)"] += 1
OUT["segments"] = dict(segs)
# 最新的 10 条（按 lastmod 排序）
news.sort(key=lambda e: e["lastmod"], reverse=True)
OUT["newest"] = news[:10]

# 2. RSC flight：检查是否包含新闻列表数据
rsc_headers = dict(H)
rsc_headers["RSC"] = "1"
r2 = httpx.get("https://www.sandisk.com/company/newsroom", timeout=20,
               follow_redirects=True, headers=rsc_headers)
OUT["rsc_status"] = r2.status_code
body = r2.text
OUT["rsc_len"] = len(body)
# 提取字符串中的 URL 与标题
urls_in_rsc = sorted(set(re.findall(r'https?://[^"\\\s]+sandisk[^"\\\s]+', body)))[:30]
OUT["rsc_urls"] = urls_in_rsc
# 提取类似标题的长文本片段
titles = re.findall(r'"([^"]{20,120})"', body)
OUT["rsc_title_like"] = [t for t in titles if not t.startswith(
    ("http", "/", "1:", "I[", "$", "[", "{"))][:20]

with open("verification/source-completion/probe-sandisk-direct3.json", "w",
          encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print(json.dumps(OUT, ensure_ascii=False, indent=2)[:4000])
