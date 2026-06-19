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

OPPORTUNITY_RADAR_VERSION = "opportunity_radar_rebuild_v1c_2026_06_19"
NY_TZ = ZoneInfo("America/New_York")
PLAN_MEMORY_KEY = "opportunity_radar:plan_memory_v1"
PLAN_EVENTS_KEY = "opportunity_radar:plan_memory_events_v1"

PERSONAL_PRICE_COMFORT = 50.0
PERSONAL_PRICE_MAX_NORMAL = 150.0
DEFAULT_SECTION_LIMIT = 12
ACTIVE_MEMORY_STATUSES = {"active", "unknown_price", "needs_reclaim_or_trigger", "under_original_entry", "extended_from_original_entry"}


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


def _level_merge_threshold(price: float, atr: float) -> float:
    if price <= 0:
        return 0.05
    tick_component = 0.05 if price >= 10 else 0.02
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

    if price_zone and price_zone.get("kind") == "congestion":
        notes.append("السعر داخل منطقة ضيقة؛ لا يُبنى قرار مستقل من فروقات سنتات داخلها.")
    if not zones:
        notes.append("لا توجد مستويات كافية لبناء مناطق دعم/مقاومة موثوقة من البيانات الحالية.")

    summary_bits = []
    if price_zone and price_zone.get("kind") == "congestion":
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
    extended_after_move = bool((change >= 4.5 or from_open >= 4.0) and (near_resistance_now or in_upper_congestion or close_pos >= 70))
    structural_support_near = bool(price > 0 and ns and (ns.get("low", 0) <= price <= ns.get("high", 0) * 1.012 or 0 <= support_dist <= max(2.2, atr_pct * 0.75)))
    lower_decision_zone_bounce = bool(_s(pz.get("kind")) == "congestion" and pz_low > 0 and pz_pos <= 0.35 and change <= 2.0)
    near_support_raw = bool(structural_support_near or lower_decision_zone_bounce)
    near_support = bool(near_support_raw and not extended_after_move and change < 4.5)
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
    low_float_pm = bool(low_price and (high_activity or _num(row.get("pre_market_volume"), 0.0) > 100_000 or _num(row.get("pre_market_change_pct"), 0.0) >= 2.0))
    if extended_after_move and low_price:
        high_risk_day = True

    gap_up = _num(row.get("open_gap_pct", row.get("gap_from_prev_close_pct", 0)), 0.0)
    gap_watch = bool(abs(gap_up) >= 2.5 or row.get("gap_fill_candidate") or row.get("gap_retest_success") or row.get("gap_fade_flag"))

    news_context = " ".join([_s(row.get("news_badge")), _s(row.get("news_title")), _s(row.get("news_category")), _s(row.get("news_scope")), _s(row.get("news_context_note"))]).lower()
    catalyst_keywords = ["fda", "clinical", "trial", "earnings", "merger", "acquisition", "upgrade", "downgrade", "price target", "contract", "approval", "biotech", "عقد", "ترقية", "أرباح", "اندماج", "استحواذ"]
    catalyst = bool(_s(row.get("news_badge")) and any(k in news_context for k in catalyst_keywords + ["positive", "negative", "legal"]))

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
        "extended_after_move": extended_after_move,
        "near_resistance_now": near_resistance_now,
        "gap_watch": gap_watch,
        "catalyst": catalyst,
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
    if flags.get("extended_after_move") and flags.get("high_risk_day"):
        return "high_risk_day_trade", "⚡ مضاربة عالية المخاطرة", "high_risk_day_trade", ["تحرك قوي وقريب من مقاومة/منطقة قرار؛ لا يصنف Support Bounce ولا يُطارد."]
    if flags.get("pre_trigger") and not flags.get("extended_after_move"):
        return "pre_trigger", "⏳ قريب من التفعيل", "pre_trigger", flags.get("pre_trigger_reasons", [])
    if flags.get("reclaim"):
        label = "🟢 Reclaim مؤكد" if flags.get("reclaim_confirmed") else "🔁 Reclaim يحتاج ثبات"
        return "reclaim", label, "reclaim", flags.get("reclaim_reasons", [])
    if flags.get("near_support"):
        return "support_bounce", "↩️ بدأ ارتداد / قريب من دعم", "support_bounce", flags.get("support_reasons", [])
    if flags.get("high_risk_day"):
        base = "سهم صغير متحرك؛ يعامل كحجم صغير عالي المخاطرة لا كدخول قوي عادي."
        if flags.get("extended_after_move"):
            base = "تحرك قوي وقريب من مقاومة/منطقة قرار؛ لا يصنف Support Bounce ولا يُطارد."
        return "high_risk_day_trade", "⚡ مضاربة عالية المخاطرة", "high_risk_day_trade", [base]
    if flags.get("low_float_pm"):
        return "low_float_premarket", "🚀 مرشح Low-Float / بري ماركت", "low_float_premarket", ["سهم صغير/نشط يحتاج مراقبة مبكرة وليس Strong عادي."]
    if flags.get("continuation_pullback"):
        return "continuation_pullback", "📈 Continuation Pullback Candidate", "continuation_pullback", ["استمرار مشروط؛ لا تطارد القمة وانتظر Pullback صحي."]
    if flags.get("gap_watch"):
        return "gap_fill_watch", "🕳️ Gap Fill Watch", "gap_fill_watch", ["توجد فجوة أو إعادة اختبار فجوة؛ ليست كل فجوة يجب أن تغلق."]
    if flags.get("catalyst"):
        return "catalyst_watch", "📰 Catalyst / News Watch", "catalyst_watch", ["يوجد سياق خبر/محفز؛ القرار ليس شراء مباشر من الخبر وحده."]
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
    merged_reasons = _dedupe(stage_reasons + technical_reasons + high_price_note, 12)
    base_extra = 0.0
    if bucket == "support_bounce":
        base_extra = flags.get("support_score", 0.0)
    elif bucket == "reclaim":
        base_extra = flags.get("reclaim_score", 0.0)
    elif bucket == "pre_trigger":
        base_extra = flags.get("pre_trigger_score", 0.0)
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
    out["opportunity_rank_score"] = _bucket_rank(out, base=base_extra)
    out["opportunity_flow_flags"] = flags
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
    if bucket in {"support_bounce", "reclaim", "pre_trigger", "continuation_pullback", "gap_fill_watch", "catalyst_watch", "low_float_premarket", "high_risk_day_trade"}:
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

    counts = {f"{key}_count": len(final_map.get(key, [])) for key in ordered_keys}
    return {
        "ok": True,
        "version": OPPORTUNITY_RADAR_VERSION,
        "market_phase": market_phase,
        "display_limit_per_section": max(1, int(limit or DEFAULT_SECTION_LIMIT)),
        "rule_ar": "Strong يبقى صارمًا؛ هذه الأقسام تعيد الحياة للمراحل التي تسبق Strong بدون تحويلها إلى BUY_NOW.",
        "counts_by_stage": raw_counts,
        "suppressed_high_price_count": len(set(suppressed_high_price)),
        "suppressed_high_price_symbols_sample": _dedupe(suppressed_high_price, 20),
        "high_price_rule_ar": "الأسهم فوق 150$ تُخفى من الأقسام العملية إلا إذا كانت فرصة استثنائية من حيث الجودة والجاهزية والسيولة.",
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
    return bucket in {"pre_trigger", "support_bounce", "reclaim", "low_float_premarket", "high_risk_day_trade", "continuation_pullback"}


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
        "storage_rule_ar": "لا يخزن هذا الإصدار raw Polygon/FMP؛ فقط ذاكرة خطط مختصرة في SQLite KV.",
    }
