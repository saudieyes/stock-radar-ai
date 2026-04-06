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


def previous_business_day():
    d = datetime.utcnow().date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def get_grouped_daily_map(date_str: str):
    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date_str}?adjusted=true&apiKey={POLYGON_API_KEY}"
    data = safe_get_json(url)

    out = {}
    for item in data.get("results", []):
        t = item.get("T")
        if not t:
            continue

        out[t] = {
            "price": float(item.get("c", 0) or 0),
            "open": float(item.get("o", 0) or 0),
            "high": float(item.get("h", 0) or 0),
            "low": float(item.get("l", 0) or 0),
            "volume": float(item.get("v", 0) or 0),
        }

    return out


def score_stock(d):
    price = d["price"]
    volume = d["volume"]

    if price < MIN_PRICE:
        return -1

    score = 0

    # سيولة
    if volume > 100_000_000:
        score += 30
    elif volume > 20_000_000:
        score += 20
    elif volume > 5_000_000:
        score += 10
    else:
        score += 2

    # حركة
    change = (price - d["open"]) / d["open"] if d["open"] else 0

    if change > 0.05:
        score += 25
    elif change > 0.02:
        score += 15
    elif change > 0:
        score += 5

    return score


def get_scan_universe(max_symbols: int = 300):
    """
    نسخة جديدة:
    - تجيب السوق كامل
    - ترتب بدون قتل الأسهم
    - تعطي 300 سهم بدل 40
    """

    date = previous_business_day()
    market = get_grouped_daily_map(date)

    scored = []

    for ticker, d in market.items():
        s = score_stock(d)

        if s < 0:
            continue

        scored.append((ticker, s))

    scored.sort(key=lambda x: x[1], reverse=True)

    return [t for t, _ in scored[:max_symbols]]
