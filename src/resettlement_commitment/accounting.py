"""确定性的安置承诺履约核算：等待天数、排除拆分、受影响家庭与补偿额度。

所有函数只处理普通字典与日期，不访问数据库或时钟，保证可以离线复算。
区间统一采用半开区间 [lo, hi)（按自然日），窗口、暂停、施工、家庭履约历程
都先裁剪到同一组窗口边界，再逐天归因，跨窗口事件因此会被精确拆成多个片段。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence


ZERO = Decimal("0")
CENT = Decimal("0.01")

# 归因优先级：家庭/政策确认的暂停优先于计划施工，避免同一天被重复扣减。
PAUSE_PRIORITY = {
    "policy_exemption": 0,
    "family_extension": 1,
    "family_unavailable": 2,
}
CONSTRUCTION_PRIORITY = 10


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _day(value: str) -> date:
    return date.fromisoformat(value)


def _iso(value: date) -> str:
    return value.isoformat()


def _intersect(
    lo_a: date, hi_a: date, lo_b: date, hi_b: date
) -> tuple[date, date] | None:
    lo = max(lo_a, lo_b)
    hi = min(hi_a, hi_b)
    return (lo, hi) if lo < hi else None


def _construction_interval(event: Mapping[str, Any]) -> tuple[date, date]:
    """计划施工事件的影响区间取 payload 的 starts_on/ends_on，缺省退化为事件当日。"""

    payload = event.get("payload") or {}
    starts_on = payload.get("starts_on", event["event_date"])
    ends_on = payload.get("ends_on", event["event_date"])
    start = _day(str(starts_on))
    end = _day(str(ends_on))
    if end < start:
        raise ValueError(f"施工事件 {event['event_id']} 的 ends_on 不能早于 starts_on")
    return start, end + timedelta(days=1)


def _notice_lead_days(event: Mapping[str, Any], construction_start: date) -> int | None:
    notice_at = event.get("notice_at")
    if not notice_at:
        return None
    return (construction_start - _day(str(notice_at)[:10])).days


def _event_eligible(
    event: Mapping[str, Any],
    rule: Mapping[str, Any] | None,
    advance_notice_days: int,
) -> tuple[bool, str]:
    """判定施工片段能否排除：窗口内必须有规则，且提前告知天数达标。"""

    if rule is None:
        return False, "窗口内没有对应的排除规则"
    if rule.get("require_advance_notice", True):
        lead = _notice_lead_days(event, _construction_interval(event)[0])
        if lead is None:
            return False, "缺少提前告知时间"
        if lead < advance_notice_days:
            return False, f"提前告知 {lead} 天不足约定 {advance_notice_days} 天"
    return True, "符合窗口排除规则与提前告知要求"


def _journey(
    events: Sequence[Mapping[str, Any]],
    start_kind: str,
    end_kind: str,
    cutoff: date,
) -> tuple[str, str | None, str | None, date, date] | None:
    """返回 (起点事件ID, 终点事件ID, 开放原因, 起点日, 半开终点日)。

    取最早起点与不早于起点的最早终点；终点缺失时历程在截止日仍开放。
    """

    starts = sorted(
        (event for event in events if event["event_kind"] == start_kind),
        key=lambda event: (event["event_date"], event["event_id"]),
    )
    if not starts:
        return None
    start_event = starts[0]
    start_day = _day(start_event["event_date"])
    ends = sorted(
        (
            event
            for event in events
            if event["event_kind"] == end_kind and _day(event["event_date"]) >= start_day
        ),
        key=lambda event: (event["event_date"], event["event_id"]),
    )
    if ends:
        end_event = ends[0]
        return (
            start_event["event_id"],
            end_event["event_id"],
            None,
            start_day,
            _day(end_event["event_date"]) + timedelta(days=1),
        )
    return (
        start_event["event_id"],
        None,
        f"截至 {_iso(cutoff)} 未记录 {end_kind} 事件",
        start_day,
        cutoff + timedelta(days=1),
    )


def _claim_days(lo: date, hi: date, claimed: set[date]) -> tuple[int, int]:
    """按优先级逐日占用，返回 (实际归因天数, 与更高优先级来源重叠的天数)。"""

    applied = 0
    overlap = 0
    cursor = lo
    while cursor < hi:
        if cursor in claimed:
            overlap += 1
        else:
            claimed.add(cursor)
            applied += 1
        cursor += timedelta(days=1)
    return applied, overlap


def _household_term_window(
    *,
    household_id: str,
    term: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    pauses: Sequence[Mapping[str, Any]],
    window: Mapping[str, Any],
    rules_by_window: Mapping[str, list[Mapping[str, Any]]],
    advance_notice_days: int,
    cutoff: date,
) -> Mapping[str, Any] | None:
    journey = _journey(events, term["start_event"], term["end_event"], cutoff)
    if journey is None:
        return None
    start_event_id, end_event_id, open_reason, journey_lo, journey_hi = journey

    win_lo = _day(window["starts_on"])
    win_hi = _day(window["ends_on"]) + timedelta(days=1)
    horizon_hi = cutoff + timedelta(days=1)
    segment = _intersect(journey_lo, journey_hi, win_lo, win_hi)
    segment = None if segment is None else _intersect(segment[0], segment[1], date.min, horizon_hi)
    if segment is None:
        return None
    segment_lo, segment_hi = segment

    candidates: list[tuple[int, int, dict[str, Any]]] = []
    sequence = 0

    for pause in sorted(pauses, key=lambda item: (item["starts_on"], item["pause_id"])):
        pause_lo = _day(pause["starts_on"])
        pause_hi = _day(pause["ends_on"] or _iso(cutoff)) + timedelta(days=1)
        clipped = _intersect(segment_lo, segment_hi, pause_lo, pause_hi)
        if clipped is None:
            continue
        fragment_lo, fragment_hi = clipped
        candidates.append((
            PAUSE_PRIORITY.get(pause["reason_code"], 9),
            sequence,
            {
                "source": "pause",
                "pause_id": pause["pause_id"],
                "reason_code": pause["reason_code"],
                "evidence": pause["evidence"],
                "window_id": window["window_id"],
                "fragment_start": _iso(fragment_lo),
                "fragment_end": _iso(fragment_hi - timedelta(days=1)),
                "days": (fragment_hi - fragment_lo).days,
                "eligible": True,
                "basis": "家庭确认的暂停区间",
                "applied_days": 0,
                "overlap_days": 0,
                "rejected_days": 0,
            },
        ))
        sequence += 1

    rules = {rule["event_kind"]: rule for rule in rules_by_window.get(window["window_id"], ())}
    construction_events = [
        event
        for event in events
        if event["event_kind"] == "construction_plan"
        and event["household_id"] in (household_id, "*")
    ]
    for event in sorted(construction_events, key=lambda item: (item["event_date"], item["event_id"])):
        construction_lo_full, construction_hi_full = _construction_interval(event)
        clipped = _intersect(segment_lo, segment_hi, construction_lo_full, construction_hi_full)
        if clipped is None:
            continue
        fragment_lo, fragment_hi = clipped
        # 提前告知以施工计划的实际起始日计算，不会因为跨窗口拆分而改变。
        eligible, basis = _event_eligible(
            event, rules.get("construction_plan"), advance_notice_days
        )
        candidates.append((
            CONSTRUCTION_PRIORITY,
            sequence,
            {
                "source": "event",
                "event_id": event["event_id"],
                "event_kind": event["event_kind"],
                "window_id": window["window_id"],
                "fragment_start": _iso(fragment_lo),
                "fragment_end": _iso(fragment_hi - timedelta(days=1)),
                "days": (fragment_hi - fragment_lo).days,
                "notice_lead_days": _notice_lead_days(event, construction_lo_full),
                "eligible": eligible,
                "basis": basis,
                "applied_days": 0,
                "overlap_days": 0,
                "rejected_days": 0,
            },
        ))
        sequence += 1

    # 逐天归因：优先级低的来源只能占用尚未被更高优先级占用的日期。
    claimed: set[date] = set()
    fragments: list[dict[str, Any]] = []
    for _, _, fragment in sorted(candidates, key=lambda item: (item[0], item[1])):
        lo = _day(fragment["fragment_start"])
        hi = _day(fragment["fragment_end"]) + timedelta(days=1)
        if fragment["eligible"]:
            applied, overlap = _claim_days(lo, hi, claimed)
            fragment["applied_days"] = applied
            fragment["overlap_days"] = overlap
        else:
            fragment["rejected_days"] = fragment["days"]
        fragments.append(fragment)

    segment_days = (segment_hi - segment_lo).days
    excluded_days = sum(fragment["applied_days"] for fragment in fragments)
    counted_days = segment_days - excluded_days

    return {
        "term_kind": term["kind"],
        "start_event": term["start_event"],
        "end_event": term["end_event"],
        "promised_days": term["promised_days"],
        "start_event_id": start_event_id,
        "end_event_id": end_event_id,
        "open_reason": open_reason,
        "window_id": window["window_id"],
        "segment_start": _iso(segment_lo),
        "segment_end": _iso(segment_hi - timedelta(days=1)),
        "segment_days": segment_days,
        "excluded_days": excluded_days,
        "counted_days": counted_days,
        "fragments": fragments,
    }


def compute_window(
    *,
    version: Mapping[str, Any],
    windows: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    pauses: Sequence[Mapping[str, Any]],
    target_window_id: str,
    as_of_date: str,
    include_observed_until: str | None = None,
) -> dict[str, Any]:
    """计算单个统计窗口的履约结果。

    承诺天数预算逐户所有：每户在每个时限上享受 promised_days 天的免责等待，
    预算按窗口时间顺序结转，超出预算的计数天数才在所在窗口形成逾期与补偿。
    为得到目标窗口的结转值，内部会先计算该承诺下全部窗口。
    """

    cutoff = _day(as_of_date)
    ordered_windows = sorted(windows, key=lambda item: (item["starts_on"], item["window_id"]))
    if not any(window["window_id"] == target_window_id for window in ordered_windows):
        raise ValueError("目标统计窗口不存在")
    terms = sorted(version["terms"], key=lambda item: item["kind"])
    rate = Decimal(str(version["daily_compensation_cny"]))
    notice_days = int(version["advance_notice_days"])

    rules_by_window: dict[str, list[Mapping[str, Any]]] = {}
    for rule in rules:
        rules_by_window.setdefault(rule["window_id"], []).append(rule)

    household_ids = sorted(
        {event["household_id"] for event in events if event["household_id"] != "*"}
    )
    pauses_by_household: dict[str, list[Mapping[str, Any]]] = {}
    for pause in pauses:
        pauses_by_household.setdefault(pause["household_id"], []).append(pause)

    # 窗口级汇总按户累加（承诺预算是逐户的，不能用总量对单一预算）。
    aggregate: dict[str, dict[str, dict[str, int]]] = {
        window["window_id"]: {
            term["kind"]: {"counted": 0, "excluded": 0, "delay": 0, "remaining_enter": 0, "present": 0}
            for term in terms
        }
        for window in ordered_windows
    }
    household_outputs: list[dict[str, Any]] = []

    for household_id in household_ids:
        household_events = [
            event for event in events if event["household_id"] in (household_id, "*")
        ]
        household_pauses = pauses_by_household.get(household_id, [])
        slots: dict[str, dict[str, Mapping[str, Any]]] = {
            term["kind"]: {} for term in terms
        }
        for term in terms:
            for window in ordered_windows:
                result = _household_term_window(
                    household_id=household_id,
                    term=term,
                    events=household_events,
                    pauses=household_pauses,
                    window=window,
                    rules_by_window=rules_by_window,
                    advance_notice_days=notice_days,
                    cutoff=cutoff,
                )
                if result is not None:
                    slots[term["kind"]][window["window_id"]] = result

        target_terms: list[Mapping[str, Any]] = []
        household_delay = 0
        household_compensation = ZERO
        household_present = False
        for term in terms:
            remaining = int(term["promised_days"])
            for window in ordered_windows:
                slot = slots[term["kind"]].get(window["window_id"])
                counted = 0 if slot is None else int(slot["counted_days"])
                excluded = 0 if slot is None else int(slot["excluded_days"])
                delay = max(0, counted - remaining)
                if slot is not None:
                    bucket = aggregate[window["window_id"]][term["kind"]]
                    bucket["counted"] += counted
                    bucket["excluded"] += excluded
                    bucket["delay"] += delay
                    bucket["remaining_enter"] += remaining
                    bucket["present"] += 1
                remaining = max(0, remaining - counted)
                if slot is not None and window["window_id"] == target_window_id:
                    household_present = True
                    compensation = quantize_money(Decimal(delay) * rate)
                    household_delay += delay
                    household_compensation += compensation
                    target_terms.append({
                        **slot,
                        "promised_remaining_on_enter": max(0, remaining + counted - delay),
                        "delay_days": delay,
                        "compensation_cny": decimal_text(compensation),
                    })
        if household_present:
            household_outputs.append({
                "household_id": household_id,
                "delay_days": household_delay,
                "compensation_cny": decimal_text(quantize_money(household_compensation)),
                "affected": household_delay > 0,
                "terms": target_terms,
            })

    window_summaries: list[dict[str, Any]] = []
    for window in ordered_windows:
        summary_terms = []
        for term in terms:
            bucket = aggregate[window["window_id"]][term["kind"]]
            summary_terms.append({
                "term_kind": term["kind"],
                "waiting_households": bucket["present"],
                "counted_days": bucket["counted"],
                "excluded_days": bucket["excluded"],
                "promised_remaining_on_enter_total": bucket["remaining_enter"],
                "delay_days": bucket["delay"],
                "compensation_cny": decimal_text(quantize_money(Decimal(bucket["delay"]) * rate)),
            })
        window_summaries.append({
            "window_id": window["window_id"],
            "starts_on": window["starts_on"],
            "ends_on": window["ends_on"],
            "terms": summary_terms,
        })

    target_bucket = aggregate[target_window_id]
    total_delay = sum(bucket["delay"] for bucket in target_bucket.values())
    total_counted = sum(bucket["counted"] for bucket in target_bucket.values())
    total_excluded = sum(bucket["excluded"] for bucket in target_bucket.values())
    waiting_households = len(household_outputs)
    affected_households = sum(1 for row in household_outputs if row["affected"])
    total_compensation = quantize_money(
        sum((Decimal(row["compensation_cny"]) for row in household_outputs), ZERO)
    )

    manifest = {
        "version": {
            "commitment_id": version["commitment_id"],
            "version_no": version["version_no"],
            "daily_compensation_cny": decimal_text(rate),
            "advance_notice_days": notice_days,
            "terms": terms,
        },
        "windows": sorted(ordered_windows, key=lambda item: item["window_id"]),
        "rules": sorted(rules, key=lambda item: item["rule_id"]),
        "events": sorted(events, key=lambda item: item["event_id"]),
        "pauses": sorted(pauses, key=lambda item: item["pause_id"]),
        "target_window_id": target_window_id,
        "as_of_date": as_of_date,
        "include_observed_until": include_observed_until,
    }

    return {
        "project_id": version["project_id"],
        "batch_id": version["batch_id"],
        "window_id": target_window_id,
        "as_of_date": as_of_date,
        "include_observed_until": include_observed_until,
        "commitment": {
            "commitment_id": version["commitment_id"],
            "version_no": version["version_no"],
            "daily_compensation_cny": decimal_text(rate),
            "advance_notice_days": notice_days,
        },
        "windows": window_summaries,
        "window_totals": {
            "window_id": target_window_id,
            "waiting_households": waiting_households,
            "affected_households": affected_households,
            "counted_days": total_counted,
            "excluded_days": total_excluded,
            "delay_days": total_delay,
            "compensation_cny": decimal_text(total_compensation),
        },
        "households": sorted(household_outputs, key=lambda item: item["household_id"]),
        "included_event_ids": sorted(event["event_id"] for event in events),
        "included_pause_ids": sorted(pause["pause_id"] for pause in pauses),
        "included_rule_ids": sorted(rule["rule_id"] for rule in rules),
        "included_window_ids": sorted(window["window_id"] for window in ordered_windows),
        "input_sha256": digest(manifest),
    }


def diff_results(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """对比同一窗口两个结算版本的差异，供纠错版本保留差异原因。"""

    def amount(row: Mapping[str, Any]) -> Decimal:
        return Decimal(str(row["compensation_cny"]))

    changed_households: list[dict[str, Any]] = []
    old_rows = {row["household_id"]: row for row in previous.get("households", [])}
    new_rows = {row["household_id"]: row for row in current.get("households", [])}
    for household_id in sorted(set(old_rows) | set(new_rows)):
        old = old_rows.get(household_id)
        new = new_rows.get(household_id)
        old_amount = ZERO if old is None else amount(old)
        new_amount = ZERO if new is None else amount(new)
        old_delay = 0 if old is None else int(old["delay_days"])
        new_delay = 0 if new is None else int(new["delay_days"])
        if old_delay != new_delay or old_amount != new_amount:
            changed_households.append({
                "household_id": household_id,
                "old_delay_days": old_delay,
                "new_delay_days": new_delay,
                "old_compensation_cny": decimal_text(old_amount),
                "new_compensation_cny": decimal_text(new_amount),
                "delta_compensation_cny": decimal_text(quantize_money(new_amount - old_amount)),
            })
    previous_totals = previous["window_totals"]
    current_totals = current["window_totals"]
    return {
        "window_id": current["window_id"],
        "previous_input_sha256": previous["input_sha256"],
        "current_input_sha256": current["input_sha256"],
        "previous_compensation_cny": decimal_text(amount(previous_totals)),
        "current_compensation_cny": decimal_text(amount(current_totals)),
        "delta_compensation_cny": decimal_text(
            quantize_money(amount(current_totals) - amount(previous_totals))
        ),
        "previous_affected_households": int(previous_totals["affected_households"]),
        "current_affected_households": int(current_totals["affected_households"]),
        "new_event_ids": sorted(
            set(current.get("included_event_ids", [])) - set(previous.get("included_event_ids", []))
        ),
        "new_pause_ids": sorted(
            set(current.get("included_pause_ids", [])) - set(previous.get("included_pause_ids", []))
        ),
        "changed_households": changed_households,
    }
