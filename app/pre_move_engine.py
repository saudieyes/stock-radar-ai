"""Pre-Move Engine for Source / Early Discovery V2.

This is a light, no-API helper. It scores whether a symbol/row looks like it is
building before a move rather than already chasing a large move.
"""
from __future__ import annotations

import os
from typing import Any

PRE_MOVE_ENGINE_VERSION = "pre_move_engine_official_prior_move_accumulation_2026_05_30"


def _env_bool(name: str, default: bool = True) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def pre_move_engine_enabled() -> bool:
    return _env_bool("PRE_MOVE_ENGINE_ENABLED", True) and _env_bool("SOURCE_EARLY_DISCOVERY_V2_ENABLED", True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
        return float(value)
    except Exception:
        return default


def _first(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row and row.get(key) not in {None, ""}:
            return _safe_float(row.get(key), default)
    return default


def analyze_pre_move(row: dict) -> dict[str, Any]:
    if not isinstance(row, dict) or not pre_move_engine_enabled():
        return {"version": PRE_MOVE_ENGINE_VERSION, "pre_move_watch_eligible": False, "pre_move_score": 0, "pre_move_reasons": [], "pre_move_invalid_reasons": ["engine disabled or invalid row"]}

    change = _first(row, ["display_change_pct", "change_vs_prev_close_pct", "change_pct", "day_change_pct", "live_change_pct", "fmp_change_pct"], 0.0)
    # Scanner grouped metrics sometimes express day_change_pct as percent already;
    # if tiny decimal is passed, keep it conservative rather than multiplying blindly.
    close_strength = _first(row, ["close_strength", "session_position_pct"], 0.0)
    range_pct = _first(row, ["range_pct", "daily_range_pct"], 0.0)
    dollar_volume = _first(row, ["dollar_volume", "live_dollar_volume", "fmp_dollar_volume"], 0.0)
    volume_ratio = _first(row, ["effective_volume_ratio", "volume_pace_ratio", "volume_ratio", "rvol"], 0.0)
    readiness = _first(row, ["execution_readiness_score"], 0.0)
    quality = _first(row, ["quality_score", "display_rank_score"], 0.0)
    res_dist = _first(row, ["nearest_resistance_distance_pct", "distance_to_resistance_pct"], 999.0)
    support_dist = _first(row, ["nearest_support_distance_pct", "support_distance_pct", "distance_to_support_pct"], 999.0)
    prior_day = _first(row, ["prior_day_change_pct", "previous_day_change_pct", "last_session_change_pct"], 0.0)
    rolling_3d = _first(row, ["rolling_3d_change_pct", "three_day_change_pct", "last_3d_change_pct"], 0.0)
    weekly = _first(row, ["weekly_change_pct", "week_change_pct", "five_day_change_pct"], 0.0)
    monthly = _first(row, ["monthly_change_pct", "month_change_pct", "twenty_day_change_pct"], 0.0)
    trend = str(row.get("trend", "") or "")
    source_text = " ".join([str(x) for x in (row.get("source_reason_tags") or row.get("sources") or [])]) + " " + str(row.get("source_reason", "") or "")

    score = 0.0
    reasons: list[str] = []
    invalid: list[str] = []

    if prior_day >= 12 or rolling_3d >= 18 or weekly >= 20 or monthly >= 35:
        invalid.append("ارتفع كثيرًا سابقًا — ليس Pre-Move نظيف")

    if change > 8.0:
        invalid.append(f"الحركة الحالية كبيرة لمراقبة مبكرة ({round(change, 2)}%)")
    elif -2.5 <= change <= 5.0:
        score += 18
        reasons.append("لم يتحرك كثيرًا بعد")
    elif 5.0 < change <= 8.0:
        score += 6
        reasons.append("بدأ يتحرك لكن لا يزال يحتاج تصنيف تأكيد مبكر")

    if close_strength >= 0.62 or close_strength >= 62:
        score += 14
        reasons.append("إغلاق/تمركز يتحسن")
    if 0.012 <= range_pct <= 0.14 or 1.2 <= range_pct <= 14:
        score += 8
        reasons.append("نطاق حركة بنّاء غير انفجاري")
    if volume_ratio >= 1.05:
        score += min(16, volume_ratio * 7)
        reasons.append("سيولة تتحسن تدريجيًا")
    elif dollar_volume >= 20_000_000:
        score += 10
        reasons.append("دولار فوليوم جيد للمراقبة")
    if trend in {"صاعد", "صاعد قوي"}:
        score += 10
        reasons.append("اتجاه داعم")
    if 0 <= support_dist <= 2.5:
        score += 12
        reasons.append("قريب من دعم/منطقة شراء يمكن ضبط وقفها")
    if 1.0 <= res_dist <= 7.0:
        score += 10
        reasons.append("قريب من منطقة اختراق بدون ملاصقة مقاومة")
    elif 0 <= res_dist < 0.8:
        invalid.append("ملاصق لمقاومة قريبة")
    if readiness >= 45:
        score += 8
        reasons.append("جاهزية أولية مقبولة")
    if quality >= 60:
        score += 8
        reasons.append("جودة فنية مقبولة")
    if any(w in source_text.lower() for w in ["quiet accumulation", "support buy", "pre-gap", "pre_move", "constructive", "near_high", "weekly_priority", "breakout"]):
        score += 12
        reasons.append("مصدر المنبع يشير إلى بناء/تجميع أو اختراق قريب")

    eligible = bool(score >= 42 and not invalid)
    label = "🟣 مراقبة مبكرة قبل الحركة" if eligible else "غير مؤهل لمراقبة مبكرة نظيفة"
    return {
        "version": PRE_MOVE_ENGINE_VERSION,
        "pre_move_score": round(max(0, min(100, score)), 2),
        "pre_move_label": label,
        "pre_move_watch_eligible": eligible,
        "pre_move_reasons": reasons[:8],
        "pre_move_invalid_reasons": invalid[:8],
    }


def enrich_row_pre_move(row: dict) -> dict:
    if not isinstance(row, dict):
        return row
    meta = analyze_pre_move(row)
    row["pre_move_v2"] = meta
    row["pre_move_score"] = meta.get("pre_move_score", 0)
    row["pre_move_label"] = meta.get("pre_move_label", "")
    row["pre_move_watch_eligible"] = bool(meta.get("pre_move_watch_eligible", False))
    row["pre_move_reasons"] = meta.get("pre_move_reasons", [])
    row["pre_move_invalid_reasons"] = meta.get("pre_move_invalid_reasons", [])
    return row
