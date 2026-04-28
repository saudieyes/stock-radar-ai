from app.settings import REF_INFO_CACHE
import time
from app.utils import *
from app.utils import _cache_get, _cache_set
from app.market_data import *
from app.historical_engine import *
from app.market_sector_engine import *
from app.news_engine import *
from app.sharia_filter import *
from app.scoring_engine import *

print("FIX12_BREAKOUT_SOURCE_V2 loaded")

# Defensive local copy kept to avoid scan-wide failure if imports change.
# IMPORTANT: strategy/scoring logic expects INTERNAL values only:
# STRONG / WEAK / FAILED / N/A. Arabic wording belongs in display_contract/UI only.
def breakout_quality_label(trade_type: str, momentum: str, body_strength: float, close_strength: float, volume_ratio: float) -> str:
    try:
        trade_type_s = str(trade_type or "").strip().lower()
        momentum_s = str(momentum or "").strip()
        bs = float(body_strength or 0)
        cs = float(close_strength or 0)
        vr = float(volume_ratio or 0)

        is_breakout = trade_type_s == "breakout" or "breakout" in trade_type_s or "اختراق" in trade_type_s
        if not is_breakout:
            return "N/A"

        positive_momentum = momentum_s in {"صاعد", "صاعد قوي", "strong", "up", "bullish"} or "صاعد" in momentum_s

        if positive_momentum and bs >= 0.60 and cs >= 0.72 and vr >= 1.15:
            return "STRONG"
        if bs < 0.35 or cs < 0.50 or vr < 0.80:
            return "FAILED"
        return "WEAK"
    except Exception:
        return "WEAK"

try:
    from scanner import enrich_strategy_profile
except Exception:
    def enrich_strategy_profile(stock: dict) -> dict:
        return stock

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
            except:
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
    except:
        return None

def get_prev(symbol):
    try:
        r = http_get_json(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=12
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
    except:
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
    except:
        return {
            "available": False,
            "current_price": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0.0,
            "updated": 0,
        }

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

        change_vs_prev_close_pct = 0.0
        if current_price > 0 and prev_close > 0:
            change_vs_prev_close_pct = ((current_price - prev_close) / prev_close) * 100

        change_from_open_pct = 0.0
        if current_price > 0 and day_open > 0:
            change_from_open_pct = ((current_price - day_open) / day_open) * 100

        out = {
            "available": current_price > 0,
            "current_price": current_price,
            "previous_close": prev_close,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "volume": day_volume,
            "change_vs_prev_close_pct": change_vs_prev_close_pct,
            "change_from_open_pct": change_from_open_pct,
            "updated": int(time.time() * 1000),
            "source": "minute+snapshot" if minute.get("available") else "snapshot",
        }
        return _cache_set(SNAPSHOT_CACHE, cache_key, out, ttl)
    except:
        return {
            "available": False,
            "current_price": 0.0,
            "previous_close": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0.0,
            "change_vs_prev_close_pct": 0.0,
            "change_from_open_pct": 0.0,
            "updated": 0,
            "source": "error",
        }

STATIC_REFERENCE_INFO = {
    "AAPL": {"company": "Apple Inc", "sector": "Technology", "industry": "Consumer Electronics / Electronic Computers", "industry_id": ""},
    "MSFT": {"company": "Microsoft Corp", "sector": "Technology", "industry": "Software / Cloud", "industry_id": ""},
    "NVDA": {"company": "NVIDIA Corp", "sector": "Technology", "industry": "Semiconductors", "industry_id": ""},
    "AMD": {"company": "Advanced Micro Devices", "sector": "Technology", "industry": "Semiconductors", "industry_id": ""},
    "AVGO": {"company": "Broadcom Inc", "sector": "Technology", "industry": "Semiconductors", "industry_id": ""},
    "META": {"company": "Meta Platforms", "sector": "Communication Services", "industry": "Internet / Social Media", "industry_id": ""},
    "GOOGL": {"company": "Alphabet Inc", "sector": "Communication Services", "industry": "Internet / Search / Cloud", "industry_id": ""},
    "GOOG": {"company": "Alphabet Inc", "sector": "Communication Services", "industry": "Internet / Search / Cloud", "industry_id": ""},
    "AMZN": {"company": "Amazon.com Inc", "sector": "Consumer Discretionary", "industry": "Internet Retail / Cloud", "industry_id": ""},
    "TSLA": {"company": "Tesla Inc", "sector": "Consumer Discretionary", "industry": "Automobiles / EV", "industry_id": ""},
}


def _infer_sector_industry_from_text(text: str) -> tuple[str, str]:
    raw = str(text or "").strip()
    t = raw.lower()
    if not t:
        return "", ""

    # Technology / electronics / software / semis
    if any(x in t for x in [
        "electronic computer", "electronic computers", "computer", "consumer electronics",
        "software", "semiconductor", "semiconductors", "chip", "data processing", "cloud",
        "computer programming", "information technology", "hardware", "communications equipment"
    ]):
        return "Technology", raw

    if any(x in t for x in ["internet", "social media", "search", "streaming", "telecommunications", "telecom", "wireless"]):
        return "Communication Services", raw

    if any(x in t for x in ["retail", "restaurant", "automobile", "auto", "apparel", "travel", "hotel", "leisure"]):
        return "Consumer Discretionary", raw

    if any(x in t for x in ["food", "beverage", "grocery", "household", "tobacco", "personal care"]):
        return "Consumer Staples", raw

    if any(x in t for x in ["pharmaceutical", "biotech", "medical", "health", "drug", "diagnostic", "hospital"]):
        return "Healthcare", raw

    if any(x in t for x in ["bank", "insurance", "financial", "capital markets", "credit", "asset management"]):
        return "Financials", raw

    if any(x in t for x in ["oil", "gas", "energy", "drilling", "exploration", "petroleum"]):
        return "Energy", raw

    if any(x in t for x in ["aerospace", "defense", "machinery", "industrial", "transport", "airline", "logistics"]):
        return "Industrials", raw

    if any(x in t for x in ["chemical", "mining", "metals", "steel", "materials", "paper", "packaging"]):
        return "Materials", raw

    if any(x in t for x in ["utility", "electric", "water", "natural gas distribution"]):
        return "Utilities", raw

    if any(x in t for x in ["reit", "real estate", "property"]):
        return "Real Estate", raw

    return "", raw


def _merge_reference_info(primary: dict, fallback: dict) -> dict:
    return {
        "company": str(primary.get("company") or fallback.get("company") or "").strip(),
        "sector": str(primary.get("sector") or fallback.get("sector") or "").strip(),
        "industry": str(primary.get("industry") or fallback.get("industry") or "").strip(),
        "industry_id": str(primary.get("industry_id") or fallback.get("industry_id") or "").strip(),
    }


def get_reference_info(symbol):
    symbol = str(symbol).upper().strip()
    if not symbol:
        return {"company": "", "sector": "", "industry": "", "industry_id": ""}

    if symbol in REF_INFO_CACHE:
        cached = REF_INFO_CACHE[symbol]
        if cached.get("sector") or cached.get("industry"):
            return cached

    out = {"company": "", "sector": "", "industry": "", "industry_id": ""}
    try:
        url = f"https://api.polygon.io/v3/reference/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        res = r.get("results", {}) or {}
        sic_description = str(res.get("sic_description", "")).strip()
        name = str(res.get("name", "")).strip()
        inferred_sector, inferred_industry = _infer_sector_industry_from_text(sic_description)

        sector = inferred_sector
        industry = inferred_industry or sic_description
        if " - " in sic_description:
            parts = [p.strip() for p in sic_description.split(" - ") if p.strip()]
            if len(parts) >= 2:
                maybe_sector, maybe_industry = parts[0], parts[-1]
                inferred_sector2, inferred_industry2 = _infer_sector_industry_from_text(" ".join(parts))
                sector = inferred_sector2 or maybe_sector or sector
                industry = inferred_industry2 or maybe_industry or industry

        out = {"company": name, "sector": sector, "industry": industry, "industry_id": ""}
    except Exception:
        pass

    out = _merge_reference_info(out, STATIC_REFERENCE_INFO.get(symbol, {}))
    REF_INFO_CACHE[symbol] = out
    return out


def get_info(symbol):
    symbol = str(symbol).upper().strip()
    c = COMPANIES_DATA.get(symbol, {})

    def _row_first(row, names):
        norm = {str(k).lower().replace(" ", "").replace("_", ""): k for k in row.keys()}
        for name in names:
            if name in row and row.get(name) not in (None, ""):
                return row.get(name)
            key = str(name).lower().replace(" ", "").replace("_", "")
            if key in norm and row.get(norm[key]) not in (None, ""):
                return row.get(norm[key])
        return ""

    industry_id = str(_row_first(c, ["IndustryId", "Industry ID", "IndustryID", "industry_id"]) or "").strip()
    s = SECTOR_DATA.get(industry_id, {})

    company = str(_row_first(c, ["Company Name", "Company", "Name", "company_name"]) or "").strip()
    sector = str(s.get("sector", "")).strip()
    industry = str(s.get("industry", "")).strip()

    # Some company files contain sector/industry directly.
    sector = sector or str(_row_first(c, ["Sector", "sector", "SectorName"]) or "").strip()
    industry = industry or str(_row_first(c, ["Industry", "industry", "IndustryName", "SIC Description"]) or "").strip()

    inferred_sector, inferred_industry = _infer_sector_industry_from_text(" ".join([sector, industry]))
    if inferred_sector and (not sector or sector.lower() in {"unknown", "n/a", "none"}):
        sector = inferred_sector
    if inferred_industry and not industry:
        industry = inferred_industry

    ref = get_reference_info(symbol)
    merged = _merge_reference_info(
        {"company": company, "sector": sector, "industry": industry, "industry_id": industry_id},
        ref,
    )
    merged = _merge_reference_info(merged, STATIC_REFERENCE_INFO.get(symbol, {}))
    return merged

def _context_closes_from_bars(bars: list[dict]) -> list[float]:
    closes = []
    for row in bars or []:
        c = to_float((row or {}).get("c", 0))
        if c > 0:
            closes.append(c)
    return closes

def _context_return_pct(bars: list[dict], lookback: int = 20) -> float:
    closes = _context_closes_from_bars(bars)
    if len(closes) < lookback + 1:
        return 0.0
    start = float(closes[-(lookback + 1)] or 0)
    end = float(closes[-1] or 0)
    if start <= 0 or end <= 0:
        return 0.0
    return ((end - start) / start) * 100.0



def _sr_strength_label(touches: int, distance_pct: float = 999.0) -> str:
    try:
        touches = int(touches or 0)
        distance_pct = float(distance_pct or 999.0)
        if touches >= 5 and distance_pct <= 2.0:
            return "قوي جدًا"
        if touches >= 4:
            return "قوي"
        if touches >= 2:
            return "متوسط"
        return "ضعيف"
    except Exception:
        return "غير واضح"


def _count_level_touches(level: float, values: list[float], tolerance_pct: float) -> int:
    try:
        level = float(level or 0)
        if level <= 0:
            return 0
        tol = max(level * float(tolerance_pct or 0.01), 0.01)
        return sum(1 for v in values if v > 0 and abs(float(v) - level) <= tol)
    except Exception:
        return 0


def build_support_resistance_context(current_price: float, daily_bars: list[dict], intraday: dict, hist: dict | None = None) -> dict:
    """Build practical support/resistance references.

    This avoids random-looking entries/targets by linking the plan to nearby
    levels, level strength, and 52-week/ATH context.
    """
    hist = hist or {}
    try:
        price = float(current_price or 0)
        bars = list(daily_bars or [])[-260:]
        highs = [to_float(x.get("h")) for x in bars if to_float(x.get("h")) > 0]
        lows = [to_float(x.get("l")) for x in bars if to_float(x.get("l")) > 0]
        closes = [to_float(x.get("c")) for x in bars if to_float(x.get("c")) > 0]
        if price <= 0 or len(highs) < 10 or len(lows) < 10:
            return {
                "nearest_support": 0.0,
                "nearest_support_label": "غير متوفر",
                "nearest_support_strength": "غير واضح",
                "nearest_support_distance_pct": 0.0,
                "nearest_resistance": 0.0,
                "nearest_resistance_label": "غير متوفر",
                "nearest_resistance_strength": "غير واضح",
                "nearest_resistance_distance_pct": 0.0,
                "major_resistance": safe_round(hist.get("year_high", 0) or hist.get("ath_high", 0) or 0),
                "major_resistance_label": "غير متوفر",
                "levels_summary": "لا توجد بيانات كافية لبناء مستويات دعم/مقاومة موثوقة.",
            }

        # Add current session high/low because intraday levels matter for execution.
        session_high = float((intraday or {}).get("session_high", 0) or 0)
        session_low = float((intraday or {}).get("session_low", 0) or 0)
        if session_high > 0:
            highs.append(session_high)
        if session_low > 0:
            lows.append(session_low)

        all_level_values = highs + lows + closes
        tolerance = 0.012
        support_candidates = [x for x in lows + closes[-60:] if 0 < x < price * 0.999]
        resistance_candidates = [x for x in highs + closes[-60:] if x > price * 1.001]

        nearest_support = max(support_candidates) if support_candidates else 0.0
        nearest_resistance = min(resistance_candidates) if resistance_candidates else 0.0

        # Major resistance: next 52w/ATH level above price if available.
        year_high = float(hist.get("year_high", 0) or 0)
        ath_high = float(hist.get("ath_high", 0) or 0)
        major_res_candidates = [x for x in [year_high, ath_high, max(highs[-120:]) if highs else 0] if x > price * 1.003]
        major_resistance = min(major_res_candidates) if major_res_candidates else (max(highs) if highs else 0)

        support_dist = ((price - nearest_support) / price) * 100 if nearest_support > 0 and price > 0 else 0.0
        resistance_dist = ((nearest_resistance - price) / price) * 100 if nearest_resistance > 0 and price > 0 else 0.0
        support_touches = _count_level_touches(nearest_support, all_level_values[-180:], tolerance)
        resistance_touches = _count_level_touches(nearest_resistance, all_level_values[-180:], tolerance)
        support_strength = _sr_strength_label(support_touches, support_dist)
        resistance_strength = _sr_strength_label(resistance_touches, resistance_dist)

        if nearest_support > 0:
            support_label = f"{support_strength} - يبعد {safe_round(support_dist, 2)}% أسفل السعر"
        else:
            support_label = "غير متوفر"
        if nearest_resistance > 0:
            resistance_label = f"{resistance_strength} - يبعد {safe_round(resistance_dist, 2)}% فوق السعر"
        else:
            resistance_label = "لا توجد مقاومة قريبة واضحة"

        major_label = ""
        if ath_high > 0 and price >= ath_high * 0.995:
            major_label = "قرب/اختراق قمة تاريخية"
        elif year_high > 0 and price >= year_high * 0.97:
            major_label = "قريب من هاي 52 أسبوع"
        elif major_resistance > 0:
            major_label = f"المقاومة الأكبر التالية قرب {safe_round(major_resistance)}"
        else:
            major_label = "غير متوفر"

        summary_parts = []
        if nearest_support > 0:
            summary_parts.append(f"الدعم الأقرب {safe_round(nearest_support)} ({support_strength})")
        if nearest_resistance > 0:
            summary_parts.append(f"المقاومة الأقرب {safe_round(nearest_resistance)} ({resistance_strength})")
        if major_label:
            summary_parts.append(major_label)

        return {
            "nearest_support": safe_round(nearest_support),
            "nearest_support_label": support_label,
            "nearest_support_strength": support_strength,
            "nearest_support_touches": support_touches,
            "nearest_support_distance_pct": safe_round(support_dist, 2),
            "nearest_resistance": safe_round(nearest_resistance),
            "nearest_resistance_label": resistance_label,
            "nearest_resistance_strength": resistance_strength,
            "nearest_resistance_touches": resistance_touches,
            "nearest_resistance_distance_pct": safe_round(resistance_dist, 2),
            "major_resistance": safe_round(major_resistance),
            "major_resistance_label": major_label,
            "year_high": safe_round(year_high),
            "ath_high": safe_round(ath_high),
            "near_strong_support": bool(nearest_support > 0 and support_dist <= 2.0 and support_strength in {"قوي", "قوي جدًا"}),
            "near_strong_resistance": bool(nearest_resistance > 0 and resistance_dist <= 2.0 and resistance_strength in {"قوي", "قوي جدًا"}),
            "levels_summary": " | ".join(summary_parts) if summary_parts else "لا توجد مستويات واضحة.",
        }
    except Exception:
        return {
            "nearest_support": 0.0,
            "nearest_support_label": "غير متوفر",
            "nearest_support_strength": "غير واضح",
            "nearest_support_distance_pct": 0.0,
            "nearest_resistance": 0.0,
            "nearest_resistance_label": "غير متوفر",
            "nearest_resistance_strength": "غير واضح",
            "nearest_resistance_distance_pct": 0.0,
            "major_resistance": 0.0,
            "major_resistance_label": "غير متوفر",
            "levels_summary": "تعذر بناء مستويات الدعم والمقاومة.",
        }


def refine_plan_with_key_levels(trade_type: str, entry: float, stop: float, target1: float, target2: float, sr: dict, daily_bars: list[dict]) -> tuple[float, float, float, float, list[str]]:
    notes = []
    try:
        entry = float(entry or 0)
        stop = float(stop or 0)
        target1 = float(target1 or 0)
        target2 = float(target2 or 0)
        if entry <= 0:
            return entry, stop, target1, target2, notes

        atr = float(calculate_atr(daily_bars, 14) or 0)
        support = float(sr.get("nearest_support", 0) or 0)
        resistance = float(sr.get("nearest_resistance", 0) or 0)
        major_resistance = float(sr.get("major_resistance", 0) or 0)

        # Stop should ideally sit below a nearby support, not at an arbitrary
        # percentage. Only tighten if it remains below entry and reasonable.
        if support > 0 and support < entry:
            support_stop = support * 0.985
            if stop <= 0 or (support_stop > stop and support_stop < entry):
                stop = support_stop
                notes.append(f"الوقف حُسّن ليكون أسفل الدعم {safe_round(support)}")

        risk_unit = max(entry - stop, 0)
        min_target = entry + max(risk_unit * 1.35, atr * 1.2, entry * 0.018)

        # Target 1 should respect nearby resistance if it is above entry and
        # before the old optimistic target.
        if resistance > entry * 1.012 and resistance < target1:
            refined = resistance * 0.995
            if refined > entry:
                target1 = max(refined, min_target)
                notes.append(f"الهدف الأول رُبط بالمقاومة القريبة {safe_round(resistance)}")
        elif target1 <= entry and min_target > entry:
            target1 = min_target

        # Target 2 can reference a major 52w/ATH resistance if available.
        if major_resistance > target1 * 1.01 and major_resistance > entry:
            target2 = max(target2, major_resistance * 0.995)
            notes.append(f"الهدف الثاني يراعي المقاومة الأكبر {safe_round(major_resistance)}")
        elif target2 <= target1:
            target2 = target1 + max(risk_unit * 0.85, atr * 1.3, entry * 0.025)

        return entry, stop, target1, target2, notes[:4]
    except Exception:
        return entry, stop, target1, target2, notes

def trade_plan_pro(symbol, manual_sharia_exclusions=None, manual_sharia_approvals=None):
    daily_bars = get_daily_bars(symbol)
    prev = get_prev_from_daily_bars(daily_bars) or get_prev(symbol)
    if not prev:
        return None

    info = get_info(symbol)
    financials = get_financials(symbol, prev)
    hist = get_history_levels(symbol, prev, daily_bars)
    trend_data = get_trend(symbol, daily_bars)
    market_sector_context = get_market_sector_context(symbol, info.get("sector", ""), info.get("industry", ""), daily_bars)
    intraday = get_intraday_snapshot(symbol)
    volume_ratio = get_volume_ratio(symbol, intraday, daily_bars)
    # Speed: do not fetch news before the stock proves it is technically worth deeper analysis.
    # This does not cache live price/quote/intraday data; it only avoids expensive news calls for weak watch names.
    news_bundle = empty_news_bundle()
    news_fetch_skipped = True
    news_note = news_bundle.get("news_note", "لا يوجد خبر حديث")
    catalyst_score = 0

    sharia_assessment = assess_sharia(
        symbol,
        info["sector"], info["industry"],
        financials["total_assets"], financials["cash"], financials["total_debt"],
        manual_sharia_exclusions,
        manual_sharia_approvals,
    )
    halal_ok = bool(sharia_assessment.get("is_halal", True))
    halal_reason = str(sharia_assessment.get("reason", "") or "")

    if sharia_assessment.get("should_block", False):
        phase = get_market_phase()
        current_price = float(prev.get("price", 0) or 0)
        exclusion_decision = "مستبعد يدويًا" if sharia_assessment.get("manual_excluded") else "مرفوض شرعياً"
        owner_text = "↩️ يمكنك إعادة السهم يدويًا إذا رغبت" if sharia_assessment.get("manual_excluded") else "تجنب السهم"
        return {
            "symbol": symbol,
            "type": "Excluded",
            "decision": exclusion_decision,
            "entry": 0,
            "stop_loss": 0,
            "target_1": 0,
            "target_2": 0,
            "risk_pct": 0,
            "quality_score": 0,
            "rank_label": "-",
            "valid_for": "-",
            "trend": trend_data["trend"],
            "volume_ratio": volume_ratio,
            "effective_volume_ratio": volume_ratio,
            "data_quality": "high",
            "catalyst_score": catalyst_score,
            "news_note": news_note,
            "news_title": news_bundle.get("news_title", ""),
            "news_badge": news_bundle.get("news_badge", ""),
            "news_category": news_bundle.get("news_category", "neutral"),
            "news_sentiment": news_bundle.get("news_sentiment", "neutral"),
            "news_scope": news_bundle.get("news_scope", "neutral"),
            "news_scope_label": news_bundle.get("news_scope_label", news_scope_label("neutral")),
            "news_context_note": news_bundle.get("news_context_note", ""),
            "display_price": safe_round(current_price),
            "current_price_live": safe_round(current_price),
            "market_phase": phase,
            "market_phase_label": market_phase_label(phase),
            "benchmark_symbol": "SPY",
            "market_support_label": "غير واضح",
            "sector_etf_symbol": "",
            "sector_support_label": "غير متوفر",
            "market_sector_score": 0,
            "market_sector_alignment_label": "محايد",
            "market_sector_alignment_detail": "لا توجد بيانات كافية عن المؤشر والقطاع.",
            "historical_behavior_score": 50.0,
            "historical_context_score": 50.0,
            "historical_context_label": "محايد",
            "historical_context_detail": "لا توجد بيانات كافية لربط السهم بالمؤشر والقطاع.",
            "price_source_label": "آخر إغلاق",
            "display_entry_label": "—",
            "display_entry_price": 0,
            "display_target_label": "—",
            "display_target_price": 0,
            "display_stop_label": "—",
            "display_stop_price": 0,
            "risk_flags": [halal_reason],
            "ai_summary": halal_reason,
            "breakout_quality": "N/A",
            "execution_status": "AVOID",
            "owner_action": owner_text,
            "company": info["company"],
            "sector": info["sector"],
            "industry": info["industry"],
            "financials": financials,
            "sharia_status": sharia_assessment.get("status", "non_compliant"),
            "sharia_label": sharia_assessment.get("label", "غير متوافق"),
            "sharia_reason": halal_reason,
            "sharia_manual_excluded": bool(sharia_assessment.get("manual_excluded", False)),
            "sharia_is_gray": bool(sharia_assessment.get("is_gray", False)),
        }

    live_block = build_live_price_block(symbol, prev, intraday)
    atr_overlay = get_atr_overlay(prev.get("price", 0), daily_bars)
    current_price = live_block["current_price_live"] if live_block["current_price_live"] > 0 else prev["price"]
    high = max(prev["high"], live_block["high_live"] if live_block["high_live"] > 0 else prev["high"])
    low = min(prev["low"], live_block["low_live"] if live_block["low_live"] > 0 else prev["low"])
    sr_context = build_support_resistance_context(current_price, daily_bars, intraday, hist)

    pullback_context = compute_pullback_context(current_price, high, low, intraday, trend_data["trend"])
    trade_type = "Pullback" if pullback_context.get("pullback_candidate") else "Breakout"
    historical_behavior = analyze_historical_behavior(daily_bars, trade_type)
    historical_context = analyze_historical_context_behavior(
        daily_bars,
        market_sector_context.get("benchmark_symbol", ""),
        market_sector_context.get("sector_etf_symbol", ""),
        trade_type,
        market_sector_context.get("market_support_score", 0),
        market_sector_context.get("sector_support_score", 0),
    )

    price_penalty, price_flag = dynamic_price_penalty(current_price, trade_type)
    volume_pace_ratio = compute_volume_pace_ratio(intraday, volume_ratio)
    effective_volume_ratio = get_effective_volume_ratio(volume_ratio, intraday)

    if trade_type == "Breakout":
        entry = high * 1.01
        stop = high * 0.95
        target1 = high * 1.07
        target2 = high * 1.10
    else:
        fib_50 = float(pullback_context.get("fib_50", 0) or 0)
        fib_62 = float(pullback_context.get("fib_62", 0) or 0)
        zone_high = float(pullback_context.get("pullback_zone_high", 0) or 0)
        entry = fib_50 if fib_50 > 0 else current_price
        if current_price > 0 and zone_high > 0 and current_price < zone_high:
            entry = max(current_price, fib_62 if fib_62 > 0 else current_price)
        stop = min(low * 0.985, fib_62 * 0.985) if fib_62 > 0 and low > 0 else low * 0.97
        target1 = max(high * 0.995, entry * 1.04)
        target2 = max(high * 1.02, entry * 1.08)

    entry, stop, target1, target2, level_refinement_notes = refine_plan_with_key_levels(
        trade_type, entry, stop, target1, target2, sr_context, daily_bars
    )
    risk_pct = ((entry - stop) / entry) * 100 if entry > 0 else 0

    breakout_quality = breakout_quality_label(
        trade_type,
        "صاعد" if trend_data["trend"] in ["صاعد", "صاعد قوي"] else trend_data["trend"],
        0.7,
        0.75,
        effective_volume_ratio,
    )

    pullback_score = int(pullback_context.get("pullback_score", 0) or 0)

    preliminary_core_quality = compute_core_quality_score(
        trend_data["trend"],
        effective_volume_ratio,
        0,
        hist,
        breakout_quality,
        pullback_score,
        trade_type,
        price_penalty,
        risk_pct,
        "neutral",
        "neutral",
        999,
        market_sector_context.get("market_sector_score", 0),
        historical_behavior.get("historical_behavior_score", 50),
        historical_context.get("historical_context_score", 50),
    )

    should_fetch_news = (
        preliminary_core_quality >= 58
        or effective_volume_ratio >= 1.25
        or (trend_data["trend"] in {"صاعد", "صاعد قوي"} and breakout_quality in {"STRONG", "WEAK"})
    )
    if should_fetch_news:
        news_bundle = get_news_bundle(symbol, info["company"], info.get("sector", ""), info.get("industry", ""))
        news_fetch_skipped = False
        news_note = news_bundle.get("news_note", "لا يوجد خبر حديث")
        catalyst_score = news_bundle.get("catalyst_score", 0)

    core_quality = compute_core_quality_score(
        trend_data["trend"],
        effective_volume_ratio,
        catalyst_score,
        hist,
        breakout_quality,
        pullback_score,
        trade_type,
        price_penalty,
        risk_pct,
        news_bundle.get("news_scope", "neutral"),
        news_bundle.get("news_sentiment", news_bundle.get("news_category", "neutral")),
        news_bundle.get("news_sessions_since", 999),
        market_sector_context.get("market_sector_score", 0),
        historical_behavior.get("historical_behavior_score", 50),
        historical_context.get("historical_context_score", 50),
    )
    quality = core_quality
    rank_label = make_rank_label(quality)

    rr_1_preview = 0.0
    if entry > 0 and stop > 0 and target1 > 0 and entry > stop:
        rr_1_preview = (target1 - entry) / (entry - stop) if (entry - stop) > 0 else 0.0

    decision = "مراقبة"
    if quality >= 82 and rr_1_preview >= 0.75 and risk_pct <= 8.5 and breakout_quality != "FAILED":
        decision = "دخول قوي"
    elif quality >= 66 and risk_pct <= 12 and breakout_quality != "FAILED":
        decision = "دخول بحذر"

    decision = apply_news_decision_guard(
        decision,
        news_bundle.get("news_scope", "neutral"),
        news_bundle.get("news_sentiment", news_bundle.get("news_category", "neutral")),
        news_bundle.get("news_sessions_since", 999),
        quality,
    )
    decision = apply_market_sector_decision_guard(
        decision,
        market_sector_context.get("market_sector_score", 0),
        market_sector_context.get("market_support_label", ""),
        market_sector_context.get("sector_support_label", ""),
        market_sector_context.get("sector_etf_symbol", ""),
    )
    if decision == "دخول قوي" and historical_behavior.get("historical_behavior_score", 50) < 42 and historical_context.get("historical_context_score", 50) < 44 and str(historical_behavior.get("historical_confidence_label", "") or "") in {"متوسطة", "عالية"}:
        decision = "دخول بحذر"

    execution_status = compute_execution_status(
        trade_type, decision, trend_data["trend"], effective_volume_ratio, catalyst_score, breakout_quality
    )
    owner_action_text = owner_decision(decision, trend_data["trend"], breakout_quality, effective_volume_ratio, catalyst_score)
    valid_for = estimate_validity(trade_type, trend_data["trend"], effective_volume_ratio, catalyst_score)

    risk_flags = []
    if price_flag:
        risk_flags.append(price_flag)
    if hist["near_ath"]:
        risk_flags.append("قريب من القمة التاريخية")
    if hist["ath_breakout_zone"]:
        risk_flags.append("منطقة اختراق قمة تاريخية")
    news_scope = str(news_bundle.get("news_scope", "neutral") or "neutral")
    news_sentiment = str(news_bundle.get("news_sentiment", "neutral") or "neutral")
    if catalyst_score > 0:
        if news_scope == "company":
            risk_flags.append("خبر شركة داعم")
        elif news_scope == "sector":
            risk_flags.append("خبر قطاعي داعم")
    elif catalyst_score < 0:
        if news_sentiment == "legal" and news_scope == "company":
            risk_flags.append("خبر قانوني مباشر")
        elif news_scope == "company":
            risk_flags.append("خبر شركة سلبي")
        elif news_scope == "sector":
            risk_flags.append("خبر قطاعي ضاغط")
        elif news_scope == "market":
            risk_flags.append("سياق سوق عام ضاغط")
    if str(market_sector_context.get("market_support_label", "") or "") in {"ضاغط", "ضاغط قوي"}:
        risk_flags.append(f"المؤشر {market_sector_context.get('benchmark_symbol', 'SPY')} {market_sector_context.get('market_support_label', '')}")
    if str(market_sector_context.get("sector_support_label", "") or "") in {"ضاغط", "ضاغط قوي"}:
        risk_flags.append(f"القطاع {market_sector_context.get('sector_support_label', '')}")
    if not str(market_sector_context.get("sector_etf_symbol", "") or ""):
        risk_flags.append("ETF القطاع غير متوفر: الثقة أقل قليلًا")
    if sharia_assessment.get("is_gray"):
        risk_flags.append("الحكم الشرعي غير محسوم")
    if info["sector"] == "":
        risk_flags.append("بيانات القطاع/الصناعة ناقصة")
    if financials["total_assets"] <= 0:
        risk_flags.append("إجمالي الأصول غير متوفر")
    if financials["shares"] <= 0:
        risk_flags.append("عدد الأسهم غير متوفر")
    if financials["approx_market_cap"] <= 0:
        risk_flags.append("القيمة السوقية التقريبية غير متوفرة")
    if intraday.get("market_open") and intraday.get("intraday_volume_ratio", 0) >= 1.5:
        risk_flags.append("سيولة لحظية قوية")
    if breakout_quality == "FAILED":
        risk_flags.append("سلوك اختراق فاشل")
    if trade_type == "Pullback" and not pullback_context.get("in_pullback_zone"):
        risk_flags.append("الارتداد خارج المنطقة المثالية")
    try:
        if sr_context.get("near_strong_resistance"):
            risk_flags.append(f"قريب من مقاومة {sr_context.get('nearest_resistance_strength', '')}: {sr_context.get('nearest_resistance')}")
        if sr_context.get("near_strong_support"):
            risk_flags.append(f"قريب من دعم {sr_context.get('nearest_support_strength', '')}: {sr_context.get('nearest_support')}")
        for note in level_refinement_notes:
            risk_flags.append(note)
    except Exception:
        pass

    ai_summary_parts = [
        f"الاتجاه {trend_data['trend']}",
        f"السيولة {'مرتفعة' if effective_volume_ratio >= 1.2 else 'ضعيفة' if effective_volume_ratio < 0.9 else 'متوسطة'}",
    ]
    if intraday.get("market_open"):
        ai_summary_parts.append(f"افتتاح اليوم: {intraday.get('opening_drive', 'unknown')}")
        if intraday.get("above_vwap_proxy"):
            ai_summary_parts.append("فوق VWAP اللحظي")
        if intraday.get("intraday_volume_ratio", 0) >= 1.2:
            ai_summary_parts.append("السيولة اللحظية داعمة")
    if market_sector_context.get("market_support_label"):
        ai_summary_parts.append(f"المؤشر {market_sector_context.get('benchmark_symbol', 'SPY')}: {market_sector_context.get('market_support_label')}")
    if market_sector_context.get("sector_etf_symbol"):
        ai_summary_parts.append(f"القطاع {market_sector_context.get('sector_etf_symbol')}: {market_sector_context.get('sector_support_label')}")
    if trade_type == "Pullback" and pullback_context.get("pullback_pattern_label"):
        ai_summary_parts.append(str(pullback_context.get("pullback_pattern_label")))
    if catalyst_score > 0:
        if news_scope == "company":
            ai_summary_parts.append("يوجد محفز شركة إيجابي")
        elif news_scope == "sector":
            ai_summary_parts.append("يوجد دعم قطاعي")
    elif catalyst_score < 0:
        if news_sentiment == "legal" and news_scope == "company":
            ai_summary_parts.append("يوجد ضغط قانوني مباشر")
        elif news_scope == "company":
            ai_summary_parts.append("يوجد خبر شركة سلبي")
        elif news_scope == "sector":
            ai_summary_parts.append("يوجد خبر قطاعي ضاغط")
        elif news_scope == "market":
            ai_summary_parts.append("السياق العام ضاغط")
    elif news_scope == "market":
        ai_summary_parts.append("سياق سوق عام فقط")
    if hist["ath_breakout_zone"]:
        ai_summary_parts.append("في منطقة قمة تاريخية")
    try:
        if sr_context.get("levels_summary"):
            ai_summary_parts.append(str(sr_context.get("levels_summary")))
    except Exception:
        pass
    if breakout_quality == "FAILED":
        ai_summary_parts.append("شمعة الاختراق فشلت")
    elif breakout_quality == "STRONG":
        ai_summary_parts.append("اختراق قوي")

    if sharia_assessment.get("is_gray"):
        ai_summary_parts.append("الحكم الشرعي غير محسوم")
    if info["sector"] == "" or financials["total_assets"] <= 0 or financials["shares"] <= 0:
        ai_summary_parts.append("جودة البيانات ضعيفة")
    elif financials["approx_market_cap"] <= 0:
        ai_summary_parts.append("جودة البيانات متوسطة")

    data_quality = "low" if (info["sector"] == "" or financials["total_assets"] <= 0 or financials["shares"] <= 0) else ("medium" if financials["approx_market_cap"] <= 0 else "high")

    levels = compute_breakout_levels(live_block["current_price_live"], high, low, intraday, trade_type, pullback_context)
    timing = compute_timing_layer(live_block["current_price_live"], intraday, effective_volume_ratio, levels, live_block.get("market_phase", "closed"))

    plan = {
        "symbol": symbol,
        "type": trade_type,
        "decision": decision,
        "entry": safe_round(entry),
        "stop_loss": safe_round(stop),
        "target_1": safe_round(target1),
        "target_2": safe_round(target2),
        "risk_pct": safe_round(risk_pct),
        "quality_score": quality,
        "quality_core_score": core_quality,
        "execution_layer_score": 50,
        "execution_layer_label": "محايد",
        "execution_layer_adjustment": 0,
        "price_penalty_points": price_penalty,
        "rank_label": rank_label,
        "valid_for": valid_for,
        "trend": trend_data["trend"],
        "volume_ratio": safe_round(volume_ratio),
        "volume_pace_ratio": safe_round(volume_pace_ratio),
        "effective_volume_ratio": safe_round(effective_volume_ratio),
        "data_quality": data_quality,
        "catalyst_score": catalyst_score,
        "news_note": news_note,
        "news_title": news_bundle.get("news_title", ""),
        "news_badge": news_bundle.get("news_badge", ""),
        "news_category": news_bundle.get("news_category", "neutral"),
        "news_sentiment": news_bundle.get("news_sentiment", "neutral"),
        "news_scope": news_bundle.get("news_scope", "neutral"),
        "news_scope_label": news_bundle.get("news_scope_label", news_scope_label("neutral")),
        "news_effect_score": news_bundle.get("news_effect_score", 0),
        "news_is_catalyst": news_bundle.get("news_is_catalyst", False),
        "news_context_note": news_bundle.get("news_context_note", ""),
        "news_related_tickers_count": news_bundle.get("news_related_tickers_count", 0),
        "news_freshness_label": news_bundle.get("news_freshness_label", ""),
        "news_published_utc": news_bundle.get("news_published_utc", ""),
        "news_sessions_since": news_bundle.get("news_sessions_since", 999),
        "news_fetch_skipped": bool(news_fetch_skipped),
        "risk_flags": risk_flags,
        "ai_summary": " - ".join(ai_summary_parts),
        "breakout_quality": breakout_quality,
        "execution_status": execution_status,
        "owner_action": owner_action_text,
        "intraday": intraday,
        **pullback_context,
        **sr_context,
        "level_refinement_notes": level_refinement_notes,
        **levels,
        **timing,
        **live_block,
        **atr_overlay,
        **historical_behavior,
        **historical_context,
        **market_sector_context,
        "company": info["company"],
        "sector": info["sector"],
        "industry": info["industry"],
        "financials": financials,
        "sharia_status": sharia_assessment.get("status", "compliant"),
        "sharia_label": sharia_assessment.get("label", "متوافق مبدئيًا"),
        "sharia_reason": halal_reason,
        "sharia_manual_excluded": bool(sharia_assessment.get("manual_excluded", False)),
        "sharia_is_gray": bool(sharia_assessment.get("is_gray", False)),
    }
    plan = enrich_strategy_profile(plan)
    plan["rr_1"] = safe_round(rr_1_preview)
    plan = apply_decision_layers(plan)
    try:
        safety_reasons = [str(x) for x in (plan.get("safety_gate_reasons") or []) if str(x).strip()]
        if safety_reasons:
            existing_flags = list(plan.get("risk_flags") or [])
            for r in safety_reasons:
                flag = f"بوابة أمان: {r}"
                if flag not in existing_flags:
                    existing_flags.append(flag)
            plan["risk_flags"] = existing_flags
            current_summary = str(plan.get("ai_summary", "") or "")
            safety_summary = " | ".join(safety_reasons[:3])
            if safety_summary and safety_summary not in current_summary:
                plan["ai_summary"] = (current_summary + " - " if current_summary else "") + "بوابة الأمان: " + safety_summary
    except Exception:
        pass
    return plan

