from fastapi import FastAPI, Body, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, PlainTextResponse
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
import threading
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
    FIRST_RUN_SETUP_ENABLED,
    NEWS_SCORE_ENABLED,
    FMP_API_KEY,
    FMP_WEBSOCKET_ENABLED,
    LIVE_QUOTES_ENABLED,
)
from app.auth_session import build_auth_cookie_value, read_auth_cookie
from app.utils import *
from app.data_loader import initialize_reference_data
from app.watchlist_store import load_manual_watchlist, save_manual_watchlist
from app.portfolio_store import load_portfolio_items, save_portfolio_items
from app.data_store import (
    get_manual_sharia_exclusions_map,
    get_manual_sharia_sync_diagnostics,
    load_manual_sharia_exclusions,
    save_manual_sharia_exclusions,
    get_manual_sharia_approvals_map,
    get_manual_sharia_approvals_sync_diagnostics,
    load_manual_sharia_approvals,
    save_manual_sharia_approvals,
)
from app.github_sync import (
    github_sync_status,
    push_json_file,
)
from app.settings import GITHUB_SYNC_MANUAL_SHARIA_PATH, GITHUB_SYNC_MANUAL_SHARIA_APPROVALS_PATH
from app.performance_tracker import *
from app.market_data import *
from app.historical_engine import *
from app.market_sector_engine import *
from app.news_engine import *
from app.sharia_filter import *
from app.scoring_engine import *
from app.strategy_engine import *
from app.display_contract import *
from app.single_stock_engine import scan_all, build_single_stock_response, get_last_scan_debug

from app.sqlite_store import init_db, sqlite_status, set_json, get_json
from app.user_auth_store import has_auth_user, create_first_user, verify_db_user
from app.live_quotes import get_live_quotes
from app.tracking_intelligence import (
    init_tracking_intelligence_db,
    tracking_status,
    record_tracking_snapshots,
    mark_tracking_absences_from_scan,
    refresh_tracking_prices_from_rows,
    build_tracking_weekly_report,
    build_tracking_weekly_brief,
    export_tracking_json,
    export_tracking_csv,
)
from app.missed_opportunities import (
    init_missed_opportunities_db,
    missed_status,
    build_missed_weekly_report,
    build_missed_weekly_brief,
    export_missed_json,
    export_missed_csv,
)
from app.source_discovery import (
    dynamic_discovery_enabled,
    get_full_market_scan_interval_sec,
    get_last_dynamic_discovery_status,
)

app = FastAPI()

try:
    init_db()
except Exception as exc:
    print(f"SQLITE_INIT_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)

try:
    init_tracking_intelligence_db()
except Exception as exc:
    print(f"TRACKING_INTELLIGENCE_INIT_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)

try:
    init_missed_opportunities_db()
except Exception as exc:
    print(f"MISSED_OPPORTUNITIES_INIT_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)



@app.middleware("http")
async def auth_session_guard(request: Request, call_next):
    path = request.url.path or "/"
    db_user_exists = has_auth_user()
    auth_required = bool(APP_AUTH_ENABLED or db_user_exists)

    if path in AUTH_EXEMPT_PATHS or path.startswith("/login") or path.startswith("/setup"):
        return await call_next(request)

    wants_html = ("text/html" in str(request.headers.get("accept", ""))) or path == "/"

    if not auth_required:
        if FIRST_RUN_SETUP_ENABLED:
            if wants_html:
                return RedirectResponse(url="/setup", status_code=307)
            return JSONResponse({"ok": False, "error": "setup_required"}, status_code=428)
        return await call_next(request)

    if read_auth_cookie(request):
        return await call_next(request)

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

# Fix29 live radar runtime settings.
# Prices are fresh during open/pre/after market; cached quote prices are allowed only when closed.
ACTIVE_MARKET_PHASES = {"open", "pre_market", "after_hours"}
LIVE_RADAR_PRICE_REFRESH_SEC = int(float(os.getenv("LIVE_RADAR_PRICE_REFRESH_SEC", "30") or 30))
LIVE_RADAR_FULL_SCAN_SEC = int(float(os.getenv("LIVE_RADAR_FULL_SCAN_SEC", "300") or 300))
LIVE_RADAR_WORKER_ENABLED = str(os.getenv("LIVE_RADAR_WORKER_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
LIVE_RADAR_WORKER_MAX_SYMBOLS = int(float(os.getenv("LIVE_RADAR_WORKER_MAX_SYMBOLS", "220") or 220))
LIVE_RADAR_WORKER_LOCK = threading.Lock()
LIVE_RADAR_FULL_SCAN_LOCK = threading.Lock()
LIVE_RADAR_WORKER_STATE = {
    "enabled": LIVE_RADAR_WORKER_ENABLED,
    "running": False,
    "last_live_refresh_at": "",
    "last_full_scan_at": "",
    "last_error": "",
    "iterations": 0,
    "full_scans": 0,
    "live_refreshes": 0,
    "price_refresh_sec": LIVE_RADAR_PRICE_REFRESH_SEC,
    "full_scan_sec": LIVE_RADAR_FULL_SCAN_SEC,
}


def _is_active_market_phase(phase: str | None = None) -> bool:
    return str(phase or get_market_phase() or "closed") in ACTIVE_MARKET_PHASES


def _prefer_price_cache_for_phase(phase: str | None = None) -> bool:
    # User rule: no cached prices while market/pre/after-hours are active.
    return not _is_active_market_phase(phase)


def _server_full_market_scan_interval_sec(phase: str | None = None) -> int:
    """Server-side full-market discovery cadence.

    The source layer performs a broad market discovery scan and the deep radar
    analyzes only the best shortlist. During active pre/open/after-hours we use
    the dynamic schedule agreed with the user; if Dynamic Discovery is disabled,
    we fall back to the legacy interval variable.
    """
    try:
        if dynamic_discovery_enabled():
            return max(300, int(get_full_market_scan_interval_sec() or LIVE_RADAR_FULL_SCAN_SEC))
    except Exception:
        pass
    try:
        return max(300, int(LIVE_RADAR_FULL_SCAN_SEC or 300))
    except Exception:
        return 300


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


def render_setup_page(error_message: str = "") -> HTMLResponse:
    error_html = f'<div style="margin-bottom:12px;color:#b91c1c;background:#fee2e2;border:1px solid #fecaca;padding:10px;border-radius:12px;">{error_message}</div>' if error_message else ''
    html = f"""
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>الإعداد الأولي</title>
<style>
body{{margin:0;font-family:Arial,sans-serif;background:#eef5ff;color:#0f172a;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
.box{{width:min(440px,92vw);background:#fff;border:1px solid #dbeafe;border-radius:20px;box-shadow:0 18px 40px rgba(15,23,42,.12);padding:24px;}}
h1{{margin:0 0 8px;font-size:26px}}
p{{margin:0 0 18px;color:#475569;line-height:1.8}}
input{{width:100%;padding:12px 14px;border-radius:12px;border:1px solid #cbd5e1;margin-bottom:12px;font-size:15px;box-sizing:border-box;}}
button{{width:100%;padding:12px 14px;border:none;border-radius:12px;background:#2563eb;color:#fff;font-size:15px;font-weight:700;cursor:pointer;}}
.note{{margin-top:14px;font-size:12px;color:#64748b;line-height:1.8}}
</style>
</head>
<body>
<div class="box">
<h1>⚙️ الإعداد الأولي</h1>
<p>أنشئ مستخدم الأداة لأول مرة. سيتم حفظه في SQLite على مساحة التخزين الدائمة.</p>
{error_html}
<input id="setupUser" placeholder="اسم المستخدم" autocomplete="username" />
<input id="setupPass" type="password" placeholder="كلمة المرور - 6 أحرف على الأقل" autocomplete="new-password" />
<button onclick="doSetup()">إنشاء المستخدم</button>
<div class="note">بعد الإنشاء سيتم تحويلك لتسجيل الدخول. إذا كنت تستخدم متغيرات Railway للدخول، فلن تظهر هذه الصفحة.</div>
</div>
<script>
async function doSetup() {{
  const username = document.getElementById('setupUser').value || '';
  const password = document.getElementById('setupPass').value || '';
  const res = await fetch('/setup', {{
    method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ username, password }})
  }});
  if (res.ok) {{ window.location.href = '/login'; return; }}
  const data = await res.json().catch(() => ({{}}));
  alert(data.error === 'password_too_short' ? 'كلمة المرور قصيرة' : data.error === 'username_too_short' ? 'اسم المستخدم قصير' : 'تعذر إنشاء المستخدم');
}}
document.getElementById('setupPass').addEventListener('keydown', (e) => {{ if (e.key === 'Enter') doSetup(); }});
</script>
</body>
</html>
"""
    return HTMLResponse(html)


@app.get("/setup")
def setup_page(request: Request):
    if APP_AUTH_ENABLED or has_auth_user() or not FIRST_RUN_SETUP_ENABLED:
        return RedirectResponse(url="/login" if (APP_AUTH_ENABLED or has_auth_user()) else "/", status_code=307)
    return render_setup_page()


@app.post("/setup")
async def setup_submit(request: Request):
    if APP_AUTH_ENABLED:
        return JSONResponse({"ok": False, "error": "env_auth_enabled"}, status_code=409)
    if has_auth_user():
        return JSONResponse({"ok": False, "error": "user_already_exists"}, status_code=409)
    payload = await request.json()
    ok, msg = create_first_user(payload.get("username", ""), payload.get("password", ""))
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=400)
    return {"ok": True}


@app.get("/login")
def login_page(request: Request):
    auth_required = bool(APP_AUTH_ENABLED or has_auth_user())
    if not auth_required:
        return RedirectResponse(url="/setup" if FIRST_RUN_SETUP_ENABLED else "/", status_code=307)
    if read_auth_cookie(request):
        return RedirectResponse(url="/", status_code=307)
    return render_login_page()


@app.post("/login")
async def login_submit(request: Request):
    auth_required = bool(APP_AUTH_ENABLED or has_auth_user())
    if not auth_required:
        return {"ok": True, "auth_enabled": False}
    payload = await request.json()
    username = str(payload.get("username", "") or "").strip()
    password = str(payload.get("password", "") or "")
    valid = False
    if APP_AUTH_ENABLED:
        valid = secrets.compare_digest(username, APP_AUTH_USERNAME) and secrets.compare_digest(password, APP_AUTH_PASSWORD)
    else:
        valid = verify_db_user(username, password)
    if valid:
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
        "auth_enabled": bool(APP_AUTH_ENABLED or has_auth_user()),
        "auth_source": "env" if APP_AUTH_ENABLED else ("sqlite" if has_auth_user() else "none"),
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
        "timestamp": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "sqlite": sqlite_status(),
        "live_quotes_enabled": bool(LIVE_QUOTES_ENABLED),
        "news_score_enabled": bool(NEWS_SCORE_ENABLED),
    }


@app.get("/runtime-diagnostics")
def runtime_diagnostics():
    return {
        "ok": True,
        "sqlite": sqlite_status(),
        "auth": {
            "env_auth_enabled": bool(APP_AUTH_ENABLED),
            "sqlite_user_exists": bool(has_auth_user()),
            "first_run_setup_enabled": bool(FIRST_RUN_SETUP_ENABLED),
        },
        "live_data": {
            "live_quotes_enabled": bool(LIVE_QUOTES_ENABLED),
            "fmp_key_configured": bool(FMP_API_KEY),
            "fmp_websocket_enabled_config": bool(FMP_WEBSOCKET_ENABLED),
            "polygon_key_configured": bool(POLYGON_API_KEY),
            "last_radar_live_refresh": get_json("last_radar_live_refresh", {}),
            "live_radar_worker": get_json("live_radar_worker_status", {}),
        },
        "scoring": {
            "news_score_enabled": bool(NEWS_SCORE_ENABLED),
            "news_mode": "scored" if NEWS_SCORE_ENABLED else "context_only",
        },
        "data_dir": str(DATA_DIR),
    }


@app.get("/live-quotes")
def live_quotes_endpoint(symbols: str = "", allow_fallback: bool = True, prefer_cache: bool | None = None):
    clean = [s.strip().upper() for s in str(symbols or "").replace(";", ",").split(",") if s.strip()]
    if not clean:
        return {"ok": True, "quotes": {}, "diagnostics": {"symbols": 0}}
    phase = get_market_phase()
    if prefer_cache is None:
        prefer_cache = _prefer_price_cache_for_phase(phase)
    bundle = get_live_quotes(clean, prefer_cache=bool(prefer_cache), allow_fallback=allow_fallback)
    if isinstance(bundle, dict):
        bundle["market_phase"] = phase
        bundle["market_phase_label"] = market_phase_label(phase)
        bundle["quote_cache_policy"] = "cache_ok_closed_market" if bool(prefer_cache) else "fresh_fmp_during_active_market"
    return bundle


# Fix19: live radar refresh layer.
# This is intentionally lightweight: it never reruns the heavy scan and never changes the
# saved technical plan. It only overlays fresh FMP/Polygon quotes on the latest saved scan
# snapshot, recalculates live distances/readiness hints, and returns newly sorted groups for
# the UI. If live data fails, the normal /trade-scan and /single-stock routes remain unchanged.
def _extract_live_symbol_list(rows: list[dict], limit: int = 220) -> list[str]:
    out = []
    for row in rows or []:
        sym = normalize_symbol_text((row or {}).get("symbol", ""))
        if sym and sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def _first_positive_number(row: dict, keys: list[str]) -> float:
    for key in keys:
        try:
            val = safe_round(row.get(key, 0))
            if val > 0:
                return float(val)
        except Exception:
            continue
    return 0.0


def _live_distance_pct(price: float, ref: float) -> float | None:
    try:
        price = float(price or 0)
        ref = float(ref or 0)
        if price <= 0 or ref <= 0:
            return None
        return safe_round(((price - ref) / ref) * 100, 2)
    except Exception:
        return None


def _apply_live_quote_overlay(row: dict, quote: dict | None) -> dict:
    """Overlay live quote fields without replacing the saved analysis plan.

    The returned row keeps the original plan prices. Extra fields are prefixed with
    live_/snapshot_ so the UI can clearly show what came from the saved plan and what
    changed after the latest quote.
    """
    out = dict(row or {})
    symbol = normalize_symbol_text(out.get("symbol", ""))
    quote = quote or {}
    live_price = safe_round(quote.get("price", 0))
    original_display_price = safe_round(out.get("display_price", out.get("current_price_live", out.get("current_price", 0))))

    out["snapshot_display_price"] = original_display_price
    out["snapshot_price_source_label"] = str(out.get("price_source_label", out.get("price_source", "")) or "")
    out["live_overlay_available"] = bool(live_price > 0)

    if live_price <= 0:
        out["live_overlay_label"] = "لا يوجد سعر حي جديد"
        out["live_rank_score"] = safe_round(out.get("display_rank_score", _stock_score_value(out)), 2)
        out["live_rank_reason"] = "تم استخدام ترتيب التحليل المحفوظ لعدم توفر سعر حي جديد."
        return out

    prev_close = safe_round(quote.get("previous_close", 0))
    change_pct = safe_round(quote.get("change_pct", 0), 2)
    volume = safe_round(quote.get("volume", 0))
    source_label = str(quote.get("source_label", "") or quote.get("source", "FMP/Live"))
    updated_label = str(quote.get("updated_label", "") or "")

    entry = _first_positive_number(out, [
        "display_entry_price", "smart_entry_price", "entry_price_real", "entry", "breakout_price", "confirmation_price"
    ])
    stop = _first_positive_number(out, ["display_stop_price", "smart_stop_loss", "stop_loss", "stop"])
    target1 = _first_positive_number(out, ["display_target_price", "smart_target_1", "target_1", "target1", "target"])

    distance_to_entry = _live_distance_pct(live_price, entry)
    distance_to_target = _live_distance_pct(live_price, target1)
    distance_to_stop = _live_distance_pct(live_price, stop)
    snapshot_move_pct = _live_distance_pct(live_price, original_display_price)

    readiness = safe_round(out.get("execution_readiness_score", 0), 2)
    quality = safe_round(out.get("quality_score", 0), 2)
    base_rank = safe_round(out.get("display_rank_score", _stock_score_value(out)), 2)
    live_adjustment = 0.0
    live_label = "تحديث حي"
    live_reason_bits = []

    if distance_to_entry is not None:
        abs_entry_dist = abs(distance_to_entry)
        out["live_distance_to_entry_pct"] = distance_to_entry
        if -0.65 <= distance_to_entry <= 1.25:
            live_adjustment += 8.0
            live_label = "قريب من نقطة التنفيذ"
            live_reason_bits.append("السعر قريب من نقطة الدخول")
        elif 1.25 < distance_to_entry <= 3.5:
            live_adjustment += 2.0
            live_label = "تحرك بعد الدخول"
            live_reason_bits.append("السعر فوق الدخول لكن لم يبتعد كثيرًا")
        elif distance_to_entry > 6.0:
            live_adjustment -= 8.0
            live_label = "ابتعد عن الدخول"
            live_reason_bits.append("السعر ابتعد عن نقطة الدخول")
        elif distance_to_entry < -3.5:
            live_adjustment -= 3.5
            live_label = "لم يؤكد الدخول بعد"
            live_reason_bits.append("السعر ما زال دون نقطة الدخول")
        else:
            live_reason_bits.append(f"بعده عن الدخول {abs_entry_dist:.2f}%")

    if target1 and live_price >= target1:
        live_adjustment -= 12.0
        live_label = "اقترب/حقق الهدف"
        live_reason_bits.append("السعر وصل إلى الهدف الأول أو تجاوزه")
    elif distance_to_target is not None:
        out["live_distance_to_target_pct"] = distance_to_target

    if stop and live_price <= stop:
        live_adjustment -= 20.0
        live_label = "كسر الوقف"
        live_reason_bits.append("السعر عند/دون وقف الخسارة")
    elif distance_to_stop is not None:
        out["live_distance_to_stop_pct"] = distance_to_stop

    if snapshot_move_pct is not None:
        out["live_move_vs_snapshot_pct"] = snapshot_move_pct
        if abs(snapshot_move_pct) >= 2.5:
            out["snapshot_stale_warning"] = True
            live_reason_bits.append("السعر تغير بوضوح عن لقطة التحليل")
        else:
            out["snapshot_stale_warning"] = False

    live_rank = max(0.0, base_rank + live_adjustment)
    out.update({
        "live_price": live_price,
        "current_price_live": live_price,
        "display_price": live_price,
        "display_change_pct": change_pct,
        "previous_close_live": prev_close,
        "volume_live": volume,
        "price_source": str(quote.get("source", "live_overlay") or "live_overlay"),
        "price_source_label": source_label,
        "last_price_update_label": updated_label,
        "live_overlay_label": live_label,
        "live_rank_score": safe_round(live_rank, 2),
        "live_rank_adjustment": safe_round(live_adjustment, 2),
        "live_rank_reason": "، ".join(live_reason_bits[:4]) if live_reason_bits else "تم تحديث السعر الحي دون تغيير كبير في الخطة.",
        "analysis_snapshot_price": original_display_price,
        "analysis_snapshot_note": "الخطة الأصلية محفوظة؛ السعر الحي يستخدم لتحديث القرب والترتيب فقط.",
    })
    return _live_plan_validity_guard(out)



def _live_plan_validity_guard(row: dict) -> dict:
    """Reclassify the visible decision from fresh price vs saved plan.

    This never changes Sharia status and never gives news points. It only prevents
    stale strong/cautious labels when live price has invalidated the saved plan,
    and it can promote candidates already inside the saved scan universe when the
    live price reaches the plan conditions.
    """
    out = dict(row or {})
    original_decision = str(out.get("original_decision") or out.get("decision") or "")
    out["original_decision"] = original_decision

    if _is_blocked_sharia(out):
        out["live_plan_status"] = "sharia_blocked"
        out["live_plan_action"] = "مستبعد شرعيًا"
        return out

    live_price = safe_round(out.get("live_price", out.get("display_price", 0)))
    if live_price <= 0 or not out.get("live_overlay_available"):
        out["effective_decision"] = original_decision
        out["live_plan_status"] = "no_fresh_price"
        out["live_plan_action"] = "لم يصل سعر حي جديد"
        return out

    entry = _first_positive_number(out, ["display_entry_price", "smart_entry_price", "entry_price_real", "entry", "breakout_price", "confirmation_price"])
    stop = _first_positive_number(out, ["display_stop_price", "smart_stop_loss", "stop_loss", "stop"])
    target1 = _first_positive_number(out, ["display_target_price", "smart_target_1", "target_1", "target1", "target"])
    quality = safe_round(out.get("quality_score", 0))
    execution = safe_round(out.get("execution_readiness_score", 0))
    risk_pct = safe_round(out.get("display_risk_pct", out.get("risk_pct", 99)))
    base_score = safe_round(out.get("display_rank_score", _stock_score_value(out)), 2)
    dist_entry = _live_distance_pct(live_price, entry) if entry else None
    dist_snapshot = _live_distance_pct(live_price, safe_round(out.get("analysis_snapshot_price", out.get("snapshot_display_price", 0))))

    status = "valid"
    action = "الخطة ما زالت صالحة"
    new_decision = original_decision
    adjustment = 0.0
    reasons = []

    if stop and live_price <= stop:
        status = "invalid_stop_broken"
        action = "الخطة غير صالحة: كسر الوقف"
        new_decision = "مراقبة"
        adjustment -= 45
        reasons.append("السعر الحي كسر وقف الخطة")
    elif target1 and live_price >= target1:
        status = "target_reached"
        action = "تم الوصول للهدف الأول"
        new_decision = "مراقبة"
        adjustment -= 22
        reasons.append("السعر وصل/تجاوز الهدف الأول")
    elif entry and dist_entry is not None:
        if original_decision == "دخول قوي":
            if dist_entry < -1.0:
                status = "strong_failed_below_entry"
                action = "هبط من دخول قوي: السعر دون الدخول"
                new_decision = "مراقبة"
                adjustment -= 30
                reasons.append("السعر الحي دون نقطة الدخول بأكثر من 1%")
            elif dist_entry > 4.0:
                status = "late_after_entry"
                action = "لم يعد دخولًا قويًا: السعر ابتعد"
                new_decision = "دخول بحذر"
                adjustment -= 15
                reasons.append("السعر ابتعد عن نقطة الدخول")
        elif original_decision == "دخول بحذر":
            if dist_entry < -1.8:
                status = "cautious_failed_below_entry"
                action = "هبط إلى مراقبة"
                new_decision = "مراقبة"
                adjustment -= 18
                reasons.append("السعر دون منطقة التفعيل")
            elif -0.4 <= dist_entry <= 1.25 and quality >= 78 and execution >= 54 and risk_pct <= 8.5:
                status = "promoted_to_strong"
                action = "ترقى إلى دخول قوي"
                new_decision = "دخول قوي"
                adjustment += 15
                reasons.append("السعر الحي أكد منطقة الدخول مع جودة وجاهزية عالية")
        else:
            if -0.5 <= dist_entry <= 1.25 and quality >= 78 and execution >= 54 and risk_pct <= 8.5:
                status = "promoted_to_strong"
                action = "دخل قائمة دخول قوي"
                new_decision = "دخول قوي"
                adjustment += 18
                reasons.append("تحققت شروط الدخول القوي من السعر الحي")
            elif -0.8 <= dist_entry <= 2.5 and quality >= 62 and risk_pct <= 12:
                status = "promoted_to_cautious"
                action = "دخل قائمة دخول بحذر"
                new_decision = "دخول بحذر"
                adjustment += 10
                reasons.append("تحققت شروط الدخول بحذر من السعر الحي")

    if dist_snapshot is not None and abs(dist_snapshot) >= 2.5 and status == "valid":
        status = "snapshot_far_from_live"
        action = "السعر ابتعد عن لقطة التحليل"
        if original_decision == "دخول قوي":
            new_decision = "دخول بحذر"
            adjustment -= 8
        reasons.append("فرق السعر الحالي عن لقطة التحليل أصبح واضحًا")

    if _is_gray_sharia(out) and new_decision == "دخول قوي":
        # Keep technical strength, but route to gray bucket instead of clean strong.
        out["gray_technical_decision"] = "دخول قوي"

    out["decision"] = new_decision
    out["effective_decision"] = new_decision
    out["live_plan_status"] = status
    out["live_plan_action"] = action
    out["live_plan_reason"] = "، ".join(reasons[:3]) if reasons else action
    out["live_plan_adjustment"] = safe_round(adjustment, 2)
    out["live_rank_score"] = safe_round(max(0.0, safe_round(out.get("live_rank_score", base_score)) + adjustment), 2)
    if reasons:
        existing = str(out.get("live_rank_reason", "") or "")
        merged = "، ".join([x for x in [existing] + reasons if x][:5])
        out["live_rank_reason"] = merged
    return out

def _sort_live_bucket(rows: list[dict]) -> list[dict]:
    return sorted(rows or [], key=lambda x: float(x.get("live_rank_score", x.get("display_rank_score", _stock_score_value(x))) or 0), reverse=True)


def _live_bucket_payload(rows: list[dict], limit: int) -> dict:
    sorted_rows = _sort_live_bucket(rows)
    return {
        "count": len(sorted_rows),
        "items": sorted_rows[:max(1, int(limit or 25))],
        "omitted": max(0, len(sorted_rows) - max(1, int(limit or 25))),
    }


@app.get("/radar-live-refresh")
def radar_live_refresh(limit: int = 25, allow_fallback: bool = True, include_watch: bool = True, prefer_cache: bool | None = None):
    """Return a fast live overlay for the last /trade-scan snapshot.

    During active/pre/after market hours, do not reuse the saved SQLite quote cache by
    default: the UI should see fresh FMP REST/BATCH quotes. When the market is closed,
    using the SQLite quote cache keeps the page fast and avoids unnecessary calls.
    """
    phase = get_market_phase()
    active_price_window = phase in {"open", "pre_market", "after_hours"}
    if prefer_cache is None:
        prefer_cache = not active_price_window

    snapshot = get_json("last_trade_scan_snapshot", {})
    rows = snapshot.get("rows", []) if isinstance(snapshot, dict) else []
    if not isinstance(rows, list) or not rows:
        return {
            "ok": False,
            "error": "no_saved_scan_snapshot",
            "message": "شغّل فحص الرادار مرة واحدة أولاً عبر الصفحة الرئيسية أو /trade-scan.",
            "news_score_enabled": bool(NEWS_SCORE_ENABLED),
            "news_mode": "scored" if NEWS_SCORE_ENABLED else "context_only",
        }

    symbols = _extract_live_symbol_list(rows, limit=220)
    quote_bundle = get_live_quotes(symbols, prefer_cache=bool(prefer_cache), allow_fallback=allow_fallback)
    quotes = quote_bundle.get("quotes", {}) if isinstance(quote_bundle, dict) else {}
    overlaid = []
    for row in rows:
        sym = normalize_symbol_text((row or {}).get("symbol", ""))
        overlaid.append(_apply_live_quote_overlay(row, quotes.get(sym)))
    # Manual Sharia decisions must override the saved scan snapshot immediately.
    overlaid = _apply_manual_sharia_overrides(overlaid)

    strong = [x for x in overlaid if x.get("decision") == "دخول قوي" and not _is_blocked_sharia(x) and not _is_gray_sharia(x)]
    gray_strong, premarket_setups, watch = _build_special_buckets(overlaid, phase)
    special_symbols = {normalize_symbol_text(x.get("symbol", "")) for x in (gray_strong + premarket_setups)}
    cautious = [
        x for x in overlaid
        if x.get("decision") == "دخول بحذر"
        and normalize_symbol_text(x.get("symbol", "")) not in special_symbols
        and not _is_blocked_sharia(x)
        and not _is_gray_sharia(x)
    ]
    if not include_watch:
        watch = []

    tracking_live_stats = {}
    try:
        # Update Tracking Intelligence outcomes from the same fresh prices already
        # fetched for the live overlay. This adds no API calls and does not create
        # new tracking records from the 30-second loop.
        tracking_live_stats = refresh_tracking_prices_from_rows(overlaid, source="radar_live_refresh")
    except Exception as exc:
        tracking_live_stats = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    updated_at_raw = str(snapshot.get("updated_at", "") or "") if isinstance(snapshot, dict) else ""
    snapshot_age_sec = None
    try:
        dt = datetime.strptime(updated_at_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("America/New_York"))
        snapshot_age_sec = int((datetime.now(ZoneInfo("America/New_York")) - dt).total_seconds())
    except Exception:
        pass

    payload = {
        "ok": True,
        "mode": "live_overlay_from_saved_scan",
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "snapshot_updated_at": updated_at_raw,
        "snapshot_age_sec": snapshot_age_sec,
        "live_updated_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "symbols_requested": len(symbols),
        "quotes_available": len(quotes),
        "quote_diagnostics": quote_bundle.get("diagnostics", {}) if isinstance(quote_bundle, dict) else {},
        "quote_cache_policy": "cache_ok_closed_market" if bool(prefer_cache) else "fresh_fmp_during_active_market",
        "news_score_enabled": bool(NEWS_SCORE_ENABLED),
        "news_mode": "scored" if NEWS_SCORE_ENABLED else "context_only",
        "tracking_intelligence": tracking_live_stats,
        "groups": {
            "strong_entries": _live_bucket_payload(strong, limit),
            "cautious_entries": _live_bucket_payload(cautious, limit),
            "gray_strong": _live_bucket_payload(gray_strong, limit),
            "premarket_setups": _live_bucket_payload(premarket_setups, limit),
            "watchlist": _live_bucket_payload(watch, limit if include_watch else 1),
        },
    }
    try:
        set_json("last_radar_live_refresh", {
            "updated_at": payload["live_updated_at"],
            "symbols_requested": payload["symbols_requested"],
            "quotes_available": payload["quotes_available"],
            "quote_diagnostics": payload["quote_diagnostics"],
        })
    except Exception:
        pass
    return payload


@app.get("/live-diagnostics")
def live_diagnostics(symbols: str = "NVDA,AAPL,MSFT", allow_fallback: bool = True):
    clean = [s.strip().upper() for s in str(symbols or "").replace(";", ",").split(",") if s.strip()]
    clean = clean[:40]
    bundle = get_live_quotes(clean, prefer_cache=False, allow_fallback=allow_fallback)
    last_refresh = get_json("last_radar_live_refresh", {})
    return {
        "ok": True,
        "requested_symbols": clean,
        "bundle": bundle,
        "last_radar_live_refresh": last_refresh if isinstance(last_refresh, dict) else {},
        "sqlite": sqlite_status(),
        "live_config": {
            "live_quotes_enabled": bool(LIVE_QUOTES_ENABLED),
            "fmp_key_configured": bool(FMP_API_KEY),
            "fmp_websocket_enabled_config": bool(FMP_WEBSOCKET_ENABLED),
            "polygon_key_configured": bool(POLYGON_API_KEY),
        },
    }



# Fix29: server-side live radar worker. Keeps radar state fresh even if the mobile page is backgrounded.
def _live_radar_worker_save_state(**kwargs):
    try:
        LIVE_RADAR_WORKER_STATE.update(kwargs)
        LIVE_RADAR_WORKER_STATE["updated_at"] = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
        set_json("live_radar_worker_status", dict(LIVE_RADAR_WORKER_STATE))
    except Exception:
        pass


def _live_radar_worker_loop():
    if not LIVE_RADAR_WORKER_ENABLED:
        _live_radar_worker_save_state(enabled=False, running=False, last_error="worker_disabled")
        return
    with LIVE_RADAR_WORKER_LOCK:
        if LIVE_RADAR_WORKER_STATE.get("running"):
            return
        LIVE_RADAR_WORKER_STATE["running"] = True
    _live_radar_worker_save_state(enabled=True, running=True, last_error="")
    last_live_ts = 0.0
    last_full_ts = 0.0
    time.sleep(6)
    while True:
        try:
            phase = get_market_phase()
            active = _is_active_market_phase(phase)
            LIVE_RADAR_WORKER_STATE["iterations"] = int(LIVE_RADAR_WORKER_STATE.get("iterations", 0) or 0) + 1
            LIVE_RADAR_WORKER_STATE["market_phase"] = phase
            LIVE_RADAR_WORKER_STATE["active_price_window"] = active
            if not active:
                _live_radar_worker_save_state(last_error="", active_price_window=False)
                time.sleep(60)
                continue

            now_ts = time.time()
            snapshot = get_json("last_trade_scan_snapshot", {}) or {}
            age_sec = _parse_scan_snapshot_age_sec(snapshot) if isinstance(snapshot, dict) else 999999.0

            full_scan_interval_sec = _server_full_market_scan_interval_sec(phase)
            LIVE_RADAR_WORKER_STATE["full_scan_sec"] = int(full_scan_interval_sec)
            LIVE_RADAR_WORKER_STATE["dynamic_discovery_enabled"] = bool(dynamic_discovery_enabled())
            if (not snapshot.get("rows")) or age_sec >= full_scan_interval_sec or (now_ts - last_full_ts >= full_scan_interval_sec):
                if LIVE_RADAR_FULL_SCAN_LOCK.acquire(blocking=False):
                    try:
                        # Full-market discovery + deep radar scan in the background.
                        # The UI keeps showing the previous snapshot until the user chooses to view the new one.
                        LIVE_RADAR_WORKER_STATE["full_scan_in_progress"] = True
                        _live_radar_worker_save_state(full_scan_in_progress=True)
                        trade_scan(include_all=False, force=True, prefer_cache=False)
                        last_full_ts = time.time()
                        LIVE_RADAR_WORKER_STATE["full_scan_in_progress"] = False
                        LIVE_RADAR_WORKER_STATE["full_scans"] = int(LIVE_RADAR_WORKER_STATE.get("full_scans", 0) or 0) + 1
                        LIVE_RADAR_WORKER_STATE["last_full_scan_at"] = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
                    finally:
                        LIVE_RADAR_WORKER_STATE["full_scan_in_progress"] = False
                        _live_radar_worker_save_state(full_scan_in_progress=False)
                        LIVE_RADAR_FULL_SCAN_LOCK.release()

            if now_ts - last_live_ts >= LIVE_RADAR_PRICE_REFRESH_SEC:
                payload = radar_live_refresh(limit=80, allow_fallback=True, include_watch=True, prefer_cache=False)
                last_live_ts = time.time()
                LIVE_RADAR_WORKER_STATE["live_refreshes"] = int(LIVE_RADAR_WORKER_STATE.get("live_refreshes", 0) or 0) + 1
                LIVE_RADAR_WORKER_STATE["last_live_refresh_at"] = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(payload, dict):
                    LIVE_RADAR_WORKER_STATE["last_quotes_available"] = payload.get("quotes_available", 0)
                    LIVE_RADAR_WORKER_STATE["last_quote_cache_policy"] = payload.get("quote_cache_policy", "")

            _live_radar_worker_save_state(last_error="", running=True)
            time.sleep(max(5, min(30, LIVE_RADAR_PRICE_REFRESH_SEC)))
        except Exception as exc:
            _live_radar_worker_save_state(last_error=f"{type(exc).__name__}: {str(exc)[:180]}", running=True)
            time.sleep(30)


@app.on_event("startup")
def start_live_radar_worker():
    if not LIVE_RADAR_WORKER_ENABLED:
        return
    try:
        t = threading.Thread(target=_live_radar_worker_loop, daemon=True, name="live-radar-worker")
        t.start()
    except Exception as exc:
        _live_radar_worker_save_state(running=False, last_error=f"startup_error: {str(exc)[:160]}")


@app.get("/live-radar-worker-status")
def live_radar_worker_status():
    status = get_json("live_radar_worker_status", {}) or {}
    out = dict(LIVE_RADAR_WORKER_STATE)
    if isinstance(status, dict):
        out.update(status)
    out["runtime_now"] = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    out["market_phase"] = get_market_phase()
    out["market_phase_label"] = market_phase_label(get_market_phase())
    out["rule"] = "fresh_fmp_prices_during_open_pre_after; no sqlite quote cache while active"
    return {"ok": True, "worker": out}


def _snapshot_updated_at(snapshot: dict) -> str:
    try:
        return str((snapshot or {}).get("updated_at", "") or "").strip()
    except Exception:
        return ""


@app.get("/source-discovery/status")
def source_discovery_status(client_updated_at: str = ""):
    snapshot = get_json("last_trade_scan_snapshot", {}) or {}
    worker_status = get_json("live_radar_worker_status", {}) or {}
    updated_at = _snapshot_updated_at(snapshot) if isinstance(snapshot, dict) else ""
    age_sec = _parse_scan_snapshot_age_sec(snapshot) if isinstance(snapshot, dict) else 999999.0
    phase = get_market_phase()
    interval_sec = _server_full_market_scan_interval_sec(phase)
    dynamic_status = get_last_dynamic_discovery_status()
    new_available = bool(updated_at and str(client_updated_at or "").strip() and updated_at != str(client_updated_at or "").strip())
    next_scan_in = None
    try:
        next_scan_in = max(0, int(interval_sec - age_sec)) if updated_at else 0
    except Exception:
        next_scan_in = None
    return {
        "ok": True,
        "enabled": bool(dynamic_discovery_enabled()),
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "latest_snapshot_at": updated_at,
        "latest_snapshot_age_sec": round(float(age_sec or 0), 1) if updated_at else None,
        "latest_snapshot_count": int((snapshot or {}).get("count", 0) or 0) if isinstance(snapshot, dict) else 0,
        "scan_running": bool((worker_status or {}).get("full_scan_in_progress", False)),
        "worker": worker_status if isinstance(worker_status, dict) else {},
        "interval_sec": int(interval_sec),
        "next_scan_in_sec": next_scan_in,
        "new_snapshot_available": new_available,
        "client_updated_at": str(client_updated_at or ""),
        "dynamic_discovery": dynamic_status if isinstance(dynamic_status, dict) else {},
        "message": "يمسح السيرفر السوق كاملًا في الخلفية ويحفظ آخر قائمة جاهزة؛ الأسعار الحية لا تستخدم كاش أثناء السوق النشط.",
    }

# Fix20: compact Market Mood / Sentiment layer.
# Context-only: does not add points to stock scoring and does not promote/demote opportunities.
MARKET_MOOD_INDEX_SYMBOLS = ["SPY", "QQQ", "DIA", "IWM"]
MARKET_MOOD_SECTOR_SYMBOLS = {
    "XLK": "التكنولوجيا",
    "SMH": "أشباه الموصلات",
    "XLC": "الاتصالات",
    "XLY": "الاستهلاكي الاختياري",
    "XLI": "الصناعة",
    "XLF": "الماليات",
    "XLE": "الطاقة",
    "XLV": "الصحة",
    "XLP": "السلع الأساسية",
    "XLU": "المرافق",
    "XLRE": "العقار",
    "XLB": "المواد",
}


def _safe_pct(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _quote_change_pct(q: dict) -> float:
    return _safe_pct((q or {}).get("change_pct", (q or {}).get("changesPercentage", 0.0)), 0.0)


def _market_mood_label(score: float, avg_index_pct: float) -> str:
    if score >= 72 and avg_index_pct >= 0.25:
        return "إيجابي قوي"
    if score >= 58:
        return "إيجابي بحذر"
    if score <= 35:
        return "سلبي"
    if score <= 45:
        return "حذر"
    return "محايد"


def _market_mood_risk_label(score: float) -> str:
    if score >= 72:
        return "اندفاع مرتفع"
    if score >= 58:
        return "مناسب لكن لا تطارد"
    if score <= 35:
        return "مخاطرة أعلى"
    return "متوازن"


def _build_market_mood_from_quotes(quotes: dict, diagnostics: dict | None = None) -> dict:
    diagnostics = diagnostics or {}
    index_rows = []
    for sym in MARKET_MOOD_INDEX_SYMBOLS:
        q = quotes.get(sym, {}) if isinstance(quotes, dict) else {}
        index_rows.append({
            "symbol": sym,
            "price": _safe_pct(q.get("price", 0.0), 0.0),
            "change_pct": _quote_change_pct(q),
            "source_label": str(q.get("source_label", q.get("source", "")) or ""),
        })

    sector_rows = []
    for sym, label in MARKET_MOOD_SECTOR_SYMBOLS.items():
        q = quotes.get(sym, {}) if isinstance(quotes, dict) else {}
        sector_rows.append({
            "symbol": sym,
            "label": label,
            "price": _safe_pct(q.get("price", 0.0), 0.0),
            "change_pct": _quote_change_pct(q),
            "source_label": str(q.get("source_label", q.get("source", "")) or ""),
        })

    valid_indexes = [x for x in index_rows if x["price"] > 0]
    valid_sectors = [x for x in sector_rows if x["price"] > 0]
    avg_index_pct = sum(x["change_pct"] for x in valid_indexes) / max(1, len(valid_indexes))
    positive_indexes = sum(1 for x in valid_indexes if x["change_pct"] > 0)
    negative_indexes = sum(1 for x in valid_indexes if x["change_pct"] < 0)

    sector_sorted = sorted(valid_sectors, key=lambda x: x["change_pct"], reverse=True)
    hot_sectors = sector_sorted[:4]
    weak_sectors = list(reversed(sector_sorted[-3:])) if sector_sorted else []
    avg_hot_pct = sum(x["change_pct"] for x in hot_sectors) / max(1, len(hot_sectors))
    breadth_pct = (positive_indexes / max(1, len(valid_indexes))) * 100.0

    score = 50.0
    score += max(-18.0, min(18.0, avg_index_pct * 8.0))
    score += (positive_indexes - negative_indexes) * 4.0
    score += max(-8.0, min(10.0, avg_hot_pct * 2.2))
    score = max(0.0, min(100.0, score))

    label = _market_mood_label(score, avg_index_pct)
    risk_label = _market_mood_risk_label(score)
    hot_text = "، ".join([f"{x['label']} {safe_round(x['change_pct'], 2)}%" for x in hot_sectors[:3]]) or "غير متوفر"
    index_text = "، ".join([f"{x['symbol']} {safe_round(x['change_pct'], 2)}%" for x in index_rows if x["price"] > 0]) or "غير متوفر"

    explanation_bits = []
    if valid_indexes:
        if avg_index_pct > 0.35:
            explanation_bits.append("المؤشرات الرئيسية تميل للصعود")
        elif avg_index_pct < -0.35:
            explanation_bits.append("المؤشرات الرئيسية تحت ضغط")
        else:
            explanation_bits.append("المؤشرات الرئيسية متوازنة")
    if hot_sectors:
        explanation_bits.append(f"أقوى القطاعات الآن: {hot_text}")
    if score >= 58:
        explanation_bits.append("يمكن متابعة الفرص القريبة من الدخول دون مطاردة الأسعار البعيدة")
    elif score <= 45:
        explanation_bits.append("الأفضل رفع الحذر وتقليل حجم المخاطرة")
    else:
        explanation_bits.append("الفرز الفني يبقى أهم من المزاج العام")

    return {
        "ok": True,
        "mode": "context_only_no_news_points",
        "score": safe_round(score, 1),
        "label": label,
        "risk_label": risk_label,
        "market_phase": get_market_phase(),
        "market_phase_label": market_phase_label(get_market_phase()),
        "avg_index_change_pct": safe_round(avg_index_pct, 2),
        "index_breadth_pct": safe_round(breadth_pct, 1),
        "indexes": index_rows,
        "hot_sectors": hot_sectors,
        "weak_sectors": weak_sectors,
        "summary_ar": " | ".join(explanation_bits[:3]),
        "index_summary_ar": index_text,
        "hot_sectors_summary_ar": hot_text,
        "source": diagnostics.get("source", diagnostics.get("sources", "FMP/Polygon live quotes")) if isinstance(diagnostics, dict) else "FMP/Polygon live quotes",
        "updated_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "note": "مزاج السوق طبقة سياقية فقط ولا تدخل في نقاط الأسهم أو ترتيبها.",
    }


@app.get("/market-mood")
def market_mood_endpoint(allow_fallback: bool = True, prefer_cache: bool | None = None):
    symbols = MARKET_MOOD_INDEX_SYMBOLS + list(MARKET_MOOD_SECTOR_SYMBOLS.keys())
    try:
        phase = get_market_phase()
        if prefer_cache is None:
            prefer_cache = _prefer_price_cache_for_phase(phase)
        bundle = get_live_quotes(symbols, prefer_cache=bool(prefer_cache), allow_fallback=allow_fallback)
        quotes = bundle.get("quotes", {}) if isinstance(bundle, dict) else {}
        diagnostics = bundle.get("diagnostics", {}) if isinstance(bundle, dict) else {}
        payload = _build_market_mood_from_quotes(quotes, diagnostics)
        payload["quote_diagnostics"] = diagnostics
        payload["quote_cache_policy"] = "cache_ok_closed_market" if bool(prefer_cache) else "fresh_fmp_during_active_market"
        set_json("last_market_mood", payload)
        return payload
    except Exception as exc:
        cached = get_json("last_market_mood", {})
        if isinstance(cached, dict) and cached.get("ok"):
            cached["stale"] = True
            cached["error"] = str(exc)[:160]
            return cached
        return {"ok": False, "error": str(exc)[:180], "note": "تعذر بناء مزاج السوق حاليًا."}



# Fix21: Analyst Snapshot layer.
# Context-only support for decision quality. It does not alter scoring, ranking, or news logic.
ANALYST_SNAPSHOT_CACHE_TTL_SEC = int(float(os.getenv("ANALYST_SNAPSHOT_CACHE_TTL_SEC", "21600") or 21600))


def _analyst_cache_key(symbol: str) -> str:
    return f"analyst_snapshot::{normalize_symbol_text(symbol)}"


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "results", "historical", "items"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        return [data]
    return []


def _first_num_from_dict(row: dict, keys: list[str]) -> float:
    for key in keys:
        try:
            val = row.get(key)
            if val is None or val == "":
                continue
            num = float(str(val).replace("%", "").replace(",", "").strip())
            if num != 0:
                return safe_round(num, 4)
        except Exception:
            continue
    return 0.0


def _first_text_from_dict(row: dict, keys: list[str]) -> str:
    for key in keys:
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _fetch_fmp_json_candidates(urls: list[str], timeout: float = 8.0) -> tuple[list, str, dict]:
    last_error = ""
    for url in urls:
        try:
            resp = HTTP_SESSION.get(url, timeout=timeout)
            meta = {"url_suffix": url.split("financialmodelingprep.com")[-1][:120], "status_code": resp.status_code}
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                continue
            data = resp.json()
            rows = _as_list(data)
            if rows:
                return rows, "FMP", meta
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
    return [], "", {"error": last_error}


def _build_analyst_snapshot(symbol: str, force_refresh: bool = False) -> dict:
    symbol = normalize_symbol_text(symbol)
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}

    cache_key = _analyst_cache_key(symbol)
    cached = get_json(cache_key, {})
    now_ts = time.time()
    if (not force_refresh) and isinstance(cached, dict) and cached.get("ok"):
        try:
            cached_at = float(cached.get("cached_at_ts", 0) or 0)
            if cached_at and now_ts - cached_at <= ANALYST_SNAPSHOT_CACHE_TTL_SEC:
                cached["cache_used"] = True
                return cached
        except Exception:
            pass

    if not FMP_API_KEY:
        return {
            "ok": True,
            "available": False,
            "symbol": symbol,
            "source": "none",
            "summary_ar": "لم يتم ضبط مفتاح FMP، لذلك لا تتوفر آراء المحللين.",
            "cache_used": False,
        }

    base = str(os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com") or "https://financialmodelingprep.com").rstrip("/")
    price_target_urls = [
        f"{base}/stable/price-target-consensus?symbol={symbol}&apikey={FMP_API_KEY}",
        f"{base}/stable/price-target-summary?symbol={symbol}&apikey={FMP_API_KEY}",
        f"{base}/api/v4/price-target-consensus?symbol={symbol}&apikey={FMP_API_KEY}",
        f"{base}/api/v4/price-target-summary?symbol={symbol}&apikey={FMP_API_KEY}",
        f"{base}/api/v3/price-target/{symbol}?apikey={FMP_API_KEY}",
    ]
    recommendation_urls = [
        f"{base}/stable/analyst-stock-recommendations?symbol={symbol}&apikey={FMP_API_KEY}",
        f"{base}/api/v3/analyst-stock-recommendations/{symbol}?apikey={FMP_API_KEY}",
        f"{base}/stable/ratings-snapshot?symbol={symbol}&apikey={FMP_API_KEY}",
        f"{base}/api/v3/rating/{symbol}?apikey={FMP_API_KEY}",
    ]
    upgrade_urls = [
        f"{base}/stable/upgrades-downgrades?symbol={symbol}&apikey={FMP_API_KEY}",
        f"{base}/api/v4/upgrades-downgrades?symbol={symbol}&apikey={FMP_API_KEY}",
    ]
    estimates_urls = [
        f"{base}/stable/analyst-estimates?symbol={symbol}&period=annual&apikey={FMP_API_KEY}",
        f"{base}/api/v3/analyst-estimates/{symbol}?period=annual&apikey={FMP_API_KEY}",
    ]

    target_rows, target_source, target_meta = _fetch_fmp_json_candidates(price_target_urls)
    rec_rows, rec_source, rec_meta = _fetch_fmp_json_candidates(recommendation_urls)
    upgrade_rows, upgrade_source, upgrade_meta = _fetch_fmp_json_candidates(upgrade_urls)
    estimates_rows, estimates_source, estimates_meta = _fetch_fmp_json_candidates(estimates_urls)

    current_price = 0.0
    try:
        qb = get_live_quotes([symbol], prefer_cache=_prefer_price_cache_for_phase(), allow_fallback=True)
        current_price = safe_round(((qb.get("quotes", {}) or {}).get(symbol, {}) or {}).get("price", 0), 4)
    except Exception:
        current_price = 0.0

    latest_target = target_rows[0] if target_rows and isinstance(target_rows[0], dict) else {}
    target_consensus = _first_num_from_dict(latest_target, [
        "targetConsensus", "target_consensus", "priceTargetAverage", "priceTargetAvg", "targetMean",
        "targetMedian", "priceTarget", "targetPrice", "average", "target"
    ])
    target_high = _first_num_from_dict(latest_target, ["targetHigh", "priceTargetHigh", "high", "maxTarget"])
    target_low = _first_num_from_dict(latest_target, ["targetLow", "priceTargetLow", "low", "minTarget"])
    target_date = _first_text_from_dict(latest_target, ["date", "publishedDate", "updatedAt", "lastUpdated", "calendarDate"])
    upside_pct = safe_round(((target_consensus - current_price) / current_price) * 100, 2) if target_consensus and current_price else 0.0

    latest_rec = rec_rows[0] if rec_rows and isinstance(rec_rows[0], dict) else {}
    rec_date = _first_text_from_dict(latest_rec, ["date", "period", "updatedAt", "calendarDate"])
    buy_count = int(_first_num_from_dict(latest_rec, [
        "analystRatingsbuy", "analystRatingsBuy", "buy", "strongBuy", "numberOfBuyRatings", "buyCount"
    ]) or 0)
    hold_count = int(_first_num_from_dict(latest_rec, [
        "analystRatingsHold", "analystRatingshold", "hold", "numberOfHoldRatings", "holdCount"
    ]) or 0)
    sell_count = int(_first_num_from_dict(latest_rec, [
        "analystRatingsSell", "analystRatingssell", "sell", "strongSell", "numberOfSellRatings", "sellCount"
    ]) or 0)
    total_count = buy_count + hold_count + sell_count
    rating_text = _first_text_from_dict(latest_rec, ["rating", "recommendation", "ratingRecommendation", "scoreRecommendation"])
    if not rating_text:
        if total_count:
            buy_ratio = buy_count / total_count
            if buy_ratio >= 0.65:
                rating_text = "شراء قوي"
            elif buy_ratio >= 0.45:
                rating_text = "شراء/احتفاظ"
            elif sell_count > buy_count:
                rating_text = "حذر/تخفيض"
            else:
                rating_text = "محايد"
        else:
            rating_text = "غير متوفر"

    upgrade_count = 0
    downgrade_count = 0
    neutral_count = 0
    latest_action_date = ""
    for row in upgrade_rows[:12] if isinstance(upgrade_rows, list) else []:
        if not isinstance(row, dict):
            continue
        action = " ".join([
            str(row.get("action") or ""),
            str(row.get("newGrade") or ""),
            str(row.get("previousGrade") or ""),
            str(row.get("grade") or ""),
        ]).lower()
        latest_action_date = latest_action_date or _first_text_from_dict(row, ["publishedDate", "date", "updatedAt"])
        if any(x in action for x in ["upgrade", "outperform", "buy", "overweight", "رفع"]):
            upgrade_count += 1
        elif any(x in action for x in ["downgrade", "underperform", "sell", "underweight", "خفض"]):
            downgrade_count += 1
        else:
            neutral_count += 1

    latest_est = estimates_rows[0] if estimates_rows and isinstance(estimates_rows[0], dict) else {}
    est_date = _first_text_from_dict(latest_est, ["date", "period", "calendarDate"])
    revenue_est = _first_num_from_dict(latest_est, ["estimatedRevenueAvg", "revenueAvg", "estimatedRevenue", "revenue"])
    eps_est = _first_num_from_dict(latest_est, ["estimatedEpsAvg", "epsAvg", "estimatedEps", "eps"])

    available = bool(target_rows or rec_rows or upgrade_rows or estimates_rows)
    if available:
        upside_phrase = ""
        if target_consensus and current_price:
            if upside_pct >= 15:
                upside_phrase = f"السعر المستهدف يشير إلى إمكانية ارتفاع تقارب {upside_pct}%."
            elif upside_pct >= 3:
                upside_phrase = f"السعر المستهدف أعلى من السعر الحالي بنحو {upside_pct}%."
            elif upside_pct <= -8:
                upside_phrase = f"السعر المستهدف أقل من السعر الحالي بنحو {abs(upside_pct)}%، وهذا يحتاج حذرًا."
            else:
                upside_phrase = "السعر المستهدف قريب من السعر الحالي."
        action_phrase = ""
        if upgrade_count or downgrade_count:
            action_phrase = f"آخر التحديثات: {upgrade_count} ترقيات و {downgrade_count} تخفيضات."
        elif total_count:
            action_phrase = f"التوصية الإجمالية مبنية على {total_count} تصنيفًا متاحًا."
        summary_ar = " ".join([x for x in [
            f"رأي المحللين: {rating_text}.",
            upside_phrase,
            action_phrase,
            "هذه طبقة مساعدة فقط ولا تغيّر نقاط الرادار أو قرار الدخول."
        ] if x]).strip()
    else:
        summary_ar = "لا تتوفر آراء محللين كافية لهذا السهم من FMP حاليًا. لا يؤثر ذلك على نقاط الرادار."

    payload = {
        "ok": True,
        "available": available,
        "symbol": symbol,
        "source": "FMP REST" if available else "none",
        "cache_used": False,
        "cached_at_ts": now_ts,
        "fetched_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "current_price": current_price,
        "consensus": {
            "rating": rating_text,
            "analyst_count": total_count,
            "buy": buy_count,
            "hold": hold_count,
            "sell": sell_count,
            "date": rec_date,
        },
        "price_target": {
            "consensus": target_consensus,
            "high": target_high,
            "low": target_low,
            "upside_pct": upside_pct,
            "date": target_date,
        },
        "updates": {
            "upgrades": upgrade_count,
            "downgrades": downgrade_count,
            "neutral": neutral_count,
            "latest_date": latest_action_date,
        },
        "estimates": {
            "date": est_date,
            "revenue_avg": revenue_est,
            "eps_avg": eps_est,
        },
        "summary_ar": summary_ar,
        "diagnostics": {
            "target_rows": len(target_rows or []),
            "recommendation_rows": len(rec_rows or []),
            "upgrade_rows": len(upgrade_rows or []),
            "estimate_rows": len(estimates_rows or []),
            "target_meta": target_meta,
            "recommendation_meta": rec_meta,
            "upgrade_meta": upgrade_meta,
            "estimates_meta": estimates_meta,
        },
        "scoring_note": "context_only_no_points",
    }
    try:
        set_json(cache_key, payload)
    except Exception:
        pass
    return payload


@app.get("/analyst-snapshot")
def analyst_snapshot_endpoint(symbol: str, force_refresh: bool = False):
    try:
        return _build_analyst_snapshot(symbol, force_refresh=force_refresh)
    except Exception as exc:
        return {
            "ok": False,
            "symbol": normalize_symbol_text(symbol),
            "error": str(exc)[:180],
            "summary_ar": "تعذر تحميل آراء المحللين مؤقتًا. لا يؤثر ذلك على ترتيب الرادار.",
            "scoring_note": "context_only_no_points",
        }


def _stock_score_value(x: dict) -> float:
    try:
        return float(x.get("display_rank_score", 0) or 0) or (
            float(x.get("quality_score", 0) or 0)
            + float(x.get("execution_readiness_score", 0) or 0) * 0.45
            + float(x.get("signal_strength_score", 0) or 0) * 0.12
            + float(x.get("continuation_score", 0) or 0) * 0.10
        )
    except Exception:
        return 0.0


def _is_blocked_sharia(stock: dict) -> bool:
    status = str(stock.get("sharia_status", "") or "").lower()
    decision = str(stock.get("decision", "") or "")
    return bool(stock.get("sharia_manual_excluded")) or status in {"non_compliant", "manual_excluded"} or decision in {"مرفوض شرعياً", "مستبعد يدويًا"}


def _is_gray_sharia(stock: dict) -> bool:
    status = str(stock.get("sharia_status", "") or "").lower()
    return bool(stock.get("sharia_is_gray")) or status == "gray"


def _apply_manual_sharia_overrides_to_stock(stock: dict, manual_exclusions: dict | None = None, manual_approvals: dict | None = None) -> dict:
    """Apply the user's latest manual Sharia decisions to cached/live rows.

    Important: the radar live refresh is mostly an overlay over the latest saved
    scan snapshot. If the user manually approves/excludes a ticker after that
    snapshot was created, the old row can still carry `sharia_status=gray` or
    appear in the wrong bucket until the next full rescan. This function makes
    manual decisions authoritative at display/bucketing time without changing
    the underlying technical logic.
    """
    out = dict(stock or {})
    symbol = normalize_symbol_text(out.get("symbol", ""))
    if not symbol:
        return out

    manual_exclusions = manual_exclusions or {}
    manual_approvals = manual_approvals or {}

    exclusion = manual_exclusions.get(symbol)
    if exclusion:
        note = str((exclusion or {}).get("note", "") or (exclusion or {}).get("reason", "") or "").strip()
        reason = "مستبعد يدويًا من قائمتك الشرعية"
        if note:
            reason = f"{reason} - {note}"
        out.update({
            "sharia_status": "manual_excluded",
            "sharia_label": "مستبعد يدويًا",
            "sharia_reason": reason,
            "sharia_manual_excluded": True,
            "sharia_manual_approved": False,
            "sharia_is_gray": False,
            "is_halal": False,
            "halal_ok": False,
            "owner_action": "↩️ يمكنك إعادة السهم يدويًا إذا رغبت",
        })
        # Keep the technical fields intact, but make sure bucket filtering removes it.
        if str(out.get("decision", "") or "") not in {"مستبعد يدويًا", "مرفوض شرعياً"}:
            out.setdefault("original_decision", out.get("decision", ""))
        out["decision"] = "مستبعد يدويًا"
        return out

    approval = manual_approvals.get(symbol)
    if approval:
        # Manual approval is intended for gray/unresolved names. Do not override
        # clearly non-compliant business/debt blocks unless the existing row is gray
        # or came from the gray bucket.
        status = str(out.get("sharia_status", "") or "").lower()
        decision = str(out.get("decision", "") or "")
        was_gray_like = bool(out.get("sharia_is_gray")) or status in {"gray", "manual_approved"} or "غير محسوم" in decision or "رمادي" in decision
        if was_gray_like:
            note = str((approval or {}).get("note", "") or (approval or {}).get("reason", "") or "").strip()
            reason = "متوافق يدويًا بعد مراجعتك"
            if note:
                reason = f"{reason} - {note}"
            out.update({
                "sharia_status": "manual_approved",
                "sharia_label": "متوافق يدويًا",
                "sharia_reason": reason,
                "sharia_manual_excluded": False,
                "sharia_manual_approved": True,
                "manual_approved": True,
                "sharia_is_gray": False,
                "is_halal": True,
                "halal_ok": True,
            })
            # If a copied gray bucket label is still present, restore the technical
            # decision so it can move to the correct clean bucket immediately.
            original = str(out.get("original_decision", "") or "").strip()
            if "غير محسوم" in decision and original:
                out["decision"] = original
            elif decision in {"قوي لكن شرعيته غير محسومة", "تهيئة قوية غير محسومة شرعيًا"}:
                out["decision"] = original or "مراقبة"
        return out

    return out


def _apply_manual_sharia_overrides(rows: list[dict]) -> list[dict]:
    try:
        manual_exclusions = get_manual_sharia_exclusions_map()
    except Exception:
        manual_exclusions = {}
    try:
        manual_approvals = get_manual_sharia_approvals_map()
    except Exception:
        manual_approvals = {}
    return [_apply_manual_sharia_overrides_to_stock(row, manual_exclusions, manual_approvals) for row in (rows or [])]


def _copy_for_bucket(stock: dict, label: str, reason: str = "") -> dict:
    out = dict(stock or {})
    out.setdefault("original_decision", stock.get("decision", "") if isinstance(stock, dict) else "")
    out["decision"] = label
    if reason:
        out["special_bucket_reason"] = reason
        summary = str(out.get("ai_summary", "") or "")
        if reason not in summary:
            out["ai_summary"] = (reason + " - " + summary).strip(" -")
    return out


def _build_special_buckets(results: list[dict], market_phase: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Create Fix14c UX buckets without weakening the clean strong-entry list.

    - Gray strong candidates: technically strong, but Sharia remains unresolved.
    - Premarket setup: good setups before open that should not be called strong entry yet.
    - Watch: original watch list minus items promoted to the special buckets.
    """
    gray_bucket = []
    premarket_bucket = []
    used = set()
    phase = str(market_phase or "")
    premarket_like = phase in {"pre_market", "closed", "after_hours"}

    for stock in results or []:
        symbol = normalize_symbol_text(stock.get("symbol", ""))
        if not symbol or _is_blocked_sharia(stock):
            continue
        quality = float(stock.get("quality_score", 0) or 0)
        strength = float(stock.get("signal_strength_score", 0) or 0)
        readiness = float(stock.get("execution_readiness_score", 0) or 0)
        rr = float(stock.get("rr_1", 0) or 0)
        breakout = str(stock.get("breakout_quality", "") or "").upper()
        decision = str(stock.get("decision", "") or "")
        technical_strong = (
            quality >= 78
            and strength >= 82
            and rr >= 1.05
            and (breakout in {"STRONG", "WEAK", "N/A"} or readiness >= 58)
        )
        if _is_gray_sharia(stock) and technical_strong:
            if decision == "دخول قوي":
                gray_label = "دخول قوي غير محسوم شرعيًا"
            elif decision == "دخول بحذر":
                gray_label = "دخول بحذر غير محسوم شرعيًا"
            elif premarket_like:
                gray_label = "تهيئة قوية غير محسومة شرعيًا"
            else:
                gray_label = "قوي لكن شرعيته غير محسومة"
            gray_bucket.append(_copy_for_bucket(
                stock,
                gray_label,
                "التحليل الفني لم يُخفض بسبب الشرعية الرمادية؛ تم فصله فقط لأن الحكم الشرعي غير محسوم.",
            ))
            used.add(symbol)
            continue
        if premarket_like and decision != "دخول قوي" and not _is_gray_sharia(stock):
            premarket_ready = (
                quality >= 72
                and strength >= 72
                and readiness >= 50
                and rr >= 0.85
                and str(stock.get("trend", "") or "") in {"صاعد", "صاعد قوي"}
            )
            if premarket_ready:
                premarket_bucket.append(_copy_for_bucket(
                    stock,
                    "تهيئة قوية قبل الافتتاح",
                    "تهيئة قوية قبل الافتتاح: ليست دخولًا قويًا بعد، وتحتاج تأكيد السعر والحجم بعد الافتتاح.",
                ))
                used.add(symbol)

    gray_bucket = sort_display_bucket(gray_bucket)[:18]
    premarket_bucket = sort_display_bucket(premarket_bucket)[:18]
    used = {normalize_symbol_text(x.get("symbol", "")) for x in (gray_bucket + premarket_bucket)}
    watch = [x for x in results or [] if str(x.get("decision", "")) == "مراقبة" and normalize_symbol_text(x.get("symbol", "")) not in used and not _is_blocked_sharia(x)]
    return gray_bucket, premarket_bucket, sort_display_bucket(watch)




def _parse_scan_snapshot_age_sec(snapshot: dict) -> float:
    try:
        updated = str((snapshot or {}).get("updated_at", "") or "").strip()
        if not updated:
            return 999999.0
        try:
            dt = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 999999.0
        now_naive = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
        return max(0.0, (now_naive - dt).total_seconds())
    except Exception:
        return 999999.0


def _trade_scan_cache_ttl_sec(phase: str, prefer_cache: bool = False) -> int:
    try:
        if dynamic_discovery_enabled() and phase in {"open", "pre_market", "after_hours"}:
            # With server-side full-market discovery, page loads should show the last ready
            # snapshot immediately and let the background worker prepare the next one.
            return int(_server_full_market_scan_interval_sec(phase) + 120)
        if prefer_cache:
            if phase in {"open", "pre_market", "after_hours"}:
                return int(os.getenv("RADAR_FAST_CACHE_TTL_OPEN_SEC", "240") or 240)
            return int(os.getenv("RADAR_FAST_CACHE_TTL_CLOSED_SEC", "1800") or 1800)
        if phase in {"open", "pre_market", "after_hours"}:
            return int(os.getenv("RADAR_SCAN_CACHE_TTL_OPEN_SEC", "300") or 300)
        return int(os.getenv("RADAR_SCAN_CACHE_TTL_CLOSED_SEC", "900") or 900)
    except Exception:
        return 300 if phase in {"open", "pre_market", "after_hours"} else 900


def _build_trade_scan_response(results, scan_debug, include_all: bool = False, cache_hit: bool = False, cache_age_sec=None, payload_note: str = ""):
    results = _apply_manual_sharia_overrides(list(results or []))
    scan_debug = dict(scan_debug or {})
    phase = get_market_phase()
    strong = sort_display_bucket([x for x in results if x.get("decision") == "دخول قوي" and not _is_blocked_sharia(x) and not _is_gray_sharia(x)])
    gray_strong, premarket_setups, watch = _build_special_buckets(results, phase)
    special_symbols = {normalize_symbol_text(x.get("symbol", "")) for x in (gray_strong + premarket_setups)}
    cautious = sort_display_bucket([
        x for x in results
        if x.get("decision") == "دخول بحذر"
        and normalize_symbol_text(x.get("symbol", "")) not in special_symbols
        and not _is_blocked_sharia(x)
        and not _is_gray_sharia(x)
    ])

    out = {
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "updated_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_updated_at": scan_debug.get("updated_at", "") or scan_debug.get("scan_updated_at", ""),
        "cache_hit": bool(cache_hit),
        "cache_age_sec": round(float(cache_age_sec or 0), 1) if cache_age_sec is not None else None,
        "payload_note": payload_note,
        "universe_count": int(scan_debug.get("after_manual_exclusion", scan_debug.get("raw_count", 150)) or 150),
        "source_target": int(scan_debug.get("source_target", scan_debug.get("raw_count", 150)) or 150),
        "source_active_count": int(scan_debug.get("source_active_count", scan_debug.get("raw_count", 150)) or 150),
        "source_engine_pool": int(scan_debug.get("source_engine_pool", scan_debug.get("raw_count", 150)) or 150),
        "source_engine_version": scan_debug.get("source_engine_version", ""),
        "source_mode": scan_debug.get("source_mode", ""),
        "source_market_mode": scan_debug.get("source_market_mode", ""),
        "manual_priority_count": int(scan_debug.get("manual_priority_count", 0) or 0),
        "sharia_source_filter_version": scan_debug.get("sharia_source_filter_version", ""),
        "sharia_prefilter_candidates": int(scan_debug.get("sharia_prefilter_candidates", 0) or 0),
        "sharia_prefilter_blocked": int(scan_debug.get("sharia_prefilter_blocked", 0) or 0),
        "sharia_prefilter_gray_used": int(scan_debug.get("sharia_prefilter_gray_used", 0) or 0),
        "sharia_prefilter_gray_total": int(scan_debug.get("sharia_prefilter_gray_total", 0) or 0),
        "sharia_prefilter_refill_count": int(scan_debug.get("sharia_prefilter_refill_count", 0) or 0),
        "sharia_refill_reserve_size": int(scan_debug.get("sharia_refill_reserve_size", 0) or 0),
        "sharia_prefilter_clean_total": int(scan_debug.get("sharia_prefilter_clean_total", 0) or 0),
        "sharia_prefilter_clean_used": int(scan_debug.get("sharia_prefilter_clean_used", 0) or 0),
        "sharia_prefilter_gray_cap": int(scan_debug.get("sharia_prefilter_gray_cap", 0) or 0),
        "sharia_prefilter_clean_shortage": int(scan_debug.get("sharia_prefilter_clean_shortage", 0) or 0),
        "sharia_prefilter_final_shortage": int(scan_debug.get("sharia_prefilter_final_shortage", 0) or 0),
        "count": len(results),
        "strong_entries_count": len(strong),
        "cautious_entries_count": len(cautious),
        "gray_strong_count": len(gray_strong),
        "premarket_setups_count": len(premarket_setups),
        "watchlist_count": len(watch),
        "manual_sharia_exclusions_count": len(load_manual_sharia_exclusions()),
        "manual_sharia_approvals_count": len(load_manual_sharia_approvals()),
        "strong_entries": strong[:25],
        "top_ranked": strong[:25],
        "cautious_entries": cautious[:25],
        "gray_strong": gray_strong[:25],
        "premarket_setups": premarket_setups[:25],
        "watchlist": watch[:50],
        "opening_mode_active": is_opening_window(),
        "opening_focus": build_opening_focus(results),
        "all_results": results if include_all else [],
        "all_results_omitted": 0 if include_all else max(0, len(results)),
        "payload_mode": "full_with_all_results" if include_all else "compact_actionable_sections",
        "scan_elapsed_sec": scan_debug.get("scan_elapsed_sec", None),
        "scan_max_workers": scan_debug.get("scan_max_workers", None),
        "scan_requested_universe": scan_debug.get("scan_requested_universe", None),
        "dynamic_discovery": {
            "enabled": bool(scan_debug.get("dynamic_discovery_enabled", False)),
            "mode": scan_debug.get("dynamic_discovery_mode", ""),
            "phase_detail": scan_debug.get("dynamic_phase_detail", ""),
            "phase_label": scan_debug.get("dynamic_phase_label", ""),
            "broad_market_count": int(scan_debug.get("dynamic_broad_market_count", 0) or 0),
            "reference_count": int(scan_debug.get("dynamic_reference_count", 0) or 0),
            "candidate_count_before_confirm": int(scan_debug.get("dynamic_candidate_count_before_confirm", 0) or 0),
            "candidate_count_after_confirm": int(scan_debug.get("dynamic_candidate_count_after_confirm", 0) or 0),
            "fmp_confirm_requested": int(scan_debug.get("dynamic_fmp_confirm_requested", 0) or 0),
            "fmp_confirmed": int(scan_debug.get("dynamic_fmp_confirmed", 0) or 0),
            "fmp_extended_confirmed": int(scan_debug.get("dynamic_fmp_extended_confirmed", 0) or 0),
            "fmp_movers_count": int(scan_debug.get("dynamic_fmp_movers_count", 0) or 0),
            "next_scan_interval_sec": int(scan_debug.get("dynamic_next_scan_interval_sec", 0) or 0),
            "source_bucket_counts": scan_debug.get("dynamic_source_bucket_counts", {}),
            "price_under_2_deprioritized": int(scan_debug.get("dynamic_price_under_2_deprioritized", 0) or 0),
            "price_under_2_exception": int(scan_debug.get("dynamic_price_under_2_exception", 0) or 0),
            "price_over_300_deprioritized": int(scan_debug.get("dynamic_price_over_300_deprioritized", 0) or 0),
            "elapsed_sec": scan_debug.get("dynamic_discovery_elapsed_sec", None),
        },
        "full_market_scan_status": {
            "enabled": bool(dynamic_discovery_enabled()),
            "last_scan_at": scan_debug.get("updated_at", "") or scan_debug.get("scan_updated_at", ""),
            "next_scan_interval_sec": _server_full_market_scan_interval_sec(phase),
            "source": "server_background_worker_and_snapshot",
        },
    }

    # Tracking Intelligence V1 is intentionally passive: full-scan snapshots only,
    # SQLite writes only, no extra API calls, and no changes to decision/Sharia/price logic.
    if not cache_hit:
        try:
            tracking_stats = record_tracking_snapshots(
                strong_rows=strong,
                cautious_rows=cautious,
                gray_strong_rows=gray_strong,
                market_phase=phase,
                source="trade_scan_full",
            )
            absence_stats = mark_tracking_absences_from_scan(
                current_signal_ids=tracking_stats.get("signal_ids", []) if isinstance(tracking_stats, dict) else [],
                source="trade_scan_full",
            )
            out["tracking_intelligence"] = {
                "ok": bool((tracking_stats or {}).get("ok", False)) and bool((absence_stats or {}).get("ok", False)),
                "mode": "passive_full_scan_snapshot",
                "recorded": {k: v for k, v in (tracking_stats or {}).items() if k != "signal_ids"},
                "absences": absence_stats,
            }
        except Exception as exc:
            out["tracking_intelligence"] = {
                "ok": False,
                "mode": "passive_full_scan_snapshot",
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            }
    return out


@app.get("/trade-scan")
def trade_scan(include_all: bool = False, force: bool = False, prefer_cache: bool = False):
    """Full radar scan with a safe snapshot cache.

    Fix25 speed: loading the page repeatedly should not rerun the heavy scan every time.
    A recent saved snapshot is returned quickly, then the UI overlays live FMP prices.
    Use force=true for a full fresh scan.
    """
    phase = get_market_phase()
    snapshot = get_json("last_trade_scan_snapshot", {}) or {}
    rows_from_snapshot = snapshot.get("rows") if isinstance(snapshot, dict) else []
    age_sec = _parse_scan_snapshot_age_sec(snapshot) if isinstance(snapshot, dict) else 999999.0
    ttl_sec = _trade_scan_cache_ttl_sec(phase, bool(prefer_cache))

    if (not force) and isinstance(rows_from_snapshot, list) and rows_from_snapshot and age_sec <= ttl_sec:
        diag = dict(snapshot.get("diagnostics") or {})
        diag.setdefault("updated_at", snapshot.get("updated_at", ""))
        return _build_trade_scan_response(
            rows_from_snapshot,
            diag,
            include_all=include_all,
            cache_hit=True,
            cache_age_sec=age_sec,
            payload_note="تم عرض آخر فحص محفوظ بسرعة، ثم تُحدّث الأسعار حيًا عبر FMP.",
        )

    results = scan_all()
    scan_debug = get_last_scan_debug()
    try:
        snapshot_payload = {
            "updated_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(results or []),
            "rows": results[:250],
            "diagnostics": scan_debug,
        }
        set_json("last_trade_scan_snapshot", snapshot_payload)
    except Exception:
        pass

    return _build_trade_scan_response(
        results,
        scan_debug,
        include_all=include_all,
        cache_hit=False,
        cache_age_sec=0,
        payload_note="فحص كامل جديد.",
    )


@app.get("/single-stock")
@app.get("/scan")
@app.get("/api/single-stock")
def single_stock(symbol: str):
    # Fix17a: keep /single-stock as the canonical endpoint,
    # and add /scan as a backwards-compatible alias because some links/buttons
    # and earlier handoff notes used /scan?symbol=NVDA.
    return build_single_stock_response(symbol)


@app.get("/debug-scan")
def debug_scan():
    rows, diag = scan_all(debug=True)
    return {
        "ok": True,
        "diagnostics": diag,
        "count": len(rows),
        "sample_rows": rows[:5],
    }


@app.get("/debug-last-scan")
def debug_last_scan():
    return {"ok": True, "diagnostics": get_last_scan_debug()}



# Fix29: AI-style Arabic context/news summary.
# Uses Claude only if ANTHROPIC_API_KEY + AI_CONTEXT_SUMMARY_ENABLED are configured;
# otherwise returns a deterministic Arabic summary so the UI remains stable.
AI_CONTEXT_SUMMARY_CACHE_TTL_SEC = int(float(os.getenv("AI_CONTEXT_SUMMARY_CACHE_TTL_SEC", "900") or 900))
AI_CONTEXT_SUMMARY_ENABLED = str(os.getenv("AI_CONTEXT_SUMMARY_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}


def _ai_context_cache_key(symbol: str) -> str:
    return f"ai_context_summary::{normalize_symbol_text(symbol)}"


def _find_stock_from_last_snapshot(symbol: str) -> dict:
    symbol = normalize_symbol_text(symbol)
    snap = get_json("last_trade_scan_snapshot", {}) or {}
    rows = snap.get("rows", []) if isinstance(snap, dict) else []
    for row in rows or []:
        if normalize_symbol_text((row or {}).get("symbol", "")) == symbol:
            return dict(row or {})
    return {}


def _rule_based_ai_context(symbol: str, stock: dict, news_bundle: dict, market_mood: dict) -> str:
    title = str((news_bundle or {}).get("news_title", "") or "").strip()
    scope = str((news_bundle or {}).get("news_scope_label", (news_bundle or {}).get("news_scope", "")) or "")
    sentiment = str((news_bundle or {}).get("news_sentiment", "neutral") or "neutral")
    age = str((news_bundle or {}).get("news_age_label", "") or "")
    mood = str((market_mood or {}).get("label", "") or "")
    decision = str((stock or {}).get("decision", "") or "")
    plan = str((stock or {}).get("live_plan_action", "") or (stock or {}).get("execution_status_ar", "") or "")
    parts = []
    if title:
        parts.append(f"الخبر المتاح عن {symbol}: {title}.")
        if scope or age:
            parts.append(f"تصنيفه: {scope or 'معلومة سياقية'}{('، ' + age) if age else ''}.")
        if sentiment in {"positive", "negative", "mixed", "legal"}:
            sentiment_ar = {"positive":"إيجابي", "negative":"سلبي", "mixed":"مختلط", "legal":"قانوني/تنظيمي"}.get(sentiment, sentiment)
            parts.append(f"النبرة: {sentiment_ar}.")
    else:
        parts.append("لا يوجد خبر حديث موثوق ومباشر يمكن تلخيصه لهذا السهم الآن.")
    if mood:
        parts.append(f"مزاج السوق العام: {mood}، وهو عامل سياقي فقط.")
    if decision or plan:
        parts.append(f"حالة الرادار الحالية: {decision or 'غير محددة'}{(' - ' + plan) if plan else ''}.")
    parts.append("هذا الملخص لا يضيف نقاطًا ولا يبدل شروط الدخول؛ القرار يبقى مبنيًا على السعر والخطة والمؤشرات.")
    return " ".join(parts)


def _build_ai_context_summary(symbol: str, force_refresh: bool = False) -> dict:
    symbol = normalize_symbol_text(symbol)
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    cache_key = _ai_context_cache_key(symbol)
    cached = get_json(cache_key, {})
    now_ts = time.time()
    if (not force_refresh) and isinstance(cached, dict) and cached.get("ok"):
        try:
            if now_ts - float(cached.get("cached_at_ts", 0) or 0) <= AI_CONTEXT_SUMMARY_CACHE_TTL_SEC:
                cached["cache_used"] = True
                return cached
        except Exception:
            pass

    stock = _find_stock_from_last_snapshot(symbol)
    info = COMPANIES_DATA.get(symbol, {}) if isinstance(COMPANIES_DATA, dict) else {}
    company = str(stock.get("company") or info.get("companyName") or info.get("name") or symbol)
    sector = str(stock.get("sector") or info.get("sector") or "")
    industry = str(stock.get("industry") or info.get("industry") or "")
    try:
        news_bundle = get_news_bundle(symbol, company, sector, industry)
    except Exception:
        news_bundle = {}
    market_mood = get_json("last_market_mood", {}) or {}
    fallback_summary = _rule_based_ai_context(symbol, stock, news_bundle, market_mood)
    provider = "rule_based"
    summary_ar = fallback_summary

    try:
        from app.settings import ANTHROPIC_API_KEY, AI_NEWS_MODEL
        if AI_CONTEXT_SUMMARY_ENABLED and ANTHROPIC_API_KEY:
            body = {
                "model": AI_NEWS_MODEL,
                "max_tokens": 360,
                "temperature": 0,
                "system": "أنت مساعد عربي مختصر لأداة رادار أسهم. لخّص الخبر ومزاج السوق فقط. لا تقدم توصية شراء أو بيع. لا تضف نقاطًا. أعد نصًا عربيًا قصيرًا فقط.",
                "messages": [{"role": "user", "content": json.dumps({
                    "symbol": symbol,
                    "company": company,
                    "current_decision": stock.get("decision", ""),
                    "live_plan_action": stock.get("live_plan_action", ""),
                    "news": {
                        "title": news_bundle.get("news_title", ""),
                        "scope": news_bundle.get("news_scope_label", news_bundle.get("news_scope", "")),
                        "sentiment": news_bundle.get("news_sentiment", ""),
                        "age": news_bundle.get("news_age_label", ""),
                        "context_note": news_bundle.get("news_context_note", ""),
                    },
                    "market_mood": {
                        "label": market_mood.get("label", ""),
                        "summary": market_mood.get("summary_ar", ""),
                    },
                    "strict": "الأخبار ومزاج السوق معلومات فقط ولا تدخل في نقاط الرادار."
                }, ensure_ascii=False)}],
            }
            resp = HTTP_SESSION.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
                timeout=8,
            )
            if resp.status_code < 300:
                data = resp.json()
                text = " ".join(str(x.get("text", "") or "") for x in (data.get("content", []) or []) if isinstance(x, dict)).strip()
                if text:
                    summary_ar = text[:900]
                    provider = "claude"
    except Exception:
        provider = "rule_based"

    payload = {
        "ok": True,
        "symbol": symbol,
        "provider": provider,
        "cache_used": False,
        "cached_at_ts": now_ts,
        "fetched_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "summary_ar": summary_ar,
        "news": news_bundle,
        "market_mood_label": market_mood.get("label", ""),
        "scoring_note": "context_only_no_points",
    }
    try:
        set_json(cache_key, payload)
    except Exception:
        pass
    return payload


@app.get("/ai-context-summary")
def ai_context_summary_endpoint(symbol: str, force_refresh: bool = False):
    try:
        return _build_ai_context_summary(symbol, force_refresh=force_refresh)
    except Exception as exc:
        return {"ok": False, "symbol": normalize_symbol_text(symbol), "error": str(exc)[:180], "summary_ar": "تعذر توليد الملخص السياقي مؤقتًا."}

@app.get("/debug-news/{symbol}")
def debug_news_symbol(symbol: str):
    sym = normalize_symbol_text(symbol)
    info = get_info(sym) if sym else {"company": "", "sector": "", "industry": ""}
    bundle = get_news_bundle(sym, info.get("company", ""), info.get("sector", ""), info.get("industry", ""))
    return {
        "ok": True,
        "symbol": sym,
        "company": info.get("company", ""),
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "bundle": bundle,
        "diagnostics": get_news_diagnostics(sym),
    }

@app.get("/debug-news")
def debug_news_all():
    return {"ok": True, "diagnostics": get_news_diagnostics()}


@app.get("/debug-ai-news")
def debug_ai_news():
    return get_ai_news_status()



@app.post("/portfolio/add")
def portfolio_add(payload: dict = Body(...)):
    symbol = str(payload.get("symbol", "") or "").upper().strip()
    buy_price = safe_round(payload.get("buy_price", 0))
    quantity = safe_round(payload.get("quantity", 0))
    target_price = safe_round(payload.get("target_price", payload.get("target", 0)))
    stop_loss = safe_round(payload.get("stop_loss", payload.get("stop", 0)))
    source_signal_type = str(payload.get("source_signal_type", payload.get("signal_type", "")) or "").strip()
    note = str(payload.get("note", "") or "").strip()
    if not symbol or buy_price <= 0:
        return {"ok": False, "message": "الرمز وسعر الشراء مطلوبان"}

    items = load_portfolio_items()
    existing = None
    for item in items:
        if str(item.get("symbol", "")).upper().strip() == symbol:
            existing = item
            break

    now_text = ny_now().strftime("%Y-%m-%d %H:%M:%S")
    payload_fields = {
        "symbol": symbol,
        "buy_price": buy_price,
        "quantity": quantity,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "source_signal_type": source_signal_type,
        "note": note,
        "updated_at": now_text,
    }
    # Preserve older values when the caller does not provide optional fields.
    if existing:
        existing["buy_price"] = buy_price
        existing["quantity"] = quantity if quantity > 0 else existing.get("quantity", 0)
        if target_price > 0:
            existing["target_price"] = target_price
        if stop_loss > 0:
            existing["stop_loss"] = stop_loss
        if source_signal_type:
            existing["source_signal_type"] = source_signal_type
        if note:
            existing["note"] = note
        existing["updated_at"] = now_text
    else:
        payload_fields["added_at"] = now_text
        items.insert(0, payload_fields)
    save_portfolio_items(items)
    return {"ok": True, "message": "تم حفظ السهم في المحفظة", "count": len(items), "symbol": symbol, "items": items}


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
            "saved_target_price": safe_round(item.get("target_price", 0)),
            "saved_stop_loss": safe_round(item.get("stop_loss", 0)),
            "source_signal_type": str(item.get("source_signal_type", "") or ""),
            "note": str(item.get("note", "") or ""),
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
    note = str(payload.get("note", "") or payload.get("reason", "") or "استبعاد يدوي شرعي").strip()
    items = load_manual_sharia_exclusions()
    now_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    found = False
    for item in items:
        if normalize_symbol_text(item.get("symbol", "")) == symbol:
            item["note"] = note
            item["reason"] = note
            item["updated_at"] = now_str
            if not item.get("excluded_at"):
                item["excluded_at"] = now_str
            item["source"] = "manual"
            found = True
            break
    if not found:
        items.insert(0, {"symbol": symbol, "note": note, "reason": note, "excluded_at": now_str, "updated_at": now_str, "source": "manual"})
    save_manual_sharia_exclusions(items)
    return {"ok": True, "count": len(items), "symbol": symbol, "saved": True}


@app.post("/sharia-exclusions/remove")
def sharia_exclusions_remove(payload: dict = Body(...)):
    symbol = normalize_symbol_text(payload.get("symbol", ""))
    items = [item for item in load_manual_sharia_exclusions() if normalize_symbol_text(item.get("symbol", "")) != symbol]
    save_manual_sharia_exclusions(items)
    return {"ok": True, "count": len(items), "symbol": symbol}




@app.post("/sharia-approvals/add")
def sharia_approvals_add(payload: dict = Body(...)):
    symbol = normalize_symbol_text(payload.get("symbol", ""))
    if not symbol:
        return JSONResponse({"ok": False, "error": "missing_symbol"}, status_code=400)
    note = str(payload.get("note", "") or payload.get("reason", "") or "اعتماد يدوي بعد مراجعة الشرعية").strip()
    # If user approves a gray symbol, remove it from manual exclusions if present.
    exclusions = [item for item in load_manual_sharia_exclusions() if normalize_symbol_text(item.get("symbol", "")) != symbol]
    save_manual_sharia_exclusions(exclusions)

    items = load_manual_sharia_approvals()
    now_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    found = False
    for item in items:
        if normalize_symbol_text(item.get("symbol", "")) == symbol:
            item["note"] = note
            item["reason"] = note
            item["updated_at"] = now_str
            if not item.get("approved_at"):
                item["approved_at"] = now_str
            item["source"] = "manual"
            found = True
            break
    if not found:
        items.insert(0, {"symbol": symbol, "note": note, "reason": note, "approved_at": now_str, "updated_at": now_str, "source": "manual"})
    save_manual_sharia_approvals(items)
    return {"ok": True, "count": len(items), "symbol": symbol, "saved": True}


@app.post("/sharia-approvals/remove")
def sharia_approvals_remove(payload: dict = Body(...)):
    symbol = normalize_symbol_text(payload.get("symbol", ""))
    items = [item for item in load_manual_sharia_approvals() if normalize_symbol_text(item.get("symbol", "")) != symbol]
    save_manual_sharia_approvals(items)
    return {"ok": True, "count": len(items), "symbol": symbol}


@app.get("/sharia-approvals")
def sharia_approvals_get():
    items = load_manual_sharia_approvals()
    return {"items": items, "count": len(items)}


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


@app.get("/data-sync/status")
def data_sync_status():
    status = github_sync_status()
    status["manual_sharia_local_count"] = len(load_manual_sharia_exclusions())
    status["manual_sharia_approvals_local_count"] = len(load_manual_sharia_approvals())
    status["manual_sharia_last_pull"] = get_manual_sharia_sync_diagnostics()
    status["manual_sharia_approvals_last_pull"] = get_manual_sharia_approvals_sync_diagnostics()
    return {"ok": True, **status}


@app.post("/data-sync/manual-sharia")
def data_sync_manual_sharia():
    items = load_manual_sharia_exclusions(force_github_pull=True)
    payload = {normalize_symbol_text(item.get("symbol", "")): item for item in items if normalize_symbol_text(item.get("symbol", ""))}
    result = push_json_file(
        GITHUB_SYNC_MANUAL_SHARIA_PATH,
        payload,
        message=f"Sync manual Sharia exclusions ({len(payload)} symbols)",
    )
    return {"ok": bool(result.get("ok")), "count": len(payload), "result": result}




@app.post("/data-sync/manual-sharia-approvals")
def data_sync_manual_sharia_approvals():
    items = load_manual_sharia_approvals(force_github_pull=True)
    payload = {normalize_symbol_text(item.get("symbol", "")): item for item in items if normalize_symbol_text(item.get("symbol", ""))}
    result = push_json_file(
        GITHUB_SYNC_MANUAL_SHARIA_APPROVALS_PATH,
        payload,
        message=f"Sync manual Sharia approvals ({len(payload)} symbols)",
    )
    return {"ok": bool(result.get("ok")), "count": len(payload), "result": result}


@app.post("/performance/track-signal")
def performance_track_signal(payload: dict = Body(...)):
    # The UI may send either the stock fields directly, or {"stock": {...}}.
    if isinstance(payload.get("stock"), dict):
        payload = payload.get("stock") or {}

    symbol = normalize_symbol_text(payload.get("symbol", ""))
    if not symbol:
        return JSONResponse({"ok": False, "error": "missing_symbol"}, status_code=400)

    signal_type = str(payload.get("signal_type", payload.get("decision", "")) or "").strip()
    if signal_type not in {"دخول قوي", "دخول بحذر"}:
        # Manual tracking is allowed, but keep it explicit and conservative.
        signal_type = "دخول بحذر"

    entry_price = safe_round(payload.get("entry_price", payload.get("display_entry_price", 0)))
    target_price = safe_round(payload.get("target_price", payload.get("display_target_price", 0)))
    target_2_price = safe_round(payload.get("target_2_price", payload.get("target_2", 0)))
    stop_loss = safe_round(payload.get("stop_loss", payload.get("display_stop_price", 0)))
    current_price = safe_round(payload.get("current_price", payload.get("display_price", 0)))
    if entry_price <= 0:
        return JSONResponse({"ok": False, "error": "missing_entry_price", "message": "سعر الدخول مطلوب للتتبع"}, status_code=400)

    before_store = rollover_performance_store_if_needed(load_performance_store())
    before_count = len(before_store.get("active_records", []) or [])
    stock = {
        "symbol": symbol,
        "decision": signal_type,
        "display_entry_price": entry_price,
        "display_target_price": target_price,
        "target_2": target_2_price,
        "display_stop_price": stop_loss,
        "display_price": current_price,
        "current_price_live": current_price,
        "price_source_label": str(payload.get("price_source_label", "manual") or "manual"),
        "price_source": str(payload.get("price_source", "manual") or "manual"),
        "strategy_label": str(payload.get("strategy_label", payload.get("plan_family", "")) or ""),
        "display_plan_family": str(payload.get("plan_family", payload.get("display_plan_family", "")) or ""),
        "market_phase_label": str(payload.get("market_phase_label", "") or ""),
        "valid_for": str(payload.get("valid_for", "") or ""),
    }
    upsert_performance_signal(stock)
    after_store = rollover_performance_store_if_needed(load_performance_store())
    after_count = len(after_store.get("active_records", []) or [])
    return {
        "ok": True,
        "symbol": symbol,
        "signal_type": signal_type,
        "before_count": before_count,
        "after_count": after_count,
        "message": "تم حفظ الإشارة في التتبع الأسبوعي",
    }


@app.get("/user-data-diagnostics")
def user_data_diagnostics():
    portfolio_items = load_portfolio_items()
    watchlist_items = load_manual_watchlist()
    sharia_exclusions = load_manual_sharia_exclusions()
    sharia_approvals = load_manual_sharia_approvals()
    performance_store = rollover_performance_store_if_needed(load_performance_store())
    active_records = performance_store.get("active_records", []) or []
    archive = performance_store.get("weekly_archive", []) or []
    last_live = get_json("last_radar_live_refresh", {}) or {}
    return {
        "ok": True,
        "sqlite": sqlite_status(),
        "portfolio_count": len(portfolio_items or []),
        "watchlist_count": len(watchlist_items or []),
        "manual_sharia_exclusions_count": len(sharia_exclusions or []),
        "manual_sharia_approvals_count": len(sharia_approvals or []),
        "performance_active_count": len(active_records),
        "performance_archive_weeks": len(archive),
        "active_week": {
            "week_key": performance_store.get("active_week_key"),
            "week_start": performance_store.get("active_week_start"),
            "week_end": performance_store.get("active_week_end"),
        },
        "last_radar_live_refresh": last_live,
        "tracking_intelligence": tracking_status(),
        "notes": {
            "news_score_enabled": bool(NEWS_SCORE_ENABLED),
            "market_mood_context_only": True,
            "tracking_intelligence_passive": True,
        },
    }


@app.get("/tracking-intelligence")
@app.get("/tracking-intelligence/weekly")
def tracking_intelligence_weekly(week_key: str = "", include_items: bool = False, format: str = "json"):
    # Computed on demand only. This endpoint does not run in the price/scan
    # decision path and does not fetch market data. Gray strong is technical-only.
    fmt = str(format or "json").strip().lower()
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(
            build_tracking_weekly_brief(week_key=week_key or None, include_items=True),
            media_type="text/plain; charset=utf-8",
        )
    return build_tracking_weekly_report(week_key=week_key or None, include_items=include_items)


@app.get("/tracking-intelligence/export.json")
def tracking_intelligence_export_json(week_key: str = "", include_items: bool = True, limit: int = 5000):
    # On-demand export only: no UI changes, no extra price calls, no decision changes.
    return export_tracking_json(week_key=week_key or None, include_items=include_items, limit=limit)


@app.get("/tracking-intelligence/export.csv")
def tracking_intelligence_export_csv(week_key: str = "", limit: int = 5000):
    # UTF-8 BOM helps Excel open Arabic text correctly.
    csv_text = "﻿" + export_tracking_csv(week_key=week_key or None, limit=limit)
    filename_week = str(week_key or "current").strip() or "current"
    headers = {"Content-Disposition": f'attachment; filename="tracking_intelligence_{filename_week}.csv"'}
    return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)


@app.get("/tracking-intelligence/status")
def tracking_intelligence_status():
    return tracking_status()


@app.get("/missed-opportunities")
@app.get("/missed-opportunities/weekly")
def missed_opportunities_weekly(week_key: str = "", threshold: float = 20.0, include_items: bool = False, format: str = "json"):
    # Computed on demand only. This diagnostic report does not alter radar scoring,
    # Sharia filtering, live prices, or displayed opportunities.
    fmt = str(format or "json").strip().lower()
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(
            build_missed_weekly_brief(week_key=week_key or None, threshold=threshold, include_items=True),
            media_type="text/plain; charset=utf-8",
        )
    return build_missed_weekly_report(week_key=week_key or None, threshold=threshold, include_items=include_items)


@app.get("/missed-opportunities/export.json")
def missed_opportunities_export_json(week_key: str = "", threshold: float = 20.0, include_items: bool = True, limit: int = 5000):
    return export_missed_json(week_key=week_key or None, threshold=threshold, include_items=include_items, limit=limit)


@app.get("/missed-opportunities/export.csv")
def missed_opportunities_export_csv(week_key: str = "", threshold: float = 20.0, limit: int = 5000):
    csv_text = "﻿" + export_missed_csv(week_key=week_key or None, threshold=threshold, limit=limit)
    filename_week = str(week_key or "current").strip() or "current"
    headers = {"Content-Disposition": f'attachment; filename="missed_opportunities_{filename_week}.csv"'}
    return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)


@app.get("/missed-opportunities/status")
def missed_opportunities_status():
    return missed_status()


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






