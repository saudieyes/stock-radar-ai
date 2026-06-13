"""Active trade-plan ledger and breakout safety guards.

Purpose:
- A BUY_NOW / دخول قوي signal must not be forgotten on later scans.
- Breakout plans must not fire BUY_NOW while price is still under the real breakout trigger.
- The module is backend-only and uses compact SQLite KV JSON; it does not store raw Polygon files.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from app.sqlite_store import get_json, set_json

TRADE_PLAN_LEDGER_VERSION = "trade_plan_ledger_v1_breakout_guard_plan_memory_2026_06_14"
NY_TZ = ZoneInfo("America/New_York")
ACTIVE_PLANS_KEY = "trade_plan_ledger:active_strong_plans_v1"
HISTORY_KEY = "trade_plan_ledger:events_v1"


def _s(value: Any) -> str:
    return str(value or "").strip()


def _u(value: Any) -> str:
    return _s(value).upper()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").replace("%", "").strip()
        return float(value)
    except Exception:
        return default


def _now_ts() -> float:
    return time.time()


def _now_str() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d")


def _dedupe(items: list[Any], limit: int = 10) -> list[str]:
    out: list[str] = []
    seen = set()
    for item in items or []:
        text = _s(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _price(row: dict) -> float:
    return _num(row.get("current_price_live") or row.get("display_price") or row.get("price") or row.get("current_price"), 0.0)


def _entry(row: dict) -> float:
    return _num(row.get("display_entry_price") or row.get("smart_entry_price") or row.get("entry_price_real") or row.get("entry_price") or row.get("entry"), 0.0)


def _stop(row: dict) -> float:
    return _num(row.get("display_stop_price") or row.get("smart_stop_loss") or row.get("stop_loss") or row.get("stop"), 0.0)


def _target1(row: dict) -> float:
    return _num(row.get("display_target_price") or row.get("smart_target_1") or row.get("target_price") or row.get("target_1") or row.get("target1") or row.get("target"), 0.0)


def _is_breakout_plan(row: dict) -> bool:
    plan_type = _s(row.get("type") or row.get("trade_type") or row.get("plan_type") or row.get("setup_type"))
    text = " ".join(_s(row.get(k)) for k in [
        "type", "trade_type", "plan_type", "setup_type", "breakout_status", "owner_action", "execution_status", "timing_signal", "final_decision_label",
    ])
    if plan_type.lower() == "breakout":
        return True
    if "اختراق" in text or "breakout" in text.lower():
        return True
    return _num(row.get("breakout_price"), 0.0) > 0 and _s(row.get("breakout_status")) != ""


def _breakout_trigger(row: dict) -> float:
    """Return the most conservative known breakout level.

    The tool historically has multiple fields (entry, breakout_price,
    confirmation_price, resistance). For breakout plans, BUY_NOW should not be
    sent while price is below the higher/real trigger. We avoid target fields.
    """
    candidates: list[float] = []
    for key in [
        "required_breakout_price", "breakout_required", "breakout_price", "confirmation_price",
        "resistance_price", "nearest_resistance", "display_resistance_price", "buy_above",
        "display_entry_price", "smart_entry_price", "entry_price_real", "entry_price", "entry",
    ]:
        n = _num(row.get(key), 0.0)
        if n > 0:
            candidates.append(n)
    if not candidates:
        return 0.0
    # Ignore extreme accidental levels far above the entry/price, but keep the
    # higher breakout/resistance level when it is close to the entry.
    price = _price(row)
    base = _entry(row) or price or min(candidates)
    sane = [x for x in candidates if base <= 0 or x <= base * 1.12]
    return max(sane or candidates)


def breakout_alert_blockers(row: dict) -> list[str]:
    """Extra Telegram/BUY_NOW blockers for breakout setups."""
    if not isinstance(row, dict) or not _is_breakout_plan(row):
        return []
    price = _price(row)
    trigger = _breakout_trigger(row)
    if price <= 0 or trigger <= 0:
        return ["breakout_trigger_missing"]
    buffer_pct = _num(os.getenv("BREAKOUT_BUY_NOW_BUFFER_PCT", "0.10"), 0.10) / 100.0
    required = trigger * (1.0 + max(0.0, buffer_pct))
    if price < required:
        return [f"breakout_not_confirmed_price_{round(price,4)}_under_{round(required,4)}"]
    # If known volume is weak, do not send Telegram as BUY_NOW for breakouts.
    rv = _num(row.get("effective_volume_ratio") or row.get("volume_ratio") or row.get("volume_pace_ratio"), 0.0)
    if 0 < rv < _num(os.getenv("BREAKOUT_MIN_VOLUME_RATIO", "1.05"), 1.05):
        return [f"breakout_volume_weak_{round(rv,2)}"]
    return []


def apply_breakout_guard_to_row(row: dict) -> dict:
    if not isinstance(row, dict):
        return row
    out = row
    out["breakout_guard_version"] = TRADE_PLAN_LEDGER_VERSION
    blockers = breakout_alert_blockers(out)
    if not blockers:
        return out
    # Only demote executable BUY_NOW/Strong states. Watch/Cautious can remain as
    # they are while we add a clear reason.
    if _s(out.get("final_decision_code")) == "BUY_NOW" or _s(out.get("decision")) == "دخول قوي":
        trigger = _breakout_trigger(out)
        price = _price(out)
        reason_ar = f"اختراق المقاومة لم يتأكد بعد: السعر {round(price, 2) if price else '—'} تحت مستوى التفعيل {round(trigger, 2) if trigger else '—'}."
        existing = list(out.get("final_decision_blockers") or [])
        out["final_decision_blockers"] = _dedupe(existing + [reason_ar], 10)
        out["decision"] = "مراقبة"
        out["effective_decision"] = "مراقبة"
        out["final_decision_code"] = "WAIT_TRIGGER"
        out["final_decision_label"] = "انتظار اختراق مقاومة"
        out["owner_action"] = f"🟠 قريب من الاختراق لكنه ليس شراء الآن — انتظر اختراق/ثبات فوق {round(trigger, 2) if trigger else 'المستوى'} بسيولة."
        out["execution_readiness_label"] = "قريب من التفعيل — لا شراء بعد"
        out["execution_status_ar"] = "انتظار اختراق مقاومة 🟠"
        out["breakout_guard_demoted_buy_now"] = True
        out["breakout_guard_blockers"] = blockers
    return out


def apply_breakout_guard_to_rows(rows: list[dict]) -> list[dict]:
    for row in rows or []:
        try:
            apply_breakout_guard_to_row(row)
        except Exception:
            continue
    return rows


def _load_active() -> dict[str, dict]:
    data = get_json(ACTIVE_PLANS_KEY, {}) or {}
    return data if isinstance(data, dict) else {}


def _save_active(data: dict[str, dict]) -> bool:
    # Keep active store compact.
    if len(data) > 250:
        items = sorted(data.items(), key=lambda kv: float(kv[1].get("created_ts", 0) or 0))[-180:]
        data = dict(items)
    return set_json(ACTIVE_PLANS_KEY, data)


def _append_events(events: list[dict]) -> None:
    if not events:
        return
    hist = get_json(HISTORY_KEY, []) or []
    if not isinstance(hist, list):
        hist = []
    hist.extend(events)
    if len(hist) > 1000:
        hist = hist[-700:]
    set_json(HISTORY_KEY, hist)


def _make_plan(row: dict, source: str = "") -> dict:
    sym = _u(row.get("symbol"))
    created = _now_ts()
    return {
        "plan_id": f"{sym}:{_today()}:{int(created)}",
        "symbol": sym,
        "status": "active",
        "created_at": _now_str(),
        "created_ts": created,
        "last_seen_at": _now_str(),
        "last_seen_ts": created,
        "source": source,
        "plan_type": "Breakout" if _is_breakout_plan(row) else _s(row.get("type") or row.get("trade_type") or row.get("plan_type") or "Unknown"),
        "alert_price": _price(row),
        "entry": _entry(row),
        "breakout_trigger": _breakout_trigger(row) if _is_breakout_plan(row) else 0.0,
        "stop": _stop(row),
        "target_1": _target1(row),
        "target_2": _num(row.get("target_2") or row.get("smart_target_2"), 0.0),
        "target_3": _num(row.get("target_3") or row.get("smart_target_3"), 0.0),
        "reasons": _dedupe((row.get("final_decision_liquidity_reasons") or row.get("success_tags") or row.get("risk_flags") or []), 8),
        "final_decision_code": _s(row.get("final_decision_code")),
        "decision": _s(row.get("decision")),
        "price_source_label": _s(row.get("price_source_label")),
        "seen_count": 1,
        "max_price_seen": _price(row),
        "min_price_seen": _price(row),
    }


def _evaluate_plan(plan: dict, row: dict) -> dict:
    price = _price(row)
    entry = _num(plan.get("entry"), 0.0)
    stop = _num(plan.get("stop"), 0.0)
    target1 = _num(plan.get("target_1"), 0.0)
    trigger = _num(plan.get("breakout_trigger"), 0.0) or entry
    plan_type = _s(plan.get("plan_type"))
    status = _s(plan.get("status") or "active")
    action = "الخطة السابقة ما زالت تحت المتابعة."
    reason = "active"
    if price > 0:
        plan["last_price"] = price
        if price > _num(plan.get("max_price_seen"), 0.0):
            plan["max_price_seen"] = price
        if _num(plan.get("min_price_seen"), 0.0) <= 0 or price < _num(plan.get("min_price_seen"), 0.0):
            plan["min_price_seen"] = price
    if price <= 0:
        status = "unknown_price"
        reason = "price_missing"
        action = "الخطة السابقة موجودة لكن السعر الحالي غير متوفر."
    elif stop > 0 and price <= stop:
        status = "failed_stop"
        reason = "stop_broken"
        action = f"🔴 الخطة السابقة فشلت: السعر كسر وقف الخطة {round(stop, 2)}."
    elif target1 > 0 and price >= target1:
        status = "target_1_hit"
        reason = "target_hit"
        action = f"✅ الخطة السابقة وصلت الهدف الأول {round(target1, 2)} — قيّم تأمين الربح أو شروط الهدف التالي."
    elif plan_type.lower() == "breakout" and trigger > 0 and price < trigger * 0.995:
        status = "breakout_needs_reclaim"
        reason = "lost_breakout_trigger"
        action = f"⚠️ الخطة السابقة كانت اختراقًا لكنها تحت مستوى التفعيل {round(trigger, 2)} الآن — لا تضف، وانتظر استعادة المستوى أو خفف المخاطرة حسب خطتك."
    elif entry > 0 and price < entry * 0.992:
        status = "under_entry_warning"
        reason = "under_entry"
        action = f"⚠️ السعر تحت دخول الخطة السابقة {round(entry, 2)} — الخطة تحتاج استعادة قبل أي إضافة."
    else:
        status = "active"
        reason = "still_valid"
        action = "🟢 الخطة السابقة ما زالت صالحة ما لم يكسر السعر وقفها أو يفقد مستوى التفعيل."
    return {"status": status, "reason": reason, "action": action}


def record_active_strong_plans(rows: list[dict], source: str = "") -> dict[str, Any]:
    active = _load_active()
    events: list[dict] = []
    recorded = []
    updated = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = _u(row.get("symbol"))
        if not sym:
            continue
        if _s(row.get("final_decision_code")) != "BUY_NOW" or _s(row.get("decision")) != "دخول قوي":
            continue
        plan = active.get(sym)
        now = _now_ts()
        if isinstance(plan, dict) and _s(plan.get("status")) in {"active", "under_entry_warning", "breakout_needs_reclaim", "unknown_price", "target_1_hit"}:
            eval_now = _evaluate_plan(plan, row)
            plan["status"] = eval_now["status"]
            plan["last_status_reason"] = eval_now["reason"]
            plan["last_action"] = eval_now["action"]
            plan["last_seen_at"] = _now_str()
            plan["last_seen_ts"] = now
            plan["seen_count"] = int(plan.get("seen_count", 0) or 0) + 1
            active[sym] = plan
            updated.append(sym)
        else:
            new_plan = _make_plan(row, source=source)
            active[sym] = new_plan
            recorded.append(sym)
            events.append({"event": "plan_created", "symbol": sym, "plan_id": new_plan.get("plan_id"), "at": _now_str(), "source": source, "entry": new_plan.get("entry"), "price": new_plan.get("alert_price"), "stop": new_plan.get("stop"), "target_1": new_plan.get("target_1"), "plan_type": new_plan.get("plan_type")})
    _save_active(active)
    _append_events(events)
    return {"ok": True, "version": TRADE_PLAN_LEDGER_VERSION, "recorded": recorded, "updated": updated, "active_count": len(active)}


def enrich_rows_with_active_plan_status(rows: list[dict]) -> list[dict]:
    active = _load_active()
    changed_events: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = _u(row.get("symbol"))
        plan = active.get(sym)
        if not isinstance(plan, dict):
            continue
        evaluation = _evaluate_plan(plan, row)
        old_status = _s(plan.get("status"))
        new_status = evaluation["status"]
        plan["status"] = new_status
        plan["last_status_reason"] = evaluation["reason"]
        plan["last_action"] = evaluation["action"]
        plan["last_seen_at"] = _now_str()
        plan["last_seen_ts"] = _now_ts()
        active[sym] = plan
        if new_status != old_status:
            changed_events.append({"event": "plan_status_changed", "symbol": sym, "from": old_status, "to": new_status, "at": _now_str(), "price": _price(row), "reason": evaluation["reason"]})
        row["active_strong_plan_version"] = TRADE_PLAN_LEDGER_VERSION
        row["active_strong_plan"] = {
            "plan_id": plan.get("plan_id"),
            "created_at": plan.get("created_at"),
            "plan_type": plan.get("plan_type"),
            "entry": plan.get("entry"),
            "breakout_trigger": plan.get("breakout_trigger"),
            "stop": plan.get("stop"),
            "target_1": plan.get("target_1"),
            "status": new_status,
            "action": evaluation["action"],
        }
        row["active_strong_plan_status"] = new_status
        row["active_strong_plan_action_ar"] = evaluation["action"]
        if new_status in {"failed_stop"}:
            row["decision"] = "مراقبة"
            row["effective_decision"] = "مراقبة"
            row["final_decision_code"] = "PLAN_BROKEN"
            row["final_decision_label"] = "الخطة السابقة فشلت"
            row["owner_action"] = evaluation["action"]
        elif new_status in {"breakout_needs_reclaim", "under_entry_warning"} and _s(row.get("final_decision_code")) != "BUY_NOW":
            old_action = _s(row.get("owner_action"))
            row["owner_action"] = evaluation["action"] + ("\n" + old_action if old_action and evaluation["action"] not in old_action else "")
    if changed_events:
        _append_events(changed_events)
    _save_active(active)
    return rows


def active_plan_status(limit: int = 100) -> dict[str, Any]:
    active = _load_active()
    rows = list(active.values())
    rows.sort(key=lambda p: float(p.get("created_ts", 0) or 0), reverse=True)
    hist = get_json(HISTORY_KEY, []) or []
    if not isinstance(hist, list):
        hist = []
    return {
        "ok": True,
        "version": TRADE_PLAN_LEDGER_VERSION,
        "active_count": len(active),
        "plans": rows[: max(1, int(limit or 100))],
        "recent_events": hist[-50:],
        "rule_ar": "أي دخول قوي يُحفظ كخطة أصلية، والفحوص اللاحقة تقيم السهم مقابل الخطة الأصلية قبل عرض خطة جديدة.",
    }
