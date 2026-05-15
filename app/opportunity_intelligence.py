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

    # Support / stop practicality.
    support_label = "✅ الدعم قريب/واضح" if (0 < abs(support_dist) <= 3.0 or "قوي" in support_strength) else "🟡 الدعم يحتاج تحقق"
    if "كسر الدعم" in risk_tags:
        score_penalty += 18
        support_label = "🔴 كسر دعم / الخطة تحتاج تأكيد جديد"
        reasons.append("ظهر كسر الدعم في عوامل الخطر")
    elif support_dist and abs(support_dist) > 5.0 and risk_pct > 6.5:
        score_penalty += 8
        support_label = "⚠️ الدعم/الوقف بعيد"
        reasons.append("الدعم أو الوقف بعيد عن نقطة الدخول")

    # Resistance / highs.
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

    return {
        "support_label": support_label,
        "resistance_label": resistance_label,
        "score_penalty": round(score_penalty, 1),
        "reasons": reasons[:7],
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


def compute_strong_entry_tier(stock: dict, pattern: dict, liquidity: dict, no_chase: dict, post_activation: dict) -> dict:
    decision = _s(stock.get("decision"))
    quality = _f(stock.get("quality_score", 0))
    readiness = _f(stock.get("execution_readiness_score", 0))
    rr = _f(stock.get("rr_1", 0))
    if decision != "دخول قوي":
        # Still expose a lower-level quality label for diagnostics.
        return {"tier": "not_strong", "label": "", "rank_bonus": 0.0, "reasons": []}

    reasons: list[str] = []
    if str(no_chase.get("status")) == "no_chase":
        return {"tier": "late_no_chase", "label": "🔴 دخول قوي متأخر / لا تطارد", "rank_bonus": -36.0, "reasons": no_chase.get("reasons", [])}
    if str(pattern.get("status")) == "high" or str(post_activation.get("status")) in {"weak", "broken"}:
        return {"tier": "high_risk", "label": "⚠️ دخول قوي عالي المخاطرة", "rank_bonus": -22.0, "reasons": (pattern.get("reasons", []) or [])[:5]}
    if quality >= 84 and readiness >= 62 and rr >= 1.15 and str(liquidity.get("status")) == "confirmed" and str(pattern.get("status")) in {"low", "watch"}:
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
    tier = compute_strong_entry_tier(out, pattern, liquidity, no_chase, post_activation)

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
    penalty = _f(no_chase.get("score_penalty"), 0) + min(_f(pattern.get("score"), 0) * 0.22, 22) + min(_f(structure.get("score_penalty"), 0) * 0.7, 18)
    if str(post_activation.get("status")) in {"weak", "broken"}:
        penalty += 8
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
    if str(post_activation.get("status")) in {"weak", "broken"}:
        _add_flag(out, "لا تكتفِ بالتفعيل؛ الخطة تحتاج استمرار فوق الدخول/الدعم")

    # If a strong signal is risky, keep the original decision but make the user guidance explicit.
    if _s(out.get("decision")) == "دخول قوي" and tier.get("tier") in {"high_risk", "late_no_chase"}:
        note = tier.get("label") or pattern.get("label")
        existing = _s(out.get("owner_action"))
        out["owner_action"] = f"{note}: انتظر تأكيد السيولة والثبات قبل الدخول. {existing}".strip()
        if tier.get("tier") == "late_no_chase":
            out["execution_mode"] = "مراقبة إعادة دخول 👀"

    return out


def enrich_opportunity_intelligence_bulk(rows: list[dict]) -> list[dict]:
    return [enrich_opportunity_intelligence(row) for row in (rows or [])]
