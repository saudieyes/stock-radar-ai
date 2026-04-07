from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

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
    return {"status": "Stock Radar AI يعمل 🚀"}

# -------------------------
# Health
# -------------------------
@app.get("/health")
def health():
    return {"status": "ok"}

# -------------------------
# Trade Scan (نسخة آمنة)
# -------------------------
@app.get("/trade-scan")
def trade_scan():
    try:
        from scanner import run_trade_scan

        data = run_trade_scan()
        return data

    except Exception as e:
        return {"error": f"trade-scan error: {str(e)}"}

# -------------------------
# Single Stock (نسخة آمنة جداً)
# -------------------------
@app.get("/single-stock")
def single_stock(symbol: str):
    try:
        symbol = str(symbol).upper().strip()

        result = {
            "symbol": symbol,
            "overview": {},
            "trade_plan": None
        }

        # overview
        try:
            from analyzer import analyze_symbol_overview
            result["overview"] = analyze_symbol_overview(symbol)
        except Exception as e:
            result["overview_error"] = str(e)

        # trade plan
        try:
            from trade_plan import trade_plan_pro
            trade = trade_plan_pro(symbol)
        except Exception as e:
            trade = None
            result["trade_error"] = str(e)

        if trade:
            try:
                from data_provider import get_info
                info = get_info(symbol)
                trade["company"] = info.get("company", "")
            except:
                pass

            try:
                from execution_engine import execution_filter, apply_late_move_filter, assign_execution_mode
                trade = execution_filter(trade)
                trade = apply_late_move_filter(trade)
                trade = assign_execution_mode(trade)
            except:
                pass

        result["trade_plan"] = trade

        return result

    except Exception as e:
        return {"error": f"server crash: {str(e)}"}
