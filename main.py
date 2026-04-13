from fastapi import FastAPI, Body
from fastapi.responses import FileResponse
import requests
import os
import csv
import re
from datetime import datetime, timedelta
import time
import json
from zoneinfo import ZoneInfo
try:
    from scanner import get_scan_universe, apply_late_move_filter, assign_execution_mode, normalize_execution_labels, recalc_reentry_plan, enrich_signal_stage, enrich_strategy_profile, finalize_display_contract
except Exception:
    try:
        from scanner_resend_momentum_suite import get_scan_universe, apply_late_move_filter, assign_execution_mode, normalize_execution_labels, recalc_reentry_plan, enrich_signal_stage, enrich_strategy_profile, finalize_display_contract
    except Exception:
        from scanner_resend_momentum_suite_no_conflict import get_scan_universe, apply_late_move_filter, assign_execution_mode, normalize_execution_labels, recalc_reentry_plan, enrich_signal_stage, enrich_strategy_profile, finalize_display_contract

app = FastAPI()

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
PERFORMANCE_FILE = "signal_performance.json"

MANUAL_WATCHLIST_FILE = "manual_watchlist.json"

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
    wins = sum(1 for r in records if r.get("outcome") == "win")
    losses = sum(1 for r in records if r.get("outcome") == "loss")
    pending = sum(1 for r in records if r.get("outcome") not in {"win", "loss"})
    total = len(records)
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


def evaluate_performance_record(record, current_price):
    entry_price = float(record.get("entry_price", 0) or 0)
    target_price = float(record.get("target_price", 0) or 0)
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

    if record.get("outcome") in {"win", "loss"}:
        return record

    max_seen = float(record.get("max_price_seen", 0) or 0)
    min_seen = float(record.get("min_price_seen", 0) or 0)
    now_str = ny_now().strftime("%Y-%m-%d %H:%M:%S")

    if target_price > 0 and max_seen >= target_price:
        record["outcome"] = "win"
        record["status_mark"] = "✅"
        record["status_label"] = "ناجحة"
        record["closed_at"] = now_str
    elif stop_loss > 0 and min_seen > 0 and min_seen <= stop_loss:
        record["outcome"] = "loss"
        record["status_mark"] = "❌"
        record["status_label"] = "خاسرة"
        record["closed_at"] = now_str
    else:
        record["outcome"] = "pending"
        record["status_mark"] = "⏳"
        record["status_label"] = "قيد المتابعة"

    return record


def upsert_performance_signal(stock: dict):
    try:
        signal_type = str(stock.get("decision", "") or "")
        if signal_type not in {"دخول قوي", "دخول بحذر"}:
            return

        symbol = str(stock.get("symbol", "") or "").upper().strip()
        if not symbol:
            return

        entry_price = float(stock.get("display_entry_price", 0) or 0)
        target_price = float(stock.get("display_target_price", 0) or 0)
        stop_loss = float(stock.get("display_stop_price", 0) or 0)
        current_price = float(stock.get("display_price", stock.get("current_price_live", 0)) or 0)
        if entry_price <= 0:
            return

        store = rollover_performance_store_if_needed(load_performance_store())
        records = store.get("active_records", [])
        record_id = f"{store['active_week_key']}::{symbol}"
        now_str = ny_now().strftime("%Y-%m-%d %H:%M:%S")

        existing = None
        for item in records:
            if item.get("id") == record_id:
                existing = item
                break

        if existing is None:
            existing = {
                "id": record_id,
                "symbol": symbol,
                "signal_type": signal_type,
                "entry_price": safe_round(entry_price),
                "target_price": safe_round(target_price),
                "stop_loss": safe_round(stop_loss),
                "first_seen_at": now_str,
                "last_seen_at": now_str,
                "current_price": safe_round(current_price),
                "max_price_seen": safe_round(current_price if current_price > 0 else entry_price),
                "min_price_seen": safe_round(current_price if current_price > 0 else entry_price),
                "price_source_label": str(stock.get("price_source_label", stock.get("price_source", "")) or ""),
                "strategy_label": str(stock.get("strategy_label", "") or ""),
                "status_mark": "⏳",
                "status_label": "قيد المتابعة",
                "outcome": "pending",
                "closed_at": "",
                "market_phase": str(stock.get("market_phase_label", stock.get("market_phase", "")) or ""),
                "last_change_pct": 0.0,
            }
            records.insert(0, existing)
        else:
            existing["last_seen_at"] = now_str
            existing["price_source_label"] = str(stock.get("price_source_label", stock.get("price_source", "")) or existing.get("price_source_label", ""))
            existing["strategy_label"] = str(stock.get("strategy_label", "") or existing.get("strategy_label", ""))
            existing["market_phase"] = str(stock.get("market_phase_label", stock.get("market_phase", "")) or existing.get("market_phase", ""))
            if not existing.get("signal_type"):
                existing["signal_type"] = signal_type
            if float(existing.get("target_price", 0) or 0) <= 0 and target_price > 0:
                existing["target_price"] = safe_round(target_price)
            if float(existing.get("stop_loss", 0) or 0) <= 0 and stop_loss > 0:
                existing["stop_loss"] = safe_round(stop_loss)

        evaluate_performance_record(existing, current_price)
        store["active_records"] = records[:300]
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
        r = requests.get(url, timeout=12).json()
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


def get_prev(symbol):
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=12
        ).json()
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
        r = requests.get(url, timeout=12).json()
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
        r = requests.get(url, timeout=12).json()
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
        r = requests.get(url, timeout=12).json()
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


def get_history_levels(symbol):
    if symbol in HISTORY_CACHE:
        return HISTORY_CACHE[symbol]

    today = datetime.utcnow().date()
    from_52w = (today - timedelta(days=365)).isoformat()
    from_5y = (today - timedelta(days=365 * 5)).isoformat()
    to_date = today.isoformat()

    out = {
        "year_high": 0.0,
        "ath_high": 0.0,
        "near_52w_high": False,
        "near_ath": False,
        "ath_breakout_zone": False,
    }

    try:
        url_52w = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{from_52w}/{to_date}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r52 = requests.get(url_52w, timeout=18).json()
        highs_52 = [to_float(x.get("h")) for x in r52.get("results", []) if to_float(x.get("h")) > 0]
        if highs_52:
            out["year_high"] = max(highs_52)
    except:
        pass

    try:
        url_5y = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{from_5y}/{to_date}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r5 = requests.get(url_5y, timeout=22).json()
        highs_5 = [to_float(x.get("h")) for x in r5.get("results", []) if to_float(x.get("h")) > 0]
        if highs_5:
            out["ath_high"] = max(highs_5)
    except:
        pass

    prev = get_prev(symbol)
    if prev:
        price = prev["price"]
        if out["year_high"] > 0:
            out["near_52w_high"] = price >= out["year_high"] * 0.97
        if out["ath_high"] > 0:
            out["near_ath"] = price >= out["ath_high"] * 0.97
            out["ath_breakout_zone"] = price >= out["ath_high"] * 0.995

    HISTORY_CACHE[symbol] = out
    return out


def get_trend(symbol):
    try:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"2024-01-01/2026-12-31?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=22).json()
        data = r.get("results", [])
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


def get_volume_ratio(symbol, intraday=None):
    try:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"2024-01-01/2026-12-31?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=22).json()
        data = r.get("results", [])
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
        r = requests.get(url, timeout=15).json()
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
        "high_live": safe_round(to_float(snap.get("high", prev_high)) or prev_high),
        "low_live": safe_round(to_float(snap.get("low", prev_low)) or prev_low),
        "volume_live": safe_round(to_float(snap.get("volume", prev_volume)) or prev_volume),
        "price_source": price_source,
        "price_source_label": price_source_label_map.get(price_source, price_source),
        "price_reliable_for_execution": price_reliable_for_execution,
        "last_price_update_ms": int(to_float(snap.get("updated", 0))),
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


def get_news(symbol, company_name=""):
    news_note = "لا يوجد خبر بارز"
    catalyst_score = 0
    try:
        url = f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=10&order=desc&sort=published_utc&apiKey={POLYGON_API_KEY}"
        r = requests.get(url, timeout=12).json()
        results = r.get("results", [])
        if not results:
            return news_note, catalyst_score

        variants = get_company_name_variants(company_name)
        best = None
        best_score = -999
        best_note = ""

        for item in results:
            title = str(item.get("title", "") or "").strip()
            title_lower = normalize_text(title)
            if not title:
                continue

            related = [str(x).upper().strip() for x in item.get("tickers", []) if str(x).strip()]
            published_utc = str(item.get("published_utc", "") or "")
            sessions_since = trading_sessions_since_news(published_utc)

            relevance = 0
            if symbol in related:
                relevance += 3
            if any(v and v in title_lower for v in variants):
                relevance += 2
            if symbol.lower() in title_lower:
                relevance += 1

            impact, note = classify_news_impact(title_lower, sessions_since)
            total = relevance + impact

            if total > best_score:
                best_score = total
                best = title
                best_note = note
                catalyst_score = impact

        if best:
            news_note = best + (f" | {best_note}" if best_note else "")
    except:
        pass

    return news_note, catalyst_score


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


def get_financials(symbol):
    b = BALANCE_DATA.get(symbol, {})
    i = INCOME_DATA.get(symbol, {})

    total_assets = to_float(b.get("Total Assets", 0))
    cash = to_float(b.get("Cash And Cash Equivalents", 0))
    total_debt = to_float(b.get("Total Debt", 0))
    shares = to_float(i.get("Shares (Diluted)", 0)) or to_float(i.get("Shares (Basic)", 0))
    prev = get_prev(symbol)
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
    prev = get_prev(symbol)
    if not prev:
        return None

    info = get_info(symbol)
    financials = get_financials(symbol)
    hist = get_history_levels(symbol)
    trend_data = get_trend(symbol)
    intraday = get_intraday_snapshot(symbol)
    volume_ratio = get_volume_ratio(symbol, intraday)
    news_note, catalyst_score = get_news(symbol, info["company"])

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
    current_price = live_block["current_price_live"] if live_block["current_price_live"] > 0 else prev["price"]
    high = max(prev["high"], live_block["high_live"] if live_block["high_live"] > 0 else prev["high"])
    low = min(prev["low"], live_block["low_live"] if live_block["low_live"] > 0 else prev["low"])

    pullback_context = compute_pullback_context(current_price, high, low, intraday, trend_data["trend"])
    trade_type = "Pullback" if pullback_context.get("pullback_candidate") else "Breakout"

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

    decision = "مراقبة"
    if quality >= 85 and risk_pct <= 8:
        decision = "دخول قوي"
    elif quality >= 65 and risk_pct <= 12:
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
    for s in symbols:
        p = trade_plan_pro(s)
        if p and p.get("type") != "Excluded":
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

            rows.append(p)
            upsert_performance_signal(p)

    rows.sort(key=lambda x: (decision_priority(x.get("decision", "")), x.get("quality_score", 0)), reverse=True)
    return rows


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
                trade_plan.setdefault("risk_flags", []).append("السعر اللحظي غير موثوق")
    except Exception as e:
        trade_error = str(e)

    return {
        "symbol": symbol,
        "overview": overview,
        "trade_plan": trade_plan,
        "overview_error": overview_error,
        "trade_error": trade_error,
    }


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


@app.get("/performance")
def performance_get():
    store = rollover_performance_store_if_needed(load_performance_store())
    records = list(store.get("active_records", []))
    updated = []

    for item in records[:300]:
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
            "stop_loss": safe_round(item.get("stop_loss", 0)),
            "max_price_seen": safe_round(item.get("max_price_seen", 0)),
            "min_price_seen": safe_round(item.get("min_price_seen", 0)),
            "last_change_pct": safe_round(item.get("last_change_pct", 0)),
        })

    status_order = {"win": 0, "loss": 1, "pending": 2}
    updated.sort(key=lambda r: (status_order.get(r.get("outcome", "pending"), 9), r.get("first_seen_at", "")), reverse=False)

    store["active_records"] = updated
    save_performance_store(store)

    return {
        "active_week": {
            "week_key": store.get("active_week_key"),
            "week_start": store.get("active_week_start"),
            "week_end": store.get("active_week_end"),
        },
        "items": updated,
        "summary": make_performance_summary(updated),
        "weekly_archive": store.get("weekly_archive", [])[:26],
    }
