import os
import requests
from datetime import datetime, timedelta

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

MIN_PRICE = 2.0


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


def get_seed_universe() -> list[str]:
    return [
        "AAPL", "NVDA", "TSLA", "AMD", "AMZN", "META", "MSFT", "GOOGL", "AVGO", "CRM",
        "ADBE", "NFLX", "ORCL", "INTC", "QCOM", "MU", "ANET", "PANW", "CRWD", "SNOW",
        "SHOP", "UBER", "ABNB", "PYPL", "COIN", "ROKU", "SQ", "TTD", "HIMS", "MARA",
        "RIOT", "OKLO", "ASTS", "MRVL", "NIO", "RKLB", "BAC", "JPM", "SOFI", "PLTR",
        "SMCI", "ARM", "DELL", "CSCO", "KLAC", "AMAT", "LRCX", "TXN", "ADI", "INTU",
        "NOW", "MDB", "PATH", "SOUN", "AI", "COST", "WMT", "DIS", "PYPL", "CRM",
        "AAL", "UAL", "DAL", "F", "GM", "NVO", "LLY", "ISRG", "VRTX", "REGN",
        "SLNO", "AEHR", "RCMT", "ODD", "LWLG", "OPTX", "CLNN", "AGL"
    ]


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

        out[ticker] = {
            "price": float(item.get("c", 0) or 0),
            "open": float(item.get("o", 0) or 0),
            "high": float(item.get("h", 0) or 0),
            "low": float(item.get("l", 0) or 0),
            "volume": float(item.get("v", 0) or 0),
        }

    return out


def score_stock(d: dict) -> float:
    price = float(d.get("price", 0) or 0)
    open_price = float(d.get("open", 0) or 0)
    high = float(d.get("high", 0) or 0)
    low = float(d.get("low", 0) or 0)
    volume = float(d.get("volume", 0) or 0)

    if price <= 0 or high <= 0 or low <= 0:
        return -1

    if price < MIN_PRICE:
        return -1

    score = 0.0

    dollar_volume = price * volume
    day_range = max(high - low, 0.0001)
    range_pct = day_range / price if price > 0 else 0
    day_change_pct = ((price - open_price) / open_price) if open_price > 0 else 0

    # volume
    if volume > 100_000_000:
        score += 30
    elif volume > 20_000_000:
        score += 20
    elif volume > 5_000_000:
        score += 10
    elif volume > 1_000_000:
        score += 4
    else:
        score += 1

    # dollar volume
    if dollar_volume > 1_000_000_000:
        score += 25
    elif dollar_volume > 250_000_000:
        score += 15
    elif dollar_volume > 50_000_000:
        score += 8
    else:
        score += 2

    # move
    if day_change_pct > 0.10:
        score += 25
    elif day_change_pct > 0.05:
        score += 18
    elif day_change_pct > 0.02:
        score += 12
    elif day_change_pct > 0:
        score += 5
    elif day_change_pct < -0.05:
        score -= 6

    # range
    if 0.02 <= range_pct <= 0.12:
        score += 10
    elif range_pct > 0.12:
        score += 4

    # near high
    if price >= high * 0.985:
        score += 8

    return score


def get_scan_universe(max_symbols: int = 300) -> list[str]:
    """
    نسخة مستقرة:
    - تحاول قراءة السوق الكامل من grouped daily
    - إذا فشل المصدر، ترجع seed universe بدل 0
    - لا تقتل الأداة بسبب endpoint واحد
    """
    seed = get_seed_universe()

    market_date = previous_business_day()
    grouped_map = get_grouped_daily_map(market_date)

    # fallback قوي
    if not grouped_map:
        return seed[:max_symbols]

    scored = []
    for ticker, daily in grouped_map.items():
        score = score_stock(daily)
        if score < 0:
            continue
        scored.append((ticker, score))

    if not scored:
        return seed[:max_symbols]

    scored.sort(key=lambda x: x[1], reverse=True)

    result = [ticker for ticker, _ in scored[:max_symbols]]

    # fallback إضافي لو صار شيء غريب
    if not result:
        return seed[:max_symbols]

    return result
