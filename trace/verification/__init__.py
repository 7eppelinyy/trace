"""SEC 公司代码权威核验。

SEC company tickers authority：
    https://www.sec.gov/files/company_tickers.json

流程：
    官方 ticker→CIK 权威表
    → 自动匹配本地 Security Master 的 ticker
    → 核验本地 CIK / 公司名称
    → 不一致时给出明确错误

本模块只负责核验与报告，不得在运行中偷偷覆盖用户数据。
修复由独立命令 `python -m trace.main verify-securities` 显式执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC 公平访问要求：User-Agent 必须是 "公司名 联系邮箱"（默认值，可被配置覆盖）
SEC_USER_AGENT = "TraceEventRadar research research@example.com"


@dataclass
class AuthorityRecord:
    cik: str                 # 补零到 10 位
    ticker: str
    title: str


@dataclass
class VerifyResult:
    ticker: str
    local_cik: str | None
    official_cik: str | None
    local_name_en: str
    official_title: str | None
    status: str              # MATCH / MISMATCH / NOT_FOUND_OFFICIAL / NOT_FOUND_LOCAL
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "MATCH"


class SECAuthority:
    """从 SEC 官方拉取并缓存 ticker→CIK 权威表。"""

    def __init__(self, cache_path=None, user_agent: str | None = None):
        self.cache_path = cache_path
        self.user_agent = user_agent or SEC_USER_AGENT
        self._records: dict[str, AuthorityRecord] = {}

    def fetch(self, timeout: float = 30.0) -> dict[str, AuthorityRecord]:
        """拉取官方权威表（按 ticker 索引）。失败抛异常，绝不静默返回空表。"""
        try:
            resp = httpx.get(COMPANY_TICKERS_URL,
                             headers={"User-Agent": self.user_agent},
                             timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"SEC authority fetch failed: {exc}") from exc

        records: dict[str, AuthorityRecord] = {}
        for row in data.values():
            rec = AuthorityRecord(
                cik=str(row["cik_str"]).zfill(10),
                ticker=str(row["ticker"]).upper(),
                title=str(row["title"]),
            )
            records[rec.ticker] = rec
        if not records:
            raise RuntimeError("SEC authority returned empty table")
        self._records = records
        logger.info("SEC authority loaded: %d companies", len(records))
        return records

    def get(self, ticker: str) -> AuthorityRecord | None:
        return self._records.get(ticker.upper())

    @property
    def records(self) -> dict[str, AuthorityRecord]:
        return self._records


def verify_securities(securities, authority: SECAuthority) -> list[VerifyResult]:
    """核验本地美股 Security 与官方权威表的一致性。"""
    results: list[VerifyResult] = []
    for sec in securities:
        if sec.market != "US":
            continue
        official = authority.get(sec.ticker)
        if official is None:
            results.append(VerifyResult(
                ticker=sec.ticker, local_cik=sec.cik, official_cik=None,
                local_name_en=sec.company_name_en, official_title=None,
                status="NOT_FOUND_OFFICIAL",
                detail=f"官方权威表中不存在 {sec.ticker}"))
            continue
        local_cik = (sec.cik or "").zfill(10) if sec.cik else None
        if local_cik is None:
            results.append(VerifyResult(
                ticker=sec.ticker, local_cik=None, official_cik=official.cik,
                local_name_en=sec.company_name_en, official_title=official.title,
                status="CIK_NOT_SET",
                detail=f"本地未登记 CIK（官方 CIK={official.cik}，可补录）"))
            continue
        if local_cik == official.cik:
            results.append(VerifyResult(
                ticker=sec.ticker, local_cik=local_cik, official_cik=official.cik,
                local_name_en=sec.company_name_en, official_title=official.title,
                status="MATCH"))
        else:
            results.append(VerifyResult(
                ticker=sec.ticker, local_cik=local_cik, official_cik=official.cik,
                local_name_en=sec.company_name_en, official_title=official.title,
                status="MISMATCH",
                detail=f"本地 CIK={local_cik} 与官方 CIK={official.cik} 不一致"))
    return results
