from fastapi import FastAPI
from fastapi.responses import FileResponse
import requests
import os
import csv
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from scanner import get_scan_universe, apply_late_move_filter, assign_execution_mode, normalize_execution_labels

app = FastAPI()

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY")

SECTOR_DATA = {}
COMPANIES_DATA = {}
BALANCE_DATA = {}
INCOME_DATA = {}
HISTORY_CACHE = {}
REF_INFO_CACHE = {}
INTRADAY_CACHE = {}

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




def get_snapshot_quote(symbol):
    try:
        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = requests.get(url, timeout=12).json()
        t = (r.get("ticker") or {})
        day = (t.get("day") or {})
        prev_day = (t.get("prevDay") or {})
        last_trade = (t.get("lastTrade") or {})
        min_data = (t.get("min") or {})

        current_price = to_float(last_trade.get("p")) or to_float(day.get("c")) or to_float(min_data.get("c")) or 0.0
        prev_close = to_float(prev_day.get("c")) or 0.0
        day_open = to_float(day.get("o")) or 0.0
        day_high = to_float(day.get("h")) or 0.0
        day_low = to_float(day.get("l")) or 0.0
        day_volume = to_float(day.get("v")) or 0.0

        change_vs_prev_close_pct = 0.0
        if current_price > 0 and prev_close > 0:
            change_vs_prev_close_pct = ((current_price - prev_close) / prev_close) * 100

        change_from_open_pct = 0.0
        if current_price > 0 and day_open > 0:
            change_from_open_pct = ((current_price - day_open) / day_open) * 100

        return {
            "available": current_price > 0,
            "current_price": current_price,
            "previous_close": prev_close,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "volume": day_volume,
            "change_vs_prev_close_pct": change_vs_prev_close_pct,
            "change_from_open_pct": change_from_open_pct,
        }
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


def get_volume_ratio(symbol):
    try:
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"2024-01-01/2026-12-31?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = requests.get(url, timeout=22).json()
        data = r.get("results", [])
        volumes = [to_float(x.get("v")) for x in data if to_float(x.get("v")) > 0]
        if len(volumes) < 20:
            return 1.0
        avg_volume = sum(volumes[-20:]) / 20
        today_volume = volumes[-1]
        return today_volume / avg_volume if avg_volume > 0 else 1.0
    except:
        return 1.0


def get_intraday_snapshot(symbol):
    symbol = str(symbol).upper().strip()
    market_open = is_market_open_now()
    cache_key = f"{symbol}:{'open' if market_open else 'closed'}"
    if cache_key in INTRADAY_CACHE:
        return INTRADAY_CACHE[cache_key]

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
        "bars_count": 0
    }

    if not market_open:
        INTRADAY_CACHE[cache_key] = out
        return out

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
            INTRADAY_CACHE[cache_key] = out
            return out

        volumes = [to_float(x.get("v")) for x in bars if to_float(x.get("v")) > 0]
        closes = [to_float(x.get("c")) for x in bars if to_float(x.get("c")) > 0]
        if not volumes or not closes:
            INTRADAY_CACHE[cache_key] = out
            return out

        session_open = to_float(bars[0].get("o"))
        session_high = max(to_float(x.get("h")) for x in bars)
        session_low = min(to_float(x.get("l")) for x in bars if to_float(x.get("l")) > 0)
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

        if current_price > session_open and current_price >= first_close:
            opening_drive = "صاعد"
        elif current_price < session_open and current_price <= first_close:
            opening_drive = "هابط"
        else:
            opening_drive = "متذبذب"

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
            "bars_count": len(bars)
        }
    except:
        pass

    INTRADAY_CACHE[cache_key] = out
    return out


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

    if phase == "open" and intraday_data.get("available"):
        current_price = to_float(intraday_data.get("current_price", 0)) or prev_price
        open_price = to_float(intraday_data.get("session_open", 0)) or prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100
        if previous_close > 0 and current_price > 0:
            change_vs_prev_close_pct = ((current_price - previous_close) / previous_close) * 100
    elif snap.get("available"):
        current_price = to_float(snap.get("current_price", prev_price)) or prev_price
        open_price = to_float(snap.get("open", prev_open)) or prev_open
        previous_close = to_float(snap.get("previous_close", prev_price)) or prev_price
        change_from_open_pct = to_float(snap.get("change_from_open_pct", 0))
        change_vs_prev_close_pct = to_float(snap.get("change_vs_prev_close_pct", 0))
    else:
        current_price = prev_price
        open_price = prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100

    return {
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "current_price_live": safe_round(current_price),
        "open_price_live": safe_round(open_price),
        "previous_close_live": safe_round(previous_close),
        "change_from_open_pct": safe_round(change_from_open_pct),
        "change_vs_prev_close_pct": safe_round(change_vs_prev_close_pct),
        "high_live": safe_round(to_float(snap.get("high", prev_high)) or prev_high),
        "low_live": safe_round(to_float(snap.get("low", prev_low)) or prev_low),
        "volume_live": safe_round(to_float(snap.get("volume", prev_volume)) or prev_volume),
    }



def get_effective_volume_ratio(volume_ratio: float, intraday: dict) -> float:
    try:
        effective = float(volume_ratio or 0)
        if intraday and intraday.get("available"):
            intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
            if intraday_ratio >= 2.0:
                effective = max(effective, 1.3)
            elif intraday_ratio >= 1.5:
                effective = max(effective, 1.15)
            elif intraday_ratio >= 1.2:
                effective = max(effective, 1.0)
            elif intraday_ratio >= 1.0:
                effective = max(effective, 0.9)
        return effective
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
        "earnings", "beats", "guidance", "raises outlook", "upgrade", "initiated", "outperform",
        "partnership", "deal", "contract", "acquisition", "merger", "approval", "fda", "launch",
        "record revenue", "strong growth", "buyback", "dividend increase", "wins", "winning"
    ]
    negative_keywords = [
        "downgrade", "miss", "cuts forecast", "lawsuit", "fraud", "investigation", "delay", "recall",
        "decline", "warning", "investor alert", "substantial losses", "law firm", "slumps", "falls"
    ]

    is_positive = any(k in title_lower for k in positive_keywords)
    is_negative = any(k in title_lower for k in negative_keywords)

    if not is_positive and not is_negative:
        return 0, "لا يوجد محفز حديث", "NONE"

    if is_positive:
        if sessions_since <= 1:
            return 12, "محفز إيجابي حديث", "POSITIVE_FRESH"
        if sessions_since == 2:
            return 5, "محفز إيجابي ضعيف", "POSITIVE_WEAK"
        return 0, "خبر إيجابي قديم - لا يعتمد عليه", "POSITIVE_OLD"

    if is_negative:
        if sessions_since <= 2:
            return -12, "خبر سلبي حديث", "NEGATIVE_FRESH"
        if sessions_since <= 5:
            return -6, "خبر سلبي ما زال مؤثرًا", "NEGATIVE_MEDIUM"
        return 0, "خبر سلبي قديم", "NEGATIVE_OLD"

    return 0, "لا يوجد محفز حديث", "NONE"



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


def compute_breakout_levels(current_price: float, high_price: float, low_price: float, intraday: dict, trade_type: str):
    breakout_price = float(high_price or current_price or 0)
    if trade_type == "Breakout":
        if breakout_price <= 0:
            breakout_price = float(current_price or 0)

        if breakout_price < 5:
            confirmation_price = breakout_price * 1.008
            entry_price_real = confirmation_price * 1.006
            late_entry_price = entry_price_real * 1.018
        elif breakout_price < 20:
            confirmation_price = breakout_price * 1.0045
            entry_price_real = confirmation_price * 1.0035
            late_entry_price = entry_price_real * 1.015
        else:
            confirmation_price = breakout_price * 1.0025
            entry_price_real = confirmation_price * 1.0025
            late_entry_price = entry_price_real * 1.01
    else:
        confirmation_price = breakout_price
        entry_price_real = current_price
        late_entry_price = current_price * 1.02 if current_price > 0 else 0

    breakout_status = "لا ينطبق"
    if trade_type == "Breakout":
        if current_price < breakout_price:
            breakout_status = "قبل الاختراق"
        elif current_price < confirmation_price:
            breakout_status = "بعد الكسر وقبل التأكيد"
        elif current_price <= entry_price_real:
            breakout_status = "تأكيد الاختراق"
        elif current_price <= late_entry_price:
            breakout_status = "اختراق مؤكد - دخول بحذر"
        else:
            breakout_status = "اختراق متأخر"

    return {
        "breakout_price": safe_round(breakout_price),
        "confirmation_price": safe_round(confirmation_price),
        "entry_price_real": safe_round(entry_price_real),
        "late_entry_price": safe_round(late_entry_price),
        "breakout_status": breakout_status
    }

def get_news_catalyst(symbol):
    try:
        return {"has_news": False, "catalyst_score": 0, "note": "لا يوجد أخبار"} if not POLYGON_API_KEY else _news_impl(symbol)
    except Exception as e:
        return {"has_news": False, "catalyst_score": 0, "note": f"خطأ في الأخبار: {str(e)}"}



def _news_impl(symbol):
    info = get_info(symbol)
    company_name = info["company"]
    url = f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=10&apiKey={POLYGON_API_KEY}"
    r = requests.get(url, timeout=12).json()
    news = r.get("results", [])
    if not news:
        return {"has_news": False, "catalyst_score": 0, "note": "لا يوجد محفز حديث", "freshness_label": "NONE", "sessions_since": None}

    symbol_lower = symbol.lower()
    company_variants = get_company_name_variants(company_name)
    weak_patterns = [
        "top stocks", "market update", "stock market", "s&p 500", "nasdaq", "dow jones", "why investors",
        "what to know", "best stocks", "should you buy", "index fund", "etf", "top-ranked stocks",
        "stocks to buy now", "long term", "consumer tech news", "weekly recap", "roundup", "news recap",
        "worth buying", "worth holding", "bullish on", "best way to buy", "compare", "comparison", "vs.",
        "versus", "top picks", "3 stocks", "5 stocks", "10 stocks"
    ]

    best = {"score": 0, "note": "لا يوجد محفز حديث", "freshness_label": "NONE", "sessions_since": None, "freshness_note": "لا يوجد محفز حديث", "has_news": False}

    for item in news[:10]:
        title = str(item.get("title", "")).strip()
        published = str(item.get("published_utc", "")).strip()
        if not title:
            continue

        title_lower = normalize_text(title)
        if any(w in title_lower for w in weak_patterns):
            continue

        symbol_match = re.search(rf"\b{re.escape(symbol_lower)}\b", title_lower) is not None
        company_match = any(v in title_lower for v in company_variants if len(v) >= 4)
        if not symbol_match and not company_match:
            continue

        sessions_since = trading_sessions_since_news(published)
        score, freshness_note, freshness_label = classify_news_impact(title_lower, sessions_since)
        if score == 0:
            continue

        if abs(score) > abs(best["score"]):
            best = {
                "score": score,
                "note": title[:120],
                "freshness_label": freshness_label,
                "sessions_since": sessions_since,
                "freshness_note": freshness_note,
                "has_news": True
            }

    if not best["has_news"]:
        return {"has_news": False, "catalyst_score": 0, "note": "لا يوجد محفز حديث", "freshness_label": "NONE", "sessions_since": None}

    return {
        "has_news": True,
        "catalyst_score": best["score"],
        "note": f'{best["note"]} | {best["freshness_note"]}',
        "freshness_label": best["freshness_label"],
        "sessions_since": best["sessions_since"],
        "freshness_note": best["freshness_note"]
    }


def data_quality_check(symbol, info, financials):
    flags = []
    quality = "high"
    if not info["company"]:
        quality = "low"
        flags.append("اسم الشركة غير متوفر")
    if not info["sector"] or not info["industry"]:
        quality = "low"
        flags.append("بيانات القطاع/الصناعة ناقصة")
    if financials.get("total_assets", 0) == 0:
        quality = "low"
        flags.append("إجمالي الأصول غير متوفر")
    if financials.get("shares", 0) == 0:
        quality = "low"
        flags.append("عدد الأسهم غير متوفر")
    if financials.get("approx_market_cap", 0) == 0:
        quality = "low"
        flags.append("القيمة السوقية التقريبية غير متوفرة")
    return quality, flags


def halal(symbol):
    info = get_info(symbol)
    text = f"{info['sector']} {info['industry']}".lower()

    if info["sector"].lower() in HARAM_SECTORS:
        return {"allowed": False, "reason": f"قطاع محرم: {info['sector']}", "financials": {}}

    for word in HARAM_INDUSTRY_KEYWORDS:
        if word in text:
            return {"allowed": False, "reason": f"نشاط محرم: {word}", "financials": {}}

    balance = BALANCE_DATA.get(symbol, {})
    income = INCOME_DATA.get(symbol, {})

    total_debt = to_float(balance.get("Short Term Debt")) + to_float(balance.get("Long Term Debt"))
    total_assets = to_float(balance.get("Total Assets"))
    cash = to_float(balance.get("Cash, Cash Equivalents & Short Term Investments"))

    shares = to_float(income.get("Shares (Diluted)"))
    if shares <= 0:
        shares = to_float(income.get("Shares (Basic)"))

    prev = get_prev(symbol)
    current_price = prev["price"] if prev else 0.0
    approx_market_cap = current_price * shares if shares > 0 else 0.0

    debt_to_market_cap = (total_debt / approx_market_cap) if approx_market_cap > 0 else None
    cash_to_assets = (cash / total_assets) if total_assets > 0 else None

    financials = {
        "total_assets": total_assets,
        "cash": cash,
        "total_debt": total_debt,
        "shares": shares,
        "current_price": current_price,
        "approx_market_cap": approx_market_cap,
        "debt_to_market_cap": debt_to_market_cap,
        "cash_to_assets": cash_to_assets,
    }

    if debt_to_market_cap is not None and debt_to_market_cap > 0.33:
        return {"allowed": False, "reason": f"نسبة الدين إلى القيمة السوقية مرتفعة: {debt_to_market_cap:.2%}", "financials": financials}
    if cash_to_assets is not None and cash_to_assets > 0.50:
        return {"allowed": False, "reason": f"نسبة النقد إلى الأصول مرتفعة: {cash_to_assets:.2%}", "financials": financials}

    return {"allowed": True, "reason": "مقبول مبدئيًا", "financials": financials}



def base_analysis(symbol):
    prev = get_prev(symbol)
    if not prev:
        return None

    intraday = get_intraday_snapshot(symbol)

    if intraday.get("available"):
        price = intraday.get("current_price", prev["price"])
        high = max(prev["high"], intraday.get("session_high", 0))
        low = min(prev["low"], intraday.get("session_low", prev["low"])) if intraday.get("session_low", 0) > 0 else prev["low"]
        volume = max(prev["volume"], intraday.get("session_volume", 0))
        open_price = intraday.get("session_open", prev["open"])
    else:
        price = prev["price"]
        high = prev["high"]
        low = prev["low"]
        volume = prev["volume"]
        open_price = prev["open"]

    day_range = max(high - low, 0.01)
    range_pct = day_range / price if price > 0 else 0.0

    momentum = "محايد"
    if price > open_price:
        momentum = "صاعد"
    elif price < open_price:
        momentum = "هابط"

    body_strength = abs(price - open_price) / day_range if day_range > 0 else 0.0
    close_strength = (price - low) / day_range if day_range > 0 else 0.0

    near_high = high > 0 and price >= high * 0.985
    near_low = low > 0 and price <= low * 1.02

    location = "وسط"
    if near_high:
        location = "قرب مقاومة"
    elif near_low:
        location = "قرب دعم"

    return {
        "symbol": symbol,
        "price": price,
        "high": high,
        "low": low,
        "open": open_price,
        "volume": volume,
        "day_range": day_range,
        "range_pct": range_pct,
        "momentum": momentum,
        "body_strength": body_strength,
        "close_strength": close_strength,
        "location": location,
        "near_high": near_high,
        "near_low": near_low
    }


def execution_filter(stock: dict) -> dict:
    entry = to_float(stock.get("entry", 0))
    current = to_float(stock.get("financials", {}).get("current_price", 0))
    volume_ratio = to_float(stock.get("volume_ratio", 0))
    breakout_quality = str(stock.get("breakout_quality", "")).upper()
    trade_type = str(stock.get("type", ""))
    decision = str(stock.get("decision", ""))
    existing_status = str(stock.get("execution_status", ""))
    intraday = stock.get("intraday", {}) or {}

    if existing_status == "AVOID":
        return stock

    if intraday.get("available"):
        current = to_float(intraday.get("current_price", current))

    is_above_entry = current > entry * 1.003 if entry > 0 and current > 0 else False
    daily_volume_ok = volume_ratio >= 1.0
    intraday_volume_ok = to_float(intraday.get("intraday_volume_ratio", 0)) >= 1.2 if intraday else False
    has_volume = daily_volume_ok or intraday_volume_ok
    acceptable_breakout = breakout_quality in {"STRONG", "WEAK"}
    above_vwap = bool(intraday.get("above_vwap_proxy", False)) if intraday else False
    opening_drive = str(intraday.get("opening_drive", "unknown"))

    if trade_type == "Breakout":
        if is_above_entry and has_volume and acceptable_breakout and decision in {"دخول قوي", "دخول بحذر"} and (above_vwap or not intraday.get("available")):
            stock["execution_status"] = "EXECUTE"
            stock["owner_action"] = "🔥 دخول فوري - تحقق تأكيد بعد الافتتاح"
        elif is_above_entry and not has_volume:
            stock["execution_status"] = "WAIT_VOLUME"
            stock["owner_action"] = "انتظر دخول سيولة أوضح"
        elif intraday.get("available") and not above_vwap:
            stock["execution_status"] = "WAIT_VWAP"
            stock["owner_action"] = "انتظر الثبات فوق متوسط اليوم"
        elif intraday.get("available") and opening_drive == "هابط":
            stock["execution_status"] = "WAIT_OPENING"
            stock["owner_action"] = "انتظر تحسن افتتاح السهم أولاً"
        elif not is_above_entry:
            stock["execution_status"] = "WAIT_BREAKOUT"
            stock["owner_action"] = "لم يتم تأكيد الاختراق فعليًا بعد"
        else:
            stock["execution_status"] = "WAIT"

    return stock


def trade_plan_pro(symbol):
    a = base_analysis(symbol)
    if not a:
        return None

    price = a["price"]
    high = a["high"]
    low = a["low"]
    volume = a["volume"]
    range_pct = a["range_pct"]
    near_high = a["near_high"]
    near_low = a["near_low"]
    momentum = a["momentum"]
    location = a["location"]
    body_strength = a["body_strength"]
    close_strength = a["close_strength"]

    if price < LOW_PRICE_HARD_BLOCK:
        return None

    risk_flags = []
    hard_block = False

    if price < LOW_PRICE_WARNING:
        risk_flags.append("سهم منخفض السعر - مخاطرة عالية")

    trend_data = get_trend(symbol)
    volume_ratio = get_volume_ratio(symbol)
    trend = trend_data["trend"]
    history = get_history_levels(symbol)
    near_ath = history["near_ath"]
    ath_breakout_zone = history["ath_breakout_zone"]
    news = get_news_catalyst(symbol)
    intraday = get_intraday_snapshot(symbol)
    effective_volume_ratio = get_effective_volume_ratio(volume_ratio, intraday)

    trade_type = "Watch"
    entry = price
    stop = low * 0.99 if low > 0 else price * 0.95
    early_momentum = False

    if near_high and momentum == "صاعد":
        trade_type = "Breakout"
        entry = high * 1.002
        stop = low * 0.995
    elif near_high and trend in {"صاعد", "صاعد قوي"} and volume_ratio >= 1.1:
        trade_type = "Breakout"
        entry = high * 1.001
        stop = low * 0.995
        early_momentum = True
        risk_flags.append("اقتراب مبكر من الاختراق")
    elif near_low:
        trade_type = "Pullback"
        entry = price
        stop = low * 0.99

    risk = entry - stop
    if risk <= 0:
        stop = price * 0.95
        risk = entry - stop

    risk_pct = risk / entry if entry > 0 else 0.0
    target_1 = entry + risk * 1.5
    target_2 = entry + risk * 2.0

    breakout_quality = breakout_quality_label(trade_type, momentum, body_strength, close_strength, effective_volume_ratio)
    quality_score = 32

    if volume > 100_000_000:
        quality_score += 14
    elif volume > 50_000_000:
        quality_score += 11
    elif volume > 10_000_000:
        quality_score += 7
    elif volume > 2_000_000:
        quality_score += 3
    else:
        quality_score -= 9
        risk_flags.append("سيولة يومية ضعيفة")

    if momentum == "صاعد":
        quality_score += 9
    elif momentum == "هابط":
        quality_score -= 6

    if trend == "صاعد قوي":
        quality_score += 13
    elif trend == "صاعد":
        quality_score += 7
    elif trend == "هابط":
        quality_score -= 13

    if trade_type == "Breakout":
        quality_score += 8
    elif trade_type == "Pullback":
        quality_score += 5
    else:
        quality_score -= 3

    if early_momentum:
        quality_score += 6

    if intraday["available"]:
        if intraday["intraday_volume_ratio"] >= 1.5:
            quality_score += 8
            risk_flags.append("سيولة لحظية قوية")
        elif intraday["intraday_volume_ratio"] >= 1.2:
            quality_score += 4
        if intraday["above_vwap_proxy"]:
            quality_score += 4
        else:
            quality_score -= 3
        if intraday["opening_drive"] == "صاعد":
            quality_score += 4
        elif intraday["opening_drive"] == "هابط":
            quality_score -= 4
        if entry > 0 and intraday["current_price"] >= entry * 0.985:
            quality_score += 4
            risk_flags.append("قريب جدًا من الاختراق")

    if trade_type == "Breakout" and trend == "هابط":
        quality_score -= 16
        risk_flags.append("اختراق عكس الاتجاه")
        hard_block = True

    if trade_type == "Breakout":
        if effective_volume_ratio >= 1.5:
            quality_score += 9
        elif effective_volume_ratio >= 1.2:
            quality_score += 5
        elif effective_volume_ratio >= 1.0:
            quality_score += 1
            risk_flags.append("اختراق يحتاج تأكيد")
        elif effective_volume_ratio >= 0.85:
            quality_score -= 2
            risk_flags.append("اختراق ضعيف بدون سيولة")
        else:
            quality_score -= 6
            risk_flags.append("اختراق فاشل (سيولة ضعيفة جدًا)")
            if not (intraday.get("available") and float(intraday.get("intraday_volume_ratio", 0) or 0) >= 1.2):
                hard_block = True
    elif trade_type == "Pullback":
        if effective_volume_ratio >= 1.3:
            quality_score += 4
        elif effective_volume_ratio >= 1.0:
            quality_score += 1
        elif effective_volume_ratio < 0.8:
            quality_score -= 5
    else:
        if effective_volume_ratio >= 1.3:
            quality_score += 2
        elif effective_volume_ratio < 0.8:
            quality_score -= 5

    if breakout_quality == "STRONG":
        quality_score += 9
        risk_flags.append("اختراق قوي")
    elif breakout_quality == "WEAK":
        quality_score -= 4
        if trade_type == "Breakout":
            risk_flags.append("شمعة اختراق ضعيفة")
    elif breakout_quality == "FAILED":
        quality_score -= 13
        risk_flags.append("سلوك اختراق فاشل")
        if trade_type == "Breakout":
            hard_block = True

    if trade_type == "Breakout" and location == "قرب مقاومة":
        quality_score += 5
    elif trade_type == "Pullback" and location == "قرب دعم":
        quality_score += 6

    if ath_breakout_zone and momentum == "صاعد":
        quality_score += 6
        risk_flags.append("قرب اختراق ATH")
    elif near_ath and trade_type == "Breakout" and breakout_quality != "STRONG":
        quality_score -= 3
        risk_flags.append("قرب ATH بدون تأكيد")

    if risk_pct <= 0.02:
        quality_score += 8
    elif risk_pct <= 0.04:
        quality_score += 4
    elif risk_pct <= 0.07:
        quality_score -= 2
    else:
        quality_score -= 8
        risk_flags.append("مخاطرة مرتفعة")
        hard_block = True

    if 0.02 <= range_pct <= 0.08:
        quality_score += 5
    elif range_pct > 0.15:
        quality_score -= 7
        risk_flags.append("ذبذبة يومية عالية")

    if abs(news["catalyst_score"]) >= 6:
        quality_score += news["catalyst_score"]
    elif abs(news["catalyst_score"]) >= 3:
        quality_score += news["catalyst_score"] * 0.5

    if news["catalyst_score"] >= 6:
        risk_flags.append("خبر إيجابي محفز")
    elif news["catalyst_score"] <= -6:
        risk_flags.append("خبر سلبي ⚠️")

    info = get_info(symbol)
    h = halal(symbol)
    data_quality, dq_flags = data_quality_check(symbol, info, h["financials"])
    risk_flags.extend(dq_flags)

    if data_quality == "low":
        if effective_volume_ratio >= 1.0 and trend in {"صاعد", "صاعد قوي"}:
            quality_score -= 5
        else:
            quality_score -= 9

    quality_score = max(1, min(100, quality_score))

    if risk_pct > 0.10:
        decision = "مراقبة"
    elif risk_pct > 0.08:
        if quality_score >= 68:
            decision = "دخول بحذر"
        elif quality_score >= 56:
            decision = "مراقبة"
        else:
            decision = "مراقبة"
    else:
        if hard_block and quality_score < 68:
            decision = "مراقبة"
        elif quality_score >= 78:
            decision = "دخول قوي"
        elif quality_score >= 68:
            decision = "دخول بحذر"
        elif quality_score >= 56:
            decision = "مراقبة"
        else:
            decision = "مراقبة"

    if (
        decision == "مراقبة"
        and risk_pct <= 0.065
        and trend in {"صاعد", "صاعد قوي"}
        and volume_ratio >= 1.0
        and breakout_quality == "WEAK"
        and trade_type == "Breakout"
        and not hard_block
    ):
        decision = "دخول بحذر"

    if trade_type == "Breakout" and volume_ratio < 0.8:
        if decision in {"دخول قوي", "دخول بحذر"}:
            decision = "مراقبة"

    if trend == "هابط" and decision in {"دخول قوي", "دخول بحذر"}:
        decision = "مراقبة"

    if data_quality == "low" and decision == "دخول قوي":
        decision = "دخول بحذر"


    reasons = []
    if trend == "صاعد قوي":
        reasons.append("الاتجاه صاعد قوي")
    elif trend == "صاعد":
        reasons.append("الاتجاه إيجابي")
    elif trend == "هابط":
        reasons.append("الاتجاه سلبي")
    else:
        reasons.append("الاتجاه متذبذب")

    if volume_ratio < 1:
        reasons.append("السيولة ضعيفة")
    elif volume_ratio >= 1.5:
        reasons.append("السيولة قوية")
    elif volume_ratio >= 1.0:
        reasons.append("السيولة مقبولة")

    if intraday["available"]:
        reasons.append(f"افتتاح اليوم: {intraday['opening_drive']}")
        if intraday["intraday_volume_ratio"] >= 1.2:
            reasons.append("السيولة اللحظية داعمة")

    if news["catalyst_score"] > 0:
        reasons.append(news.get("freshness_note", "محفز إيجابي حديث"))
    elif news["catalyst_score"] < 0:
        reasons.append(news.get("freshness_note", "خبر سلبي حديث"))
    else:
        reasons.append("لا يوجد محفز حديث")

    if trade_type == "Breakout":
        reasons.append("محاولة اختراق")
    elif trade_type == "Pullback":
        reasons.append("ارتداد من دعم")
    else:
        reasons.append("فرصة واعدة مشروطة")

    if early_momentum:
        reasons.append("بداية زخم مبكرة")

    if breakout_quality == "STRONG":
        reasons.append("شمعة الاختراق قوية")
    elif breakout_quality == "WEAK":
        reasons.append("شمعة الاختراق ضعيفة")
    elif breakout_quality == "FAILED":
        reasons.append("شمعة الاختراق فشلت")

    if data_quality == "low":
        reasons.append("جودة البيانات ضعيفة")

    live_block = build_live_price_block(symbol, a, intraday)
    levels = compute_breakout_levels(live_block["current_price_live"], high, low, intraday, trade_type)
    timing = compute_timing_layer(live_block["current_price_live"], intraday, effective_volume_ratio, levels, live_block.get("market_phase", "closed"))

    return {
        "symbol": symbol,
        "type": trade_type,
        "decision": decision,
        "entry": safe_round(entry),
        "stop_loss": safe_round(stop),
        "target_1": safe_round(target_1),
        "target_2": safe_round(target_2),
        "risk_pct": safe_round(risk_pct * 100),
        "quality_score": quality_score,
        "rank_label": make_rank_label(quality_score),
        "valid_for": estimate_validity(trade_type, trend, effective_volume_ratio, news["catalyst_score"]),
        "trend": trend,
        "volume_ratio": round(volume_ratio, 2),
        "effective_volume_ratio": round(effective_volume_ratio, 2),
        "data_quality": data_quality,
        "catalyst_score": news["catalyst_score"],
        "news_note": news["note"],
        "news_freshness_label": news.get("freshness_label", "NONE"),
        "news_sessions_since": news.get("sessions_since", None),
        "risk_flags": risk_flags,
        "ai_summary": " - ".join(reasons) if reasons else "لا يوجد وضوح كافي",
        "breakout_quality": breakout_quality,
        "execution_status": compute_execution_status(trade_type, decision, trend, effective_volume_ratio, news["catalyst_score"], breakout_quality),
        "owner_action": owner_decision(decision, trend, breakout_quality, effective_volume_ratio, news["catalyst_score"]),
        "intraday": intraday,
        **levels,
        **timing,
        **live_block
    }




def build_fallback_trade(symbol: str):
    a = base_analysis(symbol)
    if not a:
        return None
    info = get_info(symbol)
    h = halal(symbol)
    intraday = get_intraday_snapshot(symbol)
    volume_ratio = get_volume_ratio(symbol)
    effective_volume_ratio = get_effective_volume_ratio(volume_ratio, intraday)
    live_block = build_live_price_block(symbol, a, intraday)
    levels = compute_breakout_levels(live_block["current_price_live"], a.get("high", 0), a.get("low", 0), intraday, "Breakout")
    timing = compute_timing_layer(live_block["current_price_live"], intraday, effective_volume_ratio, levels, live_block.get("market_phase", "closed"))
    current = float(live_block.get("current_price_live", 0) or 0)
    breakout = float(levels.get("breakout_price", 0) or 0)
    confirm = float(levels.get("confirmation_price", 0) or 0)
    entry_real = float(levels.get("entry_price_real", 0) or 0)

    if current < breakout:
        execution_status = "WAIT_BREAKOUT"
        execution_note = f"راقب كسر {round(breakout,2)}"
    elif current < confirm:
        execution_status = "WAIT_CONFIRM"
        execution_note = f"يحتاج الثبات فوق {round(confirm,2)}"
    elif current <= entry_real:
        execution_status = "READY"
        execution_note = f"منطقة دخول قريبة من {round(entry_real,2)}"
    else:
        execution_status = "CAUTION"
        execution_note = "التحرك بدأ لكن ما زالت تحت المراقبة"

    quality_score = 34
    trend = get_trend(symbol).get("trend", "متذبذب")
    if trend == "صاعد قوي":
        quality_score += 12
    elif trend == "صاعد":
        quality_score += 7
    elif trend == "هابط":
        quality_score -= 6
    if effective_volume_ratio >= 1.0:
        quality_score += 6
    elif effective_volume_ratio < 0.8:
        quality_score -= 4
    quality_score = max(15, min(55, quality_score))

    stop_loss = a.get("low", 0) * 0.99 if a.get("low", 0) > 0 else current * 0.95
    target_1 = entry_real + max(entry_real - stop_loss, current * 0.03)
    target_2 = entry_real + max((entry_real - stop_loss) * 1.5, current * 0.05)
    risk_pct = ((entry_real - stop_loss) / entry_real * 100) if entry_real > stop_loss > 0 else 0.0

    out = {
        "symbol": symbol,
        "type": "Breakout",
        "decision": "مراقبة",
        "entry": safe_round(entry_real),
        "stop_loss": safe_round(stop_loss),
        "target_1": safe_round(target_1),
        "target_2": safe_round(target_2),
        "risk_pct": safe_round(risk_pct),
        "quality_score": int(quality_score),
        "rank_label": make_rank_label(quality_score),
        "valid_for": "صالح للمراقبة",
        "trend": trend,
        "volume_ratio": round(volume_ratio, 2),
        "effective_volume_ratio": round(effective_volume_ratio, 2),
        "data_quality": "medium",
        "catalyst_score": 0,
        "news_note": "لا يوجد محفز حديث",
        "news_freshness_label": "NONE",
        "news_sessions_since": None,
        "risk_flags": ["تم توليد بطاقة احتياطية بسبب نقص/تعطل بعض البيانات"],
        "ai_summary": "بطاقة احتياطية لضمان عدم اختفاء السهم - راقب السعر والاختراق",
        "breakout_quality": "WEAK",
        "execution_status": execution_status,
        "execution_note": execution_note,
        "owner_action": execution_note,
        "intraday": intraday,
        "company": info.get("company", ""),
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "financials": h.get("financials", {}),
        **levels,
        **timing,
        **live_block,
    }
    out = normalize_execution_labels(assign_execution_mode(apply_late_move_filter(out)))
    return out

def analyze_symbol_overview(symbol):
    symbol = str(symbol).upper().strip()
    prev = get_prev(symbol)
    if not prev:
        return {"symbol": symbol, "available": False}

    info = get_info(symbol)
    trend_data = get_trend(symbol)
    volume_ratio = get_volume_ratio(symbol)
    news = get_news_catalyst(symbol)
    history = get_history_levels(symbol)
    intraday = get_intraday_snapshot(symbol)
    halal_check = halal(symbol)
    live_block = build_live_price_block(symbol, prev, intraday)

    return {
        "symbol": symbol,
        "available": True,
        "company": info.get("company", ""),
        "sector": info.get("sector", ""),
        "industry": info.get("industry", ""),
        "price": safe_round(prev.get("price", 0)),
        "open": safe_round(prev.get("open", 0)),
        "high": safe_round(prev.get("high", 0)),
        "low": safe_round(prev.get("low", 0)),
        "volume": safe_round(prev.get("volume", 0)),
        "trend": trend_data.get("trend", "unknown"),
        "volume_ratio": safe_round(volume_ratio),
        "news_note": news.get("note", ""),
        "catalyst_score": news.get("catalyst_score", 0),
        "news_freshness_label": news.get("freshness_label", "NONE"),
        "news_sessions_since": news.get("sessions_since", None),
        "near_ath": history.get("near_ath", False),
        "ath_breakout_zone": history.get("ath_breakout_zone", False),
        "intraday": intraday,
        "halal": halal_check.get("allowed", False),
        "halal_reason": halal_check.get("reason", ""),
        **live_block
    }


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/health")
def health():
    return {
        "message": "Stock Radar AI is running 🚀",
        "loaded": {
            "companies": len(COMPANIES_DATA),
            "sector_industry": len(SECTOR_DATA),
            "balance_rows": len(BALANCE_DATA),
            "income_rows": len(INCOME_DATA),
        },
        "market_open_now": is_market_open_now(),
        "market_phase": get_market_phase(),
        "market_phase_label": market_phase_label(get_market_phase())
    }


@app.get("/trade-scan")
def trade_scan():
    trades = []
    rejected = []
    errors = []

    universe = get_active_universe(max_symbols=60)

    for s in universe:
        try:
            prev = get_prev(s)
            if prev and prev["price"] < LOW_PRICE_HARD_BLOCK:
                rejected.append({"symbol": s, "reason": f"سعر أقل من {LOW_PRICE_HARD_BLOCK}$"})
                continue

            h = halal(s)
            if not h["allowed"]:
                rejected.append({"symbol": s, "reason": h["reason"]})
                continue

            t = trade_plan_pro(s)
            if not t:
                t = build_fallback_trade(s)

            if t:
                info = get_info(s)
                t["company"] = info.get("company", t.get("company", ""))
                t["sector"] = info.get("sector", t.get("sector", ""))
                t["industry"] = info.get("industry", t.get("industry", ""))
                t["financials"] = h.get("financials", t.get("financials", {}))
                t = execution_filter(t)
                t = apply_late_move_filter(t)
                t = assign_execution_mode(t)
                t = normalize_execution_labels(t)
                trades.append(t)

        except Exception as e:
            errors.append({"symbol": s, "error": str(e)})
            continue

    trades = sorted(trades, key=lambda x: (decision_priority(x["decision"]), x["quality_score"]), reverse=True)

    top_ranked = trades[:5]
    strong_entries = [x for x in trades if x["decision"] == "دخول قوي"]
    cautious_entries = [x for x in trades if x["decision"] == "دخول بحذر"]
    watch = [x for x in trades if x["decision"] == "مراقبة"]

    phase = get_market_phase()
    return {
        "generated_at_utc": datetime.utcnow().isoformat() + "Z",
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "universe_count": len(universe),
        "count": len(trades),
        "strong_entries_count": len(strong_entries),
        "cautious_entries_count": len(cautious_entries),
        "watchlist_count": len(watch),
        "top_ranked_count": len(top_ranked),
        "top_ranked": top_ranked,
        "strong_entries": strong_entries,
        "cautious_entries": cautious_entries,
        "watchlist": watch,
        "rejected_count": len(rejected),
        "rejected": rejected[:30],
        "errors_count": len(errors),
        "errors": errors[:20]
    }



@app.get("/single-stock")
def single_stock(symbol: str):
    try:
        symbol = str(symbol).upper().strip()
        if not symbol:
            return {"error": "يرجى إدخال رمز السهم"}

        overview = {}
        trade = None
        overview_error = None
        trade_error = None

        try:
            overview = analyze_symbol_overview(symbol)
        except Exception as e:
            overview = {}
            overview_error = str(e)

        try:
            trade = trade_plan_pro(symbol)
            if not trade:
                trade = build_fallback_trade(symbol)
        except Exception as e:
            trade = build_fallback_trade(symbol)
            trade_error = str(e)

        if trade:
            try:
                info = get_info(symbol) or {}
            except Exception:
                info = {}

            try:
                h = halal(symbol) or {}
            except Exception:
                h = {}

            trade["company"] = info.get("company", trade.get("company", ""))
            trade["sector"] = info.get("sector", trade.get("sector", ""))
            trade["industry"] = info.get("industry", trade.get("industry", ""))
            trade["financials"] = h.get("financials", trade.get("financials", {}))

            try:
                trade = execution_filter(trade)
            except Exception:
                pass

            try:
                trade = apply_late_move_filter(trade)
            except Exception:
                pass

            try:
                trade = assign_execution_mode(trade)
            except Exception:
                pass

            try:
                trade = normalize_execution_labels(trade)
            except Exception:
                pass

        response = {
            "symbol": symbol,
            "overview": overview,
            "trade_plan": trade
        }

        if overview_error:
            response["overview_error"] = overview_error
        if trade_error:
            response["trade_error"] = trade_error

        return response

    except Exception as e:
        return {
            "error": f"single-stock server error: {str(e)}",
            "symbol": str(symbol).upper().strip() if symbol else ""
        }

@app.get("/debug/{symbol}")
def debug_symbol(symbol: str):
    symbol = symbol.upper()
    overview = analyze_symbol_overview(symbol)
    trade = trade_plan_pro(symbol) or build_fallback_trade(symbol)

    if trade:
        info = get_info(symbol)
        h = halal(symbol)
        trade["company"] = info["company"]
        trade["sector"] = info["sector"]
        trade["industry"] = info["industry"]
        trade["financials"] = h["financials"]
        trade = execution_filter(trade)
        trade = apply_late_move_filter(trade)
        trade = assign_execution_mode(trade)
        trade = normalize_execution_labels(trade)

    return {
        "symbol": symbol,
        "sector_info": get_info(symbol),
        "balance": BALANCE_DATA.get(symbol, {}),
        "income": INCOME_DATA.get(symbol, {}),
        "history_levels": get_history_levels(symbol),
        "halal_check": halal(symbol),
        "base_analysis": base_analysis(symbol),
        "news_catalyst": get_news_catalyst(symbol),
        "trade_plan": trade,
        "overview": overview,
        "market_open_now": is_market_open_now(),
        "market_phase": get_market_phase(),
        "market_phase_label": market_phase_label(get_market_phase())
    }

