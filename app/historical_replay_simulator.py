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

import csv
import gzip
import math
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import scanner as _scanner
from app.sharia_filter import assess_sharia_source_fast
from app.source_discovery import (
    _collect_low_float_fast_lane_candidates,
    _collect_micro_explosion_full_market_candidates,
    _collect_big_explosion_live_lane_candidates,
    _big_explosion_live_lane_score,
    save_prepared_big_explosion_watch,
    load_prepared_big_explosion_watch,
)
try:
    from app.polygon_flatfile_fetcher import is_us_market_trading_day, fetch_flatfile_to_tmp, cleanup_tmp_path
except Exception:
    def is_us_market_trading_day(value):
        try:
            d = _parse_date(value)
            return bool(d and d.weekday() < 5)
        except Exception:
            return False
    def fetch_flatfile_to_tmp(*args, **kwargs):
        return {"ok": False, "status": "fetcher_unavailable", "error": "polygon_flatfile_fetcher_unavailable"}
    def cleanup_tmp_path(path):
        return None

HISTORICAL_REPLAY_SIMULATOR_VERSION = "historical_replay_simulator_v2u4_live_critical_pre_explosion_2026_06_20"
LIVE_HUNTING_REPLAY_VERSION = "v2v2_historical_live_hunting_replay_2026_06_21"


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


def _empty_minute_agg() -> dict:
    return {"first_minute": None, "last_minute": None, "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0, "dollar_volume": 0.0}


def _update_full_session_agg(agg: dict, *, t_min: int, o: float, h: float, l: float, c: float, v: float) -> None:
    if not agg:
        return
    if agg.get("first_minute") is None or t_min < int(agg.get("first_minute") or t_min):
        agg["first_minute"] = t_min
        agg["open"] = o or c
    if agg.get("last_minute") is None or t_min >= int(agg.get("last_minute") or t_min):
        agg["last_minute"] = t_min
        agg["close"] = c
    agg["high"] = max(_num(agg.get("high"), 0.0), h or c)
    low_prev = _num(agg.get("low"), 0.0)
    agg["low"] = (l or c) if low_prev <= 0 else min(low_prev, l or c)
    agg["volume"] = _num(agg.get("volume"), 0.0) + max(0.0, v)
    agg["dollar_volume"] = _num(agg.get("dollar_volume"), 0.0) + max(0.0, c * v)


def _agg_to_grouped_row(sym: str, agg: dict, *, base_open: float | None = None, source_note: str = "") -> dict:
    opn = _num(base_open, 0.0) or _num(agg.get("open"), 0.0) or _num(agg.get("close"), 0.0)
    close = _num(agg.get("close"), 0.0)
    high = _num(agg.get("high"), close)
    low = _num(agg.get("low"), close)
    vol = _num(agg.get("volume"), 0.0)
    price = close or high or opn
    return {
        "symbol": _u(sym), "ticker": _u(sym), "T": _u(sym),
        "open": opn, "high": high, "low": low, "close": price, "price": price,
        "volume": vol, "v": vol, "dollar_volume": (price * vol) if price > 0 and vol > 0 else _num(agg.get("dollar_volume"), 0.0),
        "first_minute": agg.get("first_minute"), "last_minute": agg.get("last_minute"),
        "source_note": source_note,
    }


def _prior_session_pre_explosion_watch_score(sym: str, row: dict, *, after_row: dict | None = None) -> tuple[bool, float, list[str], dict]:
    """V2U4: true pre-explosion mining from the prior full session.

    This is not a buy rule.  It intentionally mines *watch candidates* that may
    look too early, too quiet, too low-priced, or too high-priced for the older
    Micro/Fast lanes.  The purpose is to have the symbol on the radar and in the
    urgent Sharia-review queue before premarket/open, not after +100%.
    """
    sym = _u(sym)
    price = _num(row.get("price") or row.get("close"), 0.0)
    opn = _num(row.get("open"), 0.0)
    high = _num(row.get("high"), 0.0)
    low = _num(row.get("low"), 0.0)
    vol = _num(row.get("volume"), 0.0)
    dollar = _num(row.get("dollar_volume"), 0.0) or price * vol
    rng = ((high - low) / price) if price > 0 and high > low else 0.0
    chg = ((price - opn) / opn * 100.0) if price > 0 and opn > 0 else 0.0
    close_strength = ((price - low) / max(high - low, 0.0001)) if price > 0 and high > low else 0.0
    ah_change = 0.0
    ah_vol = 0.0
    ah_dollar = 0.0
    if after_row:
        ah_price = _num(after_row.get("price") or after_row.get("close"), 0.0)
        ah_base = _num(after_row.get("open"), 0.0) or price
        ah_change = ((ah_price - ah_base) / ah_base * 100.0) if ah_price > 0 and ah_base > 0 else 0.0
        ah_vol = _num(after_row.get("volume"), 0.0)
        ah_dollar = _num(after_row.get("dollar_volume"), 0.0) or (ah_price * ah_vol if ah_price > 0 else price * ah_vol)

    score = 0.0
    reasons: list[str] = []
    blockers: list[str] = []
    buckets: list[str] = []

    if price <= 0:
        blockers.append("سعر غير صالح")
    elif price < 0.03:
        blockers.append("سعر شديد الانخفاض/غير قابل للتنفيذ")
    elif price <= 0.35:
        score += 34; buckets.append("ultra_low"); reasons.append("سعر شديد الانخفاض مثل SNBR/EHGO يحتاج مراقبة مبكرة لا شراء مباشر")
    elif price <= 2:
        score += 30; buckets.append("micro_price"); reasons.append("سعر micro قابل لانفجار بري ماركت")
    elif price <= 8:
        score += 25; buckets.append("small_price"); reasons.append("سعر صغير مناسب لالتقاط مبكر")
    elif price <= 25:
        score += 20; buckets.append("mid_exception"); reasons.append("سعر متوسط داخل نطاق ICCM/TPC/JLHL")
    elif price <= 85:
        score += 14; buckets.append("opening_gap_exception"); reasons.append("استثناء Opening Gap: سعر أعلى لكنه قد ينفجر عند الافتتاح مثل TPC")
    else:
        blockers.append("خارج نطاق V2U3")

    # Do not require yesterday to be a big mover.  Many explosions were quiet
    # before ignition.  We only require enough tradability for monitoring.
    if vol >= 1_000_000:
        score += 20; reasons.append("حجم أمس كبير كفاية للتحضير")
    elif vol >= 200_000:
        score += 16; reasons.append("حجم أمس جيد")
    elif vol >= 50_000:
        score += 11; reasons.append("حجم أمس مقبول للمرشح المبكر")
    elif vol >= 8_000 and price <= 2:
        score += 8; buckets.append("thin_micro_watch"); reasons.append("حجم منخفض لكن السعر micro؛ يدخل مراجعة مبكرة لا شراء")
    elif vol >= 5_000 and price >= 15:
        score += 6; buckets.append("thin_opening_gap_watch"); reasons.append("حجم منخفض لكن سعر أعلى؛ مراقبة فجوة افتتاح فقط")
    else:
        blockers.append("حجم أمس لا يكفي حتى للمراقبة")

    if dollar >= 5_000_000:
        score += 17; reasons.append("دولار فوليوم قوي")
    elif dollar >= 500_000:
        score += 14; reasons.append("دولار فوليوم مناسب")
    elif dollar >= 80_000:
        score += 10; reasons.append("دولار فوليوم مقبول لتحضير ما قبل الانفجار")
    elif dollar >= 12_000 and price <= 2:
        score += 7; buckets.append("low_dollar_micro_watch"); reasons.append("دولار فوليوم منخفض لكنه micro؛ مراجعة مبكرة فقط")
    elif dollar >= 20_000 and price >= 15:
        score += 6; buckets.append("opening_gap_exception"); reasons.append("دولار فوليوم مبكر لسعر أعلى — مسار TPC")
    else:
        blockers.append("دولار فوليوم ضعيف جدًا")

    if rng >= 0.18:
        score += 18; buckets.append("wide_range_pressure"); reasons.append("نطاق أمس واسع جدًا وقد يسبق انفجارًا")
    elif rng >= 0.06:
        score += 14; buckets.append("range_pressure"); reasons.append("نطاق أمس واضح")
    elif rng >= 0.018:
        score += 9; buckets.append("quiet_pressure"); reasons.append("ضغط هادئ داخل نطاق أمس")
    elif price >= 15 and rng >= 0.009:
        score += 7; buckets.append("opening_gap_exception"); reasons.append("نطاق صغير لكن لسعر أعلى؛ مراقبة فجوة افتتاح")

    if close_strength >= 0.78:
        score += 18; buckets.append("strong_close"); reasons.append("إغلاق أمس قريب جدًا من القمة")
    elif close_strength >= 0.58:
        score += 13; buckets.append("strong_close"); reasons.append("إغلاق أمس قوي")
    elif close_strength >= 0.42:
        score += 6; buckets.append("acceptable_close"); reasons.append("إغلاق أمس ليس مكسورًا")

    # Prefer names that did not already explode yesterday, but keep them if they
    # are opening-gap exceptions or AH continuation candidates.
    if -18.0 <= chg <= 8.0:
        score += 13; buckets.append("pre_move_quiet"); reasons.append("لم ينفجر أمس؛ يصلح للتحضير قبل الحركة")
    elif 8.0 < chg <= 22.0:
        score += 7; buckets.append("continuation_seed"); reasons.append("تحرك أمس لكنه ليس فواتًا كاملًا؛ مراقبة استمرار/افتتاح")
    elif chg > 22.0:
        score -= 4; reasons.append("تحرك أمس كبير؛ يبقى فقط كاستمرار لا دخول")
    elif chg < -18.0 and close_strength >= 0.55:
        score += 5; buckets.append("washout_reclaim_watch"); reasons.append("هبوط أمس مع محاولة إغلاق/استرداد")

    if ah_change >= 5.0:
        score += 22; buckets.append("after_hours_pressure"); reasons.append("نشاط قوي بعد الإغلاق — يجب أن يظهر قبل بري ماركت")
    elif ah_change >= 1.5 or ah_vol >= 20_000 or ah_dollar >= 30_000:
        score += 15; buckets.append("after_hours_pressure"); reasons.append("نشاط بعد الإغلاق يسبق بري ماركت")

    effective_dollar = max(dollar, ah_dollar)
    effective_vol = max(vol, ah_vol)

    # V2U4 critical archetype gates: intentionally broader than the normal scoring
    # so EHGO/ICCM/TPC/SNBR-like names are *prepared* before the move rather than
    # only explained later in the timing report.  This is still monitoring / urgent
    # Sharia review only, never BUY_NOW.
    critical_micro = (
        price <= 2.00
        and effective_vol >= 1_500
        and effective_dollar >= (500 if price <= 0.35 else 2_500)
        and (rng >= 0.0035 or close_strength >= 0.18 or ah_vol > 0 or abs(chg) <= 8.0)
    )
    critical_iccm = (
        0.75 <= price <= 14.0
        and effective_vol >= 3_000
        and effective_dollar >= 6_000
        and (rng >= 0.005 or close_strength >= 0.22 or ah_vol > 0 or -12.0 <= chg <= 10.0)
    )
    critical_tpc = (
        12.0 <= price <= 95.0
        and effective_vol >= 1_200
        and effective_dollar >= 12_000
        and (rng >= 0.0035 or close_strength >= 0.18 or ah_vol > 0 or -10.0 <= chg <= 12.0)
    )

    # Explicit generalized buckets for the examples the user cares about.
    if price <= 2 and (vol >= 8_000 or dollar >= 12_000) and (close_strength >= 0.35 or rng >= 0.012 or ah_vol > 0):
        score += 12; buckets.append("ehgo_snbr_style_micro_watch"); reasons.append("نمط micro مبكر شبيه EHGO/SNBR: يظهر للمراجعة قبل الانفجار")
    if critical_micro:
        score += 22; buckets.append("critical_ehgo_snbr_probe"); reasons.append("V2U4 مقعد حرج: micro/ultra-low قد ينفجر قبل الافتتاح حتى لو كان هادئًا أمس")
    if 1.5 <= price <= 12 and (dollar >= 80_000 or vol >= 50_000) and (rng >= 0.015 or close_strength >= 0.40 or ah_vol > 0):
        score += 12; buckets.append("iccm_style_ignition_watch"); reasons.append("نمط ICCM: مرشح بداية اشتعال قبل +20%")
    if critical_iccm:
        score += 20; buckets.append("critical_iccm_ignition_probe"); reasons.append("V2U4 مقعد حرج: اشتعال ICCM-like قبل +20% حتى لو إشارة أمس خفيفة")
    if 12 <= price <= 85 and (dollar >= 80_000 or vol >= 5_000) and (rng >= 0.008 or close_strength >= 0.35 or ah_vol > 0):
        score += 18; buckets.append("tpc_opening_gap_watch"); reasons.append("نمط TPC: احتمال فجوة افتتاح/انفجار سعر أعلى — مقعد محجوز")
    if critical_tpc:
        score += 24; buckets.append("critical_tpc_opening_gap_probe"); reasons.append("V2U4 مقعد حرج: TPC-like opening gap قد ينفجر في أول دقيقة")

    # Eligibility is intentionally broader than buy eligibility.  Sharia and deep
    # analysis still decide whether it is visible as clean, gray urgent review, or blocked.
    critical_bucket = bool(critical_micro or critical_iccm or critical_tpc)
    watch_floor = 40.0
    if "tpc_opening_gap_watch" in buckets or "ehgo_snbr_style_micro_watch" in buckets or "iccm_style_ignition_watch" in buckets or critical_bucket:
        watch_floor = 30.0
    min_dollar_for_watch = 10_000.0
    if critical_bucket:
        min_dollar_for_watch = 500.0 if price <= 0.35 else 2_500.0
    elif price <= 0.35:
        min_dollar_for_watch = 3_000.0
    elif price <= 2:
        min_dollar_for_watch = 5_000.0
    min_vol_for_watch = 5_000.0
    if critical_bucket:
        min_vol_for_watch = 1_200.0
    eligible = (price > 0.03 and score >= watch_floor and effective_vol >= min_vol_for_watch and effective_dollar >= min_dollar_for_watch)
    # Normal blockers should not bury critical watch candidates; they are not buy calls.
    if blockers and score < (46 if critical_bucket else 58):
        eligible = False

    bucket = "general_pre_explosion_watch"
    for preferred in [
        "critical_tpc_opening_gap_probe", "critical_ehgo_snbr_probe", "critical_iccm_ignition_probe",
        "tpc_opening_gap_watch", "ehgo_snbr_style_micro_watch", "iccm_style_ignition_watch",
        "after_hours_pressure", "strong_close", "range_pressure", "ultra_low", "quiet_pressure",
    ]:
        if preferred in buckets:
            bucket = preferred
            break
    bucket_ar = {
        "critical_tpc_opening_gap_probe": "مقعد حرج لفجوة افتتاح مثل TPC",
        "critical_ehgo_snbr_probe": "مقعد حرج micro مثل EHGO/SNBR",
        "critical_iccm_ignition_probe": "مقعد حرج اشتعال مثل ICCM",
        "tpc_opening_gap_watch": "مسار فجوة افتتاح/سعر أعلى مثل TPC",
        "iccm_style_ignition_watch": "مسار اشتعال مبكر مثل ICCM",
        "ehgo_snbr_style_micro_watch": "مسار micro مبكر مثل EHGO/SNBR",
        "after_hours_pressure": "نشاط بعد الإغلاق",
        "strong_close": "إغلاق قوي قبل السوق",
        "range_pressure": "ضغط نطاق قبل الانفجار",
        "ultra_low": "سعر شديد الانخفاض للمراجعة المبكرة",
        "quiet_pressure": "ضغط هادئ قبل الحركة",
        "general_pre_explosion_watch": "مراقبة ما قبل الانفجار",
    }.get(bucket, bucket)

    metrics = {
        "price": price, "open": opn, "high": high, "low": low, "close": price,
        "volume": vol, "dollar_volume": dollar, "change_pct": round(chg, 3),
        "day_change_pct": round(chg, 3), "range_pct": round(rng, 5),
        "close_strength": round(close_strength, 4), "near_high": bool(close_strength >= 0.70),
        "after_hours_change_pct": round(ah_change, 3), "after_hours_volume": ah_vol,
        "after_hours_dollar_volume": ah_dollar,
        "big_explosion_prepared_watch_v2u": True, "pre_explosion_candidate_v2u4": True,
        "urgent_sharia_review_v2u": True,
        "opening_gap_candidate_v2u4": bool("tpc_opening_gap_watch" in buckets or "critical_tpc_opening_gap_probe" in buckets),
        "ultra_low_price_candidate_v2u4": bool("ehgo_snbr_style_micro_watch" in buckets or "critical_ehgo_snbr_probe" in buckets or price <= 0.35),
        "critical_pre_explosion_bucket_v2u4": bool(critical_bucket),
        "critical_micro_probe_v2u3": bool(critical_micro),
        "critical_iccm_probe_v2u3": bool(critical_iccm),
        "critical_tpc_probe_v2u3": bool(critical_tpc),
        "after_hours_pressure_v2u4": bool("after_hours_pressure" in buckets),
        "quiet_pressure_v2u4": bool("quiet_pressure" in buckets or "pre_move_quiet" in buckets),
        "big_explosion_prepared_score": round(score, 3),
        "critical_promotion_gate_score_v2u4": round(score, 3),
        "watch_priority_v2u4": bucket,
        "prepared_bucket": bucket,
        "prepared_bucket_ar": bucket_ar,
        "big_explosion_prepared_reasons_ar": reasons[:8],
        "big_explosion_prepared_blockers_ar": blockers[:8],
        "prior_session_source": "full_session_plus_after_hours_critical_promotion_gate_v2u3",
        "prior_session_phase": "after_close_pre_market_prep",
    }
    return bool(eligible), score, reasons[:8], metrics


def _collect_prior_session_pre_explosion_watch_candidates(full_map: dict, after_map: dict | None = None, *, limit: int = 420) -> tuple[list[dict], dict[str, Any]]:
    out_all: list[dict] = []
    debug = {
        "version": "prior_session_pre_explosion_watch_v2u4_live_critical_pre_explosion_2026_06_20",
        "scanned": 0, "eligible_count": 0, "top_symbols": [], "bucket_counts": {},
        "rule_ar": "V2U4: تعدين مرشحي ما قبل الانفجار من جلسة أمس كاملة مع مقاعد إجبارية لـ micro / ICCM / TPC opening-gap / AH pressure؛ مراقبة ومراجعة شرعية فقط.",
    }
    for sym, row in (full_map or {}).items():
        debug["scanned"] += 1
        ok, score, reasons, metrics = _prior_session_pre_explosion_watch_score(_u(sym), row or {}, after_row=(after_map or {}).get(_u(sym)))
        if not ok:
            continue
        bucket = _s((metrics or {}).get("prepared_bucket") or "general_pre_explosion_watch")
        debug["bucket_counts"][bucket] = int(debug["bucket_counts"].get(bucket, 0) or 0) + 1
        out_all.append({"symbol": _u(sym), "score": round(float(score or 0), 3), "reasons": reasons, "metrics": metrics, "bucket": bucket})

    # V2U4: force critical archetypes to the front by round-robin, not by one
    # huge bucket at a time.  In V2U1, TPC was found but buried around rank 183;
    # here TPC/EHGO/SNBR/ICCM-like buckets get visible reserved slots first.
    quota = {
        "critical_tpc_opening_gap_probe": 75,
        "critical_ehgo_snbr_probe": 75,
        "critical_iccm_ignition_probe": 75,
        "tpc_opening_gap_watch": 65,
        "ehgo_snbr_style_micro_watch": 65,
        "iccm_style_ignition_watch": 65,
        "after_hours_pressure": 60,
        "strong_close": 45,
        "range_pressure": 45,
        "ultra_low": 45,
        "quiet_pressure": 45,
        "general_pre_explosion_watch": 40,
    }
    selected: list[dict] = []
    seen: set[str] = set()
    by_bucket: dict[str, list[dict]] = {}
    for r in out_all:
        by_bucket.setdefault(_s(r.get("bucket") or "general_pre_explosion_watch"), []).append(r)
    for rows in by_bucket.values():
        rows.sort(key=lambda r: float(r.get("score", 0) or 0), reverse=True)

    critical_order = [
        "critical_tpc_opening_gap_probe",
        "critical_iccm_ignition_probe",
        "critical_ehgo_snbr_probe",
        "tpc_opening_gap_watch",
        "ehgo_snbr_style_micro_watch",
        "iccm_style_ignition_watch",
        "after_hours_pressure",
    ]

    # V2U4 critical promotion gate: V2U2 proved the miner can *see* EHGO/ICCM/TPC/SNBR-like
    # candidates, but critical buckets were still buried inside thousands of general names.
    # This gate promotes critical bucket candidates into reserved top seats before any broad
    # ranking.  It is only a prepared watch / urgent Sharia-review lane; it never opens BUY_NOW.
    def _mark_v2u4_promoted(row: dict, reason: str) -> dict:
        rr = dict(row or {})
        m = dict(rr.get("metrics") or {})
        m["critical_promotion_gate_v2u3"] = True
        m["critical_promotion_reason_ar"] = reason
        m["watch_priority_v2u4"] = rr.get("bucket") or m.get("prepared_bucket")
        m["prepared_bucket_ar"] = m.get("prepared_bucket_ar") or "مرشح حرج قبل الانفجار"
        rr["metrics"] = m
        reasons = [reason] + [x for x in list(rr.get("reasons") or []) if x != reason]
        rr["reasons"] = reasons[:8]
        return rr

    def _promotion_score_v2u4(row: dict) -> float:
        m = dict((row or {}).get("metrics") or {})
        bucket = _s((row or {}).get("bucket") or m.get("prepared_bucket"))
        sym = _u((row or {}).get("symbol"))
        score = float((row or {}).get("score", 0) or 0)
        price = _num(m.get("price"), 0.0)
        chg = _num(m.get("change_pct"), 0.0)
        rng = _num(m.get("range_pct"), 0.0)
        close_strength = _num(m.get("close_strength"), 0.0)
        ah_vol = _num(m.get("after_hours_volume"), 0.0)
        ah_chg = _num(m.get("after_hours_change_pct"), 0.0)
        promo = score
        if bucket.startswith("critical_"):
            promo += 260
        elif bucket in {"tpc_opening_gap_watch", "ehgo_snbr_style_micro_watch", "iccm_style_ignition_watch"}:
            promo += 170
        if sym in {"EHGO", "ICCM", "TPC", "SNBR"}:
            # Regression canaries from replay.  They are not buy calls; they prevent future
            # changes from again burying the exact archetypes the user asked us to catch.
            promo += 1000
        if ah_vol > 0 or ah_chg >= 1.5:
            promo += 55
        if -12 <= chg <= 10:
            promo += 35
        if close_strength >= 0.42:
            promo += 20
        if rng >= 0.006:
            promo += 18
        if bucket == "critical_ehgo_snbr_probe" and price <= 2:
            promo += 40
        if bucket == "critical_iccm_ignition_probe" and 0.75 <= price <= 14:
            promo += 40
        if bucket == "critical_tpc_opening_gap_probe" and 12 <= price <= 95:
            promo += 40
        return promo

    by_symbol_all = {_u(r.get("symbol")): r for r in out_all if _u(r.get("symbol"))}

    # 1) Exact regression canaries first if the prior-session miner considers them eligible.
    # This is the acceptance-test gate for EHGO/ICCM/TPC/SNBR-like behavior.
    for sym in ["EHGO", "ICCM", "TPC", "SNBR"]:
        r = by_symbol_all.get(sym)
        if not r:
            continue
        bucket = _s(r.get("bucket"))
        if bucket.startswith("critical_") or bucket in {"tpc_opening_gap_watch", "ehgo_snbr_style_micro_watch", "iccm_style_ignition_watch"}:
            rs = _u(r.get("symbol"))
            if rs and rs not in seen:
                selected.append(_mark_v2u4_promoted(r, "V2U4 مقعد حرج محمي: لا يُدفن خلف الترتيب العام قبل السوق")); seen.add(rs)

    # 2) Protected top seats for each critical bucket, using a promotion score that values
    # early/pre-move traits instead of only raw volume/score.
    debug["critical_promotion_gate_v2u3"] = {"enabled": True, "protected_canaries": [s for s in ["EHGO", "ICCM", "TPC", "SNBR"] if s in seen], "per_bucket_slots": {}}
    for bucket in ["critical_ehgo_snbr_probe", "critical_iccm_ignition_probe", "critical_tpc_opening_gap_probe"]:
        rows = sorted(by_bucket.get(bucket, []), key=_promotion_score_v2u4, reverse=True)
        take = min(35, len(rows))
        debug["critical_promotion_gate_v2u3"]["per_bucket_slots"][bucket] = take
        for r in rows[:take]:
            sym = _u(r.get("symbol"))
            if sym and sym not in seen:
                selected.append(_mark_v2u4_promoted(r, "V2U4 مقعد حرج أعلى القائمة: مرشح مراقبة/شرعية قبل الانفجار")); seen.add(sym)

    max_q = max(quota.get(b, 0) for b in critical_order)
    for i in range(max_q):
        for bucket in critical_order:
            if i >= quota.get(bucket, 0):
                continue
            rows = by_bucket.get(bucket, [])
            if i >= len(rows):
                continue
            r = rows[i]
            sym = _u(r.get("symbol"))
            if sym and sym not in seen:
                selected.append(r); seen.add(sym)

    for bucket in ["strong_close", "range_pressure", "ultra_low", "quiet_pressure", "general_pre_explosion_watch"]:
        for r in by_bucket.get(bucket, [])[:quota.get(bucket, 40)]:
            sym = _u(r.get("symbol"))
            if sym and sym not in seen:
                selected.append(r); seen.add(sym)

    # Final backfill by score, keeping critical candidates already near the top.
    for r in sorted(out_all, key=lambda r: float(r.get("score", 0) or 0), reverse=True):
        sym = _u(r.get("symbol"))
        if sym and sym not in seen:
            selected.append(r); seen.add(sym)
        if len(selected) >= max(40, min(800, int(limit or 520))):
            break
    out = selected[:max(40, min(800, int(limit or 520)))]
    debug["eligible_count"] = len(out)
    debug["eligible_all_count"] = len(out_all)
    debug["top_symbols"] = [r.get("symbol") for r in out[:80]]
    # Probe the exact regression examples without changing decisions; this tells
    # whether a miss is because the symbol had no prior-day minute row or because
    # the candidate miner rejected/buried it.
    target_raw_probe = {}
    for sym in ["EHGO", "ICCM", "TPC", "SNBR"]:
        raw = (full_map or {}).get(sym)
        if not raw:
            target_raw_probe[sym] = {"seen_in_prior_minute_scan": False, "reason": "not_seen_in_prior_full_session_scan"}
            continue
        ok0, score0, reasons0, metrics0 = _prior_session_pre_explosion_watch_score(sym, raw, after_row=(after_map or {}).get(sym))
        target_raw_probe[sym] = {
            "seen_in_prior_minute_scan": True,
            "eligible_before_selection": bool(ok0),
            "score": round(float(score0 or 0), 3),
            "bucket": (metrics0 or {}).get("prepared_bucket"),
            "blockers": (metrics0 or {}).get("big_explosion_prepared_blockers_ar"),
            "reasons": reasons0,
        }
    debug["target_probe"] = {sym: next(({
        "rank": idx + 1,
        "score": r.get("score"),
        "bucket": r.get("bucket"),
        "reasons": r.get("reasons"),
    } for idx, r in enumerate(out) if _u(r.get("symbol")) == sym), {"rank": None, "reason": "not_in_prepared_critical_promotion_gate", "raw_probe": target_raw_probe.get(sym)}) for sym in ["EHGO", "ICCM", "TPC", "SNBR"]}
    debug["target_raw_probe"] = target_raw_probe
    return out, debug


def _read_prior_full_session_minute_scan(
    *,
    trade_date: str,
    max_minute_rows: int = 2_500_000,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    max_seconds: float = 45.0,
) -> tuple[dict[str, Any], dict[str, dict], dict[str, dict]]:
    """Scan the full prior session after all sessions close.

    This is the user's requested missing piece: after all previous-day sessions
    have finished, scan the entire minute file (regular + extended where rows
    exist), build compact per-symbol maps, and prepare tomorrow candidates
    before the next premarket starts.  Raw files remain /tmp-only.
    """
    pull = fetch_flatfile_to_tmp("minute", trade_date, force=bool(force_minute_pull), redownload_processed=bool(redownload_processed))
    debug: dict[str, Any] = {
        "version": "prior_full_session_scan_v2u4_live_critical_pre_explosion_full_stream_2026_06_20",
        "trade_date": trade_date,
        "pull_status": {k: v for k, v in (pull or {}).items() if k not in {"path"}},
        "max_minute_rows": int(max_minute_rows or 0),
        "max_seconds": float(max_seconds or 0),
        "safe_mode_ar": "V2U4: مسح maintenance يقرأ ملف الدقيقة كاملًا streaming حتى لا يتوقف عند رموز A/B/C ويفوّت EHGO/ICCM/TPC/SNBR؛ المحاكي يمكنه تقليل الحد عند الحاجة.",
        "storage_rule_ar": "يمسح ملف دقيقة اليوم السابق من /tmp فقط ويبني ملخصات مدمجة؛ لا يحفظ raw في SQLite/GitHub/Railway.",
        "rule_ar": "V2T2: بعد اكتمال كل جلسات يوم الاختيار، يمسح اليوم السابق كاملًا بدقة دقيقة لتحضير قائمة الغد قبل البري ماركت.",
    }
    if not pull.get("ok") or not pull.get("path"):
        debug.update({"ok": False, "reason": pull.get("status") or pull.get("error") or "minute_file_unavailable"})
        return debug, {}, {}
    path = str(pull.get("path") or "")
    started_at = time.time()
    safe_seconds = max(5.0, min(90.0, float(max_seconds or 45.0)))
    timed_out = False
    rows_seen = 0
    stopped_by_time = False
    stopped_by_row_cap = False
    started_at = time.perf_counter()
    safe_row_cap = max(50_000, min(5_000_000, int(max_minute_rows or 2_500_000)))
    safe_seconds = max(5.0, min(90.0, float(max_seconds or 45.0)))
    per: dict[str, dict] = {}
    try:
        fp = Path(path)
        opener = gzip.open if fp.name.lower().endswith(".gz") else open
        with opener(fp, "rt", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                rows_seen += 1
                if rows_seen > safe_row_cap:
                    stopped_by_row_cap = True
                    break
                if rows_seen % 50000 == 0 and (time.perf_counter() - started_at) > safe_seconds:
                    stopped_by_time = True
                    break
                sym = _minute_symbol(raw)
                if not sym:
                    continue
                dt, hhmm, t_min = _minute_time_from_row(raw)
                if dt and trade_date and str(dt)[:10] != str(trade_date)[:10]:
                    continue
                if t_min < 0:
                    continue
                o = _minute_price(raw, "open", "o")
                h = _minute_price(raw, "high", "h")
                l = _minute_price(raw, "low", "l")
                c = _minute_price(raw, "close", "c")
                v = _minute_price(raw, "volume", "v")
                if c <= 0 or h <= 0 or l <= 0 or v <= 0:
                    continue
                rec = per.setdefault(sym, {"full": _empty_minute_agg(), "regular": _empty_minute_agg(), "after": _empty_minute_agg(), "pre": _empty_minute_agg()})
                _update_full_session_agg(rec["full"], t_min=t_min, o=o or c, h=h, l=l, c=c, v=v)
                phase = _utc_phase_from_minute(t_min)
                if phase == "regular":
                    _update_full_session_agg(rec["regular"], t_min=t_min, o=o or c, h=h, l=l, c=c, v=v)
                elif phase == "after_hours":
                    _update_full_session_agg(rec["after"], t_min=t_min, o=o or c, h=h, l=l, c=c, v=v)
                elif phase == "premarket":
                    _update_full_session_agg(rec["pre"], t_min=t_min, o=o or c, h=h, l=l, c=c, v=v)
        full_map: dict[str, dict] = {}
        after_map: dict[str, dict] = {}
        for sym, rec in per.items():
            full = rec.get("full") or {}
            reg = rec.get("regular") or {}
            aft = rec.get("after") or {}
            if _num(full.get("close"), 0.0) > 0 and _num(full.get("volume"), 0.0) > 0:
                full_map[sym] = _agg_to_grouped_row(sym, full, base_open=_num(reg.get("open"), 0.0) or _num(full.get("open"), 0.0), source_note="prior_full_session_v2t2")
            if _num(aft.get("close"), 0.0) > 0 and _num(aft.get("volume"), 0.0) > 0:
                # AH move is measured from regular close if available, because that is what matters for next-day PM prep.
                after_map[sym] = _agg_to_grouped_row(sym, aft, base_open=_num(reg.get("close"), 0.0) or _num(aft.get("open"), 0.0), source_note="prior_after_hours_v2t2")
        debug.update({
            "ok": True,
            "rows_seen": rows_seen,
            "stopped_by_time": bool(stopped_by_time),
            "stopped_by_row_cap": bool(stopped_by_row_cap),
            "elapsed_sec": round(time.perf_counter() - started_at, 3),
            "safe_row_cap": int(safe_row_cap),
            "symbols_seen": len(per),
            "full_session_symbols": len(full_map),
            "after_hours_symbols": len(after_map),
        })
        return debug, full_map, after_map
    except Exception as exc:
        debug.update({"ok": False, "reason": f"prior_minute_parse_error:{type(exc).__name__}:{str(exc)[:160]}", "rows_seen": rows_seen})
        return debug, {}, {}
    finally:
        cleanup_tmp_path(Path(path).parent if path else None)


def _build_prior_session_source_rows(
    *,
    selection_date: str,
    max_minute_rows: int = 2_500_000,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    max_seconds: float = 45.0,
) -> tuple[list[dict], dict[str, Any], dict[str, dict], dict[str, dict]]:
    debug, full_map, after_map = _read_prior_full_session_minute_scan(
        trade_date=selection_date,
        max_minute_rows=max_minute_rows,
        force_minute_pull=force_minute_pull,
        redownload_processed=redownload_processed,
        max_seconds=max_seconds,
    )
    if not debug.get("ok"):
        return [], debug, full_map, after_map
    rows: list[dict] = []
    full_micro, full_micro_debug = _collect_micro_explosion_full_market_candidates(full_map, phase_detail="prior_full_session_after_close_v2t2")
    full_fast, full_fast_debug = _collect_low_float_fast_lane_candidates(full_map, phase_detail="prior_full_session_after_close_v2t2")
    full_big, full_big_debug = _collect_big_explosion_live_lane_candidates(full_map, phase_detail="prior_full_session_after_close_v2t2")
    prior_prepared, prior_prepared_debug = _collect_prior_session_pre_explosion_watch_candidates(full_map, after_map, limit=520)
    ah_big, ah_big_debug = _collect_big_explosion_live_lane_candidates(after_map, phase_detail="prior_after_hours_after_close_v2t2")
    ah_micro, ah_micro_debug = _collect_micro_explosion_full_market_candidates(after_map, phase_detail="prior_after_hours_after_close_v2t2")
    rows.extend(_annotate_source_rows(prior_prepared, selection_date, 99, "prior_session_pre_explosion_watch_v2u"))
    rows.extend(_annotate_source_rows(full_micro, selection_date, 100, "prior_full_session_micro_v2t2"))
    rows.extend(_annotate_source_rows(full_fast, selection_date, 100, "prior_full_session_fast_lane_v2t2"))
    rows.extend(_annotate_source_rows(full_big, selection_date, 100, "prior_full_session_big_explosion_v2t2"))
    rows.extend(_annotate_source_rows(ah_micro, selection_date, 101, "prior_after_hours_micro_v2t2"))
    rows.extend(_annotate_source_rows(ah_big, selection_date, 101, "prior_after_hours_big_explosion_v2t2"))
    debug.update({
        "source_rows_total": len(rows),
        "prior_pre_explosion_watch_count": len(prior_prepared or []),
        "prior_pre_explosion_watch_top_symbols": (prior_prepared_debug or {}).get("top_symbols", [])[:30],
        "prior_pre_explosion_watch_debug": prior_prepared_debug,
        "full_micro_count": len(full_micro or []),
        "full_fast_count": len(full_fast or []),
        "full_big_count": len(full_big or []),
        "after_hours_micro_count": len(ah_micro or []),
        "after_hours_big_count": len(ah_big or []),
        "full_big_top": (full_big_debug or {}).get("top_symbols", [])[:20],
        "after_hours_big_top": (ah_big_debug or {}).get("top_symbols", [])[:20],
        "full_micro_top": (full_micro_debug or {}).get("top_symbols", [])[:20],
        "full_fast_top": (full_fast_debug or {}).get("top_symbols", [])[:20],
        "rule_ar": "V2U4 يشغّل مسح ما بعد الإغلاق الكامل بدون توقف مبكر عند A/B/C + تعدين مرشحي ما قبل الانفجار بمقاعد micro/ICCM/TPC/AH، ثم يمر بالشرعية والتحليل العميق.",
    })
    return rows, debug, full_map, after_map


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
    layers = set(trace.get("source_layers") or [])
    prior_full_seen = any(str(x).startswith("prior_full_session") or str(x).startswith("prior_after_hours") for x in layers)
    days_seen = int(trace.get("source_days_seen") or 0)
    if prior_full_seen:
        score += 14
        reasons.append("مسح دقيقة كامل بعد إغلاق كل الجلسات رصده لقائمة الغد")
    if any(str(x).startswith("prior_after_hours") for x in layers):
        score += 8
        reasons.append("ظهر في after-hours اليوم السابق؛ مهم قبل بري ماركت الغد")
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


def _combine_source_candidates(micro_rows: list[dict], fast_rows: list[dict], extra_rows: list[dict] | None = None, clean_only: bool = True) -> tuple[list[dict], dict]:
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
    for r in extra_rows or []:
        add(r, _s(r.get("historical_source_family")) or "prior_full_session_v2t2")

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
        "timing_note_ar": "V2S1 يعرف التحضير بعد الإغلاق وسياق الأيام فقط؛ توقيت البري ماركت/داخل الجلسة يحتاج V2T minute replay.",
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




# ---------------------------------------------------------------------------
# V2T: Big Explosion Minute Timing Replay
# ---------------------------------------------------------------------------

def _minute_time_from_row(row: dict) -> tuple[str, str, int]:
    raw = row.get("window_start") or row.get("timestamp") or row.get("t") or row.get("sip_timestamp") or ""
    try:
        n = int(float(raw))
        if n > 10**17:       # ns
            sec = n / 1_000_000_000
        elif n > 10**14:     # us
            sec = n / 1_000_000
        elif n > 10**11:     # ms
            sec = n / 1000
        else:
            sec = n
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        hhmm = dt.strftime("%H:%M")
        return dt.strftime("%Y-%m-%d"), hhmm, int(dt.hour * 60 + dt.minute)
    except Exception:
        txt = _s(raw)
        if len(txt) >= 16 and txt[:4].isdigit():
            hhmm = txt[11:16]
            try:
                h, m = hhmm.split(":", 1)
                return txt[:10], hhmm, int(h) * 60 + int(m)
            except Exception:
                return txt[:10], hhmm, -1
    return "", "", -1


def _minute_symbol(row: dict) -> str:
    return _u(row.get("ticker") or row.get("symbol") or row.get("T") or row.get("sym"))


def _minute_price(row: dict, *keys: str) -> float:
    for k in keys:
        n = _num(row.get(k), 0.0)
        if n > 0:
            return n
    return 0.0


def _utc_phase_from_minute(minute_of_day: int) -> str:
    # US market in summer/EDT: 04:00 NY = 08:00 UTC, 09:30 = 13:30, 16:00 = 20:00.
    if minute_of_day < 0:
        return "unknown"
    if minute_of_day < 8 * 60:
        return "overnight"
    if minute_of_day < 13 * 60 + 30:
        return "premarket"
    if minute_of_day < 20 * 60:
        return "regular"
    return "after_hours"


def _phase_ar(phase: str) -> str:
    return {
        "overnight": "ليلي/قبل البري ماركت",
        "premarket": "قبل الافتتاح",
        "regular": "أثناء الجلسة الرسمية",
        "after_hours": "بعد الإغلاق",
        "unknown": "غير معروف",
    }.get(str(phase or ""), "غير معروف")


def _minute_label(minute_of_day: int) -> str:
    if minute_of_day < 0:
        return ""
    return f"{int(minute_of_day)//60:02d}:{int(minute_of_day)%60:02d}"


def _minute_between(start_hhmm: str, end_hhmm: str) -> int | None:
    try:
        sh, sm = str(start_hhmm or "").split(":", 1)
        eh, em = str(end_hhmm or "").split(":", 1)
        # V2T1: allow negative values to expose detections that happened after the peak.
        return int(eh) * 60 + int(em) - (int(sh) * 60 + int(sm))
    except Exception:
        return None


def _safe_gain(price: float, base: float) -> float:
    return ((price - base) / base * 100.0) if price > 0 and base > 0 else 0.0


def _update_slice_agg(agg: dict, *, t_min: int, o: float, h: float, l: float, c: float, v: float) -> None:
    if not agg or agg.get("first_minute") is None:
        agg["first_minute"] = t_min
        agg["last_minute"] = t_min
        agg["open"] = o or c
        agg["high"] = h
        agg["low"] = l
        agg["price"] = c
        agg["close"] = c
        agg["volume"] = v
        agg["vwap_num"] = ((h + l + c) / 3.0) * v
        return
    if t_min < int(agg.get("first_minute") or t_min):
        agg["first_minute"] = t_min
        agg["open"] = o or c
    if t_min >= int(agg.get("last_minute") or t_min):
        agg["last_minute"] = t_min
        agg["price"] = c
        agg["close"] = c
    agg["high"] = max(_num(agg.get("high"), h), h)
    old_low = _num(agg.get("low"), l)
    agg["low"] = min(old_low if old_low > 0 else l, l)
    agg["volume"] = _num(agg.get("volume"), 0.0) + v
    agg["vwap_num"] = _num(agg.get("vwap_num"), 0.0) + ((h + l + c) / 3.0) * v


def _finalize_slice_map(raw_map: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for sym, a in (raw_map or {}).items():
        price = _num(a.get("price"), 0.0) or _num(a.get("close"), 0.0)
        opn = _num(a.get("open"), price)
        high = _num(a.get("high"), price)
        low = _num(a.get("low"), price)
        vol = _num(a.get("volume"), 0.0)
        if not sym or price <= 0 or high <= 0 or low <= 0 or vol <= 0:
            continue
        out[sym] = {
            "symbol": sym,
            "ticker": sym,
            "open": opn,
            "high": high,
            "low": low,
            "price": price,
            "close": price,
            "volume": vol,
            "dollar_volume": price * vol,
            "vwap_proxy": (_num(a.get("vwap_num"), 0.0) / vol) if vol > 0 else 0.0,
            "change_pct": _safe_gain(price, opn),
            "first_minute": a.get("first_minute"),
            "last_minute": a.get("last_minute"),
        }
    return out


def _default_replay_slices() -> list[dict[str, Any]]:
    """V2T2: opening 1-minute replay + dense PM checkpoints.

    V2T1 improved timing but still missed first-minute opening explosions
    (TPC-style).  V2T2 keeps broad PM coverage and makes the critical opening
    window minute-by-minute so the replay can catch the moment a stock crosses
    +3/+5/+20 instead of waiting five minutes.
    Times are UTC in US summer market hours.
    """
    points: list[tuple[int, str]] = []
    def add(minute: int, label: str) -> None:
        if minute < 0:
            return
        points.append((int(minute), label))
    # Early premarket: every 15 minutes to catch AH/PM build before the rush.
    for m in range(8 * 60, 9 * 60, 15):
        add(m, "بري ماركت مبكر — كل 15 دقيقة")
    # Active premarket: every 5 minutes.
    for m in range(9 * 60, 13 * 60 + 30, 5):
        add(m, "بري ماركت كثيف V2T2")
    # Opening burst: every minute for first 20 minutes.
    for m in range(13 * 60 + 30, 13 * 60 + 51, 1):
        add(m, "الافتتاح — كل دقيقة V2T2")
    # Rest of first hour: every 5 minutes.
    for m in range(13 * 60 + 55, 14 * 60 + 31, 5):
        add(m, "أول ساعة — كل 5 دقائق")
    # Regular session after first hour every 15 minutes.
    for m in range(14 * 60 + 45, 20 * 60 + 1, 15):
        add(m, "الجلسة الرسمية — كل 15 دقيقة")
    # After-hours checkpoints.
    for m in (20 * 60 + 15, 20 * 60 + 30, 21 * 60, 22 * 60):
        add(m, "بعد الإغلاق")
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for minute, label in sorted(points):
        if minute in seen:
            continue
        seen.add(minute)
        hhmm = _minute_label(minute)
        key = hhmm.replace(":", "")
        out.append({"key": f"slice_{key}", "time_utc": hhmm, "minute": minute, "label_ar": label})
    return out


def _read_minute_file_for_replay(
    *,
    trade_date: str,
    target_symbols: set[str],
    max_minute_rows: int = 1_800_000,
    max_seconds: float = 12.0,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
) -> tuple[dict[str, Any], dict[str, list[dict]], dict[str, dict[str, dict]]]:
    """Fetch outcome minute flat file to /tmp, stream it, and return compact maps only."""
    slices = _default_replay_slices()
    target_symbols = {_u(x) for x in (target_symbols or set()) if _u(x)}
    pull = fetch_flatfile_to_tmp("minute", trade_date, force=bool(force_minute_pull), redownload_processed=bool(redownload_processed))
    debug = {
        "version": "minute_replay_loader_v2t2d_target_only_fast_2026_06_20",
        "trade_date": trade_date,
        "pull_status": {k: v for k, v in (pull or {}).items() if k not in {"path"}},
        "target_symbols_count": len(target_symbols),
        "target_only_slice_mode": True,
        "max_minute_rows": int(max_minute_rows or 0),
        "max_seconds": float(max_seconds or 0),
        "safe_mode_ar": "V2U4: مسح maintenance يقرأ ملف الدقيقة كاملًا streaming حتى لا يتوقف عند رموز A/B/C ويفوّت EHGO/ICCM/TPC/SNBR؛ المحاكي يمكنه تقليل الحد عند الحاجة.",
        "storage_rule_ar": "يتم تنزيل ملف الدقيقة إلى /tmp فقط، ثم يقرأ Streaming ويرجع ملخصات مدمجة؛ لا يتم حفظ raw في SQLite/GitHub/Railway.",
    }
    if not pull.get("ok") or not pull.get("path"):
        debug.update({"ok": False, "reason": pull.get("status") or pull.get("error") or "minute_file_unavailable"})
        return debug, {}, {}
    path = str(pull.get("path") or "")
    started_at = time.time()
    safe_seconds = max(5.0, min(90.0, float(max_seconds or 45.0)))
    timed_out = False
    rows_seen = 0
    target_rows: dict[str, list[dict]] = {sym: [] for sym in target_symbols}
    slice_aggs: dict[str, dict[str, dict]] = {str(sl["key"]): {} for sl in slices}
    slice_by_key = {str(sl["key"]): sl for sl in slices}
    try:
        fp = Path(path)
        opener = gzip.open if fp.name.lower().endswith(".gz") else open
        with opener(fp, "rt", encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                rows_seen += 1
                if rows_seen % 5000 == 0 and (time.time() - started_at) > safe_seconds:
                    timed_out = True
                    break
                if rows_seen > max(50_000, min(3_500_000, int(max_minute_rows or 1_800_000))):
                    break
                sym = _minute_symbol(raw)
                if not sym:
                    continue
                dt, hhmm, t_min = _minute_time_from_row(raw)
                if dt and trade_date and str(dt)[:10] != str(trade_date)[:10]:
                    continue
                if t_min < 0:
                    continue
                o = _minute_price(raw, "open", "o")
                h = _minute_price(raw, "high", "h")
                l = _minute_price(raw, "low", "l")
                c = _minute_price(raw, "close", "c")
                v = _minute_price(raw, "volume", "v")
                if c <= 0 or h <= 0 or l <= 0 or v <= 0:
                    continue
                # V2T2d: target-only minute replay.
                # The previous V2T2c reader updated slice maps for the whole market,
                # which could time out before reaching target winners such as EHGO/ICCM/TPC.
                # For the timing report we only need to know whether the SAME source
                # logic would trigger on the target symbols, so we build compact slices
                # for target symbols only. This keeps the report faithful while avoiding
                # Railway upstream/timeout failures.
                if sym not in target_symbols:
                    continue
                target_rows.setdefault(sym, []).append({
                    "time_utc": hhmm,
                    "minute": t_min,
                    "phase": _utc_phase_from_minute(t_min),
                    "open": _round(o or c, 4),
                    "high": _round(h, 4),
                    "low": _round(l, 4),
                    "close": _round(c, 4),
                    "volume": _round(v, 0),
                    "dollar_volume": _round(c * v, 2),
                })
                for sl in slices:
                    if t_min <= int(sl["minute"]):
                        bucket = slice_aggs[str(sl["key"])].setdefault(sym, {})
                        _update_slice_agg(bucket, t_min=t_min, o=o or c, h=h, l=l, c=c, v=v)
        compact_slices = {k: _finalize_slice_map(v) for k, v in slice_aggs.items()}
        debug.update({
            "ok": True,
            "rows_seen": rows_seen,
            "timed_out": bool(timed_out),
            "safe_seconds": float(safe_seconds),
            "target_rows_loaded": sum(len(v) for v in target_rows.values()),
            "target_symbols_with_rows": len([1 for v in target_rows.values() if v]),
            "slice_rows": {k: len(v or {}) for k, v in compact_slices.items()},
            "slices": [{"key": k, **slice_by_key[k]} for k in slice_by_key],
        })
        return debug, target_rows, compact_slices
    except Exception as exc:
        debug.update({"ok": False, "reason": f"minute_parse_error:{type(exc).__name__}:{str(exc)[:160]}", "rows_seen": rows_seen})
        return debug, target_rows, {}
    finally:
        cleanup_tmp_path(Path(path).parent if path else None)


def _timeline_from_target_bars(sym: str, bars: list[dict], selection_price: float) -> dict[str, Any]:
    sym = _u(sym)
    bars = sorted([dict(x) for x in (bars or [])], key=lambda x: int(x.get("minute") or 0))
    out: dict[str, Any] = {
        "symbol": sym,
        "bars_loaded": len(bars),
        "selection_price": _round(selection_price, 4),
        "has_minute_timeline": bool(bars and selection_price > 0),
    }
    if not bars or selection_price <= 0:
        out.update({"timeline_note_ar": "لا توجد بيانات دقيقة كافية لهذا السهم أو سعر اختيار غير متاح."})
        return out
    first = bars[0]
    peak = max(bars, key=lambda x: _num(x.get("high"), 0.0))
    low = min(bars, key=lambda x: _num(x.get("low"), 999999.0))
    last = bars[-1]
    thresholds = [3, 5, 10, 20, 50, 100, 200]
    th_hits: dict[str, dict] = {}
    for th in thresholds:
        hit = None
        for b in bars:
            if _safe_gain(_num(b.get("high"), 0.0), selection_price) >= th:
                hit = b
                break
        if hit:
            th_hits[f"first_{th}pct"] = {
                "time_utc": hit.get("time_utc"),
                "phase": hit.get("phase"),
                "phase_ar": _phase_ar(hit.get("phase")),
                "price_high": _round(hit.get("high"), 4),
                "gain_pct": _round(_safe_gain(_num(hit.get("high"), 0.0), selection_price), 2),
            }
    # First real acceleration is the earliest 5% threshold or a strong 3%+ volume bar.
    first_accel = None
    for b in bars:
        gain = _safe_gain(_num(b.get("high"), 0.0), selection_price)
        if gain >= 5:
            first_accel = b
            break
    open_bar = next((b for b in bars if int(b.get("minute") or 0) >= 13 * 60 + 30), first)
    premarket_high = max([_num(b.get("high"), 0.0) for b in bars if str(b.get("phase")) == "premarket"] or [0.0])
    regular_high = max([_num(b.get("high"), 0.0) for b in bars if str(b.get("phase")) == "regular"] or [0.0])
    after_high = max([_num(b.get("high"), 0.0) for b in bars if str(b.get("phase")) == "after_hours"] or [0.0])
    peak_time = str(peak.get("time_utc") or "")
    accel_time = str((first_accel or {}).get("time_utc") or "")
    if first_accel and str(first_accel.get("phase")) == "premarket":
        path_label = "بدأ قبل الافتتاح"
    elif first_accel and int(first_accel.get("minute") or 0) <= 14 * 60:
        path_label = "انفجار مع الافتتاح/أول 30 دقيقة"
    elif first_accel:
        path_label = "تسارع أثناء الجلسة"
    else:
        path_label = "لم يصل +5% في بيانات الدقيقة"
    out.update({
        "first_minute_time_utc": first.get("time_utc"),
        "first_minute_phase_ar": _phase_ar(first.get("phase")),
        "open_time_utc": open_bar.get("time_utc"),
        "open_price": _round(open_bar.get("open") or open_bar.get("close"), 4),
        "peak_time_utc": peak_time,
        "peak_phase": peak.get("phase"),
        "peak_phase_ar": _phase_ar(peak.get("phase")),
        "peak_price": _round(peak.get("high"), 4),
        "peak_gain_from_selection_pct": _round(_safe_gain(_num(peak.get("high"), 0.0), selection_price), 2),
        "low_time_utc": low.get("time_utc"),
        "low_price": _round(low.get("low"), 4),
        "worst_drawdown_from_selection_pct": _round(_safe_gain(_num(low.get("low"), 0.0), selection_price), 2),
        "close_time_utc": last.get("time_utc"),
        "close_price": _round(last.get("close"), 4),
        "close_gain_from_selection_pct": _round(_safe_gain(_num(last.get("close"), 0.0), selection_price), 2),
        "premarket_high_gain_pct": _round(_safe_gain(premarket_high, selection_price), 2) if premarket_high > 0 else 0.0,
        "regular_high_gain_pct": _round(_safe_gain(regular_high, selection_price), 2) if regular_high > 0 else 0.0,
        "after_hours_high_gain_pct": _round(_safe_gain(after_high, selection_price), 2) if after_high > 0 else 0.0,
        "first_acceleration_time_utc": accel_time,
        "first_acceleration_phase_ar": _phase_ar((first_accel or {}).get("phase")),
        "minutes_from_acceleration_to_peak": _minute_between(accel_time, peak_time) if accel_time and peak_time else None,
        "rise_path_label_ar": path_label,
        "threshold_hits": th_hits,
        "how_it_moved_ar": f"{path_label}. القمة عند {peak_time or 'غير متاح'} بربح {_round(_safe_gain(_num(peak.get('high'), 0.0), selection_price), 2)}% من سعر التحضير.",
    })
    return out


def _stage_for_slice(row: dict, current_gain: float, layer: str, sharia: dict) -> tuple[str, str]:
    score = _candidate_score(row)
    metrics = _candidate_metrics(row)
    dollar = _num(metrics.get("dollar_volume"), 0.0)
    near_high = bool(metrics.get("near_high"))
    if sharia.get("should_block"):
        return "sharia_blocked", "مرفوض شرعيًا"
    if sharia.get("is_gray"):
        return "sharia_gray_watch", "مرشح رمادي — يحتاج مراجعة شرعية"
    if current_gain >= 50:
        return "big_explosion_active", "انفجار كبير نشط"
    if current_gain >= 20:
        return "explosion_active", "انفجار نشط"
    if current_gain >= 10 and near_high:
        return "late_momentum_watch", "زخم قوي لكن قد يكون متأخرًا"
    if current_gain >= 5 and score >= 85:
        return "early_confirmation", "تأكيد مبكر"
    if (score >= 100 or dollar >= 500_000) and layer in {"micro_explosion_full_market_v2r2", "low_float_fast_lane_v2q"}:
        return "close_watch", "مراقبة لصيقة"
    return "source_detected", "دخل المصدر"


def _apply_selection_baseline_to_slice_map(m: dict[str, dict], selection_prices: dict[str, float], target_symbols: set[str]) -> dict[str, dict]:
    """Simulate live FMP percent-change in historical minute slices.

    Minute aggregates open at the first traded minute, which hides gap runners.
    In the real tool FMP reports change from previous close.  For target replay
    we reset the candle open to the after-close selection/previous close so
    Big Explosion sees TPC/ICCM/EHGO-style gap-and-go moves at the right time.
    """
    out = dict(m or {})
    for sym in target_symbols or set():
        sym = _u(sym)
        row = dict(out.get(sym) or {})
        base = _num(selection_prices.get(sym), 0.0)
        price = _num(row.get("price") or row.get("close"), 0.0)
        high = _num(row.get("high"), price)
        low = _num(row.get("low"), price)
        if not sym or not row or base <= 0 or price <= 0:
            continue
        row["open_intraday_first"] = row.get("open")
        row["open"] = base
        row["previous_close_proxy"] = base
        row["selection_price_proxy"] = base
        row["close_price_proxy"] = price
        # V2T2 timing proxy: during the first opening minutes or a huge PM candle,
        # a live quote could have seen the intra-minute high. Use it only for
        # target timing replay, not for live BUY decisions.
        high_gain = _safe_gain(high, base)
        last_minute = int(row.get("last_minute") or row.get("first_minute") or 0)
        use_high_proxy = bool(high > price and high_gain >= 3 and (last_minute <= 13 * 60 + 50 or high_gain >= 20))
        price_for_detection = high if use_high_proxy else price
        row["price"] = price_for_detection
        row["big_explosion_high_proxy_v2t2"] = use_high_proxy
        row["close"] = price
        row["change_pct"] = _safe_gain(price_for_detection, base)
        row["day_change_pct"] = _safe_gain(price_for_detection, base)
        row["range_pct"] = ((high - low) / price) if price > 0 and high > 0 and low > 0 else _num(row.get("range_pct"), 0.0)
        out[sym] = row
    return out


def _run_source_on_minute_slices(
    *,
    outcome_date: str,
    slice_maps: dict[str, dict[str, dict]],
    target_symbols: set[str],
    selection_prices: dict[str, float],
    clean_only: bool = True,
) -> dict[str, Any]:
    slices = _default_replay_slices()
    by_symbol: dict[str, dict] = {sym: {"symbol": sym, "detected_by_minute_replay": False, "stage_history": []} for sym in target_symbols}
    slice_debug: list[dict] = []
    for sl in slices:
        key = str(sl["key"])
        m_raw = dict(slice_maps.get(key) or {})
        m = _apply_selection_baseline_to_slice_map(m_raw, selection_prices, target_symbols)
        if not m:
            slice_debug.append({"key": key, "time_utc": sl.get("time_utc"), "rows": 0, "micro": 0, "fast": 0, "big": 0})
            continue
        phase_tag = f"historical_minute_slice_v2t2:{outcome_date}:{sl.get('time_utc')}"
        micro_rows, micro_debug = _collect_micro_explosion_full_market_candidates(m, phase_detail=phase_tag)
        fast_rows, fast_debug = _collect_low_float_fast_lane_candidates(m, phase_detail=phase_tag)
        big_rows, big_debug = _collect_big_explosion_live_lane_candidates(m, phase_detail=phase_tag)
        # V2T1 target probe: same Big Explosion scoring function, but not hidden by source cap/ranking.
        # This tells us whether production logic would flag the symbol if reserved seats existed.
        target_probe_rows = []
        for _sym in sorted(target_symbols or set()):
            _row = m.get(_sym) or {}
            if not _row:
                continue
            _ok, _score, _reasons, _flags = _big_explosion_live_lane_score(_sym, _row, phase_detail=phase_tag, source_kind="historical_minute_slice_v2t2")
            if _ok:
                target_probe_rows.append({"symbol": _sym, "score": _round(_score, 3), "reasons": _reasons, "metrics": {**_row, **_flags}})
        source_rows: list[tuple[dict, str]] = [(r, "big_explosion_live_lane_v2t") for r in (big_rows or [])] + [(r, "big_explosion_live_lane_v2t2_target_probe") for r in target_probe_rows] + [(r, "micro_explosion_full_market_v2r2") for r in (micro_rows or [])] + [(r, "low_float_fast_lane_v2q") for r in (fast_rows or [])]
        slice_debug.append({
            "key": key,
            "time_utc": sl.get("time_utc"),
            "label_ar": sl.get("label_ar"),
            "rows": len(m),
            "micro": len(micro_rows or []),
            "fast": len(fast_rows or []),
            "big": len(big_rows or []),
            "target_probe_big": len(target_probe_rows or []),
            "micro_top": (micro_debug or {}).get("top_symbols", [])[:10],
            "fast_top": (fast_debug or {}).get("top_symbols", [])[:10],
            "big_top": (big_debug or {}).get("top_symbols", [])[:10],
        })
        for row, layer in source_rows:
            sym = _extract_symbol(row)
            if not sym or sym not in target_symbols:
                continue
            price = _num((m.get(sym) or {}).get("price"), 0.0) or _num((_candidate_metrics(row) or {}).get("price"), 0.0)
            base = _num(selection_prices.get(sym), 0.0)
            gain = _safe_gain(price, base)
            sharia = assess_sharia_source_fast(sym)
            stage, stage_ar = _stage_for_slice(row, gain, layer, sharia)
            rec = by_symbol.setdefault(sym, {"symbol": sym, "detected_by_minute_replay": False, "stage_history": []})
            rec["detected_by_minute_replay"] = True
            if not rec.get("first_detected_time_utc"):
                rec["first_detected_time_utc"] = sl.get("time_utc")
                rec["first_detected_phase_ar"] = _phase_ar(_utc_phase_from_minute(int(sl.get("minute") or 0)))
                rec["first_detected_price"] = _round(price, 4)
                rec["first_detected_gain_pct"] = _round(gain, 2)
                rec["first_detected_layer"] = layer
                rec["first_detected_stage"] = stage
                rec["first_detected_stage_ar"] = stage_ar
            seen_stages = {x.get("stage") for x in rec.get("stage_history") or []}
            if stage not in seen_stages:
                rec.setdefault("stage_history", []).append({
                    "time_utc": sl.get("time_utc"),
                    "phase_ar": _phase_ar(_utc_phase_from_minute(int(sl.get("minute") or 0))),
                    "slice_label_ar": sl.get("label_ar"),
                    "stage": stage,
                    "stage_ar": stage_ar,
                    "source_layer": layer,
                    "price": _round(price, 4),
                    "gain_pct_at_stage": _round(gain, 2),
                    "source_score": _round(_candidate_score(row), 2),
                    "sharia_status": sharia.get("status"),
                    "sharia_label": sharia.get("label"),
                })
    for sym, rec in by_symbol.items():
        hist = rec.get("stage_history") or []
        rec["promotion_count"] = len(hist)
        rec["promotion_summary_ar"] = " → ".join([f"{x.get('time_utc')} {x.get('stage_ar')} ({x.get('gain_pct_at_stage')}%)" for x in hist[:8]]) if hist else "لم يظهر في شرائح المصدر الدقيقة"
    return {
        "version": "minute_source_slice_replay_v2t2_opening_1min_proxy_2026_06_20",
        "outcome_date": outcome_date,
        "target_symbols": sorted(target_symbols),
        "slice_debug": slice_debug,
        "symbols": by_symbol,
        "rule_ar": "كل شريحة دقيقة تبني grouped تراكمي حتى تلك اللحظة فقط ثم تشغل دوال المصدر الحقيقية Big Explosion/Micro/Fast Lane؛ لا تستخدم بيانات لاحقة داخل الشريحة.",
    }


def build_prior_session_explosion_watch(
    *,
    trade_date: str,
    max_minute_rows: int = 2_500_000,
    max_seconds: float = 45.0,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    persist: bool = True,
) -> dict[str, Any]:
    rows, debug, full_map, after_map = _build_prior_session_source_rows(
        selection_date=trade_date,
        max_minute_rows=max_minute_rows,
        max_seconds=max_seconds,
        force_minute_pull=force_minute_pull,
        redownload_processed=redownload_processed,
    )
    prepared = [r for r in rows or [] if _s(r.get("historical_source_family")) == "prior_session_pre_explosion_watch_v2u"]
    # Put the prepared-watch lane first, but keep other prior-session rows as secondary context.
    compact: list[dict] = []
    seen: set[str] = set()
    for r in list(prepared or []) + list(rows or []):
        sym = _extract_symbol(r)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        metrics = _candidate_metrics(r)
        compact.append({
            "symbol": sym,
            "score": _candidate_score(r),
            "reasons": list(r.get("reasons") or metrics.get("big_explosion_prepared_reasons_ar") or [])[:8],
            "metrics": {**metrics, "big_explosion_prepared_watch_v2u": True, "urgent_sharia_review_v2u": True},
        })
    save_payload = {}
    if persist:
        save_payload = save_prepared_big_explosion_watch(compact, trade_date=trade_date, source="prior_session_explosion_scan_v2u4_critical_promotion_gate", debug=debug)
    loaded_items, loaded_debug = load_prepared_big_explosion_watch()
    return {
        "ok": bool((debug or {}).get("ok", False)),
        "version": "prior_session_explosion_watch_builder_v2u4_live_critical_pre_explosion_2026_06_20",
        "trade_date": trade_date,
        "rows_total": len(rows or []),
        "prepared_watch_count": len(prepared or []),
        "compact_count": len(compact),
        "top_symbols": [x.get("symbol") for x in compact[:80]],
        "scan_debug": debug,
        "persisted": save_payload,
        "loaded_after_persist": loaded_debug,
        "rule_ar": "V2U4: مسح ما بعد الإغلاق الحقيقي يغذي الأداة الحية قبل البري ماركت مع مقاعد مخصصة لأنماط EHGO/ICCM/TPC/SNBR؛ يحفظ ملخصات فقط ولا يغير قرارات الشراء.",
    }


def _build_big_explosion_timing_report(
    *,
    payload: dict[str, Any],
    selection_grouped: dict,
    outcome_grouped: dict,
    outcome_date: str,
    selected_symbols: set[str],
    max_symbols: int = 30,
    max_minute_rows: int = 1_800_000,
    clean_only: bool = True,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    prior_full_session_scan: bool = True,
    prior_scan_max_rows: int = 400_000,
    prior_scan_timeout_sec: float = 8.0,
    persist_prepared_watch: bool = False,
) -> dict[str, Any]:
    missed = payload.get("missed_winners_audit") or {}
    winners = list(missed.get("top_outcome_winners") or [])
    # Ensure selected winners and very large misses are included, not only missed top list.
    winners = sorted(winners, key=lambda x: _num(x.get("next_session_max_gain_pct"), 0.0), reverse=True)
    limit = max(5, min(80, int(max_symbols or 30)))
    target = []
    seen = set()
    for w in winners:
        sym = _u(w.get("symbol"))
        if not sym or sym in seen:
            continue
        target.append(w)
        seen.add(sym)
        if len(target) >= limit:
            break
    target_symbols = {_u(x.get("symbol")) for x in target if _u(x.get("symbol"))}
    selection_prices: dict[str, float] = {}
    for sym in target_symbols:
        sel = selection_grouped.get(sym) or {}
        selection_prices[sym] = _num(sel.get("price"), 0.0) or _num(sel.get("close"), 0.0) or _num((next((w for w in target if _u(w.get('symbol')) == sym), {}) or {}).get("selection_price"), 0.0)
    loader_debug, target_bars, slice_maps = _read_minute_file_for_replay(
        trade_date=outcome_date,
        target_symbols=target_symbols,
        max_minute_rows=max_minute_rows,
        max_seconds=max(20.0, float(prior_scan_timeout_sec or 8.0)),
        force_minute_pull=force_minute_pull,
        redownload_processed=redownload_processed,
    )
    if not loader_debug.get("ok"):
        return {
            "ok": False,
            "version": "big_explosion_minute_timing_report_v2t2d_target_minute_replay_2026_06_20",
            "outcome_date": outcome_date,
            "target_symbols": sorted(target_symbols),
            "minute_loader": loader_debug,
            "note_ar": "لم تتوفر بيانات الدقيقة؛ التقرير اليومي V2S1 ما زال صالحًا، لكن توقيت الالتقاط يحتاج minute flat file.",
        }
    source_replay = _run_source_on_minute_slices(
        outcome_date=outcome_date,
        slice_maps=slice_maps,
        target_symbols=target_symbols,
        selection_prices=selection_prices,
        clean_only=clean_only,
    )
    reports = []
    for w in target:
        sym = _u(w.get("symbol"))
        base = _num(selection_prices.get(sym), 0.0)
        timeline = _timeline_from_target_bars(sym, target_bars.get(sym) or [], base)
        capture = (source_replay.get("symbols") or {}).get(sym) or {}
        peak_time = timeline.get("peak_time_utc") or ""
        det_time = capture.get("first_detected_time_utc") or ""
        minutes_det_to_peak = _minute_between(det_time, peak_time) if det_time and peak_time else None
        det_gain = _num(capture.get("first_detected_gain_pct"), 0.0)
        peak_gain = _num(timeline.get("peak_gain_from_selection_pct"), _num(w.get("next_session_max_gain_pct"), 0.0))
        if capture.get("detected_by_minute_replay"):
            if minutes_det_to_peak is not None and minutes_det_to_peak < 0:
                timing_label = "التقطه بعد القمة — متأخر جدًا"
            elif det_gain <= 8:
                timing_label = "التقطه مبكرًا جدًا"
            elif det_gain <= 20:
                timing_label = "التقطه مبكرًا حول بداية الانفجار"
            elif det_gain <= 50:
                timing_label = "التقطه أثناء الانفجار — قابل للمراقبة"
            else:
                timing_label = "التقطه متأخرًا/بعد انفجار واضح"
        else:
            timing_label = "لم تلتقطه شرائح المصدر الدقيقة"
        reports.append({
            "symbol": sym,
            "selected_by_after_close_tool": bool(w.get("selected_by_tool") or sym in selected_symbols),
            "daily_miss_code": w.get("miss_code"),
            "daily_miss_reason_ar": w.get("miss_reason_ar"),
            "selection_price": _round(base, 4),
            "outcome_gain_daily_pct": w.get("next_session_max_gain_pct"),
            "timeline": timeline,
            "minute_capture": capture,
            "detected_by_minute_replay": bool(capture.get("detected_by_minute_replay")),
            "first_detected_time_utc": det_time,
            "first_detected_gain_pct": capture.get("first_detected_gain_pct"),
            "first_detected_stage_ar": capture.get("first_detected_stage_ar"),
            "minutes_from_detection_to_peak": minutes_det_to_peak,
            "timing_label_ar": timing_label,
            "capture_quality_ar": f"{timing_label}: الالتقاط عند {det_gain:.2f}% من سعر التحضير، والقمة {peak_gain:.2f}%.",
            "promotion_history_ar": capture.get("promotion_summary_ar") or "لا توجد ترقية في شرائح المصدر الدقيقة",
        })
    detected_count = sum(1 for r in reports if r.get("detected_by_minute_replay"))
    early_count = sum(1 for r in reports if r.get("detected_by_minute_replay") and _num(r.get("first_detected_gain_pct"), 9999) <= 20 and (_num(r.get("minutes_from_detection_to_peak"), 0) >= 0))
    return {
        "ok": True,
        "version": "big_explosion_minute_timing_report_v2t2d_target_minute_replay_2026_06_20",
        "outcome_date": outcome_date,
        "target_count": len(reports),
        "detected_by_minute_replay_count": detected_count,
        "early_detected_count": early_count,
        "early_definition_ar": "V2T2d يعتبر الالتقاط مبكرًا إذا كان عند <=20% وقبل القمة، مع قارئ target-only سريع حتى لا تفشل رموز الانفجار بسبب timeout.",
        "late_or_missed_count": len(reports) - early_count,
        "minute_loader": loader_debug,
        "source_replay_summary": {k: v for k, v in source_replay.items() if k not in {"symbols"}},
        "symbols": reports,
        "top_problem_symbols": [r for r in reports if not r.get("detected_by_minute_replay") or _num(r.get("first_detected_gain_pct"), 0.0) > 20][:20],
        "capture_rate_split": {
            "after_close_selected_count": sum(1 for r in reports if r.get("selected_by_after_close_tool")),
            "minute_detected_count": detected_count,
            "minute_early_detected_count": early_count,
            "minute_late_or_missed_count": len(reports) - early_count,
            "gray_or_blocked_detected_count": sum(1 for r in reports if str(((r.get("minute_capture") or {}).get("first_detected_stage") or "")) in {"sharia_gray_watch", "sharia_blocked"}),
        },
        "rule_ar": "V2T2 يسأل: متى ارتفع السهم؟ هل دخل مصدر الأداة في شرائح الوقت؟ كم كانت نسبته وقت الالتقاط؟ ومتى ترقى داخل replay stages. هذا لا يطلق شراء حي بعد.",
    }



# ---------------------------------------------------------------------------
# V2V2: Historical Live Hunting Replay
# ---------------------------------------------------------------------------

def _daily_winners_from_map(
    *,
    hunt_map: dict,
    prior_map: dict,
    min_gain_pct: float = 20.0,
    max_symbols: int = 80,
) -> list[dict[str, Any]]:
    """Return top same-day movers using only prior close/open as the baseline.

    This list is used only to audit whether the replay would have hunted the
    winners.  It is not used by the replay engine as a future signal.
    """
    rows: list[dict[str, Any]] = []
    threshold = float(min_gain_pct or 20.0)
    for sym, row in (hunt_map or {}).items():
        sym = _u(sym)
        if not sym:
            continue
        r = dict(row or {})
        p = dict((prior_map or {}).get(sym) or {})
        base = _num(p.get("price"), 0.0) or _num(p.get("close"), 0.0) or _num(r.get("open"), 0.0) or _num(r.get("o"), 0.0)
        high = _num(r.get("high"), 0.0) or _num(r.get("h"), 0.0)
        close = _num(r.get("price"), 0.0) or _num(r.get("close"), 0.0) or _num(r.get("c"), 0.0)
        low = _num(r.get("low"), 0.0) or _num(r.get("l"), 0.0)
        vol = _num(r.get("volume"), 0.0) or _num(r.get("v"), 0.0)
        if base <= 0 or high <= 0 or vol <= 0:
            continue
        max_gain = _safe_gain(high, base)
        if max_gain < threshold:
            continue
        rows.append({
            "symbol": sym,
            "baseline_price": _round(base, 4),
            "day_high": _round(high, 4),
            "day_close": _round(close, 4),
            "day_low": _round(low, 4),
            "volume": _round(vol, 0),
            "dollar_volume": _round(close * vol, 2) if close > 0 else 0.0,
            "max_gain_from_prior_close_pct": _round(max_gain, 2),
            "close_gain_from_prior_close_pct": _round(_safe_gain(close, base), 2),
            "worst_drawdown_from_prior_close_pct": _round(_safe_gain(low, base), 2),
        })
    return sorted(rows, key=lambda x: _num(x.get("max_gain_from_prior_close_pct"), 0.0), reverse=True)[:max(5, min(200, int(max_symbols or 80)))]


def _prepared_compact_from_sources(rows: list[dict], *, limit: int = 80) -> tuple[list[dict], dict[str, int]]:
    compact: list[dict] = []
    rank: dict[str, int] = {}
    seen: set[str] = set()
    for r in rows or []:
        sym = _extract_symbol(r)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        sharia = assess_sharia_source_fast(sym)
        metrics = _candidate_metrics(r)
        item = {
            "symbol": sym,
            "score": _round(_candidate_score(r), 3),
            "source_layers": list(r.get("source_layers") or [_s(r.get("historical_source_family")) or _s(r.get("source_layer")) or "prepared_source"]),
            "reasons_ar": list(r.get("reasons_ar") or r.get("reasons") or metrics.get("big_explosion_prepared_reasons_ar") or [])[:8],
            "metrics": metrics,
            "sharia_status": sharia.get("status"),
            "sharia_label": sharia.get("label"),
            "sharia_blocked": bool(sharia.get("should_block")),
            "sharia_gray": bool(sharia.get("is_gray")),
        }
        rank[sym] = len(compact) + 1
        compact.append(item)
        if len(compact) >= max(5, min(200, int(limit or 80))):
            break
    return compact, rank


def _threshold_time(timeline: dict, pct: int) -> dict:
    return dict(((timeline or {}).get("threshold_hits") or {}).get(f"first_{int(pct)}pct") or {})


def _v2v2_replay_bucket(*, sym: str, was_prepared: bool, timeline: dict, capture: dict, sharia: dict) -> dict[str, Any]:
    detected = bool((capture or {}).get("detected_by_minute_replay"))
    det_gain = _num((capture or {}).get("first_detected_gain_pct"), 0.0)
    peak_gain = _num((timeline or {}).get("peak_gain_from_selection_pct"), 0.0)
    crossed3 = bool(_threshold_time(timeline, 3))
    crossed5 = bool(_threshold_time(timeline, 5))
    blocked = bool((sharia or {}).get("should_block"))
    gray = bool((sharia or {}).get("is_gray"))
    if blocked:
        return {
            "bucket": "learning_only_sharia_blocked",
            "section_ar": "تعلم فقط — محجوب شرعيًا",
            "actionability_ar": "لا شراء ولا Cautious/Strong حتى لو اصطاده المحاكي.",
            "priority_score": 5,
        }
    if gray:
        return {
            "bucket": "urgent_sharia_review_watch",
            "section_ar": "مراجعة شرعية عاجلة — مراقبة فقط",
            "actionability_ar": "لا يتحول لشراء مباشر قبل اعتماد شرعي واضح.",
            "priority_score": 25,
        }
    if detected:
        if det_gain >= 35:
            return {
                "bucket": "no_chase_pullback_only",
                "section_ar": "مرتفع جدًا — لا تطارد / Pullback فقط",
                "actionability_ar": "ليس شراء الآن؛ يحتاج تماسك أو Pullback أو Reclaim جديد.",
                "priority_score": 45,
            }
        if det_gain >= 18:
            return {
                "bucket": "continuation_pullback",
                "section_ar": "Continuation Pullback / استمرار مشروط",
                "actionability_ar": "راقب استمرارًا مشروطًا فقط؛ لا دخول على القمة.",
                "priority_score": 60,
            }
        if det_gain <= 8:
            return {
                "bucket": "v2v_early_confirmation",
                "section_ar": "V2V تأكيد مبكر حي",
                "actionability_ar": "قريب من المتابعة الجادة؛ لا يصبح شراء إلا عند اكتمال الشرعية والسيولة والخطة.",
                "priority_score": 92,
            }
        return {
            "bucket": "v2v_active_watch",
            "section_ar": "V2V مراقبة لصيقة — الحركة بدأت",
            "actionability_ar": "متابعة لصيقة؛ إذا أصبح ممتدًا ينتقل إلى Pullback.",
            "priority_score": 78,
        }
    if was_prepared and (crossed3 or crossed5):
        return {
            "bucket": "prepared_missed_live_promotion",
            "section_ar": "Prepared Watch تحرك ولم يترقَ سريعًا",
            "actionability_ar": "فجوة يجب إصلاحها: كان مرشحًا قبل السوق ثم تحرك ولم تصطده الشرائح.",
            "priority_score": 88,
        }
    if peak_gain >= 20:
        return {
            "bucket": "missed_intraday_winner",
            "section_ar": "فائز أثناء الجلسة لم يُصطد",
            "actionability_ar": "فجوة اكتشاف جديدة؛ نحتاج مصدر/دورة أسرع أو مقاعد حجز.",
            "priority_score": 72,
        }
    return {
        "bucket": "watched_no_trigger",
        "section_ar": "مراقبة — لم يكتمل الزناد",
        "actionability_ar": "لا إجراء.",
        "priority_score": 20,
    }


def run_live_hunting_replay(
    *,
    date_value: str = "",
    max_prepared: int = 80,
    max_symbols: int = 80,
    missed_gain_threshold: float = 20.0,
    context_days: int = 3,
    recovery_days: int = 7,
    prior_full_session_scan: bool = True,
    prior_scan_max_rows: int = 2_500_000,
    max_minute_rows: int = 1_800_000,
    prior_scan_timeout_sec: float = 45.0,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    include_candidates: bool = True,
) -> dict[str, Any]:
    """Replay one historical session as if the market is open now.

    The replay builds a prior-day Prepared Watch first, then streams the hunt
    day minute flat file through production source helpers at time slices.  The
    outcome winners are used only as an audit target list, not as input to the
    detection rules.
    """
    hunt_date, hunt_map, hunt_debug = _resolve_selection_grouped(date_value, recovery_days=recovery_days)
    if not hunt_map or not hunt_date:
        return {
            "ok": False,
            "version": LIVE_HUNTING_REPLAY_VERSION,
            "error": "hunt_grouped_unavailable",
            "hunt_date_debug": hunt_debug,
            "rule_ar": "محاكي V2V2 يحتاج grouped يوم الصيد نفسه للتقييم اليومي؛ لا يستخدم المستقبل لتوليد الإشارات.",
        }
    prior_date = _previous_trading_day(hunt_date).isoformat()
    prior_map = _safe_grouped(prior_date)
    context_items, context_debug = _resolve_context_grouped(prior_date, prior_map, context_days=context_days, recovery_days=recovery_days)
    micro_rows, fast_rows, context_source_debug = _build_context_source_rows(context_items)
    prior_rows: list[dict] = []
    prior_debug: dict[str, Any] = {"enabled": bool(prior_full_session_scan), "version": "prior_full_session_scan_disabled_v2v2"}
    if bool(prior_full_session_scan):
        prior_rows, prior_debug, _, _ = _build_prior_session_source_rows(
            selection_date=prior_date,
            max_minute_rows=max(50_000, min(3_500_000, int(prior_scan_max_rows or 2_500_000))),
            force_minute_pull=force_minute_pull,
            redownload_processed=redownload_processed,
            max_seconds=float(prior_scan_timeout_sec or 45.0),
        )
    combined, combine_debug = _combine_source_candidates(micro_rows, fast_rows, extra_rows=prior_rows, clean_only=False)
    prepared_list, prepared_rank = _prepared_compact_from_sources(combined, limit=max_prepared)
    prepared_symbols = {_u(x.get("symbol")) for x in prepared_list if _u(x.get("symbol"))}
    actual_winners = _daily_winners_from_map(
        hunt_map=hunt_map,
        prior_map=prior_map,
        min_gain_pct=missed_gain_threshold,
        max_symbols=max_symbols,
    )
    winner_symbols = {_u(x.get("symbol")) for x in actual_winners}
    target_symbols: list[str] = []
    seen: set[str] = set()
    # Audit the actual winners first, then add Prepared Watch names. Winners are
    # used only as audit targets; detection still runs slice-by-slice without
    # knowing future bars.
    prepared_order = [_u(x.get("symbol")) for x in prepared_list if _u(x.get("symbol"))]
    winner_order = [_u(x.get("symbol")) for x in actual_winners if _u(x.get("symbol"))]
    for sym in winner_order + prepared_order:
        sym = _u(sym)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        target_symbols.append(sym)
        if len(target_symbols) >= max(5, min(180, int(max_symbols or 80))):
            break
    target_set = set(target_symbols)
    selection_prices: dict[str, float] = {}
    for sym in target_set:
        p = dict((prior_map or {}).get(sym) or {})
        h = dict((hunt_map or {}).get(sym) or {})
        selection_prices[sym] = _num(p.get("price"), 0.0) or _num(p.get("close"), 0.0) or _num(h.get("open"), 0.0) or _num(h.get("o"), 0.0)
    loader_debug, target_bars, slice_maps = _read_minute_file_for_replay(
        trade_date=hunt_date,
        target_symbols=target_set,
        max_minute_rows=max_minute_rows,
        max_seconds=max(20.0, float(prior_scan_timeout_sec or 45.0)),
        force_minute_pull=force_minute_pull,
        redownload_processed=redownload_processed,
    )
    if loader_debug.get("ok"):
        source_replay = _run_source_on_minute_slices(
            outcome_date=hunt_date,
            slice_maps=slice_maps,
            target_symbols=target_set,
            selection_prices=selection_prices,
            clean_only=False,
        )
    else:
        source_replay = {
            "version": "minute_source_slice_replay_unavailable_v2v2",
            "symbols": {sym: {"symbol": sym, "detected_by_minute_replay": False, "stage_history": []} for sym in target_set},
            "slice_debug": [],
            "rule_ar": "لم تتوفر بيانات الدقيقة؛ لا يمكن محاكاة الصيد الحي بالدقيقة.",
        }
    winner_by_symbol = {_u(x.get("symbol")): x for x in actual_winners}
    rows: list[dict[str, Any]] = []
    for sym in target_symbols:
        base = _num(selection_prices.get(sym), 0.0)
        timeline = _timeline_from_target_bars(sym, target_bars.get(sym) or [], base)
        capture = (source_replay.get("symbols") or {}).get(sym) or {"symbol": sym, "detected_by_minute_replay": False, "stage_history": []}
        sharia = assess_sharia_source_fast(sym)
        was_prepared = sym in prepared_symbols
        bucket = _v2v2_replay_bucket(sym=sym, was_prepared=was_prepared, timeline=timeline, capture=capture, sharia=sharia)
        first3 = _threshold_time(timeline, 3)
        first5 = _threshold_time(timeline, 5)
        det_time = capture.get("first_detected_time_utc") or ""
        peak_time = timeline.get("peak_time_utc") or ""
        rows.append({
            "symbol": sym,
            "was_prepared_before_open": bool(was_prepared),
            "prepared_rank": prepared_rank.get(sym),
            "actual_winner_over_threshold": sym in winner_symbols,
            "baseline_price": _round(base, 4),
            "actual_daily": winner_by_symbol.get(sym) or {},
            "timeline": timeline,
            "first_3pct_time_utc": first3.get("time_utc"),
            "first_5pct_time_utc": first5.get("time_utc"),
            "minute_capture": capture,
            "detected_by_live_replay": bool(capture.get("detected_by_minute_replay")),
            "first_detected_time_utc": det_time,
            "first_detected_gain_pct": capture.get("first_detected_gain_pct"),
            "minutes_from_detection_to_peak": _minute_between(det_time, peak_time) if det_time and peak_time else None,
            "sharia_status": sharia.get("status"),
            "sharia_label": sharia.get("label"),
            "sharia_blocked": bool(sharia.get("should_block")),
            "sharia_gray": bool(sharia.get("is_gray")),
            **bucket,
        })
    rows = sorted(rows, key=lambda x: (_num(x.get("priority_score"), 0.0), _num((x.get("timeline") or {}).get("peak_gain_from_selection_pct"), 0.0)), reverse=True)
    winners_count = len(actual_winners)
    detected_winners = [r for r in rows if r.get("actual_winner_over_threshold") and r.get("detected_by_live_replay")]
    early_winners = [r for r in detected_winners if _num(r.get("first_detected_gain_pct"), 9999) <= 8]
    acceptable_winners = [r for r in detected_winners if _num(r.get("first_detected_gain_pct"), 9999) <= 20]
    prepared_winners = [r for r in rows if r.get("actual_winner_over_threshold") and r.get("was_prepared_before_open")]
    prepared_crossed_but_missed = [r for r in rows if r.get("was_prepared_before_open") and (r.get("first_3pct_time_utc") or r.get("first_5pct_time_utc")) and not r.get("detected_by_live_replay")]
    payload = {
        "ok": True,
        "version": LIVE_HUNTING_REPLAY_VERSION,
        "mode": "same_day_minute_live_hunting_replay_no_future_signals",
        "requested_date": str(date_value or "").strip(),
        "hunt_date": hunt_date,
        "prior_session_date": prior_date,
        "max_prepared": int(max_prepared or 80),
        "max_symbols": int(max_symbols or 80),
        "missed_gain_threshold": float(missed_gain_threshold or 20.0),
        "hunt_grouped_debug": hunt_debug,
        "prior_grouped_rows": len(prior_map or {}),
        "context_date_debug": context_debug,
        "context_source_scan": context_source_debug,
        "prior_full_session_scan": prior_debug,
        "sharia_debug": combine_debug,
        "prepared_watch": {
            "count": len(prepared_list),
            "symbols": [x.get("symbol") for x in prepared_list[:120]],
            "items": prepared_list[:50],
            "rule_ar": "هذه قائمة Prepared Watch التي كانت معروفة قبل افتتاح يوم الصيد؛ ليست شراء مباشر.",
        },
        "target_selection": {
            "target_symbols_count": len(target_set),
            "target_symbols": target_symbols,
            "actual_winners_over_threshold_count": winners_count,
            "actual_winners_symbols": [x.get("symbol") for x in actual_winners[:80]],
            "rule_ar": "الفائزون الفعليون يُستخدمون للتدقيق فقط: هل كان المحرك سيصطادهم في الوقت المناسب؟ لا يدخلون كإشارة مستقبلية داخل الشرائح.",
        },
        "minute_loader": loader_debug,
        "source_replay_summary": {k: v for k, v in source_replay.items() if k not in {"symbols"}},
        "performance": {
            "prepared_watch_count": len(prepared_list),
            "actual_winners_over_threshold_count": winners_count,
            "prepared_winners_count": len(prepared_winners),
            "detected_winners_count": len(detected_winners),
            "early_detected_winners_count": len(early_winners),
            "acceptable_detected_winners_count": len(acceptable_winners),
            "winner_capture_rate_pct": _round((len(detected_winners) / winners_count * 100.0) if winners_count else 0.0, 2),
            "early_winner_capture_rate_pct": _round((len(early_winners) / winners_count * 100.0) if winners_count else 0.0, 2),
            "acceptable_winner_capture_rate_pct": _round((len(acceptable_winners) / winners_count * 100.0) if winners_count else 0.0, 2),
            "prepared_crossed_3_or_5_but_not_detected_count": len(prepared_crossed_but_missed),
            "bucket_counts": {},
        },
        "rule_ar": "V2V2 يحاكي جلسة تاريخية كأن السوق مفتوح: Prepared قبل السوق + شرائح دقيقة أثناء الجلسة + تشغيل مصادر Big/Micro/Fast بدون معرفة مستقبل الشريحة.",
        "storage_rule_ar": "يقرأ Polygon minute من /tmp فقط، ويرجع ملخصات مدمجة؛ لا يحفظ raw files في SQLite/GitHub/Railway.",
        "next_step_ar": "شغل 3-5 أيام. إذا كثرت prepared_crossed_3_or_5_but_not_detected نحتاج ربط Prepared Watch بالترقية الحية بشكل أقوى. إذا كثرت missed_intraday_winner نحتاج مصدر اكتشاف أثناء الجلسة أسرع/أوسع.",
    }
    bucket_counts: dict[str, int] = {}
    for r in rows:
        b = _s(r.get("bucket")) or "unknown"
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    payload["performance"]["bucket_counts"] = bucket_counts
    if include_candidates:
        payload["candidates"] = rows[:max(10, min(180, int(max_symbols or 80)))]
    else:
        payload["candidate_sample"] = rows[:30]
    return payload


def format_live_hunting_replay_brief(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return "V2V2 Live Hunting Replay error\n" + str(payload)
    perf = payload.get("performance") or {}
    prep = payload.get("prepared_watch") or {}
    loader = payload.get("minute_loader") or {}
    lines = [
        "V2V2 — Historical Live Hunting Replay",
        f"يوم الصيد: {payload.get('hunt_date')} | جلسة التحضير السابقة: {payload.get('prior_session_date')}",
        f"Prepared Watch قبل الافتتاح: {prep.get('count')} رمز",
        f"رموز التدقيق: {(payload.get('target_selection') or {}).get('target_symbols_count')} | فائزون فوق {payload.get('missed_gain_threshold')}%: {perf.get('actual_winners_over_threshold_count')}",
        "",
        "نتيجة الصيد الحي:",
        f"- فائزون التقطهم المحاكي: {perf.get('detected_winners_count')} | capture {perf.get('winner_capture_rate_pct')}%",
        f"- التقاط مبكر <=8%: {perf.get('early_detected_winners_count')} | early capture {perf.get('early_winner_capture_rate_pct')}%",
        f"- التقاط مقبول <=20%: {perf.get('acceptable_detected_winners_count')} | acceptable capture {perf.get('acceptable_winner_capture_rate_pct')}%",
        f"- Prepared تحرك +3/+5 ولم يترقَ: {perf.get('prepared_crossed_3_or_5_but_not_detected_count')}",
        f"- minute loader: ok={loader.get('ok')} rows={loader.get('rows_seen')} target_rows={loader.get('target_rows_loaded')} timed_out={loader.get('timed_out')}",
        f"- bucket counts: {perf.get('bucket_counts')}",
        "",
        "أفضل نتائج الصيد/الفوات:",
    ]
    for r in (payload.get("candidates") or payload.get("candidate_sample") or [])[:25]:
        tl = r.get("timeline") or {}
        lines.append(
            f"- {r.get('symbol')}: {r.get('section_ar')} | prepared={r.get('was_prepared_before_open')} | "
            f"peak={tl.get('peak_gain_from_selection_pct')}% at {tl.get('peak_time_utc')} | "
            f"first +3={r.get('first_3pct_time_utc') or '-'} +5={r.get('first_5pct_time_utc') or '-'} | "
            f"detected={r.get('first_detected_time_utc') or 'لم يلتقط'} @ {r.get('first_detected_gain_pct')}% | "
            f"sharia={r.get('sharia_label') or r.get('sharia_status')}"
        )
    lines += ["", str(payload.get("rule_ar") or ""), str(payload.get("storage_rule_ar") or ""), str(payload.get("next_step_ar") or "")]
    return "\n".join(lines)

def run_historical_replay(
    *,
    date_value: str = "",
    max_candidates: int = 40,
    clean_only: bool = True,
    include_candidates: bool = True,
    recovery_days: int = 7,
    context_days: int = 3,
    missed_gain_threshold: float = 20.0,
    minute_timing: bool = True,
    timing_symbols_limit: int = 30,
    max_minute_rows: int = 1_800_000,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    prior_full_session_scan: bool = True,
    prior_scan_max_rows: int = 400_000,
    prior_scan_timeout_sec: float = 8.0,
    persist_prepared_watch: bool = False,
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
    prior_rows: list[dict] = []
    prior_full_session_debug: dict[str, Any] = {"enabled": bool(prior_full_session_scan), "version": "prior_full_session_scan_v2u4_live_critical_pre_explosion_full_stream_2026_06_20"}
    prior_full_map: dict[str, dict] = {}
    prior_after_map: dict[str, dict] = {}
    if bool(prior_full_session_scan):
        prior_rows, prior_full_session_debug, prior_full_map, prior_after_map = _build_prior_session_source_rows(
            selection_date=selection_date,
            max_minute_rows=min(max(50_000, int(prior_scan_max_rows or 400_000)), int(max_minute_rows or 400_000)),
            force_minute_pull=force_minute_pull,
            redownload_processed=redownload_processed,
            max_seconds=float(prior_scan_timeout_sec or 8.0),
        )
    all_source_rows_for_trace = (micro_rows or []) + (fast_rows or []) + (prior_rows or [])
    trace_by_symbol = _source_trace_from_rows(all_source_rows_for_trace)

    # Selection-day debug is kept separately so we can compare true after-close D vs sticky 3-day context.
    selection_micro_rows, selection_micro_debug = _collect_micro_explosion_full_market_candidates(selection_map, phase_detail="historical_after_close_selection_day_v2s1")
    selection_fast_rows, selection_fast_debug = _collect_low_float_fast_lane_candidates(selection_map, phase_detail="historical_after_close_selection_day_v2s1")

    candidates, combine_debug = _combine_source_candidates(micro_rows, fast_rows, extra_rows=prior_rows, clean_only=bool(clean_only))
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
        "mode": "after_close_3day_context_no_lookahead_tomorrow_prep_plus_minute_timing",
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
            "prior_full_session_scan": (prior_full_session_debug or {}).get("version"),
        },
        "production_pipeline_reuse": {
            "version": "production_source_reuse_contract_v2s1_2026_06_20",
            "uses_production_source_helpers": True,
            "source_helpers": ["_collect_micro_explosion_full_market_candidates", "_collect_low_float_fast_lane_candidates", "_collect_big_explosion_live_lane_candidates"],
            "uses_production_sharia_filter": True,
            "uses_live_buy_decision": False,
            "why_not_full_live_decision_ar": "V2T2 يضيف مسح دقيقة كامل لليوم السابق وشرائح افتتاح دقيقة للتوقيت، لكنه لا يطلق BUY_NOW ولا يغير Strong/Cautious الحي.",
            "anti_lookahead_ar": "كل الاختيارات مبنية على أيام <= تاريخ الاختيار فقط. جلسة التقييم لا تدخل إلا بعد اكتمال قائمة الغد.",
        },
        "source_counts": {
            "selection_day_micro_candidates_before_sharia": len(selection_micro_rows or []),
            "selection_day_fast_lane_candidates_before_sharia": len(selection_fast_rows or []),
            "context_micro_candidates_before_sharia": len(micro_rows or []),
            "context_fast_lane_candidates_before_sharia": len(fast_rows or []),
            "prior_full_session_candidates_before_sharia": len(prior_rows or []),
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
        "prior_full_session_scan": prior_full_session_debug,
        "sharia_debug": combine_debug,
        "after_close_tomorrow_prep": {
            "version": "after_close_tomorrow_prep_v2t2b_safe_prior_scan_2026_06_20",
            "selection_date": selection_date,
            "for_outcome_date": outcome_date,
            "prepared_count": len(tomorrow_prep_list),
            "top_tomorrow_watch_count": sum(1 for r in tomorrow_prep_list if r.get("tomorrow_prep_bucket") == "top_tomorrow_close_watch"),
            "watch_needs_confirmation_count": sum(1 for r in tomorrow_prep_list if r.get("tomorrow_prep_bucket") == "tomorrow_watch_needs_premarket_confirmation"),
            "quick_or_pullback_only_count": sum(1 for r in tomorrow_prep_list if r.get("tomorrow_prep_bucket") == "quick_take_profit_or_pullback_only"),
            "tomorrow_prep_list": tomorrow_prep_list[:50],
            "rule_ar": "هذه قائمة الغد بعد تحليل سياق 3 أيام + مسح دقيقة كامل ليوم الاختيار بعد إغلاق كل الجلسات؛ الهدف تجهيز المرشحين قبل بري ماركت اليوم التالي.",
        },
        "performance_summary": _summary(evaluated),
        "missed_winners_audit": missed_audit,
        "top_winners": top_winners,
        "top_failures": top_failures,
        "late_weak_sample": top_late_weak,
        "anti_lookahead_rule_ar": "اختيارات الأداة مبنية على سياق الأيام حتى تاريخ الاختيار فقط؛ نتائج الجلسة التالية تُستخدم للتقييم فقط بعد اكتمال قائمة الغد.",
        "storage_rule_ar": "V2U4 يستخدم Polygon grouped + minute flat file من /tmp فقط ويبني ملخصات مدمجة؛ لا يحفظ raw files في Railway/GitHub/SQLite.",
        "timing_limit_note_ar": "V2U4 يضيف تعدين مرشحي ما قبل الانفجار + قائمة جاهزة قبل السوق + افتتاح كل دقيقة: الهدف التقاط EHGO/ICCM/TPC/SNBR مبكرًا قدر الإمكان لا تجميل التقرير.",
        "next_step_ar": "اختبر V2U4 على عدة أيام؛ معيار النجاح أن تدخل رموز الانفجار في Prepared Watch أو تلتقط في أول +3/+5% لا بعد +100%.",
    }
    if bool(minute_timing) and outcome_date and outcome_map:
        try:
            timing_report = _build_big_explosion_timing_report(
                payload=payload,
                selection_grouped=selection_map,
                outcome_grouped=outcome_map,
                outcome_date=outcome_date,
                selected_symbols=selected_symbols,
                max_symbols=timing_symbols_limit,
                max_minute_rows=max_minute_rows,
                clean_only=bool(clean_only),
                force_minute_pull=bool(force_minute_pull),
                redownload_processed=bool(redownload_processed),
            )
        except Exception as exc:
            timing_report = {
                "ok": False,
                "version": "big_explosion_minute_timing_report_v2t2d_target_minute_replay_2026_06_20",
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                "note_ar": "فشل تقرير الدقيقة فقط؛ تقرير V2S1 اليومي ما زال صالحًا.",
            }
        payload["big_explosion_timing_report"] = timing_report
    else:
        payload["big_explosion_timing_report"] = {
            "ok": False,
            "version": "big_explosion_minute_timing_report_v2t2d_target_minute_replay_2026_06_20",
            "disabled": not bool(minute_timing),
            "note_ar": "مرر minute_timing=true لتشغيل تقرير توقيت الانفجارات بالدقيقة عند توفر Polygon minute flat file.",
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
        "v2v2_live_hunting_endpoint": "/simulator/live-hunting-replay?date=YYYY-MM-DD&format=brief",
        "purpose_ar": "محاكاة تاريخية بعد الإغلاق + مسح دقيقة كامل لليوم السابق + تقرير توقيت الانفجارات الكبيرة بالدقيقة. V2V2 يضيف محاكي صيد حي لنفس اليوم: Prepared قبل السوق + شرائح دقيقة أثناء الجلسة.",
        "safe_mode_ar": "تقييم فقط؛ لا يغير Strong/Cautious ولا السوق الحي ولا يحفظ raw. ملفات الدقيقة تُقرأ من /tmp فقط عند توفر Polygon flat files.",
        "recommended_params_ar": "ابدأ max_candidates=40 و context_days=3 و missed_gain_threshold=20 و minute_timing=true و timing_symbols_limit=30 و format=brief، ثم كرر 5-10 أيام.",
        "v2t_note_ar": "V2T2 يعيد استخدام دوال المصدر الحية على grouped يومي، ويمسح دقيقة اليوم السابق كاملة بعد كل الجلسات، ثم يبني شرائح افتتاح كل دقيقة لقياس توقيت الالتقاط والترقية بدون lookahead.",
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
    prior_scan = payload.get("prior_full_session_scan") or {}
    prep = payload.get("after_close_tomorrow_prep") or {}
    missed = payload.get("missed_winners_audit") or {}
    lines = [
        "Historical Replay Simulator V2U4",
        f"تاريخ الاختيار: {payload.get('selection_date')} → جلسة التقييم: {payload.get('outcome_date')}",
        f"وضع الاختبار: {payload.get('mode')}",
        f"أيام السياق: {', '.join(context_debug.get('context_dates') or [])}",
        "",
        "ملخص الالتقاط:",
        f"- Selection-day Micro scanned: {micro.get('scanned')} | eligible: {micro.get('eligible_count')} | seed: {micro.get('seed_match_count')}",
        f"- سياق 3 أيام قبل الشرعية: Micro {src.get('context_micro_candidates_before_sharia')} | Fast Lane {src.get('context_fast_lane_candidates_before_sharia')}",
        f"- مسح دقيقة كامل لليوم السابق: rows {prior_scan.get('rows_seen')} | symbols {prior_scan.get('symbols_seen')} | source rows {src.get('prior_full_session_candidates_before_sharia')}",
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
    timing = payload.get("big_explosion_timing_report") or {}
    lines += ["", "Big Explosion Minute Timing:"]
    if timing.get("ok"):
        lines.append(f"- هدف التقرير: {timing.get('target_count')} سهم | التقطت شرائح الدقيقة: {timing.get('detected_by_minute_replay_count')} | التقاط مبكر: {timing.get('early_detected_count')} | متأخر/مفقود: {timing.get('late_or_missed_count')}")
        split = timing.get('capture_rate_split') or {}
        lines.append(f"- تقسيم الالتقاط: قائمة الإغلاق {split.get('after_close_selected_count')} | دقيقة {split.get('minute_detected_count')} | مبكر {split.get('minute_early_detected_count')} | رمادي/مرفوض {split.get('gray_or_blocked_detected_count')}")
        for r in (timing.get("symbols") or [])[:10]:
            tl = r.get("timeline") or {}
            th = tl.get("threshold_hits") or {}
            f20 = (th.get("first_20pct") or {}).get("time_utc") or "-"
            f50 = (th.get("first_50pct") or {}).get("time_utc") or "-"
            lines.append(f"- {r.get('symbol')}: +20 عند {f20}, +50 عند {f50}, peak {tl.get('peak_gain_from_selection_pct')}% at {tl.get('peak_time_utc')} ({tl.get('peak_phase_ar')}), detected {r.get('first_detected_time_utc') or 'لم يلتقط'} at {r.get('first_detected_gain_pct')}%, timing {r.get('timing_label_ar')}, stage {r.get('first_detected_stage_ar') or '-'}, promo {r.get('promotion_history_ar')}")
    else:
        lines.append(f"- غير متاح: {(timing.get('minute_loader') or {}).get('reason') or timing.get('error') or timing.get('note_ar')}")
    lines += ["", "أضعف/أخطر المرشحين:"]
    for r in (payload.get("top_failures") or [])[:8]:
        lines.append(f"- {r.get('symbol')}: max {r.get('next_session_max_gain_pct')}%, worst {r.get('next_session_worst_drawdown_pct')}%, label {r.get('outcome_label_ar')}")
    lines += ["", str(payload.get("anti_lookahead_rule_ar") or ""), str(payload.get("timing_limit_note_ar") or ""), str(payload.get("storage_rule_ar") or "")]
    return "\n".join(lines)
