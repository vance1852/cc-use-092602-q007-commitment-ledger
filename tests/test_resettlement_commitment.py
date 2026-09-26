from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from resettlement_commitment.accounting import compute_window, diff_results
from resettlement_commitment.api import JsonApplication
from resettlement_commitment.clock import FrozenClock
from resettlement_commitment.errors import Forbidden, InvalidState, ValidationFailed
from resettlement_commitment.service import CommitmentService


TERM = {
    "kind": "formal_delivery",
    "start_event": "temporary_keys_handover",
    "end_event": "formal_delivery",
    "promised_days": 10,
}


def version(promised_days: int = 10, rate: str = "50.00", notice: int = 5) -> dict:
    term = dict(TERM, promised_days=promised_days)
    return {
        "commitment_id": "c1",
        "version_no": 1,
        "project_id": "p1",
        "batch_id": "b1",
        "terms": [term],
        "daily_compensation_cny": rate,
        "advance_notice_days": notice,
    }


WINDOWS = [
    {"window_id": "w1", "starts_on": "2026-09-01", "ends_on": "2026-09-15"},
    {"window_id": "w2", "starts_on": "2026-09-16", "ends_on": "2026-09-30"},
]


def event(event_id, kind, household, day, *, notice_at=None, payload=None) -> dict:
    return {
        "event_id": event_id,
        "event_kind": kind,
        "household_id": household,
        "event_date": day,
        "observed_at": f"{day}T08:00:00Z",
        "notice_at": notice_at,
        "payload": payload or {},
    }


class AccountingTests(unittest.TestCase):
    def test_cross_window_construction_is_split_precisely(self) -> None:
        events = [
            event("e1", "temporary_keys_handover", "hh1", "2026-09-01"),
            event("e2", "formal_delivery", "hh1", "2026-09-20"),
            event("e3", "construction_plan", "*", "2026-09-14",
                  notice_at="2026-09-07T08:00:00Z",
                  payload={"starts_on": "2026-09-14", "ends_on": "2026-09-17"}),
        ]
        rules = [
            {"rule_id": "r1", "window_id": "w1", "event_kind": "construction_plan", "require_advance_notice": True},
            {"rule_id": "r2", "window_id": "w2", "event_kind": "construction_plan", "require_advance_notice": True},
        ]
        first = compute_window(version=version(), windows=WINDOWS, rules=rules, events=events,
                               pauses=[], target_window_id="w1", as_of_date="2026-09-30")
        second = compute_window(version=version(), windows=WINDOWS, rules=rules, events=events,
                                pauses=[], target_window_id="w2", as_of_date="2026-09-30")
        fragments = first["households"][0]["terms"][0]["fragments"]
        self.assertEqual(len(fragments), 1)
        self.assertEqual((fragments[0]["fragment_start"], fragments[0]["fragment_end"]),
                         ("2026-09-14", "2026-09-15"))
        self.assertEqual(fragments[0]["applied_days"], 2)
        fragments2 = second["households"][0]["terms"][0]["fragments"]
        self.assertEqual((fragments2[0]["fragment_start"], fragments2[0]["fragment_end"]),
                         ("2026-09-16", "2026-09-17"))
        self.assertEqual(fragments2[0]["applied_days"], 2)
        # w1: 15 个日历日 - 2 个排除日 = 13，承诺 10 天，逾期 3 天。
        self.assertEqual(first["window_totals"]["counted_days"], 13)
        self.assertEqual(first["window_totals"]["delay_days"], 3)
        # w2: 5 个日历日（16..20）- 2 个排除日 = 3，承诺预算已用尽，全部逾期。
        self.assertEqual(second["window_totals"]["counted_days"], 3)
        self.assertEqual(second["window_totals"]["delay_days"], 3)

    def test_construction_without_notice_is_kept_but_not_excluded(self) -> None:
        events = [
            event("e1", "temporary_keys_handover", "hh1", "2026-09-01"),
            event("e2", "formal_delivery", "hh1", "2026-09-20"),
            event("e3", "construction_plan", "*", "2026-09-08",
                  payload={"starts_on": "2026-09-08", "ends_on": "2026-09-08"}),
        ]
        rules = [{"rule_id": "r1", "window_id": "w1", "event_kind": "construction_plan",
                  "require_advance_notice": True}]
        result = compute_window(version=version(), windows=WINDOWS, rules=rules, events=events,
                                pauses=[], target_window_id="w1", as_of_date="2026-09-30")
        fragment = result["households"][0]["terms"][0]["fragments"][0]
        self.assertFalse(fragment["eligible"])
        self.assertEqual(fragment["rejected_days"], 1)
        self.assertEqual(fragment["applied_days"], 0)
        self.assertEqual(result["window_totals"]["excluded_days"], 0)
        self.assertIn("提前告知", fragment["basis"])

    def test_construction_without_window_rule_is_not_excluded(self) -> None:
        events = [
            event("e1", "temporary_keys_handover", "hh1", "2026-09-01"),
            event("e2", "formal_delivery", "hh1", "2026-09-20"),
            event("e3", "construction_plan", "*", "2026-09-10",
                  notice_at="2026-09-01T08:00:00Z",
                  payload={"starts_on": "2026-09-10", "ends_on": "2026-09-10"}),
        ]
        result = compute_window(version=version(), windows=WINDOWS, rules=[], events=events,
                                pauses=[], target_window_id="w1", as_of_date="2026-09-30")
        fragment = result["households"][0]["terms"][0]["fragments"][0]
        self.assertFalse(fragment["eligible"])
        self.assertIn("排除规则", fragment["basis"])

    def test_pause_overlapping_construction_is_counted_once_for_pause(self) -> None:
        events = [
            event("e1", "temporary_keys_handover", "hh1", "2026-09-01"),
            event("e2", "formal_delivery", "hh1", "2026-09-20"),
            event("e3", "construction_plan", "*", "2026-09-05",
                  notice_at="2026-08-30T08:00:00Z",
                  payload={"starts_on": "2026-09-05", "ends_on": "2026-09-06"}),
        ]
        pauses = [{"pause_id": "p1", "household_id": "hh1", "starts_on": "2026-09-04",
                   "ends_on": "2026-09-06", "reason_code": "family_extension",
                   "evidence": "signed"}]
        rules = [{"rule_id": "r1", "window_id": "w1", "event_kind": "construction_plan",
                  "require_advance_notice": True}]
        result = compute_window(version=version(), windows=WINDOWS, rules=rules, events=events,
                                pauses=pauses, target_window_id="w1", as_of_date="2026-09-30")
        fragments = result["households"][0]["terms"][0]["fragments"]
        pause_fragment = next(f for f in fragments if f["source"] == "pause")
        construction_fragment = next(f for f in fragments if f["source"] == "event")
        self.assertEqual(pause_fragment["applied_days"], 3)
        # 施工与暂停重叠的两天归暂停，施工只能另外占用 0 天。
        self.assertEqual(construction_fragment["applied_days"], 0)
        self.assertEqual(construction_fragment["overlap_days"], 2)
        self.assertEqual(result["window_totals"]["excluded_days"], 3)

    def test_promised_budget_carries_across_windows_per_household(self) -> None:
        events = [
            event("e1", "temporary_keys_handover", "hh1", "2026-09-01"),
            event("e2", "formal_delivery", "hh1", "2026-09-30"),
        ]
        first = compute_window(version=version(promised_days=20), windows=WINDOWS, rules=[],
                               events=events, pauses=[], target_window_id="w1",
                               as_of_date="2026-09-30")
        second = compute_window(version=version(promised_days=20), windows=WINDOWS, rules=[],
                                events=events, pauses=[], target_window_id="w2",
                                as_of_date="2026-09-30")
        self.assertEqual(first["window_totals"]["delay_days"], 0)
        # 预算 20 天在 w1 用掉 15 天，w2 剩余 5 天，w2 有 15 个等待日，逾期 10 天。
        self.assertEqual(second["window_totals"]["delay_days"], 10)
        self.assertEqual(second["window_totals"]["compensation_cny"], "500.00")

    def test_open_journey_counts_through_cutoff_and_is_explained(self) -> None:
        events = [event("e1", "temporary_keys_handover", "hh1", "2026-09-10")]
        result = compute_window(version=version(promised_days=0), windows=WINDOWS, rules=[],
                                events=events, pauses=[], target_window_id="w1",
                                as_of_date="2026-09-15")
        slot = result["households"][0]["terms"][0]
        self.assertIsNone(slot["end_event_id"])
        self.assertIn("未记录", slot["open_reason"])
        self.assertEqual(slot["segment_days"], 6)

    def test_diff_reports_changed_household_and_delta(self) -> None:
        events_a = [
            event("e1", "temporary_keys_handover", "hh1", "2026-09-01"),
            event("e2", "formal_delivery", "hh1", "2026-09-20"),
        ]
        events_b = events_a + [
            event("e3", "construction_plan", "*", "2026-09-10",
                  notice_at="2026-09-01T08:00:00Z",
                  payload={"starts_on": "2026-09-10", "ends_on": "2026-09-10"}),
        ]
        rules = [{"rule_id": "r1", "window_id": "w1", "event_kind": "construction_plan",
                  "require_advance_notice": True}]
        before = compute_window(version=version(), windows=WINDOWS, rules=rules, events=events_a,
                                pauses=[], target_window_id="w1", as_of_date="2026-09-30")
        after = compute_window(version=version(), windows=WINDOWS, rules=rules, events=events_b,
                               pauses=[], target_window_id="w1", as_of_date="2026-09-30")
        diff = diff_results(before, after)
        self.assertEqual(diff["delta_compensation_cny"], "-50.00")
        self.assertEqual(diff["new_event_ids"], ["e3"])
        self.assertEqual(diff["changed_households"][0]["household_id"], "hh1")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc))
        self.service = CommitmentService(self.connection, self.clock)
        for user_id, role in (
            ("ops", "resettlement"),
            ("manager", "resettlement_manager"),
            ("fin", "finance"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_commitment("ops", {
            "commitment_id": "c1", "project_id": "p1", "batch_id": "b1",
            "terms": [TERM], "daily_compensation_cny": "50.00",
            "advance_notice_days": 5, "notes": "initial",
        })
        self.service.add_window("ops", "c1", {"window_id": "w1", "starts_on": "2026-09-01", "ends_on": "2026-09-30"})
        self.service.add_exclusion_rule("ops", {
            "rule_id": "r1", "window_id": "w1", "event_kind": "construction_plan",
            "require_advance_notice": True, "reason": "约定窗口内提前告知可排除",
        })
        for household in ("hh1", "hh2"):
            self.service.record_event("ops", "p1", "b1", event(
                f"start-{household}", "temporary_keys_handover", household, "2026-09-01"))
            self.service.record_event("ops", "p1", "b1", event(
                f"end-{household}", "formal_delivery", household, "2026-09-20"))

    def tearDown(self) -> None:
        self.connection.close()

    def _prepare(self) -> dict:
        return self.service.prepare_settlement("ops", "c1", "w1", "2026-09-30")

    def test_events_are_immutable(self) -> None:
        from resettlement_commitment.errors import Conflict
        with self.assertRaises(Conflict):
            self.service.record_event("ops", "p1", "b1", event(
                "start-hh1", "temporary_keys_handover", "hh1", "2026-09-02"))
        self.assertTrue(self.service.audit_chain("aud")["valid"])
        self.connection.execute("UPDATE commitment_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("aud")["valid"])

    def test_pause_requires_household_confirmation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.register_pause("ops", "c1", {
                "pause_id": "p1", "household_id": "hh1", "starts_on": "2026-09-05",
                "ends_on": "2026-09-06", "reason_code": "family_extension",
                "evidence": "signed", "confirmed_by_household": False,
            })

    def test_authorization_is_separated_across_three_actors(self) -> None:
        prepared = self._prepare()
        sid = prepared["settlement_id"]
        # 编制人不能批准。
        with self.assertRaises(Forbidden):
            self.service.approve_operations("ops", sid)
        # 财务不能做运营批准，运营经理不能做财务确认。
        with self.assertRaises(Forbidden):
            self.service.approve_operations("fin", sid)
        self.service.approve_operations("manager", sid)
        with self.assertRaises(Forbidden):
            self.service.confirm_finance("manager", sid)
        self.service.confirm_finance("fin", sid)
        stored = self.service.settlement("aud", sid)
        self.assertEqual(stored["state"], "settled")

    def test_settled_result_is_not_rewritten_by_late_event(self) -> None:
        prepared = self._prepare()
        sid = prepared["settlement_id"]
        self.service.approve_operations("manager", sid)
        self.service.confirm_finance("fin", sid)
        original_totals = dict(prepared["result"]["window_totals"])
        # 迟到的可排除事件进入不可变日志，但直接重算被拒绝。
        self.service.record_event("ops", "p1", "b1", event(
            "late-construction", "construction_plan", "*", "2026-09-10",
            notice_at="2026-09-01T08:00:00Z",
            payload={"starts_on": "2026-09-10", "ends_on": "2026-09-10"}))
        with self.assertRaises(InvalidState):
            self.service.prepare_settlement("ops", "c1", "w1", "2026-09-30")
        corrected = self.service.prepare_settlement(
            "ops", "c1", "w1", "2026-09-30", correction_reason="补录迟到的施工告知，按纠错出新版")
        self.assertEqual(corrected["supersedes"], sid)
        self.assertIn("late-construction", corrected["diff"]["new_event_ids"])
        self.assertNotEqual(
            corrected["result"]["window_totals"]["compensation_cny"],
            original_totals["compensation_cny"],
        )
        # 原记录保持不变，仍可离线复算出原结果。
        replay = self.service.replay_settlement("aud", sid)
        self.assertTrue(replay["result_matches"])
        self.assertEqual(replay["stored_window_totals"]["compensation_cny"],
                         original_totals["compensation_cny"])

    def test_recompute_project_batch_covers_all_windows(self) -> None:
        sid = self._prepare()["settlement_id"]
        self.service.approve_operations("manager", sid)
        self.service.confirm_finance("fin", sid)
        report = self.service.recompute_project_batch("aud", "p1", "b1", "2026-09-30")
        self.assertEqual(len(report["windows"]), 1)
        self.assertTrue(report["windows"][0]["latest_stored_settlement"]["result_matches_latest_inputs"])

    def test_new_commitment_version_requires_reason_and_preserves_terms(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.new_commitment_version("ops", "c1", {
                "commitment_id": "c1", "project_id": "p1", "batch_id": "b1",
                "terms": [dict(TERM, promised_days=12)],
                "daily_compensation_cny": "50.00", "advance_notice_days": 5, "notes": "",
            }, "")
        created = self.service.new_commitment_version("ops", "c1", {
            "commitment_id": "c1", "project_id": "p1", "batch_id": "b1",
            "terms": [dict(TERM, promised_days=12)],
            "daily_compensation_cny": "50.00", "advance_notice_days": 5, "notes": "",
            "based_on_version": 1,
        }, "与家庭代表重新约定宽限为 12 天")
        self.assertEqual(created["version_no"], 2)

    def test_api_boundary_health_and_permissions(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/audit/chain")
        self.assertEqual(missing_actor.status, 422)
        forbidden = app.handle("POST", "/commitments/c1/settlements",
                               {"X-Actor-Id": "aud"},
                               b'{"window_id":"w1","as_of_date":"2026-09-30"}')
        self.assertEqual(forbidden.status, 403)


if __name__ == "__main__":
    unittest.main()
