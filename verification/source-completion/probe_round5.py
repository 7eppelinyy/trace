"""第五轮探测：工信部 JSON API / 商务部列表页 / Micron 页面内嵌数据。"""

from __future__ import annotations

import json
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
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
report: dict = {}

with httpx.Client(follow_redirects=True, timeout=30) as client:
    # 1) 工信部已知数据 API（search-front-server / xxgk）
    for name, url in [
        ("miit_search_api", "https://www.miit.gov.cn/search-front-server/api/search/info?"
                            "searchWord=%E5%8D%8A%E5%AF%BC%E4%BD%93&dataTypeId=107&pageSize=10&pageNo=1"),
        ("miit_zcwj_api", "https://www.miit.gov.cn/api-gateway/jpa-openapi-zcwj/api/paper?"
                          "pageSize=10&pageNo=1"),
        ("miit_jhtml_api", "https://www.miit.gov.cn/jgsj/zbes/wjfb/index.html"),
        ("miit_sjdt_index", "https://www.miit.gov.cn/xwdt/gxdt/sjdt/index.html"),
    ]:
        try:
            r = client.get(url, headers=H)
            sample = r.text[:500]
            report[name] = {"status": r.status_code, "bytes": len(r.content),
                            "sample": sample}
            print(f"[{r.status_code}] {name}: {sample[:200]}")
        except Exception as exc:
            report[name] = {"status": "error", "sample": str(exc)[:150]}
            print(f"[err] {name}: {exc}")

    # 2) 商务部真实列表页
    for name, url in [
        ("mofcom_rcxwfb", "https://www.mofcom.gov.cn/xwfb/rcxwfb/index.html"),
        ("mofcom_xwfyrth", "https://www.mofcom.gov.cn/xwfb/xwfyrth/index.html"),
        ("mofcom_sywfb", "https://www.mofcom.gov.cn/syxwfb/index.html"),
    ]:
        try:
            r = client.get(url, headers=H)
            soup = BeautifulSoup(r.text, "lxml")
            links = [{"t": a.get_text(strip=True)[:70], "h": a.get("href", "")}
                     for a in soup.select("a[href*='art_']")]
            report[name] = {"status": r.status_code, "links": len(links),
                            "sample": links[:5]}
            print(f"[{r.status_code}] {name}: {len(links)} art links")
            for l in links[:3]:
                print("   -", l["t"], "|", l["h"][:80])
        except Exception as exc:
            report[name] = {"status": "error", "sample": str(exc)[:150]}
            print(f"[err] {name}: {exc}")

    # 3) Micron press 页面：寻找内嵌 JSON / API 线索
    r = client.get("https://www.micron.com/about/newsroom/press-releases", headers=H)
    text = r.text
    api_hits = sorted(set(re.findall(r'["\'](/api/[^"\']{3,80})["\']', text)))[:10]
    json_scripts = []
    soup = BeautifulSoup(text, "lxml")
    for s in soup.find_all("script"):
        content = s.string or ""
        if len(content) > 1000 and ("press" in content.lower() or "news" in content.lower()):
            json_scripts.append(content[:300])
    report["micron_press_analysis"] = {
        "status": r.status_code, "bytes": len(r.content),
        "api_candidates": api_hits, "json_script_samples": json_scripts[:3],
    }
    print(f"\nmicron api candidates: {api_hits}")

(OUT / "probe-round5.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[saved] {OUT / 'probe-round5.json'}")
