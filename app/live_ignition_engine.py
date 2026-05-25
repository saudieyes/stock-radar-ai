"""Live Ignition / Hot Lane helper for Source / Early Discovery V2.

The module classifies lightweight quote/mover rows. It does not fetch anything;
source_discovery decides what rows to pass in.
"""
from __future__ import annotations

import os
from typing import Any

LIVE_IGNITION_VERSION = "source_early_discovery_v2_live_ignition_hot_lane_2026_05_25"


def _env_bool(name: str, default: bool = True) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def live_ignition_enabled() -> bool:
    return _env_bool("LIVE_IGNITION_HOT_LANE_ENABLED", True) and _env_bool("SOURCE_EARLY_DISCOVERY_V2_ENABLED", True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
        return float(value)
    except Exception:
        return default


def _get(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        if key in row and row.get(key) not in {None, ""}:
            return _safe_float(row.get(key), default)
    return default


def classify_live_ignition(symbol: str, row: dict) -> dict[str, Any]:
    if not live_ignition_enabled() or not symbol:
        return {"version": LIVE_IGNITION_VERSION, "symbol": symbol, "hot_lane_eligible": False, "stage_hint": "disabled"}
    row = row or {}
    price = _get(row, ["price", "last", "close", "live_price", "fmp_price"], 0.0)
    change_pct = _get(row, ["change_pct", "changesPercentage", "live_change_pct", "fmp_change_pct", "changePercentage"], 0.0)
    volume = _get(row, ["volume", "dayVolume", "live_volume", "fmp_volume"], 0.0)
    dollar_volume = price * volume if price > 0 and volume > 0 else _get(row, ["dollar_volume", "live_dollar_volume"], 0.0)

    reasons: list[str] = []
    blockers: list[str] = []
    score = 0.0
    stage_hint = "not_interesting"

    if price <= 0:
        blockers.append("لا يوجد سعر")
    elif price < 1.5:
        blockers.append("سعر منخفض جدًا للفحص السريع")

    if 1.8 <= change_pct <= 5.0:
        score += 36
        stage_hint = "early_confirmation"
        reasons.append("حركة مبكرة 2–5%")
    elif 5.0 < change_pct <= 9.5:
        score += 24
        stage_hint = "active_confirmation"
        reasons.append("حركة نشطة قبل حد التأخر")
    elif change_pct > 9.5:
        blockers.append(f"الحركة متأخرة للفحص المبكر ({round(change_pct, 2)}%)")
        stage_hint = "late_mover_review"
    elif -1.0 <= change_pct < 1.8:
        score += 8
        stage_hint = "pre_move_quote_watch"
        reasons.append("هادئ لكنه قابل للمراقبة إذا ظهرت سيولة")

    if volume >= 1_000_000:
        score += 12
        reasons.append("حجم تداول ملحوظ")
    if dollar_volume >= 15_000_000:
        score += 14
        reasons.append("دولار فوليوم داعم")
    if dollar_volume >= 50_000_000:
        score += 8
        reasons.append("سيولة مؤسسية/نشطة")

    hot_lane = bool(score >= 42 and not blockers and change_pct < 10.0)
    if hot_lane:
        stage_hint = "live_ignition_hot_lane"
    return {
        "version": LIVE_IGNITION_VERSION,
        "symbol": str(symbol or "").upper().strip(),
        "price": round(price, 4),
        "change_pct": round(change_pct, 4),
        "volume": volume,
        "dollar_volume": round(dollar_volume, 2),
        "ignition_score": round(max(0, min(100, score)), 2),
        "hot_lane_eligible": hot_lane,
        "stage_hint": stage_hint,
        "reasons": reasons[:8],
        "blockers": blockers[:8],
    }
