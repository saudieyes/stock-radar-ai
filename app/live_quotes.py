"""FMP/Polygon live quote helpers.

Primary path: FMP REST batch/quote endpoints. FMP WebSocket can be enabled later
when the user's key proves it is entitled; this module keeps REST/BATCH as a safe
non-breaking fallback.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
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


def _clean_symbols(symbols) -> list[str]:
    out = []
    for s in symbols or []:
        t = str(s or "").upper().strip()
        if not t or t in out:
            continue
        # Keep common US symbols simple; skip unsafe query characters.
        if not all(ch.isalnum() or ch in {".", "-"} for ch in t):
            continue
        out.append(t)
    return out[:300]


def _normalize_fmp_row(row: dict) -> dict | None:
    try:
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if not symbol:
            return None
        price = to_float(row.get("price") or row.get("lastSalePrice") or row.get("last") or row.get("lp") or row.get("close"))
        prev = to_float(row.get("previousClose") or row.get("previous_close") or row.get("prevClose"))
        change_pct = to_float(row.get("changesPercentage") or row.get("changePercentage") or row.get("change_pct"))
        if not change_pct and price > 0 and prev > 0:
            change_pct = ((price - prev) / prev) * 100
        volume = to_float(row.get("volume") or row.get("avgVolume") or row.get("dayVolume"))
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
            "updated_label": datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S"),
        }
    except Exception:
        return None


def _fetch_fmp_quotes(symbols: list[str]) -> dict[str, dict]:
    if not FMP_API_KEY or not symbols:
        return {}
    csv_symbols = ",".join(symbols)
    endpoints = [
        f"{FMP_BASE_URL}/stable/batch-quote?symbols={csv_symbols}&apikey={FMP_API_KEY}",
        f"{FMP_BASE_URL}/api/v3/quote/{csv_symbols}?apikey={FMP_API_KEY}",
    ]
    for url in endpoints:
        try:
            r = HTTP_SESSION.get(url, timeout=LIVE_QUOTES_TIMEOUT_SEC)
            if r.status_code >= 400:
                continue
            data = r.json()
            rows = data if isinstance(data, list) else data.get("data") if isinstance(data, dict) else []
            out = {}
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                norm = _normalize_fmp_row(row)
                if norm:
                    out[norm["symbol"]] = norm
            if out:
                upsert_live_quotes(list(out.values()))
                return out
        except Exception:
            continue
    return {}


def _fetch_polygon_snapshot_quotes(symbols: list[str]) -> dict[str, dict]:
    # Safe fallback only; avoid if no key. This is not used as the main live layer.
    if not POLYGON_API_KEY or not symbols:
        return {}
    out = {}
    # Keep fallback bounded to avoid burning calls.
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
                "updated_label": datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S"),
            }
        except Exception:
            continue
    if out:
        upsert_live_quotes(list(out.values()))
    return out


def get_live_quotes(symbols, prefer_cache: bool = True, allow_fallback: bool = True) -> dict:
    clean = _clean_symbols(symbols)
    diagnostics = {
        "requested": len(symbols or []),
        "symbols": len(clean),
        "enabled": LIVE_QUOTES_ENABLED,
        "fmp_key_configured": bool(FMP_API_KEY),
        "websocket_configured": bool(FMP_WEBSOCKET_ENABLED),
        "source": "none",
        "cache_used": 0,
        "fetched": 0,
    }
    if not LIVE_QUOTES_ENABLED or not clean:
        return {"ok": True, "quotes": {}, "diagnostics": diagnostics}

    cached = get_cached_live_quotes(clean, max_age_sec=LIVE_QUOTES_CACHE_MAX_AGE_SEC) if prefer_cache else {}
    quotes = dict(cached or {})
    missing = [s for s in clean if s not in quotes]
    diagnostics["cache_used"] = len(quotes)

    fetched = _fetch_fmp_quotes(missing) if missing else {}
    if fetched:
        quotes.update(fetched)
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

