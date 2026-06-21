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
from datetime import datetime, time as dt_time, timedelta
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

try:
    from app.sqlite_store import get_json as _sqlite_get_json, set_json as _sqlite_set_json
except Exception:  # compact watch memory is optional and must never break scanning
    def _sqlite_get_json(key, default=None):
        return default
    def _sqlite_set_json(key, value):
        return False

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


SOURCE_DISCOVERY_MODULE_VERSION = "dynamic_discovery_v3m_v2v6c_dynamic_rotation_discovery_2026_06_21"

DYNAMIC_DISCOVERY_ENABLED = _env_bool("DYNAMIC_DISCOVERY_ENABLED", True)
DYNAMIC_DISCOVERY_USE_FMP_CONFIRMATION = _env_bool("DYNAMIC_DISCOVERY_USE_FMP_CONFIRMATION", True)
DYNAMIC_DISCOVERY_USE_FMP_MOVERS = _env_bool("DYNAMIC_DISCOVERY_USE_FMP_MOVERS", True)
DYNAMIC_DISCOVERY_FMP_CONFIRM_LIMIT = _env_int("DYNAMIC_DISCOVERY_FMP_CONFIRM_LIMIT", 560, 80, 700)
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

# V2R: source-layer capture for the user's real target: small/obscure names
# with accumulation + strong candles + ignition probability.  This is NOT a
# buy engine and does not touch Strong/Cautious.
MICRO_EXPLOSION_CAPTURE_ENABLED = _env_bool("MICRO_EXPLOSION_CAPTURE_ENABLED", True)
MICRO_EXPLOSION_CAPTURE_MIN_PRICE = float(os.getenv("MICRO_EXPLOSION_CAPTURE_MIN_PRICE", "0.10") or 0.10)
MICRO_EXPLOSION_CAPTURE_MAX_PRICE = float(os.getenv("MICRO_EXPLOSION_CAPTURE_MAX_PRICE", "10") or 10)
MICRO_EXPLOSION_CAPTURE_EXTENDED_MAX_PRICE = float(os.getenv("MICRO_EXPLOSION_CAPTURE_EXTENDED_MAX_PRICE", "15") or 15)
MICRO_EXPLOSION_CAPTURE_MAX_CHANGE_PCT = float(os.getenv("MICRO_EXPLOSION_CAPTURE_MAX_CHANGE_PCT", "24") or 24)
MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME = float(os.getenv("MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME", "60000000") or 60_000_000)
MICRO_EXPLOSION_CAPTURE_INJECT_LIMIT = _env_int("MICRO_EXPLOSION_CAPTURE_INJECT_LIMIT", 220, 30, 320)
# V2R1: do not wait for an old source/watch bucket.  Scan the whole available
# grouped market after close / premarket / regular / after-hours, then keep a
# compact sticky watch memory so candidates remain visible before they fly.
MICRO_EXPLOSION_FULL_MARKET_SCAN_CAP = _env_int("MICRO_EXPLOSION_FULL_MARKET_SCAN_CAP", 9000, 500, 12000)
MICRO_EXPLOSION_CLOSE_WATCH_LIMIT = _env_int("MICRO_EXPLOSION_CLOSE_WATCH_LIMIT", 120, 20, 220)
MICRO_EXPLOSION_WATCH_TTL_HOURS = _env_int("MICRO_EXPLOSION_WATCH_TTL_HOURS", 54, 12, 96)
MICRO_EXPLOSION_SEED_CONFIRM_LIMIT = _env_int("MICRO_EXPLOSION_SEED_CONFIRM_LIMIT", 180, 30, 300)
MICRO_EXPLOSION_WATCH_MEMORY_KEY = "source_discovery:micro_explosion_close_watch_v2r1"
# V2R2: if Polygon grouped selector lands on a closed holiday/weekend date and returns
# zero rows, recover the most recent grouped day before running the micro scan.
# This is still compact in-memory/cached data only; no raw flat files are stored.
DYNAMIC_DISCOVERY_GROUPED_RECOVERY_DAYS = _env_int("DYNAMIC_DISCOVERY_GROUPED_RECOVERY_DAYS", 10, 2, 20)
DYNAMIC_DISCOVERY_GROUPED_RECOVERY_MIN_ROWS = _env_int("DYNAMIC_DISCOVERY_GROUPED_RECOVERY_MIN_ROWS", 500, 100, 2000)
MICRO_EXPLOSION_REFERENCE_FALLBACK_LIMIT = _env_int("MICRO_EXPLOSION_REFERENCE_FALLBACK_LIMIT", 900, 120, 1500)

# V2T: monitoring-only live lane for the exact blind spot exposed by V2S2:
# big premarket/open explosions that are too extended for Micro Explosion V2R2
# or too high/fast for Low-Float Fast Lane, but still must be surfaced quickly
# with time/price/gain diagnostics.  This lane never creates BUY_NOW.
BIG_EXPLOSION_LIVE_LANE_ENABLED = _env_bool("BIG_EXPLOSION_LIVE_LANE_ENABLED", True)
BIG_EXPLOSION_LIVE_MIN_PRICE = float(os.getenv("BIG_EXPLOSION_LIVE_MIN_PRICE", "0.10") or 0.10)
BIG_EXPLOSION_LIVE_MAX_PRICE = float(os.getenv("BIG_EXPLOSION_LIVE_MAX_PRICE", "85") or 85)
BIG_EXPLOSION_LIVE_MIN_CHANGE_PCT = float(os.getenv("BIG_EXPLOSION_LIVE_MIN_CHANGE_PCT", "3") or 3)
BIG_EXPLOSION_LIVE_MAX_CHANGE_PCT = float(os.getenv("BIG_EXPLOSION_LIVE_MAX_CHANGE_PCT", "450") or 450)
BIG_EXPLOSION_LIVE_MIN_DOLLAR_VOLUME = float(os.getenv("BIG_EXPLOSION_LIVE_MIN_DOLLAR_VOLUME", "25000") or 25_000)
BIG_EXPLOSION_LIVE_MAX_DOLLAR_VOLUME = float(os.getenv("BIG_EXPLOSION_LIVE_MAX_DOLLAR_VOLUME", "260000000") or 260_000_000)
BIG_EXPLOSION_LIVE_SCAN_CAP = _env_int("BIG_EXPLOSION_LIVE_SCAN_CAP", 9000, 500, 12000)
BIG_EXPLOSION_LIVE_INJECT_LIMIT = _env_int("BIG_EXPLOSION_LIVE_INJECT_LIMIT", 320, 40, 520)

# V2U: Real pre-explosion capture pipeline.  A scheduled/manual prior-session
# scan stores compact prepared candidates after all sessions close.  Live
# discovery loads them before premarket/open so names like ICCM/EHGO are already
# on the radar before the move, and TPC-like open explosions get a reserved lane.
BIG_EXPLOSION_PREPARED_WATCH_ENABLED = _env_bool("BIG_EXPLOSION_PREPARED_WATCH_ENABLED", True)
BIG_EXPLOSION_PREPARED_WATCH_LIMIT = _env_int("BIG_EXPLOSION_PREPARED_WATCH_LIMIT", 420, 40, 800)
BIG_EXPLOSION_PREPARED_WATCH_TTL_HOURS = _env_int("BIG_EXPLOSION_PREPARED_WATCH_TTL_HOURS", 72, 12, 144)
BIG_EXPLOSION_PREPARED_WATCH_MEMORY_KEY = "source_discovery:big_explosion_prepared_watch_v2u"
BIG_EXPLOSION_TRUE_MINING_ENABLED = _env_bool("BIG_EXPLOSION_TRUE_MINING_ENABLED", True)
BIG_EXPLOSION_OPENING_INSTANT_ENABLED = _env_bool("BIG_EXPLOSION_OPENING_INSTANT_ENABLED", True)

# V2V: live tight monitoring and fast promotion source lane.  This does not
# change Strong/Cautious rules.  It only keeps prepared/new ignition candidates
# in a front-row live watch lane when +3%/+5% movement appears with real volume.
LIVE_TIGHT_MONITORING_ENABLED = _env_bool("LIVE_TIGHT_MONITORING_ENABLED", True)
LIVE_TIGHT_MONITORING_MEMORY_KEY = "source_discovery:live_tight_monitoring_v2v"
LIVE_TIGHT_MONITORING_LIMIT = _env_int("LIVE_TIGHT_MONITORING_LIMIT", 160, 20, 300)
LIVE_TIGHT_MONITORING_TTL_HOURS = _env_int("LIVE_TIGHT_MONITORING_TTL_HOURS", 10, 2, 24)
LIVE_TIGHT_MONITORING_PREPARED_MIN_CHANGE_PCT = float(os.getenv("LIVE_TIGHT_MONITORING_PREPARED_MIN_CHANGE_PCT", "3") or 3)
LIVE_TIGHT_MONITORING_NEW_MIN_CHANGE_PCT = float(os.getenv("LIVE_TIGHT_MONITORING_NEW_MIN_CHANGE_PCT", "5") or 5)
LIVE_TIGHT_MONITORING_MIN_VOLUME = float(os.getenv("LIVE_TIGHT_MONITORING_MIN_VOLUME", "20000") or 20_000)
LIVE_TIGHT_MONITORING_MIN_DOLLAR_VOLUME = float(os.getenv("LIVE_TIGHT_MONITORING_MIN_DOLLAR_VOLUME", "25000") or 25_000)
LIVE_TIGHT_MONITORING_EXTENDED_CONTINUATION_MIN_CHANGE_PCT = float(os.getenv("LIVE_TIGHT_MONITORING_EXTENDED_CONTINUATION_MIN_CHANGE_PCT", "18") or 18)
LIVE_TIGHT_MONITORING_EXTREME_EXTENSION_MIN_CHANGE_PCT = float(os.getenv("LIVE_TIGHT_MONITORING_EXTREME_EXTENSION_MIN_CHANGE_PCT", "35") or 35)

# V2V6c: dynamic live monitoring + rotating discovery.
# Important distinction:
# - live monitoring budget = how many symbols receive FMP live confirmation in this scan.
# - discovery source = broad queues/rotating sweeps that keep feeding new names into monitoring.
# The scanner must not shrink to 180 during active sessions. 180 is only a quiet
# closed/weekend diagnostic budget. After-hours and trading sessions use a wider
# budget plus rotating market slices so small/unknown stocks can enter tomorrow's
# Prepared Watch without running a heavy historical replay.
LIVE_MONITORING_BUDGET_GUARD_ENABLED = _env_bool("LIVE_MONITORING_BUDGET_GUARD_ENABLED", True)
LIVE_MONITORING_FMP_CONFIRM_TOTAL_LIMIT = _env_int("LIVE_MONITORING_FMP_CONFIRM_TOTAL_LIMIT", 560, 180, 700)
LIVE_MONITORING_PREPARED_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_PREPARED_CONFIRM_LIMIT", 120, 40, 180)
LIVE_MONITORING_MEMORY_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_MEMORY_CONFIRM_LIMIT", 100, 20, 160)
LIVE_MONITORING_RANKED_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_RANKED_CONFIRM_LIMIT", 120, 40, 220)
LIVE_MONITORING_MICRO_MEMORY_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_MICRO_MEMORY_CONFIRM_LIMIT", 80, 10, 140)
LIVE_MONITORING_SEED_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_SEED_CONFIRM_LIMIT", 60, 10, 120)
LIVE_MONITORING_LOW_FLOAT_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_LOW_FLOAT_CONFIRM_LIMIT", 160, 30, 240)
LIVE_MONITORING_MICRO_LIVE_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_MICRO_LIVE_CONFIRM_LIMIT", 120, 20, 200)
LIVE_MONITORING_ROTATION_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_ROTATION_CONFIRM_LIMIT", 300, 0, 360)
LIVE_MONITORING_EMERGENCY_CONFIRM_LIMIT = _env_int("LIVE_MONITORING_EMERGENCY_CONFIRM_LIMIT", 80, 0, 160)
# Confirmation calls are sent in batches because app/live_quotes.py intentionally
# normalizes a single request to at most 300 symbols. Chunking keeps the wider
# market coverage without creating single huge HTTP calls.
LIVE_MONITORING_FMP_BATCH_SIZE = _env_int("LIVE_MONITORING_FMP_BATCH_SIZE", 280, 80, 300)
LIVE_MONITORING_MAX_FMP_BATCHES = _env_int("LIVE_MONITORING_MAX_FMP_BATCHES", 3, 1, 4)
# Rotating discovery is the broad source feeder. It does not create buy calls;
# it only gives new names a chance to be live-confirmed and then scored normally.
AFTER_HOURS_ROTATING_DISCOVERY_ENABLED = _env_bool("AFTER_HOURS_ROTATING_DISCOVERY_ENABLED", True)
AFTER_HOURS_ROTATING_DISCOVERY_BATCH_SIZE = _env_int("AFTER_HOURS_ROTATING_DISCOVERY_BATCH_SIZE", 300, 60, 360)
INTRADAY_ROTATING_DISCOVERY_BATCH_SIZE = _env_int("INTRADAY_ROTATING_DISCOVERY_BATCH_SIZE", 160, 40, 260)
PREMARKET_ROTATING_DISCOVERY_BATCH_SIZE = _env_int("PREMARKET_ROTATING_DISCOVERY_BATCH_SIZE", 220, 60, 320)
ROTATING_DISCOVERY_SLOT_MINUTES = _env_int("ROTATING_DISCOVERY_SLOT_MINUTES", 15, 5, 60)
ROTATING_DISCOVERY_WEEKEND_ENABLED = _env_bool("ROTATING_DISCOVERY_WEEKEND_ENABLED", False)

MICRO_EXPLOSION_SEED_SYMBOLS = {
    # V2U3 regression canaries from replay: these are not buy calls; they only force live quote confirmation so the scanner can detect similar/prepared movement early.
    "EHGO", "ICCM", "TPC", "SNBR",
    # User-provided low-float / China-momentum seed universe.  These names are not
    # buy calls and are not injected as opportunities by themselves.  They are only
    # extra symbols to confirm with live data and to score if activity appears.
    "ADTX","ADVB","ADXN","AKA","AKAN","ATHE","ATPC","ATXG","BBGI","BDL","BJDX","BNRG","CHNR","CHSN","CISS","CLIK","CLRO","CUPR","CVR","DAIC","DCOY","DIT","DKI","DRCT","EEIQ","ELOX","ERNA","EZRA","FCHL","FGI","FGL","FOXX","FRGT","GNLN","GURE","GWAV","HAO","HCAI","HKIT","HTCR","ILAG","INTG","IOR","IOTR","IPST","IPW","JAGX","KUST","LIVE","LVLU","MASK","MAYS","MDRR","MI","MLEC","MTEN","MTEX","NCEW","NCI","NCSM","NCT","NDRA","NTRP","NVNO","NXTS","NYC","OLOX","ONCO","PAVS","PBM","PMAX","PNRG","PRFX","PW","RAND","RAYA","RDGT","RNAZ","RTB","SDOT","SEB","SHPH","SLXN","SMX","SNSE","SPRC","SUGP","SXTC","TLIH","UK","UONE","UPC","VALU","VEEE","VSA","WCT","WGRX","WOK","WTO","YHG","YYAI",
    "POM","ITP","PASW","CHOW","YHNA","IZM","GSUN","CCTG","RITR","DXF","RCON","TDIC","CBAT","ELPW","EHGO","YIBO","CLPS","CNEY","ONEG","MAO","EDTK","SEED","ZYBT","YRD","EH","WDH","SORA","MSC","SY","LANV","AGMH","IFBD","BQ","LZMH","MEGL","ZCMD","HUDI","DTSS","WIMI","DUO","HLP","CAAS","AZI","AIHS","JZXN","CPHI","ZNB","LXEH","CNET","ORIS","JEM","EZGO","HUIZ","ELOG","LSE","EDHL","RETO","SNTG","KRKR","GLXG","DCX","FTFT","JWEL","JYD","AIXI","GMM","HXHX","BYAH","YJ","RAY","LOBO","TAOP","WYHG","DOGZ","FAMI","EPSM","AEHL","MGIH","MOGU","ABLV","CNF","WAFU","MSGY","DSY","XHG","MIMI","GCDT","BON","MFI","YMT","HOLO","CREG","OCG","ABTS","UBXG","LBGJ","CHR","BAOS","GIBO","WXM","WAI","YOUL","FOFO","YQ","UTSI","STG","NCTY","IH","NBRG","WNW",
}

_FMP_MOVERS_CACHE: dict = {"ts": 0.0, "rows": [], "error": ""}
_LAST_DYNAMIC_DISCOVERY_STATUS: dict = {}


def dynamic_discovery_enabled() -> bool:
    return bool(DYNAMIC_DISCOVERY_ENABLED)


def _phase_detail(now: datetime | None = None) -> dict:
    now = now or datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return {"phase": "closed", "detail": "weekend", "interval_sec": 3600, "target": 180, "label": "عطلة السوق"}
    t = now.time()
    mins = now.hour * 60 + now.minute
    if dt_time(4, 0) <= t < dt_time(7, 0):
        return {"phase": "pre_market", "detail": "pre_market_early", "interval_sec": 900, "target": 420, "label": "قبل الافتتاح المبكر"}
    if dt_time(7, 0) <= t < dt_time(9, 30):
        return {"phase": "pre_market", "detail": "pre_market_active", "interval_sec": 480, "target": 560, "label": "قبل الافتتاح النشط"}
    if dt_time(9, 30) <= t < dt_time(10, 30):
        return {"phase": "open", "detail": "open_first_hour", "interval_sec": 360, "target": 560, "label": "أول ساعة تداول"}
    if dt_time(10, 30) <= t < dt_time(15, 0):
        return {"phase": "open", "detail": "open_mid_session", "interval_sec": 600, "target": 480, "label": "وسط الجلسة"}
    if dt_time(15, 0) <= t <= dt_time(16, 0):
        return {"phase": "open", "detail": "open_last_hour", "interval_sec": 420, "target": 520, "label": "آخر ساعة تداول"}
    if dt_time(16, 0) < t <= dt_time(18, 0):
        return {"phase": "after_hours", "detail": "after_hours_early", "interval_sec": 900, "target": 560, "label": "بعد الإغلاق النشط"}
    if dt_time(18, 0) < t <= dt_time(20, 0):
        return {"phase": "after_hours", "detail": "after_hours_late", "interval_sec": 900, "target": 500, "label": "بعد الإغلاق المتأخر"}
    return {"phase": "closed", "detail": "overnight_closed", "interval_sec": 1800, "target": 300, "label": "السوق مغلق"}


def get_full_market_scan_interval_sec() -> int:
    return int(_phase_detail().get("interval_sec", 1800) or 1800)


def get_recommended_deep_scan_target(default: int = 190) -> int:
    try:
        phase = _phase_detail()
        target = int(phase.get("target") or default or 190)
        # Keep the user's preference: enough choices, but not an inflated noisy list.
        return max(120, min(620, target))
    except Exception:
        return int(default or 190)


def get_last_dynamic_discovery_status() -> dict:
    return dict(_LAST_DYNAMIC_DISCOVERY_STATUS or {})


def save_prepared_big_explosion_watch(items: list[dict] | None, trade_date: str = "", source: str = "", debug: dict | None = None) -> dict:
    """Persist compact V2U prepared watch candidates for the live scanner.

    This stores only compact per-symbol summaries (symbol, score, metrics,
    reasons).  It never stores raw Polygon/FMP rows.  The live source layer will
    load this list before premarket/open and pass it through normal Sharia/deep
    analysis; it does not create BUY_NOW.
    """
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    clean: list[dict] = []
    seen: set[str] = set()
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        sym = _clean_symbol(it.get("symbol") or it.get("ticker") or it.get("T"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        metrics = dict(it.get("metrics") or {})
        clean.append({
            "symbol": sym,
            "score": safe_round(to_float(it.get("score") or metrics.get("big_explosion_prepared_score") or 0), 3),
            "stage": str(it.get("stage") or it.get("prepared_stage") or "prepared_watch"),
            "reasons": [str(x)[:140] for x in list(it.get("reasons") or metrics.get("big_explosion_prepared_reasons_ar") or [])[:8] if str(x or "").strip()],
            "metrics": {
                k: v for k, v in metrics.items()
                if k in {
                    "price", "open", "high", "low", "close", "volume", "dollar_volume",
                    "change_pct", "day_change_pct", "range_pct", "close_strength", "near_high",
                    "first_minute", "last_minute", "prior_session_phase", "prior_session_source",
                    "big_explosion_prepared_score", "big_explosion_prepared_watch_v2u",
                    "big_explosion_prepared_reasons_ar", "urgent_sharia_review_v2u",
                    "opening_instant_watch_v2u", "source_note",
                    "prepared_bucket", "prepared_bucket_ar", "pre_explosion_candidate_v2u3",
                    "opening_gap_candidate_v2u3", "ultra_low_price_candidate_v2u3",
                    "after_hours_pressure_v2u3", "quiet_pressure_v2u3", "critical_promotion_gate_score_v2u3",
                    "watch_priority_v2u3", "blocked_but_watchlist_v2u3",
                    "critical_pre_explosion_bucket_v2u3", "critical_micro_probe_v2u3",
                    "critical_iccm_probe_v2u3", "critical_tpc_probe_v2u3",
                    "critical_promotion_gate_v2u3", "critical_promotion_reason_ar"
                }
            },
        })
        if len(clean) >= int(BIG_EXPLOSION_PREPARED_WATCH_LIMIT or 160):
            break
    payload = {
        "version": "big_explosion_prepared_watch_v2u4_live_critical_pre_explosion_2026_06_20",
        "updated_at_utc": now,
        "trade_date": str(trade_date or ""),
        "source": str(source or ""),
        "count": len(clean),
        "items": clean,
        "debug": dict(debug or {}),
        "rule_ar": "V2U4: قائمة حرجة قبل الانفجار من مسح أمس الكامل؛ تظهر مبكرًا في واجهة الأداة كمراقبة/مراجعة شرعية فقط ولا تعني شراء مباشر.",
    }
    ok = False
    try:
        ok = bool(_sqlite_set_json(BIG_EXPLOSION_PREPARED_WATCH_MEMORY_KEY, payload))
    except Exception:
        ok = False
    payload["saved"] = ok
    return payload


def load_prepared_big_explosion_watch() -> tuple[list[dict], dict]:
    payload = {}
    try:
        payload = _sqlite_get_json(BIG_EXPLOSION_PREPARED_WATCH_MEMORY_KEY, {}) or {}
    except Exception:
        payload = {}
    debug = {
        "version": "prepared_big_explosion_watch_loader_v2u4_live_critical_pre_explosion_2026_06_20",
        "enabled": bool(BIG_EXPLOSION_PREPARED_WATCH_ENABLED),
        "memory_key": BIG_EXPLOSION_PREPARED_WATCH_MEMORY_KEY,
        "stored_count": int((payload or {}).get("count", 0) or 0),
        "trade_date": (payload or {}).get("trade_date", ""),
        "updated_at_utc": (payload or {}).get("updated_at_utc", ""),
        "active_count": 0,
        "expired": False,
        "rule_ar": "V2U4: يحمل مرشحي الانفجار الحرجين قبل السوق؛ الرمادي للمراجعة العاجلة والمرفوض للتعلم فقط، ولا يفتح شراء مباشر.",
    }
    if not BIG_EXPLOSION_PREPARED_WATCH_ENABLED or not isinstance(payload, dict):
        return [], debug
    try:
        updated = str(payload.get("updated_at_utc") or "").replace("Z", "+00:00")
        if updated:
            dt = datetime.fromisoformat(updated)
            age_h = (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600.0
            debug["age_hours"] = safe_round(age_h, 2)
            if age_h > float(BIG_EXPLOSION_PREPARED_WATCH_TTL_HOURS or 72):
                debug["expired"] = True
                return [], debug
    except Exception:
        pass
    out: list[dict] = []
    for it in list(payload.get("items") or [])[: int(BIG_EXPLOSION_PREPARED_WATCH_LIMIT or 160)]:
        sym = _clean_symbol((it or {}).get("symbol"))
        if not sym:
            continue
        metrics = dict((it or {}).get("metrics") or {})
        metrics.update({
            "big_explosion_prepared_watch_v2u": True,
            "urgent_sharia_review_v2u": True,
            "prepared_watch_trade_date": payload.get("trade_date", ""),
            "prepared_watch_updated_at_utc": payload.get("updated_at_utc", ""),
            "big_explosion_live_lane_v2t": True,
            "big_explosion_live_lane_v2t2": True,
        })
        out.append({
            "symbol": sym,
            "score": safe_round((it or {}).get("score", 0), 3),
            "reasons": list((it or {}).get("reasons") or [])[:8],
            "metrics": metrics,
        })
    debug["active_count"] = len(out)
    debug["symbols"] = [x.get("symbol") for x in out[:80]]
    return out, debug


def _load_live_tight_monitoring_memory() -> tuple[list[dict], dict]:
    payload = {}
    try:
        payload = _sqlite_get_json(LIVE_TIGHT_MONITORING_MEMORY_KEY, {}) or {}
    except Exception:
        payload = {}
    debug = {
        "version": "live_tight_monitoring_memory_v2v_2026_06_21",
        "enabled": bool(LIVE_TIGHT_MONITORING_ENABLED),
        "memory_key": LIVE_TIGHT_MONITORING_MEMORY_KEY,
        "stored_count": int((payload or {}).get("count", 0) or 0),
        "active_count": 0,
        "expired_count": 0,
        "rule_ar": "V2V: ذاكرة قصيرة للمرشحين الذين بدأوا يتحركون أثناء التداول؛ مراقبة/ترقية فقط ولا تفتح شراء مباشر.",
    }
    if not LIVE_TIGHT_MONITORING_ENABLED or not isinstance(payload, dict):
        return [], debug
    now_ts = time.time()
    active: list[dict] = []
    for item in list(payload.get("items") or [])[: int(LIVE_TIGHT_MONITORING_LIMIT or 160)]:
        if not isinstance(item, dict):
            continue
        sym = _clean_symbol(item.get("symbol"))
        if not sym:
            continue
        try:
            age_h = (now_ts - float(item.get("updated_ts", item.get("created_ts", now_ts)) or now_ts)) / 3600.0
        except Exception:
            age_h = 0.0
        if age_h > float(LIVE_TIGHT_MONITORING_TTL_HOURS or 10):
            debug["expired_count"] += 1
            continue
        out = dict(item)
        out["symbol"] = sym
        out["age_hours"] = safe_round(age_h, 2)
        active.append(out)
    debug["active_count"] = len(active)
    debug["symbols"] = [x.get("symbol") for x in active[:80]]
    return active, debug


def _save_live_tight_monitoring_memory(items: list[dict] | None, *, source: str = "") -> dict:
    now_ts = time.time()
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    old_items, old_debug = _load_live_tight_monitoring_memory()
    merged: dict[str, dict] = {}
    for item in old_items or []:
        sym = _clean_symbol((item or {}).get("symbol"))
        if sym:
            merged[sym] = dict(item)
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sym = _clean_symbol(item.get("symbol"))
        if not sym:
            continue
        prev = merged.get(sym, {})
        created_ts = float(prev.get("created_ts", now_ts) or now_ts)
        reasons = []
        for r in list(prev.get("reasons") or []) + list(item.get("reasons") or []):
            txt = str(r or "").strip()
            if txt and txt not in reasons:
                reasons.append(txt[:160])
        merged[sym] = {
            **prev,
            **{k: v for k, v in dict(item).items() if k != "reasons"},
            "symbol": sym,
            "created_ts": created_ts,
            "updated_ts": now_ts,
            "updated_at_utc": now_iso,
            "reasons": reasons[:10],
        }
    ordered = sorted(merged.values(), key=lambda x: (float(x.get("updated_ts", 0) or 0), float(x.get("score", 0) or 0)), reverse=True)[: int(LIVE_TIGHT_MONITORING_LIMIT or 160)]
    payload = {
        "version": "live_tight_monitoring_v2v_2026_06_21",
        "updated_at_utc": now_iso,
        "source": str(source or ""),
        "count": len(ordered),
        "items": ordered,
        "previous_debug": old_debug,
        "rule_ar": "V2V: مرشح قبل السوق أو مرشح جديد بدأ +3%/+5% مع حجم حقيقي يدخل مراقبة لصيقة/تأكيد مبكر. لا يفتح Strong/Cautious ولا يتجاوز الشرعية.",
    }
    ok = False
    try:
        ok = bool(_sqlite_set_json(LIVE_TIGHT_MONITORING_MEMORY_KEY, payload))
    except Exception:
        ok = False
    payload["saved"] = ok
    return payload


def _cap_unique_symbols(values, limit: int) -> list[str]:
    """Keep symbols unique and bounded for Railway/FMP-safe live checks."""
    try:
        limit = max(0, int(limit or 0))
    except Exception:
        limit = 0
    if limit <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        sym = _clean_symbol(value)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= limit:
            break
    return out


def _symbols_from_candidate_rows(rows: list[dict] | None, wanted_sources: set[str], limit: int = 500) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    wanted = {str(x or "").strip() for x in (wanted_sources or set()) if str(x or "").strip()}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sources = {str(x or "").strip() for x in (row.get("sources") or []) if str(x or "").strip()}
        if wanted and not (sources & wanted):
            continue
        sym = _clean_symbol(row.get("symbol"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= int(limit or 500):
            break
    return out


def _rotating_discovery_symbols(reference_tickers: list[str] | None, *, phase_info: dict, existing_symbols: list[str] | None = None) -> tuple[list[str], dict]:
    """Return a deterministic market-rotation slice for broad discovery.

    This is the answer to the 180-symbol problem: monitoring can be budgeted,
    but source discovery must keep feeding new symbols. During after-hours it
    rotates through large chunks of the reference universe every 15 minutes to
    prepare tomorrow's watchlist. During live trading it uses a smaller slice so
    new intraday movers/small stocks can enter without a full-market replay.
    """
    detail = str((phase_info or {}).get("detail", "") or "")
    phase = str((phase_info or {}).get("phase", "") or "")
    enabled = bool(AFTER_HOURS_ROTATING_DISCOVERY_ENABLED)
    if not enabled:
        return [], {"enabled": False, "reason": "disabled"}
    if detail == "weekend" and not ROTATING_DISCOVERY_WEEKEND_ENABLED:
        return [], {"enabled": False, "phase_detail": detail, "reason": "weekend_disabled"}

    if phase == "after_hours":
        batch_size = int(AFTER_HOURS_ROTATING_DISCOVERY_BATCH_SIZE or 300)
        mode = "after_hours_prepare_tomorrow"
    elif phase == "pre_market":
        batch_size = int(PREMARKET_ROTATING_DISCOVERY_BATCH_SIZE or 220)
        mode = "premarket_new_feed"
    elif phase == "open":
        batch_size = int(INTRADAY_ROTATING_DISCOVERY_BATCH_SIZE or 160)
        mode = "intraday_new_feed"
    elif detail == "overnight_closed":
        batch_size = min(220, int(AFTER_HOURS_ROTATING_DISCOVERY_BATCH_SIZE or 300))
        mode = "overnight_light_feed"
    else:
        batch_size = 0
        mode = "quiet_no_rotation"

    if batch_size <= 0:
        return [], {"enabled": True, "phase_detail": detail, "mode": mode, "batch_size": 0, "count": 0}

    all_syms: list[str] = []
    seen: set[str] = set()
    for raw in reference_tickers or []:
        sym = _clean_symbol(raw)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        all_syms.append(sym)
    total = len(all_syms)
    if total <= 0:
        return [], {"enabled": True, "phase_detail": detail, "mode": mode, "batch_size": batch_size, "count": 0, "reason": "empty_reference_universe"}

    try:
        slot_minutes = max(5, int(ROTATING_DISCOVERY_SLOT_MINUTES or 15))
    except Exception:
        slot_minutes = 15
    now = datetime.now(NY_TZ)
    day_key = int(now.strftime("%Y%m%d"))
    minutes_from_midnight = int(now.hour * 60 + now.minute)
    slot = int(minutes_from_midnight // slot_minutes)
    start = int(((day_key % 997) * 17 + slot) * batch_size) % max(total, 1)
    existing = {_clean_symbol(x) for x in (existing_symbols or []) if _clean_symbol(x)}

    selected: list[str] = []
    scanned = 0
    idx = start
    while scanned < total and len(selected) < batch_size:
        sym = all_syms[idx % total]
        idx += 1
        scanned += 1
        if sym in existing:
            continue
        selected.append(sym)

    return selected, {
        "enabled": True,
        "version": "rotating_discovery_v2v6c_2026_06_21",
        "phase": phase,
        "phase_detail": detail,
        "mode": mode,
        "batch_size": batch_size,
        "slot_minutes": slot_minutes,
        "slot": slot,
        "reference_universe_count": total,
        "start_index": start,
        "count": len(selected),
        "sample": selected[:40],
        "rule_ar": "V2V6c: هذه ليست قائمة شراء؛ هي دفعة اكتشاف دوّارة حتى تدخل أسهم جديدة، خصوصًا الصغيرة، إلى التأكيد الحي ثم إلى Prepared/Watch إذا ظهرت حركة وحجم.",
    }


def _budget_caps_for_phase(phase_info: dict) -> dict:
    detail = str((phase_info or {}).get("detail", "") or "").lower()
    phase = str((phase_info or {}).get("phase", "") or "").lower()
    # Start with env-configured active-session defaults.
    caps = {
        "total": int(LIVE_MONITORING_FMP_CONFIRM_TOTAL_LIMIT or 560),
        "prepared": int(LIVE_MONITORING_PREPARED_CONFIRM_LIMIT or 120),
        "live_tight_memory": int(LIVE_MONITORING_MEMORY_CONFIRM_LIMIT or 100),
        "emergency_ignition": int(LIVE_MONITORING_EMERGENCY_CONFIRM_LIMIT or 80),
        "low_float_fast_lane": int(LIVE_MONITORING_LOW_FLOAT_CONFIRM_LIMIT or 160),
        "micro_live_candidates": int(LIVE_MONITORING_MICRO_LIVE_CONFIRM_LIMIT or 120),
        "rotating_discovery": int(LIVE_MONITORING_ROTATION_CONFIRM_LIMIT or 300),
        "ranked_source_candidates": int(LIVE_MONITORING_RANKED_CONFIRM_LIMIT or 120),
        "micro_close_watch_memory": int(LIVE_MONITORING_MICRO_MEMORY_CONFIRM_LIMIT or 80),
        "seed_symbols": int(LIVE_MONITORING_SEED_CONFIRM_LIMIT or 60),
    }
    if "weekend" in detail:
        caps.update({"total": 180, "prepared": 80, "live_tight_memory": 50, "emergency_ignition": 20, "low_float_fast_lane": 50, "micro_live_candidates": 40, "rotating_discovery": 0, "ranked_source_candidates": 50, "micro_close_watch_memory": 35, "seed_symbols": 25})
    elif "overnight_closed" in detail:
        caps.update({"total": 300, "prepared": 100, "live_tight_memory": 70, "emergency_ignition": 35, "low_float_fast_lane": 80, "micro_live_candidates": 70, "rotating_discovery": 180, "ranked_source_candidates": 70, "micro_close_watch_memory": 50, "seed_symbols": 35})
    elif phase == "after_hours":
        caps.update({"total": 560 if "early" in detail else 500, "prepared": 120, "live_tight_memory": 100, "emergency_ignition": 70, "low_float_fast_lane": 160, "micro_live_candidates": 120, "rotating_discovery": 300, "ranked_source_candidates": 110, "micro_close_watch_memory": 80, "seed_symbols": 50})
    elif "pre_market_early" in detail:
        caps.update({"total": 420, "prepared": 120, "live_tight_memory": 90, "emergency_ignition": 60, "low_float_fast_lane": 130, "micro_live_candidates": 100, "rotating_discovery": 220, "ranked_source_candidates": 100, "micro_close_watch_memory": 70, "seed_symbols": 45})
    elif "pre_market_active" in detail:
        caps.update({"total": 560, "prepared": 140, "live_tight_memory": 110, "emergency_ignition": 80, "low_float_fast_lane": 170, "micro_live_candidates": 130, "rotating_discovery": 220, "ranked_source_candidates": 120, "micro_close_watch_memory": 80, "seed_symbols": 50})
    elif "open_first_hour" in detail:
        caps.update({"total": 560, "prepared": 140, "live_tight_memory": 120, "emergency_ignition": 100, "low_float_fast_lane": 180, "micro_live_candidates": 140, "rotating_discovery": 180, "ranked_source_candidates": 130, "micro_close_watch_memory": 80, "seed_symbols": 45})
    elif "open_mid_session" in detail:
        caps.update({"total": 480, "prepared": 120, "live_tight_memory": 100, "emergency_ignition": 80, "low_float_fast_lane": 150, "micro_live_candidates": 120, "rotating_discovery": 120, "ranked_source_candidates": 120, "micro_close_watch_memory": 70, "seed_symbols": 40})
    elif "open_last_hour" in detail:
        caps.update({"total": 520, "prepared": 130, "live_tight_memory": 110, "emergency_ignition": 90, "low_float_fast_lane": 160, "micro_live_candidates": 130, "rotating_discovery": 160, "ranked_source_candidates": 120, "micro_close_watch_memory": 80, "seed_symbols": 45})
    return {k: max(0, int(v or 0)) for k, v in caps.items()}


def _live_monitoring_confirmation_budget(
    *,
    phase_info: dict,
    prepared_symbols: list[str],
    live_tight_symbols: list[str],
    ranked_symbols: list[str],
    memory_symbols: list[str],
    seed_symbols: list[str],
    low_float_symbols: list[str] | None = None,
    micro_live_symbols: list[str] | None = None,
    emergency_symbols: list[str] | None = None,
    rotation_symbols: list[str] | None = None,
    rotation_debug: dict | None = None,
) -> tuple[list[str], dict]:
    """V2V6c dynamic router for FMP live confirmation.

    180 is no longer treated as the live market source. It is only a quiet
    weekend/closed cap. Active market and after-hours phases receive wider,
    phase-aware confirmation while rotating-discovery keeps feeding new names.
    """
    phase_detail = str((phase_info or {}).get("detail", "") or "")
    phase_label = str((phase_info or {}).get("label", "") or "")
    if not LIVE_MONITORING_BUDGET_GUARD_ENABLED:
        raw = _scanner.unique_keep_order(
            list(prepared_symbols or []) + list(live_tight_symbols or []) + list(emergency_symbols or []) + list(low_float_symbols or []) + list(micro_live_symbols or []) + list(rotation_symbols or []) + list(ranked_symbols or []) + list(memory_symbols or []) + list(seed_symbols or [])
        )
        return raw, {
            "version": "live_monitoring_dynamic_budget_v2v6c_2026_06_21",
            "enabled": False,
            "final_count": len(raw),
            "phase_detail": phase_detail,
            "phase_label": phase_label,
            "rotation_discovery": rotation_debug or {},
            "rule_ar": "V2V6c budget guard disabled by env؛ لا يُنصح بهذا أثناء السوق الحي.",
        }

    caps = _budget_caps_for_phase(phase_info or {})
    groups = {
        "prepared": _cap_unique_symbols(prepared_symbols, caps.get("prepared", 0)),
        "live_tight_memory": _cap_unique_symbols(live_tight_symbols, caps.get("live_tight_memory", 0)),
        "emergency_ignition": _cap_unique_symbols(emergency_symbols or [], caps.get("emergency_ignition", 0)),
        "low_float_fast_lane": _cap_unique_symbols(low_float_symbols or [], caps.get("low_float_fast_lane", 0)),
        "micro_live_candidates": _cap_unique_symbols(micro_live_symbols or [], caps.get("micro_live_candidates", 0)),
        "rotating_discovery": _cap_unique_symbols(rotation_symbols or [], caps.get("rotating_discovery", 0)),
        "ranked_source_candidates": _cap_unique_symbols(ranked_symbols, caps.get("ranked_source_candidates", 0)),
        "micro_close_watch_memory": _cap_unique_symbols(memory_symbols, caps.get("micro_close_watch_memory", 0)),
        "seed_symbols": _cap_unique_symbols(seed_symbols, caps.get("seed_symbols", 0)),
    }
    final = _scanner.unique_keep_order(
        groups["prepared"]
        + groups["live_tight_memory"]
        + groups["emergency_ignition"]
        + groups["low_float_fast_lane"]
        + groups["micro_live_candidates"]
        + groups["rotating_discovery"]
        + groups["ranked_source_candidates"]
        + groups["micro_close_watch_memory"]
        + groups["seed_symbols"]
    )[:int(caps.get("total", 560) or 560)]
    debug = {
        "version": "live_monitoring_dynamic_budget_v2v6c_2026_06_21",
        "enabled": True,
        "phase_detail": phase_detail,
        "phase_label": phase_label,
        "caps": caps,
        "requested_counts": {
            "prepared": len(list(prepared_symbols or [])),
            "live_tight_memory": len(list(live_tight_symbols or [])),
            "emergency_ignition": len(list(emergency_symbols or [])),
            "low_float_fast_lane": len(list(low_float_symbols or [])),
            "micro_live_candidates": len(list(micro_live_symbols or [])),
            "rotating_discovery": len(list(rotation_symbols or [])),
            "ranked_source_candidates": len(list(ranked_symbols or [])),
            "micro_close_watch_memory": len(list(memory_symbols or [])),
            "seed_symbols": len(list(seed_symbols or [])),
        },
        "selected_counts": {k: len(v) for k, v in groups.items()},
        "final_count": len(final),
        "final_sample": final[:80],
        "rotation_discovery": rotation_debug or {},
        "rule_ar": "V2V6c: 180 ليس منبع السوق؛ هو حد عطلة فقط. بعد الإغلاق والبري ماركت وأثناء التداول تستخدم الأداة ميزانية ديناميكية + دفعات اكتشاف دوّارة، مع مقاعد مخصصة للأسهم الصغيرة/Low-Float/Fast Lane.",
    }
    return final, debug


def _live_tight_profile_from_quote(symbol: str, quote: dict, *, prepared: bool = False, from_memory: bool = False) -> dict:
    sym = _clean_symbol(symbol)
    price = to_float((quote or {}).get("price"))
    change_pct = to_float((quote or {}).get("change_pct"))
    volume = to_float((quote or {}).get("volume"))
    dollar_volume = price * volume if price > 0 and volume > 0 else 0.0
    debug = {
        "symbol": sym,
        "prepared": bool(prepared),
        "from_memory": bool(from_memory),
        "price": safe_round(price, 4),
        "change_pct": safe_round(change_pct, 2),
        "volume": safe_round(volume, 0),
        "dollar_volume": safe_round(dollar_volume, 0),
        "eligible": False,
        "reasons": [],
    }
    if not LIVE_TIGHT_MONITORING_ENABLED or not sym or price <= 0:
        return debug
    min_change = LIVE_TIGHT_MONITORING_PREPARED_MIN_CHANGE_PCT if prepared else LIVE_TIGHT_MONITORING_NEW_MIN_CHANGE_PCT
    volume_ok = bool(volume >= LIVE_TIGHT_MONITORING_MIN_VOLUME or dollar_volume >= LIVE_TIGHT_MONITORING_MIN_DOLLAR_VOLUME)
    if change_pct < float(min_change or 3):
        debug["reasons"].append(f"لم يصل حد الحركة الحية بعد: {safe_round(change_pct, 2)}%")
        return debug
    if not volume_ok:
        debug["reasons"].append("الحركة موجودة لكن الحجم/الدولار فوليوم لم يتأكد بعد")
        return debug
    extended_for_pullback = bool(change_pct >= LIVE_TIGHT_MONITORING_EXTENDED_CONTINUATION_MIN_CHANGE_PCT)
    extreme_extension = bool(change_pct >= LIVE_TIGHT_MONITORING_EXTREME_EXTENSION_MIN_CHANGE_PCT)
    if extended_for_pullback:
        stage = "live_continuation_pullback_watch"
        stage_ar = "🚫 مرتفع جدًا — لا تطارد / Pullback فقط" if extreme_extension else "📈 امتداد قوي — استمرار مشروط / Pullback"
    else:
        stage = "live_early_confirmation_5pct" if change_pct >= 5.0 else "live_early_confirmation_3pct"
        stage_ar = "⚡ تأكيد مبكر حي +5%" if change_pct >= 5.0 else "⚡ تأكيد مبكر حي +3%"
    if prepared:
        head = "مرشح Prepared Watch بدأ يتحرك الآن؛ لا ينتظر +20%/+50%."
    elif from_memory:
        head = "مرشح حي سابق ما زال تحت مراقبة لصيقة بذاكرة قصيرة."
    else:
        head = "مرشح جديد أثناء التداول بدأ حركة حية مع حجم؛ يدخل مراقبة لصيقة فورًا."
    if extended_for_pullback:
        head += " الحركة أصبحت ممتدة؛ تحفظ كمراقبة V2V لكن العرض العملي يكون استمرار مشروط/Pullback وليس دخولًا مباشرًا."
    reasons = [head, f"الحركة الحالية {safe_round(change_pct, 2)}% مع حجم {int(volume or 0):,} ودولار فوليوم تقريبي {int(dollar_volume or 0):,}."]
    return {
        **debug,
        "eligible": True,
        "score": safe_round((70 if prepared else 54) + min(max(change_pct, 0), 30) * 1.6 + min(dollar_volume / 250000, 18), 3),
        "stage": stage,
        "stage_ar": stage_ar,
        "label_ar": stage_ar,
        "reasons": reasons,
        "extended_for_pullback_v2v1": extended_for_pullback,
        "extreme_extension_v2v1": extreme_extension,
        "target_bucket_v2v1": "continuation_pullback" if extended_for_pullback else "live_tight_monitoring",
        "action_ar_v2v1": "استمرار مشروط: لا دخول الآن إلا بعد تماسك أو Pullback أو إعادة اختبار واضحة." if extended_for_pullback else "تأكيد مبكر حي — مراقبة لصيقة فقط.",
        "prepared_watch_symbol": bool(prepared),
        "new_intraday_symbol": not bool(prepared),
        "extended_hours": bool((quote or {}).get("extended_hours")),
    }


def _live_tight_memory_item(symbol: str, profile: dict, *, source: str = "fmp_live") -> dict:
    sym = _clean_symbol(symbol)
    return {
        "symbol": sym,
        "score": safe_round(profile.get("score", 0), 3),
        "price": safe_round(profile.get("price", 0), 4),
        "change_pct": safe_round(profile.get("change_pct", 0), 2),
        "volume": safe_round(profile.get("volume", 0), 0),
        "dollar_volume": safe_round(profile.get("dollar_volume", 0), 0),
        "stage": str(profile.get("stage") or "live_early_confirmation"),
        "stage_ar": str(profile.get("stage_ar") or "تأكيد مبكر حي"),
        "label_ar": str(profile.get("label_ar") or profile.get("stage_ar") or "تأكيد مبكر حي"),
        "prepared_watch_symbol": bool(profile.get("prepared_watch_symbol")),
        "new_intraday_symbol": bool(profile.get("new_intraday_symbol")),
        "extended_hours": bool(profile.get("extended_hours")),
        "source": str(source or "fmp_live"),
        "reasons": list(profile.get("reasons") or [])[:8],
    }


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


def _micro_explosion_metrics_from_price_change_volume(price: float, change_pct: float, volume: float, source_kind: str = "fmp_mover") -> dict:
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
        "micro_explosion_source_kind": source_kind,
    }


def _micro_explosion_capture_score(ticker: str, metrics: dict, phase_detail: str = "", source_kind: str = "grouped") -> tuple[bool, float, list[str], dict]:
    """V2R source-only capture for likely explosive small-stock candidates.

    The old problem was mixing cheap/quiet names with the real simulator-style
    candidates.  This score does not ask "where will it display?"; it asks
    whether the symbol has evidence of accumulation, a strong candle, or first
    ignition while still not being fully extended.
    """
    price = to_float((metrics or {}).get("price") or (metrics or {}).get("fmp_price") or (metrics or {}).get("live_price"))
    raw_change = to_float((metrics or {}).get("day_change_pct"))
    chg = raw_change * 100.0 if abs(raw_change) <= 1.5 and source_kind in {"polygon_grouped", "grouped"} else raw_change
    for _ck in ("change_pct", "fmp_change_pct", "live_change_pct"):
        _cv = to_float((metrics or {}).get(_ck))
        if _cv != 0:
            chg = _cv
    volume = to_float((metrics or {}).get("volume") or (metrics or {}).get("fmp_volume") or (metrics or {}).get("live_volume"))
    dollar_volume = to_float((metrics or {}).get("dollar_volume") or (metrics or {}).get("live_dollar_volume"))
    if dollar_volume <= 0 and price > 0 and volume > 0:
        dollar_volume = price * volume
    range_pct = to_float((metrics or {}).get("range_pct"))
    close_strength = to_float((metrics or {}).get("close_strength"))
    near_high = bool((metrics or {}).get("near_high"))

    reasons: list[str] = []
    blockers: list[str] = []
    score = 0.0
    flags = {
        "price": price,
        "change_pct": chg,
        "day_change_pct": chg,
        "volume": volume,
        "dollar_volume": dollar_volume,
        "range_pct": range_pct,
        "close_strength": close_strength,
        "near_high": near_high,
        "micro_explosion_source_kind": source_kind,
        "micro_explosion_capture_v2r": True,
    }

    if price <= 0:
        blockers.append("سعر غير متاح")
        flags.update({"micro_explosion_capture_eligible": False, "micro_explosion_capture_score": 0.0, "micro_explosion_blockers_ar": blockers})
        return False, 0.0, blockers, flags
    if price < MICRO_EXPLOSION_CAPTURE_MIN_PRICE:
        blockers.append("السعر أقل من حد الالتقاط الحالي — ضجيج/خطر أعلى من الهدف")
    if price > MICRO_EXPLOSION_CAPTURE_EXTENDED_MAX_PRICE:
        blockers.append("السعر أعلى من نطاق أسهم الانفجار الصغيرة")
    if chg >= MICRO_EXPLOSION_CAPTURE_MAX_CHANGE_PCT:
        blockers.append("الحركة كبيرة جدًا الآن — لا نريد التقاطًا متأخرًا بعد الانفجار")
    if dollar_volume > MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME:
        blockers.append("دولار فوليوم كبير جدًا — غالبًا اسم معروف/حركة أبطأ لا تشبه المحاكي")

    if price <= 1.0:
        score += 26; reasons.append("سعر micro قابل لانفجار سريع")
    elif price <= 5.0:
        score += 23; reasons.append("سعر صغير مناسب للانفجار")
    elif price <= MICRO_EXPLOSION_CAPTURE_MAX_PRICE:
        score += 15; reasons.append("ضمن نطاق الأسهم الصغيرة")
    else:
        score += 5; reasons.append("فوق 10$ — يحتاج دليل أقوى جدًا")

    if 50_000 <= dollar_volume <= 350_000:
        score += 20; reasons.append("دولار فوليوم صغير يبدأ يشتعل")
    elif 350_000 < dollar_volume <= 2_500_000:
        score += 26; reasons.append("تجميع/سيولة صغيرة نشطة")
    elif 2_500_000 < dollar_volume <= 9_000_000:
        score += 20; reasons.append("سيولة كافية للانفجار وليست ضخمة")
    elif 9_000_000 < dollar_volume <= MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME:
        score += 8; reasons.append("سيولة أعلى؛ يحتاج شمعة قوية لا مجرد سعر منخفض")
    elif dollar_volume <= 0:
        score += 3; reasons.append("السيولة غير مؤكدة — يبقى مرشح مصدر فقط حتى يتأكد الحجم")

    if -3.0 <= chg < 0.8:
        score += 7; reasons.append("هادئ/تجميع محتمل قبل الحركة")
    elif 0.8 <= chg < 4.0:
        score += 18; reasons.append("بداية حركة قبل الانفجار")
    elif 4.0 <= chg < 9.0:
        score += 26; reasons.append("شمعة/حركة قوية مبكرة")
    elif 9.0 <= chg < 15.0:
        score += 17; reasons.append("Ignition قوي لكنه ليس متأخرًا جدًا")
    elif 15.0 <= chg < MICRO_EXPLOSION_CAPTURE_MAX_CHANGE_PCT:
        score += 4; reasons.append("تحرك عالٍ — مراقبة خطفة فقط إن لم يكن قرب مقاومة")

    candle_confirmed = False
    accumulation_confirmed = False
    if range_pct > 0:
        if 0.025 <= range_pct < 0.07:
            score += 12; reasons.append("اتساع شمعة مناسب بدون انفجار كامل")
        elif 0.07 <= range_pct <= 0.18:
            score += 18; reasons.append("شمعة قوية/نطاق يومي واضح")
        elif 0.18 < range_pct <= 0.30:
            score += 5; reasons.append("شمعة واسعة جدًا — عالية المخاطر")
        elif range_pct > 0.30:
            score -= 10; blockers.append("النطاق واسع جدًا وقد يكون بعد الانفجار")
    if close_strength >= 0.75:
        score += 18; reasons.append("إغلاق قوي داخل الشمعة")
        candle_confirmed = True
    elif close_strength >= 0.58:
        score += 10; reasons.append("إغلاق بنّاء/تجميع داخل النطاق")
        accumulation_confirmed = True
    elif close_strength > 0 and close_strength < 0.35 and chg > 2.0:
        score -= 12; blockers.append("إغلاق ضعيف بعد حركة — احتمال فشل لا انفجار")
    if near_high:
        score += 6; reasons.append("قريب من قمة اليوم/منطقة اختراق")

    # FMP sources often lack candle range/close position, so require clean ignition.
    fmp_source = source_kind in {"fmp_mover", "fmp_live", "fmp_small_mover", "fmp_reference_fallback_v2r2"}
    full_market_source = source_kind in {"polygon_full_market_v2r2", "polygon_full_market_v2r2"}
    if fmp_source:
        score += 8; reasons.append("مصدر حي/متحرك من FMP وليس Watch قديم")
    if full_market_source:
        score += 6; reasons.append("مسح كامل للسوق بعد/قبل/أثناء الجلسة وليس Watch قديم")

    activity_clue = bool(abs(chg) >= 0.8 or range_pct >= 0.025 or (50_000 <= dollar_volume <= MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME))
    first_ignition = bool(0.8 <= chg < 15.0 and 50_000 <= dollar_volume <= MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME)
    strong_candle = bool((range_pct >= 0.04 and close_strength >= 0.58) or (near_high and close_strength >= 0.58))
    quiet_accumulation = bool(-3.0 <= chg <= 4.0 and 0.025 <= range_pct <= 0.12 and close_strength >= 0.52)
    limited_motion = bool(abs(chg) < 0.8 and range_pct < 0.025 and dollar_volume < 100_000 and not near_high)
    if limited_motion:
        blockers.append("حركة محدودة جدًا — ليست تجميع/شمعة قوية كافية")

    eligible = bool(
        MICRO_EXPLOSION_CAPTURE_ENABLED
        and not blockers
        and activity_clue
        and (first_ignition or strong_candle or quiet_accumulation or (fmp_source and 0.6 <= chg < 15.0) or (full_market_source and score >= 54 and -4.0 <= chg < 16.0))
        and score >= 54
    )
    flags.update({
        "micro_explosion_capture_score": safe_round(score, 3),
        "micro_explosion_capture_eligible": eligible,
        "micro_explosion_reasons_ar": reasons[:8],
        "micro_explosion_blockers_ar": blockers[:8],
        "micro_explosion_first_ignition": first_ignition,
        "micro_explosion_strong_candle": strong_candle,
        "micro_explosion_quiet_accumulation": quiet_accumulation,
        "micro_explosion_limited_motion": limited_motion,
    })
    return eligible, score, (reasons if eligible else blockers or reasons)[:8], flags


def _micro_explosion_debug_payload(ranked: list[dict], final: list[str], max_symbols: int) -> dict:
    rows = []
    final_set = {str(x or "").upper() for x in (final or [])}
    for r in ranked or []:
        if not isinstance(r, dict):
            continue
        metrics = r.get("metrics") if isinstance(r.get("metrics"), dict) else {}
        sources = list(r.get("sources") or [])
        if "micro_explosion_capture_v2r" not in sources and not metrics.get("micro_explosion_capture_v2r"):
            continue
        sym = str(r.get("symbol") or "").upper()
        rows.append({
            "symbol": sym,
            "score": r.get("score", 0),
            "source_score": metrics.get("micro_explosion_capture_score", 0),
            "entered_final_universe_before_sharia": bool(sym in final_set),
            "sources": sources[:8],
            "price": metrics.get("price") or metrics.get("fmp_price") or metrics.get("live_price"),
            "change_pct": metrics.get("change_pct") or metrics.get("day_change_pct") or metrics.get("fmp_change_pct") or metrics.get("live_change_pct"),
            "dollar_volume": metrics.get("dollar_volume"),
            "range_pct": metrics.get("range_pct"),
            "close_strength": metrics.get("close_strength"),
            "reasons_ar": list(metrics.get("micro_explosion_reasons_ar") or r.get("reasons") or [])[:8],
            "blockers_ar": list(metrics.get("micro_explosion_blockers_ar") or [])[:8],
        })
    rows.sort(key=lambda x: float(x.get("source_score") or x.get("score") or 0), reverse=True)
    return {
        "version": "micro_explosion_capture_v2r1_2026_06_20",
        "enabled": bool(MICRO_EXPLOSION_CAPTURE_ENABLED),
        "source_count": len(rows),
        "entered_final_universe_count": len([x for x in rows if x.get("entered_final_universe_before_sharia")]),
        "candidate_symbols": [x.get("symbol") for x in rows[:80]],
        "top_candidates": rows[:80],
        "rule_ar": "V2R1 يلتقط أسهم تجميع/شموع قوية/احتمال انفجار من السوق كاملًا ويعيد مراقبتها لاصطيادها قبل أن تطير. لا يغير Strong/Cautious ولا يعطي شراء مباشر.",
    }


def _micro_explosion_phase_mode(phase_detail: str = "") -> str:
    txt = str(phase_detail or "").lower()
    if "pre_market" in txt:
        return "pre_market"
    if "after_hours" in txt:
        return "after_hours"
    if "open" in txt:
        return "regular"
    return "after_close_review"


def _is_micro_explosion_seed_symbol(symbol: str) -> bool:
    return _clean_symbol(symbol) in MICRO_EXPLOSION_SEED_SYMBOLS


def _micro_candidate_memory_item(symbol: str, metrics: dict, reasons: list, score: float, phase_detail: str = "", source: str = "") -> dict:
    now_iso = datetime.now(NY_TZ).isoformat(timespec="seconds")
    sym = _clean_symbol(symbol)
    return {
        "symbol": sym,
        "first_seen_at": now_iso,
        "last_seen_at": now_iso,
        "last_phase": _micro_explosion_phase_mode(phase_detail),
        "source": str(source or "micro_explosion_capture_v2r1"),
        "score": safe_round(score, 3),
        "price": safe_round((metrics or {}).get("price") or (metrics or {}).get("fmp_price") or (metrics or {}).get("live_price"), 4),
        "change_pct": safe_round((metrics or {}).get("change_pct") or (metrics or {}).get("day_change_pct") or (metrics or {}).get("fmp_change_pct") or (metrics or {}).get("live_change_pct"), 3),
        "volume": safe_round((metrics or {}).get("volume") or (metrics or {}).get("fmp_volume") or (metrics or {}).get("live_volume"), 3),
        "dollar_volume": safe_round((metrics or {}).get("dollar_volume") or (metrics or {}).get("live_dollar_volume"), 3),
        "range_pct": safe_round((metrics or {}).get("range_pct"), 5),
        "close_strength": safe_round((metrics or {}).get("close_strength"), 3),
        "reasons_ar": list(reasons or [])[:8],
        "observation_count": 1,
        "micro_explosion_capture_v2r1": True,
        "micro_explosion_capture_v2r": True,
    }


def _load_micro_explosion_watch_memory() -> tuple[list[dict], dict]:
    now_ts = time.time()
    ttl_sec = int(MICRO_EXPLOSION_WATCH_TTL_HOURS) * 3600
    raw = _sqlite_get_json(MICRO_EXPLOSION_WATCH_MEMORY_KEY, {}) or {}
    if isinstance(raw, list):
        raw = {str((x or {}).get("symbol") or "").upper(): x for x in raw if isinstance(x, dict)}
    if not isinstance(raw, dict):
        raw = {}
    active = []
    kept = {}
    expired = 0
    for sym, item in (raw or {}).items():
        if not isinstance(item, dict):
            continue
        sym = _clean_symbol(sym or item.get("symbol"))
        if not sym:
            continue
        last_seen = str(item.get("last_seen_at") or item.get("first_seen_at") or "")
        age_ok = True
        try:
            dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=NY_TZ)
            age_ok = (now_ts - dt.timestamp()) <= ttl_sec
        except Exception:
            age_ok = True
        if not age_ok:
            expired += 1
            continue
        item = dict(item)
        item["symbol"] = sym
        kept[sym] = item
        active.append(item)
    active.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    return active[:MICRO_EXPLOSION_CLOSE_WATCH_LIMIT], {
        "version": "micro_explosion_close_watch_memory_v2r1_2026_06_20",
        "active_count": len(active[:MICRO_EXPLOSION_CLOSE_WATCH_LIMIT]),
        "expired_count": expired,
        "ttl_hours": int(MICRO_EXPLOSION_WATCH_TTL_HOURS),
        "symbols": [x.get("symbol") for x in active[:60]],
    }


def _update_micro_explosion_watch_memory(items: list[dict], phase_detail: str = "") -> dict:
    if not items:
        active, debug = _load_micro_explosion_watch_memory()
        return {**debug, "updated_count": 0, "stored_count": len(active)}
    active, _ = _load_micro_explosion_watch_memory()
    existing = {str((x or {}).get("symbol") or "").upper(): dict(x or {}) for x in active if isinstance(x, dict)}
    now_iso = datetime.now(NY_TZ).isoformat(timespec="seconds")
    updated = 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        sym = _clean_symbol(item.get("symbol"))
        if not sym:
            continue
        prev = existing.get(sym, {})
        merged = dict(prev)
        merged.update(item)
        merged["symbol"] = sym
        merged["first_seen_at"] = prev.get("first_seen_at") or item.get("first_seen_at") or now_iso
        merged["last_seen_at"] = now_iso
        merged["last_phase"] = _micro_explosion_phase_mode(phase_detail)
        merged["observation_count"] = int(prev.get("observation_count", 0) or 0) + 1
        existing[sym] = merged
        updated += 1
    kept = list(existing.values())
    kept.sort(key=lambda x: float(x.get("score", 0) or 0), reverse=True)
    kept = kept[:MICRO_EXPLOSION_CLOSE_WATCH_LIMIT]
    try:
        _sqlite_set_json(MICRO_EXPLOSION_WATCH_MEMORY_KEY, {x.get("symbol"): x for x in kept if x.get("symbol")})
    except Exception:
        pass
    return {
        "version": "micro_explosion_close_watch_memory_v2r1_2026_06_20",
        "updated_count": updated,
        "stored_count": len(kept),
        "symbols": [x.get("symbol") for x in kept[:60]],
        "rule_ar": "ذاكرة Compact فقط للرموز التي ظهرت عليها بوادر انفجار؛ تُعاد للمصدر قبل الافتتاح/أثناءه/بعده ولا تخزن raw files.",
    }



def _recover_recent_grouped_market_map(current_date: str = "", grouped_map: dict | None = None) -> tuple[str, dict, str, dict]:
    """V2R2: recover a real grouped map when previous_business_day is a market holiday.

    Juneteenth/holiday Fridays can make scanner._select_grouped_market_map return
    previous_grouped with 0 rows.  For micro-explosion discovery this is fatal
    because the after-close full-market scan becomes scanned=0.  We walk backward
    over recent weekdays until Polygon returns a usable grouped daily map.
    """
    initial_count = len(grouped_map or {})
    debug = {
        "version": "recent_grouped_recovery_v2r2_2026_06_20",
        "enabled": True,
        "initial_date": str(current_date or ""),
        "initial_count": int(initial_count),
        "min_rows": int(DYNAMIC_DISCOVERY_GROUPED_RECOVERY_MIN_ROWS),
        "lookback_days": int(DYNAMIC_DISCOVERY_GROUPED_RECOVERY_DAYS),
        "attempts": [],
        "recovered": False,
        "selected_date": str(current_date or ""),
        "selected_count": int(initial_count),
        "source_mode": "original_grouped",
        "rule_ar": "V2R2: إذا صادف previous_grouped يوم عطلة/صفر بيانات، نرجع لآخر جلسة Polygon grouped صالحة حتى لا يصبح Micro Full Scan = 0.",
    }
    if initial_count >= int(DYNAMIC_DISCOVERY_GROUPED_RECOVERY_MIN_ROWS):
        debug["source_mode"] = "original_grouped_ok"
        return str(current_date or ""), dict(grouped_map or {}), "grouped_ok", debug

    try:
        if current_date:
            anchor = datetime.fromisoformat(str(current_date)[:10]).date()
        else:
            anchor = datetime.now(NY_TZ).date()
    except Exception:
        anchor = datetime.now(NY_TZ).date()

    best_date = str(current_date or "")
    best_map = dict(grouped_map or {})
    best_count = initial_count
    for offset in range(0, int(DYNAMIC_DISCOVERY_GROUPED_RECOVERY_DAYS) + 1):
        day = anchor - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        dstr = day.isoformat()
        try:
            m = _scanner.get_grouped_daily_map(dstr) or {}
            count = len(m or {})
        except Exception as exc:
            debug["attempts"].append({"date": dstr, "error": f"{type(exc).__name__}: {str(exc)[:80]}"})
            continue
        debug["attempts"].append({"date": dstr, "count": int(count)})
        if count > best_count:
            best_date, best_map, best_count = dstr, dict(m or {}), count
        if count >= int(DYNAMIC_DISCOVERY_GROUPED_RECOVERY_MIN_ROWS):
            debug.update({
                "recovered": True,
                "selected_date": dstr,
                "selected_count": int(count),
                "source_mode": "recent_grouped_recovered",
            })
            return dstr, dict(m or {}), "recent_grouped_recovered", debug

    debug.update({
        "recovered": bool(best_count > initial_count),
        "selected_date": best_date,
        "selected_count": int(best_count),
        "source_mode": "recent_grouped_best_available" if best_count > initial_count else "no_grouped_recovered",
    })
    return best_date, best_map, debug["source_mode"], debug


def _quote_rows_for_reference_fallback(symbols: list[str], phase_detail: str = "") -> tuple[list[dict], dict]:
    """Last-resort V2R2 fallback when no grouped rows can be recovered.

    It samples the Micro/China seed universe first, then reference tickers, using
    FMP batch quotes in capped chunks.  It is monitoring-only and does not create
    BUY/Cautious signals.
    """
    debug = {
        "version": "micro_explosion_reference_quote_fallback_v2r2_2026_06_20",
        "enabled": bool(MICRO_EXPLOSION_CAPTURE_ENABLED),
        "requested_symbols": 0,
        "quote_symbols": 0,
        "eligible_count": 0,
        "top_symbols": [],
        "batches": [],
        "rule_ar": "Fallback فقط إذا لم تتوفر Polygon grouped: نؤكد Seed/Reference عبر FMP batch بشكل محدود ونضيف مراقبة لصيقة لا شراء.",
    }
    if not MICRO_EXPLOSION_CAPTURE_ENABLED:
        return [], debug
    seed_first = sorted(MICRO_EXPLOSION_SEED_SYMBOLS)
    ordered = _scanner.unique_keep_order(seed_first + list(symbols or []))[:MICRO_EXPLOSION_REFERENCE_FALLBACK_LIMIT]
    debug["requested_symbols"] = len(ordered)
    out: list[dict] = []
    for start in range(0, len(ordered), 300):
        batch = ordered[start:start+300]
        if not batch:
            continue
        try:
            bundle = get_live_quotes(batch, prefer_cache=False, allow_fallback=False)
            quotes = (bundle or {}).get("quotes", {}) or {}
            qdiag = (bundle or {}).get("diagnostics", {}) or {}
            debug["batches"].append({"start": start, "requested": len(batch), "quotes": len(quotes), "source": qdiag.get("source")})
        except Exception as exc:
            debug["batches"].append({"start": start, "requested": len(batch), "error": f"{type(exc).__name__}: {str(exc)[:80]}"})
            continue
        debug["quote_symbols"] += len(quotes or {})
        for sym, q in (quotes or {}).items():
            sym = _clean_symbol(sym)
            if not sym:
                continue
            price = to_float((q or {}).get("price"))
            chg = to_float((q or {}).get("change_pct"))
            volume = to_float((q or {}).get("volume"))
            metrics = _micro_explosion_metrics_from_price_change_volume(price, chg, volume, source_kind="fmp_reference_fallback_v2r2")
            seed = _is_micro_explosion_seed_symbol(sym)
            eligible, score, reasons, flags = _micro_explosion_capture_score(sym, metrics, phase_detail=phase_detail, source_kind="fmp_reference_fallback_v2r2")
            # Quote fallback has no candle range; allow seed names a conservative sticky watch
            # when they are in the right price/dollar-volume band even before the full candle exists.
            dollar_volume = to_float(metrics.get("dollar_volume"))
            if not eligible and seed and 0 < price <= MICRO_EXPLOSION_CAPTURE_EXTENDED_MAX_PRICE and 35_000 <= dollar_volume <= MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME and -6.0 <= chg < 18.0:
                eligible = True
                score = max(float(score or 0), 52.0 + min(max(chg, 0), 12.0) * 1.2)
                reasons = ["Seed Micro/China من FMP fallback — مراقبة لصيقة حتى تظهر شمعة/حجم أوضح"] + list(reasons or [])[:5]
                flags.update({
                    "micro_explosion_capture_score": safe_round(score, 3),
                    "micro_explosion_capture_eligible": True,
                    "micro_explosion_reasons_ar": reasons[:8],
                    "micro_explosion_capture_v2r1": True,
                    "micro_explosion_seed_match": True,
                    "micro_explosion_reference_fallback_v2r2": True,
                })
            if not eligible:
                continue
            if seed:
                score += 6.0
                reasons = ["Seed Micro/China/Low-Float تحت مراقبة V2R2"] + list(reasons or [])[:7]
            flags.update({"micro_explosion_capture_v2r1": True, "micro_explosion_seed_match": seed, "micro_explosion_reference_fallback_v2r2": True})
            out.append({"symbol": sym, "score": safe_round(score, 3), "reasons": reasons[:8], "metrics": {**metrics, **flags}})
    out.sort(key=lambda r: float(r.get("score", 0) or 0), reverse=True)
    out = out[:MICRO_EXPLOSION_CAPTURE_INJECT_LIMIT]
    debug["eligible_count"] = len(out)
    debug["top_symbols"] = [r.get("symbol") for r in out[:50]]
    return out, debug


def _big_explosion_live_metrics_from_price_change_volume(price: float, change_pct: float, volume: float, source_kind: str = "live") -> dict:
    price = to_float(price)
    change_pct = to_float(change_pct)
    volume = to_float(volume)
    dollar_volume = price * volume if price > 0 and volume > 0 else 0.0
    return {
        "price": price,
        "day_change_pct": change_pct,
        "change_pct": change_pct,
        "volume": volume,
        "dollar_volume": dollar_volume,
        "range_pct": 0.0,
        "close_strength": 0.0,
        "near_high": False,
        "big_explosion_live_source_kind": source_kind,
    }


def _big_explosion_live_lane_score(ticker: str, metrics: dict, phase_detail: str = "", source_kind: str = "live") -> tuple[bool, float, list[str], dict]:
    """Monitoring-only source lane for big explosions.

    This is intentionally separate from Micro Explosion: Micro tries to catch
    pre-explosion accumulation; V2T keeps the current PM/open runner visible as
    soon as it is +5/+10/+20 with volume so we do not miss ICCM/EHGO-style
    moves just because they became "too extended" for the early lane.
    """
    price = to_float((metrics or {}).get("price") or (metrics or {}).get("fmp_price") or (metrics or {}).get("live_price"))
    chg = to_float((metrics or {}).get("change_pct") or (metrics or {}).get("fmp_change_pct") or (metrics or {}).get("live_change_pct") or (metrics or {}).get("day_change_pct"))
    if source_kind in {"polygon_grouped", "grouped", "historical_minute_slice"} and abs(chg) <= 1.5:
        chg *= 100.0
    volume = to_float((metrics or {}).get("volume") or (metrics or {}).get("fmp_volume") or (metrics or {}).get("live_volume"))
    dollar_volume = to_float((metrics or {}).get("dollar_volume") or (metrics or {}).get("live_dollar_volume"))
    if dollar_volume <= 0 and price > 0 and volume > 0:
        dollar_volume = price * volume
    range_pct = to_float((metrics or {}).get("range_pct"))
    close_strength = to_float((metrics or {}).get("close_strength"))
    near_high = bool((metrics or {}).get("near_high"))
    reasons: list[str] = []
    blockers: list[str] = []
    score = 0.0
    if price <= 0:
        blockers.append("سعر غير متاح")
    elif price < BIG_EXPLOSION_LIVE_MIN_PRICE:
        blockers.append("أقل من نطاق Big Explosion")
    elif price <= 2:
        score += 22; reasons.append("سعر micro قابل لانفجار كبير")
    elif price <= 8:
        score += 20; reasons.append("سعر صغير مناسب لانفجار كبير")
    elif price <= 20:
        score += 14; reasons.append("سعر متوسط لكن قابل لانفجار سريع")
    elif price <= BIG_EXPLOSION_LIVE_MAX_PRICE:
        score += 10; reasons.append("سعر أعلى من Low-Float لكنه ضمن انفجار الافتتاح/الجلسة")
    else:
        blockers.append("فوق نطاق Big Explosion Live Lane")

    if chg < BIG_EXPLOSION_LIVE_MIN_CHANGE_PCT:
        blockers.append("لم يبدأ تسارع كافٍ بعد")
    elif BIG_EXPLOSION_LIVE_MIN_CHANGE_PCT <= chg < 5:
        score += 18; reasons.append("بداية مبكرة جدًا +3% مع حجم — مراقبة قبل الانفجار")
    elif 5 <= chg < 12:
        score += 26; reasons.append("بداية تسارع +5% قبل الانفجار الكبير")
    elif 12 <= chg < 25:
        score += 34; reasons.append("تسارع قوي +10/+20% يحتاج ظهور فوري")
    elif 25 <= chg < 60:
        score += 30; reasons.append("انفجار نشط — مراقبة لا مطاردة")
    elif 60 <= chg < 140:
        score += 22; reasons.append("انفجار كبير جدًا — تقرير توقيت/حذر")
    elif 140 <= chg <= BIG_EXPLOSION_LIVE_MAX_CHANGE_PCT:
        score += 12; reasons.append("انفجار ضخم — يظهر للتقرير لا للدخول المباشر")
    else:
        blockers.append("حركة شاذة جدًا/متأخرة خارج نطاق التقرير")

    if BIG_EXPLOSION_LIVE_MIN_DOLLAR_VOLUME <= dollar_volume <= 1_500_000:
        score += 20; reasons.append("دولار فوليوم مبكر قابل للاشتعال")
    elif 1_500_000 < dollar_volume <= 12_000_000:
        score += 18; reasons.append("سيولة انفجار نشطة")
    elif 12_000_000 < dollar_volume <= BIG_EXPLOSION_LIVE_MAX_DOLLAR_VOLUME:
        score += 8; reasons.append("سيولة عالية — مناسب للتقرير/استمرار فقط")
    elif 10_000 <= dollar_volume < BIG_EXPLOSION_LIVE_MIN_DOLLAR_VOLUME and chg >= 10:
        score += 6; reasons.append("سيولة مبكرة منخفضة لكن الحركة قوية — تقرير مبكر")
    elif dollar_volume < BIG_EXPLOSION_LIVE_MIN_DOLLAR_VOLUME:
        blockers.append("السيولة لم تؤكد الانفجار بعد")
    else:
        blockers.append("دولار فوليوم ضخم جدًا؛ غالبًا اسم معروف/متأخر")

    if range_pct >= 0.03:
        score += min(range_pct * 100, 20); reasons.append("نطاق/شمعة انفجار واضحة")
    if close_strength >= 0.65:
        score += 8; reasons.append("يتداول قرب قمة الشريحة/اليوم")
    if near_high:
        score += 5; reasons.append("قريب من القمة — زخم نشط")
    if source_kind in {"fmp_mover", "fmp_live", "historical_minute_slice", "historical_minute_slice_v2t1", "polygon_grouped_v2t", "polygon_grouped_v2t1", "prior_session_prepared_v2u", "opening_instant_v2u"}:
        score += 8; reasons.append("مصدر حي/زمني مستقل أو قائمة جاهزة قبل السوق")

    eligible = bool(not blockers and score >= 44)
    flags = {
        "price": price,
        "day_change_pct": chg,
        "change_pct": chg,
        "volume": volume,
        "dollar_volume": dollar_volume,
        "range_pct": range_pct,
        "close_strength": close_strength,
        "near_high": near_high,
        "big_explosion_live_lane_v2t": True,
        "big_explosion_live_lane_v2t1": True,
        "big_explosion_live_lane_v2t2": True,
        "big_explosion_live_lane_v2u": True,
        "big_explosion_live_score": safe_round(score, 3),
        "big_explosion_live_eligible": eligible,
        "big_explosion_live_source_kind": source_kind,
        "big_explosion_live_reasons_ar": reasons[:8],
        "big_explosion_live_blockers_ar": blockers[:8],
        "big_explosion_gain_pct": safe_round(chg, 3),
    }
    return eligible, score, reasons[:8], flags


def _collect_big_explosion_live_lane_candidates(grouped_map: dict, phase_detail: str = "") -> tuple[list[dict], dict]:
    debug = {
        "version": "big_explosion_live_lane_v2u_real_capture_pipeline_2026_06_20",
        "enabled": bool(BIG_EXPLOSION_LIVE_LANE_ENABLED),
        "scan_cap": int(BIG_EXPLOSION_LIVE_SCAN_CAP),
        "scanned": 0,
        "eligible_count": 0,
        "top_symbols": [],
        "rule_ar": "V2U: مسار مراقبة حي + قائمة جاهزة بعد مسح الأمس الكامل؛ يبدأ من +3% مع حجم ويعطي مقاعد محجوزة للانفجارات المبكرة، ولا يفتح BUY_NOW.",
    }
    if not BIG_EXPLOSION_LIVE_LANE_ENABLED or not grouped_map:
        return [], debug
    out: list[dict] = []
    for idx, (ticker, daily) in enumerate((grouped_map or {}).items()):
        if idx >= BIG_EXPLOSION_LIVE_SCAN_CAP:
            break
        sym = _clean_symbol(ticker)
        if not sym or not daily:
            continue
        debug["scanned"] += 1
        metrics = _source_metrics_from_grouped(daily) or {}
        price = to_float(metrics.get("price"))
        volume = to_float(metrics.get("volume"))
        if price <= 0 or volume <= 0:
            continue
        chg = to_float(metrics.get("day_change_pct")) * 100.0
        metrics = {**metrics, "change_pct": chg, "day_change_pct": chg}
        eligible, score, reasons, flags = _big_explosion_live_lane_score(sym, metrics, phase_detail=phase_detail, source_kind="polygon_grouped_v2t1")
        if not eligible:
            continue
        out.append({"symbol": sym, "score": safe_round(score, 3), "reasons": reasons[:8], "metrics": {**metrics, **flags}})
    out.sort(key=lambda r: float(r.get("score", 0) or 0), reverse=True)
    out = out[:BIG_EXPLOSION_LIVE_INJECT_LIMIT]
    debug["eligible_count"] = len(out)
    debug["top_symbols"] = [r.get("symbol") for r in out[:60]]
    return out, debug

def _collect_micro_explosion_full_market_candidates(grouped_map: dict, phase_detail: str = "", reference_tickers: list[str] | None = None) -> tuple[list[dict], dict]:
    debug = {
        "version": "micro_explosion_full_market_scan_v2r2_recent_grouped_recovery_2026_06_20",
        "enabled": bool(MICRO_EXPLOSION_CAPTURE_ENABLED),
        "scan_cap": int(MICRO_EXPLOSION_FULL_MARKET_SCAN_CAP),
        "scanned": 0,
        "eligible_count": 0,
        "seed_match_count": 0,
        "blocked_count": 0,
        "top_symbols": [],
        "fallback_used": False,
        "fallback_debug": {},
        "rule_ar": "V2R2: يمسح آخر grouped صالح بعد الإغلاق/العطلة، وإذا لم يوجد يستخدم FMP reference fallback محدود؛ الهدف ألا يصبح scanned=0.",
    }
    if not MICRO_EXPLOSION_CAPTURE_ENABLED:
        return [], debug
    if not grouped_map:
        fallback_rows, fallback_debug = _quote_rows_for_reference_fallback(reference_tickers or [], phase_detail=phase_detail)
        debug.update({
            "fallback_used": True,
            "fallback_debug": fallback_debug,
            "scanned": int((fallback_debug or {}).get("quote_symbols", 0) or 0),
            "eligible_count": len(fallback_rows or []),
            "top_symbols": [r.get("symbol") for r in (fallback_rows or [])[:50]],
        })
        return fallback_rows, debug
    out: list[dict] = []
    for idx, (ticker, daily) in enumerate((grouped_map or {}).items()):
        if idx >= MICRO_EXPLOSION_FULL_MARKET_SCAN_CAP:
            break
        sym = _clean_symbol(ticker)
        if not sym or not daily:
            continue
        debug["scanned"] += 1
        metrics = _source_metrics_from_grouped(daily) or {}
        price = to_float(metrics.get("price"))
        volume = to_float(metrics.get("volume"))
        if price <= 0 or volume <= 0:
            continue
        chg = to_float(metrics.get("day_change_pct")) * 100.0
        metrics = {**metrics, "change_pct": chg, "day_change_pct": chg}
        seed = _is_micro_explosion_seed_symbol(sym)
        if seed:
            debug["seed_match_count"] += 1
        eligible, score, reasons, flags = _micro_explosion_capture_score(sym, metrics, phase_detail=phase_detail, source_kind="polygon_full_market_v2r2")
        # V2R1: seed symbols from the simulator-style universe can be watched a bit
        # earlier if they have constructive candle evidence, but still no BUY.
        if not eligible and seed:
            range_pct = to_float(metrics.get("range_pct"))
            close_strength = to_float(metrics.get("close_strength"))
            dollar_volume = to_float(metrics.get("dollar_volume")) or price * volume
            constructive = bool(0 < price <= MICRO_EXPLOSION_CAPTURE_EXTENDED_MAX_PRICE and 20_000 <= dollar_volume <= MICRO_EXPLOSION_CAPTURE_MAX_DOLLAR_VOLUME and -4.0 <= chg < 18.0 and range_pct >= 0.012 and close_strength >= 0.46)
            if constructive:
                eligible = True
                score = max(float(score or 0), 48.0 + min(max(close_strength, 0), 1) * 18.0 + min(max(range_pct, 0) * 100, 14.0))
                reasons = ["Seed Micro/China + شمعة/تجميع بنّاء مبكر"] + list(reasons or [])[:5]
                flags.update({
                    "micro_explosion_capture_score": safe_round(score, 3),
                    "micro_explosion_capture_eligible": True,
                    "micro_explosion_reasons_ar": reasons[:8],
                    "micro_explosion_capture_v2r1": True,
                    "micro_explosion_seed_match": True,
                })
        if not eligible:
            debug["blocked_count"] += 1
            continue
        if seed:
            score += 8.0
            reasons = ["رمز ضمن Seed Micro/China/Low-Float للمراقبة اللصيقة"] + list(reasons or [])[:7]
        flags.update({"micro_explosion_capture_v2r1": True, "micro_explosion_seed_match": seed})
        out.append({"symbol": sym, "score": safe_round(score, 3), "reasons": reasons[:8], "metrics": {**metrics, **flags}})
    out.sort(key=lambda r: float(r.get("score", 0) or 0), reverse=True)
    out = out[:MICRO_EXPLOSION_CAPTURE_INJECT_LIMIT]
    debug["eligible_count"] = len(out)
    debug["top_symbols"] = [r.get("symbol") for r in out[:50]]
    return out, debug


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
    grouped_recovery_debug = {}
    try:
        market_date, grouped_map, recovered_mode, grouped_recovery_debug = _recover_recent_grouped_market_map(market_date, grouped_map or {})
        if recovered_mode and recovered_mode not in {"grouped_ok", "original_grouped_ok"}:
            source_mode = str(recovered_mode)
    except Exception as exc:
        grouped_recovery_debug = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
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
    micro_explosion_capture_count = 0
    micro_explosion_capture_symbols: list[str] = []
    micro_explosion_memory_items: list[dict] = []
    micro_explosion_full_market_status: dict = {}
    micro_explosion_watch_memory_debug: dict = {}
    big_explosion_live_count = 0
    big_explosion_live_symbols: list[str] = []
    big_explosion_live_debug: dict = {}
    prepared_big_explosion_watch_rows: list[dict] = []
    prepared_big_explosion_watch_debug: dict = {}
    live_tight_monitoring_memory_rows: list[dict] = []
    live_tight_monitoring_memory_debug: dict = {}
    live_tight_monitoring_items: list[dict] = []
    live_tight_monitoring_symbols: list[str] = []
    live_tight_monitoring_by_symbol: dict[str, dict] = {}
    micro_watch_rows: list[dict] = []

    # V2U prepared explosion watch: load compact prior-session scan candidates
    # before premarket/open so Sharia review and deep analysis start before the
    # first explosive candle.  This is only a source/watch lane.
    try:
        prepared_big_explosion_watch_rows, prepared_big_explosion_watch_debug = load_prepared_big_explosion_watch()
        for item in prepared_big_explosion_watch_rows or []:
            sym = _clean_symbol((item or {}).get("symbol"))
            if not sym:
                continue
            metrics = dict((item or {}).get("metrics") or {})
            reasons = list((item or {}).get("reasons") or [])
            score = float((item or {}).get("score", 0) or metrics.get("big_explosion_prepared_score") or 0)
            critical_flag = bool(metrics.get("critical_promotion_gate_v2u3") or metrics.get("critical_micro_probe_v2u3") or metrics.get("critical_iccm_probe_v2u3") or metrics.get("critical_tpc_probe_v2u3") or sym in {"EHGO", "ICCM", "TPC", "SNBR"})
            _add_candidate(
                candidates,
                sym,
                260 + score * 0.62 if critical_flag else 142 + score * 0.42,
                "big_explosion_prepared_watch_v2u",
                ("V2U4 قائمة حرجة قبل السوق: " if critical_flag else "V2U قائمة جاهزة قبل السوق من مسح جلسة الأمس: ") + "، ".join(reasons[:3]),
                {**metrics, "big_explosion_prepared_watch_v2u": True, "urgent_sharia_review_v2u": True, "big_explosion_live_lane_v2u": True, "critical_pre_explosion_watch_v2u4": critical_flag, "critical_live_prepared_watch_v2u4": critical_flag},
            )
            big_explosion_live_symbols.append(sym)
            if record_detection is not None:
                try:
                    record_detection(sym, price=float(metrics.get("price", 0) or metrics.get("close", 0) or 0), change_pct=float(metrics.get("change_pct", 0) or metrics.get("day_change_pct", 0) or 0), source_reason="V2U prepared watch from prior full-session scan", source_layer="big_explosion_prepared_watch_v2u", source_tags=["prior_session_full_scan", "big_explosion_prepared_watch_v2u", "urgent_sharia_review"], move_stage="Prepared Explosion Watch", early_or_late_detection="prepared_before_move")
                except Exception:
                    pass
    except Exception as exc:
        prepared_big_explosion_watch_debug = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}

    # V2R1 sticky close watch: candidates detected after close / premarket /
    # regular session are put back into the source before FMP confirmation.
    try:
        micro_watch_rows, micro_explosion_watch_memory_debug = _load_micro_explosion_watch_memory()
        for item in micro_watch_rows or []:
            sym = _clean_symbol((item or {}).get("symbol"))
            if not sym:
                continue
            _add_candidate(
                candidates,
                sym,
                58 + min(float((item or {}).get("score", 0) or 0), 90) * 0.18,
                "micro_explosion_close_watch_v2r1",
                "مراقبة لصيقة مستمرة من رادار الانفجار — لا تنتظر منبعًا قديمًا",
                {**item, "micro_explosion_capture_v2r": True, "micro_explosion_capture_v2r1": True},
            )
    except Exception as exc:
        micro_explosion_watch_memory_debug = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    # V2V sticky live-tight watch: short TTL memory for candidates that started
    # moving intraday. They are re-confirmed with FMP in the next cycle and do
    # not depend on the slow broad-market ranking.
    try:
        live_tight_monitoring_memory_rows, live_tight_monitoring_memory_debug = _load_live_tight_monitoring_memory()
        for item in live_tight_monitoring_memory_rows or []:
            sym = _clean_symbol((item or {}).get("symbol"))
            if not sym:
                continue
            _add_candidate(
                candidates,
                sym,
                132 + min(float((item or {}).get("score", 0) or 0), 120) * 0.24,
                "live_tight_monitoring_v2v",
                "V2V ذاكرة مراقبة لصيقة: مرشح بدأ حركة حية ويحتاج تحديث سريع لا دورة بطيئة",
                {**item, "live_tight_monitoring_v2v": True, "live_tight_memory_v2v": True},
            )
    except Exception as exc:
        live_tight_monitoring_memory_debug = {"ok": False, "load_error": f"{type(exc).__name__}: {str(exc)[:120]}"}

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
            try:
                me_ok, me_score, me_reasons, me_flags = _micro_explosion_capture_score(
                    ticker,
                    {**m, "day_change_pct": chg, "change_pct": chg},
                    phase_detail=str(phase_info.get("detail", "") or ""),
                    source_kind="polygon_grouped",
                )
                if me_ok:
                    micro_explosion_capture_count += 1
                    micro_explosion_capture_symbols.append(ticker)
                    _add_candidate(
                        candidates,
                        ticker,
                        70 + me_score * 0.38,
                        "micro_explosion_capture_v2r",
                        "Micro Explosion Capture V2R1: " + "، ".join(me_reasons[:3]),
                        {**m, **me_flags, "micro_explosion_capture_reasons": me_reasons},
                    )
                    if record_detection is not None:
                        try:
                            record_detection(
                                ticker,
                                price=float(m.get("price", 0) or 0),
                                change_pct=float(chg or 0),
                                source_reason="Micro Explosion Capture V2R1: " + "، ".join(me_reasons[:3]),
                                source_layer="micro_explosion_capture_v2r1",
                                source_tags=["micro_explosion_capture_v2r", "accumulation_strong_candle_source"],
                                move_stage="High-Risk Early Explosion Watch",
                                early_or_late_detection="early",
                            )
                        except Exception:
                            pass
            except Exception:
                pass


    # V2R1: Whole-market micro explosion scan.  This deliberately bypasses the
    # old base_filters because simulator-style micro/China/low-float names are
    # often erased by normal liquidity filters before they ever reach Watch.
    try:
        micro_full_rows, micro_explosion_full_market_status = _collect_micro_explosion_full_market_candidates(
            grouped_map or {},
            phase_detail=str(phase_info.get("detail", "") or ""),
            reference_tickers=reference_tickers or [],
        )
        for item in micro_full_rows or []:
            sym = _clean_symbol((item or {}).get("symbol"))
            if not sym:
                continue
            score = float((item or {}).get("score", 0) or 0)
            reasons = list((item or {}).get("reasons") or [])
            metrics = dict((item or {}).get("metrics") or {})
            micro_explosion_capture_count += 1
            micro_explosion_capture_symbols.append(sym)
            micro_explosion_memory_items.append(_micro_candidate_memory_item(sym, metrics, reasons, score, str(phase_info.get("detail", "") or ""), "polygon_full_market_v2r2"))
            _add_candidate(
                candidates,
                sym,
                82 + score * 0.44,
                "micro_explosion_capture_v2r",
                "Micro Explosion Capture V2R2 — مسح آخر grouped صالح/السوق المتاح: " + "، ".join(reasons[:3]),
                {**metrics, "micro_explosion_capture_reasons": reasons, "micro_explosion_capture_v2r": True, "micro_explosion_capture_v2r1": True},
            )
            _add_candidate(
                candidates,
                sym,
                18,
                "micro_explosion_close_watch_v2r1",
                "بدأت مراقبة لصيقة لمرشح انفجار قبل أن يطير",
                {**metrics, "micro_explosion_capture_v2r": True, "micro_explosion_capture_v2r1": True},
            )
            if record_detection is not None:
                try:
                    record_detection(
                        sym,
                        price=float(metrics.get("price", 0) or 0),
                        change_pct=float(metrics.get("change_pct", metrics.get("day_change_pct", 0)) or 0),
                        source_reason="Micro Explosion Capture V2R2 full-market: " + "، ".join(reasons[:3]),
                        source_layer="micro_explosion_capture_v2r1",
                        source_tags=["micro_explosion_capture_v2r1", "full_market_scan", "sticky_close_watch"],
                        move_stage="Sticky Micro Explosion Watch",
                        early_or_late_detection="early",
                    )
                except Exception:
                    pass
    except Exception as exc:
        micro_explosion_full_market_status = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}



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
            _fast_lane_trace_update(low_float_fast_lane_trace, sym, source_kind="fmp_mover", metrics=_low_float_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_mover"), eligible=False, rejected_reason_code="price_below_source_min", rejected_reason_ar="السعر أقل من 1.5$ في مصدر Low-Float Fast Lane؛ لكنه يبقى مؤهلاً لرادار Micro Explosion V2R1 إذا ظهرت عليه بوادر انفجار.")
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
            me_metrics = _micro_explosion_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_mover")
            me_ok, me_score, me_reasons, me_flags = _micro_explosion_capture_score(sym, me_metrics, phase_detail=str(phase_info.get("detail", "") or ""), source_kind="fmp_mover")
            if me_ok:
                micro_explosion_capture_count += 1
                micro_explosion_capture_symbols.append(sym)
                micro_explosion_memory_items.append(_micro_candidate_memory_item(sym, {**me_metrics, **me_flags}, me_reasons, me_score, str(phase_info.get("detail", "") or ""), "fmp_mover"))
                _add_candidate(
                    candidates,
                    sym,
                    74 + me_score * 0.36,
                    "micro_explosion_capture_v2r",
                    "Micro Explosion Capture V2R1 من FMP: " + "، ".join(me_reasons[:3]),
                    {**me_metrics, **me_flags, "micro_explosion_capture_reasons": me_reasons},
                )
                if record_detection is not None:
                    try:
                        record_detection(sym, price=price, change_pct=change_pct, source_reason="Micro Explosion Capture V2R1 من FMP movers", source_layer="micro_explosion_capture_v2r1", source_tags=["fmp_movers", "micro_explosion_capture_v2r1", "sticky_close_watch"], move_stage="High-Risk Early Explosion Watch", early_or_late_detection="early")
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            bx_metrics = _big_explosion_live_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_mover")
            bx_ok, bx_score, bx_reasons, bx_flags = _big_explosion_live_lane_score(sym, bx_metrics, phase_detail=str(phase_info.get("detail", "") or ""), source_kind="fmp_mover")
            if bx_ok:
                big_explosion_live_count += 1
                big_explosion_live_symbols.append(sym)
                _add_candidate(candidates, sym, 92 + bx_score * 0.38, "big_explosion_live_lane_v2t", "Big Explosion V2T من FMP movers: " + "، ".join(bx_reasons[:3]), {**bx_metrics, **bx_flags})
                if record_detection is not None:
                    try:
                        stage = "Big Explosion Active" if change_pct >= 50 else "Explosion Active" if change_pct >= 20 else "Early Acceleration Watch"
                        record_detection(sym, price=price, change_pct=change_pct, source_reason="Big Explosion Live Lane V2T من FMP movers", source_layer="big_explosion_live_lane_v2t", source_tags=["fmp_movers", "big_explosion_live_lane_v2t", "monitoring_only"], move_stage=stage, early_or_late_detection="timing_watch")
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            bx_metrics = _big_explosion_live_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_live")
            bx_ok, bx_score, bx_reasons, bx_flags = _big_explosion_live_lane_score(sym, bx_metrics, phase_detail=str(phase_info.get("detail", "") or ""), source_kind="fmp_live")
            if bx_ok:
                big_explosion_live_count += 1
                big_explosion_live_symbols.append(sym)
                _add_candidate(candidates, sym, 96 + bx_score * 0.40, "big_explosion_live_lane_v2t", "Big Explosion V2T من FMP live: " + "، ".join(bx_reasons[:3]), {**bx_metrics, **bx_flags})
                if record_detection is not None:
                    try:
                        stage = "Big Explosion Active" if change_pct >= 50 else "Explosion Active" if change_pct >= 20 else "Early Acceleration Watch"
                        record_detection(sym, price=price, change_pct=change_pct, source_reason="Big Explosion Live Lane V2T من FMP live", source_layer="big_explosion_live_lane_v2t", source_tags=["fmp_live_confirmed", "big_explosion_live_lane_v2t", "monitoring_only"], move_stage=stage, early_or_late_detection="timing_watch")
                    except Exception:
                        pass
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
    # V2R1: live-confirm sticky micro watch + user-provided micro/China seeds even
    # when they did not rank high enough in old source buckets.  This is the key
    # change: do not wait for them to enter Watch first.
    seed_confirm_symbols = [s for s in sorted(MICRO_EXPLOSION_SEED_SYMBOLS)[:MICRO_EXPLOSION_SEED_CONFIRM_LIMIT]]
    memory_confirm_symbols = [str((x or {}).get("symbol") or "").upper() for x in (micro_watch_rows or [])[:MICRO_EXPLOSION_CLOSE_WATCH_LIMIT]]
    prepared_confirm_symbols = [str((x or {}).get("symbol") or "").upper() for x in (prepared_big_explosion_watch_rows or [])[:BIG_EXPLOSION_PREPARED_WATCH_LIMIT]]
    live_tight_confirm_symbols = [str((x or {}).get("symbol") or "").upper() for x in (live_tight_monitoring_memory_rows or [])[:LIVE_TIGHT_MONITORING_LIMIT]]
    ranked_confirm_symbols = [r["symbol"] for r in rows_before_confirm[:DYNAMIC_DISCOVERY_FMP_CONFIRM_LIMIT]]
    low_float_confirm_symbols = _symbols_from_candidate_rows(rows_before_confirm, {"low_float_fast_lane_v1"}, limit=500)
    micro_live_confirm_symbols = _symbols_from_candidate_rows(rows_before_confirm, {"micro_explosion_capture_v2r", "micro_explosion_capture_v2r1", "big_explosion_live_lane_v2t", "big_explosion_live_lane_v2u", "intraday_early_ramp", "high_risk_live_mover", "dip_reclaim_radar", "quiet_accumulation_radar"}, limit=500)
    emergency_confirm_symbols = _symbols_from_candidate_rows(rows_before_confirm, {"live_ignition_hot_lane", "big_explosion_live_lane_v2t", "micro_explosion_capture_v2r", "micro_explosion_capture_v2r1"}, limit=300)
    existing_confirm_pool = prepared_confirm_symbols + live_tight_confirm_symbols + ranked_confirm_symbols + memory_confirm_symbols + seed_confirm_symbols + low_float_confirm_symbols + micro_live_confirm_symbols + emergency_confirm_symbols
    rotation_confirm_symbols, rotation_discovery_debug = _rotating_discovery_symbols(reference_tickers, phase_info=phase_info, existing_symbols=existing_confirm_pool)
    fmp_confirm_symbols, live_monitoring_budget_debug = _live_monitoring_confirmation_budget(
        phase_info=phase_info,
        prepared_symbols=prepared_confirm_symbols,
        live_tight_symbols=live_tight_confirm_symbols,
        ranked_symbols=ranked_confirm_symbols,
        memory_symbols=memory_confirm_symbols,
        seed_symbols=seed_confirm_symbols,
        low_float_symbols=low_float_confirm_symbols,
        micro_live_symbols=micro_live_confirm_symbols,
        emergency_symbols=emergency_confirm_symbols,
        rotation_symbols=rotation_confirm_symbols,
        rotation_debug=rotation_discovery_debug,
    )
    # V2V6c: final phase-aware cap.  It protects Railway/FMP, but does not force
    # active-market and after-hours discovery down to the weekend 180 limit.
    try:
        hard_cap = int(((live_monitoring_budget_debug or {}).get("caps") or {}).get("total") or LIVE_MONITORING_FMP_CONFIRM_TOTAL_LIMIT or 560)
    except Exception:
        hard_cap = 560
    if len(fmp_confirm_symbols or []) > hard_cap:
        original_len = len(fmp_confirm_symbols or [])
        fmp_confirm_symbols = list(fmp_confirm_symbols or [])[:hard_cap]
        if isinstance(live_monitoring_budget_debug, dict):
            live_monitoring_budget_debug["hard_cap_applied_v2v6c"] = True
            live_monitoring_budget_debug["hard_cap"] = hard_cap
            live_monitoring_budget_debug["pre_hard_cap_count"] = original_len
            live_monitoring_budget_debug["final_count"] = len(fmp_confirm_symbols)
            live_monitoring_budget_debug["rule_ar"] = "V2V6c: تم تطبيق سقف نهائي حسب مرحلة السوق؛ 180 للعطلة فقط، وليس أثناء السوق/بعد الإغلاق."
    fmp_quotes = {}
    fmp_diag = {}
    if DYNAMIC_DISCOVERY_USE_FMP_CONFIRMATION and FMP_API_KEY and fmp_confirm_symbols:
        try:
            batch_size = max(80, min(300, int(LIVE_MONITORING_FMP_BATCH_SIZE or 280)))
        except Exception:
            batch_size = 280
        try:
            max_batches = max(1, min(4, int(LIVE_MONITORING_MAX_FMP_BATCHES or 3)))
        except Exception:
            max_batches = 3
        phase_name = str((phase_info or {}).get("phase", "") or "")
        phase_detail_name = str((phase_info or {}).get("detail", "") or "")
        # Polygon fallback is useful for after-hours/closed preparation only. It
        # remains delayed and never means execution. During live open/premarket,
        # use FMP-only for actionable freshness.
        polygon_fallback_allowed = bool(phase_name in {"after_hours", "closed"} and phase_detail_name != "weekend")
        max_fetch = int(batch_size * max_batches)
        fetch_symbols = list(fmp_confirm_symbols or [])[:max_fetch]
        if len(fetch_symbols) < len(fmp_confirm_symbols or []) and isinstance(live_monitoring_budget_debug, dict):
            live_monitoring_budget_debug["batch_capacity_cap_applied_v2v6c"] = True
            live_monitoring_budget_debug["batch_capacity"] = max_fetch
            live_monitoring_budget_debug["pre_batch_capacity_count"] = len(fmp_confirm_symbols or [])
            live_monitoring_budget_debug["final_count"] = len(fetch_symbols)
        fmp_confirm_symbols = fetch_symbols
        all_diags = []
        try:
            for i in range(0, len(fmp_confirm_symbols), batch_size):
                chunk = list(fmp_confirm_symbols[i:i + batch_size])
                if not chunk:
                    continue
                bundle = get_live_quotes(chunk, prefer_cache=False, allow_fallback=polygon_fallback_allowed)
                if isinstance(bundle, dict):
                    fmp_quotes.update(bundle.get("quotes", {}) or {})
                    all_diags.append(bundle.get("diagnostics", {}) or {})
            fetched_total = sum(int((d or {}).get("fetched", 0) or 0) for d in all_diags)
            cache_used_total = sum(int((d or {}).get("cache_used", 0) or 0) for d in all_diags)
            fmp_diag = {
                "chunked_v2v6c": True,
                "batch_size": batch_size,
                "max_batches": max_batches,
                "batch_count": len(all_diags),
                "requested": len(fmp_confirm_symbols),
                "fetched": fetched_total,
                "cache_used": cache_used_total,
                "polygon_fallback_allowed_for_after_hours_only": polygon_fallback_allowed,
                "sources": sorted({str((d or {}).get("source") or "none") for d in all_diags}),
            }
            if isinstance(live_monitoring_budget_debug, dict):
                live_monitoring_budget_debug["quote_fetch"] = dict(fmp_diag)
        except Exception as exc:
            fmp_diag = {"error": f"{type(exc).__name__}: {str(exc)[:100]}", "chunked_v2v6c": True}

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
        try:
            me_metrics = _micro_explosion_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_live")
            me_ok, me_score, me_reasons, me_flags = _micro_explosion_capture_score(sym, me_metrics, phase_detail=str(phase_info.get("detail", "") or ""), source_kind="fmp_live")
            if me_ok:
                micro_explosion_capture_count += 1
                micro_explosion_capture_symbols.append(sym)
                micro_explosion_memory_items.append(_micro_candidate_memory_item(sym, {**me_metrics, **me_flags}, me_reasons, me_score, str(phase_info.get("detail", "") or ""), "fmp_live"))
                _add_candidate(
                    candidates,
                    sym,
                    66 + me_score * 0.32,
                    "micro_explosion_capture_v2r",
                    "Micro Explosion Capture V2R1 من FMP live: " + "، ".join(me_reasons[:3]),
                    {**me_metrics, **me_flags, "micro_explosion_capture_reasons": me_reasons},
                )
                if record_detection is not None:
                    try:
                        record_detection(sym, price=price, change_pct=change_pct, source_reason="Micro Explosion Capture V2R1 من FMP live", source_layer="micro_explosion_capture_v2r1", source_tags=["fmp_live_confirmed", "micro_explosion_capture_v2r1", "sticky_close_watch"], move_stage="High-Risk Early Explosion Watch", early_or_late_detection="early")
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            bx_metrics = _big_explosion_live_metrics_from_price_change_volume(price, change_pct, volume, source_kind="fmp_live")
            bx_ok, bx_score, bx_reasons, bx_flags = _big_explosion_live_lane_score(sym, bx_metrics, phase_detail=str(phase_info.get("detail", "") or ""), source_kind="fmp_live")
            if bx_ok:
                big_explosion_live_count += 1
                big_explosion_live_symbols.append(sym)
                opening_flag = bool(BIG_EXPLOSION_OPENING_INSTANT_ENABLED and str(phase_info.get("detail", "") or "").startswith("regular"))
                _add_candidate(candidates, sym, 110 + bx_score * 0.44, "big_explosion_live_lane_v2u", "V2U التقاط حي سريع للانفجار/الافتتاح: " + "، ".join(bx_reasons[:3]), {**bx_metrics, **bx_flags, "opening_instant_watch_v2u": opening_flag})
                if record_detection is not None:
                    try:
                        stage = "Opening Explosion Watch" if opening_flag else ("Big Explosion Active" if change_pct >= 50 else "Explosion Active" if change_pct >= 20 else "Early Acceleration Watch")
                        record_detection(sym, price=price, change_pct=change_pct, source_reason="Big Explosion Live Lane V2U from FMP live confirmation", source_layer="big_explosion_live_lane_v2u", source_tags=["fmp_live_confirmed", "big_explosion_live_lane_v2u", "monitoring_only"], move_stage=stage, early_or_late_detection="early_or_opening_watch")
                    except Exception:
                        pass
        except Exception:
            pass
        if change_pct >= 4.0:
            _add_candidate(candidates, sym, 14, "live_mover", "الحركة الحية مستمرة")
        try:
            prepared_flag = sym in set(prepared_confirm_symbols or [])
        except Exception:
            prepared_flag = False
        try:
            v2v_profile = _live_tight_profile_from_quote(sym, quote or {}, prepared=bool(prepared_flag), from_memory=sym in set(live_tight_confirm_symbols or []))
            if v2v_profile.get("eligible"):
                live_tight_monitoring_symbols.append(sym)
                live_tight_monitoring_by_symbol[sym] = dict(v2v_profile)
                live_tight_monitoring_items.append(_live_tight_memory_item(sym, v2v_profile, source="fmp_live_confirmation"))
                _add_candidate(
                    candidates,
                    sym,
                    180 + float(v2v_profile.get("score", 0) or 0) * 0.42,
                    "live_tight_monitoring_v2v",
                    "V2V مراقبة لصيقة/تأكيد سريع: " + "، ".join(list(v2v_profile.get("reasons") or [])[:2]),
                    {
                        "live_tight_monitoring_v2v": True,
                        "live_tight_stage_v2v": v2v_profile.get("stage"),
                        "live_tight_stage_ar_v2v": v2v_profile.get("stage_ar"),
                        "live_tight_prepared_symbol_v2v": bool(v2v_profile.get("prepared_watch_symbol")),
                        "live_tight_new_intraday_symbol_v2v": bool(v2v_profile.get("new_intraday_symbol")),
                        "live_tight_score_v2v": v2v_profile.get("score"),
                        "live_tight_reasons_ar_v2v": list(v2v_profile.get("reasons") or [])[:8],
                        "live_price": price,
                        "live_change_pct": change_pct,
                        "live_volume": volume,
                        "live_dollar_volume": dollar_volume,
                    },
                )
                if record_detection is not None:
                    try:
                        record_detection(sym, price=price, change_pct=change_pct, source_reason="V2V Live Tight Monitoring fast confirmation", source_layer="live_tight_monitoring_v2v", source_tags=["fmp_live_confirmed", "live_tight_monitoring_v2v", "fast_promotion_watch"], move_stage="Live Early Confirmation", early_or_late_detection="early")
                    except Exception:
                        pass
        except Exception:
            pass
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

    try:
        memory_update_debug = _update_micro_explosion_watch_memory(micro_explosion_memory_items, str(phase_info.get("detail", "") or ""))
        if isinstance(micro_explosion_watch_memory_debug, dict):
            micro_explosion_watch_memory_debug.update({"update": memory_update_debug})
        else:
            micro_explosion_watch_memory_debug = {"update": memory_update_debug}
    except Exception as exc:
        micro_explosion_watch_memory_debug = {"ok": False, "update_error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    try:
        live_tight_update = _save_live_tight_monitoring_memory(live_tight_monitoring_items, source="dynamic_discovery_v2v_fmp_live") if live_tight_monitoring_items else {"saved": False, "new_items": 0}
        if isinstance(live_tight_monitoring_memory_debug, dict):
            live_tight_monitoring_memory_debug.update({"update": live_tight_update, "new_items": len(live_tight_monitoring_items or [])})
        else:
            live_tight_monitoring_memory_debug = {"update": live_tight_update, "new_items": len(live_tight_monitoring_items or [])}
    except Exception as exc:
        live_tight_monitoring_memory_debug = {"ok": False, "update_error": f"{type(exc).__name__}: {str(exc)[:120]}"}

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
    selected_order += from_source("live_tight_monitoring_v2v", min(LIVE_TIGHT_MONITORING_LIMIT, max_symbols))
    selected_order += from_source("big_explosion_prepared_watch_v2u", min(BIG_EXPLOSION_PREPARED_WATCH_LIMIT, max_symbols))
    selected_order += from_source("big_explosion_live_lane_v2u", min(120, max_symbols))
    selected_order += from_source("big_explosion_live_lane_v2t", min(90, max_symbols))
    # Official launch source order: early/prepared lanes first, then live ignition,
    # then constructive liquidity.  Late movers stay visible for review, but they
    # must never crowd out early builders or weekly-priority names.
    selected_order += from_source("micro_explosion_capture_v2r1", min(MICRO_EXPLOSION_CAPTURE_INJECT_LIMIT, max_symbols))
    selected_order += from_source("micro_explosion_capture_v2r", min(MICRO_EXPLOSION_CAPTURE_INJECT_LIMIT, max_symbols))
    selected_order += from_source("micro_explosion_close_watch_v2r1", min(MICRO_EXPLOSION_CLOSE_WATCH_LIMIT, max_symbols))
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
    micro_explosion_capture_debug = _micro_explosion_debug_payload(ranked, final, max_symbols)

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
        "engine_version": "dynamic_discovery_v3m_v2v6c_dynamic_rotation_discovery_2026_06_21",
        "dynamic_discovery_enabled": True,
        "dynamic_discovery_mode": "real_pre_explosion_capture_v2v6c_dynamic_rotation_discovery_fast_promotion",
        "requested_target": int(max_symbols),
        "target": int(max_symbols),
        "selected_count": len(final),
        "market_date": market_date,
        "source_mode": source_mode,
        "grouped_recovery_debug": grouped_recovery_debug,
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
        "live_monitoring_budget_guard_v2v6": live_monitoring_budget_debug if 'live_monitoring_budget_debug' in locals() else {},
        "fmp_confirmed": live_confirmed,
        "fmp_extended_confirmed": extended_confirmed,
        "fmp_confirm_batches": (fmp_diag or {}).get("batch_count") if isinstance(fmp_diag, dict) else None,
        "fmp_confirm_batch_size": (fmp_diag or {}).get("batch_size") if isinstance(fmp_diag, dict) else None,
        "rotating_discovery_v2v6c": rotation_discovery_debug if 'rotation_discovery_debug' in locals() else {},
        "low_float_confirm_count_v2v6c": len(low_float_confirm_symbols) if 'low_float_confirm_symbols' in locals() else 0,
        "micro_live_confirm_count_v2v6c": len(micro_live_confirm_symbols) if 'micro_live_confirm_symbols' in locals() else 0,
        "emergency_confirm_count_v2v6c": len(emergency_confirm_symbols) if 'emergency_confirm_symbols' in locals() else 0,
        "rotation_confirm_count_v2v6c": len(rotation_confirm_symbols) if 'rotation_confirm_symbols' in locals() else 0,
        "low_float_fast_lane_count": int(source_bucket_counts.get("low_float_fast_lane_v1", 0)) if 'source_bucket_counts' in locals() else int(low_float_fast_lane_count or 0),
        "low_float_fast_lane": low_float_fast_lane_status,
        "low_float_fast_lane_funnel_debug": low_float_fast_lane_funnel_debug,
        "micro_explosion_capture_count": int((source_bucket_counts.get("micro_explosion_capture_v2r1", 0) or 0) + (source_bucket_counts.get("micro_explosion_capture_v2r", 0) or 0)) if 'source_bucket_counts' in locals() else int(micro_explosion_capture_count or 0),
        "micro_explosion_capture_v2r1_count": int(source_bucket_counts.get("micro_explosion_capture_v2r1", 0) or 0) if 'source_bucket_counts' in locals() else 0,
        "micro_explosion_capture_symbols": _scanner.unique_keep_order(micro_explosion_capture_symbols)[:120],
        "micro_explosion_capture_debug": micro_explosion_capture_debug,
        "micro_explosion_full_market_scan": micro_explosion_full_market_status,
        "micro_explosion_close_watch_count": int(source_bucket_counts.get("micro_explosion_close_watch_v2r1", 0)) if 'source_bucket_counts' in locals() else 0,
        "micro_explosion_close_watch_memory": micro_explosion_watch_memory_debug,
        "micro_explosion_seed_confirm_count": len(seed_confirm_symbols) if 'seed_confirm_symbols' in locals() else 0,
        "big_explosion_live_count": int((source_bucket_counts.get("big_explosion_live_lane_v2u", 0) or 0) + (source_bucket_counts.get("big_explosion_live_lane_v2t", 0) or 0) or big_explosion_live_count) if 'source_bucket_counts' in locals() else int(big_explosion_live_count or 0),
        "big_explosion_live_v2u_count": int(source_bucket_counts.get("big_explosion_live_lane_v2u", 0) or 0) if 'source_bucket_counts' in locals() else 0,
        "big_explosion_prepared_watch_count": int(source_bucket_counts.get("big_explosion_prepared_watch_v2u", 0) or 0) if 'source_bucket_counts' in locals() else len(prepared_big_explosion_watch_rows or []),
        "big_explosion_prepared_watch_debug": prepared_big_explosion_watch_debug,
        "big_explosion_live_symbols": _scanner.unique_keep_order(big_explosion_live_symbols)[:160],
        "big_explosion_live_debug": big_explosion_live_debug,
        "live_tight_monitoring_v2v_count": int(source_bucket_counts.get("live_tight_monitoring_v2v", 0) or len(live_tight_monitoring_symbols or [])) if 'source_bucket_counts' in locals() else int(len(live_tight_monitoring_symbols or [])),
        "live_tight_monitoring_v2v_symbols": _scanner.unique_keep_order(live_tight_monitoring_symbols)[:120],
        "live_tight_monitoring_v2v_by_symbol": {k: v for k, v in (live_tight_monitoring_by_symbol or {}).items()},
        "live_tight_monitoring_v2v_memory": live_tight_monitoring_memory_debug,
        "live_tight_monitoring_v2v_rule_ar": "V2V6c: Prepared/V2V/Low-Float/Fast Lane محمية، والأسهم الجديدة تدخل عبر rotating discovery ثم FMP/Polygon after-hours confirmation. لا يفتح شراء مباشر ولا يتجاوز الشرعية.",
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
                    if k in {"price", "day_change_pct", "dollar_volume", "volume", "live_price", "live_change_pct", "live_volume", "fmp_price", "fmp_change_pct", "fmp_volume", "near_high", "close_strength", "range_pct", "intraday_early_source_lane", "intraday_early_source_score", "change_pct", "dollar_volume_pace", "reclaimed_open", "dip_depth_pct", "reclaim_from_low_pct", "low_float_fast_lane", "low_float_fast_lane_score", "low_float_fast_lane_source_kind", "low_float_fast_lane_v2p", "micro_explosion_capture_v2r", "micro_explosion_capture_score", "micro_explosion_source_kind", "micro_explosion_reasons_ar", "micro_explosion_blockers_ar", "micro_explosion_first_ignition", "micro_explosion_strong_candle", "micro_explosion_quiet_accumulation", "micro_explosion_capture_v2r1", "micro_explosion_seed_match", "micro_explosion_reference_fallback_v2r2", "big_explosion_live_lane_v2t", "big_explosion_live_score", "big_explosion_live_eligible", "big_explosion_live_source_kind", "big_explosion_live_reasons_ar", "big_explosion_live_blockers_ar", "big_explosion_gain_pct"}
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

