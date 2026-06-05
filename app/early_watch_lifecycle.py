"""Early Watch Lifecycle V1.

Early Watch is not a user action.  This module gives every early/weekly/source
candidate a clear lifecycle: monitor closely, wait for trigger, reclaim/pullback,
or remove from active consideration.  It does not create BUY_NOW; final decision
engine remains the only place that can do that.
"""
from __future__ import annotations

from typing import Any

EARLY_WATCH_LIFECYCLE_VERSION = "early_watch_lifecycle_v1_monitor_promote_or_demote_2026_06_05"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
        return float(v)
    except Exception:
        return default


def _has(row: dict, *keys: str) -> bool:
    for k in keys:
        val = row.get(k)
        if isinstance(val, bool) and val:
            return True
        if isinstance(val, str) and val.strip():
            return True
        if isinstance(val, (list, tuple, set)) and val:
            return True
    return False


def enrich_early_watch_lifecycle(row: dict) -> dict:
    out = dict(row or {})
    code = _s(out.get("final_decision_code"))
    stage = _s(out.get("visible_move_stage")) or _s(out.get("move_stage"))
    source_reason = _s(out.get("source_reason"))
    weekly = bool(out.get("polygon_weekly_priority") or out.get("weekly_priority") or out.get("early_movement_weekly_priority"))
    early = bool(stage in {"Early Watch", "Pre-Move", "Early Confirmation"} or _has(out, "early_movement_active") or "رادار" in source_reason)
    current_gain = _f(out.get("current_gain", out.get("display_change_pct", 0.0)))
    entry_dist = _f(out.get("final_decision_entry_distance_pct", out.get("distance_to_entry_pct", 999.0)), 999.0)
    liquidity_ok = bool(out.get("final_decision_liquidity_ok"))
    pattern_action = _s(out.get("pattern_action"))

    status = "not_tracked"
    label = "غير داخل متابعة مبكرة"
    action = ""
    priority = 0
    next_check_sec = 900
    promotion_gate = "none"

    if code == "BUY_NOW":
        status = "ready_now"
        label = "جاهز الآن"
        action = "✅ القرار النهائي جاهز للتنفيذ الآن حسب شروط الأداة."
        priority = 100
        next_check_sec = 60
        promotion_gate = "already_buy_now"
    elif code in {"CAUTIOUS_ENTRY", "WAIT_TRIGGER", "WAIT_RESISTANCE", "WAIT_LIQUIDITY"}:
        status = "near_trigger"
        label = "قريب من التفعيل"
        action = "🟠 متابعة لصيقة — انتظر تحقق الشرط المحدد فقط، وليس دخولًا عشوائيًا."
        priority = 78
        next_check_sec = 90
        promotion_gate = "final_conditions_required"
    elif code in {"PLAN_BROKEN", "RECLAIM_REQUIRED"}:
        status = "needs_reclaim"
        label = "يحتاج استعادة"
        action = "🔴 الخطة/الدعم مكسور — لا ترقية قبل استعادة المستوى المذكور بسيولة."
        priority = 42
        next_check_sec = 240
        promotion_gate = "reclaim_support_or_stop_first"
    elif code == "PULLBACK_REQUIRED":
        status = "needs_pullback"
        label = "يحتاج Pullback/Reposition"
        action = "⏳ قوي/كان قويًا لكنه غير قابل للتنفيذ الآن — انتظر pullback أو reclaim."
        priority = 55
        next_check_sec = 180
        promotion_gate = "pullback_or_reclaim_required"
    elif code == "NO_CHASE":
        status = "do_not_chase"
        label = "لا تطارد"
        action = "⛔ لا ترقية حتى يعود السعر لمنطقة آمنة أو يبني قاعدة جديدة."
        priority = 25
        next_check_sec = 360
        promotion_gate = "new_base_required"
    elif code == "DATA_INCOMPLETE":
        status = "data_wait"
        label = "انتظار اكتمال البيانات"
        action = "⚪ لا متابعة تنفيذية حتى يكتمل السعر/المصدر/الخطة."
        priority = 20
        next_check_sec = 300
        promotion_gate = "quote_or_plan_required"
    elif early or weekly or pattern_action in {"monitor_closely", "watch_for_trigger"}:
        status = "active_watch"
        label = "مراقبة لصيقة"
        action = "🟣 الأداة تتابع هذا السهم؛ لا يعتبر دخولًا حتى يترقى رسميًا."
        priority = 66
        next_check_sec = 120
        promotion_gate = "watch_to_cautious_or_strong_when_confirmed"
    elif current_gain < -3:
        status = "wait_rebound"
        label = "انتظار ارتداد"
        action = "⏳ السهم هابط — انتظر ارتدادًا واضحًا قبل أي ترقية."
        priority = 30
        next_check_sec = 240
        promotion_gate = "rebound_first"
    else:
        status = "normal_watch"
        label = "مراقبة عادية"
        action = "👀 مراقبة فقط حتى يظهر محفز فني واضح."
        priority = 35
        next_check_sec = 300
        promotion_gate = "new_confirmation_required"

    if weekly:
        priority += 8
    if liquidity_ok:
        priority += 6
    if -1.0 <= entry_dist <= 1.5:
        priority += 8
    priority = max(0, min(100, int(priority)))

    out["early_watch_lifecycle_version"] = EARLY_WATCH_LIFECYCLE_VERSION
    out["early_watch_lifecycle_status"] = status
    out["early_watch_lifecycle_label"] = label
    out["early_watch_lifecycle_action"] = action
    out["early_watch_lifecycle_priority"] = priority
    out["early_watch_next_check_sec"] = int(next_check_sec)
    out["early_watch_promotion_gate"] = promotion_gate
    return out


def enrich_rows_early_watch_lifecycle(rows: list[dict]) -> list[dict]:
    return [enrich_early_watch_lifecycle(x) if isinstance(x, dict) else x for x in (rows or [])]


def summarize_early_watch_lifecycle(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = _s(row.get("early_watch_lifecycle_status")) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return {
        "ok": True,
        "version": EARLY_WATCH_LIFECYCLE_VERSION,
        "counts": counts,
        "active_watch_count": sum(counts.get(k, 0) for k in ["active_watch", "near_trigger", "ready_now"]),
        "non_actionable_count": sum(counts.get(k, 0) for k in ["needs_reclaim", "needs_pullback", "do_not_chase", "data_wait", "wait_rebound"]),
    }
