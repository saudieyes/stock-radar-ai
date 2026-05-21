"""Evidence Collection Layer V2 for Stock Radar AI.

Purpose
-------
Collect market evidence during the week without changing the radar decision logic.
This layer stores observations from:
- tool signals / last saved radar snapshot;
- FMP big daily gainers, even when they never entered the tool;
- optional Polygon intraday candle summaries for a limited subset.

It is intentionally passive: no scoring, Sharia filtering, ranking, Telegram, or UI
classification changes are made here. The data is meant to be reviewed the next
weekend to discover recurring winning and losing patterns.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, date, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

from .github_sync import is_github_sync_configured, push_json_file, push_text_file, fetch_json_file, fetch_text_file, push_multiple_files
from .live_quotes import get_live_quotes
from .market_fear import get_market_fear_snapshot, market_fear_status
from .performance_tracker import get_performance_week_key, get_performance_week_window
from .settings import (
    DATA_DIR,
    FMP_API_KEY,
    HTTP_SESSION,
    POLYGON_API_KEY,
)
from .sqlite_store import SQLITE_DB_PATH, SQLITE_ENABLED, get_json, set_json
from .utils import safe_round, to_float

NY_TZ = ZoneInfo("America/New_York")
RIYADH_TZ = ZoneInfo("Asia/Riyadh")
_LOCK = threading.RLock()
_INITIALIZED = False
_WORKER_THREAD: threading.Thread | None = None
_WORKER_STARTED = False


def _env_bool(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return float(default)


EVIDENCE_COLLECTION_ENABLED = _env_bool("EVIDENCE_COLLECTION_ENABLED", True)
EVIDENCE_BACKGROUND_WORKER_ENABLED = _env_bool("EVIDENCE_BACKGROUND_WORKER_ENABLED", True)
EVIDENCE_GITHUB_AUTO_SYNC_ENABLED = _env_bool("EVIDENCE_GITHUB_AUTO_SYNC_ENABLED", True)
EVIDENCE_AUTO_SYNC_RIYADH_HOUR = max(0, min(_env_int("EVIDENCE_AUTO_SYNC_RIYADH_HOUR", 5), 23))
EVIDENCE_AUTO_SYNC_RIYADH_MINUTE = max(0, min(_env_int("EVIDENCE_AUTO_SYNC_RIYADH_MINUTE", 45), 59))
EVIDENCE_AUTO_SYNC_STATE_VERSION = "v5c"
EVIDENCE_BIG_MOVERS_ENABLED = _env_bool("EVIDENCE_BIG_MOVERS_ENABLED", True)
EVIDENCE_POLYGON_ENABLED = _env_bool("EVIDENCE_POLYGON_ENABLED", True)
EVIDENCE_BIG_MOVER_THRESHOLD_PCT = _env_float("EVIDENCE_BIG_MOVER_THRESHOLD_PCT", 10.0)
EVIDENCE_MAX_TOOL_SYMBOLS = _env_int("EVIDENCE_MAX_TOOL_SYMBOLS", 220)
EVIDENCE_MAX_BIG_MOVERS = _env_int("EVIDENCE_MAX_BIG_MOVERS", 120)
EVIDENCE_MAX_SYMBOLS_PER_RUN = _env_int("EVIDENCE_MAX_SYMBOLS_PER_RUN", 260)
EVIDENCE_POLYGON_SYMBOL_LIMIT = _env_int("EVIDENCE_POLYGON_SYMBOL_LIMIT", 45)
# V2: deeper evidence collection for next-week pattern mining.
EVIDENCE_AUTO_BACKFILL_WINNERS_ENABLED = _env_bool("EVIDENCE_AUTO_BACKFILL_WINNERS_ENABLED", True)
EVIDENCE_BIG_WINNER_BACKFILL_ENABLED = _env_bool("EVIDENCE_BIG_WINNER_BACKFILL_ENABLED", True)
EVIDENCE_BIG_WINNER_BACKFILL_SYMBOL_LIMIT = _env_int("EVIDENCE_BIG_WINNER_BACKFILL_SYMBOL_LIMIT", 180)
EVIDENCE_AUTO_BACKFILL_SYMBOL_LIMIT = _env_int("EVIDENCE_AUTO_BACKFILL_SYMBOL_LIMIT", 80)
EVIDENCE_INTRADAY_BAR_STORE_ENABLED = _env_bool("EVIDENCE_INTRADAY_BAR_STORE_ENABLED", True)
EVIDENCE_INTRADAY_BAR_SYMBOL_LIMIT = _env_int("EVIDENCE_INTRADAY_BAR_SYMBOL_LIMIT", 90)
EVIDENCE_MIN_WINNER_DOLLAR_VOLUME = _env_float("EVIDENCE_MIN_WINNER_DOLLAR_VOLUME", 0.0)
EVIDENCE_INTERVAL_PREMARKET_SEC = _env_int("EVIDENCE_INTERVAL_PREMARKET_SEC", 600)
EVIDENCE_INTERVAL_OPEN_SEC = _env_int("EVIDENCE_INTERVAL_OPEN_SEC", 900)
EVIDENCE_INTERVAL_AFTERHOURS_SEC = _env_int("EVIDENCE_INTERVAL_AFTERHOURS_SEC", 1800)
EVIDENCE_INTERVAL_CLOSED_SEC = _env_int("EVIDENCE_INTERVAL_CLOSED_SEC", 21600)
EVIDENCE_GITHUB_ARCHIVE_PATH = str(os.getenv("GITHUB_EVIDENCE_ARCHIVE_PATH", "app_data/evidence_archive") or "app_data/evidence_archive").strip().strip("/")
EVIDENCE_FMP_BASE_URL = str(os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com") or "https://financialmodelingprep.com").rstrip("/")
EVIDENCE_HTTP_TIMEOUT_SEC = _env_float("EVIDENCE_HTTP_TIMEOUT_SEC", 9.0)
EVIDENCE_RETENTION_KEEP_DAYS = _env_int("EVIDENCE_RETENTION_KEEP_DAYS", 14)
EVIDENCE_RETENTION_PRUNE_ENABLED = _env_bool("EVIDENCE_RETENTION_PRUNE_ENABLED", False)
EVIDENCE_RETENTION_REQUIRE_VERIFY = _env_bool("EVIDENCE_RETENTION_REQUIRE_VERIFY", True)

# Railway stability guard. Defaults are intentionally conservative because the
# evidence archive can become large enough to cause GitHub timeouts, high egress,
# and memory pressure when serialized as one huge JSON/CSV payload.
EVIDENCE_RAILWAY_STABILITY_GUARD_ENABLED = _env_bool("EVIDENCE_RAILWAY_STABILITY_GUARD_ENABLED", True)
EVIDENCE_GITHUB_COMPACT_SYNC = _env_bool("EVIDENCE_GITHUB_COMPACT_SYNC", True)
EVIDENCE_SYNC_INCLUDE_CSV_DEFAULT = _env_bool("EVIDENCE_SYNC_INCLUDE_CSV_DEFAULT", False)
EVIDENCE_EXPORT_MAX_ROWS = _env_int("EVIDENCE_EXPORT_MAX_ROWS", 5000)
EVIDENCE_SYNC_SAMPLE_ROWS = _env_int("EVIDENCE_SYNC_SAMPLE_ROWS", 1500)
EVIDENCE_SYNC_WINNER_LIMIT = _env_int("EVIDENCE_SYNC_WINNER_LIMIT", 800)
EVIDENCE_SYNC_BAR_SAMPLE_LIMIT = _env_int("EVIDENCE_SYNC_BAR_SAMPLE_LIMIT", 0)
EVIDENCE_WORKER_LEASE_TTL_SEC = _env_int("EVIDENCE_WORKER_LEASE_TTL_SEC", 300)
EVIDENCE_AUTO_BACKFILL_STORE_BARS = _env_bool("EVIDENCE_AUTO_BACKFILL_STORE_BARS", False)
EVIDENCE_RETENTION_VACUUM_AFTER_PRUNE = _env_bool("EVIDENCE_RETENTION_VACUUM_AFTER_PRUNE", False)


def _now_ts() -> float:
    return time.time()


def _now_dt() -> datetime:
    return datetime.now(NY_TZ)


def _now_text() -> str:
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _today_text() -> str:
    return _now_dt().strftime("%Y-%m-%d")


def _clean_symbol(value: Any) -> str:
    sym = str(value or "").upper().strip().replace(" ", "")
    if not sym:
        return ""
    if not all(ch.isalnum() or ch in {".", "-"} for ch in sym):
        return ""
    return sym[:24]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            txt = value.replace("%", "").replace(",", "").strip()
            if not txt:
                return default
            return float(txt)
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return int(default)


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




def _ensure_table_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    """Add missing columns to an existing SQLite table without destructive migrations."""
    try:
        existing = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl in (columns or {}).items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    except Exception:
        # Evidence is passive. A migration hiccup must not break the live radar.
        pass

def init_evidence_db() -> bool:
    """Create Evidence Collection tables. Safe to call repeatedly."""
    global _INITIALIZED
    if not SQLITE_ENABLED:
        return False
    if _INITIALIZED:
        return True
    with _LOCK:
        if _INITIALIZED:
            return True
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL DEFAULT '',
                    week_key TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL DEFAULT '',
                    captured_at REAL NOT NULL,
                    captured_at_text TEXT NOT NULL DEFAULT '',
                    session TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    source_group TEXT NOT NULL DEFAULT '',
                    in_tool_snapshot INTEGER NOT NULL DEFAULT 0,
                    in_big_movers INTEGER NOT NULL DEFAULT 0,
                    signal_bucket TEXT NOT NULL DEFAULT '',
                    decision TEXT NOT NULL DEFAULT '',
                    sharia_status TEXT NOT NULL DEFAULT '',
                    plan_family TEXT NOT NULL DEFAULT '',
                    price REAL NOT NULL DEFAULT 0,
                    previous_close REAL NOT NULL DEFAULT 0,
                    change_pct REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    dollar_volume REAL NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL DEFAULT 0,
                    target_price REAL NOT NULL DEFAULT 0,
                    stop_loss REAL NOT NULL DEFAULT 0,
                    support_price REAL NOT NULL DEFAULT 0,
                    resistance_price REAL NOT NULL DEFAULT 0,
                    distance_from_entry_pct REAL NOT NULL DEFAULT 0,
                    distance_from_support_pct REAL NOT NULL DEFAULT 0,
                    distance_from_resistance_pct REAL NOT NULL DEFAULT 0,
                    gap_from_prev_close_pct REAL NOT NULL DEFAULT 0,
                    pre_market_change_pct REAL NOT NULL DEFAULT 0,
                    pre_market_volume REAL NOT NULL DEFAULT 0,
                    pre_market_dollar_volume REAL NOT NULL DEFAULT 0,
                    after_hours_change_pct REAL NOT NULL DEFAULT 0,
                    open_gap_pct REAL NOT NULL DEFAULT 0,
                    first_15m_followthrough REAL NOT NULL DEFAULT 0,
                    first_30m_followthrough REAL NOT NULL DEFAULT 0,
                    held_above_open REAL NOT NULL DEFAULT 0,
                    held_above_vwap_proxy REAL NOT NULL DEFAULT 0,
                    gap_fade_flag INTEGER NOT NULL DEFAULT 0,
                    gap_retest_success INTEGER NOT NULL DEFAULT 0,
                    first_seen_change_pct REAL NOT NULL DEFAULT 0,
                    no_chase_flag INTEGER NOT NULL DEFAULT 0,
                    plan_needs_reconfirm INTEGER NOT NULL DEFAULT 0,
                    liquidity_score REAL NOT NULL DEFAULT 0,
                    momentum_acceleration_score REAL NOT NULL DEFAULT 0,
                    pattern_risk_score REAL NOT NULL DEFAULT 0,
                    risk_tags_json TEXT NOT NULL DEFAULT '[]',
                    success_tags_json TEXT NOT NULL DEFAULT '[]',
                    quote_source TEXT NOT NULL DEFAULT '',
                    price_source TEXT NOT NULL DEFAULT '',
                    polygon_summary_json TEXT NOT NULL DEFAULT '{}',
                    raw_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_big_movers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    price REAL NOT NULL DEFAULT 0,
                    change_pct REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    dollar_volume REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    in_tool_snapshot INTEGER NOT NULL DEFAULT 0,
                    tool_stage TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(trade_date, symbol)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_intraday_bars (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_key TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    bar_ts INTEGER NOT NULL,
                    bar_time_text TEXT NOT NULL DEFAULT '',
                    session TEXT NOT NULL DEFAULT '',
                    open REAL NOT NULL DEFAULT 0,
                    high REAL NOT NULL DEFAULT 0,
                    low REAL NOT NULL DEFAULT 0,
                    close REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    dollar_volume REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'polygon_5m',
                    run_id TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(trade_date, symbol, bar_ts)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_winner_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_key TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    winner_rank INTEGER NOT NULL DEFAULT 0,
                    winner_change_pct REAL NOT NULL DEFAULT 0,
                    previous_close REAL NOT NULL DEFAULT 0,
                    day_open REAL NOT NULL DEFAULT 0,
                    day_high REAL NOT NULL DEFAULT 0,
                    day_low REAL NOT NULL DEFAULT 0,
                    day_close REAL NOT NULL DEFAULT 0,
                    day_volume REAL NOT NULL DEFAULT 0,
                    day_dollar_volume REAL NOT NULL DEFAULT 0,
                    gap_pct REAL NOT NULL DEFAULT 0,
                    open_to_high_pct REAL NOT NULL DEFAULT 0,
                    close_vs_open_pct REAL NOT NULL DEFAULT 0,
                    pre_market_move_pct REAL NOT NULL DEFAULT 0,
                    pre_market_change_pct REAL NOT NULL DEFAULT 0,
                    pre_market_volume REAL NOT NULL DEFAULT 0,
                    pre_market_dollar_volume REAL NOT NULL DEFAULT 0,
                    after_hours_change_pct REAL NOT NULL DEFAULT 0,
                    previous_close_near_high REAL NOT NULL DEFAULT 0,
                    close_position_pct REAL NOT NULL DEFAULT 0,
                    late_day_volume_spike REAL NOT NULL DEFAULT 0,
                    open_gap_pct REAL NOT NULL DEFAULT 0,
                    first_15m_followthrough REAL NOT NULL DEFAULT 0,
                    first_30m_followthrough REAL NOT NULL DEFAULT 0,
                    held_above_open REAL NOT NULL DEFAULT 0,
                    held_above_vwap_proxy REAL NOT NULL DEFAULT 0,
                    gap_fade_flag INTEGER NOT NULL DEFAULT 0,
                    gap_retest_success INTEGER NOT NULL DEFAULT 0,
                    first_15m_gain_pct REAL NOT NULL DEFAULT 0,
                    first_30m_gain_pct REAL NOT NULL DEFAULT 0,
                    first_60m_gain_pct REAL NOT NULL DEFAULT 0,
                    first_30m_volume REAL NOT NULL DEFAULT 0,
                    first_60m_volume REAL NOT NULL DEFAULT 0,
                    last_30m_volume REAL NOT NULL DEFAULT 0,
                    volume_fade_flag INTEGER NOT NULL DEFAULT 0,
                    liquidity_acceleration_score REAL NOT NULL DEFAULT 0,
                    liquidity_persistence_score REAL NOT NULL DEFAULT 0,
                    gap_followthrough_label TEXT NOT NULL DEFAULT '',
                    move_quality_label TEXT NOT NULL DEFAULT '',
                    likely_pattern TEXT NOT NULL DEFAULT '',
                    tool_seen INTEGER NOT NULL DEFAULT 0,
                    tool_stage TEXT NOT NULL DEFAULT '',
                    tool_first_seen_at TEXT NOT NULL DEFAULT '',
                    tool_first_seen_change_pct REAL NOT NULL DEFAULT 0,
                    source_seen INTEGER NOT NULL DEFAULT 0,
                    data_quality TEXT NOT NULL DEFAULT '',
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL DEFAULT 0,
                    UNIQUE(trade_date, symbol)
                )
                """
            )

            _ensure_table_columns(conn, "evidence_winner_profiles", {
                "tradability_score": "REAL NOT NULL DEFAULT 0",
                "tradability_bucket": "TEXT NOT NULL DEFAULT ''",
                "tradability_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                "gap_quality_class": "TEXT NOT NULL DEFAULT ''",
                "gap_quality_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                "historical_visibility_json": "TEXT NOT NULL DEFAULT '{}'",
                "visibility_confidence_label": "TEXT NOT NULL DEFAULT ''",
                "first_source_seen_at": "TEXT NOT NULL DEFAULT ''",
                "first_source_gain_pct": "REAL NOT NULL DEFAULT 0",
                "first_deep_seen_at": "TEXT NOT NULL DEFAULT ''",
                "first_watch_seen_at": "TEXT NOT NULL DEFAULT ''",
                "first_cautious_seen_at": "TEXT NOT NULL DEFAULT ''",
                "first_strong_seen_at": "TEXT NOT NULL DEFAULT ''",
                "best_tool_stage": "TEXT NOT NULL DEFAULT ''",
                "promotion_delay_minutes": "REAL NOT NULL DEFAULT 0",
                "after_hours_change_pct": "REAL NOT NULL DEFAULT 0",
                "pre_market_change_pct": "REAL NOT NULL DEFAULT 0",
                "pre_market_dollar_volume": "REAL NOT NULL DEFAULT 0",
                "previous_close_near_high": "REAL NOT NULL DEFAULT 0",
                "close_position_pct": "REAL NOT NULL DEFAULT 0",
                "late_day_volume_spike": "REAL NOT NULL DEFAULT 0",
                "open_gap_pct": "REAL NOT NULL DEFAULT 0",
                "first_15m_followthrough": "REAL NOT NULL DEFAULT 0",
                "first_30m_followthrough": "REAL NOT NULL DEFAULT 0",
                "held_above_open": "REAL NOT NULL DEFAULT 0",
                "held_above_vwap_proxy": "REAL NOT NULL DEFAULT 0",
                "gap_fade_flag": "INTEGER NOT NULL DEFAULT 0",
                "gap_retest_success": "INTEGER NOT NULL DEFAULT 0",
            })

            _ensure_table_columns(conn, "evidence_snapshots", {
                "pre_market_change_pct": "REAL NOT NULL DEFAULT 0",
                "pre_market_volume": "REAL NOT NULL DEFAULT 0",
                "pre_market_dollar_volume": "REAL NOT NULL DEFAULT 0",
                "after_hours_change_pct": "REAL NOT NULL DEFAULT 0",
                "open_gap_pct": "REAL NOT NULL DEFAULT 0",
                "first_15m_followthrough": "REAL NOT NULL DEFAULT 0",
                "first_30m_followthrough": "REAL NOT NULL DEFAULT 0",
                "held_above_open": "REAL NOT NULL DEFAULT 0",
                "held_above_vwap_proxy": "REAL NOT NULL DEFAULT 0",
                "gap_fade_flag": "INTEGER NOT NULL DEFAULT 0",
                "gap_retest_success": "INTEGER NOT NULL DEFAULT 0",
            })


            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    started_at REAL NOT NULL,
                    finished_at REAL NOT NULL DEFAULT 0,
                    week_key TEXT NOT NULL DEFAULT '',
                    trade_date TEXT NOT NULL DEFAULT '',
                    session TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT '',
                    symbols_requested INTEGER NOT NULL DEFAULT 0,
                    snapshots_inserted INTEGER NOT NULL DEFAULT 0,
                    movers_inserted INTEGER NOT NULL DEFAULT 0,
                    polygon_symbols INTEGER NOT NULL DEFAULT 0,
                    github_synced INTEGER NOT NULL DEFAULT 0,
                    ok INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_snapshots_week_symbol ON evidence_snapshots(week_key, symbol, captured_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_snapshots_date_symbol ON evidence_snapshots(trade_date, symbol, captured_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_big_movers_date ON daily_big_movers(trade_date, change_pct DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_runs_week ON evidence_runs(week_key, started_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_bars_symbol_date ON evidence_intraday_bars(trade_date, symbol, bar_ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_winner_profiles_week ON evidence_winner_profiles(week_key, winner_change_pct DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_winner_profiles_date ON evidence_winner_profiles(trade_date, winner_change_pct DESC)")
            conn.commit()
        _INITIALIZED = True
        return True


def _market_session(now: datetime | None = None) -> str:
    now = now or _now_dt()
    if now.weekday() >= 5:
        return "closed_weekend"
    t = now.time()
    if dt_time(4, 0) <= t < dt_time(9, 30):
        return "pre_market"
    if dt_time(9, 30) <= t < dt_time(16, 0):
        return "regular"
    if dt_time(16, 0) <= t < dt_time(20, 0):
        return "after_hours"
    return "closed"


def _interval_for_session(session: str) -> int:
    if session == "pre_market":
        return max(120, int(EVIDENCE_INTERVAL_PREMARKET_SEC))
    if session == "regular":
        return max(180, int(EVIDENCE_INTERVAL_OPEN_SEC))
    if session == "after_hours":
        return max(300, int(EVIDENCE_INTERVAL_AFTERHOURS_SEC))
    return max(1800, int(EVIDENCE_INTERVAL_CLOSED_SEC))


def _current_week_key() -> str:
    try:
        return str(get_performance_week_key() or "")
    except Exception:
        return ""


def _last_trade_scan_rows() -> list[dict]:
    snap = get_json("last_trade_scan_snapshot", {})
    rows = snap.get("rows", []) if isinstance(snap, dict) else []
    return rows if isinstance(rows, list) else []


def _first_positive(row: dict, keys: list[str]) -> float:
    for key in keys:
        val = _safe_float((row or {}).get(key), 0.0)
        if val > 0:
            return val
    return 0.0


def _first_text(row: dict, keys: list[str]) -> str:
    for key in keys:
        val = (row or {}).get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x or "").strip()]
    if isinstance(value, str):
        if value.strip().startswith("["):
            loaded = _json_loads(value, [])
            if isinstance(loaded, list):
                return [str(x).strip() for x in loaded if str(x or "").strip()]
        return [x.strip() for x in value.replace(",", "|").split("|") if x.strip()]
    return []


def _pct_distance(price: float, ref: float) -> float:
    try:
        price = float(price or 0)
        ref = float(ref or 0)
        if price <= 0 or ref <= 0:
            return 0.0
        return safe_round(((price - ref) / ref) * 100.0, 2)
    except Exception:
        return 0.0


def _extract_tool_symbol_context(rows: list[dict], limit: int = EVIDENCE_MAX_TOOL_SYMBOLS) -> tuple[list[str], dict[str, dict]]:
    symbols: list[str] = []
    ctx: dict[str, dict] = {}
    for row in rows or []:
        sym = _clean_symbol((row or {}).get("symbol"))
        if not sym:
            continue
        if sym not in symbols:
            symbols.append(sym)
        ctx.setdefault(sym, row or {})
        if len(symbols) >= max(1, int(limit or EVIDENCE_MAX_TOOL_SYMBOLS)):
            break
    return symbols, ctx


def _fetch_fmp_big_movers(threshold_pct: float | None = None, limit: int | None = None) -> dict:
    """Fetch daily big gainers from FMP. Robust across old/stable endpoints."""
    threshold = float(threshold_pct if threshold_pct is not None else EVIDENCE_BIG_MOVER_THRESHOLD_PCT)
    lim = max(5, min(int(limit or EVIDENCE_MAX_BIG_MOVERS), 500))
    if not FMP_API_KEY:
        return {"ok": False, "configured": False, "items": [], "error": "fmp_api_key_missing"}

    endpoints = [
        f"{EVIDENCE_FMP_BASE_URL}/stable/biggest-gainers",
        f"{EVIDENCE_FMP_BASE_URL}/api/v3/stock_market/gainers",
    ]
    errors: list[str] = []
    for url in endpoints:
        try:
            r = HTTP_SESSION.get(url, params={"apikey": FMP_API_KEY}, timeout=float(EVIDENCE_HTTP_TIMEOUT_SEC))
            if r.status_code >= 400:
                errors.append(f"{url.split('/')[-1]}:{r.status_code}")
                continue
            payload = r.json()
            if isinstance(payload, dict):
                data = payload.get("data") or payload.get("gainers") or payload.get("items") or []
            else:
                data = payload
            out = []
            for raw in data or []:
                if not isinstance(raw, dict):
                    continue
                sym = _clean_symbol(raw.get("symbol") or raw.get("ticker"))
                if not sym:
                    continue
                price = _first_positive(raw, ["price", "lastPrice", "last", "close"])
                chg = _safe_float(raw.get("changesPercentage") or raw.get("changePercentage") or raw.get("change_pct") or raw.get("changes"), 0.0)
                # FMP may return strings like "15.2%"; _safe_float handles this.
                vol = _first_positive(raw, ["volume", "dayVolume", "avgVolume"])
                if chg < threshold:
                    continue
                out.append({
                    "symbol": sym,
                    "price": safe_round(price, 4),
                    "change_pct": safe_round(chg, 2),
                    "volume": safe_round(vol, 0),
                    "dollar_volume": safe_round(price * vol, 0) if price > 0 and vol > 0 else 0,
                    "source": "fmp_biggest_gainers" if "stable" in url else "fmp_stock_market_gainers",
                    "raw": raw,
                })
            out = sorted(out, key=lambda x: _safe_float(x.get("change_pct"), 0), reverse=True)[:lim]
            return {"ok": True, "configured": True, "threshold_pct": threshold, "items": out, "source_url": url}
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:140]}")
    return {"ok": False, "configured": True, "items": [], "error": "; ".join(errors[-3:])}


def _dt_from_polygon_ms(ms: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=NY_TZ)
    except Exception:
        return None


def _bar_session_from_dt(dt: datetime | None) -> str:
    if not dt:
        return "unknown"
    t = dt.time()
    if dt_time(4, 0) <= t < dt_time(9, 30):
        return "pre_market"
    if dt_time(9, 30) <= t < dt_time(16, 0):
        return "regular"
    if dt_time(16, 0) <= t < dt_time(20, 0):
        return "after_hours"
    return "closed"


def _bar_value(bar: dict, key: str) -> float:
    if not isinstance(bar, dict):
        return 0.0
    return _safe_float(bar.get(key), 0.0)


def _bars_between(bars: list[dict], start_h: int, start_m: int, end_h: int, end_m: int) -> list[dict]:
    out = []
    start_t = dt_time(start_h, start_m)
    end_t = dt_time(end_h, end_m)
    for bar in bars or []:
        dt = _dt_from_polygon_ms((bar or {}).get("t"))
        if not dt:
            continue
        t = dt.time()
        if start_t <= t < end_t:
            out.append(bar)
    return out


def _bar_open(bars: list[dict]) -> float:
    for bar in bars or []:
        val = _bar_value(bar, "o")
        if val > 0:
            return val
    return 0.0


def _bar_close(bars: list[dict]) -> float:
    for bar in reversed(bars or []):
        val = _bar_value(bar, "c")
        if val > 0:
            return val
    return 0.0


def _bars_high(bars: list[dict]) -> float:
    vals = [_bar_value(x, "h") for x in bars or [] if _bar_value(x, "h") > 0]
    return max(vals or [0.0])


def _bars_low(bars: list[dict]) -> float:
    vals = [_bar_value(x, "l") for x in bars or [] if _bar_value(x, "l") > 0]
    return min(vals or [0.0])


def _bars_volume(bars: list[dict]) -> float:
    return sum(_bar_value(x, "v") for x in bars or [])


def _bars_dollar_volume(bars: list[dict]) -> float:
    total = 0.0
    for b in bars or []:
        close = _bar_value(b, "c") or _bar_value(b, "vw") or _bar_value(b, "o")
        total += close * _bar_value(b, "v")
    return total


def _pct_change(a: float, b: float) -> float:
    try:
        a = float(a or 0)
        b = float(b or 0)
        if a <= 0 or b <= 0:
            return 0.0
        return safe_round(((a - b) / b) * 100.0, 2)
    except Exception:
        return 0.0


def _store_intraday_bars(symbol: str, trade_date: str, bars: list[dict], run_id: str = "", week_key: str | None = None, source: str = "polygon_5m") -> int:
    if not (SQLITE_ENABLED and EVIDENCE_INTRADAY_BAR_STORE_ENABLED and bars):
        return 0
    sym = _clean_symbol(symbol)
    if not sym:
        return 0
    wk = str(week_key or _current_week_key() or "")
    count = 0
    init_evidence_db()
    with _LOCK:
        with _connect() as conn:
            for b in bars or []:
                if not isinstance(b, dict):
                    continue
                ts = _safe_int(b.get("t"), 0)
                if ts <= 0:
                    continue
                dt = _dt_from_polygon_ms(ts)
                close = _bar_value(b, "c")
                vol = _bar_value(b, "v")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence_intraday_bars(
                        week_key, trade_date, symbol, bar_ts, bar_time_text, session, open, high, low, close, volume, dollar_volume, source, run_id, raw_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        wk, trade_date, sym, ts, dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "", _bar_session_from_dt(dt),
                        _bar_value(b, "o"), _bar_value(b, "h"), _bar_value(b, "l"), close, vol, close * vol if close > 0 and vol > 0 else 0,
                        source, str(run_id or ""), _json_dumps(b),
                    ),
                )
                count += 1
            conn.commit()
    return count


def _analyze_intraday_bars(symbol: str, trade_date: str, bars: list[dict], previous_close: float = 0.0, day_open: float = 0.0) -> dict:
    sym = _clean_symbol(symbol)
    d = str(trade_date or _today_text())[:10]
    bars = [b for b in (bars or []) if isinstance(b, dict)]
    bars = sorted(bars, key=lambda x: _safe_int(x.get("t"), 0))
    if not bars:
        return {"ok": True, "symbol": sym, "trade_date": d, "bars": 0}

    pre_bars = _bars_between(bars, 4, 0, 9, 30)
    regular_bars = _bars_between(bars, 9, 30, 16, 0)
    after_bars = _bars_between(bars, 16, 0, 20, 0)
    first_15 = _bars_between(bars, 9, 30, 9, 45)
    first_30 = _bars_between(bars, 9, 30, 10, 0)
    first_60 = _bars_between(bars, 9, 30, 10, 30)
    last_30 = regular_bars[-6:] if len(regular_bars) >= 6 else regular_bars

    open_px = float(day_open or 0) or _bar_open(regular_bars) or _bar_open(bars)
    close_px = _bar_close(regular_bars) or _bar_close(bars)
    high_px = _bars_high(regular_bars) or _bars_high(bars)
    low_px = _bars_low(regular_bars) or _bars_low(bars)
    pre_open = _bar_open(pre_bars)
    pre_close = _bar_close(pre_bars)
    after_open = _bar_open(after_bars)
    after_close = _bar_close(after_bars)
    first_15_high = _bars_high(first_15)
    first_30_high = _bars_high(first_30)
    first_60_high = _bars_high(first_60)
    first_30_close = _bar_close(first_30)
    first_60_close = _bar_close(first_60)
    prev = float(previous_close or 0)

    pre_market_move_pct = _pct_change(pre_close, prev) if prev > 0 and pre_close > 0 else _pct_change(pre_close, pre_open)
    after_hours_change_pct = _pct_change(after_close, close_px) if after_close > 0 and close_px > 0 else _pct_change(after_close, after_open)
    gap_pct = _pct_change(open_px, prev) if prev > 0 and open_px > 0 else 0.0
    open_gap_pct = gap_pct
    first_15_gain_pct = _pct_change(first_15_high, open_px)
    first_30_gain_pct = _pct_change(first_30_high, open_px)
    first_60_gain_pct = _pct_change(first_60_high, open_px)
    first_30_close_pct = _pct_change(first_30_close, open_px)
    first_60_close_pct = _pct_change(first_60_close, open_px)
    open_to_high_pct = _pct_change(high_px, open_px)
    close_vs_open_pct = _pct_change(close_px, open_px)

    pre_vol = _bars_volume(pre_bars)
    pre_dollar_vol = _bars_dollar_volume(pre_bars)
    after_vol = _bars_volume(after_bars)
    after_dollar_vol = _bars_dollar_volume(after_bars)
    first_15_vol = _bars_volume(first_15)
    first_30_vol = _bars_volume(first_30)
    first_60_vol = _bars_volume(first_60)
    last_30_vol = _bars_volume(last_30)
    total_vol = _bars_volume(regular_bars) or _bars_volume(bars)
    avg_5m_vol = total_vol / max(1, len(regular_bars or bars))
    first_30_vs_avg = first_30_vol / max(1.0, avg_5m_vol * max(1, len(first_30))) if avg_5m_vol > 0 else 0.0
    last_30_vs_first_30 = last_30_vol / max(1.0, first_30_vol) if first_30_vol > 0 else 0.0
    volume_fade_flag = 1 if first_30_vol > 0 and last_30_vol > 0 and last_30_vs_first_30 < 0.35 else 0
    regular_dollar_volume = _bars_dollar_volume(regular_bars) or _bars_dollar_volume(bars)
    vwap_proxy = (regular_dollar_volume / total_vol) if total_vol > 0 else 0.0
    close_position_pct = safe_round(((close_px - low_px) / max(0.000001, high_px - low_px)) * 100.0, 2) if high_px > low_px and close_px > 0 else 0.0
    late_day_volume_spike = safe_round(last_30_vol / max(1.0, avg_5m_vol * max(1, len(last_30))), 2) if avg_5m_vol > 0 else 0.0
    first_15_followthrough = 1 if first_15_gain_pct > 0 and (_bar_close(first_15) or 0) >= open_px else 0
    first_30_followthrough = 1 if first_30_gain_pct > 0 and first_30_close >= open_px else 0
    held_above_open = 1 if open_px > 0 and close_px >= open_px and (first_30_close <= 0 or first_30_close >= open_px) else 0
    held_above_vwap_proxy = 1 if vwap_proxy > 0 and close_px >= vwap_proxy and (first_30_close <= 0 or first_30_close >= vwap_proxy * 0.995) else 0
    gap_fade_flag = 1 if gap_pct >= 5 and (first_30_close_pct < 0 or close_vs_open_pct < 0 or (low_px > 0 and open_px > 0 and low_px < open_px * 0.985)) else 0
    gap_retest_success = 1 if gap_pct >= 2 and open_px > 0 and low_px > 0 and low_px <= open_px * 1.015 and close_px >= open_px else 0

    liquidity_accel = min(100.0, max(0.0, first_30_vs_avg * 35.0)) if first_30_vs_avg else 0.0
    # Persistence rewards first push + holding/continued volume later.
    persistence = 0.0
    if first_30_vol > 0:
        persistence += min(45.0, last_30_vs_first_30 * 55.0)
    if close_vs_open_pct > 0:
        persistence += min(25.0, close_vs_open_pct * 3.0)
    if first_60_close_pct > 0:
        persistence += min(20.0, first_60_close_pct * 4.0)
    if volume_fade_flag:
        persistence -= 20.0
    persistence = safe_round(max(0.0, min(100.0, persistence)), 2)
    liquidity_accel = safe_round(liquidity_accel, 2)

    if gap_pct >= 12 and close_vs_open_pct < 0:
        gap_label = "gap_failed_or_chased"
    elif gap_pct >= 5 and first_30_close_pct > 0 and close_vs_open_pct >= 0:
        gap_label = "gap_followthrough"
    elif gap_pct >= 5:
        gap_label = "gap_needs_confirmation"
    elif pre_market_move_pct >= 5:
        gap_label = "pre_market_build_then_open"
    else:
        gap_label = "no_major_gap"

    if liquidity_accel >= 70 and persistence >= 55 and close_vs_open_pct > 0:
        quality = "strong_real_move_candidate"
    elif gap_label == "gap_failed_or_chased" or volume_fade_flag:
        quality = "unreliable_or_chase_risk"
    elif liquidity_accel >= 45 or first_30_gain_pct >= 4:
        quality = "active_needs_followthrough"
    else:
        quality = "weak_or_unclear"

    if pre_vol > 0 and gap_pct >= 5:
        likely_pattern = "pre_gap_activity_plus_gap"
    elif gap_pct >= 8:
        likely_pattern = "large_open_gap"
    elif liquidity_accel >= 60 and gap_pct < 5:
        likely_pattern = "intraday_liquidity_acceleration"
    elif close_vs_open_pct > 3 and persistence >= 45:
        likely_pattern = "steady_followthrough"
    else:
        likely_pattern = "unclassified"

    return {
        "ok": True,
        "version": "intraday_evidence_v2",
        "symbol": sym,
        "trade_date": d,
        "bars": len(bars),
        "pre_market_bars": len(pre_bars),
        "regular_bars": len(regular_bars),
        "after_hours_bars": len(after_bars),
        "open": safe_round(open_px, 4),
        "last_close": safe_round(close_px, 4),
        "high": safe_round(high_px, 4),
        "low": safe_round(low_px, 4),
        "total_volume": safe_round(total_vol, 0),
        "pre_market_move_pct": safe_round(pre_market_move_pct, 2),
        "pre_market_change_pct": safe_round(pre_market_move_pct, 2),
        "pre_market_volume": safe_round(pre_vol, 0),
        "pre_market_dollar_volume": safe_round(pre_dollar_vol, 0),
        "after_hours_change_pct": safe_round(after_hours_change_pct, 2),
        "after_hours_volume": safe_round(after_vol, 0),
        "after_hours_dollar_volume": safe_round(after_dollar_vol, 0),
        "gap_pct": safe_round(gap_pct, 2),
        "open_gap_pct": safe_round(open_gap_pct, 2),
        "open_to_high_pct": safe_round(open_to_high_pct, 2),
        "close_vs_open_pct": safe_round(close_vs_open_pct, 2),
        "close_position_pct": safe_round(close_position_pct, 2),
        "late_day_volume_spike": safe_round(late_day_volume_spike, 2),
        "first_15m_gain_pct": safe_round(first_15_gain_pct, 2),
        "first_30m_gain_pct": safe_round(first_30_gain_pct, 2),
        "first_60m_gain_pct": safe_round(first_60_gain_pct, 2),
        "first_30m_close_pct": safe_round(first_30_close_pct, 2),
        "first_60m_close_pct": safe_round(first_60_close_pct, 2),
        "first_15m_volume": safe_round(first_15_vol, 0),
        "first_30m_volume": safe_round(first_30_vol, 0),
        "first_60m_volume": safe_round(first_60_vol, 0),
        "last_30m_volume": safe_round(last_30_vol, 0),
        "avg_5m_volume": safe_round(avg_5m_vol, 0),
        "first_30m_volume_vs_avg": safe_round(first_30_vs_avg, 2),
        "last_30m_vs_first_30m_volume": safe_round(last_30_vs_first_30, 2),
        "first_15m_followthrough": int(first_15_followthrough),
        "first_30m_followthrough": int(first_30_followthrough),
        "held_above_open": int(held_above_open),
        "held_above_vwap_proxy": int(held_above_vwap_proxy),
        "gap_fade_flag": int(gap_fade_flag),
        "gap_retest_success": int(gap_retest_success),
        "vwap_proxy": safe_round(vwap_proxy, 4),
        "volume_fade_flag": int(volume_fade_flag),
        "liquidity_acceleration_score": liquidity_accel,
        "liquidity_persistence_score": persistence,
        "gap_followthrough_label": gap_label,
        "move_quality_label": quality,
        "likely_pattern": likely_pattern,
    }


def _fetch_polygon_bars(symbol: str, trade_date: str | None = None) -> dict:
    sym = _clean_symbol(symbol)
    if not (EVIDENCE_POLYGON_ENABLED and POLYGON_API_KEY and sym):
        return {"ok": False, "enabled": bool(EVIDENCE_POLYGON_ENABLED), "configured": bool(POLYGON_API_KEY), "bars": []}
    d = str(trade_date or _today_text())[:10]
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/5/minute/{d}/{d}"
        r = HTTP_SESSION.get(
            url,
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": POLYGON_API_KEY},
            timeout=float(EVIDENCE_HTTP_TIMEOUT_SEC),
        )
        if r.status_code >= 400:
            return {"ok": False, "symbol": sym, "trade_date": d, "status_code": r.status_code, "bars": []}
        payload = r.json() or {}
        bars = payload.get("results") or []
        return {"ok": True, "symbol": sym, "trade_date": d, "bars": bars if isinstance(bars, list) else []}
    except Exception as exc:
        return {"ok": False, "symbol": sym, "trade_date": d, "error": f"{type(exc).__name__}: {str(exc)[:160]}", "bars": []}


def _fetch_polygon_intraday_summary(symbol: str, trade_date: str | None = None, previous_close: float = 0.0, day_open: float = 0.0, *, run_id: str = "", store_bars: bool = True) -> dict:
    """Enhanced 5-minute candle summary for evidence V2.

    It stores raw 5m bars for limited symbols so next weekend we can study pre-gap,
    gap follow-through, liquidity acceleration, and fade behavior.
    """
    sym = _clean_symbol(symbol)
    d = str(trade_date or _today_text())[:10]
    bundle = _fetch_polygon_bars(sym, d)
    if not bundle.get("ok"):
        return {k: v for k, v in bundle.items() if k != "bars"}
    bars = bundle.get("bars") or []
    if store_bars:
        stored = _store_intraday_bars(sym, d, bars, run_id=run_id, week_key=_current_week_key())
    else:
        stored = 0
    summary = _analyze_intraday_bars(sym, d, bars, previous_close=previous_close, day_open=day_open)
    summary["bars_stored"] = stored
    return summary

def _row_bucket(row: dict) -> str:
    decision = str((row or {}).get("decision") or "").strip()
    if decision == "دخول قوي":
        return "strong"
    if decision == "دخول بحذر":
        return "cautious"
    if "رمادي" in str((row or {}).get("sharia_label") or "") or "gray" in str((row or {}).get("sharia_status") or "").lower():
        return "gray_or_unresolved"
    if decision:
        return "watch_or_other"
    return "unknown"


def _quote_for_symbol(quotes: dict, sym: str) -> dict:
    return quotes.get(sym) or quotes.get(str(sym).upper()) or {}


def _compose_snapshot_row(run_id: str, week_key: str, trade_date: str, session: str, symbol: str, row: dict, quote: dict, *, in_big_movers: bool, mover: dict | None, polygon_summary: dict | None) -> dict:
    price = _safe_float(quote.get("price"), 0) or _first_positive(row, ["live_price", "display_price", "current_price_live", "current_price", "price"])
    prev = _safe_float(quote.get("previous_close"), 0) or _first_positive(row, ["previous_close", "regular_close", "regular_session_close"])
    change_pct = _safe_float(quote.get("change_pct"), 0)
    if not change_pct:
        change_pct = _safe_float((mover or {}).get("change_pct"), 0) or _safe_float(row.get("display_change_pct") or row.get("change_pct") or row.get("change_from_open_pct"), 0)
    volume = _safe_float(quote.get("volume"), 0) or _first_positive(row, ["volume", "day_volume", "projected_day_volume"])
    dollar_volume = price * volume if price > 0 and volume > 0 else _safe_float(row.get("dollar_volume"), 0)
    entry = _first_positive(row, ["display_entry_price", "smart_entry_price", "entry_price_real", "entry", "breakout_price", "confirmation_price"])
    target = _first_positive(row, ["display_target_price", "smart_target_1", "target_1", "target1", "target", "target_price"])
    stop = _first_positive(row, ["display_stop_price", "smart_stop_loss", "stop_loss", "stop"])
    support = _first_positive(row, ["nearest_support", "support_price", "support", "display_support_price"])
    resistance = _first_positive(row, ["nearest_resistance", "resistance_price", "resistance", "display_resistance_price"])
    risk_tags = _as_list(row.get("risk_tags"))
    success_tags = _as_list(row.get("success_tags"))
    no_chase_text = " ".join(str(row.get(k, "") or "") for k in ["no_chase_label", "late_move_label", "owner_action", "execution_note"])
    no_chase = 1 if any(x in no_chase_text for x in ["لا تطارد", "متأخر", "مطاردة"]) else 0
    plan_needs = 1 if any(x in no_chase_text for x in ["إعادة تأكيد", "مكسورة", "انتظر"]) else 0
    return {
        "run_id": run_id,
        "week_key": week_key,
        "trade_date": trade_date,
        "captured_at": _now_ts(),
        "captured_at_text": _now_text(),
        "session": session,
        "symbol": symbol,
        "source_group": "tool_and_big_mover" if row and in_big_movers else ("big_mover" if in_big_movers else "tool_signal"),
        "in_tool_snapshot": 1 if row else 0,
        "in_big_movers": 1 if in_big_movers else 0,
        "signal_bucket": _row_bucket(row),
        "decision": _first_text(row, ["decision", "signal_label"]),
        "sharia_status": _first_text(row, ["sharia_status", "sharia_label"]),
        "plan_family": _first_text(row, ["plan_family", "setup_type", "opportunity_type", "strategy_type"]),
        "price": safe_round(price, 4),
        "previous_close": safe_round(prev, 4),
        "change_pct": safe_round(change_pct, 2),
        "volume": safe_round(volume, 0),
        "dollar_volume": safe_round(dollar_volume, 0),
        "entry_price": safe_round(entry, 4),
        "target_price": safe_round(target, 4),
        "stop_loss": safe_round(stop, 4),
        "support_price": safe_round(support, 4),
        "resistance_price": safe_round(resistance, 4),
        "distance_from_entry_pct": _pct_distance(price, entry),
        "distance_from_support_pct": _pct_distance(price, support),
        "distance_from_resistance_pct": _pct_distance(price, resistance),
        "gap_from_prev_close_pct": safe_round(((price - prev) / prev * 100.0), 2) if price > 0 and prev > 0 else 0,
        "pre_market_change_pct": _safe_float((polygon_summary or {}).get("pre_market_change_pct") or (polygon_summary or {}).get("pre_market_move_pct"), 0),
        "pre_market_volume": _safe_float((polygon_summary or {}).get("pre_market_volume"), 0),
        "pre_market_dollar_volume": _safe_float((polygon_summary or {}).get("pre_market_dollar_volume"), 0),
        "after_hours_change_pct": _safe_float((polygon_summary or {}).get("after_hours_change_pct"), 0),
        "open_gap_pct": _safe_float((polygon_summary or {}).get("open_gap_pct") or (polygon_summary or {}).get("gap_pct"), 0),
        "first_15m_followthrough": int((polygon_summary or {}).get("first_15m_followthrough") or 0),
        "first_30m_followthrough": int((polygon_summary or {}).get("first_30m_followthrough") or 0),
        "held_above_open": int((polygon_summary or {}).get("held_above_open") or 0),
        "held_above_vwap_proxy": int((polygon_summary or {}).get("held_above_vwap_proxy") or 0),
        "gap_fade_flag": int((polygon_summary or {}).get("gap_fade_flag") or 0),
        "gap_retest_success": int((polygon_summary or {}).get("gap_retest_success") or 0),
        "first_seen_change_pct": _safe_float(row.get("first_seen_change_pct") or row.get("change_pct_at_first_seen"), 0),
        "no_chase_flag": no_chase,
        "plan_needs_reconfirm": plan_needs,
        "liquidity_score": _safe_float(row.get("liquidity_persistence_score") or row.get("liquidity_score") or row.get("volume_score"), 0),
        "momentum_acceleration_score": _safe_float(row.get("momentum_acceleration_score") or row.get("potential_speed") or row.get("source_rank_score"), 0),
        "pattern_risk_score": _safe_float(row.get("pattern_risk_score") or row.get("pattern_risk") or row.get("risk_score"), 0),
        "risk_tags_json": _json_dumps(risk_tags),
        "success_tags_json": _json_dumps(success_tags),
        "quote_source": str(quote.get("source") or ""),
        "price_source": str(quote.get("source_label") or quote.get("source") or row.get("price_source") or ""),
        "polygon_summary_json": _json_dumps(polygon_summary or {}),
        "raw_json": _json_dumps({"tool_row": row or {}, "quote": quote or {}, "mover": mover or {}}),
    }


def _insert_snapshot_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    init_evidence_db()
    cols = [
        "run_id", "week_key", "trade_date", "captured_at", "captured_at_text", "session", "symbol", "source_group",
        "in_tool_snapshot", "in_big_movers", "signal_bucket", "decision", "sharia_status", "plan_family",
        "price", "previous_close", "change_pct", "volume", "dollar_volume", "entry_price", "target_price", "stop_loss",
        "support_price", "resistance_price", "distance_from_entry_pct", "distance_from_support_pct", "distance_from_resistance_pct",
        "gap_from_prev_close_pct", "pre_market_change_pct", "pre_market_volume", "pre_market_dollar_volume", "after_hours_change_pct",
        "open_gap_pct", "first_15m_followthrough", "first_30m_followthrough", "held_above_open", "held_above_vwap_proxy",
        "gap_fade_flag", "gap_retest_success", "first_seen_change_pct", "no_chase_flag", "plan_needs_reconfirm", "liquidity_score",
        "momentum_acceleration_score", "pattern_risk_score", "risk_tags_json", "success_tags_json", "quote_source", "price_source",
        "polygon_summary_json", "raw_json",
    ]
    placeholders = ",".join(["?"] * len(cols))
    with _LOCK:
        with _connect() as conn:
            for row in rows:
                conn.execute(
                    f"INSERT INTO evidence_snapshots({','.join(cols)}) VALUES({placeholders})",
                    tuple(row.get(c) for c in cols),
                )
            conn.commit()
    return len(rows)


def _upsert_big_movers(trade_date: str, movers: list[dict], tool_ctx: dict[str, dict]) -> int:
    if not movers:
        return 0
    init_evidence_db()
    now = _now_ts()
    count = 0
    with _LOCK:
        with _connect() as conn:
            for item in movers:
                sym = _clean_symbol(item.get("symbol"))
                if not sym:
                    continue
                in_tool = 1 if sym in tool_ctx else 0
                stage = _row_bucket(tool_ctx.get(sym, {})) if in_tool else "not_in_tool_snapshot"
                conn.execute(
                    """
                    INSERT INTO daily_big_movers(trade_date, symbol, first_seen_at, last_seen_at, price, change_pct, volume, dollar_volume, source, in_tool_snapshot, tool_stage, raw_json)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_date, symbol) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at,
                        price=excluded.price,
                        change_pct=MAX(daily_big_movers.change_pct, excluded.change_pct),
                        volume=excluded.volume,
                        dollar_volume=excluded.dollar_volume,
                        source=excluded.source,
                        in_tool_snapshot=MAX(daily_big_movers.in_tool_snapshot, excluded.in_tool_snapshot),
                        tool_stage=CASE WHEN excluded.tool_stage != 'not_in_tool_snapshot' THEN excluded.tool_stage ELSE daily_big_movers.tool_stage END,
                        raw_json=excluded.raw_json
                    """,
                    (
                        trade_date,
                        sym,
                        now,
                        now,
                        _safe_float(item.get("price"), 0),
                        _safe_float(item.get("change_pct"), 0),
                        _safe_float(item.get("volume"), 0),
                        _safe_float(item.get("dollar_volume"), 0),
                        str(item.get("source") or ""),
                        in_tool,
                        stage,
                        _json_dumps(item),
                    ),
                )
                count += 1
            conn.commit()
    return count


def _record_run(run: dict) -> None:
    init_evidence_db()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO evidence_runs(run_id, started_at, finished_at, week_key, trade_date, session, mode, symbols_requested,
                                          snapshots_inserted, movers_inserted, polygon_symbols, github_synced, ok, error, payload_json)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    finished_at=excluded.finished_at,
                    symbols_requested=excluded.symbols_requested,
                    snapshots_inserted=excluded.snapshots_inserted,
                    movers_inserted=excluded.movers_inserted,
                    polygon_symbols=excluded.polygon_symbols,
                    github_synced=excluded.github_synced,
                    ok=excluded.ok,
                    error=excluded.error,
                    payload_json=excluded.payload_json
                """,
                (
                    run.get("run_id"), run.get("started_at", _now_ts()), run.get("finished_at", 0), run.get("week_key", ""),
                    run.get("trade_date", ""), run.get("session", ""), run.get("mode", ""), int(run.get("symbols_requested", 0) or 0),
                    int(run.get("snapshots_inserted", 0) or 0), int(run.get("movers_inserted", 0) or 0), int(run.get("polygon_symbols", 0) or 0),
                    1 if run.get("github_synced") else 0, 1 if run.get("ok") else 0, str(run.get("error", "") or "")[:500], _json_dumps(run),
                ),
            )
            conn.commit()
    try:
        set_json("evidence_last_run", run)
    except Exception:
        pass


def _parse_date(value: str | None) -> date | None:
    try:
        txt = str(value or "").strip()[:10]
        if not txt:
            return None
        return datetime.strptime(txt, "%Y-%m-%d").date()
    except Exception:
        return None


def _date_range_list(start_date: str | None = None, end_date: str | None = None, days_back: int = 5) -> list[str]:
    end = _parse_date(end_date) or _now_dt().date()
    start = _parse_date(start_date)
    if start is None:
        start = end
        # Walk back calendar days; market holidays with no data are skipped later.
        for _ in range(max(0, int(days_back or 0)) - 1):
            start = date.fromordinal(start.toordinal() - 1)
    if start > end:
        start, end = end, start
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur = date.fromordinal(cur.toordinal() + 1)
    return out


def _previous_calendar_days(d: str, max_days: int = 10) -> list[str]:
    base = _parse_date(d) or _now_dt().date()
    out = []
    cur = date.fromordinal(base.toordinal() - 1)
    while len(out) < max_days:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y-%m-%d"))
        cur = date.fromordinal(cur.toordinal() - 1)
    return out


def _fetch_polygon_grouped_daily(trade_date: str) -> dict:
    d = str(trade_date or _today_text())[:10]
    if not (POLYGON_API_KEY and EVIDENCE_POLYGON_ENABLED):
        return {"ok": False, "configured": bool(POLYGON_API_KEY), "enabled": bool(EVIDENCE_POLYGON_ENABLED), "items": []}
    try:
        url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{d}"
        r = HTTP_SESSION.get(url, params={"adjusted": "true", "apiKey": POLYGON_API_KEY}, timeout=float(EVIDENCE_HTTP_TIMEOUT_SEC))
        if r.status_code >= 400:
            return {"ok": False, "trade_date": d, "status_code": r.status_code, "items": []}
        payload = r.json() or {}
        results = payload.get("results") or []
        return {"ok": True, "trade_date": d, "count": len(results), "items": results if isinstance(results, list) else []}
    except Exception as exc:
        return {"ok": False, "trade_date": d, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "items": []}


def _previous_daily_map_for_date(trade_date: str) -> tuple[str, dict[str, dict]]:
    """Return previous trading day's grouped daily OHLCV by symbol.

    This powers the Gap Candidate / Gap Risk evidence fields without making
    symbol-by-symbol daily requests. It is read-only and safe: if Polygon data is
    unavailable, callers simply receive an empty map.
    """
    for prev_d in _previous_calendar_days(trade_date, max_days=10):
        data = _fetch_polygon_grouped_daily(prev_d)
        items = data.get("items") or []
        if data.get("ok") and items:
            out: dict[str, dict] = {}
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                sym = _clean_symbol(raw.get("T") or raw.get("symbol"))
                c = _safe_float(raw.get("c"), 0)
                if sym and c > 0:
                    out[sym] = {
                        "open": _safe_float(raw.get("o"), 0),
                        "high": _safe_float(raw.get("h"), 0),
                        "low": _safe_float(raw.get("l"), 0),
                        "close": c,
                        "volume": _safe_float(raw.get("v"), 0),
                        "dollar_volume": c * _safe_float(raw.get("v"), 0) if c > 0 else 0,
                        "raw": raw,
                    }
            if out:
                return prev_d, out
    return "", {}


def _previous_close_map_for_date(trade_date: str) -> tuple[str, dict[str, float]]:
    prev_d, daily = _previous_daily_map_for_date(trade_date)
    return prev_d, {sym: _safe_float(info.get("close"), 0) for sym, info in (daily or {}).items()}


def _close_position_pct(close: float, high: float, low: float) -> float:
    close = _safe_float(close, 0)
    high = _safe_float(high, 0)
    low = _safe_float(low, 0)
    if close <= 0 or high <= low:
        return 0.0
    return safe_round(((close - low) / max(0.000001, high - low)) * 100.0, 2)


def _near_high_flag(close: float, high: float, low: float, threshold_pct: float = 80.0) -> int:
    return 1 if _close_position_pct(close, high, low) >= float(threshold_pct or 80.0) else 0


def _visibility_from_current_tool(sym: str) -> dict:
    symbol = _clean_symbol(sym)
    rows = _last_trade_scan_rows()
    best = {}
    for row in rows or []:
        if _clean_symbol((row or {}).get("symbol")) == symbol:
            best = row or {}
            break
    if best:
        return {
            "tool_seen": True,
            "tool_stage": _row_bucket(best),
            "decision": _first_text(best, ["decision", "signal_label"]),
            "first_seen_at": _first_text(best, ["first_seen_at", "appeared_at", "created_at"]),
            "first_seen_change_pct": _safe_float(best.get("first_seen_change_pct") or best.get("change_pct_at_first_seen"), 0),
            "source": "last_trade_scan_snapshot",
        }
    return {"tool_seen": False, "tool_stage": "not_in_current_tool_snapshot", "source": "last_trade_scan_snapshot"}


def _visibility_from_evidence(sym: str, trade_date: str | None = None) -> dict:
    symbol = _clean_symbol(sym)
    if not (SQLITE_ENABLED and symbol):
        return _visibility_from_current_tool(symbol)
    init_evidence_db()
    where = "symbol=?"
    args: list[Any] = [symbol]
    if trade_date:
        where += " AND trade_date=?"
        args.append(str(trade_date)[:10])
    with _connect() as conn:
        row = conn.execute(
            f"SELECT * FROM evidence_snapshots WHERE {where} ORDER BY captured_at ASC LIMIT 1",
            tuple(args),
        ).fetchone()
    if row:
        d = dict(row)
        return {
            "tool_seen": bool(int(d.get("in_tool_snapshot") or 0)),
            "source_seen": bool(d.get("source_group") in {"tool_signal", "tool_and_big_mover"}),
            "tool_stage": d.get("signal_bucket") or ("tool_snapshot" if int(d.get("in_tool_snapshot") or 0) else "not_in_tool_snapshot"),
            "decision": d.get("decision") or "",
            "first_seen_at": d.get("captured_at_text") or "",
            "first_seen_change_pct": _safe_float(d.get("change_pct"), 0),
            "source": "evidence_snapshots",
        }
    return _visibility_from_current_tool(symbol)




def _minutes_between_text(a: str, b: str) -> float:
    try:
        if not a or not b:
            return 0.0
        da = datetime.strptime(str(a)[:19], "%Y-%m-%d %H:%M:%S")
        db = datetime.strptime(str(b)[:19], "%Y-%m-%d %H:%M:%S")
        return safe_round((db - da).total_seconds() / 60.0, 1)
    except Exception:
        return 0.0


def _first_timeline_event(events: list[dict], event_type: str) -> dict:
    wanted = str(event_type or "")
    matches = [e for e in events or [] if str(e.get("event_type") or "") == wanted]
    if not matches:
        return {}
    matches.sort(key=lambda x: str(x.get("first_seen_at") or ""))
    return matches[0]


def _historical_visibility_for_symbol(symbol: str, week_key: str = "", trade_date: str = "") -> dict:
    """Resolve whether a winner was seen in source/tool history, not only the latest snapshot.

    This addresses the user's concern that `tool_seen=0` may only mean not present in the
    last saved radar snapshot. We read the Missed Opportunities timeline/source tables when
    available, then fall back to Evidence/current snapshot.
    """
    sym = _clean_symbol(symbol)
    wk = str(week_key or _current_week_key() or "")
    base = _visibility_from_evidence(sym, trade_date=trade_date or None)
    result = {
        "symbol": sym,
        "week_key": wk,
        "trade_date": str(trade_date or "")[:10],
        "tool_seen": bool(base.get("tool_seen")),
        "source_seen": bool(base.get("source_seen")),
        "tool_stage": str(base.get("tool_stage") or ""),
        "best_tool_stage": str(base.get("tool_stage") or ""),
        "first_seen_at": str(base.get("first_seen_at") or ""),
        "first_seen_change_pct": _safe_float(base.get("first_seen_change_pct"), 0),
        "first_source_seen_at": "",
        "first_source_gain_pct": 0.0,
        "first_deep_seen_at": "",
        "first_watch_seen_at": "",
        "first_cautious_seen_at": "",
        "first_strong_seen_at": "",
        "promotion_delay_minutes": 0.0,
        "visibility_confidence_label": "current_or_evidence_snapshot_only",
        "timeline_events": {},
        "source_candidate": {},
        "seen_summary": {},
    }
    if not (SQLITE_ENABLED and sym):
        return result
    try:
        with _connect() as conn:
            timeline = []
            try:
                rows = conn.execute(
                    "SELECT * FROM missed_symbol_timeline WHERE week_key=? AND symbol=? ORDER BY first_seen_at ASC",
                    (wk, sym),
                ).fetchall()
                timeline = [dict(r) for r in rows or []]
            except Exception:
                timeline = []
            src = {}
            try:
                r = conn.execute("SELECT * FROM missed_source_candidates WHERE week_key=? AND symbol=?", (wk, sym)).fetchone()
                src = dict(r) if r else {}
            except Exception:
                src = {}
            seen = {}
            try:
                r = conn.execute("SELECT * FROM missed_seen_symbols WHERE week_key=? AND symbol=?", (wk, sym)).fetchone()
                seen = dict(r) if r else {}
            except Exception:
                seen = {}
    except Exception:
        return result

    by_type: dict[str, dict] = {}
    for ev in timeline or []:
        et = str(ev.get("event_type") or "")
        if et and et not in by_type:
            by_type[et] = ev
    source_ev = by_type.get("source") or {}
    deep_ev = by_type.get("deep_universe") or {}
    watch_ev = by_type.get("watch") or {}
    cautious_ev = by_type.get("cautious") or {}
    strong_ev = by_type.get("strong") or {}
    gray_ev = by_type.get("gray") or {}
    display_ev = by_type.get("display_any") or {}

    source_seen = bool(source_ev or deep_ev or src)
    tool_seen = bool(display_ev or strong_ev or cautious_ev or watch_ev or gray_ev or seen or result.get("tool_seen"))
    stage = "not_seen"
    if strong_ev:
        stage = "strong"
    elif cautious_ev:
        stage = "cautious"
    elif watch_ev:
        stage = "watch"
    elif gray_ev:
        stage = "gray"
    elif display_ev or seen:
        stage = str((seen or {}).get("best_category_key") or (display_ev or {}).get("category_key") or "displayed")
    elif deep_ev:
        stage = "deep_universe"
    elif source_ev or src:
        stage = "source_only"
    elif result.get("tool_stage"):
        stage = result.get("tool_stage")

    first_tool = strong_ev or cautious_ev or watch_ev or gray_ev or display_ev or {}
    first_source_at = str((source_ev or deep_ev or src).get("first_seen_at") or "")
    first_tool_at = str(first_tool.get("first_seen_at") or seen.get("first_seen_at") or result.get("first_seen_at") or "")
    result.update({
        "tool_seen": tool_seen,
        "source_seen": source_seen,
        "tool_stage": stage,
        "best_tool_stage": stage,
        "first_seen_at": first_tool_at,
        "first_seen_change_pct": _safe_float(first_tool.get("first_gain_pct") or seen.get("first_price") or result.get("first_seen_change_pct"), 0),
        "first_source_seen_at": first_source_at,
        "first_source_gain_pct": _safe_float((source_ev or deep_ev or src).get("first_gain_pct") or (source_ev or deep_ev or src).get("change_pct"), 0),
        "first_deep_seen_at": str(deep_ev.get("first_seen_at") or ""),
        "first_watch_seen_at": str(watch_ev.get("first_seen_at") or ""),
        "first_cautious_seen_at": str(cautious_ev.get("first_seen_at") or ""),
        "first_strong_seen_at": str(strong_ev.get("first_seen_at") or ""),
        "promotion_delay_minutes": _minutes_between_text(first_source_at, first_tool_at),
        "visibility_confidence_label": "historical_timeline" if timeline or src or seen else result.get("visibility_confidence_label"),
        "timeline_events": {k: {"first_seen_at": v.get("first_seen_at"), "first_gain_pct": v.get("first_gain_pct"), "first_rank": v.get("first_rank"), "category": v.get("category")} for k, v in by_type.items()},
        "source_candidate": {"candidate_stage": src.get("candidate_stage"), "source_score": src.get("source_score"), "discovery_rank": src.get("discovery_rank"), "change_pct": src.get("change_pct"), "dollar_volume": src.get("dollar_volume")} if src else {},
        "seen_summary": {"best_category": seen.get("best_category"), "best_category_key": seen.get("best_category_key"), "times_seen": seen.get("times_seen"), "max_quality": seen.get("max_quality")} if seen else {},
    })
    return result


def _classify_tradability(row: dict) -> tuple[float, str, list[str]]:
    """Separate practical/tradable winners from micro-cap/warrant-like outliers.

    This prevents the future pattern learner from over-learning from 40% moves on
    tiny, illiquid, or special-ticker symbols that a normal execution plan would avoid.
    """
    sym = _clean_symbol(row.get("symbol"))
    price = _safe_float(row.get("day_close") or row.get("day_open") or row.get("previous_close"), 0)
    dollar = _safe_float(row.get("day_dollar_volume"), 0)
    volume = _safe_float(row.get("day_volume"), 0)
    change = _safe_float(row.get("winner_change_pct"), 0)
    gap = abs(_safe_float(row.get("gap_pct"), 0))
    score = 100.0
    reasons: list[str] = []
    if price <= 0:
        score -= 25; reasons.append("لا يوجد سعر كافٍ لتقييم قابلية التداول")
    elif price < 1:
        score -= 42; reasons.append("سعر أقل من 1 دولار؛ عينة مضاربية عالية المخاطر")
    elif price < 2:
        score -= 24; reasons.append("سعر منخفض جدًا؛ يحتاج فصل عن العينة النظيفة")
    elif price < 5:
        score -= 10; reasons.append("سعر منخفض نسبيًا")
    if dollar <= 0:
        score -= 18; reasons.append("حجم الدولار غير متوفر")
    elif dollar < 1_000_000:
        score -= 36; reasons.append("حجم الدولار ضعيف جدًا")
    elif dollar < 5_000_000:
        score -= 18; reasons.append("حجم الدولار متوسط/منخفض")
    if volume and volume < 250_000:
        score -= 14; reasons.append("حجم الأسهم المتداولة منخفض")
    # Simple special-symbol heuristic. Do not block, only tag.
    if sym.endswith(("W", "WS", "WT", "U", "R", "RT")) or ".WS" in sym or ".W" in sym:
        score -= 25; reasons.append("رمز قد يكون warrant/unit/right؛ افصله عن العينة الأساسية")
    if change >= 100:
        score -= 10; reasons.append("صعود شديد جدًا؛ قد يكون عينة مضاربية غير ممثلة")
    if gap >= 25:
        score -= 8; reasons.append("قاب كبير جدًا؛ يحتاج فصل جودة القاب")
    score = max(0.0, min(100.0, safe_round(score, 1)))
    if score >= 72:
        bucket = "tradable_core"
    elif score >= 48:
        bucket = "tradable_but_high_risk"
    else:
        bucket = "micro_or_special_high_risk"
    return score, bucket, reasons[:8]


def _classify_gap_quality(row: dict, intraday: dict | None = None) -> tuple[str, list[str]]:
    intraday = intraday or {}
    gap = _safe_float(row.get("gap_pct"), 0)
    pre = _safe_float(row.get("pre_market_move_pct"), 0)
    first15 = _safe_float(row.get("first_15m_gain_pct"), 0)
    first30 = _safe_float(row.get("first_30m_gain_pct"), 0)
    first60 = _safe_float(row.get("first_60m_gain_pct"), 0)
    persist = _safe_float(row.get("liquidity_persistence_score"), 0)
    accel = _safe_float(row.get("liquidity_acceleration_score"), 0)
    fade = int(row.get("volume_fade_flag") or 0)
    reasons: list[str] = []
    if abs(gap) < 2:
        if persist >= 55 or first30 >= 3 or accel >= 45:
            return "no_gap_steady_followthrough", ["لا يوجد قاب كبير؛ الحركة اعتمدت أكثر على استمرار/تسارع داخل الجلسة"]
        return "no_gap_needs_context", ["لا يوجد قاب كبير لكن المتابعة تحتاج سياقًا إضافيًا"]
    reasons.append(f"gap {safe_round(gap, 2)}%")
    if pre >= 3:
        reasons.append(f"حركة قبل الافتتاح {safe_round(pre, 2)}%")
    if gap >= 12 and (fade or persist < 45 or first30 <= 0):
        reasons.append("قاب كبير مع ضعف متابعة/سيولة؛ خطر مطاردة")
        return "gap_chase_or_failed", reasons
    if gap >= 5 and (first30 >= 2 or first60 >= 4) and persist >= 50 and not fade:
        reasons.append("القاب تبعه استمرار وسعر/سيولة مقبولة")
        return "healthy_gap_followthrough", reasons
    if gap >= 5 and pre >= 3 and (first15 > 0 or first30 > 0):
        reasons.append("قاب سبقه نشاط قبل الافتتاح لكنه يحتاج تأكيد استمرار")
        return "pre_gap_build_needs_confirmation", reasons
    if gap >= 5:
        reasons.append("قاب يحتاج تأكيد بعد الافتتاح قبل اعتباره نمطًا قابلًا للدخول")
        return "gap_needs_confirmation", reasons
    return "small_gap_or_contextual", reasons or ["قاب صغير/متوسط يحتاج سياق السيولة"]

def _classify_winner_profile(row: dict, intraday: dict) -> tuple[str, str, str]:
    gap = _safe_float(row.get("gap_pct"), 0)
    change = _safe_float(row.get("winner_change_pct"), 0)
    first30 = _safe_float(intraday.get("first_30m_gain_pct"), 0)
    persistence = _safe_float(intraday.get("liquidity_persistence_score"), 0)
    accel = _safe_float(intraday.get("liquidity_acceleration_score"), 0)
    fade = int(intraday.get("volume_fade_flag") or 0)
    gap_label = str(intraday.get("gap_followthrough_label") or "")
    if gap >= 12 and (fade or persistence < 35):
        quality = "gap_chase_risk"
    elif change >= 10 and persistence >= 55 and accel >= 45:
        quality = "high_quality_followthrough"
    elif change >= 10 and gap_label == "gap_followthrough":
        quality = "gap_followthrough_candidate"
    elif first30 >= 4 and accel >= 45:
        quality = "early_intraday_acceleration"
    elif change >= 10:
        quality = "winner_needs_more_context"
    else:
        quality = "not_big_winner"

    if gap >= 8 and _safe_float(intraday.get("pre_market_volume"), 0) > 0:
        pattern = "pre_market_gap_then_followthrough"
    elif gap >= 8:
        pattern = "open_gap_big_mover"
    elif first30 >= 4 and accel >= 50:
        pattern = "first_hour_liquidity_acceleration"
    elif persistence >= 60 and _safe_float(intraday.get("close_vs_open_pct"), 0) > 0:
        pattern = "steady_liquidity_followthrough"
    else:
        pattern = "unclassified_winner"

    # The label intentionally stays cautious until we have a larger sample.
    if quality in {"high_quality_followthrough", "gap_followthrough_candidate", "early_intraday_acceleration"}:
        label = "candidate_real_move"
    elif quality == "gap_chase_risk":
        label = "unreliable_gap_or_chase"
    else:
        label = "needs_sample_confirmation"
    return quality, pattern, label


def _upsert_winner_profile(profile: dict) -> None:
    init_evidence_db()
    now = _now_ts()
    profile.setdefault("created_at", now)
    profile["updated_at"] = now
    cols = [
        "week_key", "trade_date", "symbol", "winner_rank", "winner_change_pct", "previous_close", "day_open", "day_high", "day_low", "day_close",
        "day_volume", "day_dollar_volume", "gap_pct", "open_to_high_pct", "close_vs_open_pct", "pre_market_move_pct", "pre_market_change_pct", "pre_market_volume",
        "pre_market_dollar_volume", "after_hours_change_pct", "previous_close_near_high", "close_position_pct", "late_day_volume_spike", "open_gap_pct",
        "first_15m_followthrough", "first_30m_followthrough", "held_above_open", "held_above_vwap_proxy", "gap_fade_flag", "gap_retest_success",
        "first_15m_gain_pct", "first_30m_gain_pct", "first_60m_gain_pct", "first_30m_volume", "first_60m_volume", "last_30m_volume",
        "volume_fade_flag", "liquidity_acceleration_score", "liquidity_persistence_score", "gap_followthrough_label", "move_quality_label", "likely_pattern",
        "tool_seen", "tool_stage", "tool_first_seen_at", "tool_first_seen_change_pct", "source_seen",
        "tradability_score", "tradability_bucket", "tradability_reasons_json", "gap_quality_class", "gap_quality_reasons_json",
        "historical_visibility_json", "visibility_confidence_label", "first_source_seen_at", "first_source_gain_pct", "first_deep_seen_at",
        "first_watch_seen_at", "first_cautious_seen_at", "first_strong_seen_at", "best_tool_stage", "promotion_delay_minutes",
        "data_quality", "profile_json", "created_at", "updated_at"
    ]
    placeholders = ",".join(["?"] * len(cols))
    updates = ",".join([f"{c}=excluded.{c}" for c in cols if c not in {"week_key", "trade_date", "symbol", "created_at"}])
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                f"""
                INSERT INTO evidence_winner_profiles({','.join(cols)}) VALUES({placeholders})
                ON CONFLICT(trade_date, symbol) DO UPDATE SET {updates}
                """,
                tuple(profile.get(c) for c in cols),
            )
            conn.commit()


def backfill_daily_winner_profiles(start_date: str | None = None, end_date: str | None = None, days_back: int = 5, threshold_pct: float | None = None, limit_per_day: int | None = None, store_bars: bool = True) -> dict:
    """Backfill big-winner profiles from Polygon grouped daily data.

    This is the V2 layer needed before next week: it studies winners regardless of
    whether they entered the tool, then stores intraday candle behavior for pattern mining.
    """
    if not (SQLITE_ENABLED and EVIDENCE_BIG_WINNER_BACKFILL_ENABLED):
        return {"ok": False, "enabled": bool(EVIDENCE_BIG_WINNER_BACKFILL_ENABLED), "error": "sqlite_or_backfill_disabled"}
    threshold = float(threshold_pct if threshold_pct is not None else EVIDENCE_BIG_MOVER_THRESHOLD_PCT)
    lim = max(5, min(int(limit_per_day or EVIDENCE_BIG_WINNER_BACKFILL_SYMBOL_LIMIT), 500))
    dates = _date_range_list(start_date=start_date, end_date=end_date, days_back=int(days_back or 5))
    wk = _current_week_key()
    run_id = uuid.uuid4().hex[:12]
    out = {
        "ok": True,
        "version": "evidence_winner_backfill_v2",
        "run_id": run_id,
        "week_key": wk,
        "dates": dates,
        "threshold_pct": threshold,
        "limit_per_day": lim,
        "days": [],
        "profiles_upserted": 0,
        "bars_stored": 0,
        "notes": "Backfill studies winners > threshold even if they never entered the tool. No scoring changes.",
    }
    init_evidence_db()
    for d in dates:
        day = {"trade_date": d, "ok": False, "winners": 0, "profiles": 0, "bars_stored": 0}
        prev_d, prev_daily_map = _previous_daily_map_for_date(d)
        grouped = _fetch_polygon_grouped_daily(d)
        if not grouped.get("ok"):
            day["error"] = grouped.get("error") or grouped.get("status_code") or "polygon_grouped_failed"
            out["days"].append(day)
            continue
        candidates = []
        for raw in grouped.get("items") or []:
            if not isinstance(raw, dict):
                continue
            sym = _clean_symbol(raw.get("T") or raw.get("symbol"))
            if not sym:
                continue
            o = _safe_float(raw.get("o"), 0)
            h = _safe_float(raw.get("h"), 0)
            l = _safe_float(raw.get("l"), 0)
            c = _safe_float(raw.get("c"), 0)
            v = _safe_float(raw.get("v"), 0)
            prev_info = (prev_daily_map or {}).get(sym, {})
            prev = _safe_float((prev_info or {}).get("close"), 0)
            if prev <= 0 or c <= 0:
                continue
            chg = _pct_change(c, prev)
            dollar_vol = c * v if c > 0 and v > 0 else 0
            if chg < threshold:
                continue
            if EVIDENCE_MIN_WINNER_DOLLAR_VOLUME > 0 and dollar_vol < EVIDENCE_MIN_WINNER_DOLLAR_VOLUME:
                continue
            candidates.append({"symbol": sym, "raw": raw, "previous_daily": prev_info or {}, "previous_close": prev, "change_pct": chg, "dollar_volume": dollar_vol, "open": o, "high": h, "low": l, "close": c, "volume": v})
        candidates = sorted(candidates, key=lambda x: _safe_float(x.get("change_pct"), 0), reverse=True)[:lim]
        day["ok"] = True
        day["previous_trade_date"] = prev_d
        day["winners"] = len(candidates)
        for rank, item in enumerate(candidates, start=1):
            sym = item["symbol"]
            intraday = _fetch_polygon_intraday_summary(sym, trade_date=d, previous_close=item.get("previous_close", 0), day_open=item.get("open", 0), run_id=run_id, store_bars=bool(store_bars))
            visibility = _historical_visibility_for_symbol(sym, week_key=wk, trade_date=d)
            base = {
                "week_key": wk,
                "trade_date": d,
                "symbol": sym,
                "winner_rank": rank,
                "winner_change_pct": safe_round(item.get("change_pct"), 2),
                "previous_close": safe_round(item.get("previous_close"), 4),
                "day_open": safe_round(item.get("open"), 4),
                "day_high": safe_round(item.get("high"), 4),
                "day_low": safe_round(item.get("low"), 4),
                "day_close": safe_round(item.get("close"), 4),
                "day_volume": safe_round(item.get("volume"), 0),
                "day_dollar_volume": safe_round(item.get("dollar_volume"), 0),
                "gap_pct": safe_round(_pct_change(item.get("open"), item.get("previous_close")), 2),
                "open_to_high_pct": safe_round(_pct_change(item.get("high"), item.get("open")), 2),
                "close_vs_open_pct": safe_round(_pct_change(item.get("close"), item.get("open")), 2),
                "pre_market_move_pct": _safe_float(intraday.get("pre_market_move_pct"), 0),
                "pre_market_change_pct": _safe_float(intraday.get("pre_market_change_pct") or intraday.get("pre_market_move_pct"), 0),
                "pre_market_volume": _safe_float(intraday.get("pre_market_volume"), 0),
                "pre_market_dollar_volume": _safe_float(intraday.get("pre_market_dollar_volume"), 0),
                "after_hours_change_pct": _safe_float(intraday.get("after_hours_change_pct"), 0),
                "previous_close_near_high": _near_high_flag((item.get("previous_daily") or {}).get("close"), (item.get("previous_daily") or {}).get("high"), (item.get("previous_daily") or {}).get("low")),
                "close_position_pct": _close_position_pct(item.get("close"), item.get("high"), item.get("low")),
                "late_day_volume_spike": _safe_float(intraday.get("late_day_volume_spike"), 0),
                "open_gap_pct": _safe_float(intraday.get("open_gap_pct") or _pct_change(item.get("open"), item.get("previous_close")), 0),
                "first_15m_followthrough": int(intraday.get("first_15m_followthrough") or 0),
                "first_30m_followthrough": int(intraday.get("first_30m_followthrough") or 0),
                "held_above_open": int(intraday.get("held_above_open") or 0),
                "held_above_vwap_proxy": int(intraday.get("held_above_vwap_proxy") or 0),
                "gap_fade_flag": int(intraday.get("gap_fade_flag") or 0),
                "gap_retest_success": int(intraday.get("gap_retest_success") or 0),
                "first_15m_gain_pct": _safe_float(intraday.get("first_15m_gain_pct"), 0),
                "first_30m_gain_pct": _safe_float(intraday.get("first_30m_gain_pct"), 0),
                "first_60m_gain_pct": _safe_float(intraday.get("first_60m_gain_pct"), 0),
                "first_30m_volume": _safe_float(intraday.get("first_30m_volume"), 0),
                "first_60m_volume": _safe_float(intraday.get("first_60m_volume"), 0),
                "last_30m_volume": _safe_float(intraday.get("last_30m_volume"), 0),
                "volume_fade_flag": int(intraday.get("volume_fade_flag") or 0),
                "liquidity_acceleration_score": _safe_float(intraday.get("liquidity_acceleration_score"), 0),
                "liquidity_persistence_score": _safe_float(intraday.get("liquidity_persistence_score"), 0),
                "gap_followthrough_label": str(intraday.get("gap_followthrough_label") or ""),
                "move_quality_label": str(intraday.get("move_quality_label") or ""),
                "likely_pattern": str(intraday.get("likely_pattern") or ""),
                "tool_seen": 1 if visibility.get("tool_seen") else 0,
                "tool_stage": str(visibility.get("tool_stage") or ""),
                "tool_first_seen_at": str(visibility.get("first_seen_at") or ""),
                "tool_first_seen_change_pct": _safe_float(visibility.get("first_seen_change_pct"), 0),
                "source_seen": 1 if visibility.get("source_seen") else 0,
                "historical_visibility_json": _json_dumps(visibility),
                "visibility_confidence_label": str(visibility.get("visibility_confidence_label") or ""),
                "first_source_seen_at": str(visibility.get("first_source_seen_at") or ""),
                "first_source_gain_pct": _safe_float(visibility.get("first_source_gain_pct"), 0),
                "first_deep_seen_at": str(visibility.get("first_deep_seen_at") or ""),
                "first_watch_seen_at": str(visibility.get("first_watch_seen_at") or ""),
                "first_cautious_seen_at": str(visibility.get("first_cautious_seen_at") or ""),
                "first_strong_seen_at": str(visibility.get("first_strong_seen_at") or ""),
                "best_tool_stage": str(visibility.get("best_tool_stage") or visibility.get("tool_stage") or ""),
                "promotion_delay_minutes": _safe_float(visibility.get("promotion_delay_minutes"), 0),
                "data_quality": "ok" if intraday.get("ok") and intraday.get("bars", 0) else "daily_only_or_no_intraday",
            }
            quality, pattern, quality_label = _classify_winner_profile(base, intraday)
            base["move_quality_label"] = quality
            base["likely_pattern"] = pattern
            tradability_score, tradability_bucket, tradability_reasons = _classify_tradability(base)
            gap_quality_class, gap_quality_reasons = _classify_gap_quality(base, intraday)
            base["tradability_score"] = tradability_score
            base["tradability_bucket"] = tradability_bucket
            base["tradability_reasons_json"] = _json_dumps(tradability_reasons)
            base["gap_quality_class"] = gap_quality_class
            base["gap_quality_reasons_json"] = _json_dumps(gap_quality_reasons)
            base["profile_json"] = _json_dumps({"quality_label": quality_label, "polygon_intraday": intraday, "tool_visibility": visibility, "tradability": {"score": tradability_score, "bucket": tradability_bucket, "reasons": tradability_reasons}, "gap_quality": {"class": gap_quality_class, "reasons": gap_quality_reasons}, "raw_daily": item.get("raw", {}), "previous_daily": item.get("previous_daily", {})})
            _upsert_winner_profile(base)
            day["profiles"] += 1
            stored = _safe_int(intraday.get("bars_stored"), 0)
            day["bars_stored"] += stored
            out["profiles_upserted"] += 1
            out["bars_stored"] += stored
        out["days"].append(day)
    return out



def _enrich_winner_profile_row(row: dict, week_key: str = "", refresh_visibility: bool = True) -> dict:
    """Return a winner-profile row with safe fallback classifications.

    V4 introduced columns such as gap_quality_class and tradability_bucket. Rows
    that were backfilled before that migration may still have blank values. This
    helper computes read-time fallbacks so Pattern Lab does not report everything
    as gap_unknown / tradability_unknown and so we do not need to re-run heavy
    Polygon backfills just to classify existing rows.
    """
    d = dict(row or {})

    # Tradability fallback.
    trad = str(d.get("tradability_bucket") or "").strip()
    if not trad or trad in {"unknown", "tradability_unknown", "غير مصنف"}:
        try:
            score, bucket, reasons = _classify_tradability(d)
            d["tradability_score"] = score
            d["tradability_bucket"] = bucket
            if not str(d.get("tradability_reasons_json") or "").strip():
                d["tradability_reasons_json"] = _json_dumps(reasons)
        except Exception:
            d["tradability_bucket"] = "tradability_unknown"

    # Gap-quality fallback. Prefer profile_json if already calculated, otherwise
    # recompute from numeric fields available on the winner profile row.
    gap_cls = str(d.get("gap_quality_class") or "").strip()
    if not gap_cls or gap_cls in {"unknown", "gap_unknown", "غير مصنف"}:
        reasons = []
        try:
            prof = _json_loads(d.get("profile_json"), {}) or {}
            if isinstance(prof, dict):
                gap_info = prof.get("gap_quality") or {}
                if isinstance(gap_info, dict):
                    gap_cls = str(gap_info.get("class") or "").strip()
                    reasons = gap_info.get("reasons") or []
        except Exception:
            gap_cls = ""
            reasons = []
        if not gap_cls:
            try:
                gap_cls, reasons = _classify_gap_quality(d)
            except Exception:
                gap_cls, reasons = "gap_unknown", []
        d["gap_quality_class"] = gap_cls or "gap_unknown"
        if not str(d.get("gap_quality_reasons_json") or "").strip():
            d["gap_quality_reasons_json"] = _json_dumps(reasons if isinstance(reasons, list) else [str(reasons)])

    # Historical visibility fallback. This is intentionally read-only and only
    # enriches the report output. It lets old backfilled rows benefit from the
    # missed-opportunities/source tables when available.
    if refresh_visibility:
        conf = str(d.get("visibility_confidence_label") or "").strip()
        needs_visibility = (not conf) or conf in {"unknown", "current_or_evidence_snapshot_only"}
        has_no_first_source = not str(d.get("first_source_seen_at") or "").strip()
        if needs_visibility or has_no_first_source:
            try:
                vis = _historical_visibility_for_symbol(str(d.get("symbol") or ""), week_key=week_key or str(d.get("week_key") or ""), trade_date=str(d.get("trade_date") or ""))
                if isinstance(vis, dict) and vis:
                    for key in [
                        "tool_seen", "source_seen", "tool_stage", "best_tool_stage", "tool_first_seen_at", "tool_first_seen_change_pct",
                        "first_source_seen_at", "first_source_gain_pct", "first_deep_seen_at", "first_watch_seen_at",
                        "first_cautious_seen_at", "first_strong_seen_at", "promotion_delay_minutes", "visibility_confidence_label",
                    ]:
                        if key in vis and (key in {"tool_seen", "source_seen"} or not str(d.get(key) or "").strip()):
                            d[key] = vis.get(key)
                    if not str(d.get("historical_visibility_json") or "").strip():
                        d["historical_visibility_json"] = _json_dumps(vis)
            except Exception:
                pass
    return d


def _aggregate_winner_rows(rows: list[dict], key_fields: list[str], limit: int = 30) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in rows or []:
        key = tuple(str(r.get(k) or "") for k in key_fields)
        item = groups.setdefault(key, {**{k: key[i] for i, k in enumerate(key_fields)}, "cases": 0, "symbols": set(), "sum_gain": 0.0, "sum_gap": 0.0, "sum_first30": 0.0, "sum_liq_accel": 0.0, "sum_liq_persist": 0.0, "sum_dollar": 0.0, "tool_seen_count": 0, "source_seen_count": 0})
        item["cases"] += 1
        if str(r.get("symbol") or ""):
            item["symbols"].add(str(r.get("symbol")))
        item["sum_gain"] += _safe_float(r.get("winner_change_pct"), 0)
        item["sum_gap"] += _safe_float(r.get("gap_pct"), 0)
        item["sum_first30"] += _safe_float(r.get("first_30m_gain_pct"), 0)
        item["sum_liq_accel"] += _safe_float(r.get("liquidity_acceleration_score"), 0)
        item["sum_liq_persist"] += _safe_float(r.get("liquidity_persistence_score"), 0)
        item["sum_dollar"] += _safe_float(r.get("day_dollar_volume"), 0)
        item["tool_seen_count"] += 1 if int(r.get("tool_seen") or 0) else 0
        item["source_seen_count"] += 1 if int(r.get("source_seen") or 0) else 0
    out = []
    for item in groups.values():
        cases = max(1, int(item["cases"]))
        row = {k: item.get(k) for k in key_fields}
        row.update({
            "cases": cases,
            "unique_symbols": len(item["symbols"]),
            "avg_gain": safe_round(item["sum_gain"] / cases, 2),
            "avg_gap": safe_round(item["sum_gap"] / cases, 2),
            "avg_first30": safe_round(item["sum_first30"] / cases, 2),
            "avg_liq_accel": safe_round(item["sum_liq_accel"] / cases, 1),
            "avg_liq_persist": safe_round(item["sum_liq_persist"] / cases, 1),
            "avg_dollar_volume": safe_round(item["sum_dollar"] / cases, 0),
            "tool_seen_count": item["tool_seen_count"],
            "source_seen_count": item["source_seen_count"],
        })
        out.append(row)
    out.sort(key=lambda x: (int(x.get("cases") or 0), _safe_float(x.get("avg_gain"), 0)), reverse=True)
    return out[:limit]


def winner_profiles_report(week_key: str | None = None, trade_date: str | None = None, format: str = "json", limit: int = 120) -> dict | str:
    wk = str(week_key or _current_week_key() or "")
    d = str(trade_date or "")[:10]
    init_evidence_db()
    where = []
    args: list[Any] = []
    if wk:
        where.append("week_key=?")
        args.append(wk)
    if d:
        where.append("trade_date=?")
        args.append(d)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    lim = max(1, min(int(limit or 120), 1000))
    with _connect() as conn:
        total_row = conn.execute(f"SELECT COUNT(*) AS c, COUNT(DISTINCT symbol) AS symbols FROM evidence_winner_profiles {where_sql}", tuple(args)).fetchone()
        rows = conn.execute(f"SELECT * FROM evidence_winner_profiles {where_sql} ORDER BY winner_change_pct DESC LIMIT ?", (*args, lim)).fetchall()
    items = [_enrich_winner_profile_row(r, week_key=wk, refresh_visibility=False) for r in _rows_to_dicts(rows)]
    total_count = int((dict(total_row).get("c") if total_row else 0) or 0)
    total_symbols = int((dict(total_row).get("symbols") if total_row else 0) or 0)
    patterns = _aggregate_winner_rows(items, ["likely_pattern", "move_quality_label"], limit=30)
    result = {"ok": True, "version": "winner_pattern_profiles_v2a", "week_key": wk, "trade_date": d, "count": len(items), "total_count": total_count, "total_symbols": total_symbols, "patterns": patterns, "items": items}
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        lines = ["تقرير Daily Winner Pattern Mining V2a", f"الأسبوع: {wk}", f"التاريخ: {d or 'كل الأسبوع'}", f"عدد ملفات الرابحين: {total_count} إجماليًا | المعروض الآن: {len(items)} | رموز فريدة: {total_symbols}", "", "أبرز الأنماط المرصودة:"]
        if not patterns:
            lines.append("لا توجد ملفات رابحين كافية بعد. شغّل backfill-winners للأسبوع السابق أو انتظر جمع الأسبوع القادم.")
        for ptn in patterns[:12]:
            lines.append(
                f"- {ptn.get('likely_pattern') or 'غير مصنف'} / {ptn.get('move_quality_label') or '-'}: حالات {ptn.get('cases')} | متوسط الصعود {safe_round(ptn.get('avg_gain'),2)}% | متوسط القاب {safe_round(ptn.get('avg_gap'),2)}% | شوهد في الأداة {int(ptn.get('tool_seen_count') or 0)}"
            )
        lines.append("")
        lines.append("أكبر الرابحين المحللين:")
        for x in items[:20]:
            tool = "ظهر في الأداة" if int(x.get("tool_seen") or 0) else "لم يظهر في الأداة/غير مؤكد"
            lines.append(f"- {x.get('symbol')}: +{safe_round(x.get('winner_change_pct'),2)}% | gap {safe_round(x.get('gap_pct'),2)}% | {x.get('likely_pattern') or '-'} | {x.get('gap_quality_class') or '-'} | {x.get('tradability_bucket') or '-'} | {tool}")
        return "\n".join(lines)
    return result

def pattern_readiness_report(week_key: str | None = None, format: str = "json") -> dict | str:
    wk = str(week_key or _current_week_key() or "")
    init_evidence_db()
    with _connect() as conn:
        snap = conn.execute("SELECT COUNT(*) AS c, COUNT(DISTINCT symbol) AS symbols FROM evidence_snapshots WHERE week_key=?", (wk,)).fetchone()
        winners = conn.execute("SELECT COUNT(*) AS c, COUNT(DISTINCT symbol) AS symbols FROM evidence_winner_profiles WHERE week_key=?", (wk,)).fetchone()
        bars = conn.execute("SELECT COUNT(*) AS c, COUNT(DISTINCT symbol) AS symbols FROM evidence_intraday_bars WHERE week_key=?", (wk,)).fetchone()
        big = conn.execute("SELECT COUNT(*) AS c FROM daily_big_movers", ()).fetchone()
        patterns = conn.execute("SELECT likely_pattern, COUNT(*) AS cases FROM evidence_winner_profiles WHERE week_key=? GROUP BY likely_pattern ORDER BY cases DESC LIMIT 12", (wk,)).fetchall()
    score = 0
    snap_c = int((snap or {}).get("c", 0) if isinstance(snap, dict) else snap["c"] if snap else 0)
    winner_c = int((winners or {}).get("c", 0) if isinstance(winners, dict) else winners["c"] if winners else 0)
    bar_c = int((bars or {}).get("c", 0) if isinstance(bars, dict) else bars["c"] if bars else 0)
    if snap_c >= 500: score += 25
    elif snap_c >= 100: score += 15
    elif snap_c > 0: score += 5
    if winner_c >= 50: score += 35
    elif winner_c >= 20: score += 25
    elif winner_c > 0: score += 10
    if bar_c >= 2000: score += 30
    elif bar_c >= 500: score += 20
    elif bar_c > 0: score += 8
    if len(patterns or []) >= 3: score += 10
    readiness = "جاهز لتحليل أولي" if score >= 55 else ("قيد التجميع" if score >= 20 else "غير كافٍ بعد")
    result = {
        "ok": True,
        "version": "pattern_readiness_v2",
        "week_key": wk,
        "readiness_score": min(100, score),
        "readiness_label": readiness,
        "snapshots": dict(snap) if snap else {},
        "winner_profiles": dict(winners) if winners else {},
        "intraday_bars": dict(bars) if bars else {},
        "daily_big_movers": dict(big) if big else {},
        "top_winner_patterns": _rows_to_dicts(patterns),
        "notes": "الغرض معرفة هل لدينا عينة كافية قبل إدخال الأنماط في الترتيب أو التنبيهات.",
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        lines = [
            "تقرير جاهزية تحليل الأنماط V2",
            f"الأسبوع: {wk}",
            f"درجة الجاهزية: {result['readiness_score']}/100 - {readiness}",
            f"لقطات الأداة: {snap_c}",
            f"ملفات الرابحين الكبار: {winner_c}",
            f"شموع/لقطات Polygon المحفوظة: {bar_c}",
            "",
            "أبرز أنماط الرابحين حتى الآن:",
        ]
        for ptn in result["top_winner_patterns"]:
            lines.append(f"- {ptn.get('likely_pattern') or 'غير مصنف'}: {ptn.get('cases')} حالات")
        if score < 55:
            lines.append("")
            lines.append("ملاحظة: لا نعتمد هذه الأنماط في السكور بعد؛ نحتاج أسبوع جمع كامل أو backfill أوسع.")
        return "\n".join(lines)
    return result



def _riyadh_dt() -> datetime:
    return datetime.now(RIYADH_TZ)


def _week_key_for_trade_date(trade_date: str) -> str:
    """Return Monday-Friday week key for a specific US trade date.

    This avoids the weekend behavior of get_performance_week_key(), which can
    move Saturday/Sunday to the upcoming week while we may be syncing Friday.
    """
    d = _parse_date(str(trade_date or "")[:10]) or _now_dt().date()
    monday = date.fromordinal(d.toordinal() - d.weekday())
    friday = date.fromordinal(monday.toordinal() + 4)
    return f"{monday.isoformat()}_{friday.isoformat()}"


def _riyadh_sync_trade_date(now_riyadh: datetime | None = None) -> tuple[bool, str, str]:
    """Return whether the Riyadh daily sync should run and the US trade date.

    Schedule requested by the user:
    - Saturday 05:45 Asia/Riyadh syncs Friday trading by default.
    - Sunday and Monday do not sync because Saturday/Sunday are closed.
    - Tuesday syncs Monday, Wednesday syncs Tuesday, Thursday syncs Wednesday,
      Friday syncs Thursday.
    """
    now_r = now_riyadh or _riyadh_dt()
    wd = now_r.weekday()  # Monday=0 ... Sunday=6
    if wd in {6, 0}:  # Sunday, Monday
        return False, "", "skip_non_trading_previous_day"
    # Tue-Sat only, after the configured Riyadh sync time. Default is 05:45.
    sync_time = dt_time(EVIDENCE_AUTO_SYNC_RIYADH_HOUR, EVIDENCE_AUTO_SYNC_RIYADH_MINUTE)
    if now_r.time() < sync_time:
        return False, "", f"before_{EVIDENCE_AUTO_SYNC_RIYADH_HOUR:02d}{EVIDENCE_AUTO_SYNC_RIYADH_MINUTE:02d}_riyadh"
    prev = date.fromordinal(now_r.date().toordinal() - 1)
    # Tue->Mon, Wed->Tue, Thu->Wed, Fri->Thu, Sat->Fri. All are weekdays.
    return True, prev.isoformat(), "due_after_trading_day"


def evidence_auto_sync_status() -> dict:
    due, trade_date, reason = _riyadh_sync_trade_date()
    key = f"evidence_auto_github_synced_{EVIDENCE_AUTO_SYNC_STATE_VERSION}_{trade_date}" if trade_date else ""
    attempt_key = f"evidence_auto_github_attempted_{EVIDENCE_AUTO_SYNC_STATE_VERSION}_{trade_date}" if trade_date else ""
    done = get_json(key, {}) if key else {}
    attempted = get_json(attempt_key, {}) if attempt_key else {}
    last = get_json("evidence_last_auto_sync", {})
    return {
        "ok": True,
        "version": "evidence_auto_sync_v5c_riyadh_daily_once_github_fallback",
        "enabled": bool(EVIDENCE_GITHUB_AUTO_SYNC_ENABLED),
        "github_configured": bool(is_github_sync_configured()),
        "now_riyadh": _riyadh_dt().strftime("%Y-%m-%d %H:%M:%S"),
        "schedule": f"Tue/Wed/Thu/Fri/Sat {EVIDENCE_AUTO_SYNC_RIYADH_HOUR:02d}:{EVIDENCE_AUTO_SYNC_RIYADH_MINUTE:02d} Asia/Riyadh; skips Sunday and Monday; never deletes Railway data.",
        "due_now": bool(EVIDENCE_GITHUB_AUTO_SYNC_ENABLED and is_github_sync_configured() and due and not (isinstance(done, dict) and done.get("ok")) and not (isinstance(attempted, dict) and attempted.get("attempted"))),
        "planned_trade_date": trade_date,
        "state_version": EVIDENCE_AUTO_SYNC_STATE_VERSION,
        "skip_reason": ("auto_sync_disabled" if not EVIDENCE_GITHUB_AUTO_SYNC_ENABLED else ("github_sync_not_configured" if not is_github_sync_configured() else (reason if not due else ("already_synced" if isinstance(done, dict) and done.get("ok") else ("already_attempted" if isinstance(attempted, dict) and attempted.get("attempted") else ""))))),
        "already_synced_for_trade_date": bool(isinstance(done, dict) and done.get("ok")),
        "already_attempted_for_trade_date": bool(isinstance(attempted, dict) and attempted.get("attempted")),
        "last_attempt_for_trade_date": attempted if isinstance(attempted, dict) else {},
        "last_auto_sync": last if isinstance(last, dict) else {},
        "railway_prune_enabled": False,
        "auto_backfill_symbol_limit": int(EVIDENCE_AUTO_BACKFILL_SYMBOL_LIMIT),
        "auto_backfill_store_bars": bool(EVIDENCE_AUTO_BACKFILL_STORE_BARS),
        "sync_include_csv_default": bool(EVIDENCE_SYNC_INCLUDE_CSV_DEFAULT),
        "attempt_state": ("incomplete_or_crashed" if isinstance(attempted, dict) and attempted.get("attempted") and attempted.get("ok") is None else ("finished" if isinstance(attempted, dict) and attempted.get("attempted") else "none")),
        "notes": "Daily Evidence sync exports compact GitHub files only, default 05:45 Riyadh. GitHub Contents API fallback avoids Git Data API 404. Railway deletion/pruning remains disabled unless the guarded prune-execute endpoint is called manually with confirmation.",
    }


def run_evidence_auto_sync(force: bool = False, dry_run: bool = False, include_csv: bool | None = None) -> dict:
    if not force and not EVIDENCE_GITHUB_AUTO_SYNC_ENABLED:
        return {"ok": True, "skipped": True, "reason": "auto_sync_disabled", "status": evidence_auto_sync_status()}
    if not force and not is_github_sync_configured():
        return {"ok": True, "skipped": True, "reason": "github_sync_not_configured", "status": evidence_auto_sync_status()}
    due, trade_date, reason = _riyadh_sync_trade_date()
    if force and not trade_date:
        # When forced on Sunday/Monday, use the most recent weekday so manual testing is possible.
        cur = _riyadh_dt().date()
        prev = date.fromordinal(cur.toordinal() - 1)
        while prev.weekday() >= 5:
            prev = date.fromordinal(prev.toordinal() - 1)
        trade_date = prev.isoformat()
        due = True
        reason = "forced_manual_recent_trading_day"
    key = f"evidence_auto_github_synced_{EVIDENCE_AUTO_SYNC_STATE_VERSION}_{trade_date}" if trade_date else ""
    attempt_key = f"evidence_auto_github_attempted_{EVIDENCE_AUTO_SYNC_STATE_VERSION}_{trade_date}" if trade_date else ""
    done = get_json(key, {}) if key else {}
    attempted = get_json(attempt_key, {}) if attempt_key else {}
    if not force and isinstance(done, dict) and done.get("ok"):
        return {"ok": True, "skipped": True, "reason": "already_synced", "trade_date": trade_date, "previous_result": done}
    if not force and isinstance(attempted, dict) and attempted.get("attempted"):
        return {"ok": True, "skipped": True, "reason": "already_attempted", "trade_date": trade_date, "previous_attempt": attempted}
    if not (due or force):
        return {"ok": True, "skipped": True, "reason": reason, "trade_date": trade_date, "status": evidence_auto_sync_status()}
    if dry_run:
        return {"ok": True, "dry_run": True, "trade_date": trade_date, "week_key": _week_key_for_trade_date(trade_date), "reason": reason, "would_sync_github": True, "would_prune_railway": False, "batch_commit": True}
    if attempt_key:
        try:
            set_json(attempt_key, {"attempted": True, "ok": None, "trade_date": trade_date, "started_at_riyadh": _riyadh_dt().strftime("%Y-%m-%d %H:%M:%S"), "reason": reason})
        except Exception:
            pass
    wk = _week_key_for_trade_date(trade_date)
    # Run a final winner-profile backfill for the trade date before syncing. This is passive and safe.
    backfill = {}
    if EVIDENCE_AUTO_BACKFILL_WINNERS_ENABLED and EVIDENCE_BIG_WINNER_BACKFILL_ENABLED:
        backfill = backfill_daily_winner_profiles(
            start_date=trade_date,
            end_date=trade_date,
            days_back=1,
            threshold_pct=EVIDENCE_BIG_MOVER_THRESHOLD_PCT,
            limit_per_day=EVIDENCE_AUTO_BACKFILL_SYMBOL_LIMIT,
            store_bars=bool(EVIDENCE_AUTO_BACKFILL_STORE_BARS),
        )
    sync = sync_evidence_to_github(week_key=wk, trade_date=trade_date, include_csv=include_csv)
    result = {
        "ok": bool(sync.get("ok")),
        "version": "evidence_daily_auto_sync_v5c_compact_once_daily",
        "trade_date": trade_date,
        "week_key": wk,
        "ran_at_riyadh": _riyadh_dt().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason,
        "backfill": backfill,
        "sync": sync,
        "pruned_railway": False,
        "notes": "GitHub sync only. One automatic attempt per trade_date; no Railway deletion is performed by daily auto-sync.",
    }
    try:
        if attempt_key:
            set_json(attempt_key, {"attempted": True, "ok": bool(result.get("ok")), "trade_date": trade_date, "finished_at_riyadh": result.get("ran_at_riyadh"), "reason": reason, "error": (sync or {}).get("error", "") if isinstance(sync, dict) else ""})
        if key and result.get("ok"):
            set_json(key, result)
        set_json("evidence_last_auto_sync", result)
    except Exception:
        pass
    return result


def liquidity_confirmation_check(symbol: str, trade_date: str | None = None, store_bars: bool = False) -> dict:
    """On-demand liquidity check for one symbol.

    This is for execution support: when price reaches entry, the user can press
    "تحديث السيولة" and get a simple answer: continuing / uncertain / fading.

    V3a safety: outside live/pre-market/after-hours sessions we do **not** label
    liquidity as fading just because intraday volume is zero. Closed-market checks
    should be displayed as "cannot confirm now" so the user does not confuse a
    weekend/overnight result with true volume failure.
    """
    sym = _clean_symbol(symbol)
    if not sym:
        return {"ok": False, "error": "invalid_symbol"}
    session = _market_session()
    quote_bundle = get_live_quotes([sym]) if EVIDENCE_COLLECTION_ENABLED else {}
    quote = quote_bundle.get(sym) or quote_bundle.get(sym.upper()) or {}
    price = _safe_float(quote.get("price"), 0)
    prev = _safe_float(quote.get("previous_close"), 0)
    d = str(trade_date or _today_text())[:10]

    if session in {"closed", "closed_weekend"} and not trade_date:
        return {
            "ok": True,
            "version": "liquidity_confirmation_v1_closed_market_safe",
            "symbol": sym,
            "trade_date": d,
            "checked_at": _now_text(),
            "session": session,
            "status": "market_closed",
            "label": "⚪ السوق مغلق — لا يمكن تأكيد استمرار السيولة الآن",
            "score": None,
            "source": "closed_market_no_live_liquidity",
            "price": safe_round(price, 4),
            "change_pct": quote.get("change_pct", 0),
            "quote_source": quote.get("source_label") or quote.get("source") or "",
            "volume": quote.get("volume", 0),
            "dollar_volume": safe_round(price * _safe_float(quote.get("volume"), 0), 0) if price > 0 else 0,
            "liquidity_acceleration_score": None,
            "liquidity_persistence_score": None,
            "volume_fade_flag": 0,
            "first_30m_volume": 0,
            "last_30m_volume": 0,
            "guidance": "لا تستخدم هذا الفحص كقرار دخول لأن السوق مغلق. أعد التحديث أثناء pre-market أو السوق الرسمي أو بعد الإغلاق النشط.",
            "polygon": {"ok": False, "skipped": True, "reason": session},
            "notes": "Closed-market guard: no live liquidity decision was made.",
        }

    poly = _fetch_polygon_intraday_summary(sym, d, previous_close=prev, day_open=0.0, run_id="liquidity_check", store_bars=bool(store_bars))
    score = _safe_float(poly.get("liquidity_persistence_score"), 0)
    accel = _safe_float(poly.get("liquidity_acceleration_score"), 0)
    fade = int(poly.get("volume_fade_flag") or 0)
    if not score:
        # Fallback if Polygon is unavailable. This is intentionally conservative.
        vol = _safe_float(quote.get("volume"), 0)
        dollar = vol * price if price > 0 and vol > 0 else 0
        if dollar >= 25_000_000:
            score = 62
        elif dollar >= 5_000_000:
            score = 52
        elif dollar > 0:
            score = 40
        else:
            score = 35
    if fade:
        score = min(score, 45)
    if accel >= 55 and score >= 55:
        score = min(100, score + 6)
    score = max(0.0, min(100.0, safe_round(score, 1)))
    if score >= 70 and not fade:
        status = "continuing"
        label = "✅ السيولة مستمرة"
        guidance = "يمكن اعتبار السيولة داعمة إذا كان السعر ثابتًا فوق نقطة الدخول/الدعم ولا توجد مقاومة مباشرة."
    elif score >= 52 and not fade:
        status = "uncertain"
        label = "⏳ السيولة مقبولة لكنها تحتاج تأكيد"
        guidance = "انتظر ثبات السعر فوق الدخول مع تحسن السيولة، أو ادخل بحجم أصغر إذا قبلت المخاطرة."
    elif score >= 38:
        status = "weak"
        label = "⚠️ السيولة غير مؤكدة"
        guidance = "لا تعتمد على لمس نقطة الدخول وحده؛ انتظر شموع/حجم يؤكد استمرار الحركة."
    else:
        status = "fading"
        label = "🔴 السيولة ضعفت — لا تدخل الآن"
        guidance = "تجنب الدخول حتى تعود السيولة ويثبت السعر فوق مستوى الدخول/الاختراق."
    return {
        "ok": True,
        "version": "liquidity_confirmation_v1",
        "symbol": sym,
        "trade_date": d,
        "checked_at": _now_text(),
        "session": _market_session(),
        "status": status,
        "label": label,
        "score": score,
        "source": "polygon_5m+fmp_quote" if poly.get("ok") else "fmp_quote_fallback",
        "price": safe_round(price, 4),
        "change_pct": quote.get("change_pct", 0),
        "quote_source": quote.get("source_label") or quote.get("source") or "",
        "volume": quote.get("volume", 0),
        "dollar_volume": safe_round(price * _safe_float(quote.get("volume"), 0), 0) if price > 0 else 0,
        "liquidity_acceleration_score": accel,
        "liquidity_persistence_score": score,
        "volume_fade_flag": int(fade),
        "first_30m_volume": poly.get("first_30m_volume", 0),
        "last_30m_volume": poly.get("last_30m_volume", 0),
        "first_30m_volume_vs_avg": poly.get("first_30m_volume_vs_avg", 0),
        "last_30m_vs_first_30m_volume": poly.get("last_30m_vs_first_30m_volume", 0),
        "gap_followthrough_label": poly.get("gap_followthrough_label", ""),
        "guidance": guidance,
        "polygon": {k: poly.get(k) for k in ["ok", "bars_stored", "likely_pattern", "move_quality_label", "gap_pct", "pre_market_volume", "first_15m_gain_pct", "first_30m_gain_pct", "first_60m_gain_pct"] if k in poly},
        "notes": "هذا فحص تنفيذ عند الطلب فقط؛ لا يغير السكور أو الفلتر الشرعي أو التصنيف.",
    }

def _daily_winner_backfill_due(session: str) -> bool:
    if not (EVIDENCE_AUTO_BACKFILL_WINNERS_ENABLED and EVIDENCE_BIG_WINNER_BACKFILL_ENABLED):
        return False
    now = _now_dt()
    if session not in {"after_hours", "closed"}:
        return False
    if session == "after_hours" and now.time() < dt_time(20, 10):
        return False
    key = f"evidence_winner_backfilled_{_today_text()}"
    attempt_key = f"evidence_winner_backfill_attempted_{_today_text()}"
    done = get_json(key, {})
    attempted = get_json(attempt_key, {})
    # One automatic backfill attempt per trade date. If it fails, we do not loop
    # every minute and burn Railway memory/network. Manual force/backfill can still
    # be used later after reviewing logs.
    return not (isinstance(done, dict) and done.get("ok")) and not (isinstance(attempted, dict) and attempted.get("attempted"))


def _mark_daily_winner_backfill(result: dict) -> None:
    try:
        key_date = _today_text()
        set_json(f"evidence_winner_backfill_attempted_{key_date}", {"attempted": True, "ok": bool((result or {}).get("ok")), "at": _now_text(), "error": str((result or {}).get("error") or "")[:180]})
        if isinstance(result, dict) and result.get("ok"):
            set_json(f"evidence_winner_backfilled_{key_date}", result)
    except Exception:
        pass


def collect_evidence_snapshot(mode: str = "manual", include_big_movers: bool = True, sync_to_github: bool = False, max_symbols: int | None = None) -> dict:
    """Collect one passive evidence snapshot.

    This function may make FMP/Polygon API calls, but it never changes radar decisions.
    """
    started = _now_ts()
    run_id = uuid.uuid4().hex[:12]
    week_key = _current_week_key()
    trade_date = _today_text()
    session = _market_session()
    run = {
        "ok": False,
        "run_id": run_id,
        "started_at": started,
        "finished_at": 0,
        "week_key": week_key,
        "trade_date": trade_date,
        "session": session,
        "mode": str(mode or "manual"),
        "enabled": bool(EVIDENCE_COLLECTION_ENABLED),
    }
    if not (SQLITE_ENABLED and EVIDENCE_COLLECTION_ENABLED):
        run.update({"error": "evidence_collection_disabled_or_sqlite_off", "finished_at": _now_ts()})
        _record_run(run)
        return run

    try:
        init_evidence_db()
        rows = _last_trade_scan_rows()
        tool_symbols, tool_ctx = _extract_tool_symbol_context(rows, limit=EVIDENCE_MAX_TOOL_SYMBOLS)
        movers_result = _fetch_fmp_big_movers() if include_big_movers and EVIDENCE_BIG_MOVERS_ENABLED else {"ok": True, "items": [], "disabled": True}
        movers = movers_result.get("items", []) if isinstance(movers_result, dict) else []
        movers = movers[:max(1, int(EVIDENCE_MAX_BIG_MOVERS))]
        mover_map = {_clean_symbol(x.get("symbol")): x for x in movers if _clean_symbol(x.get("symbol"))}
        mover_symbols = [s for s in mover_map.keys() if s]
        _upsert_big_movers(trade_date, movers, tool_ctx)

        symbols: list[str] = []
        for sym in tool_symbols + mover_symbols:
            if sym and sym not in symbols:
                symbols.append(sym)
        cap = int(max_symbols or EVIDENCE_MAX_SYMBOLS_PER_RUN or 260)
        symbols = symbols[:max(1, min(cap, 500))]

        prefer_cache = session not in {"pre_market", "regular", "after_hours"}
        quote_bundle = get_live_quotes(symbols, prefer_cache=bool(prefer_cache), allow_fallback=True)
        quotes = quote_bundle.get("quotes", {}) if isinstance(quote_bundle, dict) else {}
        try:
            market_fear_snapshot = get_market_fear_snapshot(force_refresh=False, store=True)
        except Exception as _market_fear_exc:
            market_fear_snapshot = {"ok": False, "error": str(_market_fear_exc)[:160]}

        polygon_summaries: dict[str, dict] = {}
        polygon_limit = max(0, min(int(EVIDENCE_POLYGON_SYMBOL_LIMIT or 0), len(symbols)))
        # Prioritize big movers and actionable tool rows for Polygon details.
        polygon_symbols = []
        for sym in mover_symbols + tool_symbols:
            if sym and sym not in polygon_symbols:
                polygon_symbols.append(sym)
            if len(polygon_symbols) >= polygon_limit:
                break
        for sym in polygon_symbols:
            row = tool_ctx.get(sym, {})
            mover = mover_map.get(sym, {})
            prev_hint = _first_positive(row, ["previous_close", "regular_close", "regular_session_close"])
            open_hint = _first_positive(row, ["open", "day_open", "regular_open"])
            polygon_summaries[sym] = _fetch_polygon_intraday_summary(
                sym,
                trade_date=trade_date,
                previous_close=prev_hint,
                day_open=open_hint,
                run_id=run_id,
                store_bars=True,
            )

        snapshot_rows = []
        for sym in symbols:
            row = tool_ctx.get(sym, {})
            mover = mover_map.get(sym)
            snap = _compose_snapshot_row(
                run_id,
                week_key,
                trade_date,
                session,
                sym,
                row,
                _quote_for_symbol(quotes, sym),
                in_big_movers=bool(mover),
                mover=mover,
                polygon_summary=polygon_summaries.get(sym),
            )
            snapshot_rows.append(snap)
        inserted = _insert_snapshot_rows(snapshot_rows)

        run.update({
            "ok": True,
            "finished_at": _now_ts(),
            "symbols_requested": len(symbols),
            "snapshots_inserted": inserted,
            "movers_inserted": len(movers),
            "polygon_symbols": len([x for x in polygon_summaries.values() if isinstance(x, dict) and x.get("ok")]),
            "quote_diagnostics": (quote_bundle or {}).get("diagnostics", {}),
            "big_movers": {k: v for k, v in (movers_result or {}).items() if k != "items"},
            "market_fear": market_fear_snapshot,
            "market_fear_tracking_fields": (market_fear_snapshot or {}).get("tracking_fields", {}) if isinstance(market_fear_snapshot, dict) else {},
            "notes": "Passive evidence only; no scoring/ranking/Sharia changes.",
        })
        if sync_to_github:
            sync = sync_evidence_to_github(week_key=week_key, trade_date=trade_date, include_csv=False)
            run["github_sync"] = sync
            run["github_synced"] = bool(sync.get("ok"))
        _record_run(run)
        return run
    except Exception as exc:
        run.update({"ok": False, "finished_at": _now_ts(), "error": f"{type(exc).__name__}: {str(exc)[:240]}"})
        _record_run(run)
        return run


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows or []]


def evidence_status() -> dict:
    out = {
        "ok": False,
        "enabled": bool(EVIDENCE_COLLECTION_ENABLED),
        "background_worker_enabled": bool(EVIDENCE_BACKGROUND_WORKER_ENABLED),
        "sqlite_enabled": bool(SQLITE_ENABLED),
        "db_path": str(SQLITE_DB_PATH),
        "session": _market_session(),
        "week_key": _current_week_key(),
        "github_configured": bool(is_github_sync_configured()),
        "github_auto_sync_enabled": bool(EVIDENCE_GITHUB_AUTO_SYNC_ENABLED),
        "big_mover_threshold_pct": float(EVIDENCE_BIG_MOVER_THRESHOLD_PCT),
        "polygon_enabled": bool(EVIDENCE_POLYGON_ENABLED),
        "polygon_configured": bool(POLYGON_API_KEY),
        "fmp_configured": bool(FMP_API_KEY),
        "market_fear": (market_fear_status().get("last_snapshot") or {}),
    }
    if not SQLITE_ENABLED:
        out["ok"] = True
        out["note"] = "sqlite_disabled"
        return out
    try:
        init_evidence_db()
        with _connect() as conn:
            snap_count = conn.execute("SELECT COUNT(*) AS c FROM evidence_snapshots").fetchone()["c"]
            mover_count = conn.execute("SELECT COUNT(*) AS c FROM daily_big_movers").fetchone()["c"]
            run_count = conn.execute("SELECT COUNT(*) AS c FROM evidence_runs").fetchone()["c"]
            last_run = conn.execute("SELECT * FROM evidence_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            today_snaps = conn.execute("SELECT COUNT(*) AS c FROM evidence_snapshots WHERE trade_date=?", (_today_text(),)).fetchone()["c"]
            week_snaps = conn.execute("SELECT COUNT(*) AS c FROM evidence_snapshots WHERE week_key=?", (_current_week_key(),)).fetchone()["c"]
        out.update({
            "ok": True,
            "initialized": True,
            "snapshots_total": int(snap_count or 0),
            "snapshots_today": int(today_snaps or 0),
            "snapshots_this_week": int(week_snaps or 0),
            "daily_big_movers_total": int(mover_count or 0),
            "runs_total": int(run_count or 0),
            "last_run": dict(last_run) if last_run else None,
        })
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return out


def daily_winners_report(trade_date: str | None = None, format: str = "json", limit: int = 120) -> dict | str:
    d = str(trade_date or _today_text())[:10]
    init_evidence_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM daily_big_movers WHERE trade_date=? ORDER BY change_pct DESC LIMIT ?",
            (d, max(1, min(int(limit or 120), 500))),
        ).fetchall()
    items = _rows_to_dicts(rows)
    result = {"ok": True, "trade_date": d, "count": len(items), "items": items}
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        lines = ["تقرير Daily Winner Pattern Mining V2", f"التاريخ: {d}", f"عدد الرابحين المحفوظين: {len(items)}", ""]
        if not items:
            lines.append("لا توجد قائمة رابحين محفوظة بعد. ستبدأ بالظهور بعد أول جمع أثناء السوق.")
        for x in items[:25]:
            in_tool = "ظهر في الأداة" if int(x.get("in_tool_snapshot") or 0) else "لم يظهر في لقطة الأداة"
            lines.append(f"- {x.get('symbol')}: {safe_round(x.get('change_pct'),2)}% | {in_tool} | المرحلة: {x.get('tool_stage') or '-'}")
        return "\n".join(lines)
    return result


def weekly_evidence_summary(week_key: str | None = None, format: str = "json", limit: int = 50) -> dict | str:
    wk = str(week_key or _current_week_key() or "")
    init_evidence_db()
    with _connect() as conn:
        summary = conn.execute(
            """
            SELECT
              COUNT(*) AS snapshots,
              COUNT(DISTINCT symbol) AS symbols,
              SUM(CASE WHEN in_big_movers=1 THEN 1 ELSE 0 END) AS big_mover_snapshots,
              SUM(CASE WHEN in_tool_snapshot=1 THEN 1 ELSE 0 END) AS tool_snapshots,
              AVG(change_pct) AS avg_change_pct,
              AVG(dollar_volume) AS avg_dollar_volume,
              SUM(CASE WHEN no_chase_flag=1 THEN 1 ELSE 0 END) AS no_chase_count,
              SUM(CASE WHEN plan_needs_reconfirm=1 THEN 1 ELSE 0 END) AS reconfirm_count
            FROM evidence_snapshots WHERE week_key=?
            """,
            (wk,),
        ).fetchone()
        top_movers = conn.execute(
            "SELECT symbol, MAX(change_pct) AS max_change_pct, MAX(in_tool_snapshot) AS in_tool_snapshot, MAX(in_big_movers) AS in_big_movers, COUNT(*) AS observations FROM evidence_snapshots WHERE week_key=? GROUP BY symbol ORDER BY max_change_pct DESC LIMIT ?",
            (wk, max(1, min(int(limit or 50), 200))),
        ).fetchall()
        session_rows = conn.execute(
            "SELECT session, COUNT(*) AS c, COUNT(DISTINCT symbol) AS symbols FROM evidence_snapshots WHERE week_key=? GROUP BY session ORDER BY c DESC",
            (wk,),
        ).fetchall()
        winner_profile_count = conn.execute("SELECT COUNT(*) AS c, COUNT(DISTINCT symbol) AS symbols FROM evidence_winner_profiles WHERE week_key=?", (wk,)).fetchone()
        intraday_bar_count = conn.execute("SELECT COUNT(*) AS c, COUNT(DISTINCT symbol) AS symbols FROM evidence_intraday_bars WHERE week_key=?", (wk,)).fetchone()
        winner_patterns = conn.execute("SELECT likely_pattern, move_quality_label, COUNT(*) AS cases, AVG(winner_change_pct) AS avg_gain FROM evidence_winner_profiles WHERE week_key=? GROUP BY likely_pattern, move_quality_label ORDER BY cases DESC, avg_gain DESC LIMIT 12", (wk,)).fetchall()
    result = {
        "ok": True,
        "version": "evidence_collection_v2_passive",
        "week_key": wk,
        "summary": dict(summary) if summary else {},
        "sessions": _rows_to_dicts(session_rows),
        "winner_profiles": dict(winner_profile_count) if winner_profile_count else {},
        "intraday_bars": dict(intraday_bar_count) if intraday_bar_count else {},
        "winner_patterns": _rows_to_dicts(winner_patterns),
        "top_movers_observed": _rows_to_dicts(top_movers),
        "market_fear": get_market_fear_snapshot(force_refresh=False, store=True),
        "notes": {
            "safe_mode": "جمع أدلة فقط؛ لا يغير السكور أو التصنيف.",
            "next_weekend_use": "تحليل ما سبق الرابحين والخاسرين وتحديد الأنماط المتكررة.",
        },
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        s = result["summary"] or {}
        lines = [
            "تقرير Evidence Collection V2",
            f"الأسبوع: {wk}",
            f"اللقطات المحفوظة: {int(s.get('snapshots') or 0)}",
            f"الرموز الفريدة: {int(s.get('symbols') or 0)}",
            f"لقطات لأسهم رابحة يومية: {int(s.get('big_mover_snapshots') or 0)}",
            f"لقطات من داخل الأداة: {int(s.get('tool_snapshots') or 0)}",
            f"تحذيرات لا تطارد: {int(s.get('no_chase_count') or 0)}",
            f"خطط تحتاج إعادة تأكيد: {int(s.get('reconfirm_count') or 0)}",
            f"ملفات رابحين كبار محللة: {int((result.get('winner_profiles') or {}).get('c') or 0)}",
            f"شموع/لقطات Polygon محفوظة: {int((result.get('intraday_bars') or {}).get('c') or 0)}",
            f"VIX / خوف السوق: {((result.get('market_fear') or {}).get('summary_ar') or 'غير متوفر')}",
            "",
            "أنماط الرابحين المبدئية:",
        ]
        for ptn in result.get("winner_patterns", [])[:8]:
            lines.append(f"- {ptn.get('likely_pattern') or 'غير مصنف'} / {ptn.get('move_quality_label') or '-'}: {ptn.get('cases')} حالات | متوسط الصعود {safe_round(ptn.get('avg_gain'),2)}%")
        lines.append("")
        lines.append("أعلى الرموز المرصودة تغيرًا:")
        for x in result["top_movers_observed"][:20]:
            tag = "داخل الأداة" if int(x.get("in_tool_snapshot") or 0) else "خارج الأداة"
            big = " / رابح يومي" if int(x.get("in_big_movers") or 0) else ""
            lines.append(f"- {x.get('symbol')}: {safe_round(x.get('max_change_pct'),2)}% | {tag}{big} | مشاهدات: {x.get('observations')}")
        return "\n".join(lines)
    return result


def export_evidence_json(week_key: str | None = None, trade_date: str | None = None, limit: int = 10000) -> dict:
    wk = str(week_key or _current_week_key() or "")
    d = str(trade_date or "")[:10]
    init_evidence_db()
    where = []
    args: list[Any] = []
    if wk:
        where.append("week_key=?")
        args.append(wk)
    if d:
        where.append("trade_date=?")
        args.append(d)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    lim = max(1, min(int(limit or 10000), int(EVIDENCE_EXPORT_MAX_ROWS or 5000)))
    with _connect() as conn:
        snaps = conn.execute(f"SELECT * FROM evidence_snapshots {where_sql} ORDER BY captured_at DESC LIMIT ?", (*args, lim)).fetchall()
        movers = conn.execute("SELECT * FROM daily_big_movers WHERE trade_date=? ORDER BY change_pct DESC LIMIT ?", (d or _today_text(), 500)).fetchall()
        runs = conn.execute(f"SELECT * FROM evidence_runs {where_sql} ORDER BY started_at DESC LIMIT 100", tuple(args)).fetchall() if where_sql else conn.execute("SELECT * FROM evidence_runs ORDER BY started_at DESC LIMIT 100").fetchall()
        winners = conn.execute(f"SELECT * FROM evidence_winner_profiles {where_sql} ORDER BY winner_change_pct DESC LIMIT ?", (*args, min(lim, 5000))).fetchall() if where_sql else conn.execute("SELECT * FROM evidence_winner_profiles ORDER BY trade_date DESC, winner_change_pct DESC LIMIT ?", (min(lim, 5000),)).fetchall()
        bars = conn.execute(f"SELECT * FROM evidence_intraday_bars {where_sql} ORDER BY trade_date DESC, symbol, bar_ts LIMIT ?", (*args, min(lim, int(EVIDENCE_SYNC_BAR_SAMPLE_LIMIT or 0)))).fetchall() if (where_sql and int(EVIDENCE_SYNC_BAR_SAMPLE_LIMIT or 0) > 0) else []
    return {
        "ok": True,
        "version": "evidence_collection_v2_passive",
        "week_key": wk,
        "trade_date": d,
        "exported_at": _now_text(),
        "snapshots_count": len(snaps),
        "daily_big_movers_count": len(movers),
        "winner_profiles_count": len(winners),
        "intraday_bars_count": len(bars),
        "runs_count": len(runs),
        "snapshots": _rows_to_dicts(snaps),
        "daily_big_movers": _rows_to_dicts(movers),
        "winner_profiles": _rows_to_dicts(winners),
        "intraday_bars": _rows_to_dicts(bars),
        "runs": _rows_to_dicts(runs),
        "market_fear": get_market_fear_snapshot(force_refresh=False, store=True),
        "market_fear_history": (get_json("market_fear_history", []) or [])[-50:],
    }


def export_evidence_csv(week_key: str | None = None, trade_date: str | None = None, limit: int = 10000) -> str:
    data = export_evidence_json(week_key=week_key, trade_date=trade_date, limit=limit)
    rows = data.get("snapshots", []) if isinstance(data, dict) else []
    output = io.StringIO()
    fields = [
        "captured_at_text", "week_key", "trade_date", "session", "symbol", "source_group", "in_tool_snapshot", "in_big_movers",
        "signal_bucket", "decision", "sharia_status", "plan_family", "price", "change_pct", "volume", "dollar_volume",
        "entry_price", "target_price", "stop_loss", "support_price", "resistance_price", "distance_from_entry_pct",
        "distance_from_support_pct", "distance_from_resistance_pct", "gap_from_prev_close_pct", "pre_market_change_pct", "pre_market_volume",
        "pre_market_dollar_volume", "after_hours_change_pct", "open_gap_pct", "first_15m_followthrough", "first_30m_followthrough",
        "held_above_open", "held_above_vwap_proxy", "gap_fade_flag", "gap_retest_success", "no_chase_flag", "plan_needs_reconfirm",
        "liquidity_score", "momentum_acceleration_score", "pattern_risk_score", "quote_source", "price_source",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()




def _query_tracking_rows_for_pattern_lab(week_key: str) -> list[dict]:
    if not SQLITE_ENABLED:
        return []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tracking_signals WHERE week_key=? ORDER BY first_seen_at DESC LIMIT 5000",
                (str(week_key or ""),),
            ).fetchall()
        return _rows_to_dicts(rows)
    except Exception:
        return []


def _tracking_loss_signature(row: dict) -> str:
    tags = _json_loads(row.get("risk_tags_json"), []) or []
    if not isinstance(tags, list):
        tags = []
    plan = str(row.get("plan_family") or "unknown")
    parts = [plan]
    if _safe_float(row.get("nearest_resistance_distance_pct"), 0) and _safe_float(row.get("nearest_resistance_distance_pct"), 0) <= 1.5:
        parts.append("near_resistance")
    if _safe_float(row.get("distance_to_52w_high_pct"), 0) and abs(_safe_float(row.get("distance_to_52w_high_pct"), 0)) <= 3:
        parts.append("near_52w_high")
    if any("السيولة" in str(t) and ("لم تستمر" in str(t) or "ضعف" in str(t)) for t in tags):
        parts.append("liquidity_not_persistent")
    if any("كسر الدعم" in str(t) for t in tags):
        parts.append("support_break")
    if _safe_float(row.get("volatility_pct"), 0) >= 5:
        parts.append("high_volatility")
    return " | ".join(parts)



def _winner_signature(row: dict) -> str:
    d = _enrich_winner_profile_row(row, week_key=str(row.get("week_key") or ""), refresh_visibility=False)
    return " | ".join([
        str(d.get("likely_pattern") or "unclassified_winner"),
        str(d.get("gap_quality_class") or "gap_unknown"),
        str(d.get("tradability_bucket") or "tradability_unknown"),
    ])


def _visibility_summary_from_rows(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in rows or []:
        conf = str(r.get("visibility_confidence_label") or "unknown")
        stage = str(r.get("best_tool_stage") or r.get("tool_stage") or "unknown")
        key = (conf, stage)
        item = groups.setdefault(key, {"visibility_confidence_label": conf, "best_tool_stage": stage, "cases": 0, "sum_gain": 0.0, "tool_seen_count": 0, "source_seen_count": 0})
        item["cases"] += 1
        item["sum_gain"] += _safe_float(r.get("winner_change_pct"), 0)
        item["tool_seen_count"] += 1 if int(r.get("tool_seen") or 0) else 0
        item["source_seen_count"] += 1 if int(r.get("source_seen") or 0) else 0
    out = []
    for item in groups.values():
        cases = max(1, int(item["cases"]))
        out.append({
            "visibility_confidence_label": item["visibility_confidence_label"],
            "best_tool_stage": item["best_tool_stage"],
            "cases": cases,
            "avg_gain": safe_round(item["sum_gain"] / cases, 2),
            "tool_seen_count": item["tool_seen_count"],
            "source_seen_count": item["source_seen_count"],
        })
    out.sort(key=lambda x: int(x.get("cases") or 0), reverse=True)
    return out


def pattern_lab_report(week_key: str | None = None, trade_date: str | None = None, format: str = "json", limit: int = 40) -> dict | str:
    """Exploratory Pattern Lab report V4a.

    Read-only. It does not change scoring/ranking. V4a fixes the previous
    report weakness where existing rows showed gap_unknown/tradability_unknown
    even when the raw winner profile already contained enough data to classify
    them. It computes safe read-time fallbacks and tries to link each winner to
    historical source/promotion visibility where those tables exist.
    """
    wk = str(week_key or _current_week_key() or "")
    td = str(trade_date or "")[:10]
    init_evidence_db()
    where = []
    args: list[Any] = []
    if wk:
        where.append("week_key=?")
        args.append(wk)
    if td:
        where.append("trade_date=?")
        args.append(td)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    lim = max(5, min(int(limit or 40), 200))
    with _connect() as conn:
        raw_winners = _rows_to_dicts(conn.execute(f"SELECT * FROM evidence_winner_profiles {where_sql} ORDER BY winner_change_pct DESC LIMIT 5000", tuple(args)).fetchall())

    # Enrich in Python so old backfilled rows get gap/tradability classifications
    # without requiring another heavy backfill.
    winners = [_enrich_winner_profile_row(w, week_key=wk, refresh_visibility=True) for w in raw_winners]
    pattern_rows = _aggregate_winner_rows(winners, ["likely_pattern", "gap_quality_class", "tradability_bucket"], limit=30)
    tradability_rows = _aggregate_winner_rows(winners, ["tradability_bucket"], limit=20)
    gap_rows = _aggregate_winner_rows(winners, ["gap_quality_class"], limit=20)
    visibility_rows = _visibility_summary_from_rows(winners)[:30]

    tracking = _query_tracking_rows_for_pattern_lab(wk)
    losses = [r for r in tracking if str(r.get("outcome_group") or "").lower() == "loss" or str(r.get("status") or "") in {"stopped"}]
    successes = [r for r in tracking if str(r.get("outcome_group") or "").lower() == "success" or str(r.get("status") or "") in {"target_hit", "above_target"}]
    loss_groups: dict[str, dict] = {}
    for r in losses:
        sig = _tracking_loss_signature(r)
        item = loss_groups.setdefault(sig, {"signature": sig, "cases": 0, "symbols": set(), "avg_max_loss_pct": 0.0, "avg_max_gain_pct": 0.0})
        item["cases"] += 1
        item["symbols"].add(str(r.get("symbol") or ""))
        item["avg_max_loss_pct"] += _safe_float(r.get("max_loss_pct"), 0)
        item["avg_max_gain_pct"] += _safe_float(r.get("max_gain_pct"), 0)
    loss_summary = []
    for item in loss_groups.values():
        cases = max(1, int(item["cases"]))
        loss_summary.append({
            "signature": item["signature"],
            "cases": cases,
            "unique_symbols": len([s for s in item["symbols"] if s]),
            "avg_max_loss_pct": safe_round(item["avg_max_loss_pct"] / cases, 2),
            "avg_max_gain_pct": safe_round(item["avg_max_gain_pct"] / cases, 2),
        })
    loss_summary = sorted(loss_summary, key=lambda x: x["cases"], reverse=True)[:20]

    total = len(winners)
    historical_visibility = len([w for w in winners if str(w.get("visibility_confidence_label") or "") == "historical_timeline"])
    snapshot_visibility = len([w for w in winners if str(w.get("visibility_confidence_label") or "") == "current_or_evidence_snapshot_only"])
    missing_historical_visibility = max(0, total - historical_visibility)
    low_tradability = len([w for w in winners if str(w.get("tradability_bucket") or "") == "micro_or_special_high_risk"])
    gap_unknown = len([w for w in winners if not str(w.get("gap_quality_class") or "") or str(w.get("gap_quality_class") or "") in {"gap_unknown", "unknown"}])
    tradability_unknown = len([w for w in winners if not str(w.get("tradability_bucket") or "") or str(w.get("tradability_bucket") or "") in {"tradability_unknown", "unknown"}])
    source_unknown = len([w for w in winners if not int(w.get("source_seen") or 0) and not str(w.get("first_source_seen_at") or "")])

    data_gaps = []
    if missing_historical_visibility:
        if historical_visibility:
            data_gaps.append(f"{missing_historical_visibility} رابحًا لم يرتبط بعد بسجل منبع/ظهور تاريخي مؤكد، بينما {historical_visibility} لديهم ربط تاريخي. نحتاج استمرار جمع first_source/watch/cautious/strong خلال الأسبوع.")
        else:
            data_gaps.append(f"{missing_historical_visibility} رابحًا لا يملك ربطًا تاريخيًا مؤكدًا مع المنبع/الظهور؛ نحتاج first_source/watch/cautious/strong خلال الأسبوع.")
    if low_tradability:
        data_gaps.append(f"{low_tradability} رابحًا من عينة عالية المخاطر/منخفضة السيولة أو رموز خاصة؛ يجب فصلها عن الأنماط النظيفة.")
    if gap_unknown:
        data_gaps.append(f"{gap_unknown} رابحًا لا يملك تصنيف جودة قاب واضح حتى بعد fallback؛ راجع حقول القاب/الشموع.")
    if tradability_unknown:
        data_gaps.append(f"{tradability_unknown} رابحًا لا يملك تصنيف قابلية تداول واضح حتى بعد fallback.")
    if source_unknown:
        data_gaps.append(f"{source_unknown} رابحًا غير مؤكد هل دخل المنبع تاريخيًا أم لا؛ لا تعتمد على آخر snapshot فقط.")
    if not tracking:
        data_gaps.append("لا توجد بيانات Tracking كافية للمقارنة مع الخاسرين في نفس الأسبوع.")

    hypotheses = []
    for ptn in pattern_rows[:20]:
        pattern = str(ptn.get("likely_pattern") or "unclassified_winner")
        gap_cls = str(ptn.get("gap_quality_class") or "gap_unknown")
        trad = str(ptn.get("tradability_bucket") or "tradability_unknown")
        cases = int(ptn.get("cases") or 0)
        if cases < 3:
            continue
        label = "فرضية تحتاج تأكيد"
        if trad == "tradable_core" and gap_cls in {"no_gap_steady_followthrough", "healthy_gap_followthrough"} and _safe_float(ptn.get("avg_liq_persist"), 0) >= 50:
            label = "فرضية رابحة أولية قابلة للدراسة"
        if gap_cls in {"gap_chase_or_failed"} or trad == "micro_or_special_high_risk":
            label = "فرضية مخاطرة/مطاردة لا تعتمدها قبل مقارنة الخاسرين"
        if pattern == "first_hour_liquidity_acceleration" and trad != "micro_or_special_high_risk" and _safe_float(ptn.get("avg_liq_accel"), 0) >= 45:
            label = "فرضية التقاط مبكر تستحق مراقبة الاثنين"
        hypotheses.append({
            "pattern": pattern,
            "gap_quality_class": gap_cls,
            "tradability_bucket": trad,
            "cases": cases,
            "unique_symbols": int(ptn.get("unique_symbols") or 0),
            "avg_gain": safe_round(ptn.get("avg_gain"), 2),
            "avg_gap": safe_round(ptn.get("avg_gap"), 2),
            "avg_first30": safe_round(ptn.get("avg_first30"), 2),
            "avg_liq_accel": safe_round(ptn.get("avg_liq_accel"), 1),
            "avg_liq_persist": safe_round(ptn.get("avg_liq_persist"), 1),
            "tool_seen_count": int(ptn.get("tool_seen_count") or 0),
            "source_seen_count": int(ptn.get("source_seen_count") or 0),
            "label": label,
        })

    result = {
        "ok": True,
        "version": "pattern_lab_v4b_read_only_gap_evidence",
        "week_key": wk,
        "trade_date": td,
        "generated_at": _now_text(),
        "summary": {
            "winner_profiles": total,
            "tracking_rows": len(tracking),
            "tracking_success_rows": len(successes),
            "tracking_loss_rows": len(losses),
            "historical_visibility_winners": historical_visibility,
            "snapshot_only_visibility_winners": snapshot_visibility,
            "missing_historical_visibility": missing_historical_visibility,
            "low_tradability_winners": low_tradability,
            "source_unknown_winners": source_unknown,
            "gap_unknown_winners": gap_unknown,
            "tradability_unknown_winners": tradability_unknown,
        },
        "winner_pattern_groups": pattern_rows[:lim],
        "tradability_groups": tradability_rows,
        "gap_quality_groups": gap_rows,
        "visibility_groups": visibility_rows,
        "loss_pattern_groups": loss_summary,
        "hypotheses": hypotheses[:lim],
        "data_gaps": data_gaps,
        "decision": "جاهز لتحليل فرضيات أولية فقط" if total >= 50 else "العينة غير كافية بعد",
        "gap_evidence_fields": ["after_hours_change_pct", "pre_market_change_pct", "pre_market_volume", "pre_market_dollar_volume", "previous_close_near_high", "close_position_pct", "late_day_volume_spike", "open_gap_pct", "first_15m_followthrough", "first_30m_followthrough", "held_above_open", "held_above_vwap_proxy", "gap_fade_flag", "gap_retest_success"],
        "notes": "لا تغيّر هذه النتائج السكور أو الترتيب. V4b يثبت حقول دراسة القاب/خطر القاب ضمن Evidence ويضيف Retention Guard آمنًا بلا حذف فعلي.",
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        lines = [
            "تقرير Pattern Lab V4b — فرضيات فقط، بلا تغيير في السكور",
            f"الأسبوع: {wk} | التاريخ: {td or 'كل الأسبوع'}",
            f"ملفات الرابحين: {total} | إشارات Tracking: {len(tracking)} | خسائر Tracking: {len(losses)} | نجاحات Tracking: {len(successes)}",
            f"ربط تاريخي مؤكد: {historical_visibility} | ربط snapshot فقط/غير مكتمل: {missing_historical_visibility}",
            "",
            "أقوى فرضيات الرابحين الأولية:",
        ]
        if not hypotheses:
            lines.append("لا توجد فرضيات قوية كافية بعد؛ نحتاج جمع الأسبوع القادم.")
        for h in hypotheses[:12]:
            lines.append(f"- {h['pattern']} / {h['gap_quality_class']} / {h['tradability_bucket']}: {h['cases']} حالة | متوسط الصعود {h['avg_gain']}% | سيولة {h['avg_liq_persist']}/100 | {h['label']}")
        lines.append("")
        lines.append("جودة القاب:")
        for g in gap_rows[:10]:
            lines.append(f"- {g.get('gap_quality_class') or 'غير مصنف'}: {g.get('cases')} حالة | متوسط الصعود {safe_round(g.get('avg_gain'),2)}% | متوسط القاب {safe_round(g.get('avg_gap'),2)}%")
        lines.append("")
        lines.append("قابلية التداول:")
        for t in tradability_rows[:10]:
            lines.append(f"- {t.get('tradability_bucket') or 'غير مصنف'}: {t.get('cases')} حالة | متوسط الصعود {safe_round(t.get('avg_gain'),2)}% | متوسط حجم الدولار {safe_round(t.get('avg_dollar_volume'),0)}")
        if visibility_rows:
            lines.append("")
            lines.append("ربط الرابحين بالمنبع/الأداة:")
            for v in visibility_rows[:8]:
                lines.append(f"- {v.get('visibility_confidence_label') or 'غير معروف'} / {v.get('best_tool_stage') or '-'}: {v.get('cases')} حالة | source_seen {v.get('source_seen_count')} | tool_seen {v.get('tool_seen_count')}")
        if loss_summary:
            lines.append("")
            lines.append("أبرز أنماط الخسائر للمقارنة:")
            for l in loss_summary[:8]:
                lines.append(f"- {l['signature']}: {l['cases']} حالة | متوسط خسارة {l['avg_max_loss_pct']}%")
        lines.append("")
        lines.append("نواقص البيانات قبل اعتماد أي نمط:")
        if not data_gaps:
            lines.append("- لا توجد نواقص حرجة واضحة الآن، لكن نحتاج أسبوع جمع حي للتأكيد.")
        for gap in data_gaps[:8]:
            lines.append(f"- {gap}")
        return "\n".join(lines)
    return result


def _evidence_local_counts_for_archive(week_key: str, trade_date: str) -> dict:
    """Small count manifest used for GitHub verification and safe pruning."""
    wk = str(week_key or "")
    td = str(trade_date or "")[:10]
    init_evidence_db()
    with _connect() as conn:
        counts = {
            "evidence_snapshots": conn.execute("SELECT COUNT(*) AS c FROM evidence_snapshots WHERE week_key=? AND trade_date=?", (wk, td)).fetchone()["c"],
            "snapshot_symbols": conn.execute("SELECT COUNT(DISTINCT symbol) AS c FROM evidence_snapshots WHERE week_key=? AND trade_date=?", (wk, td)).fetchone()["c"],
            "evidence_winner_profiles": conn.execute("SELECT COUNT(*) AS c FROM evidence_winner_profiles WHERE week_key=? AND trade_date=?", (wk, td)).fetchone()["c"],
            "winner_symbols": conn.execute("SELECT COUNT(DISTINCT symbol) AS c FROM evidence_winner_profiles WHERE week_key=? AND trade_date=?", (wk, td)).fetchone()["c"],
            "evidence_intraday_bars": conn.execute("SELECT COUNT(*) AS c FROM evidence_intraday_bars WHERE week_key=? AND trade_date=?", (wk, td)).fetchone()["c"],
            "bar_symbols": conn.execute("SELECT COUNT(DISTINCT symbol) AS c FROM evidence_intraday_bars WHERE week_key=? AND trade_date=?", (wk, td)).fetchone()["c"],
            "daily_big_movers": conn.execute("SELECT COUNT(*) AS c FROM daily_big_movers WHERE trade_date=?", (td,)).fetchone()["c"],
            "evidence_runs": conn.execute("SELECT COUNT(*) AS c FROM evidence_runs WHERE week_key=? AND trade_date=?", (wk, td)).fetchone()["c"],
        }
    return {k: int(v or 0) for k, v in counts.items()}


def _compact_snapshot_rows_for_sync(week_key: str, trade_date: str, limit: int | None = None) -> list[dict]:
    """Return a compact, decision-useful sample without heavy raw_json payloads."""
    lim = max(50, min(int(limit or EVIDENCE_SYNC_SAMPLE_ROWS or 1500), int(EVIDENCE_EXPORT_MAX_ROWS or 5000)))
    cols = [
        "captured_at_text", "week_key", "trade_date", "session", "symbol", "source_group",
        "in_tool_snapshot", "in_big_movers", "signal_bucket", "decision", "sharia_status", "plan_family",
        "price", "previous_close", "change_pct", "volume", "dollar_volume", "entry_price", "target_price", "stop_loss",
        "support_price", "resistance_price", "distance_from_entry_pct", "distance_from_support_pct", "distance_from_resistance_pct",
        "gap_from_prev_close_pct", "pre_market_change_pct", "pre_market_volume", "pre_market_dollar_volume", "after_hours_change_pct",
        "open_gap_pct", "first_15m_followthrough", "first_30m_followthrough", "held_above_open", "held_above_vwap_proxy",
        "gap_fade_flag", "gap_retest_success", "first_seen_change_pct", "no_chase_flag", "plan_needs_reconfirm",
        "liquidity_score", "momentum_acceleration_score", "pattern_risk_score", "quote_source", "price_source",
    ]
    select_cols = ",".join(cols)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {select_cols} FROM evidence_snapshots WHERE week_key=? AND trade_date=? ORDER BY in_big_movers DESC, ABS(change_pct) DESC, captured_at DESC LIMIT ?",
            (str(week_key or ""), str(trade_date or "")[:10], lim),
        ).fetchall()
    return _rows_to_dicts(rows)


def _compact_winner_profile_rows_for_sync(week_key: str, trade_date: str, limit: int | None = None) -> list[dict]:
    lim = max(50, min(int(limit or EVIDENCE_SYNC_WINNER_LIMIT or 800), 2000))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM evidence_winner_profiles WHERE week_key=? AND trade_date=? ORDER BY winner_change_pct DESC LIMIT ?",
            (str(week_key or ""), str(trade_date or "")[:10], lim),
        ).fetchall()
    out = []
    for row in _rows_to_dicts(rows):
        # Keep fields needed for learning/verification; drop heavy/noisy raw payload fields.
        compact = {k: v for k, v in row.items() if k not in {"raw_json", "polygon_summary_json", "payload_json"}}
        out.append(_enrich_winner_profile_row(compact, week_key=week_key, refresh_visibility=False))
    return out


def _compact_evidence_archive_payload(week_key: str, trade_date: str) -> dict:
    """A compact archive that is safe for Railway/GitHub and still useful for learning."""
    wk = str(week_key or _current_week_key() or "current")
    td = str(trade_date or _today_text())[:10]
    counts = _evidence_local_counts_for_archive(wk, td)
    snapshots = _compact_snapshot_rows_for_sync(wk, td)
    winners = _compact_winner_profile_rows_for_sync(wk, td)
    return {
        "ok": True,
        "version": "evidence_compact_archive_v5_railway_safe",
        "week_key": wk,
        "trade_date": td,
        "exported_at": _now_text(),
        "compact": True,
        "manifest": {
            "local_counts_at_sync": counts,
            "snapshot_rows_in_archive": len(snapshots),
            "winner_profile_rows_in_archive": len(winners),
            "raw_intraday_bars_archived": False,
            "raw_snapshot_json_omitted": True,
            "reason": "Railway stability: preserve counts, samples, reports and winner profiles without uploading giant raw JSON/CSV payloads.",
        },
        "snapshots_sample": snapshots,
        "winner_profiles": winners,
        "daily_big_movers": daily_winners_report(trade_date=td, format="json", limit=500).get("items", []),
        "market_fear": get_market_fear_snapshot(force_refresh=False, store=True),
        "market_fear_history_tail": (get_json("market_fear_history", []) or [])[-80:],
    }


def sync_evidence_to_github(week_key: str | None = None, trade_date: str | None = None, include_csv: bool | None = None) -> dict:
    wk = str(week_key or _current_week_key() or "current")
    d = str(trade_date or _today_text())[:10]
    if include_csv is None:
        include_csv = bool(EVIDENCE_SYNC_INCLUDE_CSV_DEFAULT)
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured"}

    base = f"{EVIDENCE_GITHUB_ARCHIVE_PATH}/{wk}"
    json_path = f"{base}/{d}_evidence.json"
    summary_path = f"{base}/{d}_summary.json"
    winners_path = f"{base}/{d}_winner_profiles.json"
    readiness_path = f"{base}/{d}_pattern_readiness.json"
    pattern_lab_path = f"{base}/{d}_pattern_lab.json"
    market_fear_path = f"{base}/{d}_market_fear.json"
    manifest_path = f"{base}/{d}_manifest.json"
    paths = {
        "json": json_path,
        "summary": summary_path,
        "winner_profiles": winners_path,
        "pattern_readiness": readiness_path,
        "pattern_lab": pattern_lab_path,
        "market_fear": market_fear_path,
        "manifest": manifest_path,
    }

    local_counts = _evidence_local_counts_for_archive(wk, d)
    compact_payload = _compact_evidence_archive_payload(wk, d) if EVIDENCE_GITHUB_COMPACT_SYNC else export_evidence_json(week_key=wk, trade_date=d, limit=EVIDENCE_SYNC_SAMPLE_ROWS)
    manifest = {
        "ok": True,
        "version": "evidence_archive_manifest_v5",
        "week_key": wk,
        "trade_date": d,
        "generated_at": _now_text(),
        "compact_sync": bool(EVIDENCE_GITHUB_COMPACT_SYNC),
        "local_counts_at_sync": local_counts,
        "prune_allowed_after_verify": True,
        "notes": "This manifest is the verification anchor before any Railway pruning. No deletion is performed by sync.",
    }
    summary = weekly_evidence_summary(week_key=wk, format="json")
    if isinstance(summary, dict):
        summary.setdefault("archive_manifest", manifest)
    market_fear_payload = {
        "ok": True,
        "week_key": wk,
        "trade_date": d,
        "snapshot": get_market_fear_snapshot(force_refresh=False, store=True),
        "history_tail": (get_json("market_fear_history", []) or [])[-80:],
    }
    files = [
        {"label": "manifest", "path": manifest_path, "content": manifest, "is_json": True},
        {"label": "json", "path": json_path, "content": compact_payload, "is_json": True},
        {"label": "summary", "path": summary_path, "content": summary, "is_json": True},
        {"label": "winner_profiles", "path": winners_path, "content": {"ok": True, "version": "winner_profiles_compact_v5", "week_key": wk, "trade_date": d, "manifest": manifest, "items": _compact_winner_profile_rows_for_sync(wk, d)}, "is_json": True},
        {"label": "pattern_readiness", "path": readiness_path, "content": pattern_readiness_report(week_key=wk, format="json"), "is_json": True},
        {"label": "pattern_lab", "path": pattern_lab_path, "content": pattern_lab_report(week_key=wk, trade_date=d, format="json"), "is_json": True},
        {"label": "market_fear", "path": market_fear_path, "content": market_fear_payload, "is_json": True},
    ]
    if include_csv:
        csv_path = f"{base}/{d}_evidence.csv"
        csv_text = "\ufeff" + export_evidence_csv(week_key=wk, trade_date=d, limit=EVIDENCE_SYNC_SAMPLE_ROWS)
        paths["csv"] = csv_path
        files.append({"label": "csv", "path": csv_path, "content": csv_text, "is_json": False})

    batch = push_multiple_files(files, message=f"Sync evidence archive {wk} {d}")
    file_results = {}
    if batch.get("ok"):
        commit_sha = str(batch.get("commit_sha") or "")
        for item in batch.get("files", []) or []:
            if isinstance(item, dict):
                label = str(item.get("label") or "")
                if label:
                    file_results[label] = {
                        "ok": True,
                        "configured": True,
                        "path": item.get("path", ""),
                        "branch": batch.get("branch", ""),
                        "commit_sha": commit_sha,
                        "bytes": item.get("bytes", 0),
                        "synced_at": batch.get("synced_at", ""),
                    }
    else:
        for item in files:
            label = str(item.get("label") or "")
            if label:
                file_results[label] = {"ok": False, "configured": True, "path": item.get("path", ""), "error": batch.get("error", "batch_sync_failed")}

    results = {
        "ok": bool(batch.get("ok")),
        "version": "evidence_github_sync_v5c_contents_fallback_compact",
        "week_key": wk,
        "trade_date": d,
        "paths": paths,
        "local_counts_at_sync": local_counts,
        "compact_sync": bool(EVIDENCE_GITHUB_COMPACT_SYNC),
        "include_csv": bool(include_csv),
        "batch_commit": bool(batch.get("method") == "git_data_batch"),
        "github_sync_method": batch.get("method", ""),
        "batch": batch,
        **file_results,
    }
    try:
        set_json("evidence_last_github_sync", results)
    except Exception:
        pass
    return results


# ---------------------------------------------------------------------------
# Retention Guard V4b (safe, no deletion by default)
# ---------------------------------------------------------------------------

def _archive_paths_for(week_key: str | None = None, trade_date: str | None = None) -> dict[str, str]:
    wk = str(week_key or _current_week_key() or "current")
    d = str(trade_date or _today_text())[:10]
    base = f"{EVIDENCE_GITHUB_ARCHIVE_PATH}/{wk}"
    return {
        "evidence_json": f"{base}/{d}_evidence.json",
        "summary_json": f"{base}/{d}_summary.json",
        "winner_profiles_json": f"{base}/{d}_winner_profiles.json",
        "pattern_readiness_json": f"{base}/{d}_pattern_readiness.json",
        "pattern_lab_json": f"{base}/{d}_pattern_lab.json",
        "market_fear_json": f"{base}/{d}_market_fear.json",
        "manifest_json": f"{base}/{d}_manifest.json",
        "evidence_csv": f"{base}/{d}_evidence.csv",
    }



def _normalize_retention_sync_paths(paths: dict | None) -> dict[str, str]:
    """Normalize sync result path keys to retention verifier path keys."""
    raw = paths if isinstance(paths, dict) else {}
    return {
        "evidence_json": str(raw.get("evidence_json") or raw.get("json") or ""),
        "summary_json": str(raw.get("summary_json") or raw.get("summary") or ""),
        "winner_profiles_json": str(raw.get("winner_profiles_json") or raw.get("winner_profiles") or ""),
        "pattern_readiness_json": str(raw.get("pattern_readiness_json") or raw.get("pattern_readiness") or ""),
        "pattern_lab_json": str(raw.get("pattern_lab_json") or raw.get("pattern_lab") or ""),
        "market_fear_json": str(raw.get("market_fear_json") or raw.get("market_fear") or ""),
        "manifest_json": str(raw.get("manifest_json") or raw.get("manifest") or ""),
        "evidence_csv": str(raw.get("evidence_csv") or raw.get("csv") or ""),
    }


def _last_successful_github_sync() -> dict:
    """Return the last successful GitHub evidence sync, if available."""
    try:
        last = get_json("evidence_last_github_sync", {})
    except Exception:
        last = {}
    if not isinstance(last, dict) or not last.get("ok"):
        return {}
    trade_date = str(last.get("trade_date") or "")[:10]
    week_key = str(last.get("week_key") or "")
    if not trade_date or not week_key:
        return {}
    return last


def _resolve_retention_archive_target(week_key: str | None = None, trade_date: str | None = None) -> dict:
    """Choose the archive target for retention checks.

    V4c behavior:
    - If the caller gives week_key/trade_date, respect it.
    - If not, verify/prune checks use the last successful GitHub sync rather
      than today's date. This avoids false failures on weekends or non-trading
      days when no archive is expected for today.
    """
    requested_week = str(week_key or "").strip()
    requested_date = str(trade_date or "").strip()[:10]
    explicit = bool(requested_week or requested_date)
    last = _last_successful_github_sync()

    if not explicit and last:
        wk = str(last.get("week_key") or _current_week_key() or "current")
        td = str(last.get("trade_date") or _today_text())[:10]
        sync_paths = _normalize_retention_sync_paths(last.get("paths") if isinstance(last.get("paths"), dict) else {})
        fallback_paths = _archive_paths_for(wk, td)
        paths = {k: (sync_paths.get(k) or fallback_paths.get(k) or "") for k in fallback_paths.keys()}
        return {
            "week_key": wk,
            "trade_date": td,
            "paths": paths,
            "target_source": "last_successful_github_sync",
            "used_last_successful_sync": True,
            "explicit_target_requested": False,
            "last_successful_sync": last,
            "note": "No date was requested, so V4c selected the last successful GitHub sync instead of today's date.",
        }

    wk = str(requested_week or _current_week_key() or "current")
    td = str(requested_date or _today_text())[:10]
    return {
        "week_key": wk,
        "trade_date": td,
        "paths": _archive_paths_for(wk, td),
        "target_source": "explicit_request" if explicit else "current_date_fallback_no_previous_sync",
        "used_last_successful_sync": False,
        "explicit_target_requested": explicit,
        "last_successful_sync": last,
        "note": "Explicit retention target was used." if explicit else "No previous successful sync was available; current date fallback was used.",
    }

def _sqlite_count(table: str, where_sql: str = "", args: tuple = ()) -> int:
    try:
        init_evidence_db()
        sql = f"SELECT COUNT(*) AS c FROM {table} {where_sql}".strip()
        with _connect() as conn:
            row = conn.execute(sql, args).fetchone()
        return int(row["c"] if row else 0)
    except Exception:
        return 0


def _retention_cutoff_date(keep_days: int | None = None) -> str:
    keep = max(1, int(keep_days if keep_days is not None else EVIDENCE_RETENTION_KEEP_DAYS))
    # Use New York date because all evidence trade_date values are NY market dates.
    cutoff = date.fromordinal((_now_dt().date()).toordinal() - keep)
    return cutoff.strftime("%Y-%m-%d")


def evidence_retention_status(week_key: str | None = None, trade_date: str | None = None) -> dict:
    """Return safe retention state. This never deletes Railway data."""
    target = _resolve_retention_archive_target(week_key, trade_date)
    wk = str(target.get("week_key") or _current_week_key() or "current")
    td = str(target.get("trade_date") or _today_text())[:10]
    cutoff = _retention_cutoff_date()
    paths = target.get("paths") if isinstance(target.get("paths"), dict) else _archive_paths_for(wk, td)
    last_sync = target.get("last_successful_sync") or get_json("evidence_last_github_sync", {})
    last_verify = get_json("evidence_last_retention_verify", {})
    last_dry = get_json("evidence_last_retention_prune_dry_run", {})
    counts = {
        "snapshots_total": _sqlite_count("evidence_snapshots"),
        "intraday_bars_total": _sqlite_count("evidence_intraday_bars"),
        "winner_profiles_total": _sqlite_count("evidence_winner_profiles"),
        "daily_big_movers_total": _sqlite_count("daily_big_movers"),
        "runs_total": _sqlite_count("evidence_runs"),
        "snapshots_candidate_old": _sqlite_count("evidence_snapshots", "WHERE trade_date < ?", (cutoff,)),
        "intraday_bars_candidate_old": _sqlite_count("evidence_intraday_bars", "WHERE trade_date < ?", (cutoff,)),
        "winner_profiles_candidate_old": _sqlite_count("evidence_winner_profiles", "WHERE trade_date < ?", (cutoff,)),
        "daily_big_movers_candidate_old": _sqlite_count("daily_big_movers", "WHERE trade_date < ?", (cutoff,)),
    }
    return {
        "ok": True,
        "version": "retention_guard_v5b_verified_prune_available_no_auto_delete",
        "week_key": wk,
        "trade_date": td,
        "generated_at": _now_text(),
        "sqlite_enabled": bool(SQLITE_ENABLED),
        "db_path": str(SQLITE_DB_PATH),
        "github_configured": bool(is_github_sync_configured()),
        "github_archive_path": EVIDENCE_GITHUB_ARCHIVE_PATH,
        "keep_recent_days": int(EVIDENCE_RETENTION_KEEP_DAYS),
        "cutoff_trade_date_exclusive": cutoff,
        "prune_enabled": bool(EVIDENCE_RETENTION_PRUNE_ENABLED),
        "actual_delete_available": True,
        "actual_delete_requires_confirm": "DELETE_ARCHIVED_EVIDENCE",
        "counts": counts,
        "paths_for_selected_date": paths,
        "retention_target": {
            "target_source": target.get("target_source"),
            "used_last_successful_sync": bool(target.get("used_last_successful_sync")),
            "explicit_target_requested": bool(target.get("explicit_target_requested")),
            "note": target.get("note", ""),
        },
        "last_github_sync": last_sync if isinstance(last_sync, dict) else {},
        "last_verify": last_verify if isinstance(last_verify, dict) else {},
        "last_prune_dry_run": last_dry if isinstance(last_dry, dict) else {},
        "safety_rules": [
            "No automatic Railway deletion. Manual prune-execute requires confirmation and GitHub verification.",
            "Default manual prune deletes only old intraday bars, daily movers, and evidence runs; snapshots/profiles require include_snapshots=true.",
            "Current week/current trade date are never deletion candidates.",
            "Deletion requires GitHub sync + readable manifest/JSON verification first.",
        ],
    }


def _verify_json_payload(name: str, path: str, expected_min_count: int | None = None) -> dict:
    fetched = fetch_json_file(path)
    out = {"name": name, "path": path, "ok": False, "exists": False, "readable": False, "count": 0, "sha": fetched.get("sha", "") if isinstance(fetched, dict) else ""}
    if not isinstance(fetched, dict) or not fetched.get("ok"):
        out["error"] = (fetched or {}).get("error") if isinstance(fetched, dict) else "fetch_failed"
        return out
    out["exists"] = bool(fetched.get("exists"))
    data = fetched.get("data")
    out["readable"] = data is not None
    if isinstance(data, dict):
        # Prefer the explicit count fields, fall back to list lengths.
        count = 0
        for key in ["snapshots_count", "winner_profiles_count", "intraday_bars_count", "runs_count"]:
            count += _safe_int(data.get(key), 0)
        if count <= 0:
            for key in ["snapshots", "winner_profiles", "intraday_bars", "daily_big_movers", "runs"]:
                val = data.get(key)
                if isinstance(val, list):
                    count += len(val)
        if count <= 0 and any(k in data for k in ["summary", "hypotheses", "winner_pattern_groups", "winner_profiles"]):
            count = 1
        out["count"] = int(count)
    elif isinstance(data, list):
        out["count"] = len(data)
    out["ok"] = bool(out["exists"] and out["readable"] and (out["count"] > 0 or expected_min_count in (None, 0)))
    if expected_min_count is not None and expected_min_count > 0:
        out["expected_min_count"] = int(expected_min_count)
        # GitHub exports may be limited, so require non-empty rather than exact parity.
        out["count_match_level"] = "non_empty" if out["count"] > 0 else "empty"
    return out


def evidence_retention_verify_github(week_key: str | None = None, trade_date: str | None = None, include_csv: bool = False) -> dict:
    """Verify GitHub archive before any Railway pruning.

    V5 prefers the manifest written by compact sync. The manifest contains local
    row counts captured at sync time. This avoids downloading huge JSON/CSV files
    just to verify an archive and prevents another memory/egress spike.
    """
    target = _resolve_retention_archive_target(week_key, trade_date)
    wk = str(target.get("week_key") or _current_week_key() or "current")
    td = str(target.get("trade_date") or _today_text())[:10]
    paths = target.get("paths") if isinstance(target.get("paths"), dict) else _archive_paths_for(wk, td)
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured", "week_key": wk, "trade_date": td, "retention_target": target}

    local_now = _evidence_local_counts_for_archive(wk, td)
    checks = {}
    manifest_fetch = fetch_json_file(paths.get("manifest_json") or _archive_paths_for(wk, td).get("manifest_json", ""))
    manifest_data = manifest_fetch.get("data") if isinstance(manifest_fetch, dict) else None
    manifest_counts = {}
    if isinstance(manifest_data, dict):
        manifest_counts = ((manifest_data.get("local_counts_at_sync") or {}) if isinstance(manifest_data.get("local_counts_at_sync"), dict) else {})
    manifest_ok = bool(isinstance(manifest_fetch, dict) and manifest_fetch.get("ok") and manifest_fetch.get("exists") and isinstance(manifest_data, dict) and manifest_counts)
    count_checks = {}
    for k in ["evidence_snapshots", "evidence_winner_profiles", "evidence_intraday_bars", "daily_big_movers", "evidence_runs"]:
        archived = int(_safe_int(manifest_counts.get(k), 0)) if manifest_counts else 0
        current = int(local_now.get(k, 0))
        count_checks[k] = {
            "archived_count": archived,
            "current_count": current,
            # Current can be higher after sync if more evidence was collected later.
            "ok": bool(archived > 0 or current == 0),
            "note": "current_may_be_higher_after_sync" if current >= archived else "current_lower_than_manifest_check_manually",
        }
        if archived > 0 and current < archived:
            count_checks[k]["ok"] = False

    checks["manifest_json"] = {
        "name": "manifest_json",
        "path": paths.get("manifest_json", ""),
        "ok": manifest_ok,
        "exists": bool(isinstance(manifest_fetch, dict) and manifest_fetch.get("exists")),
        "readable": isinstance(manifest_data, dict),
        "counts_present": bool(manifest_counts),
        "error": manifest_fetch.get("error", "") if isinstance(manifest_fetch, dict) else "fetch_failed",
    }
    for name in ["evidence_json", "summary_json", "winner_profiles_json", "pattern_readiness_json", "pattern_lab_json", "market_fear_json"]:
        path = paths.get(name) or ""
        if not path:
            checks[name] = {"name": name, "ok": False, "exists": False, "error": "missing_path"}
            continue
        fetch = fetch_json_file(path)
        data = fetch.get("data") if isinstance(fetch, dict) else None
        checks[name] = {
            "name": name,
            "path": path,
            "ok": bool(isinstance(fetch, dict) and fetch.get("ok") and fetch.get("exists") and data is not None),
            "exists": bool(isinstance(fetch, dict) and fetch.get("exists")),
            "readable": data is not None,
            "sha": fetch.get("sha", "") if isinstance(fetch, dict) else "",
            "error": fetch.get("error", "") if isinstance(fetch, dict) else "fetch_failed",
        }
    if include_csv and paths.get("evidence_csv"):
        csv_fetch = fetch_text_file(paths["evidence_csv"])
        content = csv_fetch.get("content") if isinstance(csv_fetch, dict) else None
        line_count = len([ln for ln in str(content or "").splitlines() if ln.strip()]) if content is not None else 0
        checks["evidence_csv"] = {
            "name": "evidence_csv",
            "path": paths["evidence_csv"],
            "ok": bool(isinstance(csv_fetch, dict) and csv_fetch.get("ok") and csv_fetch.get("exists") and line_count >= 1),
            "exists": bool(isinstance(csv_fetch, dict) and csv_fetch.get("exists")),
            "readable": content is not None,
            "line_count": line_count,
            "sha": csv_fetch.get("sha", "") if isinstance(csv_fetch, dict) else "",
            "error": csv_fetch.get("error", "") if isinstance(csv_fetch, dict) else "fetch_failed",
        }

    required = ["manifest_json", "evidence_json", "summary_json", "winner_profiles_json", "pattern_readiness_json", "pattern_lab_json"]
    files_ok = all(bool((checks.get(k) or {}).get("ok")) for k in required)
    counts_ok = all(bool(v.get("ok")) for v in count_checks.values()) if count_checks else False
    ok = bool(files_ok and counts_ok)
    result = {
        "ok": ok,
        "version": "retention_verify_github_v5_manifest_no_large_downloads",
        "configured": True,
        "week_key": wk,
        "trade_date": td,
        "verified_at": _now_text(),
        "paths": paths,
        "retention_target": {
            "target_source": target.get("target_source"),
            "used_last_successful_sync": bool(target.get("used_last_successful_sync")),
            "explicit_target_requested": bool(target.get("explicit_target_requested")),
            "note": target.get("note", ""),
        },
        "local_counts_now": local_now,
        "manifest_counts_at_sync": manifest_counts,
        "count_checks": count_checks,
        "checks": checks,
        "notes": "Verification only. No Railway deletion is performed here. Uses manifest to avoid huge downloads.",
    }
    try:
        set_json("evidence_last_retention_verify", result)
    except Exception:
        pass
    return result


def evidence_retention_prune_dry_run(week_key: str | None = None, trade_date: str | None = None, keep_days: int | None = None, require_verified: bool = True) -> dict:
    """Show what could be pruned later. This function never deletes data."""
    target = _resolve_retention_archive_target(week_key, trade_date)
    wk = str(target.get("week_key") or _current_week_key() or "current")
    td = str(target.get("trade_date") or _today_text())[:10]
    cutoff = _retention_cutoff_date(keep_days)
    verify = evidence_retention_verify_github(wk, td, include_csv=False) if require_verified else {"ok": True, "skipped": True, "retention_target": target}
    candidates = {
        "evidence_snapshots": _sqlite_count("evidence_snapshots", "WHERE trade_date < ? AND week_key != ?", (cutoff, wk)),
        "evidence_intraday_bars": _sqlite_count("evidence_intraday_bars", "WHERE trade_date < ? AND week_key != ?", (cutoff, wk)),
        "evidence_winner_profiles": _sqlite_count("evidence_winner_profiles", "WHERE trade_date < ? AND week_key != ?", (cutoff, wk)),
        "daily_big_movers": _sqlite_count("daily_big_movers", "WHERE trade_date < ?", (cutoff,)),
        "evidence_runs": _sqlite_count("evidence_runs", "WHERE trade_date < ? AND week_key != ?", (cutoff, wk)),
    }
    would_delete = bool((not require_verified or verify.get("ok")) and any(v > 0 for v in candidates.values()))
    result = {
        "ok": True,
        "version": "retention_prune_dry_run_v5b_verified_no_delete",
        "week_key": wk,
        "trade_date": td,
        "generated_at": _now_text(),
        "keep_days": int(keep_days if keep_days is not None else EVIDENCE_RETENTION_KEEP_DAYS),
        "cutoff_trade_date_exclusive": cutoff,
        "require_verified": bool(require_verified),
        "verification_ok": bool(verify.get("ok")),
        "verification": verify,
        "candidate_rows_by_table": candidates,
        "candidate_total_rows": int(sum(candidates.values())),
        "would_delete_if_future_prune_enabled": bool(would_delete),
        "deleted_rows": 0,
        "prune_enabled_now": bool(EVIDENCE_RETENTION_PRUNE_ENABLED),
        "retention_target": {
            "target_source": target.get("target_source"),
            "used_last_successful_sync": bool(target.get("used_last_successful_sync")),
            "explicit_target_requested": bool(target.get("explicit_target_requested")),
            "note": target.get("note", ""),
        },
        "notes": "Dry-run only. V4c intentionally performs zero Railway deletions.",
    }
    try:
        set_json("evidence_last_retention_prune_dry_run", result)
    except Exception:
        pass
    return result

def evidence_retention_prune_execute(
    week_key: str | None = None,
    trade_date: str | None = None,
    keep_days: int | None = None,
    require_verified: bool = True,
    confirm: str = "",
    include_snapshots: bool = False,
) -> dict:
    """Safely prune old Railway evidence rows after GitHub verification.

    Default deletes only old intraday bars/runs/big-mover rows because they are the
    heaviest operational data. Snapshots and winner profiles are preserved unless
    include_snapshots=true is explicitly passed after verification.
    """
    if str(confirm or "").strip() != "DELETE_ARCHIVED_EVIDENCE":
        return {
            "ok": False,
            "deleted_rows": 0,
            "error": "confirmation_required",
            "required_confirm": "DELETE_ARCHIVED_EVIDENCE",
            "notes": "No deletion was performed.",
        }
    if require_verified:
        verify = evidence_retention_verify_github(week_key=week_key, trade_date=trade_date, include_csv=False)
        if not verify.get("ok"):
            return {"ok": False, "deleted_rows": 0, "error": "github_verification_failed", "verification": verify, "notes": "No deletion was performed."}
    else:
        verify = {"ok": True, "skipped": True}

    target = _resolve_retention_archive_target(week_key, trade_date)
    wk = str(target.get("week_key") or _current_week_key() or "current")
    cutoff = _retention_cutoff_date(keep_days)
    tables = [
        ("evidence_intraday_bars", "trade_date < ? AND week_key != ?"),
        ("daily_big_movers", "trade_date < ?"),
        ("evidence_runs", "trade_date < ? AND week_key != ?"),
    ]
    if bool(include_snapshots):
        tables.extend([
            ("evidence_snapshots", "trade_date < ? AND week_key != ?"),
            ("evidence_winner_profiles", "trade_date < ? AND week_key != ?"),
        ])
    before = {}
    deleted = {}
    with _LOCK:
        with _connect() as conn:
            for table, where_sql in tables:
                if table == "daily_big_movers":
                    args = (cutoff,)
                else:
                    args = (cutoff, wk)
                before[table] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {where_sql}", args).fetchone()["c"] or 0)
                cur = conn.execute(f"DELETE FROM {table} WHERE {where_sql}", args)
                deleted[table] = int(cur.rowcount if cur.rowcount is not None else 0)
            if EVIDENCE_RETENTION_VACUUM_AFTER_PRUNE:
                conn.execute("VACUUM")
            conn.commit()
    result = {
        "ok": True,
        "version": "retention_prune_execute_v5_verified_guarded",
        "week_key_protected": wk,
        "trade_date_verified": str(target.get("trade_date") or "")[:10],
        "executed_at": _now_text(),
        "keep_days": int(keep_days if keep_days is not None else EVIDENCE_RETENTION_KEEP_DAYS),
        "cutoff_trade_date_exclusive": cutoff,
        "include_snapshots": bool(include_snapshots),
        "verification": verify,
        "candidate_rows_before_delete": before,
        "deleted_rows_by_table": deleted,
        "deleted_rows_total": int(sum(deleted.values())),
        "notes": "Pruned only data older than cutoff and outside the protected verified week. GitHub verification passed before deletion.",
    }
    try:
        set_json("evidence_last_retention_prune_execute", result)
    except Exception:
        pass
    return result

def _daily_auto_sync_due(session: str) -> bool:
    """Compatibility wrapper used by the background worker.

    The real schedule is Riyadh 05:45 after US trading days by default. We keep the
    session argument for backward compatibility but do not use it as the primary
    decision, because 5 AM Riyadh occurs while New York is closed.
    """
    status = evidence_auto_sync_status()
    return bool(status.get("due_now"))


def _mark_daily_auto_sync(result: dict) -> None:
    try:
        trade_date = str((result or {}).get("trade_date") or "")[:10]
        if trade_date:
            set_json(f"evidence_auto_github_synced_{EVIDENCE_AUTO_SYNC_STATE_VERSION}_{trade_date}", result)
        set_json("evidence_last_auto_sync", result)
    except Exception:
        pass


def _worker_lease_key() -> str:
    return "evidence_background_worker_lease"


def _claim_worker_lease() -> tuple[bool, dict]:
    """Best-effort cross-process guard for Railway.

    The old in-memory _WORKER_STARTED flag only protects one Python process. If
    Railway starts more than one process during deploy/restart, each could start a
    background evidence thread. This lease makes duplicate workers back off.
    """
    if not SQLITE_ENABLED:
        return True, {"sqlite": False}
    now = _now_ts()
    pid = os.getpid()
    try:
        lease = get_json(_worker_lease_key(), {})
        if isinstance(lease, dict) and float(lease.get("expires_at") or 0) > now and int(lease.get("pid") or -1) != pid:
            return False, lease
        new_lease = {"pid": pid, "claimed_at": _now_text(), "heartbeat_at": _now_text(), "expires_at": now + max(120, int(EVIDENCE_WORKER_LEASE_TTL_SEC or 300))}
        set_json(_worker_lease_key(), new_lease)
        return True, new_lease
    except Exception as exc:
        return True, {"lease_error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def _refresh_worker_lease() -> None:
    try:
        set_json(_worker_lease_key(), {"pid": os.getpid(), "heartbeat_at": _now_text(), "expires_at": _now_ts() + max(120, int(EVIDENCE_WORKER_LEASE_TTL_SEC or 300))})
    except Exception:
        pass


def _worker_loop() -> None:
    last_collect_ts = 0.0
    while True:
        try:
            _refresh_worker_lease()
            if not EVIDENCE_COLLECTION_ENABLED:
                time.sleep(600)
                continue
            session = _market_session()
            interval = _interval_for_session(session)
            now = _now_ts()
            # Collect only during actionable sessions. This is passive evidence and never changes decisions.
            if session in {"pre_market", "regular", "after_hours"} and now - last_collect_ts >= interval:
                collect_evidence_snapshot(mode="background", include_big_movers=True, sync_to_github=False)
                last_collect_ts = now
            # Same-day winner backfill after the market has enough data. This prepares profiles for next-week analysis.
            if _daily_winner_backfill_due(session):
                backfill = backfill_daily_winner_profiles(
                    start_date=_today_text(),
                    end_date=_today_text(),
                    days_back=1,
                    threshold_pct=EVIDENCE_BIG_MOVER_THRESHOLD_PCT,
                    limit_per_day=EVIDENCE_AUTO_BACKFILL_SYMBOL_LIMIT,
                    store_bars=bool(EVIDENCE_AUTO_BACKFILL_STORE_BARS),
                )
                _mark_daily_winner_backfill(backfill)
            # GitHub sync follows the user-defined Riyadh schedule: Tue-Sat 05:45 by default, never Sunday/Monday.
            if _daily_auto_sync_due(session):
                sync = run_evidence_auto_sync(force=False, dry_run=False, include_csv=None)
                _mark_daily_auto_sync(sync)
            time.sleep(60)
        except Exception as exc:
            try:
                set_json("evidence_worker_last_error", {"at": _now_text(), "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
            except Exception:
                pass
            time.sleep(120)


def start_evidence_background_worker() -> dict:
    global _WORKER_STARTED, _WORKER_THREAD
    if not EVIDENCE_BACKGROUND_WORKER_ENABLED:
        return {"ok": True, "started": False, "enabled": False, "reason": "background_worker_disabled"}
    if _WORKER_STARTED and _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"ok": True, "started": True, "already_running": True}
    try:
        init_evidence_db()
        claimed, lease = _claim_worker_lease()
        if not claimed:
            return {"ok": True, "started": False, "enabled": True, "reason": "another_worker_lease_active", "lease": lease}
        _WORKER_THREAD = threading.Thread(target=_worker_loop, name="evidence-collector-worker", daemon=True)
        _WORKER_THREAD.start()
        _WORKER_STARTED = True
        return {"ok": True, "started": True, "enabled": True, "lease": lease}
    except Exception as exc:
        return {"ok": False, "started": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

