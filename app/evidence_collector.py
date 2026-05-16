"""Evidence Collection Layer V1 for Stock Radar AI.

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

from .github_sync import is_github_sync_configured, push_json_file, push_text_file
from .live_quotes import get_live_quotes
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
EVIDENCE_BIG_MOVERS_ENABLED = _env_bool("EVIDENCE_BIG_MOVERS_ENABLED", True)
EVIDENCE_POLYGON_ENABLED = _env_bool("EVIDENCE_POLYGON_ENABLED", True)
EVIDENCE_BIG_MOVER_THRESHOLD_PCT = _env_float("EVIDENCE_BIG_MOVER_THRESHOLD_PCT", 10.0)
EVIDENCE_MAX_TOOL_SYMBOLS = _env_int("EVIDENCE_MAX_TOOL_SYMBOLS", 220)
EVIDENCE_MAX_BIG_MOVERS = _env_int("EVIDENCE_MAX_BIG_MOVERS", 120)
EVIDENCE_MAX_SYMBOLS_PER_RUN = _env_int("EVIDENCE_MAX_SYMBOLS_PER_RUN", 260)
EVIDENCE_POLYGON_SYMBOL_LIMIT = _env_int("EVIDENCE_POLYGON_SYMBOL_LIMIT", 45)
EVIDENCE_INTERVAL_PREMARKET_SEC = _env_int("EVIDENCE_INTERVAL_PREMARKET_SEC", 600)
EVIDENCE_INTERVAL_OPEN_SEC = _env_int("EVIDENCE_INTERVAL_OPEN_SEC", 900)
EVIDENCE_INTERVAL_AFTERHOURS_SEC = _env_int("EVIDENCE_INTERVAL_AFTERHOURS_SEC", 1800)
EVIDENCE_INTERVAL_CLOSED_SEC = _env_int("EVIDENCE_INTERVAL_CLOSED_SEC", 21600)
EVIDENCE_GITHUB_ARCHIVE_PATH = str(os.getenv("GITHUB_EVIDENCE_ARCHIVE_PATH", "app_data/evidence_archive") or "app_data/evidence_archive").strip().strip("/")
EVIDENCE_FMP_BASE_URL = str(os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com") or "https://financialmodelingprep.com").rstrip("/")
EVIDENCE_HTTP_TIMEOUT_SEC = _env_float("EVIDENCE_HTTP_TIMEOUT_SEC", 9.0)


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


def _fetch_polygon_intraday_summary(symbol: str, trade_date: str | None = None) -> dict:
    """Small 5-minute candle summary. Used for evidence only, with strict caps."""
    sym = _clean_symbol(symbol)
    if not (EVIDENCE_POLYGON_ENABLED and POLYGON_API_KEY and sym):
        return {"ok": False, "enabled": bool(EVIDENCE_POLYGON_ENABLED), "configured": bool(POLYGON_API_KEY)}
    d = str(trade_date or _today_text())[:10]
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/5/minute/{d}/{d}"
        r = HTTP_SESSION.get(
            url,
            params={"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": POLYGON_API_KEY},
            timeout=float(EVIDENCE_HTTP_TIMEOUT_SEC),
        )
        if r.status_code >= 400:
            return {"ok": False, "symbol": sym, "status_code": r.status_code}
        payload = r.json() or {}
        bars = payload.get("results") or []
        if not bars:
            return {"ok": True, "symbol": sym, "trade_date": d, "bars": 0}
        first = bars[0] or {}
        last = bars[-1] or {}
        total_vol = sum(_safe_float(x.get("v"), 0) for x in bars if isinstance(x, dict))
        high = max([_safe_float(x.get("h"), 0) for x in bars if isinstance(x, dict)] or [0])
        low_vals = [_safe_float(x.get("l"), 0) for x in bars if isinstance(x, dict) and _safe_float(x.get("l"), 0) > 0]
        low = min(low_vals or [0])
        open_px = _safe_float(first.get("o"), 0)
        close_px = _safe_float(last.get("c"), 0)
        last_3 = bars[-3:] if len(bars) >= 3 else bars
        last_6 = bars[-6:] if len(bars) >= 6 else bars
        vol_15 = sum(_safe_float(x.get("v"), 0) for x in last_3 if isinstance(x, dict))
        vol_30 = sum(_safe_float(x.get("v"), 0) for x in last_6 if isinstance(x, dict))
        first_6 = bars[:6]
        first_30_high = max([_safe_float(x.get("h"), 0) for x in first_6 if isinstance(x, dict)] or [0])
        first_30_gain = ((first_30_high - open_px) / open_px * 100.0) if open_px > 0 and first_30_high > 0 else 0.0
        return {
            "ok": True,
            "symbol": sym,
            "trade_date": d,
            "bars": len(bars),
            "open": safe_round(open_px, 4),
            "last_close": safe_round(close_px, 4),
            "high": safe_round(high, 4),
            "low": safe_round(low, 4),
            "total_volume": safe_round(total_vol, 0),
            "last_15m_volume": safe_round(vol_15, 0),
            "last_30m_volume": safe_round(vol_30, 0),
            "first_30m_gain_pct": safe_round(first_30_gain, 2),
        }
    except Exception as exc:
        return {"ok": False, "symbol": sym, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


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
        "gap_from_prev_close_pct", "first_seen_change_pct", "no_chase_flag", "plan_needs_reconfirm", "liquidity_score",
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
            polygon_summaries[sym] = _fetch_polygon_intraday_summary(sym, trade_date=trade_date)

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
        lines = ["تقرير Daily Winner Pattern Mining V1", f"التاريخ: {d}", f"عدد الرابحين المحفوظين: {len(items)}", ""]
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
    result = {
        "ok": True,
        "version": "evidence_collection_v1_passive",
        "week_key": wk,
        "summary": dict(summary) if summary else {},
        "sessions": _rows_to_dicts(session_rows),
        "top_movers_observed": _rows_to_dicts(top_movers),
        "notes": {
            "safe_mode": "جمع أدلة فقط؛ لا يغير السكور أو التصنيف.",
            "next_weekend_use": "تحليل ما سبق الرابحين والخاسرين وتحديد الأنماط المتكررة.",
        },
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        s = result["summary"] or {}
        lines = [
            "تقرير Evidence Collection V1",
            f"الأسبوع: {wk}",
            f"اللقطات المحفوظة: {int(s.get('snapshots') or 0)}",
            f"الرموز الفريدة: {int(s.get('symbols') or 0)}",
            f"لقطات لأسهم رابحة يومية: {int(s.get('big_mover_snapshots') or 0)}",
            f"لقطات من داخل الأداة: {int(s.get('tool_snapshots') or 0)}",
            f"تحذيرات لا تطارد: {int(s.get('no_chase_count') or 0)}",
            f"خطط تحتاج إعادة تأكيد: {int(s.get('reconfirm_count') or 0)}",
            "",
            "أعلى الرموز المرصودة تغيرًا:",
        ]
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
    lim = max(1, min(int(limit or 10000), 50000))
    with _connect() as conn:
        snaps = conn.execute(f"SELECT * FROM evidence_snapshots {where_sql} ORDER BY captured_at DESC LIMIT ?", (*args, lim)).fetchall()
        movers = conn.execute("SELECT * FROM daily_big_movers WHERE trade_date=? ORDER BY change_pct DESC LIMIT ?", (d or _today_text(), 500)).fetchall()
        runs = conn.execute(f"SELECT * FROM evidence_runs {where_sql} ORDER BY started_at DESC LIMIT 100", tuple(args)).fetchall() if where_sql else conn.execute("SELECT * FROM evidence_runs ORDER BY started_at DESC LIMIT 100").fetchall()
    return {
        "ok": True,
        "version": "evidence_collection_v1_passive",
        "week_key": wk,
        "trade_date": d,
        "exported_at": _now_text(),
        "snapshots_count": len(snaps),
        "daily_big_movers_count": len(movers),
        "runs_count": len(runs),
        "snapshots": _rows_to_dicts(snaps),
        "daily_big_movers": _rows_to_dicts(movers),
        "runs": _rows_to_dicts(runs),
    }


def export_evidence_csv(week_key: str | None = None, trade_date: str | None = None, limit: int = 10000) -> str:
    data = export_evidence_json(week_key=week_key, trade_date=trade_date, limit=limit)
    rows = data.get("snapshots", []) if isinstance(data, dict) else []
    output = io.StringIO()
    fields = [
        "captured_at_text", "week_key", "trade_date", "session", "symbol", "source_group", "in_tool_snapshot", "in_big_movers",
        "signal_bucket", "decision", "sharia_status", "plan_family", "price", "change_pct", "volume", "dollar_volume",
        "entry_price", "target_price", "stop_loss", "support_price", "resistance_price", "distance_from_entry_pct",
        "distance_from_support_pct", "distance_from_resistance_pct", "gap_from_prev_close_pct", "no_chase_flag", "plan_needs_reconfirm",
        "liquidity_score", "momentum_acceleration_score", "pattern_risk_score", "quote_source", "price_source",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def sync_evidence_to_github(week_key: str | None = None, trade_date: str | None = None, include_csv: bool = True) -> dict:
    wk = str(week_key or _current_week_key() or "current")
    d = str(trade_date or _today_text())[:10]
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured"}
    data = export_evidence_json(week_key=wk, trade_date=d, limit=50000)
    base = f"{EVIDENCE_GITHUB_ARCHIVE_PATH}/{wk}"
    json_path = f"{base}/{d}_evidence.json"
    summary_path = f"{base}/{d}_summary.json"
    results = {
        "ok": False,
        "week_key": wk,
        "trade_date": d,
        "paths": {"json": json_path, "summary": summary_path},
        "json": push_json_file(json_path, data, message=f"Sync evidence data {wk} {d}"),
        "summary": push_json_file(summary_path, weekly_evidence_summary(week_key=wk, format="json"), message=f"Sync evidence summary {wk} {d}"),
    }
    if include_csv:
        csv_path = f"{base}/{d}_evidence.csv"
        csv_text = "\ufeff" + export_evidence_csv(week_key=wk, trade_date=d, limit=50000)
        results["paths"]["csv"] = csv_path
        results["csv"] = push_text_file(csv_path, csv_text, message=f"Sync evidence CSV {wk} {d}")
    results["ok"] = bool((results.get("json") or {}).get("ok") and (results.get("summary") or {}).get("ok"))
    try:
        set_json("evidence_last_github_sync", results)
    except Exception:
        pass
    return results


def _daily_auto_sync_due(session: str) -> bool:
    if not EVIDENCE_GITHUB_AUTO_SYNC_ENABLED or not is_github_sync_configured():
        return False
    now = _now_dt()
    # Sync once after the regular/after-hours evidence has had time to collect.
    if session not in {"after_hours", "closed"}:
        return False
    if session == "after_hours" and now.time() < dt_time(20, 25):
        return False
    key = f"evidence_github_synced_{_today_text()}"
    done = get_json(key, {})
    if isinstance(done, dict) and done.get("ok"):
        return False
    return True


def _mark_daily_auto_sync(result: dict) -> None:
    try:
        set_json(f"evidence_github_synced_{_today_text()}", result)
    except Exception:
        pass


def _worker_loop() -> None:
    last_collect_ts = 0.0
    while True:
        try:
            if not EVIDENCE_COLLECTION_ENABLED:
                time.sleep(600)
                continue
            session = _market_session()
            interval = _interval_for_session(session)
            now = _now_ts()
            # Only collect in actionable sessions. Weekend/closed checks only handle export.
            if session in {"pre_market", "regular", "after_hours"} and now - last_collect_ts >= interval:
                collect_evidence_snapshot(mode="background", include_big_movers=True, sync_to_github=False)
                last_collect_ts = now
            if _daily_auto_sync_due(session):
                sync = sync_evidence_to_github(week_key=_current_week_key(), trade_date=_today_text(), include_csv=True)
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
        _WORKER_THREAD = threading.Thread(target=_worker_loop, name="evidence-collector-worker", daemon=True)
        _WORKER_THREAD.start()
        _WORKER_STARTED = True
        return {"ok": True, "started": True, "enabled": True}
    except Exception as exc:
        return {"ok": False, "started": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
