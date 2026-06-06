"""Polygon Weekly Candidate Builder V2.

Reads local temporary Polygon CSV/CSV.GZ/ZIP data or pulls Massive/Polygon Flat
Files into /tmp, extracts compact Weekly Priority candidates, and stores only the
compact result.  Raw minute/day files must never be saved to Railway volume,
GitHub, or SQLite.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Any

from .settings import DATA_DIR
from .data_store import get_manual_sharia_exclusions_map, get_manual_sharia_approvals_map
from .utils import safe_round, to_float
from .polygon_flatfile_fetcher import (
    POLYGON_FLATFILE_FETCHER_VERSION,
    cleanup_tmp_path,
    flatfiles_config_status,
    pull_flatfiles_for_window,
    mark_flatfile_processed,
)

POLYGON_WEEKLY_BUILDER_VERSION = "polygon_weekly_builder_v2a_direct_pull_state_fix_2026_06_06"
DEFAULT_OUTPUT_PATH = Path(DATA_DIR) / "polygon_weekly_priority_watchlist.json"
_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


def _max_minute_files() -> int:
    # Direct pulls are intended to be incremental.  Keep local/manual rebuilds Railway-safe by default.
    # Set POLYGON_WEEKLY_BUILDER_MAX_MINUTE_FILES=14 only for a deliberate weekend rebuild.
    return max(1, min(14, _env_int("POLYGON_WEEKLY_BUILDER_MAX_MINUTE_FILES", 3)))


def _clean_symbol(v) -> str:
    s = str(v or "").upper().strip()
    if not s:
        return ""
    if not all(ch.isalnum() or ch in {".", "-"} for ch in s):
        return ""
    return s


def _row_symbol(row: dict) -> str:
    for k in ["ticker", "symbol", "T", "sym"]:
        s = _clean_symbol(row.get(k))
        if s:
            return s
    return ""


def _price(row: dict, *keys: str) -> float:
    for k in keys:
        v = to_float(row.get(k))
        if v > 0:
            return float(v)
    return 0.0


def _source_date(name: str) -> str:
    m = _DATE_RE.search(str(name or ""))
    return m.group(1) if m else ""


def _classify_file_name(name: str, fallback: str = "unknown") -> str:
    n = str(name or "").lower()
    if "minute" in n or "1min" in n or "1_min" in n or "/minute_" in n or "minute_aggs" in n:
        return "minute"
    if "day_aggs" in n or "daily" in n or "/daily_" in n or " day" in n or "days" in n:
        return "daily"
    f = str(fallback or "").lower().strip()
    if f in {"minute", "daily"}:
        return f
    return "unknown"


def _open_text_from_path(path: Path):
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore", newline="")
    return path.open("r", encoding="utf-8", errors="ignore", newline="")


def _iter_standalone_sources(path: Path, fallback_kind: str) -> Iterator[tuple[str, str, Iterable[dict]]]:
    kind = _classify_file_name(path.name, fallback_kind)
    try:
        text = _open_text_from_path(path)
        reader = csv.DictReader(text)
        yield path.name, kind, reader
        text.close()
    except Exception:
        return


def _iter_zip_sources(path: Path, fallback_kind: str) -> Iterator[tuple[str, str, Iterable[dict]]]:
    try:
        with zipfile.ZipFile(path) as z:
            infos = [info for info in z.infolist() if info.filename.lower().endswith((".csv", ".csv.gz"))]
            if str(fallback_kind or "").lower() == "minute":
                infos = sorted(infos, key=lambda x: _source_date(x.filename) or x.filename)[-_max_minute_files():]
            else:
                infos = sorted(infos, key=lambda x: _source_date(x.filename) or x.filename)
            for info in infos:
                name = info.filename
                low = name.lower()
                kind = _classify_file_name(name, fallback_kind)
                raw = z.open(info)
                if low.endswith(".gz"):
                    gz = gzip.GzipFile(fileobj=raw)
                    text = io.TextIOWrapper(gz, encoding="utf-8", errors="ignore", newline="")
                else:
                    text = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore", newline="")
                try:
                    reader = csv.DictReader(text)
                    yield name, kind, reader
                finally:
                    try:
                        text.close()
                    except Exception:
                        try:
                            raw.close()
                        except Exception:
                            pass
    except Exception:
        return


def _iter_sources(path: str | Path, fallback_kind: str = "unknown") -> Iterator[tuple[str, str, Iterable[dict]]]:
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return
    if p.is_dir():
        for fp in sorted(p.glob("**/*")):
            if fp.is_file() and (fp.name.lower().endswith(".csv") or fp.name.lower().endswith(".csv.gz") or fp.name.lower().endswith(".zip")):
                yield from _iter_sources(fp, fallback_kind)
    elif p.suffix.lower() == ".zip":
        yield from _iter_zip_sources(p, fallback_kind)
    elif p.name.lower().endswith(".csv") or p.name.lower().endswith(".csv.gz"):
        yield from _iter_standalone_sources(p, fallback_kind)



_COMPANY_SYMBOL_CACHE: set[str] | None = None


def _company_symbol_set() -> set[str]:
    global _COMPANY_SYMBOL_CACHE
    if _COMPANY_SYMBOL_CACHE is not None:
        return _COMPANY_SYMBOL_CACHE
    symbols: set[str] = set()
    for path in [Path(__file__).resolve().parent.parent / "data" / "companies.csv", Path.cwd() / "data" / "companies.csv"]:
        try:
            if path.exists():
                with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
                    for line in f:
                        sym = _clean_symbol(str(line).split(";", 1)[0])
                        if sym:
                            symbols.add(sym)
                break
        except Exception:
            continue
    _COMPANY_SYMBOL_CACHE = symbols
    return symbols


def _is_common_stock_symbol(sym: str, company_symbols: set[str]) -> bool:
    s = _clean_symbol(sym)
    if not s:
        return False
    if s in company_symbols:
        return True
    # Avoid most ETFs, warrants, rights, units, preferred/classes absent from company universe.
    # Keep a conservative fallback for ordinary 1-5 letter symbols if company file is unavailable.
    if not company_symbols and s.isalpha() and 1 <= len(s) <= 5:
        return True
    return False



def _iter_text_sources(path: str | Path, fallback_kind: str = "unknown") -> Iterator[tuple[str, str, Any]]:
    """Yield raw text handles for faster csv.reader processing of large minute files."""
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return
    if p.is_dir():
        files = sorted([fp for fp in p.glob("**/*") if fp.is_file() and fp.name.lower().endswith((".csv", ".csv.gz", ".zip"))], key=lambda x: _source_date(x.name) or x.name)
        if str(fallback_kind or "").lower() == "minute":
            files = files[-_max_minute_files():]
        for fp in files:
            yield from _iter_text_sources(fp, fallback_kind)
    elif p.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(p) as z:
                infos = [info for info in z.infolist() if info.filename.lower().endswith((".csv", ".csv.gz"))]
                if str(fallback_kind or "").lower() == "minute":
                    infos = sorted(infos, key=lambda x: _source_date(x.filename) or x.filename)[-_max_minute_files():]
                else:
                    infos = sorted(infos, key=lambda x: _source_date(x.filename) or x.filename)
                for info in infos:
                    name = info.filename
                    low = name.lower()
                    kind = _classify_file_name(name, fallback_kind)
                    raw = z.open(info)
                    if low.endswith(".gz"):
                        gz = gzip.GzipFile(fileobj=raw)
                        text = io.TextIOWrapper(gz, encoding="utf-8", errors="ignore", newline="")
                    else:
                        text = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore", newline="")
                    try:
                        yield name, kind, text
                    finally:
                        try:
                            text.close()
                        except Exception:
                            try:
                                raw.close()
                            except Exception:
                                pass
        except Exception:
            return
    elif p.name.lower().endswith((".csv", ".csv.gz")):
        kind = _classify_file_name(p.name, fallback_kind)
        try:
            text = _open_text_from_path(p)
            try:
                yield p.name, kind, text
            finally:
                text.close()
        except Exception:
            return


def _new_stats(sym: str) -> dict[str, Any]:
    return {
        "symbol": sym,
        "daily": [],
        "minute_days": [],
        "total_volume": 0.0,
        "minute_rows": 0,
    }


def _stats_for(stats: dict[str, dict], sym: str) -> dict:
    return stats.setdefault(sym, _new_stats(sym))


def _update_daily(stats: dict, source_name: str, row: dict) -> None:
    sym = _row_symbol(row)
    if not sym:
        return
    o = _price(row, "open", "o")
    h = _price(row, "high", "h")
    l = _price(row, "low", "l")
    c = _price(row, "close", "c")
    v = _price(row, "volume", "v")
    tx = _price(row, "transactions", "n")
    if c <= 0 or h <= 0 or l <= 0:
        return
    s = _stats_for(stats, sym)
    day = _source_date(source_name)
    s["daily"].append({
        "date": day,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "transactions": tx,
        "day_return_pct": ((c - o) / o) * 100 if o > 0 else 0.0,
        "close_location": ((c - l) / (h - l)) if h > l else 0.5,
        "dollar_volume": c * v,
    })
    s["total_volume"] = float(s.get("total_volume", 0) or 0) + v




def _float_cell(row: list[str], idx: int) -> float:
    try:
        if idx < 0 or idx >= len(row):
            return 0.0
        v = row[idx]
        if not v:
            return 0.0
        return float(v)
    except Exception:
        return 0.0


def _process_minute_text_source(stats: dict, source_name: str, text, allowed_symbols: set[str] | None = None) -> tuple[int, int]:
    day = _source_date(source_name)
    reader = csv.reader(text)
    try:
        header = next(reader)
    except Exception:
        return 0, 0
    idx = {str(k).strip().lower(): i for i, k in enumerate(header)}
    t_i = idx.get("ticker", idx.get("symbol", idx.get("t", -1)))
    v_i = idx.get("volume", idx.get("v", -1))
    o_i = idx.get("open", idx.get("o", -1))
    c_i = idx.get("close", idx.get("c", -1))
    h_i = idx.get("high", idx.get("h", -1))
    l_i = idx.get("low", idx.get("l", -1))
    tx_i = idx.get("transactions", idx.get("n", -1))
    if t_i < 0 or c_i < 0:
        return 0, 0
    allowed = allowed_symbols or set()
    use_allowed = bool(allowed)
    day_state: dict[str, dict] = {}
    rows_seen = 0
    symbols_seen: set[str] = set()
    for row in reader:
        try:
            sym = str(row[t_i] or "").upper().strip()
        except Exception:
            continue
        if not sym or (use_allowed and sym not in allowed):
            continue
        c = _float_cell(row, c_i)
        if c <= 0:
            continue
        o = _float_cell(row, o_i) or c
        h = _float_cell(row, h_i) or c
        l = _float_cell(row, l_i) or c
        v = _float_cell(row, v_i)
        tx = _float_cell(row, tx_i)
        rows_seen += 1
        symbols_seen.add(sym)
        ds = day_state.setdefault(sym, {
            "date": day,
            "open": o,
            "first_close": c,
            "high": h,
            "low": l,
            "close": c,
            "volume": 0.0,
            "transactions": 0.0,
            "rows": 0,
            "first_60_volume": 0.0,
            "first_30_volume": 0.0,
            "first_60_close": c,
            "first_30_close": c,
            "last_60_volume": 0.0,
            "last_60_close": c,
            "last_60_queue": [],
        })
        ds["high"] = max(float(ds.get("high", h) or h), h)
        ds["low"] = min(float(ds.get("low", l) or l), l)
        ds["close"] = c
        ds["volume"] = float(ds.get("volume", 0) or 0) + v
        ds["transactions"] = float(ds.get("transactions", 0) or 0) + tx
        ds["rows"] = int(ds.get("rows", 0) or 0) + 1
        n = int(ds["rows"])
        if n <= 30:
            ds["first_30_volume"] = float(ds.get("first_30_volume", 0) or 0) + v
            ds["first_30_close"] = c
        if n <= 60:
            ds["first_60_volume"] = float(ds.get("first_60_volume", 0) or 0) + v
            ds["first_60_close"] = c
        q = ds.setdefault("last_60_queue", [])
        q.append((v, c))
        if len(q) > 60:
            q.pop(0)
    for sym, ds in day_state.items():
        q = ds.get("last_60_queue") or []
        if q:
            ds["last_60_volume"] = sum(float(x[0] or 0) for x in q)
            ds["last_60_close"] = float(q[-1][1] or ds.get("close") or 0)
        ds.pop("last_60_queue", None)
        o = float(ds.get("open", 0) or 0)
        h = float(ds.get("high", 0) or 0)
        l = float(ds.get("low", 0) or 0)
        c = float(ds.get("close", 0) or 0)
        v = float(ds.get("volume", 0) or 0)
        first60c = float(ds.get("first_60_close", 0) or 0)
        ds["day_return_pct"] = ((c - o) / o) * 100 if o > 0 else 0.0
        ds["max_rise_from_open_pct"] = ((h - o) / o) * 100 if o > 0 else 0.0
        ds["max_drop_from_open_pct"] = ((l - o) / o) * 100 if o > 0 else 0.0
        ds["close_location"] = ((c - l) / (h - l)) if h > l else 0.5
        ds["first_60_gain_pct"] = ((first60c - o) / o) * 100 if o > 0 and first60c > 0 else 0.0
        ds["early_volume_ratio"] = (float(ds.get("first_60_volume", 0) or 0) / v) if v > 0 else 0.0
        ds["reclaim_flag"] = bool(o > 0 and l < o * 0.985 and c > o and float(ds.get("close_location", 0) or 0) >= 0.62)
        s = _stats_for(stats, sym)
        s["minute_days"].append(ds)
        s["minute_rows"] = int(s.get("minute_rows", 0) or 0) + int(ds.get("rows", 0) or 0)
        s["total_volume"] = float(s.get("total_volume", 0) or 0) + v
    return rows_seen, len(symbols_seen)


def _process_minute_source(stats: dict, source_name: str, rows: Iterable[dict]) -> tuple[int, int]:
    day = _source_date(source_name)
    day_state: dict[str, dict] = {}
    rows_seen = 0
    symbols_seen: set[str] = set()
    for row in rows:
        sym = _row_symbol(row)
        if not sym:
            continue
        c = _price(row, "close", "c")
        if c <= 0:
            continue
        o = _price(row, "open", "o") or c
        h = _price(row, "high", "h") or c
        l = _price(row, "low", "l") or c
        v = _price(row, "volume", "v")
        tx = _price(row, "transactions", "n")
        rows_seen += 1
        symbols_seen.add(sym)
        ds = day_state.setdefault(sym, {
            "date": day,
            "open": o,
            "first_close": c,
            "high": h,
            "low": l,
            "close": c,
            "volume": 0.0,
            "transactions": 0.0,
            "rows": 0,
            "first_60_volume": 0.0,
            "first_30_volume": 0.0,
            "first_60_close": c,
            "first_30_close": c,
            "last_60_volume": 0.0,
            "last_60_close": c,
            "last_60_queue": [],
        })
        ds["high"] = max(float(ds.get("high", h) or h), h)
        ds["low"] = min(float(ds.get("low", l) or l), l)
        ds["close"] = c
        ds["volume"] = float(ds.get("volume", 0) or 0) + v
        ds["transactions"] = float(ds.get("transactions", 0) or 0) + tx
        ds["rows"] = int(ds.get("rows", 0) or 0) + 1
        n = int(ds["rows"])
        if n <= 30:
            ds["first_30_volume"] = float(ds.get("first_30_volume", 0) or 0) + v
            ds["first_30_close"] = c
        if n <= 60:
            ds["first_60_volume"] = float(ds.get("first_60_volume", 0) or 0) + v
            ds["first_60_close"] = c
        q = ds.setdefault("last_60_queue", [])
        q.append((v, c))
        if len(q) > 60:
            q.pop(0)
    for sym, ds in day_state.items():
        q = ds.get("last_60_queue") or []
        if q:
            ds["last_60_volume"] = sum(float(x[0] or 0) for x in q)
            ds["last_60_close"] = float(q[-1][1] or ds.get("close") or 0)
        ds.pop("last_60_queue", None)
        o = float(ds.get("open", 0) or 0)
        h = float(ds.get("high", 0) or 0)
        l = float(ds.get("low", 0) or 0)
        c = float(ds.get("close", 0) or 0)
        v = float(ds.get("volume", 0) or 0)
        first60c = float(ds.get("first_60_close", 0) or 0)
        ds["day_return_pct"] = ((c - o) / o) * 100 if o > 0 else 0.0
        ds["max_rise_from_open_pct"] = ((h - o) / o) * 100 if o > 0 else 0.0
        ds["max_drop_from_open_pct"] = ((l - o) / o) * 100 if o > 0 else 0.0
        ds["close_location"] = ((c - l) / (h - l)) if h > l else 0.5
        ds["first_60_gain_pct"] = ((first60c - o) / o) * 100 if o > 0 and first60c > 0 else 0.0
        ds["early_volume_ratio"] = (float(ds.get("first_60_volume", 0) or 0) / v) if v > 0 else 0.0
        ds["reclaim_flag"] = bool(o > 0 and l < o * 0.985 and c > o and float(ds.get("close_location", 0) or 0) >= 0.62)
        s = _stats_for(stats, sym)
        s["minute_days"].append(ds)
        s["minute_rows"] = int(s.get("minute_rows", 0) or 0) + int(ds.get("rows", 0) or 0)
        s["total_volume"] = float(s.get("total_volume", 0) or 0) + v
    return rows_seen, len(symbols_seen)


def _sort_records(stats: dict[str, dict]) -> None:
    for s in stats.values():
        s["daily"] = sorted((s.get("daily") or []), key=lambda x: str(x.get("date") or ""))[-40:]
        s["minute_days"] = sorted((s.get("minute_days") or []), key=lambda x: str(x.get("date") or ""))[-20:]


def _avg(values: list[float]) -> float:
    vals = [float(x) for x in values if isinstance(x, (int, float)) and x == x]
    return sum(vals) / len(vals) if vals else 0.0


def _pct(a: float, b: float) -> float:
    return ((a - b) / b) * 100 if b and b > 0 and a > 0 else 0.0


def _compact_metrics(s: dict, market_last: dict[str, float]) -> dict[str, Any]:
    daily = list(s.get("daily") or [])
    minute_days = list(s.get("minute_days") or [])
    closes = [float(d.get("close", 0) or 0) for d in daily]
    vols = [float(d.get("volume", 0) or 0) for d in daily]
    dollar_vols = [float(d.get("dollar_volume", 0) or 0) for d in daily]
    last = daily[-1] if daily else (minute_days[-1] if minute_days else {})
    last_close = float(last.get("close", 0) or 0)
    first_close = closes[0] if closes else 0.0
    close_5d_ago = closes[-6] if len(closes) >= 6 else (closes[0] if closes else 0.0)
    high_20 = max([float(d.get("high", 0) or 0) for d in daily] or [0.0])
    low_20 = min([float(d.get("low", 0) or 0) for d in daily if float(d.get("low", 0) or 0) > 0] or [0.0])
    avg_vol_20 = _avg(vols[-20:])
    avg_dollar_vol_20 = _avg(dollar_vols[-20:])
    avg_vol_prev = _avg(vols[-21:-1]) if len(vols) > 1 else avg_vol_20
    last_vol = float(last.get("volume", 0) or 0)
    last_ret = float(last.get("day_return_pct", 0) or 0)
    last_close_loc = float(last.get("close_location", 0.5) or 0.5)
    minute_last = minute_days[-1] if minute_days else {}
    max_intraday_rise = max([float(x.get("max_rise_from_open_pct", 0) or 0) for x in minute_days] or [0.0])
    reclaim_days = sum(1 for x in minute_days if x.get("reclaim_flag"))
    close_power_days = sum(1 for x in minute_days[-5:] if float(x.get("close_location", 0) or 0) >= 0.70)
    early_vol_ratio_avg = _avg([float(x.get("early_volume_ratio", 0) or 0) for x in minute_days[-5:]])
    red_to_green_days = sum(1 for d in daily[-10:] if float(d.get("low", 0) or 0) < float(d.get("open", 0) or 0) * 0.985 and float(d.get("close", 0) or 0) > float(d.get("open", 0) or 0))
    green_days_10 = sum(1 for d in daily[-10:] if float(d.get("close", 0) or 0) > float(d.get("open", 0) or 0))
    last_date = str(last.get("date") or minute_last.get("date") or "")
    market_stress = bool(market_last and (market_last.get("SPY", 0) <= -1.0 or market_last.get("QQQ", 0) <= -1.5 or market_last.get("IWM", 0) <= -1.5))
    resisted_stress = bool(market_stress and (last_ret >= 1.0 or last_close_loc >= 0.85))
    return {
        "symbol": s.get("symbol"),
        "last_date": last_date,
        "last_close": last_close,
        "daily_days": len(daily),
        "minute_days": len(minute_days),
        "minute_rows": int(s.get("minute_rows", 0) or 0),
        "return_5d_pct": _pct(last_close, close_5d_ago),
        "return_20d_pct": _pct(last_close, first_close),
        "last_day_return_pct": last_ret,
        "last_day_close_location": last_close_loc,
        "last_volume": last_vol,
        "avg_volume_20d": avg_vol_20,
        "volume_ratio_last_vs_prev20": (last_vol / avg_vol_prev) if avg_vol_prev > 0 else 0.0,
        "avg_dollar_volume_20d": avg_dollar_vol_20,
        "high_20d": high_20,
        "low_20d": low_20,
        "distance_from_20d_high_pct": ((last_close - high_20) / high_20) * 100 if high_20 > 0 and last_close > 0 else 0.0,
        "range_20d_pct": ((high_20 - low_20) / high_20) * 100 if high_20 > 0 and low_20 > 0 else 0.0,
        "max_intraday_rise_pct": max_intraday_rise,
        "reclaim_days": reclaim_days,
        "red_to_green_days_10d": red_to_green_days,
        "green_days_10d": green_days_10,
        "close_power_days_5d": close_power_days,
        "early_volume_ratio_5d": early_vol_ratio_avg,
        "market_stress_last_day": market_stress,
        "stress_resilience": resisted_stress,
        "minute_last": {k: safe_round(v, 4) if isinstance(v, (int, float)) else v for k, v in minute_last.items() if k in {"date", "open", "high", "low", "close", "volume", "day_return_pct", "max_rise_from_open_pct", "close_location", "early_volume_ratio", "reclaim_flag"}},
    }


def _score_metrics(m: dict[str, Any]) -> tuple[float, list[str], str, bool]:
    reasons: list[str] = []
    score = 0.0
    ret5 = float(m.get("return_5d_pct", 0) or 0)
    ret20 = float(m.get("return_20d_pct", 0) or 0)
    last_ret = float(m.get("last_day_return_pct", 0) or 0)
    close_loc = float(m.get("last_day_close_location", 0) or 0)
    vol_ratio = float(m.get("volume_ratio_last_vs_prev20", 0) or 0)
    dollar_vol = float(m.get("avg_dollar_volume_20d", 0) or 0)
    avg_vol = float(m.get("avg_volume_20d", 0) or 0)
    range20 = float(m.get("range_20d_pct", 0) or 0)
    dist_high = float(m.get("distance_from_20d_high_pct", 0) or 0)
    max_rise = float(m.get("max_intraday_rise_pct", 0) or 0)
    reclaim = int(m.get("reclaim_days", 0) or 0)
    red_green = int(m.get("red_to_green_days_10d", 0) or 0)
    close_power = int(m.get("close_power_days_5d", 0) or 0)
    early_vol = float(m.get("early_volume_ratio_5d", 0) or 0)
    stress = bool(m.get("stress_resilience"))

    if avg_vol >= 500_000 or dollar_vol >= 10_000_000:
        score += 8; reasons.append("سيولة يومية قابلة للتحليل")
    if dollar_vol >= 25_000_000:
        score += 6; reasons.append("دولار فوليوم قوي")
    if -3 <= ret5 <= 9:
        score += 12; reasons.append("حركة 5 أيام غير ممتدة")
    elif 9 < ret5 <= 18:
        score += 6; reasons.append("زخم 5 أيام جيد لكن يحتاج منع مطاردة")
    elif ret5 > 18:
        score -= 20; reasons.append("تحرك 5 أيام كبير — استمرار/تراجع فقط")
    if 2 <= ret5 <= 12:
        score += 8; reasons.append("زخم أسبوعي إيجابي بدون امتداد كبير")
    if -8 <= ret20 <= 18:
        score += 8; reasons.append("اتجاه 20 يوم قابل للمتابعة بدون انفجار مفرط")
    elif ret20 > 35:
        score -= 12; reasons.append("امتداد 20 يوم مرتفع")
    if last_ret >= 2.0:
        score += 8; reasons.append("قوة يومية واضحة في آخر جلسة")
    if last_ret >= 5.0:
        score += 5; reasons.append("اندفاع آخر جلسة قوي — يحتاج تأكيد لا مطاردة")
    if close_loc >= 0.78:
        score += 10; reasons.append("إغلاق قريب من قمة اليوم")
    elif close_loc <= 0.35:
        score -= 9; reasons.append("الإغلاق داخل اليوم ضعيف")
    if 1.15 <= vol_ratio <= 3.5:
        score += 8; reasons.append("السيولة تحسنت بدون جنون")
    elif 0.85 <= vol_ratio < 1.15:
        score += 3; reasons.append("السيولة مستقرة")
    elif vol_ratio > 5:
        score -= 7; reasons.append("قفزة سيولة حادة تحتاج تأكيد استمرارية")
    if reclaim:
        score += min(16, reclaim * 5); reasons.append("تكرر نمط dip ثم reclaim")
    if red_green:
        score += min(10, red_green * 3); reasons.append("تكرر شراء الهبوط خلال آخر جلسات")
    if close_power:
        score += min(10, close_power * 3); reasons.append("قوة إغلاق متكررة")
    if 0.18 <= early_vol <= 0.55:
        score += 6; reasons.append("دخول سيولة مبكر ومتوازن")
    if -8 <= dist_high <= -0.5:
        score += 6; reasons.append("قريب من قمة 20 يوم مع مساحة اختراق")
    elif dist_high > -0.5 and ret5 > 8:
        score -= 5; reasons.append("قريب جدًا من القمة بعد صعود")
    if 5 <= range20 <= 28:
        score += 5; reasons.append("مدى 20 يوم مناسب لبناء خطة")
    if stress:
        score += 10; reasons.append("صمد في يوم ضغط/نزول عام")
    if last_ret > 8:
        score -= 10; reasons.append("قفزة يومية كبيرة — لا مطاردة")
    if max_rise > 24:
        score -= 12; reasons.append("اندفاع داخلي مفرط — يحتاج تراجع/إعادة تمركز")

    no_chase = bool(last_ret > 10 or ret5 > 22 or max_rise > 30)
    if no_chase:
        stage = "Pullback Required / No-Chase Review"
    elif ret5 > 12 or max_rise > 18:
        stage = "Continuation Watch"
    elif reclaim or red_green or stress or close_power >= 2:
        stage = "Weekly Priority"
    else:
        stage = "Early/Quiet Accumulation Watch"
    return max(0.0, round(score, 2)), reasons[:10], stage, no_chase


def _market_last_day_returns(stats: dict[str, dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym in ("SPY", "QQQ", "IWM"):
        s = stats.get(sym) or {}
        daily = list(s.get("daily") or [])
        if daily:
            out[sym] = float(daily[-1].get("day_return_pct", 0) or 0)
    return out


def _build_from_paths_internal(minute_paths: list[str], daily_paths: list[str], top_n: int, execute: bool, input_label: str) -> dict:
    stats: dict[str, dict] = {}
    files_seen = 0
    rows_seen = 0
    source_errors: list[str] = []

    for path in daily_paths:
        for source_name, kind, rows in _iter_sources(path, "daily"):
            files_seen += 1
            try:
                for row in rows:
                    rows_seen += 1
                    _update_daily(stats, source_name, row)
            except Exception as exc:
                source_errors.append(f"daily:{source_name}:{type(exc).__name__}:{str(exc)[:80]}")

    _sort_records(stats)
    market_last = _market_last_day_returns(stats)
    exclusions = get_manual_sharia_exclusions_map() or {}
    approvals = get_manual_sharia_approvals_map() or {}
    company_symbols = _company_symbol_set()

    # Preselect a daily-based pool before parsing huge minute flat files.
    # We still stream the minute files, but we only build expensive minute-day
    # structures for names that have enough daily quality to be candidates.
    prelim: list[tuple[float, str]] = []
    for sym, s in stats.items():
        if sym in {"SPY", "QQQ", "IWM", "DIA", "VIXY"}:
            continue
        if not _is_common_stock_symbol(sym, company_symbols):
            continue
        if exclusions.get(sym) and not approvals.get(sym):
            continue
        m0 = _compact_metrics(s, market_last)
        sc0, _, _, _ = _score_metrics(m0)
        if sc0 >= 25:
            prelim.append((float(sc0), sym))
    prelim.sort(reverse=True)
    allowed_minute_symbols = {sym for _, sym in prelim[:2500]}

    for path in minute_paths:
        for source_name, kind, text in _iter_text_sources(path, "minute"):
            files_seen += 1
            try:
                n, _ = _process_minute_text_source(stats, source_name, text, allowed_minute_symbols)
                rows_seen += n
            except Exception as exc:
                source_errors.append(f"minute:{source_name}:{type(exc).__name__}:{str(exc)[:80]}")

    _sort_records(stats)
    market_last = _market_last_day_returns(stats)
    candidates = []
    rejected_count = 0
    excluded = 0
    for sym, s in stats.items():
        if sym in {"SPY", "QQQ", "IWM", "DIA", "VIXY"}:
            continue
        if not _is_common_stock_symbol(sym, company_symbols):
            rejected_count += 1
            continue
        if exclusions.get(sym) and not approvals.get(sym):
            excluded += 1
            continue
        metrics = _compact_metrics(s, market_last)
        if int(metrics.get("daily_days", 0) or 0) < 4 and int(metrics.get("minute_days", 0) or 0) < 3:
            rejected_count += 1
            continue
        score, reasons, stage, no_chase = _score_metrics(metrics)
        if score < 38:
            rejected_count += 1
            continue
        last = float(metrics.get("last_close", 0) or 0)
        low20 = float(metrics.get("low_20d", 0) or 0)
        high20 = float(metrics.get("high_20d", 0) or 0)
        support_low = max(low20, last * 0.955) if last > 0 and low20 > 0 else 0
        support_high = last * 0.99 if last > 0 else 0
        target = min(high20 * 1.025, last * 1.10) if high20 > 0 and last > 0 else 0
        item = {
            "symbol": sym,
            "score": max(0.0, min(100.0, safe_round(score, 2))),
            "rank_score": safe_round(score, 2),
            "stage": stage,
            "pattern": stage,
            "weekly_priority": stage in {"Weekly Priority", "Early/Quiet Accumulation Watch"},
            "is_buy_signal": False,
            "requires_live_confirmation": True,
            "no_chase": no_chase,
            "reasons": reasons,
            "last_close": safe_round(last, 4),
            "last_date": metrics.get("last_date"),
            "return_5d_pct": safe_round(metrics.get("return_5d_pct"), 2),
            "return_20d_pct": safe_round(metrics.get("return_20d_pct"), 2),
            "last_day_return_pct": safe_round(metrics.get("last_day_return_pct"), 2),
            "avg_dollar_volume_20d": safe_round(metrics.get("avg_dollar_volume_20d"), 0),
            "volume_ratio_last_vs_prev20": safe_round(metrics.get("volume_ratio_last_vs_prev20"), 2),
            "distance_from_20d_high_pct": safe_round(metrics.get("distance_from_20d_high_pct"), 2),
            "suggested_watch_zone_low": safe_round(support_low, 4),
            "suggested_watch_zone_high": safe_round(support_high, 4),
            "invalidation": safe_round(max(0.01, support_low * 0.985), 4) if support_low > 0 else 0,
            "first_target": safe_round(target, 4),
            "sharia_manual_status": "approved" if approvals.get(sym) else "unresolved",
            "compact_metrics": {k: v for k, v in metrics.items() if k not in {"minute_last"}},
            "minute_last": metrics.get("minute_last"),
        }
        candidates.append(item)
    candidates.sort(key=lambda x: (0 if x.get("no_chase") else 1, float(x.get("rank_score", x.get("score", 0))), float(x.get("last_day_return_pct", 0) or 0)), reverse=True)
    result = {
        "ok": True,
        "version": POLYGON_WEEKLY_BUILDER_VERSION,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "input_label": input_label,
        "files_seen": files_seen,
        "rows_seen": rows_seen,
        "symbols_seen": len(stats),
        "manual_sharia_excluded": excluded,
        "rejected_count": rejected_count,
        "market_last_day_returns": {k: safe_round(v, 2) for k, v in market_last.items()},
        "top_n": int(top_n),
        "candidates": candidates[:max(1, min(50, int(top_n or 15)))],
        "source_errors": source_errors[:20],
        "rule_ar": "هذه قائمة Polygon Weekly Priority مستقلة وليست إشارة شراء. لا تتحول إلى Cautious/Strong إلا بعد السعر الحي والسيولة والدعم/المقاومة وFinal Decision.",
        "storage_rule_ar": "تم تحليل ملفات Polygon كمدخل مؤقت فقط. الناتج المختصر فقط هو الذي يُحفظ؛ لا حفظ للملفات الخام في Railway/GitHub/SQLite.",
    }
    if execute:
        DEFAULT_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["saved_to"] = str(DEFAULT_OUTPUT_PATH)
    return result


def build_weekly_candidates_from_paths(
    minute_path: str | None = None,
    daily_path: str | None = None,
    *,
    minute_paths: list[str] | None = None,
    daily_paths: list[str] | None = None,
    top_n: int = 15,
    execute: bool = False,
) -> dict:
    mp = list(minute_paths or [])
    dp = list(daily_paths or [])
    if minute_path:
        mp.append(str(minute_path))
    if daily_path:
        dp.append(str(daily_path))
    if not mp and not dp:
        return {"ok": False, "error": "no_input_paths", "version": POLYGON_WEEKLY_BUILDER_VERSION}
    if len(mp) > _max_minute_files():
        mp = sorted(mp, key=lambda x: _source_date(str(x)) or str(x))[-_max_minute_files():]
    return _build_from_paths_internal(mp, dp, top_n, execute, input_label="local_paths_minute_daily")


def build_weekly_candidates_from_path(path: str, top_n: int = 15, execute: bool = False) -> dict:
    """Backward-compatible single-path builder.

    If the path name is ambiguous, it is classified from file/folder name, and if
    still unknown it is treated as daily for safety rather than pretending minute
    data exists.  Prefer build_weekly_candidates_from_paths(minute_path, daily_path).
    """
    p = Path(str(path or "")).expanduser()
    label = str(p).lower()
    if "minute" in label or "minutes" in label or "minute_aggs" in label:
        return build_weekly_candidates_from_paths(minute_path=str(path), top_n=top_n, execute=execute)
    if "day" in label or "daily" in label or "days" in label or "day_aggs" in label:
        return build_weekly_candidates_from_paths(daily_path=str(path), top_n=top_n, execute=execute)
    return build_weekly_candidates_from_paths(daily_path=str(path), top_n=top_n, execute=execute)


def build_weekly_candidates_from_polygon(
    *,
    trade_date: str | None = None,
    minute_days: int = 3,
    daily_days: int = 25,
    top_n: int = 15,
    execute: bool = False,
    force: bool = False,
) -> dict:
    """Pull Massive/Polygon flat files into /tmp, build candidates, delete raw files."""
    pull = pull_flatfiles_for_window(end_date=trade_date, minute_days=minute_days, daily_days=daily_days, force=force)
    tmp_dir = pull.get("tmp_dir")
    recovered_from_stale_processed_state = False
    try:
        if not pull.get("ok"):
            results0 = list(pull.get("results") or [])
            all_processed = bool(results0) and all(str((r or {}).get("status") or "") == "already_processed" for r in results0)
            # If a previous dry-run/download marked dates as processed but no compact watchlist exists,
            # there are no raw files to reuse. Recover once by forcing a fresh /tmp pull.
            if all_processed and not DEFAULT_OUTPUT_PATH.exists() and not force:
                cleanup_tmp_path(tmp_dir)
                recovered_from_stale_processed_state = True
                pull = pull_flatfiles_for_window(end_date=trade_date, minute_days=minute_days, daily_days=daily_days, force=True)
                tmp_dir = pull.get("tmp_dir")
            elif all_processed and DEFAULT_OUTPUT_PATH.exists():
                saved = load_weekly_watchlist()
                saved["ok"] = bool(saved.get("ok", True))
                saved["from_existing_watchlist"] = True
                saved["note_ar"] = "كل التواريخ معالجة سابقًا، لذلك تم إرجاع قائمة Weekly Priority المحفوظة بدل إعادة تحميل الملفات الخام."
                saved["fetcher_version"] = POLYGON_FLATFILE_FETCHER_VERSION
                saved["polygon_pull"] = {"download_results": [{k: v for k, v in (r or {}).items() if k != "path"} for r in results0], "raw_files_downloaded": False}
                return saved
        if not pull.get("ok"):
            return {
                "ok": False,
                "version": POLYGON_WEEKLY_BUILDER_VERSION,
                "fetcher_version": POLYGON_FLATFILE_FETCHER_VERSION,
                "error": pull.get("error") or "no_flatfiles_downloaded",
                "pull": {k: v for k, v in pull.items() if k != "tmp_dir"},
                "config": flatfiles_config_status(),
                "recovered_from_stale_processed_state": recovered_from_stale_processed_state,
            }
        result = _build_from_paths_internal(
            minute_paths=list(pull.get("minute_paths") or []),
            daily_paths=list(pull.get("daily_paths") or []),
            top_n=top_n,
            execute=execute,
            input_label="polygon_flatfiles_direct_pull_tmp_only",
        )
        if execute and result.get("ok"):
            for r in list(pull.get("results") or []):
                if (r or {}).get("ok") and (r or {}).get("s3_key") and (r or {}).get("trade_date") and (r or {}).get("dataset"):
                    mark_flatfile_processed(str(r.get("dataset")), str(r.get("trade_date")), str(r.get("s3_key")))
        result["fetcher_version"] = POLYGON_FLATFILE_FETCHER_VERSION
        result["recovered_from_stale_processed_state"] = recovered_from_stale_processed_state
        result["polygon_pull"] = {
            "minute_dates": pull.get("minute_dates"),
            "daily_dates": pull.get("daily_dates"),
            "download_results": [{k: v for k, v in (r or {}).items() if k != "path"} for r in list(pull.get("results") or [])],
            "raw_files_deleted_after_analysis": True,
            "config": pull.get("config"),
        }
        return result
    finally:
        cleanup_tmp_path(tmp_dir)


def polygon_flatfile_status() -> dict:
    return {
        "ok": True,
        "builder_version": POLYGON_WEEKLY_BUILDER_VERSION,
        "fetcher": flatfiles_config_status(),
        "watchlist_available": DEFAULT_OUTPUT_PATH.exists(),
        "storage_rule_ar": "السحب المباشر يستخدم /tmp فقط، ثم يحذف الخام ويحفظ ملخص Weekly Priority فقط عند execute=true.",
    }


def load_weekly_watchlist() -> dict:
    if DEFAULT_OUTPUT_PATH.exists():
        try:
            return json.loads(DEFAULT_OUTPUT_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"read_error: {type(exc).__name__}: {str(exc)[:120]}"}
    return {"ok": True, "version": POLYGON_WEEKLY_BUILDER_VERSION, "available": False, "candidates": []}
