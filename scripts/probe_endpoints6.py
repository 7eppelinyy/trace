"""第六轮：MIIT queryData 提取 + Micron 新闻条目结构。"""
import json, re, time
import httpx
from bs4 import BeautifulSoup

OUT = {}
CHROME = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
          "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}

def get(name, url, **kw):
    t0 = time.time()
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
            resp = c.get(url, **kw)
        print(f"[{name}] {resp.status_code} {len(resp.text)}B", flush=True)
        return resp
    except Exception as exc:
        OUT[name] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"[{name}] ERROR {exc}", flush=True)

# ---- MIIT: 提取 queryData 并请求 ----
r = get("miit_page", "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html")
if r:
    soup = BeautifulSoup(r.text, "lxml")
    scripts = []
    for s in soup.find_all("script"):
        attrs = {k: v for k, v in s.attrs.items() if k in ("id", "url", "querydata", "queryData")}
        if attrs.get("url") or attrs.get("querydata"):
            scripts.append(attrs)
    OUT["miit_script_attrs"] = scripts
    print("script attrs:", json.dumps(scripts, ensure_ascii=False), flush=True)

    for s in scripts:
        url_attr = s.get("url")
        qd = s.get("querydata") or s.get("queryData")
        if not url_attr or not qd:
            continue
        try:
            query = json.loads(qd.replace("'", '"'))
        except Exception as exc:
            OUT["miit_qd_parse_error"] = str(exc)
            continue
        api_url = url_attr if url_attr.startswith("http") else "https://www.miit.gov.cn" + url_attr
        try:
            with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
                resp = c.get(api_url, params=query)
            OUT["miit_unit_resp"] = {"status": resp.status_code, "bytes": len(resp.text),
                                     "snippet": resp.text[:1500]}
            print(f"[miit_unit] {resp.status_code} {len(resp.text)}B", flush=True)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    html = (data.get("data") or {}).get("html") or ""
                    OUT["miit_unit_html_len"] = len(html)
                    hsoup = BeautifulSoup(html, "lxml")
                    entries = []
                    for a in hsoup.find_all("a", href=True):
                        txt = (a.get_text() or "").strip()
                        if len(txt) >= 6:
                            parent = a.find_parent("li") or a.find_parent("div")
                            dm = re.search(r'(\d{4}-\d{2}-\d{2})', parent.get_text() if parent else "")
                            entries.append({"href": a["href"], "text": txt[:80],
                                            "date": dm.group(1) if dm else None})
                    OUT["miit_unit_entries"] = entries[:25]
                except Exception as exc:
                    OUT["miit_unit_json_error"] = str(exc)
        except Exception as exc:
            OUT["miit_unit_error"] = str(exc)
        break

# ---- Micron: 找新闻条目真实结构 ----
r = get("micron_page", "https://www.micron.com/about/newsroom/press-releases")
if r:
    body = r.text
    soup = BeautifulSoup(body, "lxml")
    # 所有 href 中含 news/press/release 的
    all_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"news|press|release|article", href, re.I):
            txt = (a.get_text() or "").strip()
            if txt:
                all_links.append({"href": href, "text": txt[:100]})
    OUT["micron_all_news_links"] = all_links[:30]
    # 按日期附近的块结构：找包含 "July 16, 2026" 的元素
    target = soup.find(string=re.compile(r"July 16, 2026"))
    if target:
        node = target.parent
        chain = []
        for _ in range(6):
            if node is None:
                break
            chain.append(f"{node.name}.{node.get('class')}")
            node = node.parent
        OUT["micron_date_ancestors"] = chain
        # 在同容器找链接
        container = target.parent
        for _ in range(4):
            if container is None:
                break
            links = container.find_all("a", href=True)
            if links:
                OUT["micron_container_links"] = [
                    {"href": a["href"], "text": (a.get_text() or "").strip()[:80]}
                    for a in links][:10]
                break
            container = container.parent
    # AEM search API 尝试
    with httpx.Client(timeout=30, follow_redirects=True, headers=CHROME) as c:
        try:
            resp = c.get("https://www.micron.com/content/micron/us/en/about/press/news/_jcr_content.search.json/getSuggestions/memory/en_US/11")
            OUT["micron_search_api"] = {"status": resp.status_code, "snippet": resp.text[:1000]}
            print("[micron_search_api]", resp.status_code, resp.text[:120], flush=True)
        except Exception as exc:
            OUT["micron_search_api"] = {"error": str(exc)}

with open("verification/_probe_endpoints6.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2, default=str)
print("saved verification/_probe_endpoints6.json")
