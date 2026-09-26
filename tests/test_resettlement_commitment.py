from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from resettlement_commitment.accounting import (
    Segment,
    clip,
    compute_window,
    diff_results,
    merge_intervals,
    parse_day,
    split_across_windows,
)
from resettlement_commitment.api import JsonApplication
from resettlement_commitment.clock import FrozenClock
from resettlement_commitment.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from resettlement_commitment.service import CommitmentService, recompute_snapshot


WINDOW = {
    "window_id": "w1", "window_start": "2026-09-01", "window_end": "2026-09-30",
    "daily_rate_cny": "100",
}
COMMITMENT = {
    "version_no": 1, "promised_turnover_date": "2026-06-30",
    "promised_delivery_date": "2026-08-31", "promised_service_date": "2026-09-15",
    "daily_compensation_cny": "100",
}
HOUSEHOLDS = [{"household_id": "h1"}, {"household_id": "h2"}]
RULE_BUILD = {
    "rule_id": "rb", "kind": "construction_plan", "min_advance_notice_days": 7,
    "allowed_reason_codes": [], "effective_start": "2026-09-01", "effective_end": "2026-09-20",
}
RULE_POLICY = {
    "rule_id": "rp", "kind": "policy_exemption", "min_advance_notice_days": 0,
    "allowed_reason_codes": ["FLOOD"], "effective_start": None, "effective_end": None,
}
RULE_PAUSE = {
    "rule_id": "rh", "kind": "household_deferral", "min_advance_notice_days": 0,
    "allowed_reason_codes": [], "effective_start": None, "effective_end": None,
}


def event(event_id, kind, *, household_id=None, start="2026-09-03", end="2026-09-06",
          notice=10, reason="X", confirmed=False):
    return {
        "event_id": event_id, "event_type": kind, "household_id": household_id,
        "event_date": start, "end_date": end, "advance_notice_days": notice,
        "reason_code": reason, "confirmed": confirmed, "note": event_id,
    }


class IntervalTests(unittest.TestCase):
    def test_clip_uses_half_open_ranges(self) -> None:
        self.assertEqual(
            clip(parse_day("2026-09-01"), parse_day("2026-09-10"),
                 parse_day("2026-09-05"), parse_day("2026-09-15")),
            (parse_day("2026-09-05"), parse_day("2026-09-10")),
        )
        self.assertIsNone(
            clip(parse_day("2026-09-10"), parse_day("2026-09-11"),
                 parse_day("2026-09-01"), parse_day("2026-09-10")),
        )

    def test_open_segment_days_to_limit(self) -> None:
        segment = Segment(parse_day("2026-09-01"), None)
        self.assertEqual(segment.days_to(parse_day("2026-09-11")), 10)

    def test_merge_intervals_unions_overlaps(self) -> None:
        merged = merge_intervals([
            (parse_day("2026-09-01"), parse_day("2026-09-03")),
            (parse_day("2026-09-02"), parse_day("2026-09-05")),
            (parse_day("2026-09-10"), parse_day("2026-09-11")),
        ])
        self.assertEqual(merged, [(parse_day("2026-09-01"), 4), (parse_day("2026-09-10"), 1)])

    def test_split_across_windows_splits_exactly_on_boundaries(self) -> None:
        windows = [
            {"window_id": "a", "window_start": "2026-09-01", "window_end": "2026-09-15"},
            {"window_id": "b", "window_start": "2026-09-16", "window_end": "2026-09-30"},
        ]
        pieces = split_across_windows("2026-09-14", "2026-09-18", windows)
        self.assertEqual([(p["window_id"], p["start_date"], p["end_date"], p["days"]) for p in pieces], [
            ("a", "2026-09-14", "2026-09-16", 2),
            ("b", "2026-09-16", "2026-09-18", 2),
        ])
        self.assertEqual(sum(p["days"] for p in pieces), 4)


class ComputeWindowTests(unittest.TestCase):
    def compute(self, events, *, households=HOUSEHOLDS, rules=None):
        return compute_window(
            window=WINDOW, commitment=COMMITMENT, households=households,
            events=events, rules=[RULE_BUILD, RULE_POLICY, RULE_PAUSE] if rules is None else rules,
        )

    def row(self, result, household_id: str) -> dict:
        return next(row for row in result["households"] if row["household_id"] == household_id)

    def test_no_delivery_counts_full_window_and_valid_event_excludes(self) -> None:
        result = self.compute([event("e1", "construction.announced")])
        row = self.row(result, "h1")
        self.assertEqual(row["waiting"]["days"], 30)
        self.assertEqual(row["excluded_days"], 3)
        self.assertEqual(row["billable_days"], 27)
        self.assertEqual(row["net_compensation_cny"], "2700.00")
        self.assertEqual(row["fragments"][0]["amount_cny"], "300.00")

    def test_delivery_cuts_waiting_and_exclusion_at_delivery_date(self) -> None:
        events = [
            event("e1", "construction.announced", start="2026-09-08", end="2026-09-15"),
            event("d1", "delivery.confirmed", household_id="h1", start="2026-09-10", end=None,
                  notice=None, reason=None, confirmed=True),
        ]
        result = self.compute(events)
        row = self.row(result, "h1")
        self.assertEqual(row["waiting"], {"start_date": "2026-09-01", "end_date": "2026-09-10", "days": 9})
        self.assertEqual(row["excluded_days"], 2)  # 施工 09-08..09-10
        self.assertEqual(row["billable_days"], 7)
        self.assertEqual(self.row(result, "h2")["waiting"]["days"], 30)

    def test_short_notice_construction_is_rejected(self) -> None:
        result = self.compute([event("e1", "construction.announced", notice=2)])
        self.assertEqual(self.row(result, "h1")["excluded_days"], 0)
        self.assertEqual(result["rejected_exclusions"][0]["reason_code"], "notice_too_short")

    def test_event_outside_agreed_window_is_rejected(self) -> None:
        # 提前告知合规，但施工日 09-25 超出规则约定的 09-20 边界。
        result = self.compute([
            event("e1", "construction.announced", start="2026-09-25", end="2026-09-27", notice=30)
        ])
        self.assertEqual(self.row(result, "h1")["excluded_days"], 0)
        self.assertEqual(result["rejected_exclusions"][0]["reason_code"], "outside_agreed_window")

    def test_crossing_event_is_clipped_to_agreed_window(self) -> None:
        result = self.compute([
            event("e1", "construction.announced", start="2026-09-18", end="2026-09-25", notice=10)
        ])
        self.assertEqual(self.row(result, "h1")["excluded_days"], 3)  # 09-18..09-20（含 20 日）

    def test_policy_reason_must_be_allowed(self) -> None:
        result = self.compute([
            event("e1", "policy.exempted", reason="OTHER", notice=None)
        ])
        self.assertEqual(self.row(result, "h1")["excluded_days"], 0)
        self.assertEqual(result["rejected_exclusions"][0]["reason_code"], "reason_not_allowed")
        ok = self.compute([event("e2", "policy.exempted", reason="FLOOD", notice=None)])
        self.assertEqual(self.row(ok, "h1")["excluded_days"], 3)

    def test_household_pause_requires_confirmation_and_is_household_scoped(self) -> None:
        denied = self.compute([
            event("p1", "household.paused", household_id="h1", confirmed=False, notice=None,
                  reason=None, start="2026-09-04", end="2026-09-06"),
        ])
        self.assertEqual(self.row(denied, "h1")["excluded_days"], 0)
        self.assertEqual(denied["rejected_exclusions"][0]["reason_code"], "pause_not_confirmed")
        allowed = self.compute([
            event("p1", "household.paused", household_id="h1", confirmed=True, notice=None,
                  reason=None, start="2026-09-04", end="2026-09-06"),
        ])
        self.assertEqual(self.row(allowed, "h1")["excluded_days"], 2)
        self.assertEqual(self.row(allowed, "h2")["excluded_days"], 0)

    def test_overlapping_exclusions_are_counted_once(self) -> None:
        events = [
            event("b", "construction.announced", start="2026-09-03", end="2026-09-08"),
            event("p", "policy.exempted", start="2026-09-05", end="2026-09-10",
                  notice=None, reason="FLOOD"),
        ]
        result = self.compute(events)
        self.assertEqual(self.row(result, "h1")["excluded_days"], 7)  # 09-03..09-10 并集

    def test_event_fully_outside_statistical_window_is_ignored_not_rejected(self) -> None:
        result = self.compute([
            event("late", "construction.announced", start="2026-10-05", end="2026-10-08", notice=30)
        ])
        self.assertEqual(result["rejected_exclusions"], [])
        self.assertEqual(self.row(result, "h1")["excluded_days"], 0)

    def test_diff_reports_household_level_amount_changes(self) -> None:
        previous = {
            "settlement_version_no": 1, "commitment_version_no": 1,
            "net_compensation_cny": "1000.00",
            "households": [{"household_id": "h1", "waiting": {"days": 10}, "excluded_days": 0,
                            "net_compensation_cny": "1000.00"}],
        }
        current = {
            "settlement_version_no": 2, "commitment_version_no": 1,
            "net_compensation_cny": "400.00",
            "households": [{"household_id": "h1", "waiting": {"days": 4}, "excluded_days": 0,
                            "net_compensation_cny": "400.00"}],
        }
        diff = diff_results(previous, current, reason="迟到交房")
        self.assertEqual(diff["household_changes"][0]["net_compensation_cny_delta"], "-600.00")
        self.assertEqual(diff["household_changes"][0]["waiting_days_delta"], -6)


class ServiceTests(unittest.TestCase):
    PROJECT = "proj-a"
    BATCH = "batch-1"

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=timezone.utc))
        self.service = CommitmentService(self.connection, self.clock)
        for user_id, role in (("ops", "placement_admin"), ("cash", "finance"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.publish_commitment("ops", {
            "project_id": self.PROJECT, "batch_id": self.BATCH,
            "promised_turnover_date": "2026-06-30",
            "promised_delivery_date": "2026-08-31",
            "promised_service_date": "2026-09-15",
            "daily_compensation_cny": "100.00", "note": "v1",
        })
        for household in ("h1", "h2"):
            self.service.register_household("ops", {
                "household_id": household, "project_id": self.PROJECT, "batch_id": self.BATCH,
                "head_name": household, "members": 2, "enrolled_at": "2026-05-01T00:00:00Z",
            })
        self.service.create_window("ops", {
            "window_id": "w1", "project_id": self.PROJECT, "batch_id": self.BATCH,
            "window_start": "2026-09-01", "window_end": "2026-09-15",
            "daily_rate_cny": "100.00", "label": "九月上半月",
        })
        self.service.add_exclusion_rule("ops", {
            "rule_id": "rb", "window_id": "w1", "kind": "construction_plan",
            "min_advance_notice_days": 7, "allowed_reason_codes": [],
            "effective_start": "2026-09-01", "effective_end": "2026-09-15", "note": "施工",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def build_event(self, event_id="ev-1", **overrides) -> dict:
        payload = {
            "event_id": event_id, "project_id": self.PROJECT, "batch_id": self.BATCH,
            "event_type": "construction.announced", "household_id": None,
            "event_date": "2026-09-03", "end_date": "2026-09-06",
            "advance_notice_days": 10, "reason_code": "PIPE", "confirmed": False,
            "note": "合规施工",
        }
        payload.update(overrides)
        return payload

    def test_commitment_versions_append_and_supersede(self) -> None:
        history = self.service.commitment_history(self.PROJECT, self.BATCH)
        self.assertEqual([row["version_no"] for row in history], [1])
        self.assertEqual(history[0]["state"], "active")
        self.service.publish_commitment("ops", {
            "project_id": self.PROJECT, "batch_id": self.BATCH,
            "promised_turnover_date": "2026-06-30",
            "promised_delivery_date": "2026-09-10",
            "promised_service_date": "2026-09-25",
            "daily_compensation_cny": "110.00", "note": "v2",
        })
        history = self.service.commitment_history(self.PROJECT, self.BATCH)
        self.assertEqual([row["state"] for row in history], ["superseded", "active"])
        with self.assertRaises(Conflict):
            self.service.publish_commitment("ops", {
                "project_id": self.PROJECT, "batch_id": self.BATCH,
                "promised_turnover_date": "2026-06-30",
                "promised_delivery_date": "2026-09-10",
                "promised_service_date": "2026-09-25",
                "daily_compensation_cny": "110.00", "note": "v2",
            })

    def test_events_are_immutable_at_database_level(self) -> None:
        self.service.record_event("ops", self.build_event())
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE commitment_events SET event_date='2026-09-04' WHERE event_id='ev-1'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM commitment_events WHERE event_id='ev-1'")

    def test_settlement_is_idempotent_and_late_event_forces_correction(self) -> None:
        self.service.record_event("ops", self.build_event())
        first = self.service.settle_window("ops", "w1")
        self.assertEqual(first["settlement_version_no"], 1)
        self.assertFalse(first["replayed"])
        replayed = self.service.settle_window("ops", "w1")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["run_id"], first["run_id"])

        self.service.record_event("ops", self.build_event(
            event_id="ev-late", event_type="delivery.confirmed", household_id="h1",
            event_date="2026-09-10", end_date=None, advance_notice_days=None,
            reason_code=None, confirmed=True, note="迟到交房",
        ))
        with self.assertRaises(InvalidState):
            self.service.settle_window("ops", "w1")
        corrected = self.service.correct_window("ops", "w1", "h1 交房凭证迟到")
        self.assertEqual(corrected["settlement_version_no"], 2)
        self.assertEqual(corrected["diff"]["reason"], "h1 交房凭证迟到")
        self.assertTrue(corrected["diff"]["household_changes"])
        old = self.service.run_detail("audit", first["run_id"])
        self.assertEqual(old["state"], "corrected")

    def test_correction_requires_actual_difference_and_reason(self) -> None:
        self.service.settle_window("ops", "w1")
        with self.assertRaises(InvalidState):
            self.service.correct_window("ops", "w1", "没有任何变化")
        with self.assertRaises(ValidationFailed):
            self.service.correct_window("ops", "w1", "  ")

    def test_finance_and_operations_authorization_is_separated(self) -> None:
        run_id = self.service.settle_window("ops", "w1")["run_id"]
        # 角色隔离：财务不能登记事件/结算，运营不能做财务确认。
        with self.assertRaises(Forbidden):
            self.service.record_event("cash", self.build_event())
        with self.assertRaises(Forbidden):
            self.service.confirm_finance("ops", run_id)
        # 顺序约束：财务确认前必须先有安置运营确认。
        with self.assertRaises(InvalidState):
            self.service.confirm_finance("cash", run_id)
        self.service.confirm_operations("ops", run_id)
        self.assertEqual(self.service.approval_status("audit", run_id)["state"], "operations_confirmed")
        self.service.confirm_finance("cash", run_id)
        self.assertEqual(self.service.approval_status("audit", run_id)["state"], "finance_confirmed")
        # 财务已确认的版本不允许再纠错。
        with self.assertRaises(InvalidState):
            self.service.correct_window("ops", "w1", "试图改已确认版本")

    def test_finance_rejection_blocks_confirmation_and_correction_opens_new_version(self) -> None:
        run_id = self.service.settle_window("ops", "w1")["run_id"]
        self.service.confirm_operations("ops", run_id)
        self.service.reject_finance("cash", run_id, "材料不全")
        self.assertEqual(self.service.approval_status("audit", run_id)["state"], "rejected")
        self.service.record_event("ops", self.build_event(
            event_id="ev-late", event_type="delivery.confirmed", household_id="h2",
            event_date="2026-09-12", end_date=None, advance_notice_days=None,
            reason_code=None, confirmed=True, note="交房",
        ))
        corrected = self.service.correct_window("ops", "w1", "补录交房后重算")
        new_status = self.service.approval_status("audit", corrected["run_id"])
        self.assertEqual(new_status["state"], "pending")

    def test_explain_run_attributes_every_deduction_to_event_segment(self) -> None:
        self.service.record_event("ops", self.build_event())
        run_id = self.service.settle_window("ops", "w1")["run_id"]
        explanation = self.service.explain_run("audit", run_id)
        self.assertEqual(len(explanation["deductions"]), 2)  # h1、h2 各一段
        item = explanation["deductions"][0]
        self.assertEqual(item["event_id"], "ev-1")
        self.assertEqual(item["rule_id"], "rb")
        self.assertEqual(item["days"], 3)
        self.assertEqual(item["deducted_cny"], "300.00")

    def test_replay_validates_stored_versions_and_flags_pending_correction(self) -> None:
        self.service.record_event("ops", self.build_event())
        self.service.settle_window("ops", "w1")
        report = self.service.replay_batch("audit", self.PROJECT, self.BATCH)
        self.assertTrue(report["stored_versions_match"])
        self.assertEqual(report["windows_pending_correction"], 0)
        # 迟到事件后离线复算标出待纠错窗口。
        self.service.record_event("ops", self.build_event(
            event_id="ev-late", event_type="delivery.confirmed", household_id="h1",
            event_date="2026-09-10", end_date=None, advance_notice_days=None,
            reason_code=None, confirmed=True, note="迟到交房",
        ))
        report = self.service.replay_batch("audit", self.PROJECT, self.BATCH)
        self.assertTrue(report["stored_versions_match"])
        self.assertEqual(report["windows_pending_correction"], 1)
        self.assertTrue(report["windows"][0]["correction_pending"])

    def test_recompute_snapshot_detects_tampered_stored_result(self) -> None:
        self.service.settle_window("ops", "w1")
        snapshot = self.service.export_snapshot("audit", self.PROJECT, self.BATCH)
        self.assertTrue(recompute_snapshot(snapshot)["stored_versions_match"])
        snapshot["windows"][0]["runs"][0]["result"]["net_compensation_cny"] = "0.01"
        self.assertFalse(recompute_snapshot(snapshot)["stored_versions_match"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.service.settle_window("ops", "w1")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE commitment_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = CommitmentService(
            self.connection, FrozenClock(datetime(2026, 9, 26, tzinfo=timezone.utc))
        )
        self.app = JsonApplication(self.service)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_actor_boundary(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        missing = self.app.handle("GET", "/audit/chain")
        self.assertEqual(missing.status, 422)
        self.assertEqual(missing.body["error"]["code"], "validation_failed")
        self.service.create_user("audit", "audit", "auditor")
        self.assertEqual(self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "audit"}).status, 200)

    def test_role_forbidden_maps_to_403(self) -> None:
        self.service.create_user("cash", "cash", "finance")
        response = self.app.handle("POST", "/events", {"X-Actor-Id": "cash"}, b"{}")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_unknown_route_is_404(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
