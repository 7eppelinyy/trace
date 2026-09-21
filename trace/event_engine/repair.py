"""一致性与遗留孤立数据修复扫描器 (T03 / F03 / F11)。

检查并修复：
1. 悬空的人工检查记录 (dangling human review): raw_item_id 无法关联到实体 raw_item。
   -> 标记为 legacy_payload_missing，禁止伪造补齐。
2. 无 analysis 的 event: 已建立 Event 但没有任何 processing_job 或 impact 分析。
   -> 补充 stage_b_analyze 待处理作业。
3. 孤立 raw item: 已入库但无 stage_a 状态追踪。
   -> 补充 stage_a_extract 状态（若已关联 event 则 completed，否则 pending）。
"""

from __future__ import annotations

from typing import Any
from trace.db.connection import Database
from trace.db.repositories import ProcessingJobRepo


def scan_and_repair_inconsistencies(db: Database, dry_run: bool = True) -> dict[str, Any]:
    """执行一致性检测与修复扫描。"""
    report = {
        "dry_run": dry_run,
        "dangling_reviews": [],
        "unanalyzed_events": [],
        "orphan_raw_items": [],
        "actions_taken": [],
    }

    # 1. 悬空人审记录检测
    dangling = db.query(
        """SELECT h.review_id, h.raw_item_id, h.reason
           FROM human_review h
           LEFT JOIN raw_item r ON h.raw_item_id = r.raw_item_id
           WHERE h.raw_item_id IS NOT NULL AND r.raw_item_id IS NULL"""
    )
    for row in dangling:
        report["dangling_reviews"].append(dict(row))
        if not dry_run:
            # 无法恢复原文的旧 review 标 legacy_payload_missing，禁止伪造补齐
            new_reason = (row["reason"] or "") + " [legacy_payload_missing]"
            db.execute(
                "UPDATE human_review SET reason = ? WHERE review_id = ?",
                (new_reason[:500], row["review_id"]),
            )
            report["actions_taken"].append(f"marked human_review {row['review_id']} as legacy_payload_missing")

    # 2. 无 analysis 的 event
    unanalyzed = db.query(
        """SELECT e.event_id, e.title
           FROM event e
           LEFT JOIN processing_job j ON j.job_type='stage_b_analyze' AND j.target_id=e.event_id
           LEFT JOIN event_impact i ON i.event_id=e.event_id
           WHERE j.job_id IS NULL AND i.impact_id IS NULL"""
    )
    job_repo = ProcessingJobRepo(db)
    for row in unanalyzed:
        report["unanalyzed_events"].append(dict(row))
        if not dry_run:
            job_repo.create_or_update(
                job_type="stage_b_analyze",
                target_id=row["event_id"],
                status="pending",
            )
            report["actions_taken"].append(f"created stage_b_analyze job for unanalyzed event {row['event_id']}")

    # 3. 孤立 raw item
    untracked_raw = db.query(
        """SELECT r.raw_item_id, r.event_id
           FROM raw_item r
           LEFT JOIN processing_job j ON j.job_type='stage_a_extract' AND j.target_id=r.raw_item_id
           WHERE j.job_id IS NULL"""
    )
    for row in untracked_raw:
        report["orphan_raw_items"].append(dict(row))
        if not dry_run:
            st = "completed" if row["event_id"] else "pending"
            job_repo.create_or_update(
                job_type="stage_a_extract",
                target_id=row["raw_item_id"],
                status=st,
            )
            report["actions_taken"].append(f"created stage_a_extract ({st}) for raw {row['raw_item_id']}")

    return report
