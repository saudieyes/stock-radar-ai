"""FMP/Polygon live quote helpers.

Fix33/V2W5: extended-hours aware live quotes with missing-symbol refill.

Why this exists:
- FMP regular batch quote can return the regular-session/previous-close style price during
  pre-market / after-hours for some symbols.
- During premarket/afterhours we first query FMP extended-hours endpoints and only use the
  regular quote for previousClose/change baseline.
- During active market phases we intentionally avoid SQLite price cache.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from .settings import HTTP_SESSION, POLYGON_API_KEY
from .sqlite_store import get_cached_live_quotes, upsert_live_quotes
from .utils import safe_round, to_float

FMP_API_KEY = str(os.getenv("FMP_API_KEY", "") or "").strip()
FMP_BASE_URL = str(os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com") or "https://financialmodelingprep.com").rstrip("/")
LIVE_QUOTES_ENABLED = str(os.getenv("LIVE_QUOTES_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
LIVE_QUOTES_CACHE_MAX_AGE_SEC = int(float(os.getenv("LIVE_QUOTES_CACHE_MAX_AGE_SEC", "45") or 45))
LIVE_QUOTES_TIMEOUT_SEC = float(os.getenv("LIVE_QUOTES_TIMEOUT_SEC", "8") or 8)
# Safety cap for last-resort single-symbol FMP fallback calls.
# Normal path uses batch/CSV endpoints; this is only used when FMP plan/API does not return batch rows.
FMP_SINGLE_FALLBACK_LIMIT = int(float(os.getenv("FMP_SINGLE_FALLBACK_LIMIT", "60") or 60))
FMP_WEBSOCKET_ENABLED = str(os.getenv("FMP_WEBSOCKET_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
LIVE_QUOTES_EXTENDED_REFILL_VERSION = "v2w5b_extended_hours_missing_symbol_refill_route_restore_2026_06_22"

NY_TZ = ZoneInfo("America/New_York")


def _market_phase_now() -> str:
    """Return a lightweight US market phase.

    We keep this local to avoid circular imports from main.py.
    """
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    if dt_time(4, 0) <= t < dt_time(9, 30):
        return "premarket"
    if dt_time(9, 30) <= t < dt_time(16, 0):
        return "open"
    if dt_time(16, 0) <= t < dt_time(20, 0):
        return "afterhours"
    return "closed"


def _active_price_phase(phase: str | None = None) -> bool:
    phase = phase or _market_phase_now()
    return phase in {"premarket", "open", "afterhours"}


def _extended_hours_phase(phase: str | None = None) -> bool:
    phase = phase or _market_phase_now()
    return phase in {"premarket", "afterhours"}


def _clean_symbols(symbols) -> list[str]:
    out = []
    for s in symbols or []:
        t = str(s or "").upper().strip()
        if not t or t in out:
            continue
        if not all(ch.isalnum() or ch in {".", "-"} for ch in t):
            continue
        out.append(t)
    return out[:300]


def _first_number(row: dict, keys: list[str]) -> float:
    for key in keys:
        try:
            val = to_float(row.get(key))
            if val > 0:
                return float(val)
        except Exception:
            continue
    return 0.0


def _first_text(row: dict, keys: list[str]) -> str:
    for key in keys:
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _ts_to_label(raw_ts=None) -> str:
    try:
        if raw_ts is None or raw_ts == "":
            return datetime.now(NY_TZ).strftime("%H:%M:%S")
        ts = float(raw_ts)
        # FMP sometimes returns milliseconds.
        if ts > 10_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, NY_TZ).strftime("%H:%M:%S")
    except Exception:
        return datetime.now(NY_TZ).strftime("%H:%M:%S")


def _normalize_fmp_regular_row(row: dict) -> dict | None:
    try:
        symbol = _first_text(row, ["symbol", "ticker"]).upper()
        if not symbol:
            return None
        price = _first_number(row, ["price", "lastSalePrice", "last", "lp", "close"])
        prev = _first_number(row, ["previousClose", "previous_close", "prevClose", "previous_close_price"])
        change_pct = to_float(row.get("changesPercentage") or row.get("changePercentage") or row.get("change_pct"))
        # FMP stable/quote-short often returns absolute change instead of percentage.
        # Reconstruct previous close and percentage so live confirmation is useful.
        absolute_change = to_float(row.get("change") or row.get("changes"))
        if prev <= 0 and price > 0 and absolute_change:
            maybe_prev = price - absolute_change
            if maybe_prev > 0:
                prev = maybe_prev
        if not change_pct and price > 0 and prev > 0:
            change_pct = ((price - prev) / prev) * 100
        # Do not let a missing/unknown percent change overwrite a previously good UI value as 0.00%.
        # FMP quote-short may return only price/change without enough baseline for some symbols.
        change_pct_reliable = bool(
            prev > 0
            or row.get("changesPercentage") is not None
            or row.get("changePercentage") is not None
            or row.get("change_pct") is not None
            or absolute_change
        )
        volume = _first_number(row, ["volume", "avgVolume", "dayVolume"])
        if price <= 0:
            return None
        now = time.time()
        return {
            "symbol": symbol,
            "price": safe_round(price, 4),
            # previous_close is the normal day-change baseline during the regular session.
            "previous_close": safe_round(prev, 4),
            # In premarket/afterhours FMP regular quote price usually represents the last
            # regular-session close. Use it as the extended-hours baseline instead of
            # previousClose, which can be yesterday's/older baseline and inflates percent change.
            "regular_session_close": safe_round(price, 4),
            "regular_previous_close": safe_round(prev, 4),
            "change_pct": safe_round(change_pct, 2),
            "change_pct_reliable": bool(change_pct_reliable),
            "volume": safe_round(volume),
            "source": "fmp_rest",
            "source_label": "FMP Live/REST",
            "updated_at": now,
            "updated_label": datetime.now(NY_TZ).strftime("%H:%M:%S"),
            "market_phase": _market_phase_now(),
            "extended_hours": False,
        }
    except Exception:
        return None


def _normalize_fmp_extended_trade_row(row: dict, regular_quote: dict | None = None) -> dict | None:
    """Normalize FMP batch-aftermarket-trade rows.

    The exact stable payload names may vary, so this accepts multiple common key names.
    """
    try:
        symbol = _first_text(row, ["symbol", "ticker", "s"]).upper()
        if not symbol:
            return None

        price = _first_number(row, [
            "price", "lastPrice", "last", "tradePrice", "lastSalePrice", "p", "lp", "close"
        ])
        if price <= 0:
            return None

        reg = regular_quote or {}
        regular_prev = _first_number(reg, ["previous_close", "previousClose", "prevClose"])
        # Extended-hours percent should be measured from the last regular-session close
        # (for example premarket +2.4% from the last 16:00 close), not from the older
        # previousClose baseline that can inflate percent change.
        prev = _first_number(reg, ["regular_session_close", "regular_close", "close", "price"])
        if prev <= 0:
            prev = _first_number(row, ["regularSessionClose", "regular_close", "close", "previousClose", "previous_close", "prevClose"])

        change_pct = ((price - prev) / prev) * 100 if prev > 0 else 0.0
        change_pct_reliable = prev > 0
        volume = _first_number(row, ["volume", "size", "lastSize", "tradeSize", "v", "dayVolume"])
        raw_ts = row.get("timestamp") or row.get("time") or row.get("t") or row.get("lastUpdated")
        now = time.time()
        phase = _market_phase_now()
        return {
            "symbol": symbol,
            "price": safe_round(price, 4),
            # During extended-hours this is the last regular-session close baseline.
            "previous_close": safe_round(prev, 4),
            "regular_session_close": safe_round(prev, 4),
            "regular_previous_close": safe_round(regular_prev, 4),
            "previous_close_source": "regular_session_close",
            "change_pct": safe_round(change_pct, 2),
            "change_pct_reliable": bool(change_pct_reliable),
            "volume": safe_round(volume),
            "source": "fmp_extended_trade",
            "source_label": "FMP Extended/Trade",
            "updated_at": now,
            "updated_label": _ts_to_label(raw_ts),
            "market_phase": phase,
            "extended_hours": True,
            "extended_source": "trade",
        }
    except Exception:
        return None


def _normalize_fmp_extended_quote_row(row: dict, regular_quote: dict | None = None) -> dict | None:
    """Normalize FMP batch-aftermarket-quote rows.

    Prefer midpoint from bid/ask if available, otherwise any quote price field.
    """
    try:
        symbol = _first_text(row, ["symbol", "ticker", "s"]).upper()
        if not symbol:
            return None

        bid = _first_number(row, ["bidPrice", "bid", "bp"])
        ask = _first_number(row, ["askPrice", "ask", "ap"])
        if bid > 0 and ask > 0:
            price = (bid + ask) / 2.0
        else:
            price = _first_number(row, [
                "price", "lastPrice", "last", "mark", "mid", "p", "lp", "close"
            ])
        if price <= 0:
            return None

        reg = regular_quote or {}
        regular_prev = _first_number(reg, ["previous_close", "previousClose", "prevClose"])
        # Extended-hours percent should be measured from the last regular-session close
        # (for example premarket +2.4% from the last 16:00 close), not from the older
        # previousClose baseline that can inflate percent change.
        prev = _first_number(reg, ["regular_session_close", "regular_close", "close", "price"])
        if prev <= 0:
            prev = _first_number(row, ["regularSessionClose", "regular_close", "close", "previousClose", "previous_close", "prevClose"])

        change_pct = ((price - prev) / prev) * 100 if prev > 0 else 0.0
        change_pct_reliable = prev > 0
        volume = _first_number(row, ["volume", "bidSize", "askSize", "size", "v"])
        raw_ts = row.get("timestamp") or row.get("time") or row.get("t") or row.get("lastUpdated")
        now = time.time()
        phase = _market_phase_now()
        return {
            "symbol": symbol,
            "price": safe_round(price, 4),
            # During extended-hours this is the last regular-session close baseline.
            "previous_close": safe_round(prev, 4),
            "regular_session_close": safe_round(prev, 4),
            "regular_previous_close": safe_round(regular_prev, 4),
            "previous_close_source": "regular_session_close",
            "change_pct": safe_round(change_pct, 2),
            "change_pct_reliable": bool(change_pct_reliable),
            "volume": safe_round(volume),
            "source": "fmp_extended_quote",
            "source_label": "FMP Extended/Quote",
            "updated_at": now,
            "updated_label": _ts_to_label(raw_ts),
            "market_phase": phase,
            "extended_hours": True,
            "extended_source": "quote",
            "bid": safe_round(bid, 4),
            "ask": safe_round(ask, 4),
        }
    except Exception:
        return None


def _rows_from_response(resp_json):
    if isinstance(resp_json, list):
        return resp_json
    if isinstance(resp_json, dict):
        for key in ("data", "quotes", "quote", "results"):
            val = resp_json.get(key)
            if isinstance(val, list):
                return val
        # Some single-symbol endpoints return one object.
        if resp_json.get("symbol") or resp_json.get("ticker"):
            return [resp_json]
    return []


def _fetch_json_rows(url: str) -> list[dict]:
    try:
        r = HTTP_SESSION.get(url, timeout=LIVE_QUOTES_TIMEOUT_SEC)
        if r.status_code >= 400:
            return []
        data = r.json()
        return [x for x in _rows_from_response(data) if isinstance(x, dict)]
    except Exception:
        return []


def _fetch_fmp_regular_quotes(symbols: list[str]) -> dict[str, dict]:
    if not FMP_API_KEY or not symbols:
        return {}
    csv_symbols = ",".join(symbols)
    endpoints = [
        # Newer FMP stable endpoints used by the user's plan.
        f"{FMP_BASE_URL}/stable/quote?symbol={csv_symbols}&apikey={FMP_API_KEY}",
        f"{FMP_BASE_URL}/stable/quote-short?symbol={csv_symbols}&apikey={FMP_API_KEY}",
        # Older/batch fallbacks.
        f"{FMP_BASE_URL}/stable/batch-quote?symbols={csv_symbols}&apikey={FMP_API_KEY}",
        f"{FMP_BASE_URL}/api/v3/quote/{csv_symbols}?apikey={FMP_API_KEY}",
    ]
    for url in endpoints:
        rows = _fetch_json_rows(url)
        out = {}
        for row in rows:
            norm = _normalize_fmp_regular_row(row)
            if norm:
                out[norm["symbol"]] = norm
        if out:
            return out

    # Last-resort fallback for plans/endpoints that only accept one symbol per request.
    # Keep this capped so Railway/API usage does not explode during frequent UI refreshes.
    out = {}
    for sym in symbols[:max(0, FMP_SINGLE_FALLBACK_LIMIT)]:
        for url in [
            f"{FMP_BASE_URL}/stable/quote?symbol={sym}&apikey={FMP_API_KEY}",
            f"{FMP_BASE_URL}/stable/quote-short?symbol={sym}&apikey={FMP_API_KEY}",
        ]:
            rows = _fetch_json_rows(url)
            for row in rows:
                norm = _normalize_fmp_regular_row(row)
                if norm:
                    out[norm["symbol"]] = norm
                    break
            if sym in out:
                break
    return out


def _fetch_fmp_extended_quotes(symbols: list[str], regular_quotes: dict[str, dict] | None = None) -> dict[str, dict]:
    if not FMP_API_KEY or not symbols:
        return {}
    regular_quotes = regular_quotes or {}
    csv_symbols = ",".join(symbols)

    out: dict[str, dict] = {}

    # First preference: last extended trade because it is closest to a traded price.
    # The user's FMP plan returns /stable/aftermarket-trade?symbol=AAPL, while some
    # previous code expected batch-aftermarket-trade. Try both before falling back.
    trade_urls = [
        f"{FMP_BASE_URL}/stable/aftermarket-trade?symbol={csv_symbols}&apikey={FMP_API_KEY}",
        f"{FMP_BASE_URL}/stable/batch-aftermarket-trade?symbols={csv_symbols}&apikey={FMP_API_KEY}",
    ]
    for url in trade_urls:
        rows = _fetch_json_rows(url)
        for row in rows:
            sym = _first_text(row, ["symbol", "ticker", "s"]).upper()
            reg = regular_quotes.get(sym) if sym else None
            norm = _normalize_fmp_extended_trade_row(row, reg)
            if norm:
                out[norm["symbol"]] = norm
        if out:
            break

    # V2W5: batch/CSV extended endpoints may return only a partial subset.
    # Previously we only tried the single-symbol trade fallback when *zero* rows were
    # returned, so symbols such as EHGO could keep a stale regular close even while
    # other symbols received extended prices. Refill every missing symbol within the
    # safety cap before falling back to aftermarket quote/midpoint.
    trade_refill_attempted = 0
    trade_refill_filled = 0
    missing_trade = [s for s in symbols if s not in out]
    if missing_trade:
        for sym in missing_trade[:max(0, FMP_SINGLE_FALLBACK_LIMIT)]:
            trade_refill_attempted += 1
            rows = _fetch_json_rows(f"{FMP_BASE_URL}/stable/aftermarket-trade?symbol={sym}&apikey={FMP_API_KEY}")
            for row in rows:
                reg = regular_quotes.get(sym)
                norm = _normalize_fmp_extended_trade_row(row, reg)
                if norm:
                    out[norm["symbol"]] = norm
                    trade_refill_filled += 1
                    break

    missing = [s for s in symbols if s not in out]
    if missing:
        csv_missing = ",".join(missing)
        quote_urls = [
            # Newer FMP stable endpoint that returned bidPrice/askPrice in live testing.
            f"{FMP_BASE_URL}/stable/aftermarket-quote?symbol={csv_missing}&apikey={FMP_API_KEY}",
            f"{FMP_BASE_URL}/stable/batch-aftermarket-quote?symbols={csv_missing}&apikey={FMP_API_KEY}",
        ]
        for url in quote_urls:
            rows = _fetch_json_rows(url)
            for row in rows:
                sym = _first_text(row, ["symbol", "ticker", "s"]).upper()
                reg = regular_quotes.get(sym) if sym else None
                norm = _normalize_fmp_extended_quote_row(row, reg)
                if norm:
                    out[norm["symbol"]] = norm
            if any(s in out for s in missing):
                break

        # Last-resort single-symbol extended quote fallback, capped for API/Railway safety.
        still_missing = [s for s in missing if s not in out]
        if still_missing:
            for sym in still_missing[:max(0, FMP_SINGLE_FALLBACK_LIMIT)]:
                rows = _fetch_json_rows(f"{FMP_BASE_URL}/stable/aftermarket-quote?symbol={sym}&apikey={FMP_API_KEY}")
                for row in rows:
                    reg = regular_quotes.get(sym)
                    norm = _normalize_fmp_extended_quote_row(row, reg)
                    if norm:
                        out[norm["symbol"]] = norm
                        break

    if out:
        upsert_live_quotes(list(out.values()))
    return out


def _fetch_fmp_quotes(symbols: list[str]) -> dict[str, dict]:
    """Fetch FMP quotes using the right endpoint for the current market phase."""
    if not FMP_API_KEY or not symbols:
        return {}

    phase = _market_phase_now()
    regular = _fetch_fmp_regular_quotes(symbols)

    if _extended_hours_phase(phase):
        extended = _fetch_fmp_extended_quotes(symbols, regular)
        # Use extended prices where available; keep regular quote only for symbols with no extended data.
        out = dict(regular or {})
        out.update(extended or {})
        if out:
            upsert_live_quotes(list(out.values()))
        return out

    if regular:
        upsert_live_quotes(list(regular.values()))
    return regular


def _fetch_polygon_snapshot_quotes(symbols: list[str]) -> dict[str, dict]:
    if not POLYGON_API_KEY or not symbols:
        return {}
    out = {}
    for symbol in symbols[:40]:
        try:
            url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
            r = HTTP_SESSION.get(url, timeout=6)
            if r.status_code >= 400:
                continue
            t = (r.json().get("ticker") or {})
            last_trade = t.get("lastTrade") or {}
            day = t.get("day") or {}
            prev_day = t.get("prevDay") or {}
            price = to_float(last_trade.get("p") or day.get("c"))
            prev = to_float(prev_day.get("c"))
            if price <= 0:
                continue
            change_pct = ((price - prev) / prev) * 100 if prev > 0 else 0
            out[symbol] = {
                "symbol": symbol,
                "price": safe_round(price, 4),
                "previous_close": safe_round(prev, 4),
                "change_pct": safe_round(change_pct, 2),
                "volume": safe_round(to_float(day.get("v"))),
                "source": "polygon_snapshot_delayed",
                "source_label": "Polygon Fallback — متأخر تقريبًا 15 دقيقة",
                "change_pct_reliable": bool(prev > 0),
                "delayed": True,
                "reliable_for_execution": False,
                "updated_at": time.time(),
                "updated_label": datetime.now(NY_TZ).strftime("%H:%M:%S"),
                "market_phase": _market_phase_now(),
                "extended_hours": _extended_hours_phase(),
            }
        except Exception:
            continue
    if out:
        upsert_live_quotes(list(out.values()))
    return out


def get_live_quotes(symbols, prefer_cache: bool = True, allow_fallback: bool = True) -> dict:
    clean = _clean_symbols(symbols)
    phase = _market_phase_now()
    active_phase = _active_price_phase(phase)

    diagnostics = {
        "requested": len(symbols or []),
        "symbols": len(clean),
        "enabled": LIVE_QUOTES_ENABLED,
        "fmp_key_configured": bool(FMP_API_KEY),
        "websocket_configured": bool(FMP_WEBSOCKET_ENABLED),
        "market_phase": phase,
        "active_price_phase": active_phase,
        "extended_hours_phase": _extended_hours_phase(phase),
        "price_cache_allowed": bool(prefer_cache and not active_phase),
        "source": "none",
        "cache_used": 0,
        "fetched": 0,
        "extended_fetched": 0,
        "extended_refill_version": LIVE_QUOTES_EXTENDED_REFILL_VERSION,
    }
    if not LIVE_QUOTES_ENABLED or not clean:
        return {"ok": True, "quotes": {}, "diagnostics": diagnostics}

    # Never use SQLite price cache during active trading phases, including premarket and afterhours.
    use_cache = bool(prefer_cache and not active_phase)
    cached = get_cached_live_quotes(clean, max_age_sec=LIVE_QUOTES_CACHE_MAX_AGE_SEC) if use_cache else {}
    quotes = dict(cached or {})
    missing = [s for s in clean if s not in quotes]
    diagnostics["cache_used"] = len(quotes)

    fetched = _fetch_fmp_quotes(missing) if missing else {}
    if fetched:
        quotes.update(fetched)
        if any(str(q.get("source", "")).startswith("fmp_extended") for q in fetched.values()):
            diagnostics["source"] = "fmp_extended"
            diagnostics["extended_fetched"] = sum(1 for q in fetched.values() if str(q.get("source", "")).startswith("fmp_extended"))
        else:
            diagnostics["source"] = "fmp_rest"
        diagnostics["fetched"] += len(fetched)

    missing = [s for s in clean if s not in quotes]

    if allow_fallback and missing:
        fallback = _fetch_polygon_snapshot_quotes(missing)
        if fallback:
            quotes.update(fallback)
            diagnostics["source"] = "mixed_fmp_polygon" if diagnostics["source"] != "none" else "polygon_snapshot"
            diagnostics["fetched"] += len(fallback)

    if diagnostics["source"] == "none" and quotes:
        diagnostics["source"] = "sqlite_cache"

    return {"ok": True, "quotes": quotes, "diagnostics": diagnostics}


