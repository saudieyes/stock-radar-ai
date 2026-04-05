import os
import requests


POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")


EXCLUDED_TYPES = {
    "ETF", "ETN", "ETV", "WARRANT", "RIGHT", "UNIT",
    "PREFERRED", "FUND", "TRUST", "INDEX"
}

EXCLUDED_SUFFIXES = (
    "W", "WS", "WT", "R", "U"
)


def safe_get_json(url: str, timeout: int = 20):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except:
        return {}


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

    if market not in {"STOCKS"}:
        return False

    if type_ in EXCLUDED_TYPES:
        return False

    for suf in EXCLUDED_SUFFIXES:
        if ticker.endswith(suf):
            return False

    bad_words = [
        "ETF", "TRUST", "FUND", "WARRANT", "RIGHT", "UNIT",
        "PREFERRED", "DEPOSITARY", "ADR"
    ]
    if any(word in name for word in bad_words):
        return False

    return True


def get_polygon_tickers(limit_pages: int = 8, page_limit: int = 1000) -> list[str]:
    """
    يجلب مجموعة أولية من الأسهم الأمريكية من Polygon.
    limit_pages:
        عدد الصفحات التي نقرأها من API.
    page_limit:
        عدد العناصر في الصفحة الواحدة إن دعمه Polygon.
    """
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
        if next_url:
            if "apiKey=" not in next_url:
                next_url = f"{next_url}&apiKey={POLYGON_API_KEY}"

        pages_read += 1

        if not next_url:
            break

    # إزالة التكرار مع الحفاظ على الترتيب
    seen = set()
    cleaned = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            cleaned.append(t)

    return cleaned


def get_seed_universe() -> list[str]:
    """
    fallback universe ثابت لو API ما رجع شيء.
    """
    return [
        "AAPL", "NVDA", "TSLA", "AMD", "AMZN", "META", "MSFT", "GOOGL", "AVGO", "CRM",
        "ADBE", "NFLX", "ORCL", "INTC", "QCOM", "MU", "ANET", "PANW", "CRWD", "SNOW",
        "SHOP", "UBER", "ABNB", "PYPL", "COIN", "ROKU", "SQ", "TTD", "HIMS", "MARA",
        "RIOT", "OKLO", "ASTS", "MRVL", "NIO", "RKLB", "BAC", "JPM", "SOFI", "PLTR"
    ]


def get_scan_universe(max_symbols: int = 60) -> list[str]:
    """
    هذا هو النداء الرئيسي الذي سيستخدمه main.py
    يرجع قائمة أسهم مرشحة أولية.
    """
    tickers = get_polygon_tickers(limit_pages=8, page_limit=1000)

    if not tickers:
        tickers = get_seed_universe()

    return tickers[:max_symbols]
