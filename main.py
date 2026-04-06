from fastapi import FastAPI
from fastapi.responses import FileResponse
import requests
import os
from datetime import datetime, timedelta
from scanner import get_scan_universe

app = FastAPI()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

LOW_PRICE = 2.0

# =========================
# HELPERS
# =========================

def to_float(x):
    try:
        return float(x)
    except:
        return 0.0


def safe_round(x):
    try:
        return round(float(x), 2)
    except:
        return x

# =========================
# DATA
# =========================

def get_prev(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=10
        ).json()

        res = r.get("results", [])
        if not res:
            return None

        d = res[0]
        return {
            "price": to_float(d.get("c")),
            "high": to_float(d.get("h")),
            "low": to_float(d.get("l")),
            "open": to_float(d.get("o")),
            "volume": to_float(d.get("v")),
        }
    except:
        return None

# =========================
# CORE ANALYSIS
# =========================

def analyze(symbol):
    d = get_prev(symbol)
    if not d:
        return None

    price = d["price"]
    high = d["high"]
    low = d["low"]
    open_p = d["open"]
    volume = d["volume"]

    if price < LOW_PRICE:
        return None

    day_range = max(high - low, 0.01)
    range_pct = day_range / price
    change = (price - open_p) / open_p if open_p else 0

    momentum = "صاعد" if price > open_p else "هابط"

    near_high = price >= high * 0.985

    # =========================
    # SCORE (بدون قتل)
    # =========================
    score = 50

    # volume
    if volume > 100_000_000:
        score += 20
    elif volume > 20_000_000:
        score += 12
    elif volume > 5_000_000:
        score += 6
    else:
        score -= 5

    # momentum
    if change > 0.05:
        score += 15
    elif change > 0.02:
        score += 10
    elif change > 0:
        score += 5
    elif change < -0.03:
        score -= 10

    # breakout
    if near_high and momentum == "صاعد":
        score += 10

    # range
    if 0.02 <= range_pct <= 0.10:
        score += 5
    elif range_pct > 0.15:
        score -= 5

    # =========================
    # DECISION (لا حذف)
    # =========================
    if score >= 80:
        decision = "دخول قوي"
    elif score >= 65:
        decision = "دخول بحذر"
    else:
        decision = "مراقبة"

    # =========================
    # EXECUTION
    # =========================
    if score >= 85 and volume > 20_000_000:
        status = "READY"
    elif score >= 65:
        status = "WAIT"
    else:
        status = "AVOID"

    return {
        "symbol": symbol,
        "price": safe_round(price),
        "decision": decision,
        "execution_status": status,
        "score": score,
        "volume": volume,
    }

# =========================
# ROUTES
# =========================

@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/health")
def health():
    universe = get_scan_universe(60)
    return {
        "message": "Stock Radar AI is running 🚀",
        "universe_count": len(universe)
    }


@app.get("/trade-scan")
def trade_scan():
    universe = get_scan_universe(60)

    results = []

    for s in universe:
        try:
            r = analyze(s)
            if r:
                results.append(r)
        except:
            continue

    strong = [x for x in results if x["decision"] == "دخول قوي"]
    cautious = [x for x in results if x["decision"] == "دخول بحذر"]
    watch = [x for x in results if x["decision"] == "مراقبة"]

    return {
        "universe_count": len(universe),
        "count": len(results),
        "strong_entries_count": len(strong),
        "cautious_entries_count": len(cautious),
        "watchlist_count": len(watch),
        "top_ranked": results[:10]
    }
