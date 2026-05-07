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
    """Normalize percent fields that may arrive as 12.3 or 0.123."""
    val = _safe_float(value, 0.0)
    if val == 0:
        return 0.0
    # Some internal metrics store day_change_pct as a decimal fraction.
    if abs(val) <= 1.5:
        return val * 100.0
    return val


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
    gain_f = _safe_float(gain_pct, 0.0)
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
                "change_pct": _safe_float(metrics.get("live_change_pct") or metrics.get("fmp_change_pct") or metrics.get("day_change_pct")) * (100 if abs(_safe_float(metrics.get("day_change_pct"))) <= 1.5 and metrics.get("day_change_pct") is not None else 1),
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
    return {
        "first_seen_at": row.get("first_seen_at", ""),
        "last_seen_at": row.get("last_seen_at", ""),
        "times_seen": _safe_int(row.get("times_seen")),
        "first_price": safe_round(row.get("first_price"), 4),
        "last_price": safe_round(row.get("last_price"), 4),
        "first_gain_pct": safe_round(row.get("first_gain_pct"), 2),
        "last_gain_pct": safe_round(row.get("last_gain_pct"), 2),
        "max_gain_pct_seen": safe_round(row.get("max_gain_pct"), 2),
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
    first_entry_gain = _safe_float(first_entry.get("first_gain_pct"), 0.0) if first_entry else 0.0
    first_source_gain = _safe_float(first_source.get("first_gain_pct"), 0.0) if first_source else 0.0
    first_display_gain = _safe_float(first_display.get("first_gain_pct"), 0.0) if first_display else 0.0

    timing_status = "no_timeline"
    timing_label = "لا يوجد خط زمني محفوظ بعد"
    if first_entry:
        if first_entry_gain >= big_threshold:
            timing_status = "very_late_entry"
            timing_label = f"ظهر كفرصة دخول بعد حركة كبيرة جدًا (+{safe_round(first_entry_gain,1)}%)"
        elif first_entry_gain >= late_threshold:
            timing_status = "late_entry"
            timing_label = f"ظهر كفرصة دخول بعد ارتفاع كبير نسبيًا (+{safe_round(first_entry_gain,1)}%)"
        else:
            timing_status = "early_or_reasonable_entry"
            timing_label = f"ظهر كفرصة دخول عند ارتفاع مبكر/معقول (+{safe_round(first_entry_gain,1)}%)"
    elif first_watch:
        watch_gain = _safe_float(first_watch.get("first_gain_pct"), 0.0)
        if watch_gain >= big_threshold:
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
        timing_label = f"دخل المنبع عند +{safe_round(first_source_gain,1)}% ولم يترقَّ للقوائم"

    promotion_issue = ""
    if first_source and first_entry and first_source_gain < 8 and first_entry_gain >= late_threshold:
        promotion_issue = "كان معروفًا للمنبع مبكرًا لكنه ترقّى متأخرًا"
    elif first_watch and first_entry and _safe_float(first_watch.get("first_gain_pct"),0) < 8 and first_entry_gain >= late_threshold:
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
        lines.append(
            f"  - {ar}: أول ظهور {row.get('first_seen_at') or '؟'} | صعود وقتها {safe_round(row.get('first_gain_pct'),1)}% | "
            f"أفضل ترتيب #{row.get('best_rank') or row.get('first_rank') or '؟'} | مرات الظهور {row.get('times_seen') or 0}"
        )
    if not lines:
        lines.append("  - لا يوجد خط زمني محفوظ لهذا السهم بعد.")
    return lines
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


def build_loss_analysis_report(week_key: str | None = None, format: str = "json", limit: int = 500) -> dict | str:
    """Analyze losing tracked signals without changing tracking logic."""
    week_key, _ws, _we = _week_parts(week_key)
    rows = _tracking_loss_rows(week_key, limit=limit)
    items = []
    for r in rows:
        stage, stage_note = _loss_stage(r)
        risk_tags = _json_loads(r.get("risk_tags_json"), []) or []
        snapshot = _json_loads(r.get("snapshot_json"), {}) or {}
        items.append({
            "symbol": r.get("symbol"),
            "bucket": r.get("signal_bucket"),
            "label": r.get("signal_label"),
            "status": r.get("status"),
            "status_label": r.get("status_label"),
            "outcome_group": r.get("outcome_group"),
            "first_seen_at": r.get("first_seen_at"),
            "activated_at": r.get("activated_at"),
            "stopped_at": r.get("stopped_at"),
            "entry_price": safe_round(r.get("entry_price"), 4),
            "stop_loss": safe_round(r.get("stop_loss"), 4),
            "target_price": safe_round(r.get("target_price"), 4),
            "max_gain_pct": safe_round(r.get("max_gain_pct"), 2),
            "max_loss_pct": safe_round(r.get("max_loss_pct"), 2),
            "minutes_to_activation": safe_round(r.get("minutes_to_activation"), 1),
            "minutes_to_stop": safe_round(r.get("minutes_to_stop"), 1),
            "failure_stage": stage,
            "failure_stage_note": stage_note,
            "risk_tags": risk_tags,
            "plan_family": r.get("plan_family", ""),
            "signal_reason": r.get("signal_reason", ""),
            "nearest_resistance_distance_pct": safe_round(r.get("nearest_resistance_distance_pct"), 2),
            "nearest_resistance_strength": r.get("nearest_resistance_strength", ""),
            "market_support_label": r.get("market_support_label", ""),
            "sector_support_label": r.get("sector_support_label", ""),
            "entry_distance_pct": safe_round(r.get("entry_distance_pct"), 2),
            "is_late_above_entry": bool(r.get("is_late_above_entry")),
            "is_entry_far": bool(r.get("is_entry_far")),
            "snapshot_note": snapshot.get("quick_explainer") or snapshot.get("ai_summary") or "",
        })
    stage_counts = _count_by(items, "failure_stage")
    bucket_counts = _count_by(items, "bucket")
    # Count repeated risk tags.
    tag_counts: dict[str, int] = {}
    for item in items:
        for t in item.get("risk_tags") or []:
            tag_counts[str(t)] = int(tag_counts.get(str(t), 0) or 0) + 1
    if str(format or "json").lower() in {"brief", "text", "txt"}:
        lines = ["تقرير خسائر الإشارات / لماذا ظهرت ثم فشلت", f"الأسبوع: {week_key}", ""]
        lines.append(f"عدد الإشارات الخاسرة المسجلة: {len(items)}")
        lines.append("مراحل الفشل:")
        for k, v in sorted(stage_counts.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"- {k}: {v}")
        if tag_counts:
            lines.append("أكثر وسوم الخطر تكرارًا:")
            for k, v in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:12]:
                lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("أمثلة مهمة:")
        for item in items[:40]:
            lines.append(f"- {item.get('symbol')} | {item.get('label')} | {item.get('failure_stage')} | {item.get('failure_stage_note')}")
            if item.get("risk_tags"):
                lines.append(f"  • مخاطر: {'؛ '.join([str(x) for x in item.get('risk_tags')[:4]])}")
            lines.append(f"  • سبب الظهور: {str(item.get('signal_reason') or '')[:180]}")
        return "\n".join(lines)
    return {"ok": True, "week_key": week_key, "count": len(items), "stage_counts": stage_counts, "bucket_counts": bucket_counts, "risk_tag_counts": tag_counts, "items": items[:int(limit or 500)]}

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
        out.update({"ok": True, "initialized": True, "seen_symbols": int(seen["c"] if seen else 0), "source_symbols": int(source["c"] if source else 0), "cached_weekly_movers": int(movers["c"] if movers else 0)})
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
