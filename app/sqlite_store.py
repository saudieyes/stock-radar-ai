"""SQLite persistence layer for Stock Radar AI.

This module is intentionally small and conservative. Existing JSON files remain as
fallback/export files, while SQLite becomes the durable primary store for user data
when enabled. It is safe on Railway when APP_DATA_DIR points to a mounted volume
(or when /data exists).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .settings import DATA_DIR

SQLITE_ENABLED = str(os.getenv("USE_SQLITE_STORAGE", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
SQLITE_DB_PATH = str(os.getenv("SQLITE_DB_PATH", str(DATA_DIR / "stock_radar_ai.sqlite3")) or str(DATA_DIR / "stock_radar_ai.sqlite3"))

_LOCK = threading.RLock()
_INITIALIZED = False


def _connect() -> sqlite3.Connection:
    path = Path(SQLITE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
    except Exception:
        pass
    return conn


def init_db() -> None:
    global _INITIALIZED
    if not SQLITE_ENABLED:
        return
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kv_store (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS live_quotes (
                    symbol TEXT PRIMARY KEY,
                    price REAL NOT NULL DEFAULT 0,
                    previous_close REAL NOT NULL DEFAULT 0,
                    change_pct REAL NOT NULL DEFAULT 0,
                    volume REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    from_bucket TEXT NOT NULL DEFAULT '',
                    to_bucket TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        _INITIALIZED = True


def sqlite_status() -> dict:
    out = {
        "enabled": bool(SQLITE_ENABLED),
        "db_path": SQLITE_DB_PATH,
        "initialized": bool(_INITIALIZED),
        "ok": False,
        "error": "",
    }
    if not SQLITE_ENABLED:
        out["ok"] = True
        return out
    try:
        init_db()
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM kv_store").fetchone()
            out["kv_items"] = int(row["c"] if row else 0)
        out["ok"] = True
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return out


def get_json(key: str, default: Any = None) -> Any:
    if not SQLITE_ENABLED:
        return default
    try:
        init_db()
        with _connect() as conn:
            row = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        return json.loads(row["value"])
    except Exception:
        return default


def set_json(key: str, value: Any) -> bool:
    if not SQLITE_ENABLED:
        return False
    try:
        init_db()
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with _LOCK:
            with _connect() as conn:
                conn.execute(
                    """
                    INSERT INTO kv_store(key, value, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, payload, time.time()),
                )
                conn.commit()
        return True
    except Exception:
        return False


def upsert_live_quotes(rows: list[dict]) -> int:
    if not SQLITE_ENABLED or not rows:
        return 0
    init_db()
    count = 0
    now = time.time()
    with _LOCK:
        with _connect() as conn:
            for row in rows:
                symbol = str(row.get("symbol", "") or "").upper().strip()
                if not symbol:
                    continue
                conn.execute(
                    """
                    INSERT INTO live_quotes(symbol, price, previous_close, change_pct, volume, source, updated_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        price=excluded.price,
                        previous_close=excluded.previous_close,
                        change_pct=excluded.change_pct,
                        volume=excluded.volume,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                    """,
                    (
                        symbol,
                        float(row.get("price", 0) or 0),
                        float(row.get("previous_close", 0) or 0),
                        float(row.get("change_pct", 0) or 0),
                        float(row.get("volume", 0) or 0),
                        str(row.get("source", "") or ""),
                        float(row.get("updated_at", now) or now),
                    ),
                )
                count += 1
            conn.commit()
    return count


def get_cached_live_quotes(symbols: list[str] | None = None, max_age_sec: int = 120) -> dict[str, dict]:
    if not SQLITE_ENABLED:
        return {}
    try:
        init_db()
        cutoff = time.time() - max(1, int(max_age_sec or 120))
        out: dict[str, dict] = {}
        with _connect() as conn:
            if symbols:
                clean = [str(s or "").upper().strip() for s in symbols if str(s or "").strip()]
                if not clean:
                    return {}
                placeholders = ",".join(["?"] * len(clean))
                rows = conn.execute(f"SELECT * FROM live_quotes WHERE updated_at>=? AND symbol IN ({placeholders})", [cutoff, *clean]).fetchall()
            else:
                rows = conn.execute("SELECT * FROM live_quotes WHERE updated_at>=?", (cutoff,)).fetchall()
        for r in rows:
            out[str(r["symbol"])] = {
                "symbol": str(r["symbol"]),
                "price": float(r["price"] or 0),
                "previous_close": float(r["previous_close"] or 0),
                "change_pct": float(r["change_pct"] or 0),
                "volume": float(r["volume"] or 0),
                "source": str(r["source"] or ""),
                "updated_at": float(r["updated_at"] or 0),
            }
        return out
    except Exception:
        return {}


def record_signal_transition(symbol: str, from_bucket: str, to_bucket: str, reason: str = "") -> bool:
    if not SQLITE_ENABLED:
        return False
    try:
        init_db()
        with _connect() as conn:
            conn.execute(
                "INSERT INTO signal_transitions(symbol, from_bucket, to_bucket, reason, created_at) VALUES(?, ?, ?, ?, ?)",
                (str(symbol or "").upper().strip(), str(from_bucket or ""), str(to_bucket or ""), str(reason or "")[:500], time.time()),
            )
            conn.commit()
        return True
    except Exception:
        return False

