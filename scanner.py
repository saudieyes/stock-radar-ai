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

MIN_PRICE = 2.0
MIN_VOLUME = 1_500_000
MIN_DOLLAR_VOLUME = 20_000_000
MAX_RANGE_PCT = 0.18
MIN_RANGE_PCT = 0.015


def safe_get_json(url: str, timeout: int = 20):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except:
        return {}


def previous_business_day() -> str:
    d = datetime.utcnow().date() - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday/Sunday
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
    """
    يجلب بيانات مجمعة لكل السوق في آخر جلسة متاحة.
    """
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


def score_candidate(ticker: str, d: dict) -> float:
    price = float(d.get("price", 0) or 0)
    open_price = float(d.get("open", 0) or 0)
    high = float(d.get("high", 0) or 0)
    low = float(d.get("low", 0) or 0)
    volume = float(d.get("volume", 0) or 0)

    if price <= 0 or high <= 0 or low <= 0 or volume <= 0:
        return -9999

    dollar_volume = price * volume
    day_range = max(high - low, 0.0001)
    range_pct = day_range / price if price > 0 else 0
    day_change_pct = ((price - open_price) / open_price) if open_price > 0 else 0
    near_high = price >= high * 0.985

    # فلاتر أولية
    if price < MIN_PRICE:
        return -9999

    if volume < MIN_VOLUME:
        return -9999

    if dollar_volume < MIN_DOLLAR_VOLUME:
        return -9999

    if range_pct > MAX_RANGE_PCT:
        return -9999

    if range_pct < MIN_RANGE_PCT:
        return -9999

    score = 0.0

    # dollar volume
    if dollar_volume > 1_000_000_000:
        score += 35
    elif dollar_volume > 500_000_000:
        score += 28
    elif dollar_volume > 200_000_000:
        score += 22
    elif dollar_volume > 100_000_000:
        score += 16
    else:
        score += 10

    # raw volume
    if volume > 100_000_000:
        score += 18
    elif volume > 50_000_000:
        score += 14
    elif volume > 20_000_000:
        score += 10
    elif volume > 5_000_000:
        score += 6
    else:
        score += 3

    # daily move
    if day_change_pct > 0.04:
        score += 12
    elif day_change_pct > 0.02:
        score += 8
    elif day_change_pct > 0:
        score += 4
    elif day_change_pct < -0.04:
        score -= 8
    elif day_change_pct < -0.02:
        score -= 4

    # technical location
    if near_high:
        score += 10

    # sweet spot for range
    if 0.02 <= range_pct <= 0.06:
        score += 10
    elif 0.06 < range_pct <= 0.10:
        score += 4

    return score


def get_seed_universe() -> list[str]:
    return [
        "AAPL", "NVDA", "TSLA", "AMD", "AMZN", "META", "MSFT", "GOOGL", "AVGO", "CRM",
        "ADBE", "NFLX", "ORCL", "INTC", "QCOM", "MU", "ANET", "PANW", "CRWD", "SNOW",
        "SHOP", "UBER", "ABNB", "PYPL", "COIN", "ROKU", "SQ", "TTD", "HIMS", "MARA",
        "RIOT", "OKLO", "ASTS", "MRVL", "NIO", "RKLB", "BAC", "JPM", "SOFI", "PLTR"
    ]


def get_scan_universe(max_symbols: int = 60) -> list[str]:
    """
    يرجّع Universe أولي أذكى:
    - يقرأ الأسهم الأمريكية العادية من Polygon reference
    - يربطها مع بيانات grouped daily لآخر جلسة
    - يسجّل الأسهم ويأخذ الأعلى جودة
    """
    reference_tickers = get_reference_tickers(limit_pages=12, page_limit=1000)
    if not reference_tickers:
        seed = get_seed_universe()
        return seed[:max_symbols]

    market_date = previous_business_day()
    grouped_map = get_grouped_daily_map(market_date)
    if not grouped_map:
        seed = get_seed_universe()
        return seed[:max_symbols]

    scored = []
    for ticker in reference_tickers:
        daily = grouped_map.get(ticker)
        if not daily:
            continue

        score = score_candidate(ticker, daily)
        if score == -9999:
            continue

        scored.append((ticker, score))

    if not scored:
        seed = get_seed_universe()
        return seed[:max_symbols]

    scored.sort(key=lambda x: x[1], reverse=True)

    return [ticker for ticker, _ in scored[:max_symbols]]
