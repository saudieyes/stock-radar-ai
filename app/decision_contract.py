"""Unified decision contract for Stock Radar AI.

This module is intentionally small and deterministic.  It does not fetch data,
does not change Sharia status, and does not send alerts.  Its only job is to
make every downstream layer agree on the same live price, plan validity, and
Arabic action label before the final decision is displayed or sent to Telegram.
"""
from __future__ import annotations

from typing import Any

DECISION_CONTRACT_VERSION = "decision_contract_v1a_2026_06_05_full_integration"


def _s(value: Any) -> str:
    return str(value or "").strip()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").replace("%", "").strip()
            if not value:
                return default
        return float(value)
    except Exception:
        return default


def _first_number(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        value = _f(row.get(key), 0.0)
        if value > 0:
            return value
    return default


def _pct(a: float, b: float) -> float:
    if a > 0 and b > 0:
        return ((a - b) / b) * 100.0
    return 0.0


def _dedupe(items: list[Any], limit: int = 10) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        text = _s(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def resolve_quote_contract(row: dict) -> dict:
    """Return one normalized quote contract and copy key fields to the row later.

    We prefer live/FMP fields when present, but we keep Polygon/snapshot as an
    explicit delayed/fallback source.  This prevents mixed cards such as: price
    exists, change missing, entry/target zero, and still showing an actionable
    label.
    """
    phase = _s(row.get("market_phase")) or _s(row.get("phase"))
    source = _s(row.get("price_source")) or _s(row.get("quote_source"))
    source_label = _s(row.get("price_source_label")) or source
    source_present = bool(source)
    price = _first_number(row, [
        "current_price_live", "live_price", "display_price", "price", "current_price", "last_price", "fmp_price"
    ], 0.0)
    previous_close = _first_number(row, ["previous_close_live", "previous_close", "prev_close", "close_prev"], 0.0)
    open_price = _first_number(row, ["open_price_live", "open", "day_open", "session_open"], 0.0)
    high = _first_number(row, ["high_live", "day_high", "session_high", "high"], 0.0)
    low = _first_number(row, ["low_live", "day_low", "session_low", "low"], 0.0)
    change = _f(row.get("display_change_pct"), 999999.0)
    if change == 999999.0:
        change = _f(row.get("live_change_pct"), 999999.0)
    if change == 999999.0:
        change = _f(row.get("change_pct"), 999999.0)
    if change == 999999.0 and previous_close > 0 and price > 0:
        change = _pct(price, previous_close)
    if change == 999999.0 and open_price > 0 and price > 0:
        change = _pct(price, open_price)
    change_available = change != 999999.0

    # Do not trust an apparently-zero change if the quote source and all
    # reference fields are missing.  This is the RKLB-style mixed-layer bug:
    # price exists but change/source/plan are not actually valid.
    if not source_present and previous_close <= 0 and open_price <= 0:
        change_available = False
    if not change_available:
        change = 0.0

    reliable = bool(row.get("price_reliable_for_execution", False))
    if not source_present:
        reliable = False
    delayed = False
    lower_source = source.lower()
    # Polygon/snapshot/minute are useful but should not be silently treated as
    # executable live price when FMP live data is missing.
    if any(x in lower_source for x in ["polygon", "snapshot", "minute", "previous_close"]):
        if phase in {"open", "pre_market", "after_hours"} and lower_source != "live_intraday":
            delayed = True
            reliable = False
    if source in {"previous_close", "unavailable_realtime"}:
        reliable = False

    missing: list[str] = []
    if price <= 0:
        missing.append("السعر غير متوفر")
    if not change_available:
        missing.append("نسبة التغير غير متوفرة")
    if not source:
        missing.append("مصدر السعر غير محدد")

    label = source_label or "غير محدد"
    if delayed and "متأخر تقريبًا 15 دقيقة" not in label:
        label = f"{label} — متأخر تقريبًا 15 دقيقة"
    if price <= 0:
        label = "بيانات سعر غير مكتملة"

    return {
        "version": DECISION_CONTRACT_VERSION,
        "price": round(price, 4),
        "previous_close": round(previous_close, 4),
        "open": round(open_price, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "change_pct": round(change, 3),
        "change_available": bool(change_available),
        "source": source or "unknown",
        "source_label": label,
        "phase": phase,
        "reliable_for_execution": bool(reliable),
        "delayed": bool(delayed),
        "last_update_ms": int(_f(row.get("last_price_update_ms"), 0.0)),
        "last_update_label": _s(row.get("last_price_update_label")),
        "missing": _dedupe(missing, 8),
        "complete": bool(price > 0 and change_available and source_present),
    }


def evaluate_plan_lifecycle(row: dict, quote: dict | None = None) -> dict:
    quote = quote or resolve_quote_contract(row)
    price = _f(quote.get("price"), 0.0)
    change = _f(quote.get("change_pct"), 0.0)
    entry = _first_number(row, ["display_entry_price", "smart_entry_price", "entry_price_real", "entry_price", "entry", "buy_above", "breakout_price", "confirmation_price"], 0.0)
    target = _first_number(row, ["display_target_price", "smart_target_1", "target_price", "target_1", "target1", "target"], 0.0)
    stop = _first_number(row, ["display_stop_price", "smart_stop_loss", "stop_loss", "stop"], 0.0)
    support = _first_number(row, ["nearest_support", "support_price", "display_support_price", "support"], 0.0)
    resistance = _first_number(row, ["nearest_resistance", "resistance_price", "display_resistance_price", "resistance"], 0.0)
    high = _f(quote.get("high"), 0.0)
    low = _f(quote.get("low"), 0.0)
    reasons: list[str] = []
    blockers: list[str] = []

    entry_distance = _pct(price, entry) if price > 0 and entry > 0 else 999.0
    stop_distance = _pct(price, stop) if price > 0 and stop > 0 else 999.0
    support_distance = _pct(price, support) if price > 0 and support > 0 else 999.0
    resistance_distance = _pct(resistance, price) if price > 0 and resistance > 0 else 999.0
    day_position = 0.0
    if price > 0 and high > low > 0:
        day_position = ((price - low) / (high - low)) * 100.0

    status = "watch"
    action = "👀 مراقبة — ليست دخولًا الآن."
    label = "مراقبة"
    execution_zone = False
    no_chase = False
    needs_reclaim = False
    broken = False
    data_complete_for_plan = bool(price > 0 and quote.get("complete"))

    if not data_complete_for_plan:
        status = "data_incomplete"
        label = "بيانات غير مكتملة"
        blockers += quote.get("missing", []) or ["بيانات السعر غير مكتملة"]
        action = "⚪ بيانات غير مكتملة — لا تعرض خطة دخول حتى يكتمل السعر والتغير ومصدرهما."
    elif entry <= 0 or target <= 0 or stop <= 0:
        status = "no_valid_plan"
        label = "لا توجد خطة قابلة للتنفيذ"
        blockers.append("الدخول/الهدف/الوقف غير مكتمل")
        action = "⚪ لا توجد خطة دخول صالحة حاليًا — أعد الفحص بعد اكتمال البيانات."
    elif price <= stop:
        status = "broken_stop"
        label = "الخطة مكسورة"
        broken = True
        needs_reclaim = True
        blockers.append(f"السعر تحت وقف الخطة {round(stop, 2)}")
        action = f"🔴 الخطة مكسورة — لا دخول حتى يستعيد السعر {round(stop, 2)} ثم يثبت قرب {round(entry, 2)}."
    elif bool(row.get("support_broken_flag")) or (support > 0 and support > price * 1.0005 and change <= 7.0):
        status = "broken_support"
        label = "دعم مكسور — انتظر استعادة"
        broken = True
        needs_reclaim = True
        level = support or _f(row.get("broken_support_level"), 0.0)
        blockers.append(f"السعر تحت الدعم {round(level, 2) if level else ''}".strip())
        action = f"🔴 دعم مكسور — انتظر استعادة {round(level, 2) if level else 'الدعم'} بسيولة قبل أي دخول."
    elif target > 0 and price >= target:
        status = "target_reached"
        label = "وصل الهدف — استمرار فقط"
        blockers.append("السعر وصل/تجاوز الهدف الأول")
        action = "🔵 وصل الهدف الأول — لا دخول جديد إلا بعد إعادة تمركز أو خطة جديدة."
    elif entry > 0 and price < entry * 0.992:
        status = "waiting_trigger"
        label = "انتظار تفعيل"
        blockers.append(f"السعر لم يصل منطقة الدخول {round(entry, 2)}")
        if day_position <= 25 or change < 0:
            action = f"⏳ انتظار ارتداد/استعادة — لا دخول حتى يقترب السعر من {round(entry, 2)} بسيولة."
        else:
            action = f"⏳ انتظار تفعيل — لا دخول حتى يثبت السعر قرب {round(entry, 2)}."
    elif entry > 0 and price > entry * 1.025 and change >= 5.0:
        status = "pullback_required"
        label = "يحتاج Pullback"
        no_chase = price > entry * 1.045 or change >= 9.0
        blockers.append(f"السعر أعلى من الدخول بنحو {round(entry_distance, 2)}%")
        action = "⏳ السهم قوي لكنه ابتعد — انتظر Pullback/Reclaim بدل مطاردة السعر."
    elif resistance > 0 and 0 <= resistance_distance <= 0.75:
        status = "blocked_by_resistance"
        label = "انتظار اختراق مقاومة"
        blockers.append(f"المقاومة قريبة جدًا {round(resistance_distance, 2)}%")
        action = "🟠 مقاومة قريبة — انتظر اختراقًا وثباتًا فوقها قبل الدخول."
    elif -0.75 <= entry_distance <= 1.35:
        status = "execution_zone"
        label = "قريب من منطقة التنفيذ"
        execution_zone = True
        reasons.append("السعر قريب من منطقة الدخول")
        action = "🟢 السعر داخل/قريب من منطقة التنفيذ — القرار النهائي يعتمد على السيولة والمقاومة."
    else:
        status = "valid_watch"
        label = "مراقبة بخطة صالحة"
        action = "👀 الخطة صالحة للمراقبة — انتظر اكتمال شرط التنفيذ."

    if change < -3.0 and status not in {"broken_stop", "broken_support", "data_incomplete", "no_valid_plan"}:
        # A red/down stock is not a no-chase case; it needs rebound/reclaim.
        if status in {"waiting_trigger", "valid_watch", "blocked_by_resistance"}:
            label = "انتظار تأكيد ارتداد"
            action = "⏳ السهم ضعيف حاليًا — انتظر ارتدادًا واضحًا واستعادة مستوى الدخول قبل أي تنفيذ."
            blockers.append("الحركة الحالية سالبة؛ ليست حالة مطاردة بل انتظار ارتداد")

    return {
        "version": DECISION_CONTRACT_VERSION,
        "status": status,
        "label": label,
        "action": action,
        "blockers": _dedupe(blockers, 10),
        "reasons": _dedupe(reasons, 8),
        "price": round(price, 4),
        "entry": round(entry, 4),
        "target": round(target, 4),
        "stop": round(stop, 4),
        "support": round(support, 4),
        "resistance": round(resistance, 4),
        "entry_distance_pct": round(entry_distance, 3) if entry_distance != 999.0 else 999.0,
        "stop_distance_pct": round(stop_distance, 3) if stop_distance != 999.0 else 999.0,
        "support_distance_pct": round(support_distance, 3) if support_distance != 999.0 else 999.0,
        "resistance_distance_pct": round(resistance_distance, 3) if resistance_distance != 999.0 else 999.0,
        "day_position_pct": round(day_position, 2),
        "execution_zone": bool(execution_zone),
        "broken": bool(broken),
        "needs_reclaim": bool(needs_reclaim),
        "no_chase": bool(no_chase),
        "data_complete_for_plan": bool(data_complete_for_plan),
        "actionable_now_possible": bool(status == "execution_zone" and quote.get("reliable_for_execution")),
    }


def apply_decision_contract(row: dict) -> dict:
    if not isinstance(row, dict):
        return row
    out = dict(row)
    quote = resolve_quote_contract(out)
    plan = evaluate_plan_lifecycle(out, quote)

    out["decision_contract_version"] = DECISION_CONTRACT_VERSION
    out["quote_contract"] = quote
    out["plan_lifecycle"] = plan
    out["plan_lifecycle_status"] = plan["status"]
    out["plan_lifecycle_label"] = plan["label"]
    out["plan_lifecycle_action"] = plan["action"]
    out["plan_lifecycle_blockers"] = plan.get("blockers", [])
    out["plan_entry_distance_pct"] = plan.get("entry_distance_pct", 999.0)
    out["plan_actionable_now_possible"] = bool(plan.get("actionable_now_possible"))
    out["price_data_complete"] = bool(quote.get("complete"))
    out["price_delayed_flag"] = bool(quote.get("delayed"))
    out["price_missing_reasons"] = quote.get("missing", [])
    out["price_source_label"] = quote.get("source_label") or out.get("price_source_label")
    out["display_price"] = quote.get("price") or out.get("display_price")
    out["current_price_live"] = quote.get("price") or out.get("current_price_live")
    out["display_change_pct"] = quote.get("change_pct")
    out["display_change_available"] = bool(quote.get("change_available"))
    out["price_reliable_for_execution"] = bool(quote.get("reliable_for_execution"))

    # If the contract says the data/plan is not executable, do not let UI cards
    # display misleading zeros for entry/target/stop.  JSON null is safer than 0.
    if plan.get("status") in {"data_incomplete", "no_valid_plan"}:
        for key in [
            "display_entry_price", "display_target_price", "display_stop_price",
            "entry_price", "target_price", "target_1", "target1", "stop_loss",
            "smart_entry_price", "smart_target_1", "smart_stop_price",
        ]:
            if _f(out.get(key), 0.0) <= 0:
                out[key] = None
        out["hide_plan_numbers"] = True
        out["invalid_plan_number_reason"] = plan.get("label")
    else:
        out["hide_plan_numbers"] = False

    # Remove stale no-chase wording when current movement is down/flat or plan is broken.
    change = _f(quote.get("change_pct"), 0.0)
    if change < 3.0 and plan.get("status") in {"waiting_trigger", "broken_support", "broken_stop", "valid_watch", "blocked_by_resistance"}:
        if _s(out.get("no_chase_guard_status")) == "no_chase":
            out["stale_no_chase_guard_status"] = out.get("no_chase_guard_status")
            out["stale_no_chase_guard_label"] = out.get("no_chase_guard_label")
        out["no_chase_guard_status"] = "not_no_chase"
        out["no_chase_guard_label"] = plan.get("label")
        out["no_chase_guard_reasons"] = plan.get("blockers", [])

    return out


def compact_decision_diagnostics(row: dict) -> dict:
    row = apply_decision_contract(row or {})
    return {
        "ok": True,
        "version": DECISION_CONTRACT_VERSION,
        "symbol": _s(row.get("symbol")),
        "decision": row.get("decision"),
        "final_decision_code": row.get("final_decision_code"),
        "final_decision_label": row.get("final_decision_label"),
        "quote_contract": row.get("quote_contract"),
        "plan_lifecycle": row.get("plan_lifecycle"),
        "owner_action": row.get("owner_action"),
        "execution_readiness_label": row.get("execution_readiness_label"),
        "final_decision_blockers": row.get("final_decision_blockers"),
    }
