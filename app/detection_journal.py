"""Durable detection journal for Source / Early Discovery V2.

The journal records the first time the tool saw a symbol and the gain at that
moment. This prevents late movers from being relabelled as "early movement"
later in the same day/week.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.sqlite_store import SQLITE_DB_PATH
from app.move_stage_classifier import (
    MOVE_STAGE_VERSION,
    apply_move_stage_to_row,
    classify_move_stage,
    extract_change_pct,
    extract_price,
)

DETECTION_JOURNAL_VERSION = "source_early_discovery_v2_detection_journal_2026_05_25_hotfix2_peak_guard"
NY_TZ = ZoneInfo("America/New_York")
_LOCK = threading.RLock()
_INIT_DONE = False


def _env_bool(name: str, default: bool = True) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def detection_journal_enabled() -> bool:
    return _env_bool("DETECTION_JOURNAL_ENABLED", True) and _env_bool("SOURCE_EARLY_DISCOVERY_V2_ENABLED", True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
        return float(value)
    except Exception:
        return default


def _clean_symbol(value: Any) -> str:
    try:
        s = str(value or "").upper().strip()
        if not s:
            return ""
        if not all(ch.isalnum() or ch in {".", "-"} for ch in s):
            return ""
        return s
    except Exception:
        return ""


def _now() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _update_throttle_sec() -> int:
    try:
        return max(0, int(float(os.getenv("DETECTION_JOURNAL_UPDATE_THROTTLE_SEC", "180") or 180)))
    except Exception:
        return 180


def _age_seconds(ts: str) -> float:
    try:
        if not ts:
            return 999999.0
        dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY_TZ)
        return max(0.0, (datetime.now(NY_TZ) - dt).total_seconds())
    except Exception:
        return 999999.0


def _journal_current_gain_is_fresh(journal: dict[str, Any]) -> bool:
    """Return True when the journal current_gain is safe to reuse.

    During diagnostics/live-refresh, cached scan rows can carry a zero change
    even after a live pass recorded a non-zero current_gain in SQLite.  Reuse the
    journal value only when it was seen recently or on the same New York trading
    date; otherwise a prior-session spike could incorrectly cap tomorrow's row.
    """
    if not isinstance(journal, dict) or not journal:
        return False
    ts = str(journal.get("last_seen_time") or journal.get("updated_at") or "").strip()
    if not ts:
        return False
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY_TZ)
        now = datetime.now(NY_TZ)
        if dt.date() == now.date():
            return True
        return 0 <= (now - dt).total_seconds() <= 20 * 60 * 60
    except Exception:
        return _age_seconds(ts) <= 20 * 60 * 60


def _same_ny_day(ts: Any) -> bool:
    try:
        if not ts:
            return False
        dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY_TZ)
        return dt.date() == datetime.now(NY_TZ).date()
    except Exception:
        return False


def _late_stage_name(stage: Any) -> bool:
    return str(stage or "") in {"Continuation Watch", "Already Moved", "Extended", "Requires Pullback", "No-Chase", "Catalyst Spike Review"}


def _peak_from_journal(journal: dict[str, Any], fallback: float = 0.0) -> float:
    if not isinstance(journal, dict):
        return fallback
    return max(
        _safe_float(journal.get("peak_gain_seen"), fallback),
        _safe_float(journal.get("current_gain"), fallback),
        _safe_float(journal.get("gain_at_detection"), fallback),
        fallback,
    )


def _late_peak_is_fresh(journal: dict[str, Any]) -> bool:
    if not isinstance(journal, dict) or not journal:
        return False
    late_ts = journal.get("late_seen_time") or journal.get("peak_gain_time") or journal.get("last_seen_time") or journal.get("updated_at")
    if _same_ny_day(late_ts):
        return True
    return _age_seconds(str(late_ts or "")) <= 20 * 60 * 60


def _merge_journal_current_gain(stock: dict, journal: dict[str, Any], row_change_pct: float) -> None:
    """Overlay fresh journal current_gain when the row appears stale/zero.

    This is the hotfix for cases like IMAX in diagnostics: the journal had
    current_gain=15.47 and move_stage=No-Chase, while the sampled row still had
    current_gain/display_change_pct=0 and was reclassified as Pre-Move.
    """
    if not isinstance(stock, dict) or not isinstance(journal, dict) or not journal:
        return
    journal_gain = _safe_float(journal.get("current_gain"), row_change_pct)
    stock["journal_recorded_current_gain"] = journal_gain
    stock["journal_last_seen_time"] = journal.get("last_seen_time") or journal.get("updated_at")
    if abs(row_change_pct) < 0.05 and abs(journal_gain) >= 1.0 and _journal_current_gain_is_fresh(journal):
        # Only this fresh overlay field is consumed by extract_change_pct.
        # A stale prior-session journal value remains visible for diagnostics
        # as journal_recorded_current_gain, but it will not classify tomorrow's
        # stock as late unless a fresh scan confirms it again.
        stock["journal_current_gain"] = journal_gain
        stock["current_gain"] = journal_gain
        stock["journal_current_gain_applied"] = True


def _connect() -> sqlite3.Connection:
    path = Path(SQLITE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=12, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_detection_journal_db() -> dict[str, Any]:
    global _INIT_DONE
    if _INIT_DONE:
        return {"ok": True, "version": DETECTION_JOURNAL_VERSION, "already_initialized": True}
    with _LOCK:
        if _INIT_DONE:
            return {"ok": True, "version": DETECTION_JOURNAL_VERSION, "already_initialized": True}
        with _connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS detection_journal (
                    symbol TEXT PRIMARY KEY,
                    first_detected_time TEXT,
                    first_detected_price REAL,
                    gain_at_detection REAL,
                    first_source_reason TEXT,
                    first_source_layer TEXT,
                    first_watch_time TEXT,
                    first_cautious_time TEXT,
                    first_strong_time TEXT,
                    last_seen_time TEXT,
                    last_seen_price REAL,
                    current_gain REAL,
                    move_stage TEXT,
                    early_or_late_detection TEXT,
                    times_seen INTEGER DEFAULT 0,
                    source_tags_json TEXT,
                    peak_gain_seen REAL DEFAULT 0,
                    peak_gain_time TEXT,
                    late_seen_flag INTEGER DEFAULT 0,
                    late_seen_time TEXT,
                    updated_at TEXT
                )
                """
            )
            existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(detection_journal)").fetchall()}
            for col_name, col_sql in {
                "peak_gain_seen": "REAL DEFAULT 0",
                "peak_gain_time": "TEXT",
                "late_seen_flag": "INTEGER DEFAULT 0",
                "late_seen_time": "TEXT",
            }.items():
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE detection_journal ADD COLUMN {col_name} {col_sql}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_detection_journal_stage ON detection_journal(move_stage)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_detection_journal_updated ON detection_journal(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_detection_journal_late_seen ON detection_journal(late_seen_flag, late_seen_time)")
            conn.commit()
        _INIT_DONE = True
    return {"ok": True, "version": DETECTION_JOURNAL_VERSION, "db_path": str(SQLITE_DB_PATH)}


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return dict(row)
    except Exception:
        return {}


def get_detection(symbol: str) -> dict[str, Any]:
    sym = _clean_symbol(symbol)
    if not sym:
        return {}
    try:
        init_detection_journal_db()
        with _connect() as conn:
            row = conn.execute("SELECT * FROM detection_journal WHERE symbol=?", (sym,)).fetchone()
            return _row_to_dict(row)
    except Exception:
        return {}


def record_detection(
    symbol: str,
    price: float = 0.0,
    change_pct: float = 0.0,
    source_reason: str = "",
    source_layer: str = "scan_row",
    source_tags: list[str] | None = None,
    decision: str = "",
    move_stage: str = "",
    early_or_late_detection: str = "",
) -> dict[str, Any]:
    sym = _clean_symbol(symbol)
    if not sym or not detection_journal_enabled():
        return {}
    init_detection_journal_db()
    now = _now()
    price = _safe_float(price, 0.0)
    change_pct = _safe_float(change_pct, 0.0)
    decision_text = str(decision or "")
    first_watch_time = now
    first_cautious_time = now if decision_text == "دخول بحذر" else None
    first_strong_time = now if decision_text == "دخول قوي" else None
    try:
        with _LOCK:
            with _connect() as conn:
                existing = conn.execute("SELECT * FROM detection_journal WHERE symbol=?", (sym,)).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO detection_journal (
                            symbol, first_detected_time, first_detected_price, gain_at_detection,
                            first_source_reason, first_source_layer, first_watch_time, first_cautious_time,
                            first_strong_time, last_seen_time, last_seen_price, current_gain, move_stage,
                            early_or_late_detection, times_seen, source_tags_json, peak_gain_seen, peak_gain_time,
                            late_seen_flag, late_seen_time, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sym,
                            now,
                            price,
                            change_pct,
                            str(source_reason or "")[:300],
                            str(source_layer or "scan_row")[:80],
                            first_watch_time,
                            first_cautious_time,
                            first_strong_time,
                            now,
                            price,
                            change_pct,
                            str(move_stage or "")[:80],
                            str(early_or_late_detection or "")[:80],
                            __import__("json").dumps(source_tags or [], ensure_ascii=False)[:800],
                            change_pct,
                            now if change_pct > 0 else None,
                            1 if (change_pct >= 10 or _late_stage_name(move_stage)) else 0,
                            now if (change_pct >= 10 or _late_stage_name(move_stage)) else None,
                            now,
                        ),
                    )
                else:
                    existing_dict = _row_to_dict(existing)
                    prev_peak = _peak_from_journal(existing_dict, 0.0)
                    new_peak = max(prev_peak, change_pct)
                    incoming_late = change_pct >= 10 or _late_stage_name(move_stage)
                    existing_late_fresh = bool(_safe_float(existing_dict.get("late_seen_flag"), 0)) and _late_peak_is_fresh(existing_dict)
                    peak_crossed_late = new_peak >= 10 and (_same_ny_day(existing_dict.get("peak_gain_time")) or incoming_late)
                    needs_peak_update = new_peak > (prev_peak + 0.05) or incoming_late or peak_crossed_late
                    needs_transition_update = (decision_text == "دخول بحذر" and not existing["first_cautious_time"]) or (decision_text == "دخول قوي" and not existing["first_strong_time"])
                    recent_update = _age_seconds(existing["updated_at"] or existing["last_seen_time"] or "") < _update_throttle_sec()
                    if recent_update and not needs_transition_update and not needs_peak_update:
                        conn.commit()
                        row = conn.execute("SELECT * FROM detection_journal WHERE symbol=?", (sym,)).fetchone()
                        return _row_to_dict(row)

                    stored_stage = str(move_stage or existing["move_stage"] or "")[:80]
                    stored_early_late = str(early_or_late_detection or existing["early_or_late_detection"] or "")[:80]
                    if existing_late_fresh and not incoming_late and change_pct < 10:
                        # Do not let a stale/zero row downgrade a symbol that already
                        # moved strongly earlier in the same trading day.  It may calm
                        # down, but it is no longer a clean Pre-Move candidate today.
                        stored_stage = "Continuation Watch" if prev_peak < 20 else "No-Chase"
                        stored_early_late = "late_peak_seen"

                    updates = {
                        "last_seen_time": now,
                        "last_seen_price": price,
                        "current_gain": change_pct,
                        "move_stage": stored_stage,
                        "early_or_late_detection": stored_early_late,
                        "peak_gain_seen": new_peak,
                        "peak_gain_time": now if new_peak > prev_peak + 0.05 else (existing_dict.get("peak_gain_time") or now if new_peak > 0 else None),
                        "late_seen_flag": 1 if (existing_late_fresh or incoming_late or peak_crossed_late or new_peak >= 10) else int(_safe_float(existing_dict.get("late_seen_flag"), 0)),
                        "late_seen_time": now if (incoming_late or (new_peak >= 10 and new_peak > prev_peak + 0.05)) else existing_dict.get("late_seen_time"),
                        "updated_at": now,
                    }
                    if decision_text == "دخول بحذر" and not existing["first_cautious_time"]:
                        updates["first_cautious_time"] = now
                    if decision_text == "دخول قوي" and not existing["first_strong_time"]:
                        updates["first_strong_time"] = now
                    set_sql = ", ".join([f"{k}=?" for k in updates]) + ", times_seen=COALESCE(times_seen,0)+1"
                    conn.execute(f"UPDATE detection_journal SET {set_sql} WHERE symbol=?", tuple(updates.values()) + (sym,))
                conn.commit()
                row = conn.execute("SELECT * FROM detection_journal WHERE symbol=?", (sym,)).fetchone()
                return _row_to_dict(row)
    except Exception as exc:
        return {"symbol": sym, "journal_error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def enrich_stock_with_detection_journal(stock: dict, source_layer: str = "scan_row") -> dict:
    if not isinstance(stock, dict) or not detection_journal_enabled():
        return stock
    sym = _clean_symbol(stock.get("symbol"))
    if not sym:
        return stock
    price = extract_price(stock)
    change_pct = extract_change_pct(stock)
    source_reason = str(stock.get("source_reason") or stock.get("live_rank_reason") or stock.get("quick_explainer") or "")[:300]
    tags = []
    try:
        tags = [str(x) for x in (stock.get("source_reason_tags") or []) if str(x).strip()]
    except Exception:
        tags = []
    # Preliminary stage before the journal write. The write keeps first detection fixed.
    prelim = classify_move_stage(stock, journal={"gain_at_detection": change_pct})
    journal = record_detection(
        sym,
        price=price,
        change_pct=change_pct,
        source_reason=source_reason,
        source_layer=source_layer,
        source_tags=tags,
        decision=str(stock.get("decision", "") or ""),
        move_stage=str(prelim.get("move_stage", "") or ""),
        early_or_late_detection=str(prelim.get("early_or_late_detection", "") or ""),
    )
    if journal:
        stock["detection_journal"] = journal
        stock["first_detected_time"] = journal.get("first_detected_time")
        stock["first_detected_price"] = journal.get("first_detected_price")
        stock["gain_at_detection"] = _safe_float(journal.get("gain_at_detection"), change_pct)
        peak_gain = _peak_from_journal(journal, max(change_pct, _safe_float(stock.get("gain_at_detection"), 0.0)))
        stock["peak_gain_seen"] = peak_gain
        stock["intraday_peak_gain"] = peak_gain if _late_peak_is_fresh(journal) else _safe_float(stock.get("intraday_peak_gain"), 0.0)
        stock["peak_gain_time"] = journal.get("peak_gain_time")
        stock["late_seen_flag"] = bool(_safe_float(journal.get("late_seen_flag"), 0))
        stock["late_seen_time"] = journal.get("late_seen_time")
        _merge_journal_current_gain(stock, journal, change_pct)
        stock["first_source_reason"] = journal.get("first_source_reason")
        stock["first_source_layer"] = journal.get("first_source_layer")
        stock["first_watch_time"] = journal.get("first_watch_time")
        stock["first_cautious_time"] = journal.get("first_cautious_time")
        stock["first_strong_time"] = journal.get("first_strong_time")
        stock["detection_times_seen"] = journal.get("times_seen")
    apply_move_stage_to_row(stock, journal=journal)
    return stock


def enrich_rows_with_detection_journal(rows: list[dict], source_layer: str = "scan_rows") -> list[dict]:
    return [enrich_stock_with_detection_journal(dict(x), source_layer=source_layer) if isinstance(x, dict) else x for x in (rows or [])]


def detection_journal_status(limit: int = 20) -> dict[str, Any]:
    try:
        init_detection_journal_db()
        with _connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM detection_journal").fetchone()[0]
            late = conn.execute("SELECT COUNT(*) FROM detection_journal WHERE COALESCE(gain_at_detection,0) >= 10").fetchone()[0]
            late_seen = conn.execute("SELECT COUNT(*) FROM detection_journal WHERE COALESCE(late_seen_flag,0)=1").fetchone()[0]
            rows = conn.execute(
                "SELECT symbol, first_detected_time, first_detected_price, gain_at_detection, current_gain, peak_gain_seen, peak_gain_time, late_seen_flag, late_seen_time, move_stage, early_or_late_detection, times_seen FROM detection_journal ORDER BY updated_at DESC LIMIT ?",
                (int(max(1, min(limit, 100))),),
            ).fetchall()
        return {
            "ok": True,
            "version": DETECTION_JOURNAL_VERSION,
            "move_stage_version": MOVE_STAGE_VERSION,
            "enabled": detection_journal_enabled(),
            "total_symbols": int(total or 0),
            "late_at_detection_count": int(late or 0),
            "late_seen_count": int(late_seen or 0),
            "recent": [dict(r) for r in rows],
        }
    except Exception as exc:
        return {"ok": False, "version": DETECTION_JOURNAL_VERSION, "enabled": detection_journal_enabled(), "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
