from fastapi import FastAPI
import requests
import os

app = FastAPI()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")
FMP_API_KEY = os.getenv("FMP_API_KEY")

@app.get("/")
def home():
    return {"message": "Stock Radar AI is running 🚀"}

# -------------------------------
# جلب الأسهم النشطة من Polygon
# -------------------------------
def get_active_stocks():
    url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers?apiKey={POLYGON_API_KEY}"

    try:
        res = requests.get(url, timeout=20).json()
    except Exception as e:
        return {"error": f"Polygon request failed: {str(e)}", "symbols": []}

    tickers = res.get("tickers", [])
    symbols = []

    for t in tickers[:100]:
        if isinstance(t, dict) and "ticker" in t:
            symbols.append(t["ticker"])

    return {"error": None, "symbols": symbols}

# -------------------------------
# فلتر شرعي مع تشخيص
# -------------------------------
def halal_filter(symbol):
    url = f"https://financialmodelingprep.com/api/v3/profile/{symbol}?apikey={FMP_API_KEY}"

    try:
        res = requests.get(url, timeout=20).json()
    except Exception as e:
        return {
            "allowed": False,
            "reason": f"FMP request failed: {str(e)}",
            "sector": "",
            "industry": ""
        }

    if not isinstance(res, list):
        return {
            "allowed": False,
            "reason": f"FMP response is not a list: {str(res)[:120]}",
            "sector": "",
            "industry": ""
        }

    if len(res) == 0:
        return {
            "allowed": False,
            "reason": "FMP returned empty list",
            "sector": "",
            "industry": ""
        }

    company = res[0]

    sector = str(company.get("sector", "")).lower()
    industry = str(company.get("industry", "")).lower()
    text = f"{sector} {industry}"

    haram_keywords = [
        "financial",
        "bank",
        "insurance",
        "gambling",
        "casino",
        "betting",
        "alcohol",
        "tobacco"
    ]

    for word in haram_keywords:
        if word in text:
            return {
                "allowed": False,
                "reason": f"Rejected by keyword: {word}",
                "sector": sector,
                "industry": industry
            }

    return {
        "allowed": True,
        "reason": "Passed",
        "sector": sector,
        "industry": industry
    }

# -------------------------------
# تحليل السهم من Polygon
# -------------------------------
def analyze_stock(symbol):
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}"

    try:
        res = requests.get(url, timeout=20).json()
    except Exception as e:
        return {"error": f"Polygon prev request failed: {str(e)}"}

    if "results" not in res or not res["results"]:
        return {"error": f"No previous aggregate data for {symbol}"}

    data = res["results"][0]

    price = data.get("c", 0)
    volume = data.get("v", 0)
    high = data.get("h", 0)
    low = data.get("l", 0)

    score = 50

    if volume > 1_000_000:
        score += 10

    if price > 0:
        score += 5

    return {
        "symbol": symbol,
        "price": price,
        "high": high,
        "low": low,
        "volume": volume,
        "score": score
    }

# -------------------------------
# الرادار الرئيسي مع تشخيص
# -------------------------------
@app.get("/scan")
def scan():
    active_data = get_active_stocks()

    if active_data["error"]:
        return {
            "error": active_data["error"],
            "count": 0,
            "results": []
        }

    symbols = active_data["symbols"]
    results = []

    total_symbols = len(symbols)
    passed_halal = 0
    failed_halal = 0
    failed_analysis = 0
    sample_rejections = []

    for s in symbols:
        halal = halal_filter(s)

        if not halal["allowed"]:
            failed_halal += 1
            if len(sample_rejections) < 10:
                sample_rejections.append({
                    "symbol": s,
                    "reason": halal["reason"],
                    "sector": halal["sector"],
                    "industry": halal["industry"]
                })
            continue

        passed_halal += 1
        analysis = analyze_stock(s)

        if analysis.get("error"):
            failed_analysis += 1
            if len(sample_rejections) < 10:
                sample_rejections.append({
                    "symbol": s,
                    "reason": analysis["error"]
                })
            continue

        results.append(analysis)

    return {
        "count": len(results),
        "debug": {
            "total_symbols": total_symbols,
            "passed_halal": passed_halal,
            "failed_halal": failed_halal,
            "failed_analysis": failed_analysis,
            "sample_rejections": sample_rejections
        },
        "results": results[:20]
    }

# -------------------------------
# تحليل سهم واحد
# -------------------------------
@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    symbol = symbol.upper()

    halal = halal_filter(symbol)
    if not halal["allowed"]:
        return {
            "error": "السهم غير متوافق شرعياً أو تعذر جلب بياناته",
            "details": halal
        }

    analysis = analyze_stock(symbol)
    if analysis.get("error"):
        return analysis

    return {
        "halal": halal,
        "analysis": analysis
    }

# -------------------------------
# تشخيص مباشر لسهم واحد
# -------------------------------
@app.get("/debug/{symbol}")
def debug_symbol(symbol: str):
    symbol = symbol.upper()
    return {
        "symbol": symbol,
        "halal_check": halal_filter(symbol),
        "analysis_check": analyze_stock(symbol)
    }
