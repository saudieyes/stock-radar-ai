"""Final decision engine for Stock Radar AI.

V2 cleans the visible decision path after months of layered patches:
- every row is first normalized through decision_contract;
- broken/stale plans cannot remain Cautious/Strong;
- No-Chase is reserved for true upward extension only;
- Telegram can trust BUY_NOW as an executable-now state.
"""
from __future__ import annotations

from typing import Any

from app.decision_contract import apply_decision_contract

FINAL_DECISION_ENGINE_VERSION = "official_final_decision_engine_v2_clean_visible_decision_2026_06_05"

BUY_NOW = "BUY_NOW"
WAIT_TRIGGER = "WAIT_TRIGGER"
WAIT_LIQUIDITY = "WAIT_LIQUIDITY"
WAIT_RESISTANCE = "WAIT_RESISTANCE"
WAIT_REBOUND = "WAIT_REBOUND"
RECLAIM_REQUIRED = "RECLAIM_REQUIRED"
PLAN_BROKEN = "PLAN_BROKEN"
DATA_INCOMPLETE = "DATA_INCOMPLETE"
EARLY_WATCH = "EARLY_WATCH"
CONTINUATION = "CONTINUATION"
PULLBACK_REQUIRED = "PULLBACK_REQUIRED"
NO_CHASE = "NO_CHASE"
WATCH = "WATCH"


def _txt(value: Any) -> str:
    return str(value or "").strip()


def _f(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        try:
            value = row.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, str):
                value = value.replace("$", "").replace("%", "").replace(",", "").strip()
            return float(value)
        except Exception:
            continue
    return float(default)


def _dedupe(items: list[Any], limit: int = 10) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        s = _txt(item)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _stage(row: dict) -> str:
    meta = row.get("move_stage_v2") if isinstance(row.get("move_stage_v2"), dict) else {}
    return _txt(meta.get("move_stage") or row.get("move_stage"))


def _stage_label(row: dict) -> str:
    meta = row.get("move_stage_v2") if isinstance(row.get("move_stage_v2"), dict) else {}
    return _txt(meta.get("move_stage_label") or row.get("move_stage_label"))


def _has_text(row: dict, keys: list[str], *phrases: str) -> bool:
    text = " ".join(_txt(row.get(key)) for key in keys)
    return any(p and p in text for p in phrases)


def _price(row: dict) -> float:
    return _f(row, ["current_price_live", "display_price", "price", "current_price", "fmp_price", "live_price", "last_price"], 0.0)


def _entry(row: dict) -> float:
    return _f(row, ["display_entry_price", "entry_price", "entry", "buy_above", "breakout_price", "confirmation_price"], 0.0)


def _entry_distance_pct(row: dict) -> float:
    contract = row.get("plan_lifecycle") if isinstance(row.get("plan_lifecycle"), dict) else {}
    if contract and contract.get("entry_distance_pct", 999.0) != 999.0:
        try:
            return float(contract.get("entry_distance_pct"))
        except Exception:
            pass
    price = _price(row)
    entry = _entry(row)
    if price > 0 and entry > 0:
        return ((price - entry) / entry) * 100.0
    return 999.0


def _liquidity_confirmed(row: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    volume = _f(row, ["effective_volume_ratio", "volume_pace_ratio", "volume_ratio", "relative_volume", "rvol"], 0.0)
    liq_score = _f(row, ["liquidity_persistence_score", "liquidity_score"], 0.0)
    dollar_volume = _f(row, ["dollar_volume", "live_dollar_volume", "fmp_dollar_volume"], 0.0)
    success_tags = " ".join(str(x) for x in (row.get("success_tags") or []))
    risk_tags = " ".join(str(x) for x in (row.get("risk_tags") or []) + (row.get("risk_flags") or []))
    action_text = " ".join([
        _txt(row.get("owner_action")),
        _txt(row.get("execution_readiness_label")),
        _txt(row.get("execution_gate_label")),
        _txt(row.get("live_plan_action")),
        _txt(row.get("liquidity_persistence_label")),
    ])

    weak = False
    if "السيولة لم تستمر" in risk_tags or "سيولة مؤقتة" in action_text or ("ضعيفة" in action_text and "السيولة" in action_text):
        weak = True
        reasons.append("السيولة الحالية غير مؤكدة أو ضعفت")
    if volume >= 1.05:
        reasons.append(f"RVOL/volume={round(volume, 2)}")
    if liq_score >= 55:
        reasons.append(f"liquidity_score={round(liq_score, 1)}")
    if dollar_volume >= 20_000_000:
        reasons.append("دولار فوليوم كافٍ")
    if "السيولة استمرت" in success_tags:
        reasons.append("السيولة استمرت")

    confirmed = (volume >= 1.05 or liq_score >= 55 or dollar_volume >= 20_000_000 or "السيولة استمرت" in success_tags) and not weak
    return confirmed, _dedupe(reasons, 6)


def _resistance_blocked(row: dict) -> tuple[bool, str]:
    contract = row.get("plan_lifecycle") if isinstance(row.get("plan_lifecycle"), dict) else {}
    status = _txt(contract.get("status"))
    if status == "blocked_by_resistance":
        blockers = contract.get("blockers") if isinstance(contract.get("blockers"), list) else []
        return True, blockers[0] if blockers else "مقاومة قريبة تمنع الدخول الآن"
    res_dist = _f(row, ["nearest_resistance_distance_pct", "resistance_distance_pct", "distance_to_resistance_pct", "structure_resistance_distance_pct"], 999.0)
    risk_tags = " ".join(str(x) for x in (row.get("risk_tags") or []) + (row.get("risk_flags") or []))
    labels = " ".join([_txt(row.get("resistance_guard_label")), _txt(row.get("support_guard_label")), _txt(row.get("execution_gate_label"))])
    if "قريب من مقاومة قوية" in risk_tags:
        return True, "قريب من مقاومة قوية"
    if _txt(row.get("close_resistance_guard_flag")).lower() in {"1", "true", "yes"}:
        return True, "مقاومة قريبة تمنع الدخول الآن"
    if "مقاومة" in labels and any(w in labels for w in ["قريب", "خانقة", "قوية"]):
        return True, "مقاومة قريبة تمنع الدخول الآن"
    if 0 <= res_dist <= 0.85:
        return True, f"المقاومة قريبة جدًا ({round(res_dist, 2)}%)"
    return False, ""


def _previous_move_flags(row: dict) -> tuple[bool, list[str]]:
    vals = {
        "prior_day_change_pct": _f(row, ["prior_day_change_pct", "previous_day_change_pct", "last_session_change_pct"], 0.0),
        "rolling_3d_change_pct": _f(row, ["rolling_3d_change_pct", "three_day_change_pct", "last_3d_change_pct"], 0.0),
        "weekly_change_pct": _f(row, ["weekly_change_pct", "week_change_pct", "five_day_change_pct"], 0.0),
        "monthly_change_pct": _f(row, ["monthly_change_pct", "month_change_pct", "twenty_day_change_pct"], 0.0),
    }
    reasons = []
    if vals["prior_day_change_pct"] >= 12:
        reasons.append(f"ارتفع في الجلسة السابقة {round(vals['prior_day_change_pct'], 1)}%")
    if vals["rolling_3d_change_pct"] >= 18:
        reasons.append(f"ارتفع خلال 3 جلسات {round(vals['rolling_3d_change_pct'], 1)}%")
    if vals["weekly_change_pct"] >= 20:
        reasons.append(f"ارتفع أسبوعيًا {round(vals['weekly_change_pct'], 1)}%")
    if vals["monthly_change_pct"] >= 35:
        reasons.append(f"ارتفع شهريًا {round(vals['monthly_change_pct'], 1)}%")
    return bool(reasons), reasons



def _visible_stage_for_code(code: str) -> tuple[str, str, str, str]:
    """Return canonical user-facing stage/list/status for the final decision.

    Legacy discovery layers can still remember a historical peak as No-Chase.
    The UI, summaries, and Telegram must follow the current final decision only.
    """
    mapping = {
        BUY_NOW: ("Actionable Now", "🟢 دخول قوي مؤكد", "buy_now", "actionable_now"),
        WAIT_TRIGGER: ("Wait Trigger", "⏳ انتظار تفعيل", "watch", "waiting_trigger"),
        WAIT_LIQUIDITY: ("Wait Liquidity", "⏳ انتظار السيولة", "watch", "wait_liquidity"),
        WAIT_RESISTANCE: ("Wait Resistance", "🟠 انتظار اختراق مقاومة", "watch", "wait_resistance"),
        WAIT_REBOUND: ("Wait Rebound", "⏳ انتظار ارتداد", "watch", "wait_rebound"),
        RECLAIM_REQUIRED: ("Reclaim Required", "🔴 انتظار استعادة", "watch", "reclaim_required"),
        PLAN_BROKEN: ("Plan Broken", "🔴 الخطة مكسورة", "watch", "plan_broken"),
        DATA_INCOMPLETE: ("Data Incomplete", "⚪ بيانات غير مكتملة", "watch", "data_incomplete"),
        EARLY_WATCH: ("Early Watch", "🔵 مراقبة مبكرة", "pre_move_watch", "early_watch"),
        CONTINUATION: ("Continuation Watch", "🔵 استمرار مشروط", "continuation", "continuation_only"),
        PULLBACK_REQUIRED: ("Requires Pullback", "⏳ يحتاج Pullback", "continuation", "pullback_required"),
        NO_CHASE: ("No-Chase", "⛔ لا تطارد", "no_chase", "hard_no_chase_cap"),
        WATCH: ("Watch", "👀 مراقبة", "watch", "watch"),
    }
    return mapping.get(code, mapping[WATCH])


def _contains_no_chase_text(value: Any) -> bool:
    text = _txt(value)
    return bool("No-Chase" in text or "لا تطارد" in text or "الحركة متأخرة" in text)


def _scrub_no_chase_text_list(items: Any) -> list[str]:
    """Remove legacy No-Chase wording unless the final decision is NO_CHASE."""
    cleaned: list[str] = []
    for item in items or []:
        text = _txt(item)
        if not text:
            continue
        if _contains_no_chase_text(text):
            continue
        cleaned.append(text)
    return _dedupe(cleaned, 10)


def _scrub_no_chase_mapping(mapping: dict, replacement: str) -> dict:
    out = dict(mapping or {})
    for key, value in list(out.items()):
        if isinstance(value, str) and _contains_no_chase_text(value):
            out[key] = "" if key in {"excluded_reason", "no_chase_reason"} else replacement
        elif isinstance(value, list):
            out[key] = [x for x in value if not _contains_no_chase_text(x)]
        elif isinstance(value, dict):
            out[key] = _scrub_no_chase_mapping(value, replacement)
    return out


def _sync_visible_legacy_fields(out: dict, code: str, final_label: str) -> dict:
    """Make legacy presentation fields obey the final decision contract.

    This function is deliberately called at the very end of final_decision_engine.
    It does not change raw evidence, but it prevents old layers from showing a
    stale No-Chase/late/cautious label after the current contract says broken,
    reclaim, rebound, pullback, data-incomplete, or watch.
    """
    visible_stage, visible_stage_label, visible_list, visible_status = _visible_stage_for_code(code)
    old_stage = _txt(out.get("move_stage"))
    old_stage_label = _txt(out.get("move_stage_label"))
    old_status = _txt(out.get("source_promotion_v2_status"))
    old_list = _txt(out.get("source_promotion_v2_list"))

    out["visible_decision_code"] = code
    out["visible_decision_label"] = final_label
    out["visible_move_stage"] = visible_stage
    out["visible_move_stage_label"] = visible_stage_label
    out["visible_source_promotion_list"] = visible_list
    out["visible_source_promotion_status"] = visible_status
    out["user_facing_decision_status"] = visible_status
    out["user_facing_decision_label"] = final_label

    legacy_no_chase_stage = old_stage in {"No-Chase", "Extended", "Catalyst Spike Review"}
    if code != NO_CHASE and legacy_no_chase_stage:
        out["legacy_move_stage_suppressed_by_decision_contract"] = True
        if old_stage_label:
            out["legacy_move_stage_label_suppressed_by_decision_contract"] = True
        out["move_stage"] = visible_stage
        out["move_stage_label"] = visible_stage_label
        mv2 = out.get("move_stage_v2") if isinstance(out.get("move_stage_v2"), dict) else None
        if mv2:
            mv2 = dict(mv2)
            mv2["legacy_move_stage_suppressed_by_decision_contract"] = True
            mv2["legacy_move_stage_label_suppressed_by_decision_contract"] = True
            mv2["move_stage"] = visible_stage
            mv2["move_stage_label"] = visible_stage_label
            mv2["move_stage_action"] = out.get("owner_action") or final_label
            out["move_stage_v2"] = mv2
    elif code == NO_CHASE:
        out["move_stage"] = "No-Chase"
        out["move_stage_label"] = "⛔ لا تطارد"

    # If V2 source/promotion already marked hard_no_chase historically, align it
    # with the current final decision before summaries/UI read it.
    if code != NO_CHASE and old_status == "hard_no_chase_cap":
        out["legacy_source_promotion_v2_status_suppressed_by_decision_contract"] = True
        out["source_promotion_v2_status"] = visible_status
        out["source_promotion_v2_list"] = visible_list
        out["source_promotion_v2_capped"] = False
        out["source_promotion_v2_cap_reason"] = final_label
    elif code == NO_CHASE:
        out["source_promotion_v2_status"] = "hard_no_chase_cap"
        out["source_promotion_v2_list"] = "no_chase"

    # Promotion V2a blockers are used in diagnostics and sometimes UI.  Remove
    # stale no-chase wording unless it is the final decision.
    if code != NO_CHASE:
        for key in ("promotion_block_reasons", "live_overlay_block_reasons", "source_promotion_v2_reasons", "tier_cap_reasons"):
            val = out.get(key)
            if isinstance(val, list):
                cleaned = _scrub_no_chase_text_list(val)
                if cleaned != val:
                    out[f"legacy_{key}_suppressed_by_decision_contract"] = True
                    out[key] = cleaned
        if _txt(out.get("promotion_summary")) and any(x in _txt(out.get("promotion_summary")) for x in ["No-Chase", "لا تطارد", "الحركة متأخرة"]):
            out["legacy_promotion_summary_suppressed_by_decision_contract"] = True
            out["promotion_summary"] = f"{out.get('source_priority_lane_label') or 'مصدر'} — القرار النهائي الحالي: {final_label}."
        if _txt(out.get("source_promotion_v2_summary")) and any(x in _txt(out.get("source_promotion_v2_summary")) for x in ["No-Chase", "لا تطارد", "الحركة متأخرة"]):
            out["legacy_source_promotion_v2_summary_suppressed_by_decision_contract"] = True
            out["source_promotion_v2_summary"] = f"{visible_stage_label} — {out.get('owner_action') or final_label}"
        if _txt(out.get("execution_gate_status")) == "no_chase":
            out["legacy_execution_gate_status_suppressed_by_decision_contract"] = True
            out["legacy_execution_gate_label_suppressed_by_decision_contract"] = True
            out["execution_gate_status"] = visible_status
            out["execution_gate_label"] = final_label
        em = out.get("early_movement") if isinstance(out.get("early_movement"), dict) else None
        if em:
            raw_em_json = ""
            try:
                raw_em_json = str(em)
            except Exception:
                raw_em_json = ""
            em = _scrub_no_chase_mapping(dict(em), visible_stage_label)
            em_text = " ".join(_txt(em.get(k)) for k in ["status", "status_label", "move_stage", "excluded_reason", "summary", "recommended_action"])
            if _txt(em.get("status")) == "no_chase" or _contains_no_chase_text(raw_em_json) or _contains_no_chase_text(em_text):
                em["legacy_status_suppressed_by_decision_contract"] = True
                em["legacy_status_label_suppressed_by_decision_contract"] = True
                em["legacy_move_stage_suppressed_by_decision_contract"] = True
                em["legacy_excluded_reason_suppressed_by_decision_contract"] = True
                em["status"] = visible_status if visible_status not in {"buy_now"} else "priority_watch"
                em["status_label"] = visible_stage_label
                em["move_stage"] = visible_stage
                em["excluded_reason"] = ""
                em["no_chase_reasons"] = []
                em["recommended_action"] = out.get("owner_action") or final_label
                em["summary"] = f"{visible_stage_label} — {out.get('owner_action') or final_label}"
            out["early_movement"] = em
            out["early_movement_status"] = em.get("status", out.get("early_movement_status"))
            out["early_movement_status_label"] = em.get("status_label", out.get("early_movement_status_label"))
    return out

def _set_common(out: dict, code: str, final_decision: str, blockers: list[str], action: str, *, liquidity_ok: bool, liquidity_reasons: list[str], entry_dist: float, stage: str, stage_label: str) -> dict:
    labels = {
        BUY_NOW: "دخول قوي مؤكد",
        WAIT_TRIGGER: "انتظار تفعيل",
        WAIT_LIQUIDITY: "انتظار السيولة",
        WAIT_RESISTANCE: "انتظار اختراق مقاومة",
        WAIT_REBOUND: "انتظار ارتداد",
        RECLAIM_REQUIRED: "انتظار استعادة",
        PLAN_BROKEN: "الخطة مكسورة",
        DATA_INCOMPLETE: "بيانات غير مكتملة",
        EARLY_WATCH: "مراقبة مبكرة",
        CONTINUATION: "استمرار مشروط",
        PULLBACK_REQUIRED: "يحتاج Pullback",
        NO_CHASE: "لا تطارد",
        WATCH: "مراقبة",
    }
    out["decision"] = final_decision
    out["effective_decision"] = final_decision
    out["final_decision_engine_version"] = FINAL_DECISION_ENGINE_VERSION
    out["final_decision_code"] = code
    out["final_decision_label"] = labels.get(code, "مراقبة")
    out["final_decision_blockers"] = _dedupe(blockers, 10)
    out["final_decision_liquidity_ok"] = bool(liquidity_ok)
    out["final_decision_liquidity_reasons"] = liquidity_reasons
    out["final_decision_entry_distance_pct"] = round(entry_dist, 3) if entry_dist != 999.0 else 999.0
    out["final_decision_stage"] = stage
    out["final_decision_stage_label"] = stage_label
    out["owner_action"] = action

    # Clear stale no-chase fields unless the final decision is truly NO_CHASE.
    # Older discovery layers may mark a stock as no-chase because it once peaked,
    # but the current actionable state can be broken/reclaim/wait-rebound.
    if code != NO_CHASE:
        stale_status = _txt(out.get("no_chase_guard_status")).lower()
        stale_label = _txt(out.get("no_chase_guard_label"))
        if stale_status == "no_chase" or "لا تطارد" in stale_label or "مطاردة" in stale_label:
            out["stale_no_chase_guard_status"] = out.get("no_chase_guard_status")
            out["stale_no_chase_guard_label"] = out.get("no_chase_guard_label")
            out["no_chase_guard_status"] = "not_no_chase"
            out["no_chase_guard_label"] = labels.get(code, "مراقبة")
            out["no_chase_guard_reasons"] = out.get("final_decision_blockers", [])
        out["no_chase_hard_cap"] = False
    # Keep old UI fields synchronized with the final contract.
    if code == BUY_NOW:
        out["execution_readiness_label"] = "جاهز للتنفيذ الآن"
        out["execution_readiness_icon"] = "🟢"
        out["execution_status_ar"] = "دخول قوي مؤكد 🟢"
    elif code == NO_CHASE:
        out["execution_readiness_label"] = "لا تطارد"
        out["execution_readiness_icon"] = "⛔"
        out["execution_status_ar"] = "لا تطارد ⛔"
    elif code == PLAN_BROKEN:
        out["execution_readiness_label"] = "الخطة مكسورة"
        out["execution_readiness_icon"] = "🔴"
        out["execution_status_ar"] = "الخطة مكسورة 🔴"
    elif code == RECLAIM_REQUIRED:
        out["execution_readiness_label"] = "انتظار استعادة"
        out["execution_readiness_icon"] = "🔴"
        out["execution_status_ar"] = "انتظار استعادة 🔴"
    elif code == WAIT_REBOUND:
        out["execution_readiness_label"] = "انتظار ارتداد"
        out["execution_readiness_icon"] = "⏳"
        out["execution_status_ar"] = "انتظار ارتداد ⏳"
    elif code == PULLBACK_REQUIRED:
        out["execution_readiness_label"] = "يحتاج Pullback"
        out["execution_readiness_icon"] = "⏳"
        out["execution_status_ar"] = "يحتاج Pullback ⏳"
    elif code == DATA_INCOMPLETE:
        out["execution_readiness_label"] = "بيانات غير مكتملة"
        out["execution_readiness_icon"] = "⚪"
        out["execution_status_ar"] = "بيانات غير مكتملة ⚪"
    elif code in {WAIT_LIQUIDITY, WAIT_RESISTANCE, WAIT_TRIGGER}:
        out["execution_readiness_label"] = "انتظار تأكيد"
        out["execution_readiness_icon"] = "🟠"
        out["execution_status_ar"] = "انتظار تأكيد 🟠"
    return _sync_visible_legacy_fields(out, code, labels.get(code, "مراقبة"))


def apply_final_decision(row: dict) -> dict:
    if not isinstance(row, dict):
        return row

    out = apply_decision_contract(dict(row))
    original_decision = _txt(out.get("decision")) or "مراقبة"
    out["decision_before_final_engine"] = original_decision
    stage = _stage(out)
    stage_label = _stage_label(out)
    plan = out.get("plan_lifecycle") if isinstance(out.get("plan_lifecycle"), dict) else {}
    plan_status = _txt(plan.get("status"))
    plan_label = _txt(plan.get("label"))
    plan_action = _txt(plan.get("action"))
    plan_blockers = plan.get("blockers") if isinstance(plan.get("blockers"), list) else []
    entry_dist = _entry_distance_pct(out)
    liquidity_ok, liquidity_reasons = _liquidity_confirmed(out)
    resistance_blocked, resistance_reason = _resistance_blocked(out)
    prior_move_risk, prior_move_reasons = _previous_move_flags(out)
    price_reliable = bool(out.get("price_reliable_for_execution", False))
    gain_at_detection = _f(out, ["gain_at_detection"], 0.0)
    current_gain = _f(out, ["current_gain", "display_change_pct", "live_change_pct", "change_pct"], 0.0)
    peak_gain = max(_f(out, ["peak_gain_seen"], 0.0), _f(out, ["intraday_peak_gain"], 0.0), _f(out, ["max_gain_basis"], 0.0), current_gain, gain_at_detection)
    readiness = _f(out, ["execution_readiness_score", "readiness_score"], 0.0)
    rr = _f(out, ["rr_1", "risk_reward", "reward_risk"], 0.0)

    # Contract hard stops come first: no old badge may override these.
    if plan_status == "data_incomplete":
        return _set_common(out, DATA_INCOMPLETE, "مراقبة", plan_blockers, plan_action, liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if plan_status in {"no_valid_plan"}:
        return _set_common(out, DATA_INCOMPLETE, "مراقبة", plan_blockers, plan_action, liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if plan_status == "broken_stop":
        return _set_common(out, PLAN_BROKEN, "مراقبة", plan_blockers, plan_action, liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if plan_status == "broken_support":
        return _set_common(out, RECLAIM_REQUIRED, "مراقبة", plan_blockers, plan_action, liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if plan_status == "target_reached":
        return _set_common(out, CONTINUATION, "مراقبة", plan_blockers, plan_action, liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)

    # If the best available quote is delayed/monitoring-only (Polygon fallback),
    # do not allow any actionable Cautious/Strong label. It may remain watch,
    # reclaim, resistance, or pullback depending on the plan, but never executable.
    if not price_reliable and (bool(out.get("price_source_delayed")) or bool(out.get("price_monitoring_only"))):
        delayed_reason = "السعر من Polygon/مصدر احتياطي متأخر تقريبًا 15 دقيقة — مراقبة فقط وليس تنفيذًا مباشرًا"
        if plan_status == "blocked_by_resistance":
            return _set_common(out, WAIT_RESISTANCE, "مراقبة", _dedupe(plan_blockers + [delayed_reason], 10), plan_action or "🟠 انتظر اختراق/ثبات بسعر مباشر موثوق.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        if plan_status == "waiting_trigger":
            return _set_common(out, WAIT_TRIGGER, "مراقبة", _dedupe(plan_blockers + [delayed_reason], 10), "👀 مراقبة فقط — تحتاج سعر FMP مباشر قبل أي تنفيذ.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        if plan_status == "pullback_required":
            return _set_common(out, PULLBACK_REQUIRED, "مراقبة", _dedupe(plan_blockers + [delayed_reason], 10), plan_action or "⏳ يحتاج Pullback وسعر مباشر موثوق قبل التنفيذ.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        # For any other valid-watch state, keep it non-actionable.
        out["price_delayed_execution_blocked"] = True

    # No-Chase must only mean upward extension.  Never use it for red/down, broken,
    # flat, or below-entry states.
    # True No-Chase requires current upward extension, not only a historical peak.
    # If the stock is now red/down, under the plan, under support, or inside a
    # broken/reclaim state, the correct label is wait/reclaim/broken, not no-chase.
    price = _price(out)
    entry = _entry(out)
    currently_above_entry = bool(price > 0 and entry > 0 and price >= entry * 1.025)
    current_upward_extension = bool(
        current_gain >= 7.0
        or (current_gain >= 3.0 and currently_above_entry)
        or (entry_dist != 999.0 and entry_dist >= 3.5 and current_gain >= 3.0)
    )
    historical_peak_only = bool(peak_gain >= 12.0 or gain_at_detection >= 10.0 or stage in {"Extended", "No-Chase", "Catalyst Spike Review"})
    explicit_no_chase = (_txt(out.get("no_chase_guard_status")).lower() == "no_chase" or _has_text(out, ["no_chase_guard_label"], "لا تطارد", "مطاردة")) and current_upward_extension
    hard_no_chase = bool((historical_peak_only and current_upward_extension) or explicit_no_chase or (plan_status == "pullback_required" and bool(plan.get("no_chase")) and current_upward_extension))

    if historical_peak_only and not current_upward_extension:
        out["stale_historical_no_chase_suppressed"] = True
        out["stale_historical_no_chase_reason"] = "No-Chase history suppressed because current price is not upward-extended."

    if hard_no_chase:
        return _set_common(out, NO_CHASE, "مراقبة", ["السعر ارتفع وابتعد عن منطقة الدخول — لا تطارد"], "⛔ لا تطارد الآن — انتظر Pullback/Reclaim بمنطقة واضحة وسيولة مستمرة.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if plan_status == "pullback_required" or (peak_gain >= 10 and stage not in {"Active Breakout", "Early Confirmation"}):
        return _set_common(out, PULLBACK_REQUIRED, "مراقبة", plan_blockers or ["يحتاج Pullback أو إعادة تمركز قبل الدخول"], plan_action or "⏳ يحتاج Pullback — لا تدخل حتى يعود قرب دعم/entry أو يحدث reclaim بسيولة.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if plan_status == "waiting_trigger" and current_gain < -3.0:
        return _set_common(out, WAIT_REBOUND, "مراقبة", plan_blockers or ["السهم هابط حاليًا ويحتاج ارتداد"], plan_action, liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if plan_status == "blocked_by_resistance":
        return _set_common(out, WAIT_RESISTANCE, "مراقبة", plan_blockers or [resistance_reason], plan_action, liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if stage == "Continuation Watch" or (gain_at_detection >= 10 and not bool(out.get("stage_allows_strong"))):
        return _set_common(out, CONTINUATION, "مراقبة", ["استمرار مشروط وليس دخولًا مباشرًا"], "🔵 استمرار مشروط — انتظر ثبات/ Pullback / Reclaim بسيولة.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
    if stage == "Pre-Move":
        if current_gain < -3.0:
            return _set_common(out, WAIT_REBOUND, "مراقبة", ["مراقبة مبكرة لكن الحركة الحالية هابطة؛ تحتاج ارتداد"], "⏳ مراقبة مبكرة هابطة — الأداة تنتظر ارتدادًا/استعادة قبل أي ترقية.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        return _set_common(out, EARLY_WATCH, "مراقبة", ["مراقبة مبكرة قبل الحركة وليست دخولًا الآن"], "🟣 مراقبة مبكرة — الأداة تتابعها حتى يظهر تأكيد حي.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)

    # Strong means executable now, not merely a good idea around an entry.
    if original_decision == "دخول قوي":
        blockers: list[str] = []
        if not price_reliable:
            blockers.append("السعر غير مباشر/غير موثوق للتنفيذ")
        if plan_status != "execution_zone":
            blockers.append(plan_label or "السعر ليس داخل منطقة تنفيذ واضحة")
        if not liquidity_ok:
            blockers.append("السيولة غير مؤكدة لدخول قوي")
        if resistance_blocked:
            blockers.append(resistance_reason)
        if readiness and readiness < 58:
            blockers.append("جاهزية التنفيذ أقل من مستوى دخول قوي")
        if rr and rr < 0.65:
            blockers.append("العائد/المخاطرة غير كافٍ لدخول قوي")
        if entry_dist != 999.0 and entry_dist < -0.75:
            blockers.append("السعر لم يتفعل بعد فوق منطقة الدخول")
        if entry_dist != 999.0 and entry_dist > 1.35:
            blockers.append("السعر ابتعد عن منطقة الدخول")
        if prior_move_risk and not bool(out.get("clean_continuation_confirmed")):
            blockers += prior_move_reasons[:2]
        if _f(out, ["losing_pattern_score"], 0.0) >= 60 and _txt(out.get("pattern_action")) == "demote_or_block":
            blockers.append("يشبه نمط فشل سابق — يحتاج تأكيد أقوى")
        blockers = _dedupe(blockers, 10)
        if not blockers:
            out["final_decision_executable_now"] = True
            return _set_common(out, BUY_NOW, "دخول قوي", [], "🟢 دخول قوي مؤكد — السعر داخل منطقة التنفيذ الآن، بشرط بقاء السيولة وعدم كسر مستوى الإلغاء.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        if any("السيولة" in b for b in blockers):
            return _set_common(out, WAIT_LIQUIDITY, "مراقبة", blockers, "🟠 انتظر تأكيد السيولة — لا تدخل حتى تثبت السيولة الحية ويبقى السعر قرب الدخول.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        if any("مقاومة" in b for b in blockers):
            return _set_common(out, WAIT_RESISTANCE, "مراقبة", blockers, "🟠 انتظر اختراق/ثبات فوق المقاومة قبل أي دخول.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        return _set_common(out, WAIT_TRIGGER, "مراقبة", blockers, "🟠 كانت مرشحة قوية لكن ليست شراء الآن — انتظر اكتمال شرط التنفيذ.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)

    # Cautious is actionable only when conditional and near execution; never a late/no-plan label.
    if original_decision == "دخول بحذر":
        blockers: list[str] = []
        if plan_status not in {"execution_zone", "waiting_trigger", "valid_watch"}:
            blockers.append(plan_label or "ليست خطة حذرة قابلة للتنفيذ")
        if resistance_blocked:
            blockers.append(resistance_reason)
        if not liquidity_ok:
            blockers.append("يحتاج استمرار السيولة قبل الدخول")
        if entry_dist != 999.0 and entry_dist < -1.0:
            blockers.append("لم يتفعل السعر بعد")
        if entry_dist != 999.0 and entry_dist > 1.75:
            blockers.append("ابتعد عن منطقة الدخول")
        if _f(out, ["losing_pattern_score"], 0.0) >= 65 and _txt(out.get("pattern_action")) == "demote_or_block":
            blockers.append("يشبه نمط فشل سابق — لا يظهر كدخول بحذر")
        if blockers and plan_status not in {"execution_zone", "waiting_trigger"}:
            return _set_common(out, WATCH, "مراقبة", blockers, plan_action or "👀 مراقبة — ليست دخولًا حذرًا صالحًا الآن.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        if any("نمط فشل" in b for b in blockers):
            return _set_common(out, WATCH, "مراقبة", blockers, "👀 مراقبة فقط — النمط الحالي لا يسمح بدخول بحذر قبل تأكيد أقوى.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        return _set_common(out, WAIT_TRIGGER, "دخول بحذر", _dedupe(blockers, 10), "🟠 دخول بحذر = شرط قريب وليس مطاردة. لا تدخل إلا إذا استمرت السيولة وبقي السعر قرب منطقة الدخول.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)

    if stage == "Early Confirmation":
        if bool(out.get("stage_allows_cautious")) and liquidity_ok and not resistance_blocked and plan_status in {"execution_zone", "waiting_trigger"}:
            return _set_common(out, WAIT_TRIGGER, "دخول بحذر", [], "🟠 تأكيد مبكر — انتظر اكتمال شرط الدخول والسيولة.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)
        return _set_common(out, EARLY_WATCH, "مراقبة", ["تأكيد مبكر يحتاج متابعة لصيقة قبل الترقية"], "🟣 تأكيد مبكر تحت المراقبة — ليست دخولًا الآن.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)

    return _set_common(out, WATCH, "مراقبة", [], plan_action or out.get("owner_action") or "👀 مراقبة — ليست دخولًا الآن.", liquidity_ok=liquidity_ok, liquidity_reasons=liquidity_reasons, entry_dist=entry_dist, stage=stage, stage_label=stage_label)


def apply_final_decisions(rows: list[dict]) -> list[dict]:
    return [apply_final_decision(x) if isinstance(x, dict) else x for x in (rows or [])]
