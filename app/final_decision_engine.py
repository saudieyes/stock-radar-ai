"""Final decision engine for Stock Radar AI official launch.

This layer is intentionally the last visible decision gate.  Upstream modules may
score, stage, promote, or warn, but the UI and alerts should trust only the
fields produced here.
"""
from __future__ import annotations

from typing import Any

FINAL_DECISION_ENGINE_VERSION = "official_final_decision_engine_v1_2026_05_30a"

BUY_NOW = "BUY_NOW"
WAIT_TRIGGER = "WAIT_TRIGGER"
WAIT_LIQUIDITY = "WAIT_LIQUIDITY"
WAIT_RESISTANCE = "WAIT_RESISTANCE"
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
                value = value.replace("%", "").replace(",", "").strip()
            return float(value)
        except Exception:
            continue
    return float(default)


def _first_existing(row: dict, keys: list[str], default: Any = None) -> Any:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    return default


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
    return _f(row, [
        "current_price_live", "display_price", "price", "current_price", "fmp_price", "live_price", "last_price"
    ], 0.0)


def _entry(row: dict) -> float:
    return _f(row, ["display_entry_price", "entry_price", "entry", "buy_above"], 0.0)


def _entry_distance_pct(row: dict) -> float:
    price = _price(row)
    entry = _entry(row)
    if price > 0 and entry > 0:
        return ((price - entry) / entry) * 100.0
    # Some rows do not have an explicit entry; do not fail them only for missing data.
    return 0.0


def _liquidity_confirmed(row: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    volume = _f(row, ["effective_volume_ratio", "volume_pace_ratio", "volume_ratio", "relative_volume", "rvol"], 0.0)
    liq_score = _f(row, ["liquidity_persistence_score", "liquidity_score"], 0.0)
    dollar_volume = _f(row, ["dollar_volume", "live_dollar_volume", "fmp_dollar_volume"], 0.0)
    success_tags = " ".join(str(x) for x in (row.get("success_tags") or []))
    risk_tags = " ".join(str(x) for x in (row.get("risk_tags") or []))
    action_text = " ".join([
        _txt(row.get("owner_action")),
        _txt(row.get("execution_readiness_label")),
        _txt(row.get("execution_gate_label")),
        _txt(row.get("live_plan_action")),
    ])

    if "السيولة لم تستمر" in risk_tags or "السيولة ضعيفة" in action_text or "ضعيفة" in action_text and "السيولة" in action_text:
        reasons.append("السيولة الحالية غير مؤكدة أو ضعفت")
    if volume >= 1.05:
        reasons.append(f"RVOL/volume={round(volume, 2)}")
    if liq_score >= 55:
        reasons.append(f"liquidity_score={round(liq_score, 1)}")
    if dollar_volume >= 20_000_000:
        reasons.append("دولار فوليوم كافٍ")
    if "السيولة استمرت" in success_tags:
        reasons.append("السيولة استمرت")

    confirmed = (
        (volume >= 1.05 or liq_score >= 55 or dollar_volume >= 20_000_000 or "السيولة استمرت" in success_tags)
        and not ("السيولة الحالية غير مؤكدة أو ضعفت" in reasons)
    )
    return confirmed, _dedupe(reasons, 6)


def _resistance_blocked(row: dict) -> tuple[bool, str]:
    res_dist = _f(row, ["nearest_resistance_distance_pct", "resistance_distance_pct", "distance_to_resistance_pct"], 999.0)
    risk_tags = " ".join(str(x) for x in (row.get("risk_tags") or []))
    labels = " ".join([
        _txt(row.get("resistance_guard_label")),
        _txt(row.get("support_guard_label")),
        _txt(row.get("execution_gate_label")),
    ])
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


def apply_final_decision(row: dict) -> dict:
    """Apply one final, non-contradictory decision to a stock row."""
    if not isinstance(row, dict):
        return row

    out = dict(row)
    original_decision = _txt(out.get("decision")) or "مراقبة"
    stage = _stage(out)
    stage_label = _stage_label(out)
    gain_at_detection = _f(out, ["gain_at_detection"], 0.0)
    current_gain = _f(out, ["current_gain", "display_change_pct", "live_change_pct", "change_pct"], 0.0)
    peak_gain = max(
        _f(out, ["peak_gain_seen"], 0.0),
        _f(out, ["intraday_peak_gain"], 0.0),
        _f(out, ["max_gain_basis"], 0.0),
        current_gain,
        gain_at_detection,
    )
    readiness = _f(out, ["execution_readiness_score", "readiness_score"], 0.0)
    rr = _f(out, ["rr_1", "risk_reward", "reward_risk"], 0.0)
    price_reliable = bool(out.get("price_reliable_for_execution", True))
    entry_dist = _entry_distance_pct(out)
    liquidity_ok, liquidity_reasons = _liquidity_confirmed(out)
    resistance_blocked, resistance_reason = _resistance_blocked(out)
    prior_move_risk, prior_move_reasons = _previous_move_flags(out)

    non_actionable_stages = {
        "Pre-Move", "Continuation Watch", "Requires Pullback", "Already Moved", "Extended", "No-Chase", "Catalyst Spike Review"
    }
    # IMPORTANT: do not infer NO_CHASE from owner_action/execution labels.
    # Those labels may already be stale from an upstream/previous decision pass and
    # caused clean Pre-Move rows to become "لا تطارد" even when current/peak gain
    # was tiny.  Only explicit no-chase guards, hard late stages, or objectively
    # extended movement are allowed to cap the final decision.
    explicit_no_chase_status = _txt(out.get("no_chase_guard_status")).lower() == "no_chase"
    explicit_no_chase_label = _has_text(out, ["no_chase_guard_label"], "لا تطارد", "مطاردة")
    hard_no_chase = (
        stage in {"Extended", "No-Chase", "Catalyst Spike Review"}
        or explicit_no_chase_status
        or explicit_no_chase_label
        or gain_at_detection >= 20
        or current_gain >= 25
        or peak_gain >= 25
    )

    code = WATCH
    final_decision = original_decision
    blockers: list[str] = []
    action = ""

    if hard_no_chase:
        code = NO_CHASE
        final_decision = "مراقبة"
        blockers = ["الحركة متأخرة أو ممتدة — لا تطارد"]
        action = "⛔ لا تطارد الآن — انتظر pullback صحي أو إعادة تمركز قبل أي دخول."
    elif stage == "Requires Pullback" or (peak_gain >= 10 and stage not in {"Active Breakout", "Early Confirmation"}):
        code = PULLBACK_REQUIRED
        final_decision = "مراقبة"
        blockers = ["يحتاج Pullback أو إعادة تمركز قبل الدخول"]
        action = "⏳ يحتاج Pullback — لا تدخل حتى يعود قرب دعم/entry أو يحدث reclaim بسيولة."
    elif stage == "Continuation Watch" or (gain_at_detection >= 10 and not bool(out.get("stage_allows_strong"))):
        code = CONTINUATION
        final_decision = "مراقبة"
        blockers = ["استمرار مشروط وليس دخولًا مباشرًا"]
        action = "🔵 استمرار مشروط — انتظر ثبات/ pullback / reclaim بسيولة."
    elif stage == "Pre-Move":
        code = EARLY_WATCH
        final_decision = "مراقبة"
        blockers = ["مراقبة مبكرة قبل الحركة وليست دخولًا الآن"]
        action = "🟣 مراقبة مبكرة — تابع فقط حتى يظهر تأكيد حي."
    elif original_decision == "دخول قوي":
        blockers = []
        if not price_reliable:
            blockers.append("السعر غير موثوق للتنفيذ")
        if not liquidity_ok:
            blockers.append("السيولة غير مؤكدة لدخول قوي")
        if resistance_blocked:
            blockers.append(resistance_reason)
        if readiness and readiness < 58:
            blockers.append("جاهزية التنفيذ أقل من مستوى دخول قوي")
        if rr and rr < 0.65:
            blockers.append("العائد/المخاطرة غير كافٍ لدخول قوي")
        if entry_dist < -0.75:
            blockers.append("السعر لم يتفعل بعد فوق منطقة الدخول")
        if entry_dist > 2.0:
            blockers.append("السعر ابتعد عن منطقة الدخول")
        if prior_move_risk and not bool(out.get("clean_continuation_confirmed")):
            blockers += prior_move_reasons[:2]
        if stage in non_actionable_stages and not bool(out.get("stage_allows_strong")):
            blockers.append("مرحلة الحركة الحالية لا تسمح بشراء الآن")

        blockers = _dedupe(blockers, 10)
        if blockers:
            if any("السيولة" in b for b in blockers):
                code = WAIT_LIQUIDITY
                action = "🟠 انتظر تأكيد السيولة — لا تدخل حتى تثبت السيولة الحية ويظل السعر قريبًا من الدخول."
            elif any("مقاومة" in b for b in blockers):
                code = WAIT_RESISTANCE
                action = "🟠 انتظر اختراق/ثبات فوق المقاومة قبل أي دخول."
            else:
                code = WAIT_TRIGGER
                action = "🟠 انتظر اكتمال شرط التنفيذ قبل الدخول."
            final_decision = "دخول بحذر" if stage in {"Active Breakout", "Early Confirmation"} or original_decision == "دخول قوي" else "مراقبة"
        else:
            code = BUY_NOW
            final_decision = "دخول قوي"
            action = "🟢 دخول قوي مؤكد — قابل للشراء الآن إذا بقي السعر داخل منطقة الدخول ولم يتجاوز عدم المطاردة."
    elif original_decision == "دخول بحذر":
        code = WAIT_TRIGGER
        final_decision = "دخول بحذر"
        blockers = []
        if resistance_blocked:
            blockers.append(resistance_reason)
        if not liquidity_ok:
            blockers.append("يحتاج استمرار السيولة قبل الدخول")
        if entry_dist < -1.0:
            blockers.append("لم يتفعل السعر بعد")
        if entry_dist > 2.5:
            blockers.append("ابتعد عن منطقة الدخول")
        action = "🟠 دخول بحذر = انتظر التفعيل. لا تدخل إلا إذا استمرت السيولة وبقي السعر قرب entry."
    else:
        if stage == "Early Confirmation":
            code = WAIT_TRIGGER
            final_decision = "دخول بحذر" if bool(out.get("stage_allows_cautious")) and liquidity_ok and not resistance_blocked else "مراقبة"
            action = "🟠 تأكيد مبكر — انتظر اكتمال شرط الدخول والسيولة."
        else:
            code = WATCH
            final_decision = "مراقبة"
            action = out.get("owner_action") or "👀 مراقبة — ليست دخولًا الآن."

    out["decision_before_final_engine"] = original_decision
    out["decision"] = final_decision
    out["effective_decision"] = final_decision
    out["final_decision_engine_version"] = FINAL_DECISION_ENGINE_VERSION
    out["final_decision_code"] = code
    out["final_decision_label"] = {
        BUY_NOW: "دخول قوي مؤكد",
        WAIT_TRIGGER: "انتظار تفعيل",
        WAIT_LIQUIDITY: "انتظار السيولة",
        WAIT_RESISTANCE: "انتظار اختراق مقاومة",
        EARLY_WATCH: "مراقبة مبكرة",
        CONTINUATION: "استمرار مشروط",
        PULLBACK_REQUIRED: "يحتاج Pullback",
        NO_CHASE: "لا تطارد",
        WATCH: "مراقبة",
    }.get(code, "مراقبة")
    out["final_decision_blockers"] = _dedupe(blockers, 10)
    out["final_decision_liquidity_ok"] = bool(liquidity_ok)
    out["final_decision_liquidity_reasons"] = liquidity_reasons
    out["final_decision_entry_distance_pct"] = round(entry_dist, 3)
    out["final_decision_stage"] = stage
    out["final_decision_stage_label"] = stage_label
    out["owner_action"] = action
    if code == BUY_NOW:
        out["execution_readiness_label"] = "جاهز للتنفيذ"
        out["execution_readiness_icon"] = "🟢"
        out["execution_status_ar"] = "دخول قوي مؤكد 🟢"
    elif code == NO_CHASE:
        out["execution_readiness_label"] = "لا تطارد"
        out["execution_readiness_icon"] = "⛔"
        out["execution_status_ar"] = "لا تطارد ⛔"
    elif code in {WAIT_LIQUIDITY, WAIT_RESISTANCE, WAIT_TRIGGER}:
        out["execution_readiness_label"] = "انتظار تأكيد"
        out["execution_readiness_icon"] = "🟠"
        out["execution_status_ar"] = "انتظار تأكيد 🟠"
    return out


def apply_final_decisions(rows: list[dict]) -> list[dict]:
    return [apply_final_decision(x) if isinstance(x, dict) else x for x in (rows or [])]
