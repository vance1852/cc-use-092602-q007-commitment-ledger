"""贯通承诺版本、不可变事件、窗口排除、周期结算、纠错新版本、分离授权
与离线复算的安置承诺核算离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import CommitmentService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
    service = CommitmentService(connection, clock)
    for user_id, role in (
        ("ops", "placement_admin"),
        ("cash", "finance"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    project, batch = "newtown-east", "b-2026-03"

    # 第一版承诺：临时周转 6-30、正式交房 8-31、公共服务接续 9-15。
    v1 = service.publish_commitment("ops", {
        "project_id": project, "batch_id": batch,
        "promised_turnover_date": "2026-06-30",
        "promised_delivery_date": "2026-08-31",
        "promised_service_date": "2026-09-15",
        "daily_compensation_cny": "120.00",
        "note": "首批安置承诺",
    })

    for household, head in (("hh-001", "张广山"), ("hh-002", "李守田"), ("hh-003", "王秀兰")):
        service.register_household("ops", {
            "household_id": household, "project_id": project, "batch_id": batch,
            "head_name": head, "members": 3, "enrolled_at": "2026-05-10T03:00:00Z",
        })

    # 第一统计窗口 2026-09-01..2026-09-15。
    service.create_window("ops", {
        "window_id": "win-sep-1", "project_id": project, "batch_id": batch,
        "window_start": "2026-09-01", "window_end": "2026-09-15",
        "daily_rate_cny": "120.00", "label": "九月上半月周期",
    })
    # 施工排除：至少提前 7 天告知，且只允许落在 9-01..9-10 的约定窗口。
    service.add_exclusion_rule("ops", {
        "rule_id": "rule-build-1", "window_id": "win-sep-1", "kind": "construction_plan",
        "min_advance_notice_days": 7, "allowed_reason_codes": [],
        "effective_start": "2026-09-01", "effective_end": "2026-09-10",
        "note": "主干管线迁改",
    })
    # 政策豁免：只承认汛期救灾原因代码。
    service.add_exclusion_rule("ops", {
        "rule_id": "rule-policy-1", "window_id": "win-sep-1", "kind": "policy_exemption",
        "min_advance_notice_days": 0, "allowed_reason_codes": ["FLOOD_RELIEF_2026"],
        "effective_start": None, "effective_end": None,
        "note": "汛期政策豁免",
    })
    # 家庭主动延期：必须家庭确认。
    service.add_exclusion_rule("ops", {
        "rule_id": "rule-pause-1", "window_id": "win-sep-1", "kind": "household_deferral",
        "min_advance_notice_days": 0, "allowed_reason_codes": [],
        "effective_start": None, "effective_end": None,
        "note": "家庭确认暂停",
    })

    # 合规施工：提前 10 天告知，9-03..9-05 落在约定窗口。
    service.record_event("ops", {
        "event_id": "ev-build-ok", "project_id": project, "batch_id": batch,
        "event_type": "construction.announced", "household_id": None,
        "event_date": "2026-09-03", "end_date": "2026-09-06",
        "advance_notice_days": 10, "reason_code": "PIPELINE", "confirmed": False,
        "note": "9-03 至 9-05 主干管线迁改（已提前告知）",
    })
    # 跨窗口施工：9-08..9-12，只有 9-08..9-10 落在约定窗口，须精确拆分。
    service.record_event("ops", {
        "event_id": "ev-build-cross", "project_id": project, "batch_id": batch,
        "event_type": "construction.announced", "household_id": None,
        "event_date": "2026-09-08", "end_date": "2026-09-13",
        "advance_notice_days": 8, "reason_code": "PIPELINE", "confirmed": False,
        "note": "跨约定窗口边界的施工",
    })
    # 告知不足的施工：拒绝排除。
    service.record_event("ops", {
        "event_id": "ev-build-late", "project_id": project, "batch_id": batch,
        "event_type": "construction.announced", "household_id": None,
        "event_date": "2026-09-02", "end_date": "2026-09-03",
        "advance_notice_days": 2, "reason_code": "PIPELINE", "confirmed": False,
        "note": "临时抢修，提前告知不足",
    })
    # 合规政策豁免 9-07..9-08。
    service.record_event("ops", {
        "event_id": "ev-policy-ok", "project_id": project, "batch_id": batch,
        "event_type": "policy.exempted", "household_id": None,
        "event_date": "2026-09-07", "end_date": "2026-09-09",
        "advance_notice_days": None, "reason_code": "FLOOD_RELIEF_2026", "confirmed": False,
        "note": "汛期救灾顺延两天",
    })
    # 未确认的家庭暂停：拒绝排除。
    service.record_event("ops", {
        "event_id": "ev-pause-unconfirmed", "project_id": project, "batch_id": batch,
        "event_type": "household.paused", "household_id": "hh-002",
        "event_date": "2026-09-04", "end_date": "2026-09-06",
        "advance_notice_days": None, "reason_code": "FAMILY_TRIP", "confirmed": False,
        "note": "家庭外出，尚未确认",
    })
    # hh-001 已确认暂停 9-11..9-12。
    service.record_event("ops", {
        "event_id": "ev-pause-ok", "project_id": project, "batch_id": batch,
        "event_type": "household.paused", "household_id": "hh-001",
        "event_date": "2026-09-11", "end_date": "2026-09-13",
        "advance_notice_days": None, "reason_code": "FAMILY_TRIP", "confirmed": True,
        "note": "家庭书面确认延期收房",
    })
    # hh-003 于 9-10 实际交房：等待只计到 9-10。
    service.record_event("ops", {
        "event_id": "ev-delivery-3", "project_id": project, "batch_id": batch,
        "event_type": "delivery.confirmed", "household_id": "hh-003",
        "event_date": "2026-09-10", "end_date": None,
        "advance_notice_days": None, "reason_code": None, "confirmed": True,
        "note": "hh-003 正式交房",
    })

    # 周期结算（v1）。
    settled = service.settle_window("ops", "win-sep-1")
    run_v1 = settled["run_id"]

    # 分离授权：财务不能越过安置运营直接确认；运营确认后财务再确认。
    from .errors import InvalidState

    finance_first = service.approval_status("cash", run_v1)
    try:
        service.confirm_finance("cash", run_v1)
        finance_blocked = False
    except InvalidState:
        finance_blocked = True
    service.confirm_operations("ops", run_v1)
    service.confirm_finance("cash", run_v1)

    # 解释：每段扣减/排除可追溯到具体事件与规则。
    explained = service.explain_run("audit", run_v1)

    # 迟到事件：hh-002 实际交房日期补录为 9-12（此前未交房，按整窗口等待）。
    service.record_event("ops", {
        "event_id": "ev-delivery-2-late", "project_id": project, "batch_id": batch,
        "event_type": "delivery.confirmed", "household_id": "hh-002",
        "event_date": "2026-09-12", "end_date": None,
        "advance_notice_days": None, "reason_code": None, "confirmed": True,
        "note": "交房凭证迟到补录",
    })

    # 已结算结果不得被迟到事件直接改写：再结算被拒绝，纠错必须走新版本。
    try:
        service.settle_window("ops", "win-sep-1")
        late_event_blocked = False
    except InvalidState:
        late_event_blocked = True
    # 窗口 1 已由财务确认，纠错通道关闭（需另行冲销），这一点同样受保护。
    try:
        service.correct_window("ops", "win-sep-1", "迟到交房补录")
        confirmed_correction_blocked = False
    except InvalidState:
        confirmed_correction_blocked = True
    service.create_window("ops", {
        "window_id": "win-sep-2", "project_id": project, "batch_id": batch,
        "window_start": "2026-09-16", "window_end": "2026-09-30",
        "daily_rate_cny": "120.00", "label": "九月下半月周期",
    })
    service.add_exclusion_rule("ops", {
        "rule_id": "rule-build-2", "window_id": "win-sep-2", "kind": "construction_plan",
        "min_advance_notice_days": 7, "allowed_reason_codes": [],
        "effective_start": "2026-09-16", "effective_end": "2026-09-30",
        "note": "配套道路施工",
    })
    settled_2 = service.settle_window("ops", "win-sep-2")
    run2_v1 = settled_2["run_id"]
    service.confirm_operations("ops", run2_v1)
    # 财务驳回：运营需处理后重新结算（此处经纠错产生新版本）。
    # hh-001 于 9-20 交房（迟到事件落在第二窗口）。
    service.record_event("ops", {
        "event_id": "ev-delivery-1-late", "project_id": project, "batch_id": batch,
        "event_type": "delivery.confirmed", "household_id": "hh-001",
        "event_date": "2026-09-20", "end_date": None,
        "advance_notice_days": None, "reason_code": None, "confirmed": True,
        "note": "hh-001 交房凭证迟到补录",
    })
    corrected = service.correct_window("ops", "win-sep-2", "hh-001 交房凭证迟到，等待截止日修正为 9-20")
    run2_v2 = corrected["run_id"]
    service.confirm_operations("ops", run2_v2)
    service.confirm_finance("cash", run2_v2)

    # 承诺换版：补偿标准调整（纠错新版本亦可由承诺换版触发）。
    service.publish_commitment("ops", {
        "project_id": project, "batch_id": batch,
        "promised_turnover_date": "2026-06-30",
        "promised_delivery_date": "2026-09-05",
        "promised_service_date": "2026-09-20",
        "daily_compensation_cny": "130.00",
        "note": "交房时限顺延、日补偿标准调整",
    })

    recomputed = service.replay_batch("audit", project, batch)
    snapshot = service.export_snapshot("audit", project, batch)
    audit = service.audit_chain("audit")

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "commitment_version_1": v1["version_no"],
        "window1_settlement": {
            "run_id": run_v1,
            "affected_households": settled["affected_households"],
            "total_waiting_days": settled["total_waiting_days"],
            "total_excluded_days": settled["total_excluded_days"],
            "total_billable_days": settled["total_billable_days"],
            "net_compensation_cny": settled["net_compensation_cny"],
            "rejected_exclusions": settled["rejected_exclusions"],
        },
        "finance_first_attempt_state": finance_first["state"],
        "finance_cannot_skip_operations": finance_blocked,
        "deduction_sources": explained["deductions"],
        "late_event_rewrite_blocked": late_event_blocked,
        "confirmed_window_correction_blocked": confirmed_correction_blocked,
        "window2_correction": {
            "superseded_run_id": run2_v1,
            "new_run_id": run2_v2,
            "reason": corrected["diff"]["reason"],
            "household_changes": corrected["diff"]["household_changes"],
        },
        "recompute": recomputed,
        "snapshot_windows": len(snapshot["windows"]),
        "snapshot_events": len(snapshot["events"]),
        "audit": audit,
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
