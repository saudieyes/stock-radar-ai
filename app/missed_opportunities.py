"""Missed Opportunities Review V1 for Stock Radar AI.

Backend/diagnostic-only layer. It records what the radar/source saw and, on
request, compares that record with broad-market weekly movers. It does not
change scoring, Sharia filtering, pricing, UI ranking, or live refresh behavior.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import scanner as _scanner
from .performance_tracker import get_performance_week_key, get_performance_week_window
from .sqlite_store import SQLITE_DB_PATH, SQLITE_ENABLED
from .settings import POLYGON_API_KEY, HTTP_SESSION
from .utils import safe_round, to_float, normalize_symbol_text

MISSED_OPPORTUNITIES_ENABLED = str(os.getenv("MISSED_OPPORTUNITIES_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
MISSED_WEEKLY_GAIN_THRESHOLDS = [10, 15, 20, 30]
MISSED_DEFAULT_GAIN_THRESHOLD = float(os.getenv("MISSED_DEFAULT_GAIN_THRESHOLD", "20") or 20)
MISSED_MOVER_LIMIT = int(float(os.getenv("MISSED_MOVER_LIMIT", "250") or 250))
MISSED_DEEP_CAUSE_LIMIT = int(float(os.getenv("MISSED_DEEP_CAUSE_LIMIT", "60") or 60))
MISSED_NEWS_LOOKUP_LIMIT = int(float(os.getenv("MISSED_NEWS_LOOKUP_LIMIT", "35") or 35))
MISSED_MIN_BASELINE_PRICE = float(os.getenv("MISSED_MIN_BASELINE_PRICE", "0.75") or 0.75)
MISSED_MIN_DOLLAR_VOLUME = float(os.getenv("MISSED_MIN_DOLLAR_VOLUME", "250000") or 250000)
MISSED_LATE_PROMOTION_PCT = float(os.getenv("MISSED_LATE_PROMOTION_PCT", "12") or 12)
MISSED_BIG_MOVE_PCT = float(os.getenv("MISSED_BIG_MOVE_PCT", "20") or 20)
MISSED_TIMELINE_ITEM_LIMIT = int(float(os.getenv("MISSED_TIMELINE_ITEM_LIMIT", "120") or 120))

# Pre-Move Evidence is intentionally light. It stores only first snapshots per
# symbol/event/milestone for the active week so Railway SQLite does not grow
# from every live refresh or every scan repetition. It is diagnostic-only and
# never changes radar scoring, Sharia filtering, or displayed opportunities.
MISSED_EVIDENCE_ENABLED = str(os.getenv("MISSED_EVIDENCE_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
MISSED_EVIDENCE_SOURCE_RANK_LIMIT = int(float(os.getenv("MISSED_EVIDENCE_SOURCE_RANK_LIMIT", "500") or 500))
MISSED_EVIDENCE_DEEP_RANK_LIMIT = int(float(os.getenv("MISSED_EVIDENCE_DEEP_RANK_LIMIT", "280") or 280))
MISSED_EVIDENCE_MILESTONES = [3, 5, 10, 15, 20, 30, 50, 100]

_LOCK = threading.RLock()
_INITIALIZED = False
NY_TZ = ZoneInfo("America/New_York")

CATEGORY_RANK = {
    "دخول قوي": 50,
    "دخول بحذر": 40,
    "دخول قوي غير محسوم شرعيًا": 35,
    "دخول بحذر غير محسوم شرعيًا": 32,
    "تهيئة قوية غير محسومة شرعيًا": 30,
    "قوي لكن شرعيته غير محسومة": 30,
    "تهيئة قوية قبل الافتتاح": 28,
    "مراقبة": 20,
}
CATEGORY_KEYS = {
    "دخول قوي": "strong",
    "دخول بحذر": "cautious",
    "دخول قوي غير محسوم شرعيًا": "gray_strong",
    "دخول بحذر غير محسوم شرعيًا": "gray_cautious",
    "تهيئة قوية غير محسومة شرعيًا": "gray_setup",
    "قوي لكن شرعيته غير محسومة": "gray_strong",
    "تهيئة قوية قبل الافتتاح": "premarket_setup",
    "مراقبة": "watch",
}


def _enabled() -> bool:
    return bool(SQLITE_ENABLED and MISSED_OPPORTUNITIES_ENABLED)


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


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}"


def _json_loads(value: Any, default: Any = None) -> Any:
    try:
        if value is None or value == "":
            return default
        return json.loads(str(value))
    except Exception:
        return default


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


def _clean_symbol(value: Any) -> str:
    return normalize_symbol_text(value)[:24]


def _clean_text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _first_float(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        val = _safe_float((row or {}).get(key), default=0.0)
        if val > 0:
            return val
    return default


def _first_text(row: dict, keys: list[str], default: str = "", limit: int = 500) -> str:
    for key in keys:
        val = (row or {}).get(key)
        if val is not None and str(val).strip():
            return _clean_text(val, limit)
    return _clean_text(default, limit)


def _normalize_pct_value(value: Any) -> float:
    """Return percentage-like values without aggressive scaling.

    Earlier versions multiplied every value between -1.5 and +1.5 by 100 to
    handle decimal fractions.  After Dynamic Discovery started storing several
    fields already as real percentages, that rule could turn +117.8% into
    impossible +11776% through mixed source metrics.  For reporting, it is safer
    to preserve the value and mark impossible numbers as unknown later.
    """
    return _safe_float(value, 0.0)


def _sanitize_timeline_gain_pct(value: Any) -> float:
    """Return a safe percentage for timeline storage.

    Source diagnostics sometimes mix decimal percentages and full percentages.
    If a bad multiplier slips through, one symbol can appear as +11776% while
    the confirmed weekly mover is near +116%.  Such values are not useful for
    promotion timing, so we store them as unknown (0) rather than misleading the report.
    """
    val = _normalize_pct_value(value)
    try:
        if val != val or val in (float("inf"), float("-inf")):
            return 0.0
    except Exception:
        return 0.0
    if abs(val) > 1000:
        return 0.0
    return val


def _timeline_gain_or_none(value: Any, price: Any = None) -> float | None:
    """Return timeline gain as a number, or None when it is unavailable/untrusted."""
    val = _safe_float(value, 0.0)
    if abs(val) > 1000:
        return None
    # When both price and percent are zero, this usually means we only knew the
    # symbol was in the deep universe, not its live move at that exact stage.
    if abs(val) < 0.00001 and _safe_float(price, 0.0) <= 0:
        return None
    return val


def _format_pct_or_na(value: Any, digits: int = 1) -> str:
    val = _safe_float(value, 0.0) if value is not None else None
    if val is None:
        return "غير متوفر"
    return f"{safe_round(val, digits)}%"


def _first_pct(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        if key in (row or {}):
            val = _normalize_pct_value((row or {}).get(key))
            if val != 0:
                return val
    return default


def _timeline_event_type_for_category(category_key: str, label: str = "") -> str:
    key = str(category_key or "").lower()
    text = str(label or "")
    if "strong" in key or "دخول قوي" in text:
        return "strong"
    if "cautious" in key or "بحذر" in text:
        return "cautious"
    if "gray" in key or "غير محسوم" in text or "رمادي" in text:
        return "gray"
    if "watch" in key or "مراقبة" in text:
        return "watch"
    return "display_other"


def _record_timeline_event(
    conn: sqlite3.Connection,
    week_key: str,
    symbol: str,
    event_type: str,
    *,
    seen_at: str,
    price: float = 0.0,
    gain_pct: float = 0.0,
    rank: int = 0,
    category: str = "",
    category_key: str = "",
    market_phase: str = "",
    source_reasons: list | None = None,
    metrics: dict | None = None,
    updated_ts: float | None = None,
) -> None:
    """Persist first/last timeline points for source → watch/cautious/strong promotion analysis."""
    sym = _clean_symbol(symbol)
    if not sym or not event_type:
        return
    now_ts = _safe_float(updated_ts, _now_ts())
    rank_i = _safe_int(rank, 0)
    price_f = _safe_float(price, 0.0)
    gain_f = _sanitize_timeline_gain_pct(gain_pct)
    conn.execute(
        """
        INSERT INTO missed_symbol_timeline(
            week_key, symbol, event_type, first_seen_at, last_seen_at, times_seen,
            first_price, last_price, first_gain_pct, last_gain_pct, max_gain_pct,
            first_rank, best_rank, category, category_key, market_phase,
            source_reasons_json, metrics_json, updated_ts
        ) VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(week_key, symbol, event_type) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            times_seen=missed_symbol_timeline.times_seen + 1,
            last_price=CASE WHEN excluded.last_price>0 THEN excluded.last_price ELSE missed_symbol_timeline.last_price END,
            last_gain_pct=CASE WHEN excluded.last_gain_pct!=0 THEN excluded.last_gain_pct ELSE missed_symbol_timeline.last_gain_pct END,
            max_gain_pct=MAX(missed_symbol_timeline.max_gain_pct, excluded.max_gain_pct),
            best_rank=CASE
                WHEN missed_symbol_timeline.best_rank=0 THEN excluded.best_rank
                WHEN excluded.best_rank=0 THEN missed_symbol_timeline.best_rank
                WHEN excluded.best_rank < missed_symbol_timeline.best_rank THEN excluded.best_rank
                ELSE missed_symbol_timeline.best_rank
            END,
            category=CASE WHEN excluded.category!='' THEN excluded.category ELSE missed_symbol_timeline.category END,
            category_key=CASE WHEN excluded.category_key!='' THEN excluded.category_key ELSE missed_symbol_timeline.category_key END,
            market_phase=CASE WHEN excluded.market_phase!='' THEN excluded.market_phase ELSE missed_symbol_timeline.market_phase END,
            source_reasons_json=CASE WHEN excluded.source_reasons_json!='[]' THEN excluded.source_reasons_json ELSE missed_symbol_timeline.source_reasons_json END,
            metrics_json=CASE WHEN excluded.metrics_json!='{}' THEN excluded.metrics_json ELSE missed_symbol_timeline.metrics_json END,
            updated_ts=excluded.updated_ts
        """,
        (
            week_key, sym, str(event_type), seen_at, seen_at,
            price_f, price_f, gain_f, gain_f, gain_f,
            rank_i, rank_i, _clean_text(category, 120), _clean_text(category_key, 80), _clean_text(market_phase, 120),
            _json_dumps(source_reasons or []), _json_dumps(metrics or {}), now_ts,
        ),
    )
    try:
        _record_pre_move_evidence(
            conn, week_key, sym, str(event_type),
            seen_at=seen_at, price=price_f, gain_pct=gain_f, rank=rank_i,
            category=category, category_key=category_key, market_phase=market_phase,
            source_reasons=source_reasons or [], metrics=metrics or {}, updated_ts=now_ts,
        )
    except Exception:
        pass


# --- Pre-Move Evidence snapshots -------------------------------------------------
# These helpers are read-only diagnostics. They intentionally record first
# occurrences and gain milestones only, so Railway SQLite is not burdened by
# every scan/live-refresh repetition.

def _snapshot_metric_pack(row: dict | None, metrics: dict | None = None) -> dict:
    row = row or {}
    metrics = metrics or {}
    merged = {}
    try:
        if isinstance(metrics, dict):
            merged.update(metrics)
        if isinstance(row, dict):
            merged.update(row)
    except Exception:
        merged = row or metrics or {}

    def f(keys: list[str], default: float = 0.0) -> float:
        return _first_float(merged, keys, default=default)

    def t(keys: list[str], default: str = "", limit: int = 140) -> str:
        return _first_text(merged, keys, default=default, limit=limit)

    return {
        "quality": f(["quality_score", "quality", "core_quality", "quality_core_score"]),
        "execution": f(["execution_readiness_score", "execution", "execution_score", "execution_layer_score"]),
        "display_rank_score": f(["display_rank_score", "rank_score"]),
        "volume": f(["volume", "live_volume", "fmp_volume", "current_volume"]),
        "volume_ratio": f(["volume_ratio", "relative_volume", "rel_volume", "rv_ratio"]),
        "effective_volume_ratio": f(["effective_volume_ratio", "intraday_volume_ratio", "projected_volume_ratio"]),
        "dollar_volume": f(["dollar_volume", "dollar_vol", "liquidity_dollar_volume"]),
        "vwap_proxy": f(["vwap_proxy", "vwap", "current_vwap"]),
        "entry_price": f(["entry_price", "entry", "planned_entry", "buy_above"]),
        "stop_loss": f(["stop_loss", "stop", "stop_price"]),
        "target_price": f(["target_price", "target", "target_1", "tp1"]),
        "risk_pct": f(["risk_pct", "plan_risk_pct"]),
        "rr_1": f(["rr_1", "risk_reward", "reward_risk", "rr"]),
        "nearest_support": f(["nearest_support", "support", "support_price"]),
        "nearest_support_strength": t(["nearest_support_strength", "support_strength"], limit=80),
        "nearest_support_distance_pct": f(["nearest_support_distance_pct", "support_distance_pct", "distance_to_support_pct"]),
        "nearest_resistance": f(["nearest_resistance", "resistance", "resistance_price"]),
        "nearest_resistance_strength": t(["nearest_resistance_strength", "resistance_strength"], limit=80),
        "nearest_resistance_distance_pct": f(["nearest_resistance_distance_pct", "resistance_distance_pct", "distance_to_resistance_pct"]),
        "distance_to_52w_high_pct": f(["distance_to_52w_high_pct", "near_52w_high_pct", "distance_52w_high_pct"]),
        "distance_to_ath_pct": f(["distance_to_ath_pct", "near_ath_pct", "distance_ath_pct"]),
        "breakout_quality": t(["breakout_quality", "breakout_quality_label"], limit=80),
        "plan_family": t(["plan_family", "setup_type", "plan_type"], limit=80),
        "strong_entry_tier": t(["strong_entry_tier"], limit=80),
        "strong_entry_tier_label": t(["strong_entry_tier_label"], limit=120),
        "pattern_risk_score": f(["pattern_risk_score"]),
        "pattern_risk_label": t(["pattern_risk_label"], limit=140),
        "liquidity_persistence_score": f(["liquidity_persistence_score"]),
        "liquidity_persistence_label": t(["liquidity_persistence_label"], limit=140),
        "post_activation_guard_score": f(["post_activation_guard_score"]),
        "post_activation_guard_label": t(["post_activation_guard_label"], limit=140),
        "no_chase_guard_status": t(["no_chase_guard_status"], limit=80),
        "no_chase_guard_label": t(["no_chase_guard_label"], limit=140),
        "risk_tags": merged.get("risk_tags") or merged.get("risk_tags_json") or [],
    }


def _hot_source_tags(source_reasons: list | None, metrics: dict | None = None) -> bool:
    text = " ".join([str(x) for x in (source_reasons or [])]) + " " + json.dumps(metrics or {}, ensure_ascii=False)[:1000]
    needles = ["fmp", "top", "mover", "volume", "runner", "near_high", "gap", "live", "spike", "اختراق", "سيولة", "حجم", "متحرك"]
    low = text.lower()
    return any(n.lower() in low for n in needles)


def _should_store_evidence(event_type: str, gain_pct: float, rank: int, source_reasons: list | None, metrics: dict | None) -> bool:
    if not MISSED_EVIDENCE_ENABLED:
        return False
    et = str(event_type or "")
    gain = _safe_float(gain_pct, 0.0)
    rank_i = _safe_int(rank, 0)
    if et == "source":
        return bool((rank_i and rank_i <= MISSED_EVIDENCE_SOURCE_RANK_LIMIT) or gain >= 3 or _hot_source_tags(source_reasons, metrics))
    if et == "deep_universe":
        return bool((rank_i and rank_i <= MISSED_EVIDENCE_DEEP_RANK_LIMIT) or gain >= 3)
    return True


def _record_pre_move_snapshot(
    conn: sqlite3.Connection,
    week_key: str,
    symbol: str,
    snapshot_key: str,
    snapshot_type: str,
    *,
    seen_at: str,
    price: float = 0.0,
    gain_pct: float = 0.0,
    rank: int = 0,
    category: str = "",
    category_key: str = "",
    market_phase: str = "",
    source_reasons: list | None = None,
    metrics: dict | None = None,
    updated_ts: float | None = None,
) -> None:
    sym = _clean_symbol(symbol)
    key = _clean_text(snapshot_key, 80)
    if not sym or not key:
        return
    pack = _snapshot_metric_pack({"price": price, "gain_pct": gain_pct, "rank": rank}, metrics or {})
    now_ts = _safe_float(updated_ts, _now_ts())
    gain_f = _sanitize_timeline_gain_pct(gain_pct)
    reasons = source_reasons or []
    risk_tags = pack.get("risk_tags") or []
    conn.execute(
        """
        INSERT INTO missed_pre_move_snapshots(
            week_key, symbol, snapshot_key, snapshot_type, first_seen_at, last_seen_at, times_seen,
            price, gain_pct, rank, category, category_key, market_phase,
            quality, execution, display_rank_score, volume, volume_ratio, effective_volume_ratio,
            dollar_volume, vwap_proxy, entry_price, stop_loss, target_price, risk_pct, rr_1,
            nearest_support, nearest_support_strength, nearest_support_distance_pct,
            nearest_resistance, nearest_resistance_strength, nearest_resistance_distance_pct,
            distance_to_52w_high_pct, distance_to_ath_pct, breakout_quality, plan_family,
            source_reasons_json, risk_tags_json, metrics_json, updated_ts
        ) VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(week_key, symbol, snapshot_key) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            times_seen=missed_pre_move_snapshots.times_seen + 1,
            price=CASE WHEN missed_pre_move_snapshots.price<=0 AND excluded.price>0 THEN excluded.price ELSE missed_pre_move_snapshots.price END,
            gain_pct=CASE WHEN missed_pre_move_snapshots.gain_pct=0 AND excluded.gain_pct!=0 THEN excluded.gain_pct ELSE missed_pre_move_snapshots.gain_pct END,
            rank=CASE
                WHEN missed_pre_move_snapshots.rank=0 THEN excluded.rank
                WHEN excluded.rank=0 THEN missed_pre_move_snapshots.rank
                WHEN excluded.rank < missed_pre_move_snapshots.rank THEN excluded.rank
                ELSE missed_pre_move_snapshots.rank
            END,
            category=CASE WHEN missed_pre_move_snapshots.category='' THEN excluded.category ELSE missed_pre_move_snapshots.category END,
            category_key=CASE WHEN missed_pre_move_snapshots.category_key='' THEN excluded.category_key ELSE missed_pre_move_snapshots.category_key END,
            market_phase=CASE WHEN excluded.market_phase!='' THEN excluded.market_phase ELSE missed_pre_move_snapshots.market_phase END,
            quality=MAX(missed_pre_move_snapshots.quality, excluded.quality),
            execution=MAX(missed_pre_move_snapshots.execution, excluded.execution),
            display_rank_score=MAX(missed_pre_move_snapshots.display_rank_score, excluded.display_rank_score),
            source_reasons_json=CASE WHEN missed_pre_move_snapshots.source_reasons_json='[]' THEN excluded.source_reasons_json ELSE missed_pre_move_snapshots.source_reasons_json END,
            risk_tags_json=CASE WHEN missed_pre_move_snapshots.risk_tags_json='[]' THEN excluded.risk_tags_json ELSE missed_pre_move_snapshots.risk_tags_json END,
            metrics_json=CASE WHEN missed_pre_move_snapshots.metrics_json='{}' THEN excluded.metrics_json ELSE missed_pre_move_snapshots.metrics_json END,
            updated_ts=excluded.updated_ts
        """,
        (
            week_key, sym, key, _clean_text(snapshot_type, 80), seen_at, seen_at,
            _safe_float(price), gain_f, _safe_int(rank, 0), _clean_text(category, 120), _clean_text(category_key, 80), _clean_text(market_phase, 120),
            pack["quality"], pack["execution"], pack["display_rank_score"], pack["volume"], pack["volume_ratio"], pack["effective_volume_ratio"],
            pack["dollar_volume"], pack["vwap_proxy"], pack["entry_price"], pack["stop_loss"], pack["target_price"], pack["risk_pct"], pack["rr_1"],
            pack["nearest_support"], pack["nearest_support_strength"], pack["nearest_support_distance_pct"],
            pack["nearest_resistance"], pack["nearest_resistance_strength"], pack["nearest_resistance_distance_pct"],
            pack["distance_to_52w_high_pct"], pack["distance_to_ath_pct"], pack["breakout_quality"], pack["plan_family"],
            _json_dumps(reasons), _json_dumps(risk_tags), _json_dumps(metrics or {}), now_ts,
        ),
    )


def _record_pre_move_evidence(
    conn: sqlite3.Connection,
    week_key: str,
    symbol: str,
    event_type: str,
    *,
    seen_at: str,
    price: float = 0.0,
    gain_pct: float = 0.0,
    rank: int = 0,
    category: str = "",
    category_key: str = "",
    market_phase: str = "",
    source_reasons: list | None = None,
    metrics: dict | None = None,
    updated_ts: float | None = None,
) -> None:
    if not _enabled() or not MISSED_EVIDENCE_ENABLED:
        return
    gain_f = _sanitize_timeline_gain_pct(gain_pct)
    rank_i = _safe_int(rank, 0)
    if not _should_store_evidence(event_type, gain_f, rank_i, source_reasons, metrics):
        return
    base_key = f"first_{_clean_text(event_type, 60)}"
    _record_pre_move_snapshot(
        conn, week_key, symbol, base_key, str(event_type), seen_at=seen_at,
        price=price, gain_pct=gain_f, rank=rank_i, category=category, category_key=category_key,
        market_phase=market_phase, source_reasons=source_reasons, metrics=metrics, updated_ts=updated_ts,
    )
    if gain_f > 0:
        for milestone in MISSED_EVIDENCE_MILESTONES:
            if gain_f >= milestone:
                _record_pre_move_snapshot(
                    conn, week_key, symbol, f"first_at_{milestone}pct", f"first_at_{milestone}pct",
                    seen_at=seen_at, price=price, gain_pct=gain_f, rank=rank_i,
                    category=category, category_key=category_key, market_phase=market_phase,
                    source_reasons=source_reasons, metrics=metrics, updated_ts=updated_ts,
                )


def init_missed_opportunities_db() -> bool:
    """Create Missed Opportunities tables without touching existing app tables."""
    global _INITIALIZED
    if not _enabled():
        return False
    if _INITIALIZED:
        return True
    with _LOCK:
        if _INITIALIZED:
            return True
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_seen_symbols (
                    week_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    times_seen INTEGER NOT NULL DEFAULT 0,
                    best_category TEXT NOT NULL DEFAULT '',
                    best_category_key TEXT NOT NULL DEFAULT '',
                    best_category_rank INTEGER NOT NULL DEFAULT 0,
                    latest_category TEXT NOT NULL DEFAULT '',
                    latest_category_key TEXT NOT NULL DEFAULT '',
                    first_price REAL NOT NULL DEFAULT 0,
                    last_price REAL NOT NULL DEFAULT 0,
                    max_quality REAL NOT NULL DEFAULT 0,
                    max_execution REAL NOT NULL DEFAULT 0,
                    max_display_rank REAL NOT NULL DEFAULT 0,
                    sharia_status TEXT NOT NULL DEFAULT '',
                    sharia_label TEXT NOT NULL DEFAULT '',
                    source_reasons_json TEXT NOT NULL DEFAULT '[]',
                    source_tags_json TEXT NOT NULL DEFAULT '[]',
                    news_title TEXT NOT NULL DEFAULT '',
                    news_sentiment TEXT NOT NULL DEFAULT '',
                    news_scope TEXT NOT NULL DEFAULT '',
                    market_phase TEXT NOT NULL DEFAULT '',
                    updated_ts REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (week_key, symbol)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_source_candidates (
                    week_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    times_seen INTEGER NOT NULL DEFAULT 0,
                    candidate_stage TEXT NOT NULL DEFAULT '',
                    source_score REAL NOT NULL DEFAULT 0,
                    discovery_rank INTEGER NOT NULL DEFAULT 0,
                    source_reasons_json TEXT NOT NULL DEFAULT '[]',
                    source_tags_json TEXT NOT NULL DEFAULT '[]',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    sharia_status TEXT NOT NULL DEFAULT '',
                    sharia_label TEXT NOT NULL DEFAULT '',
                    sharia_action TEXT NOT NULL DEFAULT '',
                    sharia_reason TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL DEFAULT 0,
                    change_pct REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    dollar_volume REAL NOT NULL DEFAULT 0,
                    updated_ts REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (week_key, symbol)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_symbol_timeline (
                    week_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    times_seen INTEGER NOT NULL DEFAULT 0,
                    first_price REAL NOT NULL DEFAULT 0,
                    last_price REAL NOT NULL DEFAULT 0,
                    first_gain_pct REAL NOT NULL DEFAULT 0,
                    last_gain_pct REAL NOT NULL DEFAULT 0,
                    max_gain_pct REAL NOT NULL DEFAULT 0,
                    first_rank INTEGER NOT NULL DEFAULT 0,
                    best_rank INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT '',
                    category_key TEXT NOT NULL DEFAULT '',
                    market_phase TEXT NOT NULL DEFAULT '',
                    source_reasons_json TEXT NOT NULL DEFAULT '[]',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    updated_ts REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (week_key, symbol, event_type)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_pre_move_snapshots (
                    week_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    snapshot_key TEXT NOT NULL,
                    snapshot_type TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL DEFAULT '',
                    last_seen_at TEXT NOT NULL DEFAULT '',
                    times_seen INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    gain_pct REAL NOT NULL DEFAULT 0,
                    rank INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL DEFAULT '',
                    category_key TEXT NOT NULL DEFAULT '',
                    market_phase TEXT NOT NULL DEFAULT '',
                    quality REAL NOT NULL DEFAULT 0,
                    execution REAL NOT NULL DEFAULT 0,
                    display_rank_score REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    volume_ratio REAL NOT NULL DEFAULT 0,
                    effective_volume_ratio REAL NOT NULL DEFAULT 0,
                    dollar_volume REAL NOT NULL DEFAULT 0,
                    vwap_proxy REAL NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL DEFAULT 0,
                    stop_loss REAL NOT NULL DEFAULT 0,
                    target_price REAL NOT NULL DEFAULT 0,
                    risk_pct REAL NOT NULL DEFAULT 0,
                    rr_1 REAL NOT NULL DEFAULT 0,
                    nearest_support REAL NOT NULL DEFAULT 0,
                    nearest_support_strength TEXT NOT NULL DEFAULT '',
                    nearest_support_distance_pct REAL NOT NULL DEFAULT 0,
                    nearest_resistance REAL NOT NULL DEFAULT 0,
                    nearest_resistance_strength TEXT NOT NULL DEFAULT '',
                    nearest_resistance_distance_pct REAL NOT NULL DEFAULT 0,
                    distance_to_52w_high_pct REAL NOT NULL DEFAULT 0,
                    distance_to_ath_pct REAL NOT NULL DEFAULT 0,
                    breakout_quality TEXT NOT NULL DEFAULT '',
                    plan_family TEXT NOT NULL DEFAULT '',
                    source_reasons_json TEXT NOT NULL DEFAULT '[]',
                    risk_tags_json TEXT NOT NULL DEFAULT '[]',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    updated_ts REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (week_key, symbol, snapshot_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missed_weekly_movers (
                    week_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    baseline_date TEXT NOT NULL DEFAULT '',
                    end_date TEXT NOT NULL DEFAULT '',
                    baseline_price REAL NOT NULL DEFAULT 0,
                    end_price REAL NOT NULL DEFAULT 0,
                    max_high REAL NOT NULL DEFAULT 0,
                    weekly_gain_pct REAL NOT NULL DEFAULT 0,
                    max_gain_pct REAL NOT NULL DEFAULT 0,
                    trigger_date TEXT NOT NULL DEFAULT '',
                    trigger_gap_pct REAL NOT NULL DEFAULT 0,
                    trigger_day_change_pct REAL NOT NULL DEFAULT 0,
                    trigger_volume REAL NOT NULL DEFAULT 0,
                    trigger_volume_multiple REAL NOT NULL DEFAULT 0,
                    close_strength REAL NOT NULL DEFAULT 0,
                    likely_driver TEXT NOT NULL DEFAULT '',
                    driver_confidence TEXT NOT NULL DEFAULT '',
                    driver_reasons_json TEXT NOT NULL DEFAULT '[]',
                    appeared_status TEXT NOT NULL DEFAULT '',
                    appeared_reason TEXT NOT NULL DEFAULT '',
                    sharia_status TEXT NOT NULL DEFAULT '',
                    sharia_label TEXT NOT NULL DEFAULT '',
                    sharia_reason TEXT NOT NULL DEFAULT '',
                    news_title TEXT NOT NULL DEFAULT '',
                    news_sentiment TEXT NOT NULL DEFAULT '',
                    news_age_label TEXT NOT NULL DEFAULT '',
                    updated_ts REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (week_key, symbol)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_seen_week ON missed_seen_symbols(week_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_source_week ON missed_source_candidates(week_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_movers_week ON missed_weekly_movers(week_key, max_gain_pct DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_timeline_week_symbol ON missed_symbol_timeline(week_key, symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_timeline_event ON missed_symbol_timeline(week_key, event_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_premove_week_symbol ON missed_pre_move_snapshots(week_key, symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_missed_premove_week_key ON missed_pre_move_snapshots(week_key, snapshot_key)")
            conn.commit()
        _INITIALIZED = True
    return True


def _week_parts(week_key: str | None = None) -> tuple[str, str, str]:
    if week_key and "_" in str(week_key):
        a, b = str(week_key).split("_", 1)
        return str(week_key), a, b
    start, end = get_performance_week_window()
    return get_performance_week_key(), start, end


def _category_key(label: str) -> str:
    label = str(label or "")
    return CATEGORY_KEYS.get(label, "watch" if "مراقبة" in label else ("gray" if "غير محسوم" in label else "other"))


def _category_rank(label: str) -> int:
    label = str(label or "")
    if label in CATEGORY_RANK:
        return CATEGORY_RANK[label]
    if "دخول قوي" in label:
        return 50
    if "بحذر" in label:
        return 40
    if "غير محسوم" in label or "رمادي" in label:
        return 30
    if "مراقبة" in label:
        return 20
    return 10


def record_missed_seen_from_scan(rows: list[dict], diagnostics: dict | None = None, market_phase: str = "", source: str = "trade_scan") -> dict:
    """Record displayed radar rows for later missed-opportunity matching."""
    if not _enabled() or not rows:
        return {"ok": False, "enabled": bool(MISSED_OPPORTUNITIES_ENABLED), "recorded": 0}
    init_missed_opportunities_db()
    week_key, _, _ = _week_parts()
    now_text = _now_text()
    now_ts = _now_ts()
    recorded = 0
    with _LOCK:
        with _connect() as conn:
            for idx, row in enumerate(rows or []):
                try:
                    sym = _clean_symbol((row or {}).get("symbol", ""))
                    if not sym:
                        continue
                    decision = _first_text(row, ["decision", "effective_decision"], "", 120)
                    rank = _category_rank(decision)
                    key = _category_key(decision)
                    price = _first_float(row, ["current_price_live", "display_price", "price", "snapshot_price", "current_price"])
                    quality = _safe_float((row or {}).get("quality_score"))
                    execution = _safe_float((row or {}).get("execution_readiness_score"))
                    display_rank = _safe_float((row or {}).get("display_rank_score"))
                    list_rank = _safe_int((row or {}).get("display_rank") or (row or {}).get("rank") or (idx + 1), idx + 1)
                    gain_pct = _first_pct(row, ["display_change_pct", "current_change_pct", "current_change_pct_live", "change_pct", "day_change_pct", "live_change_pct", "fmp_change_pct"], 0.0)
                    source_reasons = (row or {}).get("source_reason_tags") or []
                    if not source_reasons and (row or {}).get("source_reason"):
                        source_reasons = [str((row or {}).get("source_reason"))]
                    source_tags = (row or {}).get("source_tags") or []
                    conn.execute(
                        """
                        INSERT INTO missed_seen_symbols(
                            week_key, symbol, first_seen_at, last_seen_at, times_seen,
                            best_category, best_category_key, best_category_rank,
                            latest_category, latest_category_key, first_price, last_price,
                            max_quality, max_execution, max_display_rank,
                            sharia_status, sharia_label, source_reasons_json, source_tags_json,
                            news_title, news_sentiment, news_scope, market_phase, updated_ts
                        ) VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(week_key, symbol) DO UPDATE SET
                            last_seen_at=excluded.last_seen_at,
                            times_seen=missed_seen_symbols.times_seen + 1,
                            best_category=CASE WHEN excluded.best_category_rank > missed_seen_symbols.best_category_rank THEN excluded.best_category ELSE missed_seen_symbols.best_category END,
                            best_category_key=CASE WHEN excluded.best_category_rank > missed_seen_symbols.best_category_rank THEN excluded.best_category_key ELSE missed_seen_symbols.best_category_key END,
                            best_category_rank=MAX(missed_seen_symbols.best_category_rank, excluded.best_category_rank),
                            latest_category=excluded.latest_category,
                            latest_category_key=excluded.latest_category_key,
                            last_price=excluded.last_price,
                            max_quality=MAX(missed_seen_symbols.max_quality, excluded.max_quality),
                            max_execution=MAX(missed_seen_symbols.max_execution, excluded.max_execution),
                            max_display_rank=MAX(missed_seen_symbols.max_display_rank, excluded.max_display_rank),
                            sharia_status=excluded.sharia_status,
                            sharia_label=excluded.sharia_label,
                            source_reasons_json=excluded.source_reasons_json,
                            source_tags_json=excluded.source_tags_json,
                            news_title=excluded.news_title,
                            news_sentiment=excluded.news_sentiment,
                            news_scope=excluded.news_scope,
                            market_phase=excluded.market_phase,
                            updated_ts=excluded.updated_ts
                        """,
                        (
                            week_key, sym, now_text, now_text,
                            decision, key, rank, decision, key, price, price,
                            quality, execution, display_rank,
                            _first_text(row, ["sharia_status", "halal_status"], ""),
                            _first_text(row, ["sharia_label", "halal_label"], ""),
                            _json_dumps(source_reasons), _json_dumps(source_tags),
                            _first_text(row, ["news_title"], "", 500),
                            _first_text(row, ["news_sentiment"], "", 80),
                            _first_text(row, ["news_scope", "news_scope_label"], "", 100),
                            str(market_phase or (row or {}).get("market_phase", "") or ""),
                            now_ts,
                        ),
                    )
                    event_type = _timeline_event_type_for_category(key, decision)
                    _record_timeline_event(
                        conn, week_key, sym, "display_any",
                        seen_at=now_text, price=price, gain_pct=gain_pct, rank=list_rank,
                        category=decision, category_key=key, market_phase=str(market_phase or (row or {}).get("market_phase", "") or ""),
                        source_reasons=source_reasons, metrics={"quality": quality, "execution": execution, "display_rank_score": display_rank}, updated_ts=now_ts,
                    )
                    _record_timeline_event(
                        conn, week_key, sym, event_type,
                        seen_at=now_text, price=price, gain_pct=gain_pct, rank=list_rank,
                        category=decision, category_key=key, market_phase=str(market_phase or (row or {}).get("market_phase", "") or ""),
                        source_reasons=source_reasons, metrics={"quality": quality, "execution": execution, "display_rank_score": display_rank}, updated_ts=now_ts,
                    )
                    recorded += 1
                except Exception:
                    continue
            conn.commit()
    return {"ok": True, "recorded": recorded, "week_key": week_key, "source": source}


def _sharia_for_symbol(symbol: str) -> dict:
    try:
        from app.data_store import get_manual_sharia_exclusions_map, get_manual_sharia_approvals_map
        from app.sharia_filter import assess_sharia_source_fast
        return assess_sharia_source_fast(symbol, get_manual_sharia_exclusions_map(), get_manual_sharia_approvals_map()) or {}
    except Exception as exc:
        return {"status": "unknown", "label": "غير معروف", "reason": f"تعذر فحص الشرعية: {type(exc).__name__}", "source_filter_action": "unknown", "should_block": False, "is_gray": True}


def record_missed_source_candidates(symbols: list[str] | None = None, diagnostics: dict | None = None, source: str = "active_universe") -> dict:
    """Record source/deep-universe candidates without affecting the scan output."""
    if not _enabled():
        return {"ok": False, "enabled": bool(MISSED_OPPORTUNITIES_ENABLED), "recorded": 0}
    init_missed_opportunities_db()
    diagnostics = diagnostics or {}
    week_key, _, _ = _week_parts()
    now_text = _now_text()
    now_ts = _now_ts()
    rows: list[dict] = []

    # Full dynamic source diagnostics, if available, tells us about candidates that
    # were discovered before the Sharia/deep-analysis narrowing step.
    candidate_rows = diagnostics.get("ranked_candidates") or diagnostics.get("candidate_rows") or []
    if isinstance(candidate_rows, list):
        for idx, item in enumerate(candidate_rows[:1200]):
            if not isinstance(item, dict):
                continue
            sym = _clean_symbol(item.get("symbol"))
            if not sym:
                continue
            metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
            rows.append({
                "symbol": sym,
                "candidate_stage": "discovery_candidate",
                "source_score": _safe_float(item.get("score")),
                "discovery_rank": idx + 1,
                "source_reasons": item.get("reasons") or [],
                "source_tags": item.get("sources") or [],
                "metrics": metrics,
                "price": _safe_float(metrics.get("price") or metrics.get("live_price") or metrics.get("fmp_price")),
                "change_pct": _first_pct(metrics, ["live_change_pct", "fmp_change_pct", "day_change_pct", "change_pct", "current_change_pct"], 0.0),
                "volume": _safe_float(metrics.get("volume") or metrics.get("live_volume") or metrics.get("fmp_volume")),
                "dollar_volume": _safe_float(metrics.get("dollar_volume")),
            })

    reason_map = diagnostics.get("reasons") or {}
    tag_map = diagnostics.get("source_tags") or {}
    if symbols:
        for idx, sym_raw in enumerate(symbols):
            sym = _clean_symbol(sym_raw)
            if not sym:
                continue
            rows.append({
                "symbol": sym,
                "candidate_stage": "deep_universe",
                "source_score": 0,
                "discovery_rank": idx + 1,
                "source_reasons": reason_map.get(sym, []) if isinstance(reason_map, dict) else [],
                "source_tags": tag_map.get(sym, []) if isinstance(tag_map, dict) else [],
                "metrics": {},
                "price": 0,
                "change_pct": 0,
                "volume": 0,
                "dollar_volume": 0,
            })

    # De-duplicate while preserving the strongest stage. deep_universe is more useful
    # for explaining why a symbol did not produce a final row.
    dedup: dict[str, dict] = {}
    for row in rows:
        sym = row.get("symbol")
        if not sym:
            continue
        old = dedup.get(sym)
        if not old:
            dedup[sym] = row
        elif old.get("candidate_stage") != "deep_universe" and row.get("candidate_stage") == "deep_universe":
            merged = dict(old)
            merged.update({k: v for k, v in row.items() if v not in (None, "", [], {}, 0)})
            merged["candidate_stage"] = "deep_universe"
            dedup[sym] = merged

    recorded = 0
    with _LOCK:
        with _connect() as conn:
            # Timeline keeps both raw discovery-source and final deep-universe appearances.
            for trow in rows:
                try:
                    tsym = _clean_symbol(trow.get("symbol"))
                    if not tsym:
                        continue
                    timeline_event = "deep_universe" if str(trow.get("candidate_stage") or "") == "deep_universe" else "source"
                    _record_timeline_event(
                        conn, week_key, tsym, timeline_event,
                        seen_at=now_text,
                        price=_safe_float(trow.get("price")),
                        gain_pct=_safe_float(trow.get("change_pct")),
                        rank=_safe_int(trow.get("discovery_rank")),
                        category=str(trow.get("candidate_stage") or ""),
                        category_key=str(trow.get("candidate_stage") or ""),
                        market_phase=str((diagnostics or {}).get("market_phase", "") or (diagnostics or {}).get("phase_detail", "") or ""),
                        source_reasons=trow.get("source_reasons") or [],
                        metrics=trow.get("metrics") or {},
                        updated_ts=now_ts,
                    )
                except Exception:
                    continue
            for sym, row in dedup.items():
                try:
                    # Keep source-candidate recording extremely light: Sharia is
                    # assessed later only for weekly movers, not for every source symbol.
                    sh = {}
                    conn.execute(
                        """
                        INSERT INTO missed_source_candidates(
                            week_key, symbol, first_seen_at, last_seen_at, times_seen,
                            candidate_stage, source_score, discovery_rank,
                            source_reasons_json, source_tags_json, metrics_json,
                            sharia_status, sharia_label, sharia_action, sharia_reason,
                            price, change_pct, volume, dollar_volume, updated_ts
                        ) VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(week_key, symbol) DO UPDATE SET
                            last_seen_at=excluded.last_seen_at,
                            times_seen=missed_source_candidates.times_seen + 1,
                            candidate_stage=CASE WHEN excluded.candidate_stage='deep_universe' THEN excluded.candidate_stage ELSE missed_source_candidates.candidate_stage END,
                            source_score=MAX(missed_source_candidates.source_score, excluded.source_score),
                            discovery_rank=CASE WHEN missed_source_candidates.discovery_rank=0 OR excluded.discovery_rank < missed_source_candidates.discovery_rank THEN excluded.discovery_rank ELSE missed_source_candidates.discovery_rank END,
                            source_reasons_json=excluded.source_reasons_json,
                            source_tags_json=excluded.source_tags_json,
                            metrics_json=excluded.metrics_json,
                            sharia_status=excluded.sharia_status,
                            sharia_label=excluded.sharia_label,
                            sharia_action=excluded.sharia_action,
                            sharia_reason=excluded.sharia_reason,
                            price=CASE WHEN excluded.price>0 THEN excluded.price ELSE missed_source_candidates.price END,
                            change_pct=CASE WHEN excluded.change_pct!=0 THEN excluded.change_pct ELSE missed_source_candidates.change_pct END,
                            volume=CASE WHEN excluded.volume>0 THEN excluded.volume ELSE missed_source_candidates.volume END,
                            dollar_volume=CASE WHEN excluded.dollar_volume>0 THEN excluded.dollar_volume ELSE missed_source_candidates.dollar_volume END,
                            updated_ts=excluded.updated_ts
                        """,
                        (
                            week_key, sym, now_text, now_text,
                            str(row.get("candidate_stage") or ""), _safe_float(row.get("source_score")), _safe_int(row.get("discovery_rank")),
                            _json_dumps(row.get("source_reasons") or []), _json_dumps(row.get("source_tags") or []), _json_dumps(row.get("metrics") or {}),
                            str(sh.get("status", "") or ""), str(sh.get("label", "") or ""), str(sh.get("source_filter_action", "") or ""), str(sh.get("reason", "") or "")[:500],
                            _safe_float(row.get("price")), _safe_float(row.get("change_pct")), _safe_float(row.get("volume")), _safe_float(row.get("dollar_volume")), now_ts,
                        ),
                    )
                    recorded += 1
                except Exception:
                    continue
            conn.commit()
    return {"ok": True, "recorded": recorded, "week_key": week_key, "source": source}


def _date_range(start: str, end: str) -> list[str]:
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d1 = datetime.strptime(end, "%Y-%m-%d").date()
    except Exception:
        return []
    out = []
    d = d0
    while d <= d1:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _business_days_before(date_str: str, count: int = 5) -> list[str]:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)
    except Exception:
        d = datetime.now(NY_TZ).date() - timedelta(days=1)
    out = []
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


def _grouped_map_for(date_str: str) -> dict:
    try:
        return _scanner.get_grouped_daily_map(date_str) or {}
    except Exception:
        return {}


def _select_week_maps(week_start: str, week_end: str) -> tuple[str, dict, list[tuple[str, dict]], str, dict]:
    # Baseline: last available business day before the active week.
    baseline_date = ""
    baseline_map = {}
    for d in _business_days_before(week_start, 7):
        m = _grouped_map_for(d)
        if len(m or {}) >= 500:
            baseline_date, baseline_map = d, m
            break

    today_ny = datetime.now(NY_TZ).date().isoformat()
    effective_end = min(str(week_end), today_ny)
    week_maps = []
    for d in _date_range(week_start, effective_end):
        m = _grouped_map_for(d)
        if len(m or {}) >= 500:
            week_maps.append((d, m))
    if not week_maps and baseline_map:
        week_maps.append((baseline_date, baseline_map))
    end_date, end_map = week_maps[-1] if week_maps else (baseline_date, baseline_map)
    return baseline_date, baseline_map, week_maps, end_date, end_map


def _fetch_symbol_daily_bars(symbol: str, start: str, end: str) -> list[dict]:
    if not POLYGON_API_KEY:
        return []
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        r = HTTP_SESSION.get(url, timeout=14)
        if r.status_code >= 400:
            return []
        data = r.json()
        return data.get("results", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _analyze_mover_path(symbol: str, baseline_price: float, week_start: str, week_end: str) -> dict:
    """Analyze before/during move using Polygon daily bars. On-demand only."""
    try:
        start_dt = datetime.strptime(week_start, "%Y-%m-%d").date() - timedelta(days=35)
        bars = _fetch_symbol_daily_bars(symbol, start_dt.isoformat(), week_end)
    except Exception:
        bars = []
    if not bars:
        return {"likely_driver": "غير واضح", "driver_confidence": "منخفضة", "driver_reasons": ["لا توجد شموع يومية كافية لتحليل سبب الصعود"]}

    enriched = []
    for b in bars:
        try:
            ts = b.get("t")
            d = datetime.fromtimestamp(float(ts) / 1000.0, NY_TZ).date().isoformat() if ts else ""
            o, h, l, c, v = to_float(b.get("o")), to_float(b.get("h")), to_float(b.get("l")), to_float(b.get("c")), to_float(b.get("v"))
            if c <= 0:
                continue
            enriched.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v})
        except Exception:
            continue
    if not enriched:
        return {"likely_driver": "غير واضح", "driver_confidence": "منخفضة", "driver_reasons": ["تعذر قراءة الشموع اليومية"]}

    baseline = float(baseline_price or 0)
    if baseline <= 0:
        for b in enriched:
            if b["date"] < week_start:
                baseline = b["close"]
    trigger = None
    max_score = -9999.0
    for i, b in enumerate(enriched):
        if b["date"] < week_start or b["date"] > week_end:
            continue
        prev_close = enriched[i-1]["close"] if i > 0 else baseline
        if prev_close <= 0:
            continue
        prior = [x for x in enriched[max(0, i-12):i] if x.get("volume", 0) > 0]
        avg_vol = sum(x["volume"] for x in prior) / len(prior) if prior else 0
        vol_mult = (b["volume"] / avg_vol) if avg_vol > 0 else 0
        gap_pct = ((b["open"] - prev_close) / prev_close * 100.0) if b["open"] > 0 else 0
        day_change_pct = ((b["close"] - prev_close) / prev_close * 100.0) if b["close"] > 0 else 0
        high_gain_pct = ((b["high"] - baseline) / baseline * 100.0) if baseline > 0 else 0
        rng = max(b["high"] - b["low"], 0.0001)
        close_strength = (b["close"] - b["low"]) / rng if rng > 0 else 0
        score = max(high_gain_pct, day_change_pct) + min(vol_mult * 8, 30) + max(gap_pct, 0) + close_strength * 10
        if score > max_score:
            max_score = score
            trigger = dict(b, prev_close=prev_close, avg_volume=avg_vol, volume_multiple=vol_mult, gap_pct=gap_pct, day_change_pct=day_change_pct, high_gain_pct=high_gain_pct, close_strength=close_strength)
    if not trigger:
        return {"likely_driver": "غير واضح", "driver_confidence": "منخفضة", "driver_reasons": ["لم تظهر شمعة محفزة واضحة داخل الأسبوع"]}

    reasons = []
    likely = "غير واضح"
    confidence_score = 0
    if trigger.get("gap_pct", 0) >= 5:
        reasons.append(f"فجوة سعرية قوية عند البداية {safe_round(trigger.get('gap_pct'), 1)}%")
        confidence_score += 2
    if trigger.get("volume_multiple", 0) >= 3:
        reasons.append(f"حجم تداول انفجاري {safe_round(trigger.get('volume_multiple'), 1)}x مقارنة بالمتوسط السابق")
        confidence_score += 2
    elif trigger.get("volume_multiple", 0) >= 1.8:
        reasons.append(f"حجم تداول أعلى من المعتاد {safe_round(trigger.get('volume_multiple'), 1)}x")
        confidence_score += 1
    if trigger.get("close_strength", 0) >= 0.78:
        reasons.append("إغلاق قريب من قمة اليوم، ما يدل على استمرار الطلب")
        confidence_score += 1
    if trigger.get("day_change_pct", 0) >= 8:
        reasons.append(f"شمعة صعود يومية قوية {safe_round(trigger.get('day_change_pct'), 1)}%")
        confidence_score += 1

    # Consecutive continuation days inside the week.
    try:
        week_bars = [x for x in enriched if week_start <= x["date"] <= week_end]
        consecutive_up = 0
        prev = None
        best = 0
        for b in week_bars:
            if prev and b["close"] > prev:
                consecutive_up += 1
            else:
                consecutive_up = 1
            best = max(best, consecutive_up)
            prev = b["close"]
        if best >= 3:
            reasons.append(f"زخم مستمر لعدة جلسات ({best} جلسات صاعدة تقريبًا)")
            confidence_score += 1
    except Exception:
        pass

    price = float(trigger.get("close", 0) or 0)
    if price < 2 and trigger.get("volume_multiple", 0) >= 3:
        likely = "حركة مضاربية عالية المخاطر"
        reasons.append("السهم منخفض السعر جدًا؛ الحركة قد تكون مضاربية حتى لو كانت قوية")
    elif trigger.get("gap_pct", 0) >= 5 and trigger.get("volume_multiple", 0) >= 2:
        likely = "Gap + حجم غير عادي"
    elif trigger.get("day_change_pct", 0) >= 7 and trigger.get("volume_multiple", 0) >= 1.8:
        likely = "اختراق/زخم فني بحجم"
    elif trigger.get("volume_multiple", 0) >= 3:
        likely = "سيولة مفاجئة قادت الحركة"
    elif not reasons:
        reasons.append("الصعود واضح في السعر، لكن لا توجد علامة فنية كافية لتحديد السبب بدقة")

    confidence = "عالية" if confidence_score >= 4 else ("متوسطة" if confidence_score >= 2 else "منخفضة")
    return {
        "trigger_date": trigger.get("date", ""),
        "trigger_gap_pct": safe_round(trigger.get("gap_pct", 0), 2),
        "trigger_day_change_pct": safe_round(trigger.get("day_change_pct", 0), 2),
        "trigger_volume": safe_round(trigger.get("volume", 0), 0),
        "trigger_volume_multiple": safe_round(trigger.get("volume_multiple", 0), 2),
        "close_strength": safe_round(trigger.get("close_strength", 0), 3),
        "likely_driver": likely,
        "driver_confidence": confidence,
        "driver_reasons": reasons[:8],
    }


def _news_hint(symbol: str, enabled: bool = True) -> dict:
    if not enabled:
        return {}
    try:
        from app.news_engine import get_news_bundle
        b = get_news_bundle(symbol, "", "", "") or {}
        return {
            "news_title": str(b.get("news_title", "") or "")[:500],
            "news_sentiment": str(b.get("news_sentiment", "") or "")[:80],
            "news_age_label": str(b.get("news_age_label", "") or "")[:120],
            "news_scope": str(b.get("news_scope", "") or "")[:80],
            "news_is_catalyst": bool(b.get("news_is_catalyst", False)),
        }
    except Exception:
        return {}


def _seen_and_source_maps(week_key: str) -> tuple[dict, dict]:
    seen, source = {}, {}
    if not _enabled():
        return seen, source
    init_missed_opportunities_db()
    with _connect() as conn:
        for r in conn.execute("SELECT * FROM missed_seen_symbols WHERE week_key=?", (week_key,)).fetchall():
            seen[str(r["symbol"])] = dict(r)
        for r in conn.execute("SELECT * FROM missed_source_candidates WHERE week_key=?", (week_key,)).fetchall():
            source[str(r["symbol"])] = dict(r)
    return seen, source




def _timeline_maps(week_key: str) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    if not _enabled():
        return out
    try:
        init_missed_opportunities_db()
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM missed_symbol_timeline WHERE week_key=?", (week_key,)).fetchall()
        for r in rows:
            d = dict(r)
            sym = str(d.get("symbol") or "")
            et = str(d.get("event_type") or "")
            if not sym or not et:
                continue
            d["source_reasons"] = _json_loads(d.get("source_reasons_json"), []) or []
            d["metrics"] = _json_loads(d.get("metrics_json"), {}) or {}
            out.setdefault(sym, {})[et] = d
    except Exception:
        return out
    return out


def _timeline_for_symbol(week_key: str, symbol: str) -> dict[str, dict]:
    return _timeline_maps(week_key).get(_clean_symbol(symbol), {})


def _timeline_event_summary(row: dict | None) -> dict:
    if not row:
        return {}
    first_price = _safe_float(row.get("first_price"), 0.0)
    last_price = _safe_float(row.get("last_price"), 0.0)
    first_gain = _timeline_gain_or_none(row.get("first_gain_pct"), first_price)
    last_gain = _timeline_gain_or_none(row.get("last_gain_pct"), last_price)
    max_gain = _timeline_gain_or_none(row.get("max_gain_pct"), first_price or last_price)
    return {
        "first_seen_at": row.get("first_seen_at", ""),
        "last_seen_at": row.get("last_seen_at", ""),
        "times_seen": _safe_int(row.get("times_seen")),
        "first_price": safe_round(first_price, 4),
        "last_price": safe_round(last_price, 4),
        "first_gain_pct": safe_round(first_gain, 2) if first_gain is not None else None,
        "last_gain_pct": safe_round(last_gain, 2) if last_gain is not None else None,
        "max_gain_pct_seen": safe_round(max_gain, 2) if max_gain is not None else None,
        "first_rank": _safe_int(row.get("first_rank")),
        "best_rank": _safe_int(row.get("best_rank")),
        "category": row.get("category", ""),
        "category_key": row.get("category_key", ""),
        "market_phase": row.get("market_phase", ""),
        "source_reasons": row.get("source_reasons") or _json_loads(row.get("source_reasons_json"), []) or [],
    }


def _timeline_summary(timeline: dict[str, dict], mover: dict | None = None) -> dict:
    order = ["source", "deep_universe", "display_any", "watch", "gray", "cautious", "strong"]
    events = {k: _timeline_event_summary(timeline.get(k)) for k in order if timeline.get(k)}
    first_source = events.get("source") or {}
    first_deep = events.get("deep_universe") or {}
    first_display = events.get("display_any") or {}
    first_watch = events.get("watch") or {}
    first_cautious = events.get("cautious") or {}
    first_strong = events.get("strong") or {}

    # Classify timing/promotion quality.
    late_threshold = float(MISSED_LATE_PROMOTION_PCT)
    big_threshold = float(MISSED_BIG_MOVE_PCT)
    first_entry = first_strong or first_cautious
    first_entry_gain_raw = first_entry.get("first_gain_pct") if first_entry else None
    first_source_gain_raw = first_source.get("first_gain_pct") if first_source else None
    first_display_gain_raw = first_display.get("first_gain_pct") if first_display else None
    first_entry_gain = _safe_float(first_entry_gain_raw, 0.0) if first_entry_gain_raw is not None else 0.0
    first_source_gain = _safe_float(first_source_gain_raw, 0.0) if first_source_gain_raw is not None else 0.0
    first_display_gain = _safe_float(first_display_gain_raw, 0.0) if first_display_gain_raw is not None else 0.0

    timing_status = "no_timeline"
    timing_label = "لا يوجد خط زمني محفوظ بعد"
    if first_entry:
        if first_entry_gain_raw is None:
            timing_status = "entry_gain_unknown"
            timing_label = "ظهر كفرصة دخول لكن نسبة الصعود وقتها غير متوفرة"
        elif first_entry_gain >= big_threshold:
            timing_status = "very_late_entry"
            timing_label = f"ظهر كفرصة دخول بعد حركة كبيرة جدًا (+{safe_round(first_entry_gain,1)}%)"
        elif first_entry_gain >= late_threshold:
            timing_status = "late_entry"
            timing_label = f"ظهر كفرصة دخول بعد ارتفاع كبير نسبيًا (+{safe_round(first_entry_gain,1)}%)"
        else:
            timing_status = "early_or_reasonable_entry"
            timing_label = f"ظهر كفرصة دخول عند ارتفاع مبكر/معقول (+{safe_round(first_entry_gain,1)}%)"
    elif first_watch:
        watch_gain_raw = first_watch.get("first_gain_pct")
        watch_gain = _safe_float(watch_gain_raw, 0.0) if watch_gain_raw is not None else 0.0
        if watch_gain_raw is None:
            timing_status = "watch_gain_unknown"
            timing_label = "ظهر في المراقبة لكن نسبة الصعود وقتها غير متوفرة"
        elif watch_gain >= big_threshold:
            timing_status = "late_watch_only"
            timing_label = f"ظهر في المراقبة بعد حركة كبيرة (+{safe_round(watch_gain,1)}%)"
        else:
            timing_status = "watch_only_not_promoted"
            timing_label = f"ظهر في المراقبة ولم يترقَّ؛ أول مراقبة عند +{safe_round(watch_gain,1)}%"
    elif first_display:
        timing_status = "displayed_other"
        timing_label = "ظهر في الأداة لكن خارج التصنيفات الرئيسية المحفوظة"
    elif first_deep:
        timing_status = "deep_not_displayed"
        timing_label = "دخل التحليل العميق لكنه لم يظهر في القوائم"
    elif first_source:
        timing_status = "source_not_promoted"
        if first_source_gain_raw is None:
            timing_label = "دخل المنبع لكن نسبة الصعود وقتها غير متوفرة ولم يترقَّ للقوائم"
        else:
            timing_label = f"دخل المنبع عند +{safe_round(first_source_gain,1)}% ولم يترقَّ للقوائم"

    promotion_issue = ""
    if first_source and first_entry and first_source_gain_raw is not None and first_entry_gain_raw is not None and first_source_gain < 8 and first_entry_gain >= late_threshold:
        promotion_issue = "كان معروفًا للمنبع مبكرًا لكنه ترقّى متأخرًا"
    elif first_watch and first_entry and first_entry_gain_raw is not None and first_watch.get("first_gain_pct") is not None and _safe_float(first_watch.get("first_gain_pct"),0) < 8 and first_entry_gain >= late_threshold:
        promotion_issue = "ظهر مراقبة مبكرًا لكنه لم يتحول لدخول إلا بعد ارتفاع كبير"
    elif first_source and not first_display:
        promotion_issue = "مشكلة انتقاء/ترقية من المنبع إلى القوائم"
    elif first_deep and not first_display:
        promotion_issue = "دخل التحليل العميق لكنه لم ينتج فرصة ظاهرة"

    return {
        "events": events,
        "first_source_seen_at": first_source.get("first_seen_at", ""),
        "first_source_gain_pct": safe_round(first_source_gain, 2),
        "first_display_seen_at": first_display.get("first_seen_at", ""),
        "first_display_gain_pct": safe_round(first_display_gain, 2),
        "first_entry_seen_at": first_entry.get("first_seen_at", "") if first_entry else "",
        "first_entry_gain_pct": safe_round(first_entry_gain, 2),
        "first_entry_type": "strong" if first_strong and (not first_cautious or (first_strong.get("first_seen_at","") <= first_cautious.get("first_seen_at",""))) else ("cautious" if first_cautious else ""),
        "timing_status": timing_status,
        "timing_label": timing_label,
        "promotion_issue": promotion_issue,
    }


def _timeline_brief_lines(symbol: str, summary: dict) -> list[str]:
    ev = summary.get("events") or {}
    labels = [
        ("source", "المنبع"),
        ("deep_universe", "التحليل العميق"),
        ("watch", "المراقبة"),
        ("cautious", "دخول بحذر"),
        ("strong", "دخول قوي"),
        ("gray", "رمادي/غير محسوم"),
    ]
    lines = []
    for key, ar in labels:
        row = ev.get(key) or {}
        if not row:
            continue
        gain_txt = _format_pct_or_na(row.get('first_gain_pct'), 1)
        lines.append(
            f"  - {ar}: أول ظهور {row.get('first_seen_at') or '؟'} | صعود وقتها {gain_txt} | "
            f"أفضل ترتيب #{row.get('best_rank') or row.get('first_rank') or '؟'} | مرات الظهور {row.get('times_seen') or 0}"
        )
    if not lines:
        lines.append("  - لا يوجد خط زمني محفوظ لهذا السهم بعد.")
    return lines


def _pre_move_snapshots_for_symbol(week_key: str, symbol: str) -> list[dict]:
    sym = _clean_symbol(symbol)
    if not _enabled() or not sym:
        return []
    init_missed_opportunities_db()
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM missed_pre_move_snapshots
                WHERE week_key=? AND symbol=?
                ORDER BY
                    CASE
                        WHEN snapshot_key='first_source' THEN 1
                        WHEN snapshot_key='first_deep_universe' THEN 2
                        WHEN snapshot_key='first_watch' THEN 3
                        WHEN snapshot_key='first_cautious' THEN 4
                        WHEN snapshot_key='first_strong' THEN 5
                        WHEN snapshot_key LIKE 'first_at_%' THEN 20
                        ELSE 30
                    END,
                    gain_pct ASC, first_seen_at ASC
                """,
                (week_key, sym),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _pre_move_snapshots_map(week_key: str, limit: int = 20000) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not _enabled():
        return out
    init_missed_opportunities_db()
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM missed_pre_move_snapshots
                WHERE week_key=?
                ORDER BY symbol, first_seen_at ASC
                LIMIT ?
                """,
                (week_key, int(limit or 20000)),
            ).fetchall()
        for r in rows:
            d = dict(r)
            out.setdefault(str(d.get('symbol') or ''), []).append(d)
    except Exception:
        pass
    return out


def _snapshot_line(row: dict) -> str:
    if not row:
        return ""
    key = str(row.get('snapshot_key') or row.get('snapshot_type') or '')
    ar_key = {
        'first_source': 'أول منبع',
        'first_deep_universe': 'أول تحليل عميق',
        'first_watch': 'أول مراقبة',
        'first_cautious': 'أول بحذر',
        'first_strong': 'أول قوي',
        'first_gray': 'أول رمادي',
        'first_display_any': 'أول ظهور',
    }.get(key, key.replace('first_at_', 'أول ظهور عند +').replace('pct', '%'))
    bits = [f"{ar_key}: {row.get('first_seen_at') or '؟'}"]
    gp = _timeline_gain_or_none(row.get('gain_pct'), row.get('price'))
    bits.append(f"صعود { _format_pct_or_na(gp, 1) }")
    if _safe_int(row.get('rank'), 0):
        bits.append(f"ترتيب #{_safe_int(row.get('rank'),0)}")
    if _safe_float(row.get('volume_ratio'), 0) > 0:
        bits.append(f"حجم {safe_round(row.get('volume_ratio'),1)}x")
    elif _safe_float(row.get('effective_volume_ratio'), 0) > 0:
        bits.append(f"حجم فعلي {safe_round(row.get('effective_volume_ratio'),1)}x")
    if _safe_float(row.get('nearest_resistance_distance_pct'), 0) > 0:
        bits.append(f"بعد المقاومة {safe_round(row.get('nearest_resistance_distance_pct'),1)}%")
    if _safe_float(row.get('quality'), 0) > 0 or _safe_float(row.get('execution'), 0) > 0:
        bits.append(f"جودة/جاهزية {safe_round(row.get('quality'),0)}/{safe_round(row.get('execution'),0)}")
    return " | ".join(bits)


def _early_evidence_summary(snapshots: list[dict]) -> dict:
    if not snapshots:
        return {"has_snapshots": False}
    first_source = next((x for x in snapshots if x.get('snapshot_key') == 'first_source'), None)
    first_watch = next((x for x in snapshots if x.get('snapshot_key') == 'first_watch'), None)
    first_entry = next((x for x in snapshots if x.get('snapshot_key') in {'first_cautious', 'first_strong'}), None)
    first_3 = next((x for x in snapshots if x.get('snapshot_key') == 'first_at_3pct'), None)
    first_5 = next((x for x in snapshots if x.get('snapshot_key') == 'first_at_5pct'), None)
    first_10 = next((x for x in snapshots if x.get('snapshot_key') == 'first_at_10pct'), None)

    early_rows = []
    for r in [first_source, first_3, first_5, first_watch, first_10, first_entry]:
        if r and r not in early_rows:
            early_rows.append(r)

    has_before_5 = any((_timeline_gain_or_none(r.get('gain_pct'), r.get('price')) is not None and _safe_float(r.get('gain_pct'), 0) <= 5.0) for r in snapshots)
    has_before_10 = any((_timeline_gain_or_none(r.get('gain_pct'), r.get('price')) is not None and _safe_float(r.get('gain_pct'), 0) <= 10.0) for r in snapshots)
    first_entry_gain = _timeline_gain_or_none(first_entry.get('gain_pct'), first_entry.get('price')) if first_entry else None
    status = "لا توجد لقطات مبكرة كافية"
    if has_before_5:
        status = "كانت هناك إشارة محفوظة قبل/حول +5%"
    elif has_before_10:
        status = "كانت هناك إشارة محفوظة قبل/حول +10%"
    elif first_entry_gain is not None and first_entry_gain >= MISSED_LATE_PROMOTION_PCT:
        status = f"أول دخول جاء متأخرًا عند +{safe_round(first_entry_gain,1)}%"
    elif first_source:
        status = "دخل المنبع لكن لا توجد لقطة مبكرة واضحة قبل الحركة"

    return {
        "has_snapshots": True,
        "status": status,
        "has_before_5pct": bool(has_before_5),
        "has_before_10pct": bool(has_before_10),
        "first_source": first_source or {},
        "first_watch": first_watch or {},
        "first_entry": first_entry or {},
        "key_snapshots": early_rows[:8],
    }

def _match_status(symbol: str, seen: dict, source: dict, sharia: dict) -> tuple[str, str, str]:
    if bool(sharia.get("should_block", False)):
        # Still report it transparently, but do not count as a clean missed opportunity.
        if symbol in seen:
            return "appeared_but_sharia_blocked", "ظهر في السجلات لكن الحكم الشرعي يمنعه من اعتباره فرصة قابلة للتنفيذ", "excluded_sharia"
        return "sharia_blocked", "صعد بقوة لكنه مستبعد أو مرفوض شرعيًا؛ لا يُحسب كفرصة فائتة مناسبة لك", "excluded_sharia"
    if symbol in seen:
        row = seen[symbol]
        cat = str(row.get("best_category") or row.get("latest_category") or "ظهر")
        return "appeared", f"ظهر في الأداة ضمن: {cat}", str(row.get("best_category_key") or "appeared")
    if symbol in source:
        row = source[symbol]
        stage = str(row.get("candidate_stage") or "")
        if stage == "deep_universe":
            return "deep_universe_not_displayed", "دخل قائمة التحليل العميق لكنه لم ينتج فرصة ظاهرة أو لم يترقَّ", "in_deep_universe"
        return "source_not_deep", "دخل منبع الاكتشاف الديناميكي لكنه لم يصل لقائمة التحليل العميق", "in_source"
    if bool(sharia.get("is_gray", False)):
        return "not_seen_gray_sharia", "لم يظهر في الأداة، وشرعيته غير محسومة/رمادية", "gray_unknown"
    return "not_in_source", "لم يظهر في سجلات المنبع أو الرادار لهذا الأسبوع", "not_in_source"


def _count_by(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for item in items or []:
        val = str(item.get(key, "") or "غير محدد")
        out[val] = int(out.get(val, 0) or 0) + 1
    return out


def build_missed_weekly_report(week_key: str | None = None, threshold: float | None = None, include_items: bool = False, include_news: bool = True, limit: int | None = None) -> dict:
    """Compute weekly missed movers on demand. Does not run in scan/live loops."""
    week_key, week_start, week_end = _week_parts(week_key)
    threshold = float(MISSED_DEFAULT_GAIN_THRESHOLD if threshold is None else threshold)
    limit = int(limit or MISSED_MOVER_LIMIT)
    init_ok = init_missed_opportunities_db() if _enabled() else False
    baseline_date, baseline_map, week_maps, end_date, end_map = _select_week_maps(week_start, week_end)
    seen, source = _seen_and_source_maps(week_key)
    timeline_by_symbol = _timeline_maps(week_key)

    if not baseline_map or not week_maps:
        return {
            "ok": False,
            "enabled": bool(MISSED_OPPORTUNITIES_ENABLED),
            "error": "insufficient_polygon_grouped_data",
            "week_key": week_key,
            "week_start": week_start,
            "week_end": week_end,
            "baseline_date": baseline_date,
            "end_date": end_date,
            "seen_symbols": len(seen),
            "source_symbols": len(source),
        }

    universe = set(baseline_map.keys())
    for _d, m in week_maps:
        universe.update(m.keys())
    movers = []
    for sym in universe:
        base = (baseline_map or {}).get(sym) or {}
        baseline_price = _safe_float(base.get("price"))
        if baseline_price < MISSED_MIN_BASELINE_PRICE:
            continue
        highs = []
        end_price = 0.0
        total_volume = 0.0
        dollar_volume = 0.0
        for d, m in week_maps:
            row = (m or {}).get(sym) or {}
            h = _safe_float(row.get("high"))
            c = _safe_float(row.get("price"))
            v = _safe_float(row.get("volume"))
            if h > 0:
                highs.append(h)
            if c > 0:
                end_price = c
            total_volume += max(v, 0)
            if c > 0 and v > 0:
                dollar_volume += c * v
        if end_price <= 0 or not highs:
            continue
        if dollar_volume < MISSED_MIN_DOLLAR_VOLUME:
            continue
        max_high = max(highs)
        weekly_gain = ((end_price - baseline_price) / baseline_price) * 100.0 if baseline_price > 0 else 0.0
        max_gain = ((max_high - baseline_price) / baseline_price) * 100.0 if baseline_price > 0 else 0.0
        if max(max_gain, weekly_gain) < threshold:
            continue
        movers.append({
            "symbol": sym,
            "baseline_date": baseline_date,
            "end_date": end_date,
            "baseline_price": safe_round(baseline_price, 4),
            "end_price": safe_round(end_price, 4),
            "max_high": safe_round(max_high, 4),
            "weekly_gain_pct": safe_round(weekly_gain, 2),
            "max_gain_pct": safe_round(max_gain, 2),
            "weekly_volume": safe_round(total_volume, 0),
            "weekly_dollar_volume": safe_round(dollar_volume, 0),
        })
    movers.sort(key=lambda x: (float(x.get("max_gain_pct", 0) or 0), float(x.get("weekly_dollar_volume", 0) or 0)), reverse=True)
    movers = movers[:limit]

    enriched = []
    news_used = 0
    for idx, mover in enumerate(movers):
        sym = mover["symbol"]
        sh = _sharia_for_symbol(sym)
        status, reason, group = _match_status(sym, seen, source, sh)
        timeline_summary = _timeline_summary(timeline_by_symbol.get(sym, {}), mover)
        path = _analyze_mover_path(sym, float(mover.get("baseline_price", 0) or 0), week_start, end_date) if idx < MISSED_DEEP_CAUSE_LIMIT else {}
        news = {}
        if include_news and idx < MISSED_NEWS_LOOKUP_LIMIT:
            news = _news_hint(sym, enabled=True)
            news_used += 1
            if news.get("news_title") and path:
                # Promote the likely driver if a recent direct-looking news item exists.
                reasons = list(path.get("driver_reasons") or [])
                reasons.append(f"يوجد خبر/محفز حديث: {news.get('news_title')[:160]}")
                if bool(news.get("news_is_catalyst")):
                    path["likely_driver"] = "محفز خبري/شركة مع حركة فنية"
                    path["driver_confidence"] = "عالية" if path.get("driver_confidence") in {"متوسطة", "عالية"} else "متوسطة"
                elif path.get("likely_driver") == "غير واضح":
                    path["likely_driver"] = "خبر محتمل أو سياق خبري"
                    path["driver_confidence"] = "منخفضة"
                path["driver_reasons"] = reasons[:8]
        item = dict(mover)
        item.update({
            "rank": idx + 1,
            "appeared_status": status,
            "appeared_reason": reason,
            "opportunity_group": group,
            "promotion_timeline": timeline_summary,
            "timing_status": timeline_summary.get("timing_status", ""),
            "timing_label": timeline_summary.get("timing_label", ""),
            "promotion_issue": timeline_summary.get("promotion_issue", ""),
            "first_source_gain_pct": timeline_summary.get("first_source_gain_pct", 0),
            "first_display_gain_pct": timeline_summary.get("first_display_gain_pct", 0),
            "first_entry_gain_pct": timeline_summary.get("first_entry_gain_pct", 0),
            "sharia_status": str(sh.get("status", "") or ""),
            "sharia_label": str(sh.get("label", "") or ""),
            "sharia_reason": str(sh.get("reason", "") or "")[:500],
            "trigger_date": path.get("trigger_date", ""),
            "trigger_gap_pct": path.get("trigger_gap_pct", 0),
            "trigger_day_change_pct": path.get("trigger_day_change_pct", 0),
            "trigger_volume": path.get("trigger_volume", 0),
            "trigger_volume_multiple": path.get("trigger_volume_multiple", 0),
            "close_strength": path.get("close_strength", 0),
            "likely_driver": path.get("likely_driver", "غير واضح"),
            "driver_confidence": path.get("driver_confidence", "منخفضة"),
            "driver_reasons": path.get("driver_reasons", []),
            "news_title": news.get("news_title", ""),
            "news_sentiment": news.get("news_sentiment", ""),
            "news_age_label": news.get("news_age_label", ""),
        })
        enriched.append(item)

    # Persist the computed mover summary/cache.
    if _enabled():
        try:
            with _LOCK:
                with _connect() as conn:
                    for item in enriched:
                        conn.execute(
                            """
                            INSERT INTO missed_weekly_movers(
                                week_key, symbol, baseline_date, end_date, baseline_price, end_price, max_high,
                                weekly_gain_pct, max_gain_pct, trigger_date, trigger_gap_pct, trigger_day_change_pct,
                                trigger_volume, trigger_volume_multiple, close_strength, likely_driver, driver_confidence,
                                driver_reasons_json, appeared_status, appeared_reason, sharia_status, sharia_label,
                                sharia_reason, news_title, news_sentiment, news_age_label, updated_ts
                            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(week_key, symbol) DO UPDATE SET
                                baseline_date=excluded.baseline_date,
                                end_date=excluded.end_date,
                                baseline_price=excluded.baseline_price,
                                end_price=excluded.end_price,
                                max_high=excluded.max_high,
                                weekly_gain_pct=excluded.weekly_gain_pct,
                                max_gain_pct=excluded.max_gain_pct,
                                trigger_date=excluded.trigger_date,
                                trigger_gap_pct=excluded.trigger_gap_pct,
                                trigger_day_change_pct=excluded.trigger_day_change_pct,
                                trigger_volume=excluded.trigger_volume,
                                trigger_volume_multiple=excluded.trigger_volume_multiple,
                                close_strength=excluded.close_strength,
                                likely_driver=excluded.likely_driver,
                                driver_confidence=excluded.driver_confidence,
                                driver_reasons_json=excluded.driver_reasons_json,
                                appeared_status=excluded.appeared_status,
                                appeared_reason=excluded.appeared_reason,
                                sharia_status=excluded.sharia_status,
                                sharia_label=excluded.sharia_label,
                                sharia_reason=excluded.sharia_reason,
                                news_title=excluded.news_title,
                                news_sentiment=excluded.news_sentiment,
                                news_age_label=excluded.news_age_label,
                                updated_ts=excluded.updated_ts
                            """,
                            (
                                week_key, item["symbol"], item.get("baseline_date", ""), item.get("end_date", ""), _safe_float(item.get("baseline_price")), _safe_float(item.get("end_price")), _safe_float(item.get("max_high")),
                                _safe_float(item.get("weekly_gain_pct")), _safe_float(item.get("max_gain_pct")), item.get("trigger_date", ""), _safe_float(item.get("trigger_gap_pct")), _safe_float(item.get("trigger_day_change_pct")),
                                _safe_float(item.get("trigger_volume")), _safe_float(item.get("trigger_volume_multiple")), _safe_float(item.get("close_strength")), item.get("likely_driver", ""), item.get("driver_confidence", ""),
                                _json_dumps(item.get("driver_reasons") or []), item.get("appeared_status", ""), item.get("appeared_reason", ""), item.get("sharia_status", ""), item.get("sharia_label", ""),
                                item.get("sharia_reason", ""), item.get("news_title", ""), item.get("news_sentiment", ""), item.get("news_age_label", ""), _now_ts(),
                            ),
                        )
                    conn.commit()
        except Exception:
            pass

    thresholds = {}
    for t in MISSED_WEEKLY_GAIN_THRESHOLDS:
        thresholds[f"gte_{t}"] = len([x for x in enriched if max(float(x.get("max_gain_pct", 0) or 0), float(x.get("weekly_gain_pct", 0) or 0)) >= t])

    clean_missed = [x for x in enriched if x.get("opportunity_group") in {"not_in_source", "in_source", "in_deep_universe"} and not str(x.get("sharia_status", "")).startswith("non") and x.get("appeared_status") not in {"appeared"}]
    report = {
        "ok": True,
        "enabled": bool(MISSED_OPPORTUNITIES_ENABLED),
        "version": "missed_opportunities_v1",
        "week_key": week_key,
        "week_start": week_start,
        "week_end": week_end,
        "baseline_date": baseline_date,
        "end_date": end_date,
        "threshold_pct": threshold,
        "polygon_week_days_loaded": [d for d, _m in week_maps],
        "seen_symbols": len(seen),
        "source_symbols": len(source),
        "movers_count": len(enriched),
        "threshold_counts": thresholds,
        "news_lookup_count": news_used,
        "status_counts": _count_by(enriched, "appeared_status"),
        "opportunity_group_counts": _count_by(enriched, "opportunity_group"),
        "sharia_counts": _count_by(enriched, "sharia_label"),
        "likely_driver_counts": _count_by(enriched, "likely_driver"),
        "timing_status_counts": _count_by(enriched, "timing_status"),
        "promotion_issue_counts": _count_by([x for x in enriched if x.get("promotion_issue")], "promotion_issue"),
        "important_missed_count": len(clean_missed),
        "summary_note": "تقرير تشخيصي فقط: لا يغير التصنيف أو الفلتر الشرعي أو السعر الحي.",
    }
    if include_items:
        report["items"] = enriched
        report["important_missed"] = clean_missed[:80]
    else:
        report["top_items"] = enriched[:20]
        report["important_missed_sample"] = clean_missed[:20]
    return report


def build_missed_weekly_brief(week_key: str | None = None, threshold: float | None = None, include_items: bool = True) -> str:
    r = build_missed_weekly_report(week_key=week_key, threshold=threshold, include_items=True, include_news=True, limit=MISSED_MOVER_LIMIT)
    if not r.get("ok"):
        return "تقرير الفرص الفائتة غير جاهز\n" + json.dumps(r, ensure_ascii=False, indent=2)
    lines = []
    lines.append("تقرير Missed Opportunities Review V1")
    lines.append(f"الأسبوع: {r.get('week_key')}")
    lines.append(f"فترة القياس: من إغلاق {r.get('baseline_date')} إلى {r.get('end_date')}")
    lines.append(f"حد الصعود: +{safe_round(r.get('threshold_pct'), 1)}%")
    lines.append("")
    lines.append("الملخص:")
    lines.append(f"- الأسهم الصاعدة فوق الحد: {r.get('movers_count')}")
    lines.append(f"- رموز ظهرت في الأداة هذا الأسبوع: {r.get('seen_symbols')}")
    lines.append(f"- رموز دخلت المنبع/التحليل هذا الأسبوع: {r.get('source_symbols')}")
    lines.append(f"- فرص فائتة مهمة مبدئيًا: {r.get('important_missed_count')}")
    lines.append("")
    lines.append("توزيع حالة الظهور:")
    for k, v in (r.get("status_counts") or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("أكثر أسباب الصعود المرجحة:")
    for k, v in sorted((r.get("likely_driver_counts") or {}).items(), key=lambda x: x[1], reverse=True)[:8]:
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("حالة التوقيت والترقية:")
    for k, v in sorted((r.get("timing_status_counts") or {}).items(), key=lambda x: x[1], reverse=True)[:10]:
        lines.append(f"- {k}: {v}")
    issues = r.get("promotion_issue_counts") or {}
    if issues:
        lines.append("مشاكل الترقية المتكررة:")
        for k, v in sorted(issues.items(), key=lambda x: x[1], reverse=True)[:8]:
            lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("أهم الفرص الفائتة أو غير الملتقطة:")
    important = r.get("important_missed") or r.get("important_missed_sample") or []
    if not important:
        lines.append("- لا توجد فرص فائتة مهمة واضحة ضمن الحد الحالي.")
    for item in important[:20]:
        reasons = item.get("driver_reasons") or []
        reason_text = "؛ ".join([str(x) for x in reasons[:3]]) if reasons else "غير واضح"
        lines.append(
            f"- {item.get('symbol')} | أعلى صعود: {safe_round(item.get('max_gain_pct'), 1)}% | "
            f"الإغلاق الأسبوعي: {safe_round(item.get('weekly_gain_pct'), 1)}% | "
            f"الحالة: {item.get('appeared_reason')} | التوقيت: {item.get('timing_label') or 'غير متوفر'} | "
            f"الشرعية: {item.get('sharia_label') or item.get('sharia_status') or 'غير معروف'} | "
            f"السبب المرجح: {item.get('likely_driver')} ({item.get('driver_confidence')}) - {reason_text}"
        )
    lines.append("")
    lines.append("ملاحظة: السبب المعروض مرجح آليًا من السعر/الحجم/الأخبار المتاحة، وليس سببًا مؤكدًا إلا عند وجود محفز واضح.")
    return "\n".join(lines)




def build_symbol_timeline_report(symbol: str, week_key: str | None = None, threshold: float | None = None) -> dict:
    """Return a compact, single-symbol diagnostic timeline."""
    sym = _clean_symbol(symbol)
    week_key, _ws, _we = _week_parts(week_key)
    if not sym:
        return {"ok": False, "error": "missing_symbol"}
    init_missed_opportunities_db()
    seen, source = _seen_and_source_maps(week_key)
    timeline = _timeline_for_symbol(week_key, sym)
    timeline_summary = _timeline_summary(timeline)
    mover = None
    try:
        report = build_missed_weekly_report(week_key=week_key, threshold=threshold or 10.0, include_items=True, include_news=True, limit=1000)
        for item in report.get("items", []) if isinstance(report, dict) else []:
            if str(item.get("symbol") or "").upper() == sym:
                mover = item
                break
    except Exception as exc:
        mover = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {
        "ok": True,
        "version": "missed_opportunities_timeline_v1",
        "symbol": sym,
        "week_key": week_key,
        "seen_summary": seen.get(sym, {}),
        "source_summary": source.get(sym, {}),
        "promotion_timeline": timeline_summary,
        "pre_move_evidence": _pre_move_snapshots_for_symbol(week_key, sym),
        "pre_move_summary": _early_evidence_summary(_pre_move_snapshots_for_symbol(week_key, sym)),
        "weekly_mover_match": mover or {},
        "brief": build_symbol_timeline_brief(sym, week_key=week_key, threshold=threshold),
    }


def build_symbol_timeline_brief(symbol: str, week_key: str | None = None, threshold: float | None = None) -> str:
    sym = _clean_symbol(symbol)
    week_key, _ws, _we = _week_parts(week_key)
    timeline = _timeline_for_symbol(week_key, sym)
    summary = _timeline_summary(timeline)
    lines = [f"تقرير خط زمني للسهم {sym}", f"الأسبوع: {week_key}", ""]
    lines.append(f"حكم التوقيت: {summary.get('timing_label') or 'غير متوفر'}")
    if summary.get("promotion_issue"):
        lines.append(f"ملاحظة ترقية: {summary.get('promotion_issue')}")
    lines.append("")
    lines.append("خط الظهور:")
    lines.extend(_timeline_brief_lines(sym, summary))
    evidence = _pre_move_snapshots_for_symbol(week_key, sym)
    ev_summary = _early_evidence_summary(evidence)
    if evidence:
        lines.append("")
        lines.append("لقطات ما قبل/أثناء الحركة:")
        lines.append(f"- الحكم: {ev_summary.get('status')}")
        for snap in (ev_summary.get('key_snapshots') or [])[:6]:
            line = _snapshot_line(snap)
            if line:
                lines.append(f"  • {line}")
    try:
        r = build_missed_weekly_report(week_key=week_key, threshold=threshold or 10.0, include_items=True, include_news=True, limit=1000)
        match = None
        for item in r.get("items", []) if isinstance(r, dict) else []:
            if str(item.get("symbol") or "").upper() == sym:
                match = item
                break
        if match:
            lines.append("")
            lines.append("حركة الأسبوع:")
            lines.append(f"- أعلى صعود: {safe_round(match.get('max_gain_pct'),1)}% | الإغلاق الأسبوعي: {safe_round(match.get('weekly_gain_pct'),1)}%")
            lines.append(f"- سبب الصعود المرجح: {match.get('likely_driver')} ({match.get('driver_confidence')})")
            reasons = match.get("driver_reasons") or []
            for rr in reasons[:5]:
                lines.append(f"  • {rr}")
    except Exception:
        pass
    return "\n".join(lines)



def build_pre_move_evidence_report(
    week_key: str | None = None,
    threshold: float | None = None,
    format: str = "json",
    limit: int = 120,
) -> dict | str:
    """Review what the tool knew before/around the first move.

    This report is intentionally diagnostic. It does not fetch live quotes, does
    not call AI, and does not change radar results. It combines weekly movers
    with the light evidence snapshots saved during source/display events.
    """
    week_key, _ws, _we = _week_parts(week_key)
    threshold = float(threshold or 10.0)
    # Reuse the existing mover computation/cache. Keep items bounded.
    weekly = build_missed_weekly_report(week_key=week_key, threshold=threshold, include_items=True, include_news=False, limit=min(max(int(limit or 120), 20), 1000))
    if not isinstance(weekly, dict) or not weekly.get("ok"):
        return weekly if str(format).lower() == "json" else "تقرير Pre-Move Evidence غير جاهز\n" + json.dumps(weekly, ensure_ascii=False, indent=2)
    snap_map = _pre_move_snapshots_map(week_key)
    items = []
    counts = {
        "with_snapshots": 0,
        "before_5pct": 0,
        "before_10pct": 0,
        "late_first_entry": 0,
        "source_but_no_early_evidence": 0,
        "no_snapshots": 0,
    }
    for item in (weekly.get("items") or [])[: int(limit or 120)]:
        sym = str(item.get("symbol") or "").upper()
        snaps = snap_map.get(sym, [])
        evs = _early_evidence_summary(snaps)
        if evs.get("has_snapshots"):
            counts["with_snapshots"] += 1
        else:
            counts["no_snapshots"] += 1
        if evs.get("has_before_5pct"):
            counts["before_5pct"] += 1
        if evs.get("has_before_10pct"):
            counts["before_10pct"] += 1
        first_entry = evs.get("first_entry") or {}
        entry_gain = _timeline_gain_or_none(first_entry.get("gain_pct"), first_entry.get("price")) if first_entry else None
        if entry_gain is not None and entry_gain >= MISSED_LATE_PROMOTION_PCT:
            counts["late_first_entry"] += 1
        if item.get("appeared_status") in {"source_not_deep", "deep_universe_not_displayed"} and not evs.get("has_before_5pct"):
            counts["source_but_no_early_evidence"] += 1
        items.append({
            "symbol": sym,
            "max_gain_pct": item.get("max_gain_pct"),
            "weekly_gain_pct": item.get("weekly_gain_pct"),
            "appeared_status": item.get("appeared_status"),
            "timing_status": item.get("timing_status"),
            "timing_label": item.get("timing_label"),
            "likely_driver": item.get("likely_driver"),
            "driver_confidence": item.get("driver_confidence"),
            "driver_reasons": item.get("driver_reasons") or [],
            "evidence_status": evs.get("status"),
            "has_before_5pct": evs.get("has_before_5pct", False),
            "has_before_10pct": evs.get("has_before_10pct", False),
            "key_snapshots": evs.get("key_snapshots") or [],
        })
    if str(format or "json").lower() in {"brief", "text", "txt"}:
        lines = ["تقرير Pre-Move Evidence / ماذا عرفنا قبل الصعود؟", f"الأسبوع: {week_key}", f"حد الصعود: +{safe_round(threshold,1)}%", ""]
        lines.append("الملخص:")
        lines.append(f"- عدد الأسهم/الحركات المفحوصة: {len(items)}")
        lines.append(f"- لديها لقطات محفوظة: {counts['with_snapshots']}")
        lines.append(f"- ظهرت لها إشارة محفوظة قبل/حول +5%: {counts['before_5pct']}")
        lines.append(f"- ظهرت لها إشارة محفوظة قبل/حول +10%: {counts['before_10pct']}")
        lines.append(f"- أول دخول جاء متأخرًا بعد +{safe_round(MISSED_LATE_PROMOTION_PCT,1)}%: {counts['late_first_entry']}")
        lines.append(f"- دخلت المنبع/التحليل بلا لقطة مبكرة كافية: {counts['source_but_no_early_evidence']}")
        lines.append("")
        lines.append("أهم الحالات:")
        # Prioritize late entries and big movers first.
        def sort_key(x: dict):
            late = 1 if str(x.get("evidence_status") or "").startswith("أول دخول جاء متأخر") else 0
            return (late, float(x.get("max_gain_pct") or 0))
        for item in sorted(items, key=sort_key, reverse=True)[:30]:
            lines.append(f"- {item.get('symbol')} | أعلى صعود {safe_round(item.get('max_gain_pct'),1)}% | {item.get('evidence_status') or 'لا توجد لقطات'}")
            lines.append(f"  • التوقيت: {item.get('timing_label') or 'غير متوفر'} | السبب المرجح: {item.get('likely_driver')} ({item.get('driver_confidence')})")
            for snap in (item.get("key_snapshots") or [])[:4]:
                line = _snapshot_line(snap)
                if line:
                    lines.append(f"  • {line}")
        lines.append("")
        lines.append("ملاحظة: هذا التقرير للتعلم فقط. لا يغير التصنيف ولا يطلب AI ولا يحفظ كل تكرار؛ يحفظ أول لقطة لكل حدث/مرحلة.")
        return "\n".join(lines)
    return {"ok": True, "week_key": week_key, "threshold_pct": threshold, "counts": counts, "items": items[: int(limit or 120)]}


def build_late_promotions_report(week_key: str | None = None, threshold: float | None = None, format: str = "json") -> dict | str:
    """List movers that were promoted/displayed after a large move or were stuck in watch/source."""
    r = build_missed_weekly_report(week_key=week_key, threshold=threshold or 10.0, include_items=True, include_news=True, limit=1000)
    if not isinstance(r, dict) or not r.get("ok"):
        return r if str(format).lower() == "json" else "تقرير الترقية المتأخرة غير جاهز\n" + json.dumps(r, ensure_ascii=False, indent=2)
    rows = []
    for item in r.get("items", []) or []:
        st = str(item.get("timing_status") or "")
        issue = str(item.get("promotion_issue") or "")
        if st in {"very_late_entry", "late_entry", "late_watch_only", "watch_only_not_promoted", "source_not_promoted", "deep_not_displayed"} or issue:
            rows.append(item)
    rows.sort(key=lambda x: (float(x.get("first_entry_gain_pct") or 0), float(x.get("max_gain_pct") or 0)), reverse=True)
    if str(format or "json").lower() in {"brief", "text", "txt"}:
        lines = ["تقرير الترقية المتأخرة / الفرص التي ظهرت بعد الحركة", f"الأسبوع: {r.get('week_key')}", ""]
        lines.append(f"عدد الحالات المهمة: {len(rows)}")
        lines.append("")
        for item in rows[:40]:
            lines.append(f"- {item.get('symbol')} | أعلى صعود {safe_round(item.get('max_gain_pct'),1)}% | {item.get('timing_label')}")
            if item.get("promotion_issue"):
                lines.append(f"  • {item.get('promotion_issue')}")
            timeline = item.get("promotion_timeline") or {}
            for line in _timeline_brief_lines(str(item.get("symbol")), timeline)[:4]:
                lines.append(line)
            lines.append(f"  • سبب الصعود المرجح: {item.get('likely_driver')} ({item.get('driver_confidence')})")
        return "\n".join(lines)
    return {"ok": True, "week_key": r.get("week_key"), "count": len(rows), "items": rows[:200], "timing_status_counts": r.get("timing_status_counts"), "promotion_issue_counts": r.get("promotion_issue_counts")}


def _tracking_loss_rows(week_key: str, limit: int = 5000) -> list[dict]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracking_signals
                WHERE week_key=? AND (outcome_group IN ('loss','stopped','failed') OR status IN ('stopped','broken_before_activation') OR stopped_at!='')
                ORDER BY updated_at_ts DESC
                LIMIT ?
                """,
                (week_key, int(limit or 5000)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _tracking_all_rows(week_key: str, limit: int = 12000) -> list[dict]:
    """Return tracked signals for denominator/base-rate analysis.

    This is diagnostic-only. It lets the loss report compare a risk factor's
    failures against all times that factor appeared, instead of counting the
    factor inside losing rows only.
    """
    try:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracking_signals
                WHERE week_key=?
                ORDER BY updated_at_ts DESC
                LIMIT ?
                """,
                (week_key, int(limit or 12000)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _is_loss_row(row: dict) -> bool:
    outcome = str((row or {}).get("outcome_group") or "").strip().lower()
    status = str((row or {}).get("status") or "").strip().lower()
    stopped_at = str((row or {}).get("stopped_at") or "").strip()
    return bool(outcome in {"loss", "stopped", "failed"} or status in {"stopped", "broken_before_activation"} or stopped_at)


def _is_target_row(row: dict) -> bool:
    outcome = str((row or {}).get("outcome_group") or "").strip().lower()
    status = str((row or {}).get("status") or "").strip().lower()
    return bool(
        outcome in {"target", "target_hit", "win", "winner", "target_1", "target_2", "hit"}
        or status in {"target_hit", "target_2_hit", "above_target"}
        or str((row or {}).get("target_hit_at") or "").strip()
        or str((row or {}).get("target_2_hit_at") or "").strip()
    )


def _is_partial_gain_row(row: dict) -> bool:
    outcome = str((row or {}).get("outcome_group") or "").strip().lower()
    status = str((row or {}).get("status") or "").strip().lower()
    return bool(outcome in {"partial_gain", "green", "gain"} or status in {"partial_gain", "above_entry", "active_gain"})


def _loss_stage(row: dict) -> tuple[str, str]:
    activated = bool(str(row.get("activated_at") or "").strip())
    stopped = bool(str(row.get("stopped_at") or "").strip())
    minutes_to_stop = _safe_float(row.get("minutes_to_stop"), 0.0)
    minutes_to_activation = _safe_float(row.get("minutes_to_activation"), 0.0)
    if stopped and activated:
        duration = max(minutes_to_stop - minutes_to_activation, 0.0)
        candles_5m = int(round(duration / 5.0)) if duration > 0 else 0
        return "بعد التفعيل/بعد الاختراق", f"ضرب الوقف بعد التفعيل؛ استمر تقريبًا {safe_round(duration,1)} دقيقة (~{candles_5m} شموع 5د تقريبًا)"
    if stopped and not activated:
        return "قبل التفعيل", "كسر الخطة أو الوقف قبل تأكيد الدخول"
    if not activated:
        return "لم يتفعل", "لم يصل إلى نقطة الدخول أو اختفى قبل التفعيل"
    return "غير محدد", "الخسارة مسجلة لكن مرحلة الفشل غير واضحة"


def _risk_factor_tags(item: dict) -> list[str]:
    """Risk/context factors that can be counted against all appearances.

    These are denominator-safe factors: they describe the setup at/around
    appearance, not the post-failure result. For example, "قرب من قمة تاريخية"
    can be compared as: appeared N times, lost M times. Failure-only reasons
    such as "كسر الدعم" stay in _loss_reason_tags and should not be treated as
    base-rate risk factors.
    """
    tags = [str(x) for x in (item.get("risk_tags") or []) if str(x).strip()]
    out: list[str] = []

    def add(text: str) -> None:
        text = str(text or "").strip()
        if text and text not in out:
            out.append(text)

    for tag in tags:
        # Keep useful pre-existing risk tags, but skip failure-result tags that
        # do not have a meaningful denominator across all signals.
        if any(x in tag for x in ["كسر الدعم", "ضرب الوقف", "فشل سريع", "الخطة مكسورة"]):
            continue
        add(tag)

    res_dist = _safe_float(item.get("nearest_resistance_distance_pct"), 999.0)
    res_strength = str(item.get("nearest_resistance_strength") or "")
    if res_dist <= 1.25 and any(x in res_strength for x in ["قوي", "strong", "very"]):
        add("قريب من مقاومة قوية")
    elif res_dist <= 1.0:
        add("قريب من مقاومة")

    if _safe_float(item.get("distance_to_ath_pct"), 999.0) <= 3.0:
        add("قرب من قمة تاريخية")
    if _safe_float(item.get("distance_to_52w_high_pct"), 999.0) <= 3.0:
        add("قرب من قمة سنوية")
    if _safe_float(item.get("volatility_pct"), 0.0) >= 8.0:
        add("تذبذب عالي")
    if bool(item.get("is_late_above_entry")):
        add("دخول متأخر فوق نقطة الدخول")
    if bool(item.get("is_entry_far")) or _safe_float(item.get("entry_distance_pct"), 0.0) >= 3.0:
        add("نقطة الدخول بعيدة")

    market = str(item.get("market_support_label") or "")
    sector = str(item.get("sector_support_label") or "")
    if any(x in market for x in ["غير داعم", "ضعيف", "سلبي", "هبوط"]):
        add("السوق غير داعم")
    if any(x in sector for x in ["غير داعم", "ضعيف", "سلبي", "هبوط"]):
        add("القطاع غير داعم")

    return out[:10]


def _loss_reason_tags(item: dict) -> list[str]:
    """Build practical loss-reason tags from existing tracking fields only."""
    out: list[str] = []

    def add(text: str) -> None:
        text = str(text or "").strip()
        if text and text not in out:
            out.append(text)

    # Include denominator-safe risk/context factors first.
    for tag in _risk_factor_tags(item):
        add(tag)

    # Then include failure-result tags, which are useful for loss diagnosis but
    # should not be interpreted as base-rate risk factors.
    for tag in [str(x) for x in (item.get("risk_tags") or []) if str(x).strip()]:
        if any(x in tag for x in ["كسر الدعم", "ضرب الوقف", "فشل سريع", "الخطة مكسورة"]):
            add(tag)

    if "بعد التفعيل" in str(item.get("failure_stage") or "") and _safe_float(item.get("max_gain_pct"), 0.0) <= 0.5:
        add("فشل سريع بعد التفعيل")

    return out[:10]


def _tracking_item_from_row(r: dict) -> dict:
    """Normalize one tracking row for loss/base-rate diagnostics."""
    stage, stage_note = _loss_stage(r)
    risk_tags = _json_loads((r or {}).get("risk_tags_json"), []) or []
    snapshot = _json_loads((r or {}).get("snapshot_json"), {}) or {}
    return {
        "symbol": (r or {}).get("symbol"),
        "bucket": (r or {}).get("signal_bucket"),
        "label": (r or {}).get("signal_label"),
        "status": (r or {}).get("status"),
        "status_label": (r or {}).get("status_label"),
        "outcome_group": (r or {}).get("outcome_group"),
        "first_seen_at": (r or {}).get("first_seen_at"),
        "activated_at": (r or {}).get("activated_at"),
        "stopped_at": (r or {}).get("stopped_at"),
        "target_hit_at": (r or {}).get("target_hit_at"),
        "target_2_hit_at": (r or {}).get("target_2_hit_at"),
        "entry_price": safe_round((r or {}).get("entry_price"), 4),
        "stop_loss": safe_round((r or {}).get("stop_loss"), 4),
        "target_price": safe_round((r or {}).get("target_price"), 4),
        "max_gain_pct": safe_round((r or {}).get("max_gain_pct"), 2),
        "max_loss_pct": safe_round((r or {}).get("max_loss_pct"), 2),
        "minutes_to_activation": safe_round((r or {}).get("minutes_to_activation"), 1),
        "minutes_to_stop": safe_round((r or {}).get("minutes_to_stop"), 1),
        "failure_stage": stage,
        "failure_stage_note": stage_note,
        "risk_tags": risk_tags,
        "plan_family": (r or {}).get("plan_family", ""),
        "signal_reason": (r or {}).get("signal_reason", ""),
        "nearest_resistance_distance_pct": safe_round((r or {}).get("nearest_resistance_distance_pct"), 2),
        "nearest_resistance_strength": (r or {}).get("nearest_resistance_strength", ""),
        "distance_to_52w_high_pct": safe_round((r or {}).get("distance_to_52w_high_pct"), 2),
        "distance_to_ath_pct": safe_round((r or {}).get("distance_to_ath_pct"), 2),
        "volatility_pct": safe_round((r or {}).get("volatility_pct"), 2),
        "market_support_label": (r or {}).get("market_support_label", ""),
        "sector_support_label": (r or {}).get("sector_support_label", ""),
        "entry_distance_pct": safe_round((r or {}).get("entry_distance_pct"), 2),
        "is_late_above_entry": bool((r or {}).get("is_late_above_entry")),
        "is_entry_far": bool((r or {}).get("is_entry_far")),
        "snapshot_note": snapshot.get("quick_explainer") or snapshot.get("ai_summary") or "",
    }


def _build_risk_factor_base_rates(all_rows: list[dict], loss_rows: list[dict]) -> list[dict]:
    """Compare each risk factor with its own denominator across all tracked signals."""
    all_ids_by_factor: dict[str, set[str]] = {}
    loss_ids_by_factor: dict[str, set[str]] = {}
    target_ids_by_factor: dict[str, set[str]] = {}
    partial_ids_by_factor: dict[str, set[str]] = {}
    not_activated_ids_by_factor: dict[str, set[str]] = {}

    loss_ids = {str((r or {}).get("id") or f"{(r or {}).get('symbol')}|{idx}") for idx, r in enumerate(loss_rows)}

    for idx, r in enumerate(all_rows or []):
        rid = str((r or {}).get("id") or f"{(r or {}).get('symbol')}|{idx}")
        item = _tracking_item_from_row(r)
        factors = _risk_factor_tags(item)
        if not factors:
            continue
        is_loss = rid in loss_ids or _is_loss_row(r)
        is_target = _is_target_row(r)
        is_partial = _is_partial_gain_row(r)
        is_not_activated = not bool(str((r or {}).get("activated_at") or "").strip()) and not is_loss and not is_target
        for f in factors:
            all_ids_by_factor.setdefault(f, set()).add(rid)
            if is_loss:
                loss_ids_by_factor.setdefault(f, set()).add(rid)
            if is_target:
                target_ids_by_factor.setdefault(f, set()).add(rid)
            if is_partial:
                partial_ids_by_factor.setdefault(f, set()).add(rid)
            if is_not_activated:
                not_activated_ids_by_factor.setdefault(f, set()).add(rid)

    out: list[dict] = []
    total_all = max(len(all_rows or []), 1)
    total_losses = max(len(loss_rows or []), 1)
    global_loss_rate = (len(loss_rows or []) / total_all) * 100.0
    for factor, ids in all_ids_by_factor.items():
        appearances = len(ids)
        if appearances <= 0:
            continue
        losses = len(loss_ids_by_factor.get(factor, set()))
        targets = len(target_ids_by_factor.get(factor, set()))
        partials = len(partial_ids_by_factor.get(factor, set()))
        not_activated = len(not_activated_ids_by_factor.get(factor, set()))
        failure_rate = (losses / appearances) * 100.0 if appearances else 0.0
        exposure_rate = (appearances / total_all) * 100.0 if total_all else 0.0
        share_of_losses = (losses / total_losses) * 100.0 if total_losses else 0.0
        out.append({
            "factor": factor,
            "appearances": appearances,
            "losses": losses,
            "targets": targets,
            "partial_gains": partials,
            "not_activated": not_activated,
            "failure_rate_pct": safe_round(failure_rate, 1),
            "exposure_rate_pct": safe_round(exposure_rate, 1),
            "share_of_losses_pct": safe_round(share_of_losses, 1),
            "global_loss_rate_pct": safe_round(global_loss_rate, 1),
            "risk_delta_pct": safe_round(failure_rate - global_loss_rate, 1),
        })

    out.sort(key=lambda x: (_safe_float(x.get("risk_delta_pct"), 0.0), _safe_float(x.get("failure_rate_pct"), 0.0), int(x.get("appearances") or 0)), reverse=True)
    return out


def _loss_group_key(item: dict) -> tuple:
    """Group loss-analysis by symbol for the brief report.

    Previous versions grouped by symbol + label + plan + entry/stop/target. That
    preserved detail, but it made the weekly report nearly as long as the raw
    rows. For the brief diagnostic report we need one row per symbol, then show
    the important ranges and counts inside that row.
    """
    return (str(item.get("symbol") or "").upper(),)


def _merge_counter(dst: dict, key: Any, inc: int = 1) -> None:
    text = str(key or "").strip()
    if not text:
        return
    dst[text] = int(dst.get(text, 0) or 0) + int(inc or 1)


def _append_unique_limited(dst: list, value: Any, limit: int = 8) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if text not in dst and len(dst) < limit:
        dst.append(text)


def _sorted_counter_items(counter: dict, limit: int = 6) -> list[tuple[str, int]]:
    return [(str(k), int(v)) for k, v in sorted((counter or {}).items(), key=lambda x: x[1], reverse=True)[:limit]]


def _range_text(values: list[float], digits: int = 2) -> str:
    nums = [float(v) for v in values if _safe_float(v, 0.0) > 0]
    if not nums:
        return "غير متوفر"
    lo, hi = min(nums), max(nums)
    if abs(lo - hi) < 0.0001:
        return str(safe_round(lo, digits))
    return f"{safe_round(lo, digits)}–{safe_round(hi, digits)}"


def _summarize_loss_groups(items: list[dict]) -> list[dict]:
    """Summarize loss rows into one practical diagnostic row per symbol.

    The goal is not to hide detail, but to compress repeated appearances of the
    same ticker so the weekly report shows patterns that can improve the tool:
    which symbols repeatedly failed, at which stage, and which reasons recur.
    """
    groups: dict[tuple, dict] = {}
    for item in items:
        key = _loss_group_key(item)
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        g = groups.get(key)
        if not g:
            g = {
                "symbol": symbol,
                "signals_count": 0,
                "first_seen_at": "",
                "last_seen_at": "",
                "activated_count": 0,
                "stopped_count": 0,
                "pre_activation_fail_count": 0,
                "post_activation_fail_count": 0,
                "not_activated_count": 0,
                "bucket_counts": {},
                "label_counts": {},
                "plan_counts": {},
                "stage_counts": {},
                "reason_counts": {},
                "entry_prices": [],
                "stop_prices": [],
                "target_prices": [],
                "best_max_gain_pct": -9999.0,
                "worst_max_loss_pct": 9999.0,
                "min_minutes_to_stop": None,
                "max_minutes_to_stop": None,
                "min_minutes_to_activation": None,
                "max_minutes_to_activation": None,
                "sample_reasons": [],
                "sample_notes": [],
                "risk_examples": [],
            }
            groups[key] = g

        g["signals_count"] += 1
        fs = str(item.get("first_seen_at") or "")
        if fs and (not g["first_seen_at"] or fs < g["first_seen_at"]):
            g["first_seen_at"] = fs
        if fs and (not g["last_seen_at"] or fs > g["last_seen_at"]):
            g["last_seen_at"] = fs

        activated = bool(str(item.get("activated_at") or "").strip())
        stopped = bool(str(item.get("stopped_at") or "").strip())
        if activated:
            g["activated_count"] += 1
        if stopped:
            g["stopped_count"] += 1

        stage = str(item.get("failure_stage") or "غير محدد")
        _merge_counter(g["stage_counts"], stage)
        if "بعد التفعيل" in stage:
            g["post_activation_fail_count"] += 1
        elif "قبل التفعيل" in stage:
            g["pre_activation_fail_count"] += 1
        elif "لم يتفعل" in stage:
            g["not_activated_count"] += 1

        _merge_counter(g["bucket_counts"], item.get("bucket"))
        _merge_counter(g["label_counts"], item.get("label"))
        _merge_counter(g["plan_counts"], item.get("plan_family"))

        ep = _safe_float(item.get("entry_price"), 0.0)
        sp = _safe_float(item.get("stop_loss"), 0.0)
        tp = _safe_float(item.get("target_price"), 0.0)
        if ep > 0:
            g["entry_prices"].append(ep)
        if sp > 0:
            g["stop_prices"].append(sp)
        if tp > 0:
            g["target_prices"].append(tp)

        for reason in _loss_reason_tags(item):
            _merge_counter(g["reason_counts"], reason)
            _append_unique_limited(g["risk_examples"], reason, limit=8)

        g["best_max_gain_pct"] = max(g["best_max_gain_pct"], _safe_float(item.get("max_gain_pct"), -9999.0))
        g["worst_max_loss_pct"] = min(g["worst_max_loss_pct"], _safe_float(item.get("max_loss_pct"), 9999.0))

        mts = _safe_float(item.get("minutes_to_stop"), 0.0)
        if mts > 0:
            g["min_minutes_to_stop"] = mts if g["min_minutes_to_stop"] is None else min(g["min_minutes_to_stop"], mts)
            g["max_minutes_to_stop"] = mts if g["max_minutes_to_stop"] is None else max(g["max_minutes_to_stop"], mts)
        mta = _safe_float(item.get("minutes_to_activation"), 0.0)
        if mta > 0:
            g["min_minutes_to_activation"] = mta if g["min_minutes_to_activation"] is None else min(g["min_minutes_to_activation"], mta)
            g["max_minutes_to_activation"] = mta if g["max_minutes_to_activation"] is None else max(g["max_minutes_to_activation"], mta)

        sr = str(item.get("signal_reason") or "").strip()
        if sr:
            _append_unique_limited(g["sample_reasons"], sr[:220], limit=3)
        sn = str(item.get("snapshot_note") or "").strip()
        if sn:
            _append_unique_limited(g["sample_notes"], sn[:220], limit=2)

    out = []
    for g in groups.values():
        if g["best_max_gain_pct"] <= -9999:
            g["best_max_gain_pct"] = 0.0
        if g["worst_max_loss_pct"] >= 9999:
            g["worst_max_loss_pct"] = 0.0
        g["top_failure_stage"] = max(g["stage_counts"].items(), key=lambda x: x[1])[0] if g["stage_counts"] else "غير محدد"
        g["top_reasons"] = [k for k, _v in _sorted_counter_items(g["reason_counts"], limit=6)]
        g["top_labels"] = _sorted_counter_items(g["label_counts"], limit=5)
        g["top_buckets"] = _sorted_counter_items(g["bucket_counts"], limit=5)
        g["top_plans"] = _sorted_counter_items(g["plan_counts"], limit=5)
        g["entry_range"] = _range_text(g.get("entry_prices") or [], digits=4)
        g["stop_range"] = _range_text(g.get("stop_prices") or [], digits=4)
        g["target_range"] = _range_text(g.get("target_prices") or [], digits=4)

        # A simple severity score for ordering the brief report: repeated failures,
        # real stops after activation, and larger adverse move matter most.
        g["severity_score"] = (
            int(g.get("signals_count") or 0) * 2
            + int(g.get("stopped_count") or 0) * 3
            + int(g.get("post_activation_fail_count") or 0) * 4
            + abs(_safe_float(g.get("worst_max_loss_pct"), 0.0))
        )
        out.append(g)

    out.sort(key=lambda x: (_safe_float(x.get("severity_score"), 0.0), int(x.get("signals_count") or 0)), reverse=True)
    return out


def _first_counter_label(items: list[tuple[str, int]], default: str = "غير متوفر") -> str:
    if not items:
        return default
    k, v = items[0]
    return f"{k}×{v}" if v else str(k)


def build_loss_analysis_report(
    week_key: str | None = None,
    format: str = "json",
    limit: int = 500,
    detail: str = "summary",
    top: int = 20,
) -> dict | str:
    """Analyze losing tracked signals without changing tracking logic.

    Default brief output is intentionally compact: it shows the patterns and the
    most important grouped symbols, while JSON/full detail remains available for
    deeper audits. This keeps a full-week report readable without hiding the
    signals needed to improve the tool.
    """
    week_key, _ws, _we = _week_parts(week_key)
    rows = _tracking_loss_rows(week_key, limit=limit)
    # Use a wider denominator than the displayed loss limit, so factor failure
    # rates are not computed against losses only. This is still read-only and
    # does not affect tracking/radar behavior.
    all_limit = max(int(limit or 500) * 8, 5000)
    all_rows = _tracking_all_rows(week_key, limit=all_limit)
    items = []
    for r in rows:
        item = _tracking_item_from_row(r)
        item["derived_loss_reasons"] = _loss_reason_tags(item)
        items.append(item)

    risk_base_rates = _build_risk_factor_base_rates(all_rows, rows)
    stage_counts = _count_by(items, "failure_stage")
    bucket_counts = _count_by(items, "bucket")
    tag_counts: dict[str, int] = {}
    for item in items:
        for t in item.get("derived_loss_reasons") or []:
            tag_counts[str(t)] = int(tag_counts.get(str(t), 0) or 0) + 1
    grouped = _summarize_loss_groups(items)

    fmt = str(format or "json").lower()
    detail_mode = str(detail or "summary").strip().lower()
    try:
        top_n = max(5, min(int(top or 20), 80))
    except Exception:
        top_n = 20
    if detail_mode in {"full", "verbose", "raw"}:
        top_n = max(top_n, 50)

    if fmt in {"brief", "text", "txt"}:
        lines = ["تقرير خسائر الإشارات / لماذا ظهرت ثم فشلت", f"الأسبوع: {week_key}", ""]
        lines.append(f"عدد الإشارات الخاسرة الخام: {len(items)}")
        lines.append(f"عدد الأسهم بعد التجميع: {len(grouped)}")
        lines.append(f"المعروض في المختصر: أهم {min(top_n, len(grouped))} سهم فقط")
        lines.append("")

        lines.append("ملخص مراحل الفشل:")
        for k, v in sorted(stage_counts.items(), key=lambda x: x[1], reverse=True)[:6]:
            lines.append(f"- {k}: {v}")

        if tag_counts:
            lines.append("")
            lines.append("أكثر أسباب الخسارة/الخطر داخل الخسائر فقط:")
            for k, v in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
                lines.append(f"- {k}: {v}")
            lines.append("ملاحظة: هذه أعداد داخل الخسائر فقط، وليست نسبة فشل العامل.")

        if risk_base_rates:
            lines.append("")
            lines.append("نسبة فشل عوامل الخطر مقارنة بكل مرات ظهورها:")
            for rbr in risk_base_rates[:10]:
                factor = rbr.get("factor")
                lines.append(
                    f"- {factor}: ظهر {rbr.get('appearances')} | خسر {rbr.get('losses')} "
                    f"| فشل {rbr.get('failure_rate_pct')}% "
                    f"| متوسط فشل عام {rbr.get('global_loss_rate_pct')}% "
                    f"| فرق {rbr.get('risk_delta_pct')} نقطة"
                )

        # Compact interpretation, not a scoring change.
        lines.append("")
        lines.append("قراءة سريعة للتطوير:")
        top_stage = sorted(stage_counts.items(), key=lambda x: x[1], reverse=True)[0] if stage_counts else ("غير محدد", 0)
        lines.append(f"- أكثر مرحلة فشل: {top_stage[0]} ({top_stage[1]})")
        if tag_counts:
            top_tags = [f"{k} ({v})" for k, v in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:4]]
            lines.append(f"- أكثر الأنماط داخل الخسائر: {'، '.join(top_tags)}")
        if risk_base_rates:
            top_risk = risk_base_rates[0]
            lines.append(
                f"- أعلى عامل خطر حسب المقام: {top_risk.get('factor')} "
                f"فشل {top_risk.get('failure_rate_pct')}% من {top_risk.get('appearances')} ظهور"
            )
        repeat_heavy = [g for g in grouped if int(g.get("signals_count") or 0) >= 5]
        if repeat_heavy:
            lines.append(f"- أسهم تكررت خسارتها 5 مرات أو أكثر: {len(repeat_heavy)}")

        lines.append("")
        lines.append("أهم الأسهم المجمعة:")
        for idx, g in enumerate(grouped[:top_n], start=1):
            labels_txt = "، ".join([f"{k}×{v}" for k, v in (g.get("top_labels") or [])[:2]]) or "غير متوفر"
            plans_txt = "، ".join([f"{k}×{v}" for k, v in (g.get("top_plans") or [])[:2] if k]) or "غير متوفر"
            reasons = [str(x) for x in (g.get("top_reasons") or [])[:3]]
            reasons_txt = "؛ ".join(reasons) if reasons else "غير واضح"
            duration_txt = ""
            if g.get("min_minutes_to_stop") is not None:
                if g.get("min_minutes_to_stop") == g.get("max_minutes_to_stop"):
                    duration_txt = f" | مدة الوقف {safe_round(g.get('min_minutes_to_stop'),1)}د"
                else:
                    duration_txt = f" | مدة الوقف {safe_round(g.get('min_minutes_to_stop'),1)}–{safe_round(g.get('max_minutes_to_stop'),1)}د"
            lines.append(
                f"{idx}. {g.get('symbol')} | تكرر {g.get('signals_count')} | "
                f"تفعيل/وقف {g.get('activated_count')}/{g.get('stopped_count')} | "
                f"فشل: {g.get('top_failure_stage')}{duration_txt}"
            )
            lines.append(
                f"   السبب: {reasons_txt} | التصنيف: {labels_txt} | الخطة: {plans_txt} | "
                f"صعود/هبوط: {safe_round(g.get('best_max_gain_pct'),2)}% / {safe_round(g.get('worst_max_loss_pct'),2)}%"
            )
            if detail_mode in {"full", "verbose", "raw"}:
                lines.append(
                    f"   الدخول {g.get('entry_range')} | الوقف {g.get('stop_range')} | الهدف {g.get('target_range')}"
                )
                if g.get("sample_reasons"):
                    lines.append(f"   لماذا ظهر؟ {g.get('sample_reasons')[0]}")

        if detail_mode not in {"full", "verbose", "raw"}:
            lines.append("")
            lines.append("للتفاصيل الكاملة أضف: &detail=full")
        return "\n".join(lines)

    return {
        "ok": True,
        "week_key": week_key,
        "raw_count": len(items),
        "all_tracked_count_for_denominator": len(all_rows),
        "grouped_count": len(grouped),
        "stage_counts": stage_counts,
        "bucket_counts": bucket_counts,
        "loss_reason_counts": tag_counts,
        "risk_factor_base_rates": risk_base_rates,
        "grouped_items": grouped[:int(limit or 500)],
        "raw_items_sample": items[:80],
    }

def missed_status() -> dict:
    out = {
        "ok": False,
        "enabled": bool(MISSED_OPPORTUNITIES_ENABLED),
        "sqlite_enabled": bool(SQLITE_ENABLED),
        "db_path": SQLITE_DB_PATH,
        "initialized": bool(_INITIALIZED),
        "week_key": get_performance_week_key(),
    }
    if not _enabled():
        out["ok"] = True
        return out
    try:
        init_missed_opportunities_db()
        week_key = get_performance_week_key()
        with _connect() as conn:
            seen = conn.execute("SELECT COUNT(*) AS c FROM missed_seen_symbols WHERE week_key=?", (week_key,)).fetchone()
            source = conn.execute("SELECT COUNT(*) AS c FROM missed_source_candidates WHERE week_key=?", (week_key,)).fetchone()
            movers = conn.execute("SELECT COUNT(*) AS c FROM missed_weekly_movers WHERE week_key=?", (week_key,)).fetchone()
            evidence = conn.execute("SELECT COUNT(*) AS c FROM missed_pre_move_snapshots WHERE week_key=?", (week_key,)).fetchone()
        out.update({"ok": True, "initialized": True, "seen_symbols": int(seen["c"] if seen else 0), "source_symbols": int(source["c"] if source else 0), "cached_weekly_movers": int(movers["c"] if movers else 0), "pre_move_snapshots": int(evidence["c"] if evidence else 0)})
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return out


def export_missed_json(week_key: str | None = None, threshold: float | None = None, include_items: bool = True, limit: int = 5000) -> dict:
    return build_missed_weekly_report(week_key=week_key, threshold=threshold, include_items=include_items, include_news=True, limit=min(max(1, int(limit or 5000)), 5000))


def export_missed_csv(week_key: str | None = None, threshold: float | None = None, limit: int = 5000) -> str:
    r = build_missed_weekly_report(week_key=week_key, threshold=threshold, include_items=True, include_news=True, limit=min(max(1, int(limit or 5000)), 5000))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "symbol", "max_gain_pct", "weekly_gain_pct", "baseline_price", "end_price", "appeared_status", "appeared_reason", "opportunity_group",
        "timing_status", "timing_label", "promotion_issue", "first_source_gain_pct", "first_display_gain_pct", "first_entry_gain_pct",
        "sharia_label", "sharia_status", "likely_driver", "driver_confidence", "driver_reasons", "trigger_date", "trigger_gap_pct", "trigger_day_change_pct",
        "trigger_volume_multiple", "news_title", "news_sentiment", "news_age_label",
    ])
    for item in r.get("items", []) if isinstance(r, dict) else []:
        writer.writerow([
            item.get("symbol", ""), item.get("max_gain_pct", 0), item.get("weekly_gain_pct", 0), item.get("baseline_price", 0), item.get("end_price", 0),
            item.get("appeared_status", ""), item.get("appeared_reason", ""), item.get("opportunity_group", ""),
            item.get("timing_status", ""), item.get("timing_label", ""), item.get("promotion_issue", ""), item.get("first_source_gain_pct", 0), item.get("first_display_gain_pct", 0), item.get("first_entry_gain_pct", 0),
            item.get("sharia_label", ""), item.get("sharia_status", ""), item.get("likely_driver", ""), item.get("driver_confidence", ""),
            " | ".join([str(x) for x in (item.get("driver_reasons") or [])]), item.get("trigger_date", ""), item.get("trigger_gap_pct", 0), item.get("trigger_day_change_pct", 0),
            item.get("trigger_volume_multiple", 0), item.get("news_title", ""), item.get("news_sentiment", ""), item.get("news_age_label", ""),
        ])
    return output.getvalue()


