"""Polygon Weekly Candidate Builder V1.

Reads temporary Polygon flat-file CSV/ZIP data, extracts compact weekly candidates,
and stores only the compact result.  Raw files are not meant to stay in Railway,
SQLite, or GitHub.
"""
from __future__ import annotations

import csv
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .settings import DATA_DIR
from .data_store import get_manual_sharia_exclusions_map, get_manual_sharia_approvals_map
from .utils import safe_round, to_float

POLYGON_WEEKLY_BUILDER_VERSION = "polygon_weekly_builder_v1_daily_minute_temp_safe_2026_06_05"
DEFAULT_OUTPUT_PATH = Path(DATA_DIR) / "polygon_weekly_priority_watchlist.json"


def _clean_symbol(v) -> str:
    s = str(v or "").upper().strip()
    if not s:
        return ""
    if not all(ch.isalnum() or ch in {".", "-"} for ch in s):
        return ""
    return s


def _iter_csv_rows(path: Path) -> Iterable[dict]:
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield row
    except Exception:
        return


def _iter_zip_csv_rows(path: Path) -> Iterable[tuple[str, dict]]:
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if not name.lower().endswith(".csv"):
                    continue
                with z.open(name) as raw:
                    text = (line.decode("utf-8", "ignore") for line in raw)
                    reader = csv.DictReader(text)
                    for row in reader:
                        yield name, row
    except Exception:
        return


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


def _classify_file_name(name: str) -> str:
    n = str(name or "").lower()
    if "minute" in n or "1min" in n or "1_min" in n or "min" in n:
        return "minute"
    if "day" in n or "daily" in n:
        return "daily"
    return "unknown"


def _update_daily(stats: dict, sym: str, row: dict) -> None:
    o = _price(row, "open", "o")
    h = _price(row, "high", "h")
    l = _price(row, "low", "l")
    c = _price(row, "close", "c")
    v = _price(row, "volume", "v")
    if c <= 0:
        return
    s = stats.setdefault(sym, {"symbol": sym, "days": 0, "minute_rows": 0, "first_close": 0, "last_close": 0, "week_high": 0, "week_low": 0, "volume": 0, "green_days": 0, "intraday_reclaim_days": 0, "max_intraday_rise_pct": 0, "quiet_accumulation_score": 0})
    if s["first_close"] <= 0:
        s["first_close"] = c
    s["last_close"] = c
    s["week_high"] = max(float(s.get("week_high", 0) or 0), h or c)
    lo0 = float(s.get("week_low", 0) or 0)
    s["week_low"] = min(lo0 if lo0 > 0 else (l or c), l or c)
    s["volume"] = float(s.get("volume", 0) or 0) + v
    s["days"] = int(s.get("days", 0) or 0) + 1
    if c > o > 0:
        s["green_days"] = int(s.get("green_days", 0) or 0) + 1
    if o > 0 and l > 0 and c > o and l < o * 0.985:
        s["intraday_reclaim_days"] = int(s.get("intraday_reclaim_days", 0) or 0) + 1


def _update_minute(stats: dict, sym: str, row: dict) -> None:
    o = _price(row, "open", "o")
    h = _price(row, "high", "h")
    l = _price(row, "low", "l")
    c = _price(row, "close", "c")
    v = _price(row, "volume", "v")
    if c <= 0:
        return
    s = stats.setdefault(sym, {"symbol": sym, "days": 0, "minute_rows": 0, "first_close": 0, "last_close": 0, "week_high": 0, "week_low": 0, "volume": 0, "green_days": 0, "intraday_reclaim_days": 0, "max_intraday_rise_pct": 0, "quiet_accumulation_score": 0})
    s["minute_rows"] = int(s.get("minute_rows", 0) or 0) + 1
    s["volume"] = float(s.get("volume", 0) or 0) + v
    s["week_high"] = max(float(s.get("week_high", 0) or 0), h or c)
    lo0 = float(s.get("week_low", 0) or 0)
    s["week_low"] = min(lo0 if lo0 > 0 else (l or c), l or c)
    if o > 0 and h > 0:
        rise = ((h - o) / o) * 100
        s["max_intraday_rise_pct"] = max(float(s.get("max_intraday_rise_pct", 0) or 0), rise)
    if o > 0 and l > 0 and c > o and l < o * 0.99:
        s["intraday_reclaim_days"] = int(s.get("intraday_reclaim_days", 0) or 0) + 1


def _score_candidate(s: dict) -> tuple[float, list[str], str]:
    first = float(s.get("first_close", 0) or 0)
    last = float(s.get("last_close", 0) or 0)
    high = float(s.get("week_high", 0) or 0)
    low = float(s.get("week_low", 0) or 0)
    volume = float(s.get("volume", 0) or 0)
    days = int(s.get("days", 0) or 0)
    reclaim = int(s.get("intraday_reclaim_days", 0) or 0)
    max_rise = float(s.get("max_intraday_rise_pct", 0) or 0)
    week_return = ((last - first) / first) * 100 if first > 0 and last > 0 else 0.0
    range_pct = ((high - low) / high) * 100 if high > 0 and low > 0 else 0.0
    reasons: list[str] = []
    score = 0.0
    if -2 <= week_return <= 8:
        score += 22; reasons.append("حركة أسبوعية هادئة قابلة للمتابعة")
    elif 8 < week_return <= 18:
        score += 14; reasons.append("زخم أسبوعي جيد لكن يحتاج منع مطاردة")
    elif week_return > 18:
        score -= 10; reasons.append("تحرك كثيرًا — استمرار/Pullback فقط")
    if reclaim:
        score += min(24, reclaim * 8); reasons.append("تكرر نمط نزول ثم استعادة")
    if 5 <= max_rise <= 18:
        score += 18; reasons.append("أظهر اندفاعًا داخليًا يمكن مراقبته")
    if volume >= 1_000_000:
        score += 14; reasons.append("سيولة أسبوعية كافية")
    if days >= 3:
        score += 8; reasons.append("بيانات عدة جلسات")
    if 4 <= range_pct <= 22:
        score += 10; reasons.append("مدى حركة مناسب بدون انفجار مفرط")
    stage = "Weekly Priority"
    if week_return > 15 or max_rise > 20:
        stage = "Continuation/Pullback Watch"
    elif reclaim or (0 <= week_return <= 8 and max_rise >= 5):
        stage = "Quiet Accumulation / Early Movement"
    return max(0.0, min(100.0, round(score, 2))), reasons[:8], stage


def build_weekly_candidates_from_path(path: str, top_n: int = 15, execute: bool = False) -> dict:
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return {"ok": False, "error": "input_not_found", "path": str(p)}
    stats: dict[str, dict] = {}
    files_seen = 0
    rows_seen = 0
    if p.is_dir():
        paths = list(p.glob("**/*.csv"))
        for fp in paths:
            kind = _classify_file_name(fp.name)
            files_seen += 1
            for row in _iter_csv_rows(fp):
                sym = _row_symbol(row)
                if not sym:
                    continue
                rows_seen += 1
                (_update_minute if kind == "minute" else _update_daily)(stats, sym, row)
    elif p.suffix.lower() == ".zip":
        for name, row in _iter_zip_csv_rows(p):
            kind = _classify_file_name(name)
            files_seen += 1
            sym = _row_symbol(row)
            if not sym:
                continue
            rows_seen += 1
            (_update_minute if kind == "minute" else _update_daily)(stats, sym, row)
    elif p.suffix.lower() == ".csv":
        kind = _classify_file_name(p.name)
        files_seen = 1
        for row in _iter_csv_rows(p):
            sym = _row_symbol(row)
            if not sym:
                continue
            rows_seen += 1
            (_update_minute if kind == "minute" else _update_daily)(stats, sym, row)

    exclusions = get_manual_sharia_exclusions_map() or {}
    approvals = get_manual_sharia_approvals_map() or {}
    candidates = []
    excluded = 0
    for sym, s in stats.items():
        if exclusions.get(sym) and not approvals.get(sym):
            excluded += 1
            continue
        score, reasons, stage = _score_candidate(s)
        if score < 35:
            continue
        first = float(s.get("first_close", 0) or 0)
        last = float(s.get("last_close", 0) or 0)
        high = float(s.get("week_high", 0) or 0)
        low = float(s.get("week_low", 0) or 0)
        week_return = ((last - first) / first) * 100 if first > 0 and last > 0 else 0.0
        candidates.append({
            "symbol": sym,
            "score": score,
            "stage": stage,
            "reasons": reasons,
            "week_return_pct": safe_round(week_return, 2),
            "week_high": safe_round(high, 4),
            "week_low": safe_round(low, 4),
            "last_close": safe_round(last, 4),
            "suggested_watch_zone_low": safe_round(max(low, last * 0.96), 4) if last > 0 else 0,
            "suggested_watch_zone_high": safe_round(last * 1.015, 4) if last > 0 else 0,
            "invalidation": safe_round(max(0.01, low * 0.985), 4) if low > 0 else 0,
            "first_target": safe_round(min(high * 1.02, last * 1.08), 4) if last > 0 and high > 0 else 0,
            "raw_stats": {k: v for k, v in s.items() if k != "symbol"},
        })
    candidates.sort(key=lambda x: float(x.get("score", 0)), reverse=True)
    result = {
        "ok": True,
        "version": POLYGON_WEEKLY_BUILDER_VERSION,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "input_path": str(p),
        "files_seen": files_seen,
        "rows_seen": rows_seen,
        "symbols_seen": len(stats),
        "manual_sharia_excluded": excluded,
        "top_n": int(top_n),
        "candidates": candidates[:max(1, min(50, int(top_n or 15)))],
        "note": "الملفات الخام تستخدم مؤقتًا فقط. احفظ الناتج المختصر فقط ولا ترفع ملفات الدقيقة الخام إلى GitHub/SQLite.",
    }
    if execute:
        DEFAULT_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["saved_to"] = str(DEFAULT_OUTPUT_PATH)
    return result


def load_weekly_watchlist() -> dict:
    if DEFAULT_OUTPUT_PATH.exists():
        try:
            return json.loads(DEFAULT_OUTPUT_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"read_error: {type(exc).__name__}: {str(exc)[:120]}"}
    return {"ok": True, "version": POLYGON_WEEKLY_BUILDER_VERSION, "available": False, "candidates": []}
