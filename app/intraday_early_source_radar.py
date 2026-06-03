"""Intraday Early Source Radar V1 for Stock Radar AI.

This module is a clean source-layer addition.  It does NOT decide entries,
does NOT send alerts, does NOT edit Sharia status, and does NOT store heavy raw
market data.  Its only job is to create an earlier, explainable candidate lane
for stocks that are beginning to move before they become late/no-chase names.

Design constraints:
- Uses the already-fetched Polygon grouped snapshot and optional current FMP
  confirmation later in source_discovery; no new full-market minute download.
- Keeps high-risk/non-clean tickers separate from the clean source universe.
- Saves compact diagnostics only in memory.
- Final BUY/WAIT/NO-CHASE remains controlled by final_decision_engine.
"""
from __future__ import annotations

import os
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
from typing import Any

NY_TZ = ZoneInfo("America/New_York")

_LAST_INTRADAY_EARLY_SOURCE_RADAR_STATUS: dict[str, Any] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except Exception:
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


def _env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(float(os.getenv(name, str(default)) or default))
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


INTRADAY_EARLY_SOURCE_RADAR_ENABLED = _env_bool("INTRADAY_EARLY_SOURCE_RADAR_ENABLED", True)
INTRADAY_EARLY_SOURCE_RADAR_CLEAN_LIMIT = _env_int("INTRADAY_EARLY_SOURCE_RADAR_CLEAN_LIMIT", 180, 40, 320)
INTRADAY_EARLY_SOURCE_RADAR_HIGH_RISK_LIMIT = _env_int("INTRADAY_EARLY_SOURCE_RADAR_HIGH_RISK_LIMIT", 35, 5, 80)
INTRADAY_EARLY_SOURCE_RADAR_MIN_PRICE = _env_float("INTRADAY_EARLY_SOURCE_RADAR_MIN_PRICE", 1.0, 0.2, 50.0)
INTRADAY_EARLY_SOURCE_RADAR_MIN_DOLLAR_PACE = _env_float("INTRADAY_EARLY_SOURCE_RADAR_MIN_DOLLAR_PACE", 3_000_000.0, 100_000.0, 200_000_000.0)
INTRADAY_EARLY_SOURCE_RADAR_HIGH_RISK_MIN_DOLLAR_PACE = _env_float("INTRADAY_EARLY_SOURCE_RADAR_HIGH_RISK_MIN_DOLLAR_PACE", 4_500_000.0, 100_000.0, 200_000_000.0)
INTRADAY_EARLY_SOURCE_RADAR_MAX_FRESH_CHANGE = _env_float("INTRADAY_EARLY_SOURCE_RADAR_MAX_FRESH_CHANGE", 9.5, 4.0, 25.0)
INTRADAY_EARLY_SOURCE_RADAR_NO_CHASE_CHANGE = _env_float("INTRADAY_EARLY_SOURCE_RADAR_NO_CHASE_CHANGE", 15.0, 8.0, 50.0)
INTRADAY_EARLY_SOURCE_RADAR_ALLOW_PREVIOUS_GROUPED = _env_bool("INTRADAY_EARLY_SOURCE_RADAR_ALLOW_PREVIOUS_GROUPED", False)


_NON_CLEAN_SUFFIXES = ("U", "W", "WS", "WT", "R")


def intraday_early_source_radar_enabled() -> bool:
    return bool(INTRADAY_EARLY_SOURCE_RADAR_ENABLED)


def get_last_intraday_early_source_radar_status() -> dict:
    return dict(_LAST_INTRADAY_EARLY_SOURCE_RADAR_STATUS or {})


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return float(default)


def _safe_round(value: Any, digits: int = 2) -> float:
    try:
        return round(float(value or 0), digits)
    except Exception:
        return 0.0


def _clean_symbol(symbol: Any) -> str:
    try:
        s = str(symbol or "").upper().strip()
        if not s:
            return ""
        if not all(ch.isalnum() or ch in {".", "-"} for ch in s):
            return ""
        return s
    except Exception:
        return ""


def _market_phase(now: datetime | None = None) -> str:
    now = now or datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    if dt_time(4, 0) <= t < dt_time(9, 30):
        return "pre_market"
    if dt_time(9, 30) <= t <= dt_time(16, 0):
        return "regular"
    if dt_time(16, 0) < t <= dt_time(20, 0):
        return "after_hours"
    return "closed"


def _session_elapsed_ratio(now: datetime | None = None) -> float:
    """Return an approximate elapsed ratio for volume pace.

    We do not need exact minute candles here.  The purpose is to avoid rejecting
    early movers simply because a full-day volume threshold is not yet complete.
    """
    now = now or datetime.now(NY_TZ)
    phase = _market_phase(now)
    minutes = now.hour * 60 + now.minute + (now.second / 60.0)
    if phase == "pre_market":
        start = 4 * 60
        end = 9 * 60 + 30
        return max(0.08, min(0.45, (minutes - start) / max(1, end - start) * 0.45))
    if phase == "regular":
        start = 9 * 60 + 30
        end = 16 * 60
        return max(0.06, min(1.0, (minutes - start) / max(1, end - start)))
    if phase == "after_hours":
        # After-hours grouped volume can include the full regular session.  Keep
        # pace neutral instead of inflating it.
        return 1.0
    return 1.0


def _symbol_risk_profile(symbol: str, is_clean_reference: bool) -> dict:
    s = _clean_symbol(symbol)
    high_risk = False
    reasons: list[str] = []
    if not is_clean_reference:
        high_risk = True
        reasons.append("ليس ضمن مرجع الأسهم النظيفة")
    if s.endswith(_NON_CLEAN_SUFFIXES):
        high_risk = True
        reasons.append("رمز عالي المخاطر/Unit/Warrant/Right")
    return {"high_risk": high_risk, "reasons": reasons}


def _metrics_from_grouped(daily: dict, elapsed_ratio: float) -> dict:
    price = _safe_float((daily or {}).get("price"))
    open_price = _safe_float((daily or {}).get("open"))
    high = _safe_float((daily or {}).get("high"))
    low = _safe_float((daily or {}).get("low"))
    volume = _safe_float((daily or {}).get("volume"))
    dollar_volume = price * volume if price > 0 and volume > 0 else 0.0
    day_range = max(high - low, 0.0001)
    change_pct = ((price - open_price) / open_price) * 100.0 if open_price > 0 else 0.0
    range_pct = (day_range / price) * 100.0 if price > 0 else 0.0
    close_strength = (price - low) / day_range if day_range > 0 else 0.0
    body_strength = abs(price - open_price) / day_range if day_range > 0 else 0.0
    dip_depth_pct = ((open_price - low) / open_price) * 100.0 if open_price > 0 and low > 0 and low < open_price else 0.0
    reclaim_from_low_pct = ((price - low) / low) * 100.0 if low > 0 and price > low else 0.0
    high_distance_pct = ((high - price) / price) * 100.0 if price > 0 and high >= price else 0.0
    dollar_volume_pace = dollar_volume / max(0.05, elapsed_ratio)
    volume_pace = volume / max(0.05, elapsed_ratio)
    return {
        "price": price,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
        "dollar_volume": dollar_volume,
        "dollar_volume_pace": dollar_volume_pace,
        "volume_pace": volume_pace,
        "elapsed_ratio": elapsed_ratio,
        "change_pct": change_pct,
        "range_pct": range_pct,
        "close_strength": close_strength,
        "body_strength": body_strength,
        "dip_depth_pct": dip_depth_pct,
        "reclaim_from_low_pct": reclaim_from_low_pct,
        "near_high": bool(price > 0 and high > 0 and price >= high * 0.985),
        "high_distance_pct": high_distance_pct,
        "above_open": bool(price > 0 and open_price > 0 and price >= open_price),
        "reclaimed_open": bool(open_price > 0 and low < open_price * 0.985 and price >= open_price * 1.002),
    }


def classify_grouped_candidate(symbol: str, daily: dict, *, is_clean_reference: bool, elapsed_ratio: float, source_mode: str = "") -> dict | None:
    """Classify one symbol as an early-source candidate or return None.

    This intentionally uses conservative lanes.  It should broaden source
    discovery, not create buy signals.
    """
    s = _clean_symbol(symbol)
    if not s:
        return None
    m = _metrics_from_grouped(daily or {}, elapsed_ratio)
    price = float(m.get("price", 0) or 0)
    if price < INTRADAY_EARLY_SOURCE_RADAR_MIN_PRICE:
        return None

    risk = _symbol_risk_profile(s, is_clean_reference)
    high_risk = bool(risk.get("high_risk", False))
    chg = float(m.get("change_pct", 0) or 0)
    range_pct = float(m.get("range_pct", 0) or 0)
    close_strength = float(m.get("close_strength", 0) or 0)
    dollar_pace = float(m.get("dollar_volume_pace", 0) or 0)
    dollar_volume = float(m.get("dollar_volume", 0) or 0)
    dip_depth = float(m.get("dip_depth_pct", 0) or 0)
    reclaim = float(m.get("reclaim_from_low_pct", 0) or 0)
    near_high = bool(m.get("near_high", False))
    above_open = bool(m.get("above_open", False))
    reclaimed_open = bool(m.get("reclaimed_open", False))

    min_dollar_pace = INTRADAY_EARLY_SOURCE_RADAR_HIGH_RISK_MIN_DOLLAR_PACE if high_risk else INTRADAY_EARLY_SOURCE_RADAR_MIN_DOLLAR_PACE
    if dollar_pace < min_dollar_pace and dollar_volume < min_dollar_pace * 0.45:
        return None
    if range_pct <= 0.35 or range_pct > 38.0:
        return None
    if chg < -4.0:
        return None

    lane = ""
    lane_label = ""
    reasons: list[str] = []
    blockers: list[str] = []
    score = 0.0

    # True early ramp: moving but not yet too extended, near the high, and
    # volume/dollar pace is sufficient for the time of day.
    fresh_max = INTRADAY_EARLY_SOURCE_RADAR_MAX_FRESH_CHANGE
    if 1.0 <= chg <= fresh_max and close_strength >= 0.62 and (near_high or reclaimed_open or close_strength >= 0.78):
        lane = "intraday_early_ramp"
        lane_label = "رادار صعود مبكر"
        score = 44.0
        reasons.append("صعود تدريجي قبل المطاردة")
        if near_high:
            reasons.append("قريب من قمة اليوم")
        if dollar_pace >= min_dollar_pace * 2.0:
            reasons.append("تسارع سيولة قوي حسب وقت الجلسة")
        else:
            reasons.append("سيولة مقبولة حسب وقت الجلسة")

    # Dip then reclaim: starts weak, recovers open, and holds high in range.
    if not lane and reclaimed_open and 0.2 <= chg <= 10.5 and close_strength >= 0.66 and reclaim >= 2.5:
        lane = "dip_reclaim_radar"
        lane_label = "استعادة بعد نزول"
        score = 42.0
        reasons.extend(["بدأ بنزول ثم استعاد الافتتاح", "ارتداد واضح من القاع", "إغلاق/سعر قريب من أعلى النطاق"])

    # Quiet accumulation / pre-move: not yet moving much but pressure is building.
    if not lane and -1.0 <= chg <= 3.8 and close_strength >= 0.68 and 0.8 <= range_pct <= 9.0 and dollar_pace >= min_dollar_pace * 1.35:
        lane = "quiet_accumulation_radar"
        lane_label = "تجميع هادئ داخل اليوم"
        score = 34.0
        reasons.extend(["حركة هادئة مع تمركز قرب أعلى النطاق", "سيولة تتراكم دون انفجار سعري"])

    # Late review: keep visible for diagnostics/high-risk monitoring only, not as early.
    if not lane and chg >= INTRADAY_EARLY_SOURCE_RADAR_NO_CHASE_CHANGE and close_strength >= 0.55 and dollar_pace >= min_dollar_pace:
        lane = "late_intraday_mover_review"
        lane_label = "متحرك متأخر للمراجعة"
        score = 12.0
        reasons.append("ظهر بعد حركة كبيرة — للمراقبة/لا تطارد")
        blockers.append("متأخر وليس اكتشافًا مبكرًا")

    if not lane:
        return None

    if high_risk:
        # High-risk symbols may be useful for awareness but must never crowd out
        # clean opportunities or be treated as direct entries.
        if lane == "late_intraday_mover_review":
            score += 2.0
        else:
            score -= 8.0
        lane = "high_risk_live_mover" if lane != "late_intraday_mover_review" else "high_risk_late_mover_review"
        lane_label = "مراقبة عالية المخاطر"
        reasons = list(risk.get("reasons") or []) + reasons
        blockers.append("مسار منفصل عالي المخاطر وليس دخولًا مباشرًا")

    # Quality refinements.
    score += min(max(chg, 0), 8.0) * 1.4
    score += min(dollar_pace / 25_000_000.0, 12.0)
    score += min(close_strength * 10.0, 9.0)
    if near_high:
        score += 6.0
    if above_open:
        score += 3.0
    if 1.0 <= dip_depth <= 8.0 and reclaim >= 2.0:
        score += 5.0
    if chg > fresh_max and lane not in {"late_intraday_mover_review", "high_risk_late_mover_review"}:
        blockers.append("قريب من التحول إلى مطاردة")
        score -= 7.0
    if range_pct > 22.0 and high_risk:
        blockers.append("تذبذب عالٍ جدًا")
        score -= 6.0

    return {
        "symbol": s,
        "lane": lane,
        "lane_label": lane_label,
        "score": _safe_round(score, 3),
        "high_risk": high_risk,
        "source_mode": source_mode,
        "reasons": reasons[:8],
        "blockers": blockers[:8],
        "metrics": {
            "price": _safe_round(price, 4),
            "open": _safe_round(m.get("open"), 4),
            "high": _safe_round(m.get("high"), 4),
            "low": _safe_round(m.get("low"), 4),
            "change_pct": _safe_round(chg, 3),
            "range_pct": _safe_round(range_pct, 3),
            "close_strength": _safe_round(close_strength, 3),
            "dollar_volume": _safe_round(dollar_volume, 2),
            "dollar_volume_pace": _safe_round(dollar_pace, 2),
            "volume": _safe_round(m.get("volume"), 2),
            "elapsed_ratio": _safe_round(m.get("elapsed_ratio"), 3),
            "dip_depth_pct": _safe_round(dip_depth, 3),
            "reclaim_from_low_pct": _safe_round(reclaim, 3),
            "near_high": near_high,
            "reclaimed_open": reclaimed_open,
            "above_open": above_open,
        },
    }


def scan_intraday_early_source_radar(
    grouped_map: dict | None,
    reference_tickers: list[str] | set[str] | None = None,
    *,
    source_mode: str = "",
    clean_limit: int | None = None,
    high_risk_limit: int | None = None,
) -> dict:
    """Scan the current grouped market map for earlier source candidates."""
    global _LAST_INTRADAY_EARLY_SOURCE_RADAR_STATUS
    if not INTRADAY_EARLY_SOURCE_RADAR_ENABLED:
        result = {"ok": True, "enabled": False, "version": "intraday_early_source_radar_v1_clean_source_layer", "candidates": []}
        _LAST_INTRADAY_EARLY_SOURCE_RADAR_STATUS = result
        return result

    now = datetime.now(NY_TZ)
    elapsed_ratio = _session_elapsed_ratio(now)
    normalized_source_mode = str(source_mode or "").strip()
    if normalized_source_mode and normalized_source_mode != "today_grouped" and not INTRADAY_EARLY_SOURCE_RADAR_ALLOW_PREVIOUS_GROUPED:
        result = {
            "ok": True,
            "enabled": True,
            "version": "intraday_early_source_radar_v1_clean_source_layer",
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "market_phase": _market_phase(now),
            "source_mode": normalized_source_mode,
            "skipped": True,
            "skip_reason": "source_mode_not_today_grouped",
            "returned_count": 0,
            "candidates": [],
            "notes_ar": "تم تخطي رادار الحركة المبكرة لأن بيانات Polygon ليست بيانات اليوم. هذا يمنع إشارات قديمة من دخول المنبع.",
        }
        _LAST_INTRADAY_EARLY_SOURCE_RADAR_STATUS = dict(result)
        return result
    ref_set = {str(x or "").upper().strip() for x in (reference_tickers or []) if str(x or "").strip()}
    clean_limit = max(10, min(int(clean_limit or INTRADAY_EARLY_SOURCE_RADAR_CLEAN_LIMIT), 400))
    high_risk_limit = max(0, min(int(high_risk_limit or INTRADAY_EARLY_SOURCE_RADAR_HIGH_RISK_LIMIT), 120))

    clean: list[dict] = []
    high_risk: list[dict] = []
    late_review: list[dict] = []
    scanned = 0
    for sym, daily in (grouped_map or {}).items():
        s = _clean_symbol(sym)
        if not s:
            continue
        scanned += 1
        is_clean = (s in ref_set) if ref_set else True
        row = classify_grouped_candidate(s, daily or {}, is_clean_reference=is_clean, elapsed_ratio=elapsed_ratio, source_mode=source_mode)
        if not row:
            continue
        lane = str(row.get("lane") or "")
        if lane in {"high_risk_live_mover", "high_risk_late_mover_review"}:
            high_risk.append(row)
        elif lane == "late_intraday_mover_review":
            late_review.append(row)
        else:
            clean.append(row)

    clean.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    high_risk.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    late_review.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)

    candidates = clean[:clean_limit] + high_risk[:high_risk_limit] + late_review[:25]
    lane_counts: dict[str, int] = {}
    for row in candidates:
        lane = str(row.get("lane") or "unknown")
        lane_counts[lane] = int(lane_counts.get(lane, 0) or 0) + 1

    result = {
        "ok": True,
        "enabled": True,
        "version": "intraday_early_source_radar_v1_clean_source_layer",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "market_phase": _market_phase(now),
        "source_mode": source_mode,
        "elapsed_ratio": _safe_round(elapsed_ratio, 3),
        "scanned_count": scanned,
        "clean_candidate_count": len(clean),
        "high_risk_candidate_count": len(high_risk),
        "late_review_count": len(late_review),
        "returned_count": len(candidates),
        "lane_counts": lane_counts,
        "top_clean": clean[:20],
        "top_high_risk": high_risk[:15],
        "top_late_review": late_review[:15],
        "candidates": candidates,
        "notes_ar": "رادار مصدر مبكر فقط: يضيف مرشحين للمنبع ولا يعطي شراء ولا يرسل Telegram ولا يغير قرار الدخول النهائي.",
    }
    _LAST_INTRADAY_EARLY_SOURCE_RADAR_STATUS = dict(result)
    return result
