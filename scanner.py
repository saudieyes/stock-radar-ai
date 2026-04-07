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

MIN_PRICE = 1.5
MIN_VOLUME = 250_000
MIN_DOLLAR_VOLUME = 2_000_000
MAX_RANGE_PCT = 0.45
MIN_RANGE_PCT = 0.015

TOTAL_UNIVERSE = 60
BIG_CAP_LIMIT = 5
MOMENTUM_LIMIT = 25
EMERGING_LIMIT = 20
SMALL_CAP_LIMIT = 10

BIG_CAPS = [
    "AAPL", "NVDA", "MSFT", "AMZN", "META",
    "GOOGL", "TSLA", "AMD", "AVGO", "NFLX"
]


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


def get_reference_tickers(limit_pages: int = 8, page_limit: int = 1000) -> list[str]:
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

        out[ticker] = {
            "price": float(item.get("c", 0) or 0),
            "open": float(item.get("o", 0) or 0),
            "high": float(item.get("h", 0) or 0),
            "low": float(item.get("l", 0) or 0),
            "volume": float(item.get("v", 0) or 0),
        }
    return out


def get_reference_details(ticker: str) -> dict:
    if not POLYGON_API_KEY:
        return {}

    url = f"https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={POLYGON_API_KEY}"
    data = safe_get_json(url, timeout=10)
    return data.get("results", {}) or {}


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
    gap_like_pct = day_change_pct
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
        "gap_like_pct": gap_like_pct,
        "near_high": near_high,
        "close_strength": close_strength,
        "body_strength": body_strength,
    }


def base_filters(d: dict) -> bool:
    m = calc_metrics(d)
    if m["price"] < MIN_PRICE:
        return False
    if m["volume"] < MIN_VOLUME:
        return False
    if m["dollar_volume"] < MIN_DOLLAR_VOLUME:
        return False
    if m["range_pct"] > MAX_RANGE_PCT:
        return False
    if m["range_pct"] < MIN_RANGE_PCT:
        return False
    if m["day_change_pct"] < -0.08:
        return False
    return True


def quick_score_candidate(ticker: str, d: dict) -> float:
    if not base_filters(d):
        return -9999

    m = calc_metrics(d)
    score = 0.0

    if m["day_change_pct"] > 0.08:
        score += 20
    elif m["day_change_pct"] > 0.05:
        score += 15
    elif m["day_change_pct"] > 0.02:
        score += 10

    if m["gap_like_pct"] > 0.08:
        score += 12
    elif m["gap_like_pct"] > 0.04:
        score += 8

    if m["near_high"]:
        score += 10
    if m["close_strength"] > 0.7:
        score += 8
    if m["body_strength"] > 0.5:
        score += 6

    if m["volume"] > 20_000_000:
        score += 12
    elif m["volume"] > 5_000_000:
        score += 8
    elif m["volume"] > 1_000_000:
        score += 4

    if m["dollar_volume"] > 300_000_000:
        score += 10
    elif m["dollar_volume"] > 50_000_000:
        score += 6

    return score


def score_big_cap(ticker: str, d: dict) -> float:
    m = calc_metrics(d)
    if ticker not in BIG_CAPS:
        return -9999

    score = 0.0
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
    if not base_filters(d):
        return -9999

    m = calc_metrics(d)
    score = 0.0

    if ticker in BIG_CAPS:
        score -= 8

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

    if m["gap_like_pct"] > 0.15:
        score += 25
    elif m["gap_like_pct"] > 0.08:
        score += 18
    elif m["gap_like_pct"] > 0.04:
        score += 10

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

    if 0.03 <= m["range_pct"] <= 0.20:
        score += 8
    elif m["range_pct"] > 0.30:
        score -= 6

    return score


def score_emerging_candidate(ticker: str, d: dict) -> float:
    if not base_filters(d):
        return -9999

    m = calc_metrics(d)
    score = 0.0

    if ticker in BIG_CAPS:
        score -= 12

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

    if m["gap_like_pct"] > 0.03:
        score += 6

    return score


def score_small_cap_candidate(ticker: str, d: dict, ref: dict) -> float:
    m = calc_metrics(d)
    market_cap = float(ref.get("market_cap", 0) or 0)

    if market_cap <= 0 or market_cap > 2_000_000_000:
        return -9999
    if m["price"] < 1.2:
        return -9999
    if m["volume"] < 200_000:
        return -9999
    if m["dollar_volume"] < 1_500_000:
        return -9999
    if m["range_pct"] < 0.02:
        return -9999
    if m["day_change_pct"] < -0.03:
        return -9999

    score = 0.0

    if m["day_change_pct"] > 0.12:
        score += 30
    elif m["day_change_pct"] > 0.08:
        score += 24
    elif m["day_change_pct"] > 0.05:
        score += 18
    elif m["day_change_pct"] > 0.02:
        score += 10

    if m["gap_like_pct"] > 0.15:
        score += 18
    elif m["gap_like_pct"] > 0.08:
        score += 12
    elif m["gap_like_pct"] > 0.04:
        score += 6

    if m["volume"] > 10_000_000:
        score += 16
    elif m["volume"] > 3_000_000:
        score += 11
    elif m["volume"] > 800_000:
        score += 7
    else:
        score += 3

    if m["near_high"]:
        score += 14
    if m["close_strength"] > 0.75:
        score += 10
    elif m["close_strength"] < 0.35:
        score -= 8

    if m["body_strength"] > 0.55:
        score += 7

    if 0.03 <= m["range_pct"] <= 0.30:
        score += 7
    elif m["range_pct"] > 0.40:
        score -= 6

    return score




def apply_late_move_filter(stock: dict) -> dict:
    try:
        current_price = float(stock.get("current_price_live", 0) or 0)
        open_price = float(stock.get("open_price_live", 0) or 0)
        entry = float(stock.get("entry", 0) or 0)
        volume_ratio = float(stock.get("volume_ratio", 0) or 0)
        decision = str(stock.get("decision", "") or "")
        trend = str(stock.get("trend", "") or "")
        breakout_quality = str(stock.get("breakout_quality", "") or "").upper()
        intraday = stock.get("intraday", {}) or {}
        above_vwap = bool(intraday.get("above_vwap_proxy", False))
        intraday_available = bool(intraday.get("available", False))
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        opening_drive = str(intraday.get("opening_drive", "unknown") or "unknown")

        if current_price <= 0 or open_price <= 0 or entry <= 0:
            stock["late_move_flag"] = "NO_PRICE_DATA"
            return stock

        change_from_open = ((current_price - open_price) / open_price) * 100 if open_price > 0 else 0
        distance_from_entry = ((current_price - entry) / entry) * 100 if entry > 0 else 0

        stock["late_move_flag"] = "OK"
        stock["distance_from_entry_pct"] = round(distance_from_entry, 2)

        # Step 3: منع الأسهم المتأخرة
        if change_from_open > 8:
            stock["late_move_flag"] = "LATE_FROM_OPEN"
            stock["execution_status"] = "SKIP_LATE_MOVE"
            stock["owner_action"] = "السهم تحرك بقوة من الافتتاح - متأخر للدخول الآن"
            stock["decision"] = "مراقبة"
            stock.setdefault("risk_flags", []).append("تحرك متأخر من الافتتاح")
            return stock

        if distance_from_entry > 5:
            stock["late_move_flag"] = "FAR_FROM_ENTRY"
            stock["execution_status"] = "SKIP_FAR_FROM_ENTRY"
            stock["owner_action"] = "السعر ابتعد كثيرًا عن نقطة الدخول - الأفضل الانتظار"
            stock["decision"] = "مراقبة"
            stock.setdefault("risk_flags", []).append("بعيد عن نقطة الدخول")
            return stock

        # Step 4: تحويل WAIT_BREAKOUT إلى READY/EXECUTE عندما تكون الشروط مناسبة
        near_entry = -1.5 <= distance_from_entry <= 1.2
        strong_daily_volume = volume_ratio >= 1.0
        strong_intraday_volume = intraday_ratio >= 1.15
        has_volume_support = strong_daily_volume or strong_intraday_volume
        acceptable_breakout = breakout_quality in {"STRONG", "WEAK"}
        strong_context = decision in {"دخول قوي", "دخول بحذر"} and trend in {"صاعد", "صاعد قوي"}

        if strong_context and acceptable_breakout and near_entry and has_volume_support:
            if intraday_available:
                if above_vwap and opening_drive != "هابط":
                    if current_price >= entry:
                        stock["execution_status"] = "EXECUTE"
                        stock["owner_action"] = "✅ دخول ممكن الآن - تحقق الاختراق مع دعم سيولة"
                    else:
                        stock["execution_status"] = "READY"
                        stock["owner_action"] = "⏳ جاهز للدخول - انتظر لمس/اختراق نقطة الدخول"
                else:
                    stock["execution_status"] = "WAIT_INTRADAY_CONFIRM"
                    stock["owner_action"] = "انتظر تأكيد لحظي أفضل فوق VWAP أو تحسن الافتتاح"
            else:
                if current_price >= entry * 0.992:
                    stock["execution_status"] = "READY"
                    stock["owner_action"] = "جاهز للمتابعة عند الافتتاح - السهم قريب من نقطة الدخول"
                else:
                    stock["execution_status"] = "WAIT_BREAKOUT"
                    stock["owner_action"] = "السهم جيد لكن يحتاج الوصول لنقطة الدخول أولًا"

        return stock
    except:
        return stock




def assign_execution_mode(stock: dict) -> dict:
    try:
        trade_type = str(stock.get("type", "") or "")
        current_price = float(stock.get("current_price_live", 0) or 0)
        entry = float(stock.get("entry", 0) or 0)
        stop_loss = float(stock.get("stop_loss", 0) or 0)
        target_1 = float(stock.get("target_1", 0) or 0)
        volume_ratio = float(stock.get("volume_ratio", 0) or 0)
        trend = str(stock.get("trend", "") or "")
        breakout_quality = str(stock.get("breakout_quality", "") or "").upper()
        risk_pct = float(stock.get("risk_pct", 0) or 0)
        late_move_flag = str(stock.get("late_move_flag", "OK") or "OK")
        execution_status = str(stock.get("execution_status", "") or "")
        intraday = stock.get("intraday", {}) or {}

        intraday_available = bool(intraday.get("available", False))
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        above_vwap = bool(intraday.get("above_vwap_proxy", False))
        opening_drive = str(intraday.get("opening_drive", "unknown") or "unknown")

        execution_mode = "انتظار تأكيد 📊"
        execution_note = "الاختراق غير مؤكد أو السيولة ما زالت غير كافية"

        rr = 0.0
        if entry > 0 and stop_loss > 0 and target_1 > 0 and entry > stop_loss:
            risk = entry - stop_loss
            reward = target_1 - entry
            rr = reward / risk if risk > 0 else 0.0
        stock["rr_1"] = round(rr, 2)

        distance_to_entry = 0.0
        distance_from_entry = 0.0
        if current_price > 0 and entry > 0:
            distance_to_entry = ((entry - current_price) / entry) * 100
            distance_from_entry = ((current_price - entry) / entry) * 100

        stock["distance_to_entry_pct"] = round(distance_to_entry, 2)
        stock["distance_from_entry_pct"] = round(distance_from_entry, 2)

        # 1) تجاهل فوري إذا السهم متأخر أو عالي المخاطرة
        if late_move_flag in {"LATE_FROM_OPEN", "FAR_FROM_ENTRY"} or execution_status in {"SKIP_LATE_MOVE", "SKIP_FAR_FROM_ENTRY", "AVOID"}:
            execution_mode = "تجنب ❌"
            execution_note = "السهم متأخر أو ابتعد عن نقطة الدخول"
        elif risk_pct > 25:
            execution_mode = "تجنب ❌"
            execution_note = "المخاطرة مرتفعة جدًا ولا تناسب الدخول"

        # 2) جاهز للدخول
        elif (
            entry > 0
            and current_price >= entry
            and current_price <= entry * 1.035
            and trend in {"صاعد", "صاعد قوي"}
            and breakout_quality in {"STRONG", "WEAK"}
            and (
                volume_ratio >= 1.0
                or (intraday_available and intraday_ratio >= 1.05)
            )
            and (
                not intraday_available
                or (above_vwap or opening_drive != "هابط")
            )
        ):
            execution_mode = "جاهز 🔥"
            execution_note = "تم الاختراق مع دعم جيد من السيولة"

        # 3) قريب جدًا من الاختراق
        elif (
            entry > 0
            and current_price < entry
            and current_price >= entry * 0.965
            and trend in {"صاعد", "صاعد قوي"}
            and breakout_quality in {"STRONG", "WEAK"}
        ):
            execution_mode = "انتظار اختراق ⏳"
            execution_note = f"راقب كسر {round(entry, 2)} والثبات فوقها"

        # 4) يحتاج تأكيد لحظي / سيولة / VWAP
        elif execution_status in {"WAIT_VOLUME", "WAIT_VWAP", "WAIT_INTRADAY_CONFIRM", "WAIT_OPENING"}:
            execution_mode = "انتظار تأكيد 📊"
            execution_note = "ينتظر تأكيدًا لحظيًا أفضل من السيولة أو VWAP"

        # 5) الارتداد
        elif trade_type == "Pullback":
            execution_mode = "انتظار تأكيد 📊"
            execution_note = "فرصة ارتداد، انتظر تأكيد الارتداد قبل الدخول"

        # 6) الافتراضي
        else:
            execution_mode = "انتظار تأكيد 📊"
            execution_note = "ما زال يحتاج تأكيدًا إضافيًا قبل التنفيذ"

        stock["execution_mode"] = execution_mode
        stock["execution_note"] = execution_note

        if execution_mode == "جاهز 🔥":
            stock["owner_action"] = f"✅ دخول ممكن الآن | دخول: {round(entry,2)} | وقف: {round(stop_loss,2)} | هدف1: {round(target_1,2)}"
        elif execution_mode == "انتظار اختراق ⏳":
            stock["owner_action"] = f"⏳ انتظر كسر {round(entry,2)} والثبات فوقها قبل الدخول"
        elif execution_mode == "انتظار تأكيد 📊":
            stock["owner_action"] = execution_note
        elif execution_mode == "تجنب ❌":
            stock["owner_action"] = "🚫 تجاهل هذه الفرصة الآن"

        return stock
    except:
        return stock


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
    reference_tickers = get_reference_tickers(limit_pages=8, page_limit=1000)
    if not reference_tickers:
        return get_seed_universe()[:max_symbols]

    market_date = previous_business_day()
    grouped_map = get_grouped_daily_map(market_date)
    if not grouped_map:
        return get_seed_universe()[:max_symbols]

    big_caps_scored = []
    momentum_scored = []
    emerging_scored = []
    fast_pool = []

    for ticker in reference_tickers:
        daily = grouped_map.get(ticker)
        if not daily:
            continue

        quick = quick_score_candidate(ticker, daily)
        if quick != -9999:
            fast_pool.append((ticker, quick))

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
    fast_pool.sort(key=lambda x: x[1], reverse=True)

    small_cap_candidates = [t for t, _ in fast_pool[:160]]
    small_cap_scored = []

    for ticker in small_cap_candidates:
        daily = grouped_map.get(ticker)
        if not daily:
            continue
        ref = get_reference_details(ticker)
        s_small = score_small_cap_candidate(ticker, daily, ref)
        if s_small != -9999:
            small_cap_scored.append((ticker, s_small))

    small_cap_scored.sort(key=lambda x: x[1], reverse=True)

    big_caps_final = [t for t, _ in big_caps_scored[:BIG_CAP_LIMIT]]
    momentum_final = [t for t, _ in momentum_scored[:MOMENTUM_LIMIT]]
    emerging_final = [t for t, _ in emerging_scored[:EMERGING_LIMIT]]
    small_cap_final = [t for t, _ in small_cap_scored[:SMALL_CAP_LIMIT]]

    final_universe = unique_keep_order(
        big_caps_final + momentum_final + emerging_final + small_cap_final
    )

    if not final_universe:
        return get_seed_universe()[:max_symbols]

    return final_universe[:max_symbols]

