"""安置承诺履约核算的事务用例。

承诺按版本保存；履约事件只允许插入（数据库触发器同时禁止更新与删除）；
周期结算后迟到事件不能直接改写已关闭结果，只能形成带差异原因的纠错新版本；
补偿确认由安置运营（placement_admin）与财务（finance）分离授权。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .accounting import (
    canonical_json,
    compute_window,
    decimal_text,
    diff_results,
    digest,
)
from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import CommitmentEvent, CommitmentVersion, ExclusionRule, Household, SettlementWindow
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "placement_admin": {
        "commitment.write", "household.write", "window.write", "rule.write", "event.write",
        "settlement.run", "settlement.correct", "approval.operations", "report.read",
    },
    "finance": {"approval.finance", "report.read"},
    "auditor": {"report.read", "audit.read"},
}


class CommitmentService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM commitment_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM commitment_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO commitment_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    # ---------------------------------------------------------------- 用户

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO commitment_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------- 承诺版本

    def publish_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "commitment.write")
        commitment = CommitmentVersion.from_dict(raw)
        content_json = canonical_json(raw)
        content_sha256 = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        duplicate = self.connection.execute(
            "SELECT version_no FROM commitment_versions WHERE project_id=? AND batch_id=? AND content_sha256=?",
            (commitment.project_id, commitment.batch_id, content_sha256),
        ).fetchone()
        if duplicate is not None:
            raise Conflict("相同内容的承诺版本已经存在")
        with transaction(self.connection, immediate=True):
            previous = self.connection.execute(
                "SELECT version_no FROM commitment_versions WHERE project_id=? AND batch_id=? "
                "ORDER BY version_no DESC LIMIT 1",
                (commitment.project_id, commitment.batch_id),
            ).fetchone()
            version_no = 1 if previous is None else int(previous["version_no"]) + 1
            if previous is not None:
                self.connection.execute(
                    "UPDATE commitment_versions SET state='superseded' "
                    "WHERE project_id=? AND batch_id=? AND state='active'",
                    (commitment.project_id, commitment.batch_id),
                )
            self.connection.execute(
                "INSERT INTO commitment_versions(project_id,batch_id,version_no,promised_turnover_date,"
                "promised_delivery_date,promised_service_date,daily_compensation_cny,note,content_json,"
                "content_sha256,supersedes_version_no,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    commitment.project_id, commitment.batch_id, version_no,
                    commitment.promised_turnover_date, commitment.promised_delivery_date,
                    commitment.promised_service_date, decimal_text(commitment.daily_compensation_cny),
                    commitment.note, content_json, content_sha256,
                    None if previous is None else int(previous["version_no"]),
                    actor_id, self._now(),
                ),
            )
            self._audit(
                "commitment", f"{commitment.project_id}:{commitment.batch_id}",
                "commitment.published", actor_id,
                {"version_no": version_no, "sha256": content_sha256},
            )
        return {
            "project_id": commitment.project_id,
            "batch_id": commitment.batch_id,
            "version_no": version_no,
            "state": "active",
            "sha256": content_sha256,
        }

    def _active_commitment(self, project_id: str, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM commitment_versions WHERE project_id=? AND batch_id=? AND state='active' "
            "ORDER BY version_no DESC LIMIT 1",
            (project_id, batch_id),
        ).fetchone()
        if row is None:
            raise NotFound("该项目批次没有生效中的承诺版本")
        return row

    def commitment_history(self, project_id: str, batch_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT version_no,promised_turnover_date,promised_delivery_date,promised_service_date,"
            "daily_compensation_cny,note,content_sha256,supersedes_version_no,state,created_by,created_at "
            "FROM commitment_versions WHERE project_id=? AND batch_id=? ORDER BY version_no",
            (project_id, batch_id),
        ).fetchall()
        if not rows:
            raise NotFound("该项目批次没有承诺版本")
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- 家庭

    def register_household(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "household.write")
        household = Household.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resettlement_households(household_id,project_id,batch_id,head_name,"
                    "members,enrolled_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        household.household_id, household.project_id, household.batch_id,
                        household.head_name, household.members, household.enrolled_at,
                        actor_id, self._now(),
                    ),
                )
                self._audit("household", household.household_id, "household.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("家庭编号已经存在") from exc
        return {"household_id": household.household_id, "project_id": household.project_id,
                "batch_id": household.batch_id}

    # ----------------------------------------------------------- 窗口与规则

    def create_window(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "window.write")
        window = SettlementWindow.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO settlement_windows(window_id,project_id,batch_id,window_start,"
                    "window_end,daily_rate_cny,label,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        window.window_id, window.project_id, window.batch_id, window.window_start,
                        window.window_end, decimal_text(window.daily_rate_cny), window.label,
                        actor_id, self._now(),
                    ),
                )
                self._audit("window", window.window_id, "window.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("统计窗口编号已经存在") from exc
        return {"window_id": window.window_id, "state": "open"}

    def add_exclusion_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "rule.write")
        rule = ExclusionRule.from_dict(raw)
        window = self.connection.execute(
            "SELECT * FROM settlement_windows WHERE window_id=?", (rule.window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("统计窗口不存在")
        if rule.effective_start is not None and rule.effective_start > window["window_end"]:
            raise ValidationFailed("规则生效起点不能晚于统计窗口结束日期")
        if rule.effective_end is not None and rule.effective_end < window["window_start"]:
            raise ValidationFailed("规则生效终点不能早于统计窗口开始日期")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO exclusion_rules(rule_id,window_id,kind,min_advance_notice_days,"
                    "allowed_reason_codes_json,effective_start,effective_end,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        rule.rule_id, rule.window_id, rule.kind, rule.min_advance_notice_days,
                        canonical_json(list(rule.allowed_reason_codes)), rule.effective_start,
                        rule.effective_end, rule.note, actor_id, self._now(),
                    ),
                )
                self._audit("exclusion_rule", rule.rule_id, "rule.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("排除规则编号冲突或窗口不存在") from exc
        return {"rule_id": rule.rule_id, "window_id": rule.window_id, "kind": rule.kind}

    # ----------------------------------------------------------- 不可变事件

    def record_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        event = CommitmentEvent.from_dict(raw)
        content_sha256 = digest(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO commitment_events(event_id,project_id,batch_id,event_type,household_id,"
                    "event_date,end_date,advance_notice_days,reason_code,confirmed,note,content_sha256,"
                    "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id, event.project_id, event.batch_id, event.event_type,
                        event.household_id, event.event_date, event.end_date, event.advance_notice_days,
                        event.reason_code, 1 if event.confirmed else 0, event.note, content_sha256,
                        actor_id, self._now(),
                    ),
                )
                self._audit("commitment_event", event.event_id, "event.recorded", actor_id,
                            {"event_type": event.event_type, "sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件编号已经存在；履约事件不可变，纠错须形成结算新版本") from exc
        return {"event_id": event.event_id, "event_type": event.event_type, "sha256": content_sha256}

    # --------------------------------------------------------------- 核算

    @staticmethod
    def _window_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "window_id": row["window_id"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "daily_rate_cny": row["daily_rate_cny"],
            "label": row["label"],
        }

    @staticmethod
    def _commitment_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version_no": row["version_no"],
            "promised_turnover_date": row["promised_turnover_date"],
            "promised_delivery_date": row["promised_delivery_date"],
            "promised_service_date": row["promised_service_date"],
            "daily_compensation_cny": row["daily_compensation_cny"],
        }

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "household_id": row["household_id"],
            "event_date": row["event_date"],
            "end_date": row["end_date"],
            "advance_notice_days": row["advance_notice_days"],
            "reason_code": row["reason_code"],
            "confirmed": bool(row["confirmed"]),
            "note": row["note"],
        }

    @staticmethod
    def _rule_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "rule_id": row["rule_id"],
            "kind": row["kind"],
            "min_advance_notice_days": row["min_advance_notice_days"],
            "allowed_reason_codes": json.loads(row["allowed_reason_codes_json"]),
            "effective_start": row["effective_start"],
            "effective_end": row["effective_end"],
        }

    def _gather_inputs(self, window: sqlite3.Row, commitment: sqlite3.Row) -> dict[str, Any]:
        households = self.connection.execute(
            "SELECT * FROM resettlement_households WHERE project_id=? AND batch_id=? ORDER BY household_id",
            (window["project_id"], window["batch_id"]),
        ).fetchall()
        events = self.connection.execute(
            "SELECT * FROM commitment_events WHERE project_id=? AND batch_id=? ORDER BY event_date,event_id",
            (window["project_id"], window["batch_id"]),
        ).fetchall()
        rules = self.connection.execute(
            "SELECT * FROM exclusion_rules WHERE window_id=? ORDER BY rule_id",
            (window["window_id"],),
        ).fetchall()
        return {
            "window": self._window_dict(window),
            "commitment": self._commitment_dict(commitment),
            "households": [{"household_id": row["household_id"]} for row in households],
            "events": [self._event_dict(row) for row in events],
            "rules": [self._rule_dict(row) for row in rules],
        }

    def _get_window(self, window_id: str) -> sqlite3.Row:
        window = self.connection.execute(
            "SELECT * FROM settlement_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("统计窗口不存在")
        return window

    def _latest_run(self, window_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM settlement_runs WHERE window_id=? ORDER BY settlement_version_no DESC LIMIT 1",
            (window_id,),
        ).fetchone()

    @staticmethod
    def _evaluate(basis: Mapping[str, Any]) -> dict[str, Any]:
        return compute_window(
            window=basis["window"],
            commitment=basis["commitment"],
            households=basis["households"],
            events=basis["events"],
            rules=basis["rules"],
        )

    def settle_window(self, actor_id: str, window_id: str) -> dict[str, Any]:
        self._require(actor_id, "settlement.run")
        window = self._get_window(window_id)
        commitment = self._active_commitment(window["project_id"], window["batch_id"])
        basis = self._gather_inputs(window, commitment)
        basis["commitment"]["version_no"] = commitment["version_no"]
        input_sha256 = digest(basis)
        latest = self._latest_run(window_id)
        if latest is not None:
            if latest["input_sha256"] == input_sha256:
                return {"run_id": latest["run_id"], **json.loads(latest["result_json"]),
                        "settlement_version_no": latest["settlement_version_no"], "replayed": True}
            raise InvalidState(
                "窗口已结算且出现迟到事件或新版本承诺，不得直接改写；请调用纠错形成新版本"
            )
        result = self._evaluate(basis)
        result["settlement_version_no"] = 1
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO settlement_runs(window_id,project_id,batch_id,commitment_version_no,"
                "settlement_version_no,state,input_sha256,event_basis_json,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?, 'closed', ?,?,?,?,?)",
                (
                    window_id, window["project_id"], window["batch_id"], commitment["version_no"],
                    1, input_sha256, canonical_json(basis), canonical_json(result), actor_id, self._now(),
                ),
            )
            run_id = int(cursor.lastrowid)
            self._open_approval(run_id, result["net_compensation_cny"], actor_id)
            self._audit("settlement_run", str(run_id), "settlement.closed", actor_id,
                        {"window_id": window_id, "version_no": 1, "input_sha256": input_sha256})
        return {"run_id": run_id, **result, "replayed": False}

    def _open_approval(self, run_id: int, amount: str, actor_id: str) -> None:
        self.connection.execute(
            "INSERT INTO compensation_approvals(run_id,net_compensation_cny,created_by,created_at) "
            "VALUES(?,?,?,?)",
            (run_id, amount, actor_id, self._now()),
        )

    def _approval(self, run_id: int) -> sqlite3.Row:
        approval = self.connection.execute(
            "SELECT * FROM compensation_approvals WHERE run_id=?", (run_id,)
        ).fetchone()
        if approval is None:
            raise NotFound("该结算没有补偿确认单")
        return approval

    def correct_window(self, actor_id: str, window_id: str, reason: str) -> dict[str, Any]:
        """迟到事件或承诺换版后发起纠错：旧版本标记 corrected，新版本带差异。"""
        self._require(actor_id, "settlement.correct")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("纠错必须说明差异原因")
        window = self._get_window(window_id)
        previous = self._latest_run(window_id)
        if previous is None:
            raise InvalidState("窗口尚未结算，无需纠错")
        approval = self._approval(previous["run_id"])
        if approval["state"] == "finance_confirmed":
            raise InvalidState("上一版本已由财务确认，不能再纠错；需另行冲销处理")
        commitment = self._active_commitment(window["project_id"], window["batch_id"])
        basis = self._gather_inputs(window, commitment)
        basis["commitment"]["version_no"] = commitment["version_no"]
        input_sha256 = digest(basis)
        if input_sha256 == previous["input_sha256"]:
            raise InvalidState("核算输入与上一版本完全一致，没有需要纠错的差异")
        previous_result = json.loads(previous["result_json"])
        result = self._evaluate(basis)
        new_version_no = int(previous["settlement_version_no"]) + 1
        result["settlement_version_no"] = new_version_no
        comparison = diff_results(previous_result, result, reason=reason.strip())
        comparison["previous_window_version"] = int(previous["settlement_version_no"])
        comparison["current_window_version"] = new_version_no
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE settlement_runs SET state='corrected' WHERE run_id=?",
                (previous["run_id"],),
            )
            self.connection.execute(
                "UPDATE compensation_approvals SET state='rejected',reject_reason='settlement_corrected' "
                "WHERE run_id=? AND state!='finance_confirmed'",
                (previous["run_id"],),
            )
            cursor = self.connection.execute(
                "INSERT INTO settlement_runs(window_id,project_id,batch_id,commitment_version_no,"
                "settlement_version_no,state,input_sha256,event_basis_json,result_json,diff_json,"
                "correction_reason,supersedes_run_id,created_by,created_at) "
                "VALUES(?,?,?,?,?, 'closed', ?,?,?,?,?,?,?,?)",
                (
                    window_id, window["project_id"], window["batch_id"], commitment["version_no"],
                    new_version_no, input_sha256, canonical_json(basis), canonical_json(result),
                    canonical_json(comparison), reason.strip(), previous["run_id"], actor_id, self._now(),
                ),
            )
            run_id = int(cursor.lastrowid)
            self._open_approval(run_id, result["net_compensation_cny"], actor_id)
            self._audit("settlement_run", str(run_id), "settlement.corrected", actor_id,
                        {"window_id": window_id, "version_no": new_version_no,
                         "supersedes_run_id": previous["run_id"], "reason": reason.strip()})
        return {"run_id": run_id, **result, "diff": comparison, "replayed": False}

    # ----------------------------------------------------------- 分离授权

    def confirm_operations(self, actor_id: str, run_id: int) -> dict[str, Any]:
        self._require(actor_id, "approval.operations")
        approval = self._approval(run_id)
        if approval["state"] != "pending":
            raise InvalidState(f"安置运营确认要求确认单处于 pending，当前为 {approval['state']}")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE compensation_approvals SET state='operations_confirmed',operations_by=?,"
                "operations_at=? WHERE approval_id=? AND state='pending'",
                (actor_id, self._now(), approval["approval_id"]),
            )
            self._audit("compensation_approval", str(run_id), "approval.operations_confirmed", actor_id, {})
        return self.approval_status(actor_id, run_id)

    def confirm_finance(self, actor_id: str, run_id: int) -> dict[str, Any]:
        self._require(actor_id, "approval.finance")
        approval = self._approval(run_id)
        if approval["state"] != "operations_confirmed":
            raise InvalidState("财务确认前必须先由安置运营确认")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE compensation_approvals SET state='finance_confirmed',finance_by=?,finance_at=? "
                "WHERE approval_id=? AND state='operations_confirmed'",
                (actor_id, self._now(), approval["approval_id"]),
            )
            self._audit("compensation_approval", str(run_id), "approval.finance_confirmed", actor_id, {})
        return self.approval_status(actor_id, run_id)

    def reject_finance(self, actor_id: str, run_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "approval.finance")
        approval = self._approval(run_id)
        if approval["state"] != "operations_confirmed":
            raise InvalidState("只有安置运营已确认的单据可以被财务驳回")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("驳回必须填写原因")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE compensation_approvals SET state='rejected',reject_reason=?,finance_by=?,finance_at=? "
                "WHERE approval_id=?",
                (reason.strip(), actor_id, self._now(), approval["approval_id"]),
            )
            self._audit("compensation_approval", str(run_id), "approval.finance_rejected", actor_id,
                        {"reason": reason.strip()})
        return self.approval_status(actor_id, run_id)

    def approval_status(self, actor_id: str, run_id: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        approval = self._approval(run_id)
        return {
            "run_id": run_id,
            "state": approval["state"],
            "net_compensation_cny": approval["net_compensation_cny"],
            "operations_by": approval["operations_by"],
            "operations_at": approval["operations_at"],
            "finance_by": approval["finance_by"],
            "finance_at": approval["finance_at"],
            "reject_reason": approval["reject_reason"],
        }

    # ------------------------------------------------- 解释与离线复算

    def run_detail(self, actor_id: str, run_id: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM settlement_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算版本不存在")
        result = json.loads(row["result_json"])
        return {
            "run_id": run_id,
            "window_id": row["window_id"],
            "project_id": row["project_id"],
            "batch_id": row["batch_id"],
            "commitment_version_no": row["commitment_version_no"],
            "settlement_version_no": row["settlement_version_no"],
            "state": row["state"],
            "input_sha256": row["input_sha256"],
            "correction_reason": row["correction_reason"],
            "diff": None if row["diff_json"] is None else json.loads(row["diff_json"]),
            **result,
        }

    def explain_run(self, actor_id: str, run_id: int) -> dict[str, Any]:
        """解释每次扣减或排除来自哪段事件、为何被接受或拒绝。"""
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM settlement_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算版本不存在")
        basis = json.loads(row["event_basis_json"])
        events = {str(item["event_id"]): item for item in basis["events"]}
        rules = {str(item["rule_id"]): item for item in basis["rules"]}
        result = json.loads(row["result_json"])
        deductions: list[dict[str, Any]] = []
        for household in result["households"]:
            for fragment in household["fragments"]:
                event = events[str(fragment["event_id"])]
                rule = rules[str(fragment["rule_id"])]
                deductions.append({
                    "household_id": household["household_id"],
                    "decision": "excluded",
                    "exclusion_kind": fragment["exclusion_kind"],
                    "rule_id": fragment["rule_id"],
                    "event_id": fragment["event_id"],
                    "segment_start": fragment["start_date"],
                    "segment_end": fragment["end_date"],
                    "days": fragment["days"],
                    "deducted_cny": fragment["amount_cny"],
                    "event_type": event["event_type"],
                    "event_start": event["event_date"],
                    "event_end": event["end_date"],
                    "advance_notice_days": event["advance_notice_days"],
                    "required_notice_days": rule["min_advance_notice_days"],
                    "reason_code": event["reason_code"],
                    "household_confirmed": event["confirmed"],
                    "note": event["note"],
                })
        return {
            "run_id": run_id,
            "window_id": row["window_id"],
            "households": result["households"],
            "deductions": deductions,
            "rejected_exclusions": result["rejected_exclusions"],
        }

    def export_snapshot(self, actor_id: str, project_id: str, batch_id: str) -> dict[str, Any]:
        """导出项目批次的全部核算输入与历史结果，供离线复算。"""
        self._require(actor_id, "report.read")
        commitments = self.connection.execute(
            "SELECT * FROM commitment_versions WHERE project_id=? AND batch_id=? ORDER BY version_no",
            (project_id, batch_id),
        ).fetchall()
        if not commitments:
            raise NotFound("该项目批次没有承诺版本")
        windows = self.connection.execute(
            "SELECT * FROM settlement_windows WHERE project_id=? AND batch_id=? ORDER BY window_start",
            (project_id, batch_id),
        ).fetchall()
        events = self.connection.execute(
            "SELECT * FROM commitment_events WHERE project_id=? AND batch_id=? ORDER BY event_date,event_id",
            (project_id, batch_id),
        ).fetchall()
        households = self.connection.execute(
            "SELECT * FROM resettlement_households WHERE project_id=? AND batch_id=? ORDER BY household_id",
            (project_id, batch_id),
        ).fetchall()
        snapshot: dict[str, Any] = {
            "project_id": project_id,
            "batch_id": batch_id,
            "exported_at": self._now(),
            "commitments": [self._commitment_dict(row) for row in commitments],
            "households": [{"household_id": row["household_id"]} for row in households],
            "events": [self._event_dict(row) for row in events],
            "windows": [],
        }
        for window in windows:
            rules = self.connection.execute(
                "SELECT * FROM exclusion_rules WHERE window_id=? ORDER BY rule_id",
                (window["window_id"],)
            ).fetchall()
            runs = self.connection.execute(
                "SELECT run_id,commitment_version_no,settlement_version_no,state,input_sha256,"
                "event_basis_json,result_json,diff_json,correction_reason FROM settlement_runs "
                "WHERE window_id=? ORDER BY settlement_version_no",
                (window["window_id"],)
            ).fetchall()
            snapshot["windows"].append({
                "window": self._window_dict(window),
                "rules": [self._rule_dict(row) for row in rules],
                "runs": [
                    {
                        "run_id": row["run_id"],
                        "commitment_version_no": row["commitment_version_no"],
                        "settlement_version_no": row["settlement_version_no"],
                        "state": row["state"],
                        "input_sha256": row["input_sha256"],
                        "basis": json.loads(row["event_basis_json"]),
                        "result": json.loads(row["result_json"]),
                        "diff": None if row["diff_json"] is None else json.loads(row["diff_json"]),
                        "correction_reason": row["correction_reason"],
                    }
                    for row in runs
                ],
            })
        return snapshot

    def replay_batch(self, actor_id: str, project_id: str, batch_id: str) -> dict[str, Any]:
        """按项目批次离线复算：校验每个已存版本，并用当前事件重算以发现待纠错差异。"""
        self._require(actor_id, "report.read")
        snapshot = self.export_snapshot(actor_id, project_id, batch_id)
        return recompute_snapshot(snapshot)

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM commitment_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}


def recompute_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """纯离线复算：不访问数据库，按快照重放全部窗口的全部结算版本。

    - ``stored_versions_match``：每个已存版本用其核算基要重算是否仍得到同一结果；
    - ``current``：用最新承诺版本与当前全部事件重算，与最新版本不一致即说明
      出现迟到事件或承诺换版，需要发起纠错新版本。
    """
    commitments = {int(item["version_no"]): item for item in snapshot["commitments"]}
    latest_commitment = commitments[max(commitments)]
    window_reports: list[dict[str, Any]] = []
    all_stored_match = True
    pending_corrections = 0
    for block in snapshot["windows"]:
        window = block["window"]
        current_rules = block["rules"]
        current_households = snapshot["households"]
        current_events = snapshot["events"]
        version_reports = []
        latest_run = None
        for run in block["runs"]:
            # 已存版本必须用该版本自己的核算基要（当时的承诺/事件/规则）复算。
            basis = run["basis"]
            recomputed = compute_window(
                window=basis["window"],
                commitment=basis["commitment"],
                households=basis["households"],
                events=basis["events"],
                rules=basis["rules"],
            )
            recomputed["settlement_version_no"] = run["settlement_version_no"]
            stored_result = run["result"]
            stored_match = canonical_json(recomputed) == canonical_json(stored_result)
            input_match = digest(basis) == run["input_sha256"]
            all_stored_match = all_stored_match and stored_match and input_match
            version_reports.append({
                "settlement_version_no": run["settlement_version_no"],
                "run_id": run["run_id"],
                "state": run["state"],
                "commitment_version_no": run["commitment_version_no"],
                "result_matches_stored": stored_match,
                "input_basis_matches_stored": input_match,
            })
            latest_run = run
        # 当前视图：最新承诺版本 + 当前全部事件与规则。
        current = compute_window(
            window=window, commitment=latest_commitment, households=current_households,
            events=current_events, rules=current_rules,
        )
        pending = False
        if latest_run is not None and latest_run["state"] == "closed":
            pending = canonical_json(
                {k: current[k] for k in current if k != "settlement_version_no"}
            ) != canonical_json(
                {k: v for k, v in latest_run["result"].items() if k != "settlement_version_no"}
            ) or latest_commitment["version_no"] != latest_run["commitment_version_no"]
        if pending:
            pending_corrections += 1
        window_reports.append({
            "window_id": window["window_id"],
            "window_start": window["window_start"],
            "window_end": window["window_end"],
            "stored_versions": version_reports,
            "latest_version_state": None if latest_run is None else latest_run["state"],
            "correction_pending": pending,
            "current_net_compensation_cny": current["net_compensation_cny"],
            "current_affected_households": current["affected_households"],
        })
    return {
        "project_id": snapshot["project_id"],
        "batch_id": snapshot["batch_id"],
        "latest_commitment_version_no": latest_commitment["version_no"],
        "stored_versions_match": all_stored_match,
        "windows_pending_correction": pending_corrections,
        "windows": window_reports,
    }
