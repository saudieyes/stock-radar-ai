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
    WEEKLY_ARCHIVE_TOKEN,
    WEEKLY_ARCHIVE_PRUNE_AFTER_SUCCESS,
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
from app.market_fear import get_market_fear_snapshot, market_fear_status
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
    build_late_promotions_report,
    build_pre_move_evidence_report,
    build_loss_analysis_report,
    export_missed_json,
    export_missed_csv,
)

# Compatibility guard: some running deployments may temporarily have an older
# app/missed_opportunities.py without the single-symbol timeline helpers.
# Do not let that optional endpoint crash the whole web app at startup.
try:
    from app.missed_opportunities import build_symbol_timeline_report, build_symbol_timeline_brief
except Exception as _symbol_timeline_import_error:
    def build_symbol_timeline_report(symbol: str, week_key=None, threshold=None):
        return {
            "ok": False,
            "error": "symbol_timeline_unavailable",
            "symbol": str(symbol or "").upper(),
            "detail": str(_symbol_timeline_import_error),
            "hint": "Update app/missed_opportunities.py to the latest version to enable this optional diagnostic endpoint.",
        }

    def build_symbol_timeline_brief(symbol: str, week_key=None, threshold=None):
        return (
            "Symbol timeline diagnostic is unavailable in the currently loaded "
            f"missed_opportunities module for {str(symbol or '').upper()}. "
            f"Reason: {_symbol_timeline_import_error}"
        )
from app.opportunity_intelligence import enrich_opportunity_intelligence_bulk, enrich_opportunity_intelligence
from app.learning_reports import (
    build_pattern_learning_report,
    build_failure_patterns_report,
    build_winner_patterns_report,
    build_promotion_funnel_report,
)
from app.weekly_archive import archive_weekly_tracking, weekly_archive_status
from app.source_promotion_audit import (
    build_source_entry_audit,
    build_promotion_audit,
    build_clean_alternatives,
    build_source_discovery_coverage,
)
from app.evidence_collector import (
    init_evidence_db,
    start_evidence_background_worker,
    evidence_status,
    collect_evidence_snapshot,
    daily_winners_report,
    weekly_evidence_summary,
    backfill_daily_winner_profiles,
    winner_profiles_report,
    pattern_readiness_report,
    pattern_lab_report,
    export_evidence_json,
    export_evidence_csv,
    sync_evidence_to_github,
    evidence_auto_sync_status,
    run_evidence_auto_sync,
    liquidity_confirmation_check,
    big_mover_anatomy_scan_gap_report,
    evidence_retention_status,
    evidence_retention_verify_github,
    evidence_retention_prune_dry_run,
    evidence_retention_prune_execute,
    evidence_retention_sqlite_compact_status,
    evidence_retention_sqlite_compact_execute,
    evidence_retention_sqlite_smart_compact_status,
    evidence_retention_sqlite_smart_compact_execute,
    evidence_retention_sqlite_table_size_report,
    evidence_snapshots_payload_report,
    evidence_snapshots_raw_json_slim_dry_run,
    evidence_snapshots_raw_json_slim_execute,
    evidence_retention_auto_maintenance_status,
    run_evidence_retention_auto_maintenance,
    evidence_local_archive_cleanup_dry_run,
    evidence_local_archive_cleanup_execute,
)
from app.source_discovery import (
    dynamic_discovery_enabled,
    get_full_market_scan_interval_sec,
    get_last_dynamic_discovery_status,
    load_prepared_big_explosion_watch,
)
from app.early_movement import (
    enrich_stocks_with_early_movement,
    build_early_movement_sections,
    build_early_movement_static_status,
    build_early_movement_weekly_report,
)
from app.source_promotion_v2a import (
    enrich_rows_source_promotion_v2a,
    summarize_source_promotion_v2a,
    build_source_promotion_v2a_report,
)
from app.detection_journal import (
    init_detection_journal_db,
    enrich_rows_with_detection_journal,
    detection_journal_status,
)
from app.source_promotion_engine_v2 import (
    enrich_rows_source_promotion_v2,
    summarize_source_promotion_v2,
)
from app.final_decision_engine import apply_final_decisions
from app.telegram_alerts import maybe_send_buy_now_alerts, telegram_alert_status
from app.trade_plan_ledger import (
    TRADE_PLAN_LEDGER_VERSION,
    active_plan_status,
    apply_breakout_guard_to_rows,
    enrich_rows_with_active_plan_status,
    record_active_strong_plans,
)
from app.opportunity_radar import (
    OPPORTUNITY_RADAR_VERSION,
    build_opportunity_radar_sections,
    build_position_aware_snapshot,
    enrich_rows_opportunity_radar,
    enrich_rows_with_opportunity_plan_memory,
    opportunity_plan_memory_status,
    opportunity_radar_status_payload,
    record_opportunity_plans,
)
from app.paper_trading_engine import PAPER_TRADING_VERSION, paper_trading_status, process_paper_trading_scan
from app.breakout_quality_engine import BREAKOUT_QUALITY_VERSION, enrich_breakout_quality_rows, breakout_quality_status
from app.weekly_plan_lifecycle import WEEKLY_PLAN_LIFECYCLE_VERSION, evaluate_weekly_rows, weekly_plan_lifecycle_status
from app.paper_learning_report import PAPER_LEARNING_REPORT_VERSION, build_paper_learning_report
from app.system_cost_health import build_system_cost_health
from app.pre_move_engine import enrich_row_pre_move
from app.intraday_early_source_radar import get_last_intraday_early_source_radar_status
from app.decision_contract import compact_decision_diagnostics
from app.quote_resolver import resolve_symbol_quote
from app.early_watch_lifecycle import enrich_rows_early_watch_lifecycle, summarize_early_watch_lifecycle
from app.market_replay_lab import (
    MARKET_REPLAY_LAB_VERSION,
    market_replay_lab_status,
    run_small_stock_classic_replay_from_path,
    run_small_stock_classic_replay_from_polygon,
)
from app.learning_archive_v1 import (
    LEARNING_ARCHIVE_VERSION,
    learning_archive_status,
    build_learning_archive_from_polygon,
)
from app.historical_replay_simulator import (
    HISTORICAL_REPLAY_SIMULATOR_VERSION,
    historical_replay_status,
    run_historical_replay,
    format_historical_replay_brief,
    build_prior_session_explosion_watch,
    run_live_hunting_replay,
    format_live_hunting_replay_brief,
)
from app.polygon_weekly_builder import (
    build_weekly_candidates_from_path,
    build_weekly_candidates_from_paths,
    build_weekly_candidates_from_polygon,
    load_weekly_watchlist,
    polygon_flatfile_status,
    POLYGON_WEEKLY_BUILDER_VERSION,
)

app = FastAPI()

try:
    init_db()
except Exception as exc:
    print(f"SQLITE_INIT_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)

try:
    init_detection_journal_db()
except Exception as exc:
    print(f"DETECTION_JOURNAL_INIT_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)

try:
    init_tracking_intelligence_db()
except Exception as exc:
    print(f"TRACKING_INTELLIGENCE_INIT_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)

try:
    init_evidence_db()
except Exception as exc:
    print(f"EVIDENCE_INIT_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)

try:
    _evidence_worker_status = start_evidence_background_worker()
    if not _evidence_worker_status.get("ok", False):
        print(f"EVIDENCE_WORKER_START_ERROR: {_evidence_worker_status}", flush=True)
except Exception as exc:
    print(f"EVIDENCE_WORKER_START_ERROR: {type(exc).__name__}: {str(exc)[:180]}", flush=True)


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


@app.get("/telegram-alerts/status")
def telegram_alerts_status_endpoint():
    return telegram_alert_status()


@app.get("/active-plans/status")
def active_plans_status_endpoint(limit: int = 100):
    return active_plan_status(limit=limit)


@app.get("/opportunity-radar/status")
def opportunity_radar_status_endpoint():
    snapshot = get_json("last_trade_scan_snapshot", {}) or {}
    rows = snapshot.get("rows", []) if isinstance(snapshot, dict) else []
    rows = rows if isinstance(rows, list) else []
    phase = snapshot.get("market_phase", "") if isinstance(snapshot, dict) else ""
    if not phase:
        phase = get_market_phase()
    payload = opportunity_radar_status_payload(rows)
    payload["snapshot_rows_count"] = len(rows)
    payload["status_note_ar"] = "status لا يعرض كل الفرص؛ الصفحة تعتمد على /trade-scan و /radar-live-refresh. V2L2 يصلح خطأ trade-scan الذي كان يمنع تحميل الأقسام."
    try:
        enriched = enrich_rows_opportunity_radar(rows, market_phase=phase)
        sections_preview = build_opportunity_radar_sections(enriched, market_phase=phase)
        payload["status_enriched_rows_current_version"] = len(enriched)
        payload["sections_preview_counts"] = {
            "promotion_bridge_candidates_count": int(sections_preview.get("promotion_bridge_candidates_count", 0) or 0),
            "learning_opportunity_candidates_count": int(sections_preview.get("learning_opportunity_candidates_count", 0) or 0),
            "small_stock_classic_radar_count": int(sections_preview.get("small_stock_classic_radar_count", 0) or 0),
            "pre_trigger_candidates_count": int(sections_preview.get("pre_trigger_candidates_count", 0) or 0),
            "support_bounce_candidates_count": int(sections_preview.get("support_bounce_candidates_count", 0) or 0),
            "reclaim_candidates_count": int(sections_preview.get("reclaim_candidates_count", 0) or 0),
            "continuation_pullback_candidates_count": int(sections_preview.get("continuation_pullback_candidates_count", 0) or 0),
            "low_float_premarket_radar_count": int(sections_preview.get("low_float_premarket_radar_count", 0) or 0),
            "high_risk_day_trades_count": int(sections_preview.get("high_risk_day_trades_count", 0) or 0),
        }
        payload["closed_market_prep_added_count"] = int(sections_preview.get("closed_market_prep_added_count", 0) or 0)
        payload["closed_market_opportunity_mode"] = sections_preview.get("closed_market_opportunity_mode", {})
    except Exception as exc:
        payload["sections_preview_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return payload


@app.get("/opportunity-radar/plan-memory/status")
def opportunity_plan_memory_status_endpoint(limit: int = 100):
    return opportunity_plan_memory_status(limit=limit)


def _small_stock_classic_radar_status_payload():
    snapshot = get_json("last_trade_scan_snapshot", {}) or {}
    rows = snapshot.get("rows", []) if isinstance(snapshot, dict) else []
    market_phase = snapshot.get("market_phase", "") if isinstance(snapshot, dict) else ""
    rows = enrich_rows_opportunity_radar(rows if isinstance(rows, list) else [], market_phase=market_phase)
    sections = build_opportunity_radar_sections(rows, market_phase=market_phase)
    items = sections.get("small_stock_classic_radar", []) or []
    return {
        "ok": True,
        "version": OPPORTUNITY_RADAR_VERSION,
        "section": "small_stock_classic_radar",
        "count": len(items),
        "items": items,
        "aliases": [
            "/small-stock-classic-radar/status",
            "/small-stock-classic-radar",
            "/small-stock-classic/status",
            "/opportunity-radar/small-stock-classic/status",
            "/opportunity-radar/small-stock-classic-radar/status",
        ],
        "rule_ar": "فلتر الأسهم الصغيرة V2b: قرب الدعم والمقاومة طبيعي في الأسهم منخفضة السعر؛ نعاملها كمنطقة قرار صغيرة، وننتظر Fib 61.8/78.6 أو VWAP بإغلاق شمعة أو قمة أمس أو إغلاق فوق حد التفعيل. لا نطارد الشمعة الخضراء.",
        "note_ar": "إذا كان العدد صفرًا فهذا لا يعني عطلًا؛ يعني أنه لا توجد أسهم صغيرة مطابقة في آخر لقطة محفوظة. الأسهم التي تحركت كثيرًا تُحوّل إلى مضاربة عالية المخاطرة/تحتاج Pullback بدل Small Classic."
    }


@app.get("/small-stock-classic-radar/status")
@app.get("/small-stock-classic-radar")
@app.get("/small-stock-classic/status")
@app.get("/small_stock_classic_radar/status")
@app.get("/opportunity-radar/small-stock-classic/status")
@app.get("/opportunity-radar/small-stock-classic-radar/status")
def small_stock_classic_radar_status_endpoint():
    return _small_stock_classic_radar_status_payload()


@app.get("/replay-lab/status")
@app.get("/replay-lab")
def replay_lab_status_endpoint():
    payload = market_replay_lab_status()
    if isinstance(payload, dict):
        payload["aliases"] = ["/replay-lab/status", "/replay-lab"]
        payload["small_stock_status_endpoint"] = "/small-stock-classic-radar/status"
    return payload


@app.get("/replay-lab/small-stock-classic/run")
def replay_lab_small_stock_classic_run_endpoint(path: str = "", max_files: int = 5, max_rows: int = 250000, max_candidates: int = 120):
    return run_small_stock_classic_replay_from_path(path=path, max_files=max_files, max_rows=max_rows, max_candidates=max_candidates)




@app.get("/replay-lab/small-stock-classic/pull-run")
@app.get("/replay-lab/small-stock-classic/run-polygon")
def replay_lab_small_stock_classic_pull_run_endpoint(end_date: str = "", minute_days: int = 5, max_rows: int = 250000, max_candidates: int = 120, daily_lookback_days: int = 14, force: bool = False, redownload_processed: bool = True):
    return run_small_stock_classic_replay_from_polygon(end_date=end_date, minute_days=minute_days, max_rows=max_rows, max_candidates=max_candidates, daily_lookback_days=daily_lookback_days, force=force, redownload_processed=redownload_processed)



@app.get("/simulator/historical-replay/status")
@app.get("/historical-replay/status")
def historical_replay_simulator_status_endpoint():
    return historical_replay_status()

@app.get("/simulator/live-hunting-replay")
@app.get("/simulator/v2v2-live-hunting-replay")
@app.get("/simulator/v2v3-live-hunting-audit")
@app.get("/simulator/replay-audit-prepared-link")
@app.get("/replay/live-hunting")
def live_hunting_replay_endpoint(
    date: str = "",
    max_prepared: int = 80,
    max_symbols: int = 80,
    missed_gain_threshold: float = 20.0,
    context_days: int = 3,
    recovery_days: int = 7,
    prior_full_session_scan: bool = True,
    prior_scan_max_rows: int = 2500000,
    max_minute_rows: int = 1800000,
    prior_scan_timeout_sec: float = 45.0,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    include_candidates: bool = True,
    audit_symbols: str = "EHGO,ICCM,TPC,SNBR,HOUR,BFLY,NIXX,NIVF,JLHL,BIRD",
    format: str = "json",
):
    fmt = str(format or "json").strip().lower()
    try:
        payload = run_live_hunting_replay(
            date_value=date,
            max_prepared=max_prepared,
            max_symbols=max_symbols,
            missed_gain_threshold=missed_gain_threshold,
            context_days=context_days,
            recovery_days=recovery_days,
            prior_full_session_scan=prior_full_session_scan,
            prior_scan_max_rows=prior_scan_max_rows,
            max_minute_rows=max_minute_rows,
            prior_scan_timeout_sec=prior_scan_timeout_sec,
            force_minute_pull=force_minute_pull,
            redownload_processed=redownload_processed,
            include_candidates=include_candidates,
            audit_symbols=audit_symbols,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "version": "v2v3_replay_audit_endpoint_guard_2026_06_21",
            "error": f"live_hunting_replay_exception:{type(exc).__name__}:{str(exc)[:240]}",
            "rule_ar": "حارس endpoint يمنع سقوط الخدمة؛ خفّض max_symbols أو max_minute_rows إذا كان ملف الدقيقة كبيرًا.",
        }
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(format_live_hunting_replay_brief(payload), media_type="text/plain; charset=utf-8")
    return payload


@app.get("/simulator/historical-replay")
@app.get("/historical-replay")
@app.get("/replay/historical-market")
def historical_replay_simulator_endpoint(
    date: str = "",
    max_candidates: int = 40,
    clean_only: bool = True,
    include_candidates: bool = True,
    recovery_days: int = 7,
    context_days: int = 3,
    missed_gain_threshold: float = 20.0,
    minute_timing: bool = True,
    timing_symbols_limit: int = 30,
    max_minute_rows: int = 1800000,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    prior_full_session_scan: bool = True,
    prior_scan_max_rows: int = 2500000,
    prior_scan_timeout_sec: float = 45.0,
    persist_prepared_watch: bool = False,
    format: str = "json",
):
    fmt = str(format or "json").strip().lower()
    try:
        payload = run_historical_replay(
            date_value=date,
            max_candidates=max_candidates,
            clean_only=clean_only,
            include_candidates=include_candidates,
            recovery_days=recovery_days,
            context_days=context_days,
            missed_gain_threshold=missed_gain_threshold,
            minute_timing=minute_timing,
            timing_symbols_limit=timing_symbols_limit,
            max_minute_rows=max_minute_rows,
            force_minute_pull=force_minute_pull,
            redownload_processed=redownload_processed,
            prior_full_session_scan=prior_full_session_scan,
            prior_scan_max_rows=prior_scan_max_rows,
            prior_scan_timeout_sec=prior_scan_timeout_sec,
            persist_prepared_watch=persist_prepared_watch,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "version": "historical_replay_endpoint_guard_v2t2b_2026_06_20",
            "error": f"historical_replay_exception:{type(exc).__name__}:{str(exc)[:240]}",
            "rule_ar": "بدل سقوط upstream، يرجع هذا الحارس سبب الخطأ. جرّب تقليل prior_scan_max_rows أو إيقاف prior_full_session_scan مؤقتًا.",
        }
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        if not payload.get("ok"):
            return PlainTextResponse("Historical Replay error guard\n" + str(payload), media_type="text/plain; charset=utf-8")
        return PlainTextResponse(format_historical_replay_brief(payload), media_type="text/plain; charset=utf-8")
    return payload


@app.get("/diagnostics/prepared-explosion-watch")
@app.get("/source-discovery/prepared-explosion-watch")
def prepared_explosion_watch_endpoint(format: str = "json"):
    items, debug = load_prepared_big_explosion_watch()
    payload = {
        "ok": True,
        "version": "prepared_explosion_watch_status_v2u4_live_critical_pre_explosion_2026_06_20",
        "count": len(items or []),
        "symbols": [x.get("symbol") for x in (items or [])[:120]],
        "items": items[:120],
        "debug": debug,
        "rule_ar": "هذه قائمة ما بعد الإغلاق الجاهزة للأداة الحية قبل البري ماركت؛ مراقبة/مراجعة شرعية فقط وليست شراء مباشر.",
    }
    if str(format or "json").lower() in {"brief", "text", "txt"}:
        lines = ["Prepared Explosion Watch V2U4", f"count: {payload['count']}", "symbols: " + ", ".join(payload["symbols"][:80]), str(debug)]
        return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")
    return payload


@app.get("/maintenance/prior-session-explosion-scan")
@app.get("/diagnostics/prior-session-explosion-scan")
def prior_session_explosion_scan_endpoint(
    date: str = "",
    max_minute_rows: int = 2500000,
    max_seconds: float = 45.0,
    force_minute_pull: bool = False,
    redownload_processed: bool = True,
    persist: bool = True,
    format: str = "json",
):
    trade_date = str(date or "").strip()
    if not trade_date:
        # Use today's date as a safe default only when the user/job passes date empty;
        # in production scheduler this should be the last completed trading date.
        from datetime import datetime, timedelta
        trade_date = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    payload = build_prior_session_explosion_watch(
        trade_date=trade_date,
        max_minute_rows=max_minute_rows,
        max_seconds=max_seconds,
        force_minute_pull=force_minute_pull,
        redownload_processed=redownload_processed,
        persist=persist,
    )
    if str(format or "json").lower() in {"brief", "text", "txt"}:
        scan = payload.get("scan_debug") or {}
        lines = [
            "Prior Session Explosion Scan V2U4",
            f"date: {trade_date}",
            f"ok: {payload.get('ok')}",
            f"prepared_watch_count: {payload.get('prepared_watch_count')}",
            f"compact_count: {payload.get('compact_count')}",
            "top: " + ", ".join(payload.get("top_symbols", [])[:80]),
            f"rows_seen: {scan.get('rows_seen')} | symbols: {scan.get('symbols_seen')} | source_rows: {scan.get('source_rows_total')}",
            "target_probe: " + str(((scan.get('prior_pre_explosion_watch_debug') or {}).get('target_probe') or {})),
            "bucket_counts: " + str(((scan.get('prior_pre_explosion_watch_debug') or {}).get('bucket_counts') or {})),
            "مهم: هذه قائمة تحضير ومراجعة شرعية قبل السوق، لا شراء مباشر.",
        ]
        return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")
    return payload


@app.get("/learning-archive/status")
@app.get("/replay-lab/learning-archive/status")
def learning_archive_status_endpoint():
    return learning_archive_status()


@app.get("/learning-archive/build")
@app.get("/replay-lab/learning-archive/build")
def learning_archive_build_endpoint(
    end_date: str = "",
    minute_days: int = 5,
    daily_lookback_days: int = 14,
    max_rows: int = 250000,
    max_candidates: int = 120,
    redownload_processed: bool = True,
    force: bool = False,
    persist: bool = False,
    include_rows: bool = False,
    window_label: str = "",
):
    return build_learning_archive_from_polygon(
        end_date=end_date,
        minute_days=minute_days,
        daily_lookback_days=daily_lookback_days,
        max_rows=max_rows,
        max_candidates=max_candidates,
        redownload_processed=redownload_processed,
        force=force,
        persist=persist,
        include_rows=include_rows,
        window_label=window_label,
    )

@app.get("/paper-trading/status")
def paper_trading_status_endpoint():
    return paper_trading_status()


@app.get("/paper-trading/report")
def paper_trading_report_endpoint():
    return paper_trading_status()


@app.get("/paper-trading/learning-report")
def paper_trading_learning_report_endpoint():
    return build_paper_learning_report()


@app.get("/breakout-quality/status")
def breakout_quality_status_endpoint():
    return breakout_quality_status()


@app.get("/weekly-plan-lifecycle/status")
def weekly_plan_lifecycle_status_endpoint(limit: int = 100):
    return weekly_plan_lifecycle_status(limit=limit)


@app.get("/system-cost-health")
@app.get("/diagnostics/system-cost-health")
def system_cost_health_endpoint():
    return build_system_cost_health()


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


def _as_text_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    # Keep Arabic comma-separated summaries useful without over-splitting normal phrases.
    if "،" in text:
        return [x.strip() for x in text.split("،") if x.strip()]
    return [text]


def _has_any_phrase(items, phrases: list[str]) -> bool:
    joined = " | ".join(_as_text_list(items))
    return any(str(p) and str(p) in joined for p in phrases)


def _live_overlay_execution_blockers(row: dict) -> list[str]:
    """Final safety gate for radar-live-refresh/live overlay.

    Source/Promotion V2a1 correctly blocks promotion when the source layer sees
    resistance/no-chase/unreliable price. This guard prevents the *live overlay*
    from re-promoting the same row to Strong/Cautious after V2a1 already blocked it.
    It is intentionally conservative during pre-market/after-hours because these
    quotes are useful for monitoring, not execution.
    """
    row = row or {}
    blockers: list[str] = []

    def add(reason: str):
        if reason and reason not in blockers:
            blockers.append(reason)

    market_phase = str(row.get("market_phase", "") or "")
    price_score = safe_round(row.get("price_freshness_score", 99))
    price_label = str(row.get("price_freshness_label", "") or row.get("price_source_label", "") or "")

    # Any explicit V2a/source-promotion block must dominate live overlay promotion.
    promo_blocks = _as_text_list(row.get("promotion_block_reasons"))
    hard_block_phrases = [
        "No-Chase",
        "الحركة متأخرة",
        "قريب جدًا من مقاومة",
        "اختراق المقاومة القريبة",
        "السعر اللحظي غير موثوق",
        "السيولة غير مؤكدة",
        "تأكيد ما بعد التفعيل ضعيف",
        "كسر/اختبار دعم",
    ]
    for reason in promo_blocks:
        if any(p in reason for p in hard_block_phrases):
            add(reason)

    # Do not let pre-market/extended quotes become actionable when the app already
    # says the price is not reliable enough for execution.
    if (row.get("price_reliable_for_execution") is False
        or row.get("live_price_available") is False
        or price_score <= 40
        or "غير موثوق" in price_label
        or "غير كاف" in price_label
        or "unavailable" in str(row.get("price_source", "") or "").lower()):
        add("السعر اللحظي غير موثوق")

    # No-chase and near-resistance must control the final visible decision.
    em = row.get("early_movement") if isinstance(row.get("early_movement"), dict) else {}
    if str(row.get("early_movement_status", "") or "").lower() == "no_chase" or str(em.get("status", "") or "").lower() == "no_chase":
        add("No-Chase / الحركة متأخرة")
    if _as_text_list(row.get("early_movement_no_chase_reasons")) or _as_text_list(row.get("no_chase_reasons")) or _as_text_list(em.get("no_chase_reasons")):
        add("No-Chase / الحركة متأخرة")
    if str(row.get("no_chase_guard_status", "") or "").lower() == "no_chase":
        add("No-Chase / الحركة متأخرة")

    if row.get("close_resistance_guard_flag") is True or str(row.get("execution_gate_status", "") or "") == "wait_resistance_break":
        add("⏳ انتظر اختراق المقاومة القريبة والثبات فوقها")

    if str(row.get("liquidity_persistence_status", "") or "").lower() == "weak":
        add("السيولة غير مؤكدة أو لا تبدو مستمرة")
    if str(row.get("post_activation_guard_status", "") or "").lower() == "weak":
        add("تأكيد ما بعد التفعيل ضعيف")

    # During pre-market/after-hours, only monitoring is allowed unless another
    # reliable execution source marks the row executable.
    if market_phase in {"pre_market", "after_hours"} and row.get("price_reliable_for_execution") is not True:
        add("السعر اللحظي غير موثوق")

    return blockers


def _apply_live_overlay_block(row: dict, blockers: list[str], original_decision: str, base_score: float) -> dict:
    out = dict(row or {})
    out["decision"] = "مراقبة"
    out["effective_decision"] = "مراقبة"
    out["live_plan_status"] = "live_overlay_blocked"
    out["live_plan_action"] = "مراقبة فقط حتى تزول موانع الترقية"
    out["live_plan_reason"] = "، ".join(blockers[:4]) if blockers else "تم منع الترقية الحية احتياطيًا"
    out["live_plan_adjustment"] = safe_round(min(0.0, safe_round(out.get("live_plan_adjustment", 0)) - 12.0), 2)
    out["live_overlay_gate_status"] = "blocked"
    out["live_overlay_block_reasons"] = blockers[:8]
    # If V2a marked the row promoted earlier, neutralize that flag when final blockers exist.
    if out.get("source_promotion_v2a_promoted"):
        out["source_promotion_v2a_promoted_before_live_gate"] = True
        out.pop("source_promotion_v2a_promoted", None)
        out.pop("source_promotion_rank_boost", None)
    current_live_rank = safe_round(out.get("live_rank_score", out.get("display_rank_score", base_score)))
    out["live_rank_score"] = safe_round(max(0.0, current_live_rank - 12.0), 2)
    existing = str(out.get("live_rank_reason", "") or "")
    gate_note = "منع الترقية الحية: " + ("، ".join(blockers[:3]) if blockers else "بوابة حماية")
    out["live_rank_reason"] = "، ".join([x for x in [existing, gate_note] if x][:5])
    # Keep the original technical read for diagnostics, but do not show it as actionable.
    out["live_blocked_original_decision"] = original_decision
    return out


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
    quote_change_raw = quote.get("change_pct", None)
    quote_change_is_number = quote_change_raw is not None and str(quote_change_raw).strip() != ""
    quote_change_reliable = bool(quote_change_is_number and quote.get("change_pct_reliable", True) is not False)
    if quote_change_reliable:
        change_pct = safe_round(quote_change_raw, 2)
    else:
        # Keep the saved/scan percentage instead of turning the card black at 0.00%
        # when an extended-hours quote has price but no reliable previous-close baseline.
        change_pct = safe_round(
            out.get("display_change_pct",
                    out.get("change_vs_prev_close_pct",
                            out.get("change_pct", out.get("change_from_open_pct", 0)))),
            2,
        )
    volume = safe_round(quote.get("volume", 0))
    source_label = str(quote.get("source_label", "") or quote.get("source", "FMP/Live"))
    updated_label = str(quote.get("updated_label", "") or "")
    quote_source = str(quote.get("source", "live_overlay") or "live_overlay")
    quote_delayed = bool(quote.get("delayed")) or any(x in quote_source.lower() for x in ["polygon", "snapshot"])
    quote_reliable_for_execution = bool(quote.get("reliable_for_execution", True)) and not quote_delayed

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
        "change_vs_prev_close_pct": change_pct,
        "live_change_pct_reliable": bool(quote_change_reliable),
        "previous_close_live": prev_close,
        "volume_live": volume,
        "price_source": quote_source,
        "price_source_label": source_label,
        "price_source_delayed": bool(quote_delayed),
        "price_reliable_for_execution": bool(quote_reliable_for_execution),
        "price_monitoring_only": bool(quote_delayed or not quote_reliable_for_execution),
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
    live_gate_blockers = _live_overlay_execution_blockers(out)

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

    # Final V2a2 live-overlay gate: no live overlay may promote or preserve an
    # actionable label when V2a/source-promotion or execution safety says to wait.
    if new_decision in {"دخول قوي", "دخول بحذر"} and live_gate_blockers:
        blocked = _apply_live_overlay_block(out, live_gate_blockers, original_decision, base_score)
        # Preserve diagnostic status/reasons from the calculations above.
        blocked["blocked_live_candidate_decision"] = new_decision
        blocked["blocked_live_candidate_status"] = status
        blocked["blocked_live_candidate_reasons"] = reasons[:5]
        return blocked

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
    try:
        overlaid = enrich_opportunity_intelligence_bulk(overlaid)
    except Exception:
        pass

    # Wealth Builder V1d safety: the 30-second live refresh endpoint used to
    # rebuild visible buckets from the saved snapshot without reapplying the
    # Early Movement merge and final V1b/V1c safety caps. This caused the UI to
    # flip from the correct clean scan (Strong=0, Early Movement>0) to an older
    # live-overlay view (Strong returning, Early Movement=0), especially after
    # manual Sharia actions or the automatic live timer. Always pass the live
    # overlay through the same final presentation layer used by /trade-scan.
    try:
        overlaid = enrich_rows_with_detection_journal(overlaid, source_layer="radar_live_refresh")
    except Exception:
        pass
    try:
        overlaid = [enrich_row_pre_move(x) if isinstance(x, dict) else x for x in overlaid]
    except Exception:
        pass
    try:
        overlaid = enrich_stocks_with_early_movement(overlaid)
    except Exception:
        pass
    try:
        overlaid = enrich_rows_source_promotion_v2a(overlaid)
    except Exception:
        pass
    try:
        overlaid = enrich_rows_source_promotion_v2(overlaid)
    except Exception:
        pass
    try:
        overlaid = _post_early_movement_decision_safety(overlaid)
    except Exception:
        pass
    try:
        overlaid = apply_final_decisions(overlaid)
    except Exception:
        pass
    try:
        overlaid = enrich_rows_early_watch_lifecycle(overlaid)
    except Exception:
        pass
    try:
        overlaid = enrich_breakout_quality_rows(overlaid)
    except Exception:
        pass
    try:
        overlaid = apply_breakout_guard_to_rows(overlaid)
    except Exception:
        pass
    try:
        overlaid = enrich_rows_with_active_plan_status(overlaid)
    except Exception:
        pass
    try:
        overlaid = enrich_rows_with_opportunity_plan_memory(overlaid)
    except Exception:
        pass
    try:
        overlaid = enrich_rows_opportunity_radar(overlaid, market_phase=phase)
    except Exception:
        pass
    try:
        early_movement_payload = build_early_movement_sections(overlaid)
    except Exception:
        early_movement_payload = {"count": 0, "early_movement_watchlist": [], "weekly_priority_count": 0, "auto_detected_count": 0, "priority_watch_count": 0}
    try:
        opportunity_radar_payload = build_opportunity_radar_sections(overlaid, market_phase=phase)
    except Exception as exc:
        opportunity_radar_payload = {"ok": False, "version": OPPORTUNITY_RADAR_VERSION, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

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

    support_bounce_candidates = opportunity_radar_payload.get("support_bounce_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    reclaim_candidates = opportunity_radar_payload.get("reclaim_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    pre_trigger_candidates = opportunity_radar_payload.get("pre_trigger_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    continuation_pullback_candidates = opportunity_radar_payload.get("continuation_pullback_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    high_risk_day_trades = opportunity_radar_payload.get("high_risk_day_trades", []) if isinstance(opportunity_radar_payload, dict) else []
    low_float_premarket_radar = opportunity_radar_payload.get("low_float_premarket_radar", []) if isinstance(opportunity_radar_payload, dict) else []
    low_float_fast_lane_raw_watch = opportunity_radar_payload.get("low_float_fast_lane_raw_watch", []) if isinstance(opportunity_radar_payload, dict) else []
    gap_fill_watch = opportunity_radar_payload.get("gap_fill_watch", []) if isinstance(opportunity_radar_payload, dict) else []
    catalyst_watch = opportunity_radar_payload.get("catalyst_watch", []) if isinstance(opportunity_radar_payload, dict) else []
    learning_opportunity_candidates = opportunity_radar_payload.get("learning_opportunity_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    promotion_bridge_candidates = opportunity_radar_payload.get("promotion_bridge_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    live_tight_monitoring_candidates = opportunity_radar_payload.get("live_tight_monitoring_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    critical_pre_explosion_watch = opportunity_radar_payload.get("critical_pre_explosion_watch", []) if isinstance(opportunity_radar_payload, dict) else []

    try:
        plan_ledger_live_stats = record_active_strong_plans(strong, source="radar_live_refresh")
    except Exception as exc:
        plan_ledger_live_stats = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        memory_rows = []
        for bucket_rows in [strong, cautious, pre_trigger_candidates, support_bounce_candidates, reclaim_candidates, continuation_pullback_candidates, low_float_premarket_radar, high_risk_day_trades]:
            memory_rows.extend(bucket_rows or [])
        opportunity_plan_memory_live_stats = record_opportunity_plans(memory_rows, source="radar_live_refresh")
    except Exception as exc:
        opportunity_plan_memory_live_stats = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        weekly_rows_for_lifecycle = ((load_weekly_watchlist() or {}).get("candidates") or [])
        weekly_lifecycle_live_stats = evaluate_weekly_rows(weekly_rows_for_lifecycle, source="radar_live_refresh")
    except Exception as exc:
        weekly_rows_for_lifecycle = []
        weekly_lifecycle_live_stats = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        paper_trading_live_stats = process_paper_trading_scan(strong_rows=strong, cautious_rows=cautious, watch_rows=watch, weekly_rows=weekly_rows_for_lifecycle, source="radar_live_refresh")
    except Exception as exc:
        paper_trading_live_stats = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    tracking_live_stats = {}
    try:
        # Update Tracking Intelligence outcomes from the same fresh prices already
        # fetched for the live overlay. This adds no API calls and does not create
        # new tracking records from the 30-second loop.
        tracking_live_stats = refresh_tracking_prices_from_rows(overlaid, source="radar_live_refresh")
    except Exception as exc:
        tracking_live_stats = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        telegram_live_stats = maybe_send_buy_now_alerts(strong, source="radar_live_refresh")
    except Exception as exc:
        telegram_live_stats = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

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
        "telegram_alerts": telegram_live_stats,
        "plan_ledger": plan_ledger_live_stats,
        "opportunity_plan_memory": opportunity_plan_memory_live_stats,
        "paper_trading": paper_trading_live_stats,
        "weekly_plan_lifecycle": weekly_lifecycle_live_stats,
        "live_overlay_gate_version": "source_promotion_v2a2_live_overlay_gate",
        "live_overlay_blocked_count": len([x for x in overlaid if isinstance(x, dict) and x.get("live_overlay_gate_status") == "blocked"]),
        "early_movement_count": int(early_movement_payload.get("count", 0) or 0),
        "early_movement_weekly_priority_count": int(early_movement_payload.get("weekly_priority_count", 0) or 0),
        "early_movement_auto_detected_count": int(early_movement_payload.get("auto_detected_count", 0) or 0),
        "early_movement_priority_watch_count": int(early_movement_payload.get("priority_watch_count", 0) or 0),
        "early_movement_fast_lane_count": len([x for x in overlaid if isinstance(x, dict) and x.get("early_movement_fast_lane_applied")]),
        "opportunity_radar": opportunity_radar_payload,
        "opportunity_radar_version": OPPORTUNITY_RADAR_VERSION,
        "promotion_bridge_debug": opportunity_radar_payload.get("promotion_bridge_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "promotion_bridge_rule_ar": opportunity_radar_payload.get("promotion_bridge_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "promotion_bridge_candidates_count": len(promotion_bridge_candidates),
        "promotion_bridge_candidates": promotion_bridge_candidates[:limit] if 'limit' in locals() else promotion_bridge_candidates[:25],
        "live_tight_monitoring_debug": opportunity_radar_payload.get("live_tight_monitoring_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "live_tight_monitoring_rule_ar": opportunity_radar_payload.get("live_tight_monitoring_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "live_tight_monitoring_candidates_count": len(live_tight_monitoring_candidates),
        "live_tight_monitoring_candidates": live_tight_monitoring_candidates[:limit] if 'limit' in locals() else live_tight_monitoring_candidates[:25],
        "critical_pre_explosion_watch_debug": opportunity_radar_payload.get("prepared_watch_ui_bridge_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "critical_pre_explosion_watch_rule_ar": opportunity_radar_payload.get("prepared_watch_ui_bridge_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "critical_pre_explosion_watch_count": len(critical_pre_explosion_watch),
        "critical_pre_explosion_watch": critical_pre_explosion_watch[:limit] if 'limit' in locals() else critical_pre_explosion_watch[:25],
        "learning_overlay_summary": opportunity_radar_payload.get("learning_overlay_summary", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "learning_overlay_candidates": opportunity_radar_payload.get("learning_overlay_candidates", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "learning_overlay_candidates_count": int(opportunity_radar_payload.get("learning_overlay_candidates_count", 0) or 0) if isinstance(opportunity_radar_payload, dict) else 0,
        "next_week_analysis": opportunity_radar_payload.get("next_week_analysis", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "next_week_watchlist": opportunity_radar_payload.get("next_week_watchlist", []) if isinstance(opportunity_radar_payload, dict) else [],
        "next_week_analysis_count": int(opportunity_radar_payload.get("next_week_analysis_count", 0) or 0) if isinstance(opportunity_radar_payload, dict) else 0,
        "learning_bridge_debug": opportunity_radar_payload.get("learning_bridge_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "learning_bridge_rule_ar": opportunity_radar_payload.get("learning_bridge_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "low_float_capture_debug": opportunity_radar_payload.get("low_float_capture_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "low_float_capture_rule_ar": opportunity_radar_payload.get("low_float_capture_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "fast_lane_funnel_debug": opportunity_radar_payload.get("fast_lane_funnel_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "fast_lane_funnel_rule_ar": opportunity_radar_payload.get("fast_lane_funnel_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "low_float_fast_lane_raw_watch_count": len(low_float_fast_lane_raw_watch),
        "low_float_fast_lane_raw_watch": low_float_fast_lane_raw_watch[:limit] if 'limit' in locals() else low_float_fast_lane_raw_watch[:25],
        "learning_opportunity_candidates_count": len(learning_opportunity_candidates),
        "learning_opportunity_candidates": learning_opportunity_candidates[:limit],
        "live_tight_monitoring_candidates_count": len(live_tight_monitoring_candidates),
        "support_bounce_candidates_count": len(support_bounce_candidates),
        "reclaim_candidates_count": len(reclaim_candidates),
        "pre_trigger_candidates_count": len(pre_trigger_candidates),
        "continuation_pullback_candidates_count": len(continuation_pullback_candidates),
        "high_risk_day_trades_count": len(high_risk_day_trades),
        "low_float_premarket_radar_count": len(low_float_premarket_radar),
        "low_float_fast_lane_raw_watch_count": len(low_float_fast_lane_raw_watch),
        "gap_fill_watch_count": len(gap_fill_watch),
        "catalyst_watch_count": len(catalyst_watch),
        "live_tight_monitoring_candidates": live_tight_monitoring_candidates[:limit],
        "support_bounce_candidates": support_bounce_candidates[:limit],
        "reclaim_candidates": reclaim_candidates[:limit],
        "pre_trigger_candidates": pre_trigger_candidates[:limit],
        "continuation_pullback_candidates": continuation_pullback_candidates[:limit],
        "high_risk_day_trades": high_risk_day_trades[:limit],
        "low_float_premarket_radar": low_float_premarket_radar[:limit],
        "low_float_fast_lane_raw_watch": low_float_fast_lane_raw_watch[:limit],
        "gap_fill_watch": gap_fill_watch[:limit],
        "catalyst_watch": catalyst_watch[:limit],
        "source_promotion_v2a": summarize_source_promotion_v2a(overlaid),
        "source_promotion_v2a_promoted_count": len([x for x in overlaid if isinstance(x, dict) and x.get("source_promotion_v2a_promoted")]),
        "source_early_discovery_v2": summarize_source_promotion_v2(overlaid),
        "early_watch_lifecycle": summarize_early_watch_lifecycle(overlaid),
        "groups": {
            "strong_entries": _live_bucket_payload(strong, limit),
            "cautious_entries": _live_bucket_payload(cautious, limit),
            "gray_strong": _live_bucket_payload(gray_strong, limit),
            "premarket_setups": _live_bucket_payload(premarket_setups, limit),
            "early_movement_watchlist": _live_bucket_payload(early_movement_payload.get("early_movement_watchlist", []), limit),
            "promotion_bridge_candidates": _live_bucket_payload(promotion_bridge_candidates, limit),
            "live_tight_monitoring_candidates": _live_bucket_payload(live_tight_monitoring_candidates, limit),
            "support_bounce_candidates": _live_bucket_payload(support_bounce_candidates, limit),
            "reclaim_candidates": _live_bucket_payload(reclaim_candidates, limit),
            "pre_trigger_candidates": _live_bucket_payload(pre_trigger_candidates, limit),
            "continuation_pullback_candidates": _live_bucket_payload(continuation_pullback_candidates, limit),
            "high_risk_day_trades": _live_bucket_payload(high_risk_day_trades, limit),
            "low_float_premarket_radar": _live_bucket_payload(low_float_premarket_radar, limit),
            "low_float_fast_lane_raw_watch": _live_bucket_payload(low_float_fast_lane_raw_watch, limit),
            "gap_fill_watch": _live_bucket_payload(gap_fill_watch, limit),
            "catalyst_watch": _live_bucket_payload(catalyst_watch, limit),
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

    market_fear = {}
    try:
        market_fear = get_market_fear_snapshot(force_refresh=False, store=True)
    except Exception as _market_fear_exc:
        market_fear = {"ok": False, "error": str(_market_fear_exc)[:160]}

    # V4d: VIX/Market Fear is decision-support context. It does not change
    # stock score/ranking here, but it does slightly affect the displayed
    # market mood score so the user sees market stress clearly in the top card.
    fear_score = _safe_pct((market_fear or {}).get("stress_score", 0), 0) if isinstance(market_fear, dict) else 0
    if fear_score >= 70:
        score = max(0.0, min(100.0, score - 8.0))
    elif fear_score >= 55:
        score = max(0.0, min(100.0, score - 4.0))
    elif fear_score <= 35 and market_fear.get("ok"):
        score = max(0.0, min(100.0, score + 2.0))
    label = _market_mood_label(score, avg_index_pct)
    risk_label = _market_mood_risk_label(score)

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
        "market_fear": market_fear if isinstance(market_fear, dict) else {},
        "market_fear_summary_ar": (market_fear or {}).get("summary_ar", "") if isinstance(market_fear, dict) else "",
        "market_fear_guidance_ar": (market_fear or {}).get("guidance_ar", []) if isinstance(market_fear, dict) else [],
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



@app.get("/market-fear")
@app.get("/vix-risk")
def market_fear_endpoint(force_refresh: bool = False):
    """VIX / Market Fear decision-support layer.

    This endpoint is intentionally context-only in V4d: it gives execution
    guidance and tracking fields, but it does not change stock scoring, ranking,
    or Sharia filtering.
    """
    return get_market_fear_snapshot(force_refresh=bool(force_refresh), store=True)


@app.get("/market-fear/status")
def market_fear_status_endpoint():
    return market_fear_status()



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


def _dedupe_text_list(values, limit: int = 12):
    out = []
    seen = set()
    for v in values or []:
        text = str(v or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _clean_owner_action_text(text: str, decision: str = "") -> str:
    """Remove duplicated Arabic guidance fragments and soften stale Strong wording.

    Some upstream layers append the same high-risk sentence more than once.  This
    cleanup is display-only and avoids confusing messages such as the same warning
    repeated twice on the card.
    """
    try:
        import re
        text = str(text or "").strip()
        if not text:
            return text
        # If the final tier is no longer Strong, do not keep wording that says
        # "Strong entry high risk"; it is now just a high-risk watch/cautious note.
        if decision != "دخول قوي":
            text = text.replace("دخول قوي عالي المخاطرة / يحتاج تأكيد", "فرصة عالية المخاطرة / تحتاج تأكيد")
        parts = re.split(r"(?<=[.!؟])\s+", text)
        cleaned = []
        seen = set()
        for part in parts:
            part = part.strip()
            if not part:
                continue
            key = re.sub(r"\s+", " ", part)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(part)
        out = " ".join(cleaned).strip()
        # Extra guard for exact repeated phrase when punctuation splitting did not catch it.
        phrase = "⚠️ فرصة عالية المخاطرة / تحتاج تأكيد: انتظر تأكيد السيولة والثبات قبل الدخول."
        while out.count(phrase) > 1:
            out = out.replace(phrase + " " + phrase, phrase)
        phrase2 = "⚠️ دخول قوي عالي المخاطرة / يحتاج تأكيد: انتظر تأكيد السيولة والثبات قبل الدخول."
        while out.count(phrase2) > 1:
            out = out.replace(phrase2 + " " + phrase2, phrase2)
        return out
    except Exception:
        return str(text or "")


def _cap_distant_historical_target(stock: dict) -> None:
    """Do not use a far historical/ATH resistance as a practical target.

    V4h correctly separates practical resistance from distant historical levels,
    but older plan fields may still carry the distant ATH into target_2.  This
    keeps the historical level as context only and replaces the practical target
    with a nearer ATR/extension target.
    """
    try:
        if not isinstance(stock, dict):
            return
        if not bool(stock.get("major_resistance_is_distant", False)) and not bool(stock.get("price_discovery_zone", False)):
            return
        current = float(stock.get("display_price", stock.get("current_price_live", stock.get("current_price", 0))) or 0)
        target_1 = float(stock.get("target_1", stock.get("display_target_price", 0)) or 0)
        target_2 = float(stock.get("target_2", 0) or 0)
        major = float(stock.get("major_resistance", 0) or 0)
        if current <= 0 or target_2 <= 0:
            return
        # If target_2 is effectively the distant historical level, it is not a
        # near-term execution target.  Keep it in distant_resistance_context.
        too_far = target_2 >= current * 1.35 or (major and abs(target_2 - major) / max(major, 1.0) <= 0.03 and major >= current * 1.35)
        if not too_far:
            return
        atr_t2 = float(stock.get("atr_target_2_suggestion", 0) or 0)
        replacement = 0.0
        if atr_t2 > max(target_1, current) and atr_t2 < target_2:
            replacement = atr_t2
        elif target_1 > current:
            replacement = target_1 * 1.06
        else:
            replacement = current * 1.10
        # Keep the practical target from becoming unrealistically far.
        replacement = min(replacement, current * 1.22)
        if replacement <= current or replacement >= target_2:
            return
        stock["target_2_before_distant_cap"] = target_2
        stock["target_2"] = round(replacement, 2)
        stock["distant_resistance_context"] = major or target_2
        stock["target_2_capped_due_to_distant_resistance"] = True
        note = "المقاومة التاريخية البعيدة تُعرض كسياق فقط وليست هدفًا مباشرًا"
        stock["risk_flags"] = _dedupe_text_list(list(stock.get("risk_flags") or []) + [note], 12)
        notes = list(stock.get("level_refinement_notes") or [])
        notes.append(note)
        stock["level_refinement_notes"] = _dedupe_text_list(notes, 8)
        if major:
            stock["major_resistance_label"] = f"مقاومة تاريخية بعيدة قرب {safe_round(major)} — ليست هدفًا مباشرًا"
    except Exception:
        return



def _early_movement_fast_lane_reasons(stock: dict, current_decision: str, no_chase: bool, high_risk_reasons=None) -> list[str]:
    """Wealth Builder V1c: promote clean early-movement confirmations to Cautious.

    This is a limited pre-open/live-market bridge, not a full Source V2 rewrite.
    It only upgrades Monitoring -> Cautious when the stock is already in the
    Early Movement layer and receives live confirmation without no-chase, weak
    liquidity, or resistance problems.  It never upgrades to Strong.
    """
    try:
        if str(current_decision or "") != "مراقبة":
            return []
        if bool(no_chase):
            return []
        if high_risk_reasons:
            return []
        phase = str(stock.get("market_phase", "") or "").strip()
        # Do not promote from a closed-market snapshot.  The Monday test should
        # happen only when pre-market/open/after-hours prices are actually moving.
        if phase not in {"pre_market", "open", "after_hours"}:
            return []
        em = stock.get("early_movement") or {}
        if not bool(em.get("in_early_movement", False)):
            return []
        em_status = str(em.get("status", "") or stock.get("early_movement_status", "") or "")
        if em_status not in {"priority_watch", "confirmed_watch"}:
            return []
        # Source / Early Discovery V2: Pre-Move Watch is not an immediate entry
        # list.  The old fast-lane bridge may only promote rows that the V2 stage
        # classifier says are already Early Confirmation or Active Breakout.
        move_stage = str(stock.get("move_stage", "") or (stock.get("move_stage_v2") or {}).get("move_stage", "") or "")
        if move_stage not in {"Early Confirmation", "Active Breakout"}:
            return []
        if float(stock.get("gain_at_detection", stock.get("display_change_pct", 0)) or 0) >= 10:
            return []

        # Basic quality/readiness gates.  These are intentionally below Strong
        # thresholds because the target is Cautious/Close Watch, not Strong.
        quality = float(stock.get("quality_score", 0) or 0)
        readiness = float(stock.get("execution_readiness_score", 0) or 0)
        rr = float(stock.get("rr_1", 0) or 0)
        volume = float(stock.get("effective_volume_ratio", stock.get("volume_pace_ratio", stock.get("volume_ratio", 0))) or 0)
        res_dist = float(stock.get("nearest_resistance_distance_pct", 999) or 999)
        liq_status = str(stock.get("liquidity_persistence_status", "") or "")
        post_status = str(stock.get("post_activation_guard_status", "") or "")
        close_res = bool(stock.get("close_resistance_guard_flag", False))

        if quality < 62 or readiness < 50 or rr < 0.75 or volume < 1.0:
            return []
        if liq_status in {"weak", "fade", "fading"}:
            return []
        if post_status in {"weak", "failed", "danger"}:
            return []
        if close_res or (0 <= res_dist <= 1.0):
            return []

        reasons = ["مرشح مراقبة حركة مبكرة أكد حيًا"]
        if str(em.get("source", "") or "") in {"both", "weekly_priority"}:
            reasons.append("داخل قائمة Polygon الأسبوعية")
        if str(em.get("source", "") or "") in {"both", "auto_detected"}:
            reasons.append("اكتشاف تلقائي مطابق للنمط")
        if volume >= 1.15:
            reasons.append("سيولة داعمة")
        if readiness >= 55:
            reasons.append("جاهزية تنفيذ مقبولة")
        return reasons[:6]
    except Exception:
        return []


def _true_upward_chase_context(stock: dict) -> bool:
    """Return True only when current price is actually extended upward now.

    Historical peak or old move-stage alone is not enough.  This prevents stale
    No-Chase labels on symbols that are now red, broken, or waiting for reclaim.
    """
    try:
        current_gain = float(
            stock.get("current_gain", stock.get("display_change_pct", stock.get("live_change_pct", stock.get("change_pct", 0)))) or 0
        )
    except Exception:
        current_gain = 0.0
    try:
        price = float(stock.get("current_price_live", stock.get("display_price", stock.get("price", 0))) or 0)
    except Exception:
        price = 0.0
    try:
        entry = float(stock.get("display_entry_price", stock.get("entry_price", stock.get("entry", 0))) or 0)
    except Exception:
        entry = 0.0
    try:
        entry_dist = ((price - entry) / entry * 100.0) if price > 0 and entry > 0 else 999.0
    except Exception:
        entry_dist = 999.0
    return bool(
        current_gain >= 7.0
        or (current_gain >= 3.0 and entry_dist >= 2.5)
        or (price > 0 and entry > 0 and price >= entry * 1.035 and current_gain >= 2.0)
    )

def _post_early_movement_decision_safety(results):
    """Apply final decision caps after Early Movement classification.

    The first decision pass happens before Early Movement metadata exists.  A
    stock can therefore become `early_movement.status=no_chase` after it was
    already labelled Strong.  This final pass is intentionally light: it does
    not change scores, does not fetch data, and only caps the final decision
    label/owner action so the UI never says `دخول قوي` and `لا تطارد` on the
    same card.
    """
    out = []
    for stock in list(results or []):
        if not isinstance(stock, dict):
            out.append(stock)
            continue
        original_decision = str(stock.get("decision", "مراقبة") or "مراقبة")
        try:
            new_decision, safety_reasons = apply_safety_decision_guard(stock, original_decision)
        except Exception as exc:
            new_decision, safety_reasons = original_decision, [f"تعذر تطبيق سقف القرار النهائي: {type(exc).__name__}"]

        # Early Movement No-Chase is a hard display cap even if the regular
        # scoring safety gate did not see a severe enough combination.
        em = stock.get("early_movement") or {}
        move_stage_v2_name = str((stock.get("move_stage_v2") or {}).get("move_stage") or stock.get("move_stage") or "")
        v2_no_chase_stage = move_stage_v2_name in {"No-Chase", "Extended", "Catalyst Spike Review"}
        raw_no_chase = (
            str(em.get("status", "") or "") == "no_chase"
            or str(stock.get("no_chase_guard_status", "") or "") == "no_chase"
            or "لا تطارد" in str(stock.get("no_chase_guard_label", "") or "")
            or v2_no_chase_stage
            or str(stock.get("source_promotion_v2_status", "") or "") == "hard_no_chase_cap"
        )
        true_chase_context = _true_upward_chase_context(stock)
        no_chase = bool(raw_no_chase and true_chase_context)
        if raw_no_chase and not no_chase:
            stock["stale_no_chase_suppressed_by_contract_v1a"] = True
            stock["stale_no_chase_original_stage"] = move_stage_v2_name
            if str(stock.get("no_chase_guard_status", "") or "") == "no_chase":
                stock["no_chase_guard_status"] = "not_no_chase"
                stock["no_chase_guard_label"] = "بانتظار تقييم الخطة الحالية"
        em_reasons = [str(x) for x in (em.get("no_chase_reasons") or stock.get("no_chase_guard_reasons") or []) if str(x).strip()]
        if no_chase:
            if original_decision == "دخول قوي":
                new_decision = "دخول بحذر"
            # If it is both no-chase and literally sitting on resistance, keep
            # it as monitoring only until a pullback/reclaim is confirmed.
            try:
                res_dist = float(stock.get("nearest_resistance_distance_pct", 999) or 999)
            except Exception:
                res_dist = 999.0
            if res_dist <= 0.75 or any("مقاومة" in r for r in em_reasons):
                new_decision = "مراقبة"
            safety_reasons = list(safety_reasons or []) + ["No-Chase يمنع الدخول القوي"] + em_reasons[:3]
            stock["tier_cap_applied"] = True
            stock["no_chase_hard_cap"] = True
            stock["tier_cap_reasons"] = _dedupe_text_list(list(stock.get("tier_cap_reasons") or []) + ["No-Chase يمنع الدخول القوي"] + em_reasons, 8)
            stock["no_chase_guard_status"] = "no_chase"
            stock["no_chase_guard_label"] = stock.get("no_chase_guard_label") or "⛔ لا تطارد"
            stock["execution_gate_status"] = "no_chase"
            stock["execution_gate_label"] = "⛔ لا تطارد — انتظر pullback صحي أو إعادة تمركز"

        # Wealth Builder V1b: a high-risk Strong with weak liquidity or weak
        # post-activation confirmation is not a clean Strong.  This fixes the
        # remaining cases where cards said "دخول قوي عالي المخاطرة" and still
        # stayed in the Strong bucket.
        high_risk_tier = str(stock.get("strong_entry_tier", "") or "") == "high_risk"
        liq_status = str(stock.get("liquidity_persistence_status", "") or "")
        post_status = str(stock.get("post_activation_guard_status", "") or "")
        liq_label = str(stock.get("liquidity_persistence_label", "") or "")
        high_risk_reasons = []
        if high_risk_tier:
            if liq_status in {"weak", "fade", "fading"} or "غير مؤكدة" in liq_label or "ضعفت" in liq_label:
                high_risk_reasons.append("السيولة غير مؤكدة أو ضعفت")
            if post_status in {"weak", "failed", "danger"}:
                high_risk_reasons.append("تأكيد ما بعد التفعيل ضعيف")
            if str(stock.get("execution_gate_status", "") or "") == "wait_liquidity":
                high_risk_reasons.append("ينتظر تأكيد السيولة")
        if original_decision == "دخول قوي" and high_risk_reasons:
            # If both the liquidity and post-activation guards are weak, keep it
            # as monitoring only.  If only one guard is weak, cap to cautious.
            new_decision = "مراقبة" if len(set(high_risk_reasons)) >= 2 else "دخول بحذر"
            safety_reasons = list(safety_reasons or []) + high_risk_reasons
            stock["tier_cap_applied"] = True
            stock["high_risk_strong_cap"] = True
            stock["tier_cap_reasons"] = _dedupe_text_list(list(stock.get("tier_cap_reasons") or []) + high_risk_reasons, 8)
            stock["execution_gate_status"] = "wait_liquidity"
            stock["execution_gate_label"] = "⏳ انتظر تأكيد السيولة والثبات قبل الدخول"

        # Wealth Builder V1c: clean Early Movement Fast Lane.
        # If Strong is zero, users still need a disciplined path to watch early
        # candidates.  This promotes only clean, live-confirmed early movement
        # names from Monitoring to Cautious.  It never promotes to Strong.
        fast_lane_reasons = []
        try:
            fast_lane_reasons = _early_movement_fast_lane_reasons(stock, new_decision, no_chase, high_risk_reasons)
        except Exception:
            fast_lane_reasons = []
        if fast_lane_reasons and new_decision == "مراقبة":
            new_decision = "دخول بحذر"
            stock["early_movement_fast_lane_applied"] = True
            stock["early_movement_fast_lane_version"] = "wealth_builder_v1c_live_cautious_only"
            stock["early_movement_fast_lane_reasons"] = _dedupe_text_list(fast_lane_reasons, 8)
            stock["execution_gate_status"] = "early_movement_cautious"
            stock["execution_gate_label"] = "🟠 مراقبة مبكرة مؤكدة — دخول بحذر فقط مع الالتزام بالشروط"

        if new_decision != original_decision:
            stock["decision_before_final_cap"] = original_decision
            stock["decision"] = new_decision
            stock["safety_gate_reasons"] = _dedupe_text_list(list(stock.get("safety_gate_reasons") or []) + list(safety_reasons or []), 10)
            try:
                stock["execution_status"] = compute_execution_status(
                    str(stock.get("type", stock.get("trade_type", "")) or ""),
                    new_decision,
                    str(stock.get("trend", "") or ""),
                    float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)) or 0),
                    float(stock.get("catalyst_score", 0) or 0),
                    str(stock.get("breakout_quality", "") or ""),
                )
            except Exception:
                pass

        if no_chase:
            stock["owner_action"] = "⛔ لا تطارد الآن — انتظر pullback صحي أو اختراق/ثبات جديد بسيولة قبل أي دخول."
            stock["execution_status_ar"] = "لا تطارد ⛔"
            stock["execution_readiness_label"] = "لا تطارد"
            stock["execution_readiness_icon"] = "⛔"
        elif original_decision == "دخول قوي" and high_risk_reasons:
            stock["owner_action"] = "⚠️ ليست دخولًا قويًا نظيفًا الآن — انتظر تأكيد السيولة والثبات بعد التفعيل قبل أي دخول."
            stock["execution_status_ar"] = "انتظار تأكيد ⚠️"
            stock["execution_readiness_label"] = "انتظار تأكيد"
            stock["execution_readiness_icon"] = "⚠️"
        elif stock.get("early_movement_fast_lane_applied"):
            stock["owner_action"] = "🟠 ترقية مراقبة مبكرة إلى دخول بحذر — لا تدخل إلا مع استمرار السيولة والثبات وعدم المطاردة."
            stock["execution_status_ar"] = "دخول بحذر 🟠"
            stock["execution_readiness_label"] = "دخول بحذر"
            stock["execution_readiness_icon"] = "🟠"
        elif new_decision != original_decision:
            try:
                stock["owner_action"] = owner_decision(
                    new_decision,
                    str(stock.get("trend", "") or ""),
                    str(stock.get("breakout_quality", "") or ""),
                    float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)) or 0),
                    float(stock.get("catalyst_score", 0) or 0),
                )
            except Exception:
                pass

        _cap_distant_historical_target(stock)
        stock["owner_action"] = _clean_owner_action_text(stock.get("owner_action", ""), str(stock.get("decision", new_decision) or new_decision))

        out.append(stock)
    return out


def _build_trade_scan_response(results, scan_debug, include_all: bool = False, cache_hit: bool = False, cache_age_sec=None, payload_note: str = ""):
    results = _apply_manual_sharia_overrides(list(results or []))
    try:
        results = enrich_opportunity_intelligence_bulk(results)
    except Exception:
        pass
    try:
        results = enrich_rows_with_detection_journal(results, source_layer="trade_scan_response")
    except Exception:
        pass
    try:
        results = [enrich_row_pre_move(x) if isinstance(x, dict) else x for x in results]
    except Exception:
        pass
    try:
        results = enrich_stocks_with_early_movement(results)
    except Exception:
        pass
    try:
        results = enrich_rows_source_promotion_v2a(results)
    except Exception:
        pass
    try:
        results = enrich_rows_source_promotion_v2(results)
    except Exception:
        pass
    try:
        results = _post_early_movement_decision_safety(results)
    except Exception:
        pass
    try:
        results = apply_final_decisions(results)
    except Exception:
        pass
    try:
        results = enrich_rows_early_watch_lifecycle(results)
    except Exception:
        pass
    try:
        results = enrich_breakout_quality_rows(results)
    except Exception:
        pass
    try:
        results = apply_breakout_guard_to_rows(results)
    except Exception:
        pass
    try:
        results = enrich_rows_with_active_plan_status(results)
    except Exception:
        pass
    phase = get_market_phase()
    try:
        results = enrich_rows_with_opportunity_plan_memory(results)
    except Exception:
        pass
    try:
        results = enrich_rows_opportunity_radar(results, market_phase=phase)
    except Exception:
        pass
    early_movement_payload = build_early_movement_sections(results)
    try:
        opportunity_radar_payload = build_opportunity_radar_sections(results, market_phase=phase)
    except Exception as exc:
        opportunity_radar_payload = {"ok": False, "version": OPPORTUNITY_RADAR_VERSION, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
    scan_debug = dict(scan_debug or {})
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
    early_movement_watchlist = early_movement_payload.get("early_movement_watchlist", [])
    support_bounce_candidates = opportunity_radar_payload.get("support_bounce_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    reclaim_candidates = opportunity_radar_payload.get("reclaim_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    pre_trigger_candidates = opportunity_radar_payload.get("pre_trigger_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    continuation_pullback_candidates = opportunity_radar_payload.get("continuation_pullback_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    high_risk_day_trades = opportunity_radar_payload.get("high_risk_day_trades", []) if isinstance(opportunity_radar_payload, dict) else []
    low_float_premarket_radar = opportunity_radar_payload.get("low_float_premarket_radar", []) if isinstance(opportunity_radar_payload, dict) else []
    low_float_fast_lane_raw_watch = opportunity_radar_payload.get("low_float_fast_lane_raw_watch", []) if isinstance(opportunity_radar_payload, dict) else []
    gap_fill_watch = opportunity_radar_payload.get("gap_fill_watch", []) if isinstance(opportunity_radar_payload, dict) else []
    catalyst_watch = opportunity_radar_payload.get("catalyst_watch", []) if isinstance(opportunity_radar_payload, dict) else []
    learning_opportunity_candidates = opportunity_radar_payload.get("learning_opportunity_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    promotion_bridge_candidates = opportunity_radar_payload.get("promotion_bridge_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    live_tight_monitoring_candidates = opportunity_radar_payload.get("live_tight_monitoring_candidates", []) if isinstance(opportunity_radar_payload, dict) else []
    critical_pre_explosion_watch = opportunity_radar_payload.get("critical_pre_explosion_watch", []) if isinstance(opportunity_radar_payload, dict) else []

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
        "early_movement_count": int(early_movement_payload.get("count", 0) or 0),
        "early_movement_weekly_priority_count": int(early_movement_payload.get("weekly_priority_count", 0) or 0),
        "early_movement_auto_detected_count": int(early_movement_payload.get("auto_detected_count", 0) or 0),
        "early_movement_priority_watch_count": int(early_movement_payload.get("priority_watch_count", 0) or 0),
        "early_movement_fast_lane_count": len([x for x in results if isinstance(x, dict) and x.get("early_movement_fast_lane_applied")]),
        "opportunity_radar": opportunity_radar_payload,
        "opportunity_radar_version": OPPORTUNITY_RADAR_VERSION,
        "promotion_bridge_debug": opportunity_radar_payload.get("promotion_bridge_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "promotion_bridge_rule_ar": opportunity_radar_payload.get("promotion_bridge_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "promotion_bridge_candidates_count": len(promotion_bridge_candidates),
        "promotion_bridge_candidates": promotion_bridge_candidates[:limit] if 'limit' in locals() else promotion_bridge_candidates[:25],
        "live_tight_monitoring_debug": opportunity_radar_payload.get("live_tight_monitoring_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "live_tight_monitoring_rule_ar": opportunity_radar_payload.get("live_tight_monitoring_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "live_tight_monitoring_candidates_count": len(live_tight_monitoring_candidates),
        "live_tight_monitoring_candidates": live_tight_monitoring_candidates[:limit] if 'limit' in locals() else live_tight_monitoring_candidates[:25],
        "critical_pre_explosion_watch_debug": opportunity_radar_payload.get("prepared_watch_ui_bridge_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "critical_pre_explosion_watch_rule_ar": opportunity_radar_payload.get("prepared_watch_ui_bridge_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "critical_pre_explosion_watch_count": len(critical_pre_explosion_watch),
        "critical_pre_explosion_watch": critical_pre_explosion_watch[:limit] if 'limit' in locals() else critical_pre_explosion_watch[:25],
        "learning_overlay_summary": opportunity_radar_payload.get("learning_overlay_summary", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "learning_overlay_candidates": opportunity_radar_payload.get("learning_overlay_candidates", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "learning_overlay_candidates_count": int(opportunity_radar_payload.get("learning_overlay_candidates_count", 0) or 0) if isinstance(opportunity_radar_payload, dict) else 0,
        "next_week_analysis": opportunity_radar_payload.get("next_week_analysis", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "next_week_watchlist": opportunity_radar_payload.get("next_week_watchlist", []) if isinstance(opportunity_radar_payload, dict) else [],
        "next_week_analysis_count": int(opportunity_radar_payload.get("next_week_analysis_count", 0) or 0) if isinstance(opportunity_radar_payload, dict) else 0,
        "learning_bridge_debug": opportunity_radar_payload.get("learning_bridge_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "learning_bridge_rule_ar": opportunity_radar_payload.get("learning_bridge_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "low_float_capture_debug": opportunity_radar_payload.get("low_float_capture_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "low_float_capture_rule_ar": opportunity_radar_payload.get("low_float_capture_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "fast_lane_funnel_debug": opportunity_radar_payload.get("fast_lane_funnel_debug", {}) if isinstance(opportunity_radar_payload, dict) else {},
        "fast_lane_funnel_rule_ar": opportunity_radar_payload.get("fast_lane_funnel_rule_ar", "") if isinstance(opportunity_radar_payload, dict) else "",
        "low_float_fast_lane_raw_watch_count": len(low_float_fast_lane_raw_watch),
        "low_float_fast_lane_raw_watch": low_float_fast_lane_raw_watch[:limit] if 'limit' in locals() else low_float_fast_lane_raw_watch[:25],
        "learning_opportunity_candidates_count": len(learning_opportunity_candidates),
        "learning_opportunity_candidates": learning_opportunity_candidates[:25],
        "live_tight_monitoring_candidates_count": len(live_tight_monitoring_candidates),
        "support_bounce_candidates_count": len(support_bounce_candidates),
        "reclaim_candidates_count": len(reclaim_candidates),
        "pre_trigger_candidates_count": len(pre_trigger_candidates),
        "continuation_pullback_candidates_count": len(continuation_pullback_candidates),
        "high_risk_day_trades_count": len(high_risk_day_trades),
        "low_float_premarket_radar_count": len(low_float_premarket_radar),
        "low_float_fast_lane_raw_watch_count": len(low_float_fast_lane_raw_watch),
        "gap_fill_watch_count": len(gap_fill_watch),
        "catalyst_watch_count": len(catalyst_watch),
        "source_promotion_v2a": summarize_source_promotion_v2a(results),
        "source_promotion_v2a_promoted_count": len([x for x in results if isinstance(x, dict) and x.get("source_promotion_v2a_promoted")]),
        "source_early_discovery_v2": summarize_source_promotion_v2(results),
        "early_watch_lifecycle": summarize_early_watch_lifecycle(results),
        "detection_journal": detection_journal_status(limit=12),
        "manual_sharia_exclusions_count": len(load_manual_sharia_exclusions()),
        "manual_sharia_approvals_count": len(load_manual_sharia_approvals()),
        "strong_entries": strong[:25],
        "top_ranked": strong[:25],
        "cautious_entries": cautious[:25],
        "gray_strong": gray_strong[:25],
        "premarket_setups": premarket_setups[:25],
        "watchlist": watch[:50],
        "early_movement_watchlist": early_movement_watchlist[:30],
        "live_tight_monitoring_candidates": live_tight_monitoring_candidates[:25],
        "support_bounce_candidates": support_bounce_candidates[:25],
        "reclaim_candidates": reclaim_candidates[:25],
        "pre_trigger_candidates": pre_trigger_candidates[:25],
        "continuation_pullback_candidates": continuation_pullback_candidates[:25],
        "high_risk_day_trades": high_risk_day_trades[:25],
        "low_float_premarket_radar": low_float_premarket_radar[:25],
        "low_float_fast_lane_raw_watch": low_float_fast_lane_raw_watch[:25],
        "gap_fill_watch": gap_fill_watch[:25],
        "catalyst_watch": catalyst_watch[:25],
        "early_movement": early_movement_payload,
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
            "low_float_fast_lane_count": int(scan_debug.get("dynamic_low_float_fast_lane_count", 0) or 0),
            "low_float_fast_lane": scan_debug.get("dynamic_low_float_fast_lane", {}),
            "low_float_fast_lane_funnel_debug": scan_debug.get("dynamic_low_float_fast_lane_funnel_debug", {}),
            "live_ignition_hot_lane_count": int(scan_debug.get("dynamic_live_ignition_hot_lane_count", 0) or 0),
            "pre_move_engine_v2_count": int(scan_debug.get("dynamic_pre_move_engine_v2_count", 0) or 0),
            "late_mover_review_count": int(scan_debug.get("dynamic_late_mover_review_count", 0) or 0),
            "next_scan_interval_sec": int(scan_debug.get("dynamic_next_scan_interval_sec", 0) or 0),
            "source_bucket_counts": scan_debug.get("dynamic_source_bucket_counts", {}),
            "price_under_2_deprioritized": int(scan_debug.get("dynamic_price_under_2_deprioritized", 0) or 0),
            "price_under_2_exception": int(scan_debug.get("dynamic_price_under_2_exception", 0) or 0),
            "price_over_300_deprioritized": int(scan_debug.get("dynamic_price_over_300_deprioritized", 0) or 0),
            # V2R1b: expose the micro-explosion close-watch pipeline in trade-scan
            # so we can audit whether candidates are detected, persisted, and
            # re-injected before open / during session / after hours.
            "micro_explosion_capture_count": int(scan_debug.get("dynamic_micro_explosion_capture_count", 0) or 0),
            "micro_explosion_capture_symbols": scan_debug.get("dynamic_micro_explosion_capture_symbols", []),
            "micro_explosion_capture_debug": scan_debug.get("dynamic_micro_explosion_capture_debug", {}),
            "micro_explosion_full_market_scan": scan_debug.get("dynamic_micro_explosion_full_market_scan", {}),
            "micro_explosion_close_watch_count": int(scan_debug.get("dynamic_micro_explosion_close_watch_count", 0) or 0),
            "micro_explosion_close_watch_memory": scan_debug.get("dynamic_micro_explosion_close_watch_memory", {}),
            "micro_explosion_seed_confirm_count": int(scan_debug.get("dynamic_micro_explosion_seed_confirm_count", 0) or 0),
            "big_explosion_live_count": int(scan_debug.get("dynamic_big_explosion_live_count", 0) or 0),
            "big_explosion_live_symbols": scan_debug.get("dynamic_big_explosion_live_symbols", []),
            "big_explosion_live_debug": scan_debug.get("dynamic_big_explosion_live_debug", {}),
            "live_tight_monitoring_v2v_count": int(scan_debug.get("dynamic_live_tight_monitoring_v2v_count", 0) or 0),
            "live_tight_monitoring_v2v_symbols": scan_debug.get("dynamic_live_tight_monitoring_v2v_symbols", []),
            "live_tight_monitoring_v2v_memory": scan_debug.get("dynamic_live_tight_monitoring_v2v_memory", {}),
            "live_tight_monitoring_v2v_rule_ar": scan_debug.get("dynamic_live_tight_monitoring_v2v_rule_ar", ""),
            "elapsed_sec": scan_debug.get("dynamic_discovery_elapsed_sec", None),
        },
        "full_market_scan_status": {
            "enabled": bool(dynamic_discovery_enabled()),
            "last_scan_at": scan_debug.get("updated_at", "") or scan_debug.get("scan_updated_at", ""),
            "next_scan_interval_sec": _server_full_market_scan_interval_sec(phase),
            "source": "server_background_worker_and_snapshot",
        },
    }

    try:
        out["plan_ledger"] = record_active_strong_plans(strong, source="trade_scan_full" if not cache_hit else "trade_scan_cache")
    except Exception as exc:
        out["plan_ledger"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        memory_rows = []
        for bucket_rows in [strong, cautious, pre_trigger_candidates, support_bounce_candidates, reclaim_candidates, continuation_pullback_candidates, low_float_premarket_radar, high_risk_day_trades]:
            memory_rows.extend(bucket_rows or [])
        out["opportunity_plan_memory"] = record_opportunity_plans(memory_rows, source="trade_scan_full" if not cache_hit else "trade_scan_cache")
    except Exception as exc:
        out["opportunity_plan_memory"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        weekly_rows_for_lifecycle = ((load_weekly_watchlist() or {}).get("candidates") or [])
        out["weekly_plan_lifecycle"] = evaluate_weekly_rows(weekly_rows_for_lifecycle, source="trade_scan_full" if not cache_hit else "trade_scan_cache")
    except Exception as exc:
        weekly_rows_for_lifecycle = []
        out["weekly_plan_lifecycle"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        out["paper_trading"] = process_paper_trading_scan(strong_rows=strong, cautious_rows=cautious, watch_rows=watch, weekly_rows=weekly_rows_for_lifecycle, source="trade_scan_full" if not cache_hit else "trade_scan_cache")
    except Exception as exc:
        out["paper_trading"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

    try:
        out["telegram_alerts"] = maybe_send_buy_now_alerts(strong, source="trade_scan_full" if not cache_hit else "trade_scan_cache")
    except Exception as exc:
        out["telegram_alerts"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}

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


@app.get("/diagnostics/intraday-early-source-radar")
def diagnostics_intraday_early_source_radar():
    status = get_last_intraday_early_source_radar_status()
    return {
        "ok": True,
        "status_available": bool(status),
        "status": status if isinstance(status, dict) else {},
        "notes_ar": "رادار مصدر مبكر فقط. يضيف مرشحين للمنبع ولا يغير قرار الدخول النهائي ولا يرسل Telegram.",
    }


@app.get("/diagnostics/source-promotion-v2a")
def diagnostics_source_promotion_v2a(format: str = "json"):
    snap = get_json("last_trade_scan_snapshot", {}) or {}
    rows = snap.get("rows", []) if isinstance(snap, dict) else []
    try:
        rows = _apply_manual_sharia_overrides(list(rows or []))
        rows = enrich_opportunity_intelligence_bulk(rows)
        rows = enrich_rows_with_detection_journal(rows, source_layer="diagnostics_source_promotion")
        rows = [enrich_row_pre_move(x) if isinstance(x, dict) else x for x in rows]
        rows = enrich_stocks_with_early_movement(rows)
        rows = enrich_rows_source_promotion_v2a(rows)
        rows = enrich_rows_source_promotion_v2(rows)
        rows = _post_early_movement_decision_safety(rows)
        rows = apply_final_decisions(rows)
    except Exception:
        pass
    return build_source_promotion_v2a_report(rows, format=format)


@app.get("/diagnostics/source-early-discovery-v2")
def diagnostics_source_early_discovery_v2(limit: int = 50):
    snap = get_json("last_trade_scan_snapshot", {}) or {}
    rows = snap.get("rows", []) if isinstance(snap, dict) else []
    try:
        rows = _apply_manual_sharia_overrides(list(rows or []))
        rows = enrich_opportunity_intelligence_bulk(rows)
        rows = enrich_rows_with_detection_journal(rows, source_layer="diagnostics_source_early_discovery_v2")
        rows = [enrich_row_pre_move(x) if isinstance(x, dict) else x for x in rows]
        rows = enrich_stocks_with_early_movement(rows)
        rows = enrich_rows_source_promotion_v2a(rows)
        rows = enrich_rows_source_promotion_v2(rows)
        rows = _post_early_movement_decision_safety(rows)
        rows = apply_final_decisions(rows)
    except Exception:
        pass
    safe_limit = max(1, min(int(limit or 50), 100))
    return {
        "ok": True,
        "summary": summarize_source_promotion_v2(rows),
        "detection_journal": detection_journal_status(limit=safe_limit),
        "rows_sample": [
            {
                "symbol": x.get("symbol"),
                "decision": x.get("decision"),
                "move_stage": x.get("move_stage"),
                "move_stage_label": x.get("move_stage_label"),
                "gain_at_detection": x.get("gain_at_detection"),
                "current_gain": x.get("current_gain"),
                "peak_gain_seen": x.get("peak_gain_seen"),
                "intraday_peak_gain": x.get("intraday_peak_gain"),
                "max_gain_basis": x.get("max_gain_basis"),
                "late_seen_flag": x.get("late_seen_flag"),
                "late_seen_time": x.get("late_seen_time"),
                "journal_current_gain": x.get("journal_current_gain"),
                "journal_recorded_current_gain": x.get("journal_recorded_current_gain"),
                "journal_current_gain_applied": x.get("journal_current_gain_applied"),
                "first_detected_time": x.get("first_detected_time"),
                "early_movement_active": x.get("early_movement_active"),
                "source_promotion_v2_status": x.get("source_promotion_v2_status"),
                "source_promotion_v2_list": x.get("source_promotion_v2_list"),
                "final_decision_code": x.get("final_decision_code"),
                "final_decision_label": x.get("final_decision_label"),
                "final_decision_blockers": x.get("final_decision_blockers"),
                "owner_action": x.get("owner_action"),
            }
            for x in (rows or [])[:safe_limit]
            if isinstance(x, dict)
        ],
    }


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


@app.get("/diagnostics/decision-contract/symbol")
def diagnostics_decision_contract_symbol(symbol: str):
    """Explain one symbol through the unified price/plan/final decision contract."""
    payload = build_single_stock_response(symbol)
    plan = payload.get("trade_plan") if isinstance(payload, dict) else {}
    diag = compact_decision_diagnostics(plan or {"symbol": symbol})
    return {
        "ok": True,
        "symbol": str(symbol or "").upper().strip(),
        "overview_error": (payload or {}).get("overview_error") if isinstance(payload, dict) else None,
        "trade_error": (payload or {}).get("trade_error") if isinstance(payload, dict) else None,
        "diagnostics": diag,
        "trade_plan_compact": {
            "decision": (plan or {}).get("decision") if isinstance(plan, dict) else None,
            "final_decision_code": (plan or {}).get("final_decision_code") if isinstance(plan, dict) else None,
            "final_decision_label": (plan or {}).get("final_decision_label") if isinstance(plan, dict) else None,
            "display_price": (plan or {}).get("display_price") if isinstance(plan, dict) else None,
            "display_change_pct": (plan or {}).get("display_change_pct") if isinstance(plan, dict) else None,
            "hide_plan_numbers": bool((plan or {}).get("hide_plan_numbers")) if isinstance(plan, dict) else False,
            "display_entry_price": None if isinstance(plan, dict) and plan.get("hide_plan_numbers") else ((plan or {}).get("display_entry_price") if isinstance(plan, dict) else None),
            "display_target_price": None if isinstance(plan, dict) and plan.get("hide_plan_numbers") else ((plan or {}).get("display_target_price") if isinstance(plan, dict) else None),
            "display_stop_price": None if isinstance(plan, dict) and plan.get("hide_plan_numbers") else ((plan or {}).get("display_stop_price") if isinstance(plan, dict) else None),
            "owner_action": (plan or {}).get("owner_action") if isinstance(plan, dict) else None,
        },
    }


@app.get("/diagnostics/quote-resolver/symbol")
def diagnostics_quote_resolver_symbol(symbol: str, prefer_cache: bool = False, allow_fallback: bool = True):
    """Show the single-source quote contract: FMP first, Polygon delayed fallback second."""
    sym = normalize_symbol_text(symbol)
    phase = get_market_phase()
    if not sym:
        return {"ok": False, "error": "missing_symbol"}
    quote = resolve_symbol_quote(sym, phase=phase, prefer_cache=bool(prefer_cache), allow_fallback=bool(allow_fallback))
    return {
        "ok": True,
        "symbol": sym,
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "quote": quote,
        "rule_ar": "FMP أولًا. إذا لم يكتمل السعر من FMP، تُستخدم Polygon كاحتياط متأخر حوالي 15 دقيقة ومراقبة فقط لا تنفيذ مباشر.",
    }


@app.get("/diagnostics/scan-cadence")
def diagnostics_scan_cadence():
    """Explain the new safe scan cadence plan."""
    phase = get_market_phase()
    status = get_json("live_radar_worker_status", {}) or {}
    snapshot = get_json("last_trade_scan_snapshot", {}) or {}
    age_sec = _parse_scan_snapshot_age_sec(snapshot) if isinstance(snapshot, dict) else None
    interval = _server_full_market_scan_interval_sec(phase)
    return {
        "ok": True,
        "version": "scan_cadence_v1_fast_light_plus_deep_safe_2026_06_05",
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "full_scan_interval_sec": int(interval),
        "live_price_refresh_sec": int(LIVE_RADAR_PRICE_REFRESH_SEC),
        "snapshot_age_sec": age_sec,
        "worker": status if isinstance(status, dict) else {},
        "design_ar": [
            "تحديث السعر سريع للأسهم الموجودة بدون كاش أثناء السوق النشط.",
            "المسح العميق لا يعمل كل دقيقة حتى لا يضغط FMP/Railway؛ يعمل حسب المرحلة.",
            "المرشحات المبكرة وWeekly Priority تحصل على متابعة لصيقة عبر Early Watch Lifecycle.",
            "أي ترقية إلى دخول بحذر/قوي تمر عبر Decision Contract وFinal Decision فقط.",
        ],
    }


@app.get("/polygon-weekly/status")
def polygon_weekly_status():
    data = load_weekly_watchlist()
    return {
        "ok": True,
        "version": POLYGON_WEEKLY_BUILDER_VERSION,
        "watchlist": data,
        "rule_ar": "ملفات Polygon الدقيقة/اليومية تستخدم مؤقتًا للتحليل فقط. الناتج المختصر هو الذي يُحفظ، لا الملفات الخام.",
    }


@app.get("/polygon-weekly/flatfiles-status")
def polygon_weekly_flatfiles_status():
    """Show whether direct Massive/Polygon Flat Files pull is safely configured."""
    return polygon_flatfile_status()


@app.get("/polygon-weekly/build-from-local")
def polygon_weekly_build_from_local(
    path: str = "",
    minute_path: str = "",
    daily_path: str = "",
    top_n: int = 15,
    execute: bool = False,
):
    """Build a compact weekly list from temporary local CSV/CSV.GZ/ZIP paths.

    Prefer minute_path + daily_path so the builder does not guess from file names.
    This endpoint never stores raw minute files; execute=true stores only compact JSON.
    """
    try:
        if minute_path or daily_path:
            return build_weekly_candidates_from_paths(
                minute_path=minute_path or None,
                daily_path=daily_path or None,
                top_n=int(top_n or 15),
                execute=bool(execute),
            )
        return build_weekly_candidates_from_path(path=path or "app_data/polygon_weekly_input.zip", top_n=int(top_n or 15), execute=bool(execute))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "path": path, "minute_path": minute_path, "daily_path": daily_path}


@app.get("/polygon-weekly/build-from-polygon")
def polygon_weekly_build_from_polygon(
    trade_date: str = "",
    minute_days: int = 3,
    daily_days: int = 25,
    top_n: int = 15,
    execute: bool = False,
    force: bool = False,
):
    """Pull Massive/Polygon Flat Files into /tmp, build the list, then delete raw files.

    Safety gates are inside the fetcher: no weekend/holiday pulls, capped attempts per
    trade_date/dataset, and no raw-file persistence in Railway/GitHub/SQLite.
    """
    try:
        return build_weekly_candidates_from_polygon(
            trade_date=trade_date or None,
            minute_days=int(minute_days or 3),
            daily_days=int(daily_days or 25),
            top_n=int(top_n or 15),
            execute=bool(execute),
            force=bool(force),
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "trade_date": trade_date}

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
        try:
            position_aware = build_position_aware_snapshot(item, plan)
        except Exception as exc:
            position_aware = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
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
            "position_aware": position_aware,
            "position_aware_action": position_aware.get("action_ar", "") if isinstance(position_aware, dict) else "",
            "position_aware_status": position_aware.get("status", "") if isinstance(position_aware, dict) else "",
            "position_levels_summary": position_aware.get("levels_summary", "") if isinstance(position_aware, dict) else "",
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

    try:
        total_market_value = sum(float(x.get("market_value", 0) or 0) for x in out)
        total_cost_value = sum(float(x.get("cost_value", 0) or 0) for x in out)
        total_pl = total_market_value - total_cost_value
        total_pl_pct = (total_pl / total_cost_value * 100.0) if total_cost_value > 0 else 0.0
        summary = {
            "positions": len(out),
            "market_value": safe_round(total_market_value),
            "cost_value": safe_round(total_cost_value),
            "unrealized_pl": safe_round(total_pl),
            "unrealized_pl_pct": safe_round(total_pl_pct),
            "rule_ar": "المحفظة Position-Aware: التحليل يبدأ من سعر شراء المستخدم لا من خطة جديدة.",
        }
    except Exception:
        summary = {"positions": len(out)}
    return {"items": out, "summary": summary}


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

    # Sharia safety rule: a manual exclusion must be authoritative.
    # If the same symbol was previously manually approved, remove that old
    # approval immediately so the stock cannot reappear in cached/frozen lists
    # as approved after the user excludes it.
    approvals_before = load_manual_sharia_approvals()
    approvals_after = [
        item for item in approvals_before
        if normalize_symbol_text(item.get("symbol", "")) != symbol
    ]
    approval_removed = len(approvals_after) != len(approvals_before)
    if approval_removed:
        save_manual_sharia_approvals(approvals_after)

    return {
        "ok": True,
        "count": len(items),
        "symbol": symbol,
        "saved": True,
        "approval_removed": approval_removed,
        "rule": "manual_exclusion_overrides_manual_approval",
    }


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
    status["evidence_auto_sync"] = evidence_auto_sync_status()
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




@app.get("/missed-opportunities/symbol/{symbol}")
def missed_opportunities_symbol(symbol: str, week_key: str = "", threshold: float = 10.0, format: str = "json"):
    fmt = str(format or "json").strip().lower()
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(
            build_symbol_timeline_brief(symbol, week_key=week_key or None, threshold=threshold),
            media_type="text/plain; charset=utf-8",
        )
    return build_symbol_timeline_report(symbol, week_key=week_key or None, threshold=threshold)


@app.get("/missed-opportunities/late-promotions")
def missed_opportunities_late_promotions(week_key: str = "", threshold: float = 10.0, format: str = "json"):
    fmt = str(format or "json").strip().lower()
    result = build_late_promotions_report(week_key=week_key or None, threshold=threshold, format=fmt)
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/missed-opportunities/pre-move-analysis")
def missed_opportunities_pre_move_analysis(week_key: str = "", threshold: float = 10.0, format: str = "json", limit: int = 120):
    fmt = str(format or "json").strip().lower()
    result = build_pre_move_evidence_report(week_key=week_key or None, threshold=threshold, format=fmt, limit=limit)
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/missed-opportunities/loss-analysis")
def missed_opportunities_loss_analysis(week_key: str = "", format: str = "json", limit: int = 500, detail: str = "summary", top: int = 20):
    fmt = str(format or "json").strip().lower()
    result = build_loss_analysis_report(week_key=week_key or None, format=fmt, limit=limit, detail=detail, top=top)
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result

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




@app.get("/learning/patterns/weekly")
def learning_patterns_weekly(week_key: str = "", format: str = "json"):
    fmt = str(format or "json").strip().lower()
    result = build_pattern_learning_report(week_key=week_key or None, format=fmt)
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/learning/failure-patterns")
def learning_failure_patterns(week_key: str = "", format: str = "json"):
    fmt = str(format or "json").strip().lower()
    result = build_failure_patterns_report(week_key=week_key or None, format=fmt)
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/learning/winner-patterns")
def learning_winner_patterns(week_key: str = "", format: str = "json"):
    fmt = str(format or "json").strip().lower()
    result = build_winner_patterns_report(week_key=week_key or None, format=fmt)
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/diagnostics/promotion-funnel")
def diagnostics_promotion_funnel(week_key: str = "", format: str = "json"):
    fmt = str(format or "json").strip().lower()
    result = build_promotion_funnel_report(week_key=week_key or None, format=fmt)
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/evidence/status")
def evidence_status_endpoint():
    return evidence_status()


@app.post("/evidence/collect")
@app.get("/evidence/collect")
def evidence_collect_endpoint(mode: str = "manual", include_big_movers: bool = True, sync_to_github: bool = False, max_symbols: int | None = None):
    return collect_evidence_snapshot(
        mode=mode or "manual",
        include_big_movers=bool(include_big_movers),
        sync_to_github=bool(sync_to_github),
        max_symbols=max_symbols,
    )


@app.get("/evidence/daily-winners")
def evidence_daily_winners_endpoint(trade_date: str = "", format: str = "json", limit: int = 120):
    result = daily_winners_report(trade_date=trade_date or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/evidence/weekly-summary")
def evidence_weekly_summary_endpoint(week_key: str = "", format: str = "json", limit: int = 50):
    result = weekly_evidence_summary(week_key=week_key or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.post("/evidence/backfill-winners")
@app.get("/evidence/backfill-winners")
def evidence_backfill_winners_endpoint(start_date: str = "", end_date: str = "", days_back: int = 5, threshold_pct: float = 10.0, limit_per_day: int = 120, store_bars: bool = True):
    return backfill_daily_winner_profiles(
        start_date=start_date or None,
        end_date=end_date or None,
        days_back=days_back,
        threshold_pct=threshold_pct,
        limit_per_day=limit_per_day,
        store_bars=bool(store_bars),
    )


@app.get("/evidence/winner-profiles")
def evidence_winner_profiles_endpoint(week_key: str = "", trade_date: str = "", format: str = "json", limit: int = 120):
    result = winner_profiles_report(week_key=week_key or None, trade_date=trade_date or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/evidence/pattern-readiness")
def evidence_pattern_readiness_endpoint(week_key: str = "", format: str = "json"):
    result = pattern_readiness_report(week_key=week_key or None, format=format)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result




@app.get("/evidence/pattern-lab")
def evidence_pattern_lab_endpoint(week_key: str = "", trade_date: str = "", format: str = "json", limit: int = 40):
    result = pattern_lab_report(week_key=week_key or None, trade_date=trade_date or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result

@app.get("/diagnostics/big-mover-anatomy")
@app.get("/diagnostics/big-mover-pattern-audit")
@app.get("/diagnostics/scan-gap-latency")
def diagnostics_big_mover_anatomy_endpoint(
    week_key: str = "",
    trade_date: str = "",
    format: str = "json",
    threshold: float = 10.0,
    limit: int = 40,
    history_mode: str = "stored",
    lookback_days: int = 30,
    max_profiles: int = 1000,
    sample_limit: int = 120,
    external_limit: int = 0,
    compare_losses: bool = True,
):
    result = big_mover_anatomy_scan_gap_report(
        week_key=week_key or None,
        trade_date=trade_date or None,
        format=format,
        threshold=threshold,
        limit=limit,
        history_mode=history_mode,
        lookback_days=lookback_days,
        max_profiles=max_profiles,
        sample_limit=sample_limit,
        external_limit=external_limit,
        compare_losses=compare_losses,
    )
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/early-movement/status")
def early_movement_status_endpoint():
    return build_early_movement_static_status()


@app.get("/early-movement/watchlist")
def early_movement_watchlist_endpoint():
    return build_early_movement_static_status()


@app.get("/early-movement/report")
@app.get("/early-movement/weekly-report")
def early_movement_report_endpoint(format: str = "json"):
    result = build_early_movement_weekly_report(format=format)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/evidence/export.json")
def evidence_export_json_endpoint(week_key: str = "", trade_date: str = "", limit: int = 10000):
    return export_evidence_json(week_key=week_key or None, trade_date=trade_date or None, limit=limit)


@app.get("/evidence/export.csv")
def evidence_export_csv_endpoint(week_key: str = "", trade_date: str = "", limit: int = 10000):
    csv_text = "﻿" + export_evidence_csv(week_key=week_key or None, trade_date=trade_date or None, limit=limit)
    filename_week = str(week_key or "current").strip() or "current"
    filename_date = str(trade_date or "all").strip() or "all"
    headers = {"Content-Disposition": f'attachment; filename="evidence_{filename_week}_{filename_date}.csv"'}
    return Response(content=csv_text, media_type="text/csv; charset=utf-8", headers=headers)


@app.post("/evidence/sync-github")
@app.get("/evidence/sync-github")
def evidence_sync_github_endpoint(week_key: str = "", trade_date: str = "", include_csv: bool = False):
    return sync_evidence_to_github(week_key=week_key or None, trade_date=trade_date or None, include_csv=bool(include_csv))


@app.get("/evidence/auto-sync/status")
@app.get("/evidence/auto-status")
def evidence_auto_sync_status_endpoint():
    return evidence_auto_sync_status()


@app.post("/evidence/auto-sync/run")
@app.get("/evidence/auto-sync/run")
def evidence_auto_sync_run_endpoint(force: bool = False, dry_run: bool = False, include_csv: bool = False):
    return run_evidence_auto_sync(force=bool(force), dry_run=bool(dry_run), include_csv=bool(include_csv))


@app.get("/evidence/retention/auto-maintenance/status")
def evidence_retention_auto_maintenance_status_endpoint(week_key: str = "", trade_date: str = ""):
    return evidence_retention_auto_maintenance_status(week_key=week_key or None, trade_date=trade_date or None)


@app.post("/evidence/retention/auto-maintenance/run")
@app.get("/evidence/retention/auto-maintenance/run")
def evidence_retention_auto_maintenance_run_endpoint(
    force: bool = False,
    dry_run: bool = False,
    week_key: str = "",
    trade_date: str = "",
    sync_first: bool = False,
    cleanup_local: bool = True,
    include_smart_compact: bool = False,
    compact_confirm: str = "",
):
    return run_evidence_retention_auto_maintenance(
        force=bool(force),
        dry_run=bool(dry_run),
        week_key=week_key or None,
        trade_date=trade_date or None,
        sync_first=bool(sync_first),
        cleanup_local=bool(cleanup_local),
        include_smart_compact=bool(include_smart_compact),
        compact_confirm=compact_confirm,
    )


@app.post("/evidence/retention/local-archive-cleanup-dry-run")
@app.get("/evidence/retention/local-archive-cleanup-dry-run")
def evidence_local_archive_cleanup_dry_run_endpoint(week_key: str = "", trade_date: str = "", require_verified: bool = True):
    return evidence_local_archive_cleanup_dry_run(week_key=week_key or None, trade_date=trade_date or None, require_verified=bool(require_verified))


@app.post("/evidence/retention/local-archive-cleanup-execute")
@app.get("/evidence/retention/local-archive-cleanup-execute")
def evidence_local_archive_cleanup_execute_endpoint(week_key: str = "", trade_date: str = "", require_verified: bool = True, confirm: str = ""):
    return evidence_local_archive_cleanup_execute(week_key=week_key or None, trade_date=trade_date or None, require_verified=bool(require_verified), confirm=confirm)


@app.get("/evidence/liquidity-check")
def evidence_liquidity_check_endpoint(symbol: str, trade_date: str = "", store_bars: bool = False):
    return liquidity_confirmation_check(symbol=symbol, trade_date=trade_date or None, store_bars=bool(store_bars))


@app.get("/evidence/retention/status")
def evidence_retention_status_endpoint(week_key: str = "", trade_date: str = ""):
    return evidence_retention_status(week_key=week_key or None, trade_date=trade_date or None)


@app.post("/evidence/retention/verify-github")
@app.get("/evidence/retention/verify-github")
def evidence_retention_verify_github_endpoint(week_key: str = "", trade_date: str = "", include_csv: bool = True):
    return evidence_retention_verify_github(week_key=week_key or None, trade_date=trade_date or None, include_csv=bool(include_csv))


@app.post("/evidence/retention/prune-dry-run")
@app.get("/evidence/retention/prune-dry-run")
def evidence_retention_prune_dry_run_endpoint(week_key: str = "", trade_date: str = "", keep_days: int | None = None, require_verified: bool = True):
    return evidence_retention_prune_dry_run(week_key=week_key or None, trade_date=trade_date or None, keep_days=keep_days, require_verified=bool(require_verified))


@app.post("/evidence/retention/prune-execute")
@app.get("/evidence/retention/prune-execute")
def evidence_retention_prune_execute_endpoint(week_key: str = "", trade_date: str = "", keep_days: int | None = None, require_verified: bool = True, confirm: str = "", include_snapshots: bool = False):
    return evidence_retention_prune_execute(
        week_key=week_key or None,
        trade_date=trade_date or None,
        keep_days=keep_days,
        require_verified=bool(require_verified),
        confirm=confirm,
        include_snapshots=bool(include_snapshots),
    )


@app.post("/evidence/retention/sqlite-table-size-report")
@app.get("/evidence/retention/sqlite-table-size-report")
def evidence_retention_sqlite_table_size_report_endpoint(limit: int = 30, include_indexes: bool = True):
    return evidence_retention_sqlite_table_size_report(limit=limit, include_indexes=bool(include_indexes))


@app.post("/evidence/retention/evidence-snapshots-payload-report")
@app.get("/evidence/retention/evidence-snapshots-payload-report")
def evidence_snapshots_payload_report_endpoint(week_key: str = "", trade_date: str = "", limit: int = 30, heavy_threshold_kb: int = 100):
    return evidence_snapshots_payload_report(
        week_key=week_key or None,
        trade_date=trade_date or None,
        limit=limit,
        heavy_threshold_kb=heavy_threshold_kb,
    )


@app.post("/evidence/retention/evidence-snapshots-raw-json-slim-dry-run")
@app.get("/evidence/retention/evidence-snapshots-raw-json-slim-dry-run")
def evidence_snapshots_raw_json_slim_dry_run_endpoint(
    week_key: str = "",
    trade_date: str = "",
    require_verified: bool = True,
    limit: int = 20,
):
    return evidence_snapshots_raw_json_slim_dry_run(
        week_key=week_key or None,
        trade_date=trade_date or None,
        require_verified=bool(require_verified),
        limit=limit,
    )


@app.post("/evidence/retention/evidence-snapshots-raw-json-slim-execute")
@app.get("/evidence/retention/evidence-snapshots-raw-json-slim-execute")
def evidence_snapshots_raw_json_slim_execute_endpoint(
    week_key: str = "",
    trade_date: str = "",
    require_verified: bool = True,
    confirm: str = "",
):
    return evidence_snapshots_raw_json_slim_execute(
        week_key=week_key or None,
        trade_date=trade_date or None,
        require_verified=bool(require_verified),
        confirm=confirm,
    )


@app.post("/evidence/retention/sqlite-compact-status")
@app.get("/evidence/retention/sqlite-compact-status")
def evidence_retention_sqlite_compact_status_endpoint(
    week_key: str = "",
    trade_date: str = "",
    keep_days: int | None = None,
    require_verified: bool = True,
    min_free_ratio: float | None = None,
    min_free_buffer_mb: float | None = None,
):
    return evidence_retention_sqlite_compact_status(
        week_key=week_key or None,
        trade_date=trade_date or None,
        keep_days=keep_days,
        require_verified=bool(require_verified),
        min_free_ratio=min_free_ratio,
        min_free_buffer_mb=min_free_buffer_mb,
    )


@app.post("/evidence/retention/sqlite-compact-execute")
@app.get("/evidence/retention/sqlite-compact-execute")
def evidence_retention_sqlite_compact_execute_endpoint(
    week_key: str = "",
    trade_date: str = "",
    keep_days: int | None = None,
    require_verified: bool = True,
    confirm: str = "",
    min_free_ratio: float | None = None,
    min_free_buffer_mb: float | None = None,
):
    return evidence_retention_sqlite_compact_execute(
        week_key=week_key or None,
        trade_date=trade_date or None,
        keep_days=keep_days,
        require_verified=bool(require_verified),
        confirm=confirm,
        min_free_ratio=min_free_ratio,
        min_free_buffer_mb=min_free_buffer_mb,
    )


@app.post("/evidence/retention/sqlite-smart-compact-status")
@app.get("/evidence/retention/sqlite-smart-compact-status")
def evidence_retention_sqlite_smart_compact_status_endpoint(
    week_key: str = "",
    trade_date: str = "",
    keep_days: int | None = None,
    require_verified: bool = True,
    output_buffer_mb: float | None = None,
    min_reclaimable_mb: float | None = None,
):
    return evidence_retention_sqlite_smart_compact_status(
        week_key=week_key or None,
        trade_date=trade_date or None,
        keep_days=keep_days,
        require_verified=bool(require_verified),
        output_buffer_mb=output_buffer_mb,
        min_reclaimable_mb=min_reclaimable_mb,
    )


@app.post("/evidence/retention/sqlite-smart-compact-execute")
@app.get("/evidence/retention/sqlite-smart-compact-execute")
def evidence_retention_sqlite_smart_compact_execute_endpoint(
    week_key: str = "",
    trade_date: str = "",
    keep_days: int | None = None,
    require_verified: bool = True,
    confirm: str = "",
    output_buffer_mb: float | None = None,
    min_reclaimable_mb: float | None = None,
):
    return evidence_retention_sqlite_smart_compact_execute(
        week_key=week_key or None,
        trade_date=trade_date or None,
        keep_days=keep_days,
        require_verified=bool(require_verified),
        confirm=confirm,
        output_buffer_mb=output_buffer_mb,
        min_reclaimable_mb=min_reclaimable_mb,
    )


@app.get("/diagnostics/source-entry-audit")
def diagnostics_source_entry_audit(week_key: str = "", trade_date: str = "", format: str = "json", limit: int = 80):
    result = build_source_entry_audit(week_key=week_key or None, trade_date=trade_date or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/diagnostics/promotion-audit")
def diagnostics_promotion_audit(week_key: str = "", trade_date: str = "", format: str = "json", limit: int = 120):
    result = build_promotion_audit(week_key=week_key or None, trade_date=trade_date or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result


@app.get("/diagnostics/clean-alternatives")
def diagnostics_clean_alternatives(symbol: str = "", week_key: str = "", trade_date: str = "", format: str = "json", limit: int = 30):
    result = build_clean_alternatives(symbol=symbol, week_key=week_key or None, trade_date=trade_date or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result




@app.get("/diagnostics/source-discovery-coverage")
@app.get("/diagnostics/source-freshness")
def diagnostics_source_discovery_coverage(week_key: str = "", trade_date: str = "", format: str = "json", limit: int = 40):
    result = build_source_discovery_coverage(week_key=week_key or None, trade_date=trade_date or None, format=format, limit=limit)
    if str(format or "json").strip().lower() in {"brief", "text", "txt", "chatgpt"}:
        return PlainTextResponse(str(result), media_type="text/plain; charset=utf-8")
    return result

@app.post("/admin/archive-weekly-tracking")
@app.get("/admin/archive-weekly-tracking")
def admin_archive_weekly_tracking(token: str = "", week_key: str = "", prune: bool | None = None):
    expected = str(WEEKLY_ARCHIVE_TOKEN or APP_SESSION_SECRET or "").strip()
    if expected and str(token or "").strip() != expected:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if prune is None:
        prune = bool(WEEKLY_ARCHIVE_PRUNE_AFTER_SUCCESS)
    return archive_weekly_tracking(week_key=week_key or None, prune=bool(prune), include_items=True)


@app.get("/admin/archive-weekly-tracking/status")
def admin_archive_weekly_tracking_status(week_key: str = ""):
    return weekly_archive_status(week_key=week_key or None)


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

