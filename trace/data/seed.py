"""种子数据加载：把 yaml 配置写入数据库（幂等，可重复执行）。

A股产业映射等所有领域数据都在配置文件中，业务代码不硬编码。
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from trace.db.connection import Database
from trace.db.repositories import (
    EntityAliasRepo,
    IndustryEdgeRepo,
    SecurityRepo,
    SourceRepo,
)
from trace.domain.models import EntityAlias, IndustryEdge, LicenseMode, Security, Source

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent


def _load_yaml(name: str) -> dict:
    with open(DATA_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_all_seeds(db: Database) -> None:
    # 单事务批量提交：逐条 commit 在 Windows/网络盘上 fsync 开销显著
    with db.transaction():
        _seed_sources(db)
        _seed_securities(db)
        _seed_entities(db)
        _seed_edges(db)
    logger.info("seed data loaded")


def _seed_sources(db: Database) -> None:
    repo = SourceRepo(db)
    for s in _load_yaml("seed_sources.yaml").get("sources", []):
        repo.upsert(Source(
            source_id=s["source_id"],
            source_name=s["source_name"],
            source_type=s["source_type"],
            priority=int(s.get("priority", 5)),
            base_reliability=float(s.get("base_reliability", 5.0)),
            license_mode=LicenseMode(s.get("license_mode", "unknown")),
            retention_policy=s.get("retention_policy", "default"),
            enabled=bool(s.get("enabled", False)),
            authority_level=s.get("authority_level", ""),
            poll_interval_seconds=int(s.get("poll_interval_seconds", 600)),
            security_map=s.get("security_map", []),
            can_fetch=bool(s.get("can_fetch", True)),
            can_store=bool(s.get("can_store", True)),
            can_display=bool(s.get("can_display", True)),
            can_forward=bool(s.get("can_forward", True)),
            verified_at=s.get("verified_at"),
            verified_by=s.get("verified_by"),
        ))


def _security_id(ticker: str, market: str) -> str:
    return f"SEC-{market}-{ticker}"


def _seed_securities(db: Database) -> None:
    repo = SecurityRepo(db)
    for s in _load_yaml("seed_securities.yaml").get("securities", []):
        ticker, market = s["ticker"], s["market"]
        repo.upsert(Security(
            security_id=_security_id(ticker, market),
            market=market,
            exchange=s.get("exchange", ""),
            ticker=ticker,
            company_name_zh=s.get("company_name_zh", ""),
            company_name_en=s.get("company_name_en", ""),
            cik=s.get("cik"),
            aliases=s.get("aliases", []),
            products=s.get("products", []),
            industry_tags=s.get("industry_tags", []),
            graph_node_ids=s.get("graph_node_ids", []),
            is_watchlist_default=bool(s.get("is_watchlist_default", False)),
            is_context_universe=bool(s.get("is_context_universe", False)),
            status=s.get("status", "verified"),
        ))


def _seed_entities(db: Database) -> None:
    repo = EntityAliasRepo(db)
    for e in _load_yaml("seed_entities.yaml").get("entities", []):
        repo.upsert(EntityAlias(
            entity_id=e["entity_id"],
            name=e["name"],
            aliases=e.get("aliases", []),
            entity_type=e.get("entity_type", "company"),
            security_id=e.get("security_id"),
        ))


def _seed_edges(db: Database) -> None:
    repo = IndustryEdgeRepo(db)
    for i, e in enumerate(_load_yaml("seed_edges.yaml").get("edges", [])):
        repo.upsert(IndustryEdge(
            edge_id=f"EDGE-{e['from_node']}-{e['to_node']}-{e['edge_type']}",
            from_node=e["from_node"],
            to_node=e["to_node"],
            edge_type=e["edge_type"],
            confidence=float(e.get("confidence", 0.8)),
            evidence_source=e.get("evidence_source", ""),
            direction_rule=e.get("direction_rule", ""),
        ))
