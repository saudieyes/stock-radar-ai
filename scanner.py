import os
import requests
from datetime import datetime, timedelta

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

EXCLUDED_TYPES = {
    "ETF", "ETN", "ETV", "WARRANT", "RIGHT", "UNIT",
    "PREFERRED", "FUND", "TRUST", "INDEX", "SPAC"
}

EXCLUDED_SUFFIXES = (
    "W", "WS", "WT", "R", "U"
)

# =========================
# CONFIG
# =========================
MIN_PRICE = 1.5
MIN_VOLUME = 300_000
MIN_DOLLAR_VOLUME = 3_000_000
MAX_RANGE_PCT = 0.45
MIN_RANGE_PCT = 0.015

TOTAL_UNIVERSE = 60
BIG_CAP_LIMIT = 5
MOMENTUM_LIMIT = 35
EMERGING_LIMIT = 20

BIG_CAPS = [
    "AAPL", "NVDA", "MSFT", "AMZN", "META",
    "GOOGL", "TSLA", "AMD", "AVGO", "NFLX"
]


# =========================
# HELPERS
# =========================
def safe_get_json(url: str, timeout: int = 20):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except:
        return {}


def previous_business_day() -> str:
    d = datetime.utcnow().date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def is_clean_common_stock(item: dict) -> bool:
    ticker = str(item.get("ticker", "")).upper().strip()
    name = str(item.get("name", "")).upper().strip()
    market = str(item.get("market", "")).upper().strip()
    locale = str(item.get("locale", "")).lower().strip()
    active = item.get("active", False)
    type_ = str(item.get("type", "")).upper().strip()

    if not ticker:
        return False
    if not active:
        return False
    if locale != "us":
        return False
    if market != "STOCKS":
        return False
    if type_ in EXCLUDED_TYPES:
        return False

    for suf in EXCLUDED_SUFFIXES:
        if ticker.endswith(suf):
            return False

    bad_words = [
        "ETF", "TRUST", "FUND", "WARRANT", "RIGHT", "UNIT",
        "PREFERRED", "DEPOSITARY", "ADR", "SPAC"
    ]
    if any(word in name for word in bad_words):
        return False

    return True


def get_reference_tickers(limit_pages: int = 12, page_limit: int = 1000) -> list[str]:
    if not POLYGON_API_KEY:
        return []

    base_url = "https://api.polygon.io/v3/reference/tickers"
    params = {
        "market": "stocks",
        "active": "true",
        "limit": page_limit,
        "apiKey": POLYGON_API_KEY,
    }

    tickers = []
    next_url = None
    pages_read = 0

    while pages_read < limit_pages:
        if next_url:
            data = safe_get_json(next_url)
        else:
            query = "&".join([f"{k}={v}" for k, v in params.items()])
            data = safe_get_json(f"{base_url}?{query}")

        results = data.get("results", [])
        for item in results:
            if is_clean_common_stock(item):
                ticker = str(item.get("ticker", "")).upper().strip()
                if ticker:
                    tickers.append(ticker)

        next_url = data.get("next_url")
        if next_url and "apiKey=" not in next_url:
            next_url = f"{next_url}&apiKey={POLYGON_API_KEY}"

        pages_read += 1
        if not next_url:
            break

    seen = set()
    cleaned = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            cleaned.append(t)

    return cleaned


def get_grouped_daily_map(date_str: str) -> dict:
    if not POLYGON_API_KEY:
        return {}

    url = (
        f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/"
        f"{date_str}?adjusted=true&apiKey={POLYGON_API_KEY}"
    )
    data = safe_get_json(url, timeout=30)

    out = {}
    for item in data.get("results", []):
        ticker = str(item.get("T", "")).upper().strip()
        if not ticker:
            continue

        close_price = float(item.get("c", 0) or 0)
        open_price = float(item.get("o", 0) or 0)
        high_price = float(item.get("h", 0) or 0)
        low_price = float(item.get("l", 0) or 0)
        volume = float(item.get("v", 0) or 0)

        out[ticker] = {
            "price": close_price,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "volume": volume,
        }

    return out


# =========================
# SCORING
# =========================
def base_filters(d: dict) -> bool:
    price = float(d.get("price", 0) or 0)
    open_price = float(d.get("open", 0) or 0)
    high = float(d.get("high", 0) or 0)
    low = float(d.get("low", 0) or 0)
    volume = float(d.get("volume", 0) or 0)

    if price <= 0 or high <= 0 or low <= 0 or volume <= 0:
        return False

    dollar_volume = price * volume
    day_range = max(high - low, 0.0001)
    range_pct = day_range / price if price > 0 else 0
    day_change_pct = ((price - open_price) / open_price) if open_price > 0 else 0

    if price < MIN_PRICE:
        return False
    if volume < MIN_VOLUME:
        return False
    if dollar_volume < MIN_DOLLAR_VOLUME:
        return False
    if range_pct > MAX_RANGE_PCT:
        return False
    if range_pct < MIN_RANGE_PCT:
        return False
    if day_change_pct < -0.08:
        return False

    return True


def calc_metrics(d: dict) -> dict:
    price = float(d.get("price", 0) or 0)
    open_price = float(d.get("open", 0) or 0)
    high = float(d.get("high", 0) or 0)
    low = float(d.get("low", 0) or 0)
    volume = float(d.get("volume", 0) or 0)

    dollar_volume = price * volume
    day_range = max(high - low, 0.0001)
    range_pct = day_range / price if price > 0 else 0
    day_change_pct = ((price - open_price) / open_price) if open_price > 0 else 0
    gap_pct = day_change_pct  # تقريب يومي لحين إضافة premarket الحقيقي
    near_high = price >= high * 0.985 if high > 0 else False
    close_strength = (price - low) / day_range if day_range > 0 else 0
    body_strength = abs(price - open_price) / day_range if day_range > 0 else 0

    return {
        "price": price,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": volume,
        "dollar_volume": dollar_volume,
        "range_pct": range_pct,
        "day_change_pct": day_change_pct,
        "gap_pct": gap_pct,
        "near_high": near_high,
        "close_strength": close_strength,
        "body_strength": body_strength,
    }


def score_big_cap(ticker: str, d: dict) -> float:
    m = calc_metrics(d)
    score = 0.0

    if ticker not in BIG_CAPS:
        return -9999

    if m["dollar_volume"] > 1_000_000_000:
        score += 25
    elif m["dollar_volume"] > 500_000_000:
        score += 18
    else:
        score += 10

    if m["day_change_pct"] > 0.03:
        score += 12
    elif m["day_change_pct"] > 0.015:
        score += 8
    elif m["day_change_pct"] > 0:
        score += 4
    elif m["day_change_pct"] < -0.03:
        score -= 10

    if m["near_high"]:
        score += 10

    if m["close_strength"] > 0.7:
        score += 8
    elif m["close_strength"] < 0.4:
        score -= 6

    if 0.02 <= m["range_pct"] <= 0.08:
        score += 5

    return score


def score_momentum_candidate(ticker: str, d: dict) -> float:
    m = calc_metrics(d)
    score = 0.0

    if not base_filters(d):
        return -9999

    # نبعد الكبار عن هذا السلة حتى لا يهيمنوا
    if ticker in BIG_CAPS:
        score -= 8

    # حركة اليوم
    if m["day_change_pct"] > 0.10:
        score += 35
    elif m["day_change_pct"] > 0.07:
        score += 28
    elif m["day_change_pct"] > 0.04:
        score += 18
    elif m["day_change_pct"] > 0.02:
        score += 10
    elif m["day_change_pct"] < -0.02:
        return -9999

    # gap / early momentum
    if m["gap_pct"] > 0.15:
        score += 25
    elif m["gap_pct"] > 0.08:
        score += 18
    elif m["gap_pct"] > 0.04:
        score += 10

    # volume / dollar volume
    if m["volume"] > 50_000_000:
        score += 22
    elif m["volume"] > 15_000_000:
        score += 16
    elif m["volume"] > 5_000_000:
        score += 10
    elif m["volume"] > 1_000_000:
        score += 5

    if m["dollar_volume"] > 300_000_000:
        score += 14
    elif m["dollar_volume"] > 100_000_000:
        score += 10
    elif m["dollar_volume"] > 30_000_000:
        score += 6

    # price action quality
    if m["near_high"]:
        score += 14
    if m["close_strength"] > 0.75:
        score += 12
    elif m["close_strength"] > 0.6:
        score += 6
    elif m["close_strength"] < 0.35:
        score -= 8

    if m["body_strength"] > 0.55:
        score += 8
    elif m["body_strength"] < 0.2:
        score -= 5

    # sweet spot in range
    if 0.03 <= m["range_pct"] <= 0.20:
        score += 8
    elif m["range_pct"] > 0.30:
        score -= 6

    return score


def score_emerging_candidate(ticker: str, d: dict) -> float:
    m = calc_metrics(d)
    score = 0.0

    if not base_filters(d):
        return -9999

    # نفضل mid/small أكثر
    if ticker in BIG_CAPS:
        score -= 12

    # حركة مبكرة قبل الانفجار الكامل
    if m["day_change_pct"] > 0.06:
        score += 18
    elif m["day_change_pct"] > 0.03:
        score += 14
    elif m["day_change_pct"] > 0.015:
        score += 9
    elif m["day_change_pct"] > 0:
        score += 4
    elif m["day_change_pct"] < -0.03:
        return -9999

    # volume كافٍ لكن ليس لازم ضخم جدًا
    if m["volume"] > 20_000_000:
        score += 14
    elif m["volume"] > 5_000_000:
        score += 10
    elif m["volume"] > 1_500_000:
        score += 6
    else:
        score += 2

    if m["dollar_volume"] > 150_000_000:
        score += 10
    elif m["dollar_volume"] > 40_000_000:
        score += 7
    elif m["dollar_volume"] > 10_000_000:
        score += 4

    # near breakout behavior
    if m["near_high"]:
        score += 12
    if m["close_strength"] > 0.7:
        score += 8
    elif m["close_strength"] < 0.4:
        score -= 5

    if 0.02 <= m["range_pct"] <= 0.15:
        score += 8
    elif m["range_pct"] > 0.25:
        score -= 4

    if m["gap_pct"] > 0.03:
        score += 6

    return score


def unique_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for t in items:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def get_seed_universe() -> list[str]:
    return [
        "AAPL", "NVDA", "MSFT", "AMZN", "META",
        "NFLX", "AMD", "TSLA", "GOOGL", "AVGO",
        "SMCI", "PLTR", "HIMS", "MARA", "RIOT",
        "OKLO", "ASTS", "NIO", "RKLB", "MRVL",
        "SLNO", "AEHR", "LWLG", "OPTX", "CLNN",
        "SOUN", "AI", "IONQ", "TEM", "APP",
        "CRWD", "PANW", "ANET", "SNOW", "SHOP",
        "TTD", "PATH", "ROKU", "COIN", "UBER"
    ]


def get_scan_universe(max_symbols: int = TOTAL_UNIVERSE) -> list[str]:
    reference_tickers = get_reference_tickers(limit_pages=12, page_limit=1000)
    if not reference_tickers:
        return get_seed_universe()[:max_symbols]

    market_date = previous_business_day()
    grouped_map = get_grouped_daily_map(market_date)
    if not grouped_map:
        return get_seed_universe()[:max_symbols]

    big_caps_scored = []
    momentum_scored = []
    emerging_scored = []

    for ticker in reference_tickers:
        daily = grouped_map.get(ticker)
        if not daily:
            continue

        s_big = score_big_cap(ticker, daily)
        if s_big != -9999:
            big_caps_scored.append((ticker, s_big))

        s_momo = score_momentum_candidate(ticker, daily)
        if s_momo != -9999:
            momentum_scored.append((ticker, s_momo))

        s_emg = score_emerging_candidate(ticker, daily)
        if s_emg != -9999:
            emerging_scored.append((ticker, s_emg))

    big_caps_scored.sort(key=lambda x: x[1], reverse=True)
    momentum_scored.sort(key=lambda x: x[1], reverse=True)
    emerging_scored.sort(key=lambda x: x[1], reverse=True)

    big_caps_final = [t for t, _ in big_caps_scored[:BIG_CAP_LIMIT]]
    momentum_final = [t for t, _ in momentum_scored[:MOMENTUM_LIMIT]]
    emerging_final = [t for t, _ in emerging_scored[:EMERGING_LIMIT]]

    final_universe = unique_keep_order(big_caps_final + momentum_final + emerging_final)

    if not final_universe:
        return get_seed_universe()[:max_symbols]

    return final_universe[:max_symbols]
