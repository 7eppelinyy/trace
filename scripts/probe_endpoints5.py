"""第五轮探测：MIIT API 姿势 / MOFCOM 首页配对 / Micron 新闻室结构 / BIS 解析。"""
import json, re, time
import httpx
from bs4 import BeautifulSoup

OUT = {}
CHROME = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
          "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

def get(name, url, headers=None, **kw):
    t0 = time.time()
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=headers or CHROME) as c:
            resp = c.request("GET", url, **kw)
        OUT[name] = {"url": str(resp.url), "status": resp.status_code,
                     "latency_ms": int((time.time() - t0) * 1000),
                     "bytes": len(resp.text), "snippet": resp.text[:1200]}
        print(f"[{name}] {resp.status_code} {len(resp.text)}B", flush=True)
        return resp
    except Exception as exc:
        OUT[name] = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
        print(f"[{name}] ERROR {type(exc).__name__}: {exc}", flush=True)

# ---- MIIT API 姿势试验 ----
api = "https://www.miit.gov.cn/api-gateway/jpaas-publish-server/front/page/build/unit"
WEB = "8d828e408d90447786ddbe128d495e9e"
with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
    # GET with query params
    for params in [
        {"webId": WEB, "pageId": "028da85b0dbd4c9cb96fd5f421cd32b8"},
        {"webId": WEB},
    ]:
        try:
            r = c.get(api, params=params)
            key = f"miit_get_{'_'.join(params.keys())}"
            OUT[key] = {"status": r.status_code, "snippet": r.text[:800]}
            print(f"[{key}] {r.status_code} {r.text[:120]}", flush=True)
        except Exception as e:
            print("err", e)
    # POST JSON
    try:
        r = c.post(api, json={"webId": WEB, "pageId": "028da85b0dbd4c9cb96fd5f421cd32b8"})
        OUT["miit_post_json"] = {"status": r.status_code, "snippet": r.text[:800]}
        print("[miit_post_json]", r.status_code, r.text[:150], flush=True)
    except Exception as e:
        print("err", e)

# 读 unitbuild.js 找 API 用法
r = get("miit_unitbuild_js", "https://www.miit.gov.cn/cms_files/default/script/AuthorizedRead/unitbuild.js?v=3.1.3-GXB")
if r:
    body = r.text
    OUT["miit_unitbuild_len"] = len(body)
    # find ajax calls
    OUT["miit_unitbuild_ajax"] = re.findall(r'(?:url|ajax|post|get)\s*[\(:]?\s*["\']([^"\']+)["\']', body)[:20]
    OUT["miit_unitbuild_snippet"] = body[:2000]

# ---- MOFCOM 首页：链接与日期配对 ----
r = get("mofcom_home_full", "https://www.mofcom.gov.cn/", headers=CHROME)
if r:
    soup = BeautifulSoup(r.text, "lxml")
    pairs = []
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        txt = (a.get_text() or "").strip()
        href = a["href"]
        if len(txt) >= 8 and ("/art/20" in href or "/article/" in href):
            date_m = re.search(r'(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})', li.get_text())
            pairs.append({"href": href, "text": txt[:80],
                          "date": date_m.group(1) if date_m else None})
    OUT["mofcom_home_pairs"] = pairs[:30]

# ---- Micron newsroom 页面结构 ----
r = get("micron_newsroom2", "https://www.micron.com/about/newsroom/press-releases")
if r:
    body = r.text
    soup = BeautifulSoup(body, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]; txt = (a.get_text() or "").strip()
        if re.search(r"/about/newsroom/|/about/blog/|press", href) and len(txt) > 15:
            links.append({"href": href, "text": txt[:100]})
    OUT["micron_links"] = links[:25]
    # embedded JSON (Next/AEM often embed JSON-LD or __NEXT_DATA__)
    nxt = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.S)
    OUT["micron_next_data"] = bool(nxt)
    jsonld = re.findall(r'<script type="application/ld\+json">(.*?)</script>', body, re.S)
    OUT["micron_jsonld_count"] = len(jsonld)
    if jsonld:
        OUT["micron_jsonld_snippet"] = jsonld[0][:800]
    # dates
    dates = re.findall(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, \d{4})', body)
    OUT["micron_dates"] = dates[:15]
    # look for API hints
    OUT["micron_api_hints"] = list(set(re.findall(r'["\']([^"\']*(?:api|graphql|json)[^"\']*)["\']', body, re.I)))[:20]

# ---- BIS news-updates 可解析性（HTML anchors + dates） ----
r = get("bis_news2", "https://media.bis.gov/news-updates")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    entries = []
    for a in soup.find_all("a", href=True):
        href = a["href"]; txt = (a.get_text() or "").strip()
        if href.startswith("/press-release/") and len(txt) > 15:
            # find sibling date
            parent = a.find_parent(["li", "div", "article"])
            date = None
            if parent:
                dm = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})', parent.get_text())
                date = dm.group(1) if dm else None
            entries.append({"href": href, "text": txt[:100], "date": date})
    OUT["bis_entries"] = entries[:15]

with open("verification/_probe_endpoints5.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2, default=str)
print("saved verification/_probe_endpoints5.json")
