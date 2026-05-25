"""Source Promotion Engine V2.

Final row-level governance layer for Source / Early Discovery V2.
It enforces the agreed lists:
- Strong Entry: actionable now / very near valid entry
- Early Confirmation: beginning of move, not yet strong
- Continuation: already moved, conditional only
- Pre-Move Watch: not an entry, watch for coming sessions
- No-Chase: wait only
"""
from __future__ import annotations

import os
from collections import Counter
from typing import Any

from app.detection_journal import enrich_stock_with_detection_journal
from app.move_stage_classifier import apply_move_stage_to_row
from app.pre_move_engine import enrich_row_pre_move

SOURCE_PROMOTION_ENGINE_V2_VERSION = "source_promotion_engine_v2_root_early_discovery_2026_05_25"


def _env_bool(name: str, default: bool = True) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def source_promotion_engine_v2_enabled() -> bool:
    return _env_bool("SOURCE_PROMOTION_ENGINE_V2_ENABLED", True) and _env_bool("SOURCE_EARLY_DISCOVERY_V2_ENABLED", True)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
        return float(value)
    except Exception:
        return default


def _dedupe(items: list[Any], limit: int = 10) -> list[str]:
    out = []
    seen = set()
    for item in items or []:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _stage(row: dict) -> str:
    return str((row.get("move_stage_v2") or {}).get("move_stage") or row.get("move_stage") or "")


def _stage_label(row: dict) -> str:
    return str((row.get("move_stage_v2") or {}).get("move_stage_label") or row.get("move_stage_label") or "")


def _stage_blockers(row: dict) -> list[str]:
    meta = row.get("move_stage_v2") or {}
    return [str(x) for x in (meta.get("stage_blockers") or row.get("stage_blockers") or []) if str(x).strip()]


def _cap_to_watch(row: dict, reason: str) -> None:
    prior = str(row.get("decision", "") or "")
    if prior != "مراقبة":
        row["decision_before_source_promotion_v2"] = prior
    row["decision"] = "مراقبة"
    row["signal_strength_label"] = "مراقبة"
    row["signal_strength_bucket"] = -1
    row["source_promotion_v2_capped"] = True
    row["source_promotion_v2_cap_reason"] = reason


def _cap_strong_to_cautious(row: dict, reason: str) -> None:
    prior = str(row.get("decision", "") or "")
    if prior == "دخول قوي":
        row["decision_before_source_promotion_v2"] = prior
        row["decision"] = "دخول بحذر"
        row["signal_strength_label"] = "بحذر"
        row["signal_strength_bucket"] = 0
        row["source_promotion_v2_capped"] = True
        row["source_promotion_v2_cap_reason"] = reason


def enrich_row_source_promotion_v2(row: dict) -> dict:
    if not isinstance(row, dict) or not source_promotion_engine_v2_enabled():
        return row

    # Make the row self-contained: journal + movement stage + pre-move metadata.
    try:
        row = enrich_stock_with_detection_journal(row, source_layer="source_promotion_v2")
    except Exception:
        try:
            row = apply_move_stage_to_row(row)
        except Exception:
            pass
    try:
        row = enrich_row_pre_move(row)
    except Exception:
        pass

    stage = _stage(row)
    label = _stage_label(row)
    decision = str(row.get("decision", "مراقبة") or "مراقبة")
    gain_at_detection = _safe_float(row.get("gain_at_detection", 0), 0)
    current_gain = _safe_float(row.get("current_gain", row.get("display_change_pct", 0)), 0)
    readiness = _safe_float(row.get("execution_readiness_score", 0), 0)
    quality = _safe_float(row.get("quality_score", 0), 0)
    volume = _safe_float(row.get("effective_volume_ratio", row.get("volume_pace_ratio", row.get("volume_ratio", 0))), 0)
    rr = _safe_float(row.get("rr_1", 0), 0)
    price_reliable = bool(row.get("price_reliable_for_execution", True))
    blockers = _stage_blockers(row)
    reasons = list((row.get("stage_reasons") or []))

    status = "observed"
    immediate_list = "watch"

    late_stages = {"Continuation Watch", "Already Moved", "Extended", "Requires Pullback", "No-Chase", "Catalyst Spike Review"}
    hard_no_chase_stages = {"Extended", "No-Chase", "Catalyst Spike Review"}

    if stage in hard_no_chase_stages or gain_at_detection >= 20 or current_gain >= 25:
        _cap_to_watch(row, "الحركة متأخرة أو ممتدة — لا تطارد")
        row["no_chase_guard_status"] = "no_chase"
        row["no_chase_guard_label"] = "⛔ لا تطارد"
        row["no_chase_guard_reasons"] = _dedupe(list(row.get("no_chase_guard_reasons") or []) + ["الحركة متأخرة عند الاكتشاف", f"gain_at_detection={round(gain_at_detection,2)}%"], 8)
        row["owner_action"] = "⛔ لا تطارد الآن — انتظر pullback صحي أو إعادة تمركز قبل أي دخول."
        status = "hard_no_chase_cap"
        immediate_list = "no_chase"
    elif stage in {"Continuation Watch", "Requires Pullback"} or gain_at_detection >= 10:
        if decision == "دخول قوي":
            # Continuation is not a clean immediate Strong unless the setup is exceptionally clean.
            clean_continuation = price_reliable and readiness >= 62 and quality >= 76 and volume >= 1.25 and rr >= 0.8 and not blockers and bool(row.get("stage_allows_strong"))
            if not clean_continuation:
                _cap_strong_to_cautious(row, "استمرار مشروط وليس دخول قوي نظيف")
        row["continuation_watch_active"] = True
        row["continuation_watch_label"] = "🔵 استمرار مشروط"
        row["owner_action"] = row.get("owner_action") or "🔵 استمرار مشروط — لا تدخل إلا بعد ثبات/ pullback / reclaim بسيولة."
        status = "continuation_only"
        immediate_list = "continuation"
    elif stage == "Early Confirmation":
        immediate_list = "early_confirmation"
        status = "early_confirmation"
        if decision == "مراقبة" and bool(row.get("stage_allows_cautious")) and readiness >= 50 and volume >= 1.05 and price_reliable:
            row["decision_before_source_promotion_v2"] = decision
            row["decision"] = "دخول بحذر"
            row["signal_strength_label"] = "بحذر"
            row["signal_strength_bucket"] = max(0, int(float(row.get("signal_strength_bucket", 0) or 0)))
            row["source_promotion_v2_promoted"] = True
            status = "early_confirmation_promoted_to_cautious"
            row["owner_action"] = "🟠 تأكيد مبكر — دخول بحذر فقط إذا استمرت السيولة وبقي السعر قريبًا من الدخول."
    elif stage == "Active Breakout":
        immediate_list = "active_confirmation"
        status = "active_breakout"
        if decision == "دخول قوي" and not bool(row.get("stage_allows_strong")):
            _cap_strong_to_cautious(row, "اختراق نشط لكن شروط Strong النظيفة لم تكتمل")
        elif decision == "مراقبة" and bool(row.get("stage_allows_cautious")):
            row["decision_before_source_promotion_v2"] = decision
            row["decision"] = "دخول بحذر"
            row["signal_strength_label"] = "بحذر"
            row["source_promotion_v2_promoted"] = True
            status = "active_breakout_promoted_to_cautious"
    elif stage == "Pre-Move":
        immediate_list = "pre_move_watch"
        status = "pre_move_watch"
        # Pre-Move is not an entry. If an older scoring layer called it Strong, cap it.
        if decision == "دخول قوي" and not bool(row.get("stage_allows_strong")):
            _cap_strong_to_cautious(row, "Pre-Move ليس دخولًا قويًا حتى يظهر تأكيد حي")
        row.setdefault("owner_action", "🟣 مراقبة مبكرة قبل الحركة — ليست دخولًا الآن حتى يظهر تأكيد حي.")

    # Strong final guard: no Strong with unreliable price, late detection, hard blockers.
    final_decision = str(row.get("decision", decision) or decision)
    if final_decision == "دخول قوي":
        strong_blockers = []
        if not price_reliable:
            strong_blockers.append("السعر غير موثوق")
        if stage in late_stages and not bool(row.get("stage_allows_strong")):
            strong_blockers.append("مرحلة الحركة لا تسمح بدخول قوي نظيف")
        if gain_at_detection >= 10 and not bool(row.get("stage_allows_strong")):
            strong_blockers.append("السهم اكتُشف متأخرًا فوق +10%")
        if blockers:
            strong_blockers += blockers[:3]
        if readiness < 54:
            strong_blockers.append("جاهزية التنفيذ غير كافية لـ Strong")
        if volume < 0.95:
            strong_blockers.append("السيولة غير كافية لـ Strong")
        if strong_blockers:
            _cap_strong_to_cautious(row, strong_blockers[0])
            row["source_promotion_v2_strong_blockers"] = _dedupe(strong_blockers, 8)
            status = "strong_capped_by_v2"

    # Display / diagnostics fields.
    row["source_promotion_v2_version"] = SOURCE_PROMOTION_ENGINE_V2_VERSION
    row["source_promotion_v2_status"] = status
    row["source_promotion_v2_list"] = immediate_list
    row["move_stage_label"] = label
    row["source_promotion_v2_summary"] = f"{label} — اكتشاف أولي {round(gain_at_detection, 2)}%، الحالي {round(current_gain, 2)}%."
    row["source_promotion_v2_reasons"] = _dedupe(reasons + blockers, 10)
    try:
        row["display_rank_score"] = round(_safe_float(row.get("display_rank_score", row.get("quality_score", 0)), 0) + _safe_float(row.get("stage_rank_adjustment", 0), 0), 2)
    except Exception:
        pass
    return row


def enrich_rows_source_promotion_v2(rows: list[dict]) -> list[dict]:
    return [enrich_row_source_promotion_v2(dict(x)) if isinstance(x, dict) else x for x in (rows or [])]


def summarize_source_promotion_v2(rows: list[dict]) -> dict[str, Any]:
    stage_counts = Counter()
    status_counts = Counter()
    list_counts = Counter()
    capped = []
    promoted = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        stage_counts[str(row.get("move_stage") or "unknown")] += 1
        status_counts[str(row.get("source_promotion_v2_status") or "unknown")] += 1
        list_counts[str(row.get("source_promotion_v2_list") or "unknown")] += 1
        compact = {
            "symbol": row.get("symbol"),
            "decision": row.get("decision"),
            "move_stage": row.get("move_stage"),
            "gain_at_detection": row.get("gain_at_detection"),
            "current_gain": row.get("current_gain"),
            "status": row.get("source_promotion_v2_status"),
        }
        if row.get("source_promotion_v2_capped"):
            capped.append(compact)
        if row.get("source_promotion_v2_promoted"):
            promoted.append(compact)
    return {
        "version": SOURCE_PROMOTION_ENGINE_V2_VERSION,
        "enabled": source_promotion_engine_v2_enabled(),
        "stage_counts": dict(stage_counts),
        "status_counts": dict(status_counts),
        "list_counts": dict(list_counts),
        "capped_count": len(capped),
        "promoted_count": len(promoted),
        "capped_samples": capped[:15],
        "promoted_samples": promoted[:15],
    }
