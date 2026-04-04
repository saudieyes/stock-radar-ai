from fastapi import FastAPI
import requests
import os
import csv

app = FastAPI()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

ALLOWED_TEST_SYMBOLS = ["AAPL", "NVDA", "JPM", "TSLA"]

SECTOR_DATA = {}
COMPANIES_DATA = {}
BALANCE_DATA = {}
INCOME_DATA = {}

HARAM_SECTORS = {
    "financial services",
    "banks",
    "insurance",
}

HARAM_INDUSTRY_KEYWORDS = [
    "bank",
    "insurance",
    "tobacco",
    "alcohol",
    "gambling",
    "casino",
    "betting",
    "credit services",
    "mortgage",
    "reit mortgage",
]

def load_sector_industry():
    data = {}
    path = "data/sector_industry.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            industry_id = str(row.get("IndustryId", "")).strip()
            if industry_id:
                data[industry_id] = {
                    "industry": str(row.get("Industry", "")).strip(),
                    "sector": str(row.get("Sector", "")).strip(),
                }
    return data

def load_companies():
    data = {}
    path = "data/companies.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ticker = str(row.get("Ticker", "")).strip().upper()
            if not ticker:
                continue

            data[ticker] = row
    return data

def load_latest_balance():
    data = {}
    path = "data/balance_sheet.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ticker = str(row.get("Ticker", "")).strip().upper()
            fiscal_year = str(row.get("Fiscal Year", "")).strip()
            fiscal_period = str(row.get("Fiscal Period", "")).strip()
            if not ticker:
                continue

            key = (fiscal_year, fiscal_period)
            if ticker not in data or key > data[ticker]["_key"]:
                row["_key"] = key
                data[ticker] = row

    for t in list(data.keys()):
        data[t].pop("_key", None)

    return data

def load_latest_income():
    data = {}
    path = "data/income_statement.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            ticker = str(row.get("Ticker", "")).strip().upper()
            fiscal_year = str(row.get("Fiscal Year", "")).strip()
            fiscal_period = str(row.get("Fiscal Period", "")).strip()
            if not ticker:
                continue

            key = (fiscal_year, fiscal_period)
            if ticker not in data or key > data[ticker]["_key"]:
                row["_key"] = key
                data[ticker] = row

    for t in list(data.keys()):
        data[t].pop("_key", None)

    return data

def to_float(value):
    try:
        if value is None or value == "":
            return 0.0
        return float(str(value).replace(",", ""))
    except:
        return 0.0

SECTOR_DATA = load_sector_industry()
COMPANIES_DATA = load_companies()
BALANCE_DATA = load_latest_balance()
INCOME_DATA = load_latest_income()

@app.get("/")
def home():
    return {"message": "Stock Radar AI is running 🚀"}

def get_test_symbols():
    return ALLOWED_TEST_SYMBOLS

def get_polygon_prev(symbol):
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}"
    res = requests.get(url, timeout=20).json()

    if "results" not in res or not res["results"]:
        return None

    d = res["results"][0]
    return {
        "price": d.get("c", 0),
        "high": d.get("h", 0),
        "low": d.get("l", 0),
        "volume": d.get("v", 0),
        "open": d.get("o", 0),
    }

def get_company_sector_info(symbol):
    company = COMPANIES_DATA.get(symbol, {})
    industry_id = str(company.get("IndustryId", "")).strip()
    info = SECTOR_DATA.get(industry_id, {})

    return {
        "company_name": str(company.get("Company Name", "")).strip(),
        "industry_id": industry_id,
        "industry": str(info.get("industry", "")).strip(),
        "sector": str(info.get("sector", "")).strip(),
    }

def halal_filter(symbol):
    info = get_company_sector_info(symbol)
    sector = info["sector"].lower()
    industry = info["industry"].lower()

    if sector in HARAM_SECTORS:
        return {
            "allowed": False,
            "reason": f"قطاع محرم: {info['sector']}",
            "sector": info["sector"],
            "industry": info["industry"],
        }

    text = f"{sector} {industry}"
    for word in HARAM_INDUSTRY_KEYWORDS:
        if word in text:
            return {
                "allowed": False,
                "reason": f"نشاط محرم: {word}",
                "sector": info["sector"],
                "industry": info["industry"],
            }

    balance = BALANCE_DATA.get(symbol, {})
    total_assets = to_float(balance.get("Total Assets"))
    short_term_debt = to_float(balance.get("Short Term Debt"))
    long_term_debt = to_float(balance.get("Long Term Debt"))
    total_debt = short_term_debt + long_term_debt

    if total_assets > 0:
        debt_ratio = total_debt / total_assets
        if debt_ratio > 0.33:
            return {
                "allowed": False,
                "reason": f"نسبة ديون مرتفعة: {debt_ratio:.2%}",
                "sector": info["sector"],
                "industry": info["industry"],
            }

    return {
        "allowed": True,
        "reason": "مقبول مبدئيًا",
        "sector": info["sector"],
        "industry": info["industry"],
    }

def analyze_stock(symbol):
    prev = get_polygon_prev(symbol)
    if not prev:
        return None

    price = prev["price"]
    high = prev["high"]
    low = prev["low"]
    volume = prev["volume"]
    open_price = prev["open"]

    momentum_signal = "محايد"
    if price > open_price:
        momentum_signal = "صاعد"
    elif price < open_price:
        momentum_signal = "هابط"

    volume_signal = "ضعيفة"
    if volume > 5_000_000:
        volume_signal = "قوية"
    elif volume > 1_000_000:
        volume_signal = "متوسطة"

    location_signal = "وسط"
    if high > 0:
        near_high = price >= (high * 0.985)
        near_low = price <= (low * 1.015 if low > 0 else low)
        if near_high:
            location_signal = "قرب مقاومة"
        elif near_low:
            location_signal = "قرب دعم"

    score = 40
    if momentum_signal == "صاعد":
        score += 20
    if volume_signal == "قوية":
        score += 20
    elif volume_signal == "متوسطة":
        score += 10
    if location_signal == "قرب مقاومة":
        score += 10
    elif location_signal == "قرب دعم":
        score += 8

    decision = "تجنب"
    if score >= 75:
        decision = "دخول"
    elif score >= 55:
        decision = "مراقبة"

    return {
        "symbol": symbol,
        "price": price,
        "high": high,
        "low": low,
        "volume": volume,
        "momentumSignal": momentum_signal,
        "volumeSignal": volume_signal,
        "locationSignal": location_signal,
        "score": score,
        "decision": decision,
    }

@app.get("/scan")
def scan():
    symbols = get_test_symbols()
    results = []
    rejected = []

    for symbol in symbols:
        halal = halal_filter(symbol)
        if not halal["allowed"]:
            rejected.append({
                "symbol": symbol,
                "reason": halal["reason"],
                "sector": halal["sector"],
                "industry": halal["industry"],
            })
            continue

        analysis = analyze_stock(symbol)
        if analysis:
            info = get_company_sector_info(symbol)
            analysis["companyName"] = info["company_name"]
            analysis["sector"] = info["sector"]
            analysis["industry"] = info["industry"]
            results.append(analysis)

    return {
        "tested_symbols": symbols,
        "accepted_count": len(results),
        "rejected_count": len(rejected),
        "accepted": results,
        "rejected": rejected,
    }

@app.get("/analyze/{symbol}")
def analyze_single(symbol: str):
    symbol = symbol.upper()

    halal = halal_filter(symbol)
    info = get_company_sector_info(symbol)

    if not halal["allowed"]:
        return {
            "symbol": symbol,
            "companyName": info["company_name"],
            "halal": halal,
            "analysis": None,
        }

    analysis = analyze_stock(symbol)
    return {
        "symbol": symbol,
        "companyName": info["company_name"],
        "halal": halal,
        "analysis": analysis,
    }

@app.get("/debug/{symbol}")
def debug_symbol(symbol: str):
    symbol = symbol.upper()
    return {
        "symbol": symbol,
        "company": COMPANIES_DATA.get(symbol, {}),
        "sector_info": get_company_sector_info(symbol),
        "balance": BALANCE_DATA.get(symbol, {}),
        "income": INCOME_DATA.get(symbol, {}),
        "halal_check": halal_filter(symbol),
        "analysis": analyze_stock(symbol),
    }
