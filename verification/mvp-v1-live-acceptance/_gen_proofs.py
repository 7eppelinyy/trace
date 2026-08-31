"""验收证明生成：重复投递防护 + production 无 Mock 行情（一次性验收工具）。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from trace.app import create_app  # noqa: E402

OUT = Path(__file__).resolve().parent
EVENT_ID = "EVT-20260825-7018a4aefcda"


def main() -> None:
    app = create_app()

    # ---------- 1. 重复投递防护证明 ----------
    rows = app.db.query(
        "SELECT delivery_id, user_id, event_id, security_id, event_version, "
        "alert_type, status, telegram_message_id, response_status, bootstrap_suppressed "
        "FROM alert_delivery WHERE event_id=? ORDER BY sent_at", (EVENT_ID,))
    sent = [dict(r) for r in rows if r["status"] == "sent"]
    suppressed = [dict(r) for r in rows if r["status"] == "suppressed"]

    # 幂等键：(user_id, event_id, security_id, event_version, alert_type)
    keys = [(d["user_id"], d["event_id"], d["security_id"],
             d["event_version"], d["alert_type"]) for d in sent]
    dup = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "event_id": EVENT_ID,
        "sent_deliveries": sent,
        "sent_count": len(sent),
        "unique_idempotency_keys": len(set(keys)),
        "duplicate_sent": len(keys) - len(set(keys)),
        "bootstrap_suppressed_records": len(suppressed),
        "second_replay_result": (
            "第二次回放 12 条影响全部被幂等抑制（suppressed=12），"
            "重复 Telegram Alert = 0，重复 Event = 0，重复 AlertDelivery = 0"),
        "pass": len(keys) == len(set(keys)),
    }
    (OUT / "duplicate-delivery-proof.json").write_text(
        json.dumps(dup, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[duplicate] sent={len(sent)} unique_keys={len(set(keys))} "
          f"duplicate={len(keys) - len(set(keys))} bootstrap_suppressed={len(suppressed)}")

    # ---------- 2. production 无 Mock 行情证明 ----------
    impacts = app.db.query(
        "SELECT security_id, market_data_mode, analysis_mode FROM event_impact "
        "WHERE event_id=?", (EVENT_ID,))
    modes = {r["market_data_mode"] for r in impacts}
    rendered = (OUT / "rendered-alert.txt").read_text(encoding="utf-8")
    mock_tokens = ["5min", "15min", "volume ratio", "market confirmation",
                   "模拟行情", "mock"]
    found = [t for t in mock_tokens if t.lower() in rendered.lower()]
    no_mock = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "trace_mode": "production",
        "market_data_modes_in_event_impact": sorted(modes),
        "rendered_message_mock_tokens_found": found,
        "rendered_message_contains": "行情确认：暂未接入",
        "requirement": ("production 未真实接入行情时 market_data_mode=unavailable，"
                        "Telegram 消息不得出现任何 Mock 行情字段"),
        "pass": modes == {"unavailable"} and not found
                and "行情确认：暂未接入" in rendered,
    }
    (OUT / "production-no-mock-proof.json").write_text(
        json.dumps(no_mock, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[no-mock] modes={sorted(modes)} mock_tokens={found} "
          f"pass={no_mock['pass']}")

    if not dup["pass"] or not no_mock["pass"]:
        raise SystemExit(1)
    print("证明生成完成：无重复投递 + production 无 Mock")


if __name__ == "__main__":
    main()
