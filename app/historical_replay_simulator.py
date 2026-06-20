"""Historical Replay Simulator V2S.

Purpose
-------
Run an isolated, no-lookahead replay for a past market date:
1) let the current source/Low-Float/Micro-Explosion logic choose candidates using
   only the selected day's grouped daily data;
2) evaluate those candidates on the next trading session;
3) return compact metrics for learning and quality calibration.

Safety rules
------------
- This module does not alter live Strong/Cautious/BUY_NOW logic.
- It does not store raw Polygon files in SQLite/GitHub/Railway volume.
- It uses compact Polygon grouped daily REST data only in V1/V2S.
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

HISTORICAL_REPLAY_SIMULATOR_VERSION = "historical_replay_simulator_v2s_daily_next_session_2026_06_20"


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
            "sharia_status": status,
            "sharia_label": sharia.get("label"),
            "sharia_reason": sharia.get("reason"),
            "sharia_is_gray": bool(sharia.get("is_gray")),
            "sharia_blocked": bool(sharia.get("should_block")),
        }
        if layer not in rec["source_layers"]:
            rec["source_layers"].append(layer)
        rec["source_score"] = max(_num(rec.get("source_score"), 0.0), _candidate_score(row))
        reasons = list(metrics.get("micro_explosion_reasons_ar") or metrics.get("low_float_fast_lane_reasons") or row.get("reasons") or [])
        for r in reasons:
            txt = _s(r)
            if txt and txt not in rec["reasons_ar"]:
                rec["reasons_ar"].append(txt)
        # Keep the metrics from the highest-scoring row.
        if _candidate_score(row) >= _num(rec.get("source_score"), 0.0) or not rec.get("selection_metrics"):
            rec["selection_metrics"] = metrics
        by_symbol[sym] = rec

    for r in micro_rows or []:
        add(r, "micro_explosion_full_market_v2r2")
    for r in fast_rows or []:
        add(r, "low_float_fast_lane_v2q")

    out = sorted(by_symbol.values(), key=lambda x: _num(x.get("source_score"), 0.0), reverse=True)
    return out, {"sharia_counts": sharia_counts, "excluded_by_sharia_sample": sorted(excluded, key=lambda x: _num(x.get("score"), 0.0), reverse=True)[:40]}


def _evaluate_candidate(c: dict, selection_grouped: dict, outcome_grouped: dict, outcome_date: str) -> dict:
    sym = c.get("symbol")
    sel = selection_grouped.get(sym) or {}
    nxt = outcome_grouped.get(sym) or {}
    metrics = c.get("selection_metrics") or {}
    sel_price = _num(metrics.get("price"), 0.0) or _num(sel.get("price"), 0.0) or _num(sel.get("close"), 0.0)
    sel_open = _num(sel.get("open"), 0.0)
    sel_high = _num(sel.get("high"), 0.0)
    sel_low = _num(sel.get("low"), 0.0)
    sel_vol = _num(sel.get("volume"), 0.0)
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
    selection_change = _num(metrics.get("change_pct"), _num(metrics.get("day_change_pct"), 0.0))
    if abs(selection_change) <= 1.5:
        selection_change *= 100.0

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

    return {
        **c,
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
    micro_rows, micro_debug = _collect_micro_explosion_full_market_candidates(selection_map, phase_detail="historical_after_close_v2s")
    fast_rows, fast_debug = _collect_low_float_fast_lane_candidates(selection_map, phase_detail="historical_after_close_v2s")
    candidates, combine_debug = _combine_source_candidates(micro_rows, fast_rows, clean_only=bool(clean_only))
    limit = max(5, min(120, int(max_candidates or 40)))
    selected = candidates[:limit]
    evaluated = [_evaluate_candidate(c, selection_map, outcome_map, outcome_date) for c in selected]
    evaluated_sorted = sorted(evaluated, key=lambda x: _num(x.get("next_session_max_gain_pct"), 0.0), reverse=True)

    top_winners = evaluated_sorted[:15]
    top_failures = sorted(evaluated, key=lambda x: (_num(x.get("next_session_worst_drawdown_pct"), 0.0), -_num(x.get("next_session_max_gain_pct"), 0.0)))[:15]
    top_late_weak = [r for r in evaluated_sorted if str(r.get("outcome_rating")) == "late_weak"][:15]

    payload = {
        "ok": True,
        "version": HISTORICAL_REPLAY_SIMULATOR_VERSION,
        "mode": "after_close_daily_grouped_no_lookahead",
        "requested_date": str(date_value or "").strip(),
        "selection_date": selection_date,
        "outcome_date": outcome_date,
        "clean_only": bool(clean_only),
        "max_candidates": limit,
        "selection_rows_available": len(selection_map or {}),
        "outcome_rows_available": len(outcome_map or {}),
        "selection_date_debug": selection_debug,
        "outcome_date_debug": outcome_debug,
        "source_versions": {
            "micro_full_scan": (micro_debug or {}).get("version"),
            "fast_lane": (fast_debug or {}).get("version"),
        },
        "source_counts": {
            "micro_candidates_before_sharia": len(micro_rows or []),
            "fast_lane_candidates_before_sharia": len(fast_rows or []),
            "combined_candidates_after_sharia_policy": len(candidates),
            "selected_for_evaluation": len(selected),
        },
        "micro_explosion_full_market_scan": {
            k: v for k, v in (micro_debug or {}).items()
            if k in {"version", "scanned", "eligible_count", "top_symbols", "seed_match_count", "rule_ar"}
        },
        "fast_lane_scan": {
            k: v for k, v in (fast_debug or {}).items()
            if k in {"version", "scanned", "eligible_count", "top_symbols", "raw_source_count", "rule_ar"}
        },
        "sharia_debug": combine_debug,
        "performance_summary": _summary(evaluated),
        "top_winners": top_winners,
        "top_failures": top_failures,
        "late_weak_sample": top_late_weak,
        "anti_lookahead_rule_ar": "اختيارات الأداة مبنية على grouped تاريخ الاختيار فقط؛ نتائج الجلسة التالية تُستخدم للتقييم فقط بعد اكتمال الاختيار.",
        "storage_rule_ar": "V2S يستخدم Polygon grouped REST compact فقط ولا يحفظ raw files في Railway/GitHub/SQLite.",
        "next_step_ar": "شغّل نفس المحاكي على 5-10 أيام. إذا الالتقاط كثير والفائزون قليلون نضبط ترتيب Low-Float؛ إذا الالتقاط يسبق الفائزين ننتقل لتخفيف القيود وتحسين التعلم.",
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
        "purpose_ar": "محاكاة يوم تاريخي: الأداة تختار من بيانات ذلك اليوم فقط، ثم نقيمها على الجلسة التالية.",
        "safe_mode_ar": "تقييم فقط؛ لا يغير Strong/Cautious ولا السوق الحي ولا يحفظ raw.",
        "recommended_params_ar": "ابدأ max_candidates=40 و clean_only=true و format=brief، ثم كرر 5-10 أيام.",
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
    lines = [
        "Historical Replay Simulator V2S",
        f"تاريخ الاختيار: {payload.get('selection_date')} → جلسة التقييم: {payload.get('outcome_date')}",
        f"وضع الاختبار: {payload.get('mode')}",
        "",
        "ملخص الالتقاط:",
        f"- Micro scanned: {micro.get('scanned')} | eligible: {micro.get('eligible_count')} | seed: {micro.get('seed_match_count')}",
        f"- قبل الشرعية: Micro {src.get('micro_candidates_before_sharia')} | Fast Lane {src.get('fast_lane_candidates_before_sharia')}",
        f"- بعد سياسة الشرعية: {src.get('combined_candidates_after_sharia_policy')} | المختار للتقييم: {src.get('selected_for_evaluation')}",
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
        "أفضل الفائزين:",
    ]
    for r in (payload.get("top_winners") or [])[:10]:
        lines.append(f"- {r.get('symbol')}: max {r.get('next_session_max_gain_pct')}%, worst {r.get('next_session_worst_drawdown_pct')}%, label {r.get('outcome_label_ar')}")
    lines += ["", "أضعف/أخطر المرشحين:"]
    for r in (payload.get("top_failures") or [])[:8]:
        lines.append(f"- {r.get('symbol')}: max {r.get('next_session_max_gain_pct')}%, worst {r.get('next_session_worst_drawdown_pct')}%, label {r.get('outcome_label_ar')}")
    lines += ["", str(payload.get("anti_lookahead_rule_ar") or ""), str(payload.get("storage_rule_ar") or "")]
    return "\n".join(lines)
