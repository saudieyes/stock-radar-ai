"""Historical Replay Simulator V2S.

Purpose
-------
Run an isolated, no-lookahead replay for a past market date:
1) run the same production source helpers over the selected day and optional
   prior daily context to simulate after-close tomorrow preparation;
2) evaluate the prepared list on the next trading session;
3) audit winners that the tool missed so learning is not based only on selected
   candidates;
4) return compact metrics for learning and quality calibration.

Safety rules
------------
- This module does not alter live Strong/Cautious/BUY_NOW logic.
- It does not store raw Polygon files in SQLite/GitHub/Railway volume.
- It uses compact Polygon grouped daily REST data only in V2S/V2S1.
- It clearly separates selection data from next-session outcome data to avoid
  historical lookahead.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any

import scanner as _scanner
from app.sharia_filter import assess_sharia_source_fast
from app.source_discovery import (
    _collect_low_float_fast_lane_candidates,
    _collect_micro_explosion_full_market_candidates,
)
try:
    from app.polygon_flatfile_fetcher import is_us_market_trading_day
except Exception:
    def is_us_market_trading_day(value):
        try:
            d = _parse_date(value)
            return bool(d and d.weekday() < 5)
        except Exception:
            return False

HISTORICAL_REPLAY_SIMULATOR_VERSION = "historical_replay_simulator_v2s1_after_close_context_missed_winners_2026_06_20"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default or 0.0)
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").replace("%", "").strip()
        n = float(v)
        if math.isnan(n) or math.isinf(n):
            return float(default or 0.0)
        return n
    except Exception:
        return float(default or 0.0)


def _round(v: Any, nd: int = 2) -> float:
    try:
        return round(_num(v), int(nd))
    except Exception:
        return 0.0


def _parse_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _today_utc() -> date:
    return datetime.utcnow().date()


def _previous_trading_day(value: str | date | datetime | None = None) -> date:
    d = _parse_date(value) or _today_utc()
    d -= timedelta(days=1)
    for _ in range(20):
        if is_us_market_trading_day(d):
            return d
        d -= timedelta(days=1)
    return d


def _next_trading_day(value: str | date | datetime | None = None) -> date:
    d = _parse_date(value) or _today_utc()
    d += timedelta(days=1)
    for _ in range(20):
        if is_us_market_trading_day(d):
            return d
        d += timedelta(days=1)
    return d


def _safe_grouped(date_value: str | date | datetime | None) -> dict[str, dict]:
    d = _parse_date(date_value)
    if not d:
        return {}
    try:
        m = _scanner.get_grouped_daily_map(d.isoformat()) or {}
        return dict(m or {})
    except Exception:
        return {}




def _normalize_pct_value(value: Any, default: float = 0.0) -> float:
    """Normalize mixed percentage encodings without turning 1.33% into 133%."""
    raw = _num(value, default)
    # Source helpers generally use percent points (e.g. 7.8). Some raw
    # ratios can arrive as 0.078. Only multiply tiny decimal ratios.
    if raw != 0 and abs(raw) < 0.35:
        return raw * 100.0
    return raw


def _grouped_row_metrics(row: dict | None) -> dict:
    row = dict(row or {})
    price = _num(row.get("price"), 0.0) or _num(row.get("close"), 0.0) or _num(row.get("c"), 0.0)
    opn = _num(row.get("open"), 0.0) or _num(row.get("o"), 0.0)
    high = _num(row.get("high"), 0.0) or _num(row.get("h"), 0.0)
    low = _num(row.get("low"), 0.0) or _num(row.get("l"), 0.0)
    vol = _num(row.get("volume"), 0.0) or _num(row.get("v"), 0.0)
    dollar_vol = price * vol if price > 0 and vol > 0 else 0.0
    range_pct = ((high - low) / price * 100.0) if price > 0 and high > 0 and low > 0 else 0.0
    change_pct = ((price - opn) / opn * 100.0) if price > 0 and opn > 0 else _normalize_pct_value(row.get("change_pct"), 0.0)
    close_strength = ((price - low) / (high - low)) if high > low and price > 0 else 0.0
    near_high = bool(high > 0 and price > 0 and ((high - price) / max(price, 0.01) * 100.0) <= 1.5)
    return {
        "price": _round(price, 4),
        "open": _round(opn, 4),
        "high": _round(high, 4),
        "low": _round(low, 4),
        "volume": _round(vol, 0),
        "dollar_volume": _round(dollar_vol, 2),
        "range_pct": _round(range_pct, 2),
        "change_pct": _round(change_pct, 2),
        "close_strength": _round(close_strength, 3),
        "near_high": near_high,
    }


def _resolve_context_grouped(selection_date: str, selection_map: dict, context_days: int = 3, recovery_days: int = 7) -> tuple[list[dict], dict]:
    """Return prior trading-day context ending at selection_date, no lookahead."""
    base = _parse_date(selection_date)
    target_days = max(1, min(10, int(context_days or 3)))
    max_days = max(target_days, min(30, int(recovery_days or 7) + target_days + 5))
    debug = {
        "version": "historical_replay_context_resolver_v2s1_2026_06_20",
        "selection_date": selection_date,
        "requested_context_days": target_days,
        "attempts": [],
        "rule_ar": "V2S1 يجمع أيامًا سابقة فقط حتى يوم الاختيار لمحاكاة ذاكرة/تحضير الإغلاق، ولا يستخدم يوم التقييم أو ما بعده.",
    }
    if not base:
        debug.update({"ok": False, "reason": "invalid_selection_date"})
        return [], debug
    out: list[dict] = []
    d = base
    for _ in range(max_days):
        trading = bool(is_us_market_trading_day(d))
        if d.isoformat() == str(selection_date) and selection_map:
            m = selection_map
        else:
            m = _safe_grouped(d) if trading else {}
        debug["attempts"].append({"date": d.isoformat(), "trading_day": trading, "rows": len(m or {})})
        if trading and len(m or {}) >= 500:
            out.append({"date": d.isoformat(), "map": m, "rows": len(m or {})})
            if len(out) >= target_days:
                break
        d -= timedelta(days=1)
    out = sorted(out, key=lambda x: str(x.get("date") or ""))
    debug.update({"ok": bool(out), "context_dates": [x.get("date") for x in out], "context_days_found": len(out)})
    return out, debug


def _annotate_source_rows(rows: list[dict], context_date: str, context_index: int, source_family: str) -> list[dict]:
    out = []
    for r in rows or []:
        item = dict(r or {})
        item["historical_context_date"] = context_date
        item["historical_context_index"] = context_index
        item["historical_source_family"] = source_family
        out.append(item)
    return out


def _build_context_source_rows(context_items: list[dict]) -> tuple[list[dict], list[dict], dict]:
    all_micro: list[dict] = []
    all_fast: list[dict] = []
    daily_debug: list[dict] = []
    for idx, item in enumerate(context_items or []):
        d = str(item.get("date") or "")
        m = dict(item.get("map") or {})
        micro_rows, micro_debug = _collect_micro_explosion_full_market_candidates(m, phase_detail=f"historical_after_close_context_v2s1:{d}")
        fast_rows, fast_debug = _collect_low_float_fast_lane_candidates(m, phase_detail=f"historical_after_close_context_v2s1:{d}")
        all_micro.extend(_annotate_source_rows(micro_rows, d, idx, "micro_explosion_full_market_v2r2"))
        all_fast.extend(_annotate_source_rows(fast_rows, d, idx, "low_float_fast_lane_v2q"))
        daily_debug.append({
            "date": d,
            "rows": len(m),
            "micro_scanned": (micro_debug or {}).get("scanned"),
            "micro_eligible": (micro_debug or {}).get("eligible_count"),
            "micro_seed": (micro_debug or {}).get("seed_match_count"),
            "micro_top_symbols": (micro_debug or {}).get("top_symbols", [])[:15],
            "fast_scanned": (fast_debug or {}).get("scanned"),
            "fast_eligible": (fast_debug or {}).get("eligible_count"),
            "fast_top_symbols": (fast_debug or {}).get("top_symbols", [])[:15],
        })
    debug = {
        "version": "historical_replay_context_source_scan_v2s1_2026_06_20",
        "daily_debug": daily_debug,
        "micro_rows_context_total": len(all_micro),
        "fast_rows_context_total": len(all_fast),
        "rule_ar": "كل يوم في السياق يُفحص بنفس دوال Source Discovery الحالية، ثم تُجمع النتائج لمحاكاة ذاكرة الإغلاق/قائمة الغد.",
    }
    return all_micro, all_fast, debug


def _build_symbol_context(context_items: list[dict]) -> dict[str, dict]:
    by_symbol: dict[str, dict] = {}
    for item in context_items or []:
        d = str(item.get("date") or "")
        m = dict(item.get("map") or {})
        for sym_raw, row in m.items():
            sym = _u(sym_raw or (row or {}).get("symbol") or (row or {}).get("ticker") or (row or {}).get("T"))
            if not sym:
                continue
            rec = by_symbol.get(sym) or {"symbol": sym, "dates": [], "daily": [], "first_seen_date": d, "last_seen_date": d}
            metrics = _grouped_row_metrics(row)
            rec["dates"].append(d)
            rec["daily"].append({"date": d, **metrics})
            rec["first_seen_date"] = min(str(rec.get("first_seen_date") or d), d)
            rec["last_seen_date"] = max(str(rec.get("last_seen_date") or d), d)
            by_symbol[sym] = rec
    for rec in by_symbol.values():
        daily = sorted(rec.get("daily") or [], key=lambda x: str(x.get("date") or ""))
        rec["daily"] = daily
        rec["days_available"] = len(daily)
        if daily:
            rec["latest"] = daily[-1]
            first_price = _num(daily[0].get("price"), 0.0)
            last_price = _num(daily[-1].get("price"), 0.0)
            first_vol = _num(daily[0].get("volume"), 0.0)
            last_vol = _num(daily[-1].get("volume"), 0.0)
            rec["context_price_change_pct"] = _round(((last_price - first_price) / first_price * 100.0) if first_price > 0 else 0.0, 2)
            rec["context_volume_change_pct"] = _round(((last_vol - first_vol) / first_vol * 100.0) if first_vol > 0 else 0.0, 2)
            rec["avg_close_strength"] = _round(sum(_num(x.get("close_strength"), 0.0) for x in daily) / max(1, len(daily)), 3)
            rec["max_range_pct"] = _round(max(_num(x.get("range_pct"), 0.0) for x in daily), 2)
            rec["near_high_days"] = sum(1 for x in daily if x.get("near_high"))
    return by_symbol


def _source_trace_from_rows(rows: list[dict]) -> dict[str, dict]:
    trace: dict[str, dict] = {}
    for r in rows or []:
        sym = _extract_symbol(r)
        if not sym:
            continue
        d = _s(r.get("historical_context_date"))
        fam = _s(r.get("historical_source_family")) or "source"
        rec = trace.get(sym) or {"symbol": sym, "source_layers": [], "seen_dates": [], "source_score": 0.0}
        if fam and fam not in rec["source_layers"]:
            rec["source_layers"].append(fam)
        if d and d not in rec["seen_dates"]:
            rec["seen_dates"].append(d)
        rec["source_score"] = max(_num(rec.get("source_score"), 0.0), _candidate_score(r))
        trace[sym] = rec
    for rec in trace.values():
        rec["seen_dates"] = sorted(rec.get("seen_dates") or [])
        rec["first_source_date"] = (rec.get("seen_dates") or [""])[0]
        rec["last_source_date"] = (rec.get("seen_dates") or [""])[-1]
        rec["source_days_seen"] = len(rec.get("seen_dates") or [])
    return trace


def _context_quality_score(symbol_context: dict | None, source_trace: dict | None) -> tuple[float, list[str]]:
    ctx = dict(symbol_context or {})
    latest = dict(ctx.get("latest") or {})
    trace = dict(source_trace or {})
    score = 0.0
    reasons: list[str] = []
    days_seen = int(trace.get("source_days_seen") or 0)
    if days_seen >= 2:
        score += 12
        reasons.append("ظهر في المنبع خلال أكثر من يوم؛ مناسب لقائمة الغد")
    if days_seen >= 3:
        score += 6
        reasons.append("استمر في الظهور 3 أيام؛ احتمال متابعة لصيقة أعلى")
    vol_chg = _num(ctx.get("context_volume_change_pct"), 0.0)
    if vol_chg >= 50:
        score += 10
        reasons.append("الحجم يتحسن عبر الأيام")
    price_chg = _num(ctx.get("context_price_change_pct"), 0.0)
    if -4 <= price_chg <= 18:
        score += 8
        reasons.append("الحركة خلال السياق ليست مطاردة مبالغًا فيها")
    elif price_chg > 25:
        score -= 12
        reasons.append("الحركة خلال السياق متقدمة؛ قد تكون خطفة لا تجهيز مبكر")
    if _num(ctx.get("avg_close_strength"), 0.0) >= 0.62:
        score += 8
        reasons.append("إغلاقات بناءة داخل الشموع")
    if int(ctx.get("near_high_days") or 0) >= 1:
        score += 6
        reasons.append("أغلق قريبًا من قمة يوم واحد على الأقل")
    if _num(latest.get("dollar_volume"), 0.0) >= 250000:
        score += 7
        reasons.append("دولار فوليوم كافٍ للمراقبة")
    if _num(latest.get("change_pct"), 0.0) >= 18:
        score -= 10
        reasons.append("شمعة اليوم متأخرة نسبيًا")
    return _round(score, 2), reasons

def _resolve_selection_grouped(requested_date: str, recovery_days: int = 7) -> tuple[str, dict, dict]:
    requested = _parse_date(requested_date) or _previous_trading_day()
    debug = {
        "requested_date": requested.isoformat(),
        "version": "historical_replay_selection_date_resolver_v2s_2026_06_20",
        "attempts": [],
        "rule_ar": "إذا كان التاريخ عطلة أو Polygon grouped غير متاح، نرجع للخلف حتى آخر يوم تداول صالح. لا نستخدم بيانات اليوم التالي في الاختيار.",
    }
    d = requested
    max_days = max(1, min(20, int(recovery_days or 7)))
    for _ in range(max_days + 1):
        trading = bool(is_us_market_trading_day(d))
        m = _safe_grouped(d) if trading else {}
        debug["attempts"].append({"date": d.isoformat(), "trading_day": trading, "rows": len(m or {})})
        if len(m or {}) >= 500:
            debug.update({"ok": True, "effective_selection_date": d.isoformat(), "rows": len(m or {}), "source_mode": "requested_or_prior_grouped" if d == requested else "prior_grouped_recovered"})
            return d.isoformat(), m, debug
        d -= timedelta(days=1)
    debug.update({"ok": False, "effective_selection_date": "", "rows": 0, "source_mode": "no_grouped_found"})
    return "", {}, debug


def _resolve_outcome_grouped(selection_date: str, lookahead_days: int = 7) -> tuple[str, dict, dict]:
    base = _parse_date(selection_date)
    if not base:
        return "", {}, {"ok": False, "error": "invalid_selection_date"}
    next_d = _next_trading_day(base)
    debug = {
        "selection_date": base.isoformat(),
        "expected_next_trading_day": next_d.isoformat(),
        "version": "historical_replay_outcome_date_resolver_v2s_2026_06_20",
        "attempts": [],
        "rule_ar": "التقييم يستخدم جلسة التداول التالية فقط. إذا لم يتوفر grouped لذلك اليوم بعد، يحاول الأيام التالية كتغطية بيانات ويُظهر ذلك في التشخيص.",
    }
    d = next_d
    max_days = max(1, min(20, int(lookahead_days or 7)))
    for _ in range(max_days + 1):
        trading = bool(is_us_market_trading_day(d))
        m = _safe_grouped(d) if trading else {}
        debug["attempts"].append({"date": d.isoformat(), "trading_day": trading, "rows": len(m or {})})
        if len(m or {}) >= 500:
            debug.update({"ok": True, "effective_outcome_date": d.isoformat(), "rows": len(m or {}), "source_mode": "next_session_grouped" if d == next_d else "later_available_grouped"})
            return d.isoformat(), m, debug
        d += timedelta(days=1)
    debug.update({"ok": False, "effective_outcome_date": "", "rows": 0, "source_mode": "no_outcome_grouped_found"})
    return "", {}, debug


def _extract_symbol(row: dict) -> str:
    return _u(row.get("symbol") or row.get("ticker") or row.get("T"))


def _candidate_score(row: dict) -> float:
    return max(
        _num(row.get("source_score"), 0.0),
        _num(row.get("score"), 0.0),
        _num((row.get("metrics") or {}).get("micro_explosion_capture_score"), 0.0),
        _num((row.get("metrics") or {}).get("low_float_fast_lane_score"), 0.0),
    )


def _candidate_metrics(row: dict) -> dict:
    m = dict(row.get("metrics") or {})
    # Some source rows put fields at top-level.
    for k in ["price", "change_pct", "day_change_pct", "volume", "dollar_volume", "range_pct", "close_strength", "near_high"]:
        if k not in m and k in row:
            m[k] = row.get(k)
    return m


def _combine_source_candidates(micro_rows: list[dict], fast_rows: list[dict], clean_only: bool = True) -> tuple[list[dict], dict]:
    by_symbol: dict[str, dict] = {}
    sharia_counts = {"clean": 0, "gray": 0, "blocked": 0, "manual_approved": 0}
    excluded: list[dict] = []

    def add(row: dict, layer: str):
        sym = _extract_symbol(row)
        if not sym:
            return
        metrics = _candidate_metrics(row)
        sharia = assess_sharia_source_fast(sym)
        status = str(sharia.get("status") or "")
        if sharia.get("should_block"):
            sharia_key = "blocked"
        elif sharia.get("is_gray"):
            sharia_key = "gray"
        elif sharia.get("manual_approved"):
            sharia_key = "manual_approved"
        else:
            sharia_key = "clean"
        sharia_counts[sharia_key] = sharia_counts.get(sharia_key, 0) + 1
        if clean_only and sharia_key in {"blocked", "gray"}:
            excluded.append({
                "symbol": sym,
                "layer": layer,
                "score": _round(_candidate_score(row), 3),
                "sharia_status": status,
                "sharia_label": sharia.get("label"),
                "reason_ar": sharia.get("reason"),
            })
            return
        rec = by_symbol.get(sym) or {
            "symbol": sym,
            "source_layers": [],
            "source_score": 0.0,
            "reasons_ar": [],
            "selection_metrics": {},
            "seen_dates": [],
            "first_source_date": "",
            "last_source_date": "",
            "source_days_seen": 0,
            "sharia_status": status,
            "sharia_label": sharia.get("label"),
            "sharia_reason": sharia.get("reason"),
            "sharia_is_gray": bool(sharia.get("is_gray")),
            "sharia_blocked": bool(sharia.get("should_block")),
        }
        if layer not in rec["source_layers"]:
            rec["source_layers"].append(layer)
        ctx_date = _s(row.get("historical_context_date"))
        if ctx_date and ctx_date not in rec["seen_dates"]:
            rec["seen_dates"].append(ctx_date)
            rec["seen_dates"] = sorted(rec["seen_dates"])
            rec["first_source_date"] = rec["seen_dates"][0]
            rec["last_source_date"] = rec["seen_dates"][-1]
            rec["source_days_seen"] = len(rec["seen_dates"])
        rec["source_score"] = max(_num(rec.get("source_score"), 0.0), _candidate_score(row))
        reasons = list(metrics.get("micro_explosion_reasons_ar") or metrics.get("low_float_fast_lane_reasons") or row.get("reasons") or [])
        for r in reasons:
            txt = _s(r)
            if txt and txt not in rec["reasons_ar"]:
                rec["reasons_ar"].append(txt)
        # Keep the metrics from the highest-scoring row or latest available source row.
        if not rec.get("selection_metrics") or _s(row.get("historical_context_date")) >= _s(rec.get("last_metrics_date")):
            rec["selection_metrics"] = metrics
            rec["last_metrics_date"] = _s(row.get("historical_context_date"))
        by_symbol[sym] = rec

    for r in micro_rows or []:
        add(r, "micro_explosion_full_market_v2r2")
    for r in fast_rows or []:
        add(r, "low_float_fast_lane_v2q")

    out = sorted(by_symbol.values(), key=lambda x: _num(x.get("source_score"), 0.0), reverse=True)
    return out, {"sharia_counts": sharia_counts, "excluded_by_sharia_sample": sorted(excluded, key=lambda x: _num(x.get("score"), 0.0), reverse=True)[:40]}


def _prep_bucket(c: dict, context_by_symbol: dict | None = None, trace_by_symbol: dict | None = None) -> dict:
    sym = _u(c.get("symbol"))
    ctx = (context_by_symbol or {}).get(sym) or {}
    trace = (trace_by_symbol or {}).get(sym) or {}
    qscore, qreasons = _context_quality_score(ctx, trace)
    metrics = c.get("selection_metrics") or {}
    change = _normalize_pct_value(metrics.get("change_pct", metrics.get("day_change_pct", 0.0)), 0.0)
    layers = set(c.get("source_layers") or [])
    if change >= 18:
        bucket = "quick_take_profit_or_pullback_only"
        label = "خطفة/متأخر — لا يطارد"
    elif qscore >= 28 and len(layers) >= 2:
        bucket = "top_tomorrow_close_watch"
        label = "قائمة الغد — مراقبة لصيقة"
    elif qscore >= 18:
        bucket = "tomorrow_watch_needs_premarket_confirmation"
        label = "قائمة الغد — يحتاج تأكيد بري ماركت/افتتاح"
    else:
        bucket = "raw_tomorrow_watch"
        label = "مراقبة خام للغد"
    return {
        "tomorrow_prep_bucket": bucket,
        "tomorrow_prep_label_ar": label,
        "context_quality_score": qscore,
        "context_quality_reasons_ar": qreasons,
        "context_days_seen": int(trace.get("source_days_seen") or c.get("source_days_seen") or 0),
        "first_source_date": trace.get("first_source_date") or c.get("first_source_date") or "",
        "last_source_date": trace.get("last_source_date") or c.get("last_source_date") or "",
        "context_price_change_pct": _round((ctx or {}).get("context_price_change_pct"), 2),
        "context_volume_change_pct": _round((ctx or {}).get("context_volume_change_pct"), 2),
    }


def _evaluate_candidate(c: dict, selection_grouped: dict, outcome_grouped: dict, outcome_date: str, context_by_symbol: dict | None = None, trace_by_symbol: dict | None = None) -> dict:
    sym = _u(c.get("symbol"))
    sel = selection_grouped.get(sym) or {}
    nxt = outcome_grouped.get(sym) or {}
    metrics = c.get("selection_metrics") or {}
    ctx = (context_by_symbol or {}).get(sym) or {}
    latest_ctx = (ctx.get("latest") or {}) if isinstance(ctx, dict) else {}
    sel_price = _num(metrics.get("price"), 0.0) or _num(sel.get("price"), 0.0) or _num(sel.get("close"), 0.0) or _num(latest_ctx.get("price"), 0.0)
    sel_open = _num(sel.get("open"), 0.0) or _num(latest_ctx.get("open"), 0.0)
    sel_high = _num(sel.get("high"), 0.0) or _num(latest_ctx.get("high"), 0.0)
    sel_low = _num(sel.get("low"), 0.0) or _num(latest_ctx.get("low"), 0.0)
    sel_vol = _num(sel.get("volume"), 0.0) or _num(latest_ctx.get("volume"), 0.0)
    nxt_open = _num(nxt.get("open"), 0.0)
    nxt_high = _num(nxt.get("high"), 0.0)
    nxt_low = _num(nxt.get("low"), 0.0)
    nxt_close = _num(nxt.get("price"), 0.0) or _num(nxt.get("close"), 0.0)
    nxt_vol = _num(nxt.get("volume"), 0.0)

    has_outcome = bool(sel_price > 0 and nxt_high > 0 and nxt_low > 0)
    max_gain = ((nxt_high - sel_price) / sel_price * 100.0) if has_outcome else 0.0
    worst_dd = ((nxt_low - sel_price) / sel_price * 100.0) if has_outcome else 0.0
    close_gain = ((nxt_close - sel_price) / sel_price * 100.0) if has_outcome and nxt_close > 0 else 0.0
    gap_open = ((nxt_open - sel_price) / sel_price * 100.0) if has_outcome and nxt_open > 0 else 0.0
    intraday_from_open = ((nxt_high - nxt_open) / nxt_open * 100.0) if nxt_open > 0 and nxt_high > 0 else 0.0
    selection_change = _normalize_pct_value(metrics.get("change_pct", metrics.get("day_change_pct", 0.0)), 0.0)

    if not has_outcome:
        label = "لا توجد بيانات للجلسة التالية"
        rating = "no_outcome"
    elif max_gain >= 20 and worst_dd > -10:
        label = "فائز قوي نظيف"
        rating = "strong_clean_winner"
    elif max_gain >= 10 and worst_dd > -8:
        label = "فائز جيد"
        rating = "good_winner"
    elif max_gain >= 5 and worst_dd > -8:
        label = "حركة مقبولة"
        rating = "moderate_move"
    elif worst_dd <= -12:
        label = "خطر/فشل"
        rating = "danger_fail"
    else:
        label = "ضعيف"
        rating = "weak"

    late_risk = bool(selection_change >= 18 or gap_open >= 12)
    if has_outcome and late_risk and max_gain < 10:
        label = "متأخر وضعيف"
        rating = "late_weak"

    prep = _prep_bucket(c, context_by_symbol=context_by_symbol, trace_by_symbol=trace_by_symbol)
    return {
        **c,
        **prep,
        "outcome_date": outcome_date,
        "has_next_session_outcome": has_outcome,
        "selection_price": _round(sel_price, 4),
        "selection_open": _round(sel_open, 4),
        "selection_high": _round(sel_high, 4),
        "selection_low": _round(sel_low, 4),
        "selection_volume": _round(sel_vol, 0),
        "selection_change_pct": _round(selection_change, 2),
        "next_open": _round(nxt_open, 4),
        "next_high": _round(nxt_high, 4),
        "next_low": _round(nxt_low, 4),
        "next_close": _round(nxt_close, 4),
        "next_volume": _round(nxt_vol, 0),
        "gap_to_next_open_pct": _round(gap_open, 2),
        "next_session_max_gain_pct": _round(max_gain, 2),
        "next_session_worst_drawdown_pct": _round(worst_dd, 2),
        "next_session_close_gain_pct": _round(close_gain, 2),
        "next_intraday_gain_from_open_pct": _round(intraday_from_open, 2),
        "hit_5pct": bool(max_gain >= 5),
        "hit_10pct": bool(max_gain >= 10),
        "hit_20pct": bool(max_gain >= 20),
        "failed_minus_8pct": bool(worst_dd <= -8),
        "failed_minus_12pct": bool(worst_dd <= -12),
        "late_risk_from_selection": late_risk,
        "outcome_rating": rating,
        "outcome_label_ar": label,
        "timing_level": "daily_after_close_context",
        "timing_note_ar": "V2S1 يعرف التحضير بعد الإغلاق وسياق الأيام فقط؛ توقيت البري ماركت/داخل الجلسة يحتاج V2S2 minute replay.",
    }


def _audit_missed_winners(selection_grouped: dict, outcome_grouped: dict, outcome_date: str, selected_symbols: set[str], candidate_symbols: set[str], trace_by_symbol: dict, context_by_symbol: dict, min_gain_pct: float = 20.0) -> dict:
    min_gain = max(5.0, min(300.0, float(min_gain_pct or 20.0)))
    winners: list[dict] = []
    all_counts = {"20pct": 0, "50pct": 0, "100pct": 0, "200pct": 0}
    captured_counts = {"20pct": 0, "50pct": 0, "100pct": 0, "200pct": 0}
    for sym_raw, out_row in (outcome_grouped or {}).items():
        sym = _u(sym_raw or (out_row or {}).get("symbol") or (out_row or {}).get("ticker") or (out_row or {}).get("T"))
        if not sym:
            continue
        ctx = context_by_symbol.get(sym) or {}
        latest = (ctx.get("latest") or {}) if isinstance(ctx, dict) else {}
        sel = selection_grouped.get(sym) or {}
        sel_price = _num(sel.get("price"), 0.0) or _num(sel.get("close"), 0.0) or _num(latest.get("price"), 0.0)
        high = _num((out_row or {}).get("high"), 0.0)
        low = _num((out_row or {}).get("low"), 0.0)
        close = _num((out_row or {}).get("price"), 0.0) or _num((out_row or {}).get("close"), 0.0)
        if sel_price <= 0 or high <= 0:
            continue
        max_gain = (high - sel_price) / sel_price * 100.0
        worst_dd = ((low - sel_price) / sel_price * 100.0) if low > 0 else 0.0
        close_gain = ((close - sel_price) / sel_price * 100.0) if close > 0 else 0.0
        for thr, key in [(20, "20pct"), (50, "50pct"), (100, "100pct"), (200, "200pct")]:
            if max_gain >= thr:
                all_counts[key] += 1
                if sym in selected_symbols:
                    captured_counts[key] += 1
        if max_gain < min_gain:
            continue
        sharia = assess_sharia_source_fast(sym)
        trace = trace_by_symbol.get(sym) or {}
        in_selected = sym in selected_symbols
        in_candidate_pool = sym in candidate_symbols
        in_pre_source = bool(trace)
        if in_selected:
            miss_reason = "التقطته قائمة الغد/المحاكي — ليس مفقودًا، نراجع الترتيب والتوقيت لاحقًا."
            miss_code = "captured_selected"
        elif not in_pre_source:
            miss_reason = "لم يدخل Micro/Fast Lane خلال سياق الأيام؛ نحتاج معرفة هل الانفجار بدأ في بري ماركت/داخل الجلسة أو يحتاج minute replay."
            miss_code = "not_in_source_context"
        elif sharia.get("should_block"):
            miss_reason = f"التقطه المصدر لكن فلتر الشرعية منعه: {sharia.get('reason')}"
            miss_code = "sharia_blocked"
        elif sharia.get("is_gray"):
            miss_reason = f"التقطه المصدر لكنه رمادي شرعيًا ولم يدخل clean_only: {sharia.get('reason')}"
            miss_code = "sharia_gray"
        elif in_candidate_pool:
            miss_reason = "دخل المرشحين بعد الشرعية لكنه لم يدخل قائمة التقييم بسبب limit/ترتيب أقل."
            miss_code = "outside_top_limit"
        else:
            miss_reason = "دخل المصدر قبل الشرعية لكن لم يصل لقائمة clean النهائية؛ راجع الترتيب/الشرعية/الدمج."
            miss_code = "source_not_selected"
        winners.append({
            "symbol": sym,
            "outcome_date": outcome_date,
            "selection_price": _round(sel_price, 4),
            "next_high": _round(high, 4),
            "next_low": _round(low, 4),
            "next_close": _round(close, 4),
            "next_session_max_gain_pct": _round(max_gain, 2),
            "next_session_worst_drawdown_pct": _round(worst_dd, 2),
            "next_session_close_gain_pct": _round(close_gain, 2),
            "selected_by_tool": in_selected,
            "in_candidate_pool_after_sharia": in_candidate_pool,
            "in_source_context_before_sharia": in_pre_source,
            "source_layers": trace.get("source_layers", []),
            "source_days_seen": int(trace.get("source_days_seen") or 0),
            "first_source_date": trace.get("first_source_date", ""),
            "last_source_date": trace.get("last_source_date", ""),
            "sharia_status": sharia.get("status"),
            "sharia_label": sharia.get("label"),
            "sharia_reason": sharia.get("reason"),
            "miss_code": miss_code,
            "miss_reason_ar": miss_reason,
            "context_price_change_pct": _round((ctx or {}).get("context_price_change_pct"), 2),
            "context_volume_change_pct": _round((ctx or {}).get("context_volume_change_pct"), 2),
        })
    winners = sorted(winners, key=lambda x: _num(x.get("next_session_max_gain_pct"), 0.0), reverse=True)
    missed = [w for w in winners if not w.get("selected_by_tool")]
    captured = [w for w in winners if w.get("selected_by_tool")]
    def pct(a, b):
        return _round((float(a) / max(1, int(b)) * 100.0), 1)
    return {
        "version": "missed_explosion_audit_v2s1_daily_context_2026_06_20",
        "min_gain_pct": min_gain,
        "outcome_date": outcome_date,
        "all_explosion_counts": all_counts,
        "captured_explosion_counts": captured_counts,
        "capture_rate_20pct": pct(captured_counts.get("20pct", 0), all_counts.get("20pct", 0)),
        "winners_over_threshold_count": len(winners),
        "captured_over_threshold_count": len(captured),
        "missed_over_threshold_count": len(missed),
        "missed_reason_counts": {k: sum(1 for w in missed if w.get("miss_code") == k) for k in sorted({w.get("miss_code") for w in missed})},
        "top_outcome_winners": winners[:30],
        "top_missed_winners": missed[:30],
        "top_captured_winners": captured[:20],
        "rule_ar": "هذه القائمة تبحث عن كل أسهم جلسة التقييم التي انفجرت، ثم تسأل هل كانت في قائمة الإغلاق أم لا ولماذا، بدون استخدام بيانات التقييم في الاختيار.",
    }

def _summary(rows: list[dict]) -> dict:
    total = max(1, len(rows))
    with_outcome = [r for r in rows if r.get("has_next_session_outcome")]
    n = max(1, len(with_outcome))
    gains = [_num(r.get("next_session_max_gain_pct"), 0.0) for r in with_outcome]
    dds = [_num(r.get("next_session_worst_drawdown_pct"), 0.0) for r in with_outcome]
    def count(pred):
        return sum(1 for r in with_outcome if pred(r))
    def pct(x, denom=n):
        return round(float(x) / max(1, denom) * 100.0, 1)
    med_gain = sorted(gains)[len(gains)//2] if gains else 0.0
    med_dd = sorted(dds)[len(dds)//2] if dds else 0.0
    return {
        "selected_count": len(rows),
        "with_outcome_count": len(with_outcome),
        "hit_5pct_count": count(lambda r: r.get("hit_5pct")),
        "hit_5pct_pct": pct(count(lambda r: r.get("hit_5pct"))),
        "hit_10pct_count": count(lambda r: r.get("hit_10pct")),
        "hit_10pct_pct": pct(count(lambda r: r.get("hit_10pct"))),
        "hit_20pct_count": count(lambda r: r.get("hit_20pct")),
        "hit_20pct_pct": pct(count(lambda r: r.get("hit_20pct"))),
        "danger_fail_count": count(lambda r: str(r.get("outcome_rating")) == "danger_fail"),
        "danger_fail_pct": pct(count(lambda r: str(r.get("outcome_rating")) == "danger_fail")),
        "late_weak_count": count(lambda r: str(r.get("outcome_rating")) == "late_weak"),
        "late_weak_pct": pct(count(lambda r: str(r.get("outcome_rating")) == "late_weak")),
        "median_next_session_max_gain_pct": _round(med_gain, 2),
        "median_next_session_worst_drawdown_pct": _round(med_dd, 2),
        "avg_next_session_max_gain_pct": _round(sum(gains) / max(1, len(gains)), 2),
        "avg_next_session_worst_drawdown_pct": _round(sum(dds) / max(1, len(dds)), 2),
        "assessment_ar": _assessment_ar(rows, with_outcome),
        "rule_ar": "هذه نتائج يوم واحد فقط؛ لا تعتمد عليها كحكم نهائي حتى نكررها على 5-10 أيام تاريخية.",
    }


def _assessment_ar(rows: list[dict], with_outcome: list[dict]) -> str:
    if not rows:
        return "لا توجد اختيارات؛ الالتقاط ضيق أو البيانات غير متاحة."
    n = max(1, len(with_outcome))
    hit10 = sum(1 for r in with_outcome if r.get("hit_10pct")) / n * 100.0
    fail = sum(1 for r in with_outcome if str(r.get("outcome_rating")) in {"danger_fail", "late_weak"}) / n * 100.0
    if hit10 >= 25 and fail <= 25:
        return "نتيجة جيدة مبدئيًا: الالتقاط وجد فرصًا قابلة للحركة، ونحتاج تحسين الترتيب لا زيادة الالتقاط."
    if hit10 >= 15:
        return "نتيجة مقبولة لكنها تحتاج فلترة جودة/ترتيب أقوى قبل فتح القيود."
    if fail >= 35:
        return "الالتقاط واسع أكثر من اللازم لهذا اليوم؛ نحتاج تضييق الجودة أو فصل المتأخر/الخطفة عن المرشح المبكر."
    return "النتيجة ضعيفة أو هادئة؛ كرر الاختبار على أيام أكثر قبل الحكم."


def run_historical_replay(
    *,
    date_value: str = "",
    max_candidates: int = 40,
    clean_only: bool = True,
    include_candidates: bool = True,
    recovery_days: int = 7,
    context_days: int = 3,
    missed_gain_threshold: float = 20.0,
) -> dict[str, Any]:
    selection_date, selection_map, selection_debug = _resolve_selection_grouped(date_value, recovery_days=recovery_days)
    if not selection_map:
        return {
            "ok": False,
            "version": HISTORICAL_REPLAY_SIMULATOR_VERSION,
            "error": "selection_grouped_unavailable",
            "selection_date_debug": selection_debug,
            "rule_ar": "المحاكي لا يستخدم بيانات اليوم التالي للاختيار. إذا لم يجد grouped ليوم الاختيار أو ما قبله، يتوقف بدل الغش التاريخي.",
        }

    outcome_date, outcome_map, outcome_debug = _resolve_outcome_grouped(selection_date, lookahead_days=recovery_days)
    context_items, context_debug = _resolve_context_grouped(selection_date, selection_map, context_days=context_days, recovery_days=recovery_days)
    context_by_symbol = _build_symbol_context(context_items)
    micro_rows, fast_rows, context_source_debug = _build_context_source_rows(context_items)
    trace_by_symbol = _source_trace_from_rows((micro_rows or []) + (fast_rows or []))

    # Selection-day debug is kept separately so we can compare true after-close D vs sticky 3-day context.
    selection_micro_rows, selection_micro_debug = _collect_micro_explosion_full_market_candidates(selection_map, phase_detail="historical_after_close_selection_day_v2s1")
    selection_fast_rows, selection_fast_debug = _collect_low_float_fast_lane_candidates(selection_map, phase_detail="historical_after_close_selection_day_v2s1")

    candidates, combine_debug = _combine_source_candidates(micro_rows, fast_rows, clean_only=bool(clean_only))
    limit = max(5, min(160, int(max_candidates or 40)))
    selected = candidates[:limit]
    selected_symbols = {_u(x.get("symbol")) for x in selected}
    candidate_symbols = {_u(x.get("symbol")) for x in candidates}
    evaluated = [_evaluate_candidate(c, selection_map, outcome_map, outcome_date, context_by_symbol=context_by_symbol, trace_by_symbol=trace_by_symbol) for c in selected]
    evaluated_sorted = sorted(evaluated, key=lambda x: _num(x.get("next_session_max_gain_pct"), 0.0), reverse=True)

    top_winners = evaluated_sorted[:15]
    top_failures = sorted(evaluated, key=lambda x: (_num(x.get("next_session_worst_drawdown_pct"), 0.0), -_num(x.get("next_session_max_gain_pct"), 0.0)))[:15]
    top_late_weak = [r for r in evaluated_sorted if str(r.get("outcome_rating")) == "late_weak"][:15]
    tomorrow_prep_list = sorted(evaluated, key=lambda x: (_num(x.get("context_quality_score"), 0.0), _num(x.get("source_score"), 0.0)), reverse=True)[:limit]
    missed_audit = _audit_missed_winners(selection_map, outcome_map, outcome_date, selected_symbols, candidate_symbols, trace_by_symbol, context_by_symbol, min_gain_pct=missed_gain_threshold)

    payload = {
        "ok": True,
        "version": HISTORICAL_REPLAY_SIMULATOR_VERSION,
        "mode": "after_close_3day_context_no_lookahead_tomorrow_prep",
        "requested_date": str(date_value or "").strip(),
        "selection_date": selection_date,
        "outcome_date": outcome_date,
        "clean_only": bool(clean_only),
        "max_candidates": limit,
        "context_days": max(1, min(10, int(context_days or 3))),
        "selection_rows_available": len(selection_map or {}),
        "outcome_rows_available": len(outcome_map or {}),
        "selection_date_debug": selection_debug,
        "outcome_date_debug": outcome_debug,
        "context_date_debug": context_debug,
        "source_versions": {
            "micro_full_scan": (selection_micro_debug or {}).get("version"),
            "fast_lane": (selection_fast_debug or {}).get("version"),
            "context_source_scan": (context_source_debug or {}).get("version"),
        },
        "production_pipeline_reuse": {
            "version": "production_source_reuse_contract_v2s1_2026_06_20",
            "uses_production_source_helpers": True,
            "source_helpers": ["_collect_micro_explosion_full_market_candidates", "_collect_low_float_fast_lane_candidates"],
            "uses_production_sharia_filter": True,
            "uses_live_buy_decision": False,
            "why_not_full_live_decision_ar": "V2S1 هو محاكي يومي بعد الإغلاق، لذلك لا يطلق BUY_NOW ولا يغير Strong/Cautious. V2S2 سيضيف time slices لتشغيل منطق السوق الحي على دقائق تاريخية.",
            "anti_lookahead_ar": "كل الاختيارات مبنية على أيام <= تاريخ الاختيار فقط. جلسة التقييم لا تدخل إلا بعد اكتمال قائمة الغد.",
        },
        "source_counts": {
            "selection_day_micro_candidates_before_sharia": len(selection_micro_rows or []),
            "selection_day_fast_lane_candidates_before_sharia": len(selection_fast_rows or []),
            "context_micro_candidates_before_sharia": len(micro_rows or []),
            "context_fast_lane_candidates_before_sharia": len(fast_rows or []),
            "combined_candidates_after_sharia_policy": len(candidates),
            "selected_for_evaluation": len(selected),
        },
        "micro_explosion_full_market_scan": {
            k: v for k, v in (selection_micro_debug or {}).items()
            if k in {"version", "scanned", "eligible_count", "top_symbols", "seed_match_count", "rule_ar"}
        },
        "fast_lane_scan": {
            k: v for k, v in (selection_fast_debug or {}).items()
            if k in {"version", "scanned", "eligible_count", "top_symbols", "raw_source_count", "rule_ar"}
        },
        "context_source_scan": context_source_debug,
        "sharia_debug": combine_debug,
        "after_close_tomorrow_prep": {
            "version": "after_close_tomorrow_prep_v2s1_daily_context_2026_06_20",
            "selection_date": selection_date,
            "for_outcome_date": outcome_date,
            "prepared_count": len(tomorrow_prep_list),
            "top_tomorrow_watch_count": sum(1 for r in tomorrow_prep_list if r.get("tomorrow_prep_bucket") == "top_tomorrow_close_watch"),
            "watch_needs_confirmation_count": sum(1 for r in tomorrow_prep_list if r.get("tomorrow_prep_bucket") == "tomorrow_watch_needs_premarket_confirmation"),
            "quick_or_pullback_only_count": sum(1 for r in tomorrow_prep_list if r.get("tomorrow_prep_bucket") == "quick_take_profit_or_pullback_only"),
            "tomorrow_prep_list": tomorrow_prep_list[:50],
            "rule_ar": "هذه هي قائمة الغد كما لو أن الأداة حللت إغلاق تاريخ الاختيار وجهزت مرشحين للمراقبة قبل بري ماركت اليوم التالي.",
        },
        "performance_summary": _summary(evaluated),
        "missed_winners_audit": missed_audit,
        "top_winners": top_winners,
        "top_failures": top_failures,
        "late_weak_sample": top_late_weak,
        "anti_lookahead_rule_ar": "اختيارات الأداة مبنية على سياق الأيام حتى تاريخ الاختيار فقط؛ نتائج الجلسة التالية تُستخدم للتقييم فقط بعد اكتمال قائمة الغد.",
        "storage_rule_ar": "V2S1 يستخدم Polygon grouped REST compact فقط ولا يحفظ raw files في Railway/GitHub/SQLite.",
        "timing_limit_note_ar": "V2S1 لا يعرف دقائق البري ماركت/داخل الجلسة. إذا ظهر winner مفقود، V2S2 minute replay سيحدد أول وقت وسعر التقاطه.",
        "next_step_ar": "شغّل V2S1 على عدة أيام. إذا winners الكبيرة ليست في قائمة الغد، نضيف V2S2 minute timing لمعرفة هل الانفجار بدأ قبل/بعد الافتتاح أو فشل منبع يومي.",
    }
    if include_candidates:
        payload["candidates"] = evaluated
    else:
        payload["candidate_sample"] = evaluated_sorted[:20]
    return payload

def historical_replay_status() -> dict[str, Any]:
    return {
        "ok": True,
        "version": HISTORICAL_REPLAY_SIMULATOR_VERSION,
        "endpoint": "/simulator/historical-replay?date=YYYY-MM-DD",
        "aliases": ["/simulator/historical-replay", "/historical-replay", "/replay/historical-market"],
        "purpose_ar": "محاكاة تاريخية بعد الإغلاق: الأداة تشاهد آخر 3 أيام يومية، تجهز قائمة الغد، ثم نقيمها على الجلسة التالية ونراجع الفائزين الذين فاتوا.",
        "safe_mode_ar": "تقييم فقط؛ لا يغير Strong/Cautious ولا السوق الحي ولا يحفظ raw.",
        "recommended_params_ar": "ابدأ max_candidates=40 و context_days=3 و clean_only=true و format=brief، ثم كرر 5-10 أيام.",
        "v2s1_note_ar": "V2S1 يعيد استخدام دوال المصدر الحية مع daily historical adapter. V2S2 لاحقًا يضيف minute time slices للبري ماركت/داخل الجلسة/بعد الإغلاق.",
    }


def format_historical_replay_brief(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return str(payload)
    if not payload.get("ok"):
        return "Historical Replay Simulator\n" + "\n".join([
            f"الحالة: فشل",
            f"السبب: {payload.get('error')}",
            f"التشخيص: {payload.get('selection_date_debug')}",
        ])
    perf = payload.get("performance_summary") or {}
    src = payload.get("source_counts") or {}
    micro = payload.get("micro_explosion_full_market_scan") or {}
    context_debug = payload.get("context_date_debug") or {}
    prep = payload.get("after_close_tomorrow_prep") or {}
    missed = payload.get("missed_winners_audit") or {}
    lines = [
        "Historical Replay Simulator V2S1",
        f"تاريخ الاختيار: {payload.get('selection_date')} → جلسة التقييم: {payload.get('outcome_date')}",
        f"وضع الاختبار: {payload.get('mode')}",
        f"أيام السياق: {', '.join(context_debug.get('context_dates') or [])}",
        "",
        "ملخص الالتقاط:",
        f"- Selection-day Micro scanned: {micro.get('scanned')} | eligible: {micro.get('eligible_count')} | seed: {micro.get('seed_match_count')}",
        f"- سياق 3 أيام قبل الشرعية: Micro {src.get('context_micro_candidates_before_sharia')} | Fast Lane {src.get('context_fast_lane_candidates_before_sharia')}",
        f"- بعد سياسة الشرعية: {src.get('combined_candidates_after_sharia_policy')} | المختار للتقييم: {src.get('selected_for_evaluation')}",
        "",
        "قائمة الغد بعد الإغلاق:",
        f"- إجمالي القائمة: {prep.get('prepared_count')}",
        f"- مراقبة لصيقة: {prep.get('top_tomorrow_watch_count')} | يحتاج تأكيد: {prep.get('watch_needs_confirmation_count')} | خطفة/انتظار Pullback: {prep.get('quick_or_pullback_only_count')}",
        "",
        "نتيجة الجلسة التالية:",
        f"- +5%: {perf.get('hit_5pct_count')} ({perf.get('hit_5pct_pct')}%)",
        f"- +10%: {perf.get('hit_10pct_count')} ({perf.get('hit_10pct_pct')}%)",
        f"- +20%: {perf.get('hit_20pct_count')} ({perf.get('hit_20pct_pct')}%)",
        f"- فشل/خطر: {perf.get('danger_fail_count')} ({perf.get('danger_fail_pct')}%)",
        f"- متوسط أعلى صعود: {perf.get('avg_next_session_max_gain_pct')}% | وسيط الصعود: {perf.get('median_next_session_max_gain_pct')}%",
        f"- متوسط أسوأ هبوط: {perf.get('avg_next_session_worst_drawdown_pct')}% | وسيط الهبوط: {perf.get('median_next_session_worst_drawdown_pct')}%",
        "",
        f"التقييم: {perf.get('assessment_ar')}",
        "",
        "Missed Winners Audit:",
        f"- انفجارات +20% في جلسة التقييم: {missed.get('all_explosion_counts', {}).get('20pct')} | التقطت القائمة منها: {missed.get('captured_explosion_counts', {}).get('20pct')} | capture rate: {missed.get('capture_rate_20pct')}%",
        f"- فوق حد {missed.get('min_gain_pct')}%: إجمالي {missed.get('winners_over_threshold_count')} | ملتقط {missed.get('captured_over_threshold_count')} | مفقود {missed.get('missed_over_threshold_count')}",
        f"- أسباب الفوات: {missed.get('missed_reason_counts')}",
        "",
        "أفضل الفائزين من اختيارات الأداة:",
    ]
    for r in (payload.get("top_winners") or [])[:10]:
        lines.append(f"- {r.get('symbol')}: max {r.get('next_session_max_gain_pct')}%, worst {r.get('next_session_worst_drawdown_pct')}%, prep {r.get('tomorrow_prep_label_ar')}, label {r.get('outcome_label_ar')}")
    lines += ["", "أكبر الفائزين الذين فاتوا/أو لم يدخلوا القائمة:"]
    for r in (missed.get("top_missed_winners") or [])[:10]:
        lines.append(f"- {r.get('symbol')}: max {r.get('next_session_max_gain_pct')}%, reason {r.get('miss_reason_ar')}")
    lines += ["", "أضعف/أخطر المرشحين:"]
    for r in (payload.get("top_failures") or [])[:8]:
        lines.append(f"- {r.get('symbol')}: max {r.get('next_session_max_gain_pct')}%, worst {r.get('next_session_worst_drawdown_pct')}%, label {r.get('outcome_label_ar')}")
    lines += ["", str(payload.get("anti_lookahead_rule_ar") or ""), str(payload.get("timing_limit_note_ar") or ""), str(payload.get("storage_rule_ar") or "")]
    return "\n".join(lines)
