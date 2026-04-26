from .settings import HARAM_INDUSTRY_KEYWORDS, HARAM_SECTORS, LOW_PRICE_HARD_BLOCK, LOW_PRICE_WARNING
from .utils import *
from .data_loader import BALANCE_DATA, INCOME_DATA

def assess_sharia(symbol, sector, industry, total_assets, cash, total_debt, manual_exclusions=None):
    symbol = normalize_symbol_text(symbol)
    sector_l = str(sector).lower().strip()
    industry_l = str(industry).lower().strip()
    if isinstance(manual_exclusions, dict):
        exclusions = manual_exclusions
    else:
        try:
            from .data_store import get_manual_sharia_exclusions_map
            exclusions = get_manual_sharia_exclusions_map()
        except Exception:
            exclusions = {}
    manual_entry = exclusions.get(symbol, {}) if symbol else {}

    if manual_entry:
        note = str(manual_entry.get("note", "") or "").strip()
        reason = "مستبعد يدويًا من قائمتك الشرعية"
        if note:
            reason = f"{reason} - {note}"
        return {
            "status": "manual_excluded",
            "label": "مستبعد يدويًا",
            "reason": reason,
            "manual_excluded": True,
            "is_gray": False,
            "is_halal": False,
            "should_block": True,
            "note": note,
        }

    if sector_l in HARAM_SECTORS:
        return {
            "status": "non_compliant",
            "label": "غير متوافق",
            "reason": f"مرفوض شرعيًا: القطاع ({sector}) غير مقبول",
            "manual_excluded": False,
            "is_gray": False,
            "is_halal": False,
            "should_block": True,
            "note": "",
        }

    for kw in HARAM_INDUSTRY_KEYWORDS:
        if kw in industry_l:
            return {
                "status": "non_compliant",
                "label": "غير متوافق",
                "reason": f"مرفوض شرعيًا: الصناعة تحتوي ({kw})",
                "manual_excluded": False,
                "is_gray": False,
                "is_halal": False,
                "should_block": True,
                "note": "",
            }

    missing_research = (not sector_l) or (not industry_l) or total_assets <= 0
    if missing_research:
        missing_parts = []
        if not sector_l or not industry_l:
            missing_parts.append("القطاع/الصناعة")
        if total_assets <= 0:
            missing_parts.append("الأصول")
        reason_tail = " و ".join(missing_parts) if missing_parts else "البيانات الأساسية"
        return {
            "status": "gray",
            "label": "غير محسوم",
            "reason": f"الحكم الشرعي غير محسوم بسبب نقص أو قدم بيانات {reason_tail}",
            "manual_excluded": False,
            "is_gray": True,
            "is_halal": True,
            "should_block": False,
            "note": "",
        }

    debt_ratio = total_debt / total_assets if total_assets > 0 else 0
    cash_ratio = cash / total_assets if total_assets > 0 else 0

    if debt_ratio > 0.33:
        return {
            "status": "non_compliant",
            "label": "غير متوافق",
            "reason": f"مرفوض شرعيًا: الديون {safe_round(debt_ratio*100)}% من الأصول",
            "manual_excluded": False,
            "is_gray": False,
            "is_halal": False,
            "should_block": True,
            "note": "",
        }
    if cash_ratio > 0.33:
        return {
            "status": "non_compliant",
            "label": "غير متوافق",
            "reason": f"مرفوض شرعيًا: النقد {safe_round(cash_ratio*100)}% من الأصول",
            "manual_excluded": False,
            "is_gray": False,
            "is_halal": False,
            "should_block": True,
            "note": "",
        }

    return {
        "status": "compliant",
        "label": "متوافق مبدئيًا",
        "reason": "مطابق للضوابط الشرعية المبدئية",
        "manual_excluded": False,
        "is_gray": False,
        "is_halal": True,
        "should_block": False,
        "note": "",
    }


def is_halal(sector, industry, total_assets, cash, total_debt):
    assessment = assess_sharia("", sector, industry, total_assets, cash, total_debt, {})
    return assessment.get("is_halal", True), assessment.get("reason", "")


def get_financials(symbol, prev_data=None):
    b = BALANCE_DATA.get(symbol, {})
    i = INCOME_DATA.get(symbol, {})

    total_assets = to_float(b.get("Total Assets", 0))
    cash = to_float(b.get("Cash And Cash Equivalents", 0))
    total_debt = to_float(b.get("Total Debt", 0))
    shares = to_float(i.get("Shares (Diluted)", 0)) or to_float(i.get("Shares (Basic)", 0))
    if prev_data is not None:
        prev = prev_data
    else:
        try:
            from .market_data import get_prev
            prev = get_prev(symbol)
        except Exception:
            prev = None
    current_price = prev["price"] if prev else 0.0
    approx_market_cap = current_price * shares if shares > 0 and current_price > 0 else 0.0
    debt_to_market_cap = (total_debt / approx_market_cap) if approx_market_cap > 0 else None
    cash_to_assets = (cash / total_assets) if total_assets > 0 else None

    return {
        "total_assets": total_assets,
        "cash": cash,
        "total_debt": total_debt,
        "shares": shares,
        "current_price": current_price,
        "approx_market_cap": approx_market_cap,
        "debt_to_market_cap": debt_to_market_cap,
        "cash_to_assets": cash_to_assets,
    }


def dynamic_price_penalty(current_price: float, trade_type: str) -> tuple[int, str]:
    if current_price <= 0:
        return 0, ""
    if current_price < LOW_PRICE_HARD_BLOCK:
        return -30, "سهم منخفض السعر جدًا (أقل من 2$)"
    if trade_type == "Breakout" and current_price < LOW_PRICE_WARNING:
        return -15, "سهم اختراق منخفض السعر (أقل من 3$)"
    return 0, ""
