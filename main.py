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
    res = requests.get(url).json()
    tickers = res.get("tickers", [])

    symbols = []
    for t in tickers[:100]:
        if "ticker" in t:
            symbols.append(t["ticker"])

    return symbols

# -------------------------------
# فلتر شرعي (محسن + آمن)
# -------------------------------
def halal_filter(symbol):
    url = f"https://financialmodelingprep.com/api/v3/profile/{symbol}?apikey={FMP_API_KEY}"

    try:
        res = requests.get(url).json()
    except:
        return False

    # إذا ما رجعت قائمة
    if not isinstance(res, list):
        return False

    # إذا القائمة فاضية
    if len(res) == 0:
        return False

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

    return not any(word in text for word in haram_keywords)

# -------------------------------
# تحليل السهم (مبدئي)
# -------------------------------
def analyze_stock(symbol):
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}"

    try:
        res = requests.get(url).json()
    except:
        return None

    if "results" not in res:
        return None

    data = res["results"][0]

    price = data.get("c", 0)
    volume = data.get("v", 0)

    score = 50

    if volume > 1_000_000:
        score += 10

    if price > 0:
        score += 5

    return {
        "symbol": symbol,
        "price": price,
        "volume": volume,
        "score": score
    }

# -------------------------------
# الرادار الرئيسي
# -------------------------------
@app.get("/scan")
def scan():
    symbols = get_active_stocks()
    results = []

    for s in symbols:
        try:
            if halal_filter(s):
                data = analyze_stock(s)
                if data:
                    results.append(data)
        except:
            continue

    return {
        "count": len(results),
        "results": results[:20]
    }

# -------------------------------
# تحليل سهم واحد
# -------------------------------
@app.get("/analyze/{symbol}")
def analyze(symbol: str):
    try:
        if not halal_filter(symbol):
            return {"error": "السهم غير متوافق شرعياً"}

        return analyze_stock(symbol)
    except:
        return {"error": "حدث خطأ أثناء التحليل"}
