"""Learning Archive V1 for Stock Radar AI.

Creates compact learning features/outcomes from replay output without storing raw
Polygon candles.  The archive is intentionally small and safe for GitHub/app_data:
JSONL.GZ feature rows, JSONL.GZ outcome rows, pattern_scores.json and manifest.json.
"""
from __future__ import annotations

import gzip
import json
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from app.settings import DATA_DIR
from app.market_replay_lab import MARKET_REPLAY_LAB_VERSION, run_small_stock_classic_replay_from_polygon

LEARNING_ARCHIVE_VERSION = "learning_archive_v1_compact_replay_features_2026_06_19"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").replace("%", "").strip()
        n = float(v)
        if math.isnan(n) or math.isinf(n):
            return default
        return n
    except Exception:
        return default


def _round(v: Any, nd: int = 2) -> float:
    return round(_num(v), nd)


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    text = re.sub(r"_+", "_", text).strip("._-")
    return text[:80] or "learning_archive"


def _safe_list(v: Any, limit: int = 8) -> list[Any]:
    if isinstance(v, list):
        return v[:limit]
    if isinstance(v, tuple):
        return list(v)[:limit]
    return []


def _gain_bucket(peak: float) -> str:
    if peak >= 50:
        return "50pct_plus"
    if peak >= 30:
        return "30_50pct"
    if peak >= 20:
        return "20_30pct"
    if peak >= 10:
        return "10_20pct"
    return "under_10pct"


def _drawdown_bucket(drop: float) -> str:
    if drop <= -30:
        return "post_peak_drop_30pct_plus"
    if drop <= -20:
        return "post_peak_drop_20_30pct"
    if drop <= -10:
        return "post_peak_drop_10_20pct"
    if drop <= -5:
        return "post_peak_drop_5_10pct"
    return "post_peak_drop_under_5pct"


def _outcome_label(c: dict[str, Any]) -> str:
    peak = _num(c.get("max_after_pct"), 0.0)
    post_drop = _num(c.get("post_peak_drawdown_pct"), 0.0)
    retention = _num(c.get("gain_retention_pct"), 0.0)
    quick = _bool(c.get("quick_take_profit_candidate"))
    runner = _bool(c.get("runner_hold_candidate"))
    late = str(c.get("chase_risk_at_detection") or "") in {"late", "very_late"}
    if runner:
        return "runner_hold"
    if quick or post_drop <= -20 or late:
        return "quick_take_profit"
    if peak >= 20 and retention >= 50 and post_drop > -20:
        return "managed_winner"
    if peak >= 10:
        return "small_profit"
    return "weak_or_no_followthrough"


def _entry_quality_label(c: dict[str, Any]) -> str:
    peak = _num(c.get("max_after_pct"), 0.0)
    early = str(c.get("chase_risk_at_detection") or "") in {"early", "watch_carefully"}
    prior = _bool(c.get("candidate_from_previous_trading_session")) or _bool(c.get("detected_previous_session"))
    worst = _num(c.get("min_after_pct"), 0.0)
    if prior and early and peak >= 20 and worst > -10:
        return "clean_entry_winner"
    if prior and early and peak >= 10:
        return "early_prior_watch_profit"
    if str(c.get("chase_risk_at_detection") or "") in {"late", "very_late"}:
        return "late_chase_entry"
    return "watch_or_unproven"


def _pattern_key(c: dict[str, Any]) -> str:
    parts = [
        str(c.get("classic_state") or "unknown_setup"),
        str(c.get("phase_at_detection") or "unknown_phase"),
        "prev_session" if (_bool(c.get("candidate_from_previous_trading_session")) or _bool(c.get("detected_previous_session"))) else "new_symbol",
        str(c.get("chase_risk_at_detection") or "unknown_chase"),
    ]
    return "|".join(parts)


def _feature_row(c: dict[str, Any], run_meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "learning_feature_v1",
        "run_id": run_meta.get("run_id"),
        "window_label": run_meta.get("window_label"),
        "symbol": c.get("symbol"),
        "date": c.get("date"),
        "phase_at_detection": c.get("phase_at_detection"),
        "phase_at_detection_ar": c.get("phase_at_detection_ar"),
        "first_seen_time_utc": c.get("first_seen_time_utc"),
        "first_seen_price": _round(c.get("first_seen_price"), 4),
        "first_seen_change_pct": _round(c.get("first_seen_change_pct"), 2),
        "max_gain_before_detection_pct": _round(c.get("max_gain_before_detection_pct"), 2),
        "chase_risk_at_detection": c.get("chase_risk_at_detection"),
        "previous_trading_session_found": _bool(c.get("previous_trading_session_found")),
        "previous_trading_session_date": c.get("previous_trading_session_date"),
        "previous_session_age_days": c.get("previous_session_age_days"),
        "previous_session_gap_label_ar": c.get("previous_session_gap_label_ar"),
        "candidate_from_previous_trading_session": _bool(c.get("candidate_from_previous_trading_session")),
        "candidate_from_any_prior_session": _bool(c.get("candidate_from_any_prior_session")),
        "prior_candidate_count": int(_num(c.get("prior_candidate_count"), 0)),
        "previous_candidate_dates": _safe_list(c.get("previous_candidate_dates"), 10),
        "stage": c.get("stage"),
        "bucket": c.get("bucket"),
        "classic_state": c.get("classic_state"),
        "classic_score": c.get("classic_score"),
        "behavior_tags": _safe_list(c.get("behavior_tags"), 10),
        "detected_premarket_before_5pct": _bool(c.get("detected_premarket_before_5pct")),
        "detected_at_or_before_open": _bool(c.get("detected_at_or_before_open")),
        "pattern_key": _pattern_key(c),
        "entry_quality_label": _entry_quality_label(c),
    }


def _outcome_row(c: dict[str, Any], run_meta: dict[str, Any]) -> dict[str, Any]:
    peak = _num(c.get("max_after_pct"), 0.0)
    post_drop = _num(c.get("post_peak_drawdown_pct"), 0.0)
    return {
        "schema": "learning_outcome_v1",
        "run_id": run_meta.get("run_id"),
        "window_label": run_meta.get("window_label"),
        "symbol": c.get("symbol"),
        "date": c.get("date"),
        "pattern_key": _pattern_key(c),
        "max_after_pct": _round(peak, 2),
        "peak_gain_after_detection_pct": _round(c.get("peak_gain_after_detection_pct"), 2),
        "max_after_time_utc": c.get("max_after_time_utc"),
        "minutes_to_peak": c.get("minutes_to_peak"),
        "time_to_peak_label_ar": c.get("time_to_peak_label_ar"),
        "min_after_pct": _round(c.get("min_after_pct"), 2),
        "post_peak_drawdown_pct": _round(post_drop, 2),
        "minutes_to_drop_10pct_from_peak": c.get("minutes_to_drop_10pct_from_peak"),
        "gain_retention_pct": _round(c.get("gain_retention_pct"), 2),
        "runner_hold_candidate": _bool(c.get("runner_hold_candidate")),
        "quick_take_profit_candidate": _bool(c.get("quick_take_profit_candidate")),
        "exit_action_label_ar": c.get("exit_action_label_ar"),
        "exit_behavior_label_ar": c.get("exit_behavior_label_ar"),
        "risk_exit_flags": _safe_list(c.get("risk_exit_flags"), 8),
        "outcome_label": _outcome_label(c),
        "gain_bucket": _gain_bucket(peak),
        "post_peak_drawdown_bucket": _drawdown_bucket(post_drop),
    }


def _aggregate_pattern_scores(features: list[dict[str, Any]], outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    by_key: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    fmap = {(f.get("symbol"), f.get("date"), f.get("pattern_key")): f for f in features}
    for o in outcomes:
        f = fmap.get((o.get("symbol"), o.get("date"), o.get("pattern_key"))) or {}
        by_key[str(o.get("pattern_key") or "unknown")].append((f, o))

    patterns: list[dict[str, Any]] = []
    for key, rows in by_key.items():
        n = len(rows)
        peaks = [_num(o.get("max_after_pct"), 0.0) for _, o in rows]
        post = [_num(o.get("post_peak_drawdown_pct"), 0.0) for _, o in rows]
        retention = [_num(o.get("gain_retention_pct"), 0.0) for _, o in rows]
        runner_count = sum(1 for _, o in rows if _bool(o.get("runner_hold_candidate")))
        quick_count = sum(1 for _, o in rows if _bool(o.get("quick_take_profit_candidate")))
        clean_entry_count = sum(1 for f, _ in rows if str(f.get("entry_quality_label") or "") == "clean_entry_winner")
        late_count = sum(1 for f, _ in rows if str(f.get("chase_risk_at_detection") or "") in {"late", "very_late"})
        peak20_count = sum(1 for p in peaks if p >= 20.0)
        peak30_count = sum(1 for p in peaks if p >= 30.0)
        avg_peak = sum(peaks) / max(1, len(peaks))
        avg_post = sum(post) / max(1, len(post))
        med_retention = float(median(retention)) if retention else 0.0
        runner_pct = runner_count / n * 100.0
        quick_pct = quick_count / n * 100.0
        peak20_pct = peak20_count / n * 100.0
        risk_penalty = max(0.0, (quick_pct - runner_pct) / 100.0)
        pattern_score = round((peak20_pct * 0.45) + (runner_pct * 0.35) + max(0, med_retention) * 0.10 - (risk_penalty * 25.0) - (late_count / n * 10.0), 2)
        if n < 3:
            confidence = "low_sample"
        elif n < 8:
            confidence = "medium_sample"
        else:
            confidence = "better_sample"
        patterns.append({
            "pattern_key": key,
            "count": n,
            "confidence": confidence,
            "avg_peak_gain_pct": round(avg_peak, 2),
            "median_peak_gain_pct": round(float(median(peaks)) if peaks else 0.0, 2),
            "avg_post_peak_drawdown_pct": round(avg_post, 2),
            "median_gain_retention_pct": round(med_retention, 2),
            "peak20_count": peak20_count,
            "peak20_pct": round(peak20_pct, 1),
            "peak30_count": peak30_count,
            "peak30_pct": round(peak30_count / n * 100.0, 1),
            "runner_count": runner_count,
            "runner_pct": round(runner_pct, 1),
            "quick_take_profit_count": quick_count,
            "quick_take_profit_pct": round(quick_pct, 1),
            "clean_entry_count": clean_entry_count,
            "late_or_chase_count": late_count,
            "pattern_score": pattern_score,
            "learning_hint_ar": _pattern_learning_hint(pattern_score, runner_pct, quick_pct, late_count, n),
        })
    patterns.sort(key=lambda x: (x.get("pattern_score", 0), x.get("count", 0)), reverse=True)
    return {
        "schema": "pattern_scores_v1",
        "version": LEARNING_ARCHIVE_VERSION,
        "note_ar": "هذه أوزان مقترحة من Replay فقط وليست تعديل مباشر لقواعد الشراء. لا تُطبق إلا بعد اختبارها على نافذة لاحقة.",
        "patterns": patterns,
        "top_patterns": patterns[:20],
        "weak_patterns": sorted(patterns, key=lambda x: (x.get("pattern_score", 0), -x.get("count", 0)))[:20],
    }


def _pattern_learning_hint(score: float, runner_pct: float, quick_pct: float, late_count: int, n: int) -> str:
    if n < 3:
        return "عينة قليلة؛ استخدمها كملاحظة فقط ولا ترفع الوزن بعد."
    if runner_pct >= 25 and score >= 30:
        return "نمط واعد للـ Runner؛ يصلح لرفع أولوية الاحتفاظ الجزئي بعد اختبار لاحق."
    if quick_pct >= 50:
        return "نمط خطفة؛ لا تمنعه، لكن اعرضه بخطة بيع سريع وحجم أصغر."
    if late_count / max(1, n) >= 0.4:
        return "النمط يظهر متأخرًا كثيرًا؛ يحتاج فلتر دخول مبكر أو تحويله إلى Pullback فقط."
    if score >= 25:
        return "نمط جيد للمتابعة، لكن يحتاج تأكيد نافذة لاحقة."
    return "نمط ضعيف أو متذبذب؛ لا ترفع وزنه الآن."


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return path.stat().st_size


def _write_json(path: Path, payload: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.stat().st_size


def learning_archive_status() -> dict[str, Any]:
    return {
        "ok": True,
        "version": LEARNING_ARCHIVE_VERSION,
        "market_replay_lab_version": MARKET_REPLAY_LAB_VERSION,
        "storage_rule_ar": "Learning Archive V1 يحفظ أو يرجع features/outcomes مضغوطة فقط؛ لا يحفظ raw minute ولا سلاسل شموع كاملة.",
        "recommended_flow_ar": "ابدأ بنافذة 5 أيام اختبار، ثم 10 أيام متابعة + 5 أيام نتائج بنظام rolling بعد التأكد من system-cost-health.",
        "build_endpoint": "/learning-archive/build?end_date=2026-06-18&minute_days=5&daily_lookback_days=14&max_rows=250000&max_candidates=120&redownload_processed=true",
        "persist_example": "/learning-archive/build?end_date=2026-06-18&minute_days=5&daily_lookback_days=14&persist=true&include_rows=false",
    }


def build_learning_archive_from_replay(
    replay: dict[str, Any],
    *,
    window_label: str = "",
    persist: bool = False,
    include_rows: bool = False,
    output_root: str = "",
) -> dict[str, Any]:
    candidates = list(replay.get("candidates") or [])
    run_id = _slug(window_label or f"replay_{_s(replay.get('dates_seen'))}_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")
    run_meta = {
        "run_id": run_id,
        "window_label": window_label or run_id,
        "built_at_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    features = [_feature_row(c, run_meta) for c in candidates]
    outcomes = [_outcome_row(c, run_meta) for c in candidates]
    pattern_scores = _aggregate_pattern_scores(features, outcomes)
    runner_count = sum(1 for o in outcomes if _bool(o.get("runner_hold_candidate")))
    quick_count = sum(1 for o in outcomes if _bool(o.get("quick_take_profit_candidate")))
    peak20 = sum(1 for o in outcomes if _num(o.get("max_after_pct"), 0.0) >= 20.0)
    clean_entry = sum(1 for f in features if str(f.get("entry_quality_label") or "") == "clean_entry_winner")
    manifest = {
        "schema": "learning_archive_manifest_v1",
        "version": LEARNING_ARCHIVE_VERSION,
        "run_id": run_id,
        "window_label": run_meta["window_label"],
        "built_at_utc": run_meta["built_at_utc"],
        "source_versions": {
            "market_replay_lab": replay.get("version"),
            "opportunity_radar": replay.get("opportunity_radar_version"),
        },
        "replay_inputs": {
            "dates_seen": replay.get("dates_seen"),
            "files_seen_count": len(replay.get("files_seen") or []),
            "daily_files_seen_count": len(replay.get("daily_files_seen") or []),
            "rows_seen": replay.get("rows_seen"),
            "candidate_count": len(candidates),
            "row_cap_mode": replay.get("row_cap_mode"),
            "max_rows_per_file": replay.get("max_rows_per_file"),
        },
        "counts": {
            "feature_rows": len(features),
            "outcome_rows": len(outcomes),
            "pattern_count": len(pattern_scores.get("patterns") or []),
            "peak20_plus_count": peak20,
            "clean_entry_winner_count": clean_entry,
            "runner_hold_count": runner_count,
            "quick_take_profit_count": quick_count,
        },
        "safety_rules_ar": [
            "لا يحتوي على raw minute rows.",
            "لا يحتوي على سلاسل شموع كاملة لكل سهم.",
            "النتائج تستخدم لتعديل أوزان مقترحة فقط، وليس تغيير مباشر لقواعد Strong/Cautious.",
            "يجب اختبار أي وزن جديد على نافذة لاحقة لمنع خداع المستقبل.",
        ],
        "rolling_window_plan_ar": "المرحلة التالية: 10 أيام متابعة + 5 أيام نتائج، ثم تحريك النافذة 5 أيام للأمام ومقارنة الأوزان بدون استخدام المستقبل أثناء الترشيح.",
    }
    files: dict[str, Any] = {}
    if persist:
        root = Path(output_root or Path(DATA_DIR).parent / "app_data" / "learning_archive") / run_id
        features_path = root / "features.jsonl.gz"
        outcomes_path = root / "outcomes.jsonl.gz"
        patterns_path = root / "pattern_scores.json"
        manifest_path = root / "manifest.json"
        files = {
            "root": str(root),
            "features_jsonl_gz": {"path": str(features_path), "bytes": _write_jsonl_gz(features_path, features)},
            "outcomes_jsonl_gz": {"path": str(outcomes_path), "bytes": _write_jsonl_gz(outcomes_path, outcomes)},
            "pattern_scores_json": {"path": str(patterns_path), "bytes": _write_json(patterns_path, pattern_scores)},
            "manifest_json": {"path": str(manifest_path), "bytes": _write_json(manifest_path, manifest)},
        }
    return {
        "ok": True,
        "version": LEARNING_ARCHIVE_VERSION,
        "manifest": manifest,
        "pattern_scores": pattern_scores,
        "samples": {
            "features": features[:5],
            "outcomes": outcomes[:5],
        },
        "rows": {"features": features, "outcomes": outcomes} if include_rows else {"omitted": True, "hint_ar": "أضف include_rows=true إذا أردت إرجاع كل الصفوف في JSON؛ الافتراضي يحمي حجم الاستجابة."},
        "persisted": bool(persist),
        "files": files,
        "replay_summary": {
            "version": replay.get("version"),
            "timing_summary": replay.get("timing_summary"),
            "previous_session_context_summary": replay.get("previous_session_context_summary"),
            "railway_usage_guard": replay.get("railway_usage_guard"),
        },
        "note_ar": "هذا أرشيف تعلم compact من Replay. لا يدرّب النموذج ولا يغير قرارات الأداة؛ يجهز البيانات والأوزان المقترحة فقط.",
    }


def build_learning_archive_from_polygon(
    *,
    end_date: str = "",
    minute_days: int = 5,
    daily_lookback_days: int = 14,
    max_rows: int = 250_000,
    max_candidates: int = 120,
    redownload_processed: bool = True,
    force: bool = False,
    persist: bool = False,
    include_rows: bool = False,
    window_label: str = "",
) -> dict[str, Any]:
    replay = run_small_stock_classic_replay_from_polygon(
        end_date=end_date,
        minute_days=minute_days,
        daily_lookback_days=daily_lookback_days,
        max_rows=max_rows,
        max_candidates=max_candidates,
        redownload_processed=redownload_processed,
        force=force,
    )
    if not replay.get("ok"):
        return {
            "ok": False,
            "version": LEARNING_ARCHIVE_VERSION,
            "error": "replay_failed",
            "replay": replay,
        }
    label = window_label or f"learning_{end_date or 'latest'}_{minute_days}m_{daily_lookback_days}d"
    archive = build_learning_archive_from_replay(replay, window_label=label, persist=persist, include_rows=include_rows)
    archive["source_replay_ok"] = True
    return archive
