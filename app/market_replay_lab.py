"""Market Replay Lab V1.

Compact Polygon minute replay for testing the Opportunity Radar before enabling
new logic on the live market.  It never stores raw Polygon rows in SQLite/GitHub;
it streams a local /tmp CSV/ZIP and returns compact candidate/event summaries.
"""
from __future__ import annotations

import csv
import gzip
import io
import math
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.opportunity_radar import OPPORTUNITY_RADAR_VERSION, enrich_row_opportunity_radar

MARKET_REPLAY_LAB_VERSION = "market_replay_lab_v1_small_classic_2026_06_19"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").replace("%", "").strip()
        n = float(v)
        if math.isnan(n) or math.isinf(n):
            return default
        return n
    except Exception:
        return default


def _round(v: Any, nd: int = 2) -> float:
    try:
        return round(_num(v), nd)
    except Exception:
        return 0.0


def _sym(row: dict) -> str:
    for k in ["ticker", "symbol", "T", "sym"]:
        x = _u(row.get(k))
        if x and all(ch.isalnum() or ch in {".", "-"} for ch in x):
            return x
    return ""


def _bar_time(row: dict) -> tuple[str, str]:
    raw = row.get("window_start") or row.get("timestamp") or row.get("t") or row.get("sip_timestamp") or ""
    try:
        n = int(float(raw))
        # Polygon flat files often use nanoseconds.  Be forgiving.
        if n > 10**17:
            sec = n / 1_000_000_000
        elif n > 10**14:
            sec = n / 1_000_000
        elif n > 10**11:
            sec = n / 1000
        else:
            sec = n
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        txt = _s(raw)
        if len(txt) >= 10 and txt[:4].isdigit():
            return txt[:10], txt[11:16] if len(txt) >= 16 else ""
    return "unknown", ""


def _price(row: dict, *keys: str) -> float:
    for k in keys:
        n = _num(row.get(k), 0.0)
        if n > 0:
            return n
    return 0.0


def _source_date(name: str) -> str:
    import re
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", str(name or ""))
    return m.group(1) if m else ""


def _iter_zip_sources(path: Path, max_files: int) -> Iterator[tuple[str, Iterable[dict]]]:
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if i.filename.lower().endswith((".csv", ".csv.gz"))]
        infos = sorted(infos, key=lambda i: _source_date(i.filename) or i.filename)[-max(1, int(max_files or 1)):]
        for info in infos:
            raw = z.open(info)
            low = info.filename.lower()
            if low.endswith(".gz"):
                gz = gzip.GzipFile(fileobj=raw)
                text = io.TextIOWrapper(gz, encoding="utf-8", errors="ignore", newline="")
            else:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore", newline="")
            try:
                yield info.filename, csv.DictReader(text)
            finally:
                try:
                    text.close()
                except Exception:
                    pass


def _iter_sources(path: str | Path, max_files: int = 5) -> Iterator[tuple[str, Iterable[dict]]]:
    p = Path(str(path or "")).expanduser()
    if not p.exists():
        return
    if p.is_dir():
        files = [x for x in p.glob("**/*") if x.is_file() and x.name.lower().endswith((".csv", ".csv.gz", ".zip"))]
        files = sorted(files, key=lambda x: _source_date(x.name) or x.name)[-max(1, int(max_files or 1)):]
        for fp in files:
            yield from _iter_sources(fp, max_files=max_files)
    elif p.suffix.lower() == ".zip":
        yield from _iter_zip_sources(p, max_files=max_files)
    elif p.name.lower().endswith(".csv.gz"):
        f = gzip.open(p, "rt", encoding="utf-8", errors="ignore", newline="")
        try:
            yield p.name, csv.DictReader(f)
        finally:
            f.close()
    elif p.name.lower().endswith(".csv"):
        f = p.open("r", encoding="utf-8", errors="ignore", newline="")
        try:
            yield p.name, csv.DictReader(f)
        finally:
            f.close()


def _safe_path(path: str) -> tuple[bool, str]:
    if not path:
        return False, "path_missing"
    try:
        p = Path(path).expanduser().resolve()
        allowed_roots = [Path("/tmp").resolve(), Path("/mnt/data").resolve()]
        # Railway use should be /tmp.  /mnt/data is allowed only for local ChatGPT packaging tests.
        if not any(str(p).startswith(str(root)) for root in allowed_roots):
            return False, "path_not_allowed_use_tmp"
        if not p.exists():
            return False, "path_not_found"
        return True, str(p)
    except Exception as exc:
        return False, f"bad_path:{type(exc).__name__}"


def market_replay_lab_status() -> dict:
    return {
        "ok": True,
        "version": MARKET_REPLAY_LAB_VERSION,
        "opportunity_radar_version": OPPORTUNITY_RADAR_VERSION,
        "available_runs": ["/replay-lab/small-stock-classic/run?path=/tmp/your_polygon_minutes.zip"],
        "storage_rule_ar": "المحاكي يقرأ raw Polygon من /tmp فقط ويعيد نتائج مختصرة؛ لا يحفظ raw في SQLite/GitHub/Railway volume.",
        "small_stock_rules_ar": [
            "فريم 5د/15د عند المضاربة اللحظية.",
            "مستويات Fib 38.2/50/61.8/78.6 من آخر قاع إلى آخر قمة، مع تركيز على 61.8/78.6.",
            "VWAP: دخول فقط قربه أو بعد إغلاق شمعة فوقه.",
            "قمة اليوم السابق: منطقة تفعيل/شراء كلاسيكية بشرط إغلاق شمعة.",
            "لا تلحق الشمعة الخضراء؛ إذا ابتعد السعر ينتظر Pullback.",
        ],
    }


def run_small_stock_classic_replay_from_path(path: str, max_files: int = 5, max_rows: int = 250_000, max_candidates: int = 120) -> dict:
    ok, resolved = _safe_path(path)
    if not ok:
        return {"ok": False, "version": MARKET_REPLAY_LAB_VERSION, "error": resolved, "hint_ar": "ضع ملف Polygon minute zip مؤقتًا في /tmp ثم مرر path=/tmp/file.zip."}

    prev_day_high: dict[str, float] = {}
    day_state: dict[tuple[str, str], dict] = {}
    recent_vol: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
    events: dict[tuple[str, str], dict] = {}
    rows_seen = 0
    files_seen = []

    for source_name, reader in _iter_sources(resolved, max_files=max_files):
        files_seen.append(source_name)
        for raw in reader:
            rows_seen += 1
            if rows_seen > max_rows:
                break
            sym = _sym(raw)
            if not sym:
                continue
            date, hhmm = _bar_time(raw)
            o = _price(raw, "open", "o")
            h = _price(raw, "high", "h")
            l = _price(raw, "low", "l")
            c = _price(raw, "close", "c")
            v = _price(raw, "volume", "v")
            if c <= 0 or h <= 0 or l <= 0 or v <= 0:
                continue
            # Candidate price window for small-stock study.
            if not (0.75 <= c <= 25.0):
                continue

            key = (sym, date)
            st = day_state.get(key)
            if not st:
                st = {"open": o or c, "high": h, "low": l, "close": c, "volume": 0.0, "dollar": 0.0, "vwap_num": 0.0, "first_time": hhmm}
                day_state[key] = st
            st["high"] = max(_num(st.get("high"), h), h)
            st["low"] = min(_num(st.get("low"), l), l)
            st["close"] = c
            st["volume"] = _num(st.get("volume"), 0.0) + v
            typical = (h + l + c) / 3.0
            st["dollar"] = _num(st.get("dollar"), 0.0) + c * v
            st["vwap_num"] = _num(st.get("vwap_num"), 0.0) + typical * v
            vwap = st["vwap_num"] / st["volume"] if st["volume"] > 0 else 0.0
            avg_bar_vol = (sum(recent_vol[sym]) / len(recent_vol[sym])) if recent_vol[sym] else v
            rv = (v / avg_bar_vol) if avg_bar_vol > 0 else 1.0
            recent_vol[sym].append(v)
            change = ((c - st["open"]) / st["open"] * 100.0) if st.get("open") else 0.0

            row = {
                "symbol": sym,
                "display_price": c,
                "current_price_live": c,
                "display_change_pct": change,
                "day_low": st["low"],
                "day_high": st["high"],
                "session_low": st["low"],
                "session_high": st["high"],
                "vwap_proxy": vwap,
                "above_vwap_proxy": c >= vwap if vwap > 0 else False,
                "previous_day_high": prev_day_high.get(sym, 0.0),
                "effective_volume_ratio": rv,
                "volume": st["volume"],
                "dollar_volume": st["dollar"],
                "quality_score": 70,
                "execution_readiness_score": 55,
                "final_decision_code": "EARLY_WATCH",
                "decision": "مراقبة",
            }
            enriched = enrich_row_opportunity_radar(row, market_phase="replay")
            classic = enriched.get("small_stock_classic_setup") or {}
            event_key = (sym, date)
            if (classic.get("eligible") or enriched.get("opportunity_bucket") in {"small_stock_classic", "high_risk_day_trade", "low_float_premarket"}) and event_key not in events:
                events[event_key] = {
                    "symbol": sym,
                    "date": date,
                    "first_seen_time_utc": hhmm,
                    "first_seen_price": _round(c, 4),
                    "first_seen_change_pct": _round(change, 2),
                    "stage": enriched.get("opportunity_stage"),
                    "bucket": enriched.get("opportunity_bucket"),
                    "classic_state": classic.get("setup_state"),
                    "classic_score": classic.get("score"),
                    "vwap": classic.get("vwap"),
                    "previous_day_high": classic.get("previous_day_high"),
                    "fib_levels": classic.get("fib_levels"),
                    "behavior_tags": (classic.get("behavior_group") or {}).get("tags", []),
                    "reasons": classic.get("reasons", [])[:8],
                    "max_after_price": _round(c, 4),
                    "min_after_price": _round(c, 4),
                    "max_after_pct": 0.0,
                    "min_after_pct": 0.0,
                }
            if event_key in events:
                ev = events[event_key]
                ev["max_after_price"] = max(_num(ev.get("max_after_price"), c), c)
                ev["min_after_price"] = min(_num(ev.get("min_after_price"), c), c)
                base = _num(ev.get("first_seen_price"), c)
                ev["max_after_pct"] = _round(((ev["max_after_price"] - base) / base * 100.0) if base > 0 else 0.0, 2)
                ev["min_after_pct"] = _round(((ev["min_after_price"] - base) / base * 100.0) if base > 0 else 0.0, 2)
        # finalize prev highs after each file/date pass roughly from day_state
        for (sym, dt), st in list(day_state.items()):
            if _num(st.get("high"), 0.0) > 0:
                prev_day_high[sym] = _num(st.get("high"), 0.0)
        if rows_seen > max_rows:
            break

    candidates = sorted(events.values(), key=lambda x: (_num(x.get("classic_score"), 0.0), _num(x.get("max_after_pct"), 0.0)), reverse=True)[:max(1, int(max_candidates or 120))]
    grouped: dict[str, int] = defaultdict(int)
    for c in candidates:
        for tag in c.get("behavior_tags") or ["غير مصنف"]:
            grouped[tag] += 1
    return {
        "ok": True,
        "version": MARKET_REPLAY_LAB_VERSION,
        "opportunity_radar_version": OPPORTUNITY_RADAR_VERSION,
        "source_path": resolved,
        "files_seen": files_seen[:20],
        "rows_seen": rows_seen,
        "candidate_count": len(candidates),
        "behavior_groups": sorted([{"tag": k, "count": v} for k, v in grouped.items()], key=lambda x: x["count"], reverse=True)[:20],
        "candidates": candidates,
        "rule_ar": "هذه نتائج Replay مختصرة لا تخزن raw؛ تقيس متى ظهر السهم وأين ذهب بعد ظهوره.",
    }
