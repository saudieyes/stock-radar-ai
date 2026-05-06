"""Tracking Intelligence V1 for Stock Radar AI.

A lightweight, append/update-only SQLite layer that observes radar signals after
classification. It does not change scoring, Sharia filtering, price fetching, or
UI behavior. It uses prices already available from trade scans/live overlays.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .performance_tracker import get_performance_week_key, get_performance_week_window
from .sqlite_store import SQLITE_DB_PATH, SQLITE_ENABLED
from .utils import safe_round

TRACKING_INTELLIGENCE_ENABLED = str(os.getenv("TRACKING_INTELLIGENCE_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
TRACKING_INTELLIGENCE_MAX_ACTIVE_ROWS = int(float(os.getenv("TRACKING_INTELLIGENCE_MAX_ACTIVE_ROWS", "1500") or 1500))
TRACKING_INTELLIGENCE_ABSENCE_GRACE_SEC = int(float(os.getenv("TRACKING_INTELLIGENCE_ABSENCE_GRACE_SEC", "240") or 240))

_LOCK = threading.RLock()
_INITIALIZED = False
NY_TZ = ZoneInfo("America/New_York")

BUCKET_LABELS = {
    "strong": "دخول قوي",
    "cautious": "دخول بحذر",
    "gray_strong": "قوي غير محسوم شرعيًا - تقييم فني فقط",
}
FINAL_STATUSES = {
    "target_hit",
    "above_target",
    "stopped",
    "plan_broken_before_activation",
}


def _now_ts() -> float:
    return time.time()


def _now_text() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SQLITE_DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
    except Exception:
        pass
    return conn


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            clean = value.replace("%", "").replace(",", "").strip()
            if not clean:
                return default
            return float(clean)
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return default


def _clean_text(value: Any, limit: int = 500) -> str:
    txt = str(value or "").strip()
    return txt[:limit]


def _first_text(row: dict, keys: list[str], limit: int = 500) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return _clean_text(value, limit)
    return ""


def _first_float(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        value = row.get(key)
        number = _safe_float(value, default=0.0)
        if number > 0:
            return number
    return default


def _normalize_symbol(value: Any) -> str:
    return str(value or "").upper().strip().replace(" ", "")[:24]


def _bucket_plan_price(value: float) -> float:
    value = _safe_float(value)
    if value <= 0:
        return 0.0
    if value < 5:
        return round(value, 2)
    if value < 25:
        return round(value, 1)
    return round(round(value * 2.0) / 2.0, 2)


def _plan_signature(entry_price: float, target_price: float, stop_loss: float) -> str:
    return f"{_bucket_plan_price(entry_price)}::{_bucket_plan_price(target_price)}::{_bucket_plan_price(stop_loss)}"


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _json_loads(value: Any, default: Any = None) -> Any:
    try:
        if not value:
            return default
        return json.loads(str(value))
    except Exception:
        return default


def _pct(num: float, den: float) -> float:
    try:
        den = float(den or 0)
        if den <= 0:
            return 0.0
        return float(num or 0) / den * 100.0
    except Exception:
        return 0.0


def init_tracking_intelligence_db() -> bool:
    """Create Tracking Intelligence tables without touching existing app tables."""
    global _INITIALIZED
    if not (SQLITE_ENABLED and TRACKING_INTELLIGENCE_ENABLED):
        return False
    if _INITIALIZED:
        return True
    with _LOCK:
        if _INITIALIZED:
            return True
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracking_signals (
                    id TEXT PRIMARY KEY,
                    week_key TEXT NOT NULL DEFAULT '',
                    week_start TEXT NOT NULL DEFAULT '',
                    week_end TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    signal_bucket TEXT NOT NULL DEFAULT '',
                    signal_label TEXT NOT NULL DEFAULT '',
                    plan_signature TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    last_price_update_at TEXT NOT NULL DEFAULT '',
                    disappeared_at TEXT NOT NULL DEFAULT '',
                    activated_at TEXT NOT NULL DEFAULT '',
                    target_hit_at TEXT NOT NULL DEFAULT '',
                    target_2_hit_at TEXT NOT NULL DEFAULT '',
                    stopped_at TEXT NOT NULL DEFAULT '',
                    closed_at TEXT NOT NULL DEFAULT '',
                    times_seen_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    status_label TEXT NOT NULL DEFAULT 'ظهر ولم يتفعل',
                    outcome_group TEXT NOT NULL DEFAULT 'pending',
                    snapshot_price REAL NOT NULL DEFAULT 0,
                    current_price REAL NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL DEFAULT 0,
                    target_price REAL NOT NULL DEFAULT 0,
                    target_2_price REAL NOT NULL DEFAULT 0,
                    stop_loss REAL NOT NULL DEFAULT 0,
                    risk_pct REAL NOT NULL DEFAULT 0,
                    rr_ratio REAL NOT NULL DEFAULT 0,
                    quality_score REAL NOT NULL DEFAULT 0,
                    execution_readiness_score REAL NOT NULL DEFAULT 0,
                    signal_strength REAL NOT NULL DEFAULT 0,
                    entry_distance_pct REAL NOT NULL DEFAULT 0,
                    is_near_entry INTEGER NOT NULL DEFAULT 0,
                    is_late_above_entry INTEGER NOT NULL DEFAULT 0,
                    is_entry_far INTEGER NOT NULL DEFAULT 0,
                    plan_family TEXT NOT NULL DEFAULT '',
                    signal_reason TEXT NOT NULL DEFAULT '',
                    nearest_support REAL NOT NULL DEFAULT 0,
                    nearest_support_strength TEXT NOT NULL DEFAULT '',
                    nearest_support_distance_pct REAL NOT NULL DEFAULT 0,
                    nearest_resistance REAL NOT NULL DEFAULT 0,
                    nearest_resistance_strength TEXT NOT NULL DEFAULT '',
                    nearest_resistance_distance_pct REAL NOT NULL DEFAULT 0,
                    year_high REAL NOT NULL DEFAULT 0,
                    ath_high REAL NOT NULL DEFAULT 0,
                    distance_to_52w_high_pct REAL NOT NULL DEFAULT 0,
                    distance_to_ath_pct REAL NOT NULL DEFAULT 0,
                    liquidity_ratio REAL NOT NULL DEFAULT 0,
                    volume_ratio REAL NOT NULL DEFAULT 0,
                    volatility_pct REAL NOT NULL DEFAULT 0,
                    market_phase TEXT NOT NULL DEFAULT '',
                    market_phase_label TEXT NOT NULL DEFAULT '',
                    market_support_label TEXT NOT NULL DEFAULT '',
                    sector_support_label TEXT NOT NULL DEFAULT '',
                    market_sector_score REAL NOT NULL DEFAULT 0,
                    market_sector_live_label TEXT NOT NULL DEFAULT '',
                    benchmark_symbol TEXT NOT NULL DEFAULT '',
                    sector_etf_symbol TEXT NOT NULL DEFAULT '',
                    spy_change_pct REAL NOT NULL DEFAULT 0,
                    qqq_change_pct REAL NOT NULL DEFAULT 0,
                    sector_change_pct REAL NOT NULL DEFAULT 0,
                    max_price_after REAL NOT NULL DEFAULT 0,
                    min_price_after REAL NOT NULL DEFAULT 0,
                    max_gain_pct REAL NOT NULL DEFAULT 0,
                    max_loss_pct REAL NOT NULL DEFAULT 0,
                    minutes_to_activation REAL NOT NULL DEFAULT 0,
                    minutes_to_target REAL NOT NULL DEFAULT 0,
                    minutes_to_stop REAL NOT NULL DEFAULT 0,
                    success_tags_json TEXT NOT NULL DEFAULT '[]',
                    risk_tags_json TEXT NOT NULL DEFAULT '[]',
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    last_update_source TEXT NOT NULL DEFAULT '',
                    created_at_ts REAL NOT NULL DEFAULT 0,
                    updated_at_ts REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracking_signal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id TEXT NOT NULL,
                    week_key TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    event_key TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL DEFAULT '',
                    event_label TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT '',
                    created_at_ts REAL NOT NULL DEFAULT 0,
                    UNIQUE(signal_id, event_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tracking_weekly_insights (
                    week_key TEXT PRIMARY KEY,
                    generated_at TEXT NOT NULL DEFAULT '',
                    generated_at_ts REAL NOT NULL DEFAULT 0,
                    summary_ar TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracking_signals_week_bucket ON tracking_signals(week_key, signal_bucket)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracking_signals_symbol ON tracking_signals(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracking_signals_status ON tracking_signals(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracking_events_week ON tracking_signal_events(week_key, event_type)")
            conn.commit()
        _INITIALIZED = True
    return True


def tracking_status() -> dict:
    out = {
        "enabled": bool(TRACKING_INTELLIGENCE_ENABLED and SQLITE_ENABLED),
        "db_path": str(SQLITE_DB_PATH),
        "initialized": bool(_INITIALIZED),
        "ok": False,
        "error": "",
    }
    if not out["enabled"]:
        out["ok"] = True
        return out
    try:
        init_tracking_intelligence_db()
        with _connect() as conn:
            week_key = get_performance_week_key()
            row = conn.execute("SELECT COUNT(*) AS c FROM tracking_signals WHERE week_key=?", (week_key,)).fetchone()
            out["active_week_key"] = week_key
            out["active_week_signals"] = int(row["c"] if row else 0)
        out["ok"] = True
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return out


def _extract_snapshot(row: dict, bucket: str, market_phase: str = "") -> dict:
    row = dict(row or {})
    symbol = _normalize_symbol(row.get("symbol"))
    current_price = _first_float(row, ["current_price_live", "live_price", "display_price", "current_price", "price"])
    entry_price = _first_float(row, ["display_entry_price", "smart_entry_price", "entry_price_real", "entry", "breakout_price", "confirmation_price"])
    target_price = _first_float(row, ["display_target_price", "smart_target_1", "target_1", "target1", "breakout_target", "target"])
    target_2_price = _first_float(row, ["target_2", "smart_target_2", "display_target_2_price", "target_2_price"])
    stop_loss = _first_float(row, ["display_stop_price", "smart_stop_loss", "stop_loss", "atr_stop_price", "stop"])
    risk_pct = _safe_float(row.get("display_risk_pct", row.get("risk_pct", 0)))
    rr_ratio = _safe_float(row.get("rr_1", row.get("risk_reward", row.get("reward_risk", 0))))
    quality = _safe_float(row.get("quality_score", row.get("display_rank_score", 0)))
    readiness = _safe_float(row.get("execution_readiness_score", row.get("execution_layer_score", 0)))
    signal_strength = _safe_float(row.get("display_rank_score", row.get("quality_score", 0)))

    support = _safe_float(row.get("nearest_support", 0))
    resistance = _safe_float(row.get("nearest_resistance", 0))
    support_dist = _safe_float(row.get("nearest_support_distance_pct", 0))
    resistance_dist = _safe_float(row.get("nearest_resistance_distance_pct", 0))
    year_high = _safe_float(row.get("year_high", row.get("high_52w", row.get("fifty_two_week_high", 0))))
    ath_high = _safe_float(row.get("ath_high", row.get("all_time_high", 0)))
    distance_to_52w = ((year_high - current_price) / current_price * 100.0) if current_price > 0 and year_high > 0 else 0.0
    distance_to_ath = ((ath_high - current_price) / current_price * 100.0) if current_price > 0 and ath_high > 0 else 0.0
    entry_distance = ((current_price - entry_price) / entry_price * 100.0) if current_price > 0 and entry_price > 0 else 0.0

    reason = _first_text(row, [
        "live_rank_reason", "quick_explainer", "decision_reason", "why_appeared", "reason", "notes", "strategy_label"
    ], limit=900)
    plan_family = _first_text(row, ["display_plan_family", "plan_family", "type", "trade_type_label_ar", "strategy_label"], limit=160)
    market_phase_label = _first_text(row, ["market_phase_label"], limit=120)

    liquidity = _safe_float(row.get("effective_volume_ratio", row.get("volume_ratio", row.get("relative_volume", 0))))
    volume_ratio = _safe_float(row.get("volume_ratio", row.get("effective_volume_ratio", row.get("relative_volume", 0))))
    volatility = _safe_float(row.get("atr_pct", row.get("volatility_pct", row.get("display_risk_pct", 0))))

    compact_snapshot_keys = [
        "symbol", "decision", "original_decision", "display_price", "live_price", "display_change_pct",
        "display_entry_price", "display_target_price", "target_2", "display_stop_price", "display_risk_pct",
        "quality_score", "execution_readiness_score", "display_rank_score", "nearest_support",
        "nearest_support_strength", "nearest_support_distance_pct", "nearest_resistance",
        "nearest_resistance_strength", "nearest_resistance_distance_pct", "year_high", "ath_high",
        "market_support_label", "sector_support_label", "market_sector_score", "market_sector_live_label",
        "sector_etf_symbol", "strategy_label", "display_plan_family", "type", "quick_explainer",
    ]
    compact_snapshot = {key: row.get(key) for key in compact_snapshot_keys if key in row}

    return {
        "symbol": symbol,
        "signal_bucket": bucket,
        "signal_label": BUCKET_LABELS.get(bucket, bucket),
        "snapshot_price": safe_round(current_price),
        "current_price": safe_round(current_price),
        "entry_price": safe_round(entry_price),
        "target_price": safe_round(target_price),
        "target_2_price": safe_round(target_2_price),
        "stop_loss": safe_round(stop_loss),
        "risk_pct": safe_round(risk_pct),
        "rr_ratio": safe_round(rr_ratio),
        "quality_score": safe_round(quality),
        "execution_readiness_score": safe_round(readiness),
        "signal_strength": safe_round(signal_strength),
        "entry_distance_pct": safe_round(entry_distance),
        "is_near_entry": 1 if -0.8 <= entry_distance <= 1.25 else 0,
        "is_late_above_entry": 1 if entry_distance > 2.5 else 0,
        "is_entry_far": 1 if entry_distance < -2.5 else 0,
        "plan_family": plan_family,
        "signal_reason": reason,
        "nearest_support": safe_round(support),
        "nearest_support_strength": _first_text(row, ["nearest_support_strength"], limit=80),
        "nearest_support_distance_pct": safe_round(support_dist),
        "nearest_resistance": safe_round(resistance),
        "nearest_resistance_strength": _first_text(row, ["nearest_resistance_strength"], limit=80),
        "nearest_resistance_distance_pct": safe_round(resistance_dist),
        "year_high": safe_round(year_high),
        "ath_high": safe_round(ath_high),
        "distance_to_52w_high_pct": safe_round(distance_to_52w),
        "distance_to_ath_pct": safe_round(distance_to_ath),
        "liquidity_ratio": safe_round(liquidity),
        "volume_ratio": safe_round(volume_ratio),
        "volatility_pct": safe_round(volatility),
        "market_phase": _clean_text(market_phase or row.get("market_phase", ""), 80),
        "market_phase_label": market_phase_label,
        "market_support_label": _first_text(row, ["market_support_label"], limit=120),
        "sector_support_label": _first_text(row, ["sector_support_label"], limit=120),
        "market_sector_score": safe_round(_safe_float(row.get("market_sector_score", 0))),
        "market_sector_live_label": _first_text(row, ["market_sector_live_label"], limit=160),
        "benchmark_symbol": _first_text(row, ["benchmark_symbol", "market_benchmark_symbol"], limit=20) or "SPY",
        "sector_etf_symbol": _first_text(row, ["sector_etf_symbol"], limit=20),
        "spy_change_pct": safe_round(_safe_float(row.get("spy_change_pct", row.get("market_intraday_change_pct", 0)))) ,
        "qqq_change_pct": safe_round(_safe_float(row.get("qqq_change_pct", 0))),
        "sector_change_pct": safe_round(_safe_float(row.get("sector_intraday_change_pct", 0))),
        "snapshot_json": _json_dumps(compact_snapshot),
        "plan_signature": _plan_signature(entry_price, target_price, stop_loss),
    }


def _tags_for_snapshot(snapshot: dict, status: str = "") -> tuple[list[str], list[str]]:
    success_tags: list[str] = []
    risk_tags: list[str] = []

    support_strength = str(snapshot.get("nearest_support_strength", "") or "")
    resistance_strength = str(snapshot.get("nearest_resistance_strength", "") or "")
    support_dist = _safe_float(snapshot.get("nearest_support_distance_pct", 0))
    resistance_dist = _safe_float(snapshot.get("nearest_resistance_distance_pct", 0))
    market_label = str(snapshot.get("market_support_label", "") or "")
    sector_label = str(snapshot.get("sector_support_label", "") or "")
    market_sector_score = _safe_float(snapshot.get("market_sector_score", 0))
    volume_ratio = _safe_float(snapshot.get("volume_ratio", snapshot.get("liquidity_ratio", 0)))
    volatility = _safe_float(snapshot.get("volatility_pct", snapshot.get("risk_pct", 0)))
    risk_pct = _safe_float(snapshot.get("risk_pct", 0))
    current_price = _safe_float(snapshot.get("current_price", snapshot.get("snapshot_price", 0)))
    support = _safe_float(snapshot.get("nearest_support", 0))
    plan_family = str(snapshot.get("plan_family", "") or "")
    distance_52w = _safe_float(snapshot.get("distance_to_52w_high_pct", 0))
    distance_ath = _safe_float(snapshot.get("distance_to_ath_pct", 0))
    entry_distance = _safe_float(snapshot.get("entry_distance_pct", 0))

    strong_words = {"قوي", "قوي جدًا", "قوية", "قوية جدًا"}
    if resistance_dist > 0 and resistance_dist <= 2.0 and any(w in resistance_strength for w in strong_words):
        risk_tags.append("قريب من مقاومة قوية")
    elif resistance_dist >= 3.0:
        success_tags.append("المقاومة بعيدة")

    if support_dist > 0 and support_dist <= 2.0 and any(w in support_strength for w in strong_words):
        success_tags.append("الدعم قريب وقوي")
    elif support <= 0:
        risk_tags.append("الدعم غير واضح")

    if -0.8 <= entry_distance <= 1.25:
        success_tags.append("السعر قريب من نقطة الدخول")
    elif entry_distance > 2.5:
        risk_tags.append("السعر متأخر فوق نقطة الدخول")
    elif entry_distance < -2.5:
        risk_tags.append("نقطة الدخول بعيدة")

    supportive_text = " ".join([market_label, sector_label, str(snapshot.get("market_sector_live_label", "") or "")])
    if market_sector_score >= 5 or any(x in supportive_text for x in ["داعم", "قوي", "إيجابي", "صاعد"]):
        success_tags.append("السوق والقطاع داعمان")
    if market_sector_score <= -5 or any(x in supportive_text for x in ["ضعيف", "غير داعم", "هابط", "سلبي"]):
        risk_tags.append("السوق أو القطاع غير داعم")

    if volume_ratio >= 1.1:
        success_tags.append("السيولة استمرت")
    elif 0 < volume_ratio < 0.9:
        risk_tags.append("السيولة لم تستمر")

    if volatility >= 6.0 or risk_pct >= 10.0:
        risk_tags.append("تذبذب عالي")
    if 0 < current_price < 5:
        risk_tags.append("سهم صغير عالي المخاطر")
    if support > 0 and current_price > 0 and current_price < support:
        risk_tags.append("كسر الدعم")
    if "Breakout" in plan_family or "اختراق" in plan_family:
        if status in {"target_hit", "above_target", "activated"}:
            success_tags.append("الاختراق ثبت")
        elif status in {"stopped", "plan_broken_before_activation"}:
            risk_tags.append("فشل الاختراق")
    if 0 < distance_52w <= 3.0:
        risk_tags.append("قرب من قمة سنوية")
    if 0 < distance_ath <= 3.0:
        risk_tags.append("قرب من قمة تاريخية")

    # Deduplicate while preserving order.
    success_tags = list(dict.fromkeys([x for x in success_tags if x]))
    risk_tags = list(dict.fromkeys([x for x in risk_tags if x]))
    return success_tags[:8], risk_tags[:8]


def _add_event(conn: sqlite3.Connection, signal_id: str, week_key: str, symbol: str, event_key: str, event_type: str, event_label: str, price: float = 0.0, details: dict | None = None) -> None:
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO tracking_signal_events(signal_id, week_key, symbol, event_key, event_type, event_label, price, details_json, created_at, created_at_ts)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                week_key,
                symbol,
                event_key,
                event_type,
                event_label,
                safe_round(price),
                _json_dumps(details or {}),
                _now_text(),
                _now_ts(),
            ),
        )
    except Exception:
        pass


def _minutes_between(first_seen_at: str, later_at: str) -> float:
    try:
        a = datetime.strptime(str(first_seen_at or "")[:19], "%Y-%m-%d %H:%M:%S")
        b = datetime.strptime(str(later_at or "")[:19], "%Y-%m-%d %H:%M:%S")
        return round(max(0.0, (b - a).total_seconds() / 60.0), 1)
    except Exception:
        return 0.0


def _evaluate_state(existing: dict, price: float, now_text: str) -> dict:
    row = dict(existing or {})
    price = _safe_float(price)
    entry = _safe_float(row.get("entry_price", 0))
    target = _safe_float(row.get("target_price", 0))
    target2 = _safe_float(row.get("target_2_price", 0))
    stop = _safe_float(row.get("stop_loss", 0))
    first_seen = str(row.get("first_seen_at", "") or now_text)

    if price > 0:
        old_max = _safe_float(row.get("max_price_after", 0))
        old_min = _safe_float(row.get("min_price_after", 0))
        row["current_price"] = safe_round(price)
        row["max_price_after"] = safe_round(max(old_max if old_max > 0 else price, price))
        row["min_price_after"] = safe_round(min(old_min if old_min > 0 else price, price))

    max_seen = _safe_float(row.get("max_price_after", price))
    min_seen = _safe_float(row.get("min_price_after", price))
    activated_at = str(row.get("activated_at", "") or "")
    status = str(row.get("status", "pending") or "pending")

    if entry > 0 and max_seen >= entry and not activated_at:
        activated_at = now_text
        row["activated_at"] = activated_at
        row["minutes_to_activation"] = _minutes_between(first_seen, activated_at)
        status = "activated"
        row["status_label"] = "تفعلت ومستمرة"
        row["outcome_group"] = "active"

    # Count stop only after activation. Before activation, it is a broken setup, not a losing trade.
    if target2 > 0 and max_seen >= target2:
        if not str(row.get("target_2_hit_at", "") or ""):
            row["target_2_hit_at"] = now_text
            row["minutes_to_target"] = _minutes_between(first_seen, now_text)
        status = "above_target"
        row["status_label"] = "تجاوزت الهدف"
        row["outcome_group"] = "success"
        row["closed_at"] = row.get("closed_at") or now_text
    elif target > 0 and max_seen >= target:
        if not str(row.get("target_hit_at", "") or ""):
            row["target_hit_at"] = now_text
            row["minutes_to_target"] = _minutes_between(first_seen, now_text)
        status = "target_hit"
        row["status_label"] = "وصلت الهدف"
        row["outcome_group"] = "success"
        row["closed_at"] = row.get("closed_at") or now_text
    elif activated_at and stop > 0 and min_seen > 0 and min_seen <= stop:
        if not str(row.get("stopped_at", "") or ""):
            row["stopped_at"] = now_text
            row["minutes_to_stop"] = _minutes_between(first_seen, now_text)
        status = "stopped"
        row["status_label"] = "تفعلت وضربت الوقف"
        row["outcome_group"] = "loss"
        row["closed_at"] = row.get("closed_at") or now_text
    elif (not activated_at) and stop > 0 and min_seen > 0 and min_seen <= stop:
        status = "plan_broken_before_activation"
        row["status_label"] = "كسر الخطة قبل التفعيل"
        row["outcome_group"] = "inactive"
        row["closed_at"] = row.get("closed_at") or now_text
    elif activated_at:
        status = "activated"
        row["status_label"] = "تفعلت ومستمرة"
        row["outcome_group"] = "active"
    else:
        if status not in {"disappeared_before_activation"}:
            status = "pending"
            row["status_label"] = "ظهر ولم يتفعل"
            row["outcome_group"] = "pending"

    row["status"] = status
    if entry > 0:
        row["max_gain_pct"] = safe_round(((max_seen - entry) / entry) * 100.0)
        row["max_loss_pct"] = safe_round(((min_seen - entry) / entry) * 100.0)
    success_tags, risk_tags = _tags_for_snapshot(row, status=status)
    row["success_tags_json"] = _json_dumps(success_tags)
    row["risk_tags_json"] = _json_dumps(risk_tags)
    return row


def _row_to_dict(row: sqlite3.Row | None) -> dict:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _update_signal_row(conn: sqlite3.Connection, signal_id: str, updates: dict) -> None:
    allowed = {
        "last_seen_at", "last_price_update_at", "disappeared_at", "activated_at", "target_hit_at", "target_2_hit_at",
        "stopped_at", "closed_at", "times_seen_count", "status", "status_label", "outcome_group", "current_price",
        "target_2_price", "max_price_after", "min_price_after", "max_gain_pct", "max_loss_pct", "minutes_to_activation",
        "minutes_to_target", "minutes_to_stop", "success_tags_json", "risk_tags_json", "last_update_source", "updated_at_ts",
    }
    pairs = [(k, v) for k, v in updates.items() if k in allowed]
    if not pairs:
        return
    sql = "UPDATE tracking_signals SET " + ", ".join([f"{k}=?" for k, _ in pairs]) + " WHERE id=?"
    conn.execute(sql, [v for _, v in pairs] + [signal_id])


def _insert_signal_row(conn: sqlite3.Connection, signal_id: str, week_key: str, week_start: str, week_end: str, snapshot: dict, source: str) -> None:
    now_text = _now_text()
    now_ts = _now_ts()
    evaluated = _evaluate_state({
        **snapshot,
        "id": signal_id,
        "week_key": week_key,
        "week_start": week_start,
        "week_end": week_end,
        "first_seen_at": now_text,
        "last_seen_at": now_text,
        "last_price_update_at": now_text,
        "times_seen_count": 1,
        "max_price_after": snapshot.get("current_price", 0),
        "min_price_after": snapshot.get("current_price", 0),
        "status": "pending",
        "status_label": "ظهر ولم يتفعل",
        "outcome_group": "pending",
    }, _safe_float(snapshot.get("current_price", 0)), now_text)
    evaluated["last_update_source"] = source
    evaluated["created_at_ts"] = now_ts
    evaluated["updated_at_ts"] = now_ts

    columns = [
        "id", "week_key", "week_start", "week_end", "symbol", "signal_bucket", "signal_label", "plan_signature",
        "first_seen_at", "last_seen_at", "last_price_update_at", "times_seen_count", "status", "status_label", "outcome_group",
        "snapshot_price", "current_price", "entry_price", "target_price", "target_2_price", "stop_loss", "risk_pct", "rr_ratio",
        "quality_score", "execution_readiness_score", "signal_strength", "entry_distance_pct", "is_near_entry", "is_late_above_entry", "is_entry_far",
        "plan_family", "signal_reason", "nearest_support", "nearest_support_strength", "nearest_support_distance_pct",
        "nearest_resistance", "nearest_resistance_strength", "nearest_resistance_distance_pct", "year_high", "ath_high",
        "distance_to_52w_high_pct", "distance_to_ath_pct", "liquidity_ratio", "volume_ratio", "volatility_pct",
        "market_phase", "market_phase_label", "market_support_label", "sector_support_label", "market_sector_score", "market_sector_live_label",
        "benchmark_symbol", "sector_etf_symbol", "spy_change_pct", "qqq_change_pct", "sector_change_pct", "max_price_after", "min_price_after",
        "max_gain_pct", "max_loss_pct", "minutes_to_activation", "minutes_to_target", "minutes_to_stop", "success_tags_json", "risk_tags_json",
        "snapshot_json", "last_update_source", "created_at_ts", "updated_at_ts", "activated_at", "target_hit_at", "target_2_hit_at", "stopped_at", "closed_at",
    ]
    payload = {**snapshot, **evaluated, "id": signal_id, "week_key": week_key, "week_start": week_start, "week_end": week_end}
    conn.execute(
        f"INSERT INTO tracking_signals({', '.join(columns)}) VALUES({', '.join(['?'] * len(columns))})",
        [payload.get(col, "") for col in columns],
    )
    _add_event(conn, signal_id, week_key, snapshot.get("symbol", ""), "first_seen", "first_seen", "ظهرت الإشارة لأول مرة", snapshot.get("current_price", 0), {"bucket": snapshot.get("signal_bucket"), "source": source})
    if evaluated.get("activated_at"):
        _add_event(conn, signal_id, week_key, snapshot.get("symbol", ""), "activated", "activated", "تفعلت الإشارة", snapshot.get("current_price", 0), {"entry_price": snapshot.get("entry_price")})
    if evaluated.get("target_hit_at"):
        _add_event(conn, signal_id, week_key, snapshot.get("symbol", ""), "target_hit", "target_hit", "وصلت الهدف", snapshot.get("current_price", 0), {"target_price": snapshot.get("target_price")})
    if evaluated.get("target_2_hit_at"):
        _add_event(conn, signal_id, week_key, snapshot.get("symbol", ""), "above_target", "above_target", "تجاوزت الهدف", snapshot.get("current_price", 0), {"target_2_price": snapshot.get("target_2_price")})


def _upsert_snapshot_rows(conn: sqlite3.Connection, rows: list[dict], bucket: str, week_key: str, week_start: str, week_end: str, market_phase: str, source: str) -> tuple[int, int, list[str]]:
    inserted = 0
    updated = 0
    signal_ids: list[str] = []
    now_text = _now_text()
    now_ts = _now_ts()
    for raw in rows or []:
        snapshot = _extract_snapshot(raw, bucket=bucket, market_phase=market_phase)
        symbol = snapshot.get("symbol", "")
        entry = _safe_float(snapshot.get("entry_price", 0))
        if not symbol or entry <= 0:
            continue
        signal_id = f"{week_key}::{bucket}::{symbol}::{snapshot.get('plan_signature', '')}"
        signal_ids.append(signal_id)
        existing = _row_to_dict(conn.execute("SELECT * FROM tracking_signals WHERE id=?", (signal_id,)).fetchone())
        if not existing:
            _insert_signal_row(conn, signal_id, week_key, week_start, week_end, snapshot, source)
            inserted += 1
            continue

        if str(existing.get("status", "") or "") == "disappeared_before_activation":
            _add_event(conn, signal_id, week_key, symbol, f"reappeared_{_safe_int(existing.get('times_seen_count', 0)) + 1}", "reappeared", "عادت الإشارة للظهور", snapshot.get("current_price", 0), {"source": source})
            existing["status"] = "pending"
            existing["status_label"] = "ظهر ولم يتفعل"
            existing["outcome_group"] = "pending"
            existing["disappeared_at"] = ""

        evaluated = _evaluate_state({**existing, **snapshot}, _safe_float(snapshot.get("current_price", 0)), now_text)
        prior_status = str(existing.get("status", "") or "")
        new_status = str(evaluated.get("status", "") or "")
        if prior_status != new_status:
            _add_event(conn, signal_id, week_key, symbol, new_status, new_status, evaluated.get("status_label", new_status), snapshot.get("current_price", 0), {"source": source})

        _update_signal_row(conn, signal_id, {
            **evaluated,
            "last_seen_at": now_text,
            "last_price_update_at": now_text,
            "times_seen_count": _safe_int(existing.get("times_seen_count", 0)) + 1,
            "last_update_source": source,
            "updated_at_ts": now_ts,
        })
        updated += 1
    return inserted, updated, signal_ids


def record_tracking_snapshots(strong_rows: list[dict] | None = None, cautious_rows: list[dict] | None = None, gray_strong_rows: list[dict] | None = None, market_phase: str = "", source: str = "trade_scan") -> dict:
    """Record fresh full-scan signal appearances. No API calls; SQLite only."""
    if not (SQLITE_ENABLED and TRACKING_INTELLIGENCE_ENABLED):
        return {"ok": True, "enabled": False, "inserted": 0, "updated": 0, "signal_ids": []}
    init_tracking_intelligence_db()
    week_key = get_performance_week_key()
    week_start, week_end = get_performance_week_window()
    totals = {"ok": True, "enabled": True, "inserted": 0, "updated": 0, "seen_count": 0, "signal_ids": [], "error": ""}
    try:
        with _LOCK:
            with _connect() as conn:
                for bucket, rows in (("strong", strong_rows or []), ("cautious", cautious_rows or []), ("gray_strong", gray_strong_rows or [])):
                    ins, upd, ids = _upsert_snapshot_rows(conn, rows, bucket, week_key, week_start, week_end, market_phase, source)
                    totals["inserted"] += ins
                    totals["updated"] += upd
                    totals["seen_count"] += len(ids)
                    totals["signal_ids"].extend(ids)
                conn.commit()
        totals["signal_ids"] = totals["signal_ids"][:TRACKING_INTELLIGENCE_MAX_ACTIVE_ROWS]
        return totals
    except Exception as exc:
        return {"ok": False, "enabled": True, "inserted": totals["inserted"], "updated": totals["updated"], "seen_count": totals["seen_count"], "signal_ids": totals["signal_ids"], "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def mark_tracking_absences_from_scan(current_signal_ids: list[str] | None = None, source: str = "trade_scan") -> dict:
    """Mark active signals that disappeared from a fresh full scan before activation.

    This is intentionally called only after full scans, never from the 30-second
    live price loop, so it does not create noisy absence outcomes.
    """
    if not (SQLITE_ENABLED and TRACKING_INTELLIGENCE_ENABLED):
        return {"ok": True, "enabled": False, "marked": 0}
    init_tracking_intelligence_db()
    current_set = set(current_signal_ids or [])
    week_key = get_performance_week_key()
    now_text = _now_text()
    now_ts = _now_ts()
    marked = 0
    try:
        with _LOCK:
            with _connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM tracking_signals
                    WHERE week_key=? AND status IN ('pending')
                    ORDER BY updated_at_ts DESC
                    LIMIT ?
                    """,
                    (week_key, TRACKING_INTELLIGENCE_MAX_ACTIVE_ROWS),
                ).fetchall()
                for row in rows:
                    item = _row_to_dict(row)
                    signal_id = str(item.get("id", "") or "")
                    if signal_id in current_set:
                        continue
                    last_seen_ts = _safe_float(item.get("updated_at_ts", 0))
                    if last_seen_ts > 0 and (now_ts - last_seen_ts) < TRACKING_INTELLIGENCE_ABSENCE_GRACE_SEC:
                        continue
                    _update_signal_row(conn, signal_id, {
                        "status": "disappeared_before_activation",
                        "status_label": "اختفت قبل التفعيل",
                        "outcome_group": "inactive",
                        "disappeared_at": now_text,
                        "closed_at": now_text,
                        "last_update_source": source,
                        "updated_at_ts": now_ts,
                    })
                    _add_event(conn, signal_id, week_key, item.get("symbol", ""), "disappeared_before_activation", "disappeared_before_activation", "اختفت قبل التفعيل", item.get("current_price", 0), {"source": source})
                    marked += 1
                conn.commit()
        return {"ok": True, "enabled": True, "marked": marked}
    except Exception as exc:
        return {"ok": False, "enabled": True, "marked": marked, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def refresh_tracking_prices_from_rows(rows: list[dict] | None = None, source: str = "radar_live_refresh") -> dict:
    """Update existing active tracking signals using prices already present in rows."""
    if not (SQLITE_ENABLED and TRACKING_INTELLIGENCE_ENABLED):
        return {"ok": True, "enabled": False, "updated": 0}
    rows = rows or []
    if not rows:
        return {"ok": True, "enabled": True, "updated": 0}
    init_tracking_intelligence_db()
    price_by_symbol: dict[str, float] = {}
    for row in rows:
        symbol = _normalize_symbol((row or {}).get("symbol"))
        price = _first_float(row or {}, ["current_price_live", "live_price", "display_price", "current_price", "price"])
        if symbol and price > 0:
            price_by_symbol[symbol] = price
    if not price_by_symbol:
        return {"ok": True, "enabled": True, "updated": 0}

    week_key = get_performance_week_key()
    now_text = _now_text()
    now_ts = _now_ts()
    updated = 0
    changed = 0
    try:
        with _LOCK:
            with _connect() as conn:
                placeholders = ",".join(["?"] * len(price_by_symbol))
                records = conn.execute(
                    f"""
                    SELECT * FROM tracking_signals
                    WHERE week_key=? AND symbol IN ({placeholders}) AND status NOT IN ('target_hit', 'above_target', 'stopped', 'plan_broken_before_activation')
                    ORDER BY updated_at_ts DESC
                    LIMIT ?
                    """,
                    [week_key, *price_by_symbol.keys(), TRACKING_INTELLIGENCE_MAX_ACTIVE_ROWS],
                ).fetchall()
                for record in records:
                    item = _row_to_dict(record)
                    symbol = str(item.get("symbol", "") or "")
                    price = price_by_symbol.get(symbol, 0.0)
                    if price <= 0:
                        continue
                    before_status = str(item.get("status", "") or "")
                    evaluated = _evaluate_state(item, price, now_text)
                    after_status = str(evaluated.get("status", "") or "")
                    if before_status != after_status:
                        changed += 1
                        _add_event(conn, str(item.get("id", "")), week_key, symbol, after_status, after_status, evaluated.get("status_label", after_status), price, {"source": source})
                    _update_signal_row(conn, str(item.get("id", "")), {
                        **evaluated,
                        "last_price_update_at": now_text,
                        "last_update_source": source,
                        "updated_at_ts": now_ts,
                    })
                    updated += 1
                conn.commit()
        return {"ok": True, "enabled": True, "updated": updated, "status_changed": changed}
    except Exception as exc:
        return {"ok": False, "enabled": True, "updated": updated, "status_changed": changed, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def _summary_blank() -> dict:
    return {
        "signals": 0,
        "activated": 0,
        "target_hit": 0,
        "above_target": 0,
        "stopped": 0,
        "partial_gain": 0,
        "appeared_not_activated": 0,
        "disappeared_before_activation": 0,
        "plan_broken": 0,
        "ongoing": 0,
        "avg_minutes_to_activation": 0.0,
        "avg_minutes_to_target": 0.0,
        "avg_minutes_to_stop": 0.0,
        "avg_max_gain_pct": 0.0,
        "avg_max_loss_pct": 0.0,
    }


def _summarize_bucket(rows: list[dict]) -> dict:
    out = _summary_blank()
    out["signals"] = len(rows)
    activation_minutes = []
    target_minutes = []
    stop_minutes = []
    gains = []
    losses = []
    for item in rows:
        status = str(item.get("status", "") or "")
        activated = bool(str(item.get("activated_at", "") or ""))
        if activated:
            out["activated"] += 1
        if status == "target_hit":
            out["target_hit"] += 1
        if status == "above_target":
            out["above_target"] += 1
        if status == "stopped":
            out["stopped"] += 1
        if status == "disappeared_before_activation":
            out["disappeared_before_activation"] += 1
        if status == "plan_broken_before_activation":
            out["plan_broken"] += 1
        if not activated:
            out["appeared_not_activated"] += 1
        max_gain = _safe_float(item.get("max_gain_pct", 0))
        if activated and status not in {"target_hit", "above_target", "stopped"} and max_gain > 0:
            out["partial_gain"] += 1
        if status in {"pending", "activated"}:
            out["ongoing"] += 1
        if _safe_float(item.get("minutes_to_activation", 0)) > 0:
            activation_minutes.append(_safe_float(item.get("minutes_to_activation")))
        if _safe_float(item.get("minutes_to_target", 0)) > 0:
            target_minutes.append(_safe_float(item.get("minutes_to_target")))
        if _safe_float(item.get("minutes_to_stop", 0)) > 0:
            stop_minutes.append(_safe_float(item.get("minutes_to_stop")))
        if max_gain != 0:
            gains.append(max_gain)
        max_loss = _safe_float(item.get("max_loss_pct", 0))
        if max_loss != 0:
            losses.append(max_loss)

    def avg(values: list[float]) -> float:
        return safe_round(sum(values) / len(values), 2) if values else 0.0

    out["avg_minutes_to_activation"] = avg(activation_minutes)
    out["avg_minutes_to_target"] = avg(target_minutes)
    out["avg_minutes_to_stop"] = avg(stop_minutes)
    out["avg_max_gain_pct"] = avg(gains)
    out["avg_max_loss_pct"] = avg(losses)
    return out


def _tag_distribution(rows: list[dict], field: str, only_bad: bool = False) -> list[dict]:
    counts: dict[str, int] = {}
    base_rows = rows
    if only_bad:
        base_rows = [x for x in rows if str(x.get("status", "") or "") in {"stopped", "disappeared_before_activation", "plan_broken_before_activation"}]
    total = max(1, len(base_rows))
    for item in base_rows:
        for tag in _json_loads(item.get(field, "[]"), []) or []:
            counts[str(tag)] = counts.get(str(tag), 0) + 1
    dist = [
        {"reason": tag, "count": count, "pct": safe_round(count / total * 100.0, 1)}
        for tag, count in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return dist[:12]


def _rule_based_weekly_ar(groups: dict, risk_reasons: dict, success_reasons: dict) -> str:
    strong = groups.get("strong", {}).get("summary", {})
    cautious = groups.get("cautious", {}).get("summary", {})
    gray = groups.get("gray_strong", {}).get("summary", {})
    lines = []
    lines.append("ملخص Tracking Intelligence V1: هذا تقرير أداء بعدي فقط، ولا يغيّر شروط الدخول أو الفلتر الشرعي.")
    if strong.get("signals", 0):
        lines.append(
            f"دخول قوي: {strong.get('signals', 0)} إشارة، تفعلت {strong.get('activated', 0)}، وصلت/تجاوزت الهدف {strong.get('target_hit', 0) + strong.get('above_target', 0)}، ضربت الوقف {strong.get('stopped', 0)}، واختفت قبل التفعيل {strong.get('disappeared_before_activation', 0)}."
        )
    if cautious.get("signals", 0):
        lines.append(
            f"دخول بحذر: {cautious.get('signals', 0)} إشارة، تفعلت {cautious.get('activated', 0)}، وصلت/تجاوزت الهدف {cautious.get('target_hit', 0) + cautious.get('above_target', 0)}، وضربت الوقف {cautious.get('stopped', 0)}."
        )
    if gray.get("signals", 0):
        lines.append(
            f"الرمادي القوي فنيًا: {gray.get('signals', 0)} إشارة. هذه قراءة فنية فقط وليست توصية شرعية."
        )

    top_risk = (risk_reasons.get("strong") or [])[:3]
    if top_risk:
        joined = "، ".join([f"{x['reason']} ({x['pct']}%)" for x in top_risk])
        lines.append(f"أبرز نمط سلبي في دخول قوي: {joined}.")
    top_success = (success_reasons.get("strong") or [])[:3]
    if top_success:
        joined = "، ".join([f"{x['reason']} ({x['pct']}%)" for x in top_success])
        lines.append(f"أبرز نمط إيجابي في دخول قوي: {joined}.")
    if top_risk:
        lines.append("القاعدة المقترحة لاحقًا: لا نغيّر الفلاتر الآن؛ ننتظر عينة أسبوعين إلى شهر، ثم نختبر أكثر سبب متكرر قبل إدخاله كتنبيه أو خفض جاهزية فقط.")
    return "\n".join(lines)


def build_tracking_weekly_report(week_key: str | None = None, include_items: bool = False) -> dict:
    if not (SQLITE_ENABLED and TRACKING_INTELLIGENCE_ENABLED):
        return {"ok": True, "enabled": False, "message": "Tracking Intelligence غير مفعّل."}
    init_tracking_intelligence_db()
    week_key = str(week_key or get_performance_week_key())
    try:
        with _connect() as conn:
            rows = [_row_to_dict(r) for r in conn.execute("SELECT * FROM tracking_signals WHERE week_key=? ORDER BY first_seen_at DESC", (week_key,)).fetchall()]
            events_count_row = conn.execute("SELECT COUNT(*) AS c FROM tracking_signal_events WHERE week_key=?", (week_key,)).fetchone()
        week_start = rows[0].get("week_start", "") if rows else ""
        week_end = rows[0].get("week_end", "") if rows else ""
        groups: dict[str, dict] = {}
        risk_reasons: dict[str, list[dict]] = {}
        success_reasons: dict[str, list[dict]] = {}
        for bucket in ["strong", "cautious", "gray_strong"]:
            bucket_rows = [x for x in rows if str(x.get("signal_bucket", "") or "") == bucket]
            groups[bucket] = {
                "label": BUCKET_LABELS.get(bucket, bucket),
                "summary": _summarize_bucket(bucket_rows),
            }
            if include_items:
                groups[bucket]["items"] = [
                    {
                        "symbol": x.get("symbol"),
                        "status": x.get("status"),
                        "status_label": x.get("status_label"),
                        "first_seen_at": x.get("first_seen_at"),
                        "activated_at": x.get("activated_at"),
                        "closed_at": x.get("closed_at"),
                        "snapshot_price": safe_round(x.get("snapshot_price")),
                        "entry_price": safe_round(x.get("entry_price")),
                        "target_price": safe_round(x.get("target_price")),
                        "stop_loss": safe_round(x.get("stop_loss")),
                        "max_price_after": safe_round(x.get("max_price_after")),
                        "min_price_after": safe_round(x.get("min_price_after")),
                        "max_gain_pct": safe_round(x.get("max_gain_pct")),
                        "risk_tags": _json_loads(x.get("risk_tags_json", "[]"), []),
                        "success_tags": _json_loads(x.get("success_tags_json", "[]"), []),
                    }
                    for x in bucket_rows[:250]
                ]
            risk_reasons[bucket] = _tag_distribution(bucket_rows, "risk_tags_json", only_bad=True)
            success_reasons[bucket] = _tag_distribution(bucket_rows, "success_tags_json", only_bad=False)

        summary_ar = _rule_based_weekly_ar(groups, risk_reasons, success_reasons)
        payload = {
            "ok": True,
            "enabled": True,
            "version": "tracking_intelligence_v1",
            "week_key": week_key,
            "week_start": week_start,
            "week_end": week_end,
            "generated_at": _now_text(),
            "total_signals": len(rows),
            "events_count": int(events_count_row["c"] if events_count_row else 0),
            "groups": groups,
            "risk_reasons": risk_reasons,
            "success_reasons": success_reasons,
            "ai_style_summary_ar": summary_ar,
            "notes": {
                "safe_mode": "لا يغيّر التصنيف أو الفلاتر أو الأسعار؛ يحلل النتائج فقط.",
                "gray_strong": "فئة رمادية للتقييم الفني فقط وليست توصية شرعية.",
                "watchlist": "المراقبة لا تدخل الربح والخسارة الرئيسي في V1.",
            },
        }
        try:
            with _LOCK:
                with _connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO tracking_weekly_insights(week_key, generated_at, generated_at_ts, summary_ar, payload_json)
                        VALUES(?, ?, ?, ?, ?)
                        ON CONFLICT(week_key) DO UPDATE SET
                            generated_at=excluded.generated_at,
                            generated_at_ts=excluded.generated_at_ts,
                            summary_ar=excluded.summary_ar,
                            payload_json=excluded.payload_json
                        """,
                        (week_key, payload["generated_at"], _now_ts(), summary_ar, _json_dumps(payload)),
                    )
                    conn.commit()
        except Exception:
            pass
        return payload
    except Exception as exc:
        return {"ok": False, "enabled": True, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}



EXPORT_COLUMNS = [
    "symbol", "signal_bucket", "signal_label", "status", "status_label", "outcome_group",
    "first_seen_at", "last_seen_at", "disappeared_at", "activated_at", "target_hit_at", "stopped_at", "closed_at",
    "snapshot_price", "current_price", "entry_price", "target_price", "target_2_price", "stop_loss",
    "risk_pct", "rr_ratio", "quality_score", "execution_readiness_score", "signal_strength",
    "entry_distance_pct", "is_near_entry", "is_late_above_entry", "is_entry_far",
    "plan_family", "signal_reason", "nearest_support", "nearest_support_strength", "nearest_support_distance_pct",
    "nearest_resistance", "nearest_resistance_strength", "nearest_resistance_distance_pct",
    "year_high", "ath_high", "distance_to_52w_high_pct", "distance_to_ath_pct",
    "liquidity_ratio", "volume_ratio", "volatility_pct", "market_phase", "market_phase_label",
    "market_support_label", "sector_support_label", "market_sector_score", "market_sector_live_label",
    "benchmark_symbol", "sector_etf_symbol", "spy_change_pct", "qqq_change_pct", "sector_change_pct",
    "max_price_after", "min_price_after", "max_gain_pct", "max_loss_pct",
    "minutes_to_activation", "minutes_to_target", "minutes_to_stop", "risk_tags", "success_tags",
]


def build_tracking_weekly_brief(week_key: str | None = None, include_items: bool = True) -> str:
    """Arabic plain-text report designed to be copied into ChatGPT.

    This is calculated on demand only. It does not run in the price loop and does
    not fetch fresh market data.
    """
    report = build_tracking_weekly_report(week_key=week_key, include_items=include_items)
    if not report.get("enabled", True):
        return "Tracking Intelligence غير مفعّل."
    if not report.get("ok", False):
        return f"تعذر إنشاء تقرير Tracking Intelligence: {report.get('error', 'unknown_error')}"

    lines: list[str] = []
    lines.append("تقرير Tracking Intelligence V1")
    lines.append(f"الأسبوع: {report.get('week_key', '')}")
    if report.get("week_start") or report.get("week_end"):
        lines.append(f"الفترة: {report.get('week_start', '')} إلى {report.get('week_end', '')}")
    lines.append(f"وقت إنشاء التقرير: {report.get('generated_at', '')}")
    lines.append("")
    lines.append("ملاحظة: هذا تقرير أداء بعدي فقط؛ لا يغيّر شروط الدخول أو الفلتر الشرعي أو منطق الأسعار.")
    lines.append("")

    groups = report.get("groups", {}) if isinstance(report.get("groups"), dict) else {}
    for bucket in ["strong", "cautious", "gray_strong"]:
        g = groups.get(bucket, {}) if isinstance(groups.get(bucket), dict) else {}
        summary = g.get("summary", {}) if isinstance(g.get("summary"), dict) else {}
        label = g.get("label") or BUCKET_LABELS.get(bucket, bucket)
        lines.append(f"{label}:")
        lines.append(f"- عدد الإشارات: {summary.get('signals', 0)}")
        lines.append(f"- تفعلت: {summary.get('activated', 0)}")
        lines.append(f"- وصلت الهدف: {summary.get('target_hit', 0)}")
        lines.append(f"- تجاوزت الهدف: {summary.get('above_target', 0)}")
        lines.append(f"- ضربت الوقف: {summary.get('stopped', 0)}")
        lines.append(f"- صعدت أقل من الهدف: {summary.get('partial_gain', 0)}")
        lines.append(f"- ظهرت ولم تتفعل: {summary.get('appeared_not_activated', 0)}")
        lines.append(f"- اختفت قبل التفعيل: {summary.get('disappeared_before_activation', 0)}")
        lines.append(f"- كسر الخطة قبل التفعيل: {summary.get('plan_broken', 0)}")
        lines.append(f"- مستمرة: {summary.get('ongoing', 0)}")
        if summary.get("avg_minutes_to_target", 0):
            lines.append(f"- متوسط وقت الوصول للهدف بالدقائق: {summary.get('avg_minutes_to_target', 0)}")
        if summary.get("avg_minutes_to_stop", 0):
            lines.append(f"- متوسط وقت ضرب الوقف بالدقائق: {summary.get('avg_minutes_to_stop', 0)}")
        if bucket == "gray_strong":
            lines.append("- تنبيه: هذه فئة تقييم فني فقط وليست توصية شرعية.")
        lines.append("")

    def append_reasons(title: str, data: list[dict]) -> None:
        lines.append(title)
        if not data:
            lines.append("- لا توجد بيانات كافية بعد.")
        else:
            for idx, item in enumerate(data[:8], 1):
                lines.append(f"{idx}. {item.get('reason', '')}: {item.get('pct', 0)}% ({item.get('count', 0)} مرة)")
        lines.append("")

    risk = report.get("risk_reasons", {}) if isinstance(report.get("risk_reasons"), dict) else {}
    success = report.get("success_reasons", {}) if isinstance(report.get("success_reasons"), dict) else {}
    append_reasons("أسباب خسائر/تعثر دخول قوي:", risk.get("strong", []) or [])
    append_reasons("أسباب نجاح دخول قوي:", success.get("strong", []) or [])
    append_reasons("أسباب خسائر/تعثر دخول بحذر:", risk.get("cautious", []) or [])
    append_reasons("أسباب نجاح دخول بحذر:", success.get("cautious", []) or [])

    summary_ar = str(report.get("ai_style_summary_ar", "") or "").strip()
    if summary_ar:
        lines.append("ملخص عربي سريع:")
        lines.append(summary_ar)
        lines.append("")

    if include_items:
        lines.append("أهم الإشارات للمراجعة:")
        any_item = False
        for bucket in ["strong", "cautious", "gray_strong"]:
            g = groups.get(bucket, {}) if isinstance(groups.get(bucket), dict) else {}
            items = g.get("items", []) if isinstance(g.get("items"), list) else []
            if not items:
                continue
            any_item = True
            lines.append(f"{g.get('label') or BUCKET_LABELS.get(bucket, bucket)}:")
            for item in items[:20]:
                risk_tags = item.get("risk_tags") or []
                success_tags = item.get("success_tags") or []
                risk_text = "، ".join([str(x) for x in risk_tags[:3]]) if risk_tags else "-"
                success_text = "، ".join([str(x) for x in success_tags[:3]]) if success_tags else "-"
                lines.append(
                    f"- {item.get('symbol', '')}: {item.get('status_label', item.get('status', ''))} | "
                    f"ظهور {item.get('first_seen_at', '')} | دخول {item.get('entry_price', 0)} | "
                    f"هدف {item.get('target_price', 0)} | وقف {item.get('stop_loss', 0)} | "
                    f"أعلى ربح {item.get('max_gain_pct', 0)}% | أسباب سلبية: {risk_text} | أسباب إيجابية: {success_text}"
                )
            lines.append("")
        if not any_item:
            lines.append("- لا توجد إشارات مسجلة في هذا الأسبوع بعد.")
            lines.append("")

    lines.append("ما أحتاجه منك يا ChatGPT: اقرأ هذا التقرير وحدد الأنماط المتكررة في خسائر ونجاحات دخول قوي/بحذر، واقترح تحسينات لاحقة بدون تعديل مباشر للفلاتر الآن.")
    return "\n".join(lines).strip() + "\n"


def _tracking_export_rows(week_key: str | None = None, limit: int = 5000) -> tuple[str, list[dict]]:
    if not (SQLITE_ENABLED and TRACKING_INTELLIGENCE_ENABLED):
        return str(week_key or get_performance_week_key()), []
    init_tracking_intelligence_db()
    wk = str(week_key or get_performance_week_key())
    lim = max(1, min(int(_safe_int(limit, 5000) or 5000), 20000))
    with _connect() as conn:
        rows = [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM tracking_signals WHERE week_key=? ORDER BY first_seen_at DESC LIMIT ?",
            (wk, lim),
        ).fetchall()]
    for row in rows:
        row["risk_tags"] = _json_loads(row.get("risk_tags_json", "[]"), [])
        row["success_tags"] = _json_loads(row.get("success_tags_json", "[]"), [])
        # Keep exports readable and reasonably small. The raw snapshot remains in SQLite.
        row.pop("snapshot_json", None)
        row.pop("risk_tags_json", None)
        row.pop("success_tags_json", None)
    return wk, rows


def export_tracking_json(week_key: str | None = None, include_items: bool = True, limit: int = 5000) -> dict:
    """JSON export for archiving or sharing with ChatGPT; generated on demand."""
    if not (SQLITE_ENABLED and TRACKING_INTELLIGENCE_ENABLED):
        return {"ok": True, "enabled": False, "message": "Tracking Intelligence غير مفعّل."}
    wk, rows = _tracking_export_rows(week_key=week_key, limit=limit)
    return {
        "ok": True,
        "enabled": True,
        "version": "tracking_intelligence_v1_export",
        "week_key": wk,
        "generated_at": _now_text(),
        "rows_count": len(rows),
        "report": build_tracking_weekly_report(week_key=wk, include_items=include_items),
        "signals": rows,
        "notes": {
            "safe_mode": "تصدير عند الطلب فقط؛ لا يضيف API calls ولا يغير التصنيف أو الأسعار.",
            "gray_strong": "فئة رمادية للتقييم الفني فقط وليست توصية شرعية.",
        },
    }


def export_tracking_csv(week_key: str | None = None, limit: int = 5000) -> str:
    """CSV export string for Excel/Sheets; generated on demand."""
    wk, rows = _tracking_export_rows(week_key=week_key, limit=limit)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        clean = dict(row)
        clean["risk_tags"] = " | ".join([str(x) for x in (row.get("risk_tags") or [])])
        clean["success_tags"] = " | ".join([str(x) for x in (row.get("success_tags") or [])])
        writer.writerow({key: clean.get(key, "") for key in EXPORT_COLUMNS})
    return output.getvalue()
