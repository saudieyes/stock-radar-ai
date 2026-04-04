from fastapi import FastAPI
import requests
import os
import csv
from datetime import datetime, timedelta

app = FastAPI()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

ALLOWED_TEST_SYMBOLS = ["AAPL", "NVDA", "JPM", "TSLA"]

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

def latest_key(row):
    return (
        parse_date_safe(row.get("Publish Date", "")),
        int(to_float(row.get("Fiscal Year", 0))),
        period_rank(row.get("Fiscal Period", ""))
    )

def safe_round(x, digits=2):
    try:
        return round(float(x), digits)
    except:
        return x

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

# -------------------- market data --------------------
def get_prev(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=10
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
        r52 = requests.get(url_52w, timeout=15).json()
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
        r5 = requests.get(url_5y, timeout=20).json()
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

# -------------------- halal --------------------
def halal(symbol):
    i = get_info(symbol)
    txt = f"{i['sector']} {i['industry']}".lower()

    if i["sector"].lower() in HARAM_SECTORS:
        return {
            "allowed": False,
            "reason": f"قطاع محرم: {i['sector']}",
            "financials": {}
        }

    for w in HARAM_INDUSTRY_KEYWORDS:
        if w in txt:
            return {
                "allowed": False,
                "reason": f"نشاط محرم: {w}",
                "financials": {}
            }

    b = BALANCE_DATA.get(symbol, {})
    inc = INCOME_DATA.get(symbol, {})

    debt = to_float(b.get("Short Term Debt")) + to_float(b.get("Long Term Debt"))
    assets = to_float(b.get("Total Assets"))
    cash = to_float(b.get("Cash, Cash Equivalents & Short Term Investments"))

    shares = to_float(inc.get("Shares (Diluted)"))
    if shares <= 0:
        shares = to_float(inc.get("Shares (Basic)"))

    p = get_prev(symbol)
    price = p["price"] if p else 0.0
    mcap = price * shares if shares > 0 else 0.0

    debt_to_market_cap = (debt / mcap) if mcap > 0 else None
    cash_to_assets = (cash / assets) if assets > 0 else None

    financials = {
        "total_assets": assets,
        "cash": cash,
        "total_debt": debt,
        "shares": shares,
        "current_price": price,
        "approx_market_cap": mcap,
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
    p = get_prev(symbol)
    if not p:
        return None

    price = p["price"]
    high = p["high"]
    low = p["low"]
    volume = p["volume"]
    open_price = p["open"]

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

    location = "وسط"
    near_high = high > 0 and price >= high * 0.985
    near_low = low > 0 and price <= low * 1.02

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
    if LOW_PRICE_HARD_BLOCK <= price < LOW_PRICE_WARNING:
        risk_flags.append("سهم منخفض السعر - مخاطرة عالية")

    if price <= 0 or high <= 0 or low <= 0:
        return None

    if volume < 2_000_000:
        return None

    if range_pct > 0.15:
        return None

    history = get_history_levels(symbol)
    near_52w_high = history["near_52w_high"]
    near_ath = history["near_ath"]
    ath_breakout_zone = history["ath_breakout_zone"]

    if near_ath and momentum != "صاعد" and near_high:
        risk_flags.append("قرب قمة تاريخية بدون زخم كافٍ")

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

    risk_pct = risk / entry if entry > 0 else 0

    if risk_pct > 0.08:
        return None

    target_1 = entry + risk * 1.5
    target_2 = entry + risk * 2.0
    rr_1 = (target_1 - entry) / risk if risk > 0 else 0
    rr_2 = (target_2 - entry) / risk if risk > 0 else 0

    quality_score = 42

    # volume
    if volume > 120_000_000:
        quality_score += 15
    elif volume > 80_000_000:
        quality_score += 12
    elif volume > 50_000_000:
        quality_score += 9
    elif volume > 10_000_000:
        quality_score += 6
    elif volume > 2_000_000:
        quality_score += 3

    # momentum
    if trade_type == "Breakout":
        if momentum == "صاعد":
            quality_score += 16
        elif momentum == "محايد":
            quality_score += 4
        else:
            quality_score -= 12
    elif trade_type == "Pullback":
        if momentum == "صاعد":
            quality_score += 8
        elif momentum == "محايد":
            quality_score += 3
        elif momentum == "هابط":
            quality_score -= 8  # تشديد مهم

    # position
    if trade_type == "Breakout" and location == "قرب مقاومة":
        quality_score += 10
    if trade_type == "Pullback" and location == "قرب دعم":
        quality_score += 8

    # risk
    if risk_pct <= 0.015:
        quality_score += 14
    elif risk_pct <= 0.025:
        quality_score += 10
    elif risk_pct <= 0.04:
        quality_score += 6
    elif risk_pct <= 0.06:
        quality_score += 2
    else:
        quality_score -= 6

    # range
    if range_pct <= 0.03:
        quality_score += 8
    elif range_pct <= 0.06:
        quality_score += 5
    elif range_pct <= 0.10:
        quality_score += 1
    else:
        quality_score -= 5

    # ATH / 52W
    if ath_breakout_zone and trade_type == "Breakout" and momentum == "صاعد":
        quality_score += 8
        risk_flags.append("قرب/اختراق قمة تاريخية")
    elif near_ath and trade_type == "Breakout" and momentum == "صاعد":
        quality_score += 4
        risk_flags.append("قرب قمة تاريخية")
    elif near_52w_high and trade_type == "Breakout" and momentum == "صاعد":
        quality_score += 2
        risk_flags.append("قرب أعلى مستوى سنوي")

    if near_ath and momentum == "هابط":
        quality_score -= 10

    # extra pullback penalties
    if trade_type == "Pullback":
        if volume < 20_000_000:
            quality_score -= 4
        if range_pct > 0.06:
            quality_score -= 4

    quality_score = min(100, max(1, quality_score))

    # decision
    if quality_score >= 82:
        decision = "دخول"
    elif quality_score >= 65:
        decision = "مراقبة"
    else:
        decision = "تجنب"

    confidence = "ضعيف"
    if quality_score >= 88:
        confidence = "عالي جدًا 🔥"
    elif quality_score >= 78:
        confidence = "عالي"
    elif quality_score >= 65:
        confidence = "متوسط"
    else:
        confidence = "ضعيف"

    if quality_score < 65:
        return None

    return {
        "symbol": symbol,
        "type": trade_type,
        "decision": decision,
        "entry": safe_round(entry),
        "stop_loss": safe_round(stop),
        "risk_per_share": safe_round(risk),
        "risk_pct": safe_round(risk_pct * 100),
        "target_1": safe_round(target_1),
        "target_2": safe_round(target_2),
        "rr_1": safe_round(rr_1),
        "rr_2": safe_round(rr_2),
        "valid_for": valid_for,
        "confidence": confidence,
        "quality_score": quality_score,
        "price": safe_round(price),
        "high": safe_round(high),
        "low": safe_round(low),
        "volume": int(volume),
        "momentum": momentum,
        "location": location,
        "year_high": safe_round(history["year_high"]),
        "ath_high": safe_round(history["ath_high"]),
        "near_52w_high": near_52w_high,
        "near_ath": near_ath,
        "ath_breakout_zone": ath_breakout_zone,
        "risk_flags": risk_flags,
    }

# -------------------- endpoints --------------------
@app.get("/")
def home():
    return {
        "message": "Stock Radar AI is running 🚀",
        "loaded": {
            "companies": len(COMPANIES_DATA),
            "sector_industry": len(SECTOR_DATA),
            "balance_rows": len(BALANCE_DATA),
            "income_rows": len(INCOME_DATA),
        }
    }

@app.get("/scan")
def scan():
    results = []
    rejected = []

    for s in ALLOWED_TEST_SYMBOLS:
        info = get_info(s)
        prev = get_prev(s)

        if prev and prev["price"] < LOW_PRICE_HARD_BLOCK:
            rejected.append({
                "symbol": s,
                "reason": f"سعر السهم أقل من {LOW_PRICE_HARD_BLOCK}$ - مرفوض بسبب المخاطرة",
                "sector": info["sector"],
                "industry": info["industry"],
                "financials": {}
            })
            continue

        h = halal(s)

        if not h["allowed"]:
            rejected.append({
                "symbol": s,
                "reason": h["reason"],
                "sector": info["sector"],
                "industry": info["industry"],
                "financials": h["financials"]
            })
            continue

        base = base_analysis(s)
        if base:
            warning = None
            if base["price"] < LOW_PRICE_WARNING:
                warning = "مخاطرة عالية - سهم منخفض السعر"

            results.append({
                "symbol": s,
                "companyName": info["company"],
                "sector": info["sector"],
                "industry": info["industry"],
                "price": safe_round(base["price"]),
                "high": safe_round(base["high"]),
                "low": safe_round(base["low"]),
                "volume": int(base["volume"]),
                "momentumSignal": base["momentum"],
                "volumeSignal": base["volume_signal"],
                "locationSignal": base["location"],
                "warning": warning,
                "financials": h["financials"]
            })

    return {
        "tested_symbols": ALLOWED_TEST_SYMBOLS,
        "accepted_count": len(results),
        "rejected_count": len(rejected),
        "accepted": results,
        "rejected": rejected,
    }

@app.get("/trade-scan")
def trade_scan():
    trades = []
    rejected = []

    for s in ALLOWED_TEST_SYMBOLS:
        info = get_info(s)
        prev = get_prev(s)

        if prev and prev["price"] < LOW_PRICE_HARD_BLOCK:
            rejected.append({
                "symbol": s,
                "reason": f"سعر السهم أقل من {LOW_PRICE_HARD_BLOCK}$ - مرفوض بسبب المخاطرة العالية"
            })
            continue

        h = halal(s)
        if not h["allowed"]:
            rejected.append({
                "symbol": s,
                "reason": h["reason"]
            })
            continue

        plan = trade_plan_pro(s)
        if not plan:
            continue

        plan["company"] = info["company"]
        plan["sector"] = info["sector"]
        plan["industry"] = info["industry"]
        plan["financials"] = h["financials"]
        trades.append(plan)

    trades = sorted(trades, key=lambda x: x["quality_score"], reverse=True)

    top_picks = [x for x in trades if x["decision"] == "دخول"]
    watchlist = [x for x in trades if x["decision"] == "مراقبة"]

    return {
        "count": len(trades),
        "top_picks_count": len(top_picks),
        "watchlist_count": len(watchlist),
        "rejected_count": len(rejected),
        "top_picks": top_picks,
        "watchlist": watchlist,
        "rejected": rejected
    }

@app.get("/analyze/{symbol}")
def analyze_single(symbol: str):
    symbol = symbol.upper()
    info = get_info(symbol)
    prev = get_prev(symbol)

    if prev and prev["price"] < LOW_PRICE_HARD_BLOCK:
        return {
            "symbol": symbol,
            "company": info["company"],
            "sector": info["sector"],
            "industry": info["industry"],
            "halal": {
                "allowed": False,
                "reason": f"سعر السهم أقل من {LOW_PRICE_HARD_BLOCK}$ - مرفوض بسبب المخاطرة العالية"
            },
            "trade_plan": None
        }

    h = halal(symbol)

    if not h["allowed"]:
        return {
            "symbol": symbol,
            "company": info["company"],
            "sector": info["sector"],
            "industry": info["industry"],
            "halal": h,
            "trade_plan": None
        }

    plan = trade_plan_pro(symbol)

    return {
        "symbol": symbol,
        "company": info["company"],
        "sector": info["sector"],
        "industry": info["industry"],
        "halal": h,
        "trade_plan": plan
    }

@app.get("/debug/{symbol}")
def debug_symbol(symbol: str):
    symbol = symbol.upper()
    return {
        "symbol": symbol,
        "company": COMPANIES_DATA.get(symbol, {}),
        "sector_info": get_info(symbol),
        "balance": BALANCE_DATA.get(symbol, {}),
        "income": INCOME_DATA.get(symbol, {}),
        "history_levels": get_history_levels(symbol),
        "halal_check": halal(symbol),
        "base_analysis": base_analysis(symbol),
        "trade_plan": trade_plan_pro(symbol),
    }
