import time
from datetime import datetime, timedelta

from scanner import get_scan_universe, get_dynamic_universe_target, record_active_universe_diagnostics
from app.source_discovery import (
    dynamic_discovery_enabled,
    build_dynamic_universe,
    get_recommended_deep_scan_target,
)

from .settings import (
    HISTORY_CACHE, HTTP_SESSION, INTRADAY_CACHE, INTRADAY_CACHE_TTL_CLOSED, INTRADAY_CACHE_TTL_OPEN,
    POLYGON_API_KEY, SNAPSHOT_CACHE, SNAPSHOT_CACHE_TTL_CLOSED, SNAPSHOT_CACHE_TTL_EXTENDED, SNAPSHOT_CACHE_TTL_OPEN,
    SHARIA_SOURCE_GRAY_MAX_RATIO, SHARIA_SOURCE_GRAY_MIN_HARD_CAP, SHARIA_SOURCE_GRAY_SOFT_CAP,
    SHARIA_SOURCE_REFILL_MAX_RESERVE, SHARIA_SOURCE_REFILL_MIN_RESERVE, SHARIA_SOURCE_REFILL_MULTIPLIER,
)
from .utils import *
from .utils import _cache_get, _cache_set

def http_get_json(url, timeout=12):
    try:
        r = HTTP_SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except:
        return {}




# Fix14c: extra clean-refill symbols. These are not auto-approved;
# every name still passes Sharia Filter V2 before analysis. The goal is to
# broaden the replacement pool with liquid names that usually have better
# reference data, so the source is not starved after strict Sharia filtering.
CLEAN_REFILL_SYMBOLS = [
    "AAPL","MSFT","NVDA","AMD","META","AMZN","GOOGL","GOOG","TSM","ASML","ADBE","CRM","NOW","SNOW","SHOP","DDOG","NET","MDB","PANW","CRWD","ZS","FTNT","ANET","CDNS","SNPS","KLAC","LRCX","AMAT","TER","QCOM","TXN","ADI","NXPI","ON","MPWR","MU","STX","WDC","DELL","HPQ","NTAP","HPE","IBM","ORCL","INTU","ADSK","TEAM","WDAY","VEEV","PLTR","U","PATH","AI","APPF","ESTC","FSLY","AKAM","TTD","PINS","ROKU","SPOT","UBER","LYFT","DASH","ABNB","BKNG","EXPE",
    "LLY","NVO","MRK","ABBV","AMGN","GILD","VRTX","REGN","BIIB","ALNY","INCY","TECH","TMO","DHR","A","IDXX","ISRG","SYK","BSX","MDT","EW","DXCM","PODD","HOLX","ALGN","RMD","STE","ZBH","WST","COO","ABT","BAX","BDX","ZTS","HUM","CI","ELV","CNC","GEHC",
    "CAT","DE","PCAR","CMI","ETN","EMR","ROK","AME","PH","ITW","DOV","XYL","IR","CARR","OTIS","JCI","HON","GE","RTX","LMT","NOC","GD","TXT","HWM","TDG","AXON","FAST","GWW","URI","PWR","FIX","FLS","GNRC","AYI","BLDR","TREX","SWK",
    "COST","WMT","TGT","DG","DLTR","HD","LOW","TJX","ROST","ULTA","NKE","LULU","DECK","CROX","TPR","RL","SBUX","CMG","YUM","MCD","DRI","TXRH","DPZ","SHAK","DKS","BBY","WING","ELF","CELH","MNST","KO","PEP","KDP","HSY","MDLZ","CL","CLX","PG","CHD","KMB","EL","KVUE","KHC",
    "LIN","SHW","ECL","APD","NEM","FCX","SCCO","AA","ALB","LTHM","CE","DD","DOW","EMN","PPG","RPM","CF","MOS","NTR","SMG","FMC","CCK","BLL","PKG","IP",
    "ENPH","FSLR","SEDG","NEE","DUK","SO","AEP","XEL","SRE","PEG","ED","WEC","DTE","EXC","AWK","ATO","CMS","LNT",
    "TMUS","VZ","T","CHTR","CMCSA","DIS","NFLX","PARA","WBD","NYT","RDDT","SNAP","RBLX","TTWO","EA","MTCH","BMBL",
    "GM","F","TSLA","RIVN","LCID","NIO","LI","XPEV","MBLY","APTV","BWA","ALV","GNTX","LEA","MGA","ORLY","AZO","AAP",
    "OKLO","ASTS","RKLB","IONQ","QBTS","RGTI","CRML","MP","NVTS","SOUN","BROS","FIVE","CAVA","RDDT","ARM","SMCI","VRT","CEG","TLN","VST","GTLB","ESTC","DOCN","DUOL","HIMS","HOOD","COIN","MARA","RIOT",
]


def _extract_symbol_from_item(item):
    try:
        if isinstance(item, str):
            return item.upper().strip()
        if isinstance(item, dict):
            return str(item.get("symbol") or item.get("ticker") or "").upper().strip()
    except Exception:
        return ""
    return ""




def unique_keep_order(items):
    """Return unique symbols/items while preserving order.

    Fix14b hotfix: market_data.get_active_universe uses this helper locally
    after moving source filtering into app/market_data.py. Keeping it local
    avoids depending on scanner.unique_keep_order and prevents /debug-scan
    from crashing when building the active universe.
    """
    out = []
    seen = set()
    try:
        iterable = items or []
    except Exception:
        iterable = []
    for item in iterable:
        try:
            if isinstance(item, dict):
                key = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
                val = key or item
            else:
                key = str(item or "").upper().strip()
                val = key
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(val)
        except Exception:
            continue
    return out


def _manual_priority_symbols(limit: int = 60):
    """Keep user's watched/owned symbols in the source without letting them dominate it."""
    out = []
    try:
        from app.portfolio_store import load_portfolio_items
        for item in load_portfolio_items() or []:
            s = _extract_symbol_from_item(item)
            if s and s not in out:
                out.append(s)
    except Exception:
        pass
    try:
        from app.watchlist_store import load_manual_watchlist
        for item in load_manual_watchlist() or []:
            s = _extract_symbol_from_item(item)
            if s and s not in out:
                out.append(s)
    except Exception:
        pass
    return out[:limit]


def get_active_universe(max_symbols: int = 60):
    """
    Source/universe gateway.

    Fix14a behavior:
    - keeps the final target dynamic (quiet/normal/active);
    - asks scanner for a much wider ranked reserve;
    - applies Sharia Filter V2 before deep analysis;
    - refills from clean candidates first;
    - limits gray/uncertain symbols instead of letting them dominate the list;
    - records detailed diagnostics for /debug-scan.
    """
    try:
        max_symbols = int(max_symbols or 60)
    except Exception:
        max_symbols = 60
    max_symbols = max(40, min(max_symbols, 300))

    # Dynamic final target. With Dynamic Discovery enabled, this target reflects
    # the agreed full-market scan schedule (quiet/normal/hot sessions) while
    # remaining bounded so the deep radar analysis stays fast and selective.
    if 120 <= max_symbols <= 260:
        try:
            if dynamic_discovery_enabled():
                max_symbols = max(120, min(260, int(get_recommended_deep_scan_target(default=max_symbols) or max_symbols)))
            elif max_symbols <= 180:
                max_symbols = max(100, min(200, int(get_dynamic_universe_target(default=max_symbols) or max_symbols)))
        except Exception:
            pass

    manual = _manual_priority_symbols(limit=min(40, max_symbols))

    # Wider reserve, but still bounded. This improves refill without caching live prices.
    try:
        reserve_size = int(max_symbols * float(SHARIA_SOURCE_REFILL_MULTIPLIER or 3.2))
    except Exception:
        reserve_size = int(max_symbols * 3.2)
    reserve_size = max(int(SHARIA_SOURCE_REFILL_MIN_RESERVE or 620), reserve_size, max_symbols + 220)
    reserve_size = min(int(SHARIA_SOURCE_REFILL_MAX_RESERVE or 700), reserve_size)
    reserve_size = max(max_symbols, reserve_size)

    if dynamic_discovery_enabled():
        base = build_dynamic_universe(max_symbols=reserve_size) or []
    else:
        base = get_scan_universe(max_symbols=reserve_size) or []

    sharia_blocked = []
    sharia_gray = []
    sharia_allowed = []
    sharia_unknown_errors = []
    sharia_assessments = {}
    seen_candidates = set()

    try:
        from app.data_store import get_manual_sharia_exclusions_map, get_manual_sharia_approvals_map
        from app.sharia_filter import assess_sharia_source_fast
        manual_exclusions = get_manual_sharia_exclusions_map()
        manual_approvals = get_manual_sharia_approvals_map()
    except Exception:
        manual_exclusions = {}
        manual_approvals = {}
        assess_sharia_source_fast = None

    def _screen_symbol(sym: str):
        t = str(sym or "").upper().strip()
        if not t or t in seen_candidates:
            return
        seen_candidates.add(t)
        if assess_sharia_source_fast is None:
            sharia_allowed.append(t)
            sharia_assessments[t] = {"status": "unknown", "reason": "لم يعمل فحص الشرعية السريع"}
            return
        try:
            assessment = assess_sharia_source_fast(t, manual_exclusions, manual_approvals)
            sharia_assessments[t] = assessment
            if bool(assessment.get("should_block", False)):
                sharia_blocked.append({
                    "symbol": t,
                    "status": assessment.get("status", ""),
                    "reason": str(assessment.get("reason", "") or "")[:180],
                })
                return
            if bool(assessment.get("is_gray", False)):
                sharia_gray.append(t)
                return
            sharia_allowed.append(t)
        except Exception as exc:
            sharia_unknown_errors.append({"symbol": t, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            # Candidate can be used as gray only if there is a clean shortage.
            sharia_gray.append(t)

    for s in manual + list(base) + CLEAN_REFILL_SYMBOLS:
        _screen_symbol(s)

    clean_final = []
    for t in sharia_allowed:
        if t and t not in clean_final:
            clean_final.append(t)
        if len(clean_final) >= max_symbols:
            break

    # Gray symbols are allowed only as a shortage fallback, and are capped.
    try:
        gray_cap = max(int(SHARIA_SOURCE_GRAY_MIN_HARD_CAP or 18), int(max_symbols * float(SHARIA_SOURCE_GRAY_MAX_RATIO or 0.24)))
        gray_cap = min(int(SHARIA_SOURCE_GRAY_SOFT_CAP or 48), gray_cap)
    except Exception:
        gray_cap = 45
    needed_after_clean = max(0, max_symbols - len(clean_final))
    allowed_gray_count = min(needed_after_clean, gray_cap)

    gray_final = []
    if allowed_gray_count > 0:
        for t in sharia_gray:
            if t and t not in clean_final and t not in gray_final:
                gray_final.append(t)
            if len(gray_final) >= allowed_gray_count:
                break

    final = (clean_final + gray_final)[:max_symbols]

    # Re-add user priority only if not manually blocked. This keeps owned/watchlist names visible,
    # but does not override explicit Sharia manual exclusions.
    for sym in manual:
        if sym in final:
            continue
        a = sharia_assessments.get(sym, {})
        if a and not bool(a.get("should_block", False)) and len(final) < max_symbols:
            final.insert(0, sym)

    final = unique_keep_order(final)[:max_symbols]

    try:
        record_active_universe_diagnostics(final, manual, max_symbols)
        # Attach source-level sharia diagnostics without changing the public API.
        import scanner as _scanner
        diag = dict(getattr(_scanner, "LAST_SOURCE_DIAGNOSTICS", {}) or {})
        gray_set = set(sharia_gray)
        clean_set = set(sharia_allowed)
        diag["sharia_source_filter_version"] = "sharia_v2_refill14c_balanced_buckets"
        diag["dynamic_discovery_active_in_source"] = bool(dynamic_discovery_enabled())
        diag["sharia_refill_reserve_size"] = int(reserve_size)
        diag["sharia_prefilter_candidates"] = len(seen_candidates)
        diag["sharia_prefilter_blocked"] = len(sharia_blocked)
        diag["sharia_prefilter_clean_total"] = len(sharia_allowed)
        diag["sharia_prefilter_clean_used"] = len([x for x in final if x in clean_set])
        diag["sharia_prefilter_gray_used"] = len([x for x in final if x in gray_set])
        diag["sharia_prefilter_gray_total"] = len(sharia_gray)
        diag["sharia_prefilter_gray_cap"] = int(gray_cap)
        diag["sharia_clean_refill_symbols"] = int(len(CLEAN_REFILL_SYMBOLS))
        diag["sharia_manual_approvals_loaded"] = int(len(manual_approvals or {}))
        diag["sharia_prefilter_clean_shortage"] = max(0, max_symbols - len(clean_final))
        diag["sharia_prefilter_final_shortage"] = max(0, max_symbols - len(final))
        diag["sharia_prefilter_refill_count"] = max(0, len(final) - min(len(sharia_allowed), max_symbols))
        diag["sharia_prefilter_block_rate_pct"] = safe_round((len(sharia_blocked) / max(1, len(seen_candidates))) * 100, 1)
        diag["sharia_prefilter_sample_blocked"] = sharia_blocked[:25]
        diag["sharia_prefilter_errors"] = sharia_unknown_errors[:15]
        _scanner.LAST_SOURCE_DIAGNOSTICS = diag
    except Exception:
        pass
    return final

def get_daily_bars(symbol):
    try:
        today = datetime.utcnow().date()
        from_5y = (today - timedelta(days=365 * 5)).isoformat()
        to_date = today.isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{from_5y}/{to_date}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=22)
        return r.get("results", []) or []
    except:
        return []



# Runtime helper functions moved here so market_data can use them directly after refactor.
def get_prev_from_daily_bars(daily_bars):
    if not daily_bars:
        return None
    try:
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date()
        market_open = is_market_open_now()
        candidates = []
        for row in daily_bars:
            close_price = to_float(row.get("c"))
            if close_price <= 0:
                continue
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except Exception:
                row_date = None
            if market_open and row_date == today_ny:
                continue
            candidates.append(row)
        source = candidates[-1] if candidates else daily_bars[-1]
        return {
            "price": to_float(source.get("c")),
            "high": to_float(source.get("h")),
            "low": to_float(source.get("l")),
            "volume": to_float(source.get("v")),
            "open": to_float(source.get("o")),
        }
    except Exception:
        return None


def get_prev(symbol):
    try:
        r = http_get_json(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=12,
        )
        results = r.get("results", [])
        if not results:
            return None
        d = results[0]
        return {
            "price": to_float(d.get("c")),
            "high": to_float(d.get("h")),
            "low": to_float(d.get("l")),
            "volume": to_float(d.get("v")),
            "open": to_float(d.get("o")),
        }
    except Exception:
        return None


def get_latest_minute_price(symbol):
    try:
        today_ny = latest_market_date_str()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/"
            f"{today_ny}/{today_ny}?adjusted=true&sort=desc&limit=5&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=12)
        bars = r.get("results", []) or []
        if not bars:
            return {
                "available": False,
                "current_price": 0.0,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume": 0.0,
                "updated": 0,
            }
        bar = bars[0]
        return {
            "available": True,
            "current_price": to_float(bar.get("c")),
            "open": to_float(bar.get("o")),
            "high": to_float(bar.get("h")),
            "low": to_float(bar.get("l")),
            "volume": to_float(bar.get("v")),
            "updated": int(to_float(bar.get("t"))),
        }
    except Exception:
        return {
            "available": False,
            "current_price": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0.0,
            "updated": 0,
        }



def _normalize_timestamp_to_ms(value) -> int:
    """Normalize Polygon-style timestamps (seconds/ms/ns) to milliseconds."""
    try:
        ts = int(float(value or 0))
        if ts <= 0:
            return 0
        # nanoseconds: 1700000000000000000
        if ts > 10_000_000_000_000_000:
            return int(ts / 1_000_000)
        # microseconds: 1700000000000000
        if ts > 10_000_000_000_000:
            return int(ts / 1_000)
        # already ms: 1700000000000
        if ts > 10_000_000_000:
            return ts
        # seconds
        if ts > 1_000_000_000:
            return ts * 1000
    except Exception:
        pass
    return 0

def get_snapshot_quote(symbol):
    try:
        symbol = str(symbol).upper().strip()
        phase = get_market_phase()
        cache_key = f"{symbol}:{phase}"
        cached = _cache_get(SNAPSHOT_CACHE, cache_key)
        if cached:
            return cached

        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        t = (r.get("ticker") or {})
        day = (t.get("day") or {})
        prev_day = (t.get("prevDay") or {})
        last_trade = (t.get("lastTrade") or {})
        min_data = (t.get("min") or {})

        last_price = to_float(last_trade.get("p"))
        prev_close = to_float(prev_day.get("c")) or 0.0
        day_open = to_float(day.get("o")) or 0.0
        day_high = to_float(day.get("h")) or 0.0
        day_low = to_float(day.get("l")) or 0.0
        day_volume = to_float(day.get("v")) or 0.0

        minute = get_latest_minute_price(symbol)

        current_price = last_price or to_float(min_data.get("c")) or to_float(day.get("c")) or 0.0
        if minute.get("available") and minute.get("current_price", 0) > 0:
            minute_price = to_float(minute.get("current_price", 0))
            if phase in {"open", "after_hours", "pre_market"}:
                current_price = minute_price or current_price
                if minute.get("high", 0) > 0:
                    day_high = max(day_high, to_float(minute.get("high", 0)))
                if minute.get("low", 0) > 0:
                    minute_low = to_float(minute.get("low", 0))
                    day_low = min(day_low, minute_low) if day_low > 0 else minute_low
                if minute.get("volume", 0) > 0:
                    day_volume = max(day_volume, to_float(minute.get("volume", 0)))

        if phase == "open":
            ttl = SNAPSHOT_CACHE_TTL_OPEN
        elif phase in {"after_hours", "pre_market"}:
            ttl = SNAPSHOT_CACHE_TTL_EXTENDED
        else:
            ttl = SNAPSHOT_CACHE_TTL_CLOSED

        updated_ms = (
            _normalize_timestamp_to_ms(last_trade.get("t"))
            or _normalize_timestamp_to_ms(min_data.get("t"))
            or _normalize_timestamp_to_ms(minute.get("updated"))
            or 0
        )

        out = {
            "available": current_price > 0,
            "current_price": safe_round(current_price, 4),
            "open": safe_round(day_open, 4),
            "high": safe_round(day_high, 4),
            "low": safe_round(day_low, 4),
            "volume": safe_round(day_volume),
            "previous_close": safe_round(prev_close, 4),
            "change_vs_prev_close_pct": safe_round(((current_price - prev_close) / prev_close) * 100, 2) if prev_close > 0 and current_price > 0 else 0.0,
            "change_from_open_pct": safe_round(((current_price - day_open) / day_open) * 100, 2) if day_open > 0 and current_price > 0 else 0.0,
            "phase": phase,
            "source": "snapshot",
            "updated": updated_ms,
        }
        return _cache_set(SNAPSHOT_CACHE, cache_key, out, ttl)
    except Exception:
        return {"available": False, "current_price": 0.0, "source": "snapshot_error"}
def calculate_atr(daily_bars, period: int = 14) -> float:
    try:
        if not daily_bars or len(daily_bars) < period + 1:
            return 0.0
        true_ranges = []
        prev_close = None
        for row in daily_bars[-(period + 40):]:
            high = to_float(row.get("h"))
            low = to_float(row.get("l"))
            close = to_float(row.get("c"))
            if high <= 0 or low <= 0 or close <= 0:
                prev_close = close or prev_close
                continue
            if prev_close is None or prev_close <= 0:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
            prev_close = close
        if len(true_ranges) < period:
            return 0.0
        return safe_round(sum(true_ranges[-period:]) / period, 4)
    except:
        return 0.0


def get_atr_overlay(entry_price: float, daily_bars) -> dict:
    try:
        entry_price = float(entry_price or 0)
        atr_14 = float(calculate_atr(daily_bars, 14) or 0)
        if entry_price <= 0:
            return {
                "atr_14": 0.0,
                "atr_pct": 0.0,
                "volatility_label": "لا توجد بيانات كافية",
                "volatility_detail": "لا توجد بيانات كافية لتحديد التذبذب.",
                "atr_stop_suggestion": 0.0,
                "atr_target_1_suggestion": 0.0,
                "atr_target_2_suggestion": 0.0,
            }
        effective_atr = atr_14 if atr_14 > 0 else entry_price * 0.03
        atr_pct = (effective_atr / entry_price) * 100 if entry_price > 0 else 0.0
        if atr_pct <= 2.0:
            label = "هادئ"
            detail = "تذبذب السهم منخفض نسبيًا، ويمكن أن يتحمل وقفًا أقرب."
        elif atr_pct <= 4.5:
            label = "متوازن"
            detail = "تذبذب السهم طبيعي ومناسب لمعظم الخطط."
        elif atr_pct <= 7.0:
            label = "نشط"
            detail = "السهم متذبذب نسبيًا ويحتاج وقفًا أوسع وإدارة أدق."
        else:
            label = "عنيف"
            detail = "السهم عالي التذبذب وقد يضرب الوقف بسهولة إذا كان ضيقًا."
        return {
            "atr_14": safe_round(effective_atr, 4),
            "atr_pct": safe_round(atr_pct, 2),
            "volatility_label": label,
            "volatility_detail": detail,
            "atr_stop_suggestion": safe_round(entry_price - (effective_atr * 1.5)),
            "atr_target_1_suggestion": safe_round(entry_price + (effective_atr * 2.0)),
            "atr_target_2_suggestion": safe_round(entry_price + (effective_atr * 4.0)),
        }
    except:
        return {
            "atr_14": 0.0,
            "atr_pct": 0.0,
            "volatility_label": "لا توجد بيانات كافية",
            "volatility_detail": "لا توجد بيانات كافية لتحديد التذبذب.",
            "atr_stop_suggestion": 0.0,
            "atr_target_1_suggestion": 0.0,
            "atr_target_2_suggestion": 0.0,
        }


def get_history_levels(symbol, prev_data=None, daily_bars=None):
    if symbol in HISTORY_CACHE:
        return HISTORY_CACHE[symbol]

    out = {
        "year_high": 0.0,
        "ath_high": 0.0,
        "near_52w_high": False,
        "near_ath": False,
        "ath_breakout_zone": False,
    }

    bars = daily_bars if daily_bars is not None else get_daily_bars(symbol)
    if bars:
        ny = ZoneInfo("America/New_York")
        cutoff_52w = datetime.utcnow().date() - timedelta(days=365)
        highs_5 = []
        highs_52 = []
        for row in bars:
            high = to_float(row.get("h"))
            if high <= 0:
                continue
            highs_5.append(high)
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None
            if row_date and row_date >= cutoff_52w:
                highs_52.append(high)

        if highs_52:
            out["year_high"] = max(highs_52)
        if highs_5:
            out["ath_high"] = max(highs_5)

    prev = prev_data if prev_data is not None else get_prev_from_daily_bars(bars) or get_prev(symbol)
    if prev:
        price = prev["price"]
        if out["year_high"] > 0:
            out["near_52w_high"] = price >= out["year_high"] * 0.97
        if out["ath_high"] > 0:
            out["near_ath"] = price >= out["ath_high"] * 0.97
            out["ath_breakout_zone"] = price >= out["ath_high"] * 0.995

    HISTORY_CACHE[symbol] = out
    return out


def get_trend(symbol, daily_bars=None):
    try:
        data = daily_bars if daily_bars is not None else get_daily_bars(symbol)
        closes = [to_float(x.get("c")) for x in data if to_float(x.get("c")) > 0]
        if len(closes) < 50:
            return {"trend": "unknown", "ma20": 0.0, "ma50": 0.0}

        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes[-50:]) / 50
        price = closes[-1]

        if price > ma20 > ma50:
            trend = "صاعد قوي"
        elif price > ma50:
            trend = "صاعد"
        elif price < ma20 < ma50:
            trend = "هابط"
        else:
            trend = "متذبذب"

        return {"trend": trend, "ma20": ma20, "ma50": ma50}
    except:
        return {"trend": "unknown", "ma20": 0.0, "ma50": 0.0}


def get_session_elapsed_ratio() -> float:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return 0.0
        session_start = 9 * 60 + 30
        session_end = 16 * 60
        current_minutes = now_ny.hour * 60 + now_ny.minute
        if current_minutes <= session_start:
            return 0.0
        if current_minutes >= session_end:
            return 1.0
        elapsed = (current_minutes - session_start) / float(session_end - session_start)
        return clamp(elapsed, 0.0, 1.0)
    except:
        return 0.0


def get_volume_ratio(symbol, intraday=None, daily_bars=None):
    try:
        data = daily_bars if daily_bars is not None else get_daily_bars(symbol)
        if not data:
            return 1.0

        market_open = is_market_open_now()
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date()

        historical_volumes = []
        current_session_daily_volume = 0.0

        for row in data:
            volume = to_float(row.get("v"))
            if volume <= 0:
                continue
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None

            if market_open and row_date == today_ny:
                current_session_daily_volume = volume
                continue

            historical_volumes.append(volume)

        if len(historical_volumes) < 20:
            if len(historical_volumes) >= 5:
                avg_volume = sum(historical_volumes) / len(historical_volumes)
            else:
                return 1.0
        else:
            avg_volume = sum(historical_volumes[-20:]) / 20.0

        if avg_volume <= 0:
            return 1.0

        if market_open:
            intraday = intraday or get_intraday_snapshot(symbol)
            session_volume = float((intraday or {}).get("session_volume", 0) or 0)
            if session_volume <= 0:
                session_volume = current_session_daily_volume
            elapsed_ratio = float((intraday or {}).get("session_elapsed_ratio", 0) or 0)
            if elapsed_ratio <= 0:
                elapsed_ratio = get_session_elapsed_ratio()
            if session_volume > 0 and elapsed_ratio > 0:
                normalized_elapsed = max(elapsed_ratio, 0.08)
                projected_day_volume = session_volume / normalized_elapsed
                return clamp(projected_day_volume / avg_volume, 0.2, 8.0)

        latest_complete_volume = historical_volumes[-1] if historical_volumes else current_session_daily_volume
        if latest_complete_volume <= 0:
            return 1.0
        return clamp(latest_complete_volume / avg_volume, 0.2, 8.0)
    except:
        return 1.0


def get_intraday_snapshot(symbol):
    symbol = str(symbol).upper().strip()
    market_open = is_market_open_now()
    cache_key = f"{symbol}:{'open' if market_open else 'closed'}"
    ttl = INTRADAY_CACHE_TTL_OPEN if market_open else INTRADAY_CACHE_TTL_CLOSED

    cached = _cache_get(INTRADAY_CACHE, cache_key)
    if cached:
        return cached

    out = {
        "available": False,
        "market_open": market_open,
        "current_price": 0.0,
        "session_open": 0.0,
        "session_high": 0.0,
        "session_low": 0.0,
        "session_volume": 0.0,
        "avg_5m_volume": 0.0,
        "latest_5m_volume": 0.0,
        "intraday_volume_ratio": 0.0,
        "vwap_proxy": 0.0,
        "above_vwap_proxy": False,
        "opening_drive": "unknown",
        "bars_count": 0,
        "session_elapsed_ratio": 0.0,
        "projected_day_volume": 0.0,
        "recent_red_bars": 0,
        "recent_green_bars": 0,
        "pullback_volume_dry": False,
        "pullback_volume_ratio": 0.0,
        "spike_from_open_pct": 0.0,
        "pullback_from_high_pct": 0.0,
        "session_position_pct": 0.0,
    }

    if not market_open:
        return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

    try:
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date().isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/5/minute/"
            f"{today_ny}/{today_ny}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=15)
        bars = r.get("results", [])
        if not bars:
            return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

        volumes = [to_float(x.get("v")) for x in bars if to_float(x.get("v")) > 0]
        closes = [to_float(x.get("c")) for x in bars if to_float(x.get("c")) > 0]
        if not volumes or not closes:
            return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

        session_open = to_float(bars[0].get("o"))
        session_high = max(to_float(x.get("h")) for x in bars)
        lows = [to_float(x.get("l")) for x in bars if to_float(x.get("l")) > 0]
        session_low = min(lows) if lows else 0.0
        session_volume = sum(volumes)
        latest_5m_volume = volumes[-1]
        avg_5m_volume = sum(volumes) / len(volumes) if volumes else 0.0
        intraday_volume_ratio = latest_5m_volume / avg_5m_volume if avg_5m_volume > 0 else 0.0

        weighted_total = 0.0
        volume_total = 0.0
        for bar in bars:
            typical = (to_float(bar.get("h")) + to_float(bar.get("l")) + to_float(bar.get("c"))) / 3
            vol = to_float(bar.get("v"))
            if vol > 0:
                weighted_total += typical * vol
                volume_total += vol

        vwap_proxy = weighted_total / volume_total if volume_total > 0 else closes[-1]
        current_price = closes[-1]
        first_close = to_float(bars[0].get("c"))
        elapsed_ratio = get_session_elapsed_ratio()
        normalized_elapsed = max(elapsed_ratio, 0.08) if elapsed_ratio > 0 else 0.0
        projected_day_volume = (session_volume / normalized_elapsed) if normalized_elapsed > 0 else 0.0

        if current_price > session_open and current_price >= first_close:
            opening_drive = "صاعد"
        elif current_price < session_open and current_price <= first_close:
            opening_drive = "هابط"
        else:
            opening_drive = "متذبذب"

        recent_red_bars = 0
        recent_green_bars = 0
        recent_slice = bars[-4:]
        for idx in range(1, len(recent_slice)):
            prev_close = to_float(recent_slice[idx - 1].get("c"))
            cur_close = to_float(recent_slice[idx].get("c"))
            if cur_close < prev_close:
                recent_red_bars += 1
            elif cur_close > prev_close:
                recent_green_bars += 1

        last3 = volumes[-3:] if len(volumes) >= 3 else volumes
        prior6 = volumes[-9:-3] if len(volumes) >= 9 else volumes[:-3]
        last3_avg = sum(last3) / len(last3) if last3 else 0.0
        prior6_avg = sum(prior6) / len(prior6) if prior6 else avg_5m_volume
        pullback_volume_ratio = (last3_avg / prior6_avg) if prior6_avg > 0 else 0.0
        pullback_volume_dry = pullback_volume_ratio <= 0.85 if prior6_avg > 0 else False

        spike_from_open_pct = ((session_high - session_open) / session_open) if session_open > 0 and session_high > 0 else 0.0
        pullback_from_high_pct = ((session_high - current_price) / session_high) if session_high > 0 and current_price > 0 else 0.0
        session_range = max(session_high - session_low, 0.0001)
        session_position_pct = ((current_price - session_low) / session_range) * 100 if session_range > 0 else 0.0

        out = {
            "available": True,
            "market_open": market_open,
            "current_price": current_price,
            "session_open": session_open,
            "session_high": session_high,
            "session_low": session_low,
            "session_volume": session_volume,
            "avg_5m_volume": avg_5m_volume,
            "latest_5m_volume": latest_5m_volume,
            "intraday_volume_ratio": intraday_volume_ratio,
            "vwap_proxy": vwap_proxy,
            "above_vwap_proxy": current_price >= vwap_proxy if vwap_proxy > 0 else False,
            "opening_drive": opening_drive,
            "bars_count": len(bars),
            "session_elapsed_ratio": safe_round(elapsed_ratio, 4),
            "projected_day_volume": safe_round(projected_day_volume),
            "recent_red_bars": recent_red_bars,
            "recent_green_bars": recent_green_bars,
            "pullback_volume_dry": pullback_volume_dry,
            "pullback_volume_ratio": safe_round(pullback_volume_ratio, 2),
            "spike_from_open_pct": safe_round(spike_from_open_pct * 100, 2),
            "pullback_from_high_pct": safe_round(pullback_from_high_pct * 100, 2),
            "session_position_pct": safe_round(session_position_pct, 2),
        }
    except:
        pass

    return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)


def build_live_price_block(symbol, prev_data, intraday_data):
    phase = get_market_phase()
    prev_price = to_float(prev_data.get("price", 0)) if prev_data else 0.0
    prev_open = to_float(prev_data.get("open", 0)) if prev_data else 0.0
    prev_high = to_float(prev_data.get("high", 0)) if prev_data else 0.0
    prev_low = to_float(prev_data.get("low", 0)) if prev_data else 0.0
    prev_volume = to_float(prev_data.get("volume", 0)) if prev_data else 0.0

    snap = {}
    if not (phase == "open" and intraday_data.get("available") and to_float(intraday_data.get("current_price", 0)) > 0):
        snap = get_snapshot_quote(symbol)

    current_price = prev_price
    open_price = prev_open
    previous_close = prev_price
    change_vs_prev_close_pct = 0.0
    change_from_open_pct = 0.0
    price_source = "previous_close"
    price_reliable_for_execution = False

    if phase == "open" and intraday_data.get("available") and to_float(intraday_data.get("current_price", 0)) > 0:
        current_price = to_float(intraday_data.get("current_price", 0)) or prev_price
        open_price = to_float(intraday_data.get("session_open", 0)) or prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100
        if previous_close > 0 and current_price > 0:
            change_vs_prev_close_pct = ((current_price - previous_close) / previous_close) * 100
        price_source = "live_intraday"
        price_reliable_for_execution = True
    elif phase in {"after_hours", "pre_market"} and snap.get("available") and to_float(snap.get("current_price", 0)) > 0:
        current_price = to_float(snap.get("current_price", prev_price)) or prev_price
        open_price = to_float(snap.get("open", prev_open)) or prev_open
        previous_close = to_float(snap.get("previous_close", prev_price)) or prev_price
        change_from_open_pct = to_float(snap.get("change_from_open_pct", 0))
        change_vs_prev_close_pct = to_float(snap.get("change_vs_prev_close_pct", 0))
        price_source = phase
        price_reliable_for_execution = True
    elif phase == "closed":
        current_price = prev_price
        open_price = prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100
        price_source = "previous_close"
        price_reliable_for_execution = False
    elif snap.get("available") and to_float(snap.get("current_price", 0)) > 0:
        current_price = to_float(snap.get("current_price", prev_price)) or prev_price
        open_price = to_float(snap.get("open", prev_open)) or prev_open
        previous_close = to_float(snap.get("previous_close", prev_price)) or prev_price
        change_from_open_pct = to_float(snap.get("change_from_open_pct", 0))
        change_vs_prev_close_pct = to_float(snap.get("change_vs_prev_close_pct", 0))
        price_source = str(snap.get("source", "snapshot") or "snapshot")
        price_reliable_for_execution = False
    else:
        current_price = 0.0 if phase in {"open", "after_hours", "pre_market"} else prev_price
        open_price = prev_open
        previous_close = prev_price
        price_source = "unavailable_realtime"
        price_reliable_for_execution = False

    price_source_label_map = {
        "live_intraday": "مباشر أثناء التداول",
        "after_hours": "بعد الإغلاق",
        "pre_market": "قبل الافتتاح",
        "previous_close": "آخر إغلاق",
        "unavailable_realtime": "بيانات لحظية غير متاحة",
        "snapshot": "لقطة سوق",
        "minute+snapshot": "دقيقة + لقطة",
    }

    display_price = current_price if current_price > 0 else previous_close
    display_price_label = "السعر الحالي" if current_price > 0 else "آخر إغلاق"
    live_price_available = current_price > 0
    display_change_pct = change_vs_prev_close_pct if previous_close > 0 else change_from_open_pct
    display_change_available = abs(display_change_pct) > 0 or live_price_available

    high_live = prev_high
    low_live = prev_low
    volume_live = prev_volume
    last_price_update_ms = int(time.time() * 1000) if phase == "open" and intraday_data.get("available") else int(to_float(snap.get("updated", 0)))

    if phase == "open" and intraday_data.get("available"):
        high_live = safe_round(to_float(intraday_data.get("session_high", 0)) or prev_high)
        low_live = safe_round(to_float(intraday_data.get("session_low", 0)) or prev_low)
        volume_live = safe_round(to_float(intraday_data.get("session_volume", 0)) or prev_volume)
    else:
        high_live = safe_round(to_float(snap.get("high", prev_high)) or prev_high)
        low_live = safe_round(to_float(snap.get("low", prev_low)) or prev_low)
        volume_live = safe_round(to_float(snap.get("volume", prev_volume)) or prev_volume)

    return {
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "current_price_live": safe_round(current_price),
        "open_price_live": safe_round(open_price),
        "previous_close_live": safe_round(previous_close),
        "change_from_open_pct": safe_round(change_from_open_pct),
        "change_vs_prev_close_pct": safe_round(change_vs_prev_close_pct),
        "display_price": safe_round(display_price),
        "display_price_label": display_price_label,
        "display_change_pct": safe_round(display_change_pct),
        "display_change_available": display_change_available,
        "live_price_available": live_price_available,
        "high_live": high_live,
        "low_live": low_live,
        "volume_live": volume_live,
        "price_source": price_source,
        "price_source_label": price_source_label_map.get(price_source, price_source),
        "price_reliable_for_execution": price_reliable_for_execution,
        "last_price_update_ms": last_price_update_ms,
        "last_price_update_label": datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S"),
    }


def compute_volume_pace_ratio(intraday: dict, daily_volume_ratio: float) -> float:
    try:
        if not intraday or not intraday.get("available"):
            return float(daily_volume_ratio or 0)
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        latest_5m = float(intraday.get("latest_5m_volume", 0) or 0)
        avg_5m = float(intraday.get("avg_5m_volume", 0) or 0)
        pullback_volume_ratio = float(intraday.get("pullback_volume_ratio", 0) or 0)
        burst_ratio = (latest_5m / avg_5m) if avg_5m > 0 else intraday_ratio
        if pullback_volume_ratio > 0:
            return clamp(max(float(daily_volume_ratio or 0), intraday_ratio, burst_ratio, 1 / max(pullback_volume_ratio, 0.01) if pullback_volume_ratio < 1 else pullback_volume_ratio), 0.2, 8.0)
        return clamp(max(float(daily_volume_ratio or 0), intraday_ratio, burst_ratio), 0.2, 8.0)
    except:
        return float(daily_volume_ratio or 0)


def get_effective_volume_ratio(volume_ratio: float, intraday: dict) -> float:
    try:
        effective = float(volume_ratio or 0)
        if intraday and intraday.get("available"):
            intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
            pace_ratio = compute_volume_pace_ratio(intraday, volume_ratio)
            projected_bias = 0.0
            projected_day_volume = float(intraday.get("projected_day_volume", 0) or 0)
            session_volume = float(intraday.get("session_volume", 0) or 0)
            if projected_day_volume > 0 and session_volume > 0:
                projected_bias = projected_day_volume / max(session_volume, 1.0)
            effective = max(
                effective,
                intraday_ratio * 0.9,
                pace_ratio,
                min(max(float(volume_ratio or 0), 0.0) + (projected_bias * 0.02), 8.0)
            )
        return clamp(effective, 0.2, 8.0)
    except:
        return float(volume_ratio or 0)


