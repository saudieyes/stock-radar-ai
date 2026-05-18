"""Opportunity Intelligence V1.

This module adds a conservative read-only intelligence layer on top of the
existing Stock Radar scoring. It does not change Sharia decisions and it does
not create new Strong/Cautious classifications. It only adds warnings, quality
sub-tiers, no-chase labels, and a safer display/ranking adjustment.
"""
from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return float(value)
    except Exception:
        return default


def _s(value: Any) -> str:
    return str(value or "").strip()


def _lst(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x or "").strip()]
    if isinstance(value, tuple):
        return [str(x).strip() for x in value if str(x or "").strip()]
    return []


def _add_flag(stock: dict, flag: str) -> None:
    if not flag:
        return
    flags = _lst(stock.get("risk_flags"))
    if flag not in flags:
        flags.append(flag)
    stock["risk_flags"] = flags[:18]


def _pct_distance(price: float, level: float) -> float:
    if price > 0 and level > 0:
        return ((level - price) / price) * 100.0
    return 0.0


def _nearest_distance(stock: dict, key: str, fallback_level_key: str, price: float) -> float:
    raw = _f(stock.get(key), 0.0)
    if raw:
        return raw
    return _pct_distance(price, _f(stock.get(fallback_level_key), 0.0))


def _first_level_below(stock: dict, price: float, exclude_level: float = 0.0) -> float:
    """Return the closest real support level below the live price.

    Live quotes may move below a support computed at analysis time. When that
    happens, the old support is broken and should not be displayed as the active
    nearest support; this helper looks for the next support below.
    """
    if price <= 0:
        return 0.0
    candidates: list[float] = []
    for key in ("support_levels_below", "support_levels", "key_support_levels", "nearby_support_levels"):
        vals = stock.get(key)
        if not isinstance(vals, (list, tuple)):
            continue
        for v in vals:
            n = _f(v, 0.0)
            if n > 0 and n < price * 0.999 and (exclude_level <= 0 or abs(n - exclude_level) / max(exclude_level, 1.0) > 0.001):
                candidates.append(n)
    if not candidates:
        return 0.0
    return max(candidates)


def _first_level_above(stock: dict, price: float, exclude_level: float = 0.0) -> float:
    """Return the closest real resistance level above the live price.

    V4f guard: when live price moves above the previous nearest resistance, that
    level becomes a reclaimed/broken resistance, not the active nearest
    resistance. This helper finds the next level above live price.
    """
    if price <= 0:
        return 0.0
    candidates: list[float] = []
    for key in ("resistance_levels_above", "resistance_levels", "key_resistance_levels", "nearby_resistance_levels"):
        vals = stock.get(key)
        if not isinstance(vals, (list, tuple)):
            continue
        for v in vals:
            n = _f(v, 0.0)
            if n > price * 1.001 and (exclude_level <= 0 or abs(n - exclude_level) / max(exclude_level, 1.0) > 0.001):
                candidates.append(n)
    for key in ("major_resistance", "year_high", "ath_high", "high_52w"):
        n = _f(stock.get(key), 0.0)
        if n > price * 1.001 and (exclude_level <= 0 or abs(n - exclude_level) / max(exclude_level, 1.0) > 0.001):
            candidates.append(n)
    if not candidates:
        return 0.0
    return min(candidates)


def _session_low_for_stock(stock: dict) -> float:
    intraday = stock.get("intraday") if isinstance(stock.get("intraday"), dict) else {}
    vals = [
        _f(stock.get("session_low"), 0.0),
        _f(stock.get("low_live"), 0.0),
        _f(stock.get("day_low"), 0.0),
        _f(intraday.get("session_low"), 0.0),
        _f(intraday.get("low"), 0.0),
    ]
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else 0.0


def compute_liquidity_persistence(stock: dict) -> dict:
    risk_tags = set(_lst(stock.get("risk_tags")) + _lst(stock.get("risk_flags")))
    success_tags = set(_lst(stock.get("success_tags")))
    effective = _f(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)))
    daily = _f(stock.get("volume_ratio", 0))
    pace = _f(stock.get("volume_pace_ratio", 0))
    intraday = stock.get("intraday") if isinstance(stock.get("intraday"), dict) else {}
    intraday_ratio = _f(intraday.get("intraday_volume_ratio", 0))
    score = 50.0
    reasons: list[str] = []

    best_ratio = max(effective, daily, pace, intraday_ratio)
    if "السيولة استمرت" in success_tags:
        score += 24
        reasons.append("السيولة مذكورة كعامل إيجابي مستمر")
    if "السيولة لم تستمر" in risk_tags:
        score -= 34
        reasons.append("تقارير المخاطر تشير إلى أن السيولة لم تستمر")
    if best_ratio >= 1.8:
        score += 18
        reasons.append("RVOL/السيولة أعلى من الطبيعي بقوة")
    elif best_ratio >= 1.25:
        score += 10
        reasons.append("السيولة أعلى من الطبيعي")
    elif 0 < best_ratio <= 0.75:
        score -= 16
        reasons.append("السيولة الحالية ضعيفة أو بدأت تفقد زخمها")

    if pace and daily and pace < max(0.55, daily * 0.55):
        score -= 16
        reasons.append("سرعة السيولة أضعف من السيولة اليومية؛ احتمال اندفاع قصير")

    score = max(0.0, min(100.0, round(score, 1)))
    if score >= 76:
        label = "✅ السيولة مستمرة"
        status = "confirmed"
    elif score >= 54:
        label = "🟡 السيولة مقبولة وتحتاج متابعة"
        status = "watch"
    elif score >= 34:
        label = "⚠️ السيولة غير مؤكدة"
        status = "weak"
    else:
        label = "🔴 سيولة مؤقتة / لم تثبت"
        status = "fade"
    return {"score": score, "label": label, "status": status, "reasons": reasons[:5]}


def compute_no_chase_guard(stock: dict) -> dict:
    price = _f(stock.get("current_price_live", stock.get("display_price", 0)))
    entry = _f(stock.get("display_entry_price", stock.get("entry_price_real", stock.get("entry", 0))))
    change_open = _f(stock.get("change_from_open_pct", stock.get("display_change_pct", stock.get("change_pct", 0))))
    live_dist = _f(stock.get("live_distance_to_entry_pct"), 0.0)
    if price > 0 and entry > 0:
        dist_to_entry = ((price - entry) / entry) * 100.0
    else:
        dist_to_entry = live_dist

    late_flag = _s(stock.get("late_move_flag"))
    execution_label = _s(stock.get("execution_readiness_label"))
    reasons: list[str] = []
    severity = 0
    if late_flag in {"CONFIRMED_LATE", "FAST_AFTER_CONFIRMATION"}:
        severity = max(severity, 3)
        reasons.append("السعر تجاوز منطقة الدخول المناسبة حسب late_move_flag")
    if dist_to_entry >= 6.0:
        severity = max(severity, 3)
        reasons.append(f"السعر أعلى من الدخول بنحو {dist_to_entry:.1f}%")
    elif dist_to_entry >= 3.0:
        severity = max(severity, 2)
        reasons.append(f"السعر ابتعد عن الدخول بنحو {dist_to_entry:.1f}%")
    if change_open >= 12:
        severity = max(severity, 3)
        reasons.append(f"السهم صاعد بقوة منذ الافتتاح ({change_open:.1f}%)")
    elif change_open >= 8:
        severity = max(severity, 2)
        reasons.append(f"السهم صاعد كثيرًا منذ الافتتاح ({change_open:.1f}%)")
    if "مطاردة" in execution_label:
        severity = max(severity, 3)
        reasons.append("جاهزية التنفيذ تشير إلى مطاردة سعرية")

    if severity >= 3:
        return {"status": "no_chase", "label": "🔴 متأخر / لا تطارد", "score_penalty": 26, "reasons": reasons[:5]}
    if severity == 2:
        return {"status": "late_watch", "label": "⚠️ قريب من المطاردة", "score_penalty": 12, "reasons": reasons[:5]}
    return {"status": "ok", "label": "✅ ليس متأخرًا", "score_penalty": 0, "reasons": reasons[:5]}


def compute_structure_guards(stock: dict) -> dict:
    price = _f(stock.get("current_price_live", stock.get("display_price", 0)))
    support = _f(stock.get("nearest_support"), 0.0)
    resistance = _f(stock.get("nearest_resistance"), 0.0)
    support_dist = _nearest_distance(stock, "nearest_support_distance_pct", "nearest_support", price)
    resistance_dist = _nearest_distance(stock, "nearest_resistance_distance_pct", "nearest_resistance", price)
    support_strength = _s(stock.get("nearest_support_strength", stock.get("support_strength_label", "")))
    resistance_strength = _s(stock.get("nearest_resistance_strength", stock.get("resistance_strength_label", "")))
    risk_tags = set(_lst(stock.get("risk_tags")) + _lst(stock.get("risk_flags")))
    risk_pct = _f(stock.get("display_risk_pct", stock.get("risk_pct", 0)))
    dist_52 = _f(stock.get("distance_to_52w_high_pct", 0))
    dist_ath = _f(stock.get("distance_to_ath_pct", 0))

    score_penalty = 0.0
    reasons: list[str] = []

    support_broken_flag = False
    support_reclaimed_flag = False
    broken_support_distance_pct = 0.0
    next_support_below = 0.0
    support_state = "unknown"
    reclaimed_support_level = 0.0

    resistance_reclaimed_flag = False
    reclaimed_resistance_distance_pct = 0.0
    next_resistance_above = 0.0
    resistance_state = "unknown"
    reclaimed_resistance_level = 0.0

    # Support practicality.
    # V4f: classify the level by live price state, not by the stale label. If a
    # level was broken intraday then recovered, show it as reclaimed/tested
    # support; if live price is still below it, show broken support plus the next
    # lower support. Do not display an above-price level as active support.
    if price > 0 and support > 0:
        session_low = _session_low_for_stock(stock)
        if support > price * 1.0005:
            support_state = "broken"
            support_broken_flag = True
            broken_support_distance_pct = ((support - price) / price) * 100.0
            next_support_below = _first_level_below(stock, price, support)
            score_penalty += 18
            support_dist = -round(broken_support_distance_pct, 2)
            support_label = f"🔴 دعم مكسور {support:.2f} — السعر تحته بنحو {broken_support_distance_pct:.2f}%"
            if next_support_below > 0:
                below_pct = ((price - next_support_below) / price) * 100.0
                support_label += f" | الدعم التالي {next_support_below:.2f} أسفل السعر بنحو {below_pct:.2f}%"
            else:
                support_label += " | لا يوجد دعم أدنى مؤكد من البيانات الحالية"
            reasons.append("السعر الحي أصبح تحت الدعم المحسوب؛ لا تعتمد هذا المستوى كدعم إلا بعد استعادته")
        elif session_low > 0 and session_low < support * 0.999 and price > support * 1.0005:
            support_state = "reclaimed"
            support_reclaimed_flag = True
            reclaimed_support_level = support
            above_pct = ((price - support) / price) * 100.0
            support_label = f"🟢 دعم مستعاد {support:.2f} — السعر فوقه بنحو {above_pct:.2f}% بعد كسره/اختباره"
            reasons.append("السهم نزل تحت الدعم خلال الجلسة ثم عاد فوقه؛ راقب الثبات فوق المستوى")
        else:
            support_state = "active"
            support_label = "✅ الدعم قريب/واضح" if (0 < abs(support_dist) <= 3.0 or "قوي" in support_strength) else "🟡 الدعم يحتاج تحقق"
            if "كسر الدعم" in risk_tags:
                score_penalty += 18
                support_label = "🔴 كسر دعم / الخطة تحتاج تأكيد جديد"
                reasons.append("ظهر كسر الدعم في عوامل الخطر")
            elif support_dist and abs(support_dist) > 5.0 and risk_pct > 6.5:
                score_penalty += 8
                support_label = "⚠️ الدعم/الوقف بعيد"
                reasons.append("الدعم أو الوقف بعيد عن نقطة الدخول")
    else:
        support_label = "🟡 الدعم يحتاج تحقق"

    # Resistance practicality.
    # V4f: a resistance level below live price is no longer the nearest active
    # resistance. It becomes a reclaimed/broken resistance (potential support),
    # and we look for the next resistance above live price.
    if price > 0 and resistance > 0 and resistance < price * 0.9995:
        resistance_state = "reclaimed"
        resistance_reclaimed_flag = True
        reclaimed_resistance_level = resistance
        reclaimed_resistance_distance_pct = ((price - resistance) / price) * 100.0
        next_resistance_above = _first_level_above(stock, price, resistance)
        resistance_dist = ((next_resistance_above - price) / price) * 100.0 if next_resistance_above > 0 else 0.0
        if next_resistance_above > 0:
            resistance_label = (
                f"🟢 مقاومة مخترقة {resistance:.2f} — السعر فوقها بنحو {reclaimed_resistance_distance_pct:.2f}%"
                f" | المقاومة التالية {next_resistance_above:.2f} فوق السعر بنحو {resistance_dist:.2f}%"
            )
        else:
            resistance_label = f"🟢 مقاومة مخترقة {resistance:.2f} — السعر فوقها بنحو {reclaimed_resistance_distance_pct:.2f}% | لا توجد مقاومة أعلى مؤكدة"
        reasons.append("السعر تجاوز المقاومة السابقة؛ لا تعرضها كمقاومة نشطة، بل كمستوى يحتاج ثباتًا فوقه")
    else:
        resistance_state = "active" if resistance > 0 else "unknown"
        resistance_label = "✅ المقاومة بعيدة" if resistance_dist >= 3.0 else "🟡 مقاومة تحتاج متابعة"
        if "قريب من مقاومة قوية" in risk_tags:
            score_penalty += 16
            resistance_label = "🔴 قريب من مقاومة قوية"
            reasons.append("قرب من مقاومة قوية كان من أنماط الخسارة المتكررة")
        elif "قريب من مقاومة" in risk_tags or (0 < resistance_dist <= 1.5):
            score_penalty += 10
            resistance_label = "⚠️ مقاومة قريبة"
            reasons.append("المقاومة قريبة وقد تحد من استمرار الحركة")

    if "قرب من قمة تاريخية" in risk_tags or (dist_ath and abs(dist_ath) <= 3.0):
        score_penalty += 12
        reasons.append("السهم قريب من قمة تاريخية")
    if "قرب من قمة سنوية" in risk_tags or (dist_52 and abs(dist_52) <= 3.0):
        score_penalty += 10
        reasons.append("السهم قريب من قمة سنوية")

    active_resistance_close = bool(resistance_state != "reclaimed" and ((0 < resistance_dist <= 1.5) or "قريب من مقاومة قوية" in risk_tags or "قريب من مقاومة" in risk_tags))
    if resistance_state == "reclaimed" and next_resistance_above > 0:
        active_resistance_close = bool(0 < resistance_dist <= 1.5)
    close_resistance_flag = active_resistance_close
    near_high_flag = bool("قرب من قمة تاريخية" in risk_tags or "قرب من قمة سنوية" in risk_tags or (dist_ath and abs(dist_ath) <= 3.0) or (dist_52 and abs(dist_52) <= 3.0))
    return {
        "support_label": support_label,
        "resistance_label": resistance_label,
        "score_penalty": round(score_penalty, 1),
        "reasons": reasons[:8],
        "support_distance_pct": round(support_dist, 2),
        "resistance_distance_pct": round(resistance_dist, 2),
        "close_resistance_flag": close_resistance_flag,
        "near_high_flag": near_high_flag,
        "support_state": support_state,
        "support_broken_flag": support_broken_flag,
        "support_reclaimed_flag": support_reclaimed_flag,
        "broken_support_level": round(support, 4) if support_broken_flag else 0.0,
        "reclaimed_support_level": round(reclaimed_support_level, 4) if support_reclaimed_flag else 0.0,
        "broken_support_distance_pct": round(broken_support_distance_pct, 2),
        "next_support_below": round(next_support_below, 4) if next_support_below > 0 else 0.0,
        "resistance_state": resistance_state,
        "resistance_reclaimed_flag": resistance_reclaimed_flag,
        "reclaimed_resistance_level": round(reclaimed_resistance_level, 4) if resistance_reclaimed_flag else 0.0,
        "reclaimed_resistance_distance_pct": round(reclaimed_resistance_distance_pct, 2),
        "next_resistance_above": round(next_resistance_above, 4) if next_resistance_above > 0 else 0.0,
    }

def compute_pattern_risk(stock: dict, liquidity: dict, no_chase: dict, structure: dict) -> dict:
    risk_tags = set(_lst(stock.get("risk_tags")) + _lst(stock.get("risk_flags")))
    score = 0.0
    reasons: list[str] = []

    weights = {
        "كسر الدعم": 24,
        "السيولة لم تستمر": 22,
        "قريب من مقاومة قوية": 20,
        "قريب من مقاومة": 15,
        "تذبذب عالي": 16,
        "قرب من قمة سنوية": 14,
        "قرب من قمة تاريخية": 16,
        "سهم صغير عالي المخاطر": 14,
        "نقطة الدخول بعيدة": 8,
    }
    for tag, weight in weights.items():
        if tag in risk_tags:
            score += weight
            reasons.append(tag)
    if str(liquidity.get("status")) in {"fade", "weak"}:
        score += 18 if str(liquidity.get("status")) == "fade" else 10
        reasons.append("السيولة غير مثبتة")
    if str(no_chase.get("status")) == "no_chase":
        score += 20
        reasons.append("دخول متأخر/مطاردة")
    score += min(_f(structure.get("score_penalty"), 0), 28)

    # Combination penalty: this is the pattern that repeated in the reports.
    if {"كسر الدعم", "السيولة لم تستمر"}.issubset(risk_tags) and ("قريب من مقاومة قوية" in risk_tags or "قريب من مقاومة" in risk_tags):
        score += 18
        reasons.append("تركيبة خسارة متكررة: دعم مكسور + سيولة ضعفت + مقاومة")

    score = max(0.0, min(100.0, round(score, 1)))
    if score >= 70:
        label = "🔴 نمط خطر مشابه لخسائر سابقة"
        status = "high"
    elif score >= 45:
        label = "⚠️ نمط مخاطرة يحتاج تأكيد"
        status = "medium"
    elif score >= 25:
        label = "🟡 مخاطرة قابلة للإدارة"
        status = "watch"
    else:
        label = "✅ لا يظهر نمط خسارة قوي"
        status = "low"
    return {"score": score, "label": label, "status": status, "reasons": reasons[:9]}


def compute_post_activation_guard(stock: dict, liquidity: dict, pattern: dict) -> dict:
    trade_type = _s(stock.get("type", stock.get("display_plan_family", "")))
    breakout_quality = _s(stock.get("breakout_quality")).upper()
    current = _f(stock.get("current_price_live", stock.get("display_price", 0)))
    stop = _f(stock.get("display_stop_price", stock.get("stop_loss", 0)))
    entry = _f(stock.get("display_entry_price", stock.get("entry_price_real", stock.get("entry", 0))))
    reasons: list[str] = []
    score = 65.0

    if current > 0 and stop > 0 and current <= stop:
        score = 5
        reasons.append("السعر عند/دون وقف الخطة")
    elif current > 0 and entry > 0 and current >= entry:
        score += 8
        reasons.append("السعر وصل منطقة التفعيل/الدخول")
    if breakout_quality == "FAILED":
        score -= 32
        reasons.append("جودة الاختراق فاشلة")
    if str(liquidity.get("status")) in {"fade", "weak"}:
        score -= 22
        reasons.append("استمرار السيولة غير كافٍ بعد التفعيل")
    if str(pattern.get("status")) == "high":
        score -= 18
        reasons.append("النمط العام مشابه لخسائر بعد التفعيل")
    if "Breakout" in trade_type or _s(stock.get("display_plan_family")) == "breakout":
        reasons.append("الخطة تحتاج متابعة استمرار الاختراق لا مجرد التفعيل")

    score = max(0.0, min(100.0, round(score, 1)))
    if score >= 78:
        label = "✅ استمرار الاختراق مقبول"
        status = "ok"
    elif score >= 55:
        label = "🟡 يحتاج متابعة بعد التفعيل"
        status = "watch"
    elif score >= 30:
        label = "⚠️ اختراق/تفعيل ضعيف"
        status = "weak"
    else:
        label = "🔴 الخطة مكسورة أو تحتاج إعادة تأكيد"
        status = "broken"
    return {"score": score, "label": label, "status": status, "reasons": reasons[:6]}


def compute_strong_entry_tier(stock: dict, pattern: dict, liquidity: dict, no_chase: dict, post_activation: dict, structure: dict) -> dict:
    decision = _s(stock.get("decision"))
    quality = _f(stock.get("quality_score", 0))
    readiness = _f(stock.get("execution_readiness_score", 0))
    rr = _f(stock.get("rr_1", 0))
    if decision != "دخول قوي":
        # Still expose a lower-level quality label for diagnostics.
        return {"tier": "not_strong", "label": "", "rank_bonus": 0.0, "reasons": []}

    reasons: list[str] = []
    if str(no_chase.get("status")) == "no_chase":
        return {"tier": "late_no_chase", "label": "🔴 دخول قوي متأخر / لا تطارد", "rank_bonus": -42.0, "reasons": no_chase.get("reasons", [])}
    liquidity_status = str(liquidity.get("status"))
    close_resistance = bool(structure.get("close_resistance_flag"))
    near_high = bool(structure.get("near_high_flag"))
    if (
        str(pattern.get("status")) == "high"
        or str(post_activation.get("status")) in {"weak", "broken"}
        or liquidity_status in {"weak", "fade"}
        or (close_resistance and near_high and liquidity_status != "confirmed")
    ):
        risk_reasons = []
        risk_reasons.extend(pattern.get("reasons", []) or [])
        if liquidity_status in {"weak", "fade"}:
            risk_reasons.append("السيولة غير مؤكدة أو ضعفت")
        if close_resistance:
            risk_reasons.append("مقاومة قريبة تحتاج اختراقًا واضحًا")
        if near_high:
            risk_reasons.append("قرب من قمة سنوية/تاريخية يحتاج تأكيدًا أقوى")
        if str(post_activation.get("status")) in {"weak", "broken"}:
            risk_reasons.append("حارس ما بعد التفعيل ضعيف")
        return {"tier": "high_risk", "label": "⚠️ دخول قوي عالي المخاطرة / يحتاج تأكيد", "rank_bonus": -34.0, "reasons": risk_reasons[:7]}
    if quality >= 84 and readiness >= 62 and rr >= 1.15 and str(liquidity.get("status")) == "confirmed" and str(pattern.get("status")) in {"low", "watch"} and not (close_resistance and near_high):
        reasons.append("جودة عالية + جاهزية جيدة + سيولة مستمرة + لا يظهر نمط خسارة قوي")
        return {"tier": "excellent", "label": "🚀 دخول قوي ممتاز", "rank_bonus": 12.0, "reasons": reasons}
    return {"tier": "normal", "label": "✅ دخول قوي عادي", "rank_bonus": 0.0, "reasons": ["فرصة قوية لكن ليست في فئة الممتاز بعد"]}


def enrich_opportunity_intelligence(stock: dict | None) -> dict:
    out = dict(stock or {})
    if not out:
        return out

    liquidity = compute_liquidity_persistence(out)
    no_chase = compute_no_chase_guard(out)
    structure = compute_structure_guards(out)
    pattern = compute_pattern_risk(out, liquidity, no_chase, structure)
    post_activation = compute_post_activation_guard(out, liquidity, pattern)
    tier = compute_strong_entry_tier(out, pattern, liquidity, no_chase, post_activation, structure)

    out.update({
        "intelligence_layer_version": "pattern_learning_v1_guard_only",
        "liquidity_persistence_score": liquidity["score"],
        "liquidity_persistence_label": liquidity["label"],
        "liquidity_persistence_status": liquidity["status"],
        "liquidity_persistence_reasons": liquidity.get("reasons", []),
        "no_chase_guard_status": no_chase["status"],
        "no_chase_guard_label": no_chase["label"],
        "no_chase_guard_reasons": no_chase.get("reasons", []),
        "support_guard_label": structure["support_label"],
        "resistance_guard_label": structure["resistance_label"],
        "structure_guard_reasons": structure.get("reasons", []),
        "structure_resistance_distance_pct": structure.get("resistance_distance_pct", 0),
        "structure_support_distance_pct": structure.get("support_distance_pct", 0),
        "support_broken_flag": bool(structure.get("support_broken_flag")),
        "broken_support_level": structure.get("broken_support_level", 0),
        "broken_support_distance_pct": structure.get("broken_support_distance_pct", 0),
        "next_support_below": structure.get("next_support_below", 0),
        "near_high_guard_flag": bool(structure.get("near_high_flag")),
        "close_resistance_guard_flag": bool(structure.get("close_resistance_flag")),
        "pattern_risk_score": pattern["score"],
        "pattern_risk_label": pattern["label"],
        "pattern_risk_status": pattern["status"],
        "pattern_risk_reasons": pattern.get("reasons", []),
        "post_activation_guard_score": post_activation["score"],
        "post_activation_guard_label": post_activation["label"],
        "post_activation_guard_status": post_activation["status"],
        "post_activation_guard_reasons": post_activation.get("reasons", []),
        "strong_entry_tier": tier["tier"],
        "strong_entry_tier_label": tier["label"],
        "strong_entry_tier_reasons": tier.get("reasons", []),
    })

    # Conservative display re-ranking only. No core score/classification rewrite.
    base_rank = _f(out.get("display_rank_score", out.get("quality_score", 0)), 0)
    penalty = _f(no_chase.get("score_penalty"), 0) + min(_f(pattern.get("score"), 0) * 0.30, 30) + min(_f(structure.get("score_penalty"), 0) * 0.95, 26)
    if str(liquidity.get("status")) in {"weak", "fade"}:
        penalty += 14
    if bool(structure.get("close_resistance_flag")) and bool(structure.get("near_high_flag")):
        penalty += 16
    if str(post_activation.get("status")) in {"weak", "broken"}:
        penalty += 14
    adjusted = max(0.0, base_rank + _f(tier.get("rank_bonus"), 0) - penalty)
    out["display_rank_score_raw"] = round(base_rank, 2)
    out["display_rank_score"] = round(adjusted, 2)
    out["intelligence_rank_adjustment"] = round(adjusted - base_rank, 2)
    if "live_rank_score" in out:
        live_base = _f(out.get("live_rank_score"), base_rank)
        out["live_rank_score_raw"] = round(live_base, 2)
        out["live_rank_score"] = round(max(0.0, live_base + (adjusted - base_rank)), 2)

    # Owner-facing flags. Keep them compact because the UI already has many details.
    if str(pattern.get("status")) == "high":
        _add_flag(out, "نمط مخاطرة متكرر: يحتاج تأكيد أقوى قبل الدخول")
    if str(no_chase.get("status")) == "no_chase":
        _add_flag(out, "لا تطارد السعر؛ انتظر Pullback أو إعادة اختبار")
    if str(liquidity.get("status")) in {"fade", "weak"}:
        _add_flag(out, "السيولة غير مثبتة أو بدأت تضعف")
    if bool(structure.get("support_broken_flag")):
        _add_flag(out, "كسر الدعم الأقرب؛ يحتاج استعادة المستوى أو انتظار دعم أدنى واضح")
    if str(post_activation.get("status")) in {"weak", "broken"}:
        _add_flag(out, "لا تكتفِ بالتفعيل؛ الخطة تحتاج استمرار فوق الدخول/الدعم")

    # If a strong signal is risky, keep the original decision but make the user guidance explicit.
    if _s(out.get("decision")) == "دخول قوي" and tier.get("tier") in {"high_risk", "late_no_chase"}:
        note = tier.get("label") or pattern.get("label")
        existing = _s(out.get("owner_action"))
        out["owner_action"] = f"{note}: انتظر تأكيد السيولة والثبات قبل الدخول. {existing}".strip()
        if tier.get("tier") == "late_no_chase":
            out["execution_mode"] = "مراقبة إعادة دخول 👀"


    # Clear execution guidance for non-expert users. This is display guidance only.
    if str(liquidity.get("status")) == "confirmed" and not bool(structure.get("close_resistance_flag")) and str(post_activation.get("status")) in {"ok", "watch"}:
        out["execution_gate_label"] = "✅ قابل للتنفيذ إذا ثبت السعر فوق الدخول"
        out["execution_gate_status"] = "ready_with_plan"
    elif str(liquidity.get("status")) in {"weak", "fade"}:
        out["execution_gate_label"] = "⏳ انتظر تأكيد السيولة قبل الدخول"
        out["execution_gate_status"] = "wait_liquidity"
    elif bool(structure.get("close_resistance_flag")):
        out["execution_gate_label"] = "⏳ انتظر اختراق المقاومة القريبة والثبات فوقها"
        out["execution_gate_status"] = "wait_resistance_break"
    elif tier.get("tier") in {"high_risk", "late_no_chase"}:
        out["execution_gate_label"] = "⚠️ قوي فنيًا لكنه يحتاج تأكيدًا إضافيًا"
        out["execution_gate_status"] = "needs_confirmation"
    else:
        out["execution_gate_label"] = "🟡 يحتاج متابعة قبل التنفيذ"
        out["execution_gate_status"] = "watch"

    return out


def enrich_opportunity_intelligence_bulk(rows: list[dict]) -> list[dict]:
    return [enrich_opportunity_intelligence(row) for row in (rows or [])]
