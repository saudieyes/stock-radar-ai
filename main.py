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

HARAM_SECTORS = {"financial services", "banks", "insurance"}

HARAM_INDUSTRY_KEYWORDS = [
    "bank","banks","insurance","tobacco","alcohol",
    "gambling","casino","betting","credit services",
    "mortgage","reit mortgage","asset management","capital markets",
]

# -------------------- utils --------------------
def clean_key(key): return str(key).replace("\ufeff", "").strip()
def clean_row(row): return {clean_key(k): v for k, v in row.items()}

def to_float(value):
    try:
        if value is None: return 0.0
        value = str(value).replace(",", "").strip()
        return float(value) if value else 0.0
    except:
        return 0.0

def period_rank(p):
    return {"Q1":1,"Q2":2,"Q3":3,"Q4":4,"FY":5,"TTM":6}.get(str(p).upper(),0)

def parse_date_safe(v):
    try: return datetime.strptime(v, "%Y-%m-%d")
    except: return datetime.min

def latest_key(row):
    return (
        parse_date_safe(row.get("Publish Date","")),
        int(to_float(row.get("Fiscal Year",0))),
        period_rank(row.get("Fiscal Period",""))
    )

# -------------------- CSV smart --------------------
def read_csv(path):
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            reader = csv.DictReader(f, dialect=dialect)
            return [clean_row(r) for r in reader]
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
        data[str(r.get("IndustryId","")).strip()] = {
            "industry": r.get("Industry",""),
            "sector": r.get("Sector","")
        }
    return data

def load_companies():
    data = {}
    for r in read_csv("data/companies.csv"):
        t = str(r.get("Ticker","")).upper().strip()
        if t: data[t] = r
    return data

def load_latest(path):
    data = {}
    for r in read_csv(path):
        t = str(r.get("Ticker","")).upper().strip()
        if not t: continue
        k = latest_key(r)
        if t not in data or k > data[t]["_k"]:
            r["_k"] = k
            data[t] = r
    for t in data: data[t].pop("_k", None)
    return data

SECTOR_DATA = load_sector()
COMPANIES_DATA = load_companies()
BALANCE_DATA = load_latest("data/balance_sheet.csv")
INCOME_DATA = load_latest("data/income_statement.csv")

# -------------------- core --------------------
def get_prev(symbol):
    try:
        r = requests.get(f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}", timeout=10).json()
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

def get_info(symbol):
    c = COMPANIES_DATA.get(symbol, {})
    ind = str(c.get("IndustryId","")).strip()
    s = SECTOR_DATA.get(ind, {})
    return {
        "company": c.get("Company Name",""),
        "sector": s.get("sector",""),
        "industry": s.get("industry",""),
    }

# -------------------- HALAL --------------------
def halal(symbol):
    i = get_info(symbol)
    txt = f"{i['sector']} {i['industry']}".lower()

    if i["sector"].lower() in HARAM_SECTORS:
        return False

    for w in HARAM_INDUSTRY_KEYWORDS:
        if w in txt: return False

    b = BALANCE_DATA.get(symbol, {})
    inc = INCOME_DATA.get(symbol, {})

    debt = to_float(b.get("Short Term Debt")) + to_float(b.get("Long Term Debt"))
    assets = to_float(b.get("Total Assets"))
    shares = to_float(inc.get("Shares (Diluted)"))

    p = get_prev(symbol)
    price = p["price"] if p else 0
    mcap = price * shares if shares else 0

    if mcap > 0 and debt/mcap > 0.33:
        return False

    if assets > 0:
        cash = to_float(b.get("Cash, Cash Equivalents & Short Term Investments"))
        if cash/assets > 0.5:
            return False

    return True

# -------------------- TRADE ENGINE --------------------
def trade_plan(symbol):
    p = get_prev(symbol)
    if not p: return None

    price, high, low = p["price"], p["high"], p["low"]
    vol = p["volume"]

    # Breakout
    breakout = price >= high * 0.985
    pullback = price <= low * 1.02

    if breakout:
        entry = high * 1.002
        stop = low * 0.995
        t1 = entry + (entry - stop) * 1.5
        t2 = entry + (entry - stop) * 2
        ttype = "Breakout"
        valid = "Intraday"

    elif pullback:
        entry = price
        stop = low * 0.99
        t1 = entry + (entry - stop) * 1.5
        t2 = entry + (entry - stop) * 2
        ttype = "Pullback"
        valid = "1-3 days"

    else:
        return None

    confidence = "عادي"
    if vol > 50_000_000:
        confidence = "عالي 🔥"

    return {
        "symbol": symbol,
        "type": ttype,
        "entry": round(entry,2),
        "stop_loss": round(stop,2),
        "target_1": round(t1,2),
        "target_2": round(t2,2),
        "valid_for": valid,
        "confidence": confidence
    }

# -------------------- endpoints --------------------
@app.get("/")
def home():
    return {
        "message":"Stock Radar AI is running 🚀",
        "loaded":{
            "companies":len(COMPANIES_DATA),
            "sector":len(SECTOR_DATA),
            "balance":len(BALANCE_DATA),
            "income":len(INCOME_DATA),
        }
    }

@app.get("/trade-scan")
def trade_scan():
    results = []
    for s in ALLOWED_TEST_SYMBOLS:
        if not halal(s): continue
        plan = trade_plan(s)
        if plan:
            info = get_info(s)
            plan["company"] = info["company"]
            results.append(plan)

    return {
        "count": len(results),
        "trades": results
    }
