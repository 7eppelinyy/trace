"""第六轮探测：工信部 search API 参数 / 商务部 CMS 接口 / Micron 页面数据。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import trace.config  # noqa: F402,E402
import httpx  # noqa: E402

OUT = Path(__file__).resolve().parent
H = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
report: dict = {}

with httpx.Client(follow_redirects=True, timeout=30) as client:
    # 1) 工信部 search API：尝试补齐参数（dataTypeId/categoryId/siteId）
    for name, url in [
        ("miit_search_v2", "https://www.miit.gov.cn/search-front-server/api/search/info?"
                           "searchWord=%E5%8D%8A%E5%AF%BC%E4%BD%93&dataTypeId=107&categoryId=234&pageSize=5&pageNo=1"),
        ("miit_search_v3", "https://www.miit.gov.cn/search-front-server/api/search/info?"
                           "searchWord=%E5%8D%8A%E5%AF%BC%E4%BD%93&siteId=34&dataTypeId=107&pageSize=5&pageNo=1"),
        ("miit_search_v4", "https://www.miit.gov.cn/search-front-server/api/search/info?"
                           "searchWord=%E5%8D%8A%E5%AF%BC%E4%BD%93&dataTypeId=107&regionCode=&pageSize=5&pageNo=1"),
    ]:
        try:
            r = client.get(url, headers=H)
            txt = r.text[:800]
            report[name] = {"status": r.status_code, "sample": txt}
            print(f"[{r.status_code}] {name}: {txt[:250]}\n")
        except Exception as exc:
            report[name] = {"error": str(exc)[:150]}
            print(f"[err] {name}: {exc}\n")

    # 2) 商务部：尝试 cms-api 常见路径（zj 系 CMS）
    for name, url in [
        ("mofcom_cms_api1", "https://www.mofcom.gov.cn/cms-api/news/list?channelId=xwfb&pageSize=10"),
        ("mofcom_cms_api2", "https://www.mofcom.gov.cn/jcms/api/news?siteId=1&pageSize=10"),
        ("mofcom_art_list", "https://www.mofcom.gov.cn/xwfb/rcxwfb/art/2026/index.html"),
        ("mofcom_jsonp", "https://www.mofcom.gov.cn/module/jslib/news/queryNewsList.jsp?channel=xwfb"),
    ]:
        try:
            r = client.get(url, headers=H)
            txt = r.text[:400]
            report[name] = {"status": r.status_code, "bytes": len(r.content), "sample": txt}
            print(f"[{r.status_code}] {name}: {txt[:250]}\n")
        except Exception as exc:
            report[name] = {"error": str(exc)[:150]}
            print(f"[err] {name}: {exc}\n")

    # 3) Micron：下载完整页面找 <script id="__NEXT_DATA__"> 或 data-* 属性
    r = client.get("https://www.micron.com/about/newsroom/press-releases", headers=H)
    text = r.text
    found = {}
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if m:
        found["next_data"] = m.group(1)[:400]
    # 查找所有 data- 属性中的 json
    data_attrs = re.findall(r'data-(?:props|state|config)=["\']([^"\']{100,500})', text)
    found["data_attrs"] = [d[:200] for d in data_attrs[:3]]
    # 查找 buildId / chunk 线索
    found["script_srcs"] = re.findall(r'src="(/[^"]+\.js)"', text)[:8]
    report["micron_deep"] = {"status": r.status_code, "bytes": len(r.content), **found}
    print(f"micron_deep: next_data={'yes' if 'next_data' in found else 'no'}, "
          f"scripts={found.get('script_srcs', [])[:4]}")

(OUT / "probe-round6.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[saved] {OUT / 'probe-round6.json'}")
