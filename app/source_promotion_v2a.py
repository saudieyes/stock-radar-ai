"""Source / Promotion Engine V2a for Stock Radar AI.

This layer is intentionally conservative.  It does not replace the full source
engine yet; it makes the current source/promotion path evidence-aware by:
- tagging why a symbol entered the source,
- prioritizing Early Movement / weekly priority names for close watch,
- preventing confirmed early names from being buried in plain Watch,
- explaining why a symbol was not promoted.

No external API calls and no writes are performed here.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

SOURCE_PROMOTION_V2A_VERSION = "source_promotion_engine_v2a_evidence_aware_fastlane"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe(items: list[Any], limit: int = 10) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        text = _clean_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _market_is_live(row: dict) -> bool:
    return str(row.get("market_phase", "") or "") in {"open", "pre_market", "after_hours"}


def _source_tags(row: dict) -> list[str]:
    tags = []
    for key in ("source_reason_tags", "source_tags"):
        val = row.get(key)
        if isinstance(val, list):
            tags.extend([_clean_text(x) for x in val if _clean_text(x)])
        elif _clean_text(val):
            tags.append(_clean_text(val))
    reason = _clean_text(row.get("source_reason"))
    if reason:
        for part in reason.replace("+", "،").split("،"):
            if _clean_text(part):
                tags.append(_clean_text(part))
    return _dedupe(tags, 12)


def _lane_from_row(row: dict) -> str:
    em = row.get("early_movement") or {}
    src = str(em.get("source") or row.get("early_movement_source") or "")
    if src == "both":
        return "weekly_priority_plus_auto"
    if src == "weekly_priority":
        return "weekly_priority"
    if src == "high_risk_manual" or src == "high_risk_manual_plus_auto":
        return "high_risk_manual"
    if src == "auto_detected":
        return "auto_detected_early_movement"

    tags = " | ".join(_source_tags(row))
    if any(x in tags for x in ["قائمة الحركة المبكرة", "Weekly Priority", "weekly_priority"]):
        return "weekly_priority"
    if any(x in tags for x in ["تأكيد سعر حي", "قائمة رابحين", "الحركة الحية"]):
        return "live_mover"
    if any(x in tags for x in ["متحرك قوي", "سيولة/حجم غير عادي", "مرشح استمرار"]):
        return "fast_momentum"
    if any(x in tags for x in ["تهيئة", "قريب من اختراق", "تجميع"]):
        return "constructive_setup"
    if any(x in tags for x in ["منبع أساسي سابق", "baseline"]):
        return "baseline"
    return "standard_source"


def _lane_label(lane: str) -> str:
    return {
        "weekly_priority_plus_auto": "🔥 قائمة الويكند + تأكيد تلقائي",
        "weekly_priority": "🟣 قائمة الويكند المختارة",
        "auto_detected_early_movement": "🔵 حركة مبكرة مكتشفة تلقائيًا",
        "high_risk_manual": "⚠️ مراقبة عالية المخاطر",
        "live_mover": "⚡ متحرك حي/قبل الافتتاح",
        "fast_momentum": "🚀 زخم/سيولة من المنبع",
        "constructive_setup": "🧱 تهيئة بنّاءة",
        "baseline": "📌 منبع أساسي سابق",
        "standard_source": "مصدر عادي",
    }.get(lane, lane or "مصدر عادي")


def _block_reasons(row: dict) -> list[str]:
    reasons: list[str] = []
    em_status = str(row.get("early_movement_status", "") or "")
    no_chase = str(row.get("no_chase_guard_status", "") or "") == "no_chase" or em_status == "no_chase"
    if no_chase:
        reasons.append("No-Chase / الحركة متأخرة")
    if em_status in {"distribution_risk", "weak_or_expired"}:
        reasons.append("خطر تصريف أو انتهاء صلاحية النمط")
    if str(row.get("liquidity_persistence_status", "") or "") in {"weak", "fade", "fading"}:
        reasons.append("السيولة غير مؤكدة أو لا تبدو مستمرة")
    if str(row.get("post_activation_guard_status", "") or "") in {"weak", "failed", "danger"}:
        reasons.append("تأكيد ما بعد التفعيل ضعيف")
    if bool(row.get("close_resistance_guard_flag")) or _safe_float(row.get("structure_resistance_distance_pct", 999), 999) <= 0.75:
        reasons.append("قريب جدًا من مقاومة")
    if bool(row.get("support_broken_flag")):
        reasons.append("كسر/اختبار دعم يحتاج استعادة واضحة")
    if _safe_float(row.get("display_change_pct", row.get("change_vs_prev_close_pct", 0)), 0) >= 12:
        reasons.append("ارتفع كثيرًا مقارنة بسعر آخر إغلاق")
    if str(row.get("execution_gate_status", "") or "") in {"wait_liquidity", "wait_resistance_break"}:
        reasons.append(str(row.get("execution_gate_label") or "ينتظر تأكيدًا إضافيًا"))
    return _dedupe(reasons, 8)


def _ready_reasons(row: dict) -> list[str]:
    reasons: list[str] = []
    lane = _lane_from_row(row)
    if lane in {"weekly_priority", "weekly_priority_plus_auto"}:
        reasons.append("من قائمة الويكند المختارة")
    if lane in {"auto_detected_early_movement", "weekly_priority_plus_auto"}:
        reasons.append("ظهر عليه نمط حركة مبكرة تلقائي")
    if _safe_float(row.get("execution_readiness_score", 0)) >= 55:
        reasons.append("جاهزية التنفيذ مقبولة")
    if _safe_float(row.get("quality_score", 0)) >= 65:
        reasons.append("الجودة الفنية جيدة")
    if _safe_float(row.get("effective_volume_ratio", row.get("volume_ratio", 0))) >= 1.0:
        reasons.append("السيولة ليست دون الطبيعي")
    if _safe_float(row.get("nearest_resistance_distance_pct", 999), 999) > 1.0 or bool(row.get("price_discovery_zone")):
        reasons.append("لا توجد مقاومة خانقة جدًا")
    return _dedupe(reasons, 8)


def _promotion_pressure(row: dict, lane: str, blockers: list[str]) -> float:
    score = 0.0
    if lane == "weekly_priority_plus_auto":
        score += 36
    elif lane == "weekly_priority":
        score += 24
    elif lane == "auto_detected_early_movement":
        score += 22
    elif lane == "live_mover":
        score += 18
    elif lane == "fast_momentum":
        score += 14
    elif lane == "constructive_setup":
        score += 8
    score += min(_safe_float(row.get("execution_readiness_score", 0)) * 0.22, 18)
    score += min(_safe_float(row.get("quality_score", 0)) * 0.12, 12)
    vol = _safe_float(row.get("effective_volume_ratio", row.get("volume_ratio", 0)))
    if vol >= 2.0:
        score += 12
    elif vol >= 1.15:
        score += 8
    elif vol >= 0.9:
        score += 3
    if _safe_float(row.get("nearest_resistance_distance_pct", 999), 999) <= 0.75 and not bool(row.get("price_discovery_zone")):
        score -= 18
    if blockers:
        score -= min(28, len(blockers) * 8)
    return round(max(0.0, min(100.0, score)), 2)


def enrich_row_source_promotion_v2a(row: dict) -> dict:
    if not isinstance(row, dict):
        return row
    lane = _lane_from_row(row)
    blockers = _block_reasons(row)
    ready = _ready_reasons(row)
    pressure = _promotion_pressure(row, lane, blockers)
    decision = str(row.get("decision", "") or "")
    live = _market_is_live(row)

    status = "source_observed"
    if blockers:
        status = "promotion_blocked"
    elif lane in {"weekly_priority_plus_auto", "auto_detected_early_movement", "weekly_priority", "live_mover", "fast_momentum"} and pressure >= 52:
        status = "close_watch"
    if live and not blockers and pressure >= 62 and decision == "مراقبة":
        # Conservative live-only lift: only to Cautious, never to Strong.
        row["decision_before_source_promotion_v2a"] = decision
        row["decision"] = "دخول بحذر"
        row["source_promotion_v2a_promoted"] = True
        status = "promoted_to_cautious_live"
        try:
            row["signal_strength_label"] = "بحذر"
            row["signal_strength_bucket"] = max(0, int(float(row.get("signal_strength_bucket", 0) or 0)))
        except Exception:
            pass
        row["owner_action"] = "🟠 ترقية من مسار المنبع السريع إلى دخول بحذر — يلزم استمرار السيولة وعدم المطاردة."

    if lane in {"weekly_priority", "weekly_priority_plus_auto", "auto_detected_early_movement"} and decision in {"مراقبة", "دخول بحذر"}:
        try:
            row["display_rank_score"] = round(_safe_float(row.get("display_rank_score", 0)) + (8 if not blockers else 2), 2)
            row["source_promotion_rank_boost"] = 8 if not blockers else 2
        except Exception:
            pass

    delayed_flag = ""
    chg = _safe_float(row.get("display_change_pct", row.get("change_vs_prev_close_pct", 0)), 0)
    if lane in {"weekly_priority", "weekly_priority_plus_auto", "auto_detected_early_movement"} and decision == "مراقبة" and chg >= 6 and not blockers:
        delayed_flag = "needs_fast_review"
    if blockers and chg >= 8:
        delayed_flag = "moved_but_blocked_or_no_chase"

    row["source_promotion_v2a_version"] = SOURCE_PROMOTION_V2A_VERSION
    row["source_priority_lane"] = lane
    row["source_priority_lane_label"] = _lane_label(lane)
    row["promotion_pressure_score"] = pressure
    row["promotion_v2a_status"] = status
    row["promotion_ready_reasons"] = ready
    row["promotion_block_reasons"] = blockers
    row["promotion_delay_flag"] = delayed_flag
    if blockers:
        row["promotion_summary"] = f"{_lane_label(lane)} — لم يترقَّ بسبب: " + "، ".join(blockers[:3])
    elif status == "promoted_to_cautious_live":
        row["promotion_summary"] = f"{_lane_label(lane)} — ترقى بحذر بسبب تأكيد حي وانعدام موانع واضحة."
    elif status == "close_watch":
        row["promotion_summary"] = f"{_lane_label(lane)} — مراقبة لصيقة؛ يحتاج تأكيد حي قبل الترقية."
    else:
        row["promotion_summary"] = f"{_lane_label(lane)} — تحت المراقبة."
    return row


def enrich_rows_source_promotion_v2a(rows: list[dict]) -> list[dict]:
    return [enrich_row_source_promotion_v2a(dict(x)) if isinstance(x, dict) else x for x in (rows or [])]


def summarize_source_promotion_v2a(rows: list[dict]) -> dict[str, Any]:
    lane_counts = Counter()
    status_counts = Counter()
    blocked_counts = Counter()
    promoted = []
    close_watch = []
    blocked = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        lane = str(row.get("source_priority_lane") or _lane_from_row(row))
        status = str(row.get("promotion_v2a_status") or "")
        lane_counts[lane] += 1
        if status:
            status_counts[status] += 1
        for r in row.get("promotion_block_reasons") or []:
            blocked_counts[str(r)] += 1
        compact = {
            "symbol": row.get("symbol"),
            "decision": row.get("decision"),
            "lane": lane,
            "status": status,
            "pressure": row.get("promotion_pressure_score"),
            "summary": row.get("promotion_summary", ""),
        }
        if row.get("source_promotion_v2a_promoted"):
            promoted.append(compact)
        elif status == "close_watch":
            close_watch.append(compact)
        elif status == "promotion_blocked":
            blocked.append(compact)
    return {
        "version": SOURCE_PROMOTION_V2A_VERSION,
        "lane_counts": dict(lane_counts),
        "status_counts": dict(status_counts),
        "block_reason_counts": dict(blocked_counts.most_common(10)),
        "promoted_count": len(promoted),
        "close_watch_count": len(close_watch),
        "blocked_count": len(blocked),
        "promoted_samples": promoted[:12],
        "close_watch_samples": close_watch[:12],
        "blocked_samples": blocked[:12],
    }


def build_source_promotion_v2a_report(rows: list[dict], format: str = "json") -> Any:
    enriched = enrich_rows_source_promotion_v2a(rows or [])
    summary = summarize_source_promotion_v2a(enriched)
    if str(format or "json").lower() not in {"brief", "text", "txt", "chatgpt"}:
        return {"ok": True, "summary": summary, "rows_count": len(enriched)}
    lines = [
        "تقرير Source / Promotion Engine V2a",
        f"version: {summary['version']}",
        "",
        "ملخص المسارات:",
    ]
    for k, v in summary.get("lane_counts", {}).items():
        lines.append(f"- {k}: {v}")
    lines += ["", "حالات الترقية/المنع:"]
    for k, v in summary.get("status_counts", {}).items():
        lines.append(f"- {k}: {v}")
    if summary.get("block_reason_counts"):
        lines += ["", "أبرز أسباب منع الترقية:"]
        for k, v in summary["block_reason_counts"].items():
            lines.append(f"- {k}: {v}")
    if summary.get("close_watch_samples"):
        lines += ["", "Close Watch:"]
        for x in summary["close_watch_samples"][:10]:
            lines.append(f"- {x['symbol']} | {x['decision']} | {x['lane']} | pressure={x['pressure']} | {x['summary']}")
    if summary.get("promoted_samples"):
        lines += ["", "Promoted to cautious by V2a:"]
        for x in summary["promoted_samples"][:10]:
            lines.append(f"- {x['symbol']} | pressure={x['pressure']} | {x['summary']}")
    return "\n".join(lines)
