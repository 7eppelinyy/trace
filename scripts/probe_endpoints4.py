"""第四轮探测：SanDisk RSS 重试 + MOFCOM 列表页 + MIIT API + BIS news-updates。"""
import json, re, time
import httpx
from bs4 import BeautifulSoup

OUT = {}
CHROME = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

def get(name, url, headers=None, **kw):
    t0 = time.time()
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=headers or CHROME) as c:
            resp = c.request("GET", url, **kw)
        OUT[name] = {"url": str(resp.url), "status": resp.status_code,
                     "latency_ms": int((time.time() - t0) * 1000),
                     "bytes": len(resp.text),
                     "etag": resp.headers.get("etag"),
                     "last_modified": resp.headers.get("last-modified"),
                     "snippet": resp.text[:3000]}
        print(f"[{name}] {resp.status_code} {len(resp.text)}B {resp.url}", flush=True)
        return resp
    except Exception as exc:
        OUT[name] = {"url": url, "error": f"{type(exc).__name__}: {exc}",
                     "latency_ms": int((time.time() - t0) * 1000)}
        print(f"[{name}] ERROR {type(exc).__name__}: {exc}", flush=True)
        return None

# SanDisk RSS with browser UA
get("sandisk_rss_chrome", "https://investor.sandisk.com/rss.xml")
# SanDisk news releases listing page
r = get("sandisk_news_list", "https://investor.sandisk.com/news-events/news-releases")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    entries = []
    for a in soup.find_all("a", href=True):
        if "/news-release-details/" in a["href"]:
            entries.append({"href": a["href"], "text": (a.get_text() or "").strip()[:100]})
    OUT["sandisk_news_entries"] = entries[:20]
    # date patterns near entries
    dates = re.findall(r'(\w+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})', r.text)
    OUT["sandisk_dates_sample"] = dates[:20]

# MOFCOM 列表页
r = get("mofcom_rcxwfb", "https://www.mofcom.gov.cn/xwfb/rcxwfb/index.html")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    entries = []
    for a in soup.find_all("a", href=True):
        txt = (a.get_text() or "").strip()
        if "/art/20" in a["href"] and len(txt) >= 6:
            entries.append({"href": a["href"], "text": txt[:80]})
    OUT["mofcom_rcxwfb_entries"] = entries[:20]
    dates = re.findall(r'(\d{4}-\d{2}-\d{2})', r.text)
    OUT["mofcom_rcxwfb_dates"] = dates[:20]
    OUT["mofcom_rcxwfb_snippet_tail"] = r.text[-1500:]

r = get("mofcom_xwfyrth", "https://www.mofcom.gov.cn/xwfb/xwfyrth/index.html")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    entries = [{"href": a["href"], "text": (a.get_text() or "").strip()[:80]}
               for a in soup.find_all("a", href=True)
               if "/art/20" in a["href"] and len((a.get_text() or "").strip()) >= 6]
    OUT["mofcom_xwfyrth_entries"] = entries[:20]

# MIIT API gateway
r = get("miit_api", "https://www.miit.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit",
        headers={**CHROME, "Content-Type": "application/x-www-form-urlencoded"})
# try POST
try:
    with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
        resp = c.post("https://www.miit.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit",
                      data={"webId": "8d828e408d90447786ddbe128d495e9e",
                            "pageId": "test", "unitId": "test"})
        OUT["miit_api_post"] = {"status": resp.status_code, "snippet": resp.text[:500]}
        print("[miit_api_post]", resp.status_code, flush=True)
except Exception as exc:
    OUT["miit_api_post"] = {"error": str(exc)}
    print("[miit_api_post] ERROR", exc, flush=True)

# MIIT 页面里 unit 构建的 data 参数
r2 = get("miit_sjdt_full", "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html")
if r2:
    OUT["miit_page_data_attrs"] = re.findall(r'data-([a-z]+)="([^"]{0,80})"', r2.text)[:40]
    OUT["miit_unit_calls"] = re.findall(r'build/unit[^"\']*', r2.text)[:5]
    # 查找 buildUnit 相关 JS 参数
    OUT["miit_js_params"] = re.findall(r'(webId|pageId|unitId|channelId)["\']?\s*[:=]\s*["\']([^"\']+)', r2.text)[:20]

# BIS news-updates 列表页
r = get("bis_news_updates", "https://media.bis.gov/news-updates")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    entries = []
    for a in soup.find_all("a", href=True):
        if "/press-release/" in a["href"] or "/news-update" in a["href"]:
            entries.append({"href": a["href"], "text": (a.get_text() or "").strip()[:100]})
    OUT["bis_news_entries"] = entries[:20]
    dates = re.findall(r'([A-Z][a-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})', r.text)
    OUT["bis_dates_sample"] = dates[:20]
    # Next.js data
    nxt = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    OUT["bis_next_data"] = bool(nxt)
    if nxt:
        OUT["bis_next_data_snippet"] = nxt.group(1)[:1500]

with open("verification/_probe_endpoints4.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2, default=str)
print("saved verification/_probe_endpoints4.json")
