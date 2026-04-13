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

TOTAL_UNIVERSE = 150
BIG_CAP_LIMIT = 15
MOMENTUM_LIMIT = 60
EMERGING_LIMIT = 45
SMALL_CAP_LIMIT = 30

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
    if 0.018 <= m["range_pct"] <= 0.08 and m["close_strength"] >= 0.62 and m["day_change_pct"] >= 0.015:
        score += 10
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
    if 0.015 <= m["range_pct"] <= 0.07 and m["close_strength"] >= 0.6 and m["day_change_pct"] >= 0.01:
        score += 8
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
    if 0.018 <= m["range_pct"] <= 0.08 and m["close_strength"] >= 0.62 and m["day_change_pct"] >= 0.015:
        score += 10
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



def round2(value):
    try:
        return round(float(value or 0), 2)
    except:
        return 0.0


def classify_runner_stage(stock: dict) -> dict:
    try:
        runner_score = float(stock.get("runner_score", 0) or 0)
        runner_label = str(stock.get("runner_label", "") or "")
        strategy_label = str(stock.get("strategy_label", "") or "")
        delayed_label = str(stock.get("delayed_compensation_label", "") or "")
        if runner_score >= 80:
            stock["runner_stage"] = "strong"
            stock["runner_stage_label"] = runner_label or "Runner محتمل 4-6 ساعات 🔥"
        elif runner_score >= 66:
            stock["runner_stage"] = "good"
            stock["runner_stage_label"] = runner_label or "مرشح استمرار اليوم ✅"
        elif runner_score >= 56:
            stock["runner_stage"] = "cautious"
            stock["runner_stage_label"] = runner_label or "استمرار محتمل بحذر 🟠"
        else:
            stock["runner_stage"] = ""
            stock["runner_stage_label"] = ""

        if not strategy_label and str(stock.get("type", "")) == "Pullback":
            stock["strategy_label"] = "دخول ارتداد"
        elif not strategy_label:
            stock["strategy_label"] = "دخول استمرار"

        if delayed_label and "ملاحظة" not in str(stock.get("owner_action", "")):
            stock["delayed_compensation_label"] = delayed_label
        return stock
    except:
        return stock


def recalc_reentry_plan(stock: dict) -> dict:
    try:
        current_price = float(stock.get("current_price_live", 0) or 0)
        breakout_price = float(stock.get("breakout_price", 0) or 0)
        confirmation_price = float(stock.get("confirmation_price", 0) or 0)
        late_entry_price = float(stock.get("late_entry_price", 0) or 0)
        stop_loss = float(stock.get("stop_loss", 0) or 0)
        target_1 = float(stock.get("target_1", 0) or 0)
        high_live = float(stock.get("high_live", 0) or current_price)

        if current_price <= 0:
            return stock

        # خطة إعادة تمركز تعتمد على السعر الحالي بعد فوات الدخول الأول
        move_from_breakout = max(current_price - breakout_price, 0)
        pullback_entry = confirmation_price if confirmation_price > 0 else current_price
        if move_from_breakout > 0:
            pullback_entry = max(confirmation_price, current_price - (move_from_breakout * 0.35))
        if late_entry_price > 0:
            pullback_entry = min(pullback_entry, late_entry_price)

        rebreakout_entry = max(high_live, current_price) * 1.003
        smart_entry = round2(pullback_entry)
        smart_stop = round2(max(stop_loss, smart_entry * 0.965)) if smart_entry > 0 else round2(stop_loss)
        if smart_stop >= smart_entry and smart_entry > 0:
            smart_stop = round2(smart_entry * 0.97)
        risk_unit = max(smart_entry - smart_stop, 0)
        smart_target = round2(max(target_1, smart_entry + (risk_unit * 1.8))) if smart_entry > 0 else round2(target_1)

        stock["late_as_watch"] = True
        stock["reentry_plan_active"] = True
        stock["reentry_pullback_price"] = round2(pullback_entry)
        stock["rebreakout_price"] = round2(rebreakout_entry)
        stock["smart_entry_price"] = smart_entry
        stock["smart_stop_price"] = smart_stop
        stock["smart_target_1"] = smart_target
        stock["reentry_note"] = (
            f"فات الدخول الأول. راقب إعادة دخول قرب {round2(pullback_entry)} "
            f"أو اختراق جديد فوق {round2(rebreakout_entry)}"
        )
        return stock
    except:
        return stock


def apply_late_move_filter(stock: dict) -> dict:
    try:
        current_price = float(stock.get("current_price_live", 0) or 0)
        open_price = float(stock.get("open_price_live", 0) or 0)
        confirmation_price = float(stock.get("confirmation_price", 0) or 0)
        entry_price_real = float(stock.get("entry_price_real", stock.get("entry", 0)) or 0)
        late_entry_price = float(stock.get("late_entry_price", 0) or 0)
        breakout_status = str(stock.get("breakout_status", "") or "")

        if current_price <= 0 or open_price <= 0:
            stock["late_move_flag"] = "NO_PRICE_DATA"
            return stock

        change_from_open = ((current_price - open_price) / open_price) * 100 if open_price > 0 else 0
        stock["change_from_open_pct"] = round(change_from_open, 2)
        stock["late_move_flag"] = "OK"

        # لا نعتبر السهم متأخرًا قبل التأكيد أو قبل الدخول الفعلي
        if entry_price_real <= 0 or current_price < entry_price_real:
            return stock

        # بعد تجاوز آخر دخول مناسب فقط يصبح متأخرًا
        if late_entry_price > 0 and current_price > late_entry_price:
            stock["late_move_flag"] = "CONFIRMED_LATE"
            stock["execution_status"] = "SKIP_FAR_FROM_ENTRY"
            stock["owner_action"] = "السهم تجاوز آخر دخول مناسب - لا تطارد السعر الآن"
            stock.setdefault("risk_flags", []).append("السعر تجاوز آخر دخول مناسب")
            return stock

        # إذا تحرك بقوة من الافتتاح بعد التأكيد وبعد الدخول الفعلي
        if change_from_open > 10 and breakout_status in {"تأكيد الاختراق", "اختراق مؤكد - دخول بحذر", "اختراق متأخر"}:
            stock["late_move_flag"] = "FAST_AFTER_CONFIRMATION"
            stock.setdefault("risk_flags", []).append("تحرك سريع بعد التأكيد")

        return stock
    except:
        return stock





def assign_execution_mode(stock: dict) -> dict:
    try:
        trade_type = str(stock.get("type", "") or "")
        current_price = float(stock.get("current_price_live", 0) or 0)
        price_reliable = bool(stock.get("price_reliable_for_execution", False))
        market_phase = str(stock.get("market_phase", "") or "")
        stop_loss = float(stock.get("stop_loss", 0) or 0)
        target_1 = float(stock.get("target_1", 0) or 0)
        risk_pct = float(stock.get("risk_pct", 0) or 0)
        late_move_flag = str(stock.get("late_move_flag", "OK") or "OK")
        execution_status = str(stock.get("execution_status", "") or "")
        breakout_price = float(stock.get("breakout_price", 0) or 0)
        confirmation_price = float(stock.get("confirmation_price", 0) or 0)
        entry_price_real = float(stock.get("entry_price_real", stock.get("entry", 0)) or 0)
        late_entry_price = float(stock.get("late_entry_price", 0) or 0)
        effective_volume_ratio = float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)) or 0)
        intraday = stock.get("intraday", {}) or {}
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        above_vwap = bool(intraday.get("above_vwap_proxy", False))
        opening_drive = str(intraday.get("opening_drive", "unknown") or "unknown")
        market_open = bool(intraday.get("market_open", False))
        timing_signal = str(stock.get("timing_signal", "") or "")
        timing_reason = str(stock.get("timing_reason", "") or "")
        trend = str(stock.get("trend", "") or "")
        breakout_quality = str(stock.get("breakout_quality", "") or "")
        quality_score = float(stock.get("quality_score", 0) or 0)
        runner_score = float(stock.get("runner_score", 0) or 0)
        strategy_label = str(stock.get("strategy_label", "") or "")
        delayed_comp_label = str(stock.get("delayed_compensation_label", "") or "")
        close_strength_hint = "قوية" if quality_score >= 78 else "متوسطة" if quality_score >= 66 else "ضعيفة"

        rr = 0.0
        if entry_price_real > 0 and stop_loss > 0 and target_1 > 0 and entry_price_real > stop_loss:
            risk = entry_price_real - stop_loss
            reward = target_1 - entry_price_real
            rr = reward / risk if risk > 0 else 0.0
        stock["rr_1"] = round(rr, 2)

        distance_to_entry = 0.0
        if current_price > 0 and entry_price_real > 0:
            distance_to_entry = ((entry_price_real - current_price) / entry_price_real) * 100
        stock["distance_to_entry_pct"] = round(distance_to_entry, 2)

        execution_mode = "انتظار تأكيد 📊"
        execution_note = timing_reason or "يحتاج السهم إلى تأكيد إضافي"

        if not price_reliable and market_phase in {"open", "pre_market", "after_hours"}:
            stock["decision"] = "مراقبة"
            stock["execution_mode"] = "مراقبة 👀"
            stock["execution_note"] = "السعر اللحظي غير موثوق الآن - راقب فقط"
            stock["owner_action"] = "👀 راقب حتى تتوفر بيانات لحظية موثوقة"
            return classify_runner_stage(stock)

        if timing_signal:
            execution_mode = timing_signal
            execution_note = timing_reason or execution_note

        if risk_pct > 25:
            execution_mode = "تجنب ❌"
            execution_note = "المخاطرة مرتفعة جدًا"
        elif late_move_flag in {"CONFIRMED_LATE"} or execution_status in {"SKIP_FAR_FROM_ENTRY"}:
            stock = recalc_reentry_plan(stock)
            stock["decision"] = "مراقبة"
            execution_mode = "مراقبة إعادة دخول 👀"
            execution_note = stock.get("reentry_note", "فات الدخول الأول - راقب إعادة دخول")
        elif trade_type == "Breakout":
            has_good_volume = effective_volume_ratio >= 1.0 or intraday_ratio >= 1.1
            strong_volume = effective_volume_ratio >= 1.15 or intraday_ratio >= 1.25
            runner_ready = runner_score >= 66
            intraday_ok = (not market_open) or (above_vwap and opening_drive != "هابط")

            if breakout_quality == "FAILED":
                stock["decision"] = "مراقبة"
                execution_mode = "مراقبة 👀"
                execution_note = "احتمال اختراق وهمي - الأفضل المراقبة"
            elif current_price < breakout_price:
                if trend in {"صاعد", "صاعد قوي"} and effective_volume_ratio >= 0.9 and quality_score >= 66:
                    execution_mode = "انتظار اختراق ⏳"
                    execution_note = f"⏳ رادار مبكر: راقب كسر {round(breakout_price,2)} ثم تأكيد {round(confirmation_price,2)}"
                else:
                    execution_mode = "انتظار اختراق ⏳"
                    execution_note = f"⏳ انتظر اختراق {round(breakout_price,2)} ثم تأكيد {round(confirmation_price,2)}"
            elif breakout_price <= current_price < confirmation_price:
                execution_mode = "انتظار تأكيد 📊"
                execution_note = f"📊 يحتاج الثبات فوق {round(confirmation_price,2)}"
            elif confirmation_price <= current_price <= entry_price_real:
                if market_phase == "open":
                    if strong_volume and intraday_ok and runner_ready:
                        execution_mode = "جاهز 🔥"
                        execution_note = f"✅ {strategy_label or 'دخول'} ممكن الآن قرب {round(entry_price_real,2)}"
                    elif has_good_volume:
                        execution_mode = "دخول بحذر 🟠"
                        execution_note = f"🟠 {strategy_label or 'دخول'} بحذر قرب {round(entry_price_real,2)} - الجودة {close_strength_hint}"
                    else:
                        execution_mode = "انتظار تأكيد 📊"
                        execution_note = "السعر في منطقة جيدة لكن يحتاج سيولة/VWAP أفضل"
                else:
                    if has_good_volume and quality_score >= 70:
                        execution_mode = "دخول بحذر 🟠"
                        execution_note = "خارج الجلسة: فرصة جيدة لكن القرار النهائي مع الافتتاح"
                    else:
                        execution_mode = "انتظار تأكيد 📊"
                        execution_note = "السهم في منطقة جيدة، والقرار الأفضل يكون مع افتتاح السوق"
            elif entry_price_real < current_price <= late_entry_price:
                if has_good_volume and (market_phase != "open" or intraday_ok):
                    execution_mode = "دخول بحذر 🟠"
                    execution_note = f"🟠 ما زال ضمن آخر دخول مناسب حتى {round(late_entry_price,2)}"
                else:
                    execution_mode = "انتظار تأكيد 📊"
                    execution_note = "اقترب من الدخول لكن التوقيت ليس مثاليًا"
            elif late_entry_price > 0 and current_price > late_entry_price:
                stock = recalc_reentry_plan(stock)
                stock["decision"] = "مراقبة"
                execution_mode = "مراقبة إعادة دخول 👀"
                execution_note = stock.get("reentry_note", "فات الدخول الأول - راقب إعادة دخول")

            if execution_status == "WAIT_VWAP" and execution_mode in {"جاهز 🔥", "دخول بحذر 🟠"}:
                execution_mode = "انتظار تأكيد 📊"
                execution_note = "السعر مناسب لكن يحتاج الثبات فوق VWAP"
            elif execution_status == "WAIT_VOLUME" and execution_mode in {"جاهز 🔥", "دخول بحذر 🟠"}:
                execution_mode = "انتظار تأكيد 📊"
                execution_note = "السعر مناسب لكن يحتاج سيولة أقوى"
        elif trade_type == "Pullback":
            if trend in {"صاعد", "صاعد قوي"} and risk_pct <= 8 and effective_volume_ratio >= 0.9:
                execution_mode = "دخول بحذر 🟠"
                execution_note = "🟠 ارتداد جيد من دعم - دخول بحذر"
            else:
                execution_mode = "انتظار تأكيد 📊"
                execution_note = "فرصة ارتداد تحتاج تأكيدًا"
        else:
            execution_mode = "مراقبة 👀"
            execution_note = "تحت المراقبة فقط"

        stock["execution_mode"] = execution_mode
        stock["execution_note"] = execution_note

        if execution_mode == "جاهز 🔥":
            stock["owner_action"] = f"✅ دخول ممكن الآن قرب {round(entry_price_real,2)} | وقف: {round(stop_loss,2)} | هدف1: {round(target_1,2)}"
        elif execution_mode == "دخول بحذر 🟠":
            stock["owner_action"] = f"🟠 دخول بحذر - حتى {round(late_entry_price if late_entry_price > 0 else entry_price_real,2)} | وقف: {round(stop_loss,2)}"
        elif execution_mode == "انتظار اختراق ⏳":
            stock["owner_action"] = f"⏳ انتظر اختراق {round(breakout_price,2)} ثم تأكيد {round(confirmation_price,2)}"
        elif execution_mode == "انتظار تأكيد 📊":
            stock["owner_action"] = execution_note
        elif execution_mode == "مراقبة إعادة دخول 👀":
            stock["owner_action"] = stock.get("reentry_note", "👀 راقب إعادة دخول")
        elif execution_mode in {"متأخر ⚠️", "تجنب ❌"}:
            stock["owner_action"] = "🚫 لا تطارد السعر الآن"
        else:
            stock["owner_action"] = "👀 تحت المراقبة فقط"

        if delayed_comp_label and execution_mode in {"مراقبة 👀", "مراقبة إعادة دخول 👀", "دخول بحذر 🟠"}:
            stock["owner_action"] = f"{stock['owner_action']} | {delayed_comp_label}"

        return classify_runner_stage(stock)
    except:
        return stock


def normalize_execution_labels(stock: dict) -> dict:
    try:
        intraday = stock.get("intraday", {}) or {}
        market_open = bool(intraday.get("market_open", False))
        status = str(stock.get("execution_status", "") or "")
        mode = str(stock.get("execution_mode", "") or "")

        mapping = {
            "READY": "جاهز 🔥",
            "EXECUTE": "جاهز 🔥",
            "WAIT_BREAKOUT": "انتظار اختراق ⏳",
            "WAIT_CONFIRM": "انتظار تأكيد 📊",
            "WAIT_INTRADAY_CONFIRM": "انتظار تأكيد 📊",
            "WAIT_VWAP": "انتظار تأكيد 📊",
            "WAIT_VOLUME": "انتظار تأكيد 📊",
            "WAIT_OPENING": "انتظار تأكيد 📊" if market_open else "انتظار الافتتاح ⏰",
            "WATCH": "مراقبة 👀",
            "AVOID": "تجنب ❌",
            "SKIP_LATE_MOVE": "مراقبة إعادة دخول 👀",
            "SKIP_FAR_FROM_ENTRY": "مراقبة إعادة دخول 👀",
        }

        stock["execution_status_ar"] = mapping.get(status, status)

        if not mode:
            stock["execution_mode"] = stock["execution_status_ar"]
        else:
            stock["execution_mode"] = mode

        if market_open and stock["execution_mode"] == "انتظار الافتتاح ⏰":
            stock["execution_mode"] = "انتظار تأكيد 📊"

        return stock
    except:
        return stock




def enrich_signal_stage(stock: dict) -> dict:
    try:
        mode = str(stock.get("execution_mode", "") or "")
        note = str(stock.get("execution_note", "") or "")
        late_watch = bool(stock.get("late_as_watch", False) or stock.get("reentry_plan_active", False))
        current_price = float(stock.get("current_price_live", 0) or 0)
        breakout_price = float(stock.get("breakout_price", 0) or 0)
        confirmation_price = float(stock.get("confirmation_price", 0) or 0)
        quality_score = float(stock.get("quality_score", 0) or 0)
        effective_volume_ratio = float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)) or 0)

        stage = "normal"
        stage_label = ""
        stage_color = ""
        runner_stage = str(stock.get("runner_stage", "") or "")

        if late_watch or "إعادة دخول" in mode:
            stage = "reentry"
            stage_label = "إعادة دخول"
            stage_color = "watch"
        elif runner_stage in {"strong", "good"} and ("جاهز" in mode or "دخول بحذر" in mode):
            stage = "actionable"
            stage_label = "استمرار اليوم"
            stage_color = "strong" if runner_stage == "strong" else "cautious"
        elif "جاهز" in mode or "دخول بحذر" in mode:
            stage = "actionable"
            stage_label = "قابل للتنفيذ"
            stage_color = "strong" if "جاهز" in mode else "cautious"
        elif current_price > 0 and breakout_price > 0 and current_price < breakout_price and quality_score >= 66 and effective_volume_ratio >= 0.9:
            stage = "early"
            stage_label = "إشارة مبكرة"
            stage_color = "watch"
        elif "رادار مبكر" in note:
            stage = "early"
            stage_label = "إشارة مبكرة"
            stage_color = "watch"
        elif current_price > 0 and confirmation_price > 0 and current_price < confirmation_price and quality_score >= 60:
            stage = "building"
            stage_label = "تحت التجهيز"
            stage_color = "watch"

        stock["signal_stage"] = stage
        stock["signal_stage_label"] = stage_label
        stock["signal_stage_color"] = stage_color
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

