"""Opportunity Radar Rebuild V1.

Backend-only enrichment layer for the user's new opportunity philosophy:
- Strong remains strict; this layer creates the living stages before Strong.
- Support/resistance are displayed as zones, not cent-level fake precision.
- Rows are grouped into Support Bounce, Reclaim, Pre-Trigger, Low-Float/PM,
  Gap Fill, Catalyst/News, Continuation Pullback, and High-Risk Day Trade.
- No raw Polygon/FMP payloads are stored here; only compact row metadata.
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

try:
    from app.sqlite_store import get_json, set_json
except Exception:  # pragma: no cover
    def get_json(key, default=None):
        return default
    def set_json(key, value):
        return False

OPPORTUNITY_RADAR_VERSION = "opportunity_radar_rebuild_v2o_low_float_fast_lane_source_2026_06_20"
NY_TZ = ZoneInfo("America/New_York")
PLAN_MEMORY_KEY = "opportunity_radar:plan_memory_v1"
PLAN_EVENTS_KEY = "opportunity_radar:plan_memory_events_v1"

PERSONAL_PRICE_COMFORT = 50.0
PERSONAL_PRICE_MAX_NORMAL = 150.0
DEFAULT_SECTION_LIMIT = 12
ACTIVE_MEMORY_STATUSES = {"active", "unknown_price", "needs_reclaim_or_trigger", "under_original_entry", "extended_from_original_entry"}


# Learning Overlay V1
# -------------------
# Static, conservative conclusions from two replay learning windows:
# - learning_2026-06-18_5m_14d
# - learning_2026-06-12_5m_14d
# This overlay only explains/labels opportunity candidates. It must not promote a
# symbol to Strong/Cautious or change execution gates.
LEARNING_OVERLAY_VERSION = "learning_overlay_v1_two_windows_2026_06_19"
LEARNING_MIN_SAMPLE_FOR_WEIGHT = 8
LEARNING_PATTERN_LIBRARY: dict[str, dict[str, Any]] = {
    "fib_golden_pullback|premarket|prev_session|early": {
        "label_ar": "نمط تعلّم إيجابي — مبكر من جلسة سابقة",
        "action_ar": "ارفع أولوية المتابعة فقط: هذا النمط تكرر في نافذتين، لكنه ليس Strong تلقائيًا. الأفضل بيع تدريجي وحماية جزء صغير فقط إذا تحول إلى Runner.",
        "risk_ar": "يميل إلى إعطاء فرصة مبكرة جيدة، لكن نسبة كبيرة منه تتحول إلى خطفة بعد القمة.",
        "entry_bias": "positive_watch",
        "exit_bias": "scale_then_trail",
        "sample_count": 44,
        "peak20_pct": 59.1,
        "runner_pct": 11.4,
        "quick_take_profit_pct": 36.4,
        "confidence": "confirmed_two_windows",
        "rule_ar": "Fib Golden + بري ماركت + كان مرشحًا من جلسة سابقة + غير متأخر = أفضل نمط تعلم حاليًا للمتابعة المبكرة، وليس شراء مباشر.",
    },
    "needs_volume|premarket|prev_session|early": {
        "label_ar": "نمط قابل للمتابعة — يحتاج حجم مؤكد",
        "action_ar": "راقبه مبكرًا، لكن لا ترفع الحجم إلا بعد ظهور حجم/دولار فوليوم حقيقي وثبات فوق VWAP أو منطقة القرار.",
        "risk_ar": "العينة متوسطة؛ بعض الحالات رابحة وبعضها خطفة، لذلك لا نرفعه إلى قرار تنفيذ.",
        "entry_bias": "watch_needs_volume",
        "exit_bias": "small_size_fast_manage",
        "sample_count": 8,
        "peak20_pct": 62.5,
        "runner_pct": 12.5,
        "quick_take_profit_pct": 50.0,
        "confidence": "medium_two_windows",
        "rule_ar": "نمط يحتاج volume confirmation؛ لا يكفي وحده للدخول.",
    },
    "fib_golden_pullback|premarket|new_symbol|early": {
        "label_ar": "نمط خطفة محتمل — سهم جديد على الرادار",
        "action_ar": "لا تمنعه؛ اعرضه كفرصة مضاربة بحجم أصغر وخطة بيع سريع، وليس كـ Runner افتراضي.",
        "risk_ar": "يرتفع بقوة أحيانًا، لكنه لم يكن مرشحًا من جلسة سابقة ويحتاج إدارة خروج أسرع.",
        "entry_bias": "speculative_watch",
        "exit_bias": "quick_take_profit",
        "sample_count": 9,
        "peak20_pct": 66.7,
        "runner_pct": 11.1,
        "quick_take_profit_pct": 44.4,
        "confidence": "medium_two_windows",
        "rule_ar": "New symbol + premarket + Fib قد يكون سريعًا؛ لا تخلطه مع فرص الذاكرة السابقة.",
    },
    "vwap_pullback|premarket|prev_session|early": {
        "label_ar": "نمط متذبذب — VWAP Pullback مبكر",
        "action_ar": "اعرضه للمتابعة فقط ولا ترفع وزنه الآن؛ يحتاج تأكيد إضافي مثل دولار فوليوم قوي أو كسر/استعادة واضحة.",
        "risk_ar": "تكرر كثيرًا لكنه أقل ثباتًا من Fib Golden؛ نسبة Runner ضعيفة وسلوك الخطفة حاضر.",
        "entry_bias": "mixed_watch",
        "exit_bias": "active_management",
        "sample_count": 33,
        "peak20_pct": 42.4,
        "runner_pct": 6.1,
        "quick_take_profit_pct": 39.4,
        "confidence": "mixed_two_windows",
        "rule_ar": "لا نرفع وزن VWAP Pullback وحده؛ يحتاج عامل تأكيد آخر.",
    },
    "fib_618_reclaim|premarket|prev_session|early": {
        "label_ar": "Fib 61.8 Reclaim — قابل للمضاربة لا للثقة العالية",
        "action_ar": "يمكن عرضه كفرصة متابعة، لكن بخطة بيع سريع حتى يثبت أنه Runner.",
        "risk_ar": "العينة محدودة وتميل للتلاشي بعد القمة في نافذة من النوافذ.",
        "entry_bias": "cautious_watch",
        "exit_bias": "quick_take_profit",
        "sample_count": 5,
        "peak20_pct": 60.0,
        "runner_pct": 0.0,
        "quick_take_profit_pct": 60.0,
        "confidence": "medium_sample_but_not_runner",
        "rule_ar": "Reclaim عند 61.8 جيد للمراقبة، لكن ليس Runner حتى يثبت احتفاظه بالمكسب.",
    },
    "vwap_pullback|regular|prev_session|early": {
        "label_ar": "نمط ضعيف في السوق الرسمي",
        "action_ar": "لا ترفع وزنه الآن؛ إن ظهر أثناء السوق الرسمي فالأفضل انتظار Pullback/تفعيل أو تحويله لخطة بيع سريع.",
        "risk_ar": "النافذتان أظهرتا ضعفًا/تذبذبًا واضحًا لهذا الشكل مقارنة بالبري ماركت.",
        "entry_bias": "weak_watch",
        "exit_bias": "do_not_upgrade",
        "sample_count": 4,
        "peak20_pct": 0.0,
        "runner_pct": 0.0,
        "quick_take_profit_pct": 50.0,
        "confidence": "weak_two_windows",
        "rule_ar": "VWAP Pullback أثناء السوق الرسمي لا يرفع الأولوية وحده.",
    },
    "fib_golden_pullback|regular|prev_session|early": {
        "label_ar": "Fib أثناء السوق الرسمي — متذبذب",
        "action_ar": "لا ترفعه كتعلم إيجابي عام؛ يحتاج تأكيد نافذة إضافية لأن النتائج اختلفت بين النافذتين.",
        "risk_ar": "كان ضعيفًا في نافذة وقويًا في أخرى بعينة صغيرة؛ لا نغير الوزن بناء عليه.",
        "entry_bias": "mixed_regular",
        "exit_bias": "active_management",
        "sample_count": 8,
        "peak20_pct": 25.0,
        "runner_pct": 0.0,
        "quick_take_profit_pct": 25.0,
        "confidence": "mixed_two_windows",
        "rule_ar": "القوة الحالية في premarket المبكر، لا في regular وحده.",
    },
}

LEARNING_GENERIC_RULES_AR = [
    "طبقة التعلم لا تغيّر Strong/Cautious؛ هي وسم شرح وترتيب فقط.",
    "العينة القليلة لا ترفع الوزن مهما كان الأداء عاليًا.",
    "أفضل نمط مؤكد حاليًا: Fib Golden + بري ماركت + مرشح من جلسة سابقة + غير متأخر.",
    "الأنماط المتأخرة أو very_late تبقى خطفة/بيع سريع ولا تتحول إلى Runner.",
]


def _s(value: Any) -> str:
    return str(value or "").strip()


def _u(value: Any) -> str:
    return _s(value).upper()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").replace("%", "").strip()
        if value == "":
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _round(value: Any, nd: int = 2) -> float:
    try:
        return round(_num(value, 0.0), nd)
    except Exception:
        return 0.0


def _first(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        try:
            val = row.get(key)
            if val is None or val == "":
                continue
            n = _num(val, 0.0)
            if n > 0:
                return n
        except Exception:
            continue
    return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "y", "ok", "on", "نعم", "صحيح"}


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [_s(x) for x in value if _s(x)]
    text = _s(value)
    if not text:
        return []
    if "،" in text:
        return [x.strip() for x in text.split("،") if x.strip()]
    return [text]


def _dedupe(items: list[Any], limit: int = 12) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        text = _s(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _now_text() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d")


def _price(row: dict) -> float:
    return _first(row, ["current_price_live", "display_price", "price", "current_price", "live_price", "fmp_price", "last_price"], 0.0)


def _entry(row: dict) -> float:
    return _first(row, ["display_entry_price", "smart_entry_price", "entry_price_real", "entry_price", "entry", "buy_above", "breakout_price", "confirmation_price"], 0.0)


def _stop(row: dict) -> float:
    return _first(row, ["display_stop_price", "smart_stop_loss", "stop_loss", "stop", "stop_invalidation"], 0.0)


def _target1(row: dict) -> float:
    return _first(row, ["display_target_price", "smart_target_1", "target_1", "target1", "target_price", "target"], 0.0)


def _atr(row: dict, price: float) -> tuple[float, float]:
    atr = _first(row, ["atr_14", "atr", "average_true_range"], 0.0)
    atr_pct = _first(row, ["atr_pct", "atr_percent", "volatility_pct"], 0.0)
    if atr <= 0 and price > 0 and atr_pct > 0:
        atr = price * atr_pct / 100.0
    if atr_pct <= 0 and price > 0 and atr > 0:
        atr_pct = atr / price * 100.0
    if atr <= 0 and price > 0:
        # Conservative proxy used only for display-zone sanity, not execution.
        atr = max(price * 0.015, 0.05)
        atr_pct = max(atr_pct, atr / price * 100.0)
    return atr, atr_pct


def _pct_distance(price: float, ref: float) -> float:
    if price <= 0 or ref <= 0:
        return 999.0
    return ((price - ref) / ref) * 100.0


def _abs_pct_distance(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return 999.0
    return abs((a - b) / b) * 100.0


def _change_pct(row: dict) -> float:
    """Read displayed/live percent change using all known source field names.

    Critical rule: a stock already up strongly today must never be classified as
    Support Bounce just because one source omitted display_change_pct.  We read
    all known UI/live/cache fields, normalize scanner decimal ratios when the key
    implies percent, and finally calculate from previous close/open if possible.
    """
    keys = [
        "display_change_pct", "change_vs_prev_close_pct", "live_change_pct",
        "change_pct", "percent_change", "change_percent", "changePercentage",
        "changesPercentage", "changes_percentage", "changePercent",
        "regularMarketChangePercent", "fmp_change_pct", "today_change_pct",
        "day_change_pct", "session_change_pct", "current_gain",
        "change_from_open_pct", "pm_change_pct", "pre_market_change_pct",
        "premarket_change_pct", "after_hours_change_pct", "gap_from_prev_close_pct",
    ]
    for key in keys:
        if key not in row:
            continue
        val = row.get(key)
        if val is None or val == "":
            continue
        n = _num(val, 999999.0)
        if n == 999999.0:
            continue
        # scanner.py stores some *_pct fields as decimal ratios (0.08 = 8%).
        if key in {"day_change_pct", "session_change_pct", "change_from_open_pct", "gap_from_prev_close_pct"} and -1.0 <= n <= 1.0 and abs(n) >= 0.015:
            n *= 100.0
        return n

    price = _price(row)
    prev = _first(row, ["previous_close", "prev_close", "prior_close", "regularMarketPreviousClose", "close_previous", "last_close"], 0.0)
    if price > 0 and prev > 0:
        return ((price - prev) / prev) * 100.0
    open_px = _first(row, ["open_price", "day_open", "open", "regularMarketOpen"], 0.0)
    if price > 0 and open_px > 0:
        return ((price - open_px) / open_px) * 100.0
    return 0.0




def _move_risk_pct(row: dict) -> float:
    """Best-effort movement risk for small-stock logic.

    Live quotes may show 0% when realtime is unavailable, while the same row can
    already have journal/peak movement evidence.  For speculative small stocks,
    we use this as a chase-risk input only, not as an execution quote.
    """
    vals = [_change_pct(row)]
    for key in [
        "max_gain_basis", "peak_gain_seen", "intraday_peak_gain",
        "gain_at_detection", "source_promotion_v2_peak_gain_seen",
        "rolling_session_peak_gain", "prior_session_peak_gain",
    ]:
        if key in row:
            vals.append(_num(row.get(key), 0.0))
    for parent in ["move_stage_v2", "detection_journal"]:
        block = row.get(parent)
        if isinstance(block, dict):
            for key in ["max_gain_basis", "peak_gain_seen", "gain_at_detection", "current_gain"]:
                vals.append(_num(block.get(key), 0.0))
    return max([v for v in vals if v > 0] or [0.0])


def _is_low_price_stock(price: float) -> bool:
    return bool(1.0 <= price <= 20.0)


def _micro_zone_width_pct(price: float, low: float, high: float) -> float:
    if price <= 0 or low <= 0 or high <= 0 or high <= low:
        return 999.0
    return ((high - low) / price) * 100.0


def _small_stock_micro_zone_ok(price: float, atr_pct: float, low: float, high: float) -> bool:
    """Close S/R is normal for low-priced names; judge it as one micro-zone."""
    if not _is_low_price_stock(price):
        return False
    width_pct = _micro_zone_width_pct(price, low, high)
    allowed = max(0.85, min(4.0, max(atr_pct, 1.0) * 0.55))
    return bool(width_pct <= allowed)


def _catalyst_type_from_row(row: dict) -> tuple[str, str]:
    """Return compact catalyst/news type codes for display only.

    This does not add buy points by itself; it only prevents Catalyst / News Watch
    from showing an unnamed/undated generic catalyst.
    """
    scope = _s(row.get("news_scope")).lower()
    category = _s(row.get("news_category") or row.get("news_sentiment")).lower()
    title = " ".join([
        _s(row.get("news_title")), _s(row.get("news_public_summary")),
        _s(row.get("news_context_note")), _s(row.get("news_badge")),
    ]).lower()
    if scope == "sector":
        return "sector_context", "سياق قطاعي"
    if scope == "market":
        return "market_context", "سياق سوق عام"
    if scope == "opinion":
        return "opinion", "مقال رأي / قائمة ترشيحات"
    if scope == "unrelated":
        return "unrelated", "غير مرتبط مباشرة"
    if category == "legal" or any(k in title for k in ["lawsuit", "sec", "investigation", "legal", "class action", "قضية", "تحقيق"]):
        return "legal_risk", "خبر قانوني / مخاطر"
    if any(k in title for k in ["fda", "clinical", "trial", "phase", "approval", "clearance", "pdufa", "biotech", "دواء", "سريري", "موافقة"]):
        return "biotech_regulatory", "محفز دوائي / تنظيمي"
    if any(k in title for k in ["earnings", "revenue", "eps", "guidance", "results", "quarter", "أرباح", "إيرادات", "توجيهات", "نتائج"]):
        return "earnings", "أرباح / نتائج"
    if any(k in title for k in ["contract", "order", "award", "agreement", "partnership", "deal", "عقد", "طلب", "اتفاق", "شراكة"]):
        return "contract_partnership", "عقد / شراكة"
    if any(k in title for k in ["merger", "acquisition", "buyout", "takeover", "اندماج", "استحواذ"]):
        return "ma", "اندماج / استحواذ"
    if any(k in title for k in ["upgrade", "downgrade", "price target", "initiates", "analyst", "ترقية", "تخفيض", "سعر مستهدف", "محلل"]):
        return "analyst_action", "تغيير محللين / سعر مستهدف"
    if any(k in title for k in ["offering", "registered direct", "atm", "warrant", "financing", "طرح", "تمويل"]):
        return "financing", "تمويل / طرح"
    if category == "positive":
        return "company_positive", "خبر شركة إيجابي"
    if category == "negative":
        return "company_negative", "خبر شركة سلبي"
    if category == "mixed":
        return "company_mixed", "خبر شركة مختلط"
    if scope == "company":
        return "company_news", "خبر شركة مباشر"
    return "no_clear_catalyst", "لا يوجد محفز واضح"


def _build_catalyst_details(row: dict) -> dict[str, Any]:
    code, label = _catalyst_type_from_row(row or {})
    published_ksa = _s(row.get("news_published_ksa"))
    published_utc = _s(row.get("news_published_utc"))
    age = _s(row.get("news_age_label")) or _s(row.get("news_freshness_label"))
    source = _s(row.get("news_source_name"))
    title = _s(row.get("news_title")) or _s(row.get("news_public_summary")) or _s(row.get("news_note"))
    scope = _s(row.get("news_scope")) or "neutral"
    category = _s(row.get("news_category")) or _s(row.get("news_sentiment")) or "neutral"
    is_catalyst = bool(row.get("news_is_catalyst"))
    date_text = published_ksa or published_utc or age or "تاريخ الخبر غير متوفر"
    time_parts = []
    if age:
        time_parts.append(age)
    if published_ksa:
        time_parts.append(published_ksa)
    elif published_utc:
        time_parts.append(published_utc)
    if source:
        time_parts.append("المصدر: " + source)
    time_line = " | ".join(time_parts) if time_parts else "تاريخ الخبر غير متوفر"
    context_only = bool(row.get("news_context_only") or scope in {"sector", "market", "opinion", "unrelated", "neutral"})
    actionability = "محفز مباشر" if is_catalyst else ("سياق فقط" if context_only else "خبر للمتابعة")
    return {
        "type_code": code,
        "type_ar": label,
        "date_ar": date_text,
        "time_line_ar": time_line,
        "title": title,
        "source": source,
        "age_label": age,
        "published_ksa": published_ksa,
        "published_utc": published_utc,
        "scope": scope,
        "category": category,
        "is_catalyst": is_catalyst,
        "context_only": context_only,
        "actionability_ar": actionability,
        "summary_ar": f"{label} — {date_text}" + (f" — {title[:140]}" if title else ""),
        "rule_ar": "الأخبار في هذا القسم سياق مساعد وليست شراء مباشر؛ نعرض نوع المحفز وتاريخه حتى لا تظهر بطاقة Catalyst مبهمة.",
        "has_news": bool(title or _s(row.get("news_badge")) or published_ksa or published_utc),
    }


def _catalyst_reasons(details: dict) -> list[str]:
    if not isinstance(details, dict) or not details.get("has_news"):
        return []
    out = [f"نوع المحفز/الخبر: {details.get('type_ar')}"]
    out.append(f"تاريخ/حداثة الخبر: {details.get('date_ar')}")
    if details.get("actionability_ar"):
        out.append(f"قابلية الاعتماد: {details.get('actionability_ar')}")
    return _dedupe(out, 4)



def _learning_phase_for_row(row: dict, market_phase: str = "") -> str:
    raw = _s(row.get("phase_at_detection") or row.get("session") or row.get("market_phase") or market_phase).lower()
    raw = raw.replace("-", "_").replace(" ", "_")
    if raw in {"pre_market", "premarket", "قبل_الافتتاح"}:
        return "premarket"
    if raw in {"after_hours", "afterhours", "postmarket", "post_market", "بعد_الإغلاق"}:
        return "after_hours"
    if raw in {"open", "opening", "market_open", "لحظة_الافتتاح"}:
        return "open"
    if raw in {"overnight", "overnight_watch"}:
        return "overnight"
    return "regular" if raw else "regular"


def _learning_prior_session_state(row: dict) -> str:
    prior_count = _num(row.get("prior_candidate_count"), 0.0)
    prev_dates = row.get("previous_candidate_dates")
    has_prev_dates = isinstance(prev_dates, list) and len(prev_dates) > 0
    if _bool(row.get("candidate_from_previous_trading_session")) or _bool(row.get("detected_previous_session")) or prior_count > 0 or has_prev_dates:
        return "prev_session"
    return "new_symbol"


def _learning_chase_state(row: dict, flags: dict | None = None) -> str:
    flags = flags if isinstance(flags, dict) else {}
    raw = _s(row.get("chase_risk_at_detection") or row.get("source_chase_risk") or "").lower()
    if raw in {"early", "watch_carefully", "late", "very_late"}:
        return raw
    change = abs(_change_pct(row))
    move_risk = _move_risk_pct(row)
    max_before = _num(row.get("max_gain_before_detection_pct"), 0.0)
    if flags.get("classic_small_chase_risk") or flags.get("extended_after_move") or max_before >= 15 or move_risk >= 15 or change >= 18:
        return "very_late" if max(max_before, move_risk, change) >= 20 else "late"
    if change >= 5.0 or move_risk >= 7.0:
        return "watch_carefully"
    return "early"


def _learning_setup_state(row: dict, flags: dict | None = None) -> str:
    flags = flags if isinstance(flags, dict) else {}
    classic = flags.get("classic_small_stock") if isinstance(flags.get("classic_small_stock"), dict) else {}
    setup = _s(classic.get("setup_state") or row.get("classic_state") or row.get("small_stock_classic_state"))
    if setup:
        return setup
    bucket = _s(row.get("opportunity_bucket"))
    if bucket == "pre_trigger":
        return "pre_trigger"
    if bucket == "reclaim":
        return "vwap_reclaim_hold" if row.get("vwap") else "reclaim"
    if bucket == "support_bounce":
        return "support_bounce"
    if bucket == "high_risk_day_trade":
        return "chase_risk_wait_pullback"
    if bucket == "catalyst_watch":
        return "catalyst_watch"
    return "unknown_setup"


def _learning_pattern_key_for_row(row: dict, flags: dict | None = None, market_phase: str = "") -> str:
    return "|".join([
        _learning_setup_state(row, flags),
        _learning_phase_for_row(row, market_phase),
        _learning_prior_session_state(row),
        _learning_chase_state(row, flags),
    ])


def _learning_overlay_for_row(row: dict, flags: dict | None = None, market_phase: str = "") -> dict[str, Any]:
    key = _learning_pattern_key_for_row(row, flags, market_phase)
    rule = LEARNING_PATTERN_LIBRARY.get(key)
    chase_state = key.split("|")[-1] if "|" in key else _learning_chase_state(row, flags)
    if rule:
        priority_boost = 0.0
        # Explanation-only ranking assist for watch panels. Do not touch decisions.
        if rule.get("entry_bias") == "positive_watch":
            priority_boost = 7.5
        elif rule.get("entry_bias") in {"watch_needs_volume", "speculative_watch"}:
            priority_boost = 3.0
        elif rule.get("entry_bias") in {"weak_watch", "mixed_regular"}:
            priority_boost = -3.0
        return {
            "ok": True,
            "version": LEARNING_OVERLAY_VERSION,
            "pattern_key": key,
            "matched": True,
            "label_ar": rule.get("label_ar"),
            "action_ar": rule.get("action_ar"),
            "risk_ar": rule.get("risk_ar"),
            "rule_ar": rule.get("rule_ar"),
            "confidence": rule.get("confidence"),
            "sample_count": rule.get("sample_count"),
            "peak20_pct": rule.get("peak20_pct"),
            "runner_pct": rule.get("runner_pct"),
            "quick_take_profit_pct": rule.get("quick_take_profit_pct"),
            "entry_bias": rule.get("entry_bias"),
            "exit_bias": rule.get("exit_bias"),
            "priority_boost": priority_boost,
            "applies_to_execution": False,
        }
    if chase_state in {"late", "very_late"}:
        return {
            "ok": True,
            "version": LEARNING_OVERLAY_VERSION,
            "pattern_key": key,
            "matched": False,
            "label_ar": "تعلم: التقاط متأخر — تعامل كخطفة فقط",
            "action_ar": "لا ترفع الوزن ولا تعتبره Runner؛ إن ظهر ربح فالأولوية لجني سريع أو انتظار Pullback.",
            "risk_ar": "النافذتان أظهرتا أن late/very_late غالبًا تحتاج خروجًا سريعًا لا مطاردة.",
            "confidence": "generic_late_rule",
            "priority_boost": -5.0,
            "entry_bias": "late_guard",
            "exit_bias": "quick_take_profit",
            "applies_to_execution": False,
        }
    return {
        "ok": True,
        "version": LEARNING_OVERLAY_VERSION,
        "pattern_key": key,
        "matched": False,
        "label_ar": "تعلم: لا توجد عينة مؤكدة بعد",
        "action_ar": "اعرضه كمراقبة عادية؛ لا ترفع الوزن حتى تتكرر العينة في نافذة لاحقة.",
        "risk_ar": "لا يوجد نمط مؤكد من نافذتي التعلم لهذه التركيبة.",
        "confidence": "unconfirmed",
        "priority_boost": 0.0,
        "entry_bias": "neutral_watch",
        "exit_bias": "normal_management",
        "applies_to_execution": False,
    }


def _learning_overlay_summary() -> dict[str, Any]:
    return {
        "ok": True,
        "version": LEARNING_OVERLAY_VERSION,
        "mode_ar": "وسم تعلّم فقط — لا يغيّر Strong/Cautious ولا يفعّل شراء مباشر",
        "best_confirmed_pattern_key": "fib_golden_pullback|premarket|prev_session|early",
        "best_confirmed_pattern_ar": LEARNING_PATTERN_LIBRARY["fib_golden_pullback|premarket|prev_session|early"].get("label_ar"),
        "best_confirmed_rule_ar": LEARNING_PATTERN_LIBRARY["fib_golden_pullback|premarket|prev_session|early"].get("rule_ar"),
        "stable_patterns_count": len(LEARNING_PATTERN_LIBRARY),
        "min_sample_for_weight": LEARNING_MIN_SAMPLE_FOR_WEIGHT,
        "rules_ar": LEARNING_GENERIC_RULES_AR,
        "pattern_library_sample": [
            {"pattern_key": k, "label_ar": v.get("label_ar"), "sample_count": v.get("sample_count"), "confidence": v.get("confidence"), "peak20_pct": v.get("peak20_pct"), "runner_pct": v.get("runner_pct"), "quick_take_profit_pct": v.get("quick_take_profit_pct")}
            for k, v in list(LEARNING_PATTERN_LIBRARY.items())[:7]
        ],
    }



def _learning_overlay_candidate_row(row: dict) -> dict[str, Any]:
    lov = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else {}
    sym = _u(row.get("symbol"))
    return {
        "symbol": sym,
        "price": _round(_price(row), 4),
        "decision": _s(row.get("decision")),
        "opportunity_bucket": _s(row.get("opportunity_bucket")),
        "stage_label": _s(row.get("opportunity_stage_label")),
        "learning_label_ar": _s(lov.get("label_ar")),
        "learning_action_ar": _s(lov.get("action_ar")),
        "learning_risk_ar": _s(lov.get("risk_ar")),
        "learning_pattern_key": _s(lov.get("pattern_key")),
        "learning_confidence": _s(lov.get("confidence")),
        "learning_entry_bias": _s(lov.get("entry_bias")),
        "learning_exit_bias": _s(lov.get("exit_bias")),
        "learning_matched": bool(lov.get("matched")),
        "opportunity_rank_score": _round(row.get("opportunity_rank_score"), 2),
        "why_ar": _s(row.get("why_appeared_ar") or row.get("quick_explainer") or row.get("special_bucket_reason")),
    }


def _build_visible_learning_overlay_candidates(rows: list[dict], limit: int = 16) -> dict[str, Any]:
    """Build a visible learning panel from all enriched rows, not only Opportunity buckets.

    This fixes the UI case where today's candidates are mainly Early Movement / Watch,
    while the learning overlay exists only in row metadata. The panel remains
    educational and never promotes execution decisions.
    """
    positive: list[dict] = []
    quick: list[dict] = []
    weak: list[dict] = []
    sample: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict) or _is_blocked(row):
            continue
        if not _is_personal_section_eligible(row):
            continue
        lov = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else None
        if not isinstance(lov, dict):
            continue
        sym = _u(row.get("symbol"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        item = _learning_overlay_candidate_row(row)
        bias = _s(lov.get("entry_bias"))
        exit_bias = _s(lov.get("exit_bias"))
        confidence = _s(lov.get("confidence"))
        matched = bool(lov.get("matched"))
        if matched and bias in {"positive_watch", "watch_needs_volume"}:
            positive.append(item)
        elif exit_bias == "quick_take_profit" or bias in {"speculative_watch", "late_guard"}:
            quick.append(item)
        elif confidence in {"weak_two_windows", "mixed_two_windows"} or bias in {"weak_watch", "mixed_regular", "mixed_watch"}:
            weak.append(item)
        elif matched:
            sample.append(item)
    def sort_items(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda x: _num(x.get("opportunity_rank_score"), 0.0), reverse=True)[:max(1, int(limit or 16))]
    positive = sort_items(positive)
    quick = sort_items(quick)
    weak = sort_items(weak)
    sample = sort_items(sample)
    return {
        "ok": True,
        "version": LEARNING_OVERLAY_VERSION,
        "mode_ar": "ظاهر دائمًا — وسم تعلّم فقط لا يغيّر Strong/Cautious",
        "visible_note_ar": "إذا لم تظهر فرص في أقسام Opportunity، تعرض هذه اللوحة إشارات التعلم من Early Movement / Watch أيضًا حتى لا تختفي طبقة التعلم.",
        "positive_count": len(positive),
        "quick_take_profit_count": len(quick),
        "weak_or_mixed_count": len(weak),
        "sample_only_count": len(sample),
        "positive_watch": positive,
        "quick_take_profit_watch": quick,
        "weak_or_mixed_watch": weak,
        "sample_only_watch": sample,
    }


def _row_is_early_or_watch_context(row: dict) -> bool:
    """Return True for rows that are visible in the current tool as Early Movement / Watch.

    These rows often carry useful prior-session / setup data but do not pass the
    stricter Opportunity buckets yet. V2k2 exposes them as *prepared learning
    candidates* so the user can see the data without falsely promoting them.
    """
    if not isinstance(row, dict):
        return False
    bucket = _s(row.get("opportunity_bucket"))
    stage = _s(row.get("opportunity_stage")).lower()
    decision = _s(row.get("decision"))
    labels = " ".join([
        _s(row.get("opportunity_stage_label")),
        _s(row.get("display_plan_family_label")),
        _s(row.get("signal_bucket")),
        _s(row.get("source_group")),
        _s(row.get("plan_family")),
        _s(row.get("quick_explainer")),
    ]).lower()
    if bucket in {"watch", "watchlist"} or stage in {"watch", "early_watch", "pre_move"}:
        return True
    if decision in {"مراقبة", "انتظار", "Watch", "WATCH"}:
        return True
    return any(token in labels for token in [
        "early", "pre-move", "pre move", "quiet", "accumulation", "مراقبة", "الحركة المبكرة", "تجميع", "تحضير"
    ])


def _learning_bridge_score(row: dict) -> tuple[float, list[str], str]:
    """Score watch/early rows for a visible learning-prep section.

    This is not an execution score. It exists to avoid the current confusing UI
    state where all actionable sections are empty while the data is sitting in
    Watch / Early Movement.
    """
    if not isinstance(row, dict):
        return 0.0, [], "neutral"
    reasons: list[str] = []
    lov = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else {}
    bias = _s(lov.get("entry_bias"))
    exit_bias = _s(lov.get("exit_bias"))
    confidence = _s(lov.get("confidence"))
    matched = bool(lov.get("matched"))
    score = 0.0
    mode = "neutral"

    if matched and bias == "positive_watch":
        score += 55; mode = "positive"; reasons.append("مطابقة نمط تعلم إيجابي مؤكد من نافذتين")
    elif matched and bias in {"watch_needs_volume", "speculative_watch"}:
        score += 36; mode = "speculative"; reasons.append("مطابقة نمط تعلم قابل للمتابعة لكن يحتاج تأكيد")
    elif exit_bias == "quick_take_profit" or bias == "late_guard":
        score += 24; mode = "quick"; reasons.append("تعلم: إن تحرك فهو أقرب إلى خطفة/بيع سريع")
    elif confidence in {"mixed_two_windows", "weak_two_windows"}:
        score += 12; mode = "mixed"; reasons.append("تعلم: نمط متذبذب لا نرفع وزنه")

    # Fallback bridge: surface credible watch/early candidates even when exact
    # replay pattern fields are missing from live rows.
    if _row_is_early_or_watch_context(row):
        score += 18; reasons.append("موجود حاليًا في المراقبة/الحركة المبكرة وليس في قسم فرصة متخصص")
    change = abs(_change_pct(row))
    price = _price(row)
    move_risk = _move_risk_pct(row)
    prior_count = _num(row.get("prior_candidate_count"), 0.0)
    prev_dates = row.get("previous_candidate_dates")
    has_prior = _bool(row.get("candidate_from_previous_trading_session")) or _bool(row.get("detected_previous_session")) or prior_count > 0 or (isinstance(prev_dates, list) and len(prev_dates) > 0)
    if has_prior:
        score += 12; reasons.append("له أثر سابق/جلسة سابقة في الذاكرة")
    if 0 < price <= PERSONAL_PRICE_MAX_NORMAL:
        score += 6; reasons.append("سعره ضمن النطاق الشخصي المقبول")
    if change <= 5.0 and move_risk < 8.0:
        score += 8; reasons.append("لم يتحول إلى مطاردة بعد")
    elif change <= 8.0 and move_risk < 12.0:
        score += 4; reasons.append("ما زال قريبًا من نطاق متابعة لا شراء مباشر")
    else:
        score -= 8; reasons.append("فيه تمدد/حركة سابقة؛ يحتاج بيع سريع أو Pullback")

    if mode == "neutral" and score >= 30:
        mode = "prepared"
    return round(max(0.0, score), 2), _dedupe(reasons, 6), mode


def _learning_bridge_label(mode: str) -> tuple[str, str]:
    if mode == "positive":
        return "🧠 مرشح تعلم إيجابي", "فرصة متابعة مبكرة من طبقة التعلم؛ ليست Strong ولا Cautious حتى تكتمل شروط التنفيذ."
    if mode == "speculative":
        return "🧠 مرشح تعلم يحتاج تأكيد", "راقبه بحجم صغير فقط بعد تأكيد حجم/VWAP/منطقة قرار."
    if mode == "quick":
        return "🧠 خطفة محتملة — بيع سريع", "لا تتعامل معه كـ Runner؛ إن تحرك فالخروج الجزئي السريع أهم."
    if mode == "mixed":
        return "🧠 نمط متذبذب — لا ترفع الوزن", "يظهر للمتابعة فقط لأن العينة متذبذبة."
    return "🧠 مرشح تحضير من المراقبة", "البيانات موجودة في Watch/Early Movement، لكنها لم تصل بعد إلى فرصة متخصصة."


def _make_learning_bridge_row(row: dict, mode: str, score: float, reasons: list[str]) -> dict:
    out = dict(row or {})
    label, action = _learning_bridge_label(mode)
    lov = out.get("learning_overlay_v1") if isinstance(out.get("learning_overlay_v1"), dict) else {}
    if not lov:
        lov = _learning_overlay_for_row(out, out.get("opportunity_flow_flags") if isinstance(out.get("opportunity_flow_flags"), dict) else {}, _s(out.get("market_phase")))
    out["learning_overlay_v1"] = lov
    out["learning_bridge_v2k2"] = {
        "ok": True,
        "version": OPPORTUNITY_RADAR_VERSION,
        "mode": mode,
        "score": score,
        "reasons_ar": reasons,
        "label_ar": label,
        "action_ar": action,
        "applies_to_execution": False,
    }
    out["learning_overlay_label_ar"] = _s(lov.get("label_ar") or label)
    out["learning_overlay_action_ar"] = _s(lov.get("action_ar") or action)
    out["opportunity_bucket"] = "learning_opportunity"
    out["opportunity_stage"] = "learning_opportunity"
    out["opportunity_stage_label"] = label
    out["display_plan_family_label"] = label
    out["opportunity_rank_score"] = round(max(_num(out.get("opportunity_rank_score"), 0.0), score), 2)
    base_why = _s(out.get("why_appeared_ar") or out.get("quick_explainer") or out.get("special_bucket_reason"))
    out["why_appeared_ar"] = "، ".join(_dedupe(reasons + ([base_why] if base_why else []), 5))
    out["special_bucket_reason"] = out["why_appeared_ar"]
    return out


def _build_learning_opportunity_bridge(rows: list[dict], excluded_symbols: set[str] | None = None, limit: int = DEFAULT_SECTION_LIMIT) -> tuple[list[dict], dict[str, Any]]:
    excluded_symbols = excluded_symbols or set()
    candidates: list[dict] = []
    debug = {
        "rows_seen": 0,
        "watch_or_early_rows_seen": 0,
        "exact_learning_matches_seen": 0,
        "candidate_rows_before_limit": 0,
        "excluded_existing_specific_symbols": len(excluded_symbols),
        "fallback_used": False,
        "rule_ar": "V2k2 يعرض مرشحي التعلم من Watch/Early Movement عندما تكون أقسام الفرص المتخصصة فارغة أو قليلة، بدون ترقية تنفيذية.",
    }
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict) or _is_blocked(row) or not _is_personal_section_eligible(row):
            continue
        debug["rows_seen"] += 1
        sym = _u(row.get("symbol"))
        if not sym or sym in seen or sym in excluded_symbols:
            continue
        is_watch = _row_is_early_or_watch_context(row)
        if is_watch:
            debug["watch_or_early_rows_seen"] += 1
        lov = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else {}
        if bool(lov.get("matched")):
            debug["exact_learning_matches_seen"] += 1
        score, reasons, mode = _learning_bridge_score(row)
        # Show exact matches, credible watch/early rows, and a small fallback list
        # so the section is not blank when the data exists but strict buckets are empty.
        if score >= 22 or bool(lov.get("matched")):
            seen.add(sym)
            candidates.append(_make_learning_bridge_row(row, mode, score, reasons))
    if not candidates:
        # Last-resort transparent fallback: top watch/early rows only, marked as neutral.
        debug["fallback_used"] = True
        for row in rows or []:
            if not isinstance(row, dict) or _is_blocked(row) or not _is_personal_section_eligible(row):
                continue
            sym = _u(row.get("symbol"))
            if not sym or sym in seen or sym in excluded_symbols:
                continue
            if not _row_is_early_or_watch_context(row):
                continue
            score = max(10.0, _num(row.get("display_rank_score"), 0.0) * 0.25)
            reasons = ["بياناته موجودة في المراقبة/الحركة المبكرة لكن لم يكتمل سبب فرصة متخصص", "يعرض هنا حتى لا تختفي طبقة التعلم عندما تكون أقسام التنفيذ فارغة"]
            seen.add(sym)
            candidates.append(_make_learning_bridge_row(row, "neutral", score, reasons))
            if len(candidates) >= max(1, int(limit or DEFAULT_SECTION_LIMIT)):
                break
    candidates = _sort_bucket(candidates)[:max(1, int(limit or DEFAULT_SECTION_LIMIT))]
    debug["candidate_rows_before_limit"] = len(candidates)
    debug["candidate_symbols"] = [_u(x.get("symbol")) for x in candidates[:12] if _u(x.get("symbol"))]
    return candidates, debug

def _next_week_action_for_row(row: dict) -> str:
    bucket = _s(row.get("opportunity_bucket"))
    flags = row.get("opportunity_flow_flags") if isinstance(row.get("opportunity_flow_flags"), dict) else {}
    trigger = _num(flags.get("trigger_price") if isinstance(flags, dict) else 0.0, 0.0)
    cdet = row.get("catalyst_details") if isinstance(row.get("catalyst_details"), dict) else {}
    if bucket == "learning_opportunity":
        bridge = row.get("learning_bridge_v2k2") if isinstance(row.get("learning_bridge_v2k2"), dict) else {}
        return _s(bridge.get("action_ar")) or "مرشح تحضير من طبقة التعلم/المراقبة؛ ليس شراء مباشر حتى تكتمل شروط التنفيذ."
    if bucket == "small_stock_classic":
        return "راقبه للأسبوع القادم كمرشح أسهم صغيرة: انتظار إغلاق 5د/15د فوق Fib/VWAP/قمة أمس، وليس شراء مباشر من القائمة."
    if bucket == "pre_trigger":
        return f"قريب من التفعيل؛ راقب إغلاقًا فوق {round(trigger, 2) if trigger else 'حد التفعيل'} مع حجم واضح."
    if bucket == "support_bounce":
        return "مرشح ارتداد: صالح للمراقبة قرب الدعم فقط؛ إذا ابتعد سريعًا يتحول إلى مضاربة/استمرار ولا يُطارد."
    if bucket == "reclaim":
        return "مرشح Reclaim: راقب ثبات السعر فوق المستوى المستعاد مع عدم كسر الدعم مرة أخرى."
    if bucket == "continuation_pullback":
        return "استمرار مشروط: الأفضل انتظار Pullback صحي أو إعادة اختبار VWAP/دعم قبل الدخول."
    if bucket == "low_float_premarket":
        return "مرشح Low-Float/Pre-Market: يظهر مبكرًا للأسبوع القادم لكن حجم الصفقة يجب أن يكون صغيرًا جدًا."
    if bucket == "high_risk_day_trade":
        return "مضاربة عالية المخاطرة: إن ظهرت فرصة فهي سريعة؛ جني ربح سريع ولا تعاملها كـ Runner إلا بعد ثبات واضح."
    if bucket == "gap_fill_watch":
        return "Gap Watch: راقب دخول السعر داخل الفجوة أو احترام حدها؛ لا تفترض أن كل فجوة ستغلق."
    if bucket == "catalyst_watch":
        extra = f" ({cdet.get('type_ar')} — {cdet.get('date_ar')})" if cdet else ""
        return "Catalyst Watch" + extra + ": الخبر سياق مساعد فقط؛ القرار من السعر والسيولة بعد الخبر."
    return "مراقبة فقط حتى تظهر مرحلة أوضح."


def _build_next_week_analysis(final_map: dict[str, list[dict]], counts: dict | None = None) -> dict[str, Any]:
    labels = {
        "promotion_bridge_candidates": "جسر الترقية قبل الافتتاح",
        "learning_opportunity_candidates": "مرشحو طبقة التعلم / تحضير",
        "small_stock_classic_radar": "أسهم صغيرة كلاسيكية",
        "pre_trigger_candidates": "قريبة من التفعيل",
        "support_bounce_candidates": "ارتداد من دعم",
        "reclaim_candidates": "Reclaim / استعادة مستوى",
        "continuation_pullback_candidates": "Continuation Pullback",
        "low_float_premarket_radar": "Low-Float / بري ماركت",
        "high_risk_day_trades": "مضاربة عالية المخاطرة",
        "gap_fill_watch": "Gap Fill Watch",
        "catalyst_watch": "Catalyst / News Watch",
    }
    priority = [
        "promotion_bridge_candidates",
        "learning_opportunity_candidates",
        "small_stock_classic_radar", "pre_trigger_candidates", "support_bounce_candidates", "reclaim_candidates",
        "continuation_pullback_candidates", "low_float_premarket_radar", "catalyst_watch",
        "gap_fill_watch", "high_risk_day_trades",
    ]
    groups = []
    top = []
    for key in priority:
        rows = final_map.get(key, []) or []
        if rows:
            groups.append({"key": key, "label_ar": labels.get(key, key), "count": len(rows), "symbols_sample": [_u(r.get("symbol")) for r in rows[:6] if _u(r.get("symbol"))]})
        for r in rows[:4]:
            sym = _u(r.get("symbol"))
            if not sym:
                continue
            item = {
                "symbol": sym,
                "group_key": key,
                "group_ar": labels.get(key, key),
                "price": _round(_price(r), 4),
                "stage_label": _s(r.get("opportunity_stage_label")),
                "why_ar": _s(r.get("why_appeared_ar") or r.get("special_bucket_reason")),
                "next_week_action_ar": _next_week_action_for_row(r),
                "opportunity_rank_score": _round(r.get("opportunity_rank_score"), 2),
            }
            cdet = r.get("catalyst_details") if isinstance(r.get("catalyst_details"), dict) else {}
            lov = r.get("learning_overlay_v1") if isinstance(r.get("learning_overlay_v1"), dict) else {}
            if lov:
                item["learning_overlay_label_ar"] = lov.get("label_ar")
                item["learning_overlay_action_ar"] = lov.get("action_ar")
                item["learning_pattern_key"] = lov.get("pattern_key")
                item["learning_exit_bias"] = lov.get("exit_bias")
            if key == "catalyst_watch" and cdet:
                item["catalyst_type_ar"] = cdet.get("type_ar")
                item["catalyst_date_ar"] = cdet.get("date_ar")
                item["catalyst_summary_ar"] = cdet.get("summary_ar")
            top.append(item)
    return {
        "ok": True,
        "version": OPPORTUNITY_RADAR_VERSION,
        "label_ar": "تحليل الأسبوع القادم",
        "generated_at": _now_text(),
        "mode_ar": "تحضير ومراقبة فقط — ليس شراء مباشر",
        "summary_ar": "هذه اللوحة تجمع المرشحين الذين يستحقون المتابعة للأسبوع القادم حسب مراحل Opportunity Radar، مع بقاء Strong/Cautious منفصلين كقرارات تنفيذ.",
        "learning_overlay_summary": _learning_overlay_summary(),
        "groups": groups,
        "top_candidates": top[:24],
        "rules_ar": [
            "لا تدخل من Watch وحده؛ انتظر تحول السهم إلى Cautious/Strong أو إغلاق تأكيد واضح.",
            "مرشحو الأسهم الصغيرة وLow-Float يظهرون مبكرًا، لكن حجم الصفقة صغير والخروج أسرع.",
            "Catalyst/News Watch يعرض نوع وتاريخ المحفز، لكن الخبر وحده لا يضيف قرار شراء مباشر.",
        ],
        "learning_archive_v1_note_ar": "Learning Overlay V1 يستخدم نتائج نافذتين كوسم شرح وترتيب فقط، بدون تغيير Strong/Cautious وبدون raw على Railway.",
    }

def _level_merge_threshold(price: float, atr: float) -> float:
    if price <= 0:
        return 0.05
    # Low-priced stocks naturally trade with support/resistance close together.
    # Merge them as a tradable micro-zone, but do not pretend every cent is a
    # separate decision level.
    if price <= 5:
        tick_component = 0.015
        pct_component = price * 0.009
        atr_component = atr * 0.32 if atr > 0 else 0.0
    elif price <= 20:
        tick_component = 0.025
        pct_component = price * 0.0075
        atr_component = atr * 0.30 if atr > 0 else 0.0
    else:
        tick_component = 0.05
        pct_component = price * 0.006
        atr_component = atr * 0.28 if atr > 0 else 0.0
    return max(tick_component, pct_component, atr_component)


def _zone_width(price: float, atr: float, strength: str = "") -> float:
    if price <= 0:
        return 0.02
    base = max(price * 0.0045, atr * 0.18 if atr > 0 else 0.0, 0.03 if price >= 10 else 0.015)
    text = strength.lower()
    if "strong" in text or "قوي" in strength:
        base *= 1.15
    elif "weak" in text or "ضعيف" in strength:
        base *= 0.85
    return min(max(base, 0.01), max(price * 0.035, 0.05))


def _zone_around(level: float, price: float, atr: float, label: str, strength: str = "") -> dict:
    width = _zone_width(price, atr, strength)
    return {
        "label": label,
        "low": _round(max(0.01, level - width), 2),
        "high": _round(level + width, 2),
        "center": _round(level, 2),
        "width": _round(width * 2.0, 2),
        "strength": strength or "متوسطة",
    }


def _collect_raw_levels(row: dict) -> list[dict]:
    levels: list[dict] = []
    candidates = [
        ("nearest_support", "support", "دعم قريب", row.get("nearest_support_strength", "")),
        ("display_support_price", "support", "دعم معروض", ""),
        ("support_price", "support", "دعم", ""),
        ("support", "support", "دعم", ""),
        ("broken_support_level", "broken_support", "دعم مكسور", "مكسور"),
        ("reclaimed_support_level", "reclaim", "دعم مستعاد", "مستعاد"),
        ("pullback_zone_low", "support", "بداية منطقة ارتداد", ""),
        ("pullback_zone_high", "support", "نهاية منطقة ارتداد", ""),
        ("fib_38", "support", "Fib 38", ""),
        ("fib_50", "support", "Fib 50", ""),
        ("fib_62", "support", "Fib 62", ""),
        ("nearest_resistance", "resistance", "مقاومة قريبة", row.get("nearest_resistance_strength", "")),
        ("display_resistance_price", "resistance", "مقاومة معروضة", ""),
        ("resistance_price", "resistance", "مقاومة", ""),
        ("resistance", "resistance", "مقاومة", ""),
        ("breakout_price", "trigger", "مستوى اختراق", ""),
        ("confirmation_price", "trigger", "مستوى تأكيد", ""),
        ("major_resistance", "major_resistance", "مقاومة مهمة", row.get("major_resistance_label", "")),
        ("target_1", "target", "هدف أول", ""),
        ("display_target_price", "target", "هدف معروض", ""),
    ]
    seen: set[tuple[str, float]] = set()
    for key, typ, label, strength in candidates:
        n = _num(row.get(key), 0.0)
        if n <= 0:
            continue
        ident = (typ, round(n, 3))
        if ident in seen:
            continue
        seen.add(ident)
        levels.append({"price": n, "type": typ, "label": label, "strength": _s(strength)})
    return levels


def build_support_resistance_zones(row: dict) -> dict:
    row = row or {}
    price = _price(row)
    atr, atr_pct = _atr(row, price)
    raw = _collect_raw_levels(row)
    threshold = _level_merge_threshold(price, atr)
    notes: list[str] = []

    # Keep only sane levels near enough to matter for current decision, except major target/resistance.
    sane = []
    for lvl in raw:
        p = _num(lvl.get("price"), 0.0)
        if p <= 0:
            continue
        dist = _abs_pct_distance(price, p) if price > 0 else 0.0
        if dist <= 18.0 or lvl.get("type") in {"major_resistance", "target"}:
            sane.append(lvl)
    raw = sorted(sane, key=lambda x: _num(x.get("price"), 0.0))

    clusters: list[list[dict]] = []
    for lvl in raw:
        p = _num(lvl.get("price"), 0.0)
        placed = False
        for cluster in clusters:
            centers = [_num(x.get("price"), 0.0) for x in cluster]
            c = sum(centers) / max(1, len(centers))
            if abs(p - c) <= threshold:
                cluster.append(lvl)
                placed = True
                break
        if not placed:
            clusters.append([lvl])

    zones: list[dict] = []
    for cluster in clusters:
        prices = [_num(x.get("price"), 0.0) for x in cluster if _num(x.get("price"), 0.0) > 0]
        if not prices:
            continue
        low, high = min(prices), max(prices)
        center = sum(prices) / len(prices)
        types = {_s(x.get("type")) for x in cluster}
        labels = _dedupe([x.get("label") for x in cluster], 4)
        strengths = _dedupe([x.get("strength") for x in cluster if _s(x.get("strength"))], 4)
        if {"support", "resistance", "trigger"} & types and price > 0 and low <= price <= high:
            kind = "congestion"
            label = "منطقة ازدحام / قرار"
        elif "reclaim" in types:
            kind = "reclaim"
            label = "مستوى مستعاد"
        elif "broken_support" in types:
            kind = "broken_support"
            label = "دعم مكسور يحتاج استعادة"
        elif "major_resistance" in types:
            kind = "major_resistance"
            label = "مقاومة مهمة"
        elif "target" in types and not ({"support", "resistance", "trigger"} & types):
            kind = "target"
            label = "هدف / مقاومة بعيدة"
        elif any(t in types for t in ["resistance", "trigger"]):
            kind = "resistance"
            label = "منطقة مقاومة / تفعيل"
        else:
            kind = "support"
            label = "منطقة دعم"
        width = max(_zone_width(price, atr, " ".join(strengths)), (high - low) / 2.0)
        zone_low = max(0.01, low - width)
        zone_high = high + width
        dist_pct = _pct_distance(price, center) if price > 0 else 999.0
        touch_count_proxy = len(cluster)
        strength_label = "قوية" if touch_count_proxy >= 3 or any("قوي" in s for s in strengths) else "ضعيفة" if any("ضعيف" in s for s in strengths) else "متوسطة"
        zones.append({
            "kind": kind,
            "label": label,
            "low": _round(zone_low, 2),
            "high": _round(zone_high, 2),
            "center": _round(center, 2),
            "distance_pct": _round(dist_pct, 2) if dist_pct != 999.0 else 999.0,
            "strength": strength_label,
            "raw_level_count": len(cluster),
            "merged_labels": labels,
        })
        if len(cluster) >= 2:
            notes.append(f"تم دمج {len(cluster)} مستويات قريبة حول {round(center, 2)} بدل عرض فروقات سنتات.")

    price_zone = None
    nearest_support = None
    nearest_resistance = None
    for z in zones:
        if price > 0 and z["low"] <= price <= z["high"] and z["kind"] in {"support", "resistance", "congestion", "reclaim", "broken_support"}:
            price_zone = z
            break
    # Do not treat a congestion/decision zone as both a tradable support and
    # resistance.  Inside congestion, the lower boundary is the failure side and
    # the upper boundary is the activation side; the card should not show
    # cent-level support/resistance as separate decisions.
    structural_supports = [z for z in zones if z["kind"] in {"support", "reclaim", "broken_support"} and price > 0 and z["center"] <= price * 1.015]
    structural_resistances = [z for z in zones if z["kind"] in {"resistance", "major_resistance", "target"} and price > 0 and z["center"] >= price * 0.985]
    if structural_supports:
        nearest_support = sorted(structural_supports, key=lambda z: abs(price - z["center"]))[0]
    if structural_resistances:
        nearest_resistance = sorted(structural_resistances, key=lambda z: abs(price - z["center"]))[0]

    micro_zone = False
    if price_zone and price_zone.get("kind") == "congestion":
        micro_zone = _small_stock_micro_zone_ok(price, atr_pct, _num(price_zone.get("low"), 0.0), _num(price_zone.get("high"), 0.0))
        if micro_zone:
            notes.append("سهم صغير السعر: قرب الدعم والمقاومة طبيعي؛ الحكم يكون من إغلاق شمعة فوق/تحت المنطقة لا من فروقات السنت.")
        else:
            notes.append("السعر داخل منطقة ضيقة؛ لا يُبنى قرار مستقل من فروقات سنتات داخلها.")
    if not zones:
        notes.append("لا توجد مستويات كافية لبناء مناطق دعم/مقاومة موثوقة من البيانات الحالية.")

    summary_bits = []
    if price_zone and price_zone.get("kind") == "congestion":
        if 'micro_zone' in locals() and micro_zone:
            summary_bits.append(f"منطقة تداول صغيرة للسهم: {price_zone['low']} - {price_zone['high']}")
        else:
            summary_bits.append(f"السعر داخل منطقة قرار: {price_zone['low']} - {price_zone['high']}")
        summary_bits.append(f"حد الفشل أسفل {price_zone['low']}")
        summary_bits.append(f"حد التفعيل فوق {price_zone['high']}")
    else:
        if price_zone:
            summary_bits.append(f"السعر داخل {price_zone['label']}: {price_zone['low']} - {price_zone['high']}")
        if nearest_support:
            summary_bits.append(f"الدعم/المنطقة الأقرب: {nearest_support['low']} - {nearest_support['high']} ({nearest_support['strength']})")
        if nearest_resistance:
            summary_bits.append(f"المقاومة/التفعيل الأقرب: {nearest_resistance['low']} - {nearest_resistance['high']} ({nearest_resistance['strength']})")
    if not summary_bits:
        summary_bits.append("لا توجد منطقة قرار موثوقة كفاية من المستويات الحالية.")

    return {
        "version": OPPORTUNITY_RADAR_VERSION,
        "price": _round(price, 2),
        "atr": _round(atr, 2),
        "atr_pct": _round(atr_pct, 2),
        "merge_threshold": _round(threshold, 2),
        "zones": zones[:10],
        "price_zone": price_zone or {},
        "nearest_support_zone": nearest_support or {},
        "nearest_resistance_zone": nearest_resistance or {},
        "summary_ar": " | ".join(summary_bits[:3]),
        "notes": _dedupe(notes, 8),
        "micro_price_zone": bool(micro_zone) if 'micro_zone' in locals() else False,
        "micro_zone_rule_ar": "للأسهم الصغيرة ذات السعر المنخفض، قرب الدعم والمقاومة طبيعي؛ لا نعتمد السنتات كقرار منفصل، بل ننتظر إغلاق 5د/15د فوق حد التفعيل أو تحت حد الفشل." if ('micro_zone' in locals() and micro_zone) else "",
    }


def _is_true_no_chase(row: dict) -> bool:
    decision_code = _s(row.get("final_decision_code"))
    if decision_code == "NO_CHASE":
        return True
    status = _s(row.get("no_chase_guard_status")).lower()
    if status == "no_chase":
        change = _change_pct(row)
        entry = _entry(row)
        price = _price(row)
        dist = _pct_distance(price, entry) if price > 0 and entry > 0 else 0.0
        return change >= 7.0 or dist >= 3.0
    text = " ".join([_s(row.get("owner_action")), _s(row.get("execution_readiness_label")), _s(row.get("move_stage_label"))])
    return bool(("لا تطارد" in text or "No-Chase" in text) and _change_pct(row) >= 7.0)


def _liquidity_score(row: dict) -> tuple[float, list[str]]:
    rv = _num(row.get("effective_volume_ratio", row.get("volume_pace_ratio", row.get("volume_ratio", 0))), 0.0)
    liq = _num(row.get("liquidity_persistence_score"), 0.0)
    dollar = _num(row.get("dollar_volume", row.get("live_dollar_volume", row.get("fmp_dollar_volume", 0))), 0.0)
    reasons = []
    score = 0.0
    if rv >= 2.0:
        score += 26; reasons.append(f"RVOL قوي {round(rv, 2)}x")
    elif rv >= 1.2:
        score += 18; reasons.append(f"الحجم يتحسن {round(rv, 2)}x")
    elif rv >= 0.9:
        score += 9; reasons.append(f"الحجم قريب من الطبيعي {round(rv, 2)}x")
    if liq >= 70:
        score += 22; reasons.append("استمرار السيولة جيد")
    elif liq >= 50:
        score += 12; reasons.append("السيولة مقبولة")
    if dollar >= 50_000_000:
        score += 18; reasons.append("دولار فوليوم قوي")
    elif dollar >= 8_000_000:
        score += 9; reasons.append("دولار فوليوم قابل للتداول")
    return min(60.0, score), reasons


def _price_filter(row: dict) -> dict:
    """Personal price comfort filter.

    High-priced stocks are not treated as bad data. They are simply not
    practical for the user's main opportunity flow unless the setup is truly
    exceptional. This keeps MU-like prices valid while preventing expensive
    names from filling the actionable sections.
    """
    price = _price(row)
    quality = _num(row.get("quality_score"), 0.0)
    readiness = _num(row.get("execution_readiness_score"), 0.0)
    decision = _s(row.get("decision"))
    final_code = _s(row.get("final_decision_code"))
    change = _change_pct(row)
    liquidity_points, liquidity_reasons = _liquidity_score(row)
    if price <= 0:
        return {
            "bucket": "unknown",
            "label": "سعر غير متوفر",
            "rank_adjustment": 0.0,
            "practical": True,
            "section_eligible": True,
            "memory_eligible": True,
        }
    if price < PERSONAL_PRICE_COMFORT:
        return {
            "bucket": "comfortable",
            "label": "سعر مريح للمستخدم (<50$)",
            "rank_adjustment": 6.0,
            "practical": True,
            "section_eligible": True,
            "memory_eligible": True,
        }
    if price <= PERSONAL_PRICE_MAX_NORMAL:
        return {
            "bucket": "acceptable",
            "label": "سعر مقبول للمستخدم (50–150$)",
            "rank_adjustment": 0.0,
            "practical": True,
            "section_eligible": True,
            "memory_eligible": True,
        }

    strong_exception = bool(
        decision == "دخول قوي"
        and final_code == "BUY_NOW"
        and quality >= 86
        and readiness >= 68
        and liquidity_points >= 18
    )
    cautious_exception = bool(
        decision == "دخول بحذر"
        and quality >= 90
        and readiness >= 74
        and liquidity_points >= 24
        and change < 5.5
    )
    pre_stage_exception = bool(
        final_code in {"WAIT_TRIGGER", "EARLY_WATCH", "WAIT_RESISTANCE"}
        and quality >= 93
        and readiness >= 82
        and liquidity_points >= 32
        and change < 4.5
    )
    exceptional = strong_exception or cautious_exception or pre_stage_exception
    exception_reasons = []
    if quality >= 90:
        exception_reasons.append(f"جودة عالية {round(quality, 1)}/100")
    if readiness >= 74:
        exception_reasons.append(f"جاهزية عالية {round(readiness, 1)}/100")
    if liquidity_points >= 24:
        exception_reasons.extend(liquidity_reasons[:2])

    return {
        "bucket": "high_price_exception" if exceptional else "high_price_deprioritized",
        "label": "سعر مرتفع لكن الفرصة استثنائية فنيًا" if exceptional else "سعر مرتفع — مخفي من الفرص العملية إلا إذا أصبح استثنائيًا",
        "rank_adjustment": -14.0 if exceptional else -55.0,
        "practical": bool(exceptional),
        "section_eligible": bool(exceptional),
        "memory_eligible": bool(exceptional),
        "exceptional": bool(exceptional),
        "exception_reasons": _dedupe(exception_reasons, 5),
        "rule_ar": "فوق 150$ لا يدخل الأقسام العملية ولا Plan Memory إلا إذا اجتمعت جودة عالية + جاهزية + سيولة واضحة.",
    }



def _nested(row: dict, keys: list[str], default: Any = None) -> Any:
    """Read a value from flat keys or common nested intraday/live blocks."""
    if not isinstance(row, dict):
        return default
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
    for parent in ["intraday_context", "intraday", "live_intraday", "polygon_intraday", "evidence", "market_context"]:
        block = row.get(parent)
        if isinstance(block, dict):
            for key in keys:
                if key in block and block.get(key) not in (None, ""):
                    return block.get(key)
    return default


def _first_nested(row: dict, keys: list[str], default: float = 0.0) -> float:
    val = _nested(row, keys, None)
    return _num(val, default)


def _company_text(row: dict) -> str:
    return " ".join([
        _s(row.get("symbol")), _s(row.get("company_name")), _s(row.get("name")),
        _s(row.get("sector")), _s(row.get("industry")), _s(row.get("country")),
    ]).lower()


def _behavior_group(row: dict, price: float) -> dict:
    text = _company_text(row)
    sector = _s(row.get("sector") or row.get("Sector") or row.get("industry"))
    shares_float = _first_nested(row, ["shares_float", "float_shares", "free_float", "public_float", "float"], 0.0)
    market_cap = _first_nested(row, ["market_cap", "marketCap", "mkt_cap", "approx_market_cap"], 0.0)
    tags: list[str] = []
    if any(w in text for w in ["china", "chinese", "hong kong", "beijing", "shanghai", "shenzhen", "cayman"]):
        tags.append("موجة صينية/ADR")
    if any(w in text for w in ["japan", "japanese", "tokyo"]):
        tags.append("موجة يابانية")
    if 0 < shares_float <= 1_000_000:
        tags.append("Float تحت مليون")
    elif 0 < shares_float <= 10_000_000:
        tags.append("Low Float")
    if 0 < price < 5:
        tags.append("موجة سنتات")
    if sector:
        tags.append(f"قطاع: {sector[:40]}")
    if market_cap and market_cap <= 300_000_000:
        tags.append("Micro Cap")
    elif market_cap and market_cap <= 2_000_000_000:
        tags.append("Small Cap")
    return {
        "shares_float": _round(shares_float, 0),
        "market_cap": _round(market_cap, 0),
        "tags": _dedupe(tags, 6),
    }


def _classic_small_stock_setup(row: dict, zones: dict, flags_hint: dict | None = None) -> dict:
    """Classic small-stock radar based on Fib/VWAP/previous-high behavior.

    Low-priced names can have support/resistance only cents apart.  That is not
    automatically a bug or a blocker.  For them we treat close levels as a
    micro decision zone and require candle/zone behavior: Fib golden-zone,
    VWAP pullback/reclaim, previous-day-high reclaim, or a micro-range breakout
    watch.  This remains monitoring/high-risk context, not BUY_NOW.
    """
    price = _price(row)
    change = _change_pct(row)
    move_risk = _move_risk_pct(row)
    rv = _num(row.get("effective_volume_ratio", row.get("volume_pace_ratio", row.get("volume_ratio", 0))), 0.0)
    dollar = _first_nested(row, ["dollar_volume", "live_dollar_volume", "day_dollar_volume", "pre_market_dollar_volume"], 0.0)
    volume = _first_nested(row, ["volume", "day_volume", "pre_market_volume", "volume_live"], 0.0)
    spread_pct = _first_nested(row, ["spread_pct", "bid_ask_spread_pct", "spread_percent"], 0.0)
    vwap = _first_nested(row, ["vwap_proxy", "vwap", "current_vwap", "session_vwap"], 0.0)
    above_vwap = bool(_nested(row, ["above_vwap_proxy", "above_vwap", "price_above_vwap"], False))
    prev_high = _first_nested(row, ["previous_day_high", "prev_day_high", "prior_day_high", "previous_high", "prev_high"], 0.0)
    day_low = _first_nested(row, ["session_low", "day_low", "low_live", "low"], 0.0)
    day_high = _first_nested(row, ["session_high", "day_high", "high_live", "high"], 0.0)
    if day_low <= 0:
        day_low = _first_nested(row, ["nearest_support", "support_price", "display_support_price"], 0.0)
    if day_high <= 0:
        day_high = _first_nested(row, ["nearest_resistance", "resistance_price", "display_resistance_price", "major_resistance"], 0.0)

    atr, atr_pct = _atr(row, price)
    pz = zones.get("price_zone") if isinstance(zones, dict) else {}
    pz_low = _num((pz or {}).get("low"), 0.0)
    pz_high = _num((pz or {}).get("high"), 0.0)
    micro_zone = bool((pz or {}).get("kind") == "congestion" and _small_stock_micro_zone_ok(price, atr_pct, pz_low, pz_high))
    micro_pos = ((price - pz_low) / max(pz_high - pz_low, 0.0001)) if micro_zone and pz_low > 0 and pz_high > pz_low else 0.0
    near_micro_top = bool(micro_zone and micro_pos >= 0.62)
    near_micro_bottom = bool(micro_zone and micro_pos <= 0.38)

    eligible_price = bool(1.0 <= price <= 20.0)
    penny_or_low = bool(1.0 <= price <= 12.0)
    liquid_enough = bool(rv >= 1.15 or dollar >= 500_000 or volume >= 120_000 or _first_nested(row, ["pre_market_volume"], 0.0) >= 80_000)
    spread_ok = bool(spread_pct <= 0 or spread_pct <= (3.0 if price < 5 else 1.7))

    fib_levels: dict[str, float] = {}
    fib_state = "unavailable"
    fib_reasons: list[str] = []
    if day_low > 0 and day_high > day_low * 1.015:
        rng = day_high - day_low
        fib_levels = {
            "38.2": _round(day_high - rng * 0.382, 4),
            "50": _round(day_high - rng * 0.500, 4),
            "61.8": _round(day_high - rng * 0.618, 4),
            "78.6": _round(day_high - rng * 0.786, 4),
        }
        f382, f50, f618, f786 = fib_levels["38.2"], fib_levels["50"], fib_levels["61.8"], fib_levels["78.6"]
        golden_low = min(f618, f786)
        golden_high = max(f50, f618)
        near_golden = golden_low * 0.990 <= price <= golden_high * 1.012
        reclaimed_618 = bool(price >= f618 and _abs_pct_distance(price, f618) <= (2.4 if price <= 10 else 1.8) and move_risk < 11.0)
        if near_golden:
            fib_state = "golden_zone_watch"
            fib_reasons.append(f"قريب من المنطقة الذهبية Fib 61.8–78.6 تقريبًا: {round(golden_low, 2)} - {round(max(f618, f786), 2)}")
        elif reclaimed_618:
            fib_state = "fib_618_reclaim"
            fib_reasons.append(f"استعاد/قريب من Fib 61.8 عند {round(f618, 2)} بشرط إغلاق شمعة فوقه")
        elif price > f382 * 1.018 and move_risk >= 7.0:
            fib_state = "extended_above_fib"
            fib_reasons.append("ابتعد فوق مستويات الفيبو؛ لا تلحق الشمعة الخضراء وانتظر رجوع لمنطقة أدق")
        else:
            fib_state = "between_levels"
            fib_reasons.append("بين مستويات الفيبو؛ الأفضل انتظار إغلاق واضح فوق 61.8 أو رجوع للمنطقة الذهبية")

    vwap_state = "unavailable"
    vwap_reasons: list[str] = []
    vwap_dist = 999.0
    if vwap > 0 and price > 0:
        vwap_dist = ((price - vwap) / vwap) * 100.0
        if -0.55 <= vwap_dist <= 1.05:
            vwap_state = "vwap_pullback"
            vwap_reasons.append(f"قريب من VWAP {round(vwap, 2)}؛ مناسب للمراقبة بشرط إغلاق شمعة 5د/15د فوقه")
        elif 1.05 < vwap_dist <= 2.6 and above_vwap and move_risk < 10.0:
            vwap_state = "vwap_reclaim_hold"
            vwap_reasons.append(f"فوق VWAP {round(vwap, 2)} بعد استعادة/ثبات؛ لا يطارد إذا ابتعد كثيرًا")
        elif vwap_dist < -0.55:
            vwap_state = "below_vwap_wait_reclaim"
            vwap_reasons.append(f"تحت VWAP {round(vwap, 2)}؛ انتظر إغلاق شمعة فوقه")
        else:
            vwap_state = "extended_from_vwap"
            vwap_reasons.append("ابتعد عن VWAP؛ الأفضل انتظار Pullback بدل اللحاق")

    prev_high_state = "unavailable"
    prev_high_reasons: list[str] = []
    prev_high_dist = 999.0
    if prev_high > 0 and price > 0:
        prev_high_dist = ((price - prev_high) / prev_high) * 100.0
        if -0.8 <= prev_high_dist <= 1.5:
            prev_high_state = "previous_high_zone"
            prev_high_reasons.append(f"قريب من أعلى شمعة يومية سابقة {round(prev_high, 2)}؛ منطقة شراء/تفعيل كلاسيكية بشرط إغلاق فوقها")
        elif 1.5 < prev_high_dist <= 3.2 and move_risk < 9.0:
            prev_high_state = "previous_high_reclaim_hold"
            prev_high_reasons.append(f"استعاد قمة أمس {round(prev_high, 2)} ويحتاج ثبات بدون مطاردة")
        elif prev_high_dist > 3.2:
            prev_high_state = "extended_above_previous_high"
            prev_high_reasons.append("ابتعد فوق قمة أمس؛ ليس دخولًا كلاسيكيًا جديدًا إلا بعد Pullback")
        else:
            prev_high_state = "below_previous_high"
            prev_high_reasons.append("تحت قمة أمس؛ انتظر إغلاق شمعة فوقها")

    micro_state = "none"
    micro_reasons: list[str] = []
    if micro_zone:
        if near_micro_top:
            micro_state = "micro_breakout_watch"
            micro_reasons.append(f"داخل منطقة صغيرة طبيعية للسهم {round(pz_low, 2)} - {round(pz_high, 2)}؛ لا قرار إلا بإغلاق فوق {round(pz_high, 2)}")
        elif near_micro_bottom:
            micro_state = "micro_support_watch"
            micro_reasons.append(f"قريب من حد الفشل داخل منطقة صغيرة {round(pz_low, 2)} - {round(pz_high, 2)}؛ يحتاج دفاع واضح لا مجرد رقم دعم")
        else:
            micro_state = "micro_decision_zone"
            micro_reasons.append(f"الدعم والمقاومة قريبان طبيعيًا لسهم صغير؛ تعامل معها كمنطقة قرار {round(pz_low, 2)} - {round(pz_high, 2)}")

    anchor_good = bool(
        fib_state in {"golden_zone_watch", "fib_618_reclaim"}
        or vwap_state in {"vwap_pullback", "vwap_reclaim_hold"}
        or prev_high_state in {"previous_high_zone", "previous_high_reclaim_hold"}
        or micro_state in {"micro_breakout_watch", "micro_support_watch"}
    )
    execution_anchor_available = bool(vwap > 0 or prev_high > 0 or fib_levels or micro_zone)

    score = 0.0
    reasons: list[str] = []
    if eligible_price:
        score += 18; reasons.append("سعر مناسب لرادار الأسهم الصغيرة")
    if penny_or_low:
        score += 6; reasons.append("سعر منخفض سريع الحركة؛ حجم الصفقة يجب أن يكون صغيرًا")
    if liquid_enough:
        score += 20; reasons.append(f"نشاط/حجم مقبول للأسهم الصغيرة RVOL {round(rv, 2)}x")
    if spread_ok:
        score += 8; reasons.append("السبريد مقبول مبدئيًا إن توفرت بياناته")
    if fib_state in {"golden_zone_watch", "fib_618_reclaim"}:
        score += 18; reasons.extend(fib_reasons[:1])
    if vwap_state in {"vwap_pullback", "vwap_reclaim_hold"}:
        score += 18; reasons.extend(vwap_reasons[:1])
    elif vwap <= 0:
        reasons.append("VWAP غير متاح؛ لا نستخدمه كسبب دخول ونبقيه مراقبة فقط")
    if prev_high_state in {"previous_high_zone", "previous_high_reclaim_hold"}:
        score += 16; reasons.extend(prev_high_reasons[:1])
    elif prev_high <= 0:
        reasons.append("قمة اليوم السابق غير متاحة؛ لا نستخدمها كسبب دخول")
    if micro_state in {"micro_breakout_watch", "micro_support_watch"}:
        score += 12; reasons.extend(micro_reasons[:1])
    elif micro_state == "micro_decision_zone":
        score += 6; reasons.extend(micro_reasons[:1])
    if not execution_anchor_available:
        score -= 10; reasons.append("لا توجد منطقة تنفيذ كلاسيكية مؤكدة بعد؛ يحتاج بيانات 5د/15د أو VWAP/قمة أمس")
    if move_risk >= 10.0 and not (fib_state == "golden_zone_watch" or vwap_state == "vwap_pullback" or near_micro_bottom):
        score -= 24; reasons.append(f"سبق أن تحرك بقوة {round(move_risk, 2)}%؛ لا تلحق الحركة وانتظر Pullback")
    elif move_risk >= 7.0 and not anchor_good:
        score -= 14; reasons.append(f"الحركة كبيرة نسبيًا {round(move_risk, 2)}% ولا توجد منطقة كلاسيكية كافية")
    elif move_risk <= 5.5:
        score += 7; reasons.append("لم يتحول إلى مطاردة كبيرة بعد")

    setup_state = "watch"
    if not eligible_price:
        setup_state = "not_small_price"
    elif not liquid_enough:
        setup_state = "needs_volume"
    elif move_risk >= 10.0 and not anchor_good:
        setup_state = "chase_risk_wait_pullback"
    elif fib_state == "golden_zone_watch":
        setup_state = "fib_golden_pullback"
    elif fib_state == "fib_618_reclaim":
        setup_state = "fib_618_reclaim"
    elif vwap_state == "vwap_pullback":
        setup_state = "vwap_pullback"
    elif vwap_state == "vwap_reclaim_hold":
        setup_state = "vwap_reclaim_hold"
    elif prev_high_state in {"previous_high_zone", "previous_high_reclaim_hold"}:
        setup_state = "previous_high_setup"
    elif micro_state == "micro_breakout_watch":
        setup_state = "micro_breakout_watch"
    elif micro_state == "micro_support_watch":
        setup_state = "micro_support_watch"
    elif score >= 52 and anchor_good:
        setup_state = "active_small_stock_watch"
    elif score >= 46:
        setup_state = "monitor_only_missing_anchor"

    behavior = _behavior_group(row, price)
    candidate = bool(eligible_price and liquid_enough and spread_ok and setup_state not in {"not_small_price", "needs_volume"})
    eligible = bool(candidate and setup_state not in {"monitor_only_missing_anchor", "chase_risk_wait_pullback"} and score >= 48)
    return {
        "version": OPPORTUNITY_RADAR_VERSION,
        "eligible": eligible,
        "candidate": candidate,
        "setup_state": setup_state,
        "score": _round(score, 2),
        "price": _round(price, 4),
        "change_pct": _round(change, 2),
        "move_risk_pct": _round(move_risk, 2),
        "fib_levels": fib_levels,
        "fib_state": fib_state,
        "vwap": _round(vwap, 4),
        "vwap_state": vwap_state,
        "vwap_distance_pct": _round(vwap_dist, 2) if vwap_dist != 999.0 else 999.0,
        "previous_day_high": _round(prev_high, 4),
        "previous_high_state": prev_high_state,
        "previous_high_distance_pct": _round(prev_high_dist, 2) if prev_high_dist != 999.0 else 999.0,
        "micro_zone": micro_zone,
        "micro_zone_state": micro_state,
        "micro_zone_low": _round(pz_low, 4),
        "micro_zone_high": _round(pz_high, 4),
        "micro_zone_width_pct": _round(_micro_zone_width_pct(price, pz_low, pz_high), 2) if micro_zone else 999.0,
        "anchor_good": anchor_good,
        "execution_anchor_available": execution_anchor_available,
        "behavior_group": behavior,
        "reasons": _dedupe(reasons + fib_reasons + vwap_reasons + prev_high_reasons + micro_reasons, 12),
        "rule_ar": "للأسهم الصغيرة: قرب الدعم والمقاومة طبيعي، لذلك نعاملها كمنطقة قرار وننتظر Fib/VWAP/قمة أمس أو إغلاق شمعة 5د/15د فوق حد التفعيل؛ لا نلحق الشمعة الخضراء.",
    }

def _technical_reasons(row: dict, zones: dict) -> list[str]:
    reasons: list[str] = []
    decision = _s(row.get("decision"))
    if decision == "دخول قوي":
        reasons.append("قرار Strong بقي صارمًا: شراء الآن فقط إذا اكتملت الخطة والسيولة والسعر.")
    elif decision == "دخول بحذر":
        reasons.append("الخطة جيدة لكنها ليست Strong؛ تحتاج حجم أصغر أو تأكيد بسيط.")
    quality = _num(row.get("quality_score"), 0.0)
    if quality >= 80:
        reasons.append(f"جودة فنية مرتفعة {round(quality, 1)}/100")
    elif quality >= 65:
        reasons.append(f"جودة فنية مقبولة {round(quality, 1)}/100")
    rv = _num(row.get("effective_volume_ratio", row.get("volume_pace_ratio", row.get("volume_ratio", 0))), 0.0)
    if rv >= 1.2:
        reasons.append(f"حجم أعلى من المعتاد {round(rv, 2)}x")
    close_pos = _num(row.get("close_position_pct", row.get("session_position_pct", 0)), 0.0)
    if close_pos >= 75:
        reasons.append("الإغلاق/السعر قريب من أعلى النطاق")
    if row.get("support_reclaimed_flag") or row.get("reclaimed_support_level"):
        reasons.append("استعاد مستوى دعم/محور مهم")
    if row.get("support_broken_flag") or row.get("broken_support_level"):
        reasons.append("يوجد مستوى مكسور يحتاج Reclaim قبل الثقة")
    pz = zones.get("price_zone") or {}
    if pz:
        reasons.append(f"السعر داخل {pz.get('label')}: {pz.get('low')} - {pz.get('high')}")
    ns = zones.get("nearest_support_zone") or {}
    nr = zones.get("nearest_resistance_zone") or {}
    if ns:
        reasons.append(f"أقرب دعم كمنطقة: {ns.get('low')} - {ns.get('high')}")
    if nr:
        reasons.append(f"أقرب مقاومة/تفعيل كمنطقة: {nr.get('low')} - {nr.get('high')}")
    news_badge = _s(row.get("news_badge"))
    if news_badge:
        reasons.append(f"سياق الأخبار: {news_badge}")
    return _dedupe(reasons, 10)


def _bucket_rank(row: dict, base: float = 0.0, extra: float = 0.0) -> float:
    quality = _num(row.get("quality_score"), 0.0)
    readiness = _num(row.get("execution_readiness_score"), 0.0)
    rank = _num(row.get("display_rank_score", row.get("live_rank_score", 0)), 0.0)
    price_adj = _num((row.get("personal_price_filter") or {}).get("rank_adjustment"), 0.0) if isinstance(row.get("personal_price_filter"), dict) else 0.0
    return round(max(0.0, base + extra + quality * 0.30 + readiness * 0.22 + rank * 0.20 + price_adj), 2)


def _within(value: float, low: float, high: float) -> bool:
    return low <= value <= high


def _flow_flags(row: dict, zones: dict) -> dict[str, Any]:
    price = _price(row)
    entry = _entry(row)
    stop = _stop(row)
    target = _target1(row)
    atr, atr_pct = _atr(row, price)
    no_chase = _is_true_no_chase(row)
    change = _change_pct(row)
    from_open = _num(row.get("change_from_open_pct", 0), 0.0)
    quality = _num(row.get("quality_score"), 0.0)
    readiness = _num(row.get("execution_readiness_score"), 0.0)
    liquidity_points, liquidity_reasons = _liquidity_score(row)
    decision = _s(row.get("decision"))
    final_code = _s(row.get("final_decision_code"))
    pz = zones.get("price_zone") or {}
    ns = zones.get("nearest_support_zone") or {}
    nr = zones.get("nearest_resistance_zone") or {}

    support_center = _num(ns.get("center"), _first(row, ["nearest_support", "support_price", "display_support_price"], 0.0))
    resistance_center = _num(nr.get("center"), _first(row, ["nearest_resistance", "resistance_price", "display_resistance_price"], 0.0))
    support_dist = _pct_distance(price, support_center) if price > 0 and support_center > 0 else 999.0
    resistance_dist = ((resistance_center - price) / price * 100.0) if price > 0 and resistance_center > 0 else 999.0
    close_pos = _num(row.get("close_position_pct", row.get("session_position_pct", row.get("day_range_position_pct", 0))), 0.0)
    pz_low = _num(pz.get("low"), 0.0)
    pz_high = _num(pz.get("high"), 0.0)
    pz_mid = (pz_low + pz_high) / 2.0 if pz_low > 0 and pz_high > 0 else 0.0
    pz_pos = ((price - pz_low) / (pz_high - pz_low)) if price > 0 and pz_high > pz_low and pz_low <= price <= pz_high else 0.0
    in_upper_congestion = bool(_s(pz.get("kind")) == "congestion" and pz_mid > 0 and (price >= pz_mid or pz_pos >= 0.45))
    # If no structural resistance exists, the upper boundary of the decision zone
    # is the real activation wall, not a separate cents-level resistance.
    if resistance_center <= 0 and _s(pz.get("kind")) == "congestion" and pz_high > price:
        resistance_center = pz_high
        resistance_dist = ((resistance_center - price) / price * 100.0) if price > 0 else 999.0
    near_resistance_now = bool((nr or (_s(pz.get("kind")) == "congestion" and pz_high > 0)) and -0.25 <= resistance_dist <= max(1.2, atr_pct * 0.55))
    resistance_closer_than_support = bool(
        resistance_dist != 999.0 and support_dist != 999.0 and resistance_dist >= -0.25 and resistance_dist < max(0.15, support_dist * 0.72)
    )
    support_bounce_distance_limit = max(1.45, min(2.15, atr_pct * 0.55 if atr_pct > 0 else 1.45))
    extended_after_move = bool((change >= 3.8 or from_open >= 3.5) and (near_resistance_now or in_upper_congestion or close_pos >= 68 or resistance_closer_than_support))
    structural_support_near = bool(
        price > 0 and ns and not resistance_closer_than_support
        and (
            ns.get("low", 0) <= price <= ns.get("high", 0) * 1.006
            or 0 <= support_dist <= support_bounce_distance_limit
        )
    )
    lower_decision_zone_bounce = bool(_s(pz.get("kind")) == "congestion" and pz_low > 0 and pz_pos <= 0.28 and change <= 1.8 and not resistance_closer_than_support)
    near_support_raw = bool(structural_support_near or lower_decision_zone_bounce)
    near_support = bool(near_support_raw and not extended_after_move and change < 3.5 and not resistance_closer_than_support)
    reclaim = bool(row.get("support_reclaimed_flag") or row.get("reclaimed_support_level") or final_code == "RECLAIM_REQUIRED" or _s(pz.get("kind")) == "reclaim")
    broken_needs_reclaim = bool(row.get("support_broken_flag") or row.get("broken_support_level") or final_code == "RECLAIM_REQUIRED")

    trigger = entry
    if nr and _num(nr.get("center"), 0.0) > 0:
        trigger = min([x for x in [entry, _num(nr.get("center"), 0.0)] if x > 0] or [entry])
    trigger_dist = _pct_distance(trigger, price) if price > 0 and trigger > 0 else 999.0  # positive means trigger above price
    pre_trigger = bool(price > 0 and trigger > price and _within(trigger_dist, 0.0, max(2.2, atr_pct * 0.75)) and not no_chase and change < 7.0)

    low_price = 1.0 <= price <= 12.0
    very_low = 1.0 <= price <= 5.0
    high_activity = bool(change >= 4.0 or from_open >= 3.0 or liquidity_points >= 30)
    high_risk_day = bool(low_price and high_activity and not no_chase)
    # V2O: Low-Float classification must not rely only on Watch/Early context.
    # It now uses the stricter fast-lane/float/small-price proxy profile, so
    # known liquid names like NOK are not put in Low-Float just because they are
    # low-ish price and in a watch bucket.
    low_float_profile = _low_float_proxy_metrics(row)
    low_float_pm = bool(
        low_float_profile.get("confirmed_float")
        or low_float_profile.get("small_cap_proxy")
        or low_float_profile.get("proxy_candidate")
        or (low_price and (_num(row.get("pre_market_volume"), 0.0) > 100_000 or _num(row.get("pre_market_change_pct"), 0.0) >= 2.0))
    )
    if extended_after_move and low_price:
        high_risk_day = True
    if low_float_profile.get("fast_lane_source") and low_float_pm:
        high_risk_day = True

    gap_up = _num(row.get("open_gap_pct", row.get("gap_from_prev_close_pct", 0)), 0.0)
    gap_watch = bool(abs(gap_up) >= 2.5 or row.get("gap_fill_candidate") or row.get("gap_retest_success") or row.get("gap_fade_flag"))

    news_context = " ".join([_s(row.get("news_badge")), _s(row.get("news_title")), _s(row.get("news_category")), _s(row.get("news_scope")), _s(row.get("news_context_note"))]).lower()
    catalyst_keywords = ["fda", "clinical", "trial", "earnings", "merger", "acquisition", "upgrade", "downgrade", "price target", "contract", "approval", "biotech", "عقد", "ترقية", "أرباح", "اندماج", "استحواذ"]
    catalyst_details = _build_catalyst_details(row)
    catalyst = bool(catalyst_details.get("has_news") and (catalyst_details.get("is_catalyst") or any(k in news_context for k in catalyst_keywords + ["positive", "negative", "legal"])))

    classic_small = _classic_small_stock_setup(row, zones, {})
    classic_candidate = bool(classic_small.get("eligible") or classic_small.get("candidate"))
    classic_chase_risk = _s(classic_small.get("setup_state")) == "chase_risk_wait_pullback"
    classic_move_risk = _num(classic_small.get("move_risk_pct"), _move_risk_pct(row))

    continuation_pullback = bool((change >= 2.0 or _s(row.get("move_stage")) in {"Continuation Watch", "Requires Pullback"}) and not no_chase and (entry > 0 and price <= entry * 1.035) and quality >= 58)

    support_score = 0.0
    support_reasons = []
    if near_support:
        support_score += 35; support_reasons.append("قريب من منطقة دعم ذات معنى")
    elif near_support_raw and extended_after_move:
        support_reasons.append("كان قريبًا من منطقة قرار/دعم، لكنه تحرك وأصبح قريبًا من مقاومة؛ لا يصنف كارتداد دعم مبكر.")
    if support_dist != 999.0 and support_center > 0:
        support_reasons.append(f"المسافة عن الدعم {round(support_dist, 2)}%")
    elif _s(pz.get("kind")) == "congestion" and pz_low > 0:
        boundary_dist = ((price - pz_low) / price * 100.0) if price > 0 else 999.0
        support_reasons.append(f"المسافة عن حد الفشل في منطقة القرار {round(boundary_dist, 2)}%")
    if change <= 2.0 and not extended_after_move:
        support_score += 8; support_reasons.append("لم يتحرك بعيدًا بعد")
    elif change >= 5.0:
        support_reasons.append(f"السهم متحرك الآن {round(change, 2)}%؛ يحتاج تصنيف مخاطرة/استمرار لا Support Bounce.")
    if readiness >= 45:
        support_score += 8; support_reasons.append("جاهزية أولية مقبولة")
    if liquidity_points >= 18:
        support_score += 10; support_reasons.extend(liquidity_reasons[:2])
    if stop > 0 and price > stop and support_center > 0 and stop < support_center * 1.02:
        support_score += 5; support_reasons.append("الوقف قريب من منطقة الدعم")

    reclaim_score = 0.0
    reclaim_reasons = []
    if reclaim:
        reclaim_score += 36; reclaim_reasons.append("السهم في مسار Reclaim / استعادة مستوى")
    if broken_needs_reclaim:
        reclaim_score += 12; reclaim_reasons.append("كان هناك كسر/هزة ويحتاج ثبات فوق المستوى")
    if liquidity_points >= 18:
        reclaim_score += 12; reclaim_reasons.extend(liquidity_reasons[:2])
    if not no_chase and change < 8.0:
        reclaim_score += 8; reclaim_reasons.append("ليس مطاردة حتى الآن")

    pre_score = 0.0
    pre_reasons = []
    if pre_trigger:
        pre_score += 40; pre_reasons.append(f"قريب من التفعيل: يحتاج تقريبًا {round(trigger_dist, 2)}%")
    if quality >= 62:
        pre_score += 8; pre_reasons.append("الخطة الفنية جيدة كمرحلة قبل التنفيذ")
    if liquidity_points >= 18:
        pre_score += 10; pre_reasons.extend(liquidity_reasons[:2])
    if nr:
        pre_reasons.append(f"منطقة التفعيل/المقاومة: {nr.get('low')} - {nr.get('high')}")

    return {
        "no_chase": no_chase,
        "near_support": near_support,
        "support_score": round(support_score, 2),
        "support_reasons": _dedupe(support_reasons, 8),
        "reclaim": reclaim or broken_needs_reclaim,
        "reclaim_confirmed": bool(reclaim and liquidity_points >= 18 and price > 0),
        "reclaim_score": round(reclaim_score, 2),
        "reclaim_reasons": _dedupe(reclaim_reasons, 8),
        "pre_trigger": pre_trigger,
        "pre_trigger_score": round(pre_score, 2),
        "pre_trigger_reasons": _dedupe(pre_reasons, 8),
        "high_risk_day": high_risk_day,
        "low_float_pm": low_float_pm,
        "low_float_fast_lane": bool(low_float_profile.get("fast_lane_source")),
        "low_float_profile_v2o": low_float_profile,
        "classic_small_stock": classic_small,
        "classic_small_candidate": classic_candidate,
        "classic_small_chase_risk": classic_chase_risk,
        "extended_after_move": extended_after_move,
        "near_resistance_now": near_resistance_now,
        "resistance_closer_than_support": resistance_closer_than_support,
        "gap_watch": gap_watch,
        "catalyst": catalyst,
        "catalyst_details": catalyst_details,
        "continuation_pullback": continuation_pullback,
        "liquidity_score": round(liquidity_points, 2),
        "liquidity_reasons": _dedupe(liquidity_reasons, 6),
        "trigger_price": _round(trigger, 2),
        "trigger_distance_pct": _round(trigger_dist, 2) if trigger_dist != 999.0 else 999.0,
        "support_distance_pct": _round(support_dist, 2) if support_dist != 999.0 else 999.0,
        "resistance_distance_pct": _round(resistance_dist, 2) if resistance_dist != 999.0 else 999.0,
        "atr_pct": _round(atr_pct, 2),
    }


def _stage_from_flags(row: dict, flags: dict) -> tuple[str, str, str, list[str]]:
    decision = _s(row.get("decision"))
    final_code = _s(row.get("final_decision_code"))
    if decision == "دخول قوي" and final_code == "BUY_NOW":
        return "strong", "🟢 دخول قوي مؤكد", "strong_entries", ["Strong هو آخر مرحلة مؤكدة وليس بديلًا عن المراحل المبكرة."]
    if decision == "دخول بحذر":
        if flags.get("near_support"):
            return "cautious_support_bounce", "🟠 دخول بحذر — ارتداد من دعم", "cautious_entries", flags.get("support_reasons", [])
        if flags.get("reclaim"):
            return "cautious_reclaim", "🟠 دخول بحذر — Reclaim", "cautious_entries", flags.get("reclaim_reasons", [])
        return "cautious", "🟠 دخول بحذر", "cautious_entries", ["خطة جيدة لكنها تحتاج انضباطًا وحجمًا أصغر من Strong."]
    classic = flags.get("classic_small_stock") or {}
    if flags.get("classic_small_chase_risk") and flags.get("classic_small_candidate"):
        return "high_risk_day_trade", "⚡ مضاربة عالية المخاطرة", "high_risk_day_trade", classic.get("reasons", []) or ["سهم صغير سبق أن تحرك؛ انتظر Pullback إلى Fib/VWAP/قمة أمس ولا تطارد."]
    if flags.get("extended_after_move") and (flags.get("high_risk_day") or classic.get("candidate")):
        return "high_risk_day_trade", "⚡ مضاربة عالية المخاطرة", "high_risk_day_trade", ["تحرك قوي وقريب من مقاومة/منطقة قرار؛ لا يصنف Support Bounce ولا يُطارد."]
    if flags.get("classic_small_candidate") and not flags.get("classic_small_chase_risk") and not flags.get("extended_after_move"):
        return "small_stock_classic", "🎯 أسهم صغيرة — Fib/VWAP/قمة أمس", "small_stock_classic", classic.get("reasons", []) or ["مرشح سهم صغير وفق فيبو/VWAP/قمة اليوم السابق؛ ليس Strong عادي."]
    if flags.get("pre_trigger") and not flags.get("extended_after_move"):
        return "pre_trigger", "⏳ قريب من التفعيل", "pre_trigger", flags.get("pre_trigger_reasons", [])
    if flags.get("reclaim"):
        label = "🟢 Reclaim مؤكد" if flags.get("reclaim_confirmed") else "🔁 Reclaim يحتاج ثبات"
        return "reclaim", label, "reclaim", flags.get("reclaim_reasons", [])
    if flags.get("near_support"):
        return "support_bounce", "↩️ بدأ ارتداد / قريب من دعم", "support_bounce", flags.get("support_reasons", [])
    if flags.get("low_float_fast_lane") and flags.get("low_float_pm"):
        profile = flags.get("low_float_profile_v2o") if isinstance(flags.get("low_float_profile_v2o"), dict) else {}
        reasons = ["مصدر Low-Float Fast Lane مستقل — عالي المخاطر ومراقبة فقط."]
        reasons.extend(list(profile.get("reasons") or [])[:5])
        return "low_float_premarket", "🚀 Low-Float Fast Lane / بري ماركت", "low_float_premarket", _dedupe(reasons, 7)
    if flags.get("low_float_pm") and not flags.get("extended_after_move"):
        profile = flags.get("low_float_profile_v2o") if isinstance(flags.get("low_float_profile_v2o"), dict) else {}
        reasons = ["سهم صغير/نشط يحتاج مراقبة مبكرة وليس Strong عادي."]
        if profile.get("label_ar"):
            reasons.append(_s(profile.get("label_ar")))
        return "low_float_premarket", "🚀 مرشح Low-Float / بري ماركت", "low_float_premarket", _dedupe(reasons, 7)
    if flags.get("high_risk_day"):
        base = "سهم صغير متحرك؛ يعامل كحجم صغير عالي المخاطرة لا كدخول قوي عادي."
        if flags.get("extended_after_move"):
            base = "تحرك قوي وقريب من مقاومة/منطقة قرار؛ لا يصنف Support Bounce ولا يُطارد."
        return "high_risk_day_trade", "⚡ مضاربة عالية المخاطرة", "high_risk_day_trade", [base]
    if flags.get("continuation_pullback"):
        return "continuation_pullback", "📈 Continuation Pullback Candidate", "continuation_pullback", ["استمرار مشروط؛ لا تطارد القمة وانتظر Pullback صحي."]
    if flags.get("gap_watch"):
        return "gap_fill_watch", "🕳️ Gap Fill Watch", "gap_fill_watch", ["توجد فجوة أو إعادة اختبار فجوة؛ ليست كل فجوة يجب أن تغلق."]
    if flags.get("catalyst"):
        details = flags.get("catalyst_details") if isinstance(flags.get("catalyst_details"), dict) else {}
        reasons = _catalyst_reasons(details) or ["يوجد سياق خبر/محفز؛ القرار ليس شراء مباشر من الخبر وحده."]
        return "catalyst_watch", "📰 Catalyst / News Watch", "catalyst_watch", reasons
    if flags.get("no_chase"):
        return "no_chase", "⛔ تحرك وفات / لا تطارد", "no_chase", ["الفرصة أصبحت متأخرة؛ انتظر Pullback أو Reclaim جديد."]
    return "watch", "👀 مراقبة", "watchlist", ["تحت المراقبة حتى تظهر مرحلة أوضح."]


def enrich_row_opportunity_radar(row: dict, market_phase: str = "") -> dict:
    if not isinstance(row, dict):
        return row
    out = row
    price = _price(out)
    zones = build_support_resistance_zones(out)
    price_filter = _price_filter(out)
    flags = _flow_flags(out, zones)
    stage_code, stage_label, bucket, stage_reasons = _stage_from_flags(out, flags)
    technical_reasons = _technical_reasons(out, zones)
    high_price_note = []
    if _s(price_filter.get("bucket")) == "high_price_deprioritized":
        high_price_note.append(_s(price_filter.get("label")))
    elif _s(price_filter.get("bucket")) == "high_price_exception":
        high_price_note.append(_s(price_filter.get("label")))
        high_price_note.extend(price_filter.get("exception_reasons") or [])
    catalyst_details = flags.get("catalyst_details") if isinstance(flags.get("catalyst_details"), dict) else _build_catalyst_details(out)
    catalyst_note = _catalyst_reasons(catalyst_details)
    learning_overlay = _learning_overlay_for_row(out, flags, market_phase)
    learning_note = []
    if isinstance(learning_overlay, dict):
        label = _s(learning_overlay.get("label_ar"))
        action = _s(learning_overlay.get("action_ar"))
        if label and learning_overlay.get("matched"):
            learning_note.append(label)
        if action and learning_overlay.get("matched"):
            learning_note.append(action)
    merged_reasons = _dedupe(stage_reasons + catalyst_note + learning_note + technical_reasons + high_price_note, 12)
    base_extra = 0.0
    if bucket == "support_bounce":
        base_extra = flags.get("support_score", 0.0)
    elif bucket == "reclaim":
        base_extra = flags.get("reclaim_score", 0.0)
    elif bucket == "pre_trigger":
        base_extra = flags.get("pre_trigger_score", 0.0)
    elif bucket == "small_stock_classic":
        base_extra = 24.0 + _num((flags.get("classic_small_stock") or {}).get("score"), 0.0) * 0.55
    elif bucket == "low_float_premarket":
        base_extra = 20.0 + flags.get("liquidity_score", 0.0)
    elif bucket == "high_risk_day_trade":
        base_extra = 14.0 + flags.get("liquidity_score", 0.0)
    elif bucket == "gap_fill_watch":
        base_extra = 15.0
    elif bucket == "catalyst_watch":
        base_extra = 12.0
    elif bucket == "continuation_pullback":
        base_extra = 18.0

    out["opportunity_radar_version"] = OPPORTUNITY_RADAR_VERSION
    out["support_resistance_zones_v2"] = zones
    out["levels_summary"] = zones.get("summary_ar") or out.get("levels_summary", "")
    out["level_refinement_notes"] = _dedupe(list(out.get("level_refinement_notes") or []) + zones.get("notes", []), 10)
    out["personal_price_filter"] = price_filter
    out["personal_price_label"] = price_filter.get("label")
    out["personal_price_bucket"] = price_filter.get("bucket")
    out["personal_price_section_eligible"] = bool(price_filter.get("section_eligible", True))
    out["personal_price_exceptional"] = bool(price_filter.get("exceptional", False))
    out["personal_visibility_status"] = "visible_exception" if price_filter.get("exceptional") else ("deprioritized_high_price" if _s(price_filter.get("bucket")) == "high_price_deprioritized" else "visible")
    out["opportunity_stage"] = stage_code
    out["opportunity_stage_label"] = stage_label
    out["opportunity_bucket"] = bucket
    out["opportunity_reasons"] = merged_reasons
    out["technical_explainer_reasons"] = merged_reasons
    out["catalyst_details"] = catalyst_details
    out["catalyst_type_ar"] = catalyst_details.get("type_ar")
    out["catalyst_date_ar"] = catalyst_details.get("date_ar")
    out["catalyst_time_line_ar"] = catalyst_details.get("time_line_ar")
    out["catalyst_actionability_ar"] = catalyst_details.get("actionability_ar")
    out["catalyst_summary_ar"] = catalyst_details.get("summary_ar")
    learning_boost = _num((learning_overlay or {}).get("priority_boost"), 0.0) if isinstance(learning_overlay, dict) else 0.0
    out["opportunity_rank_score"] = _bucket_rank(out, base=base_extra + learning_boost)
    out["learning_overlay_v1"] = learning_overlay
    out["learning_overlay_label_ar"] = (learning_overlay or {}).get("label_ar") if isinstance(learning_overlay, dict) else ""
    out["learning_overlay_action_ar"] = (learning_overlay or {}).get("action_ar") if isinstance(learning_overlay, dict) else ""
    out["learning_overlay_exit_bias"] = (learning_overlay or {}).get("exit_bias") if isinstance(learning_overlay, dict) else ""
    out["learning_pattern_key"] = (learning_overlay or {}).get("pattern_key") if isinstance(learning_overlay, dict) else ""
    out["opportunity_flow_flags"] = flags
    out["small_stock_classic_setup"] = flags.get("classic_small_stock") or {}
    out["why_appeared_ar"] = "، ".join(merged_reasons[:4])

    # Make cards educational without overriding stronger existing summaries.
    quick = _s(out.get("quick_explainer"))
    if not quick or quick == "تجتمع عدة مؤشرات فنية وسعرية داعمة":
        out["quick_explainer"] = out["why_appeared_ar"]
    # For non-Strong pre-stages, keep No-Chase wording out unless truly no-chase.
    if bucket not in {"no_chase"} and flags.get("no_chase") is False:
        for key in ["owner_action", "execution_readiness_label", "execution_gate_label"]:
            txt = _s(out.get(key))
            if "لا تطارد" in txt and price > 0 and _change_pct(out) < 7.0:
                out[key] = txt.replace("لا تطارد", "انتظر تأكيد")

    # Let old UI plan badge show the new flow if it was generic monitoring.
    if bucket in {"support_bounce", "reclaim", "pre_trigger", "continuation_pullback", "small_stock_classic", "gap_fill_watch", "catalyst_watch", "low_float_premarket", "high_risk_day_trade"}:
        if _s(out.get("display_plan_family_label")) in {"", "الخطة الحالية"}:
            out["display_plan_family_label"] = stage_label
        out["special_bucket_reason"] = out["why_appeared_ar"]

    return out


def enrich_rows_opportunity_radar(rows: list[dict], market_phase: str = "") -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        try:
            out.append(enrich_row_opportunity_radar(row, market_phase=market_phase))
        except Exception as exc:
            if isinstance(row, dict):
                row["opportunity_radar_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            out.append(row)
    return out


OPPORTUNITY_BUCKET_KEYS = [
    "promotion_bridge_candidates",
    "learning_opportunity_candidates",
    "small_stock_classic_radar",
    "support_bounce_candidates",
    "reclaim_candidates",
    "pre_trigger_candidates",
    "continuation_pullback_candidates",
    "high_risk_day_trades",
    "low_float_premarket_radar",
    "gap_fill_watch",
    "catalyst_watch",
]


def _is_blocked(row: dict) -> bool:
    sharia = _s(row.get("sharia_status")).lower()
    if row.get("sharia_manual_excluded") or sharia in {"non_compliant", "haram", "excluded"}:
        return True
    if _s(row.get("final_decision_code")) in {"PLAN_BROKEN", "DATA_INCOMPLETE"}:
        return True
    return False


def _is_personal_section_eligible(row: dict) -> bool:
    pf = row.get("personal_price_filter")
    if not isinstance(pf, dict):
        pf = _price_filter(row)
    # For expensive names, hide by default from practical sections. The stock
    # remains valid for study/comparison, but it should not crowd the user's
    # opportunity radar unless it passes the exception rule.
    if _s(pf.get("bucket")) == "high_price_deprioritized" and not pf.get("section_eligible"):
        return False
    return True


def _high_price_suppression_reason(row: dict) -> str:
    pf = row.get("personal_price_filter")
    if not isinstance(pf, dict):
        pf = _price_filter(row)
    if _s(pf.get("bucket")) == "high_price_deprioritized":
        return _s(pf.get("label")) or "سعر مرتفع — ليس أولوية شخصية"
    return ""


def _sort_bucket(rows: list[dict]) -> list[dict]:
    return sorted(rows or [], key=lambda r: _num(r.get("opportunity_rank_score", r.get("display_rank_score", 0)), 0.0), reverse=True)


# V2L: Closed-market / pre-open planning sections
# -----------------------------------------------
# Strong/Cautious remain execution sections.  When the market is closed (or not
# in the regular session), the user still needs to see concrete preparation
# candidates: small stocks, pre-trigger, continuation/pullback, catalyst, etc.
# This layer copies credible Watch/Early rows into non-actionable prep sections
# with explicit labels.  It never promotes to BUY_NOW and never changes the
# final decision engine.
CLOSED_MARKET_PREP_VERSION = "closed_market_prep_sections_v1_2026_06_19"
PREMARKET_PROMOTION_BRIDGE_VERSION = "premarket_promotion_bridge_v1_2026_06_20"
LOW_FLOAT_FAST_LANE_CAPTURE_VERSION = "low_float_fast_lane_capture_v1_2026_06_20"

PREP_SECTION_TO_BUCKET = {
    "small_stock_classic_radar": "small_stock_classic",
    "pre_trigger_candidates": "pre_trigger",
    "support_bounce_candidates": "support_bounce",
    "reclaim_candidates": "reclaim",
    "continuation_pullback_candidates": "continuation_pullback",
    "low_float_premarket_radar": "low_float_premarket",
    "gap_fill_watch": "gap_fill_watch",
    "catalyst_watch": "catalyst_watch",
}

PREP_SECTION_LABELS_AR = {
    "small_stock_classic_radar": "🎯 سهم صغير للتحضير — Fib/VWAP/منطقة قرار",
    "pre_trigger_candidates": "⏳ قريب من التفعيل — تحضير قبل الافتتاح",
    "support_bounce_candidates": "↩️ قرب دعم — تحقق من المقاومة قبل الافتتاح",
    "reclaim_candidates": "🔁 Reclaim Watch — يحتاج ثبات",
    "continuation_pullback_candidates": "📈 Continuation / Pullback — تحضير لا مطاردة",
    "low_float_premarket_radar": "🚀 Low-Float / سهم صغير تحت المراقبة",
    "gap_fill_watch": "🕳️ Gap Watch — مراقبة فجوة",
    "catalyst_watch": "📰 Catalyst / News Context — تحقق يدويًا",
}


def _closed_market_prep_enabled(market_phase: str = "") -> tuple[bool, str]:
    phase = _s(market_phase or "").lower()
    # Treat pre-market and after-hours as planning phases too.  The card must be
    # visible before the open, but it remains non-actionable until live gates pass.
    if phase in {"open", "regular", "market_open"}:
        return False, "regular_session"
    if phase in {"pre_market", "premarket", "after_hours", "afterhours", "closed", "overnight", "weekend", "holiday", ""}:
        return True, phase or "unknown_closed_like"
    # Unknown phases should fail open for visibility, but labels keep the warning.
    return True, f"unknown_phase:{phase}"


def _prep_level_distances(row: dict) -> dict[str, float]:
    price = _price(row)
    zones = row.get("support_resistance_zones_v2") if isinstance(row.get("support_resistance_zones_v2"), dict) else {}
    ns = zones.get("nearest_support_zone") if isinstance(zones.get("nearest_support_zone"), dict) else {}
    nr = zones.get("nearest_resistance_zone") if isinstance(zones.get("nearest_resistance_zone"), dict) else {}
    support = _num(ns.get("center"), _first(row, ["nearest_support", "support_price", "display_support_price", "support"], 0.0))
    resistance = _num(nr.get("center"), _first(row, ["nearest_resistance", "resistance_price", "display_resistance_price", "resistance"], 0.0))
    entry = _entry(row)
    trigger = resistance if resistance > 0 else entry
    support_dist = _pct_distance(price, support) if price > 0 and support > 0 else 999.0
    resistance_dist = ((resistance - price) / price * 100.0) if price > 0 and resistance > 0 else 999.0
    trigger_dist = ((trigger - price) / price * 100.0) if price > 0 and trigger > 0 else 999.0
    return {
        "support": support,
        "resistance": resistance,
        "trigger": trigger,
        "support_dist": support_dist,
        "resistance_dist": resistance_dist,
        "trigger_dist": trigger_dist,
    }


def _prep_row_base_score(row: dict) -> float:
    quality = _num(row.get("quality_score"), 0.0)
    readiness = _num(row.get("execution_readiness_score"), 0.0)
    rank = _num(row.get("display_rank_score", row.get("live_rank_score", row.get("opportunity_rank_score", 0))), 0.0)
    learning = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else {}
    learning_boost = _num(learning.get("priority_boost"), 0.0) if learning.get("matched") else 0.0
    price = _price(row)
    price_bonus = 8.0 if 1.0 <= price <= 20.0 else 0.0
    return quality * 0.28 + readiness * 0.18 + rank * 0.16 + learning_boost + price_bonus


def _first_positive_number(row: dict, keys: list[str]) -> float:
    for key in keys:
        val = _num(row.get(key), 0.0)
        if val > 0:
            return val
    return 0.0


def _low_float_proxy_metrics(row: dict) -> dict[str, Any]:
    """Return a transparent low-float / small-stock proxy used only for prep visibility.

    Live feeds often do not provide a trusted current public float for every
    micro-cap name.  This helper therefore separates confirmed float from proxy
    candidates so the UI can show them without pretending the float is verified.
    """
    price = _price(row)
    change = _change_pct(row)
    move_risk = _move_risk_pct(row)
    vol_ratio = _num(row.get("effective_volume_ratio", row.get("volume_pace_ratio", row.get("volume_ratio", row.get("relative_volume", 0)))), 0.0)
    dollar_vol = _num(row.get("dollar_volume", row.get("current_dollar_volume", row.get("live_dollar_volume", row.get("premarket_dollar_volume", 0)))), 0.0)
    pm_vol = _num(row.get("pre_market_volume", row.get("premarket_volume", row.get("pm_volume", 0))), 0.0)
    pm_change = _num(row.get("pre_market_change_pct", row.get("premarket_change_pct", row.get("pm_change_pct", 0))), 0.0)
    float_shares = _first_positive_number(row, [
        "float_shares", "shares_float", "public_float", "free_float",
        "float", "freeFloat", "share_float", "sharesFloat",
    ])
    market_cap = _first_positive_number(row, ["market_cap", "marketCap", "mkt_cap", "company_market_cap"])
    prior_count = _num(row.get("prior_candidate_count"), 0.0)
    prev_dates = row.get("previous_candidate_dates")
    has_prev = _bool(row.get("candidate_from_previous_trading_session")) or _bool(row.get("detected_previous_session")) or prior_count > 0 or (isinstance(prev_dates, list) and len(prev_dates) > 0)
    watch_context = _row_is_early_or_watch_context(row)
    learning = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else {}
    learning_positive = _s(learning.get("entry_bias")) in {"positive_watch", "watch_needs_volume", "speculative_watch"}
    tags = row.get("source_reason_tags") if isinstance(row.get("source_reason_tags"), list) else []
    source_text = " ".join([_s(row.get("source_reason")), _s(row.get("first_source_layer")), _s(row.get("source_priority_lane")), " ".join([_s(x) for x in tags])]).lower()
    fast_lane = bool(
        row.get("low_float_fast_lane")
        or row.get("low_float_fast_lane_v1")
        or "low_float_fast_lane" in source_text
        or "low-float fast lane" in source_text
        or "fast lane low-float" in source_text
    )
    confirmed_float = bool(float_shares > 0 and float_shares <= 25_000_000)
    small_cap_proxy = bool(market_cap > 0 and market_cap <= 350_000_000)
    # V2O: Do not call every known, liquid, low-priced stock a Low-Float candidate.
    # The old proxy used Watch/Early context too broadly, so names like NOK could
    # enter Low-Float even with no float proof and no explosive source lane.
    core_micro_price = bool(0.35 <= price <= 12.0)
    extended_small_price = bool(12.0 < price <= 20.0)
    explosive_activity = bool(
        vol_ratio >= 1.35 or dollar_vol >= 75_000 or pm_vol >= 30_000 or abs(pm_change) >= 1.0
        or abs(change) >= 1.0 or move_risk >= 3.0 or has_prev or learning_positive or fast_lane
    )
    # For 12–20$ names, require a dedicated fast-lane/small-cap/confirmed float signal.
    proxy_candidate = bool(
        confirmed_float
        or small_cap_proxy
        or (core_micro_price and explosive_activity)
        or (extended_small_price and fast_lane)
    )
    strong_proxy = bool(
        confirmed_float
        or small_cap_proxy
        or (0.35 <= price <= 8.0 and (explosive_activity or has_prev or fast_lane))
        or (core_micro_price and fast_lane)
    )
    label = "confirmed_float" if confirmed_float else ("small_cap_proxy" if small_cap_proxy else ("fast_lane_proxy" if fast_lane and proxy_candidate else ("proxy_low_price_activity" if proxy_candidate else "not_low_float_candidate")))
    reasons = []
    if confirmed_float:
        reasons.append(f"Float معروف تقريبًا {round(float_shares/1_000_000, 2)}M")
    elif small_cap_proxy:
        reasons.append(f"قيمة سوقية صغيرة تقريبًا {round(market_cap/1_000_000, 1)}M — بديل عند غياب float")
    elif fast_lane and proxy_candidate:
        reasons.append("مرشح من Low-Float Fast Lane: صغير/غامض أو سريع وليس مجرد Watch عادي")
    elif proxy_candidate:
        reasons.append("Float غير مؤكد؛ مرشح Proxy بسبب السعر المنخفض/النشاط/الذاكرة")
    if price > 0:
        reasons.append(f"السعر {round(price, 3)}$ ضمن نطاق الأسهم الصغيرة")
    if has_prev:
        reasons.append("كان موجودًا في جلسة سابقة/ذاكرة مراقبة")
    if watch_context:
        reasons.append("ظاهر في Watch/Early Movement")
    if vol_ratio > 0:
        reasons.append(f"حجم نسبي {round(vol_ratio, 2)}x")
    if pm_vol > 0:
        reasons.append(f"حجم بري ماركت {int(pm_vol):,}")
    if dollar_vol > 0:
        reasons.append(f"دولار فوليوم {round(dollar_vol/1000, 1)}K")
    if abs(change) >= 1.0:
        reasons.append(f"حركة/تغير {round(change, 2)}%")
    return {
        "price": price,
        "float_shares": float_shares,
        "market_cap": market_cap,
        "confirmed_float": confirmed_float,
        "small_cap_proxy": small_cap_proxy,
        "proxy_candidate": proxy_candidate,
        "strong_proxy": strong_proxy,
        "label": label,
        "label_ar": {
            "confirmed_float": "Low-Float مؤكد من بيانات float",
            "small_cap_proxy": "سهم صغير/قيمة سوقية صغيرة — بديل عند غياب float",
            "fast_lane_proxy": "Low-Float Fast Lane — مرشح انفجار مبكر غير مؤكد بالـ float",
            "proxy_low_price_activity": "مرشح Low-Float بالوكالة — السعر/النشاط/الذاكرة",
            "not_low_float_candidate": "ليس مرشح Low-Float حاليًا",
        }.get(label, label),
        "activity": explosive_activity,
        "fast_lane_source": fast_lane,
        "has_previous_session_memory": has_prev,
        "watch_context": watch_context,
        "learning_positive": learning_positive,
        "known_watch_only_excluded": bool(extended_small_price and watch_context and not fast_lane and not confirmed_float and not small_cap_proxy),
        "volume_ratio": vol_ratio,
        "dollar_volume": dollar_vol,
        "premarket_volume": pm_vol,
        "premarket_change_pct": pm_change,
        "move_risk_pct": move_risk,
        "reasons": _dedupe(reasons, 8),
    }


def _low_float_capture_debug(rows: list[dict], existing: list[dict] | None = None) -> dict[str, Any]:
    rows = [r for r in (rows or []) if isinstance(r, dict) and not _is_blocked(r)]
    existing = existing if isinstance(existing, list) else []
    debug = {
        "version": "low_float_capture_audit_v1_2026_06_19",
        "rows_seen": len(rows),
        "price_0_35_to_20_count": 0,
        "price_0_35_to_12_count": 0,
        "confirmed_float_count": 0,
        "small_cap_proxy_count": 0,
        "proxy_candidate_count": 0,
        "fast_lane_source_count": 0,
        "watch_only_excluded_count": 0,
        "watch_or_early_context_count": 0,
        "previous_session_memory_count": 0,
        "existing_low_float_section_count": len(existing),
        "sample_candidates": [],
        "excluded_known_watch_only_sample": [],
        "rule_ar": "V2O: Low-Float الحقيقي لا يأتي من Watch/Early فقط. نفضل Fast Lane مستقل أو سعر صغير جدًا مع نشاط، ونستبعد الأسماء المعروفة/الأكثر سيولة مثل NOK إذا لم يوجد float أو Fast Lane.",
    }
    samples = []
    for row in rows:
        m = _low_float_proxy_metrics(row)
        price = m.get("price", 0.0) or 0.0
        if 0.35 <= price <= 20.0:
            debug["price_0_35_to_20_count"] += 1
        if 0.35 <= price <= 12.0:
            debug["price_0_35_to_12_count"] += 1
        if m.get("confirmed_float"):
            debug["confirmed_float_count"] += 1
        if m.get("small_cap_proxy"):
            debug["small_cap_proxy_count"] += 1
        if m.get("proxy_candidate"):
            debug["proxy_candidate_count"] += 1
        if m.get("fast_lane_source"):
            debug["fast_lane_source_count"] += 1
        if m.get("known_watch_only_excluded"):
            debug["watch_only_excluded_count"] += 1
            if len(debug.get("excluded_known_watch_only_sample", [])) < 12:
                debug["excluded_known_watch_only_sample"].append({"symbol": _u(row.get("symbol")), "price": m.get("price"), "reason_ar": "سعر فوق 12$ أو اسم معروف/Watch فقط بدون fast-lane أو float مؤكد"})
        if m.get("watch_context"):
            debug["watch_or_early_context_count"] += 1
        if m.get("has_previous_session_memory"):
            debug["previous_session_memory_count"] += 1
        if m.get("proxy_candidate") or m.get("confirmed_float") or m.get("small_cap_proxy"):
            samples.append({
                "symbol": _u(row.get("symbol")),
                "price": _round(price, 3),
                "label_ar": m.get("label_ar"),
                "reasons": m.get("reasons", [])[:4],
                "bucket": _s(row.get("opportunity_bucket")),
                "decision": _s(row.get("decision")),
            })
    debug["sample_candidates"] = samples[:15]
    return debug


def _prep_candidate_sections(row: dict) -> list[tuple[str, float, list[str]]]:
    if not isinstance(row, dict):
        return []
    price = _price(row)
    if price <= 0:
        return []
    flags = row.get("opportunity_flow_flags") if isinstance(row.get("opportunity_flow_flags"), dict) else {}
    classic = flags.get("classic_small_stock") if isinstance(flags.get("classic_small_stock"), dict) else (row.get("small_stock_classic_setup") if isinstance(row.get("small_stock_classic_setup"), dict) else {})
    learning = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else {}
    levels = _prep_level_distances(row)
    change = _change_pct(row)
    move_risk = _move_risk_pct(row)
    quality = _num(row.get("quality_score"), 0.0)
    readiness = _num(row.get("execution_readiness_score"), 0.0)
    volume_ratio = _num(row.get("effective_volume_ratio", row.get("volume_pace_ratio", row.get("volume_ratio", 0))), 0.0)
    dollar_vol = _num(row.get("dollar_volume", row.get("current_dollar_volume", row.get("live_dollar_volume", 0))), 0.0)
    base = _prep_row_base_score(row)
    out: list[tuple[str, float, list[str]]] = []

    def add(section: str, bonus: float, reasons: list[str]):
        clean_reasons = _dedupe([r for r in reasons if _s(r)], 8)
        if clean_reasons:
            out.append((section, round(base + bonus, 2), clean_reasons))

    learning_matched = bool(learning.get("matched"))
    learning_positive = _s(learning.get("entry_bias")) in {"positive_watch", "watch_needs_volume", "speculative_watch"}
    was_watch_or_early = _row_is_early_or_watch_context(row)
    low_price = 1.0 <= price <= 20.0
    very_low = 1.0 <= price <= 8.0

    # Small-stock classic prep: show low-price candidates before the open even
    # when live liquidity/readiness is not enough to classify them as execution.
    if low_price and (classic.get("candidate") or classic.get("eligible") or learning_positive or was_watch_or_early or quality >= 48):
        reasons = ["تحضير سهم صغير أثناء الإغلاق/قبل الافتتاح — ليس شراء مباشر."]
        setup = _s(classic.get("setup_state") or row.get("classic_state") or row.get("plan_family"))
        if setup:
            reasons.append(f"النمط/التمركز: {setup}")
        if learning_matched:
            reasons.append(_s(learning.get("label_ar")))
        if levels["resistance_dist"] != 999.0:
            reasons.append(f"راقب التفعيل/المقاومة: تبعد {round(levels['resistance_dist'], 2)}%")
        if levels["support_dist"] != 999.0:
            reasons.append(f"الدعم المرجعي يبعد {round(levels['support_dist'], 2)}%")
        add("small_stock_classic_radar", 34.0, reasons)

    # Pre-trigger prep: near a trigger/resistance/entry zone.  Looser than live
    # Pre-Trigger because it is explicitly non-actionable preparation.
    if levels["trigger_dist"] != 999.0 and -0.35 <= levels["trigger_dist"] <= 5.0 and not flags.get("no_chase"):
        reasons = ["قريب من منطقة تفعيل/مقاومة؛ راقبه قبل الافتتاح ولا تدخل حتى يؤكد."]
        reasons.append(f"المسافة إلى التفعيل تقريبًا {round(levels['trigger_dist'], 2)}%")
        if quality >= 55:
            reasons.append(f"الجودة الفنية {round(quality, 1)}/100")
        if volume_ratio > 0:
            reasons.append(f"حجم نسبي {round(volume_ratio, 2)}x")
        add("pre_trigger_candidates", 28.0, reasons)

    # Support bounce prep: near lower side/support, but not if already extended.
    if levels["support_dist"] != 999.0 and -0.45 <= levels["support_dist"] <= 4.0 and change <= 4.5:
        reasons = ["قرب دعم/منطقة فشل — صالح للمراجعة قبل الافتتاح."]
        reasons.append(f"المسافة عن الدعم {round(levels['support_dist'], 2)}%")
        if levels["resistance_dist"] != 999.0:
            reasons.append(f"تأكد من المقاومة فوق السعر: {round(levels['resistance_dist'], 2)}%")
        add("support_bounce_candidates", 24.0, reasons)

    # Reclaim prep: show broken/reclaim setups even if no live confirmation.
    if flags.get("reclaim") or row.get("support_reclaimed_flag") or row.get("reclaimed_support_level") or row.get("support_broken_flag") or _s(row.get("final_decision_code")) == "RECLAIM_REQUIRED":
        reasons = ["Reclaim Watch: يحتاج ثبات فوق المستوى مع حجم عند الافتتاح."]
        if row.get("reclaimed_support_level"):
            reasons.append(f"مستوى مستعاد: {row.get('reclaimed_support_level')}")
        if row.get("broken_support_level"):
            reasons.append(f"مستوى مكسور يحتاج استعادة: {row.get('broken_support_level')}")
        add("reclaim_candidates", 24.0, reasons)

    # Continuation/Pullback prep: moved before, but should not be chased.
    if (change >= 2.0 or move_risk >= 8.0 or "continuation" in _s(row.get("move_stage")).lower() or "pullback" in _s(row.get("move_stage")).lower()):
        reasons = ["استمرار مشروط بعد حركة سابقة — لا تطارد؛ انتظر Pullback/Reclaim."]
        if move_risk > 0:
            reasons.append(f"أعلى حركة/مخاطرة مطاردة مرصودة {round(move_risk, 2)}%")
        if levels["support_dist"] != 999.0:
            reasons.append(f"منطقة دعم/عودة محتملة تبعد {round(levels['support_dist'], 2)}%")
        add("continuation_pullback_candidates", 21.0, reasons)

    # Low-float/small-stock pre-open prep.  V2M separates confirmed float from
    # proxy low-float candidates so the user can audit candidates before open.
    lf = _low_float_proxy_metrics(row)
    if lf.get("confirmed_float") or lf.get("small_cap_proxy") or lf.get("proxy_candidate") or (very_low and (was_watch_or_early or learning_positive)):
        reasons = ["Low-Float / سهم صغير للتحضير قبل الافتتاح — ليس شراء مباشر."]
        reasons.append(_s(lf.get("label_ar")))
        reasons.extend(lf.get("reasons", [])[:6])
        add("low_float_premarket_radar", 32.0 if lf.get("fast_lane_source") else (27.0 if lf.get("confirmed_float") else 22.0), reasons)

    # Gap/Catalyst are context sections, not entry calls.
    gap = _num(row.get("open_gap_pct", row.get("gap_from_prev_close_pct", 0)), 0.0)
    if abs(gap) >= 2.0 or row.get("gap_fill_candidate") or row.get("gap_retest_success") or row.get("gap_fade_flag"):
        reasons = ["Gap Watch للتحضير: راقب هل يحترم الفجوة أو يدخل داخلها."]
        if gap:
            reasons.append(f"الفجوة/التغير عن الإغلاق السابق تقريبًا {round(gap, 2)}%")
        add("gap_fill_watch", 12.0, reasons)

    details = row.get("catalyst_details") if isinstance(row.get("catalyst_details"), dict) else _build_catalyst_details(row)
    if details.get("has_news"):
        reasons = _catalyst_reasons(details) or ["يوجد خبر/سياق؛ تحقق من قوة المحفز وتاريخه قبل اعتباره سببًا للتداول."]
        add("catalyst_watch", 10.0, reasons)

    return out


def _make_closed_market_prep_row(row: dict, section: str, score: float, reasons: list[str], market_phase: str = "") -> dict:
    out = dict(row or {})
    bucket = PREP_SECTION_TO_BUCKET.get(section, "watchlist")
    label = PREP_SECTION_LABELS_AR.get(section, "تحضير قبل الافتتاح")
    original_bucket = _s(out.get("opportunity_bucket"))
    out["original_opportunity_bucket"] = original_bucket
    out["opportunity_bucket"] = bucket
    out["opportunity_stage"] = f"closed_market_prep_{bucket}"
    out["opportunity_stage_label"] = label
    out["display_plan_family_label"] = label
    out["decision"] = "تحضير قبل الافتتاح — ليس شراء مباشر"
    out["closed_market_prep_v2l"] = {
        "version": CLOSED_MARKET_PREP_VERSION,
        "section": section,
        "source_bucket": original_bucket or "watch_or_early",
        "market_phase": _s(market_phase),
        "rule_ar": "يظهر أثناء الإغلاق/قبل الافتتاح لمراجعة الفرصة والمقاومة والدعم، ولا يتحول إلى دخول إلا بعد تحقق البوابة الحية.",
    }
    prefix = "تحضير أثناء الإغلاق/قبل الافتتاح — راجع المقاومة والدعم ولا تعتبرها شراء مباشر."
    merged = _dedupe([prefix] + (reasons or []) + (out.get("opportunity_reasons") if isinstance(out.get("opportunity_reasons"), list) else []), 10)
    out["opportunity_reasons"] = merged
    out["technical_explainer_reasons"] = merged
    out["why_appeared_ar"] = "، ".join(merged[:4])
    out["special_bucket_reason"] = out["why_appeared_ar"]
    out["opportunity_rank_score"] = round(max(score, _num(out.get("opportunity_rank_score"), 0.0)), 2)
    if section == "low_float_premarket_radar":
        out["low_float_capture_v2m"] = _low_float_proxy_metrics(out)
        out["low_float_label_ar"] = (out.get("low_float_capture_v2m") or {}).get("label_ar")
    out["non_actionable_prep"] = True
    return out



def _sharia_audit_for_row(row: dict) -> dict[str, Any]:
    status = _s(row.get("sharia_status"))
    label = _s(row.get("sharia_label"))
    reason = _s(row.get("sharia_reason"))
    manual_approved = _bool(row.get("sharia_manual_approved"))
    manual_excluded = _bool(row.get("sharia_manual_excluded"))
    is_gray = _bool(row.get("sharia_is_gray")) or status.lower() in {"gray", "needs_review", "review", "unknown"}
    if manual_excluded or status.lower() in {"non_compliant", "haram", "excluded"}:
        state = "blocked"
        label_ar = "مستبعد شرعيًا"
    elif manual_approved or status.lower() in {"manual_approved", "compliant", "clean", "approved"}:
        state = "clean"
        label_ar = label or ("متوافق يدويًا" if manual_approved else "متوافق مبدئيًا")
    elif is_gray:
        state = "needs_review"
        label_ar = label or "يحتاج مراجعة شرعية"
    else:
        state = "unknown"
        label_ar = label or "شرعية غير مؤكدة"
    return {
        "status": status,
        "state": state,
        "label_ar": label_ar,
        "reason_ar": reason,
        "manual_approved": manual_approved,
        "manual_excluded": manual_excluded,
        "is_gray": is_gray,
        "rule_ar": "تظهر هنا للمراجعة قبل الافتتاح؛ لا يسمح التحديث بتحويل سهم مستبعد شرعيًا إلى فرصة تنفيذية.",
    }


def _preopen_audit_for_row(row: dict) -> dict[str, Any]:
    price = _price(row)
    levels = _prep_level_distances(row)
    flags = row.get("opportunity_flow_flags") if isinstance(row.get("opportunity_flow_flags"), dict) else {}
    trigger = _num(flags.get("trigger_price"), 0.0) or levels.get("trigger", 0.0) or _entry(row)
    support = levels.get("support", 0.0) or _stop(row)
    resistance = levels.get("resistance", 0.0)
    failure = support if support and support > 0 else _stop(row)
    trigger_dist = ((trigger - price) / price * 100.0) if price > 0 and trigger > 0 else 999.0
    failure_dist = ((price - failure) / price * 100.0) if price > 0 and failure > 0 else 999.0
    resistance_dist = ((resistance - price) / price * 100.0) if price > 0 and resistance > 0 else 999.0
    support_dist = _pct_distance(price, support) if price > 0 and support > 0 else 999.0
    sharia = _sharia_audit_for_row(row)
    return {
        "version": "pre_open_audit_v1_2026_06_20",
        "price": _round(price, 4),
        "sharia": sharia,
        "sharia_label_ar": sharia.get("label_ar"),
        "trigger_price": _round(trigger, 4),
        "trigger_distance_pct": _round(trigger_dist, 2) if trigger_dist != 999.0 else 999.0,
        "support_price": _round(support, 4),
        "support_distance_pct": _round(support_dist, 2) if support_dist != 999.0 else 999.0,
        "resistance_price": _round(resistance, 4),
        "resistance_distance_pct": _round(resistance_dist, 2) if resistance_dist != 999.0 else 999.0,
        "failure_price": _round(failure, 4),
        "failure_distance_pct": _round(failure_dist, 2) if failure_dist != 999.0 else 999.0,
        "rule_ar": "هذه بطاقة مراجعة قبل الافتتاح: شرعية + تفعيل + دعم/فشل + مقاومة. لا تعني شراء مباشر.",
    }


def _promotion_bridge_enabled(market_phase: str = "") -> tuple[bool, str]:
    phase = _s(market_phase).lower()
    if phase in {"open", "regular", "market_open"}:
        # Keep bridge available during the open too, but as monitoring/promotion context;
        # actual Strong/Cautious still come from the execution engine.
        return True, "regular_session_watch"
    if phase in {"pre_market", "premarket", "after_hours", "afterhours", "closed", "overnight", "weekend", "holiday", ""}:
        return True, phase or "unknown_closed_like"
    return True, f"unknown_phase:{phase}"


def _promotion_bridge_score(row: dict, source_section: str = "") -> tuple[float, str, str, list[str]]:
    price = _price(row)
    if price <= 0:
        return 0.0, "skip", "لا توجد ترقية", []
    if _is_blocked(row) or not _is_personal_section_eligible(row):
        return 0.0, "skip", "لا توجد ترقية", []
    flags = row.get("opportunity_flow_flags") if isinstance(row.get("opportunity_flow_flags"), dict) else {}
    audit = _preopen_audit_for_row(row)
    sharia_state = _s((audit.get("sharia") or {}).get("state"))
    if sharia_state == "blocked":
        return 0.0, "blocked_sharia", "مستبعد شرعيًا", ["مستبعد شرعيًا؛ لا يدخل جسر الترقية."]
    quality = _num(row.get("quality_score"), 0.0)
    readiness = _num(row.get("execution_readiness_score"), 0.0)
    rank = _num(row.get("opportunity_rank_score", row.get("display_rank_score", 0.0)), 0.0)
    change = _change_pct(row)
    move_risk = _move_risk_pct(row)
    liquidity_points = _num(flags.get("liquidity_score"), 0.0)
    if liquidity_points <= 0:
        liquidity_points, _ = _liquidity_score(row)
    rv = _num(row.get("effective_volume_ratio", row.get("volume_pace_ratio", row.get("volume_ratio", row.get("relative_volume", 0)))), 0.0)
    learning = row.get("learning_overlay_v1") if isinstance(row.get("learning_overlay_v1"), dict) else {}
    low_float = row.get("low_float_capture_v2m") if isinstance(row.get("low_float_capture_v2m"), dict) else _low_float_proxy_metrics(row)
    trigger_dist = _num(audit.get("trigger_distance_pct"), 999.0)
    resistance_dist = _num(audit.get("resistance_distance_pct"), 999.0)
    support_dist = _num(audit.get("support_distance_pct"), 999.0)
    failure_dist = _num(audit.get("failure_distance_pct"), 999.0)
    score = 0.0
    reasons: list[str] = []
    bucket = _s(row.get("opportunity_bucket"))
    if source_section:
        reasons.append(f"مصدر المرشح: {source_section}")
    if sharia_state == "clean":
        score += 8; reasons.append(_s(audit.get("sharia_label_ar")) or "شرعيًا: مقبول مبدئيًا")
    elif sharia_state in {"needs_review", "unknown"}:
        score -= 12; reasons.append(_s(audit.get("sharia_label_ar")) or "يحتاج مراجعة شرعية قبل التفعيل")
    if bucket in {"pre_trigger", "reclaim"} or source_section in {"pre_trigger_candidates", "reclaim_candidates"}:
        score += 22; reasons.append("قريب من مرحلة قابلة للترقية إذا أكد السعر والحجم")
    if bucket in {"low_float_premarket", "small_stock_classic"} or source_section in {"low_float_premarket_radar", "small_stock_classic_radar"}:
        score += 16; reasons.append("مرشح سهم صغير/Low-Float يحتاج مراقبة بري ماركت")
    if _s(learning.get("entry_bias")) in {"positive_watch", "watch_needs_volume", "speculative_watch"}:
        score += 10; reasons.append(_s(learning.get("label_ar")) or "نمط تعلم إيجابي/قابل للمتابعة")
    if low_float.get("confirmed_float") or low_float.get("small_cap_proxy"):
        score += 9; reasons.append(_s(low_float.get("label_ar")))
    elif low_float.get("proxy_candidate"):
        score += 5; reasons.append(_s(low_float.get("label_ar")))
    if 0 <= trigger_dist <= 1.25:
        score += 20; reasons.append(f"قريب جدًا من التفعيل {round(trigger_dist, 2)}%")
    elif 0 <= trigger_dist <= 3.0:
        score += 12; reasons.append(f"ضمن نطاق متابعة للتفعيل {round(trigger_dist, 2)}%")
    elif trigger_dist != 999.0 and trigger_dist > 5.0:
        score -= 4; reasons.append(f"بعيد عن التفعيل الآن {round(trigger_dist, 2)}%")
    if liquidity_points >= 30 or rv >= 2.0:
        score += 16; reasons.append(f"حجم/سيولة قوية نسبيًا RVOL {round(rv, 2)}x")
    elif liquidity_points >= 18 or rv >= 1.2:
        score += 9; reasons.append(f"الحجم مقبول للمتابعة RVOL {round(rv, 2)}x")
    else:
        reasons.append("يحتاج حجم بري ماركت/افتتاح أوضح قبل الترقية")
    if quality >= 70:
        score += 8; reasons.append(f"جودة فنية جيدة {round(quality, 1)}/100")
    elif quality >= 55:
        score += 4; reasons.append(f"جودة فنية مقبولة {round(quality, 1)}/100")
    if readiness >= 60:
        score += 7; reasons.append(f"جاهزية تنفيذ أولية {round(readiness, 1)}/100")
    if flags.get("no_chase") or (move_risk >= 10.0 and resistance_dist <= 2.0):
        score -= 22; reasons.append("مخاطرة مطاردة/قرب مقاومة؛ لا يترقى إلا بعد Pullback أو Reclaim")
    elif move_risk >= 7.0:
        score -= 8; reasons.append("تحرك مسبق؛ إدارة سريعة لا Runner افتراضي")
    if failure_dist != 999.0 and failure_dist <= 0.7:
        score -= 6; reasons.append("قريب من حد الفشل؛ يحتاج ثبات قبل الترقية")
    if support_dist != 999.0 and support_dist <= 2.0:
        score += 5; reasons.append(f"قريب من دعم مرجعي {round(support_dist, 2)}%")

    # State/action are labels only; they do not change execution decisions.
    if score >= 58 and (0 <= trigger_dist <= 2.0) and (liquidity_points >= 18 or rv >= 1.2) and not flags.get("no_chase"):
        state = "ready_for_cautious_if_live_confirms"
        action = "قابل للترقية إلى دخول بحذر إذا أكد البري ماركت/الافتتاح الحجم والإغلاق فوق التفعيل."
    elif score >= 46:
        state = "watch_for_trigger"
        action = "مرشح متابعة نشط: راقب التفعيل والحجم؛ ليس شراء مباشر الآن."
    elif flags.get("no_chase") or move_risk >= 10.0:
        state = "pullback_required"
        action = "لا تطارد؛ انتظر Pullback/Reclaim جديد قبل أي ترقية."
    else:
        state = "needs_volume_or_reclaim"
        action = "يبقى مراقبة: يحتاج حجم أو Reclaim أو اقتراب أوضح من التفعيل."
    return round(max(0.0, score), 2), state, action, _dedupe(reasons, 10)


def _make_promotion_bridge_row(row: dict, source_section: str = "") -> dict:
    score, state, action, reasons = _promotion_bridge_score(row, source_section=source_section)
    out = dict(row or {})
    audit = _preopen_audit_for_row(out)
    original_bucket = _s(out.get("opportunity_bucket"))
    source_label_map = {
        "pre_trigger_candidates": "قريب من التفعيل",
        "reclaim_candidates": "Reclaim",
        "low_float_premarket_radar": "Low-Float",
        "small_stock_classic_radar": "Small Classic",
        "support_bounce_candidates": "قرب دعم",
        "continuation_pullback_candidates": "Continuation/Pullback",
        "learning_opportunity_candidates": "طبقة التعلم",
    }
    label = "🧭 جسر الترقية قبل الافتتاح"
    if state == "ready_for_cautious_if_live_confirms":
        label = "🟠 جاهز للمراقبة — قد يصبح دخول بحذر"
    elif state == "watch_for_trigger":
        label = "🧭 راقب التفعيل والحجم"
    elif state == "pullback_required":
        label = "↩️ يحتاج Pullback قبل الترقية"
    elif state == "needs_volume_or_reclaim":
        label = "👀 يحتاج حجم/Reclaim"
    out["original_opportunity_bucket"] = original_bucket
    out["opportunity_bucket"] = "promotion_bridge"
    out["opportunity_stage"] = f"promotion_bridge_{state}"
    out["opportunity_stage_label"] = label
    out["display_plan_family_label"] = label
    out["decision"] = "جسر ترقية — ليس شراء مباشر"
    out["pre_open_audit_v2n"] = audit
    out["promotion_bridge_v2n"] = {
        "version": PREMARKET_PROMOTION_BRIDGE_VERSION,
        "source_section": source_section,
        "source_section_label_ar": source_label_map.get(source_section, source_section),
        "source_bucket": original_bucket,
        "state": state,
        "score": score,
        "action_ar": action,
        "reasons_ar": reasons,
        "applies_to_execution": False,
        "rule_ar": "يراقب المرشحين قبل الافتتاح ويحدد من قد يترقى لاحقًا؛ لا يغير Strong/Cautious بذاته.",
    }
    out["promotion_state"] = state
    out["promotion_status_ar"] = label
    out["promotion_action_ar"] = action
    out["promotion_trigger_price"] = audit.get("trigger_price")
    out["promotion_failure_price"] = audit.get("failure_price")
    out["sharia_preopen_label_ar"] = audit.get("sharia_label_ar")
    merged = _dedupe(reasons + [_s(action)] + (out.get("opportunity_reasons") if isinstance(out.get("opportunity_reasons"), list) else []), 12)
    out["opportunity_reasons"] = merged
    out["technical_explainer_reasons"] = merged
    out["why_appeared_ar"] = "، ".join(merged[:5])
    out["special_bucket_reason"] = out["why_appeared_ar"]
    out["opportunity_rank_score"] = round(max(_num(out.get("opportunity_rank_score"), 0.0), score), 2)
    out["non_actionable_prep"] = True
    return out


def _build_promotion_bridge_candidates(final_map: dict[str, list[dict]], market_phase: str = "", limit: int = DEFAULT_SECTION_LIMIT) -> tuple[list[dict], dict[str, Any]]:
    enabled, reason = _promotion_bridge_enabled(market_phase)
    priority_sources = [
        "pre_trigger_candidates",
        "reclaim_candidates",
        "low_float_premarket_radar",
        "small_stock_classic_radar",
        "support_bounce_candidates",
        "continuation_pullback_candidates",
        "learning_opportunity_candidates",
    ]
    debug: dict[str, Any] = {
        "version": PREMARKET_PROMOTION_BRIDGE_VERSION,
        "enabled": enabled,
        "reason": reason,
        "sources_seen": {},
        "candidate_count_before_limit": 0,
        "state_counts": {},
        "rule_ar": "يجمع أفضل مرشحي الأقسام التحضيرية ويضع لهم حالة ترقية قبل الافتتاح بدون تغيير Strong/Cautious.",
    }
    if not enabled:
        return [], debug
    candidates: list[dict] = []
    best_by_symbol: dict[str, dict] = {}
    for source in priority_sources:
        rows = final_map.get(source, []) or []
        debug["sources_seen"][source] = len(rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = _u(row.get("symbol"))
            if not sym or _is_blocked(row) or not _is_personal_section_eligible(row):
                continue
            score, state, action, reasons = _promotion_bridge_score(row, source_section=source)
            if state in {"skip", "blocked_sharia"} or score < 24:
                continue
            item = _make_promotion_bridge_row(row, source_section=source)
            prev = best_by_symbol.get(sym)
            if not prev or _num(item.get("opportunity_rank_score"), 0.0) > _num(prev.get("opportunity_rank_score"), 0.0):
                best_by_symbol[sym] = item
    candidates = _sort_bucket(list(best_by_symbol.values()))
    debug["candidate_count_before_limit"] = len(candidates)
    for item in candidates:
        state = _s(item.get("promotion_state"))
        debug["state_counts"][state] = debug["state_counts"].get(state, 0) + 1
    debug["candidate_symbols"] = [_u(x.get("symbol")) for x in candidates[:12] if _u(x.get("symbol"))]
    return candidates[:max(1, int(limit or DEFAULT_SECTION_LIMIT))], debug

def _fill_closed_market_prep_sections(final_map: dict[str, list[dict]], rows: list[dict], market_phase: str = "", limit: int = DEFAULT_SECTION_LIMIT) -> dict[str, Any]:
    enabled, reason = _closed_market_prep_enabled(market_phase)
    debug = {
        "version": CLOSED_MARKET_PREP_VERSION,
        "enabled": enabled,
        "reason": reason,
        "rows_seen": len(rows or []),
        "added_by_section": {},
        "candidate_hits_by_section": {},
        "skipped_duplicate_symbols": 0,
        "rule_ar": "في الإغلاق/قبل الافتتاح نملأ أقسام التحضير من Watch/Early إذا كانت شروط التنفيذ الحية غير مكتملة. لا يغير Strong/Cautious.",
    }
    if not enabled:
        return debug
    section_candidates: dict[str, list[dict]] = {k: [] for k in PREP_SECTION_TO_BUCKET}
    existing_symbols_by_section: dict[str, set[str]] = {}
    global_specific_seen: set[str] = set()
    for key, vals in (final_map or {}).items():
        syms = {_u(v.get("symbol")) for v in (vals or []) if isinstance(v, dict) and _u(v.get("symbol"))}
        existing_symbols_by_section[key] = syms
        if key != "learning_opportunity_candidates":
            global_specific_seen.update(syms)

    for row in rows or []:
        if not isinstance(row, dict) or _is_blocked(row) or not _is_personal_section_eligible(row):
            continue
        sym = _u(row.get("symbol"))
        if not sym:
            continue
        for section, score, reasons in _prep_candidate_sections(row):
            debug["candidate_hits_by_section"][section] = debug["candidate_hits_by_section"].get(section, 0) + 1
            # Avoid exact duplicates in the same section; allow a symbol in one
            # existing specialized section to remain there rather than being copied.
            if sym in existing_symbols_by_section.get(section, set()):
                debug["skipped_duplicate_symbols"] += 1
                continue
            # If a symbol is already in a stronger specific prep section, do not
            # spread it everywhere; keep one or two clear places max.
            current_section_items = section_candidates.get(section, [])
            already_prepped_elsewhere = any(_u(x.get("symbol")) == sym for k, vals in section_candidates.items() if k != section for x in vals)
            # V2M: allow Small Classic and Low-Float to be visible even when the
            # same symbol is also in Learning/Pre-trigger. The user must be able
            # to audit small-stock and low-float candidates before the open.
            duplicate_allowed_sections = {"catalyst_watch", "gap_fill_watch", "small_stock_classic_radar", "low_float_premarket_radar"}
            if sym in global_specific_seen and section not in duplicate_allowed_sections:
                debug["skipped_duplicate_symbols"] += 1
                continue
            if already_prepped_elsewhere and section not in duplicate_allowed_sections:
                continue
            current_section_items.append(_make_closed_market_prep_row(row, section, score, reasons, market_phase))

    for section, candidates in section_candidates.items():
        if not candidates:
            debug["added_by_section"][section] = 0
            continue
        existing = final_map.get(section, []) or []
        seen = {_u(x.get("symbol")) for x in existing if isinstance(x, dict)}
        to_add = []
        for item in _sort_bucket(candidates):
            sym = _u(item.get("symbol"))
            if not sym or sym in seen:
                continue
            seen.add(sym)
            to_add.append(item)
            if len(existing) + len(to_add) >= max(1, int(limit or DEFAULT_SECTION_LIMIT)):
                break
        final_map[section] = _sort_bucket(existing + to_add)[:max(1, int(limit or DEFAULT_SECTION_LIMIT))]
        debug["added_by_section"][section] = len(to_add)
    debug["total_added"] = sum(debug["added_by_section"].values())
    return debug


def build_opportunity_radar_sections(rows: list[dict], market_phase: str = "", limit: int = DEFAULT_SECTION_LIMIT) -> dict:
    bucket_map = {key: [] for key in OPPORTUNITY_BUCKET_KEYS}
    raw_counts: dict[str, int] = {}
    suppressed_high_price: list[str] = []
    for row in rows or []:
        if not isinstance(row, dict) or _is_blocked(row):
            continue
        bucket = _s(row.get("opportunity_bucket"))
        if bucket:
            raw_counts[bucket] = raw_counts.get(bucket, 0) + 1
        if not _is_personal_section_eligible(row):
            sym = _u(row.get("symbol"))
            if sym:
                suppressed_high_price.append(sym)
            continue
        if bucket == "support_bounce":
            bucket_map["support_bounce_candidates"].append(row)
        elif bucket == "reclaim":
            bucket_map["reclaim_candidates"].append(row)
        elif bucket == "pre_trigger":
            bucket_map["pre_trigger_candidates"].append(row)
        elif bucket == "continuation_pullback":
            bucket_map["continuation_pullback_candidates"].append(row)
        elif bucket == "small_stock_classic":
            bucket_map["small_stock_classic_radar"].append(row)
        elif bucket == "high_risk_day_trade":
            bucket_map["high_risk_day_trades"].append(row)
        elif bucket == "low_float_premarket":
            bucket_map["low_float_premarket_radar"].append(row)
        elif bucket == "gap_fill_watch":
            bucket_map["gap_fill_watch"].append(row)
        elif bucket == "catalyst_watch":
            bucket_map["catalyst_watch"].append(row)

    # Keep sections distinct: if a symbol is in a more specific high-priority stage,
    # do not repeat it in lower-information sections.
    ordered_keys = [
        "promotion_bridge_candidates",
        "learning_opportunity_candidates",
        "small_stock_classic_radar",
        "pre_trigger_candidates",
        "support_bounce_candidates",
        "reclaim_candidates",
        "continuation_pullback_candidates",
        "low_float_premarket_radar",
        "high_risk_day_trades",
        "gap_fill_watch",
        "catalyst_watch",
    ]
    seen: set[str] = set()
    final_map: dict[str, list[dict]] = {}
    for key in ordered_keys:
        items = []
        for row in _sort_bucket(bucket_map.get(key, [])):
            sym = _u(row.get("symbol"))
            if not sym or sym in seen:
                continue
            seen.add(sym)
            items.append(row)
            if len(items) >= max(1, int(limit or 25)):
                break
        final_map[key] = items

    # V2k2 bridge: when the live tool only shows Watch/Early Movement, expose
    # non-execution learning/prep candidates in their own visible section.
    specific_seen = set()
    for k, vals in final_map.items():
        if k == "learning_opportunity_candidates":
            continue
        for r in vals or []:
            sym = _u(r.get("symbol")) if isinstance(r, dict) else ""
            if sym:
                specific_seen.add(sym)
    learning_bridge_rows, learning_bridge_debug = _build_learning_opportunity_bridge(rows or [], specific_seen, limit=limit)
    final_map["learning_opportunity_candidates"] = learning_bridge_rows

    closed_market_planning_debug = _fill_closed_market_prep_sections(final_map, rows or [], market_phase=market_phase, limit=limit)
    promotion_bridge_rows, promotion_bridge_debug = _build_promotion_bridge_candidates(final_map, market_phase=market_phase, limit=limit)
    final_map["promotion_bridge_candidates"] = promotion_bridge_rows
    low_float_capture = _low_float_capture_debug(rows or [], final_map.get("low_float_premarket_radar", []))

    counts = {f"{key}_count": len(final_map.get(key, [])) for key in ordered_keys}
    next_week_analysis = _build_next_week_analysis(final_map, counts)
    learning_overlay_candidates = _build_visible_learning_overlay_candidates(rows or [], limit=16)
    return {
        "ok": True,
        "version": OPPORTUNITY_RADAR_VERSION,
        "market_phase": market_phase,
        "display_limit_per_section": max(1, int(limit or DEFAULT_SECTION_LIMIT)),
        "rule_ar": "Strong يبقى صارمًا؛ أثناء الإغلاق/قبل الافتتاح تظهر أقسام تحضيرية لمراجعة الفرص والمقاومة والدعم بدون تحويلها إلى BUY_NOW.",
        "counts_by_stage": raw_counts,
        "suppressed_high_price_count": len(set(suppressed_high_price)),
        "suppressed_high_price_symbols_sample": _dedupe(suppressed_high_price, 20),
        "high_price_rule_ar": "الأسهم فوق 150$ تُخفى من الأقسام العملية إلا إذا كانت فرصة استثنائية من حيث الجودة والجاهزية والسيولة.",
        "learning_overlay_summary": _learning_overlay_summary(),
        "learning_overlay_candidates": learning_overlay_candidates,
        "learning_overlay_candidates_count": int((learning_overlay_candidates or {}).get("positive_count", 0) or 0) + int((learning_overlay_candidates or {}).get("quick_take_profit_count", 0) or 0) + int((learning_overlay_candidates or {}).get("weak_or_mixed_count", 0) or 0) + int((learning_overlay_candidates or {}).get("sample_only_count", 0) or 0),
        "learning_bridge_debug": learning_bridge_debug,
        "learning_bridge_rule_ar": learning_bridge_debug.get("rule_ar"),
        "closed_market_opportunity_mode": closed_market_planning_debug,
        "closed_market_prep_enabled": bool(closed_market_planning_debug.get("enabled")),
        "closed_market_prep_added_count": int(closed_market_planning_debug.get("total_added", 0) or 0),
        "closed_market_prep_rule_ar": closed_market_planning_debug.get("rule_ar"),
        "low_float_capture_debug": low_float_capture,
        "low_float_capture_rule_ar": low_float_capture.get("rule_ar"),
        "promotion_bridge_debug": promotion_bridge_debug,
        "promotion_bridge_rule_ar": promotion_bridge_debug.get("rule_ar"),
        "promotion_bridge_candidates_count": len(promotion_bridge_rows),
        "promotion_bridge_candidates": promotion_bridge_rows,
        "next_week_analysis": next_week_analysis,
        "next_week_watchlist": next_week_analysis.get("top_candidates", []),
        "next_week_analysis_count": len(next_week_analysis.get("top_candidates", [])),
        **counts,
        **final_map,
    }


def _plan_store() -> dict[str, dict]:
    data = get_json(PLAN_MEMORY_KEY, {}) or {}
    return data if isinstance(data, dict) else {}


def _save_plan_store(data: dict[str, dict]) -> None:
    if len(data) > 500:
        items = sorted(data.items(), key=lambda kv: _num(kv[1].get("created_ts"), 0.0))[-350:]
        data = dict(items)
    set_json(PLAN_MEMORY_KEY, data)


def _append_events(events: list[dict]) -> None:
    if not events:
        return
    hist = get_json(PLAN_EVENTS_KEY, []) or []
    if not isinstance(hist, list):
        hist = []
    hist.extend(events)
    if len(hist) > 1500:
        hist = hist[-900:]
    set_json(PLAN_EVENTS_KEY, hist)


def _make_memory_plan(row: dict, source: str = "") -> dict:
    sym = _u(row.get("symbol"))
    ts = time.time()
    reasons = _dedupe(list(row.get("opportunity_reasons") or []) + list(row.get("technical_explainer_reasons") or []) + list(row.get("final_decision_blockers") or []), 10)
    return {
        "plan_id": f"{sym}:{_today()}:{int(ts)}",
        "symbol": sym,
        "status": "active",
        "created_at": _now_text(),
        "created_ts": ts,
        "last_seen_at": _now_text(),
        "last_seen_ts": ts,
        "source": source,
        "original_decision": _s(row.get("decision")),
        "original_final_code": _s(row.get("final_decision_code")),
        "original_stage": _s(row.get("opportunity_stage")),
        "original_stage_label": _s(row.get("opportunity_stage_label")),
        "original_bucket": _s(row.get("opportunity_bucket")),
        "alert_price": _round(_price(row), 4),
        "entry": _round(_entry(row), 4),
        "trigger": _round((row.get("opportunity_flow_flags") or {}).get("trigger_price", 0) if isinstance(row.get("opportunity_flow_flags"), dict) else _entry(row), 4),
        "stop": _round(_stop(row), 4),
        "target_1": _round(_target1(row), 4),
        "support_resistance_summary": _s((row.get("support_resistance_zones_v2") or {}).get("summary_ar") if isinstance(row.get("support_resistance_zones_v2"), dict) else row.get("levels_summary")),
        "reasons": reasons,
        "max_price_seen": _round(_price(row), 4),
        "min_price_seen": _round(_price(row), 4),
        "seen_count": 1,
    }


def _evaluate_memory_plan(plan: dict, row: dict) -> dict:
    price = _price(row)
    entry = _num(plan.get("entry"), 0.0)
    trigger = _num(plan.get("trigger"), 0.0) or entry
    stop = _num(plan.get("stop"), 0.0)
    target = _num(plan.get("target_1"), 0.0)
    status = _s(plan.get("status") or "active")
    action = "الخطة الأصلية ما زالت تحت المتابعة."
    reason = "active"
    if price > 0:
        plan["last_price"] = _round(price, 4)
        plan["max_price_seen"] = max(_num(plan.get("max_price_seen"), price), _round(price, 4))
        min_seen = _num(plan.get("min_price_seen"), price)
        plan["min_price_seen"] = _round(price, 4) if min_seen <= 0 else min(min_seen, _round(price, 4))
    if price <= 0:
        status = "unknown_price"
        reason = "price_missing"
        action = "الخطة الأصلية محفوظة لكن السعر الحالي غير متوفر."
    elif stop > 0 and price <= stop:
        status = "failed_stop"
        reason = "stop_broken"
        action = f"🔴 فشل خطة: كسر الوقف الأصلي {round(stop, 2)}."
    elif target > 0 and price >= target:
        status = "target_1_hit"
        reason = "target_hit"
        action = f"✅ وصلت الهدف الأول الأصلي {round(target, 2)} — قيّم تأمين الربح."
    elif _s(plan.get("original_bucket")) == "support_bounce" and isinstance(row.get("opportunity_flow_flags"), dict) and row.get("opportunity_flow_flags", {}).get("extended_after_move"):
        status = "extended_after_support_bounce"
        reason = "moved_near_resistance"
        action = "🟡 لم تعد Support Bounce مبكرة؛ السهم تحرك واقترب من مقاومة/منطقة قرار، فانتظر Pullback أو Reclaim جديد."
    elif trigger > 0 and price < trigger * 0.992 and _s(plan.get("original_bucket")) in {"pre_trigger", "reclaim"}:
        status = "needs_reclaim_or_trigger"
        reason = "trigger_lost"
        action = f"⚠️ الخطة الأصلية تحتاج استعادة/تفعيل فوق {round(trigger, 2)} قبل أي إضافة."
    elif entry > 0 and price < entry * 0.985 and _s(plan.get("original_decision")) in {"دخول قوي", "دخول بحذر"}:
        status = "under_original_entry"
        reason = "under_entry"
        action = f"⚠️ السعر تحت دخول الخطة الأصلية {round(entry, 2)} — لا تبنِ خطة جديدة قبل استعادة المستوى."
    elif entry > 0 and price > entry * 1.055 and target <= 0:
        status = "extended_from_original_entry"
        reason = "extended"
        action = "⚠️ ابتعد السعر عن الخطة الأصلية؛ لا تطارد، انتظر Pullback أو Reclaim."
    else:
        status = "active"
        reason = "still_valid"
        action = "🟢 الخطة الأصلية ما زالت نشطة ما لم يكسر السعر الوقف/مستوى الفشل."
    return {"status": status, "reason": reason, "action": action}


def _should_record(row: dict) -> bool:
    if not isinstance(row, dict) or _is_blocked(row):
        return False
    pf = row.get("personal_price_filter")
    if not isinstance(pf, dict):
        pf = _price_filter(row)
    if _s(pf.get("bucket")) == "high_price_deprioritized" and not pf.get("memory_eligible"):
        return False
    decision = _s(row.get("decision"))
    bucket = _s(row.get("opportunity_bucket"))
    if decision in {"دخول قوي", "دخول بحذر"}:
        return True
    return bucket in {"pre_trigger", "support_bounce", "reclaim", "small_stock_classic", "low_float_premarket", "high_risk_day_trade", "continuation_pullback"}


def _deprioritize_existing_high_price_plan(store: dict[str, dict], row: dict) -> bool:
    sym = _u(row.get("symbol"))
    if not sym or sym not in store:
        return False
    reason = _high_price_suppression_reason(row)
    if not reason:
        return False
    plan = store.get(sym)
    if not isinstance(plan, dict):
        return False
    plan["status"] = "deprioritized_high_price"
    plan["last_status_reason"] = "personal_price_filter"
    plan["last_action"] = "🟡 أُخفيت من الفرص العملية لأنها فوق 150$ وليست استثنائية حاليًا حسب الجودة/الجاهزية/السيولة."
    plan["last_seen_at"] = _now_text()
    plan["last_seen_ts"] = time.time()
    store[sym] = plan
    return True

def record_opportunity_plans(rows: list[dict], source: str = "") -> dict:
    store = _plan_store()
    events: list[dict] = []
    recorded, updated = [], []
    for row in rows or []:
        sym = _u(row.get("symbol")) if isinstance(row, dict) else ""
        if not _should_record(row):
            if isinstance(row, dict) and sym and _deprioritize_existing_high_price_plan(store, row):
                updated.append(sym)
            continue
        if not sym:
            continue
        current = store.get(sym)
        if isinstance(current, dict) and _s(current.get("status")) in ACTIVE_MEMORY_STATUSES.union({"target_1_hit"}):
            ev = _evaluate_memory_plan(current, row)
            current["status"] = ev["status"]
            current["last_status_reason"] = ev["reason"]
            current["last_action"] = ev["action"]
            current["last_seen_at"] = _now_text()
            current["last_seen_ts"] = time.time()
            current["seen_count"] = int(current.get("seen_count", 0) or 0) + 1
            store[sym] = current
            updated.append(sym)
        else:
            plan = _make_memory_plan(row, source=source)
            store[sym] = plan
            events.append({"event": "opportunity_plan_created", "symbol": sym, "at": plan["created_at"], "source": source, "stage": plan.get("original_stage"), "decision": plan.get("original_decision"), "price": plan.get("alert_price"), "entry": plan.get("entry"), "stop": plan.get("stop"), "target_1": plan.get("target_1")})
            recorded.append(sym)
    _save_plan_store(store)
    _append_events(events)
    return {"ok": True, "version": OPPORTUNITY_RADAR_VERSION, "recorded": recorded, "updated": updated, "active_count": len(store)}


def enrich_rows_with_opportunity_plan_memory(rows: list[dict]) -> list[dict]:
    store = _plan_store()
    changed: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sym = _u(row.get("symbol"))
        plan = store.get(sym)
        if not isinstance(plan, dict):
            continue
        old = _s(plan.get("status"))
        ev = _evaluate_memory_plan(plan, row)
        plan["status"] = ev["status"]
        plan["last_status_reason"] = ev["reason"]
        plan["last_action"] = ev["action"]
        plan["last_seen_at"] = _now_text()
        plan["last_seen_ts"] = time.time()
        store[sym] = plan
        if ev["status"] != old:
            changed.append({"event": "opportunity_plan_status_changed", "symbol": sym, "from": old, "to": ev["status"], "at": _now_text(), "reason": ev["reason"], "price": _round(_price(row), 4)})
        row["opportunity_plan_memory_version"] = OPPORTUNITY_RADAR_VERSION
        row["original_plan"] = {
            "plan_id": plan.get("plan_id"),
            "created_at": plan.get("created_at"),
            "original_decision": plan.get("original_decision"),
            "original_stage_label": plan.get("original_stage_label"),
            "alert_price": plan.get("alert_price"),
            "entry": plan.get("entry"),
            "trigger": plan.get("trigger"),
            "stop": plan.get("stop"),
            "target_1": plan.get("target_1"),
            "reasons": plan.get("reasons", []),
            "support_resistance_summary": plan.get("support_resistance_summary", ""),
        }
        row["current_plan_state"] = {
            "status": ev["status"],
            "reason": ev["reason"],
            "action": ev["action"],
            "last_price": _round(_price(row), 4),
            "max_price_seen": plan.get("max_price_seen"),
            "min_price_seen": plan.get("min_price_seen"),
        }
        row["live_plan_action"] = row.get("live_plan_action") or ev["action"]
        row["live_plan_reason"] = row.get("live_plan_reason") or ev["reason"]
        if ev["status"] == "failed_stop":
            row["decision"] = "مراقبة"
            row["effective_decision"] = "مراقبة"
            row["final_decision_code"] = "PLAN_BROKEN"
            row["final_decision_label"] = "الخطة الأصلية فشلت"
            row["owner_action"] = ev["action"]
    if changed:
        _append_events(changed)
    _save_plan_store(store)
    return rows


def opportunity_plan_memory_status(limit: int = 100) -> dict:
    store = _plan_store()
    plans = list(store.values())
    plans.sort(key=lambda p: _num(p.get("created_ts"), 0.0), reverse=True)
    hist = get_json(PLAN_EVENTS_KEY, []) or []
    if not isinstance(hist, list):
        hist = []
    active_plans = [p for p in plans if _s(p.get("status")) in ACTIVE_MEMORY_STATUSES]
    return {
        "ok": True,
        "version": OPPORTUNITY_RADAR_VERSION,
        "active_count": len(active_plans),
        "total_saved_count": len(plans),
        "plans": plans[:max(1, int(limit or 100))],
        "recent_events": hist[-50:],
        "rule_ar": "تُحفظ خطط Strong/Cautious/Pre-Trigger/Support Bounce/Reclaim حتى لا تعيد الأداة اختراع خطة جديدة بعد تغير السعر، مع إخفاء خطط الأسهم فوق 150$ إذا لم تعد استثنائية.",
    }


def build_position_aware_snapshot(holding: dict, plan: dict) -> dict:
    buy = _num(holding.get("buy_price"), 0.0)
    qty = _num(holding.get("quantity"), 0.0)
    current = _price(plan)
    pnl_pct = ((current - buy) / buy * 100.0) if buy > 0 and current > 0 else 0.0
    zones = build_support_resistance_zones(plan)
    ns = zones.get("nearest_support_zone") or {}
    nr = zones.get("nearest_resistance_zone") or {}
    stop = _stop(plan) or (buy * 0.97 if buy > 0 else 0.0)
    target = _target1(plan) or (buy * 1.06 if buy > 0 else 0.0)
    action = "احتفاظ ومتابعة الخطة."
    status = "holding_watch"
    if current > 0 and stop > 0 and current <= stop:
        status = "risk_exit"
        action = f"🔴 السعر عند/تحت الوقف المنطقي {round(stop, 2)} — خفف أو اخرج حسب خطتك."
    elif target > 0 and current >= target:
        status = "protect_profit"
        action = f"✅ وصل الهدف الأول {round(target, 2)} — أمّن جزءًا من الربح."
    elif buy > 0 and pnl_pct >= 3.0 and nr:
        status = "profit_near_resistance"
        action = f"🟢 رابح {round(pnl_pct, 2)}%؛ راقب المقاومة {nr.get('low')} - {nr.get('high')} لتأمين الربح."
    elif buy > 0 and pnl_pct < -3.0:
        status = "position_in_risk"
        action = "⚠️ المركز تحت سعر الدخول؛ لا تضف قبل استعادة مستوى الخطة أو ظهور Reclaim واضح."
    elif ns:
        status = "holding_above_support"
        action = f"🟢 ما زال فوق منطقة دعم {ns.get('low')} - {ns.get('high')}؛ كسرها بحجم يضعف الخطة."
    return {
        "version": OPPORTUNITY_RADAR_VERSION,
        "buy_price": _round(buy, 4),
        "quantity": _round(qty, 4),
        "current_price": _round(current, 4),
        "pnl_pct": _round(pnl_pct, 2),
        "status": status,
        "action_ar": action,
        "logical_stop": _round(stop, 4),
        "target_1": _round(target, 4),
        "support_zone": ns,
        "resistance_zone": nr,
        "levels_summary": zones.get("summary_ar", ""),
        "rule_ar": "هذا التحليل يبدأ من سعر شرائك، لا من خطة جديدة كأنك خارج السهم.",
    }


def opportunity_radar_status_payload(rows: list[dict] | None = None) -> dict:
    rows = rows or []
    enriched_count = len([r for r in rows if isinstance(r, dict) and r.get("opportunity_radar_version") == OPPORTUNITY_RADAR_VERSION])
    return {
        "ok": True,
        "version": OPPORTUNITY_RADAR_VERSION,
        "enriched_rows_in_payload": enriched_count,
        "sections": OPPORTUNITY_BUCKET_KEYS,
        "personal_price_filter": {
            "comfortable_under": PERSONAL_PRICE_COMFORT,
            "acceptable_until": PERSONAL_PRICE_MAX_NORMAL,
            "above_rule_ar": "فوق 150$ لا يدخل الأقسام العملية ولا Plan Memory إلا إذا كان استثنائيًا جدًا من حيث الجودة والجاهزية والسيولة.",
            "exception_rule_ar": "الاستثناء يحتاج عادة جودة >= 90 تقريبًا + جاهزية عالية + سيولة واضحة، أو Strong BUY_NOW مكتمل.",
        },
        "display_limit_per_section_default": DEFAULT_SECTION_LIMIT,
        "small_stock_classic_rule_ar": "للأسهم الصغيرة: قرب الدعم والمقاومة طبيعي؛ لا نعامل فروقات السنت كقرار منفصل. نعتمد Fib 61.8/78.6، VWAP بإغلاق شمعة 5د/15د، قمة أمس، أو اختراق واضح لمنطقة صغيرة، ولا نطارد الشمعة الخضراء.",
        "low_float_capture_rule_ar": "V2O يصلح مصدر Low-Float: لا يعتمد على Watch/Early فقط، ويستبعد الأسماء المعروفة/الأكثر سيولة من Low-Float إذا لم يوجد float أو Fast Lane مستقل.",
        "promotion_bridge_rule_ar": "V2N/V2O يضيف جسر ترقية قبل الافتتاح: يقرأ Low-Float/Small Classic/Pre-Trigger/Support ويحدد من قد يترقى عند تحقق الحجم والسعر، بدون تغيير Strong/Cautious.",
        "storage_rule_ar": "لا يخزن هذا الإصدار raw Polygon/FMP؛ فقط ذاكرة خطط مختصرة في SQLite KV.",
    }
