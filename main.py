from fastapi import FastAPI
import requests
import os
import csv
import re
from datetime import datetime, timedelta
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


# -------------------- utils --------------------
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
    text = re.sub(r"[^a-z0-9\s&.-]", " ", text)
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
        " group", " technologies", " technology", " systems", " international",
        " company", " companies", " class a", " class c", " common stock"
    ]

    for n in noise:
        if name.endswith(n):
            variants.add(name[: -len(n)].strip())

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

    cleaned = list(dict.fromkeys(cleaned))
    return cleaned


# -------------------- CSV reader --------------------
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


# -------------------- loaders --------------------
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


# -------------------- universe --------------------
def get_active_universe(max_symbols: int = 60):
    try:
        return get_scan_universe(max_symbols=max_symbols)
    except:
        return []


# -------------------- market data --------------------
def get_prev(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=12
        ).json()
        d = r["results"][0]
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


# -------------------- trend / volume AI --------------------
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


# -------------------- info --------------------
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


# -------------------- news / catalyst (cleaned) --------------------
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
            "top-ranked stocks", "stocks to buy now", "long term"
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
            "delay", "recall", "decline", "warning"
        ]

        for item in news[:7]:
            title = str(item.get("title", "")).strip()
            published = str(item.get("published_utc", "")).strip()

            if not title:
                continue

            title_lower = normalize_text(title)

            # استبعاد الأخبار العامة
            if any(w in title_lower for w in weak_patterns):
                continue

            # يجب أن يكون الخبر متعلقًا بالسهم نفسه
            symbol_match = re.search(rf"\b{re.escape(symbol_lower)}\b", title_lower) is not None
            company_match = any(v in title_lower for v in company_variants if len(v) >= 4)

            if not symbol_match and not company_match:
                continue

            score = 0

            if any(k in title_lower for k in strong_keywords):
                score += 6

            if any(k in title_lower for k in negative_keywords):
                score -= 6

            try:
                news_date = datetime.strptime(published[:10], "%Y-%m-%d")
                days_diff = (datetime.utcnow() - news_date).days

                if days_diff <= 1:
                    score += 5
                elif days_diff <= 2:
                    score += 3
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

    except:
        return {
            "has_news": False,
            "catalyst_score": 0,
            "note": "خطأ في الأخبار"
        }


# -------------------- data quality --------------------
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


# -------------------- halal / financial filter --------------------
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


# -------------------- base analysis --------------------
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

    volume_signal = "ضعيفة"
    if volume > 100_000_000:
        volume_signal = "عالية جدًا"
    elif volume > 50_000_000:
        volume_signal = "قوية جدًا"
    elif volume > 10_000_000:
        volume_signal = "قوية"
    elif volume > 2_000_000:
        volume_signal = "متوسطة"

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
        "volume_signal": volume_signal,
        "location": location,
        "near_high": near_high,
        "near_low": near_low
    }


# -------------------- professional trade engine --------------------
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

    if price < LOW_PRICE_HARD_BLOCK:
        return None

    risk_flags = []
    if price < LOW_PRICE_WARNING:
        risk_flags.append("سهم منخفض السعر - مخاطرة عالية")

    if volume < 2_000_000:
        return None

    if range_pct > 0.15:
        return None

    trend_data = get_trend(symbol)
    volume_ratio = get_volume_ratio(symbol)
    trend = trend_data["trend"]

    history = get_history_levels(symbol)
    near_ath = history["near_ath"]
    ath_breakout_zone = history["ath_breakout_zone"]

    news = get_news_catalyst(symbol)

    trade_type = None
    entry = None
    stop = None
    valid_for = None

    if near_high and momentum == "صاعد":
        trade_type = "Breakout"
        entry = high * 1.002
        stop = low * 0.995
        valid_for = "Intraday"
    elif near_low:
        trade_type = "Pullback"
        entry = price
        stop = low * 0.99
        valid_for = "1-3 days"

    if not trade_type:
        return None

    risk = entry - stop
    if risk <= 0:
        return None

    risk_pct = risk / entry

    if risk_pct > 0.08:
        return None

    target_1 = entry + risk * 1.5
    target_2 = entry + risk * 2.0

    quality_score = 42

    # volume strength
    if volume > 120_000_000:
        quality_score += 15
    elif volume > 80_000_000:
        quality_score += 12
    elif volume > 50_000_000:
        quality_score += 9
    elif volume > 10_000_000:
        quality_score += 6
    else:
        quality_score += 3

    # momentum
    if momentum == "صاعد":
        quality_score += 12
    else:
        quality_score -= 6

    # trend AI
    if trend == "صاعد قوي":
        quality_score += 10
    elif trend == "صاعد":
        quality_score += 5
    elif trend == "هابط":
        quality_score -= 10

    # volume ratio AI
    if volume_ratio >= 2.0:
        quality_score += 8
    elif volume_ratio >= 1.5:
        quality_score += 5
    elif volume_ratio >= 1.2:
        quality_score += 2
    elif volume_ratio >= 0.9:
        quality_score += 0
    elif volume_ratio >= 0.8:
        quality_score -= 2
    else:
        quality_score -= 5

    # breakout confidence
    if trade_type == "Breakout":
        if volume_ratio < 0.9:
            quality_score -= 6
            risk_flags.append("اختراق ضعيف (بدون سيولة كافية)")
        elif volume_ratio < 1.1:
            risk_flags.append("اختراق يحتاج تأكيد سيولة")

    # position
    if trade_type == "Breakout" and location == "قرب مقاومة":
        quality_score += 10
    elif trade_type == "Pullback" and location == "قرب دعم":
        quality_score += 8

    # risk
    if risk_pct <= 0.02:
        quality_score += 10
    elif risk_pct <= 0.04:
        quality_score += 6
    else:
        quality_score -= 5

    # range
    if range_pct <= 0.03:
        quality_score += 6
    elif range_pct <= 0.06:
        quality_score += 3
    else:
        quality_score -= 5

    # ATH logic
    if ath_breakout_zone and momentum == "صاعد":
        quality_score += 8
        risk_flags.append("قرب اختراق ATH")
    elif near_ath:
        quality_score -= 6
        risk_flags.append("قرب ATH بدون اختراق")

    # News / Catalyst
    quality_score += news["catalyst_score"]
    if news["catalyst_score"] >= 6:
        risk_flags.append("خبر إيجابي محفز")
    elif news["catalyst_score"] <= -6:
        risk_flags.append("خبر سلبي ⚠️")

    # data quality
    info = get_info(symbol)
    h = halal(symbol)
    data_quality, dq_flags = data_quality_check(symbol, info, h["financials"])
    risk_flags.extend(dq_flags)

    if data_quality == "low":
        quality_score -= 12

    quality_score = max(1, min(100, quality_score))

    if quality_score >= 82:
        decision = "دخول قوي"
    elif quality_score >= 72:
        decision = "دخول بحذر"
    elif quality_score >= 62:
        decision = "مراقبة"
    else:
        return None

    if data_quality == "low" and decision in {"دخول قوي", "دخول بحذر"}:
        decision = "مراقبة"

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
        "trend": trend,
        "volume_ratio": round(volume_ratio, 2),
        "data_quality": data_quality,
        "catalyst_score": news["catalyst_score"],
        "news_note": news["note"],
        "risk_flags": risk_flags
    }


# -------------------- API --------------------
@app.get("/")
def home():
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

    universe = get_active_universe(max_symbols=60)

    for s in universe:
        prev = get_prev(s)
        if prev and prev["price"] < LOW_PRICE_HARD_BLOCK:
            rejected.append({"symbol": s, "reason": f"سعر أقل من {LOW_PRICE_HARD_BLOCK}$"})
            continue

        h = halal(s)
        if not h["allowed"]:
            rejected.append({"symbol": s, "reason": h["reason"]})
            continue

        t = trade_plan_pro(s)
        if t:
            info = get_info(s)
            t["company"] = info["company"]
            t["sector"] = info["sector"]
            t["industry"] = info["industry"]
            t["financials"] = h["financials"]
            trades.append(t)

    trades = sorted(trades, key=lambda x: x["quality_score"], reverse=True)

    strong_entries = [x for x in trades if x["decision"] == "دخول قوي"]
    cautious_entries = [x for x in trades if x["decision"] == "دخول بحذر"]
    watch = [x for x in trades if x["decision"] == "مراقبة"]

    return {
        "universe_count": len(universe),
        "count": len(trades),
        "strong_entries_count": len(strong_entries),
        "cautious_entries_count": len(cautious_entries),
        "watchlist_count": len(watch),
        "strong_entries": strong_entries,
        "cautious_entries": cautious_entries,
        "watchlist": watch,
        "rejected_count": len(rejected),
        "rejected": rejected[:30]
    }


@app.get("/debug/{symbol}")
def debug_symbol(symbol: str):
    symbol = symbol.upper()
    return {
        "symbol": symbol,
        "sector_info": get_info(symbol),
        "balance": BALANCE_DATA.get(symbol, {}),
        "income": INCOME_DATA.get(symbol, {}),
        "history_levels": get_history_levels(symbol),
        "halal_check": halal(symbol),
        "base_analysis": base_analysis(symbol),
        "news_catalyst": get_news_catalyst(symbol),
        "trade_plan": trade_plan_pro(symbol),
    }
