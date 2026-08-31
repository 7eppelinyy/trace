"""真实数据源烟雾探测（生成 live-source-smoke.json）。

只探测官方公开端点，不写入数据库。输出不含任何凭据。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

SEC_UA = "TraceEventRadar research zeppelin@example.com"
OUT = Path(__file__).parent / "live-source-smoke.json"

results: dict = {"probed_at": datetime.now(timezone.utc).isoformat(), "checks": []}


def check(name: str, fn):
    t0 = time.monotonic()
    try:
        detail = fn()
        results["checks"].append({
            "name": name, "ok": True,
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
            "detail": detail,
        })
        print(f"[OK] {name}: {detail}")
    except Exception as exc:
        results["checks"].append({
            "name": name, "ok": False,
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        })
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")


def probe_sec_authority():
    r = httpx.get("https://www.sec.gov/files/company_tickers.json",
                  headers={"User-Agent": SEC_UA}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    by_ticker = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in data.values()}
    return {
        "total_companies": len(data),
        "SNDK": by_ticker.get("SNDK"),
        "MU": by_ticker.get("MU"),
        "NVDA": by_ticker.get("NVDA"),
    }


def probe_edgar(cik: str, ticker: str):
    r = httpx.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                  headers={"User-Agent": SEC_UA}, timeout=30)
    r.raise_for_status()
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    return {
        "name": data.get("name"),
        "recent_filings": len(forms),
        "latest_form": forms[0] if forms else None,
        "latest_date": (recent.get("filingDate") or [None])[0],
    }


def probe_nvda_rss():
    r = httpx.get("https://nvidianews.nvidia.com/rss.xml",
                  headers={"User-Agent": "TraceEventRadar/0.2 research"},
                  timeout=30, follow_redirects=True)
    r.raise_for_status()
    body = r.text
    n_items = body.count("<item>")
    first_title = ""
    if "<title>" in body:
        parts = body.split("<item>", 1)
        if len(parts) > 1 and "<title>" in parts[1]:
            seg = parts[1].split("<title>", 1)[1].split("</title>", 1)[0]
            first_title = seg.strip()[:120]
    return {"items": n_items, "first_title": first_title}


def probe_micron_ir():
    candidates = [
        "https://investors.micron.com/rss.xml",
        "https://investors.micron.com/rss/news-releases.xml",
        "https://www.micron.com/about/investors/rss",
    ]
    tried = []
    for url in candidates:
        try:
            r = httpx.get(url, headers={"User-Agent": "TraceEventRadar/0.2 research"},
                          timeout=15, follow_redirects=True)
            tried.append({"url": url, "status": r.status_code})
            if r.status_code == 200 and ("<item>" in r.text or "<entry>" in r.text):
                return {"working_url": url, "items": r.text.count("<item>") or r.text.count("<entry>")}
        except Exception as exc:
            tried.append({"url": url, "error": type(exc).__name__})
    raise RuntimeError(f"no working Micron RSS endpoint; tried={tried}")


def probe_cninfo_topsearch(code: str):
    r = httpx.post(
        "http://www.cninfo.com.cn/new/information/topSearch/query",
        data={"keyWord": code, "maxNum": "10"},
        headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list) and data:
        row = data[0]
        return {"code": row.get("code"), "zwjc": row.get("zwjc"),
                "orgId": row.get("orgId"), "category": row.get("category")}
    raise RuntimeError(f"topSearch returned no result for {code}: {data}")


def probe_cninfo_announcements(code: str, org_id: str):
    r = httpx.post(
        "http://www.cninfo.com.cn/new/hisAnnouncement/query",
        data={
            "pageNum": "1", "pageSize": "10", "column": "szse", "tabName": "fulltext",
            "plate": "", "stock": f"{code},{org_id}", "searchkey": "", "secid": "",
            "category": "", "trade": "", "seDate": "", "sortName": "", "sortType": "",
            "isHLtitle": "true",
        },
        headers={"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    anns = data.get("announcements") or []
    first = anns[0] if anns else {}
    return {
        "total_announcements": data.get("totalAnnouncement"),
        "first_title": (first.get("announcementTitle") or "")[:80],
        "first_id": first.get("announcementId"),
    }


if __name__ == "__main__":
    check("sec_company_tickers_authority", probe_sec_authority)
    check("sec_edgar_SNDK_2023554", lambda: probe_edgar("0002023554", "SNDK"))
    check("sec_edgar_MU_723125", lambda: probe_edgar("0000723125", "MU"))
    check("sec_edgar_NVDA_1045810", lambda: probe_edgar("0001045810", "NVDA"))
    check("nvda_official_rss", probe_nvda_rss)
    check("micron_ir_rss", probe_micron_ir)

    # 巨潮：先探一家沪市（中芯国际 688981）一家深市（浪潮信息 000977 实际是深市 000977.SZ）
    try:
        check("cninfo_topSearch_688981", lambda: probe_cninfo_topsearch("688981"))
        org = probe_cninfo_topsearch("688981")
        check("cninfo_announcements_688981",
              lambda: probe_cninfo_announcements("688981", org["orgId"]))
    except Exception as exc:
        results["checks"].append({"name": "cninfo_688981", "ok": False,
                                  "error": str(exc)})

    try:
        check("cninfo_topSearch_000977", lambda: probe_cninfo_topsearch("000977"))
        org2 = probe_cninfo_topsearch("000977")
        check("cninfo_announcements_000977",
              lambda: probe_cninfo_announcements("000977", org2["orgId"]))
    except Exception as exc:
        results["checks"].append({"name": "cninfo_000977", "ok": False,
                                  "error": str(exc)})

    OUT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = all(c.get("ok") for c in results["checks"])
    print(f"\nresult written to {OUT}")
    sys.exit(0 if ok else 1)
