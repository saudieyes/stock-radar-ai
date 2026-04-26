from fastapi import FastAPI, Body, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
import requests
import os
import csv
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import time
import json
import secrets
import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo
from requests.adapters import HTTPAdapter
from scanner import get_scan_universe, apply_late_move_filter, assign_execution_mode, normalize_execution_labels, recalc_reentry_plan, enrich_signal_stage, enrich_strategy_profile, finalize_display_contract

from app.settings import (
    AUTH_EXEMPT_PATHS,
    APP_AUTH_ENABLED,
    APP_AUTH_USERNAME,
    APP_AUTH_PASSWORD,
    APP_AUTH_COOKIE_NAME,
    APP_AUTH_SESSION_DAYS,
    CONTEXT_CACHE,
    DATA_DIR,
    HISTORY_CACHE,
    HTTP_SESSION,
    INTRADAY_CACHE,
    INTRADAY_CACHE_TTL_CLOSED,
    INTRADAY_CACHE_TTL_OPEN,
    LOW_PRICE_HARD_BLOCK,
    LOW_PRICE_WARNING,
    NEGATIVE_NEWS_MAX_SESSIONS,
    NEWS_SCOPE_LABELS,
    PERFORMANCE_REFRESH_CACHE,
    POLYGON_API_KEY,
    POSITIVE_NEWS_MAX_SESSIONS,
    REF_INFO_CACHE,
    SECTOR_ETF_MAP,
    SNAPSHOT_CACHE,
    SNAPSHOT_CACHE_TTL_CLOSED,
    SNAPSHOT_CACHE_TTL_EXTENDED,
    SNAPSHOT_CACHE_TTL_OPEN,
    HARAM_INDUSTRY_KEYWORDS,
    HARAM_SECTORS,
)
from app.auth_session import build_auth_cookie_value, read_auth_cookie
from app.utils import *
from app.data_loader import initialize_reference_data
from app.watchlist_store import load_manual_watchlist, save_manual_watchlist
from app.portfolio_store import load_portfolio_items, save_portfolio_items
from app.data_store import (
    get_manual_sharia_exclusions_map,
    load_manual_sharia_exclusions,
    save_manual_sharia_exclusions,
)
from app.performance_tracker import *
from app.market_data import *
from app.historical_engine import *
from app.market_sector_engine import *
from app.news_engine import *
from app.sharia_filter import *
from app.scoring_engine import *
from app.strategy_engine import *
from app.display_contract import *
from app.single_stock_engine import scan_all, build_single_stock_response

app = FastAPI()


@app.middleware("http")
async def auth_session_guard(request: Request, call_next):
    if not APP_AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path or "/"
    if path in AUTH_EXEMPT_PATHS or path.startswith("/login"):
        return await call_next(request)

    if read_auth_cookie(request):
        return await call_next(request)

    wants_html = ("text/html" in str(request.headers.get("accept", ""))) or path == "/"
    if wants_html:
        return RedirectResponse(url="/login", status_code=307)
    return JSONResponse({"ok": False, "error": "auth_required"}, status_code=401)


@app.middleware("http")
async def disable_http_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


SECTOR_DATA, COMPANIES_DATA, BALANCE_DATA, INCOME_DATA = initialize_reference_data()


def render_login_page(error_message: str = "") -> HTMLResponse:
    error_html = f'<div style="margin-bottom:12px;color:#b91c1c;background:#fee2e2;border:1px solid #fecaca;padding:10px;border-radius:12px;">{error_message}</div>' if error_message else ''
    html = f"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>تسجيل دخول الأداة</title>
<style>
body{{margin:0;font-family:Arial,sans-serif;background:#eef5ff;color:#0f172a;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
.box{{width:min(420px,92vw);background:#fff;border:1px solid #dbeafe;border-radius:20px;box-shadow:0 18px 40px rgba(15,23,42,.12);padding:24px;}}
h1{{margin:0 0 8px;font-size:26px}}
p{{margin:0 0 18px;color:#475569;line-height:1.8}}
input{{width:100%;padding:12px 14px;border-radius:12px;border:1px solid #cbd5e1;margin-bottom:12px;font-size:15px;box-sizing:border-box;}}
button{{width:100%;padding:12px 14px;border:none;border-radius:12px;background:#2563eb;color:#fff;font-size:15px;font-weight:700;cursor:pointer;}}
.note{{margin-top:14px;font-size:12px;color:#64748b;line-height:1.8}}
</style>
</head>
<body>
<div class="box">
<h1>🔒 دخول الأداة</h1>
<p>هذه الأداة محمية. أدخل اسم المستخدم وكلمة المرور مرة واحدة وسيتم حفظ الجلسة على هذا الجهاز.</p>
{error_html}
<input id="loginUser" placeholder="اسم المستخدم" autocomplete="username" />
<input id="loginPass" type="password" placeholder="كلمة المرور" autocomplete="current-password" />
<button onclick="doLogin()">تسجيل الدخول</button>
<div class="note">إذا سجّلت الدخول بنجاح فلن تحتاج لإعادة الإدخال في كل زيارة، إلا إذا انتهت الجلسة أو قمت بتسجيل الخروج.</div>
</div>
<script>
async function doLogin() {{
  const username = document.getElementById('loginUser').value || '';
  const password = document.getElementById('loginPass').value || '';
  const res = await fetch('/login', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ username, password }})
  }});
  if (res.ok) {{
    window.location.href = '/';
    return;
  }}
  const data = await res.json().catch(() => ({{}}));
  alert(data.error === 'invalid_credentials' ? 'بيانات الدخول غير صحيحة' : 'تعذر تسجيل الدخول');
}}
document.getElementById('loginPass').addEventListener('keydown', (e) => {{ if (e.key === 'Enter') doLogin(); }});
</script>
</body>
</html>
"""
    return HTMLResponse(html)


@app.get("/login")
def login_page(request: Request):
    if not APP_AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=307)
    if read_auth_cookie(request):
        return RedirectResponse(url="/", status_code=307)
    return render_login_page()


@app.post("/login")
async def login_submit(request: Request):
    if not APP_AUTH_ENABLED:
        return {"ok": True, "auth_enabled": False}
    payload = await request.json()
    username = str(payload.get("username", "") or "").strip()
    password = str(payload.get("password", "") or "")
    if secrets.compare_digest(username, APP_AUTH_USERNAME) and secrets.compare_digest(password, APP_AUTH_PASSWORD):
        response = JSONResponse({"ok": True, "auth_enabled": True})
        response.set_cookie(
            key=APP_AUTH_COOKIE_NAME,
            value=build_auth_cookie_value(username),
            max_age=APP_AUTH_SESSION_DAYS * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )
        return response
    return JSONResponse({"ok": False, "error": "invalid_credentials"}, status_code=401)


@app.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(APP_AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/session")
def session_state(request: Request):
    auth_info = read_auth_cookie(request)
    return {
        "authenticated": bool(auth_info),
        "auth_enabled": APP_AUTH_ENABLED,
        "username": auth_info.get("username", "") if auth_info else "",
    }


@app.get("/")
def root():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    if os.path.exists("index_weekly_compact.html"):
        return FileResponse("index_weekly_compact.html")
    if os.path.exists("index_no_conflict.html"):
        return FileResponse("index_no_conflict.html")
    return FileResponse("index-new.html")


@app.get("/health")
def health():
    return {
        "ok": True,
        "market_phase": get_market_phase(),
        "market_phase_label": market_phase_label(get_market_phase()),
        "timestamp": datetime.now(ZoneInfo("America/New_York")).isoformat()
    }


@app.get("/trade-scan")
def trade_scan():
    results = scan_all()

    strong = sort_display_bucket([x for x in results if x.get("decision") == "دخول قوي"])
    cautious = sort_display_bucket([x for x in results if x.get("decision") == "دخول بحذر"])
    watch = sort_display_bucket([x for x in results if x.get("decision") == "مراقبة"])

    return {
        "market_phase": get_market_phase(),
        "market_phase_label": market_phase_label(get_market_phase()),
        "updated_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "universe_count": 150,
        "count": len(results),
        "strong_entries_count": len(strong),
        "cautious_entries_count": len(cautious),
        "watchlist_count": len(watch),
        "manual_sharia_exclusions_count": len(load_manual_sharia_exclusions()),
        "strong_entries": strong[:25],
        "top_ranked": strong[:25],
        "cautious_entries": cautious[:25],
        "watchlist": watch[:50],
        "opening_mode_active": is_opening_window(),
        "opening_focus": build_opening_focus(results),
        "all_results": results,
    }


@app.get("/single-stock")
def single_stock(symbol: str):
    return build_single_stock_response(symbol)


@app.post("/portfolio/add")
def portfolio_add(payload: dict = Body(...)):
    symbol = str(payload.get("symbol", "") or "").upper().strip()
    buy_price = safe_round(payload.get("buy_price", 0))
    quantity = safe_round(payload.get("quantity", 0))
    if not symbol or buy_price <= 0:
        return {"ok": False, "message": "الرمز وسعر الشراء مطلوبان"}

    items = load_portfolio_items()
    existing = None
    for item in items:
        if str(item.get("symbol", "")).upper().strip() == symbol:
            existing = item
            break

    now_text = ny_now().strftime("%Y-%m-%d %H:%M:%S")
    if existing:
        existing["buy_price"] = buy_price
        existing["quantity"] = quantity if quantity > 0 else existing.get("quantity", 0)
        existing["updated_at"] = now_text
    else:
        items.insert(0, {
            "symbol": symbol,
            "buy_price": buy_price,
            "quantity": quantity,
            "added_at": now_text,
            "updated_at": now_text,
        })
    save_portfolio_items(items)
    return {"ok": True, "message": "تم حفظ السهم في المحفظة", "items": items}


@app.post("/portfolio/remove")
def portfolio_remove(payload: dict = Body(...)):
    symbol = str(payload.get("symbol", "") or "").upper().strip()
    items = load_portfolio_items()
    items = [x for x in items if str(x.get("symbol", "")).upper().strip() != symbol]
    save_portfolio_items(items)
    return {"ok": True, "message": "تم حذف السهم من المحفظة", "items": items}


@app.get("/portfolio")
def portfolio_get():
    items = load_portfolio_items()
    out = []
    for item in items:
        symbol = str(item.get("symbol", "") or "").upper().strip()
        if not symbol:
            continue
        plan = trade_plan_pro(symbol)
        if not plan:
            continue
        plan = apply_late_move_filter(plan)
        plan = assign_execution_mode(plan)
        plan = normalize_execution_labels(plan)
        plan = enrich_signal_stage(plan)
        plan = finalize_display_contract(plan)
        if not plan.get("price_reliable_for_execution", True) and plan.get("market_phase") in {"open", "pre_market", "after_hours"}:
            plan["decision"] = "مراقبة"
            plan["execution_mode"] = "مراقبة 👀"
            plan["execution_note"] = "السعر اللحظي غير موثوق - لا تعتمد عليه للتنفيذ"
            plan["owner_action"] = "👀 راقب فقط حتى تتوفر بيانات سعر لحظية موثوقة"

        plan = enrich_display_meta(plan)
        recommendation = evaluate_portfolio_action(item, plan)
        current_price = float(plan.get("display_price", plan.get("current_price_live", 0)) or 0)
        buy_price = float(item.get("buy_price", 0) or 0)
        quantity = float(item.get("quantity", 0) or 0)
        market_value = current_price * quantity if current_price > 0 and quantity > 0 else 0.0
        cost_value = buy_price * quantity if buy_price > 0 and quantity > 0 else 0.0
        out.append({
            "symbol": symbol,
            "buy_price": safe_round(buy_price),
            "quantity": safe_round(quantity),
            "current_price": safe_round(current_price),
            "holding_change_pct": recommendation["holding_change_pct"],
            "price_source_label": str(plan.get("price_source_label", plan.get("price_source", "")) or ""),
            "market_phase_label": str(plan.get("market_phase_label", plan.get("market_phase", "")) or ""),
            "type_label": str(plan.get("trade_type_label_ar", plan.get("display_plan_family_label", plan.get("strategy_label", ""))) or ""),
            "recommendation": recommendation["recommendation"],
            "recommendation_note": recommendation["recommendation_note"],
            "target_price": recommendation["target_price"],
            "stop_loss": recommendation["stop_loss"],
            "market_value": safe_round(market_value),
            "cost_value": safe_round(cost_value),
            "added_at": str(item.get("added_at", "") or ""),
            "updated_at": str(item.get("updated_at", "") or ""),
        })

    return {"items": out}


@app.post("/watchlist/add")
def watchlist_add(payload: dict = Body(...)):
    symbol = str(payload.get("symbol", "") or "").upper().strip()
    price = safe_round(payload.get("price", 0))
    if not symbol:
        return {"ok": False, "message": "رمز السهم مطلوب"}

    items = load_manual_watchlist()
    if any(str(x.get("symbol", "")).upper().strip() == symbol for x in items):
        return {"ok": True, "message": "السهم موجود مسبقًا", "items": items}

    items.insert(0, {
        "symbol": symbol,
        "added_price": price,
        "added_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    })
    save_manual_watchlist(items)
    return {"ok": True, "message": "تمت الإضافة للمراقبة", "items": items}


@app.post("/watchlist/remove")
def watchlist_remove(payload: dict = Body(...)):
    symbol = str(payload.get("symbol", "") or "").upper().strip()
    items = load_manual_watchlist()
    items = [x for x in items if str(x.get("symbol", "")).upper().strip() != symbol]
    save_manual_watchlist(items)
    return {"ok": True, "message": "تم حذف السهم من المراقبة", "items": items}


@app.get("/watchlist")
def watchlist_get():
    items = load_manual_watchlist()
    out = []
    for item in items:
        symbol = str(item.get("symbol", "") or "").upper().strip()
        prev = get_prev(symbol)
        intraday = get_intraday_snapshot(symbol)
        live_block = build_live_price_block(symbol, prev or {}, intraday)
        current_display = live_block.get("display_price", 0)
        added_price = safe_round(item.get("added_price", 0))
        change_pct = 0.0
        if current_display and added_price:
            change_pct = ((current_display - added_price) / added_price) * 100
        out.append({
            "symbol": symbol,
            "added_price": added_price,
            "added_at": item.get("added_at", ""),
            "current_price": safe_round(current_display),
            "change_pct": safe_round(change_pct),
            "price_source": live_block.get("price_source", ""),
            "price_source_label": live_block.get("price_source_label", ""),
            "market_phase_label": live_block.get("market_phase_label", ""),
        })
    return {"items": out}


@app.post("/sharia-exclusions/add")
def sharia_exclusions_add(payload: dict = Body(...)):
    symbol = normalize_symbol_text(payload.get("symbol", ""))
    if not symbol:
        return JSONResponse({"ok": False, "error": "missing_symbol"}, status_code=400)
    note = str(payload.get("note", "") or "").strip()
    items = load_manual_sharia_exclusions()
    now_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    found = False
    for item in items:
        if normalize_symbol_text(item.get("symbol", "")) == symbol:
            item["note"] = note
            item["excluded_at"] = now_str
            found = True
            break
    if not found:
        items.insert(0, {"symbol": symbol, "note": note, "excluded_at": now_str})
    save_manual_sharia_exclusions(items)
    return {"ok": True, "count": len(items), "symbol": symbol}


@app.post("/sharia-exclusions/remove")
def sharia_exclusions_remove(payload: dict = Body(...)):
    symbol = normalize_symbol_text(payload.get("symbol", ""))
    items = [item for item in load_manual_sharia_exclusions() if normalize_symbol_text(item.get("symbol", "")) != symbol]
    save_manual_sharia_exclusions(items)
    return {"ok": True, "count": len(items), "symbol": symbol}


@app.get("/sharia-exclusions")
def sharia_exclusions_get():
    items = load_manual_sharia_exclusions()
    enriched = []
    for item in items:
        symbol = normalize_symbol_text(item.get("symbol", ""))
        if not symbol:
            continue
        info = get_info(symbol)
        prev = get_prev(symbol)
        financials = get_financials(symbol, prev)
        assessment = assess_sharia(symbol, info.get("sector", ""), info.get("industry", ""), financials["total_assets"], financials["cash"], financials["total_debt"], {symbol: item})
        enriched.append({
            "symbol": symbol,
            "company": info.get("company", ""),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "excluded_at": str(item.get("excluded_at", "") or ""),
            "note": str(item.get("note", "") or ""),
            "current_price": float((prev or {}).get("price", 0) or 0),
            "sharia_status": assessment.get("status", "manual_excluded"),
            "sharia_label": assessment.get("label", "مستبعد يدويًا"),
            "sharia_reason": assessment.get("reason", "مستبعد يدويًا من قائمتك الشرعية"),
        })
    return {"items": enriched, "count": len(enriched)}


@app.get("/performance")
def performance_get():
    store = rollover_performance_store_if_needed(load_performance_store())
    records = list(store.get("active_records", []))
    updated = []
    now_text = ny_now().strftime("%Y-%m-%d %H:%M:%S")
    final_outcomes = {"above_target", "target_hit", "loss", "expired", "partial_gain"}
    refresh_rows = []
    refresh_symbols = []
    for item in records[:500]:
        if str(item.get("outcome", "ongoing") or "ongoing") not in final_outcomes:
            refresh_rows.append(item)
            refresh_symbols.append(str(item.get("symbol", "") or "").upper().strip())

    refresh_results = {}
    if refresh_symbols:
        max_workers = min(12, max(4, len(refresh_symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(get_performance_live_price, symbol): symbol for symbol in refresh_symbols if symbol}
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    refresh_results[symbol] = future.result() or {}
                except:
                    refresh_results[symbol] = {}

    for item in records[:500]:
        symbol = str(item.get("symbol", "") or "").upper().strip()
        current_price = float(item.get("current_price", 0) or 0)
        price_source_label = str(item.get("price_source_label", "") or "")
        live_payload = refresh_results.get(symbol, {}) if symbol else {}
        live_price = float((live_payload or {}).get("current_price", 0) or 0)
        if live_price > 0:
            current_price = live_price
            price_source_label = str((live_payload or {}).get("price_source_label", price_source_label) or price_source_label)
        item["last_seen_at"] = now_text
        item["price_source_label"] = price_source_label
        evaluate_performance_record(item, current_price)
        updated.append({
            **item,
            "current_price": safe_round(item.get("current_price", 0)),
            "entry_price": safe_round(item.get("entry_price", 0)),
            "target_price": safe_round(item.get("target_price", 0)),
            "target_2_price": safe_round(item.get("target_2_price", 0)),
            "stop_loss": safe_round(item.get("stop_loss", 0)),
            "max_price_seen": safe_round(item.get("max_price_seen", 0)),
            "min_price_seen": safe_round(item.get("min_price_seen", 0)),
            "last_change_pct": safe_round(item.get("last_change_pct", 0)),
        })

    dashboard = build_performance_dashboard(updated)
    store["active_records"] = dashboard["rows"]
    save_performance_store(store)

    return {
        "storage": {"data_dir": str(DATA_DIR), "performance_file": PERFORMANCE_FILE},
        "active_week": {
            "week_key": store.get("active_week_key"),
            "week_start": store.get("active_week_start"),
            "week_end": store.get("active_week_end"),
        },
        "items": dashboard["rows"],
        "summary": dashboard["summary"],
        "groups": dashboard["groups"],
        "simulation": dashboard["simulation"],
        "weekly_archive": store.get("weekly_archive", [])[:26],
    }
