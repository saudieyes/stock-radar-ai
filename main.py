from fastapi import FastAPI
import requests, os, csv, re
from datetime import datetime, timedelta
from scanner import get_scan_universe

app = FastAPI()
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

SECTOR_DATA, COMPANIES_DATA = {}, {}
BALANCE_DATA, INCOME_DATA = {}, {}
HISTORY_CACHE = {}

LOW_PRICE_HARD_BLOCK = 2.0

# -------------------- helpers --------------------
def to_float(x):
    try: return float(str(x).replace(",", ""))
    except: return 0.0

def safe_round(x): 
    try: return round(float(x),2)
    except: return x

# -------------------- data --------------------
def read_csv(path):
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def load():
    global SECTOR_DATA, COMPANIES_DATA
    global BALANCE_DATA, INCOME_DATA

    for r in read_csv("data/sector_industry.csv"):
        SECTOR_DATA[r["IndustryId"]] = r

    for r in read_csv("data/companies.csv"):
        COMPANIES_DATA[r["Ticker"]] = r

    for r in read_csv("data/balance_sheet.csv"):
        BALANCE_DATA[r["Ticker"]] = r

    for r in read_csv("data/income_statement.csv"):
        INCOME_DATA[r["Ticker"]] = r

load()

# -------------------- info --------------------
def get_info(symbol):
    c = COMPANIES_DATA.get(symbol, {})
    s = SECTOR_DATA.get(str(c.get("IndustryId","")), {})
    return {
        "company": c.get("Company Name",""),
        "sector": s.get("Sector",""),
        "industry": s.get("Industry","")
    }

# -------------------- market --------------------
def get_prev(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}"
        ).json()["results"][0]
        return {
            "price": r["c"],
            "high": r["h"],
            "low": r["l"],
            "volume": r["v"],
            "open": r["o"],
        }
    except:
        return None

# -------------------- trend --------------------
def get_trend(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/2024-01-01/2026-01-01?apiKey={POLYGON_API_KEY}"
        ).json()["results"]

        closes = [x["c"] for x in r][-50:]
        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes) / 50
        price = closes[-1]

        if price > ma20 > ma50: return "صاعد قوي"
        if price > ma50: return "صاعد"
        if price < ma20 < ma50: return "هابط"
        return "متذبذب"
    except:
        return "unknown"

def get_volume_ratio(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/2024-01-01/2026-01-01?apiKey={POLYGON_API_KEY}"
        ).json()["results"]

        vols = [x["v"] for x in r][-20:]
        avg = sum(vols)/20
        return vols[-1]/avg if avg else 1
    except:
        return 1

# -------------------- NEWS CLEAN FINAL --------------------
def get_news_catalyst(symbol):
    try:
        info = get_info(symbol)
        company = info["company"].lower()

        r = requests.get(
            f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=10&apiKey={POLYGON_API_KEY}"
        ).json()

        news = r.get("results", [])

        strong_words = [
            "earnings","beats","guidance","upgrade","outperform",
            "deal","contract","partnership","acquisition","merger",
            "approval","fda","launch","buyback"
        ]

        bad_words = [
            "top stocks","market","etf","index","s&p","nasdaq",
            "dow","weekly","consumer tech news","roundup"
        ]

        best_score = 0
        best_note = ""

        for n in news:
            title = n.get("title","").lower()

            # ❌ حذف الأخبار العامة
            if any(b in title for b in bad_words):
                continue

            # ✅ لازم يكون متعلق بالسهم
            if symbol.lower() not in title and company.split()[0] not in title:
                continue

            score = 0

            if any(w in title for w in strong_words):
                score += 6

            # تاريخ
            try:
                d = datetime.strptime(n["published_utc"][:10], "%Y-%m-%d")
                days = (datetime.utcnow()-d).days
                if days <= 1: score += 5
                elif days <= 2: score += 3
            except: pass

            if score > best_score:
                best_score = score
                best_note = title

        return {
            "catalyst_score": best_score,
            "note": best_note if best_note else "لا يوجد محفز قوي"
        }

    except:
        return {"catalyst_score":0,"note":"error"}

# -------------------- trade engine --------------------
def trade_plan(symbol):
    d = get_prev(symbol)
    if not d: return None

    price, high, low = d["price"], d["high"], d["low"]

    if price < LOW_PRICE_HARD_BLOCK: return None

    trend = get_trend(symbol)
    vr = get_volume_ratio(symbol)
    news = get_news_catalyst(symbol)

    entry = high * 1.002
    stop = low * 0.995
    risk = entry - stop

    if risk <= 0: return None

    score = 50

    if trend == "صاعد قوي": score += 10
    if vr >= 1.5: score += 6
    elif vr < 0.9: score -= 6

    score += news["catalyst_score"]

    decision = "مراقبة"
    if score >= 82: decision = "دخول قوي"
    elif score >= 72: decision = "دخول بحذر"

    return {
        "symbol": symbol,
        "entry": safe_round(entry),
        "stop": safe_round(stop),
        "trend": trend,
        "volume_ratio": round(vr,2),
        "catalyst_score": news["catalyst_score"],
        "news_note": news["note"],
        "quality_score": score,
        "decision": decision
    }

# -------------------- API --------------------
@app.get("/trade-scan")
def scan():
    trades = []

    for s in get_scan_universe(40):
        t = trade_plan(s)
        if t:
            trades.append(t)

    trades = sorted(trades, key=lambda x: x["quality_score"], reverse=True)

    return {
        "count": len(trades),
        "strong": [x for x in trades if x["decision"]=="دخول قوي"],
        "cautious": [x for x in trades if x["decision"]=="دخول بحذر"],
        "watch": [x for x in trades if x["decision"]=="مراقبة"]
    }
