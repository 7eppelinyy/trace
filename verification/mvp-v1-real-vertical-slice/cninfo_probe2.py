"""深度探测巨潮公告字段结构与 column 参数行为。"""
from __future__ import annotations
import json
import httpx

UA = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}


def top(code):
    r = httpx.post("http://www.cninfo.com.cn/new/information/topSearch/query",
                   data={"keyWord": code, "maxNum": "10"}, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def query(code, org_id, column, page_size=5):
    r = httpx.post("http://www.cninfo.com.cn/new/hisAnnouncement/query",
                   data={
                       "pageNum": "1", "pageSize": str(page_size), "column": column,
                       "tabName": "fulltext", "plate": "", "stock": f"{code},{org_id}",
                       "searchkey": "", "secid": "", "category": "", "trade": "",
                       "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true",
                   }, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


for code, mkt in [("688981", "sse"), ("000977", "szse")]:
    print(f"\n===== {code} =====")
    t = top(code)
    org = t[0]["orgId"] if t else None
    print("topSearch:", json.dumps(t[0] if t else {}, ensure_ascii=False))
    for col in (mkt,):
        try:
            d = query(code, org, col)
            anns = d.get("announcements") or []
            print(f"column={col} total={d.get('totalAnnouncement')} got={len(anns)}")
            if anns:
                print("FIELD_KEYS:", sorted(anns[0].keys()))
                print("SAMPLE:", json.dumps(anns[0], ensure_ascii=False)[:900])
        except Exception as e:
            print(f"column={col} FAILED: {type(e).__name__} {e}")
