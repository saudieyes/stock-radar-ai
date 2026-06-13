"""Weekly Plan Lifecycle V1 for Polygon / swing plans.

Tracks saved weekly candidates as plans that can become: active, warning,
needs_reclaim, failed, target_hit, or no_chase. Backend-only, compact SQLite.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from app.sqlite_store import get_json, set_json

WEEKLY_PLAN_LIFECYCLE_VERSION = "weekly_plan_lifecycle_v1_2026_06_14"
STATE_KEY = "weekly_plan_lifecycle:state_v1"
EVENTS_KEY = "weekly_plan_lifecycle:events_v1"
NY_TZ = ZoneInfo("America/New_York")


def _s(v: Any) -> str:
    return str(v or "").strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace("$", "").replace(",", "").replace("%", "").strip()
        return float(v)
    except Exception:
        return default


def _now_str() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _now_ts() -> float:
    return time.time()


def _price(row: dict) -> float:
    return _num(row.get("current_price_live") or row.get("display_price") or row.get("price") or row.get("current_price") or row.get("last_close") or row.get("close"), 0.0)


def _trigger(row: dict) -> float:
    return _num(row.get("breakout_trigger_price") or row.get("suggested_watch_zone_high") or row.get("display_entry_price") or row.get("smart_entry_price") or row.get("entry") or row.get("last_close"), 0.0)


def _stop(row: dict) -> float:
    return _num(row.get("invalidation") or row.get("display_stop_price") or row.get("smart_stop_loss") or row.get("stop_loss") or row.get("stop"), 0.0)


def _target(row: dict) -> float:
    return _num(row.get("first_target") or row.get("display_target_price") or row.get("smart_target_1") or row.get("target_1") or row.get("target"), 0.0)


def _load() -> dict:
    data = get_json(STATE_KEY, {}) or {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> bool:
    if len(data) > 250:
        items = sorted(data.items(), key=lambda kv: float(kv[1].get("created_ts", 0) or 0))[-160:]
        data = dict(items)
    return set_json(STATE_KEY, data)


def _append(events: list[dict]) -> None:
    if not events:
        return
    hist = get_json(EVENTS_KEY, []) or []
    if not isinstance(hist, list):
        hist = []
    hist.extend(events)
    if len(hist) > 2000:
        hist = hist[-1200:]
    set_json(EVENTS_KEY, hist)


def _is_weekly(row: dict) -> bool:
    txt = " ".join(_s(row.get(k)) for k in ["stage", "pattern", "quality_bucket", "source"]).lower()
    return "weekly" in txt or "polygon" in txt or bool(row.get("weekly_priority")) or bool(row.get("clean_weekly_priority"))


def _make_plan(row: dict) -> dict:
    sym = _u(row.get("symbol"))
    p = _price(row)
    return {
        "symbol": sym,
        "status": "active",
        "status_ar": "الخطة الأسبوعية نشطة",
        "created_at": _now_str(),
        "created_ts": _now_ts(),
        "last_seen_at": _now_str(),
        "last_price": p,
        "entry_reference": p,
        "trigger": _trigger(row),
        "stop": _stop(row),
        "target_1": _target(row),
        "highest_seen": p,
        "lowest_seen": p,
        "seen_count": 1,
        "source": "polygon_weekly",
        "reasons": [str(x) for x in (row.get("reasons") or [])][:8],
    }


def _evaluate(plan: dict, row: dict) -> tuple[dict, list[dict]]:
    events: list[dict] = []
    price = _price(row) or _num(plan.get("last_price"), 0.0)
    trigger = _num(plan.get("trigger"), 0.0)
    stop = _num(plan.get("stop"), 0.0)
    target = _num(plan.get("target_1"), 0.0)
    old_status = _s(plan.get("status")) or "active"
    status = old_status
    status_ar = _s(plan.get("status_ar")) or "الخطة الأسبوعية نشطة"
    action_ar = "استمر بالمراقبة حسب الخطة."
    if price > 0:
        plan["last_price"] = round(price, 4)
        plan["highest_seen"] = max(_num(plan.get("highest_seen"), price), price)
        plan["lowest_seen"] = min(_num(plan.get("lowest_seen"), price), price)
    plan["last_seen_at"] = _now_str()
    plan["seen_count"] = int(_num(plan.get("seen_count"), 0)) + 1
    if price > 0 and target > 0 and price >= target:
        status = "target_hit"
        status_ar = "حقق الهدف الأول"
        action_ar = "أمّن جزءًا من الربح؛ الهدف الثاني لا يعتمد إلا بثبات وسيولة."
    elif price > 0 and stop > 0 and price <= stop:
        status = "failed"
        status_ar = "فشلت الخطة الأسبوعية"
        action_ar = "لا تضف؛ الخطة فشلت أو تحتاج بناء جديد بعد استعادة المستوى."
    elif price > 0 and trigger > 0 and price < trigger * 0.985:
        status = "needs_reclaim"
        status_ar = f"تحتاج استعادة {round(trigger, 2)}"
        action_ar = f"لا دخول جديد قبل استعادة {round(trigger, 2)} بثبات."
    elif price > 0 and trigger > 0 and price >= trigger and price <= trigger * 1.035:
        status = "active"
        status_ar = "الخطة الأسبوعية نشطة قرب التفعيل"
        action_ar = "صالحة للمراقبة؛ لا تتحول لتنفيذ إلا بتأكيد حي."
    elif price > 0 and trigger > 0 and price > trigger * 1.07:
        status = "no_chase"
        status_ar = "تحرك وفات — لا تطارد"
        action_ar = "انتظر Pullback أو خطة جديدة؛ لا تلاحق السعر."
    else:
        status = "active"
        status_ar = "الخطة الأسبوعية نشطة"
        action_ar = "استمر بالمراقبة حسب الخطة."
    plan["status"] = status
    plan["status_ar"] = status_ar
    plan["action_ar"] = action_ar
    if status != old_status:
        events.append({"event": "weekly_status_change", "at": _now_str(), "symbol": plan.get("symbol"), "from": old_status, "to": status, "price": round(price, 4), "action_ar": action_ar})
    return plan, events


def evaluate_weekly_rows(rows: list[dict], source: str = "scan") -> dict:
    data = _load()
    events: list[dict] = []
    processed = 0
    for row in rows or []:
        if not isinstance(row, dict) or not _is_weekly(row):
            continue
        sym = _u(row.get("symbol"))
        if not sym:
            continue
        plan = data.get(sym) or _make_plan(row)
        plan, ev = _evaluate(plan, row)
        plan["source_last"] = source
        data[sym] = plan
        row["weekly_plan_status"] = plan.get("status")
        row["weekly_plan_status_ar"] = plan.get("status_ar")
        row["weekly_plan_action_ar"] = plan.get("action_ar")
        row["weekly_plan_trigger"] = plan.get("trigger")
        row["weekly_plan_stop"] = plan.get("stop")
        row["weekly_plan_target_1"] = plan.get("target_1")
        events.extend(ev)
        processed += 1
    _save(data)
    _append(events)
    return {"ok": True, "version": WEEKLY_PLAN_LIFECYCLE_VERSION, "processed": processed, "events": events[-20:]}


def weekly_plan_lifecycle_status(limit: int = 80) -> dict:
    data = _load()
    events = get_json(EVENTS_KEY, []) or []
    plans = list(data.values())
    plans.sort(key=lambda p: str(p.get("symbol")))
    return {
        "ok": True,
        "version": WEEKLY_PLAN_LIFECYCLE_VERSION,
        "active_count": len(plans),
        "plans": plans[: max(1, min(int(limit or 80), 200))],
        "recent_events": events[-40:] if isinstance(events, list) else [],
        "rule_ar": "قائمة Polygon لا تبقى ثابتة عمياء؛ كل سهم له حالة أسبوعية: نشطة، تحتاج استعادة، فشلت، هدف تحقق، أو لا تطارد.",
    }
