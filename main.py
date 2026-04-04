from fastapi import FastAPI
import requests
import os
import csv
from datetime import datetime

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
    "banks",
    "insurance",
    "tobacco",
    "alcohol",
    "gambling",
    "casino",
    "betting",
    "credit services",
    "mortgage",
    "reit mortgage",
    "asset management",
    "capital markets",
]

def clean_key(key: str) -> str:
    return str(key).replace("\ufeff", "").strip()

def clean_row(row: dict) -> dict:
    return {clean_key(k): v for k, v in row.items()}

def to_float(value):
    try:
        if value is None:
            return 0.0
        value = str(value).strip().replace(",", "")
        if value == "":
            return 0.0
        return float(value)
    except:
        return 0.0

def period_rank(period: str) -> int:
    p = str(period).strip().upper()
    order = {
        "Q1": 1,
        "Q2": 2,
        "Q3": 3,
        "Q4": 4,
        "FY": 5,
        "TTM": 6,
    }
    return order.get(p, 0)

def parse_date_safe(value: str):
    try:
        value = str(value).strip()
        if not value:
            return datetime.min
        return datetime.strptime(value, "%Y-%m-%d")
    except:
        return datetime.min

def latest_key_from_row(row: dict):
    publish_date = parse_date_safe(row.get("Publish Date", ""))
    fiscal_year = int(to_float(row.get("Fiscal Year", 0)))
    fiscal_period = period_rank(row.get("Fiscal Period", ""))
    return (publish_date, fiscal_year, fiscal_period)

# -------------------------------
# تحميل Sector / Industry
# -------------------------------
def load_sector_industry():
    data = {}
    path = "data/sector_industry.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=",")
        for raw in reader:
            row = clean_row(raw)
            industry_id = str(row.get("IndustryId", "")).strip()
            if industry_id:
                data[industry_id] = {
                    "industry": str(row.get("Industry", "")).strip(),
                    "sector": str(row.get("Sector", "")).strip(),
                }
    return data

# -------------------------------
# تحميل الشركات
# -------------------------------
def load_companies():
    data = {}
    path = "data/companies.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=",")
        for raw in reader:
            row = clean_row(raw)
            ticker = str(row.get("Ticker", "")).strip().upper()
            if not ticker:
                continue
            data[ticker] = row
    return data

# -------------------------------
# تحميل أحدث Balance Sheet لكل سهم
# -------------------------------
def load_latest_balance():
    data = {}
    path = "data/balance_sheet.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=",")
        for raw in reader:
            row = clean_row(raw)
            ticker = str(row.get("Ticker", "")).strip().upper()
            if not ticker:
                continue

            key = latest_key_from_row(row)
            if ticker not in data or key > data[ticker]["_latest_key"]:
                row["_latest_key"] = key
                data[ticker] = row

    for ticker in list(data.keys()):
        data[ticker].pop("_latest_key", None)

    return data

# -------------------------------
# تحميل أحدث Income Statement لكل سهم
# -------------------------------
def load_latest_income():
    data = {}
    path = "data/income_statement.csv"
    if not os.path.exists(path):
        return data

    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=",")
        for raw in reader:
            row = clean_row(raw)
            ticker = str(row.get("Ticker", "")).strip().upper()
            if not ticker:
                continue

            key = latest_key_from_row(row)
            if ticker not in data or key > data[ticker]["_latest_key"]:
                row["_latest_key"] = key
                data[ticker] = row

    for ticker in list(data.keys()):
        data[ticker].pop("_latest_key", None)

    return data

SECTOR_DATA = load_sector_industry()
COMPANIES_DATA = load_companies()
BALANCE_DATA = load_latest_balance()
INCOME_DATA = load_latest_income()

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

def get_test_symbols():
    return ALLOWED_TEST_SYMBOLS

# -------------------------------
# جلب بيانات Polygon اليومية
# -------------------------------
def get_polygon_prev(symbol):
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}"
    try:
        res = requests.get(url, timeout=20).json()
    except:
        return None

    if "results" not in res or not res["results"]:
        return None

    d = res["results"][0]
    return {
        "price": to_float(d.get("c", 0)),
        "high": to_float(d.get("h", 0)),
        "low": to_float(d.get("l", 0)),
        "volume": to_float(d.get("v", 0)),
        "open": to_float(d.get("o", 0)),
    }

# -------------------------------
# بيانات الشركة والقطاع
# -------------------------------
def get_company_sector_info(symbol):
    company = COMPANIES_DATA.get(symbol, {})
    industry_id = str(company.get("IndustryId", "")).strip()
    sector_info = SECTOR_DATA.get(industry_id, {})

    return {
        "company_name": str(company.get("Company Name", "")).strip(),
        "industry_id": industry_id,
        "industry": str(sector_info.get("industry", "")).strip(),
        "sector": str(sector_info.get("sector", "")).strip(),
    }

# -------------------------------
# الفلتر الشرعي
# -------------------------------
def halal_filter(symbol):
    info = get_company_sector_info(symbol)
    sector = info["sector"].lower()
    industry = info["industry"].lower()

    # 1) فلتر النشاط
    if sector in HARAM_SECTORS:
        return {
            "allowed": False,
            "reason": f"قطاع محرم: {info['sector']}",
            "sector": info["sector"],
            "industry": info["industry"],
            "financials": {}
        }

    text = f"{sector} {industry}"
    for word in HARAM_INDUSTRY_KEYWORDS:
        if word in text:
            return {
                "allowed": False,
                "reason": f"نشاط محرم: {word}",
                "sector": info["sector"],
                "industry": info["industry"],
                "financials": {}
            }

    # 2) الفلتر المالي
    balance = BALANCE_DATA.get(symbol, {})
    income = INCOME_DATA.get(symbol, {})

    total_assets = to_float(balance.get("Total Assets"))
    cash = to_float(balance.get("Cash, Cash Equivalents & Short Term Investments"))
    short_term_debt = to_float(balance.get("Short Term Debt"))
    long_term_debt = to_float(balance.get("Long Term Debt"))
    total_debt = short_term_debt + long_term_debt

    prev = get_polygon_prev(symbol)
    current_price = prev["price"] if prev else 0.0

    shares_diluted = to_float(income.get("Shares (Diluted)"))
    shares_basic = to_float(income.get("Shares (Basic)"))
    shares = shares_diluted if shares_diluted > 0 else shares_basic

    approx_market_cap = current_price * shares if current_price > 0 and shares > 0 else 0.0

    debt_to_market_cap = (total_debt / approx_market_cap) if approx_market_cap > 0 else None
    cash_to_assets = (cash / total_assets) if total_assets > 0 else None

    financials = {
        "total_assets": total_assets,
        "cash": cash,
        "short_term_debt": short_term_debt,
        "long_term_debt": long_term_debt,
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
            "sector": info["sector"],
            "industry": info["industry"],
            "financials": financials
        }

    if cash_to_assets is not None and cash_to_assets > 0.50:
        return {
            "allowed": False,
            "reason": f"نسبة النقد إلى الأصول مرتفعة: {cash_to_assets:.2%}",
            "sector": info["sector"],
            "industry": info["industry"],
            "financials": financials
        }

    return {
        "allowed": True,
        "reason": "مقبول مبدئيًا",
        "sector": info["sector"],
        "industry": info["industry"],
        "financials": financials
    }

# -------------------------------
# التحليل الفني المبدئي
# -------------------------------
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
    if high > 0 and low > 0:
        if price >= (high * 0.985):
            location_signal = "قرب مقاومة"
        elif price <= (low * 1.015):
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
                "financials": halal["financials"],
            })
            continue

        analysis = analyze_stock(symbol)
        if analysis:
            info = get_company_sector_info(symbol)
            analysis["companyName"] = info["company_name"]
            analysis["sector"] = info["sector"]
            analysis["industry"] = info["industry"]
            analysis["financials"] = halal["financials"]
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
