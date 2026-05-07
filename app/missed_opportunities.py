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
            for row in rows or []:
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
            f"الحالة: {item.get('appeared_reason')} | الشرعية: {item.get('sharia_label') or item.get('sharia_status') or 'غير معروف'} | "
            f"السبب المرجح: {item.get('likely_driver')} ({item.get('driver_confidence')}) - {reason_text}"
        )
    lines.append("")
    lines.append("ملاحظة: السبب المعروض مرجح آليًا من السعر/الحجم/الأخبار المتاحة، وليس سببًا مؤكدًا إلا عند وجود محفز واضح.")
    return "\n".join(lines)


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
        "sharia_label", "sharia_status", "likely_driver", "driver_confidence", "driver_reasons", "trigger_date", "trigger_gap_pct", "trigger_day_change_pct",
        "trigger_volume_multiple", "news_title", "news_sentiment", "news_age_label",
    ])
    for item in r.get("items", []) if isinstance(r, dict) else []:
        writer.writerow([
            item.get("symbol", ""), item.get("max_gain_pct", 0), item.get("weekly_gain_pct", 0), item.get("baseline_price", 0), item.get("end_price", 0),
            item.get("appeared_status", ""), item.get("appeared_reason", ""), item.get("opportunity_group", ""),
            item.get("sharia_label", ""), item.get("sharia_status", ""), item.get("likely_driver", ""), item.get("driver_confidence", ""),
            " | ".join([str(x) for x in (item.get("driver_reasons") or [])]), item.get("trigger_date", ""), item.get("trigger_gap_pct", 0), item.get("trigger_day_change_pct", 0),
            item.get("trigger_volume_multiple", 0), item.get("news_title", ""), item.get("news_sentiment", ""), item.get("news_age_label", ""),
        ])
    return output.getvalue()
