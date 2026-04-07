from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# استيراد ملفاتك
from scanner import run_trade_scan
from analyzer import analyze_symbol_overview
from trade_plan import trade_plan_pro
from data_provider import get_info
from halal_filter import halal
from execution_engine import execution_filter, apply_late_move_filter, assign_execution_mode

app = FastAPI()

# السماح للواجهة
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# الصفحة الرئيسية
# -------------------------
@app.get("/")
def home():
    return {"status": "Stock Radar AI يعمل بنجاح 🚀"}

# -------------------------
# Health Check
# -------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

# -------------------------
# Trade Scan
# -------------------------
@app.get("/trade-scan")
def trade_scan():
    try:
        data = run_trade_scan()

        # تطبيق execution logic
        for stock in data.get("top_ranked", []):
            try:
                stock = execution_filter(stock)
                stock = apply_late_move_filter(stock)
                stock = assign_execution_mode(stock)
            except:
                pass

        return data

    except Exception as e:
        return {"error": f"خطأ في trade-scan: {str(e)}"}

# -------------------------
# Single Stock (مهم جداً 🔥)
# -------------------------
@app.get("/single-stock")
def single_stock(symbol: str):
    try:
        symbol = str(symbol).upper().strip()

        if not symbol:
            return {"error": "يرجى إدخال رمز السهم"}

        # overview
        overview = {}
        try:
            overview = analyze_symbol_overview(symbol)
        except:
            overview = {}

        # trade plan
        trade = None
        try:
            trade = trade_plan_pro(symbol)
        except:
            trade = None

        if trade:
            try:
                info = get_info(symbol)
            except:
                info = {}

            try:
                h = halal(symbol)
            except:
                h = {"financials": {}}

            # إضافة البيانات
            trade["company"] = info.get("company", "")
            trade["sector"] = info.get("sector", "")
            trade["industry"] = info.get("industry", "")
            trade["financials"] = h.get("financials", {})

            # execution system
            try:
                trade = execution_filter(trade)
                trade = apply_late_move_filter(trade)
                trade = assign_execution_mode(trade)
            except:
                pass

        return {
            "symbol": symbol,
            "overview": overview,
            "trade_plan": trade
        }

    except Exception as e:
        return {
            "error": f"حدث خطأ في السيرفر: {str(e)}"
        }
