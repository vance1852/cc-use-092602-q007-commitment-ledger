"""贯通承诺版本、窗口、排除规则、家庭暂停、不可变事件与结算授权的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import InvalidState
from .service import CommitmentService


PROJECT = "new-town-north"
BATCH = "batch-2026-03"
COMMITMENT = "commit-north-03"


def _commitment_payload() -> dict[str, object]:
    return {
        "commitment_id": COMMITMENT,
        "project_id": PROJECT,
        "batch_id": BATCH,
        "terms": [
            {
                "kind": "formal_delivery",
                "start_event": "temporary_keys_handover",
                "end_event": "formal_delivery",
                "promised_days": 10,
            }
        ],
        "daily_compensation_cny": "50.00",
        "advance_notice_days": 5,
        "notes": "第三批次：临交后 10 日内正式交房",
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc))
    service = CommitmentService(connection, clock)
    for user_id, role in (
        ("ops", "resettlement"),
        ("manager", "resettlement_manager"),
        ("fin", "finance"),
        ("aud", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 承诺版本、统计窗口与排除规则。
    service.create_commitment("ops", _commitment_payload())
    service.add_window("ops", COMMITMENT, {"window_id": "w-09a", "starts_on": "2026-09-01", "ends_on": "2026-09-15"})
    service.add_window("ops", COMMITMENT, {"window_id": "w-09b", "starts_on": "2026-09-16", "ends_on": "2026-09-30"})
    for window_id in ("w-09a", "w-09b"):
        service.add_exclusion_rule("ops", {
            "rule_id": f"rule-{window_id}",
            "window_id": window_id,
            "event_kind": "construction_plan",
            "require_advance_notice": True,
            "reason": "提前 5 日告知且落在窗口内的集中施工可以排除",
        })

    # 两户家庭：9 月 1 日临交，9 月 20 日正式交房。
    for household in ("hh-01", "hh-02"):
        service.record_event("ops", PROJECT, BATCH, {
            "event_id": f"evt-handover-{household}",
            "event_kind": "temporary_keys_handover",
            "household_id": household,
            "event_date": "2026-09-01",
            "observed_at": "2026-09-01T09:00:00Z",
            "payload": {"site": "north"},
        })
        service.record_event("ops", PROJECT, BATCH, {
            "event_id": f"evt-delivery-{household}",
            "event_kind": "formal_delivery",
            "household_id": household,
            "event_date": "2026-09-20",
            "observed_at": "2026-09-20T10:00:00Z",
            "payload": {"building": "b3"},
        })

    # 跨窗口的计划施工：9/14-9/17，提前 7 天告知，应精确拆分到两个窗口。
    service.record_event("ops", PROJECT, BATCH, {
        "event_id": "evt-construction-cross",
        "event_kind": "construction_plan",
        "household_id": "*",
        "event_date": "2026-09-14",
        "observed_at": "2026-09-14T08:00:00Z",
        "notice_at": "2026-09-07T08:00:00Z",
        "payload": {"starts_on": "2026-09-14", "ends_on": "2026-09-17", "scope": "batch"},
    })
    # 未提前告知的施工：片段必须保留但不得排除。
    service.record_event("ops", PROJECT, BATCH, {
        "event_id": "evt-construction-no-notice",
        "event_kind": "construction_plan",
        "household_id": "*",
        "event_date": "2026-09-08",
        "observed_at": "2026-09-08T08:00:00Z",
        "payload": {"starts_on": "2026-09-08", "ends_on": "2026-09-08", "scope": "batch"},
    })

    # hh-02 家庭主动申请延期暂停 8 天（已家庭确认）。
    service.register_pause("ops", COMMITMENT, {
        "pause_id": "pause-hh02-extension",
        "household_id": "hh-02",
        "starts_on": "2026-09-05",
        "ends_on": "2026-09-12",
        "reason_code": "family_extension",
        "evidence": "家庭签字延期申请书 scan-2026-09-04",
        "confirmed_by_household": True,
    })

    # 第一窗口结算：编制 -> 运营批准 -> 财务确认，三段授权彼此分离。
    first = service.prepare_settlement("ops", COMMITMENT, "w-09a", "2026-09-30")
    try:
        service.approve_operations("ops", first["settlement_id"])
    except Exception as exc:  # 编制人不能批准自己的结算
        self_approval_rejected = str(exc)
    else:  # pragma: no cover
        self_approval_rejected = ""
    service.approve_operations("manager", first["settlement_id"])
    service.confirm_finance("fin", first["settlement_id"])

    # 迟到事件不得直接改写已确认结算。
    service.record_event("ops", PROJECT, BATCH, {
        "event_id": "evt-construction-late-reported",
        "event_kind": "construction_plan",
        "household_id": "*",
        "event_date": "2026-09-10",
        "observed_at": "2026-09-10T08:00:00Z",
        "notice_at": "2026-09-01T08:00:00Z",
        "payload": {"starts_on": "2026-09-10", "ends_on": "2026-09-10", "scope": "batch"},
    })
    clock.advance(days=3)
    try:
        service.prepare_settlement("ops", COMMITMENT, "w-09a", "2026-09-30")
    except InvalidState as exc:
        rewrite_rejected = str(exc)
    else:  # pragma: no cover
        rewrite_rejected = ""
    corrected = service.prepare_settlement(
        "ops", COMMITMENT, "w-09a", "2026-09-30",
        correction_reason="补录 9 月 10 日提前告知的管线施工日，按迟到事件纠错流程出具新版",
    )
    service.approve_operations("manager", corrected["settlement_id"])
    service.confirm_finance("fin", corrected["settlement_id"])

    second_window = service.prepare_settlement("ops", COMMITMENT, "w-09b", "2026-09-30")
    service.approve_operations("manager", second_window["settlement_id"])
    service.confirm_finance("fin", second_window["settlement_id"])

    replay_v1 = service.replay_settlement("aud", first["settlement_id"])
    replay_v2 = service.replay_settlement("aud", corrected["settlement_id"])
    recomputed = service.recompute_project_batch("aud", PROJECT, BATCH, "2026-09-30")

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "self_approval_rejected": self_approval_rejected,
        "rewrite_rejected": rewrite_rejected,
        "first_window_totals": first["result"]["window_totals"],
        "corrected_window_totals": corrected["result"]["window_totals"],
        "correction_diff": corrected["diff"],
        "second_window_totals": second_window["result"]["window_totals"],
        "hh01_fragments_first_version": [
            term["fragments"]
            for household in first["result"]["households"]
            if household["household_id"] == "hh-01"
            for term in household["terms"]
        ][0],
        "replay": {"v1": replay_v1, "v2": replay_v2},
        "recompute": recomputed,
        "audit": service.audit_chain("aud"),
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行安置承诺核算服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
