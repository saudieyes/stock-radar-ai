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

MARKET_REPLAY_LAB_VERSION = "market_replay_lab_v1b_multiday_previous_session_2026_06_19"


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


def _source_kind(name: str) -> str:
    low = str(name or "").lower()
    if "day_" in low or "daily" in low or "/day" in low or "day_aggs" in low:
        return "daily"
    if "minute" in low or "min_" in low or "minute_aggs" in low:
        return "minute"
    return "unknown"


def _include_source(name: str, kind: str | None) -> bool:
    if not kind or kind == "all":
        return True
    k = _source_kind(name)
    if kind == "minute":
        # For ad-hoc CSVs with no daily/minute label, assume minute bars.
        return k in {"minute", "unknown"}
    if kind == "daily":
        return k == "daily"
    return True


def _phase_utc(hhmm: str) -> str:
    # US market times in UTC during daylight-saving months.  This is sufficient
    # for current Polygon replay diagnostics and intentionally conservative.
    t = str(hhmm or "")[:5]
    if not t:
        return "unknown"
    if t < "08:00":
        return "overnight"
    if t < "13:30":
        return "premarket"
    if t == "13:30":
        return "open"
    if t < "20:00":
        return "regular"
    return "after_hours"


def _phase_label_ar(phase: str) -> str:
    return {
        "overnight": "قبل البري ماركت / ليلي",
        "premarket": "قبل الافتتاح",
        "open": "لحظة الافتتاح",
        "regular": "أثناء السوق الرسمي",
        "after_hours": "بعد الإغلاق",
    }.get(str(phase or ""), "غير معروف")


def _chase_risk(max_gain_before: float, change_at_detection: float) -> tuple[str, str]:
    g = max(abs(_num(max_gain_before)), abs(_num(change_at_detection)))
    if g >= 15:
        return "very_late", "متأخر جدًا / مطاردة محتملة"
    if g >= 8:
        return "late", "متأخر"
    if g >= 5:
        return "watch_carefully", "مقبول بحذر"
    return "early", "مبكر"


def _iter_zip_sources(path: Path, max_files: int, kind: str | None = None) -> Iterator[tuple[str, Iterable[dict]]]:
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if i.filename.lower().endswith((".csv", ".csv.gz")) and _include_source(i.filename, kind)]
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


def _iter_sources(path: str | Path, max_files: int = 5, kind: str | None = None) -> Iterator[tuple[str, Iterable[dict]]]:
    p = Path(str(path or "")).expanduser()
    if not p.exists():
        return
    if p.is_dir():
        files = [x for x in p.glob("**/*") if x.is_file() and x.name.lower().endswith((".csv", ".csv.gz", ".zip")) and _include_source(x.name, kind)]
        files = sorted(files, key=lambda x: _source_date(x.name) or x.name)[-max(1, int(max_files or 1)):]
        for fp in files:
            yield from _iter_sources(fp, max_files=max_files, kind=kind)
    elif p.suffix.lower() == ".zip":
        yield from _iter_zip_sources(p, max_files=max_files, kind=kind)
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
        "available_runs": [
            "/replay-lab/small-stock-classic/run?path=/tmp/your_polygon_minutes.zip",
            "/replay-lab/small-stock-classic/pull-run?end_date=2026-06-18&minute_days=5&max_rows=250000",
        ],
        "storage_rule_ar": "المحاكي يقرأ raw Polygon من /tmp فقط ويعيد نتائج مختصرة؛ لا يحفظ raw في SQLite/GitHub/Railway volume. V2d يقرأ عدة أيام فعليًا ويستخدم daily context لقمة/إغلاق الجلسة السابقة مع تخطي الويكند/الإجازات.",
        "small_stock_rules_ar": [
            "فريم 5د/15د عند المضاربة اللحظية.",
            "مستويات Fib 38.2/50/61.8/78.6 من آخر قاع إلى آخر قمة، مع تركيز على 61.8/78.6.",
            "VWAP: دخول فقط قربه أو بعد إغلاق شمعة فوقه.",
            "قمة اليوم السابق: منطقة تفعيل/شراء كلاسيكية بشرط إغلاق شمعة.",
            "لا تلحق الشمعة الخضراء؛ إذا ابتعد السعر ينتظر Pullback.",
        ],
    }


def _load_daily_context(resolved: str, max_files: int = 20) -> tuple[dict[str, dict[str, dict[str, float]]], list[str]]:
    """Load compact daily context from daily flat files in the same /tmp path."""
    daily_by_date: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    files: list[str] = []
    try:
        for source_name, reader in _iter_sources(resolved, max_files=max_files, kind="daily"):
            files.append(source_name)
            file_date = _source_date(source_name)
            for raw in reader:
                sym = _sym(raw)
                if not sym:
                    continue
                dt, _ = _bar_time(raw)
                if dt == "unknown" and file_date:
                    dt = file_date
                if dt == "unknown":
                    continue
                o = _price(raw, "open", "o")
                h = _price(raw, "high", "h")
                l = _price(raw, "low", "l")
                c = _price(raw, "close", "c")
                v = _price(raw, "volume", "v")
                if h <= 0 or l <= 0 or c <= 0:
                    continue
                daily_by_date[dt][sym] = {"open": o or c, "high": h, "low": l, "close": c, "volume": v}
    except Exception:
        # Daily context is helpful, not required.  Replay must not fail because of it.
        pass
    return dict(daily_by_date), files


def _latest_prior_daily(daily_by_date: dict[str, dict[str, dict[str, float]]], date_s: str, sym: str) -> tuple[str, dict[str, float]]:
    for dt in sorted([d for d in daily_by_date.keys() if d < date_s], reverse=True):
        rec = (daily_by_date.get(dt) or {}).get(sym)
        if rec:
            return dt, rec
    return "", {}


def run_small_stock_classic_replay_from_path(path: str, max_files: int = 5, max_rows: int = 250_000, max_candidates: int = 120) -> dict:
    ok, resolved = _safe_path(path)
    if not ok:
        return {"ok": False, "version": MARKET_REPLAY_LAB_VERSION, "error": resolved, "hint_ar": "ضع ملف Polygon minute zip مؤقتًا في /tmp ثم مرر path=/tmp/file.zip."}

    # max_rows is now per file/day, not a global cap.  This prevents the replay
    # from silently stopping after the first large Polygon file.
    max_rows_per_file = max(10_000, min(500_000, int(max_rows or 250_000)))
    max_files_safe = max(1, min(10, int(max_files or 5)))

    daily_by_date, daily_files_seen = _load_daily_context(resolved, max_files=max_files_safe + 10)
    prev_day_high: dict[str, float] = {}
    prev_day_low: dict[str, float] = {}
    prev_day_close: dict[str, float] = {}
    prior_candidate_dates_by_symbol: dict[str, set[str]] = defaultdict(set)
    day_state: dict[tuple[str, str], dict] = {}
    recent_vol: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
    events: dict[tuple[str, str], dict] = {}
    rows_seen = 0
    rows_by_file: dict[str, int] = {}
    files_seen: list[str] = []
    dates_seen: set[str] = set()

    # Keep minute sources only.  Daily files are used for previous-day context.
    for source_name, reader in _iter_sources(resolved, max_files=max_files_safe, kind="minute"):
        files_seen.append(source_name)
        file_date = _source_date(source_name)
        file_rows = 0
        current_file_day_keys: set[tuple[str, str]] = set()
        for raw in reader:
            file_rows += 1
            if file_rows > max_rows_per_file:
                break
            rows_seen += 1
            sym = _sym(raw)
            if not sym:
                continue
            date, hhmm = _bar_time(raw)
            if date == "unknown" and file_date:
                date = file_date
            if date == "unknown":
                continue
            dates_seen.add(date)
            o = _price(raw, "open", "o")
            h = _price(raw, "high", "h")
            l = _price(raw, "low", "l")
            c = _price(raw, "close", "c")
            v = _price(raw, "volume", "v")
            if c <= 0 or h <= 0 or l <= 0 or v <= 0:
                continue
            if not (0.75 <= c <= 25.0):
                continue

            daily_dt, daily_rec = _latest_prior_daily(daily_by_date, date, sym)
            if daily_rec:
                p_high = _num(daily_rec.get("high"), 0.0)
                p_low = _num(daily_rec.get("low"), 0.0)
                p_close = _num(daily_rec.get("close"), 0.0)
                if p_high > 0:
                    prev_day_high[sym] = p_high
                if p_low > 0:
                    prev_day_low[sym] = p_low
                if p_close > 0:
                    prev_day_close[sym] = p_close

            key = (sym, date)
            current_file_day_keys.add(key)
            st = day_state.get(key)
            if not st:
                st = {"open": o or c, "high": h, "low": l, "close": c, "volume": 0.0, "dollar": 0.0, "vwap_num": 0.0, "first_time": hhmm}
                day_state[key] = st
            # Gain before detection candidate uses the high already seen before this bar.
            high_before = _num(st.get("high"), h)
            open_for_gain = _num(st.get("open"), o or c)
            max_gain_before = ((high_before - open_for_gain) / open_for_gain * 100.0) if open_for_gain > 0 else 0.0

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
            phase = _phase_utc(hhmm)
            chase_code, chase_label = _chase_risk(max_gain_before, change)
            pday_high = prev_day_high.get(sym, 0.0)
            pday_close = prev_day_close.get(sym, 0.0)
            prev_high_dist = ((c - pday_high) / pday_high * 100.0) if pday_high > 0 else 999.0
            prev_close_gap = ((c - pday_close) / pday_close * 100.0) if pday_close > 0 else 0.0
            was_prior_candidate = bool(prior_candidate_dates_by_symbol.get(sym))

            row = {
                "symbol": sym,
                "display_price": c,
                "current_price_live": c,
                "display_change_pct": change,
                "change_from_open_pct": change,
                "change_vs_prev_close_pct": prev_close_gap if pday_close > 0 else change,
                "day_low": st["low"],
                "day_high": st["high"],
                "session_low": st["low"],
                "session_high": st["high"],
                "vwap_proxy": vwap,
                "above_vwap_proxy": c >= vwap if vwap > 0 else False,
                "previous_day_high": pday_high,
                "previous_day_low": prev_day_low.get(sym, 0.0),
                "previous_close": pday_close,
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
                    "phase_at_detection": phase,
                    "phase_at_detection_ar": _phase_label_ar(phase),
                    "first_seen_price": _round(c, 4),
                    "first_seen_change_pct": _round(change, 2),
                    "max_gain_before_detection_pct": _round(max_gain_before, 2),
                    "detected_premarket_before_5pct": bool(phase == "premarket" and max(abs(change), abs(max_gain_before)) <= 5.0),
                    "detected_before_regular_open": bool(phase in {"overnight", "premarket"}),
                    "detected_at_or_before_open": bool(phase in {"overnight", "premarket", "open"}),
                    "chase_risk_at_detection": chase_code,
                    "chase_risk_label_ar": chase_label,
                    "previous_session_date": daily_dt,
                    "previous_day_high": _round(pday_high, 4),
                    "previous_day_high_distance_pct": _round(prev_high_dist, 2) if pday_high > 0 else 999.0,
                    "previous_close_gap_pct": _round(prev_close_gap, 2) if pday_close > 0 else 0.0,
                    "detected_previous_session": bool(was_prior_candidate),
                    "previous_candidate_dates": sorted(list(prior_candidate_dates_by_symbol.get(sym) or []))[-5:],
                    "stage": enriched.get("opportunity_stage"),
                    "bucket": enriched.get("opportunity_bucket"),
                    "classic_state": classic.get("setup_state"),
                    "classic_score": classic.get("score"),
                    "vwap": classic.get("vwap"),
                    "fib_levels": classic.get("fib_levels"),
                    "behavior_tags": (classic.get("behavior_group") or {}).get("tags", []),
                    "reasons": classic.get("reasons", [])[:8],
                    "max_after_price": _round(c, 4),
                    "min_after_price": _round(c, 4),
                    "max_after_pct": 0.0,
                    "min_after_pct": 0.0,
                }
                prior_candidate_dates_by_symbol[sym].add(date)
            if event_key in events:
                ev = events[event_key]
                ev["max_after_price"] = max(_num(ev.get("max_after_price"), c), c)
                ev["min_after_price"] = min(_num(ev.get("min_after_price"), c), c)
                base = _num(ev.get("first_seen_price"), c)
                ev["max_after_pct"] = _round(((ev["max_after_price"] - base) / base * 100.0) if base > 0 else 0.0, 2)
                ev["min_after_pct"] = _round(((ev["min_after_price"] - base) / base * 100.0) if base > 0 else 0.0, 2)
        rows_by_file[source_name] = file_rows
        # Finalize this file's daily high/low/close so the next downloaded minute
        # file can use it as previous session context even when daily files are unavailable.
        for (sym, dt) in current_file_day_keys:
            st = day_state.get((sym, dt)) or {}
            if _num(st.get("high"), 0.0) > 0:
                prev_day_high[sym] = _num(st.get("high"), 0.0)
                prev_day_low[sym] = _num(st.get("low"), 0.0)
                prev_day_close[sym] = _num(st.get("close"), 0.0)

    candidates = sorted(events.values(), key=lambda x: (_num(x.get("max_after_pct"), 0.0), -abs(_num(x.get("max_gain_before_detection_pct"), 0.0))), reverse=True)[:max(1, int(max_candidates or 120))]
    grouped: dict[str, int] = defaultdict(int)
    phase_counts: dict[str, int] = defaultdict(int)
    timing_counts: dict[str, int] = defaultdict(int)
    for c in candidates:
        for tag in c.get("behavior_tags") or ["غير مصنف"]:
            grouped[tag] += 1
        phase_counts[str(c.get("phase_at_detection") or "unknown")] += 1
        if c.get("detected_previous_session"):
            timing_counts["detected_previous_session"] += 1
        if c.get("detected_premarket_before_5pct"):
            timing_counts["premarket_before_5pct"] += 1
        if c.get("detected_at_or_before_open"):
            timing_counts["at_or_before_open"] += 1
        if str(c.get("chase_risk_at_detection")) in {"late", "very_late"}:
            timing_counts["late_or_chase"] += 1
        else:
            timing_counts["early_or_acceptable"] += 1
    return {
        "ok": True,
        "version": MARKET_REPLAY_LAB_VERSION,
        "opportunity_radar_version": OPPORTUNITY_RADAR_VERSION,
        "source_path": resolved,
        "files_seen": files_seen[:30],
        "daily_files_seen": daily_files_seen[:30],
        "dates_seen": sorted(list(dates_seen)),
        "rows_seen": rows_seen,
        "rows_by_file": rows_by_file,
        "row_cap_mode": "per_file_day",
        "max_rows_per_file": max_rows_per_file,
        "candidate_count": len(candidates),
        "timing_summary": dict(timing_counts),
        "phase_counts": [{"phase": k, "label_ar": _phase_label_ar(k), "count": v} for k, v in sorted(phase_counts.items())],
        "behavior_groups": sorted([{"tag": k, "count": v} for k, v in grouped.items()], key=lambda x: x["count"], reverse=True)[:20],
        "candidates": candidates,
        "rule_ar": "هذه نتائج Replay مختصرة لا تخزن raw؛ تقيس متى ظهر السهم، هل ظهر قبل الافتتاح/قبل المطاردة، وهل كان مرشحًا في الجلسة السابقة إذا توفرت أيام متعددة.",
    }



def run_small_stock_classic_replay_from_polygon(
    *,
    end_date: str = "",
    minute_days: int = 5,
    max_rows: int = 250_000,
    max_candidates: int = 120,
    force: bool = False,
) -> dict[str, Any]:
    """Pull Polygon minute flat files to /tmp, replay them, then delete raw files.

    This is the production-safe path for Railway: the user does not need to
    manually place a ZIP in /tmp.  It uses the existing Polygon/Massive flat-file
    credentials and the fetcher's attempt cap.  Raw files are temporary only and
    are cleaned after compact replay results are produced.
    """
    try:
        from app.polygon_flatfile_fetcher import cleanup_tmp_path, flatfiles_config_status, pull_flatfiles_for_window
    except Exception as exc:
        return {
            "ok": False,
            "version": MARKET_REPLAY_LAB_VERSION,
            "error": "polygon_fetcher_unavailable",
            "detail": f"{type(exc).__name__}: {str(exc)[:180]}",
        }

    days = max(1, min(10, int(minute_days or 5)))
    # Pull enough daily context to cover the previous trading session even when
    # the immediately prior calendar day is a weekend/holiday.
    daily_days = max(days + 8, 10)
    pull = pull_flatfiles_for_window(end_date=end_date or None, minute_days=days, daily_days=daily_days, force=bool(force))
    tmp_dir = str(pull.get("tmp_dir") or "")
    try:
        if not pull.get("ok"):
            return {
                "ok": False,
                "version": MARKET_REPLAY_LAB_VERSION,
                "error": "polygon_pull_failed_or_empty",
                "pull_status": pull,
                "config": flatfiles_config_status(),
                "hint_ar": "تأكد من تفعيل POLYGON_FLATFILES_ENABLED ومفاتيح Flat Files S3. لن تُحفظ الملفات الخام؛ التحميل مؤقت في /tmp فقط.",
            }
        minute_paths = [str(x) for x in (pull.get("minute_paths") or []) if str(x)]
        if not minute_paths:
            return {
                "ok": False,
                "version": MARKET_REPLAY_LAB_VERSION,
                "error": "no_minute_files_downloaded",
                "pull_status": pull,
                "hint_ar": "تم الاتصال لكن لم يتم تنزيل ملفات minute. ربما التاريخ غير متاح بعد أو وصل حد المحاولات.",
            }
        replay = run_small_stock_classic_replay_from_path(
            path=tmp_dir or str(Path(minute_paths[0]).parent),
            max_files=days,
            max_rows=max_rows,
            max_candidates=max_candidates,
        )
        replay["polygon_pull"] = {
            "ok": True,
            "minute_dates": pull.get("minute_dates"),
            "daily_dates": pull.get("daily_dates"),
            "minute_files_downloaded": len(minute_paths),
            "daily_files_downloaded": len([str(x) for x in (pull.get("daily_paths") or []) if str(x)]),
            "daily_days_requested_for_previous_session_context": daily_days,
            "results_summary": [
                {
                    "dataset": r.get("dataset"),
                    "trade_date": r.get("trade_date"),
                    "status": r.get("status"),
                    "ok": r.get("ok"),
                    "attempts": r.get("attempts"),
                    "skipped": r.get("skipped"),
                    "error": r.get("error", ""),
                }
                for r in (pull.get("results") or [])
                if str(r.get("dataset")) == "minute"
            ],
        }
        replay["storage_rule_ar"] = "تم تنزيل ملفات Polygon مؤقتًا إلى /tmp للتشغيل ثم تنظيفها؛ لا يتم حفظ raw في SQLite/GitHub/Railway volume."
        return replay
    finally:
        if tmp_dir:
            cleanup_tmp_path(tmp_dir)
