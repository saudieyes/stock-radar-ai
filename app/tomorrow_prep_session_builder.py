"""FMP session-based tomorrow prep builder (V2W8).

Purpose
-------
Build a compact tomorrow watch/prep list from TODAY'S completed trading session
using FMP quote/extended data in small rotating chunks. Polygon daily/grouped data
may arrive later; this module does not wait for Polygon and does not replace the
existing Polygon builder. It feeds the existing prepared-watch memory so the app
can review candidates before premarket without changing Strong/Cautious rules.

Safety
------
- display/prep only; no BUY_NOW decisions;
- no raw data persistence;
- chunked FMP requests with per-session caps;
- stops at least two hours before 04:00 ET premarket by default;
- manual Sharia exclusions are blocked from active prep and kept only in learning.
"""
from __future__ import annotations

import csv
import json
import os
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import scanner as _scanner

from .settings import DATA_DIR, FMP_API_KEY
from .utils import safe_round, to_float
from .live_quotes import get_live_quotes
from .sqlite_store import get_json as _get_json, set_json as _set_json
from .data_store import get_manual_sharia_exclusions_map, get_manual_sharia_approvals_map

TOMORROW_PREP_SESSION_BUILDER_VERSION = "tomorrow_prep_session_builder_v2w8_fmp_session_chunks_2026_06_23"
TOMORROW_PREP_STATE_KEY = "tomorrow_prep:session_scan_state_v2w8"
TOMORROW_PREP_OUTPUT_KEY = "tomorrow_prep:session_candidates_v2w8"
TOMORROW_PREP_OUTPUT_PATH = Path(DATA_DIR) / "tomorrow_prep_session_candidates.json"
NY_TZ = ZoneInfo("America/New_York")


def _env_bool(name: str, default: bool = False) -> bool:
    text = str(os.getenv(name, "" if default is False else "true") or ("true" if default else "false")).strip().lower()
    return text in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(os.getenv(name, str(default)) or default))
    except Exception:
        v = int(default)
    if min_value is not None:
        v = max(int(min_value), v)
    if max_value is not None:
        v = min(int(max_value), v)
    return int(v)


TOMORROW_PREP_ENABLED = _env_bool("TOMORROW_PREP_ENABLED", True)
TOMORROW_PREP_WORKER_ENABLED = _env_bool("TOMORROW_PREP_WORKER_ENABLED", True)
# One FMP batch every interval. get_live_quotes may call regular + extended endpoints,
# so keep the batch conservative.
TOMORROW_PREP_BATCH_SIZE = _env_int("TOMORROW_PREP_BATCH_SIZE", 220, 60, 280)
TOMORROW_PREP_INTERVAL_SEC = _env_int("TOMORROW_PREP_INTERVAL_SEC", 900, 300, 3600)
TOMORROW_PREP_MAX_BATCHES_PER_SESSION = _env_int("TOMORROW_PREP_MAX_BATCHES_PER_SESSION", 28, 4, 40)
TOMORROW_PREP_MAX_SYMBOLS_PER_SESSION = _env_int("TOMORROW_PREP_MAX_SYMBOLS_PER_SESSION", 6000, 500, 9000)
TOMORROW_PREP_TOP_N = _env_int("TOMORROW_PREP_TOP_N", 420, 80, 800)
TOMORROW_PREP_MIN_SCORE = float(os.getenv("TOMORROW_PREP_MIN_SCORE", "22") or 22)
TOMORROW_PREP_START_MINUTE_ET = _env_int("TOMORROW_PREP_START_MINUTE_ET", 16 * 60 + 10, 16 * 60, 20 * 60)
TOMORROW_PREP_STOP_MINUTE_ET = _env_int("TOMORROW_PREP_STOP_MINUTE_ET", 2 * 60, 0, 3 * 60)
TOMORROW_PREP_PREMARKET_START_MINUTE_ET = _env_int("TOMORROW_PREP_PREMARKET_START_MINUTE_ET", 4 * 60, 4 * 60, 7 * 60)
TOMORROW_PREP_REFERENCE_LIMIT_PAGES = _env_int("TOMORROW_PREP_REFERENCE_LIMIT_PAGES", 12, 4, 20)
TOMORROW_PREP_REFERENCE_PAGE_LIMIT = _env_int("TOMORROW_PREP_REFERENCE_PAGE_LIMIT", 1000, 100, 1000)


def _now_utc_text() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _now_ny() -> datetime:
    return datetime.now(NY_TZ)


def _clean_symbol(value: Any) -> str:
    s = str(value or "").upper().strip()
    if not s:
        return ""
    if not all(ch.isalnum() or ch in {".", "-"} for ch in s):
        return ""
    # Avoid obvious non-common-stock share classes/warrants in the prep universe.
    if s.endswith(("W", "WS", "WT", "U", "R")) and len(s) > 4:
        return ""
    return s[:12]


def _previous_trading_day(d: date) -> date:
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def _session_trade_date_for_now(now: datetime | None = None) -> str:
    now = now or _now_ny()
    minutes = now.hour * 60 + now.minute
    # After close: today's session.  After midnight before the stop window: previous session.
    if minutes >= int(TOMORROW_PREP_START_MINUTE_ET):
        d = now.date()
    else:
        d = _previous_trading_day(now.date())
    while d.weekday() >= 5:
        d = _previous_trading_day(d)
    return d.isoformat()


def tomorrow_prep_window_info(now: datetime | None = None) -> dict:
    now = now or _now_ny()
    minutes = now.hour * 60 + now.minute
    weekday = now.weekday()
    after_close_window = weekday <= 4 and minutes >= int(TOMORROW_PREP_START_MINUTE_ET)
    # Tuesday-Saturday 00:00-02:00 ET belongs to the prior completed session.
    early_overnight_window = weekday in {1, 2, 3, 4, 5} and minutes < int(TOMORROW_PREP_STOP_MINUTE_ET)
    enabled_window = bool(TOMORROW_PREP_ENABLED and (after_close_window or early_overnight_window))
    review_buffer_minutes = int(TOMORROW_PREP_PREMARKET_START_MINUTE_ET) - int(TOMORROW_PREP_STOP_MINUTE_ET)
    reason = "window_open" if enabled_window else "outside_after_close_to_2am_window"
    if not TOMORROW_PREP_ENABLED:
        reason = "disabled"
    if weekday >= 5 and not early_overnight_window:
        reason = "weekend_or_no_completed_today_session"
    return {
        "enabled": bool(TOMORROW_PREP_ENABLED),
        "worker_enabled": bool(TOMORROW_PREP_WORKER_ENABLED),
        "window_open": enabled_window,
        "reason": reason,
        "now_et": now.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": _session_trade_date_for_now(now),
        "start_after_close_et": "16:10",
        "stop_scan_et": f"{int(TOMORROW_PREP_STOP_MINUTE_ET)//60:02d}:{int(TOMORROW_PREP_STOP_MINUTE_ET)%60:02d}",
        "premarket_start_et": f"{int(TOMORROW_PREP_PREMARKET_START_MINUTE_ET)//60:02d}:{int(TOMORROW_PREP_PREMARKET_START_MINUTE_ET)%60:02d}",
        "review_buffer_minutes": review_buffer_minutes,
        "rule_ar": "V2W8: يبني قائمة الغد من جلسة اليوم عبر FMP على دفعات بعد الإغلاق، ويتوقف قبل البري ماركت بساعتين على الأقل.",
    }


def _read_json_file(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json_file(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _load_state() -> dict:
    state = {}
    try:
        state = _get_json(TOMORROW_PREP_STATE_KEY, {}) or {}
    except Exception:
        state = {}
    if not isinstance(state, dict) or not state:
        state = _read_json_file(TOMORROW_PREP_OUTPUT_PATH.with_name("tomorrow_prep_session_state.json"), {})
    return state if isinstance(state, dict) else {}


def _save_state(state: dict) -> None:
    try:
        _set_json(TOMORROW_PREP_STATE_KEY, state)
    except Exception:
        pass
    _write_json_file(TOMORROW_PREP_OUTPUT_PATH.with_name("tomorrow_prep_session_state.json"), state)


def _save_output(payload: dict) -> None:
    try:
        _set_json(TOMORROW_PREP_OUTPUT_KEY, payload)
    except Exception:
        pass
    _write_json_file(TOMORROW_PREP_OUTPUT_PATH, payload)


def load_tomorrow_prep_session_candidates() -> dict:
    data = {}
    try:
        data = _get_json(TOMORROW_PREP_OUTPUT_KEY, {}) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        data = _read_json_file(TOMORROW_PREP_OUTPUT_PATH, {})
    if isinstance(data, dict) and data:
        return data
    return {"ok": False, "version": TOMORROW_PREP_SESSION_BUILDER_VERSION, "reason": "no_saved_tomorrow_prep_session_candidates", "candidates": [], "sections": {}}


def _reference_symbols() -> tuple[list[str], dict]:
    debug = {"source": "polygon_reference_tickers_cached", "fallback": ""}
    symbols: list[str] = []
    try:
        symbols = list(_scanner.get_reference_tickers(limit_pages=TOMORROW_PREP_REFERENCE_LIMIT_PAGES, page_limit=TOMORROW_PREP_REFERENCE_PAGE_LIMIT) or [])
    except Exception as exc:
        debug["reference_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    clean: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        sym = _clean_symbol(raw)
        if sym and sym not in seen:
            seen.add(sym)
            clean.append(sym)
        if len(clean) >= int(TOMORROW_PREP_MAX_SYMBOLS_PER_SESSION):
            break
    if not clean:
        # Last-resort fallback from latest visible scan, not full market, but avoids an empty prep if reference is unavailable.
        debug["fallback"] = "last_trade_scan_snapshot_symbols"
        try:
            snap = _get_json("last_trade_scan_snapshot", {}) or {}
            for key, val in (snap or {}).items():
                if isinstance(val, list):
                    for row in val:
                        if isinstance(row, dict):
                            sym = _clean_symbol(row.get("symbol"))
                            if sym and sym not in seen:
                                seen.add(sym)
                                clean.append(sym)
                if len(clean) >= 800:
                    break
        except Exception as exc:
            debug["fallback_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    debug["count"] = len(clean)
    debug["max_symbols_per_session"] = int(TOMORROW_PREP_MAX_SYMBOLS_PER_SESSION)
    return clean, debug


def _sharia_status(sym: str) -> tuple[str, str]:
    try:
        exclusions = get_manual_sharia_exclusions_map() or {}
        approvals = get_manual_sharia_approvals_map() or {}
    except Exception:
        exclusions, approvals = {}, {}
    if sym in exclusions:
        item = exclusions.get(sym) or {}
        return "blocked", str(item.get("reason") or item.get("note") or "محجوب شرعيًا من القائمة اليدوية")[:180]
    if sym in approvals:
        return "approved", "معتمد يدويًا"
    return "needs_review", "يحتاج مراجعة شرعية قبل أي شراء"


def _quote_score_candidate(sym: str, q: dict, trade_date: str) -> dict | None:
    price = to_float((q or {}).get("price"))
    if price <= 0:
        return None
    prev = to_float((q or {}).get("previous_close") or (q or {}).get("regular_session_close"))
    change_pct = to_float((q or {}).get("change_pct"))
    if not change_pct and prev > 0:
        change_pct = ((price - prev) / prev) * 100.0
    volume = to_float((q or {}).get("volume"))
    dollar_volume = price * volume if price > 0 and volume > 0 else 0.0
    source = str((q or {}).get("source") or "")
    extended = bool((q or {}).get("extended_hours")) or source.startswith("fmp_extended")

    score = 0.0
    tags: list[str] = []
    reasons: list[str] = []

    if 0.35 <= price <= 15:
        score += 28
        tags.append("small_stock_focus")
        reasons.append("سعر صغير مناسب لرادار الغد")
    elif 15 < price <= 35:
        score += 14
        tags.append("mid_small_watch")
    elif price > 150:
        score -= 16
        tags.append("high_price_deprioritized")

    if 0.5 <= change_pct <= 12:
        score += 22
        tags.append("controlled_session_strength")
        reasons.append("قوة جلسة اليوم بدون امتداد مبالغ")
    elif 12 < change_pct <= 28:
        score += 10
        tags.append("extended_pullback_watch")
        reasons.append("تحرك قوي اليوم — للمتابعة مع Pullback فقط")
    elif change_pct > 28:
        score -= 4
        tags.append("very_extended_learning")
        reasons.append("ممتد جدًا — تعلم/انتظار تهدئة")
    elif -4 <= change_pct < 0.5 and 0.35 <= price <= 20 and dollar_volume >= 150_000:
        score += 8
        tags.append("quiet_or_reclaim_candidate")
        reasons.append("هادئ/محاولة تجهيز تحتاج تأكيد قبل الافتتاح")

    if 150_000 <= dollar_volume <= 75_000_000:
        score += 22
        tags.append("tradable_dollar_volume")
        reasons.append("دولار فوليوم مناسب للمراقبة")
    elif 40_000 <= dollar_volume < 150_000:
        score += 8
        tags.append("thin_watch_only")
        reasons.append("سيولة خفيفة — مراقبة فقط")
    elif dollar_volume > 180_000_000:
        score -= 5
        tags.append("institutional_liquidity_deprioritized")

    if volume >= 100_000:
        score += 8
    elif volume <= 0:
        score -= 6

    if extended:
        score += 5
        tags.append("extended_price_seen")
        reasons.append("له سعر خارج السوق من FMP Extended")

    if 0.35 <= price <= 12 and dollar_volume >= 120_000 and -2 <= change_pct <= 18:
        score += 18
        tags.append("low_float_proxy")
        reasons.append("مرشح Low-Float proxy للغد من السعر/السيولة/حركة اليوم")

    if 0.35 <= price <= 8 and 4 <= change_pct <= 18 and dollar_volume >= 200_000:
        score += 14
        tags.append("pre_explosion_candidate")
        reasons.append("احتمال تجهيز مبكر قبل الافتتاح — يحتاج تأكيد حي")

    if 0.35 <= price <= 20 and -2 <= change_pct <= 6 and dollar_volume >= 300_000:
        score += 8
        tags.append("pre_trigger_candidate")

    sh, sh_note = _sharia_status(sym)
    if sh == "blocked":
        score -= 1000
        tags.append("blocked_learning_only")
    if not reasons:
        reasons.append("مرشح فحص الغد من FMP يحتاج تأكيد قبل الافتتاح")

    metrics = {
        "price": safe_round(price, 4),
        "close": safe_round(prev if prev > 0 else price, 4),
        "volume": safe_round(volume, 0),
        "dollar_volume": safe_round(dollar_volume, 0),
        "change_pct": safe_round(change_pct, 2),
        "day_change_pct": safe_round(change_pct, 2),
        "prior_session_phase": "after_close_fmp_session_scan",
        "prior_session_source": "fmp_quote_extended_chunked",
        "big_explosion_prepared_score": safe_round(score, 3),
        "big_explosion_prepared_watch_v2u": True,
        "big_explosion_prepared_reasons_ar": reasons[:8],
        "urgent_sharia_review_v2u": sh != "approved",
        "source_note": "V2W8 FMP session-based tomorrow prep; Polygon later if available",
        "prepared_bucket": "low_float_proxy" if "low_float_proxy" in tags else ("pre_trigger" if "pre_trigger_candidate" in tags else "tomorrow_session_watch"),
        "prepared_bucket_ar": "مرشح Low-Float للغد" if "low_float_proxy" in tags else "مرشح تجهيز للغد من جلسة اليوم",
        "watch_priority_v2u3": safe_round(score, 3),
        "pre_explosion_candidate_v2u3": "pre_explosion_candidate" in tags,
        "after_hours_pressure_v2u3": bool(extended),
    }
    return {
        "symbol": sym,
        "score": safe_round(score, 3),
        "trade_date": trade_date,
        "price": safe_round(price, 4),
        "change_pct": safe_round(change_pct, 2),
        "volume": safe_round(volume, 0),
        "dollar_volume": safe_round(dollar_volume, 0),
        "source": "fmp_session_chunked",
        "quote_source": source,
        "extended_price_seen": extended,
        "sharia_status": sh,
        "sharia_note": sh_note,
        "actionability": "learning_only" if sh == "blocked" else "watch_only",
        "tags": tags[:14],
        "reasons": reasons[:8],
        "reasons_ar": reasons[:8],
        "metrics": metrics,
        "updated_at_utc": _now_utc_text(),
        "rule_ar": "V2W8: تحضير للغد من تداولات اليوم عبر FMP، وليس شراء مباشر.",
    }


def _merge_candidates(old: list[dict], new: list[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for item in list(old or []) + list(new or []):
        if not isinstance(item, dict):
            continue
        sym = _clean_symbol(item.get("symbol"))
        if not sym:
            continue
        prev = by.get(sym)
        if prev is None or float(item.get("score") or -9999) > float(prev.get("score") or -9999):
            by[sym] = item
    return sorted(by.values(), key=lambda x: float((x or {}).get("score") or 0), reverse=True)[: max(int(TOMORROW_PREP_TOP_N), 80)]


def _build_output_from_state(state: dict, reference_debug: dict | None = None) -> dict:
    candidates = list((state or {}).get("candidates") or [])
    active = [x for x in candidates if (x or {}).get("sharia_status") != "blocked" and float((x or {}).get("score") or 0) >= float(TOMORROW_PREP_MIN_SCORE)]
    blocked = [x for x in candidates if (x or {}).get("sharia_status") == "blocked"]
    low_float = [x for x in active if "low_float_proxy" in (x.get("tags") or [])]
    pre_trigger = [x for x in active if "pre_trigger_candidate" in (x.get("tags") or [])]
    extended = [x for x in active if bool(x.get("extended_price_seen"))]
    payload = {
        "ok": bool(active),
        "version": TOMORROW_PREP_SESSION_BUILDER_VERSION,
        "trade_date": (state or {}).get("trade_date", ""),
        "status": (state or {}).get("status", "in_progress"),
        "started_at_utc": (state or {}).get("started_at_utc", ""),
        "updated_at_utc": (state or {}).get("updated_at_utc", ""),
        "completed_at_utc": (state or {}).get("completed_at_utc", ""),
        "progress": {
            "cursor": int((state or {}).get("cursor", 0) or 0),
            "universe_count": int((state or {}).get("universe_count", 0) or 0),
            "batches_done": int((state or {}).get("batches_done", 0) or 0),
            "max_batches_per_session": int(TOMORROW_PREP_MAX_BATCHES_PER_SESSION),
            "batch_size": int(TOMORROW_PREP_BATCH_SIZE),
            "coverage_pct": safe_round((int((state or {}).get("cursor", 0) or 0) / max(1, int((state or {}).get("universe_count", 0) or 0))) * 100.0, 1),
        },
        "counts": {
            "selected_total": len(active),
            "low_float_proxy": len(low_float),
            "pre_trigger": len(pre_trigger),
            "extended_price_seen": len(extended),
            "blocked_learning_only": len(blocked),
        },
        "candidates": active[: int(TOMORROW_PREP_TOP_N)],
        "sections": {
            "low_float_proxy": low_float[:160],
            "pre_trigger": pre_trigger[:160],
            "extended_price_seen": extended[:160],
            "needs_sharia_review": [x for x in active if x.get("sharia_status") == "needs_review"][:220],
            "clean_approved": [x for x in active if x.get("sharia_status") == "approved"][:220],
            "learning_only_sharia_blocked": blocked[:120],
        },
        "reference_debug": reference_debug or (state or {}).get("reference_debug", {}),
        "last_run_debug": (state or {}).get("last_run_debug", {}),
        "rule_ar": "V2W8: هذه قائمة تجهيز للغد مبنية من تداولات اليوم عبر FMP على دفعات. لا تفتح شراء مباشر ولا تستبدل Strong/Cautious. Polygon يلحق لاحقًا كتعزيز عندما يتاح.",
    }
    return payload


def _save_prepared_watch_from_payload(payload: dict) -> dict:
    try:
        from .source_discovery import save_prepared_big_explosion_watch
        active = list((payload or {}).get("candidates") or [])
        # Do not send blocked symbols into active prepared watch.
        active = [x for x in active if (x or {}).get("sharia_status") != "blocked"][: int(TOMORROW_PREP_TOP_N)]
        return save_prepared_big_explosion_watch(
            active,
            trade_date=str((payload or {}).get("trade_date") or ""),
            source="tomorrow_prep_fmp_session_builder_v2w8",
            debug={
                "version": TOMORROW_PREP_SESSION_BUILDER_VERSION,
                "payload_counts": (payload or {}).get("counts", {}),
                "progress": (payload or {}).get("progress", {}),
                "rule_ar": "تم حقن قائمة الغد التحضيرية في Prepared Watch كمراقبة فقط؛ لا تغير قرارات الشراء.",
            },
        )
    except Exception as exc:
        return {"saved": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def run_tomorrow_prep_session_chunk(*, execute: bool = True, max_batches: int = 1, force_reset: bool = False, respect_window: bool = True) -> dict:
    window = tomorrow_prep_window_info()
    if respect_window and not window.get("window_open"):
        saved = load_tomorrow_prep_session_candidates()
        return {
            "ok": bool(saved.get("ok")),
            "version": TOMORROW_PREP_SESSION_BUILDER_VERSION,
            "ran": False,
            "reason": window.get("reason"),
            "window": window,
            "saved": saved,
            "rule_ar": "خارج نافذة بناء الغد؛ لا يتم استهلاك FMP.",
        }
    if not FMP_API_KEY:
        return {"ok": False, "version": TOMORROW_PREP_SESSION_BUILDER_VERSION, "ran": False, "reason": "FMP_API_KEY missing", "window": window}

    trade_date = str(window.get("trade_date") or _session_trade_date_for_now())
    state = _load_state()
    if force_reset or state.get("trade_date") != trade_date:
        state = {
            "version": TOMORROW_PREP_SESSION_BUILDER_VERSION,
            "trade_date": trade_date,
            "status": "in_progress",
            "started_at_utc": _now_utc_text(),
            "updated_at_utc": _now_utc_text(),
            "cursor": 0,
            "batches_done": 0,
            "candidates": [],
            "symbols_scanned": 0,
        }

    reference, ref_debug = _reference_symbols()
    universe_count = len(reference)
    state["universe_count"] = universe_count
    state["reference_debug"] = ref_debug
    if universe_count <= 0:
        state["last_run_debug"] = {"ok": False, "reason": "empty_reference_universe", "reference_debug": ref_debug}
        _save_state(state)
        payload = _build_output_from_state(state, ref_debug)
        _save_output(payload)
        return {"ok": False, "version": TOMORROW_PREP_SESSION_BUILDER_VERSION, "ran": False, "reason": "empty_reference_universe", "window": window, "state": state, "payload": payload}

    try:
        max_batches = max(1, min(int(max_batches or 1), 6))
    except Exception:
        max_batches = 1

    batches_run = 0
    all_new: list[dict] = []
    quote_debug: list[dict] = []
    for _ in range(max_batches):
        if int(state.get("batches_done", 0) or 0) >= int(TOMORROW_PREP_MAX_BATCHES_PER_SESSION):
            state["status"] = "completed_budget_cap"
            break
        cursor = int(state.get("cursor", 0) or 0)
        if cursor >= universe_count:
            state["status"] = "completed_full_coverage"
            break
        chunk = reference[cursor: cursor + int(TOMORROW_PREP_BATCH_SIZE)]
        if not chunk:
            state["status"] = "completed_full_coverage"
            break
        bundle = get_live_quotes(chunk, prefer_cache=False, allow_fallback=True)
        quotes = (bundle or {}).get("quotes", {}) if isinstance(bundle, dict) else {}
        new_items: list[dict] = []
        for sym in chunk:
            q = (quotes or {}).get(sym)
            item = _quote_score_candidate(sym, q or {}, trade_date=trade_date) if q else None
            if item and (float(item.get("score") or 0) >= float(TOMORROW_PREP_MIN_SCORE) or item.get("sharia_status") == "blocked"):
                new_items.append(item)
        all_new.extend(new_items)
        quote_debug.append({
            "cursor_start": cursor,
            "requested": len(chunk),
            "quotes_available": len(quotes or {}),
            "selected_from_chunk": len([x for x in new_items if x.get("sharia_status") != "blocked"]),
            "diagnostics": (bundle or {}).get("diagnostics", {}) if isinstance(bundle, dict) else {},
        })
        state["cursor"] = cursor + len(chunk)
        state["batches_done"] = int(state.get("batches_done", 0) or 0) + 1
        state["symbols_scanned"] = int(state.get("symbols_scanned", 0) or 0) + len(chunk)
        batches_run += 1
        # Do not hammer FMP when the endpoint is manually asked to run multiple batches.
        if max_batches > 1:
            time.sleep(0.25)

    state["candidates"] = _merge_candidates(list(state.get("candidates") or []), all_new)
    state["updated_at_utc"] = _now_utc_text()
    if int(state.get("cursor", 0) or 0) >= universe_count:
        state["status"] = "completed_full_coverage"
        state["completed_at_utc"] = _now_utc_text()
    elif int(state.get("batches_done", 0) or 0) >= int(TOMORROW_PREP_MAX_BATCHES_PER_SESSION):
        state["status"] = "completed_budget_cap"
        state["completed_at_utc"] = _now_utc_text()
    else:
        state["status"] = "in_progress"
    state["last_run_debug"] = {
        "ok": True,
        "batches_run": batches_run,
        "new_candidates": len([x for x in all_new if x.get("sharia_status") != "blocked"]),
        "quote_debug": quote_debug,
        "min_score": float(TOMORROW_PREP_MIN_SCORE),
        "rule_ar": "كل تشغيل يطلب دفعة واحدة/محدودة من FMP ثم يحفظ قائمة مضغوطة للغد.",
    }
    _save_state(state)
    payload = _build_output_from_state(state, ref_debug)
    if execute:
        _save_output(payload)
        payload["prepared_watch_save"] = _save_prepared_watch_from_payload(payload)
        _save_output(payload)
    return {
        "ok": bool(payload.get("ok")),
        "version": TOMORROW_PREP_SESSION_BUILDER_VERSION,
        "ran": True,
        "execute": bool(execute),
        "window": window,
        "batches_run": batches_run,
        "payload": payload,
        "state": {k: v for k, v in state.items() if k != "candidates"},
    }


def tomorrow_prep_session_status() -> dict:
    state = _load_state()
    saved = load_tomorrow_prep_session_candidates()
    window = tomorrow_prep_window_info()
    next_due = None
    try:
        last_ts = 0.0
        raw = str((state or {}).get("updated_at_utc") or "").replace("Z", "+00:00")
        if raw:
            dt = datetime.fromisoformat(raw)
            last_ts = dt.timestamp()
        next_due = max(0, int(float(TOMORROW_PREP_INTERVAL_SEC) - (time.time() - last_ts))) if last_ts > 0 else 0
    except Exception:
        next_due = None
    return {
        "ok": True,
        "version": TOMORROW_PREP_SESSION_BUILDER_VERSION,
        "enabled": bool(TOMORROW_PREP_ENABLED),
        "worker_enabled": bool(TOMORROW_PREP_WORKER_ENABLED),
        "window": window,
        "interval_sec": int(TOMORROW_PREP_INTERVAL_SEC),
        "batch_size": int(TOMORROW_PREP_BATCH_SIZE),
        "max_batches_per_session": int(TOMORROW_PREP_MAX_BATCHES_PER_SESSION),
        "max_symbols_per_session": int(TOMORROW_PREP_MAX_SYMBOLS_PER_SESSION),
        "next_worker_run_due_in_sec": next_due,
        "state": {k: v for k, v in (state or {}).items() if k != "candidates"},
        "saved_counts": (saved or {}).get("counts", {}),
        "saved_progress": (saved or {}).get("progress", {}),
        "saved_status": (saved or {}).get("status", ""),
        "saved_trade_date": (saved or {}).get("trade_date", ""),
        "saved_candidate_count": len((saved or {}).get("candidates") or []) if isinstance(saved, dict) else 0,
        "saved_sample": [x.get("symbol") for x in list((saved or {}).get("candidates") or [])[:30] if isinstance(x, dict)] if isinstance(saved, dict) else [],
        "rule_ar": "V2W8 يبني قائمة الغد من FMP/جلسة اليوم على دفعات، ويحقنها كـ Prepared Watch للمراجعة قبل البري ماركت. Polygon يعزز لاحقًا فقط عند توفر بيانات اليوم.",
    }


def format_tomorrow_prep_session_brief(data: dict) -> str:
    if not isinstance(data, dict):
        return "V2W8 — Tomorrow Prep\nلا توجد بيانات."
    saved = data.get("payload") if isinstance(data.get("payload"), dict) else data
    if data.get("saved") and isinstance(data.get("saved"), dict):
        saved = data.get("saved")
    counts = (saved or {}).get("counts", {}) or {}
    progress = (saved or {}).get("progress", {}) or {}
    lines = [
        "V2W8 — Tomorrow Prep من تداولات اليوم عبر FMP",
        f"تاريخ الجلسة: {(saved or {}).get('trade_date', '-')}",
        f"الحالة: {(saved or {}).get('status', '-')}",
        f"التقدم: {progress.get('cursor', 0)}/{progress.get('universe_count', 0)} ({progress.get('coverage_pct', 0)}%) | دفعات: {progress.get('batches_done', 0)}/{progress.get('max_batches_per_session', 0)}",
        f"مرشحون: {counts.get('selected_total', 0)} | Low-Float proxy: {counts.get('low_float_proxy', 0)} | Pre-Trigger: {counts.get('pre_trigger', 0)} | Extended: {counts.get('extended_price_seen', 0)}",
        "",
        "أفضل المرشحين:",
    ]
    for item in list((saved or {}).get("candidates") or [])[:25]:
        lines.append(f"- {item.get('symbol')}: score={item.get('score')} | price={item.get('price')} | chg={item.get('change_pct')}% | sharia={item.get('sharia_status')} | {'، '.join(list(item.get('reasons_ar') or item.get('reasons') or [])[:2])}")
    lines.append("")
    lines.append("هذه قائمة تجهيز فقط. لا تغير Strong/Cautious ولا الشرعية ولا تفتح شراء مباشر.")
    return "\n".join(lines)
