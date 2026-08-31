"""证据生成：从真实运行数据库中导出美股/A股真实事件证据并渲染预警。

输出到 verification/mvp-v1-real-vertical-slice/：
    us-real-event.json / cn-real-event.json
    us-rendered-alert.txt / cn-rendered-alert.txt
    source-health.json / duplicate-run-proof.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trace.app import create_app
from trace.alerts.template import AlertRenderer
from trace.db.repositories import (
    EventImpactRepo, EventRepo, EventSourceRepo, RawItemRepo,
    SecurityRepo, SourceRepo,
)

OUT = Path(__file__).parent


def event_payload(app, event_id: str) -> dict:
    db = app.db
    ev = EventRepo(db).get(event_id)
    impacts = [i for i in EventImpactRepo(db).list_by_event(event_id)]
    evidence = []
    for es in EventSourceRepo(db).list_by_event(event_id):
        item = RawItemRepo(db).get(es.raw_item_id)
        if item:
            evidence.append({
                "raw_item_id": item.raw_item_id,
                "source_id": item.source_id,
                "source_item_id": item.source_item_id,
                "title": item.title,
                "url": item.url,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "reference": item.reference,
                "role": es.role,
            })
    sec_repo = SecurityRepo(db)
    src_repo = SourceRepo(db)
    first_src = src_repo.get(ev.first_source_id or "")
    return {
        "event": {
            "event_id": ev.event_id,
            "title": ev.title,
            "summary": ev.summary,
            "event_type": ev.event_type,
            "status": ev.status,
            "version": ev.version,
            "event_time": ev.event_time.isoformat() if ev.event_time else None,
            "first_source_id": ev.first_source_id,
            "primary_source_id": ev.primary_source_id,
            "first_source_name": first_src.source_name if first_src else None,
        },
        "evidence": evidence,
        "impacts": [
            (lambda s: {
                "security_id": i.security_id,
                "ticker": s.ticker if s else None,
                "market": s.market if s else None,
                "direction": i.direction,
                "directness": i.directness,
                "magnitude": i.magnitude,
                "persistence": i.persistence,
                "confidence": i.confidence,
                "source_reliability": i.source_reliability,
                "base_score": i.base_score,
                "market_confirmation": i.market_confirmation,
                "final_score": i.final_score,
                "reason": i.reason,
                "industry_path": i.industry_path,
                "evidence_ids": i.evidence_ids,
                "analysis_mode": i.analysis_mode,
                "market_data_mode": i.market_data_mode,
            })(sec_repo.get(i.security_id))
            for i in impacts
        ],
    }


def pick_events(app):
    """选择案例A（美股官方来源事件）与案例B（巨潮/上交所/深交所公告）。"""
    db = app.db
    rows = db.query("""
        SELECT e.event_id, e.title, e.first_source_id, s.market, s.ticker,
               ei.final_score, ei.direction
        FROM event_impact ei
        JOIN event e ON ei.event_id = e.event_id
        JOIN security s ON ei.security_id = s.security_id
    """)
    us_sources = {"src_sec_edgar", "src_nvidia_ir", "src_micron_ir", "src_sndk_ir"}
    cn_sources = {"src_cninfo", "src_sse", "src_szse"}
    us_watch = {"SNDK", "MU", "NVDA"}
    us, cn = None, None
    for r in sorted(rows, key=lambda r: r["final_score"], reverse=True):
        if us is None and r["first_source_id"] in us_sources \
                and r["ticker"] in us_watch:
            us = r["event_id"]
        if cn is None and r["first_source_id"] in cn_sources \
                and r["ticker"].endswith((".SH", ".SZ")):
            cn = r["event_id"]
        if us and cn:
            break
    return us, cn


def main():
    app = create_app()
    renderer = AlertRenderer(app.db)
    impact_repo = EventImpactRepo(app.db)
    ev_repo = EventRepo(app.db)

    us_id, cn_id = pick_events(app)
    assert us_id, "no US event with impacts found"
    assert cn_id, "no CN event with impacts found"

    for tag, event_id in (("us", us_id), ("cn", cn_id)):
        payload = event_payload(app, event_id)
        (OUT / f"{tag}-real-event.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        ev = ev_repo.get(event_id)
        impacts = impact_repo.list_by_event(event_id)
        # 优先渲染事件标题中直接提及的证券（案例主体），其次分数最高
        sec_repo = SecurityRepo(app.db)

        def rank(i):
            sec = sec_repo.get(i.security_id)
            mentioned = bool(sec and sec.ticker in ev.title)
            return (mentioned, i.final_score)

        imp = max(impacts, key=rank)
        text, url = renderer.render(ev, imp, "Asia/Taipei")
        rendered = text + (f"\n\n🔗 原文: {url}" if url else "")
        (OUT / f"{tag}-rendered-alert.txt").write_text(rendered, encoding="utf-8")
        print(f"[{tag}] event={event_id} title={ev.title!r} "
              f"top_impact={imp.final_score:.2f} rendered={len(text)} chars")

    # source_health.json
    from trace.db.health import SourceHealthRepo
    health_rows = SourceHealthRepo(app.db).all()
    (OUT / "source-health.json").write_text(
        json.dumps(health_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"source-health: {len(health_rows)} sources")


if __name__ == "__main__":
    main()
