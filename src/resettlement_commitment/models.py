"""安置承诺核算的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 不可变事件类型：
#   temporary_keys_handover 临时周转房交接（等待期起点候选）
#   formal_delivery         正式交房（等待期终点候选）
#   public_service_resume   公共服务接续（等待期终点候选）
#   construction_plan       计划施工（只有提前告知且落在约定窗口内才允许排除）
EVENT_KINDS = {
    "temporary_keys_handover",
    "formal_delivery",
    "public_service_resume",
    "construction_plan",
}

# 排除规则的可排除事件类别
EXCLUDABLE_KINDS = {"construction_plan"}

# 承诺的三个时限
PROMISE_KINDS = {"temporary_turnover", "formal_delivery", "public_service_resume"}

# 家庭确认的暂停原因类别
PAUSE_REASONS = {"family_extension", "policy_exemption", "family_unavailable"}


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


def optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return identifier(value, field)


def household_identifier(value: object, field: str) -> str:
    """家庭编号允许使用 * 表示批次级（如集中施工）事件。"""

    if isinstance(value, str) and value == "*":
        return "*"
    return identifier(value, field)


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


def integer_value(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationFailed(f"{field} 必须是 {minimum} 到 {maximum} 的整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def _date_range(start: object, end: object, start_field: str, end_field: str) -> tuple[str, str | None]:
    start_text = date_text(start, start_field)
    end_text = None if end is None else date_text(end, end_field)
    if end_text is not None and end_text < start_text:
        raise ValidationFailed(f"{end_field} 不能早于 {start_field}")
    return start_text, end_text


@dataclass(frozen=True, slots=True)
class Window:
    """统计窗口（含，按自然日闭区间）。"""

    window_id: str
    starts_on: str
    ends_on: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Window":
        starts_on, ends_on = _date_range(
            raw.get("starts_on"), raw.get("ends_on"), "starts_on", "ends_on"
        )
        if ends_on is None:
            raise ValidationFailed("ends_on 不能为空")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            starts_on=starts_on,
            ends_on=ends_on,
        )


@dataclass(frozen=True, slots=True)
class PromiseTerm:
    """单项时限承诺：起点事件、终点事件与承诺天数。"""

    kind: str
    start_event: str
    end_event: str
    promised_days: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PromiseTerm":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in PROMISE_KINDS:
            raise ValidationFailed("kind 必须是 temporary_turnover、formal_delivery 或 public_service_resume")
        start_event = required_text(raw.get("start_event"), "start_event", 48)
        end_event = required_text(raw.get("end_event"), "end_event", 48)
        if start_event not in EVENT_KINDS or end_event not in EVENT_KINDS:
            raise ValidationFailed("时限端点必须是已登记的事件类型")
        if start_event == end_event:
            raise ValidationFailed("时限起点和终点不能相同")
        return cls(
            kind=kind,
            start_event=start_event,
            end_event=end_event,
            promised_days=integer_value(raw.get("promised_days"), "promised_days", minimum=0, maximum=3650),
        )


@dataclass(frozen=True, slots=True)
class CommitmentVersion:
    """承诺版本：项目/批次下的时限、补偿单价与窗口约定，整体不可变。"""

    commitment_id: str
    project_id: str
    batch_id: str
    terms: tuple[PromiseTerm, ...]
    daily_compensation_cny: Decimal
    advance_notice_days: int
    notes: str
    based_on_version: int | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommitmentVersion":
        terms_raw = raw.get("terms")
        if not isinstance(terms_raw, list) or not terms_raw:
            raise ValidationFailed("terms 必须是非空数组")
        terms = tuple(PromiseTerm.from_dict(item) for item in terms_raw)
        kinds = [term.kind for term in terms]
        if len(set(kinds)) != len(kinds):
            raise ValidationFailed("同一承诺版本内时限种类不能重复")
        based_on = raw.get("based_on_version")
        return cls(
            commitment_id=identifier(raw.get("commitment_id"), "commitment_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            terms=terms,
            daily_compensation_cny=decimal_value(
                raw.get("daily_compensation_cny"),
                "daily_compensation_cny",
                minimum=Decimal("0"),
            ),
            advance_notice_days=integer_value(
                raw.get("advance_notice_days", 0), "advance_notice_days", minimum=0, maximum=365
            ),
            notes=required_text(raw.get("notes", ""), "notes", 1024) if raw.get("notes") else "",
            based_on_version=None if based_on is None else integer_value(
                based_on, "based_on_version", minimum=1, maximum=10_000
            ),
        )


@dataclass(frozen=True, slots=True)
class ExclusionRule:
    """排除规则：指定窗口内允许排除的事件类别与提前告知要求。"""

    rule_id: str
    window_id: str
    event_kind: str
    require_advance_notice: bool
    reason: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExclusionRule":
        event_kind = required_text(raw.get("event_kind"), "event_kind", 48)
        if event_kind not in EXCLUDABLE_KINDS:
            raise ValidationFailed("当前只有计划施工事件允许配置排除规则")
        require_notice = raw.get("require_advance_notice", True)
        if not isinstance(require_notice, bool):
            raise ValidationFailed("require_advance_notice 必须是布尔值")
        return cls(
            rule_id=identifier(raw.get("rule_id"), "rule_id"),
            window_id=identifier(raw.get("window_id"), "window_id"),
            event_kind=event_kind,
            require_advance_notice=require_notice,
            reason=required_text(raw.get("reason"), "reason", 512),
        )


@dataclass(frozen=True, slots=True)
class HouseholdPause:
    """家庭确认的暂停区间：逐户、逐原因、带凭据。"""

    pause_id: str
    household_id: str
    starts_on: str
    ends_on: str | None
    reason_code: str
    evidence: str
    confirmed_by_household: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdPause":
        reason_code = required_text(raw.get("reason_code"), "reason_code", 32)
        if reason_code not in PAUSE_REASONS:
            raise ValidationFailed("reason_code 必须是 family_extension、policy_exemption 或 family_unavailable")
        confirmed = raw.get("confirmed_by_household")
        if not isinstance(confirmed, bool) or not confirmed:
            raise ValidationFailed("暂停区间必须经家庭确认")
        starts_on, ends_on = _date_range(
            raw.get("starts_on"), raw.get("ends_on"), "starts_on", "ends_on"
        )
        return cls(
            pause_id=identifier(raw.get("pause_id"), "pause_id"),
            household_id=identifier(raw.get("household_id"), "household_id"),
            starts_on=starts_on,
            ends_on=ends_on,
            reason_code=reason_code,
            evidence=required_text(raw.get("evidence"), "evidence", 512),
            confirmed_by_household=True,
        )


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """不可变领域事件。observed_at 为事实发生时刻，notice_at 为提前告知时刻。"""

    event_id: str
    event_kind: str
    household_id: str
    event_date: str
    observed_at: str
    notice_at: str | None
    payload: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DomainEvent":
        event_kind = required_text(raw.get("event_kind"), "event_kind", 48)
        if event_kind not in EVENT_KINDS:
            raise ValidationFailed("event_kind 不是受支持的事件类型")
        event_date = date_text(raw.get("event_date"), "event_date")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        parse_utc(observed_at, "observed_at")
        notice_at = raw.get("notice_at")
        if notice_at is not None:
            notice_text = required_text(notice_at, "notice_at", 40)
            parse_utc(notice_text, "notice_at")
        else:
            notice_text = None
        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise ValidationFailed("payload 必须是对象")
        return cls(
            event_id=identifier(raw.get("event_id"), "event_id"),
            event_kind=event_kind,
            household_id=household_identifier(raw.get("household_id"), "household_id"),
            event_date=event_date,
            observed_at=observed_at,
            notice_at=notice_text,
            payload=dict(payload),
        )
