"""第三轮探测：从可达页面提取真实新闻列表入口。"""
import json, re, time
import httpx
from bs4 import BeautifulSoup

OUT = {}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

def get(name, url, **kw):
    t0 = time.time()
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=UA) as c:
            resp = c.request("GET", url, **kw)
        OUT[name] = {"url": str(resp.url), "status": resp.status_code,
                     "latency_ms": int((time.time() - t0) * 1000),
                     "bytes": len(resp.text),
                     "etag": resp.headers.get("etag"),
                     "last_modified": resp.headers.get("last-modified"),
                     "_resp": resp}
        print(f"[{name}] {resp.status_code} {len(resp.text)}B {resp.url}", flush=True)
        return resp
    except Exception as exc:
        OUT[name] = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
        print(f"[{name}] ERROR {exc}", flush=True)
        return None

# ---- SanDisk：从首页找 RSS / news 链接 ----
r = get("sandisk_home", "https://investor.sandisk.com/")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]; txt = (a.get_text() or "").strip()
        if re.search(r"rss|news|press|feed", href + txt, re.I):
            links.append({"href": href, "text": txt[:80]})
    OUT["sandisk_home_links"] = links[:40]
    for m in re.finditer(r'href="([^"]*rss[^"]*)"', r.text, re.I):
        print("  RSS?", m.group(1), flush=True)

# ---- MOFCOM：首页找新闻链接 ----
r = get("mofcom_home", "https://www.mofcom.gov.cn/")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]; txt = (a.get_text() or "").strip()
        if txt and len(txt) >= 6 and "/article/" in href:
            links.append({"href": href, "text": txt[:80]})
    OUT["mofcom_home_links"] = links[:40]
    # 找栏目入口
    cols = [{"href": a["href"], "text": (a.get_text() or "").strip()[:30]}
            for a in soup.find_all("a", href=True)
            if re.search(r"xwfb|news|article/(xwfb|ae)|press", a["href"])]
    OUT["mofcom_columns"] = cols[:30]

# ---- MIIT：页面是否 JS 加载？找 JSON 接口 ----
r = get("miit_sjdt", "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html")
if r:
    body = r.text
    OUT["miit_sjdt_body_len"] = len(body)
    scripts = re.findall(r'src="([^"]+\.js[^"]*)"', body)
    OUT["miit_scripts"] = scripts[:20]
    # 找 json/data 接口线索
    api_hints = re.findall(r'["\']([^"\']*(?:json|list|data|api)[^"\']*)["\']', body)
    OUT["miit_api_hints"] = list(set(api_hints))[:30]
    # iframe?
    OUT["miit_iframes"] = re.findall(r'<iframe[^>]+src="([^"]+)"', body)

# ---- BIS media.bis.gov：Next.js 找 buildId / API ----
r = get("bis_media_home", "https://media.bis.gov/")
if r:
    body = r.text
    build = re.search(r'"buildId":"([^"]+)"', body)
    OUT["bis_buildId"] = build.group(1) if build else None
    OUT["bis_api_hints"] = list(set(re.findall(r'/_next/data/[^"\']+', body)))[:10]
    # 找 press releases 链接
    links = re.findall(r'href="(/[^"]*(?:press|news|media)[^"]*)"', body, re.I)
    OUT["bis_links"] = list(set(links))[:30]

# 保存（去掉 _resp）
out = {k: {kk: vv for kk, vv in v.items() if kk != "_resp"} if isinstance(v, dict) else v
       for k, v in OUT.items()}
with open("verification/_probe_endpoints3.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2, default=str)
print("saved verification/_probe_endpoints3.json")
