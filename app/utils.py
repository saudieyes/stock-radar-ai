import time
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .settings import (
    POSITIVE_NEWS_MAX_SESSIONS,
    NEGATIVE_NEWS_MAX_SESSIONS,
    HTTP_SESSION,
)
def ny_now():
    return datetime.now(ZoneInfo("America/New_York"))

def normalize_symbol_text(symbol: str) -> str:
    return str(symbol or "").upper().strip()

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


def _text_label(value) -> str:
    return str(value or "").strip()


def historical_confidence_bonus(label: str) -> float:
    label = _text_label(label)
    if "عالية" in label:
        return 6.0
    if "متوسطة" in label:
        return 3.0
    return 0.0


def historical_behavior_bonus(label: str) -> float:
    label = _text_label(label)
    if "يدعم" in label:
        return 4.0
    if "محايد" in label:
        return 0.0
    if "ضعيف" in label or "لا يدعم" in label:
        return -2.0
    return 0.0

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
    return classify_news_effect("company", detect_news_sentiment(title_lower), sessions_since), ""


POSITIVE_NEWS_MAX_SESSIONS = 3
NEGATIVE_NEWS_MAX_SESSIONS = 5


def get_news_session_limit(scope: str, sentiment: str) -> int:
    scope = str(scope or "neutral")
    sentiment = str(sentiment or "neutral")
    if scope in {"market", "opinion", "neutral", "unrelated"}:
        return 0
    if sentiment == "positive":
        return POSITIVE_NEWS_MAX_SESSIONS
    if sentiment in {"negative", "legal"}:
        return NEGATIVE_NEWS_MAX_SESSIONS
    return 0


def is_news_within_session_limit(scope: str, sentiment: str, sessions_since: int) -> bool:
    limit = get_news_session_limit(scope, sentiment)
    if limit <= 0:
        return False
    return int(sessions_since or 999) <= limit


def classify_news_freshness_label(sessions_since: int) -> tuple[str, int]:
    if sessions_since <= 0:
        return "حديث جدًا", 100
    if sessions_since == 1:
        return "حديث", 78
    if sessions_since == 2:
        return "حديث نسبيًا", 52
    if sessions_since == 3:
        return "أقدم قليلًا", 28
    if sessions_since <= 5:
        return "قديم", 12
    return "قديم جدًا", 4


NEWS_SCOPE_LABELS = {
    "company": "خبر شركة",
    "sector": "خبر قطاعي",
    "market": "سياق سوق عام",
    "opinion": "مقال رأي",
    "neutral": "محايد",
    "unrelated": "غير ذي صلة",
}


def news_scope_label(scope: str) -> str:
    return NEWS_SCOPE_LABELS.get(str(scope or "neutral"), "محايد")

