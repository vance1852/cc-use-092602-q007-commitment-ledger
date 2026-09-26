"""安置承诺核算的事务用例。

结算生命周期：
    preliminary（经办编制）
      -> operations_approved（安置运营负责人批准，编制人不得批准）
      -> settled（财务确认补偿；与运营授权角色完全分离）
周期结算一旦达到 operations_approved/settled，迟到事件不会改写原记录，
只能以新结算版本纠错，并保留与上一版的差异原因和逐户差异。
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
from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    CommitmentVersion,
    DomainEvent,
    ExclusionRule,
    HouseholdPause,
    Window,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "resettlement": {
        "catalog.write",
        "event.write",
        "pause.write",
        "settlement.prepare",
        "report.read",
    },
    "resettlement_manager": {
        "catalog.write",
        "settlement.operations_approve",
        "report.read",
    },
    "finance": {"settlement.finance_confirm", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

_TERMINAL_REVISION_STATES = ("operations_approved", "settled")


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
            "INSERT INTO commitment_audit_events(entity_type,entity_id,event_type,actor_id,"
            "payload_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ---- 账号 ----------------------------------------------------------

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

    # ---- 承诺版本 ------------------------------------------------------

    def _get_commitment(self, commitment_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("安置承诺不存在")
        return row

    def create_commitment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        commitment = CommitmentVersion.from_dict(raw)
        definition = self._definition_dict(commitment, version_no=1)
        content_sha = digest(definition)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO commitments(commitment_id,project_id,batch_id,"
                    "current_version_no,created_at) VALUES(?,?,?,?,?)",
                    (
                        commitment.commitment_id,
                        commitment.project_id,
                        commitment.batch_id,
                        1,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO commitment_versions(commitment_id,version_no,based_on_version,"
                    "definition_json,content_sha256,correction_reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        commitment.commitment_id,
                        1,
                        None,
                        canonical_json(definition),
                        content_sha,
                        "",
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "commitment",
                    commitment.commitment_id,
                    "commitment.created",
                    actor_id,
                    {"project_id": commitment.project_id, "batch_id": commitment.batch_id, "sha256": content_sha},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("项目/批次下已存在承诺或承诺编号冲突") from exc
        return {
            "commitment_id": commitment.commitment_id,
            "version_no": 1,
            "state": "active",
            "sha256": content_sha,
        }

    def new_commitment_version(
        self,
        actor_id: str,
        commitment_id: str,
        raw: Mapping[str, Any],
        correction_reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        commitment_row = self._get_commitment(commitment_id)
        candidate = CommitmentVersion.from_dict(raw)
        if candidate.commitment_id != commitment_id:
            raise ValidationFailed("承诺编号与路径不一致")
        if (candidate.project_id, candidate.batch_id) != (
            commitment_row["project_id"],
            commitment_row["batch_id"],
        ):
            raise ValidationFailed("新项目与批次不能更改，只能建立新承诺")
        reason = correction_reason.strip()
        if not reason:
            raise ValidationFailed("新版本必须说明修订或差异原因")
        next_version = int(commitment_row["current_version_no"]) + 1
        expected_base = next_version - 1
        if candidate.based_on_version is not None and candidate.based_on_version != expected_base:
            raise Conflict(f"新版本必须基于当前版本 {expected_base}")
        definition = self._definition_dict(candidate, version_no=next_version)
        content_sha = digest(definition)
        prior = self.connection.execute(
            "SELECT content_sha256 FROM commitment_versions WHERE commitment_id=? AND version_no=?",
            (commitment_id, expected_base),
        ).fetchone()
        if prior is not None and prior["content_sha256"] == content_sha:
            raise Conflict("新版本内容与当前版本完全相同，没有差异")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO commitment_versions(commitment_id,version_no,based_on_version,"
                    "definition_json,content_sha256,correction_reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        commitment_id,
                        next_version,
                        expected_base,
                        canonical_json(definition),
                        content_sha,
                        reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE commitments SET current_version_no=? WHERE commitment_id=?",
                    (next_version, commitment_id),
                )
                self._audit(
                    "commitment",
                    commitment_id,
                    "commitment.version_created",
                    actor_id,
                    {"version_no": next_version, "based_on_version": expected_base, "reason": reason},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("承诺版本冲突") from exc
        return {
            "commitment_id": commitment_id,
            "version_no": next_version,
            "based_on_version": expected_base,
            "sha256": content_sha,
        }

    @staticmethod
    def _definition_dict(commitment: CommitmentVersion, *, version_no: int) -> dict[str, Any]:
        return {
            "commitment_id": commitment.commitment_id,
            "version_no": version_no,
            "project_id": commitment.project_id,
            "batch_id": commitment.batch_id,
            "terms": [
                {
                    "kind": term.kind,
                    "start_event": term.start_event,
                    "end_event": term.end_event,
                    "promised_days": term.promised_days,
                }
                for term in commitment.terms
            ],
            "daily_compensation_cny": decimal_text(commitment.daily_compensation_cny),
            "advance_notice_days": commitment.advance_notice_days,
            "notes": commitment.notes,
        }

    def _load_version(self, commitment_id: str, version_no: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT definition_json FROM commitment_versions WHERE commitment_id=? AND version_no=?",
            (commitment_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound("承诺版本不存在")
        return json.loads(row["definition_json"])

    # ---- 统计窗口与排除规则 -------------------------------------------

    def add_window(self, actor_id: str, commitment_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._get_commitment(commitment_id)
        window = Window.from_dict(raw)
        overlapping = self.connection.execute(
            "SELECT 1 FROM stat_windows WHERE commitment_id=? "
            "AND NOT (ends_on < ? OR starts_on > ?)",
            (commitment_id, window.starts_on, window.ends_on),
        ).fetchone()
        if overlapping is not None:
            raise Conflict("统计窗口不能与同一承诺下的其他窗口重叠")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO stat_windows(window_id,commitment_id,starts_on,ends_on,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        window.window_id,
                        commitment_id,
                        window.starts_on,
                        window.ends_on,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("window", window.window_id, "window.created", actor_id, {
                    "commitment_id": commitment_id,
                    "starts_on": window.starts_on,
                    "ends_on": window.ends_on,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("统计窗口编号冲突") from exc
        return {
            "window_id": window.window_id,
            "commitment_id": commitment_id,
            "starts_on": window.starts_on,
            "ends_on": window.ends_on,
        }

    def add_exclusion_rule(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        rule = ExclusionRule.from_dict(raw)
        window = self.connection.execute(
            "SELECT * FROM stat_windows WHERE window_id=?", (rule.window_id,)
        ).fetchone()
        if window is None:
            raise NotFound("统计窗口不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO exclusion_rules(rule_id,window_id,event_kind,"
                    "require_advance_notice,reason,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        rule.rule_id,
                        rule.window_id,
                        rule.event_kind,
                        1 if rule.require_advance_notice else 0,
                        rule.reason,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("exclusion_rule", rule.rule_id, "rule.created", actor_id, {
                    "window_id": rule.window_id,
                    "event_kind": rule.event_kind,
                    "require_advance_notice": rule.require_advance_notice,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("排除规则编号冲突或窗口已有同类规则") from exc
        return {
            "rule_id": rule.rule_id,
            "window_id": rule.window_id,
            "event_kind": rule.event_kind,
            "require_advance_notice": rule.require_advance_notice,
        }

    def deactivate_exclusion_rule(self, actor_id: str, rule_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if not reason.strip():
            raise ValidationFailed("停用规则必须说明原因")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exclusion_rules SET active=0,revision=revision+1 WHERE rule_id=? AND active=1",
                (rule_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("排除规则不存在或已停用")
            self._audit("exclusion_rule", rule_id, "rule.deactivated", actor_id, {"reason": reason.strip()})
        return {"rule_id": rule_id, "active": False}

    # ---- 家庭暂停区间 --------------------------------------------------

    def register_pause(self, actor_id: str, commitment_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "pause.write")
        self._get_commitment(commitment_id)
        pause = HouseholdPause.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO household_pauses(pause_id,commitment_id,household_id,starts_on,"
                    "ends_on,reason_code,evidence,confirmed_by_household,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        pause.pause_id,
                        commitment_id,
                        pause.household_id,
                        pause.starts_on,
                        pause.ends_on,
                        pause.reason_code,
                        pause.evidence,
                        1,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("household_pause", pause.pause_id, "pause.registered", actor_id, {
                    "commitment_id": commitment_id,
                    "household_id": pause.household_id,
                    "starts_on": pause.starts_on,
                    "ends_on": pause.ends_on,
                    "reason_code": pause.reason_code,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("暂停区间编号冲突") from exc
        return {
            "pause_id": pause.pause_id,
            "commitment_id": commitment_id,
            "household_id": pause.household_id,
            "starts_on": pause.starts_on,
            "ends_on": pause.ends_on,
            "reason_code": pause.reason_code,
            "state": "active",
        }

    def close_pause(
        self,
        actor_id: str,
        pause_id: str,
        ends_on: str,
        expected_revision: int,
        confirmed: bool,
    ) -> dict[str, Any]:
        self._require(actor_id, "pause.write")
        row = self.connection.execute(
            "SELECT * FROM household_pauses WHERE pause_id=?", (pause_id,)
        ).fetchone()
        if row is None:
            raise NotFound("暂停区间不存在")
        if not row["active"]:
            raise InvalidState("暂停区间已关闭")
        if row["revision"] != expected_revision:
            raise Conflict("暂停区间已被其他人更新，请使用最新版本")
        if not confirmed:
            raise ValidationFailed("结束暂停区间必须保留家庭确认")
        from .models import date_text
        end_text = date_text(ends_on, "ends_on")
        if end_text < row["starts_on"]:
            raise ValidationFailed("ends_on 不能早于 starts_on")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE household_pauses SET ends_on=?,revision=revision+1 WHERE pause_id=? AND revision=?",
                (end_text, pause_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise Conflict("暂停区间版本冲突")
            self._audit("household_pause", pause_id, "pause.closed", actor_id, {"ends_on": end_text})
        return {"pause_id": pause_id, "ends_on": end_text, "revision": expected_revision + 1, "state": "closed"}

    # ---- 不可变事件 ----------------------------------------------------

    def record_event(self, actor_id: str, project_id: str, batch_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        event = DomainEvent.from_dict(raw)
        recorded_at = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO commitment_events(event_id,project_id,batch_id,event_kind,"
                    "household_id,event_date,observed_at,notice_at,payload_json,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        project_id,
                        batch_id,
                        event.event_kind,
                        event.household_id,
                        event.event_date,
                        event.observed_at,
                        event.notice_at,
                        canonical_json(event.payload),
                        actor_id,
                        recorded_at,
                    ),
                )
                self._audit("domain_event", event.event_id, "event.recorded", actor_id, {
                    "project_id": project_id,
                    "batch_id": batch_id,
                    "event_kind": event.event_kind,
                    "event_date": event.event_date,
                    "recorded_at": recorded_at,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("事件编号已经存在；事件不可修改") from exc
        return {
            "event_id": event.event_id,
            "project_id": project_id,
            "batch_id": batch_id,
            "event_kind": event.event_kind,
            "recorded_at": recorded_at,
            "state": "immutable",
        }

    def list_events(self, actor_id: str, project_id: str, batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT * FROM commitment_events WHERE project_id=? AND batch_id=? "
            "ORDER BY event_date,event_id",
            (project_id, batch_id),
        ).fetchall()
        return {"project_id": project_id, "batch_id": batch_id, "events": [self._event_dict(row) for row in rows]}

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "event_kind": row["event_kind"],
            "household_id": row["household_id"],
            "event_date": row["event_date"],
            "observed_at": row["observed_at"],
            "notice_at": row["notice_at"],
            "payload": json.loads(row["payload_json"]),
            "recorded_by": row["recorded_by"],
            "recorded_at": row["recorded_at"],
        }

    # ---- 结算输入快照 --------------------------------------------------

    def _snapshot_inputs(
        self,
        commitment_row: sqlite3.Row,
        version_no: int,
        as_of_date: str,
        include_observed_until: str,
    ) -> dict[str, Any]:
        commitment_id = commitment_row["commitment_id"]
        windows = [
            {"window_id": row["window_id"], "starts_on": row["starts_on"], "ends_on": row["ends_on"]}
            for row in self.connection.execute(
                "SELECT window_id,starts_on,ends_on FROM stat_windows "
                "WHERE commitment_id=? ORDER BY starts_on,window_id",
                (commitment_id,),
            ).fetchall()
        ]
        rules = [
            {
                "rule_id": row["rule_id"],
                "window_id": row["window_id"],
                "event_kind": row["event_kind"],
                "require_advance_notice": bool(row["require_advance_notice"]),
            }
            for row in self.connection.execute(
                "SELECT rule_id,window_id,event_kind,require_advance_notice FROM exclusion_rules "
                "WHERE active=1 AND window_id IN ("
                "SELECT window_id FROM stat_windows WHERE commitment_id=?) "
                "ORDER BY rule_id",
                (commitment_id,),
            ).fetchall()
        ]
        boundary = parse_utc(include_observed_until, "include_observed_until")
        events: list[dict[str, Any]] = []
        for row in self.connection.execute(
            "SELECT * FROM commitment_events WHERE project_id=? AND batch_id=? "
            "ORDER BY event_date,event_id",
            (commitment_row["project_id"], commitment_row["batch_id"]),
        ).fetchall():
            if row["event_date"] > as_of_date:
                continue
            if parse_utc(row["recorded_at"], "recorded_at") > boundary:
                continue
            events.append(self._event_dict(row))
        pauses = [
            {
                "pause_id": row["pause_id"],
                "household_id": row["household_id"],
                "starts_on": row["starts_on"],
                "ends_on": row["ends_on"],
                "reason_code": row["reason_code"],
                "evidence": row["evidence"],
            }
            for row in self.connection.execute(
                "SELECT * FROM household_pauses WHERE commitment_id=? AND active=1 "
                "AND starts_on<=? AND created_at<=? ORDER BY starts_on,pause_id",
                (commitment_id, as_of_date, include_observed_until),
            ).fetchall()
        ]
        return {
            "version": self._load_version(commitment_id, version_no),
            "windows": windows,
            "rules": rules,
            "events": events,
            "pauses": pauses,
            "as_of_date": as_of_date,
            "include_observed_until": include_observed_until,
        }

    # ---- 周期结算 ------------------------------------------------------

    def prepare_settlement(
        self,
        actor_id: str,
        commitment_id: str,
        window_id: str,
        as_of_date: str,
        include_observed_until: str | None = None,
        correction_reason: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "settlement.prepare")
        commitment_row = self._get_commitment(commitment_id)
        window = self.connection.execute(
            "SELECT * FROM stat_windows WHERE commitment_id=? AND window_id=?",
            (commitment_id, window_id),
        ).fetchone()
        if window is None:
            raise NotFound("统计窗口不存在或不属于该承诺")
        from .models import date_text
        as_of = date_text(as_of_date, "as_of_date")
        if as_of < window["starts_on"]:
            raise ValidationFailed("as_of_date 不能早于窗口起始日")
        boundary = include_observed_until or self._now()
        parse_utc(boundary, "include_observed_until")

        prior = self.connection.execute(
            "SELECT * FROM settlements WHERE commitment_id=? AND window_id=? "
            "ORDER BY settlement_no DESC LIMIT 1",
            (commitment_id, window_id),
        ).fetchone()
        if prior is not None and prior["state"] == "preliminary":
            raise InvalidState("该窗口已有待批准的初步结算，请先完成批准或驳回")
        if prior is not None and prior["state"] in _TERMINAL_REVISION_STATES and not correction_reason.strip():
            raise InvalidState("该窗口已有经运营/财务确认的结算，纠错必须形成新版本并说明差异原因")
        if prior is not None and prior["state"] == "corrected" and not correction_reason.strip():
            raise InvalidState("上一版结算已被纠正，再次编制必须说明差异原因")
        commitment_version_no = int(commitment_row["current_version_no"])
        snapshot = self._snapshot_inputs(commitment_row, commitment_version_no, as_of, boundary)
        result = compute_window(
            version=snapshot["version"],
            windows=snapshot["windows"],
            rules=snapshot["rules"],
            events=snapshot["events"],
            pauses=snapshot["pauses"],
            target_window_id=window_id,
            as_of_date=as_of,
            include_observed_until=boundary,
        )
        next_no = 1 if prior is None else int(prior["settlement_no"]) + 1
        settlement_id = f"{commitment_id}:{window_id}:v{next_no}"
        diff = None
        if prior is not None:
            diff = diff_results(json.loads(prior["result_json"]), result)
            diff["reason"] = correction_reason.strip()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO settlements(settlement_id,project_id,batch_id,commitment_id,"
                "commitment_version_no,window_id,settlement_no,as_of_date,include_observed_until,"
                "state,input_json,result_json,input_sha256,supersedes_settlement_id,"
                "correction_reason,diff_json,prepared_by,prepared_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    settlement_id,
                    commitment_row["project_id"],
                    commitment_row["batch_id"],
                    commitment_id,
                    commitment_version_no,
                    window_id,
                    next_no,
                    as_of,
                    boundary,
                    "preliminary",
                    canonical_json(snapshot),
                    canonical_json(result),
                    result["input_sha256"],
                    None if prior is None else prior["settlement_id"],
                    correction_reason.strip(),
                    None if diff is None else canonical_json(diff),
                    actor_id,
                    self._now(),
                ),
            )
            self._audit("settlement", settlement_id, "settlement.prepared", actor_id, {
                "commitment_id": commitment_id,
                "window_id": window_id,
                "settlement_no": next_no,
                "commitment_version_no": commitment_version_no,
                "as_of_date": as_of,
                "include_observed_until": boundary,
                "supersedes": None if prior is None else prior["settlement_id"],
                "correction_reason": correction_reason.strip(),
            })
        return {
            "settlement_id": settlement_id,
            "settlement_no": next_no,
            "state": "preliminary",
            "supersedes": None if prior is None else prior["settlement_id"],
            "diff": diff,
            "result": result,
        }

    def _load_settlement(self, settlement_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM settlements WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结算记录不存在")
        return row

    def approve_operations(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "settlement.operations_approve")
        row = self._load_settlement(settlement_id)
        if row["state"] != "preliminary":
            raise InvalidState("只有待批准的初步结算可以运营批准")
        if row["prepared_by"] == actor_id:
            raise Forbidden("编制人与运营批准人必须分离")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE settlements SET state='operations_approved',"
                "operations_approved_by=?,operations_approved_at=? "
                "WHERE settlement_id=? AND state='preliminary'",
                (actor_id, self._now(), settlement_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结算状态已变化，请刷新后重试")
            self._audit("settlement", settlement_id, "settlement.operations_approved", actor_id, {
                "prepared_by": row["prepared_by"],
            })
        totals = json.loads(row["result_json"])["window_totals"]
        return {"settlement_id": settlement_id, "state": "operations_approved", "window_totals": totals}

    def confirm_finance(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "settlement.finance_confirm")
        row = self._load_settlement(settlement_id)
        if row["state"] != "operations_approved":
            raise InvalidState("只有经安置运营批准的结算才能由财务确认")
        if row["operations_approved_by"] == actor_id:
            raise Forbidden("财务确认人与运营批准人必须分离")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE settlements SET state='settled',finance_confirmed_by=?,finance_confirmed_at=? "
                "WHERE settlement_id=? AND state='operations_approved'",
                (actor_id, self._now(), settlement_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("结算状态已变化，请刷新后重试")
            # 同窗口的旧版本（草稿、已批准或已确认）在新版本确认后标记为已纠正，原行仍保留。
            self.connection.execute(
                "UPDATE settlements SET state='corrected' WHERE commitment_id=? AND window_id=? "
                "AND settlement_id<>? AND state IN ('preliminary','operations_approved','settled')",
                (row["commitment_id"], row["window_id"], settlement_id),
            )
            self._audit("settlement", settlement_id, "settlement.finance_confirmed", actor_id, {
                "operations_approved_by": row["operations_approved_by"],
            })
        return {
            "settlement_id": settlement_id,
            "state": "settled",
            "window_totals": json.loads(row["result_json"])["window_totals"],
        }

    def settlement(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self._load_settlement(settlement_id)
        return {
            "settlement_id": row["settlement_id"],
            "project_id": row["project_id"],
            "batch_id": row["batch_id"],
            "commitment_id": row["commitment_id"],
            "commitment_version_no": row["commitment_version_no"],
            "settlement_no": row["settlement_no"],
            "window_id": row["window_id"],
            "as_of_date": row["as_of_date"],
            "include_observed_until": row["include_observed_until"],
            "state": row["state"],
            "supersedes": row["supersedes_settlement_id"],
            "correction_reason": row["correction_reason"],
            "diff": None if row["diff_json"] is None else json.loads(row["diff_json"]),
            "prepared_by": row["prepared_by"],
            "operations_approved_by": row["operations_approved_by"],
            "finance_confirmed_by": row["finance_confirmed_by"],
            "result": json.loads(row["result_json"]),
        }

    # ---- 离线复算 ------------------------------------------------------

    def replay_settlement(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        """用结算时冻结的输入快照离线重算，校验已保存结果没有被改写。"""

        self._require(actor_id, "report.read")
        row = self._load_settlement(settlement_id)
        snapshot = json.loads(row["input_json"])
        recomputed = compute_window(
            version=snapshot["version"],
            windows=snapshot["windows"],
            rules=snapshot["rules"],
            events=snapshot["events"],
            pauses=snapshot["pauses"],
            target_window_id=row["window_id"],
            as_of_date=snapshot["as_of_date"],
            include_observed_until=snapshot["include_observed_until"],
        )
        stored_result = json.loads(row["result_json"])
        return {
            "settlement_id": settlement_id,
            "state": row["state"],
            "stored_input_sha256": row["input_sha256"],
            "recomputed_input_sha256": recomputed["input_sha256"],
            "input_matches": row["input_sha256"] == recomputed["input_sha256"],
            "result_matches": canonical_json(stored_result) == canonical_json(recomputed),
            "stored_window_totals": stored_result["window_totals"],
            "recomputed_window_totals": recomputed["window_totals"],
        }

    def recompute_project_batch(
        self,
        actor_id: str,
        project_id: str,
        batch_id: str,
        as_of_date: str,
        include_observed_until: str | None = None,
    ) -> dict[str, Any]:
        """按项目与批次对全部窗口做不落库的离线复算，并核对已有结算。"""

        self._require(actor_id, "report.read")
        from .models import date_text
        as_of = date_text(as_of_date, "as_of_date")
        boundary = include_observed_until or self._now()
        parse_utc(boundary, "include_observed_until")
        commitment_row = self.connection.execute(
            "SELECT * FROM commitments WHERE project_id=? AND batch_id=?",
            (project_id, batch_id),
        ).fetchone()
        if commitment_row is None:
            raise NotFound("项目/批次下没有安置承诺")
        snapshot = self._snapshot_inputs(
            commitment_row, int(commitment_row["current_version_no"]), as_of, boundary
        )
        windows = snapshot["windows"]
        recomputed_windows = []
        for window in windows:
            result = compute_window(
                version=snapshot["version"],
                windows=windows,
                rules=snapshot["rules"],
                events=snapshot["events"],
                pauses=snapshot["pauses"],
                target_window_id=window["window_id"],
                as_of_date=as_of,
                include_observed_until=boundary,
            )
            stored = self.connection.execute(
                "SELECT settlement_id,state,result_json,input_sha256 FROM settlements "
                "WHERE commitment_id=? AND window_id=? ORDER BY settlement_no DESC LIMIT 1",
                (commitment_row["commitment_id"], window["window_id"]),
            ).fetchone()
            recomputed_windows.append({
                "window_id": window["window_id"],
                "window_totals": result["window_totals"],
                "input_sha256": result["input_sha256"],
                "latest_stored_settlement": None
                if stored is None
                else {
                    "settlement_id": stored["settlement_id"],
                    "state": stored["state"],
                    "result_matches_latest_inputs": stored["input_sha256"] == result["input_sha256"],
                },
            })
        return {
            "project_id": project_id,
            "batch_id": batch_id,
            "commitment_id": commitment_row["commitment_id"],
            "commitment_version_no": int(commitment_row["current_version_no"]),
            "as_of_date": as_of,
            "include_observed_until": boundary,
            "windows": recomputed_windows,
            "event_count": len(snapshot["events"]),
            "pause_count": len(snapshot["pauses"]),
        }

    # ---- 审计 ----------------------------------------------------------

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
