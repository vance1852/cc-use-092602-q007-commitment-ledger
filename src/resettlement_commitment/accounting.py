"""安置承诺履约的确定性核算：等待天数、排除区间与补偿额度。

日期统一使用 ``YYYY-MM-DD``，区间采用左闭右开 ``[start, end)``，
区间天数即两个日期之差。没有结束日期的事件视为开放区间，
在与统计窗口或家庭等待时间线相交时按窗口边界截断。

核算函数只接收普通字典，不依赖数据库，因此可以离线对任意快照复算。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

ZERO = Decimal("0")
CENT = Decimal("0.01")

# 不可变事件类型。
EVENT_CONSTRUCTION = "construction.announced"   # 计划施工（提前告知）
EVENT_POLICY = "policy.exempted"                # 政策豁免
EVENT_PAUSE = "household.paused"                # 家庭主动延期（家庭确认的暂停区间）
EVENT_DELIVERY = "delivery.confirmed"           # 实际正式交房

EXCLUSION_KINDS = {
    EVENT_CONSTRUCTION: "construction_plan",
    EVENT_POLICY: "policy_exemption",
    EVENT_PAUSE: "household_deferral",
}

# 排除被拒绝的原因代码。
REJECT_NOTICE_SHORT = "notice_too_short"        # 提前告知不足
REJECT_OUTSIDE_WINDOW = "outside_agreed_window"  # 不落在约定窗口内
REJECT_NOT_CONFIRMED = "pause_not_confirmed"     # 暂停区间未经家庭确认
REJECT_REASON_NOT_ALLOWED = "reason_not_allowed"  # 豁免原因代码不在规则范围内


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def day_text(value: date) -> str:
    return value.isoformat()


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Segment:
    """左闭右开的日期切片。``end`` 为 ``None`` 表示开放。"""

    start: date
    end: date | None

    @property
    def open_ended(self) -> bool:
        return self.end is None

    def days_to(self, limit: date) -> int:
        finish = limit if self.end is None else min(self.end, limit)
        return max(0, (finish - self.start).days)


def clip(start: date, end: date | None, lower: date, upper: date) -> tuple[date, date] | None:
    """把 ``[start, end)`` 截断到 ``[lower, upper)``；空交集返回 ``None``。"""
    clipped_start = max(start, lower)
    clipped_end = upper if end is None else min(end, upper)
    if clipped_start >= clipped_end:
        return None
    return clipped_start, clipped_end


def merge_intervals(intervals: Iterable[tuple[date, date]]) -> list[tuple[date, int]]:
    """合并可能重叠的闭区间片段，返回 ``[start, days]`` 互不重叠列表。"""
    ordered = sorted(intervals, key=lambda item: item[0])
    merged: list[list[date]] = []
    for start, end in ordered:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, (end - start).days) for start, end in merged]


def split_across_windows(
    start_date: str,
    end_date: str | None,
    windows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把一个可能跨窗口的事件精确拆分到每个统计窗口。

    跨窗口部分按窗口边界逐日截断，既不重复也不遗漏。
    """
    start = parse_day(start_date)
    end = None if end_date is None else parse_day(end_date)
    if end is not None and end <= start:
        raise ValueError("事件结束日期必须晚于开始日期")
    result: list[dict[str, Any]] = []
    for window in sorted(windows, key=lambda item: str(item["window_start"])):
        lower = parse_day(str(window["window_start"]))
        upper = parse_day(str(window["window_end"])) + timedelta(days=1)
        clipped = clip(start, end, lower, upper)
        if clipped is None:
            continue
        clipped_start, clipped_end = clipped
        result.append({
            "window_id": window["window_id"],
            "start_date": day_text(clipped_start),
            "end_date": day_text(clipped_end),
            "days": (clipped_end - clipped_start).days,
        })
    return result


def _rule_bounds(rule: Mapping[str, Any], lower: date, upper: date) -> tuple[date, date]:
    effective_start = rule.get("effective_start")
    effective_end = rule.get("effective_end")
    bound_start = lower if effective_start is None else max(parse_day(str(effective_start)), lower)
    if effective_end is None:
        bound_end = upper
    else:
        bound_end = min(parse_day(str(effective_end)) + timedelta(days=1), upper)
    return bound_start, bound_end


def _reject(rejections: dict[tuple[str, str, str], dict[str, str]], event: Mapping[str, Any],
            rule: Mapping[str, Any], code: str, message: str) -> None:
    key = (str(event["event_id"]), str(rule["rule_id"]), code)
    if key not in rejections:
        rejections[key] = {
            "event_id": event["event_id"],
            "rule_id": rule["rule_id"],
            "reason_code": code,
            "message": message,
        }


def _rate(window: Mapping[str, Any]) -> Decimal:
    return Decimal(str(window["daily_rate_cny"]))


def compute_window(
    *,
    window: Mapping[str, Any],
    commitment: Mapping[str, Any],
    households: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """依据不可变事件计算单个统计窗口的履约结果。

    返回每个家庭的等待/排除/可补偿天数、逐事件来源片段，以及窗口汇总。
    结果完全由入参决定，便于离线复算与比对。
    """
    lower = parse_day(str(window["window_start"]))
    upper = parse_day(str(window["window_end"])) + timedelta(days=1)
    rate = _rate(window)
    if rate < ZERO:
        raise ValueError("日补偿标准不能为负数")
    promised = parse_day(str(commitment["promised_delivery_date"]))

    # 实际交房取每个家庭最早一条 immuttable 交房事件。
    deliveries: dict[str, date] = {}
    for event in sorted(events, key=lambda item: (str(item["event_date"]), str(item["event_id"]))):
        if event["event_type"] != EVENT_DELIVERY or not event.get("household_id"):
            continue
        delivery_date = parse_day(str(event["event_date"]))
        household_id = str(event["household_id"])
        if household_id not in deliveries or delivery_date < deliveries[household_id]:
            deliveries[household_id] = delivery_date

    construction_events = [e for e in events if e["event_type"] == EVENT_CONSTRUCTION]
    policy_events = [e for e in events if e["event_type"] == EVENT_POLICY]
    rejections: dict[tuple[str, str, str], dict[str, str]] = {}

    household_rows: list[dict[str, Any]] = []
    for household in sorted(households, key=lambda item: str(item["household_id"])):
        household_id = str(household["household_id"])
        delivery = deliveries.get(household_id)
        waiting = clip(promised, delivery, lower, upper)
        if waiting is None:
            continue  # 本窗口内尚未进入逾期等待或已在窗口前交房。
        wait_start, wait_end = waiting
        raw_days = (wait_end - wait_start).days

        pause_events = [
            event for event in events
            if event["event_type"] == EVENT_PAUSE and event.get("household_id") == household_id
        ]
        fragments: list[dict[str, Any]] = []
        excluded_intervals: list[tuple[date, date]] = []

        for rule in sorted(rules, key=lambda item: str(item["rule_id"])):
            kind = str(rule["kind"])
            bound_start, bound_end = _rule_bounds(rule, lower, upper)
            if kind == "construction_plan":
                candidates = construction_events
            elif kind == "policy_exemption":
                candidates = policy_events
            elif kind == "household_deferral":
                candidates = pause_events
            else:
                continue
            minimum_notice = rule.get("min_advance_notice_days")
            allowed_reasons = rule.get("allowed_reason_codes") or []
            for event in sorted(candidates, key=lambda item: str(item["event_id"])):
                event_start = parse_day(str(event["event_date"]))
                raw_end = event.get("end_date")
                event_end = None if raw_end is None else parse_day(str(raw_end))
                if event_end is not None and event_end <= event_start:
                    continue

                # 与本统计窗口完全不相交的事件对本窗口不适用，静默跳过
                # （跨窗口事件由 split_across_windows 精确拆分后分别进入各窗口）。
                if clip(event_start, event_end, lower, upper) is None:
                    continue

                if kind == "construction_plan":
                    notice = event.get("advance_notice_days")
                    if notice is None or int(notice) < int(minimum_notice or 0):
                        _reject(
                            rejections, event, rule, REJECT_NOTICE_SHORT,
                            f"施工事件提前告知不足：约定至少 {minimum_notice or 0} 天，实际 "
                            f"{0 if notice is None else int(notice)} 天",
                        )
                        continue
                elif kind == "policy_exemption":
                    if allowed_reasons and str(event.get("reason_code") or "") not in {
                        str(code) for code in allowed_reasons
                    }:
                        _reject(
                            rejections, event, rule, REJECT_REASON_NOT_ALLOWED,
                            f"豁免原因 {event.get('reason_code')!r} 不在规则允许范围内",
                        )
                        continue
                elif kind == "household_deferral":
                    if not event.get("confirmed"):
                        _reject(rejections, event, rule, REJECT_NOT_CONFIRMED, "暂停区间未经家庭确认")
                        continue

                within_rule = clip(event_start, event_end, bound_start, bound_end)
                if within_rule is None:
                    _reject(
                        rejections, event, rule, REJECT_OUTSIDE_WINDOW,
                        "事件不落在规则约定的排除窗口内，整段不予排除",
                    )
                    continue
                within_waiting = clip(within_rule[0], within_rule[1], wait_start, wait_end)
                if within_waiting is None:
                    continue  # 与该家庭的逾期等待时间线不重叠（例如窗口内已交房之后）。
                seg_start, seg_end = within_waiting
                seg_days = (seg_end - seg_start).days
                fragments.append({
                    "rule_id": rule["rule_id"],
                    "event_id": event["event_id"],
                    "exclusion_kind": kind,
                    "start_date": day_text(seg_start),
                    "end_date": day_text(seg_end),
                    "days": seg_days,
                    "amount_cny": decimal_text(quantize_money(Decimal(seg_days) * rate)),
                })
                excluded_intervals.append((seg_start, seg_end))

        merged = merge_intervals(excluded_intervals)
        excluded_days = sum(days for _, days in merged)
        billable_days = raw_days - excluded_days
        household_rows.append({
            "household_id": household_id,
            "waiting": {
                "start_date": day_text(wait_start),
                "end_date": day_text(wait_end),
                "days": raw_days,
            },
            "excluded_days": excluded_days,
            "billable_days": billable_days,
            "gross_compensation_cny": decimal_text(quantize_money(Decimal(raw_days) * rate)),
            "deducted_compensation_cny": decimal_text(quantize_money(Decimal(excluded_days) * rate)),
            "net_compensation_cny": decimal_text(quantize_money(Decimal(billable_days) * rate)),
            "fragments": sorted(fragments, key=lambda item: (item["start_date"], str(item["event_id"]))),
        })

    total_raw = sum(row["waiting"]["days"] for row in household_rows)
    total_excluded = sum(row["excluded_days"] for row in household_rows)
    total_billable = sum(row["billable_days"] for row in household_rows)
    return {
        "window_id": window["window_id"],
        "commitment_version_no": commitment["version_no"],
        "window_start": str(window["window_start"]),
        "window_end": str(window["window_end"]),
        "daily_rate_cny": decimal_text(rate),
        "affected_households": sum(1 for row in household_rows if row["waiting"]["days"] > 0),
        "compensated_households": sum(1 for row in household_rows if row["billable_days"] > 0),
        "total_waiting_days": total_raw,
        "total_excluded_days": total_excluded,
        "total_billable_days": total_billable,
        "gross_compensation_cny": decimal_text(quantize_money(Decimal(total_raw) * rate)),
        "deducted_compensation_cny": decimal_text(quantize_money(Decimal(total_excluded) * rate)),
        "net_compensation_cny": decimal_text(quantize_money(Decimal(total_billable) * rate)),
        "households": household_rows,
        "rejected_exclusions": [rejections[key] for key in sorted(rejections)],
    }


def diff_results(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    reason: str = "",
) -> dict[str, Any]:
    """对比两个结算版本，逐家庭列出等待、排除与补偿差异。"""
    prior = {str(row["household_id"]): row for row in previous.get("households", [])}
    after = {str(row["household_id"]): row for row in current.get("households", [])}
    changes: list[dict[str, Any]] = []
    for household_id in sorted(set(prior) | set(after)):
        old = prior.get(household_id)
        new = after.get(household_id)
        old_wait = 0 if old is None else int(old["waiting"]["days"])
        new_wait = 0 if new is None else int(new["waiting"]["days"])
        old_excluded = 0 if old is None else int(old["excluded_days"])
        new_excluded = 0 if new is None else int(new["excluded_days"])
        old_net = ZERO if old is None else Decimal(str(old["net_compensation_cny"]))
        new_net = ZERO if new is None else Decimal(str(new["net_compensation_cny"]))
        if old_wait == new_wait and old_excluded == new_excluded and old_net == new_net:
            continue
        changes.append({
            "household_id": household_id,
            "waiting_days_delta": new_wait - old_wait,
            "excluded_days_delta": new_excluded - old_excluded,
            "billable_days_delta": (new_wait - new_excluded) - (old_wait - old_excluded),
            "net_compensation_cny_before": decimal_text(old_net),
            "net_compensation_cny_after": decimal_text(new_net),
            "net_compensation_cny_delta": decimal_text(quantize_money(new_net - old_net)),
        })
    return {
        "previous_window_version": previous.get("settlement_version_no"),
        "current_window_version": current.get("settlement_version_no"),
        "previous_commitment_version_no": previous.get("commitment_version_no"),
        "current_commitment_version_no": current.get("commitment_version_no"),
        "reason": reason,
        "total_net_cny_before": previous.get("net_compensation_cny"),
        "total_net_cny_after": current.get("net_compensation_cny"),
        "household_changes": changes,
    }
