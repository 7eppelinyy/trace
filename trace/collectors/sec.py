"""SEC EDGAR Collector（真实，P0）。

SEC 请求规范：
    - User-Agent: "公司名 联系邮箱"（公平访问要求）
    - 请求频率限制（<= 5 req/s，保守取 2 req/s）
    - timeout / retry / 指数退避（HttpClient）
    - 增量游标：按已处理的 accession_number 去重，不重复下载
    - 保存 accession number / filing URL / filed_at / form type

关注类型：8-K / 10-Q / 10-K（6-K 为未来扩展保留）。
注意：filing 元数据作为结构化公告事件处理，
不得把完整 filing 当作普通新闻正文抓取。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from trace.collectors.base import BaseCollector
from trace.common.hashing import canonical_url, content_hash, title_hash
from trace.common.http_client import HttpClient, ParseError
from trace.common.ids import raw_item_id
from trace.db.repositories import SecurityRepo
from trace.domain.models import RawItem

logger = logging.getLogger(__name__)

EDGAR_SUBMISSIONS_API = "https://data.sec.gov/submissions/CIK{cik}.json"
EDGAR_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# 8-K/10-Q/10-K 为核心关注类型；6-K 为未来扩展保留
_WATCHED_FORMS = {"8-K", "10-Q", "10-K", "8-K/A", "10-Q/A", "10-K/A", "6-K"}


class SECCollector(BaseCollector):
    collector_type = "sec"

    @property
    def handled_source_ids(self) -> set[str]:
        return {"src_sec_edgar"}

    # ------------------------------------------------------------------
    def collect(self) -> list[RawItem]:
        http = self.http_client(
            "src_sec_edgar",
            rate_limit_qps=2.0,          # SEC 公平访问：<=10 req/s，保守取 2
            headers={"User-Agent": self.sec_user_agent},
        )
        try:
            cursor = self.cursor_repo.get("src_sec_edgar")
            seen: set[str] = set(cursor.get("seen_accessions", []))

            repo = SecurityRepo(self.db)
            targets = [s for s in repo.list_all()
                       if s.market == "US" and s.cik and s.is_watchlist_default]
            if not targets:
                raise ParseError("no US watchlist securities with CIK")

            items: list[RawItem] = []
            for sec in targets:
                items.extend(self._collect_one(http, sec.cik, sec.ticker, seen))

            # 更新游标（保留最近 2000 条防膨胀）
            self.cursor_repo.set("src_sec_edgar", {
                "seen_accessions": sorted(seen)[-2000:],
            })
            return items
        finally:
            http.close()

    # ------------------------------------------------------------------
    def _collect_one(self, http: HttpClient, cik: str, ticker: str,
                     seen: set[str]) -> list[RawItem]:
        cik10 = cik.zfill(10)
        url = EDGAR_SUBMISSIONS_API.format(cik=cik10)
        resp = http.get(url)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ParseError(f"EDGAR JSON parse failed for {ticker}: {exc}") from exc

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        descriptions = recent.get("primaryDocDescription", [])
        if not forms:
            raise ParseError(f"EDGAR submissions structure changed for {ticker}")

        # 归档 URL 使用 EDGAR 响应中的申报人 CIK（权威值），
        # 而非请求时使用的 CIK，避免补零/前导零差异导致链接错误。
        resp_cik = str(data.get("cik") or cik.lstrip("0") or cik)

        items: list[RawItem] = []
        for i, form in enumerate(forms[:50]):
            if form not in _WATCHED_FORMS:
                continue
            accession = accessions[i]
            if accession in seen:
                continue  # 增量游标：不重复处理已采集的 filing
            seen.add(accession)

            doc = primary_docs[i] if i < len(primary_docs) else ""
            item_url = (f"{EDGAR_ARCHIVES_BASE}/{resp_cik.lstrip('0')}/"
                        f"{accession.replace('-', '')}/{doc}")
            desc = descriptions[i] if i < len(descriptions) else ""
            title = f"{ticker} {form}: {desc}".strip().rstrip(":")
            filed_at = None
            try:
                filed_at = datetime.fromisoformat(filing_dates[i] + "T00:00:00+00:00")
            except (IndexError, ValueError):
                pass

            # filing 是结构化公告：reference 保存关键元数据，不抓全文
            reference = (f"SEC form={form} accession={accession} cik={cik} "
                         f"filed_at={filing_dates[i] if i < len(filing_dates) else ''}")
            items.append(RawItem(
                raw_item_id=raw_item_id(),
                source_id="src_sec_edgar",
                source_item_id=f"edgar:{accession}",
                title=title,
                url=item_url,
                canonical_url=canonical_url(item_url),
                published_at=filed_at,
                fetched_at=datetime.now(timezone.utc),
                language="en",
                content=None,
                reference=reference,
                title_hash=title_hash(title),
                content_hash=content_hash(reference),
            ))
        return items
