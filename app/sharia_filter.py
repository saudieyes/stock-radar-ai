from .settings import (
    HARAM_INDUSTRY_KEYWORDS,
    HARAM_SECTORS,
    LOW_PRICE_HARD_BLOCK,
    LOW_PRICE_WARNING,
    SHARIA_MAX_DEBT_TO_ASSETS,
    SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE,
    SHARIA_MAX_IMPERMISSIBLE_REVENUE_RATIO,
    SHARIA_GRAY_CASH_TO_ASSETS,
    SHARIA_BLOCK_GRAY_IN_SOURCE,
)
from .utils import *
from .data_loader import BALANCE_DATA, INCOME_DATA, COMPANIES_DATA, SECTOR_DATA


def _first_number(row: dict, names: list[str], default: float = 0.0) -> float:
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return to_float(row.get(name))
    norm = {str(k).lower().replace(" ", "").replace("_", "").replace(",", "").replace("&", "and"): k for k in (row or {}).keys()}
    for name in names:
        key = str(name).lower().replace(" ", "").replace("_", "").replace(",", "").replace("&", "and")
        if key in norm and row.get(norm[key]) not in (None, ""):
            return to_float(row.get(norm[key]))
    return float(default or 0.0)


def _first_text(row: dict, names: list[str], default: str = "") -> str:
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return str(row.get(name) or "").strip()
    norm = {str(k).lower().replace(" ", "").replace("_", ""): k for k in (row or {}).keys()}
    for name in names:
        key = str(name).lower().replace(" ", "").replace("_", "")
        if key in norm and row.get(norm[key]) not in (None, ""):
            return str(row.get(norm[key]) or "").strip()
    return str(default or "")


def _ratio_label(value: float) -> str:
    try:
        return f"{safe_round(float(value or 0) * 100, 1)}%"
    except Exception:
        return "غير متوفر"


def _company_sector_industry(symbol: str, sector: str = "", industry: str = "") -> tuple[str, str, str]:
    symbol = normalize_symbol_text(symbol)
    sector = str(sector or "").strip()
    industry = str(industry or "").strip()
    summary = ""
    try:
        company = COMPANIES_DATA.get(symbol, {}) or {}
        summary = _first_text(company, ["Business Summary", "business_summary", "Description", "Summary"], "")
        industry_id = _first_text(company, ["IndustryId", "Industry ID", "IndustryID", "industry_id"], "")
        if industry_id and industry_id in SECTOR_DATA:
            ref = SECTOR_DATA.get(industry_id, {}) or {}
            if not industry:
                industry = str(ref.get("industry", "") or "").strip()
            if not sector:
                sector = str(ref.get("sector", "") or "").strip()
    except Exception:
        pass
    return sector, industry, summary


def _business_activity_block_reason(symbol: str, sector: str, industry: str, summary: str = "") -> str:
    sector_l = str(sector or "").lower().strip()
    industry_l = str(industry or "").lower().strip()
    summary_l = str(summary or "").lower().strip()
    combined = " ".join([sector_l, industry_l, summary_l])

    if sector_l in HARAM_SECTORS:
        return f"مرفوض شرعيًا: القطاع ({sector}) غير مقبول"

    # Keep this list focused on explicit impermissible activities. Broad words like
    # "media" or "entertainment" are intentionally not blocked here because they
    # create too many false positives; those remain subject to manual review.
    hard_keywords = set(HARAM_INDUSTRY_KEYWORDS or [])
    hard_keywords.update({
        "casino", "casinos", "gambling", "betting", "sportsbook",
        "tobacco", "cigarette", "cigarettes", "alcohol", "brewery", "distillery",
        "bank", "banks", "banking", "insurance", "reinsurance",
        "mortgage", "credit services", "consumer finance", "lending",
        "asset management", "capital markets",
    })
    for kw in sorted(hard_keywords, key=len, reverse=True):
        if kw and kw in combined:
            return f"مرفوض شرعيًا: النشاط/الصناعة تحتوي ({kw})"
    return ""


def _manual_exclusion_entry(symbol: str, manual_exclusions=None) -> dict:
    symbol = normalize_symbol_text(symbol)
    if isinstance(manual_exclusions, dict):
        exclusions = manual_exclusions
    else:
        try:
            from .data_store import get_manual_sharia_exclusions_map
            exclusions = get_manual_sharia_exclusions_map()
        except Exception:
            exclusions = {}
    return (exclusions or {}).get(symbol, {}) if symbol else {}


def _manual_approval_entry(symbol: str, manual_approvals=None) -> dict:
    symbol = normalize_symbol_text(symbol)
    if isinstance(manual_approvals, dict):
        approvals = manual_approvals
    else:
        try:
            from .data_store import get_manual_sharia_approvals_map
            approvals = get_manual_sharia_approvals_map()
        except Exception:
            approvals = {}
    return (approvals or {}).get(symbol, {}) if symbol else {}


def _manual_approved_response(symbol: str, approval: dict, base_reason: str = "") -> dict:
    note = str((approval or {}).get("note", "") or (approval or {}).get("reason", "") or "").strip()
    reason = "متوافق يدويًا بعد مراجعتك"
    if base_reason:
        reason += f"؛ السبب الأصلي: {base_reason}"
    if note:
        reason += f" - {note}"
    return {
        "status": "manual_approved",
        "label": "متوافق يدويًا",
        "reason": reason,
        "manual_excluded": False,
        "manual_approved": True,
        "is_gray": False,
        "is_halal": True,
        "should_block": False,
        "note": note,
        "debt_to_assets": None,
        "interest_expense_to_revenue": None,
        "cash_to_assets": None,
        "source_filter_action": "allow",
    }


def assess_sharia(symbol, sector, industry, total_assets, cash, total_debt, manual_exclusions=None, manual_approvals=None):
    """Sharia Filter V2.

    The old filter was too late and too blunt. This version:
    - blocks manual and clearly impermissible activities;
    - blocks high interest-bearing debt to assets;
    - uses income statement proxies for interest burden where available;
    - treats missing/uncertain data as gray instead of pretending it is clean;
    - does not hard-block high cash alone, because cash is not the same as
      impermissible revenue and was causing false exclusions.
    """
    symbol = normalize_symbol_text(symbol)
    sector, industry, summary = _company_sector_industry(symbol, sector, industry)
    sector_l = str(sector).lower().strip()
    industry_l = str(industry).lower().strip()

    manual_entry = _manual_exclusion_entry(symbol, manual_exclusions)
    approval_entry = _manual_approval_entry(symbol, manual_approvals)
    if manual_entry:
        note = str(manual_entry.get("note", "") or manual_entry.get("reason", "") or "").strip()
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
            "debt_to_assets": None,
            "interest_expense_to_revenue": None,
            "cash_to_assets": None,
            "source_filter_action": "block",
        }

    activity_reason = _business_activity_block_reason(symbol, sector, industry, summary)
    if activity_reason:
        return {
            "status": "non_compliant",
            "label": "غير متوافق",
            "reason": activity_reason,
            "manual_excluded": False,
            "is_gray": False,
            "is_halal": False,
            "should_block": True,
            "note": "",
            "debt_to_assets": None,
            "interest_expense_to_revenue": None,
            "cash_to_assets": None,
            "source_filter_action": "block",
        }

    total_assets = float(total_assets or 0)
    cash = float(cash or 0)
    total_debt = float(total_debt or 0)

    income_row = INCOME_DATA.get(symbol, {}) if symbol else {}
    revenue = _first_number(income_row, ["Revenue", "Total Revenue"], 0.0)
    interest_expense_net = _first_number(income_row, ["Interest Expense, Net", "Interest Expense", "Net Interest Expense"], 0.0)
    # Positive non-operating income can include many legitimate items, so it is
    # only a weak/gray proxy unless a dedicated data provider is added later.
    non_operating_income = _first_number(income_row, ["Non-Operating Income (Loss)", "Non Operating Income"], 0.0)

    missing_research = (not sector_l) or (not industry_l) or total_assets <= 0
    if missing_research:
        missing_parts = []
        if not sector_l or not industry_l:
            missing_parts.append("القطاع/الصناعة")
        if total_assets <= 0:
            missing_parts.append("الأصول")
        reason_tail = " و ".join(missing_parts) if missing_parts else "البيانات الأساسية"
        gray_reason = f"الحكم الشرعي غير محسوم بسبب نقص أو قدم بيانات {reason_tail}"
        if approval_entry:
            return _manual_approved_response(symbol, approval_entry, gray_reason)
        return {
            "status": "gray",
            "label": "رمادي",
            "reason": gray_reason,
            "manual_excluded": False,
            "manual_approved": False,
            "is_gray": True,
            "is_halal": True,
            "should_block": bool(SHARIA_BLOCK_GRAY_IN_SOURCE),
            "note": "",
            "debt_to_assets": None,
            "interest_expense_to_revenue": None,
            "cash_to_assets": None,
            "source_filter_action": "gray",
        }

    debt_ratio = total_debt / total_assets if total_assets > 0 else 0.0
    cash_ratio = cash / total_assets if total_assets > 0 else 0.0
    interest_expense_ratio = abs(interest_expense_net) / revenue if revenue > 0 and interest_expense_net else 0.0
    non_operating_income_ratio = max(non_operating_income, 0.0) / revenue if revenue > 0 and non_operating_income > 0 else 0.0

    if debt_ratio > float(SHARIA_MAX_DEBT_TO_ASSETS or 0.33):
        return {
            "status": "non_compliant",
            "label": "غير متوافق",
            "reason": f"مرفوض شرعيًا: الديون/القروض { _ratio_label(debt_ratio) } من الأصول وتتجاوز الحد { _ratio_label(SHARIA_MAX_DEBT_TO_ASSETS) }",
            "manual_excluded": False,
            "is_gray": False,
            "is_halal": False,
            "should_block": True,
            "note": "",
            "debt_to_assets": safe_round(debt_ratio, 4),
            "interest_expense_to_revenue": safe_round(interest_expense_ratio, 4),
            "cash_to_assets": safe_round(cash_ratio, 4),
            "source_filter_action": "block",
        }

    if interest_expense_ratio > float(SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE or 0.05):
        return {
            "status": "non_compliant",
            "label": "غير متوافق",
            "reason": f"مرفوض شرعيًا: عبء الفوائد/الربا المقدر { _ratio_label(interest_expense_ratio) } من الإيرادات ويتجاوز الحد { _ratio_label(SHARIA_MAX_INTEREST_EXPENSE_TO_REVENUE) }",
            "manual_excluded": False,
            "is_gray": False,
            "is_halal": False,
            "should_block": True,
            "note": "",
            "debt_to_assets": safe_round(debt_ratio, 4),
            "interest_expense_to_revenue": safe_round(interest_expense_ratio, 4),
            "cash_to_assets": safe_round(cash_ratio, 4),
            "source_filter_action": "block",
        }

    # Non-operating income is not a clean interest-income field in this dataset.
    # We therefore avoid a hard block unless a later data source gives a direct
    # impermissible revenue ratio. For now it becomes gray if it is unusually high.
    if non_operating_income_ratio > max(float(SHARIA_MAX_IMPERMISSIBLE_REVENUE_RATIO or 0.05), 0.10):
        gray_reason = f"رمادي: دخل غير تشغيلي مرتفع { _ratio_label(non_operating_income_ratio) } ويحتاج تحققًا من مصدره"
        if approval_entry:
            resp = _manual_approved_response(symbol, approval_entry, gray_reason)
            resp.update({"debt_to_assets": safe_round(debt_ratio, 4), "interest_expense_to_revenue": safe_round(interest_expense_ratio, 4), "cash_to_assets": safe_round(cash_ratio, 4)})
            return resp
        return {
            "status": "gray",
            "label": "رمادي",
            "reason": gray_reason,
            "manual_excluded": False,
            "is_gray": True,
            "is_halal": True,
            "should_block": bool(SHARIA_BLOCK_GRAY_IN_SOURCE),
            "note": "",
            "debt_to_assets": safe_round(debt_ratio, 4),
            "interest_expense_to_revenue": safe_round(interest_expense_ratio, 4),
            "cash_to_assets": safe_round(cash_ratio, 4),
            "source_filter_action": "gray",
        }

    if cash_ratio > float(SHARIA_GRAY_CASH_TO_ASSETS or 0.33):
        gray_reason = f"رمادي: النقد/الاستثمارات السائلة { _ratio_label(cash_ratio) } من الأصول؛ لا يُستبعد تلقائيًا لكنه يحتاج مراجعة"
        if approval_entry:
            resp = _manual_approved_response(symbol, approval_entry, gray_reason)
            resp.update({"debt_to_assets": safe_round(debt_ratio, 4), "interest_expense_to_revenue": safe_round(interest_expense_ratio, 4), "cash_to_assets": safe_round(cash_ratio, 4)})
            return resp
        return {
            "status": "gray",
            "label": "رمادي",
            "reason": gray_reason,
            "manual_excluded": False,
            "is_gray": True,
            "is_halal": True,
            "should_block": bool(SHARIA_BLOCK_GRAY_IN_SOURCE),
            "note": "",
            "debt_to_assets": safe_round(debt_ratio, 4),
            "interest_expense_to_revenue": safe_round(interest_expense_ratio, 4),
            "cash_to_assets": safe_round(cash_ratio, 4),
            "source_filter_action": "gray",
        }

    return {
        "status": "compliant",
        "label": "متوافق مبدئيًا",
        "reason": f"مطابق للضوابط المبدئية: الديون { _ratio_label(debt_ratio) }، عبء الفوائد { _ratio_label(interest_expense_ratio) }",
        "manual_excluded": False,
        "manual_approved": False,
        "is_gray": False,
        "is_halal": True,
        "should_block": False,
        "note": "",
        "debt_to_assets": safe_round(debt_ratio, 4),
        "interest_expense_to_revenue": safe_round(interest_expense_ratio, 4),
        "cash_to_assets": safe_round(cash_ratio, 4),
        "source_filter_action": "allow",
    }


def assess_sharia_source_fast(symbol: str, manual_exclusions=None, manual_approvals=None) -> dict:
    """Fast pre-source Sharia screen using local datasets only.

    It intentionally avoids live quote/news calls so it can run before expensive
    technical analysis and replace non-compliant names with better candidates.
    """
    symbol = normalize_symbol_text(symbol)
    sector, industry, _summary = _company_sector_industry(symbol, "", "")
    financials = get_financials(symbol, prev_data={})
    return assess_sharia(
        symbol,
        sector,
        industry,
        financials.get("total_assets", 0),
        financials.get("cash", 0),
        financials.get("total_debt", 0),
        manual_exclusions,
        manual_approvals,
    )


def is_halal(sector, industry, total_assets, cash, total_debt):
    assessment = assess_sharia("", sector, industry, total_assets, cash, total_debt, {})
    return assessment.get("is_halal", True), assessment.get("reason", "")


def get_financials(symbol, prev_data=None):
    symbol = normalize_symbol_text(symbol)
    b = BALANCE_DATA.get(symbol, {}) or {}
    i = INCOME_DATA.get(symbol, {}) or {}

    total_assets = _first_number(b, ["Total Assets"], 0.0)
    cash = _first_number(b, [
        "Cash And Cash Equivalents",
        "Cash, Cash Equivalents & Short Term Investments",
        "Cash and Cash Equivalents",
        "Cash & Cash Equivalents",
    ], 0.0)
    short_debt = _first_number(b, ["Short Term Debt", "Short-Term Debt"], 0.0)
    long_debt = _first_number(b, ["Long Term Debt", "Long-Term Debt"], 0.0)
    total_debt = _first_number(b, ["Total Debt"], 0.0)
    if total_debt <= 0:
        total_debt = max(0.0, short_debt) + max(0.0, long_debt)

    shares = _first_number(i, ["Shares (Diluted)", "Shares Diluted"], 0.0) or _first_number(i, ["Shares (Basic)", "Shares Basic"], 0.0)
    revenue = _first_number(i, ["Revenue", "Total Revenue"], 0.0)
    interest_expense_net = _first_number(i, ["Interest Expense, Net", "Interest Expense", "Net Interest Expense"], 0.0)
    non_operating_income = _first_number(i, ["Non-Operating Income (Loss)", "Non Operating Income"], 0.0)

    if prev_data is not None and isinstance(prev_data, dict) and prev_data:
        prev = prev_data
    elif prev_data is not None and isinstance(prev_data, dict) and not prev_data:
        prev = None
    else:
        try:
            from .market_data import get_prev
            prev = get_prev(symbol)
        except Exception:
            prev = None

    current_price = prev["price"] if prev else 0.0
    approx_market_cap = current_price * shares if shares > 0 and current_price > 0 else 0.0
    debt_to_market_cap = (total_debt / approx_market_cap) if approx_market_cap > 0 else None
    debt_to_assets = (total_debt / total_assets) if total_assets > 0 else None
    cash_to_assets = (cash / total_assets) if total_assets > 0 else None
    interest_expense_to_revenue = (abs(interest_expense_net) / revenue) if revenue > 0 and interest_expense_net else None
    non_operating_income_to_revenue = (max(non_operating_income, 0) / revenue) if revenue > 0 and non_operating_income > 0 else None

    return {
        "total_assets": total_assets,
        "cash": cash,
        "short_debt": short_debt,
        "long_debt": long_debt,
        "total_debt": total_debt,
        "shares": shares,
        "revenue": revenue,
        "interest_expense_net": interest_expense_net,
        "non_operating_income": non_operating_income,
        "current_price": current_price,
        "approx_market_cap": approx_market_cap,
        "debt_to_market_cap": debt_to_market_cap,
        "debt_to_assets": debt_to_assets,
        "cash_to_assets": cash_to_assets,
        "interest_expense_to_revenue": interest_expense_to_revenue,
        "non_operating_income_to_revenue": non_operating_income_to_revenue,
    }


def dynamic_price_penalty(current_price: float, trade_type: str) -> tuple[int, str]:
    if current_price <= 0:
        return 0, ""
    if current_price < LOW_PRICE_HARD_BLOCK:
        return -30, "سهم منخفض السعر جدًا (أقل من 2$)"
    if trade_type == "Breakout" and current_price < LOW_PRICE_WARNING:
        return -15, "سهم اختراق منخفض السعر (أقل من 3$)"
    return 0, ""



