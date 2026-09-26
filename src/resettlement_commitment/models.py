"""安置承诺核算领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .accounting import EVENT_CONSTRUCTION, EVENT_DELIVERY, EVENT_PAUSE, EVENT_POLICY
from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
EVENT_TYPES = {EVENT_CONSTRUCTION, EVENT_POLICY, EVENT_PAUSE, EVENT_DELIVERY}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def optional_date_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return date_text(value, field)


def utc_text_field(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


@dataclass(frozen=True, slots=True)
class CommitmentVersion:
    """承诺版本：同一项目批次下逐版演进，保存承诺时限与补偿基准。"""

    project_id: str
    batch_id: str
    promised_turnover_date: str
    promised_delivery_date: str
    promised_service_date: str
    daily_compensation_cny: Decimal
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentVersion":
        turnover = date_text(raw.get("promised_turnover_date"), "promised_turnover_date")
        delivery = date_text(raw.get("promised_delivery_date"), "promised_delivery_date")
        service = date_text(raw.get("promised_service_date"), "promised_service_date")
        if delivery < turnover:
            raise ValidationFailed("正式交房时限不能早于临时周转时限")
        if service < delivery:
            raise ValidationFailed("公共服务接续时限不能早于正式交房时限")
        return cls(
            project_id=identifier(raw.get("project_id"), "project_id"),
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            promised_turnover_date=turnover,
            promised_delivery_date=delivery,
            promised_service_date=service,
            daily_compensation_cny=decimal_value(
                raw.get("daily_compensation_cny"), "daily_compensation_cny", minimum=Decimal("0.01")
            ),
            note=required_text(raw.get("note"), "note", 512),
        )


@dataclass(frozen=True, slots=True)
class Household:
    household_id: str
    project_id: str
    batch_id: str
    head_name: str
    members: int
    enrolled_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Household":
        return cls(
            household_id=identifier(raw.get("household_id"), "household_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            head_name=required_text(raw.get("head_name"), "head_name", 64),
            members=positive_integer(raw.get("members", 1), "members"),
            enrolled_at=utc_text_field(raw.get("enrolled_at"), "enrolled_at"),
        )


@dataclass(frozen=True, slots=True)
class SettlementWindow:
    window_id: str
    project_id: str
    batch_id: str
    window_start: str
    window_end: str
    daily_rate_cny: Decimal
    label: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SettlementWindow":
        start = date_text(raw.get("window_start"), "window_start")
        end = date_text(raw.get("window_end"), "window_end")
        if end < start:
            raise ValidationFailed("统计窗口结束日期不能早于开始日期")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            window_start=start,
            window_end=end,
            daily_rate_cny=decimal_value(
                raw.get("daily_rate_cny"), "daily_rate_cny", minimum=Decimal("0")
            ),
            label=required_text(raw.get("label"), "label", 128),
        )


@dataclass(frozen=True, slots=True)
class ExclusionRule:
    """排除规则：计划施工必须提前告知且落在约定窗口内才可排除。"""

    rule_id: str
    window_id: str
    kind: str
    min_advance_notice_days: int
    allowed_reason_codes: tuple[str, ...]
    effective_start: str | None
    effective_end: str | None
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExclusionRule":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in {"construction_plan", "policy_exemption", "household_deferral"}:
            raise ValidationFailed("kind 必须是 construction_plan、policy_exemption 或 household_deferral")
        notice = raw.get("min_advance_notice_days", 0)
        if isinstance(notice, bool) or not isinstance(notice, int) or not 0 <= notice <= 3650:
            raise ValidationFailed("min_advance_notice_days 必须是 0 到 3650 的整数")
        reasons_raw = raw.get("allowed_reason_codes", [])
        if not isinstance(reasons_raw, (list, tuple)):
            raise ValidationFailed("allowed_reason_codes 必须是数组")
        reasons = tuple(required_text(code, "allowed_reason_codes", 32) for code in reasons_raw)
        start = optional_date_text(raw.get("effective_start"), "effective_start")
        end = optional_date_text(raw.get("effective_end"), "effective_end")
        if start is not None and end is not None and end < start:
            raise ValidationFailed("effective_end 不能早于 effective_start")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            window_id=identifier(raw.get("window_id"), "window_id"),
            kind=kind,
            min_advance_notice_days=notice,
            allowed_reason_codes=reasons,
            effective_start=start,
            effective_end=end,
            note=required_text(raw.get("note"), "note", 256),
        )


@dataclass(frozen=True, slots=True)
class CommitmentEvent:
    """不可变履约事件：政策豁免、家庭暂停、计划施工与实际交房。"""

    event_id: str
    project_id: str
    batch_id: str
    event_type: str
    household_id: str | None
    event_date: str
    end_date: str | None
    advance_notice_days: int | None
    reason_code: str | None
    confirmed: bool
    note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentEvent":
        event_type = required_text(raw.get("event_type"), "event_type", 32)
        if event_type not in EVENT_TYPES:
            raise ValidationFailed("event_type 不是受支持的履约事件类型")
        household_raw = raw.get("household_id")
        household_id = None if household_raw is None else identifier(household_raw, "household_id")
        event_date = date_text(raw.get("event_date"), "event_date")
        end_date = optional_date_text(raw.get("end_date"), "end_date")
        if end_date is not None and end_date <= event_date:
            raise ValidationFailed("end_date 必须晚于 event_date")
        notice_raw = raw.get("advance_notice_days")
        notice: int | None
        if notice_raw is None:
            notice = None
        else:
            if isinstance(notice_raw, bool) or not isinstance(notice_raw, int) or not 0 <= notice_raw <= 3650:
                raise ValidationFailed("advance_notice_days 必须是 0 到 3650 的整数")
            notice = notice_raw
        confirmed_raw = raw.get("confirmed", False)
        if not isinstance(confirmed_raw, bool):
            raise ValidationFailed("confirmed 必须是布尔值")
        if event_type == EVENT_DELIVERY:
            if household_id is None:
                raise ValidationFailed("交房事件必须指定 household_id")
            if end_date is not None:
                raise ValidationFailed("交房事件不允许 end_date")
        if event_type == EVENT_PAUSE:
            if household_id is None:
                raise ValidationFailed("家庭暂停事件必须指定 household_id")
            if end_date is None:
                raise ValidationFailed("家庭暂停事件必须给出 end_date 暂停区间")
        if event_type == EVENT_CONSTRUCTION and notice is None:
            raise ValidationFailed("计划施工事件必须给出 advance_notice_days")
        reason_raw = raw.get("reason_code")
        reason_code = None if reason_raw is None else required_text(reason_raw, "reason_code", 32)
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            event_type=event_type,
            household_id=household_id,
            event_date=event_date,
            end_date=end_date,
            advance_notice_days=notice,
            reason_code=reason_code,
            confirmed=confirmed_raw,
            note=required_text(raw.get("note", "无"), "note", 256),
        )
