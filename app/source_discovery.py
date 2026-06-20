"""Dynamic full-market discovery source for Stock Radar AI.

This module is intentionally limited to the *source/universe* layer.
It does not change Sharia decisions, final strong/cautious rules, entry/stop/target
logic, or live price rendering. Its job is to scan the broad market lightly,
confirm the most promising candidates with FMP live/extended quotes, then pass a
ranked symbol reserve to the existing Sharia prefilter and deep radar engine.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import scanner as _scanner
from app.live_quotes import get_live_quotes
from app.early_movement import get_weekly_priority_items
from app.polygon_weekly_builder import load_weekly_watchlist
from app.settings import FMP_API_KEY, HTTP_SESSION, POLYGON_API_KEY
from app.utils import safe_round, to_float
from app.live_ignition_engine import classify_live_ignition, live_ignition_enabled
from app.pre_move_engine import analyze_pre_move, pre_move_engine_enabled
from app.intraday_early_source_radar import (
    get_last_intraday_early_source_radar_status,
    intraday_early_source_radar_enabled,
    scan_intraday_early_source_radar,
)
try:
    from app.detection_journal import record_detection
except Exception:  # keep source layer resilient if SQLite is unavailable during import
    record_detection = None

NY_TZ = ZoneInfo("America/New_York")


def _env_bool(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(float(os.getenv(name, str(default)) or default))
    except Exception:
        value = int(default)
    if min_value is not None:
        value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


DYNAMIC_DISCOVERY_ENABLED = _env_bool("DYNAMIC_DISCOVERY_ENABLED", True)
DYNAMIC_DISCOVERY_USE_FMP_CONFIRMATION = _env_bool("DYNAMIC_DISCOVERY_USE_FMP_CONFIRMATION", True)
DYNAMIC_DISCOVERY_USE_FMP_MOVERS = _env_bool("DYNAMIC_DISCOVERY_USE_FMP_MOVERS", True)
DYNAMIC_DISCOVERY_FMP_CONFIRM_LIMIT = _env_int("DYNAMIC_DISCOVERY_FMP_CONFIRM_LIMIT", 300, 40, 300)
DYNAMIC_DISCOVERY_REFERENCE_LIMIT_PAGES = _env_int("DYNAMIC_DISCOVERY_REFERENCE_LIMIT_PAGES", 12, 4, 20)
DYNAMIC_DISCOVERY_REFERENCE_PAGE_LIMIT = _env_int("DYNAMIC_DISCOVERY_REFERENCE_PAGE_LIMIT", 1000, 100, 1000)
DYNAMIC_DISCOVERY_MIN_PREFERRED_PRICE = float(os.getenv("DYNAMIC_DISCOVERY_MIN_PREFERRED_PRICE", "2") or 2)
DYNAMIC_DISCOVERY_MAX_PREFERRED_PRICE = float(os.getenv("DYNAMIC_DISCOVERY_MAX_PREFERRED_PRICE", "300") or 300)
DYNAMIC_DISCOVERY_UNDER_2_EXCEPTION_CHANGE_PCT = float(os.getenv("DYNAMIC_DISCOVERY_UNDER_2_EXCEPTION_CHANGE_PCT", "8") or 8)
DYNAMIC_DISCOVERY_MOVER_CACHE_TTL_SEC = _env_int("DYNAMIC_DISCOVERY_MOVER_CACHE_TTL_SEC", 120, 30, 600)
LOW_FLOAT_FAST_LANE_ENABLED = _env_bool("LOW_FLOAT_FAST_LANE_ENABLED", True)
LOW_FLOAT_FAST_LANE_SCAN_CAP = _env_int("LOW_FLOAT_FAST_LANE_SCAN_CAP", 6000, 300, 9000)
LOW_FLOAT_FAST_LANE_INJECT_LIMIT = _env_int("LOW_FLOAT_FAST_LANE_INJECT_LIMIT", 220, 30, 350)
LOW_FLOAT_FAST_LANE_MAX_PRICE = float(os.getenv("LOW_FLOAT_FAST_LANE_MAX_PRICE", "12") or 12)
LOW_FLOAT_FAST_LANE_EXTENDED_MAX_PRICE = float(os.getenv("LOW_FLOAT_FAST_LANE_EXTENDED_MAX_PRICE", "20") or 20)
LOW_FLOAT_FAST_LANE_MIN_PRICE = float(os.getenv("LOW_FLOAT_FAST_LANE_MIN_PRICE", "0.35") or 0.35)
LOW_FLOAT_FAST_LANE_MAX_DOLLAR_VOLUME = float(os.getenv("LOW_FLOAT_FAST_LANE_MAX_DOLLAR_VOLUME", "60000000") or 60_000_000)

_FMP_MOVERS_CACHE: dict = {"ts": 0.0, "rows": [], "error": ""}
_LAST_DYNAMIC_DISCOVERY_STATUS: dict = {}


def dynamic_discovery_enabled() -> bool:
    return bool(DYNAMIC_DISCOVERY_ENABLED)


def _phase_detail(now: datetime | None = None) -> dict:
    now = now or datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return {"phase": "closed", "detail": "weekend", "interval_sec": 3600, "target": 150, "label": "عطلة السوق"}
    t = now.time()
    mins = now.hour * 60 + now.minute
    if dt_time(4, 0) <= t < dt_time(7, 0):
        return {"phase": "pre_market", "detail": "pre_market_early", "interval_sec": 1200, "target": 150, "label": "قبل الافتتاح المبكر"}
    if dt_time(7, 0) <= t < dt_time(9, 30):
        return {"phase": "pre_market", "detail": "pre_market_active", "interval_sec": 600, "target": 220, "label": "قبل الافتتاح النشط"}
    if dt_time(9, 30) <= t < dt_time(10, 30):
        return {"phase": "open", "detail": "open_first_hour", "interval_sec": 420, "target": 240, "label": "أول ساعة تداول"}
    if dt_time(10, 30) <= t < dt_time(15, 0):
        return {"phase": "open", "detail": "open_mid_session", "interval_sec": 720, "target": 210, "label": "وسط الجلسة"}
    if dt_time(15, 0) <= t <= dt_time(16, 0):
        return {"phase": "open", "detail": "open_last_hour", "interval_sec": 480, "target": 230, "label": "آخر ساعة تداول"}
    if dt_time(16, 0) < t <= dt_time(18, 0):
        return {"phase": "after_hours", "detail": "after_hours_early", "interval_sec": 600, "target": 220, "label": "بعد الإغلاق النشط"}
    if dt_time(18, 0) < t <= dt_time(20, 0):
        return {"phase": "after_hours", "detail": "after_hours_late", "interval_sec": 1200, "target": 180, "label": "بعد الإغلاق المتأخر"}
    return {"phase": "closed", "detail": "overnight_closed", "interval_sec": 3600, "target": 150, "label": "السوق مغلق"}


def get_full_market_scan_interval_sec() -> int:
    return int(_phase_detail().get("interval_sec", 1800) or 1800)


def get_recommended_deep_scan_target(default: int = 190) -> int:
    try:
        phase = _phase_detail()
        target = int(phase.get("target") or default or 190)
        # Keep the user's preference: enough choices, but not an inflated noisy list.
        return max(120, min(260, target))
    except Exception:
        return int(default or 190)


def get_last_dynamic_discovery_status() -> dict:
    return dict(_LAST_DYNAMIC_DISCOVERY_STATUS or {})


def _clean_symbol(symbol) -> str:
    try:
        s = str(symbol or "").upper().strip()
        if not s:
            return ""
        if not all(ch.isalnum() or ch in {".", "-"} for ch in s):
            return ""
        return s
    except Exception:
        return ""


def _add_candidate(candidates: dict, symbol: str, score: float, source: str, reason: str = "", metrics: dict | None = None) -> None:
    s = _clean_symbol(symbol)
    if not s:
        return
    row = candidates.setdefault(s, {"symbol": s, "score": 0.0, "sources": set(), "reasons": [], "metrics": {}})
    try:
        row["score"] = float(row.get("score", 0.0) or 0.0) + float(score or 0.0)
    except Exception:
        pass
    if source:
        row["sources"].add(str(source))
    if reason and reason not in row["reasons"]:
        row["reasons"].append(str(reason)[:80])
    if metrics:
        try:
            row["metrics"].update(metrics)
        except Exception:
            pass


def _fast_lane_source_flags(source_kind: str) -> dict:
    kind = str(source_kind or "").strip().lower()
    return {
        "from_fmp_movers": kind in {"fmp_mover", "fmp_small_mover"},
        "from_fmp_live": kind == "fmp_live",
        "from_polygon_grouped": kind in {"polygon_grouped", "grouped"},
        "from_watch_early": False,
        "from_previous_session_memory": False,
    }


def _fast_lane_trace_update(trace: dict, symbol: str, *, source_kind: str, metrics: dict | None = None,
                            score: float = 0.0, reasons: list | None = None, eligible: bool = True,
                            rejected_reason_code: str = "", rejected_reason_ar: str = "") -> None:
    """Compact in-memory V2Q trace for Fast Lane source candidates.

    This is diagnostic-only.  It does not store raw vendor payloads and does not
    change source ranking, Sharia screening, Strong, or Cautious decisions.
    """
    sym = _clean_symbol(symbol)
    if not sym:
        return
    metrics = metrics if isinstance(metrics, dict) else {}
    entry = trace.setdefault(sym, {
        "symbol": sym,
        "source_kinds": [],
        "source_flags": _fast_lane_source_flags(""),
        "price": 0.0,
        "change_pct": 0.0,
        "volume": 0.0,
        "dollar_volume": 0.0,
        "score": 0.0,
        "source_reasons_ar": [],
        "source_eligible": False,
        "source_stage": "raw_source",
        "excluded_reason_code": "",
        "excluded_reason_ar": "",
    })
    kind = str(source_kind or "unknown").strip() or "unknown"
    if kind not in entry["source_kinds"]:
        entry["source_kinds"].append(kind)
    flags = _fast_lane_source_flags(kind)
    for k, v in flags.items():
        entry["source_flags"][k] = bool(entry["source_flags"].get(k) or v)
    price = to_float(metrics.get("price") or metrics.get("fmp_price") or metrics.get("live_price"))
    change = to_float(metrics.get("change_pct") or metrics.get("fmp_change_pct") or metrics.get("live_change_pct") or metrics.get("day_change_pct"))
    # grouped metrics can be fractional; FMP/live are percentages.
    if kind in {"polygon_grouped", "grouped"} and abs(change) <= 1.5:
        change *= 100.0
    volume = to_float(metrics.get("volume") or metrics.get("fmp_volume") or metrics.get("live_volume"))
    dollar_volume = to_float(metrics.get("dollar_volume") or metrics.get("live_dollar_volume"))
    if dollar_volume <= 0 and price > 0 and volume > 0:
        dollar_volume = price * volume
    for key, val in (("price", price), ("change_pct", change), ("volume", volume), ("dollar_volume", dollar_volume)):
        if val and (not entry.get(key) or abs(float(val)) > abs(float(entry.get(key) or 0))):
            entry[key] = safe_round(val, 4 if key == "price" else 3)
    try:
        entry["score"] = safe_round(max(float(entry.get("score", 0) or 0), float(score or 0)), 3)
    except Exception:
        pass
    for reason in list(reasons or [])[:8]:
        text = str(reason or "").strip()
        if text and text not in entry["source_reasons_ar"]:
            entry["source_reasons_ar"].append(text[:120])
    if eligible:
        entry["source_eligible"] = True
        entry["source_stage"] = "source_eligible"
    elif not entry.get("source_eligible"):
        entry["source_stage"] = "source_rejected"
        entry["excluded_reason_code"] = str(rejected_reason_code or "source_rejected")
        entry["excluded_reason_ar"] = str(rejected_reason_ar or "لم يجتز شروط Fast Lane من المصدر")[:180]


def _fast_lane_funnel_debug_payload(trace: dict, ranked: list[dict], final: list[str], max_symbols: int) -> dict:
    ranked_map = {str((r or {}).get("symbol") or "").upper(): r for r in (ranked or []) if isinstance(r, dict)}
    final_set = {str(x or "").upper() for x in (final or [])}
    rows: list[dict] = []
    stage_counts: dict[str, int] = {}
    source_kind_counts: dict[str, int] = {}
    for sym, item in (trace or {}).items():
        if not isinstance(item, dict):
            continue
        out = dict(item)
        out["source_kinds"] = list(item.get("source_kinds") or [])[:5]
        for kind in out["source_kinds"]:
            source_kind_counts[kind] = int(source_kind_counts.get(kind, 0) or 0) + 1
        out["after_source_candidate_pool"] = bool(sym in ranked_map)
        out["entered_final_universe_before_sharia"] = bool(sym in final_set)
        out["source_rank_score"] = safe_round((ranked_map.get(sym) or {}).get("score", out.get("score", 0)), 3)
        out["source_tags"] = list((ranked_map.get(sym) or {}).get("sources") or [])[:8]
        if not out.get("source_eligible"):
            stage = "source_rejected"
        elif not out.get("after_source_candidate_pool"):
            stage = "not_in_candidate_pool"
            out["excluded_reason_code"] = out.get("excluded_reason_code") or "not_in_candidate_pool"
            out["excluded_reason_ar"] = out.get("excluded_reason_ar") or "لم يدخل candidate pool بعد حساب المصدر."
        elif not out.get("entered_final_universe_before_sharia"):
            stage = "source_universe_limit"
            out["excluded_reason_code"] = out.get("excluded_reason_code") or "source_universe_limit_or_lower_rank"
            out["excluded_reason_ar"] = out.get("excluded_reason_ar") or f"مرشح Fast Lane لكنه خارج أول {int(max_symbols or 0)} رمز قبل فلتر الشرعية/التحليل العميق."
        else:
            stage = "entered_source_universe"
        out["funnel_stage"] = stage
        stage_counts[stage] = int(stage_counts.get(stage, 0) or 0) + 1
        rows.append(out)
    rows.sort(key=lambda x: float(x.get("source_rank_score", x.get("score", 0)) or 0), reverse=True)
    return {
        "version": "fast_lane_funnel_debug_v2q_source_2026_06_20",
        "raw_fast_lane_source_count": len([x for x in rows if x.get("source_eligible")]),
        "trace_count": len(rows),
        "entered_source_universe_count": len([x for x in rows if x.get("entered_final_universe_before_sharia")]),
        "source_universe_limit_count": len([x for x in rows if x.get("funnel_stage") == "source_universe_limit"]),
        "stage_counts": stage_counts,
        "source_kind_counts": source_kind_counts,
        "candidate_traces": rows[:120],
        "candidate_symbols": [x.get("symbol") for x in rows if x.get("source_eligible")][:80],
        "rule_ar": "V2Q: هذا Funnel تشخيصي فقط. يشرح انتقال مرشح Fast Lane من المصدر إلى final universe قبل الشرعية ثم إلى العرض، ولا يغير Strong/Cautious أو قواعد الشراء.",
    }


def _source_metrics_from_grouped(daily: dict) -> dict:
    try:
        return _scanner.calc_metrics(daily or {})
    except Exception:
        return {}


def _score_price_preference(price: float, change_pct: float = 0.0, dollar_volume: float = 0.0) -> tuple[float, list[str], dict]:
    score = 0.0
    reasons: list[str] = []
    flags = {"under_2_deprioritized": False, "under_2_exception": False, "over_300_deprioritized": False}
    try:
        price = float(price or 0)
        change_pct = float(change_pct or 0)
        dollar_volume = float(dollar_volume or 0)
        if price <= 0:
            return -25.0, ["سعر غير متاح"], flags
        if price < DYNAMIC_DISCOVERY_MIN_PREFERRED_PRICE:
            exceptional = abs(change_pct) >= DYNAMIC_DISCOVERY_UNDER_2_EXCEPTION_CHANGE_PCT and dollar_volume >= 15_000_000
            if exceptional:
                score -= 8.0
                reasons.append("أقل من 2 دولار - استثنائي عالي المخاطر")
                flags["under_2_exception"] = True
            else:
                score -= 45.0
                reasons.append("أقل من 2 دولار - أولوية منخفضة")
                flags["under_2_deprioritized"] = True
        elif DYNAMIC_DISCOVERY_MIN_PREFERRED_PRICE <= price <= min(DYNAMIC_DISCOVERY_MAX_PREFERRED_PRICE, 300):
            score += 7.0
        elif price > DYNAMIC_DISCOVERY_MAX_PREFERRED_PRICE:
            score -= 8.0
            reasons.append("فوق السعر المفضل - أولوية أقل")
            flags["over_300_deprioritized"] = True
    except Exception:
        pass
    return score, reasons, flags




def _source_move_stage(change_pct: float) -> str:
    try:
        change_pct = float(change_pct or 0)
    except Exception:
        change_pct = 0.0
    if change_pct >= 50:
        return "catalyst_spike_review"
    if change_pct >= 20:
        return "extended_late"
    if change_pct >= 10:
        return "late_continuation"
    if change_pct >= 5:
        return "active_confirmation"
    if change_pct >= 2:
        return "early_confirmation"
    return "pre_move_or_quiet"


def _score_fmp_mover_source(change_pct: float, dollar_volume: float) -> tuple[float, str]:
    """Score FMP movers without rewarding already-large moves as early discovery.

    Older scoring rewarded higher change_pct directly.  For Source / Early
    Discovery V2, large movers still enter the candidate pool for continuation /
    no-chase review, but they should not outrank quieter early builders as
    early opportunities.
    """
    try:
        change_pct = float(change_pct or 0)
        dollar_volume = float(dollar_volume or 0)
    except Exception:
        change_pct, dollar_volume = 0.0, 0.0
    liquidity_bonus = min(max(dollar_volume, 0) / 120_000_000, 10)
    stage = _source_move_stage(change_pct)
    if stage in {"catalyst_spike_review", "extended_late"}:
        return 4.0 + min(max(dollar_volume, 0) / 200_000_000, 5), stage
    if stage == "late_continuation":
        return 9.0 + min(max(dollar_volume, 0) / 180_000_000, 6), stage
    if stage == "active_confirmation":
        return 30.0 + liquidity_bonus, stage
    if stage == "early_confirmation":
        return 36.0 + liquidity_bonus, stage
    return 18.0 + min(max(dollar_volume, 0) / 200_000_000, 5), stage


def _weekly_priority_is_clean_pre_move(pattern: str, reasons: list | None = None) -> bool:
    text = (str(pattern or "") + " " + " ".join(str(x) for x in (reasons or []))).lower()
    if any(token in text for token in ["high-risk", "continuation", "extended", "large friday", "no chase", "avoid gap chase", "after-hours follow-through"]):
        return False
    return any(token in text for token in ["pre-move", "build-up", "quiet", "early"])

def _fetch_fmp_movers() -> tuple[list[dict], str]:
    if not (FMP_API_KEY and DYNAMIC_DISCOVERY_USE_FMP_MOVERS):
        return [], "disabled_or_no_key"
    now = time.time()
    if _FMP_MOVERS_CACHE.get("rows") and now - float(_FMP_MOVERS_CACHE.get("ts", 0) or 0) < DYNAMIC_DISCOVERY_MOVER_CACHE_TTL_SEC:
        return list(_FMP_MOVERS_CACHE.get("rows") or []), "cache"
    base = str(os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com") or "https://financialmodelingprep.com").rstrip("/")
    urls = [
        f"{base}/stable/biggest-gainers?apikey={FMP_API_KEY}",
        f"{base}/api/v3/stock_market/gainers?apikey={FMP_API_KEY}",
        f"{base}/api/v3/stock_market/actives?apikey={FMP_API_KEY}",
    ]
    rows: list[dict] = []
    last_error = ""
    for url in urls:
        try:
            r = HTTP_SESSION.get(url, timeout=10)
            if r.status_code >= 400:
                last_error = f"http_{r.status_code}"
                continue
            data = r.json()
            if isinstance(data, dict):
                for key in ("data", "results", "quotes", "gainers", "actives"):
                    val = data.get(key)
                    if isinstance(val, list):
                        rows = [x for x in val if isinstance(x, dict)]
                        break
                if not rows and (data.get("symbol") or data.get("ticker")):
                    rows = [data]
            elif isinstance(data, list):
                rows = [x for x in data if isinstance(x, dict)]
            if rows:
                _FMP_MOVERS_CACHE.update({"ts": now, "rows": rows[:250], "error": ""})
                return rows[:250], "fmp"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:80]}"
            continue
    _FMP_MOVERS_CACHE.update({"ts": now, "rows": [], "error": last_error})
    return [], last_error or "empty"


def _symbol_from_mover(row: dict) -> str:
    return _clean_symbol(row.get("symbol") or row.get("ticker") or row.get("T"))


def _mover_change_pct(row: dict) -> float:
    for key in ("changesPercentage", "changePercentage", "change_pct", "changesPct", "percent", "pct_change"):
        try:
            val = row.get(key)
            if isinstance(val, str):
                val = val.replace("%", "").strip()
            num = float(val or 0)
            if num:
                return num
        except Exception:
            continue
    try:
        price = to_float(row.get("price") or row.get("last") or row.get("close"))
        prev = to_float(row.get("previousClose") or row.get("previous_close") or row.get("prevClose"))
        if price > 0 and prev > 0:
            return ((price - prev) / prev) * 100
    except Exception:
        pass
    return 0.0


def _normalize_candidate_rows(candidates: dict) -> list[dict]:
    out = []
    for sym, row in (candidates or {}).items():
        try:
            normalized = dict(row)
            normalized["symbol"] = sym
            normalized["sources"] = sorted(list(row.get("sources") or []))
            normalized["reasons"] = list(row.get("reasons") or [])[:8]
            normalized["score"] = safe_round(row.get("score", 0), 3)
            out.append(normalized)
        except Exception:
            continue
    out.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    return out




def _low_float_fast_lane_score(ticker: str, metrics: dict, phase_detail: str = "", source_kind: str = "grouped") -> tuple[bool, float, list[str], dict]:
    """Dedicated obscure small-stock / low-float-like source lane.

    V2Q keeps this lane usable when the market is closed and adds a visible funnel while Polygon grouped
    data is unavailable.  It accepts either grouped-day metrics or FMP mover/live
    metrics, but remains a *source/watch* lane only. It never creates BUY_NOW.
    """
    price = to_float((metrics or {}).get("price") or (metrics or {}).get("fmp_price") or (metrics or {}).get("live_price"))
    raw_change = to_float((metrics or {}).get("day_change_pct"))
    # grouped calc_metrics stores change as a fraction; FMP/live rows store it as percent.
    chg = raw_change * 100.0 if abs(raw_change) <= 1.5 else raw_change
    for _ck in ("change_pct", "fmp_change_pct", "live_change_pct"):
        _cv = to_float((metrics or {}).get(_ck))
        if _cv != 0:
            chg = _cv
    dollar_volume = to_float((metrics or {}).get("dollar_volume") or (metrics or {}).get("live_dollar_volume"))
    volume = to_float((metrics or {}).get("volume") or (metrics or {}).get("fmp_volume") or (metrics or {}).get("live_volume"))
    if dollar_volume <= 0 and price > 0 and volume > 0:
        dollar_volume = price * volume
    range_pct = to_float((metrics or {}).get("range_pct"))
    close_strength = to_float((metrics or {}).get("close_strength"))
    near_high = bool((metrics or {}).get("near_high"))
    reasons: list[str] = []
    score = 0.0
    flags = {
        "price": price,
        "day_change_pct": chg,
        "change_pct": chg,
        "dollar_volume": dollar_volume,
        "volume": volume,
        "range_pct": range_pct,
        "close_strength": close_strength,
        "near_high": near_high,
        "low_float_fast_lane_source_kind": source_kind,
    }
    if price <= 0:
        return False, 0.0, ["سعر غير متاح"], flags
    if price < LOW_FLOAT_FAST_LANE_MIN_PRICE:
        return False, 0.0, ["أقل من 0.35$ — خطر/ضجيج أعلى من الهدف"], flags

    core_price = price <= LOW_FLOAT_FAST_LANE_MAX_PRICE
    explosive_micro = price <= 5.0
    extended_price = LOW_FLOAT_FAST_LANE_MAX_PRICE < price <= LOW_FLOAT_FAST_LANE_EXTENDED_MAX_PRICE
    if core_price:
        score += 28 if explosive_micro else 20
        reasons.append("سعر صغير مناسب لرادار الانفجارات")
    elif extended_price:
        # 12–20$ names are allowed only if they show a real independent fast-lane clue.
        if not (abs(chg) >= 5.0 and (range_pct >= 0.045 or dollar_volume <= 35_000_000)):
            return False, 0.0, ["سعر 12–20$ بدون تمدد/نشاط مستقل كافٍ — لا يدخل Fast Lane"], flags
        score += 8
        reasons.append("سعر أعلى قليلًا لكن الحركة غير عادية")
    else:
        return False, 0.0, ["فوق نطاق Low-Float Fast Lane"], flags

    # Require smaller/ignitable liquidity.  This intentionally avoids known, liquid names.
    if 50_000 <= dollar_volume <= 800_000:
        score += 24; reasons.append("دولار فوليوم صغير قابل للاشتعال")
    elif 800_000 < dollar_volume <= 6_000_000:
        score += 20; reasons.append("دولار فوليوم متوسط صغير مناسب للمضاربة")
    elif 6_000_000 < dollar_volume <= 25_000_000:
        score += 12; reasons.append("سيولة كافية لكن ليست ضخمة")
    elif 25_000_000 < dollar_volume <= LOW_FLOAT_FAST_LANE_MAX_DOLLAR_VOLUME:
        score += 2; reasons.append("سيولة عالية نسبيًا — يحتاج دليل أقوى")
    elif dollar_volume > LOW_FLOAT_FAST_LANE_MAX_DOLLAR_VOLUME:
        score -= 30; reasons.append("سيولة كبيرة جدًا — غالبًا اسم معروف وليس Low-Float")
    else:
        # Do not throw it away while market is closed; classify as watch-only until premarket volume arrives.
        score += 3; reasons.append("سيولة منخفضة/غير واضحة — مراقبة فقط حتى يظهر حجم")

    if 0.5 <= chg < 4.0:
        score += 13; reasons.append("حركة مبكرة قبل الانفجار")
    elif 4.0 <= chg < 9.0:
        score += 18; reasons.append("نشاط قوي مبكر")
    elif 9.0 <= chg < 18.0:
        score += 11; reasons.append("حركة قوية عالية المخاطر")
    elif 18.0 <= chg < 35.0:
        score += 2; reasons.append("تحرك كبير — مراجعة خطفة فقط")
    elif chg >= 35.0:
        score -= 18; reasons.append("تحرك كبير جدًا — غالبًا متأخر")
    elif -4.0 <= chg < 0.5:
        score += 5; reasons.append("هادئ/تجميع محتمل قبل الحركة")

    # Polygon grouped has range/close position. FMP movers often do not.
    if range_pct > 0:
        if 0.025 <= range_pct <= 0.18:
            score += 12; reasons.append("نطاق يومي مناسب لبدء حركة")
        elif 0.18 < range_pct <= 0.35:
            score += 5; reasons.append("نطاق واسع — خطر أعلى لكنه قابل للمراقبة")
    if close_strength >= 0.70:
        score += 9; reasons.append("إغلاق قوي داخل النطاق")
    elif close_strength >= 0.52:
        score += 5; reasons.append("إغلاق مقبول/بناء")
    if near_high:
        score += 4; reasons.append("قريب من قمة اليوم/منطقة اختراق")

    if source_kind in {"fmp_mover", "fmp_live", "fmp_small_mover"}:
        score += 8; reasons.append("مصدر مستقل من FMP وليس من Watch/Early فقط")

    # Stronger eligibility than V2O: require an independent clue, but do not require grouped_map.
    independent_activity = (
        abs(chg) >= 0.5 or (range_pct >= 0.025) or (dollar_volume >= 50_000 and dollar_volume <= LOW_FLOAT_FAST_LANE_MAX_DOLLAR_VOLUME)
    )
    not_too_known = bool(dollar_volume <= LOW_FLOAT_FAST_LANE_MAX_DOLLAR_VOLUME or dollar_volume <= 0)
    not_already_exploded = bool(chg < 35.0)
    eligible = bool(independent_activity and not_too_known and not_already_exploded and score >= 28)
    flags.update({
        "low_float_fast_lane_score": safe_round(score, 3),
        "low_float_fast_lane_eligible": eligible,
        "low_float_fast_lane_v2p": True,
    })
    return eligible, score, reasons[:8], flags


def _low_float_metrics_from_price_change_volume(price: float, change_pct: float, volume: float, source_kind: str = "fmp_mover") -> dict:
    price = to_float(price)
    change_pct = to_float(change_pct)
    volume = to_float(volume)
    return {
        "price": price,
        "day_change_pct": change_pct,
        "change_pct": change_pct,
        "volume": volume,
        "dollar_volume": price * volume if price > 0 and volume > 0 else 0.0,
        "range_pct": 0.0,
        "close_strength": 0.0,
        "near_high": False,
        "low_float_fast_lane_source_kind": source_kind,
    }


def _collect_low_float_fast_lane_candidates(grouped_map: dict, phase_detail: str = "") -> tuple[list[dict], dict]:
    debug = {
        "version": "low_float_fast_lane_source_v2q_funnel_debug_2026_06_20",
        "enabled": bool(LOW_FLOAT_FAST_LANE_ENABLED),
        "scan_cap": int(LOW_FLOAT_FAST_LANE_SCAN_CAP),
        "inject_limit": int(LOW_FLOAT_FAST_LANE_INJECT_LIMIT),
        "scanned": 0,
        "eligible_count": 0,
        "rejected_price_or_known_count": 0,
        "top_symbols": [],
        "rule_ar": "V2Q: مصدر مستقل حقيقي مع Funnel واضح. يستخدم Polygon grouped إن توفر، ويستخدم FMP movers/live عندما يكون السوق مغلقًا ولا توجد grouped data. لا يعتمد على Watch/Early فقط.",
    }
    if not LOW_FLOAT_FAST_LANE_ENABLED or not grouped_map:
        return [], debug
    out: list[dict] = []
    # Scan broadly, not only the old reference list.  Cap keeps Railway safe.
    for idx, (ticker, daily) in enumerate((grouped_map or {}).items()):
        if idx >= LOW_FLOAT_FAST_LANE_SCAN_CAP:
            break
        sym = _clean_symbol(ticker)
        if not sym or not daily:
            continue
        debug["scanned"] += 1
        # Do NOT use the normal scanner.base_filters here. They require high
        # volume/dollar-volume and erase exactly the obscure small-stock names
        # this lane is supposed to find. Use minimal price/volume sanity only.
        metrics = _source_metrics_from_grouped(daily) or {}
        if to_float(metrics.get("price")) <= 0 or to_float(metrics.get("volume")) <= 0:
            continue
        eligible, score, reasons, flags = _low_float_fast_lane_score(sym, metrics, phase_detail=phase_detail, source_kind="polygon_grouped")
        if not eligible:
            if reasons and ("فوق نطاق" in reasons[0] or "12–20" in reasons[0] or "سيولة كبيرة" in " ".join(reasons)):
                debug["rejected_price_or_known_count"] += 1
            continue
        row = {"symbol": sym, "score": safe_round(score, 3), "reasons": reasons, "metrics": {**metrics, **flags}}
        out.append(row)
    out.sort(key=lambda r: float(r.get("score", 0) or 0), reverse=True)
    out = out[:LOW_FLOAT_FAST_LANE_INJECT_LIMIT]
    debug["eligible_count"] = len(out)
    debug["top_symbols"] = [r.get("symbol") for r in out[:30]]
    return out, debug

def build_dynamic_universe(max_symbols: int = 700) -> list[str]:
    """Return a broad ranked reserve for the existing Sharia/deep-analysis pipeline."""
    global _LAST_DYNAMIC_DISCOVERY_STATUS
    started = time.time()
    try:
        max_symbols = max(80, min(900, int(max_symbols or 700)))
    except Exception:
        max_symbols = 700

    if not DYNAMIC_DISCOVERY_ENABLED:
        base = _scanner.get_scan_universe(max_symbols=max_symbols) or []
        return base[:max_symbols]

    phase_info = _phase_detail()
    candidates: dict = {}
    price_flags = {"under_2_deprioritized": 0, "under_2_exception": 0, "over_300_deprioritized": 0}

    reference_tickers = _scanner.get_reference_tickers(
        limit_pages=DYNAMIC_DISCOVERY_REFERENCE_LIMIT_PAGES,
        page_limit=DYNAMIC_DISCOVERY_REFERENCE_PAGE_LIMIT,
    ) or []
    market_date, grouped_map, source_mode = _scanner._select_grouped_market_map()
    market_activity_mode, suggested_target, activity_stats = _scanner._classify_source_market_activity(grouped_map or {})
    intraday_early_radar_status = {}
    intraday_early_radar_count = 0
    intraday_early_radar_high_risk_count = 0
    low_float_fast_lane_status = {}
    low_float_fast_lane_count = 0
    fmp_low_float_fast_lane_count = 0
    fmp_low_float_fast_lane_symbols: list[str] = []
    live_low_float_fast_lane_count = 0
    live_low_float_fast_lane_symbols: list[str] = []
    low_float_fast_lane_trace: dict[str, dict] = {}

    # Source / Promotion V2a: explicitly inject the curated Early Movement
    # watchlist into the dynamic discovery source.  Previously it usually arrived
    # indirectly through the old baseline bucket; now diagnostics and ordering
    # know it is a weekly-priority/monitoring source, while it still passes the
    # Sharia prefilter and deep analysis like every other symbol.
    weekly_priority_count = 0
    weekly_high_risk_count = 0
    try:
        for item in get_weekly_priority_items(include_high_risk=True) or []:
            sym = str((item or {}).get("symbol") or "").upper().strip()
            if not sym:
                continue
            pattern = str((item or {}).get("pattern") or "Early Movement Watch")
            is_high_risk = pattern.lower().startswith("high-risk")
            if is_high_risk:
                weekly_high_risk_count += 1
                _add_candidate(candidates, sym, 36, "weekly_high_risk_manual", "مراقبة يدوية عالية المخاطر من قائمة الحركة المبكرة", {"weekly_pattern": pattern})
            else:
                weekly_priority_count += 1
                priority = str((item or {}).get("priority") or "medium")
                confidence = to_float((item or {}).get("confidence"))
                reasons = list((item or {}).get("reasons") or [])
                clean_pre_move = _weekly_priority_is_clean_pre_move(pattern, reasons)
                # Keep all curated Polygon names in the priority lane, but do not
                # let continuation/high-risk names dominate the early-discovery
                # source just because they came from the manual list.
                score = (64 + min(confidence, 18) + (8 if priority == "high" else 0)) if clean_pre_move else (34 + min(confidence, 10))
                _add_candidate(candidates, sym, score, "weekly_priority_watchlist", "قائمة Polygon الأسبوعية ذات أولوية مراقبة", {"weekly_pattern": pattern, "weekly_confidence": confidence, "weekly_clean_pre_move": clean_pre_move})
                if clean_pre_move:
                    _add_candidate(candidates, sym, 14, "pre_move_watch", "مرشح ما قبل الحركة من تحليل Polygon")
                else:
                    _add_candidate(candidates, sym, 10, "continuation_watch", "مرشح متابعة/Pullback من قائمة Polygon")
    except Exception:
        weekly_priority_count = 0
        weekly_high_risk_count = 0

    # Polygon Weekly Candidate Builder V1 compact output: auto-built weekly candidates
    # are injected as their own source bucket, separate from manual Early Movement.
    polygon_weekly_builder_count = 0
    try:
        weekly_payload = load_weekly_watchlist() or {}
        for item in (weekly_payload.get("candidates") or []):
            sym = str((item or {}).get("symbol") or "").upper().strip()
            if not sym:
                continue
            stage = str((item or {}).get("stage") or "Weekly Priority")
            score = to_float((item or {}).get("score"))
            reason = "Polygon Weekly Builder: " + "، ".join(list((item or {}).get("reasons") or [])[:3])
            base_score = 48 + min(score, 35) * 0.45
            if "Quiet" in stage or "Early" in stage:
                base_score += 10
            if "Continuation" in stage or "Pullback" in stage:
                base_score -= 4
            _add_candidate(candidates, sym, base_score, "polygon_weekly_builder", reason, {
                "polygon_weekly_builder_score": score,
                "polygon_weekly_stage": stage,
                "polygon_weekly_last_close": (item or {}).get("last_close"),
                "polygon_weekly_watch_zone_low": (item or {}).get("suggested_watch_zone_low"),
                "polygon_weekly_watch_zone_high": (item or {}).get("suggested_watch_zone_high"),
            })
            polygon_weekly_builder_count += 1
    except Exception:
        polygon_weekly_builder_count = 0

    # Keep the old engine as one bucket only. It no longer owns the entire source list.
    old_baseline_limit = min(260, max(120, int(max_symbols * 0.38)))
    old_baseline = []
    baseline_error = ""
    try:
        old_baseline = _scanner.get_scan_universe(max_symbols=old_baseline_limit) or []
    except Exception as exc:
        baseline_error = f"{type(exc).__name__}: {str(exc)[:100]}"
        old_baseline = _scanner.get_seed_universe()[:80]
    for idx, sym in enumerate(old_baseline):
        _add_candidate(candidates, sym, max(10.0, 46.0 - (idx * 0.08)), "baseline", "منبع أساسي سابق")

    # Broad market discovery from Polygon grouped data.
    grouped_tradable = 0
    grouped_scored = 0
    if grouped_map:
        for ticker in reference_tickers or list(grouped_map.keys()):
            daily = (grouped_map or {}).get(ticker)
            if not daily:
                continue
            try:
                if not _scanner.base_filters(daily):
                    continue
            except Exception:
                continue
            grouped_tradable += 1
            m = _source_metrics_from_grouped(daily)
            if not m:
                continue
            grouped_scored += 1
            price = float(m.get("price", 0) or 0)
            chg = float(m.get("day_change_pct", 0) or 0) * 100.0
            dollar_volume = float(m.get("dollar_volume", 0) or 0)
            source_score = 0.0
            try:
                source_score = float(_scanner.score_source_candidate(ticker, daily) or -9999)
            except Exception:
                source_score = -9999
            if source_score != -9999:
                pref_score, pref_reasons, flags = _score_price_preference(price, chg, dollar_volume)
                for key in price_flags:
                    if flags.get(key):
                        price_flags[key] += 1
                total_score = source_score + pref_score
                _add_candidate(candidates, ticker, total_score, "polygon_grouped", "مسح سوق شامل من Polygon", m)
                for reason in pref_reasons:
                    _add_candidate(candidates, ticker, 0, "price_preference", reason)

            # Explicit live-discovery buckets: give them names visible in diagnostics/source tags.
            close_strength = float(m.get("close_strength", 0) or 0)
            range_pct = float(m.get("range_pct", 0) or 0)
            if chg >= 10.0:
                _add_candidate(candidates, ticker, 8 + min(dollar_volume / 180_000_000, 6), "late_mover_review", "متحرك كبير من Polygon — استمرار/لا تطارد وليس اكتشاف مبكر", {**m, "late_move_change_pct": chg})
            elif chg >= 4.0 and close_strength >= 0.60:
                _add_candidate(candidates, ticker, 22 + min(chg, 9), "top_mover", "متحرك قوي مبكر/متوسط اليوم", m)
            if 7.0 <= chg < 10.0 and close_strength >= 0.70 and range_pct <= 0.22:
                _add_candidate(candidates, ticker, 26 + min(chg, 10), "runner", "مرشح استمرار يومي قبل المطاردة", m)
            if dollar_volume >= 50_000_000:
                _add_candidate(candidates, ticker, 10 + min(dollar_volume / 150_000_000, 18), "volume_spike", "سيولة/حجم غير عادي", m)
            if bool(m.get("near_high")) and close_strength >= 0.68:
                _add_candidate(candidates, ticker, 16, "near_high", "قريب من قمة اليوم/اختراق")
            if -2.0 <= chg <= 4.5 and close_strength >= 0.62 and 0.015 <= range_pct <= 0.12:
                _add_candidate(candidates, ticker, 12, "constructive", "تهيئة بنّاءة")
            try:
                pre_meta = analyze_pre_move({**(m or {}), "day_change_pct": chg, "source_reason": "polygon_grouped"}) if pre_move_engine_enabled() else {}
                if pre_meta.get("pre_move_watch_eligible"):
                    _add_candidate(candidates, ticker, 20 + float(pre_meta.get("pre_move_score", 0) or 0) * 0.15, "pre_move_engine_v2", "Pre-Move Engine V2: " + "، ".join((pre_meta.get("pre_move_reasons") or [])[:2]), {**m, "pre_move_score": pre_meta.get("pre_move_score")})
            except Exception:
                pass


    # Low-Float Fast Lane V1: independent from Watch/Early/baseline.  This is
    # the user's high-priority radar for obscure small-stock candidates before
    # premarket/open.  It only adds source candidates; no BUY/Cautious changes.
    try:
        low_float_rows, low_float_fast_lane_status = _collect_low_float_fast_lane_candidates(
            grouped_map or {},
            phase_detail=str(phase_info.get("detail", "") or ""),
        )
        low_float_fast_lane_count = len(low_float_rows or [])
        for item in low_float_rows or []:
            sym = _clean_symbol((item or {}).get("symbol"))
            if not sym:
                continue
            score = float((item or {}).get("score", 0) or 0)
            reasons = list((item or {}).get("reasons") or [])
            metrics = dict((item or {}).get("metrics") or {})
            _fast_lane_trace_update(low_float_fast_lane_trace, sym, source_kind="polygon_grouped", metrics=metrics, score=score, reasons=reasons, eligible=True)
            _add_candidate(
                candidates,
                sym,
                52 + score * 0.35,
                "low_float_fast_lane_v1",
                "Low-Float Fast Lane: " + "، ".join(reasons[:3]),
                {**metrics, "low_float_fast_lane": True, "low_float_fast_lane_score": score, "low_float_fast_lane_reasons": reasons},
            )
            if record_detection is not None:
                try:
                    record_detection(
                        sym,
                        price=float(metrics.get("price", 0) or 0),
                        change_pct=float(metrics.get("day_change_pct", 0) or 0) * 100.0,
                        source_reason="Low-Float Fast Lane V1: " + "، ".join(reasons[:3]),
                        source_layer="low_float_fast_lane_v1",
                        source_tags=["low_float_fast_lane_v1", "small_stock_explosive_source"],
                        move_stage="High-Risk Pre-Open Watch",
                        early_or_late_detection="high_risk_early",
                    )
                except Exception:
                    pass
    except Exception as exc:
        low_float_fast_lane_status = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}



    # Intraday Early Source Radar V1: a clean, separate source-layer radar for
    # calm intraday ramps and dip-then-reclaim moves.  It does not create BUY
    # decisions.  It only adds early candidates to the universe so the existing
    # deep analysis and final decision engine can evaluate them before they
    # become late/no-chase.
    try:
        if intraday_early_source_radar_enabled() and grouped_map:
            intraday_early_radar_status = scan_intraday_early_source_radar(
                grouped_map,
                reference_tickers,
                source_mode=source_mode,
            ) or {}
            for item in intraday_early_radar_status.get("candidates", []) or []:
                sym = _clean_symbol((item or {}).get("symbol"))
                if not sym:
                    continue
                lane = str((item or {}).get("lane") or "intraday_early_ramp")
                score = float((item or {}).get("score", 0) or 0)
                reasons = list((item or {}).get("reasons") or [])
                blockers = list((item or {}).get("blockers") or [])
                metrics = dict((item or {}).get("metrics") or {})
                is_high_risk = bool((item or {}).get("high_risk", False))
                if lane in {"high_risk_live_mover", "high_risk_late_mover_review"} or is_high_risk:
                    intraday_early_radar_high_risk_count += 1
                    _add_candidate(
                        candidates,
                        sym,
                        max(8.0, score),
                        "high_risk_live_mover",
                        "مراقبة حركة مبكرة عالية المخاطر: " + "، ".join(reasons[:2]),
                        {**metrics, "intraday_early_source_lane": lane, "intraday_early_source_score": score},
                    )
                elif lane == "dip_reclaim_radar":
                    intraday_early_radar_count += 1
                    _add_candidate(
                        candidates,
                        sym,
                        max(18.0, score),
                        "dip_reclaim_radar",
                        "استعادة بعد نزول داخل اليوم: " + "، ".join(reasons[:2]),
                        {**metrics, "intraday_early_source_lane": lane, "intraday_early_source_score": score},
                    )
                elif lane == "quiet_accumulation_radar":
                    intraday_early_radar_count += 1
                    _add_candidate(
                        candidates,
                        sym,
                        max(14.0, score),
                        "quiet_accumulation_radar",
                        "تجميع هادئ داخل اليوم: " + "، ".join(reasons[:2]),
                        {**metrics, "intraday_early_source_lane": lane, "intraday_early_source_score": score},
                    )
                elif lane == "late_intraday_mover_review":
                    _add_candidate(
                        candidates,
                        sym,
                        max(5.0, score),
                        "late_mover_review",
                        "مراجعة متحرك متأخر من رادار الحركة المبكرة",
                        {**metrics, "intraday_early_source_lane": lane, "intraday_early_source_score": score},
                    )
                else:
                    intraday_early_radar_count += 1
                    _add_candidate(
                        candidates,
                        sym,
                        max(18.0, score),
                        "intraday_early_ramp",
                        "رادار صعود مبكر داخل اليوم: " + "، ".join(reasons[:2]),
                        {**metrics, "intraday_early_source_lane": lane, "intraday_early_source_score": score},
                    )
                if record_detection is not None and lane not in {"late_intraday_mover_review", "high_risk_late_mover_review"}:
                    try:
                        move_stage = "High-Risk Watch" if is_high_risk else "Early Confirmation"
                        early_late = "high_risk" if is_high_risk else "early"
                        record_detection(
                            sym,
                            price=float(metrics.get("price", 0) or 0),
                            change_pct=float(metrics.get("change_pct", 0) or 0),
                            source_reason="Intraday Early Source Radar V1: " + "، ".join(reasons[:3]),
                            source_layer=lane,
                            source_tags=["intraday_early_source_radar", lane],
                            move_stage=move_stage,
                            early_or_late_detection=early_late,
                        )
                    except Exception:
                        pass
    except Exception as exc:
        intraday_early_radar_status = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}

    # FMP movers are source candidates only; they still pass Sharia and deep analysis later.
    fmp_movers, fmp_movers_source = _fetch_fmp_movers()
    fmp_mover_count = 0
    for row in fmp_movers or []:
        sym = _symbol_from_mover(row)
        if not sym:
            continue
        change_pct = _mover_change_pct(row)
        price = to_float(row.get("price") or row.get("last") or row.get("close"))
        volume = to_float(row.get("volume") or row.get("dayVolume"))
        dollar_volume = price * volume if price > 0 and volume > 0 else 0.0
        if price and price < 1.5:
            _fast_lane_trace_update(low_float_fast_lane_trace, sym, source_kind="fmp_mover", metrics=_low_float_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_mover"), eligible=False, rejected_reason_code="price_below_source_min", rejected_reason_ar="السعر أقل من 1.5$ في مصدر FMP movers؛ لم يدخل مصدر Fast Lane الحالي.")
            continue
        fmp_mover_count += 1
        score, source_move_stage = _score_fmp_mover_source(change_pct, dollar_volume)
        pref_score, pref_reasons, flags = _score_price_preference(price, change_pct, dollar_volume)
        for key in price_flags:
            if flags.get(key):
                price_flags[key] += 1
        mover_source = "fmp_movers" if change_pct < 10 else "late_mover_review"
        mover_reason = "قائمة رابحين/نشطين من FMP" if change_pct < 10 else "متحرك متأخر من FMP — استمرار/لا تطارد"
        _add_candidate(candidates, sym, score + pref_score, mover_source, mover_reason, {"fmp_change_pct": change_pct, "fmp_price": price, "fmp_volume": volume, "source_move_stage": source_move_stage})
        try:
            lf_metrics = _low_float_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_mover")
            lf_ok, lf_score, lf_reasons, lf_flags = _low_float_fast_lane_score(sym, lf_metrics, phase_detail=str(phase_info.get("detail", "") or ""), source_kind="fmp_mover")
            if lf_ok:
                _fast_lane_trace_update(low_float_fast_lane_trace, sym, source_kind="fmp_mover", metrics=lf_metrics, score=lf_score, reasons=lf_reasons, eligible=True)
                _add_candidate(
                    candidates,
                    sym,
                    58 + lf_score * 0.42,
                    "low_float_fast_lane_v1",
                    "Low-Float Fast Lane V2Q من FMP: " + "، ".join(lf_reasons[:3]),
                    {**lf_metrics, **lf_flags, "low_float_fast_lane": True, "low_float_fast_lane_reasons": lf_reasons},
                )
                fmp_low_float_fast_lane_count += 1
                fmp_low_float_fast_lane_symbols.append(sym)
        except Exception:
            pass
        try:
            ignition = classify_live_ignition(sym, {"price": price, "change_pct": change_pct, "volume": volume, "dollar_volume": dollar_volume}) if live_ignition_enabled() else {}
            if ignition.get("hot_lane_eligible"):
                _add_candidate(candidates, sym, 42 + float(ignition.get("ignition_score", 0) or 0) * 0.25, "live_ignition_hot_lane", "Hot Lane: بداية حركة مبكرة بسيولة", {"live_ignition_score": ignition.get("ignition_score"), "live_ignition_stage": ignition.get("stage_hint"), "fmp_change_pct": change_pct, "fmp_price": price, "fmp_volume": volume})
                if record_detection is not None:
                    record_detection(sym, price=price, change_pct=change_pct, source_reason="Live Ignition Hot Lane من FMP movers", source_layer="live_ignition_hot_lane", source_tags=["fmp_movers", "live_ignition_hot_lane"], move_stage="Early Confirmation", early_or_late_detection="early")
            elif change_pct >= 10:
                _add_candidate(candidates, sym, 8, "late_mover_review", "متحرك متأخر — استمرار/لا تطارد وليس مراقبة مبكرة", {"late_move_change_pct": change_pct, "fmp_price": price, "fmp_volume": volume})
                if record_detection is not None:
                    stage = "Catalyst Spike Review" if change_pct >= 50 else "Extended" if change_pct >= 20 else "Continuation Watch"
                    record_detection(sym, price=price, change_pct=change_pct, source_reason="FMP mover late review", source_layer="late_mover_review", source_tags=["fmp_movers", "late_mover_review"], move_stage=stage, early_or_late_detection="late")
        except Exception:
            pass
        for reason in pref_reasons:
            _add_candidate(candidates, sym, 0, "price_preference", reason)

    # Confirm only the best candidates with FMP live/extended quotes. No price cache is used here.
    rows_before_confirm = _normalize_candidate_rows(candidates)
    fmp_confirm_symbols = [r["symbol"] for r in rows_before_confirm[:DYNAMIC_DISCOVERY_FMP_CONFIRM_LIMIT]]
    fmp_quotes = {}
    fmp_diag = {}
    if DYNAMIC_DISCOVERY_USE_FMP_CONFIRMATION and FMP_API_KEY and fmp_confirm_symbols:
        try:
            bundle = get_live_quotes(fmp_confirm_symbols, prefer_cache=False, allow_fallback=False)
            if isinstance(bundle, dict):
                fmp_quotes = bundle.get("quotes", {}) or {}
                fmp_diag = bundle.get("diagnostics", {}) or {}
        except Exception as exc:
            fmp_diag = {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}

    live_confirmed = 0
    extended_confirmed = 0
    for sym, quote in (fmp_quotes or {}).items():
        price = to_float((quote or {}).get("price"))
        if price <= 0:
            continue
        live_confirmed += 1
        change_pct = to_float((quote or {}).get("change_pct"))
        volume = to_float((quote or {}).get("volume"))
        dollar_volume = price * volume if volume > 0 else 0.0
        live_stage = _source_move_stage(change_pct)
        live_score = 8.0
        if 2.0 <= change_pct < 5.0:
            live_score += 12.0
        elif 5.0 <= change_pct < 10.0:
            live_score += 20.0
        elif change_pct >= 10.0:
            # Keep late movers visible for review, but do not let live-confirmed
            # +10% names outrank early builders as fresh opportunities.
            live_score += 2.0
        if bool((quote or {}).get("extended_hours")):
            live_score += 8.0
            extended_confirmed += 1
        if volume >= 1_000_000:
            live_score += min(volume / 3_000_000, 10)
        pref_score, pref_reasons, flags = _score_price_preference(price, change_pct, dollar_volume)
        for key in price_flags:
            if flags.get(key):
                price_flags[key] += 1
        _add_candidate(candidates, sym, live_score + pref_score, "fmp_live_confirmed", "تأكيد سعر حي من FMP", {"live_price": price, "live_change_pct": change_pct, "live_volume": volume})
        try:
            lf_metrics = _low_float_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_live")
            lf_ok, lf_score, lf_reasons, lf_flags = _low_float_fast_lane_score(sym, lf_metrics, phase_detail=str(phase_info.get("detail", "") or ""), source_kind="fmp_live")
            if lf_ok:
                _fast_lane_trace_update(low_float_fast_lane_trace, sym, source_kind="fmp_live", metrics=lf_metrics, score=lf_score, reasons=lf_reasons, eligible=True)
                _add_candidate(
                    candidates,
                    sym,
                    48 + lf_score * 0.32,
                    "low_float_fast_lane_v1",
                    "Low-Float Fast Lane V2Q من FMP live: " + "، ".join(lf_reasons[:3]),
                    {**lf_metrics, **lf_flags, "low_float_fast_lane": True, "low_float_fast_lane_reasons": lf_reasons},
                )
                live_low_float_fast_lane_count += 1
                live_low_float_fast_lane_symbols.append(sym)
        except Exception:
            pass
        if change_pct >= 4.0:
            _add_candidate(candidates, sym, 14, "live_mover", "الحركة الحية مستمرة")
        try:
            ignition = classify_live_ignition(sym, {"price": price, "change_pct": change_pct, "volume": volume, "dollar_volume": dollar_volume}) if live_ignition_enabled() else {}
            if ignition.get("hot_lane_eligible"):
                _add_candidate(candidates, sym, 46 + float(ignition.get("ignition_score", 0) or 0) * 0.25, "live_ignition_hot_lane", "Hot Lane: بداية حركة مؤكدة بسعر حي", {"live_ignition_score": ignition.get("ignition_score"), "live_ignition_stage": ignition.get("stage_hint"), "live_price": price, "live_change_pct": change_pct, "live_volume": volume})
                if record_detection is not None:
                    record_detection(sym, price=price, change_pct=change_pct, source_reason="Live Ignition Hot Lane من FMP live confirmation", source_layer="live_ignition_hot_lane", source_tags=["fmp_live_confirmed", "live_ignition_hot_lane"], move_stage="Early Confirmation", early_or_late_detection="early")
            elif change_pct >= 10:
                _add_candidate(candidates, sym, 6, "late_mover_review", "تأكيد حي متأخر — استمرار/لا تطارد", {"late_move_change_pct": change_pct, "live_price": price, "live_volume": volume})
        except Exception:
            pass
        for reason in pref_reasons:
            _add_candidate(candidates, sym, 0, "price_preference", reason)

    ranked = _normalize_candidate_rows(candidates)

    def from_source(source: str, limit: int) -> list[str]:
        selected = []
        for row in ranked:
            if source in (row.get("sources") or []):
                selected.append(row["symbol"])
            if len(selected) >= limit:
                break
        return selected

    # Balanced order: V2a gives known weekly-priority names a front-row seat,
    # then today's live/new movers, with the old baseline as support only.
    selected_order = []
    # Official launch source order: early/prepared lanes first, then live ignition,
    # then constructive liquidity.  Late movers stay visible for review, but they
    # must never crowd out early builders or weekly-priority names.
    selected_order += from_source("low_float_fast_lane_v1", min(160, max_symbols))
    selected_order += from_source("weekly_priority_watchlist", min(110, max_symbols))
    selected_order += from_source("polygon_weekly_builder", min(90, max_symbols))
    selected_order += from_source("intraday_early_ramp", min(140, max_symbols))
    selected_order += from_source("dip_reclaim_radar", min(120, max_symbols))
    selected_order += from_source("quiet_accumulation_radar", min(90, max_symbols))
    selected_order += from_source("pre_move_engine_v2", min(120, max_symbols))
    selected_order += from_source("pre_move_watch", min(70, max_symbols))
    selected_order += from_source("live_ignition_hot_lane", min(120, max_symbols))
    selected_order += from_source("constructive", min(120, max_symbols))
    selected_order += from_source("near_high", min(100, max_symbols))
    selected_order += from_source("volume_spike", min(110, max_symbols))
    selected_order += from_source("runner", min(90, max_symbols))
    selected_order += from_source("fmp_live_confirmed", min(120, max_symbols))
    selected_order += from_source("fmp_movers", min(80, max_symbols))
    selected_order += from_source("continuation_watch", min(35, max_symbols))
    selected_order += from_source("weekly_high_risk_manual", min(15, max_symbols))
    selected_order += from_source("high_risk_live_mover", min(20, max_symbols))
    selected_order += from_source("late_mover_review", min(25, max_symbols))
    selected_order += from_source("top_mover", min(60, max_symbols))
    selected_order += from_source("baseline", min(220, max_symbols))
    selected_order += [r["symbol"] for r in ranked]
    selected_order += _scanner.get_seed_universe()

    final = _scanner.unique_keep_order(selected_order)[:max_symbols]
    reason_map = {r["symbol"]: r for r in ranked}
    low_float_fast_lane_funnel_debug = _fast_lane_funnel_debug_payload(low_float_fast_lane_trace, ranked, final, max_symbols)

    elapsed = safe_round(time.time() - started, 2)
    source_bucket_counts = {}
    for row in ranked:
        for src in row.get("sources") or []:
            source_bucket_counts[src] = int(source_bucket_counts.get(src, 0) or 0) + 1

    try:
        low_float_fast_lane_status = dict(low_float_fast_lane_status or {})
        low_float_fast_lane_status.update({
            "v2p_fmp_fallback_enabled": True,
            "v2q_funnel_debug_enabled": True,
            "fmp_fast_lane_count": int(fmp_low_float_fast_lane_count or 0),
            "fmp_fast_lane_symbols": _scanner.unique_keep_order(fmp_low_float_fast_lane_symbols)[:50],
            "live_fast_lane_count": int(live_low_float_fast_lane_count or 0),
            "live_fast_lane_symbols": _scanner.unique_keep_order(live_low_float_fast_lane_symbols)[:50],
            "total_fast_lane_source_count": int(source_bucket_counts.get("low_float_fast_lane_v1", 0) or 0),
            "funnel_debug_version": (low_float_fast_lane_funnel_debug or {}).get("version"),
            "raw_fast_lane_source_count": int((low_float_fast_lane_funnel_debug or {}).get("raw_fast_lane_source_count", 0) or 0),
            "entered_source_universe_count": int((low_float_fast_lane_funnel_debug or {}).get("entered_source_universe_count", 0) or 0),
            "source_universe_limit_count": int((low_float_fast_lane_funnel_debug or {}).get("source_universe_limit_count", 0) or 0),
            "diagnostic_ar": "إذا كان broad_market_count=0 وقت الإغلاق، يستخدم V2Q FMP movers/live كمنبع مستقل، ويعرض Funnel يشرح لماذا دخل/خرج كل مرشح Fast Lane.",
        })
    except Exception:
        pass

    diag = {
        "engine_version": "dynamic_discovery_v3d_fast_lane_funnel_debug_2026_06_20",
        "dynamic_discovery_enabled": True,
        "dynamic_discovery_mode": "candidate_pool_plus_true_low_float_fast_lane_funnel_debug",
        "requested_target": int(max_symbols),
        "target": int(max_symbols),
        "selected_count": len(final),
        "market_date": market_date,
        "source_mode": source_mode,
        "phase_detail": phase_info.get("detail", ""),
        "phase_label": phase_info.get("label", ""),
        "recommended_deep_scan_target": get_recommended_deep_scan_target(190),
        "next_scan_interval_sec": int(phase_info.get("interval_sec", 0) or 0),
        "market_activity_mode": market_activity_mode,
        "suggested_dynamic_target": suggested_target,
        "activity_stats": activity_stats,
        "broad_market_count": len(grouped_map or {}),
        "reference_count": len(reference_tickers or []),
        "grouped_tradable_count": grouped_tradable,
        "grouped_scored_count": grouped_scored,
        "candidate_count_before_confirm": len(rows_before_confirm),
        "candidate_count_after_confirm": len(ranked),
        "fmp_movers_source": fmp_movers_source,
        "fmp_movers_count": fmp_mover_count,
        "fmp_confirm_requested": len(fmp_confirm_symbols),
        "fmp_confirmed": live_confirmed,
        "fmp_extended_confirmed": extended_confirmed,
        "low_float_fast_lane_count": int(source_bucket_counts.get("low_float_fast_lane_v1", 0)) if 'source_bucket_counts' in locals() else int(low_float_fast_lane_count or 0),
        "low_float_fast_lane": low_float_fast_lane_status,
        "low_float_fast_lane_funnel_debug": low_float_fast_lane_funnel_debug,
        "live_ignition_hot_lane_count": int(source_bucket_counts.get("live_ignition_hot_lane", 0)) if 'source_bucket_counts' in locals() else 0,
        "intraday_early_ramp_count": int(source_bucket_counts.get("intraday_early_ramp", 0)) if 'source_bucket_counts' in locals() else 0,
        "dip_reclaim_radar_count": int(source_bucket_counts.get("dip_reclaim_radar", 0)) if 'source_bucket_counts' in locals() else 0,
        "quiet_accumulation_radar_count": int(source_bucket_counts.get("quiet_accumulation_radar", 0)) if 'source_bucket_counts' in locals() else 0,
        "high_risk_live_mover_count": int(source_bucket_counts.get("high_risk_live_mover", 0)) if 'source_bucket_counts' in locals() else 0,
        "pre_move_engine_v2_count": int(source_bucket_counts.get("pre_move_engine_v2", 0)) if 'source_bucket_counts' in locals() else 0,
        "late_mover_review_count": int(source_bucket_counts.get("late_mover_review", 0)) if 'source_bucket_counts' in locals() else 0,
        "intraday_early_source_radar": {
            k: v for k, v in (intraday_early_radar_status or {}).items()
            if k not in {"candidates"}
        },
        "intraday_early_source_radar_sample": (intraday_early_radar_status or {}).get("candidates", [])[:20],
        "fmp_quote_diagnostics": fmp_diag,
        "source_bucket_counts": source_bucket_counts,
        "price_under_2_deprioritized": price_flags.get("under_2_deprioritized", 0),
        "price_under_2_exception": price_flags.get("under_2_exception", 0),
        "price_over_300_deprioritized": price_flags.get("over_300_deprioritized", 0),
        "weekly_priority_injected_count": int(weekly_priority_count),
        "weekly_high_risk_injected_count": int(weekly_high_risk_count),
        "polygon_weekly_builder_injected_count": int(polygon_weekly_builder_count),
        "baseline_old_engine_count": len(old_baseline),
        "baseline_error": baseline_error,
        "elapsed_sec": elapsed,
        "updated_at": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "final_sample": final[:40],
        "final_symbols": final[:max_symbols],
        "reasons": {
            sym: list((reason_map.get(sym, {}) or {}).get("reasons", []) or [])[:7]
            for sym in final
        },
        "source_tags": {
            sym: list((reason_map.get(sym, {}) or {}).get("sources", []) or [])[:8]
            for sym in final[:220]
        },
        # Compact top-candidate snapshot for Missed Opportunities Review.
        # Diagnostic-only; it does not change the returned universe or scoring.
        "ranked_candidates": [
            {
                "symbol": r.get("symbol"),
                "score": r.get("score", 0),
                "sources": list(r.get("sources") or [])[:8],
                "reasons": list(r.get("reasons") or [])[:8],
                "metrics": {
                    k: v for k, v in (r.get("metrics") or {}).items()
                    if k in {"price", "day_change_pct", "dollar_volume", "volume", "live_price", "live_change_pct", "live_volume", "fmp_price", "fmp_change_pct", "fmp_volume", "near_high", "close_strength", "range_pct", "intraday_early_source_lane", "intraday_early_source_score", "change_pct", "dollar_volume_pace", "reclaimed_open", "dip_depth_pct", "reclaim_from_low_pct", "low_float_fast_lane", "low_float_fast_lane_score", "low_float_fast_lane_source_kind", "low_float_fast_lane_v2p"}
                },
            }
            for r in ranked[:max_symbols]
        ],
    }
    _LAST_DYNAMIC_DISCOVERY_STATUS = dict(diag)
    try:
        _scanner.LAST_SOURCE_DIAGNOSTICS = dict(diag)
    except Exception:
        pass
    return final

