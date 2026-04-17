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

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
APP_DATA_DIR_ENV = str(os.getenv("APP_DATA_DIR", "") or "").strip()
if APP_DATA_DIR_ENV:
    DATA_DIR = Path(APP_DATA_DIR_ENV).expanduser()
elif Path("/data").exists():
    DATA_DIR = Path("/data")
else:
    DATA_DIR = BASE_DIR / "app_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

APP_AUTH_USERNAME = str(os.getenv("APP_BASIC_AUTH_USERNAME", "") or "").strip()
APP_AUTH_PASSWORD = str(os.getenv("APP_BASIC_AUTH_PASSWORD", "") or "").strip()
APP_AUTH_ENABLED = bool(APP_AUTH_USERNAME and APP_AUTH_PASSWORD)
APP_AUTH_SESSION_DAYS = int(float(os.getenv("APP_AUTH_SESSION_DAYS", "14") or 14))
APP_SESSION_SECRET = os.getenv("APP_SESSION_SECRET") or hashlib.sha256(f"{APP_AUTH_USERNAME}:{APP_AUTH_PASSWORD}:stock-radar".encode("utf-8")).hexdigest()
APP_AUTH_COOKIE_NAME = "sr_auth"
AUTH_EXEMPT_PATHS = {"/health", "/login", "/logout", "/session"}

HTTP_SESSION = requests.Session()
HTTP_ADAPTER = HTTPAdapter(pool_connections=256, pool_maxsize=256, max_retries=0)
HTTP_SESSION.mount("https://", HTTP_ADAPTER)
HTTP_SESSION.mount("http://", HTTP_ADAPTER)


def http_get_json(url, timeout=12):
    try:
        r = HTTP_SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except:
        return {}


def _auth_cookie_sign(payload: str) -> str:
    return hashlib.sha256(f"{payload}|{APP_SESSION_SECRET}".encode("utf-8")).hexdigest()


def build_auth_cookie_value(username: str) -> str:
    expires_at = int(time.time()) + (APP_AUTH_SESSION_DAYS * 24 * 60 * 60)
    payload = f"{username}|{expires_at}"
    signature = _auth_cookie_sign(payload)
    return f"{payload}|{signature}"


def read_auth_cookie(request: Request):
    token = str(request.cookies.get(APP_AUTH_COOKIE_NAME, "") or "").strip()
    if not token:
        return None
    parts = token.split("|")
    if len(parts) != 3:
        return None
    username, expires_at_raw, signature = parts
    if not username or not expires_at_raw or not signature:
        return None
    try:
        expires_at = int(expires_at_raw)
    except:
        return None
    if expires_at < int(time.time()):
        return None
    expected = _auth_cookie_sign(f"{username}|{expires_at}")
    if not secrets.compare_digest(signature, expected):
        return None
    if APP_AUTH_ENABLED and username != APP_AUTH_USERNAME:
        return None
    return {"username": username, "expires_at": expires_at}




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

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

SECTOR_DATA = {}
COMPANIES_DATA = {}
BALANCE_DATA = {}
INCOME_DATA = {}
HISTORY_CACHE = {}
REF_INFO_CACHE = {}
INTRADAY_CACHE = {}
SNAPSHOT_CACHE = {}
PERFORMANCE_FILE = str(DATA_DIR / "signal_performance.json")

MANUAL_WATCHLIST_FILE = str(DATA_DIR / "manual_watchlist.json")
PORTFOLIO_FILE = str(DATA_DIR / "portfolio_holdings.json")

def ny_now():
    return datetime.now(ZoneInfo("America/New_York"))

def load_manual_watchlist():
    try:
        with open(MANUAL_WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []

def save_manual_watchlist(items):
    try:
        with open(MANUAL_WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except:
        pass


def load_portfolio_items():
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []


def save_portfolio_items(items):
    try:
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except:
        pass


def get_performance_week_window(base_dt=None):
    dt = base_dt.astimezone(ZoneInfo("America/New_York")) if base_dt else ny_now()
    monday = dt.date() - timedelta(days=dt.weekday())
    friday = monday + timedelta(days=4)
    return monday.isoformat(), friday.isoformat()


def get_performance_week_key(base_dt=None):
    week_start, week_end = get_performance_week_window(base_dt)
    return f"{week_start}_{week_end}"


def make_blank_performance_store(base_dt=None):
    week_start, week_end = get_performance_week_window(base_dt)
    return {
        "active_week_key": get_performance_week_key(base_dt),
        "active_week_start": week_start,
        "active_week_end": week_end,
        "active_records": [],
        "weekly_archive": [],
    }


def make_performance_summary(records):
    total = len(records)
    wins = sum(1 for r in records if str(r.get("outcome", "") or "") in {"target_hit", "above_target"})
    losses = sum(1 for r in records if str(r.get("outcome", "") or "") == "loss")
    pending = sum(1 for r in records if str(r.get("outcome", "") or "") not in {"target_hit", "above_target", "loss"})
    win_rate = safe_round((wins / total) * 100, 2) if total > 0 else 0.0
    return {
        "count": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate_pct": win_rate,
    }


def build_archive_summary_for_week(week_start, week_end, records):
    summary = make_performance_summary(records)
    return {
        "week_key": f"{week_start}_{week_end}",
        "week_start": week_start,
        "week_end": week_end,
        "count": summary["count"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "pending": summary["pending"],
        "win_rate_pct": summary["win_rate_pct"],
    }


def normalize_performance_store(store):
    if not isinstance(store, dict):
        store = make_blank_performance_store()
    store.setdefault("active_week_key", get_performance_week_key())
    store.setdefault("active_week_start", get_performance_week_window()[0])
    store.setdefault("active_week_end", get_performance_week_window()[1])
    store.setdefault("active_records", [])
    store.setdefault("weekly_archive", [])
    if not isinstance(store.get("active_records"), list):
        store["active_records"] = []
    if not isinstance(store.get("weekly_archive"), list):
        store["weekly_archive"] = []
    return store


def migrate_legacy_performance_items(items):
    store = make_blank_performance_store()
    week_start = store["active_week_start"]
    week_end = store["active_week_end"]
    records = []
    for item in items if isinstance(items, list) else []:
        signal_type = str(item.get("signal_type", "") or "")
        if signal_type not in {"دخول قوي", "دخول بحذر"}:
            continue
        item_date = str(item.get("date", "") or "")
        if not item_date or item_date < week_start or item_date > week_end:
            continue
        entry_price = float(item.get("entry_price", 0) or 0)
        if entry_price <= 0:
            continue
        created_at = f"{item_date} {str(item.get('time', '') or '').strip()}".strip()
        records.append({
            "id": f"{store['active_week_key']}::{str(item.get('symbol', '')).upper().strip()}",
            "symbol": str(item.get("symbol", "") or "").upper().strip(),
            "signal_type": signal_type,
            "entry_price": safe_round(entry_price),
            "target_price": 0.0,
            "stop_loss": 0.0,
            "first_seen_at": created_at,
            "last_seen_at": created_at,
            "current_price": float(item.get("current_price", 0) or 0),
            "max_price_seen": float(item.get("current_price", 0) or 0),
            "min_price_seen": float(item.get("current_price", 0) or 0),
            "price_source_label": str(item.get("price_source", "") or ""),
            "strategy_label": str(item.get("strategy_label", "") or ""),
            "status_mark": "⏳",
            "status_label": "قيد المتابعة",
            "outcome": "pending",
            "closed_at": "",
            "market_phase": str(item.get("market_phase", "") or ""),
            "last_change_pct": float(item.get("change_pct", 0) or 0),
        })
    store["active_records"] = records[:200]
    return store


def load_performance_store():
    try:
        with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except:
        return make_blank_performance_store()

    if isinstance(raw, list):
        return migrate_legacy_performance_items(raw)

    return normalize_performance_store(raw)


def save_performance_store(store):
    try:
        with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
            json.dump(normalize_performance_store(store), f, ensure_ascii=False, indent=2)
    except:
        pass


def rollover_performance_store_if_needed(store, base_dt=None):
    store = normalize_performance_store(store)
    current_week_key = get_performance_week_key(base_dt)
    if store.get("active_week_key") == current_week_key:
        return store

    old_records = list(store.get("active_records", []))
    old_week_start = str(store.get("active_week_start", "") or "")
    old_week_end = str(store.get("active_week_end", "") or "")
    if old_records and old_week_start and old_week_end:
        archive_entry = build_archive_summary_for_week(old_week_start, old_week_end, old_records)
        existing_index = next((i for i, row in enumerate(store.get("weekly_archive", [])) if row.get("week_key") == archive_entry["week_key"]), None)
        if existing_index is None:
            store.setdefault("weekly_archive", []).insert(0, archive_entry)
        else:
            store["weekly_archive"][existing_index] = archive_entry

    new_store = make_blank_performance_store(base_dt)
    new_store["weekly_archive"] = store.get("weekly_archive", [])[:26]
    return new_store


def parse_validity_days(valid_for: str) -> int:
    txt = str(valid_for or "")
    if "اليوم فقط" in txt:
        return 1
    if "الجلسة القادمة" in txt:
        return 2
    if "1-2" in txt:
        return 2
    if "1-3" in txt:
        return 3
    if "مراقبة" in txt:
        return 5
    return 3


def estimate_signal_expired(record: dict) -> bool:
    try:
        first_seen = str(record.get("first_seen_at", "") or "")
        if not first_seen:
            return False
        first_dt = datetime.strptime(first_seen[:19], "%Y-%m-%d %H:%M:%S")
        days_allowed = int(record.get("signal_ttl_days", 3) or 3)
        return (ny_now().replace(tzinfo=None) - first_dt).days >= days_allowed
    except:
        return False


def outcome_sort_rank(outcome: str) -> int:
    order = {
        "above_target": 0,
        "target_hit": 1,
        "partial_gain": 2,
        "ongoing": 3,
        "loss": 4,
        "expired": 5,
    }
    return order.get(str(outcome or "ongoing"), 9)


def evaluate_performance_record(record, current_price):
    entry_price = float(record.get("entry_price", 0) or 0)
    target_price = float(record.get("target_price", 0) or 0)
    target_2_price = float(record.get("target_2_price", 0) or 0)
    stop_loss = float(record.get("stop_loss", 0) or 0)
    current_price = float(current_price or 0)

    if current_price > 0:
        if float(record.get("max_price_seen", 0) or 0) <= 0:
            record["max_price_seen"] = current_price
        else:
            record["max_price_seen"] = max(float(record.get("max_price_seen", 0) or 0), current_price)

        if float(record.get("min_price_seen", 0) or 0) <= 0:
            record["min_price_seen"] = current_price
        else:
            record["min_price_seen"] = min(float(record.get("min_price_seen", current_price) or current_price), current_price)

        record["current_price"] = safe_round(current_price)

    change_pct = 0.0
    if entry_price > 0 and current_price > 0:
        change_pct = ((current_price - entry_price) / entry_price) * 100
    record["last_change_pct"] = safe_round(change_pct)

    max_seen = float(record.get("max_price_seen", 0) or 0)
    min_seen = float(record.get("min_price_seen", 0) or 0)
    now_str = ny_now().strftime("%Y-%m-%d %H:%M:%S")

    outcome = "ongoing"
    label = "مستمرة"
    mark = "⏳"

    if target_2_price > 0 and max_seen >= target_2_price:
        outcome, label, mark = "above_target", "تجاوزت الهدف", "🚀"
    elif target_price > 0 and max_seen >= target_price:
        outcome, label, mark = "target_hit", "وصلت الهدف", "✅"
    elif stop_loss > 0 and min_seen > 0 and min_seen <= stop_loss:
        outcome, label, mark = "loss", "خاسرة", "❌"
    else:
        expired = estimate_signal_expired(record)
        positive_move_pct = ((max_seen - entry_price) / entry_price) * 100 if entry_price > 0 and max_seen > 0 else 0.0
        if expired:
            if positive_move_pct >= 0.8 or change_pct >= 0.8:
                outcome, label, mark = "partial_gain", "صعد أقل من الهدف", "⚠️"
            else:
                outcome, label, mark = "expired", "منتهية بلا حسم", "🕓"
        else:
            outcome, label, mark = "ongoing", "مستمرة", "⏳"

    record["outcome"] = outcome
    record["status_mark"] = mark
    record["status_label"] = label
    if outcome in {"above_target", "target_hit", "loss", "partial_gain", "expired"}:
        record["closed_at"] = record.get("closed_at") or now_str
    return record


def build_signal_record_id(week_key: str, symbol: str, signal_type: str, plan_family: str, entry_price: float, target_price: float, stop_loss: float) -> str:
    return f"{week_key}::{symbol}::{signal_type}::{plan_family}::{safe_round(entry_price)}::{safe_round(target_price)}::{safe_round(stop_loss)}"


def upsert_performance_signal(stock: dict):
    try:
        signal_type = str(stock.get("decision", "") or "")
        if signal_type not in {"دخول قوي", "دخول بحذر"}:
            return
        symbol = str(stock.get("symbol", "") or "").upper().strip()
        if not symbol:
            return
        entry_price = float(
            stock.get("display_entry_price",
            stock.get("entry_price_real",
            stock.get("entry", 0))) or 0
        )
        target_price = float(
            stock.get("display_target_price",
            stock.get("target_1",
            stock.get("breakout_target", 0))) or 0
        )
        stop_loss = float(
            stock.get("display_stop_price",
            stock.get("stop_loss",
            stock.get("atr_stop_price", 0))) or 0
        )
        target_2_price = float(stock.get("target_2", 0) or 0)
        current_price = float(stock.get("display_price", stock.get("current_price_live", 0)) or 0)
        if entry_price <= 0:
            return
        store = rollover_performance_store_if_needed(load_performance_store())
        records = store.get("active_records", [])
        plan_family = str(stock.get("display_plan_family", stock.get("type", "")) or "")
        record_id = build_signal_record_id(
            store['active_week_key'],
            symbol,
            signal_type,
            plan_family,
            entry_price,
            target_price,
            stop_loss,
        )
        now_str = ny_now().strftime("%Y-%m-%d %H:%M:%S")
        existing = next((item for item in records if item.get("id") == record_id), None)
        if existing is None:
            existing = {
                "id": record_id,
                "symbol": symbol,
                "signal_type": signal_type,
                "plan_family": plan_family,
                "entry_price": safe_round(entry_price),
                "target_price": safe_round(target_price),
                "target_2_price": safe_round(target_2_price),
                "stop_loss": safe_round(stop_loss),
                "first_seen_at": now_str,
                "last_seen_at": now_str,
                "current_price": safe_round(current_price),
                "max_price_seen": safe_round(current_price if current_price > 0 else entry_price),
                "min_price_seen": safe_round(current_price if current_price > 0 else entry_price),
                "price_source_label": str(stock.get("price_source_label", stock.get("price_source", "")) or ""),
                "strategy_label": str(stock.get("strategy_label", "") or ""),
                "status_mark": "⏳",
                "status_label": "مستمرة",
                "outcome": "ongoing",
                "closed_at": "",
                "market_phase": str(stock.get("market_phase_label", stock.get("market_phase", "")) or ""),
                "last_change_pct": 0.0,
                "valid_for": str(stock.get("valid_for", "") or ""),
                "signal_ttl_days": parse_validity_days(stock.get("valid_for", "")),
            }
            records.insert(0, existing)
        else:
            existing["last_seen_at"] = now_str
            existing["price_source_label"] = str(stock.get("price_source_label", stock.get("price_source", "")) or existing.get("price_source_label", ""))
            existing["strategy_label"] = str(stock.get("strategy_label", "") or existing.get("strategy_label", ""))
            existing["market_phase"] = str(stock.get("market_phase_label", stock.get("market_phase", "")) or existing.get("market_phase", ""))
            existing["target_2_price"] = max(float(existing.get("target_2_price", 0) or 0), target_2_price)

        evaluate_performance_record(existing, current_price)
        store["active_records"] = records[:500]
        save_performance_store(store)
    except:
        pass

INTRADAY_CACHE_TTL_OPEN = 12
INTRADAY_CACHE_TTL_CLOSED = 60
SNAPSHOT_CACHE_TTL_OPEN = 8
SNAPSHOT_CACHE_TTL_EXTENDED = 15
SNAPSHOT_CACHE_TTL_CLOSED = 120

HARAM_SECTORS = {"financial services", "banks", "insurance"}

HARAM_INDUSTRY_KEYWORDS = [
    "bank", "banks", "insurance", "tobacco", "alcohol",
    "gambling", "casino", "betting", "credit services",
    "mortgage", "reit mortgage", "asset management", "capital markets",
]

LOW_PRICE_HARD_BLOCK = 2.0
LOW_PRICE_WARNING = 3.0


def clean_key(key):
    return str(key).replace("\ufeff", "").strip()


def clean_row(row):
    return {clean_key(k): v for k, v in row.items()}


def to_float(value):
    try:
        if value is None:
            return 0.0
        value = str(value).replace(",", "").strip()
        return float(value) if value else 0.0
    except:
        return 0.0


def period_rank(p):
    return {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "FY": 5, "TTM": 6}.get(str(p).upper(), 0)


def parse_date_safe(v):
    try:
        return datetime.strptime(str(v).strip(), "%Y-%m-%d")
    except:
        return datetime.min


def safe_round(x, digits=2):
    try:
        return round(float(x), digits)
    except:
        return x


def clamp(value, min_value, max_value):
    try:
        return max(float(min_value), min(float(value), float(max_value)))
    except:
        return min_value


def _cache_get(cache_obj, key):
    item = cache_obj.get(key)
    if not item:
        return None
    expires_at = float(item.get("expires_at", 0) or 0)
    if expires_at <= time.time():
        cache_obj.pop(key, None)
        return None
    return item.get("value")


def _cache_set(cache_obj, key, value, ttl_seconds):
    cache_obj[key] = {
        "value": value,
        "expires_at": time.time() + max(float(ttl_seconds or 0), 0.0)
    }
    return value


def latest_market_date_str():
    ny = ZoneInfo("America/New_York")
    return datetime.now(ny).date().isoformat()


def latest_key(row):
    return (
        parse_date_safe(row.get("Publish Date", "")),
        int(to_float(row.get("Fiscal Year", 0))),
        period_rank(row.get("Fiscal Period", ""))
    )


def normalize_text(text: str) -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^a-z0-9\s&.\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_company_name_variants(company_name: str) -> list[str]:
    name = normalize_text(company_name)
    if not name:
        return []

    variants = {name}
    noise = [
        " inc", " inc.", " corp", " corp.", " corporation", " co", " co.",
        " ltd", " ltd.", " limited", " plc", " holdings", " holding",
        " group", " technologies", " technology", " systems", " system",
        " international", " company", " companies", " class a", " class c",
        " common stock"
    ]

    for n in noise:
        if name.endswith(n):
            variants.add(name[:-len(n)].strip())

    parts = name.split()
    if len(parts) >= 2:
        variants.add(" ".join(parts[:2]))
    if len(parts) >= 1:
        variants.add(parts[0])

    cleaned = []
    for v in variants:
        v = v.strip()
        if len(v) >= 3:
            cleaned.append(v)

    return list(dict.fromkeys(cleaned))


def make_rank_label(score: float) -> str:
    if score >= 90:
        return "A+"
    if score >= 84:
        return "A"
    if score >= 78:
        return "B+"
    if score >= 72:
        return "B"
    if score >= 66:
        return "C+"
    if score >= 60:
        return "C"
    return "D"


def is_market_open_now() -> bool:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return False
        current_minutes = now_ny.hour * 60 + now_ny.minute
        return (9 * 60 + 30) <= current_minutes <= (16 * 60)
    except:
        return False


def get_market_phase() -> str:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return "closed"

        current_minutes = now_ny.hour * 60 + now_ny.minute
        if (9 * 60 + 30) <= current_minutes <= (16 * 60):
            return "open"
        if (16 * 60) < current_minutes <= (20 * 60):
            return "after_hours"
        if (4 * 60) <= current_minutes < (9 * 60 + 30):
            return "pre_market"
        return "closed"
    except:
        return "closed"


def market_phase_label(phase: str) -> str:
    mapping = {
        "open": "مفتوح",
        "after_hours": "بعد الإغلاق",
        "pre_market": "قبل الافتتاح",
        "closed": "مغلق",
    }
    return mapping.get(str(phase or "closed"), "مغلق")


def get_snapshot_data(symbol):
    symbol = str(symbol).upper().strip()
    if not symbol:
        return {}

    try:
        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        data = r.get("ticker") or r.get("results") or {}
        last_trade = data.get("lastTrade", {}) or {}
        prev_day = data.get("prevDay", {}) or {}
        day = data.get("day", {}) or {}

        last_price = to_float(last_trade.get("p"))
        prev_close = to_float(prev_day.get("c"))
        prev_open = to_float(prev_day.get("o"))
        day_open = to_float(day.get("o"))
        day_high = to_float(day.get("h"))
        day_low = to_float(day.get("l"))
        day_close = to_float(day.get("c"))
        day_volume = to_float(day.get("v"))

        return {
            "last_price": last_price,
            "prev_close": prev_close,
            "prev_open": prev_open,
            "day_open": day_open,
            "day_high": day_high,
            "day_low": day_low,
            "day_close": day_close,
            "day_volume": day_volume,
            "updated": data.get("updated", 0),
        }
    except:
        return {}


def estimate_validity(trade_type: str, trend: str, volume_ratio: float, catalyst_score: float) -> str:
    if trade_type == "Breakout":
        if volume_ratio >= 1.3 and catalyst_score > 0:
            return "صالح اليوم وحتى الجلسة القادمة"
        if volume_ratio >= 1.0:
            return "صالح اليوم فقط"
        return "يحتاج تأكيد أثناء التداول" if is_market_open_now() else "يحتاج تأكيد بعد الافتتاح"

    if trade_type == "Pullback":
        if trend == "صاعد قوي" and volume_ratio >= 1.0:
            return "1-3 أيام"
        if trend == "صاعد":
            return "1-2 يوم"
        return "مراقبة يومية"

    return "مراقبة مشروطة"


def decision_priority(decision: str) -> int:
    if decision == "دخول قوي":
        return 3
    if decision == "دخول بحذر":
        return 2
    if decision == "مراقبة":
        return 1
    return 0


def compute_execution_status(trade_type: str, decision: str, trend: str, volume_ratio: float, catalyst_score: float, breakout_quality: str) -> str:
    if breakout_quality == "FAILED" and trade_type == "Breakout":
        return "AVOID"

    if decision == "دخول قوي" and trend == "صاعد قوي" and volume_ratio >= 1.2 and breakout_quality in {"STRONG", "WEAK"} and catalyst_score >= 0:
        return "READY"

    if decision in {"دخول قوي", "دخول بحذر"}:
        return "WAIT"

    if decision == "مراقبة":
        return "WAIT"

    return "AVOID"


def owner_decision(decision: str, trend: str, breakout_quality: str, volume_ratio: float, catalyst_score: float) -> str:
    if breakout_quality == "FAILED":
        return "لا تزد الكمية الآن - الأفضل الاحتفاظ بحذر أو التخفيف إذا كسر الدعم"
    if decision == "دخول قوي" and trend == "صاعد قوي" and volume_ratio >= 1.2:
        return "يمكن الشراء أو زيادة الكمية بشكل جزئي"
    if decision == "دخول بحذر":
        return "احتفاظ أو زيادة جزئية بحذر بعد تأكيد الحركة"
    if trend == "هابط":
        return "الأفضل عدم زيادة الكمية ومراقبة الدعم"
    return "احتفاظ ومراقبة - لا توجد زيادة واضحة الآن"


def breakout_quality_label(trade_type: str, momentum: str, body_strength: float, close_strength: float, volume_ratio: float) -> str:
    if trade_type != "Breakout":
        return "N/A"
    if momentum == "صاعد" and body_strength >= 0.6 and close_strength >= 0.75 and volume_ratio >= 1.2:
        return "STRONG"
    if body_strength < 0.35 or close_strength < 0.5 or volume_ratio < 0.8:
        return "FAILED"
    return "WEAK"


def read_csv(path):
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,")
            reader = csv.DictReader(f, dialect=dialect)
            rows = [clean_row(r) for r in reader]
            if rows:
                return rows
        except:
            pass

    for d in [";", ","]:
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=d)
            rows = [clean_row(r) for r in reader]
            if rows and len(rows[0].keys()) > 1:
                return rows

    return []


def load_sector():
    data = {}
    for r in read_csv("data/sector_industry.csv"):
        industry_id = str(r.get("IndustryId", "")).strip()
        if industry_id:
            data[industry_id] = {
                "industry": str(r.get("Industry", "")).strip(),
                "sector": str(r.get("Sector", "")).strip()
            }
    return data


def load_companies():
    data = {}
    for r in read_csv("data/companies.csv"):
        t = str(r.get("Ticker", "")).upper().strip()
        if t:
            data[t] = r
    return data


def load_latest(path):
    data = {}
    for r in read_csv(path):
        t = str(r.get("Ticker", "")).upper().strip()
        if not t:
            continue
        k = latest_key(r)
        if t not in data or k > data[t]["_k"]:
            r["_k"] = k
            data[t] = r
    for t in data:
        data[t].pop("_k", None)
    return data


SECTOR_DATA = load_sector()
COMPANIES_DATA = load_companies()
BALANCE_DATA = load_latest("data/balance_sheet.csv")
INCOME_DATA = load_latest("data/income_statement.csv")


def get_active_universe(max_symbols: int = 60):
    return get_scan_universe(max_symbols=max_symbols)


def get_daily_bars(symbol):
    try:
        today = datetime.utcnow().date()
        from_5y = (today - timedelta(days=365 * 5)).isoformat()
        to_date = today.isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{from_5y}/{to_date}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=22)
        return r.get("results", []) or []
    except:
        return []



def calculate_atr(daily_bars, period: int = 14) -> float:
    try:
        if not daily_bars or len(daily_bars) < period + 1:
            return 0.0
        true_ranges = []
        prev_close = None
        for row in daily_bars[-(period + 40):]:
            high = to_float(row.get("h"))
            low = to_float(row.get("l"))
            close = to_float(row.get("c"))
            if high <= 0 or low <= 0 or close <= 0:
                prev_close = close or prev_close
                continue
            if prev_close is None or prev_close <= 0:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
            prev_close = close
        if len(true_ranges) < period:
            return 0.0
        return safe_round(sum(true_ranges[-period:]) / period, 4)
    except:
        return 0.0


def get_atr_overlay(entry_price: float, daily_bars) -> dict:
    try:
        entry_price = float(entry_price or 0)
        atr_14 = float(calculate_atr(daily_bars, 14) or 0)
        if entry_price <= 0:
            return {
                "atr_14": 0.0,
                "atr_pct": 0.0,
                "volatility_label": "لا توجد بيانات كافية",
                "volatility_detail": "لا توجد بيانات كافية لتحديد التذبذب.",
                "atr_stop_suggestion": 0.0,
                "atr_target_1_suggestion": 0.0,
                "atr_target_2_suggestion": 0.0,
            }
        effective_atr = atr_14 if atr_14 > 0 else entry_price * 0.03
        atr_pct = (effective_atr / entry_price) * 100 if entry_price > 0 else 0.0
        if atr_pct <= 2.0:
            label = "هادئ"
            detail = "تذبذب السهم منخفض نسبيًا، ويمكن أن يتحمل وقفًا أقرب."
        elif atr_pct <= 4.5:
            label = "متوازن"
            detail = "تذبذب السهم طبيعي ومناسب لمعظم الخطط."
        elif atr_pct <= 7.0:
            label = "نشط"
            detail = "السهم متذبذب نسبيًا ويحتاج وقفًا أوسع وإدارة أدق."
        else:
            label = "عنيف"
            detail = "السهم عالي التذبذب وقد يضرب الوقف بسهولة إذا كان ضيقًا."
        return {
            "atr_14": safe_round(effective_atr, 4),
            "atr_pct": safe_round(atr_pct, 2),
            "volatility_label": label,
            "volatility_detail": detail,
            "atr_stop_suggestion": safe_round(entry_price - (effective_atr * 1.5)),
            "atr_target_1_suggestion": safe_round(entry_price + (effective_atr * 2.0)),
            "atr_target_2_suggestion": safe_round(entry_price + (effective_atr * 4.0)),
        }
    except:
        return {
            "atr_14": 0.0,
            "atr_pct": 0.0,
            "volatility_label": "لا توجد بيانات كافية",
            "volatility_detail": "لا توجد بيانات كافية لتحديد التذبذب.",
            "atr_stop_suggestion": 0.0,
            "atr_target_1_suggestion": 0.0,
            "atr_target_2_suggestion": 0.0,
        }


def _avg(values):
    vals = [float(x) for x in values if float(x or 0) > 0]
    return (sum(vals) / len(vals)) if vals else 0.0


def analyze_historical_behavior(daily_bars, current_setup: str = "") -> dict:
    base = {
        "historical_behavior_ready": False,
        "historical_breakout_success_pct": 0.0,
        "historical_pullback_success_pct": 0.0,
        "historical_volume_followthrough_pct": 0.0,
        "historical_breakout_cases": 0,
        "historical_pullback_cases": 0,
        "historical_volume_cases": 0,
        "historical_confidence_label": "منخفضة",
        "historical_breakout_speed_label": "لا توجد بيانات كافية",
        "historical_pullback_speed_label": "لا توجد بيانات كافية",
        "historical_behavior_label": "لا توجد بيانات كافية",
        "historical_behavior_detail": "لا توجد بيانات تاريخية كافية لبناء رأي سلوكي موثوق.",
    }
    try:
        if not daily_bars or len(daily_bars) < 120:
            return base
        bars = []
        for row in daily_bars:
            bars.append({
                "o": to_float(row.get("o")),
                "h": to_float(row.get("h")),
                "l": to_float(row.get("l")),
                "c": to_float(row.get("c")),
                "v": to_float(row.get("v")),
            })
        breakout_cases = breakout_success = 0
        breakout_days = []
        pullback_cases = pullback_success = 0
        pullback_days = []
        volume_cases = volume_success = 0
        for i in range(60, len(bars) - 6):
            close_i = bars[i]["c"]
            open_i = bars[i]["o"]
            high_i = bars[i]["h"]
            low_i = bars[i]["l"]
            vol_i = bars[i]["v"]
            if close_i <= 0 or high_i <= 0 or low_i <= 0 or open_i <= 0:
                continue
            prev20_high = max(b["h"] for b in bars[i-20:i])
            avg20_vol = _avg([b["v"] for b in bars[i-20:i]])
            sma20 = _avg([b["c"] for b in bars[i-20:i]])
            sma50 = _avg([b["c"] for b in bars[i-50:i]])
            future = bars[i+1:i+6]
            future_highs = [b["h"] for b in future if b["h"] > 0]
            future_lows = [b["l"] for b in future if b["l"] > 0]
            if not future_highs or not future_lows:
                continue

            if close_i >= prev20_high * 1.002 and avg20_vol > 0 and vol_i >= avg20_vol * 1.15:
                breakout_cases += 1
                for days_ahead, fb in enumerate(future, start=1):
                    if fb["h"] >= close_i * 1.03:
                        breakout_success += 1
                        breakout_days.append(days_ahead)
                        break

            near_support = False
            if sma20 > 0 and low_i <= sma20 * 1.01 and close_i >= sma20:
                near_support = True
            if not near_support and sma50 > 0 and low_i <= sma50 * 1.01 and close_i >= sma50:
                near_support = True
            if near_support and close_i > open_i:
                pullback_cases += 1
                for days_ahead, fb in enumerate(future, start=1):
                    if fb["h"] >= close_i * 1.03:
                        pullback_success += 1
                        pullback_days.append(days_ahead)
                        break

            day_change = ((close_i - open_i) / open_i) * 100 if open_i > 0 else 0.0
            if avg20_vol > 0 and vol_i >= avg20_vol * 1.5 and day_change >= 2.0:
                volume_cases += 1
                if max(future_highs) >= close_i * 1.02:
                    volume_success += 1

        breakout_pct = safe_round((breakout_success / breakout_cases) * 100, 1) if breakout_cases > 0 else 0.0
        pullback_pct = safe_round((pullback_success / pullback_cases) * 100, 1) if pullback_cases > 0 else 0.0
        volume_pct = safe_round((volume_success / volume_cases) * 100, 1) if volume_cases > 0 else 0.0

        def speed_label(days_list):
            if not days_list:
                return "لا توجد بيانات كافية"
            avg_days = sum(days_list) / len(days_list)
            if avg_days <= 2.0:
                return "سريع"
            if avg_days <= 3.5:
                return "متوسط"
            return "بطيء"

        sample_size = max(breakout_cases, pullback_cases, volume_cases)
        if sample_size >= 30:
            confidence = "عالية"
        elif sample_size >= 15:
            confidence = "متوسطة"
        elif sample_size >= 8:
            confidence = "محدودة"
        else:
            confidence = "منخفضة"

        breakout_speed = speed_label(breakout_days)
        pullback_speed = speed_label(pullback_days)

        setup = str(current_setup or "")
        if setup == "Breakout":
            main_pct = breakout_pct
            main_label = "يدعم الاختراق" if breakout_pct >= 60 else "محايد" if breakout_pct >= 45 else "ضعيف في الاختراق"
            parts = [f"في مثل حالة هذه الفرصة (اختراق)، نجحت الاختراقات المشابهة في {breakout_pct}% من {breakout_cases} حالة، وسرعة الحركة غالبًا {breakout_speed}."]
            if volume_cases > 0:
                parts.append(f"ومع السيولة القوية نجح الاستمرار في {volume_pct}% من {volume_cases} حالة.")
            parts.append(f"درجة الثقة {confidence}.")
            detail = " ".join(parts)
        elif setup == "Pullback":
            main_pct = pullback_pct
            main_label = "يحترم الارتداد" if pullback_pct >= 60 else "محايد" if pullback_pct >= 45 else "ضعيف في الارتداد"
            parts = [f"في مثل حالة هذه الفرصة (ارتداد)، نجحت الارتدادات المشابهة في {pullback_pct}% من {pullback_cases} حالة، وسرعة التعافي غالبًا {pullback_speed}."]
            if volume_cases > 0:
                parts.append(f"ومع السيولة القوية نجح الاستمرار في {volume_pct}% من {volume_cases} حالة.")
            parts.append(f"درجة الثقة {confidence}.")
            detail = " ".join(parts)
        else:
            main_pct = max(breakout_pct, pullback_pct, volume_pct)
            main_label = "سلوك تاريخي داعم" if main_pct >= 60 else "محايد" if main_pct >= 45 else "ضعيف"
            detail = f"نجاح الاختراقات {breakout_pct}% ({breakout_cases} حالة)، والارتدادات {pullback_pct}% ({pullback_cases} حالة)، واستجابة السيولة {volume_pct}% ({volume_cases} حالة). درجة الثقة {confidence}."

        return {
            "historical_behavior_ready": True,
            "historical_breakout_success_pct": breakout_pct,
            "historical_pullback_success_pct": pullback_pct,
            "historical_volume_followthrough_pct": volume_pct,
            "historical_breakout_cases": breakout_cases,
            "historical_pullback_cases": pullback_cases,
            "historical_volume_cases": volume_cases,
            "historical_confidence_label": confidence,
            "historical_breakout_speed_label": breakout_speed,
            "historical_pullback_speed_label": pullback_speed,
            "historical_behavior_label": main_label,
            "historical_behavior_detail": detail,
        }
    except:
        return base

def get_alignment_meta(stock: dict) -> dict:
    try:
        intraday = stock.get("intraday", {}) or {}
        trend = str(stock.get("trend", "") or "")
        opening_drive = str(intraday.get("opening_drive", "unknown") or "unknown")
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        above_vwap = bool(intraday.get("above_vwap_proxy", False))
        score = 50
        if trend == "صاعد قوي":
            score += 25
        elif trend == "صاعد":
            score += 18
        elif trend == "متذبذب":
            score += 4
        else:
            score -= 18
        if opening_drive == "صاعد":
            score += 12
        elif opening_drive == "متذبذب":
            score += 4
        elif opening_drive == "هابط":
            score -= 10
        if above_vwap:
            score += 8
        else:
            score -= 5
        if intraday_ratio >= 1.1:
            score += 6
        elif intraday_ratio < 0.85 and intraday_ratio > 0:
            score -= 6
        score = max(0, min(100, int(round(score))))
        if score >= 80:
            label = "متوافق جدًا"
            detail = "الاتجاه اليومي والحركة القريبة يدعمان بعضهما بشكل جيد."
        elif score >= 65:
            label = "متوافق"
            detail = "هناك توافق جيد لكنه ليس مثاليًا بالكامل."
        elif score >= 50:
            label = "متوسط"
            detail = "يوجد بعض التوافق لكن ما زال يحتاج حذرًا."
        else:
            label = "ضعيف"
            detail = "الحركة الحالية لا تتوافق جيدًا مع الاتجاه الأكبر."
        return {
            "alignment_score": score,
            "alignment_label": label,
            "alignment_detail": detail,
        }
    except:
        return {
            "alignment_score": 0,
            "alignment_label": "لا توجد بيانات كافية",
            "alignment_detail": "لا توجد بيانات كافية للتوافق الزمني.",
        }


def get_risk_profile_meta(stock: dict) -> dict:
    try:
        risk_pct = float(stock.get("display_risk_pct", stock.get("risk_pct", 0)) or 0)
        quality = float(stock.get("quality_score", 0) or 0)
        if risk_pct < 4 and quality >= 75:
            label = "منخفضة"
            detail = "الصفقة منخفضة المخاطرة نسبيًا مقارنة بجودتها."
            min_risk = 1.0
        elif (4 <= risk_pct <= 7) or (65 <= quality < 75):
            label = "متوسطة"
            detail = "الصفقة متوسطة المخاطرة وتحتاج التزامًا بالخطة."
            min_risk = 1.5
        else:
            label = "مرتفعة"
            detail = "الصفقة أعلى مخاطرة من المثالي، ولا تناسب إلا جزءًا صغيرًا من رأس المال."
            min_risk = 2.0
        fit_note = f"⚠️ هذه الصفقة غير مناسبة لك إذا كانت مخاطرتك أقل من {safe_round(min_risk, 1)}%." if min_risk >= 2 else f"✅ مناسبة لمخاطرة تقريبية بين {safe_round(min_risk, 1)}% و 2.0%."
        return {
            "risk_profile_label": label,
            "risk_profile_detail": detail,
            "risk_profile_fit_note": fit_note,
            "risk_profile_min_pct": min_risk,
        }
    except:
        return {
            "risk_profile_label": "لا توجد بيانات كافية",
            "risk_profile_detail": "لا توجد بيانات كافية لتوصيف المخاطرة.",
            "risk_profile_fit_note": "",
            "risk_profile_min_pct": 0.0,
        }



def get_prev_from_daily_bars(daily_bars):
    if not daily_bars:
        return None
    try:
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date()
        market_open = is_market_open_now()
        candidates = []
        for row in daily_bars:
            close_price = to_float(row.get("c"))
            if close_price <= 0:
                continue
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None
            if market_open and row_date == today_ny:
                continue
            candidates.append(row)
        source = candidates[-1] if candidates else daily_bars[-1]
        return {
            "price": to_float(source.get("c")),
            "high": to_float(source.get("h")),
            "low": to_float(source.get("l")),
            "volume": to_float(source.get("v")),
            "open": to_float(source.get("o")),
        }
    except:
        return None


def get_prev(symbol):
    try:
        r = http_get_json(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=12
        )
        results = r.get("results", [])
        if not results:
            return None
        d = results[0]
        return {
            "price": to_float(d.get("c")),
            "high": to_float(d.get("h")),
            "low": to_float(d.get("l")),
            "volume": to_float(d.get("v")),
            "open": to_float(d.get("o")),
        }
    except:
        return None


def get_latest_minute_price(symbol):
    try:
        today_ny = latest_market_date_str()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/"
            f"{today_ny}/{today_ny}?adjusted=true&sort=desc&limit=5&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=12)
        bars = r.get("results", []) or []
        if not bars:
            return {
                "available": False,
                "current_price": 0.0,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume": 0.0,
                "updated": 0,
            }

        bar = bars[0]
        return {
            "available": True,
            "current_price": to_float(bar.get("c")),
            "open": to_float(bar.get("o")),
            "high": to_float(bar.get("h")),
            "low": to_float(bar.get("l")),
            "volume": to_float(bar.get("v")),
            "updated": int(to_float(bar.get("t"))),
        }
    except:
        return {
            "available": False,
            "current_price": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0.0,
            "updated": 0,
        }


def get_snapshot_quote(symbol):
    try:
        symbol = str(symbol).upper().strip()
        phase = get_market_phase()
        cache_key = f"{symbol}:{phase}"
        cached = _cache_get(SNAPSHOT_CACHE, cache_key)
        if cached:
            return cached

        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        t = (r.get("ticker") or {})
        day = (t.get("day") or {})
        prev_day = (t.get("prevDay") or {})
        last_trade = (t.get("lastTrade") or {})
        min_data = (t.get("min") or {})

        last_price = to_float(last_trade.get("p"))
        prev_close = to_float(prev_day.get("c")) or 0.0
        day_open = to_float(day.get("o")) or 0.0
        day_high = to_float(day.get("h")) or 0.0
        day_low = to_float(day.get("l")) or 0.0
        day_volume = to_float(day.get("v")) or 0.0

        minute = get_latest_minute_price(symbol)

        current_price = last_price or to_float(min_data.get("c")) or to_float(day.get("c")) or 0.0
        if minute.get("available") and minute.get("current_price", 0) > 0:
            minute_price = to_float(minute.get("current_price", 0))
            if phase in {"open", "after_hours", "pre_market"}:
                current_price = minute_price or current_price
                if minute.get("high", 0) > 0:
                    day_high = max(day_high, to_float(minute.get("high", 0)))
                if minute.get("low", 0) > 0:
                    minute_low = to_float(minute.get("low", 0))
                    day_low = min(day_low, minute_low) if day_low > 0 else minute_low
                if minute.get("volume", 0) > 0:
                    day_volume = max(day_volume, to_float(minute.get("volume", 0)))

        if phase == "open":
            ttl = SNAPSHOT_CACHE_TTL_OPEN
        elif phase in {"after_hours", "pre_market"}:
            ttl = SNAPSHOT_CACHE_TTL_EXTENDED
        else:
            ttl = SNAPSHOT_CACHE_TTL_CLOSED

        change_vs_prev_close_pct = 0.0
        if current_price > 0 and prev_close > 0:
            change_vs_prev_close_pct = ((current_price - prev_close) / prev_close) * 100

        change_from_open_pct = 0.0
        if current_price > 0 and day_open > 0:
            change_from_open_pct = ((current_price - day_open) / day_open) * 100

        out = {
            "available": current_price > 0,
            "current_price": current_price,
            "previous_close": prev_close,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "volume": day_volume,
            "change_vs_prev_close_pct": change_vs_prev_close_pct,
            "change_from_open_pct": change_from_open_pct,
            "updated": int(time.time() * 1000),
            "source": "minute+snapshot" if minute.get("available") else "snapshot",
        }
        return _cache_set(SNAPSHOT_CACHE, cache_key, out, ttl)
    except:
        return {
            "available": False,
            "current_price": 0.0,
            "previous_close": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0.0,
            "change_vs_prev_close_pct": 0.0,
            "change_from_open_pct": 0.0,
            "updated": 0,
            "source": "error",
        }


def get_reference_info(symbol):
    symbol = str(symbol).upper().strip()
    if not symbol:
        return {"company": "", "sector": "", "industry": "", "industry_id": ""}

    if symbol in REF_INFO_CACHE:
        return REF_INFO_CACHE[symbol]

    out = {"company": "", "sector": "", "industry": "", "industry_id": ""}
    try:
        url = f"https://api.polygon.io/v3/reference/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        res = r.get("results", {}) or {}
        sic_description = str(res.get("sic_description", "")).strip()
        sector = ""
        industry = sic_description
        if " - " in sic_description:
            parts = [p.strip() for p in sic_description.split(" - ") if p.strip()]
            if len(parts) >= 2:
                sector = parts[0]
                industry = parts[-1]

        out = {
            "company": str(res.get("name", "")).strip(),
            "sector": sector,
            "industry": industry,
            "industry_id": ""
        }
    except:
        pass

    REF_INFO_CACHE[symbol] = out
    return out


def get_info(symbol):
    c = COMPANIES_DATA.get(symbol, {})
    industry_id = str(c.get("IndustryId", "")).strip()
    s = SECTOR_DATA.get(industry_id, {})

    company = str(c.get("Company Name", "")).strip()
    sector = str(s.get("sector", "")).strip()
    industry = str(s.get("industry", "")).strip()

    if company and sector and industry:
        return {
            "company": company,
            "sector": sector,
            "industry": industry,
            "industry_id": industry_id
        }

    ref = get_reference_info(symbol)
    return {
        "company": company or ref["company"],
        "sector": sector or ref["sector"],
        "industry": industry or ref["industry"],
        "industry_id": industry_id
    }


def get_history_levels(symbol, prev_data=None, daily_bars=None):
    if symbol in HISTORY_CACHE:
        return HISTORY_CACHE[symbol]

    out = {
        "year_high": 0.0,
        "ath_high": 0.0,
        "near_52w_high": False,
        "near_ath": False,
        "ath_breakout_zone": False,
    }

    bars = daily_bars if daily_bars is not None else get_daily_bars(symbol)
    if bars:
        ny = ZoneInfo("America/New_York")
        cutoff_52w = datetime.utcnow().date() - timedelta(days=365)
        highs_5 = []
        highs_52 = []
        for row in bars:
            high = to_float(row.get("h"))
            if high <= 0:
                continue
            highs_5.append(high)
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None
            if row_date and row_date >= cutoff_52w:
                highs_52.append(high)

        if highs_52:
            out["year_high"] = max(highs_52)
        if highs_5:
            out["ath_high"] = max(highs_5)

    prev = prev_data if prev_data is not None else get_prev_from_daily_bars(bars) or get_prev(symbol)
    if prev:
        price = prev["price"]
        if out["year_high"] > 0:
            out["near_52w_high"] = price >= out["year_high"] * 0.97
        if out["ath_high"] > 0:
            out["near_ath"] = price >= out["ath_high"] * 0.97
            out["ath_breakout_zone"] = price >= out["ath_high"] * 0.995

    HISTORY_CACHE[symbol] = out
    return out



def get_trend(symbol, daily_bars=None):
    try:
        data = daily_bars if daily_bars is not None else get_daily_bars(symbol)
        closes = [to_float(x.get("c")) for x in data if to_float(x.get("c")) > 0]
        if len(closes) < 50:
            return {"trend": "unknown", "ma20": 0.0, "ma50": 0.0}

        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes[-50:]) / 50
        price = closes[-1]

        if price > ma20 > ma50:
            trend = "صاعد قوي"
        elif price > ma50:
            trend = "صاعد"
        elif price < ma20 < ma50:
            trend = "هابط"
        else:
            trend = "متذبذب"

        return {"trend": trend, "ma20": ma20, "ma50": ma50}
    except:
        return {"trend": "unknown", "ma20": 0.0, "ma50": 0.0}



def get_session_elapsed_ratio() -> float:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return 0.0
        session_start = 9 * 60 + 30
        session_end = 16 * 60
        current_minutes = now_ny.hour * 60 + now_ny.minute
        if current_minutes <= session_start:
            return 0.0
        if current_minutes >= session_end:
            return 1.0
        elapsed = (current_minutes - session_start) / float(session_end - session_start)
        return clamp(elapsed, 0.0, 1.0)
    except:
        return 0.0


def get_volume_ratio(symbol, intraday=None, daily_bars=None):
    try:
        data = daily_bars if daily_bars is not None else get_daily_bars(symbol)
        if not data:
            return 1.0

        market_open = is_market_open_now()
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date()

        historical_volumes = []
        current_session_daily_volume = 0.0

        for row in data:
            volume = to_float(row.get("v"))
            if volume <= 0:
                continue
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None

            if market_open and row_date == today_ny:
                current_session_daily_volume = volume
                continue

            historical_volumes.append(volume)

        if len(historical_volumes) < 20:
            if len(historical_volumes) >= 5:
                avg_volume = sum(historical_volumes) / len(historical_volumes)
            else:
                return 1.0
        else:
            avg_volume = sum(historical_volumes[-20:]) / 20.0

        if avg_volume <= 0:
            return 1.0

        if market_open:
            intraday = intraday or get_intraday_snapshot(symbol)
            session_volume = float((intraday or {}).get("session_volume", 0) or 0)
            if session_volume <= 0:
                session_volume = current_session_daily_volume
            elapsed_ratio = float((intraday or {}).get("session_elapsed_ratio", 0) or 0)
            if elapsed_ratio <= 0:
                elapsed_ratio = get_session_elapsed_ratio()
            if session_volume > 0 and elapsed_ratio > 0:
                normalized_elapsed = max(elapsed_ratio, 0.08)
                projected_day_volume = session_volume / normalized_elapsed
                return clamp(projected_day_volume / avg_volume, 0.2, 8.0)

        latest_complete_volume = historical_volumes[-1] if historical_volumes else current_session_daily_volume
        if latest_complete_volume <= 0:
            return 1.0
        return clamp(latest_complete_volume / avg_volume, 0.2, 8.0)
    except:
        return 1.0



def get_intraday_snapshot(symbol):
    symbol = str(symbol).upper().strip()
    market_open = is_market_open_now()
    cache_key = f"{symbol}:{'open' if market_open else 'closed'}"
    ttl = INTRADAY_CACHE_TTL_OPEN if market_open else INTRADAY_CACHE_TTL_CLOSED

    cached = _cache_get(INTRADAY_CACHE, cache_key)
    if cached:
        return cached

    out = {
        "available": False,
        "market_open": market_open,
        "current_price": 0.0,
        "session_open": 0.0,
        "session_high": 0.0,
        "session_low": 0.0,
        "session_volume": 0.0,
        "avg_5m_volume": 0.0,
        "latest_5m_volume": 0.0,
        "intraday_volume_ratio": 0.0,
        "vwap_proxy": 0.0,
        "above_vwap_proxy": False,
        "opening_drive": "unknown",
        "bars_count": 0,
        "session_elapsed_ratio": 0.0,
        "projected_day_volume": 0.0,
        "recent_red_bars": 0,
        "recent_green_bars": 0,
        "pullback_volume_dry": False,
        "pullback_volume_ratio": 0.0,
        "spike_from_open_pct": 0.0,
        "pullback_from_high_pct": 0.0,
        "session_position_pct": 0.0,
    }

    if not market_open:
        return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

    try:
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date().isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/5/minute/"
            f"{today_ny}/{today_ny}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=15)
        bars = r.get("results", [])
        if not bars:
            return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

        volumes = [to_float(x.get("v")) for x in bars if to_float(x.get("v")) > 0]
        closes = [to_float(x.get("c")) for x in bars if to_float(x.get("c")) > 0]
        if not volumes or not closes:
            return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

        session_open = to_float(bars[0].get("o"))
        session_high = max(to_float(x.get("h")) for x in bars)
        lows = [to_float(x.get("l")) for x in bars if to_float(x.get("l")) > 0]
        session_low = min(lows) if lows else 0.0
        session_volume = sum(volumes)
        latest_5m_volume = volumes[-1]
        avg_5m_volume = sum(volumes) / len(volumes) if volumes else 0.0
        intraday_volume_ratio = latest_5m_volume / avg_5m_volume if avg_5m_volume > 0 else 0.0

        weighted_total = 0.0
        volume_total = 0.0
        for bar in bars:
            typical = (to_float(bar.get("h")) + to_float(bar.get("l")) + to_float(bar.get("c"))) / 3
            vol = to_float(bar.get("v"))
            if vol > 0:
                weighted_total += typical * vol
                volume_total += vol

        vwap_proxy = weighted_total / volume_total if volume_total > 0 else closes[-1]
        current_price = closes[-1]
        first_close = to_float(bars[0].get("c"))
        elapsed_ratio = get_session_elapsed_ratio()
        normalized_elapsed = max(elapsed_ratio, 0.08) if elapsed_ratio > 0 else 0.0
        projected_day_volume = (session_volume / normalized_elapsed) if normalized_elapsed > 0 else 0.0

        if current_price > session_open and current_price >= first_close:
            opening_drive = "صاعد"
        elif current_price < session_open and current_price <= first_close:
            opening_drive = "هابط"
        else:
            opening_drive = "متذبذب"

        recent_red_bars = 0
        recent_green_bars = 0
        recent_slice = bars[-4:]
        for idx in range(1, len(recent_slice)):
            prev_close = to_float(recent_slice[idx - 1].get("c"))
            cur_close = to_float(recent_slice[idx].get("c"))
            if cur_close < prev_close:
                recent_red_bars += 1
            elif cur_close > prev_close:
                recent_green_bars += 1

        last3 = volumes[-3:] if len(volumes) >= 3 else volumes
        prior6 = volumes[-9:-3] if len(volumes) >= 9 else volumes[:-3]
        last3_avg = sum(last3) / len(last3) if last3 else 0.0
        prior6_avg = sum(prior6) / len(prior6) if prior6 else avg_5m_volume
        pullback_volume_ratio = (last3_avg / prior6_avg) if prior6_avg > 0 else 0.0
        pullback_volume_dry = pullback_volume_ratio <= 0.85 if prior6_avg > 0 else False

        spike_from_open_pct = ((session_high - session_open) / session_open) if session_open > 0 and session_high > 0 else 0.0
        pullback_from_high_pct = ((session_high - current_price) / session_high) if session_high > 0 and current_price > 0 else 0.0
        session_range = max(session_high - session_low, 0.0001)
        session_position_pct = ((current_price - session_low) / session_range) * 100 if session_range > 0 else 0.0

        out = {
            "available": True,
            "market_open": market_open,
            "current_price": current_price,
            "session_open": session_open,
            "session_high": session_high,
            "session_low": session_low,
            "session_volume": session_volume,
            "avg_5m_volume": avg_5m_volume,
            "latest_5m_volume": latest_5m_volume,
            "intraday_volume_ratio": intraday_volume_ratio,
            "vwap_proxy": vwap_proxy,
            "above_vwap_proxy": current_price >= vwap_proxy if vwap_proxy > 0 else False,
            "opening_drive": opening_drive,
            "bars_count": len(bars),
            "session_elapsed_ratio": safe_round(elapsed_ratio, 4),
            "projected_day_volume": safe_round(projected_day_volume),
            "recent_red_bars": recent_red_bars,
            "recent_green_bars": recent_green_bars,
            "pullback_volume_dry": pullback_volume_dry,
            "pullback_volume_ratio": safe_round(pullback_volume_ratio, 2),
            "spike_from_open_pct": safe_round(spike_from_open_pct * 100, 2),
            "pullback_from_high_pct": safe_round(pullback_from_high_pct * 100, 2),
            "session_position_pct": safe_round(session_position_pct, 2),
        }
    except:
        pass

    return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)


def build_live_price_block(symbol, prev_data, intraday_data):
    phase = get_market_phase()
    prev_price = to_float(prev_data.get("price", 0)) if prev_data else 0.0
    prev_open = to_float(prev_data.get("open", 0)) if prev_data else 0.0
    prev_high = to_float(prev_data.get("high", 0)) if prev_data else 0.0
    prev_low = to_float(prev_data.get("low", 0)) if prev_data else 0.0
    prev_volume = to_float(prev_data.get("volume", 0)) if prev_data else 0.0

    snap = {}
    if not (phase == "open" and intraday_data.get("available") and to_float(intraday_data.get("current_price", 0)) > 0):
        snap = get_snapshot_quote(symbol)

    current_price = prev_price
    open_price = prev_open
    previous_close = prev_price
    change_vs_prev_close_pct = 0.0
    change_from_open_pct = 0.0
    price_source = "previous_close"
    price_reliable_for_execution = False

    if phase == "open" and intraday_data.get("available") and to_float(intraday_data.get("current_price", 0)) > 0:
        current_price = to_float(intraday_data.get("current_price", 0)) or prev_price
        open_price = to_float(intraday_data.get("session_open", 0)) or prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100
        if previous_close > 0 and current_price > 0:
            change_vs_prev_close_pct = ((current_price - previous_close) / previous_close) * 100
        price_source = "live_intraday"
        price_reliable_for_execution = True
    elif phase in {"after_hours", "pre_market"} and snap.get("available") and to_float(snap.get("current_price", 0)) > 0:
        current_price = to_float(snap.get("current_price", prev_price)) or prev_price
        open_price = to_float(snap.get("open", prev_open)) or prev_open
        previous_close = to_float(snap.get("previous_close", prev_price)) or prev_price
        change_from_open_pct = to_float(snap.get("change_from_open_pct", 0))
        change_vs_prev_close_pct = to_float(snap.get("change_vs_prev_close_pct", 0))
        price_source = phase
        price_reliable_for_execution = True
    elif phase == "closed":
        current_price = prev_price
        open_price = prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100
        price_source = "previous_close"
        price_reliable_for_execution = False
    elif snap.get("available") and to_float(snap.get("current_price", 0)) > 0:
        current_price = to_float(snap.get("current_price", prev_price)) or prev_price
        open_price = to_float(snap.get("open", prev_open)) or prev_open
        previous_close = to_float(snap.get("previous_close", prev_price)) or prev_price
        change_from_open_pct = to_float(snap.get("change_from_open_pct", 0))
        change_vs_prev_close_pct = to_float(snap.get("change_vs_prev_close_pct", 0))
        price_source = str(snap.get("source", "snapshot") or "snapshot")
        price_reliable_for_execution = False
    else:
        current_price = 0.0 if phase in {"open", "after_hours", "pre_market"} else prev_price
        open_price = prev_open
        previous_close = prev_price
        price_source = "unavailable_realtime"
        price_reliable_for_execution = False

    price_source_label_map = {
        "live_intraday": "مباشر أثناء التداول",
        "after_hours": "بعد الإغلاق",
        "pre_market": "قبل الافتتاح",
        "previous_close": "آخر إغلاق",
        "unavailable_realtime": "بيانات لحظية غير متاحة",
        "snapshot": "لقطة سوق",
        "minute+snapshot": "دقيقة + لقطة",
    }

    display_price = current_price if current_price > 0 else previous_close
    display_price_label = "السعر الحالي" if current_price > 0 else "آخر إغلاق"
    live_price_available = current_price > 0
    display_change_pct = change_vs_prev_close_pct if previous_close > 0 else change_from_open_pct
    display_change_available = abs(display_change_pct) > 0 or live_price_available

    high_live = prev_high
    low_live = prev_low
    volume_live = prev_volume
    last_price_update_ms = int(time.time() * 1000) if phase == "open" and intraday_data.get("available") else int(to_float(snap.get("updated", 0)))

    if phase == "open" and intraday_data.get("available"):
        high_live = safe_round(to_float(intraday_data.get("session_high", 0)) or prev_high)
        low_live = safe_round(to_float(intraday_data.get("session_low", 0)) or prev_low)
        volume_live = safe_round(to_float(intraday_data.get("session_volume", 0)) or prev_volume)
    else:
        high_live = safe_round(to_float(snap.get("high", prev_high)) or prev_high)
        low_live = safe_round(to_float(snap.get("low", prev_low)) or prev_low)
        volume_live = safe_round(to_float(snap.get("volume", prev_volume)) or prev_volume)

    return {
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "current_price_live": safe_round(current_price),
        "open_price_live": safe_round(open_price),
        "previous_close_live": safe_round(previous_close),
        "change_from_open_pct": safe_round(change_from_open_pct),
        "change_vs_prev_close_pct": safe_round(change_vs_prev_close_pct),
        "display_price": safe_round(display_price),
        "display_price_label": display_price_label,
        "display_change_pct": safe_round(display_change_pct),
        "display_change_available": display_change_available,
        "live_price_available": live_price_available,
        "high_live": high_live,
        "low_live": low_live,
        "volume_live": volume_live,
        "price_source": price_source,
        "price_source_label": price_source_label_map.get(price_source, price_source),
        "price_reliable_for_execution": price_reliable_for_execution,
        "last_price_update_ms": last_price_update_ms,
        "last_price_update_label": datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S"),
    }



def compute_volume_pace_ratio(intraday: dict, daily_volume_ratio: float) -> float:
    try:
        if not intraday or not intraday.get("available"):
            return float(daily_volume_ratio or 0)
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        latest_5m = float(intraday.get("latest_5m_volume", 0) or 0)
        avg_5m = float(intraday.get("avg_5m_volume", 0) or 0)
        pullback_volume_ratio = float(intraday.get("pullback_volume_ratio", 0) or 0)
        burst_ratio = (latest_5m / avg_5m) if avg_5m > 0 else intraday_ratio
        if pullback_volume_ratio > 0:
            return clamp(max(float(daily_volume_ratio or 0), intraday_ratio, burst_ratio, 1 / max(pullback_volume_ratio, 0.01) if pullback_volume_ratio < 1 else pullback_volume_ratio), 0.2, 8.0)
        return clamp(max(float(daily_volume_ratio or 0), intraday_ratio, burst_ratio), 0.2, 8.0)
    except:
        return float(daily_volume_ratio or 0)


def get_effective_volume_ratio(volume_ratio: float, intraday: dict) -> float:
    try:
        effective = float(volume_ratio or 0)
        if intraday and intraday.get("available"):
            intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
            pace_ratio = compute_volume_pace_ratio(intraday, volume_ratio)
            projected_bias = 0.0
            projected_day_volume = float(intraday.get("projected_day_volume", 0) or 0)
            session_volume = float(intraday.get("session_volume", 0) or 0)
            if projected_day_volume > 0 and session_volume > 0:
                projected_bias = projected_day_volume / max(session_volume, 1.0)
            effective = max(
                effective,
                intraday_ratio * 0.9,
                pace_ratio,
                min(max(float(volume_ratio or 0), 0.0) + (projected_bias * 0.02), 8.0)
            )
        return clamp(effective, 0.2, 8.0)
    except:
        return float(volume_ratio or 0)


def next_business_day(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d

def prev_business_day(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def count_business_days_exclusive(start_date, end_date):
    days = 0
    d = start_date + timedelta(days=1)
    while d <= end_date:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days

def trading_sessions_since_news(published_utc: str) -> int:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        current_trade_date = now_ny.date()
        if current_trade_date.weekday() >= 5:
            current_trade_date = prev_business_day(current_trade_date)
        elif (now_ny.hour * 60 + now_ny.minute) < (9 * 60 + 30):
            current_trade_date = prev_business_day(current_trade_date - timedelta(days=1))

        published = datetime.fromisoformat(str(published_utc).replace("Z", "+00:00"))
        pub_ny = published.astimezone(ny)
        reaction_date = pub_ny.date()
        minutes = pub_ny.hour * 60 + pub_ny.minute

        if reaction_date.weekday() >= 5:
            reaction_date = next_business_day(reaction_date)
        elif minutes >= 16 * 60:
            reaction_date = next_business_day(reaction_date + timedelta(days=1))
        else:
            reaction_date = next_business_day(reaction_date)

        return count_business_days_exclusive(reaction_date, current_trade_date)
    except:
        return 999

def classify_news_impact(title_lower: str, sessions_since: int):
    positive_keywords = [
        "beat", "beats", "strong guidance", "raises guidance", "buyback", "surge",
        "jumps", "soars", "wins", "upgrade", "partnership", "contract", "record revenue",
        "secures", "launch", "breakthrough", "approval", "expands", "growth"
    ]
    negative_keywords = [
        "miss", "misses", "cuts guidance", "downgrade", "offering", "dilution", "lawsuit",
        "probe", "investigation", "warning", "declines", "falls", "plunges", "recall", "delay",
        "bankruptcy", "default"
    ]

    is_positive = any(k in title_lower for k in positive_keywords)
    is_negative = any(k in title_lower for k in negative_keywords)

    pos_score = 0
    neg_score = 0

    if is_positive:
        if sessions_since == 0:
            pos_score = 10
        elif sessions_since == 1:
            pos_score = 7
        elif sessions_since == 2:
            pos_score = 4
        else:
            pos_score = 0

    if is_negative:
        if sessions_since == 0:
            neg_score = -10
        elif sessions_since == 1:
            neg_score = -8
        elif sessions_since == 2:
            neg_score = -6
        elif sessions_since <= 5:
            neg_score = -3
        else:
            neg_score = 0

    note = ""
    if is_positive and pos_score > 0:
        note = "محفز إيجابي حديث"
    elif is_negative and neg_score < 0:
        note = "محفز سلبي حديث"

    return pos_score + neg_score, note




def classify_news_freshness_label(sessions_since: int) -> tuple[str, int]:
    if sessions_since <= 0:
        return "حديث جدًا", 100
    if sessions_since == 1:
        return "حديث", 85
    if sessions_since <= 3:
        return "حديث نسبيًا", 65
    if sessions_since <= 5:
        return "أقدم قليلًا", 40
    return "قديم", 15


def detect_news_category(title_lower: str) -> str:
    insider_negative = [
        "ceo sold", "chief executive officer sold", "insider sold", "insider selling", "director sold", "officer sold",
        "board member sold", "sold shares", "disposed shares", "sale of shares", "share sale", "unloaded shares"
    ]
    legal_negative = ["lawsuit", "investigation", "investigates", "class action", "investor alert", "law firm", "claims on behalf"]
    clear_negative = [
        "downgrade", "guidance cut", "missed estimates", "weak earnings", "offering", "crashed", "crash",
        "drops", "drop", "plunges", "plunge", "fell", "falls", "declines", "slumps", "slump", "revenue drops"
    ]
    positive = [
        "upgrade", "approval", "partnership", "contract", "buyback", "beats", "beat estimates", "record revenue",
        "raises guidance", "strong guidance", "wins", "surge", "soars", "jumps", "order", "award", "fda"
    ]
    opinion_only = [
        "is it finally time to buy", "is it time to buy", "why this stock", "opinion", "analysis", "article",
        "i finally pulled the trigger", "here s what this means", "here's what this means", "better ev stock"
    ]

    if any(k in title_lower for k in insider_negative):
        return "negative"
    if any(k in title_lower for k in legal_negative):
        return "legal"
    if any(k in title_lower for k in clear_negative):
        return "negative"
    if any(k in title_lower for k in positive):
        return "positive"
    if any(k in title_lower for k in opinion_only):
        return "opinion"
    return "neutral"


def get_news_bundle(symbol, company_name=""):
    bundle = {
        "news_note": "لا يوجد خبر حديث",
        "news_title": "",
        "news_badge": "",
        "news_category": "neutral",
        "news_sentiment": "neutral",
        "news_freshness_label": "",
        "news_published_utc": "",
        "news_sessions_since": 999,
        "catalyst_score": 0,
    }
    try:
        url = f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=10&order=desc&sort=published_utc&apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        results = r.get("results", [])
        if not results:
            return bundle

        variants = get_company_name_variants(company_name)
        best = None
        best_score = -999

        for item in results:
            title = str(item.get("title", "") or "").strip()
            title_lower = normalize_text(title)
            if not title:
                continue

            related = [str(x).upper().strip() for x in item.get("tickers", []) if str(x).strip()]
            published_utc = str(item.get("published_utc", "") or "")
            sessions_since = trading_sessions_since_news(published_utc)
            freshness_label, _ = classify_news_freshness_label(sessions_since)

            relevance = 0
            if symbol in related:
                relevance += 3
            if any(v and v in title_lower for v in variants):
                relevance += 2
            if symbol.lower() in title_lower:
                relevance += 1

            impact, note = classify_news_impact(title_lower, sessions_since)
            category = detect_news_category(title_lower)
            if impact == 0 or not note:
                continue

            total = relevance + abs(impact)
            if total > best_score:
                best_score = total
                best = {
                    "title": title,
                    "note": note,
                    "impact": impact,
                    "category": category,
                    "freshness_label": freshness_label,
                    "published_utc": published_utc,
                    "sessions_since": sessions_since,
                }

        if best:
            bundle.update({
                "news_note": best["title"],
                "news_title": best["title"],
                "news_badge": best["note"],
                "news_category": best["category"],
                "news_sentiment": "positive" if best["impact"] > 0 else "negative",
                "news_freshness_label": best["freshness_label"],
                "news_published_utc": best["published_utc"],
                "news_sessions_since": best["sessions_since"],
                "catalyst_score": best["impact"],
            })
    except:
        pass
    return bundle

def get_news(symbol, company_name=""):
    bundle = get_news_bundle(symbol, company_name)
    return bundle.get("news_note", "لا يوجد خبر حديث"), bundle.get("catalyst_score", 0)


def is_halal(sector, industry, total_assets, cash, total_debt):
    sector_l = str(sector).lower().strip()
    industry_l = str(industry).lower().strip()

    if sector_l in HARAM_SECTORS:
        return False, f"مرفوض شرعيًا: القطاع ({sector}) غير مقبول"

    for kw in HARAM_INDUSTRY_KEYWORDS:
        if kw in industry_l:
            return False, f"مرفوض شرعيًا: الصناعة تحتوي ({kw})"

    if total_assets <= 0:
        return True, "مقبول مبدئيًا"

    debt_ratio = total_debt / total_assets if total_assets > 0 else 0
    cash_ratio = cash / total_assets if total_assets > 0 else 0

    if debt_ratio > 0.33:
        return False, f"مرفوض شرعيًا: الديون {safe_round(debt_ratio*100)}% من الأصول"
    if cash_ratio > 0.33:
        return False, f"مرفوض شرعيًا: النقد {safe_round(cash_ratio*100)}% من الأصول"

    return True, "مطابق للضوابط الشرعية المبدئية"


def get_financials(symbol, prev_data=None):
    b = BALANCE_DATA.get(symbol, {})
    i = INCOME_DATA.get(symbol, {})

    total_assets = to_float(b.get("Total Assets", 0))
    cash = to_float(b.get("Cash And Cash Equivalents", 0))
    total_debt = to_float(b.get("Total Debt", 0))
    shares = to_float(i.get("Shares (Diluted)", 0)) or to_float(i.get("Shares (Basic)", 0))
    prev = prev_data if prev_data is not None else get_prev(symbol)
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


def compute_pullback_context(current_price: float, high_price: float, low_price: float, intraday: dict, trend: str) -> dict:
    try:
        session_high = float((intraday or {}).get("session_high", 0) or 0)
        session_low = float((intraday or {}).get("session_low", 0) or 0)
        session_open = float((intraday or {}).get("session_open", 0) or 0)
        above_vwap = bool((intraday or {}).get("above_vwap_proxy", False))
        spike_from_open_pct = float((intraday or {}).get("spike_from_open_pct", 0) or 0)
        pullback_volume_dry = bool((intraday or {}).get("pullback_volume_dry", False))
        recent_red_bars = int((intraday or {}).get("recent_red_bars", 0) or 0)
        session_position_pct = float((intraday or {}).get("session_position_pct", 0) or 0)

        swing_high = session_high if session_high > 0 else high_price
        base_low = session_low if session_low > 0 else low_price
        if session_open > 0 and base_low > 0:
            base_low = min(base_low, session_open)
        swing_low = base_low if base_low > 0 else low_price
        swing_range = max(swing_high - swing_low, 0.0)

        fib_38 = 0.0
        fib_50 = 0.0
        fib_62 = 0.0
        in_pullback_zone = False
        near_support = current_price <= low_price * 1.05 if low_price > 0 else False
        strong_spike = False
        pullback_score = 0
        pattern_label = ""

        if swing_high > 0 and swing_low > 0 and swing_range > 0:
            fib_38 = swing_high - (swing_range * 0.382)
            fib_50 = swing_high - (swing_range * 0.5)
            fib_62 = swing_high - (swing_range * 0.618)
            zone_low = min(fib_38, fib_62)
            zone_high = max(fib_38, fib_62)
            in_pullback_zone = zone_low <= current_price <= zone_high if current_price > 0 else False
            strong_spike = spike_from_open_pct >= 3.0 or ((swing_high - swing_low) / max(swing_low, 0.01)) >= 0.04
            if trend in {"صاعد", "صاعد قوي"}:
                if strong_spike:
                    pullback_score += 25
                if in_pullback_zone:
                    pullback_score += 24
                if pullback_volume_dry:
                    pullback_score += 18
                if 2 <= recent_red_bars <= 4:
                    pullback_score += 10
                if above_vwap:
                    pullback_score += 8
                if session_position_pct >= 45:
                    pullback_score += 8
                elif session_position_pct < 30:
                    pullback_score -= 8
                if near_support:
                    pullback_score += 10

            if strong_spike and in_pullback_zone and pullback_volume_dry:
                pattern_label = "ارتداد فيبوناتشي بعد قفزة قوية"
            elif in_pullback_zone:
                pattern_label = "ارتداد داخل منطقة فيبوناتشي"
            elif near_support:
                pattern_label = "ارتداد قرب دعم يومي"

            return {
                "fib_38": safe_round(fib_38),
                "fib_50": safe_round(fib_50),
                "fib_62": safe_round(fib_62),
                "pullback_zone_low": safe_round(zone_low),
                "pullback_zone_high": safe_round(zone_high),
                "in_pullback_zone": in_pullback_zone,
                "near_support": near_support,
                "strong_spike_detected": strong_spike,
                "pullback_score": max(0, min(99, int(round(pullback_score)))),
                "pullback_pattern_label": pattern_label,
                "pullback_volume_label": "جفاف سيولة على الارتداد ✅" if pullback_volume_dry else "سيولة الارتداد ما زالت مرتفعة ⚠️",
                "pullback_multi_bar_label": f"{recent_red_bars} شموع تراجع" if recent_red_bars > 0 else "لا يوجد تراجع متعدد واضح",
                "pullback_candidate": trend in {"صاعد", "صاعد قوي"} and (in_pullback_zone or near_support),
            }

        return {
            "fib_38": 0.0,
            "fib_50": 0.0,
            "fib_62": 0.0,
            "pullback_zone_low": 0.0,
            "pullback_zone_high": 0.0,
            "in_pullback_zone": False,
            "near_support": near_support,
            "strong_spike_detected": False,
            "pullback_score": 0,
            "pullback_pattern_label": "",
            "pullback_volume_label": "",
            "pullback_multi_bar_label": "",
            "pullback_candidate": trend in {"صاعد", "صاعد قوي"} and near_support,
        }
    except:
        return {
            "fib_38": 0.0,
            "fib_50": 0.0,
            "fib_62": 0.0,
            "pullback_zone_low": 0.0,
            "pullback_zone_high": 0.0,
            "in_pullback_zone": False,
            "near_support": False,
            "strong_spike_detected": False,
            "pullback_score": 0,
            "pullback_pattern_label": "",
            "pullback_volume_label": "",
            "pullback_multi_bar_label": "",
            "pullback_candidate": False,
        }


def compute_breakout_levels(current_price: float, high_price: float, low_price: float, intraday: dict, trade_type: str, pullback_context: dict | None = None):
    breakout_price = 0.0
    confirmation_price = 0.0
    entry_price_real = 0.0
    late_entry_price = 0.0
    breakout_status = ""
    pullback_context = pullback_context or {}

    if trade_type == "Breakout" and high_price > 0:
        breakout_price = high_price
        confirmation_price = high_price * 1.0025
        entry_price_real = high_price * 1.005
        late_entry_price = high_price * 1.015

        if current_price < breakout_price:
            breakout_status = "قبل الاختراق"
        elif breakout_price <= current_price < confirmation_price:
            breakout_status = "اختراق أولي"
        elif confirmation_price <= current_price <= entry_price_real:
            breakout_status = "تأكيد الاختراق"
        elif entry_price_real < current_price <= late_entry_price:
            breakout_status = "اختراق مؤكد - دخول بحذر"
        else:
            breakout_status = "اختراق متأخر"
    elif trade_type == "Pullback":
        fib_38 = float(pullback_context.get("fib_38", 0) or 0)
        fib_50 = float(pullback_context.get("fib_50", 0) or 0)
        fib_62 = float(pullback_context.get("fib_62", 0) or 0)
        zone_low = float(pullback_context.get("pullback_zone_low", 0) or 0)
        zone_high = float(pullback_context.get("pullback_zone_high", 0) or 0)

        breakout_price = high_price if high_price > 0 else (fib_38 if fib_38 > 0 else low_price * 1.03)
        confirmation_price = fib_38 if fib_38 > 0 else low_price * 1.02
        entry_price_real = fib_50 if fib_50 > 0 else (current_price if current_price > 0 else low_price * 1.01)
        if current_price > 0 and zone_high > 0 and current_price < zone_low:
            entry_price_real = zone_low
        late_entry_price = zone_high if zone_high > 0 else (fib_38 if fib_38 > 0 else low_price * 1.025)
        breakout_status = str(pullback_context.get("pullback_pattern_label", "ارتداد من دعم") or "ارتداد من دعم")
        if fib_62 > 0 and current_price > 0 and current_price < fib_62 * 0.995:
            breakout_status = "كسر منطقة الارتداد"

    return {
        "breakout_price": safe_round(breakout_price),
        "confirmation_price": safe_round(confirmation_price),
        "entry_price_real": safe_round(entry_price_real),
        "late_entry_price": safe_round(late_entry_price),
        "breakout_status": breakout_status,
    }


def compute_timing_layer(current_price: float, intraday: dict, effective_volume_ratio: float, levels: dict, market_phase: str):
    breakout_price = float(levels.get("breakout_price", 0) or 0)
    confirmation_price = float(levels.get("confirmation_price", 0) or 0)
    entry_price_real = float(levels.get("entry_price_real", 0) or 0)
    late_entry_price = float(levels.get("late_entry_price", 0) or 0)

    intraday_ratio = float((intraday or {}).get("intraday_volume_ratio", 0) or 0)
    vwap_proxy = float((intraday or {}).get("vwap_proxy", 0) or 0)
    above_vwap = bool((intraday or {}).get("above_vwap_proxy", False))
    opening_drive = str((intraday or {}).get("opening_drive", "unknown") or "unknown")
    market_open = bool((intraday or {}).get("market_open", False))

    strong_volume = effective_volume_ratio >= 1.1 or intraday_ratio >= 1.2
    excellent_volume = effective_volume_ratio >= 1.25 or intraday_ratio >= 1.5

    if market_phase == "open":
        if market_open and vwap_proxy > 0:
            vwap_status = "فوق VWAP ✅" if above_vwap else "تحت VWAP ❌"
        else:
            vwap_status = "VWAP غير متاح"
    else:
        vwap_status = "VWAP يكتمل أثناء السوق"

    if excellent_volume:
        volume_status = "سيولة قوية جدًا ✅"
    elif strong_volume:
        volume_status = "سيولة داعمة ✅"
    elif effective_volume_ratio >= 0.9 or intraday_ratio >= 0.95:
        volume_status = "سيولة متوسطة ⚠️"
    else:
        volume_status = "سيولة ضعيفة ❌"

    timing_signal = "مراقبة 👀"
    timing_reason = "تحت المراقبة"
    smart_entry_price = entry_price_real if entry_price_real > 0 else confirmation_price
    smart_stop_price = 0.0
    smart_target_1 = 0.0

    if confirmation_price > 0:
        if current_price < breakout_price:
            timing_signal = "انتظار اختراق ⏳"
            timing_reason = f"السعر ما زال تحت الاختراق {safe_round(breakout_price)}"
            smart_entry_price = confirmation_price
        elif breakout_price <= current_price < confirmation_price:
            timing_signal = "انتظار تأكيد 📊"
            timing_reason = f"تم الكسر الأولي ويحتاج الثبات فوق {safe_round(confirmation_price)}"
            smart_entry_price = confirmation_price
        elif confirmation_price <= current_price <= entry_price_real:
            if market_phase == "open":
                if above_vwap and strong_volume and opening_drive != "هابط":
                    timing_signal = "جاهز 🔥"
                    timing_reason = "السعر فوق التأكيد وفوق VWAP والسيولة داعمة"
                elif strong_volume:
                    timing_signal = "دخول بحذر 🟠"
                    timing_reason = "السعر فوق التأكيد لكن يحتاج ثباتًا لحظيًا أفضل"
                else:
                    timing_signal = "انتظار تأكيد 📊"
                    timing_reason = "السعر في منطقة جيدة لكن السيولة ليست كافية بعد"
            else:
                timing_signal = "انتظار تأكيد 📊"
                timing_reason = "السهم في منطقة جيدة، وقرار التنفيذ الأفضل يكون مع افتتاح السوق"
            smart_entry_price = entry_price_real
        elif entry_price_real < current_price <= late_entry_price:
            if market_phase == "open" and above_vwap and excellent_volume:
                timing_signal = "دخول بحذر 🟠"
                timing_reason = "السعر تجاوز الدخول المثالي لكن ما زال ضمن آخر دخول مناسب"
            else:
                timing_signal = "متأخر ⚠️"
                timing_reason = "السعر تجاوز الدخول المثالي وأصبح أقل جاذبية"
            smart_entry_price = late_entry_price
        elif late_entry_price > 0 and current_price > late_entry_price:
            timing_signal = "متأخر ⚠️"
            timing_reason = "السعر تجاوز آخر دخول مناسب - لا تطارد"
            smart_entry_price = late_entry_price

    if entry_price_real > 0:
        smart_stop_price = max(0.0, entry_price_real * 0.97)
        smart_target_1 = entry_price_real * 1.04

    return {
        "timing_signal": timing_signal,
        "timing_reason": timing_reason,
        "vwap_status": vwap_status,
        "volume_status": volume_status,
        "smart_entry_price": safe_round(smart_entry_price),
        "smart_stop_price": safe_round(smart_stop_price),
        "smart_target_1": safe_round(smart_target_1),
    }


def trade_plan_pro(symbol):
    daily_bars = get_daily_bars(symbol)
    prev = get_prev_from_daily_bars(daily_bars) or get_prev(symbol)
    if not prev:
        return None

    info = get_info(symbol)
    financials = get_financials(symbol, prev)
    hist = get_history_levels(symbol, prev, daily_bars)
    trend_data = get_trend(symbol, daily_bars)
    intraday = get_intraday_snapshot(symbol)
    volume_ratio = get_volume_ratio(symbol, intraday, daily_bars)
    news_bundle = get_news_bundle(symbol, info["company"])
    news_note = news_bundle.get("news_note", "لا يوجد خبر حديث")
    catalyst_score = news_bundle.get("catalyst_score", 0)

    halal_ok, halal_reason = is_halal(
        info["sector"], info["industry"],
        financials["total_assets"], financials["cash"], financials["total_debt"]
    )

    if not halal_ok:
        return {
            "symbol": symbol,
            "type": "Excluded",
            "decision": "مرفوض شرعياً",
            "entry": 0,
            "stop_loss": 0,
            "target_1": 0,
            "target_2": 0,
            "risk_pct": 0,
            "quality_score": 0,
            "rank_label": "-",
            "valid_for": "-",
            "trend": trend_data["trend"],
            "volume_ratio": volume_ratio,
            "effective_volume_ratio": volume_ratio,
            "data_quality": "high",
            "catalyst_score": catalyst_score,
            "news_note": news_note,
            "risk_flags": [halal_reason],
            "ai_summary": halal_reason,
            "breakout_quality": "N/A",
            "execution_status": "AVOID",
            "owner_action": "تجنب السهم",
            "company": info["company"],
            "sector": info["sector"],
            "industry": info["industry"],
            "financials": financials,
        }

    live_block = build_live_price_block(symbol, prev, intraday)
    atr_overlay = get_atr_overlay(prev.get("price", 0), daily_bars)
    current_price = live_block["current_price_live"] if live_block["current_price_live"] > 0 else prev["price"]
    high = max(prev["high"], live_block["high_live"] if live_block["high_live"] > 0 else prev["high"])
    low = min(prev["low"], live_block["low_live"] if live_block["low_live"] > 0 else prev["low"])

    pullback_context = compute_pullback_context(current_price, high, low, intraday, trend_data["trend"])
    trade_type = "Pullback" if pullback_context.get("pullback_candidate") else "Breakout"
    historical_behavior = analyze_historical_behavior(daily_bars, trade_type)

    price_penalty, price_flag = dynamic_price_penalty(current_price, trade_type)
    volume_pace_ratio = compute_volume_pace_ratio(intraday, volume_ratio)
    effective_volume_ratio = get_effective_volume_ratio(volume_ratio, intraday)

    if trade_type == "Breakout":
        entry = high * 1.01
        stop = high * 0.95
        target1 = high * 1.07
        target2 = high * 1.10
    else:
        fib_50 = float(pullback_context.get("fib_50", 0) or 0)
        fib_62 = float(pullback_context.get("fib_62", 0) or 0)
        zone_high = float(pullback_context.get("pullback_zone_high", 0) or 0)
        entry = fib_50 if fib_50 > 0 else current_price
        if current_price > 0 and zone_high > 0 and current_price < zone_high:
            entry = max(current_price, fib_62 if fib_62 > 0 else current_price)
        stop = min(low * 0.985, fib_62 * 0.985) if fib_62 > 0 and low > 0 else low * 0.97
        target1 = max(high * 0.995, entry * 1.04)
        target2 = max(high * 1.02, entry * 1.08)

    risk_pct = ((entry - stop) / entry) * 100 if entry > 0 else 0

    quality = 50
    if trend_data["trend"] == "صاعد قوي":
        quality += 18
    elif trend_data["trend"] == "صاعد":
        quality += 10
    elif trend_data["trend"] == "متذبذب":
        quality -= 5
    else:
        quality -= 18

    if effective_volume_ratio >= 1.5:
        quality += 12
    elif effective_volume_ratio >= 1.2:
        quality += 8
    elif effective_volume_ratio >= 1.0:
        quality += 4
    else:
        quality -= 6

    if volume_pace_ratio >= 1.25:
        quality += 7
    elif volume_pace_ratio >= 1.0:
        quality += 3
    elif intraday.get("market_open"):
        quality -= 4

    if catalyst_score > 0:
        quality += catalyst_score

    if hist["ath_breakout_zone"]:
        quality -= 6
    elif hist["near_52w_high"]:
        quality -= 2

    breakout_quality = breakout_quality_label(
        trade_type,
        "صاعد" if trend_data["trend"] in ["صاعد", "صاعد قوي"] else trend_data["trend"],
        0.7,
        0.75,
        effective_volume_ratio,
    )
    if breakout_quality == "FAILED":
        quality -= 25
    elif breakout_quality == "WEAK":
        quality -= 8
    elif breakout_quality == "STRONG":
        quality += 6

    pullback_score = int(pullback_context.get("pullback_score", 0) or 0)
    if trade_type == "Pullback":
        if pullback_score >= 70:
            quality += 10
        elif pullback_score >= 58:
            quality += 5
        else:
            quality -= 4

    quality += price_penalty

    if risk_pct > 12:
        quality -= 18
    elif risk_pct > 8:
        quality -= 10
    elif risk_pct > 5:
        quality -= 4

    quality = max(1, min(99, int(round(quality))))
    rank_label = make_rank_label(quality)

    rr_1_preview = 0.0
    if entry > 0 and stop > 0 and target1 > 0 and entry > stop:
        rr_1_preview = (target1 - entry) / (entry - stop) if (entry - stop) > 0 else 0.0

    strong_ready = (
        quality >= 86
        and risk_pct <= 7.5
        and rr_1_preview >= 0.80
        and effective_volume_ratio >= 1.0
        and trend_data["trend"] in {"صاعد", "صاعد قوي"}
        and breakout_quality != "FAILED"
    )
    if trade_type == "Breakout":
        strong_ready = strong_ready and breakout_quality == "STRONG"
    elif trade_type == "Pullback":
        strong_ready = strong_ready and pullback_score >= 68

    cautious_ready = quality >= 66 and risk_pct <= 12

    decision = "مراقبة"
    if strong_ready:
        decision = "دخول قوي"
    elif cautious_ready:
        decision = "دخول بحذر"

    execution_status = compute_execution_status(
        trade_type, decision, trend_data["trend"], effective_volume_ratio, catalyst_score, breakout_quality
    )
    owner_action_text = owner_decision(decision, trend_data["trend"], breakout_quality, effective_volume_ratio, catalyst_score)
    valid_for = estimate_validity(trade_type, trend_data["trend"], effective_volume_ratio, catalyst_score)

    risk_flags = []
    if price_flag:
        risk_flags.append(price_flag)
    if hist["near_ath"]:
        risk_flags.append("قريب من القمة التاريخية")
    if hist["ath_breakout_zone"]:
        risk_flags.append("منطقة اختراق قمة تاريخية")
    if catalyst_score > 0:
        risk_flags.append("خبر إيجابي محفز")
    if info["sector"] == "":
        risk_flags.append("بيانات القطاع/الصناعة ناقصة")
    if financials["total_assets"] <= 0:
        risk_flags.append("إجمالي الأصول غير متوفر")
    if financials["shares"] <= 0:
        risk_flags.append("عدد الأسهم غير متوفر")
    if financials["approx_market_cap"] <= 0:
        risk_flags.append("القيمة السوقية التقريبية غير متوفرة")
    if intraday.get("market_open") and intraday.get("intraday_volume_ratio", 0) >= 1.5:
        risk_flags.append("سيولة لحظية قوية")
    if breakout_quality == "FAILED":
        risk_flags.append("سلوك اختراق فاشل")
    if trade_type == "Pullback" and not pullback_context.get("in_pullback_zone"):
        risk_flags.append("الارتداد خارج المنطقة المثالية")

    ai_summary_parts = [
        f"الاتجاه {trend_data['trend']}",
        f"السيولة {'مرتفعة' if effective_volume_ratio >= 1.2 else 'ضعيفة' if effective_volume_ratio < 0.9 else 'متوسطة'}",
    ]
    if intraday.get("market_open"):
        ai_summary_parts.append(f"افتتاح اليوم: {intraday.get('opening_drive', 'unknown')}")
        if intraday.get("above_vwap_proxy"):
            ai_summary_parts.append("فوق VWAP اللحظي")
        if intraday.get("intraday_volume_ratio", 0) >= 1.2:
            ai_summary_parts.append("السيولة اللحظية داعمة")
    if trade_type == "Pullback" and pullback_context.get("pullback_pattern_label"):
        ai_summary_parts.append(str(pullback_context.get("pullback_pattern_label")))
    if catalyst_score > 0:
        ai_summary_parts.append("يوجد محفز إيجابي")
    if hist["ath_breakout_zone"]:
        ai_summary_parts.append("في منطقة قمة تاريخية")
    if breakout_quality == "FAILED":
        ai_summary_parts.append("شمعة الاختراق فشلت")
    elif breakout_quality == "STRONG":
        ai_summary_parts.append("اختراق قوي")

    if info["sector"] == "" or financials["total_assets"] <= 0 or financials["shares"] <= 0:
        ai_summary_parts.append("جودة البيانات ضعيفة")
    elif financials["approx_market_cap"] <= 0:
        ai_summary_parts.append("جودة البيانات متوسطة")

    data_quality = "low" if (info["sector"] == "" or financials["total_assets"] <= 0 or financials["shares"] <= 0) else ("medium" if financials["approx_market_cap"] <= 0 else "high")

    levels = compute_breakout_levels(live_block["current_price_live"], high, low, intraday, trade_type, pullback_context)
    timing = compute_timing_layer(live_block["current_price_live"], intraday, effective_volume_ratio, levels, live_block.get("market_phase", "closed"))

    plan = {
        "symbol": symbol,
        "type": trade_type,
        "decision": decision,
        "entry": safe_round(entry),
        "stop_loss": safe_round(stop),
        "target_1": safe_round(target1),
        "target_2": safe_round(target2),
        "risk_pct": safe_round(risk_pct),
        "quality_score": quality,
        "rank_label": rank_label,
        "valid_for": valid_for,
        "trend": trend_data["trend"],
        "volume_ratio": safe_round(volume_ratio),
        "volume_pace_ratio": safe_round(volume_pace_ratio),
        "effective_volume_ratio": safe_round(effective_volume_ratio),
        "data_quality": data_quality,
        "catalyst_score": catalyst_score,
        "news_note": news_note,
        "news_title": news_bundle.get("news_title", ""),
        "news_badge": news_bundle.get("news_badge", ""),
        "news_category": news_bundle.get("news_category", "neutral"),
        "news_sentiment": news_bundle.get("news_sentiment", "neutral"),
        "news_freshness_label": news_bundle.get("news_freshness_label", ""),
        "news_published_utc": news_bundle.get("news_published_utc", ""),
        "news_sessions_since": news_bundle.get("news_sessions_since", 999),
        "risk_flags": risk_flags,
        "ai_summary": " - ".join(ai_summary_parts),
        "breakout_quality": breakout_quality,
        "execution_status": execution_status,
        "owner_action": owner_action_text,
        "intraday": intraday,
        **pullback_context,
        **levels,
        **timing,
        **live_block,
        **atr_overlay,
        **historical_behavior,
        "company": info["company"],
        "sector": info["sector"],
        "industry": info["industry"],
        "financials": financials,
    }
    plan = enrich_strategy_profile(plan)
    return plan


def scan_all():
    symbols = get_active_universe(150)
    rows = []

    def process_symbol(s):
        p = trade_plan_pro(s)
        if not p or p.get("type") == "Excluded":
            return None

        p = apply_late_move_filter(p)
        p = assign_execution_mode(p)
        p = normalize_execution_labels(p)
        p = enrich_signal_stage(p)
        p = finalize_display_contract(p)

        if not p.get("price_reliable_for_execution", True) and p.get("market_phase") in {"open", "pre_market", "after_hours"}:
            p["decision"] = "مراقبة"
            p["execution_mode"] = "مراقبة 👀"
            p["execution_note"] = "السعر اللحظي غير موثوق - لا تعتمد عليه للتنفيذ"
            p["owner_action"] = "👀 راقب فقط حتى تتوفر بيانات سعر لحظية موثوقة"
            p.setdefault("risk_flags", []).append("السعر اللحظي غير موثوق")
            p.setdefault("ai_summary", "")
            if p["ai_summary"]:
                p["ai_summary"] += " - "
            p["ai_summary"] += "السعر اللحظي غير موثوق"

        p = enrich_display_meta(p)
        # تثبيت دقة "دخول قوي" بدون قتل الإشارات الجيدة قبل التأكيد:
        # نهبط فقط إذا كانت الجاهزية ضعيفة فعلاً أو إذا كانت مطاردة سعرية/خطة غير مكتملة بوضوح.
        try:
            if str(p.get("decision", "") or "") == "دخول قوي":
                readiness_score = float(p.get("execution_readiness_score", 0) or 0)
                readiness_label = str(p.get("execution_readiness_label", "") or "")
                if readiness_score < 62 or readiness_label in {"مطاردة سعرية", "ارتداد قيد التكوين"}:
                    p["decision"] = "دخول بحذر"
            if str(p.get("decision", "") or "") == "دخول بحذر":
                readiness_score = float(p.get("execution_readiness_score", 0) or 0)
                if readiness_score < 35:
                    p["decision"] = "مراقبة"
        except:
            pass
        upsert_performance_signal(p)
        return p

    max_workers = min(12, max(4, len(symbols)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_symbol, s) for s in symbols]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    rows.append(result)
            except:
                continue

    rows.sort(key=lambda x: (decision_priority(x.get("decision", "")), x.get("quality_score", 0)), reverse=True)
    return rows



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

    strong = [x for x in results if x.get("decision") == "دخول قوي"]
    cautious = [x for x in results if x.get("decision") == "دخول بحذر"]
    watch = [x for x in results if x.get("decision") == "مراقبة"]

    return {
        "market_phase": get_market_phase(),
        "market_phase_label": market_phase_label(get_market_phase()),
        "updated_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "universe_count": 150,
        "count": len(results),
        "strong_entries_count": len(strong),
        "cautious_entries_count": len(cautious),
        "watchlist_count": len(watch),
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
    symbol = str(symbol).upper().strip()
    overview = None
    trade_plan = None
    overview_error = None
    trade_error = None

    try:
        prev = get_prev(symbol)
        if not prev:
            overview = {"symbol": symbol, "available": False, "reason": "No daily data"}
        else:
            info = get_info(symbol)
            financials = get_financials(symbol)
            hist = get_history_levels(symbol)
            trend_data = get_trend(symbol)
            intraday = get_intraday_snapshot(symbol)
            volume_ratio = get_volume_ratio(symbol, intraday)
            news_note, catalyst_score = get_news(symbol, info["company"])
            halal_ok, halal_reason = is_halal(info["sector"], info["industry"], financials["total_assets"], financials["cash"], financials["total_debt"])
            live_block = build_live_price_block(symbol, prev, intraday)
            overview = {
                "symbol": symbol,
                "available": True,
                "company": info["company"],
                "sector": info["sector"],
                "industry": info["industry"],
                "price": prev["price"],
                "open": prev["open"],
                "high": prev["high"],
                "low": prev["low"],
                "volume": prev["volume"],
                "trend": trend_data["trend"],
                "volume_ratio": safe_round(volume_ratio),
                "news_note": news_note,
                "catalyst_score": catalyst_score,
                "near_ath": hist["near_ath"],
                "ath_breakout_zone": hist["ath_breakout_zone"],
                "intraday": intraday,
                "halal": halal_ok,
                "halal_reason": halal_reason,
                **live_block,
            }
    except Exception as e:
        overview_error = str(e)
        overview = {"symbol": symbol, "available": False}

    try:
        trade_plan = trade_plan_pro(symbol)
        if trade_plan:
            trade_plan = apply_late_move_filter(trade_plan)
            trade_plan = assign_execution_mode(trade_plan)
            trade_plan = normalize_execution_labels(trade_plan)
            trade_plan = enrich_signal_stage(trade_plan)
            trade_plan = finalize_display_contract(trade_plan)
            if not trade_plan.get("price_reliable_for_execution", True) and trade_plan.get("market_phase") in {"open", "pre_market", "after_hours"}:
                trade_plan["decision"] = "مراقبة"
                trade_plan["execution_mode"] = "مراقبة 👀"
                trade_plan["execution_note"] = "السعر اللحظي غير موثوق - لا تعتمد عليه للتنفيذ"
                trade_plan["owner_action"] = "👀 راقب فقط حتى تتوفر بيانات سعر لحظية موثوقة"
            if not trade_plan.get("price_reliable_for_execution", True) and trade_plan.get("market_phase") in {"open", "pre_market", "after_hours"}:
                trade_plan.setdefault("risk_flags", []).append("السعر اللحظي غير موثوق")
            trade_plan = enrich_display_meta(trade_plan)
    except Exception as e:
        trade_error = str(e)

    return {
        "symbol": symbol,
        "overview": overview,
        "trade_plan": trade_plan,
        "overview_error": overview_error,
        "trade_error": trade_error,
    }




def get_price_freshness_meta(stock: dict) -> dict:
    try:
        phase = str(stock.get("market_phase", "") or "")
        reliable = bool(stock.get("price_reliable_for_execution", False))
        source = str(stock.get("price_source", "") or "")
        last_ms = int(float(stock.get("last_price_update_ms", 0) or 0))
        age_seconds = 0
        if last_ms > 0:
            age_seconds = max(0, int((time.time() * 1000 - last_ms) / 1000))

        if source == "previous_close" or phase == "closed":
            return {
                "price_freshness_label": "آخر إغلاق",
                "price_freshness_icon": "🌙",
                "price_freshness_score": 55,
                "price_freshness_detail": "السعر الحالي يمثل آخر إغلاق، وليس سعرًا مباشرًا أثناء التداول.",
            }
        if source == "pre_market":
            return {
                "price_freshness_label": "قبل الافتتاح",
                "price_freshness_icon": "🌅",
                "price_freshness_score": 78 if age_seconds <= 600 else 58,
                "price_freshness_detail": f"السعر من تداولات ما قبل الافتتاح. آخر تحديث تقريبي منذ {age_seconds} ثانية." if age_seconds > 0 else "السعر من تداولات ما قبل الافتتاح.",
            }
        if source == "after_hours":
            return {
                "price_freshness_label": "بعد الإغلاق",
                "price_freshness_icon": "🌙",
                "price_freshness_score": 78 if age_seconds <= 600 else 58,
                "price_freshness_detail": f"السعر من تداولات ما بعد الإغلاق. آخر تحديث تقريبي منذ {age_seconds} ثانية." if age_seconds > 0 else "السعر من تداولات ما بعد الإغلاق.",
            }
        if reliable:
            return {
                "price_freshness_label": "مباشر / حديث" if age_seconds <= 300 else "مباشر / متأخر قليلًا",
                "price_freshness_icon": "🟢" if age_seconds <= 300 else "🟡",
                "price_freshness_score": 96 if age_seconds <= 90 else 88 if age_seconds <= 300 else 70,
                "price_freshness_detail": f"السعر مباشر أثناء التداول. آخر تحديث تقريبي منذ {age_seconds} ثانية." if age_seconds > 0 else "السعر مباشر أثناء التداول.",
            }
        return {
            "price_freshness_label": "لا توجد بيانات كافية",
            "price_freshness_icon": "❓",
            "price_freshness_score": 0,
            "price_freshness_detail": "لا توجد بيانات كافية لتحديد حداثة السعر بثقة.",
        }
    except:
        return {
            "price_freshness_label": "لا توجد بيانات كافية",
            "price_freshness_icon": "❓",
            "price_freshness_score": 0,
            "price_freshness_detail": "لا توجد بيانات كافية لتحديد حداثة السعر.",
        }


def get_execution_readiness_meta(stock: dict) -> dict:
    try:
        decision = str(stock.get("decision", "") or "")
        trade_type = str(stock.get("type", "") or "")
        mode = str(stock.get("execution_mode", "") or "")
        reliable = bool(stock.get("price_reliable_for_execution", False))
        market_phase = str(stock.get("market_phase", "") or "")
        current_price = float(stock.get("display_price", stock.get("current_price_live", 0)) or 0)
        breakout_price = float(stock.get("breakout_price", 0) or 0)
        confirmation_price = float(stock.get("confirmation_price", 0) or 0)
        entry_price = float(stock.get("display_entry_price", stock.get("entry_price_real", stock.get("entry", 0))) or 0)
        stop_price = float(stock.get("display_stop_price", stock.get("stop_loss", 0)) or 0)
        zone_low = float(stock.get("pullback_zone_low", 0) or 0)
        zone_high = float(stock.get("pullback_zone_high", 0) or 0)
        quality = float(stock.get("quality_score", 0) or 0)
        rr = float(stock.get("rr_1", 0) or 0)
        volume_ratio = float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)) or 0)
        risk_pct = float(stock.get("display_risk_pct", stock.get("risk_pct", 0)) or 0)

        label = "مراقبة"
        icon = "👀"
        score = 28
        detail = "راقب السهم حتى تتضح إشارة التنفيذ."

        if not reliable and market_phase in {"open", "pre_market", "after_hours"}:
            label = "لا توجد بيانات كافية"
            icon = "❓"
            score = 12
            detail = "السعر اللحظي غير موثوق الآن، لذلك لا يُفضَّل اتخاذ قرار تنفيذ مباشر."
        elif trade_type == "Breakout":
            if current_price > 0 and stop_price > 0 and current_price <= stop_price:
                label = "خطة مكسورة"
                icon = "❌"
                score = 8
                detail = f"السعر كسر وقف الخطة السابق عند {safe_round(stop_price)}. انتظر إعادة تكوين فرصة جديدة."
            elif breakout_price > 0 and current_price < breakout_price:
                label = "انتظار اختراق"
                icon = "⏳"
                score = 55
                detail = f"انتظر اختراق {safe_round(breakout_price)} ثم تأكيد فوق {safe_round(confirmation_price or breakout_price)}."
            elif confirmation_price > 0 and current_price < confirmation_price:
                label = "اختراق أولي"
                icon = "📊"
                score = 62
                detail = f"تم الاختراق مبدئيًا، لكن الأفضل انتظار الثبات فوق {safe_round(confirmation_price)}."
            elif entry_price > 0 and current_price <= entry_price * 1.01:
                label = "دخول فوري"
                icon = "🔥"
                score = 90
                detail = f"السعر قريب من منطقة الدخول الحالية حول {safe_round(entry_price)} مع وقف عند {safe_round(stop_price)}."
            else:
                label = "مطاردة سعرية"
                icon = "⚠️"
                score = 40
                detail = f"السعر ابتعد عن منطقة الدخول ({safe_round(entry_price)}). الأفضل انتظار إعادة تمركز أو إعادة دخول."
        elif trade_type == "Pullback":
            if current_price > 0 and stop_price > 0 and current_price <= stop_price:
                label = "خطة ارتداد مكسورة"
                icon = "❌"
                score = 10
                detail = f"السعر كسر وقف الارتداد عند {safe_round(stop_price)}. انتظر ارتدادًا جديدًا من دعم أحدث."
            elif zone_low > 0 and zone_high > 0:
                if decision == "دخول قوي":
                    label = "دخول قوي"
                    icon = "🔥"
                    score = 82
                    detail = f"الخطة قوية كارتداد من الدعم. راقب الارتداد قرب منطقة {safe_round(zone_low)} - {safe_round(zone_high)} ثم تأكيد فوق {safe_round(entry_price or zone_high)}."
                elif decision == "دخول بحذر":
                    label = "دخول بحذر"
                    icon = "🟠"
                    score = 70
                    detail = f"الخطة بحذر كارتداد من الدعم. راقب الارتداد قرب منطقة {safe_round(zone_low)} - {safe_round(zone_high)} ثم تأكيد فوق {safe_round(entry_price or zone_high)}."
                else:
                    label = "ارتداد من الدعم"
                    icon = "↩️"
                    score = 72
                    detail = f"راقب الارتداد قرب منطقة {safe_round(zone_low)} - {safe_round(zone_high)} ثم تأكيد فوق {safe_round(entry_price or zone_high)}."
            elif entry_price > 0:
                label = "ارتداد قيد التكوين"
                icon = "🟠"
                score = 58
                detail = f"الخطة تميل لارتداد من الدعم. راقب التأكيد فوق {safe_round(entry_price)}."
        elif "إعادة دخول" in mode:
            label = "إعادة دخول"
            icon = "🔁"
            score = 48
            detail = "فات الدخول الأول. راقب إعادة دخول أفضل بدل مطاردة الحركة."
        elif decision in {"دخول قوي", "دخول بحذر"}:
            label = "دخول فوري"
            icon = "🔥" if decision == "دخول قوي" else "🟠"
            score = 82 if decision == "دخول قوي" else 70
            detail = f"الخطة صالحة حاليًا للدخول مع إدارة واضحة للمخاطر. نقطة الدخول الحالية قرب {safe_round(entry_price)}."

        if quality >= 85:
            score += 3
        elif quality < 60:
            score -= 8
        if rr >= 1.8:
            score += 4
        elif rr < 1.2:
            score -= 5
        if volume_ratio >= 1.2:
            score += 3
        elif volume_ratio < 0.9:
            score -= 4
        if risk_pct > 8:
            score -= 5
        score = int(max(0, min(score, 99)))
        return {
            "execution_readiness_score": score,
            "execution_readiness_label": label,
            "execution_readiness_icon": icon,
            "execution_readiness_detail": detail,
        }
    except:
        return {
            "execution_readiness_score": 0,
            "execution_readiness_label": "لا توجد بيانات كافية",
            "execution_readiness_icon": "❓",
            "execution_readiness_detail": "لا توجد بيانات كافية لتحديد جاهزية التنفيذ.",
        }


def explain_metric_ar(name: str, value, stock: dict) -> dict:
    try:
        if name == "quality":
            v = float(value or 0)
            if v >= 85:
                return {"icon": "🏅", "label": "ممتازة", "detail": "الجودة مرتفعة جدًا وتدعم القرار."}
            if v >= 65:
                return {"icon": "✅", "label": "جيدة", "detail": "الجودة جيدة لكن ليست مثالية."}
            if v >= 50:
                return {"icon": "🟰", "label": "متوسطة", "detail": "الجودة متوسطة وتحتاج انتقاء أفضل."}
            return {"icon": "❌", "label": "ضعيفة", "detail": "الجودة منخفضة ولا تعطي ثقة كافية."}
        if name == "risk_pct":
            v = float(value or 0)
            if v <= 4:
                return {"icon": "🟢", "label": "منخفضة", "detail": "المخاطرة منخفضة نسبيًا."}
            if v <= 8:
                return {"icon": "🟡", "label": "متوسطة", "detail": "المخاطرة مقبولة مع إدارة جيدة."}
            if v <= 12:
                return {"icon": "🟠", "label": "مرتفعة نسبيًا", "detail": "المخاطرة أعلى من المثالي وتحتاج حذرًا."}
            return {"icon": "🔴", "label": "مرتفعة", "detail": "المخاطرة مرتفعة ولا تناسب معظم التداولات."}
        if name in {"volume", "volume_daily", "volume_pace"}:
            v = float(value or 0)
            if v >= 1.5:
                return {"icon": "🔥", "label": "قوية جدًا", "detail": "السيولة أعلى بكثير من المعتاد."}
            if v >= 1.2:
                return {"icon": "✅", "label": "إيجابية", "detail": "السيولة داعمة للحركة."}
            if v >= 0.95:
                return {"icon": "🟰", "label": "مقبولة", "detail": "السيولة مقبولة لكنها ليست انفجارية."}
            return {"icon": "❌", "label": "ضعيفة", "detail": "السيولة أقل من المطلوب غالبًا."}
        if name == "rr":
            v = float(value or 0)
            if v >= 2.0:
                return {"icon": "🏅", "label": "ممتاز", "detail": "العائد المتوقع جيد جدًا مقارنة بالخطر."}
            if v >= 1.5:
                return {"icon": "✅", "label": "جيد", "detail": "العائد إلى المخاطرة مناسب."}
            if v >= 1.2:
                return {"icon": "🟡", "label": "متوسط", "detail": "العائد مقبول لكنه ليس مريحًا جدًا."}
            return {"icon": "❌", "label": "ضعيف", "detail": "العائد لا يبرر الخطر غالبًا."}
        if name == "trend":
            t = str(value or "")
            mapping = {
                "صاعد قوي": {"icon": "⬆️", "label": "إيجابي جدًا", "detail": "السهم أعلى من المتوسطات الرئيسية."},
                "صاعد": {"icon": "🟢", "label": "إيجابي", "detail": "الاتجاه العام جيد."},
                "متذبذب": {"icon": "🟰", "label": "محايد", "detail": "السهم غير واضح الاتجاه."},
                "هابط": {"icon": "⬇️", "label": "سلبي", "detail": "الاتجاه الهابط يضعف الثقة."},
            }
            return mapping.get(t, {"icon": "❓", "label": "غير واضح", "detail": "لا توجد بيانات كافية لاتجاه واضح."})
        if name == "continuation":
            label = str(value or "")
            if "بقوة" in label or "مرجح" in label or "مرشح استمرار اليوم" in label:
                return {"icon": "🔥", "label": "إيجابي", "detail": "احتمال استمرار الحركة جيد."}
            if "محتمل" in label:
                return {"icon": "🟠", "label": "بحذر", "detail": "الاستمرار ممكن لكن ليس مضمونًا."}
            if label:
                return {"icon": "⚠️", "label": "غير مؤكد", "detail": "الاستمرار غير واضح حتى الآن."}
            return {"icon": "❓", "label": "لا توجد بيانات كافية", "detail": "لا توجد بيانات كافية لتقييم الاستمرار."}
        if name in {"runner_score", "continuation_score"}:
            v = float(value or 0)
            if v >= 80:
                return {"icon": "🔥", "label": "قوي جدًا", "detail": "الدرجة مرتفعة جدًا وتدعم الاستمرار."}
            if v >= 66:
                return {"icon": "✅", "label": "إيجابي", "detail": "الدرجة داعمة للاستمرار."}
            if v >= 50:
                return {"icon": "🟡", "label": "متوسطة", "detail": "الدرجة متوسطة وليست حاسمة."}
            return {"icon": "❌", "label": "ضعيف", "detail": "الدرجة ضعيفة ولا تعطي أفضلية كافية."}
        if name == "news":
            category = str(stock.get("news_category", "neutral") or "neutral")
            freshness = str(stock.get("news_freshness_label", "") or "")
            if category == "positive":
                return {"icon": "🟢", "label": f"إيجابي {freshness}".strip(), "detail": "خبر داعم ومصنف كمحفز فعلي."}
            if category in {"negative", "legal"}:
                return {"icon": "🔴", "label": f"سلبي {freshness}".strip(), "detail": "الخبر سلبي ولا يجب اعتباره محفزًا إيجابيًا."}
            if category == "opinion":
                return {"icon": "🚫", "label": "مقال رأي", "detail": "هذا رأي أو مقال عام وليس محفز تداول معتمد."}
            return {"icon": "⚪", "label": freshness or "محايد", "detail": "لا يوجد خبر محفز حديث."}
    except:
        pass
    return {"icon": "❓", "label": "لا توجد بيانات كافية", "detail": "لا توجد بيانات كافية."}


def enrich_display_meta(stock: dict) -> dict:
    try:
        stock.update(get_price_freshness_meta(stock))
        stock.update(get_execution_readiness_meta(stock))
        stock.update(get_alignment_meta(stock))
        stock.update(get_risk_profile_meta(stock))
        stock["metric_quality"] = explain_metric_ar("quality", stock.get("quality_score"), stock)
        stock["metric_risk_pct"] = explain_metric_ar("risk_pct", stock.get("display_risk_pct", stock.get("risk_pct", 0)), stock)
        stock["metric_volume_daily"] = explain_metric_ar("volume_daily", stock.get("volume_ratio", 0), stock)
        stock["metric_volume_pace"] = explain_metric_ar("volume_pace", stock.get("volume_pace_ratio", 0), stock)
        stock["metric_volume"] = explain_metric_ar("volume", stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)), stock)
        stock["metric_rr"] = explain_metric_ar("rr", stock.get("rr_1"), stock)
        stock["metric_trend"] = explain_metric_ar("trend", stock.get("trend"), stock)
        stock["metric_continuation"] = explain_metric_ar("continuation", stock.get("continuation_label", stock.get("runner_label", "")), stock)
        stock["metric_runner_score"] = explain_metric_ar("runner_score", stock.get("runner_score", 0), stock)
        stock["metric_continuation_score"] = explain_metric_ar("continuation_score", stock.get("continuation_score", 0), stock)
        stock["metric_news"] = explain_metric_ar("news", stock.get("news_badge"), stock)
        stock["trade_type_label_ar"] = (
            "اختراق مقاومة" if str(stock.get("type", "")) == "Breakout"
            else "ارتداد من دعم" if str(stock.get("type", "")) == "Pullback"
            else "خطة متابعة"
        )
        summary_bits = [
            f"{stock['metric_quality'].get('icon')} الجودة: {stock['metric_quality'].get('label')}",
            f"{stock['metric_volume'].get('icon')} السيولة: {stock['metric_volume'].get('label')}",
            f"{stock['metric_trend'].get('icon')} الاتجاه: {stock['metric_trend'].get('label')}",
            f"{stock['metric_rr'].get('icon')} العائد/المخاطرة: {stock['metric_rr'].get('label')}",
            f"{stock.get('execution_readiness_icon', '👀')} الجاهزية: {stock.get('execution_readiness_label', '')}",
        ]
        if stock.get("historical_behavior_label"):
            summary_bits.append(f"📚 السلوك التاريخي: {stock.get('historical_behavior_label')}")
        if stock.get("alignment_label"):
            summary_bits.append(f"🧭 التوافق الزمني: {stock.get('alignment_label')}")
        stock["quick_explainer"] = " | ".join([x for x in summary_bits if x])
        return stock
    except:
        return stock


def is_opening_window() -> bool:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return False
        minutes = now_ny.hour * 60 + now_ny.minute
        return (4 * 60) <= minutes <= (10 * 60 + 15)
    except:
        return False


def build_opening_focus(results: list[dict]) -> list[dict]:
    portfolio_symbols = {str(x.get("symbol", "")).upper().strip() for x in load_portfolio_items()}
    watch_symbols = {str(x.get("symbol", "")).upper().strip() for x in load_manual_watchlist()}
    def opening_rank(item: dict):
        symbol = str(item.get("symbol", "")).upper().strip()
        priority = 0
        if symbol in portfolio_symbols:
            priority += 40
        if symbol in watch_symbols:
            priority += 20
        priority += int(float(item.get("execution_readiness_score", 0) or 0))
        priority += int(float(item.get("quality_score", 0) or 0) * 0.25)
        if item.get("decision") == "دخول قوي":
            priority += 20
        elif item.get("decision") == "دخول بحذر":
            priority += 10
        return priority
    return sorted(results, key=opening_rank, reverse=True)[:8]

def evaluate_portfolio_action(holding: dict, plan: dict) -> dict:
    current_price = float(plan.get("display_price", plan.get("current_price_live", 0)) or 0)
    buy_price = float(holding.get("buy_price", 0) or 0)
    target_1 = float(plan.get("display_target_price", plan.get("target_1", 0)) or 0)
    stop_loss = float(plan.get("display_stop_price", plan.get("stop_loss", 0)) or 0)
    decision = str(plan.get("decision", "") or "")
    trend = str(plan.get("trend", "") or "")
    readiness_score = int(float(plan.get("execution_readiness_score", 0) or 0))
    pnl_pct = ((current_price - buy_price) / buy_price) * 100 if current_price > 0 and buy_price > 0 else 0.0

    recommendation = "احتفاظ"
    note = "احتفظ بالسهم مع متابعة التحديثات."
    if stop_loss > 0 and current_price > 0 and current_price <= stop_loss:
        recommendation = "بيع"
        note = "السعر عند الوقف أو تحته، والأفضل الخروج والانضباط."
    elif target_1 > 0 and current_price >= target_1:
        recommendation = "تقليل"
        note = "السهم وصل للهدف الأول، وجني جزء من الربح منطقي."
    elif decision == "دخول قوي" and readiness_score >= 80 and trend in {"صاعد", "صاعد قوي"} and pnl_pct > -6:
        recommendation = "زيادة الكمية"
        note = "الإشارة الحالية تدعم زيادة جزئية مدروسة إذا كانت إدارة المخاطر مناسبة."
    elif decision == "دخول بحذر" and trend in {"صاعد", "صاعد قوي"}:
        recommendation = "احتفاظ"
        note = "الوضع جيد لكن الأفضل عدم التوسع بقوة."
    elif decision == "مراقبة" and trend == "هابط":
        recommendation = "تقليل"
        note = "القوة الحالية لا تدعم الاحتفاظ الكامل بنفس الثقة."
    elif pnl_pct <= -8:
        recommendation = "تقليل"
        note = "الخسارة اتسعت نسبيًا، ويستحق المركز تخفيفًا أو مراجعة وقفك."

    return {
        "recommendation": recommendation,
        "recommendation_note": note,
        "holding_change_pct": safe_round(pnl_pct),
        "target_price": safe_round(target_1),
        "stop_loss": safe_round(stop_loss),
    }


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



def summarize_outcomes(records: list[dict]) -> dict:
    rows = list(records or [])
    total = len(rows)
    out = {
        "count": total,
        "target_hit": 0,
        "above_target": 0,
        "partial_gain": 0,
        "ongoing": 0,
        "loss": 0,
        "expired": 0,
    }
    for row in rows:
        key = str(row.get("outcome", "ongoing") or "ongoing")
        if key in out:
            out[key] += 1
    if total > 0:
        out.update({k + "_pct": safe_round((v / total) * 100, 2) for k, v in out.items() if k != "count"})
    else:
        out.update({k + "_pct": 0.0 for k in ["target_hit", "above_target", "partial_gain", "ongoing", "loss", "expired"]})
    return out


def simulate_equal_weight(records: list[dict], per_trade: float = 1000.0) -> dict:
    rows = list(records or [])
    starting_capital = per_trade * len(rows)
    pnl = 0.0
    for row in rows:
        entry = float(row.get("entry_price", 0) or 0)
        target = float(row.get("target_price", 0) or 0)
        target2 = float(row.get("target_2_price", 0) or 0)
        stop = float(row.get("stop_loss", 0) or 0)
        current = float(row.get("current_price", 0) or 0)
        max_seen = float(row.get("max_price_seen", current) or current)
        if entry <= 0:
            continue
        outcome = str(row.get("outcome", "ongoing") or "ongoing")
        if outcome == "above_target" and target2 > 0:
            ret = (target2 - entry) / entry
        elif outcome == "target_hit" and target > 0:
            ret = (target - entry) / entry
        elif outcome == "loss" and stop > 0:
            ret = (stop - entry) / entry
        elif outcome == "partial_gain":
            ref = max(current, max_seen)
            ret = (ref - entry) / entry
        elif outcome == "ongoing":
            ref = current if current > 0 else max_seen
            ret = (ref - entry) / entry if ref > 0 else 0.0
        else:
            ret = 0.0
        pnl += per_trade * ret
    final_capital = starting_capital + pnl
    roi_pct = safe_round((pnl / starting_capital) * 100, 2) if starting_capital > 0 else 0.0
    return {
        "per_trade": per_trade,
        "starting_capital": safe_round(starting_capital),
        "pnl": safe_round(pnl),
        "final_capital": safe_round(final_capital),
        "roi_pct": roi_pct,
    }


def build_performance_dashboard(records: list[dict]) -> dict:
    rows = sorted(list(records or []), key=lambda r: (outcome_sort_rank(r.get("outcome")), r.get("first_seen_at", "")))
    strong = [x for x in rows if str(x.get("signal_type", "") or "") == "دخول قوي"]
    cautious = [x for x in rows if str(x.get("signal_type", "") or "") == "دخول بحذر"]
    return {
        "rows": rows,
        "summary": summarize_outcomes(rows),
        "groups": {
            "strong": {"items": strong, "summary": summarize_outcomes(strong), "simulation": simulate_equal_weight(strong)},
            "cautious": {"items": cautious, "summary": summarize_outcomes(cautious), "simulation": simulate_equal_weight(cautious)},
        },
        "simulation": simulate_equal_weight(rows),
    }



@app.get("/performance")
def performance_get():
    store = rollover_performance_store_if_needed(load_performance_store())
    records = list(store.get("active_records", []))
    updated = []

    for item in records[:500]:
        symbol = str(item.get("symbol", "") or "").upper().strip()
        current_price = float(item.get("current_price", 0) or 0)
        price_source_label = str(item.get("price_source_label", "") or "")
        if symbol:
            prev = get_prev(symbol)
            intraday = get_intraday_snapshot(symbol)
            live_block = build_live_price_block(symbol, prev or {}, intraday)
            live_price = float(live_block.get("display_price", 0) or 0)
            if live_price > 0:
                current_price = live_price
                price_source_label = live_block.get("price_source_label", price_source_label)
        item["last_seen_at"] = ny_now().strftime("%Y-%m-%d %H:%M:%S")
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


