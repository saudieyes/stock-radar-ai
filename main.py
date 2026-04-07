from fastapi import FastAPI
from fastapi.responses import FileResponse
import requests
import os
import csv
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from scanner import get_scan_universe

app = FastAPI()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

SECTOR_DATA = {}
COMPANIES_DATA = {}
BALANCE_DATA = {}
INCOME_DATA = {}
HISTORY_CACHE = {}

HARAM_SECTORS = {"financial services", "banks", "insurance"}

HARAM_INDUSTRY_KEYWORDS = [
    "bank", "banks", "insurance", "tobacco", "alcohol",
    "gambling", "casino", "betting", "credit services",
    "mortgage", "reit mortgage", "asset management", "capital markets",
]

LOW_PRICE_HARD_BLOCK = 2.0
LOW_PRICE_WARNING = 3.0


def clean_key(key):
    return str(key).replace("\ufeff", "").strip()


def clean_row(row):
    return {clean_key(k): v for k, v in row.items()}


def to_float(value):
    try:
        if value is None:
            return 0.0
        value = str(value).replace(",", "").strip()
        return float(value) if value else 0.0
    except:
        return 0.0


def period_rank(p):
    return {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5, "TTM": 6}.get(str(p).upper(), 0)


def parse_date_safe(v):
    try:
        return datetime.strptime(str(v).strip(), "%Y-%m-%d")
    except:
        return datetime.min


def safe_round(x, digits=2):
    try:
        return round(float(x), digits)
    except:
        return x


def latest_key(row):
    return (
        parse_date_safe(row.get("Publish Date", "")),
        int(to_float(row.get("Fiscal Year", 0))),
        period_rank(row.get("Fiscal Period", ""))
    )


def normalize_text(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^a-z0-9\s&.\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_company_name_variants(company_name: str) -> list[str]:
    name = normalize_text(company_name)
    if not name:
        return []

    variants = {name}

    noise = [
        " inc", " inc.", " corp", " corp.", " corporation", " co", " co.",
        " ltd", " ltd.", " limited", " plc", " holdings", " holding",
        " group", " technologies", " technology", " systems", " system",
        " international", " company", " companies", " class a", " class c",
        " common stock"
    ]

    for n in noise:
        if name.endswith(n):
            variants.add(name[:-len(n)].strip())

    parts = name.split()
    if len(parts) >= 2:
        variants.add(" ".join(parts[:2]))
    if len(parts) >= 1:
        variants.add(parts[0])

    cleaned = []
    for v in variants:
        v = v.strip()
        if len(v) >= 3:
            cleaned.append(v)

    return list(dict.fromkeys(cleaned))


def make_rank_label(score: float) -> str:
    if score >= 90:
        return "A+"
    if score >= 84:
        return "A"
    if score >= 78:
        return "B+"
    if score >= 72:
        return "B"
    if score >= 66:
        return "C+"
    if score >= 60:
        return "C"
    return "D"


def is_market_open_now() -> bool:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return False

        current_minutes = now_ny.hour * 60 + now_ny.minute
        open_minutes = 9 * 60 + 30
        close_minutes = 16 * 60
        return open_minutes <= current_minutes <= close_minutes
    except:
        return False


def estimate_validity(trade_type: str, trend: str, volume_ratio: float, catalyst_score: float) -> str:
    if trade_type == "Breakout":
        if volume_ratio >= 1.3 and catalyst_score > 0:
            return "صالح اليوم وحتى الجلسة القادمة"
        if volume_ratio >= 1.0:
            return "صالح اليوم فقط"
        return "يحتاج تأكيد أثناء التداول" if is_market_open_now() else "يحتاج تأكيد بعد الافتتاح"

    if trade_type == "Pullback":
        if trend == "صاعد قوي" and volume_ratio >= 1.0:
            return "1-3 أيام"
        if trend == "صاعد":
            return "1-2 يوم"
        return "مراقبة يومية"

    return "مراقبة مشروطة"


def decision_priority(decision: str) -> int:
    if decision == "دخول قوي":
        return 3
    if decision == "دخول بحذر":
        return 2
    if decision == "مراقبة":
        return 1
    return 0


def compute_execution_status(trade_type: str, decision: str, trend: str, volume_ratio: float, catalyst_score: float, breakout_quality: str) -> str:
    if breakout_quality == "FAILED" and trade_type == "Breakout":
        return "AVOID"

    if decision == "دخول قوي" and trend == "صاعد قوي" and volume_ratio >= 1.2 and breakout_quality in {"STRONG", "WEAK"} and catalyst_score >= 0:
        return "READY"

    if decision in {"دخول قوي", "دخول بحذر"}:
        return "WAIT"

    if decision == "مراقبة":
        return "WAIT"

    return "AVOID"


def owner_decision(decision: str, trend: str, breakout_quality: str, volume_ratio: float, catalyst_score: float) -> str:
    if breakout_quality == "FAILED":
        return "لا تزد الكمية الآن - الأفضل الاحتفاظ بحذر أو التخفيف إذا كسر الدعم"
    if decision == "دخول قوي" and trend == "صاعد قوي" and volume_ratio >= 1.2:
        return "يمكن الشراء أو زيادة الكمية بشكل جزئي"
    if decision == "دخول بحذر":
        return "احتفاظ أو زيادة جزئية بحذر بعد تأكيد الحركة"
    if trend == "هابط":
        return "الأفضل عدم زيادة الكمية ومراقبة الدعم"
    return "احتفاظ ومراقبة - لا توجد زيادة واضحة الآن"


def breakout_quality_label(trade_type: str, momentum: str, body_strength: float, close_strength: float, volume_ratio: float) -> str:
    if trade_type != "Breakout":
        return "N/A"
    if momentum == "صاعد" and body_strength >= 0.6 and close_strength >= 0.75 and volume_ratio >= 1.2:
        return "STRONG"
    if body_strength < 0.35 or close_strength < 0.5 or volume_ratio < 0.8:
        return "FAILED"
    return "WEAK"


def read_csv(path):
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            reader = csv.DictReader(f, dialect=dialect)
            rows = [clean_row(r) for r in reader]
            if rows:
                return rows
        except:
            pass

    for d in [";", ","]:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=d)
            rows = [clean_row(r) for r in reader]
            if rows and len(rows[0].keys()) > 1:
                return rows

    return []


def load_sector():
    data = {}
    for r in read_csv("data/sector_industry.csv"):
        industry_id = str(r.get("IndustryId", "")).strip()
        if industry_id:
            data[industry_id] = {
                "industry": str(r.get("Industry", "")).strip(),
                "sector": str(r.get("Sector", "")).strip()
            }
    return data


def load_companies():
    data = {}
    for r in read_csv("data/companies.csv"):
        t = str(r.get("Ticker", "")).upper().strip()
        if t:
            data[t] = r
    return data


def load_latest(path):
    data = {}
    for r in read_csv(path):
        t = str(r.get("Ticker", "")).upper().strip()
        if not t:
            continue
        k = latest_key(r)
        if t not in data or k > data[t]["_k"]:
            r["_k"] = k
            data[t] = r
    for t in data:
        data[t].pop("_k", None)
    return data


SECTOR_DATA = load_sector()
COMPANIES_DATA = load_companies()
BALANCE_DATA = load_latest("data/balance_sheet.csv")
INCOME_DATA = load_latest("data/income_statement.csv")


def get_active_universe(max_symbols: int = 60):
    return get_scan_universe(max_symbols=max_symbols)


def get_prev(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=12
        ).json()

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


def get_history_levels(symbol):
    if symbol in HISTORY_CACHE:
        return HISTORY_CACHE[symbol]

    today = datetime.utcnow().date()
    from_52w = (today - timedelta(days=365)).isoformat()
    from_5y = (today - timedelta(days=365 * 5)).isoformat()
    to_date = today.isoformat()

    out = {
        "year_high": 0.0,
        "ath_high": 0.0,
        "near_52w_high": False,
        "near_ath": False,
        "ath_breakout_zone": False,
    }

    try:
        url_52w = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{from_52w}/{to_date}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r52 = requests.get(url_52w, timeout=18).json()
        highs_52 = [to_float(x.get("h")) for x in r52.get("results", []) if to_float(x.get("h")) > 0]
        if highs_52:
            out["year_high"] = max(highs_52)
    except:
        pass

    try:
        url_5y = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{from_5y}/{to_date}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r5 = requests.get(url_5y, timeout=22).json()
        highs_5 = [to_float(x.get("h")) for x in r5.get("results", []) if to_float(x.get("h")) > 0]
        if highs_5:
            out["ath_high"] = max(highs_5)
    except:
        pass

    prev = get_prev(symbol)
    if prev:
        price = prev["price"]
        if out["year_high"] > 0:
            out["near_52w_high"] = price >= out["year_high"] * 0.97
        if out["ath_high"] > 0:
            out["near_ath"] = price >= out["ath_high"] * 0.97
            out["ath_breakout_zone"] = price >= out["ath_high"] * 0.995

    HISTORY_CACHE[symbol] = out
    return out


def get_trend(symbol):
    try:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"2024-01-01/2026-12-31?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=22).json()
        data = r.get("results", [])

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


def get_volume_ratio(symbol):
    try:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"2024-01-01/2026-12-31?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=22).json()
        data = r.get("results", [])

        volumes = [to_float(x.get("v")) for x in data if to_float(x.get("v")) > 0]
        if len(volumes) < 20:
            return 1.0

        avg_volume = sum(volumes[-20:]) / 20
        today_volume = volumes[-1]
        return today_volume / avg_volume if avg_volume > 0 else 1.0
    except:
        return 1.0


def get_info(symbol):
    c = COMPANIES_DATA.get(symbol, {})
    industry_id = str(c.get("IndustryId", "")).strip()
    s = SECTOR_DATA.get(industry_id, {})
    return {
        "company": str(c.get("Company Name", "")).strip(),
        "sector": str(s.get("sector", "")).strip(),
        "industry": str(s.get("industry", "")).strip(),
        "industry_id": industry_id
    }


def get_news_catalyst(symbol):
    try:
        info = get_info(symbol)
        company_name = info["company"]

        url = f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=10&apiKey={POLYGON_API_KEY}"
        r = requests.get(url, timeout=12).json()

        news = r.get("results", [])
        if not news:
            return {
                "has_news": False,
                "catalyst_score": 0,
                "note": "لا يوجد أخبار"
            }

        best_score = 0
        best_note = ""

        symbol_lower = symbol.lower()
        company_variants = get_company_name_variants(company_name)

        weak_patterns = [
            "top stocks", "market update", "stock market", "s&p 500",
            "nasdaq", "dow jones", "why investors", "what to know",
            "best stocks", "should you buy", "index fund", "etf",
            "top-ranked stocks", "stocks to buy now", "long term",
            "consumer tech news", "weekly recap", "roundup", "news recap",
            "worth buying", "worth holding", "bullish on", "best way to buy",
            "compare", "comparison", "vs.", "versus", "top picks", "3 stocks",
            "5 stocks", "10 stocks", "owns over", "entire u.s. market"
        ]

        strong_keywords = [
            "earnings", "beats", "guidance", "raises outlook",
            "upgrade", "initiated", "outperform",
            "partnership", "deal", "contract",
            "acquisition", "merger",
            "approval", "fda", "launch",
            "record revenue", "strong growth",
            "buyback", "dividend increase"
        ]

        negative_keywords = [
            "downgrade", "miss", "cuts forecast",
            "lawsuit", "fraud", "investigation",
            "delay", "recall", "decline", "warning",
            "investor alert", "substantial losses", "law firm"
        ]

        for item in news[:7]:
            title = str(item.get("title", "")).strip()
            published = str(item.get("published_utc", "")).strip()
            if not title:
                continue

            title_lower = normalize_text(title)
            if any(w in title_lower for w in weak_patterns):
                continue

            symbol_match = re.search(rf"\b{re.escape(symbol_lower)}\b", title_lower) is not None
            company_match = any(v in title_lower for v in company_variants if len(v) >= 4)
            if not symbol_match and not company_match:
                continue

            score = 0
            if any(k in title_lower for k in strong_keywords):
                score += 6
            if any(k in title_lower for k in negative_keywords):
                score -= 6
            if score == 0:
                continue

            try:
                news_date = datetime.strptime(published[:10], "%Y-%m-%d")
                days_diff = (datetime.utcnow() - news_date).days
                if days_diff <= 1:
                    score += 5 if score > 0 else -5
                elif days_diff <= 2:
                    score += 3 if score > 0 else -3
            except:
                pass

            if abs(score) > abs(best_score):
                best_score = score
                best_note = title[:120]

        return {
            "has_news": best_score != 0,
            "catalyst_score": best_score,
            "note": best_note if best_note else "لا يوجد محفز قوي"
        }
    except Exception as e:
        return {
            "has_news": False,
            "catalyst_score": 0,
            "note": f"خطأ في الأخبار: {str(e)}"
        }


def data_quality_check(symbol, info, financials):
    flags = []
    quality = "high"

    if not info["company"]:
        quality = "low"
        flags.append("اسم الشركة غير متوفر")
    if not info["sector"] or not info["industry"]:
        quality = "low"
        flags.append("بيانات القطاع/الصناعة ناقصة")
    if financials.get("total_assets", 0) == 0:
        quality = "low"
        flags.append("إجمالي الأصول غير متوفر")
    if financials.get("shares", 0) == 0:
        quality = "low"
        flags.append("عدد الأسهم غير متوفر")
    if financials.get("approx_market_cap", 0) == 0:
        quality = "low"
        flags.append("القيمة السوقية التقريبية غير متوفرة")

    return quality, flags


def halal(symbol):
    info = get_info(symbol)
    text = f"{info['sector']} {info['industry']}".lower()

    if info["sector"].lower() in HARAM_SECTORS:
        return {
            "allowed": False,
            "reason": f"قطاع محرم: {info['sector']}",
            "financials": {}
        }

    for word in HARAM_INDUSTRY_KEYWORDS:
        if word in text:
            return {
                "allowed": False,
                "reason": f"نشاط محرم: {word}",
                "financials": {}
            }

    balance = BALANCE_DATA.get(symbol, {})
    income = INCOME_DATA.get(symbol, {})

    total_debt = to_float(balance.get("Short Term Debt")) + to_float(balance.get("Long Term Debt"))
    total_assets = to_float(balance.get("Total Assets"))
    cash = to_float(balance.get("Cash, Cash Equivalents & Short Term Investments"))

    shares = to_float(income.get("Shares (Diluted)"))
    if shares <= 0:
        shares = to_float(income.get("Shares (Basic)"))

    prev = get_prev(symbol)
    current_price = prev["price"] if prev else 0.0
    approx_market_cap = current_price * shares if shares > 0 else 0.0

    debt_to_market_cap = (total_debt / approx_market_cap) if approx_market_cap > 0 else None
    cash_to_assets = (cash / total_assets) if total_assets > 0 else None

    financials = {
        "total_assets": total_assets,
        "cash": cash,
        "total_debt": total_debt,
        "shares": shares,
        "current_price": current_price,
        "approx_market_cap": approx_market_cap,
        "debt_to_market_cap": debt_to_market_cap,
        "cash_to_assets": cash_to_assets,
    }

    if debt_to_market_cap is not None and debt_to_market_cap > 0.33:
        return {
            "allowed": False,
            "reason": f"نسبة الدين إلى القيمة السوقية مرتفعة: {debt_to_market_cap:.2%}",
            "financials": financials
        }

    if cash_to_assets is not None and cash_to_assets > 0.50:
        return {
            "allowed": False,
            "reason": f"نسبة النقد إلى الأصول مرتفعة: {cash_to_assets:.2%}",
            "financials": financials
        }

    return {
        "allowed": True,
        "reason": "مقبول مبدئيًا",
        "financials": financials
    }


def base_analysis(symbol):
    prev = get_prev(symbol)
    if not prev:
        return None

    price = prev["price"]
    high = prev["high"]
    low = prev["low"]
    volume = prev["volume"]
    open_price = prev["open"]

    day_range = max(high - low, 0.01)
    range_pct = day_range / price if price > 0 else 0.0

    momentum = "محايد"
    if price > open_price:
        momentum = "صاعد"
    elif price < open_price:
        momentum = "هابط"

    body_strength = abs(price - open_price) / day_range if day_range > 0 else 0.0
    close_strength = (price - low) / day_range if day_range > 0 else 0.0

    near_high = high > 0 and price >= high * 0.985
    near_low = low > 0 and price <= low * 1.02

    location = "وسط"
    if near_high:
        location = "قرب مقاومة"
    elif near_low:
        location = "قرب دعم"

    return {
        "symbol": symbol,
        "price": price,
        "high": high,
        "low": low,
        "open": open_price,
        "volume": volume,
        "day_range": day_range,
        "range_pct": range_pct,
        "momentum": momentum,
        "body_strength": body_strength,
        "close_strength": close_strength,
        "location": location,
        "near_high": near_high,
        "near_low": near_low
    }


def analyze_symbol_overview(symbol):
    symbol = symbol.upper().strip()

    prev = get_prev(symbol)
    info = get_info(symbol)
    trend_data = get_trend(symbol)
    volume_ratio = get_volume_ratio(symbol)
    history = get_history_levels(symbol)
    news = get_news_catalyst(symbol)
    halal_result = halal(symbol)
    base = base_analysis(symbol)

    if not prev or not base:
        return {
            "symbol": symbol,
            "found": False,
            "message": "تعذر جلب بيانات السهم"
        }

    price = prev["price"]
    high = prev["high"]
    low = prev["low"]
    open_price = prev["open"]

    trade_type = "Breakout" if base["near_high"] and base["momentum"] == "صاعد" else ("Pullback" if base["near_low"] else "Watch")

    breakout_quality = breakout_quality_label(
        trade_type=trade_type,
        momentum=base["momentum"],
        body_strength=base["body_strength"],
        close_strength=base["close_strength"],
        volume_ratio=volume_ratio
    )

    status = "لا توجد فرصة واضحة الآن"
    if trend_data["trend"] == "صاعد قوي" and volume_ratio >= 1.0:
        status = "السهم إيجابي ويستحق المتابعة"
    elif trend_data["trend"] == "هابط":
        status = "السهم ضعيف حاليًا"

    reasons = [f"الاتجاه: {trend_data['trend']}", f"الزخم: {base['momentum']}"]

    if volume_ratio < 0.9:
        reasons.append("السيولة ضعيفة")
    elif volume_ratio >= 1.2:
        reasons.append("السيولة جيدة")

    if news["catalyst_score"] > 0:
        reasons.append("يوجد محفز إيجابي")
    elif news["catalyst_score"] < 0:
        reasons.append("يوجد خبر سلبي")
    else:
        reasons.append("لا يوجد محفز قوي")

    if history["near_ath"]:
        reasons.append("قريب من القمة التاريخية")

    owner_action = owner_decision(
        decision="مراقبة",
        trend=trend_data["trend"],
        breakout_quality=breakout_quality,
        volume_ratio=volume_ratio,
        catalyst_score=news["catalyst_score"]
    )

    execution_status = compute_execution_status(
        trade_type=trade_type,
        decision="مراقبة",
        trend=trend_data["trend"],
        volume_ratio=volume_ratio,
        catalyst_score=news["catalyst_score"],
        breakout_quality=breakout_quality
    )

    ai_summary = " - ".join(reasons)

    return {
        "symbol": symbol,
        "found": True,
        "company": info["company"],
        "sector": info["sector"],
        "industry": info["industry"],
        "price": safe_round(price),
        "high": safe_round(high),
        "low": safe_round(low),
        "open": safe_round(open_price),
        "trend": trend_data["trend"],
        "volume_ratio": round(volume_ratio, 2),
        "momentum": base["momentum"],
        "year_high": safe_round(history["year_high"]),
        "ath_high": safe_round(history["ath_high"]),
        "near_ath": history["near_ath"],
        "catalyst_score": news["catalyst_score"],
        "news_note": news["note"],
        "halal_allowed": halal_result["allowed"],
        "halal_reason": halal_result["reason"],
        "financials": halal_result["financials"],
        "status": status,
        "ai_summary": ai_summary,
        "breakout_quality": breakout_quality,
        "execution_status": execution_status,
        "owner_action": owner_action
    }


def trade_plan_pro(symbol):
    a = base_analysis(symbol)
    if not a:
        return None

    price = a["price"]
    high = a["high"]
    low = a["low"]
    volume = a["volume"]
    range_pct = a["range_pct"]
    near_high = a["near_high"]
    near_low = a["near_low"]
    momentum = a["momentum"]
    location = a["location"]
    body_strength = a["body_strength"]
    close_strength = a["close_strength"]

    if price < LOW_PRICE_HARD_BLOCK:
        return None

    risk_flags = []
    hard_block = False

    if price < LOW_PRICE_WARNING:
        risk_flags.append("سهم منخفض السعر - مخاطرة عالية")

    trend_data = get_trend(symbol)
    volume_ratio = get_volume_ratio(symbol)
    trend = trend_data["trend"]

    history = get_history_levels(symbol)
    near_ath = history["near_ath"]
    ath_breakout_zone = history["ath_breakout_zone"]

    news = get_news_catalyst(symbol)

    trade_type = "Watch"
    entry = price
    stop = low * 0.99 if low > 0 else price * 0.95

    if near_high and momentum == "صاعد":
        trade_type = "Breakout"
        entry = high * 1.002
        stop = low * 0.995
    elif near_low:
        trade_type = "Pullback"
        entry = price
        stop = low * 0.99

    risk = entry - stop
    if risk <= 0:
        stop = price * 0.95
        risk = entry - stop

    risk_pct = risk / entry if entry > 0 else 0.0
    target_1 = entry + risk * 1.5
    target_2 = entry + risk * 2.0

    breakout_quality = breakout_quality_label(
        trade_type=trade_type,
        momentum=momentum,
        body_strength=body_strength,
        close_strength=close_strength,
        volume_ratio=volume_ratio
    )

    quality_score = 32

    if volume > 100_000_000:
        quality_score += 14
    elif volume > 50_000_000:
        quality_score += 11
    elif volume > 10_000_000:
        quality_score += 7
    elif volume > 2_000_000:
        quality_score += 3
    else:
        quality_score -= 9
        risk_flags.append("سيولة يومية ضعيفة")

    if momentum == "صاعد":
        quality_score += 9
    elif momentum == "هابط":
        quality_score -= 6

    if trend == "صاعد قوي":
        quality_score += 13
    elif trend == "صاعد":
        quality_score += 7
    elif trend == "هابط":
        quality_score -= 13

    if trade_type == "Breakout":
        quality_score += 8
    elif trade_type == "Pullback":
        quality_score += 5
    else:
        quality_score -= 3

    if trade_type == "Breakout" and trend == "هابط":
        quality_score -= 16
        risk_flags.append("اختراق عكس الاتجاه")
        hard_block = True

    if trade_type == "Breakout":
        if volume_ratio >= 1.5:
            quality_score += 9
        elif volume_ratio >= 1.2:
            quality_score += 5
        elif volume_ratio >= 1.0:
            quality_score += 1
            risk_flags.append("اختراق يحتاج تأكيد")
        elif volume_ratio >= 0.8:
            quality_score -= 4
            risk_flags.append("اختراق ضعيف بدون سيولة")
        else:
            quality_score -= 12
            risk_flags.append("اختراق فاشل (سيولة ضعيفة جدًا)")
            hard_block = True
    elif trade_type == "Pullback":
        if volume_ratio >= 1.3:
            quality_score += 4
        elif volume_ratio >= 1.0:
            quality_score += 1
        elif volume_ratio < 0.8:
            quality_score -= 5
    else:
        if volume_ratio >= 1.3:
            quality_score += 2
        elif volume_ratio < 0.8:
            quality_score -= 5

    if breakout_quality == "STRONG":
        quality_score += 9
        risk_flags.append("اختراق قوي")
    elif breakout_quality == "WEAK":
        quality_score -= 4
        if trade_type == "Breakout":
            risk_flags.append("شمعة اختراق ضعيفة")
    elif breakout_quality == "FAILED":
        quality_score -= 13
        risk_flags.append("سلوك اختراق فاشل")
        if trade_type == "Breakout":
            hard_block = True

    if trade_type == "Breakout" and location == "قرب مقاومة":
        quality_score += 5
    elif trade_type == "Pullback" and location == "قرب دعم":
        quality_score += 6

    if ath_breakout_zone and momentum == "صاعد":
        quality_score += 6
        risk_flags.append("قرب اختراق ATH")
    elif near_ath and trade_type == "Breakout" and breakout_quality != "STRONG":
        quality_score -= 3
        risk_flags.append("قرب ATH بدون تأكيد")

    if risk_pct <= 0.02:
        quality_score += 8
    elif risk_pct <= 0.04:
        quality_score += 4
    elif risk_pct <= 0.07:
        quality_score -= 2
    else:
        quality_score -= 8
        risk_flags.append("مخاطرة مرتفعة")
        hard_block = True

    if 0.02 <= range_pct <= 0.08:
        quality_score += 5
    elif range_pct > 0.15:
        quality_score -= 7
        risk_flags.append("ذبذبة يومية عالية")

    if abs(news["catalyst_score"]) >= 6:
        quality_score += news["catalyst_score"]
    elif abs(news["catalyst_score"]) >= 3:
        quality_score += news["catalyst_score"] * 0.5

    if news["catalyst_score"] >= 6:
        risk_flags.append("خبر إيجابي محفز")
    elif news["catalyst_score"] <= -6:
        risk_flags.append("خبر سلبي ⚠️")

    info = get_info(symbol)
    h = halal(symbol)
    data_quality, dq_flags = data_quality_check(symbol, info, h["financials"])
    risk_flags.extend(dq_flags)

    if data_quality == "low":
        quality_score -= 9

    quality_score = max(1, min(100, quality_score))

    # ربط القرار بالمخاطرة بشكل صارم
    if risk_pct > 0.10:
        decision = "مراقبة"
    elif risk_pct > 0.08:
        if quality_score >= 70:
            decision = "دخول بحذر"
        elif quality_score >= 56:
            decision = "مراقبة"
        else:
            return None
    else:
        if hard_block and quality_score < 70:
            decision = "مراقبة"
        elif quality_score >= 78:
            decision = "دخول قوي"
        elif quality_score >= 68:
            decision = "دخول بحذر"
        elif quality_score >= 56:
            decision = "مراقبة"
        else:
            return None

    if trade_type == "Breakout" and volume_ratio < 0.8:
        if decision in {"دخول قوي", "دخول بحذر"}:
            decision = "مراقبة"

    if trend == "هابط" and decision in {"دخول قوي", "دخول بحذر"}:
        decision = "مراقبة"

    if data_quality == "low" and decision == "دخول قوي":
        decision = "دخول بحذر"

    if decision == "مراقبة" and quality_score < 60 and trade_type == "Watch":
        return None

    reasons = []
    if trend == "صاعد قوي":
        reasons.append("الاتجاه صاعد قوي")
    elif trend == "صاعد":
        reasons.append("الاتجاه إيجابي")
    elif trend == "هابط":
        reasons.append("الاتجاه سلبي")
    else:
        reasons.append("الاتجاه متذبذب")

    if volume_ratio < 1:
        reasons.append("السيولة ضعيفة")
    elif volume_ratio >= 1.5:
        reasons.append("السيولة قوية")
    elif volume_ratio >= 1.0:
        reasons.append("السيولة مقبولة")

    if news["catalyst_score"] > 0:
        reasons.append("يوجد محفز إيجابي")
    elif news["catalyst_score"] < 0:
        reasons.append("يوجد خبر سلبي")
    else:
        reasons.append("لا يوجد محفز قوي")

    if trade_type == "Breakout":
        reasons.append("محاولة اختراق")
    elif trade_type == "Pullback":
        reasons.append("ارتداد من دعم")
    else:
        reasons.append("فرصة واعدة مشروطة")

    if breakout_quality == "STRONG":
        reasons.append("شمعة الاختراق قوية")
    elif breakout_quality == "WEAK":
        reasons.append("شمعة الاختراق ضعيفة")
    elif breakout_quality == "FAILED":
        reasons.append("شمعة الاختراق فشلت")

    if data_quality == "low":
        reasons.append("جودة البيانات ضعيفة")

    ai_summary = " - ".join(reasons) if reasons else "لا يوجد وضوح كافي"

    valid_for = estimate_validity(
        trade_type=trade_type,
        trend=trend,
        volume_ratio=volume_ratio,
        catalyst_score=news["catalyst_score"]
    )

    rank_label = make_rank_label(quality_score)
    execution_status = compute_execution_status(
        trade_type=trade_type,
        decision=decision,
        trend=trend,
        volume_ratio=volume_ratio,
        catalyst_score=news["catalyst_score"],
        breakout_quality=breakout_quality
    )
    owner_action = owner_decision(decision, trend, breakout_quality, volume_ratio, news["catalyst_score"])

    return {
        "symbol": symbol,
        "type": trade_type,
        "decision": decision,
        "entry": safe_round(entry),
        "stop_loss": safe_round(stop),
        "target_1": safe_round(target_1),
        "target_2": safe_round(target_2),
        "risk_pct": safe_round(risk_pct * 100),
        "quality_score": quality_score,
        "rank_label": rank_label,
        "valid_for": valid_for,
        "trend": trend,
        "volume_ratio": round(volume_ratio, 2),
        "data_quality": data_quality,
        "catalyst_score": news["catalyst_score"],
        "news_note": news["note"],
        "risk_flags": risk_flags,
        "ai_summary": ai_summary,
        "breakout_quality": breakout_quality,
        "execution_status": execution_status,
        "owner_action": owner_action
    }


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/health")
def health():
    universe = get_active_universe(max_symbols=60)
    return {
        "message": "Stock Radar AI is running 🚀",
        "loaded": {
            "companies": len(COMPANIES_DATA),
            "sector_industry": len(SECTOR_DATA),
            "balance_rows": len(BALANCE_DATA),
            "income_rows": len(INCOME_DATA),
        },
        "universe_count": len(universe)
    }


@app.get("/trade-scan")
def trade_scan():
    trades = []
    rejected = []
    errors = []

    universe = get_active_universe(max_symbols=60)

    for s in universe:
        try:
            prev = get_prev(s)
            if prev and prev["price"] < LOW_PRICE_HARD_BLOCK:
                rejected.append({
                    "symbol": s,
                    "reason": f"سعر أقل من {LOW_PRICE_HARD_BLOCK}$"
                })
                continue

            h = halal(s)
            if not h["allowed"]:
                rejected.append({
                    "symbol": s,
                    "reason": h["reason"]
                })
                continue

            t = trade_plan_pro(s)
            if t:
                info = get_info(s)
                t["company"] = info["company"]
                t["sector"] = info["sector"]
                t["industry"] = info["industry"]
                t["financials"] = h["financials"]
                trades.append(t)

        except Exception as e:
            errors.append({
                "symbol": s,
                "error": str(e)
            })
            continue

    trades = sorted(
        trades,
        key=lambda x: (decision_priority(x["decision"]), x["quality_score"]),
        reverse=True
    )

    top_ranked = trades[:5]
    strong_entries = [x for x in trades if x["decision"] == "دخول قوي"]
    cautious_entries = [x for x in trades if x["decision"] == "دخول بحذر"]
    watch = [x for x in trades if x["decision"] == "مراقبة"]

    return {
        "universe_count": len(universe),
        "count": len(trades),
        "strong_entries_count": len(strong_entries),
        "cautious_entries_count": len(cautious_entries),
        "watchlist_count": len(watch),
        "top_ranked_count": len(top_ranked),
        "top_ranked": top_ranked,
        "strong_entries": strong_entries,
        "cautious_entries": cautious_entries,
        "watchlist": watch,
        "rejected_count": len(rejected),
        "rejected": rejected[:30],
        "errors_count": len(errors),
        "errors": errors[:20]
    }


@app.get("/debug/{symbol}")
def debug_symbol(symbol: str):
    symbol = symbol.upper()
    overview = analyze_symbol_overview(symbol)
    trade = trade_plan_pro(symbol)

    return {
        "symbol": symbol,
        "sector_info": get_info(symbol),
        "balance": BALANCE_DATA.get(symbol, {}),
        "income": INCOME_DATA.get(symbol, {}),
        "history_levels": get_history_levels(symbol),
        "halal_check": halal(symbol),
        "base_analysis": base_analysis(symbol),
        "news_catalyst": get_news_catalyst(symbol),
        "trade_plan": trade,
        "overview": overview
    }
