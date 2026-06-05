"""Pattern-to-Action Engine V1.

Converts the weekly-learning ideas into visible, actionable fields.  This is not
another passive report: final_decision_engine can use the risk/action fields to
block bad promotions, and the UI can show the winning/losing pattern reasons.
"""
from __future__ import annotations

from typing import Any

PATTERN_ACTION_VERSION = "pattern_to_action_v1_2026_06_05"


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
        return float(v)
    except Exception:
        return default


def _s(v: Any) -> str:
    return str(v or "").strip()


def _lst(v: Any) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x or "").strip()]
    return []


def _append_unique(dst: list[str], value: str) -> None:
    value = _s(value)
    if value and value not in dst:
        dst.append(value)


def evaluate_pattern_action(row: dict) -> dict:
    row = row or {}
    price = _f(row.get("current_price_live", row.get("display_price", 0)))
    open_price = _f(row.get("open_price_live", row.get("open", row.get("day_open", 0))))
    low = _f(row.get("low_live", row.get("day_low", row.get("session_low", 0))))
    high = _f(row.get("high_live", row.get("day_high", row.get("session_high", 0))))
    change = _f(row.get("display_change_pct", row.get("change_pct", 0)))
    change_open = _f(row.get("change_from_open_pct", 0))
    volume = max(_f(row.get("effective_volume_ratio", 0)), _f(row.get("volume_pace_ratio", 0)), _f(row.get("volume_ratio", 0)))
    liquidity_score = _f(row.get("liquidity_persistence_score", row.get("liquidity_score", 0)))
    close_strength = 0.0
    if price > 0 and high > low > 0:
        close_strength = (price - low) / (high - low)
    support_broken = bool(row.get("support_broken_flag")) or _s(row.get("post_activation_guard_status")) == "broken"
    close_resistance = bool(row.get("close_resistance_guard_flag"))
    plan_status = _s(row.get("plan_lifecycle_status"))
    risk_tags = " ".join(_lst(row.get("risk_tags")) + _lst(row.get("risk_flags")))

    winning: list[str] = []
    losing: list[str] = []
    winning_score = 0.0
    losing_score = 0.0

    if open_price > 0 and low > 0 and price > 0:
        dipped = low < open_price * 0.985
        reclaimed_open = price > open_price * 1.003
        if dipped and reclaimed_open and close_strength >= 0.62:
            winning_score += 28
            _append_unique(winning, "نزول أولي ثم استعادة الافتتاح")
    if volume >= 1.25 or liquidity_score >= 60:
        winning_score += 22
        _append_unique(winning, "تسارع سيولة/دولار فوليوم داعم")
    if close_strength >= 0.72 and change >= 1.5:
        winning_score += 18
        _append_unique(winning, "إغلاق/تمركز قريب من قمة اليوم")
    if 0 <= change <= 7 and plan_status in {"execution_zone", "waiting_trigger", "valid_watch"}:
        winning_score += 16
        _append_unique(winning, "الحركة لم تصبح مطاردة بعد")
    if _f(row.get("pre_move_score", row.get("pre_move_v2_score", 0))) >= 60:
        winning_score += 16
        _append_unique(winning, "نمط مراقبة مبكرة/تجميع هادئ")

    if support_broken or plan_status in {"broken_stop", "broken_support"}:
        losing_score += 34
        _append_unique(losing, "الخطة/الدعم مكسور")
    if "السيولة لم تستمر" in risk_tags or _s(row.get("liquidity_persistence_status")) in {"fade", "weak"}:
        losing_score += 24
        _append_unique(losing, "السيولة ضعفت أو لم تستمر")
    if close_resistance and change >= 0:
        losing_score += 20
        _append_unique(losing, "قرب مقاومة قد يمنع استمرار الحركة")
    if plan_status in {"pullback_required", "target_reached"}:
        losing_score += 18
        _append_unique(losing, "الحركة تحتاج إعادة تمركز لا دخول مباشر")
    if change < -3 and price > 0:
        losing_score += 18
        _append_unique(losing, "ضغط سعري سلبي يحتاج ارتداد")

    winning_score = max(0.0, min(100.0, round(winning_score, 1)))
    losing_score = max(0.0, min(100.0, round(losing_score, 1)))
    if losing_score >= 60:
        action = "demote_or_block"
        label = "🔴 يشبه نمط فشل — لا ترقية دون تأكيد قوي"
        priority = "risk"
    elif winning_score >= 60 and losing_score < 35:
        action = "monitor_closely"
        label = "🟢 يشبه نمط رابح — مراقبة لصيقة"
        priority = "positive"
    elif winning_score >= 40 and losing_score < 50:
        action = "watch_for_trigger"
        label = "🟡 نمط إيجابي يحتاج تفعيل"
        priority = "watch"
    else:
        action = "neutral"
        label = "⚪ لا يوجد نمط حاسم"
        priority = "neutral"

    return {
        "version": PATTERN_ACTION_VERSION,
        "winning_pattern_score": winning_score,
        "winning_pattern_reasons": winning[:8],
        "losing_pattern_score": losing_score,
        "losing_pattern_reasons": losing[:8],
        "pattern_action": action,
        "pattern_action_label": label,
        "pattern_action_priority": priority,
    }


def enrich_pattern_action(row: dict) -> dict:
    out = dict(row or {})
    out.update(evaluate_pattern_action(out))
    return out
