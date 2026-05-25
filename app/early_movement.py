"""Early Movement Watchlist layer for Stock Radar AI.

This module is intentionally lightweight and Railway-safe:
- No external API calls.
- No GitHub writes.
- No large exports.
- Uses the current scan rows + a small weekly curated list.

The goal is to keep the new Polygon-derived watchlist separate from the
normal Strong/Cautious/Watch workflow while still letting confirmed names be
recognized quickly during the week.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.settings import DATA_DIR
from app.sqlite_store import SQLITE_DB_PATH

EARLY_MOVEMENT_VERSION = "early_movement_watchlist_v2_hotfix6_prior_move_gate"
NY_TZ = ZoneInfo("America/New_York")

# The weekly list is deliberately small. It is the Sharia-filtered user/assistant
# curated list from the Polygon flat-file weekend analysis. It is not a buy list.
DEFAULT_WEEKLY_PRIORITY: list[dict[str, Any]] = [
    {"symbol": "HLIT", "pattern": "Trend Continuation", "priority": "high", "validity_days": 5, "confidence": 76, "reasons": ["Friday momentum", "continuation candidate", "needs calm open/pullback hold"]},
    {"symbol": "DECK", "pattern": "Pre-Move Build-Up", "priority": "high", "validity_days": 5, "confidence": 74, "reasons": ["quiet build-up", "quality watch", "needs live volume confirmation"]},
    {"symbol": "AMBQ", "pattern": "Pre-Move Build-Up", "priority": "high", "validity_days": 5, "confidence": 73, "reasons": ["quiet build-up", "relative strength", "needs breakout/hold confirmation"]},
    {"symbol": "LNTH", "pattern": "Trend Continuation", "priority": "high", "validity_days": 5, "confidence": 72, "reasons": ["Friday continuation", "must avoid chase", "watch pullback/hold"]},
    {"symbol": "PKE", "pattern": "Pre-Move Build-Up", "priority": "high", "validity_days": 5, "confidence": 71, "reasons": ["quiet watch", "not extended like high movers", "needs liquidity confirmation"]},
    {"symbol": "CRSR", "pattern": "Trend Continuation", "priority": "medium", "validity_days": 4, "confidence": 69, "reasons": ["Friday strength", "continuation only", "avoid gap chase"]},
    {"symbol": "BB", "pattern": "High-Risk Continuation", "priority": "medium", "validity_days": 3, "confidence": 66, "reasons": ["large Friday move", "watch only after pullback/hold", "no chase if gaps again"]},
    {"symbol": "NTAP", "pattern": "Trend Continuation", "priority": "medium", "validity_days": 4, "confidence": 68, "reasons": ["Friday strength", "needs calm open", "confirm liquidity"]},
    {"symbol": "RDW", "pattern": "High-Risk Continuation", "priority": "medium", "validity_days": 3, "confidence": 64, "reasons": ["extended Friday move", "watch only", "no chase if premarket gap"]},
    {"symbol": "VELO", "pattern": "High-Risk Continuation", "priority": "medium", "validity_days": 3, "confidence": 64, "reasons": ["Friday + after-hours strength", "needs strict no-chase guard", "higher volatility"]},
    {"symbol": "NAVN", "pattern": "Continuation Watch", "priority": "medium", "validity_days": 4, "confidence": 66, "reasons": ["Friday continuation", "after-hours follow-through", "watch liquidity persistence"]},
    {"symbol": "CHGG", "pattern": "Continuation Watch", "priority": "medium", "validity_days": 4, "confidence": 65, "reasons": ["Friday continuation", "after-hours follow-through", "watch for distribution risk"]},
    {"symbol": "TMCI", "pattern": "Pre-Move / Quiet Watch", "priority": "medium", "validity_days": 3, "confidence": 62, "reasons": ["quiet candidate from prior review", "not yet extended", "needs unusual volume or range breakout"]},
]

DEFAULT_HIGH_RISK_MANUAL: list[dict[str, Any]] = [
    {"symbol": "KXIN", "pattern": "High-Risk Manual Watch", "priority": "low", "validity_days": 2, "confidence": 50, "reasons": ["technically active", "reverse-split history", "manual watch only — not clean list"]},
]


def _clean_symbol(value: str) -> str:
    return str(value or "").upper().strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _watchlist_store_path() -> Path:
    return Path(DATA_DIR) / "early_movement_watchlist.json"


def _default_payload() -> dict[str, Any]:
    now = datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "ok": True,
        "version": EARLY_MOVEMENT_VERSION,
        "updated_at": now,
        "source": "default_curated_polygon_weekly_list",
        "weekly_priority": DEFAULT_WEEKLY_PRIORITY,
        "high_risk_manual": DEFAULT_HIGH_RISK_MANUAL,
        "notes": "Separate monitoring layer only. Does not change Strong/Cautious by itself.",
    }


def load_early_movement_store() -> dict[str, Any]:
    """Load the watchlist store, falling back to the curated default list.

    We do not create/write the file on read to avoid unexpected disk churn. A
    future admin endpoint may explicitly save overrides.
    """
    path = _watchlist_store_path()
    if not path.exists():
        return _default_payload()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return _default_payload()
        payload.setdefault("version", EARLY_MOVEMENT_VERSION)
        payload.setdefault("weekly_priority", DEFAULT_WEEKLY_PRIORITY)
        payload.setdefault("high_risk_manual", DEFAULT_HIGH_RISK_MANUAL)
        return payload
    except Exception as exc:
        out = _default_payload()
        out["warning"] = f"failed_to_read_store: {type(exc).__name__}: {str(exc)[:160]}"
        return out


def get_weekly_priority_items(include_high_risk: bool = False) -> list[dict[str, Any]]:
    store = load_early_movement_store()
    rows = list(store.get("weekly_priority") or [])
    if include_high_risk:
        rows += list(store.get("high_risk_manual") or [])
    out = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = _clean_symbol(row.get("symbol", ""))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        item = dict(row)
        item["symbol"] = sym
        item.setdefault("pattern", "Early Movement Watch")
        item.setdefault("priority", "medium")
        item.setdefault("validity_days", 5)
        item.setdefault("confidence", 60)
        item.setdefault("reasons", [])
        out.append(item)
    return out


def get_weekly_priority_symbols(include_high_risk: bool = False) -> list[str]:
    return [x["symbol"] for x in get_weekly_priority_items(include_high_risk=include_high_risk)]


def _weekly_map(include_high_risk: bool = True) -> dict[str, dict[str, Any]]:
    return {x["symbol"]: x for x in get_weekly_priority_items(include_high_risk=include_high_risk)}




def _weekly_is_clean_pre_move(item: dict | None) -> bool:
    """Return True only for weekly Polygon names that are meant to be true early/pre-move watches.

    The curated Polygon list intentionally contains both quiet build-up names and
    high-risk continuation names.  Continuation/High-Risk names should remain
    visible in the broader source/promotion pipeline, but they must not enter the
    clean Early Movement list just because the current session is quiet.
    """
    if not isinstance(item, dict):
        return False
    pattern = str(item.get("pattern") or "").lower()
    reasons = " ".join(str(x).lower() for x in (item.get("reasons") or []))
    text = f"{pattern} {reasons}"
    if any(token in text for token in ["high-risk", "continuation", "already", "large friday", "extended", "no chase", "avoid gap chase", "after-hours follow-through"]):
        return False
    return any(token in text for token in ["pre-move", "build-up", "quiet", "early"])


def _positive_max(values: list[float]) -> float:
    out = 0.0
    for value in values:
        try:
            f = float(value or 0)
        except Exception:
            continue
        if f > out:
            out = f
    return out


def _prior_or_rolling_move_guard(stock: dict, stage_meta: dict | None = None) -> dict[str, Any]:
    """Detect prior-session / multi-session moves before allowing clean Early Watch.

    Many data providers use different field names.  This guard is intentionally
    broad and no-API: it only consumes fields already present on the scan row or
    move-stage metadata.  If a field is missing, it does nothing.
    """
    stage_meta = stage_meta or {}
    prior_fields = [
        "prior_day_change_pct", "previous_day_change_pct", "prev_day_change_pct",
        "yesterday_change_pct", "last_day_change_pct", "last_session_change_pct",
        "previous_session_change_pct", "day_change_pct_prev", "prev_close_change_pct",
    ]
    rolling_fields = [
        "rolling_2d_change_pct", "rolling_3d_change_pct", "rolling_5d_change_pct",
        "two_day_change_pct", "three_day_change_pct", "five_day_change_pct",
        "change_2d_pct", "change_3d_pct", "change_5d_pct", "move_2d_pct",
        "move_3d_pct", "move_5d_pct", "weekly_change_pct",
    ]
    gap_fields = [
        "after_hours_change_pct", "pre_market_change_pct", "gap_from_regular_close_pct",
        "open_gap_pct", "premarket_change_pct",
    ]
    prior_values = [_safe_float(stock.get(k, stage_meta.get(k, 0)), 0) for k in prior_fields]
    rolling_values = [_safe_float(stock.get(k, stage_meta.get(k, 0)), 0) for k in rolling_fields]
    gap_values = [_safe_float(stock.get(k, stage_meta.get(k, 0)), 0) for k in gap_fields]
    prior_peak = _positive_max(prior_values)
    rolling_peak = _positive_max(rolling_values)
    gap_peak = _positive_max(gap_values)
    blocked = bool(prior_peak >= 10.0 or rolling_peak >= 12.0 or gap_peak >= 10.0)
    return {
        "blocked": blocked,
        "prior_session_peak_gain": round(prior_peak, 4),
        "rolling_session_peak_gain": round(rolling_peak, 4),
        "gap_or_extended_peak_gain": round(gap_peak, 4),
        "reason": "تحرك سابق/متعدد الجلسات كبير؛ ليس مراقبة مبكرة نظيفة" if blocked else "",
    }


def _read_stock_numbers(stock: dict) -> dict[str, float]:
    intraday = stock.get("intraday", {}) or {}
    return {
        "price": _safe_float(stock.get("display_price", stock.get("current_price_live", 0))),
        "entry": _safe_float(stock.get("display_entry_price", stock.get("entry", 0))),
        "change": _safe_float(stock.get("display_change_pct", stock.get("change_vs_prev_close_pct", 0))),
        "change_prev": _safe_float(stock.get("change_vs_prev_close_pct", 0)),
        "change_open": _safe_float(stock.get("change_from_open_pct", 0)),
        "volume": _safe_float(stock.get("volume_pace_ratio", stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)))),
        "effective_volume": _safe_float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0))),
        "readiness": _safe_float(stock.get("execution_readiness_score", 0)),
        "quality": _safe_float(stock.get("quality_score", 0)),
        "rank": _safe_float(stock.get("display_rank_score", 0)),
        "res_dist": _safe_float(stock.get("nearest_resistance_distance_pct", 999), 999),
        "support_dist": _safe_float(stock.get("nearest_support_distance_pct", 999), 999),
        "session_position": _safe_float(intraday.get("session_position_pct", stock.get("session_position_pct", 0))),
        "liquidity_persistence": _safe_float(stock.get("liquidity_persistence_score", 0)),
        "continuation_score": _safe_float(stock.get("continuation_score", 0)),
        "runner_score": _safe_float(stock.get("runner_score", 0)),
    }


def _is_auto_detected_candidate(stock: dict, n: dict[str, float]) -> tuple[bool, str, list[str], int]:
    reasons: list[str] = []
    pattern = ""
    score = 0
    trend = str(stock.get("trend", "") or "")
    decision = str(stock.get("decision", "") or "")
    stage = str(stock.get("signal_stage_label", stock.get("signal_stage", "")) or "")

    if n["volume"] >= 1.35 and 0 <= n["change"] <= 9 and trend in {"صاعد", "صاعد قوي"}:
        pattern = "First-Hour Liquidity Acceleration"
        reasons.append("تسارع سيولة مع صعود غير مبالغ فيه")
        score += 28
    if n["continuation_score"] >= 70 and n["effective_volume"] >= 1.0:
        pattern = pattern or "Trend Continuation"
        reasons.append("استمرار اتجاه مدعوم بسيولة")
        score += 20
    if n["runner_score"] >= 70 and n["session_position"] >= 60:
        pattern = pattern or "Steady Liquidity Followthrough"
        reasons.append("Runner/Followthrough مع تمركز جيد داخل الجلسة")
        score += 18
    if decision in {"دخول قوي", "دخول بحذر"} and n["readiness"] >= 55 and n["change"] <= 10:
        pattern = pattern or "Live Confirmation"
        reasons.append("دخل فرصة في الأداة مع جاهزية تنفيذ مقبولة")
        score += 18
    if "اختراق" in stage and n["volume"] >= 1.15:
        pattern = pattern or "Breakout Watch"
        reasons.append("مرحلة اختراق مع سيولة")
        score += 10

    ok = score >= 24
    return ok, (pattern or "Auto-Detected Early Movement"), reasons[:5], min(100, 50 + score)


def classify_early_movement(stock: dict) -> dict[str, Any]:
    sym = _clean_symbol(stock.get("symbol", ""))
    weekly = _weekly_map(include_high_risk=True)
    item = weekly.get(sym)
    is_high_risk_manual = bool(item and str(item.get("pattern", "")).lower().startswith("high-risk"))
    # Weekly Polygon contains both true pre-move names and continuation/no-chase
    # names. Only true build-up/quiet names are allowed into the clean Early
    # Movement list by weekly status alone. Continuation names remain in the
    # source/promotion lane, not in the clean Early Movement section.
    is_clean_weekly_pre_move = _weekly_is_clean_pre_move(item)
    is_weekly = bool(item and not is_high_risk_manual and is_clean_weekly_pre_move)
    is_weekly_priority_symbol = bool(item and not is_high_risk_manual)
    n = _read_stock_numbers(stock)
    auto_ok, auto_pattern, auto_reasons, auto_conf = _is_auto_detected_candidate(stock, n)
    pre_move_ok = bool(stock.get("pre_move_watch_eligible", False))
    pre_move_reasons = [str(x) for x in (stock.get("pre_move_reasons") or []) if str(x).strip()]
    if pre_move_ok and not auto_ok:
        auto_pattern = "Pre-Move Engine V2"
        auto_reasons = pre_move_reasons or ["Pre-Move Engine V2 يرى بناء حركة قبل الانفجار"]
        auto_conf = max(auto_conf, int(_safe_float(stock.get("pre_move_score", 60), 60)))

    if item and not is_clean_weekly_pre_move:
        # A curated Polygon continuation/high-risk name should remain in the
        # priority source lane, but not in the clean Early Movement list.  This
        # prevents BB/VELO/RDW/CRSR-style names from looking like pre-move
        # candidates on a quiet session after a prior large move.
        return {
            "in_early_movement": False,
            "symbol": sym,
            "version": EARLY_MOVEMENT_VERSION,
            "polygon_weekly_priority": bool(is_weekly_priority_symbol or is_high_risk_manual),
            "polygon_weekly_stage": str((item or {}).get("pattern") or ""),
            "excluded_reason": "قائمة Polygon: استمرار/مخاطرة عالية — تعرض في مسار المتابعة/Pullback وليس مراقبة مبكرة نظيفة",
        }

    if not (is_weekly or auto_ok or pre_move_ok):
        return {"in_early_movement": False, "symbol": sym}

    # Source / Early Discovery V2: Early Movement must be a clean pre-move or
    # early-confirmation list.  If the detection journal says the stock was
    # first seen after it had already moved +10% or more, it must not appear in
    # the Early Movement section.  It can still appear elsewhere as
    # Continuation / Requires Pullback / No-Chase via move_stage fields.
    stage_meta = stock.get("move_stage_v2") or {}
    move_stage = str(stage_meta.get("move_stage") or stock.get("move_stage") or "")
    gain_at_detection = _safe_float(stock.get("gain_at_detection", stage_meta.get("gain_at_detection", n["change"])), n["change"])
    current_gain = _safe_float(stock.get("current_gain", stage_meta.get("current_gain", n["change"])), n["change"])
    peak_gain_seen = max(
        _safe_float(stock.get("peak_gain_seen", 0), 0),
        _safe_float(stock.get("intraday_peak_gain", 0), 0),
        _safe_float(stage_meta.get("peak_gain_seen", 0), 0),
        _safe_float(stage_meta.get("max_gain_basis", 0), 0),
        current_gain,
        gain_at_detection,
    )
    stage_allows_early = stage_meta.get("stage_allows_early_watch", stock.get("stage_allows_early_watch", True))
    prior_guard = _prior_or_rolling_move_guard(stock, stage_meta)
    late_stages = {"Continuation Watch", "Already Moved", "Extended", "Requires Pullback", "No-Chase", "Catalyst Spike Review"}
    if gain_at_detection >= 10 or current_gain >= 10 or peak_gain_seen >= 10 or move_stage in late_stages or stage_allows_early is False or prior_guard.get("blocked"):
        return {
            "in_early_movement": False,
            "symbol": sym,
            "version": EARLY_MOVEMENT_VERSION,
            "late_movement_excluded": True,
            "move_stage": move_stage,
            "gain_at_detection": round(gain_at_detection, 4),
            "current_gain": round(current_gain, 4),
            "peak_gain_seen": round(peak_gain_seen, 4),
            "prior_session_peak_gain": prior_guard.get("prior_session_peak_gain"),
            "rolling_session_peak_gain": prior_guard.get("rolling_session_peak_gain"),
            "gap_or_extended_peak_gain": prior_guard.get("gap_or_extended_peak_gain"),
            "polygon_weekly_priority": bool(is_weekly_priority_symbol),
            "polygon_weekly_stage": str((item or {}).get("pattern") or "") if item else "",
            "excluded_reason": prior_guard.get("reason") or "ليس مراقبة مبكرة: السهم متحرك/متأخر عند الاكتشاف أو تجاوز/لامس +10% خلال الجلسة",
        }

    source = "weekly_priority" if is_weekly else "high_risk_manual" if is_high_risk_manual else "pre_move_engine_v2" if pre_move_ok else "auto_detected"
    if (is_weekly or is_high_risk_manual) and (auto_ok or pre_move_ok):
        source = "both" if is_weekly else "high_risk_manual_plus_auto"

    pattern = str((item or {}).get("pattern") or auto_pattern or "Early Movement Watch")
    reasons = []
    for r in (item or {}).get("reasons", []) or []:
        if str(r).strip():
            reasons.append(str(r).strip())
    for r in auto_reasons:
        if r and r not in reasons:
            reasons.append(r)

    confidence = max(_safe_int((item or {}).get("confidence"), 0), auto_conf if (auto_ok or pre_move_ok) else 0)
    validity_days = _safe_int((item or {}).get("validity_days"), 3 if auto_ok else 5)

    no_chase_reasons: list[str] = []
    distribution_reasons: list[str] = []
    if n["change"] >= 12 or n["change_prev"] >= 12:
        no_chase_reasons.append(f"صعود اليوم/آخر سعر مرتفع ({round(max(n['change'], n['change_prev']), 2)}%)")
    if n["change_open"] >= 8:
        no_chase_reasons.append(f"ابتعد عن الافتتاح ({round(n['change_open'], 2)}%)")
    if n["entry"] > 0 and n["price"] > n["entry"] * 1.045:
        no_chase_reasons.append("السعر ابتعد عن منطقة الدخول")
    readiness_label = str(stock.get("execution_readiness_label", "") or "")
    if "مطاردة" in readiness_label:
        no_chase_reasons.append("جاهزية التنفيذ تصفه كمطاردة سعرية")
    if n["res_dist"] <= 1.0 and n["res_dist"] >= 0:
        no_chase_reasons.append("قريب جدًا من مقاومة")
    if n["session_position"] and n["session_position"] < 45 and (n["change"] > 4 or n["change_open"] > 4):
        distribution_reasons.append("صعد ثم تراجع من قمة الجلسة")
    if n["liquidity_persistence"] and n["liquidity_persistence"] < 42:
        distribution_reasons.append("السيولة لا تبدو مستمرة")

    status = "watch"
    status_label = "🟣 مراقبة حركة مبكرة"
    recommended_action = "راقب فقط حتى تظهر سيولة/اختراق/ثبات."
    rank_bucket = 1

    if no_chase_reasons and (n["change"] >= 15 or n["change_open"] >= 10 or n["res_dist"] <= 0.6):
        status = "no_chase"
        status_label = "⛔ لا تطارد"
        recommended_action = "لا تدخل بعد الحركة الحالية؛ انتظر pullback صحي أو إعادة تمركز."
        rank_bucket = -2
    elif distribution_reasons:
        status = "distribution_risk"
        status_label = "🟠 خطر تصريف/فشل متابعة"
        recommended_action = "راقب فقط؛ يحتاج استعادة قوة وسيولة قبل أي ترقية."
        rank_bucket = -1
    elif source == "both" and n["readiness"] >= 50 and n["volume"] >= 1.0 and not no_chase_reasons:
        status = "priority_watch"
        status_label = "🔥 Priority Watch"
        recommended_action = "مرشح من قائمة الويكند وأكد حيًا؛ يستحق متابعة لصيقة دون مطاردة."
        rank_bucket = 4
    elif auto_ok and n["readiness"] >= 55 and n["volume"] >= 1.15 and not no_chase_reasons:
        status = "confirmed_watch"
        status_label = "✅ تأكيد حي للمراقبة"
        recommended_action = "تأكيد حي جيد؛ يترقى فقط إذا اجتاز دعم/مقاومة وسيولة وعدم مطاردة."
        rank_bucket = 3
    elif n["change"] <= -3 and n["volume"] < 1.0:
        status = "weak_or_expired"
        status_label = "❌ ضعيف/انتهاء صلاحية مؤقت"
        recommended_action = "ينخفض في الأولوية حتى يظهر محفز أو سيولة جديدة."
        rank_bucket = -1
    elif is_high_risk_manual:
        status = "high_risk_manual_watch"
        status_label = "⚠️ مراقبة عالية المخاطر"
        recommended_action = "مراقبة فقط بسبب مخاطر السلوك/الهيكل؛ لا يختلط مع القائمة النظيفة."
        rank_bucket = 0

    if no_chase_reasons:
        reasons += [f"No-Chase: {x}" for x in no_chase_reasons[:3]]
    if distribution_reasons:
        reasons += [f"خطر تصريف: {x}" for x in distribution_reasons[:3]]

    summary = f"{status_label} — {pattern}. {recommended_action}"
    return {
        "in_early_movement": True,
        "version": EARLY_MOVEMENT_VERSION,
        "symbol": sym,
        "source": source,
        "pattern": pattern,
        "status": status,
        "status_label": status_label,
        "priority": str((item or {}).get("priority") or ("auto" if auto_ok else "medium")),
        "confidence_score": int(max(0, min(100, confidence))),
        "validity_days": int(max(1, validity_days)),
        "rank_bucket": int(rank_bucket),
        "reasons": reasons[:8],
        "no_chase_reasons": no_chase_reasons[:5],
        "distribution_reasons": distribution_reasons[:5],
        "recommended_action": recommended_action,
        "summary": summary,
        "is_weekly_priority": bool(is_weekly),
        "polygon_weekly_priority": bool(is_weekly_priority_symbol),
        "polygon_weekly_stage": str((item or {}).get("pattern") or "") if item else "",
        "is_auto_detected": bool(auto_ok or pre_move_ok),
        "is_pre_move_engine_v2": bool(pre_move_ok),
        "is_high_risk_manual": bool(is_high_risk_manual),
    }


def enrich_stock_with_early_movement(stock: dict) -> dict:
    if not isinstance(stock, dict):
        return stock
    meta = classify_early_movement(stock)
    stock["early_movement"] = meta
    stock["early_movement_active"] = bool(meta.get("in_early_movement"))
    if meta.get("in_early_movement"):
        stock["early_movement_source"] = meta.get("source", "")
        stock["early_movement_pattern"] = meta.get("pattern", "")
        stock["early_movement_status"] = meta.get("status", "")
        stock["early_movement_status_label"] = meta.get("status_label", "")
        stock["early_movement_confidence_score"] = meta.get("confidence_score", 0)
        stock["early_movement_validity_days"] = meta.get("validity_days", 0)
        stock["early_movement_reasons"] = meta.get("reasons", [])
        stock["early_movement_summary"] = meta.get("summary", "")
        # If a watched name becomes no-chase/distribution risk, expose that in
        # the same guard fields used by the UI. This does not upgrade decisions.
        if meta.get("status") == "no_chase":
            stock["no_chase_guard_status"] = "no_chase"
            stock["no_chase_guard_label"] = meta.get("status_label", "⛔ لا تطارد")
            existing = list(stock.get("no_chase_guard_reasons") or [])
            for r in meta.get("no_chase_reasons", []):
                if r not in existing:
                    existing.append(r)
            stock["no_chase_guard_reasons"] = existing[:7]
        elif meta.get("status") == "distribution_risk":
            stock["pattern_risk_status"] = "medium"
            stock["pattern_risk_label"] = meta.get("status_label", "🟠 خطر تصريف")
            existing = list(stock.get("pattern_risk_reasons") or [])
            for r in meta.get("distribution_reasons", []):
                if r not in existing:
                    existing.append(r)
            stock["pattern_risk_reasons"] = existing[:7]
    return stock


def enrich_stocks_with_early_movement(rows: list[dict]) -> list[dict]:
    return [enrich_stock_with_early_movement(x) for x in (rows or [])]


def _early_sort_key(stock: dict) -> tuple:
    em = stock.get("early_movement") or {}
    return (
        _safe_int(em.get("rank_bucket"), 0),
        _safe_float(em.get("confidence_score", 0)),
        _safe_float(stock.get("execution_readiness_score", 0)),
        _safe_float(stock.get("quality_score", 0)),
        _safe_float(stock.get("display_rank_score", 0)),
    )


def build_early_movement_sections(rows: list[dict]) -> dict[str, Any]:
    enriched = [x for x in (rows or []) if isinstance(x, dict) and (x.get("early_movement") or {}).get("in_early_movement")]
    weekly = [x for x in enriched if (x.get("early_movement") or {}).get("is_weekly_priority")]
    auto = [x for x in enriched if (x.get("early_movement") or {}).get("is_auto_detected") and not (x.get("early_movement") or {}).get("is_weekly_priority")]
    priority = [x for x in enriched if (x.get("early_movement") or {}).get("status") == "priority_watch"]
    high_risk = [x for x in enriched if (x.get("early_movement") or {}).get("is_high_risk_manual")]
    no_chase = [x for x in enriched if (x.get("early_movement") or {}).get("status") in {"no_chase", "distribution_risk", "weak_or_expired"}]

    def sorted_limited(items, limit=25):
        return sorted(items, key=_early_sort_key, reverse=True)[:limit]

    return {
        "version": EARLY_MOVEMENT_VERSION,
        "count": len(enriched),
        "weekly_priority_count": len(weekly),
        "auto_detected_count": len(auto),
        "priority_watch_count": len(priority),
        "high_risk_count": len(high_risk),
        "risk_watch_count": len(no_chase),
        "early_movement_watchlist": sorted_limited(enriched, 30),
        "weekly_priority_rows": sorted_limited(weekly, 20),
        "auto_detected_rows": sorted_limited(auto, 20),
        "priority_watch_rows": sorted_limited(priority, 12),
        "risk_watch_rows": sorted_limited(no_chase, 20),
    }


def _current_week_key() -> str:
    d = datetime.now(NY_TZ).date()
    start = d - timedelta(days=d.weekday())
    end = start + timedelta(days=4)
    return f"{start.isoformat()}_{end.isoformat()}"


def _tracking_summary(symbols: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {s: {} for s in symbols}
    try:
        db_path = str(SQLITE_DB_PATH or "")
        if not db_path or not Path(db_path).exists():
            return out
        with sqlite3.connect(db_path, timeout=8) as conn:
            conn.row_factory = sqlite3.Row
            q_marks = ",".join(["?"] * len(symbols))
            if not q_marks:
                return out
            rows = conn.execute(
                f"""
                SELECT symbol,
                       COUNT(*) AS signal_rows,
                       SUM(times_seen_count) AS times_seen,
                       MAX(max_gain_pct) AS max_gain_pct,
                       MIN(max_loss_pct) AS max_loss_pct,
                       MAX(CASE WHEN signal_bucket LIKE '%قوي%' OR signal_label LIKE '%قوي%' THEN 1 ELSE 0 END) AS ever_strong,
                       MAX(CASE WHEN signal_bucket LIKE '%حذر%' OR signal_label LIKE '%حذر%' THEN 1 ELSE 0 END) AS ever_cautious,
                       MAX(last_seen_at) AS last_seen_at,
                       GROUP_CONCAT(DISTINCT outcome_group) AS outcome_groups
                FROM tracking_signals
                WHERE symbol IN ({q_marks})
                GROUP BY symbol
                """,
                symbols,
            ).fetchall()
            for r in rows:
                out[str(r["symbol"]).upper()] = dict(r)
    except Exception as exc:
        for s in symbols:
            out.setdefault(s, {})["tracking_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return out


def build_early_movement_static_status() -> dict[str, Any]:
    store = load_early_movement_store()
    weekly = get_weekly_priority_items(include_high_risk=False)
    high = get_weekly_priority_items(include_high_risk=True)[len(weekly):]
    return {
        "ok": True,
        "version": EARLY_MOVEMENT_VERSION,
        "store_source": store.get("source", "default"),
        "weekly_priority_count": len(weekly),
        "high_risk_manual_count": len(high),
        "weekly_priority": weekly,
        "high_risk_manual": high,
        "notes": "Monitoring layer only; it does not replace Strong/Cautious/Watch.",
    }


def build_early_movement_weekly_report(format: str = "json") -> Any:
    weekly = get_weekly_priority_items(include_high_risk=True)
    symbols = [x["symbol"] for x in weekly]
    tracking = _tracking_summary(symbols)
    rows = []
    for item in weekly:
        sym = item["symbol"]
        t = tracking.get(sym, {}) or {}
        status = "not_seen_yet"
        if _safe_float(t.get("max_gain_pct", 0)) >= 10:
            status = "moved_10pct_plus"
        elif _safe_int(t.get("ever_strong", 0)):
            status = "promoted_to_strong"
        elif _safe_int(t.get("ever_cautious", 0)):
            status = "promoted_to_cautious"
        elif _safe_int(t.get("signal_rows", 0)):
            status = "seen_by_tool"
        rows.append({
            "symbol": sym,
            "pattern": item.get("pattern"),
            "priority": item.get("priority"),
            "confidence": item.get("confidence"),
            "validity_days": item.get("validity_days"),
            "status": status,
            "tracking": t,
            "reasons": item.get("reasons", []),
        })
    payload = {
        "ok": True,
        "version": EARLY_MOVEMENT_VERSION,
        "week_key": _current_week_key(),
        "rows_count": len(rows),
        "rows": rows,
        "summary": {
            "moved_10pct_plus": len([x for x in rows if x["status"] == "moved_10pct_plus"]),
            "promoted_to_strong": len([x for x in rows if x["status"] == "promoted_to_strong"]),
            "promoted_to_cautious": len([x for x in rows if x["status"] == "promoted_to_cautious"]),
            "seen_by_tool_only": len([x for x in rows if x["status"] == "seen_by_tool"]),
            "seen_total": len([x for x in rows if _safe_int((x.get("tracking") or {}).get("signal_rows", 0)) > 0]),
            "not_seen_yet": len([x for x in rows if x["status"] == "not_seen_yet"]),
        },
    }
    if str(format or "json").lower() not in {"brief", "text", "txt", "chatgpt"}:
        return payload
    lines = [
        "تقرير مراقبة الحركة المبكرة",
        f"الأسبوع: {payload['week_key']}",
        f"عدد الأسهم: {len(rows)}",
        "",
        "الملخص:",
    ]
    for k, v in payload["summary"].items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("الأسهم:")
    for row in rows:
        tr = row.get("tracking", {}) or {}
        lines.append(
            f"- {row['symbol']}: {row.get('pattern')} | status={row['status']} | "
            f"max_gain={round(_safe_float(tr.get('max_gain_pct', 0)), 2)}% | "
            f"max_loss={round(_safe_float(tr.get('max_loss_pct', 0)), 2)}% | "
            f"seen={_safe_int(tr.get('times_seen', 0))}"
        )
    return "\n".join(lines)
