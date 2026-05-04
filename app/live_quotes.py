"""FMP/Polygon live quote helpers.

Fix33: extended-hours aware live quotes.

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
FMP_WEBSOCKET_ENABLED = str(os.getenv("FMP_WEBSOCKET_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}

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
        if not change_pct and price > 0 and prev > 0:
            change_pct = ((price - prev) / prev) * 100
        volume = _first_number(row, ["volume", "avgVolume", "dayVolume"])
        if price <= 0:
            return None
        now = time.time()
        return {
            "symbol": symbol,
            "price": safe_round(price, 4),
            "previous_close": safe_round(prev, 4),
            "change_pct": safe_round(change_pct, 2),
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
        prev = _first_number(reg, ["previous_close", "previousClose", "prevClose"])
        if prev <= 0:
            prev = _first_number(row, ["previousClose", "previous_close", "prevClose"])

        change_pct = ((price - prev) / prev) * 100 if prev > 0 else 0.0
        volume = _first_number(row, ["volume", "size", "lastSize", "tradeSize", "v", "dayVolume"])
        raw_ts = row.get("timestamp") or row.get("time") or row.get("t") or row.get("lastUpdated")
        now = time.time()
        phase = _market_phase_now()
        return {
            "symbol": symbol,
            "price": safe_round(price, 4),
            "previous_close": safe_round(prev, 4),
            "change_pct": safe_round(change_pct, 2),
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
        prev = _first_number(reg, ["previous_close", "previousClose", "prevClose"])
        if prev <= 0:
            prev = _first_number(row, ["previousClose", "previous_close", "prevClose"])

        change_pct = ((price - prev) / prev) * 100 if prev > 0 else 0.0
        volume = _first_number(row, ["volume", "bidSize", "askSize", "size", "v"])
        raw_ts = row.get("timestamp") or row.get("time") or row.get("t") or row.get("lastUpdated")
        now = time.time()
        phase = _market_phase_now()
        return {
            "symbol": symbol,
            "price": safe_round(price, 4),
            "previous_close": safe_round(prev, 4),
            "change_pct": safe_round(change_pct, 2),
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
    return {}


def _fetch_fmp_extended_quotes(symbols: list[str], regular_quotes: dict[str, dict] | None = None) -> dict[str, dict]:
    if not FMP_API_KEY or not symbols:
        return {}
    regular_quotes = regular_quotes or {}
    csv_symbols = ",".join(symbols)

    out: dict[str, dict] = {}

    # First preference: last extended trade because it is closest to a traded price.
    trade_urls = [
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

    missing = [s for s in symbols if s not in out]
    if missing:
        csv_missing = ",".join(missing)
        quote_urls = [
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
                "source": "polygon_snapshot",
                "source_label": "Polygon Fallback",
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

