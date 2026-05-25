"""Source / Early Discovery V2 move-stage classifier.

This module is deliberately pure and Railway-safe:
- no HTTP/API calls
- no GitHub writes
- no heavy files
- no dependency on live workers

It answers the question the previous Early Movement layer could not answer:
"Is this stock genuinely early, already moved, continuation-only, or no-chase?"
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

MOVE_STAGE_VERSION = "source_early_discovery_v2_move_stage_2026_05_25_hotfix4_active_pullback_calibration"
NY_TZ = ZoneInfo("America/New_York")


def _env_bool(name: str, default: bool = True) -> bool:
    return str(os.getenv(name, "true" if default else "false") or ("true" if default else "false")).strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("%", "").replace(",", "").strip()
        return float(value)
    except Exception:
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _same_ny_day(ts: Any) -> bool:
    try:
        if not ts:
            return False
        dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S").replace(tzinfo=NY_TZ)
        return dt.date() == datetime.now(NY_TZ).date()
    except Exception:
        return False


def _fresh_peak_value(row: dict, journal: dict) -> float:
    """Return the strongest same-day / fresh movement seen by V2.

    This prevents a stock that already ran +10% intraday from being relabelled
    Pre-Move when a later cached row has current_gain=0.
    """
    candidates = [
        _safe_float(row.get("intraday_peak_gain"), 0.0),
        _safe_float(row.get("peak_gain_seen"), 0.0),
        _safe_float(journal.get("peak_gain_seen"), 0.0) if isinstance(journal, dict) else 0.0,
    ]
    peak_time = None
    if isinstance(journal, dict):
        peak_time = journal.get("late_seen_time") or journal.get("peak_gain_time") or journal.get("last_seen_time") or journal.get("updated_at")
    peak_time = peak_time or row.get("late_seen_time") or row.get("peak_gain_time") or row.get("journal_last_seen_time")
    fresh = _same_ny_day(peak_time)
    late_flag = bool(_safe_bool(row.get("late_seen_flag"), False) or (isinstance(journal, dict) and _safe_bool(journal.get("late_seen_flag"), False)))
    peak = max(candidates or [0.0])
    if fresh or (late_flag and peak >= 10):
        return peak
    return 0.0


def _first_number(row: dict, keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        try:
            if key in row and row.get(key) not in {None, ""}:
                val = _safe_float(row.get(key), default)
                if val != default or str(row.get(key)).strip() not in {"", "0", "0.0"}:
                    return val
        except Exception:
            continue
    return default


def extract_price(row: dict) -> float:
    if not isinstance(row, dict):
        return 0.0
    return _first_number(
        row,
        [
            "current_price_live",
            "display_price",
            "price",
            "current_price",
            "fmp_price",
            "live_price",
            "last_price",
        ],
        0.0,
    )


def extract_change_pct(row: dict) -> float:
    if not isinstance(row, dict):
        return 0.0
    return _first_number(
        row,
        [
            "display_change_pct",
            "change_vs_prev_close_pct",
            "change_pct",
            "changesPercentage",
            "fmp_change_pct",
            "live_change_pct",
            "day_change_pct",
            # Hotfix: rows rebuilt from cached snapshots may temporarily carry
            # display/live change as 0 while the detection journal has the latest
            # observed intraday gain.  Keep current_gain after explicit live fields
            # so real quote fields win, but the journal can still prevent a late
            # mover from being relabelled as Pre-Move.
            "current_gain",
            "journal_current_gain",
        ],
        0.0,
    )


def _distance_from_entry_pct(row: dict, price: float) -> float:
    entry = _first_number(row, ["display_entry_price", "entry", "entry_price", "buy_above"], 0.0)
    if price > 0 and entry > 0:
        return ((price - entry) / entry) * 100.0
    return 999.0


def _resistance_distance_pct(row: dict) -> float:
    return _first_number(row, ["nearest_resistance_distance_pct", "resistance_distance_pct", "distance_to_resistance_pct"], 999.0)


def _support_distance_pct(row: dict) -> float:
    return _first_number(row, ["nearest_support_distance_pct", "support_distance_pct", "distance_to_support_pct"], 999.0)


def _volume_ratio(row: dict) -> float:
    return _first_number(row, ["effective_volume_ratio", "volume_pace_ratio", "volume_ratio", "relative_volume", "rvol"], 0.0)


def _liquidity_score(row: dict) -> float:
    return _first_number(row, ["liquidity_persistence_score", "liquidity_score"], 0.0)


def _session_position(row: dict) -> float:
    intraday = row.get("intraday") if isinstance(row.get("intraday"), dict) else {}
    return _safe_float(intraday.get("session_position_pct", row.get("session_position_pct", 0)), 0.0)


def _has_close_resistance(row: dict, res_dist: float) -> bool:
    if _safe_bool(row.get("close_resistance_guard_flag"), False):
        return True
    label = _text(row.get("resistance_guard_label") or row.get("support_guard_label") or row.get("execution_gate_label"))
    if "مقاومة" in label and any(w in label for w in ["قريب", "خانقة", "قوية"]):
        return True
    return 0 <= res_dist <= 0.85


def _no_chase_existing(row: dict) -> bool:
    if _text(row.get("no_chase_guard_status")) == "no_chase":
        return True
    label = " ".join(
        [
            _text(row.get("no_chase_guard_label")),
            _text(row.get("execution_readiness_label")),
            _text(row.get("execution_gate_label")),
            _text(row.get("owner_action")),
        ]
    )
    return "لا تطارد" in label or "مطاردة" in label


def classify_move_stage(row: dict, journal: dict | None = None) -> dict[str, Any]:
    """Return a deterministic stage classification for one stock row.

    The classifier intentionally treats gain_at_detection as more important than
    current movement. If the tool first saw a stock at +19%, it remains late even
    if the current row later looks calmer or has weekly-watch metadata.
    """
    if not isinstance(row, dict):
        return {"version": MOVE_STAGE_VERSION, "move_stage": "Unknown", "move_stage_label": "غير معروف", "stage_allows_early_watch": False}

    price = extract_price(row)
    current_gain = extract_change_pct(row)
    journal = journal or row.get("detection_journal") or {}
    gain_at_detection = _safe_float(
        journal.get("gain_at_detection", row.get("gain_at_detection", current_gain)),
        current_gain,
    )
    peak_gain_seen = _fresh_peak_value(row, journal)
    max_gain_basis = max(current_gain, gain_at_detection, peak_gain_seen)
    entry_distance = _distance_from_entry_pct(row, price)
    res_dist = _resistance_distance_pct(row)
    support_dist = _support_distance_pct(row)
    volume = _volume_ratio(row)
    readiness = _first_number(row, ["execution_readiness_score", "readiness_score"], 0.0)
    quality = _first_number(row, ["quality_score", "core_quality", "display_rank_score"], 0.0)
    rr = _first_number(row, ["rr_1", "risk_reward", "reward_risk"], 0.0)
    liq_score = _liquidity_score(row)
    session_pos = _session_position(row)
    price_reliable = bool(row.get("price_reliable_for_execution", True))
    close_resistance = _has_close_resistance(row, res_dist)
    existing_no_chase = _no_chase_existing(row)

    reasons: list[str] = []
    blockers: list[str] = []
    action = "راقب فقط حتى تكتمل الشروط."
    stage = "Pre-Move"
    label = "🟣 مراقبة مبكرة"
    early_or_late = "early"
    allows_early_watch = True
    allows_strong = False
    allows_cautious = False
    allows_hot_lane = False
    rank_adjust = 0.0

    if gain_at_detection:
        reasons.append(f"أول اكتشاف عند {round(gain_at_detection, 2)}%")
    if current_gain:
        reasons.append(f"الحركة الحالية {round(current_gain, 2)}%")
    if peak_gain_seen >= 10 and peak_gain_seen > max(current_gain, gain_at_detection):
        reasons.append(f"أعلى حركة شوهدت اليوم {round(peak_gain_seen, 2)}%")

    if not price_reliable:
        blockers.append("السعر غير موثوق للتنفيذ")

    if existing_no_chase:
        blockers.append("حارس عدم المطاردة مفعّل")
    if close_resistance:
        blockers.append("قريب من مقاومة خانقة")
    if entry_distance != 999.0 and entry_distance > 4.5:
        blockers.append("السعر ابتعد عن منطقة الدخول")

    # Hard late-stage movement rules first.
    if max_gain_basis >= 50.0:
        stage = "Catalyst Spike Review"
        label = "🧨 انفجار/خبر — لا تطارد"
        early_or_late = "very_late"
        allows_early_watch = False
        action = "لا تدخل بعد الانفجار؛ انتظر reset أو pullback واضح جدًا."
        rank_adjust -= 30
    elif max_gain_basis >= 20.0:
        stage = "Extended"
        label = "🔴 ممتد / Already Moved"
        early_or_late = "late"
        allows_early_watch = False
        action = "السهم متأخر؛ لا يدخل مراقبة مبكرة، راقب فقط فرصة إعادة تمركز."
        rank_adjust -= 20
    elif max_gain_basis >= 10.0:
        stage = "Continuation Watch"
        label = "🔵 استمرار مشروط"
        early_or_late = "late_continuation"
        allows_early_watch = False
        action = "ليس دخولًا مباشرًا؛ يحتاج ثبات، pullback صحي، أو reclaim بسيولة."
        rank_adjust -= 8
        if close_resistance or existing_no_chase or (entry_distance != 999.0 and entry_distance > 5.5):
            stage = "Requires Pullback"
            label = "⏳ يحتاج Pullback"
            action = "انتظر pullback أو اختراق/ثبات جديد قبل أي دخول."
            rank_adjust -= 6
    elif 5.0 <= max_gain_basis < 10.0:
        stage = "Active Breakout"
        label = "⚡ تأكيد نشط"
        early_or_late = "active_confirmation"
        allows_early_watch = False
        allows_hot_lane = True
        allows_cautious = True
        action = "بداية اختراق نشطة؛ يمكن الترقية إذا بقي قريبًا من الدخول واستمرت السيولة."
        rank_adjust += 4
    elif 1.8 <= max_gain_basis < 5.0:
        stage = "Early Confirmation"
        label = "🟢 تأكيد مبكر"
        early_or_late = "early"
        allows_early_watch = True
        allows_hot_lane = True
        allows_cautious = True
        action = "بدأت الحركة مبكرًا؛ راقب استمرار السيولة والقرب من نقطة الدخول."
        rank_adjust += 8
    else:
        stage = "Pre-Move"
        label = "🟣 مراقبة مبكرة قبل الحركة"
        early_or_late = "pre_move"
        allows_early_watch = True
        action = "مرشح قبل الحركة؛ ليس دخولًا الآن حتى يظهر تأكيد حي."
        rank_adjust += 4

    # Convert unsafe execution context carefully.  Hotfix 4 keeps the hard
    # +10% peak guard, but avoids treating every clean sub-10% active move as
    # permanent No-Chase merely because resistance/entry metadata is incomplete
    # or temporarily conservative.  Under +10%, resistance/entry issues become
    # Requires Pullback unless an upstream *explicit* no-chase guard exists.
    unsafe_pullback_context = (close_resistance and max_gain_basis >= 5.0) or (entry_distance != 999.0 and entry_distance > 6.0 and max_gain_basis >= 5.0)
    if existing_no_chase or unsafe_pullback_context:
        if stage in {"Active Breakout", "Early Confirmation", "Continuation Watch", "Requires Pullback", "Extended", "Catalyst Spike Review"}:
            if stage in {"Extended", "Catalyst Spike Review"} or max_gain_basis >= 12.0 or existing_no_chase:
                stage = "No-Chase"
                label = "⛔ لا تطارد"
                early_or_late = "late_no_chase" if max_gain_basis >= 10.0 else "no_chase"
                allows_early_watch = False
                allows_hot_lane = False
                allows_cautious = False
                allows_strong = False
                action = "لا تدخل الآن؛ انتظر pullback/إعادة تمركز أو اختراق جديد مؤكد."
                rank_adjust -= 18
            elif max_gain_basis < 10.0:
                stage = "Requires Pullback"
                label = "⏳ يحتاج Pullback"
                early_or_late = "pullback_required"
                allows_early_watch = False
                allows_hot_lane = False
                allows_cautious = False
                allows_strong = False
                action = "الحركة نشطة لكنها غير نظيفة للتنفيذ الآن؛ انتظر pullback/ثبات/reclaim قبل الدخول."
                rank_adjust -= 6

    # Execution permissions: strict and explicit.
    if stage in {"Early Confirmation", "Active Breakout"}:
        if price_reliable and not close_resistance and not existing_no_chase and volume >= 1.0 and readiness >= 45 and quality >= 58:
            allows_cautious = True
        if price_reliable and stage == "Active Breakout" and volume >= 1.15 and readiness >= 58 and quality >= 70 and rr >= 0.7 and entry_distance <= 3.5 and not blockers:
            allows_strong = True
    elif stage == "Continuation Watch":
        # Continuation can only become actionable after an exceptional clean setup.
        allows_cautious = price_reliable and volume >= 1.25 and readiness >= 58 and quality >= 72 and entry_distance <= 2.0 and not close_resistance and not existing_no_chase
        allows_strong = allows_cautious and liq_score >= 58 and rr >= 0.8 and session_pos >= 55

    if volume >= 1.2:
        reasons.append("السيولة داعمة")
    elif volume and volume < 0.8:
        blockers.append("السيولة ضعيفة")
    if liq_score >= 55:
        reasons.append("استمرار السيولة مقبول")
    elif liq_score and liq_score < 42:
        blockers.append("استمرار السيولة غير كافٍ")
    if res_dist != 999 and res_dist >= 1.5:
        reasons.append("ليس ملاصقًا لمقاومة قريبة")
    if support_dist != 999 and 0 <= support_dist <= 3.5:
        reasons.append("قريب من دعم/منطقة حماية")

    return {
        "version": MOVE_STAGE_VERSION,
        "classified_at": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "move_stage": stage,
        "move_stage_label": label,
        "move_stage_action": action,
        "early_or_late_detection": early_or_late,
        "gain_at_detection": round(gain_at_detection, 4),
        "current_gain": round(current_gain, 4),
        "peak_gain_seen": round(peak_gain_seen, 4),
        "max_gain_basis": round(max_gain_basis, 4),
        "distance_from_entry_pct": round(entry_distance, 4) if entry_distance != 999.0 else None,
        "nearest_resistance_distance_pct_v2": round(res_dist, 4) if res_dist != 999.0 else None,
        "stage_allows_early_watch": bool(allows_early_watch),
        "stage_allows_hot_lane": bool(allows_hot_lane),
        "stage_allows_cautious": bool(allows_cautious),
        "stage_allows_strong": bool(allows_strong),
        "stage_rank_adjustment": round(rank_adjust, 2),
        "stage_reasons": reasons[:8],
        "stage_blockers": blockers[:8],
    }


def apply_move_stage_to_row(row: dict, journal: dict | None = None) -> dict:
    if not isinstance(row, dict):
        return row
    meta = classify_move_stage(row, journal=journal)
    row["move_stage_v2"] = meta
    for key in (
        "move_stage",
        "move_stage_label",
        "move_stage_action",
        "early_or_late_detection",
        "gain_at_detection",
        "current_gain",
        "peak_gain_seen",
        "max_gain_basis",
        "stage_allows_early_watch",
        "stage_allows_hot_lane",
        "stage_allows_cautious",
        "stage_allows_strong",
        "stage_rank_adjustment",
        "stage_reasons",
        "stage_blockers",
    ):
        row[key] = meta.get(key)
    return row


def movement_is_late(row: dict) -> bool:
    stage = _text((row.get("move_stage_v2") or {}).get("move_stage") or row.get("move_stage"))
    return stage in {"Continuation Watch", "Already Moved", "Extended", "Requires Pullback", "No-Chase", "Catalyst Spike Review"}
