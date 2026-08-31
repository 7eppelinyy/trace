"""SanDisk 直连入口探测：www.sandisk.com/company/newsroom（JS 渲染页）。

目标：寻找无需代理、无第三方转载的官方一手入口。
"""
import httpx
import re
import json

H = {"User-Agent": "TraceEventRadar/0.2 research@example.com"}
OUT = {}

def get(url, timeout=20):
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True, headers=H)
        return r.status_code, r.text, str(r.url)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", url

# 1. newsroom 页面：找 script bundle / JSON 端点
st, body, final = get("https://www.sandisk.com/company/newsroom")
OUT["newsroom_status"] = st
OUT["newsroom_final_url"] = final
if st == 200:
    OUT["scripts"] = re.findall(r'<script[^>]*src="([^"]+)"', body)[:20]
    OUT["api_like_refs"] = sorted(set(re.findall(
        r'["\']((?:https?://[^"\']*|/)[^"\']*(?:api|json|feed|rss|\.jsp|graphql)[^"\']*)["\']',
        body, re.I)))[:40]
    OUT["next_data"] = "__NEXT_DATA__" in body
    OUT["body_len"] = len(body)

# 2. robots.txt / sitemap（官方主站）
for key, u in [("robots", "https://www.sandisk.com/robots.txt"),
               ("sitemap", "https://www.sandisk.com/sitemap.xml")]:
    st2, body2, _ = get(u, timeout=15)
    OUT[key] = {"status": st2, "sample": body2[:800] if st2 == 200 else body2}

# 3. 常见 press-release JSON API 猜测（Q4 平台 / investor 平台通用模式）
candidates = [
    "https://www.sandisk.com/api/newsroom/articles",
    "https://www.sandisk.com/api/v1/newsroom",
    "https://www.sandisk.com/company/newsroom.json",
    "https://investor.sandisk.com/rss/news-releases.xml",
    "https://investor.sandisk.com/rss.xml",
]
OUT["candidates"] = {}
for u in candidates:
    st3, body3, final3 = get(u, timeout=12)
    OUT["candidates"][u] = {"status": st3,
                            "sample": (body3[:300] if st3 == 200 else body3),
                            "final": final3}

with open("verification/source-completion/probe-sandisk-direct.json", "w",
          encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)
print(json.dumps({k: (v if not isinstance(v, str) else v[:200])
                  for k, v in OUT.items() if k != "candidates"},
                 ensure_ascii=False, indent=2))
print(json.dumps(OUT.get("candidates", {}), ensure_ascii=False, indent=2)[:2000])
