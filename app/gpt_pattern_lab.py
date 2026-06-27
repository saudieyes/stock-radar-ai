"""GPT Pattern Lab V2W13.

A lightweight, no-lookahead pattern layer for Stock Radar AI.

The module has two jobs:
1) Enrich current radar rows with transparent, non-actionable pattern tags.
2) Run a small simulator/backtest over stored intraday bars when available.

It intentionally does NOT create BUY_NOW decisions.  Strong/Cautious remain under
existing gates.  Pattern tags can feed monitoring/pre-trigger sections and later
Promotion Engine audits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

try:
    from .settings import DATA_DIR
    from .sqlite_store import SQLITE_DB_PATH
except Exception:  # pragma: no cover - safe import fallback for local tests
    DATA_DIR = Path(os.getenv("APP_DATA_DIR", "/tmp"))
    SQLITE_DB_PATH = str(Path(DATA_DIR) / "stock_radar_ai.sqlite3")

GPT_PATTERN_LAB_VERSION = "gpt_pattern_lab_v2w15d_pivot_stage_quality_report_2026_06_27"
GPT_PATTERN_CALIBRATION_VERSION = "pattern_lab_scoring_calibration_v2w15d_pivot_stage_quality_report_2026_06_27"

# Patterns intentionally separated into analyst-derived vs GPT custom so the
# simulator can rank them independently and we do not over-trust any single idea.
ANALYST_PATTERN_IDS = {
    "elephant_trunk_drop",
    "strong_bos_bullish",
    "weak_bos_bullish",
    "strong_bos_bearish",
    "weak_bos_bearish",
    "tasuki_gap_bullish",
    "tasuki_gap_bearish",
    "tweezer_bottom",
    "tweezer_top",
}
GPT_ALPHA_PATTERN_IDS = {
    "gpt_liquidity_coil_reclaim",
    "gpt_silent_compression_break",
    "gpt_second_wave_controlled_pullback",
    "gpt_smart_pivot_reset",
}

_PATTERN_AR = {
    "elephant_trunk_drop": "خرطوم الفيل الهابط — رفض علوي ثم سيطرة بائعين",
    "strong_bos_bullish": "كسر هيكل قوي صاعد BOS",
    "weak_bos_bullish": "كسر هيكل ضعيف صاعد BOS — يحتاج تأكيد",
    "strong_bos_bearish": "كسر هيكل قوي هابط BOS",
    "weak_bos_bearish": "كسر هيكل ضعيف هابط BOS — تحذير فقط",
    "tasuki_gap_bullish": "فجوة تاسوكي صاعدة — استمرار اتجاه بعد تصحيح غير كاسر",
    "tasuki_gap_bearish": "فجوة تاسوكي هابطة — استمرار هبوط بعد تصحيح غير كاسر",
    "tweezer_bottom": "ملقاط قاع — فشل كسر نفس القاع واحتمال انعكاس",
    "tweezer_top": "ملقاط قمة — فشل اختراق نفس القمة واحتمال انعكاس",
    "gpt_liquidity_coil_reclaim": "GPT Alpha: مصيدة سيولة + ضغط صامت + استرداد",
    "gpt_silent_compression_break": "GPT Alpha: ضغط صامت قبل الانفجار",
    "gpt_second_wave_controlled_pullback": "GPT Alpha: موجة ثانية بعد Pullback منضبط",
    "gpt_smart_pivot_reset": "GPT Alpha: الارتكاز الذكي — Reset + قاع أعلى + استرداد",
}

_BEARISH_PATTERN_IDS = {"elephant_trunk_drop", "strong_bos_bearish", "weak_bos_bearish", "tasuki_gap_bearish", "tweezer_top"}


# V2W13b turns the first replay lessons into explicit, conservative routing rules.
# These numbers are not permanent truths; they are the first calibration from
# 2026-06-26 replay and should be re-ranked by more Polygon sessions before any
# automatic trading decision.  The goal is: better monitoring/ranking + stronger
# risk guards, never BUY_NOW by pattern alone.
_PATTERN_CALIBRATION = {
    "tweezer_bottom": {
        "role": "bullish_setup",
        "recommended_bucket": "support_bounce",
        "promotion_hint": "support_bounce_or_reclaim_watch",
        "score_bonus": 12.0,
        "min_live_score": 64.0,
        "replay_win_rate_proxy": 72.73,
        "replay_avg_gain_proxy": 9.20,
        "replay_avg_drawdown_proxy": -3.78,
        "requires_confirmation": True,
        "activation_rule_ar": "يرتفع إلى Support Bounce/Reclaim فقط إذا كان بعد هبوط وقرب دعم، مع تفعيل فوق قمة شمعة الملقاط أو استرداد واضح؛ ليس BUY_NOW وحده.",
        "leaderboard_note_ar": "أفضل نمط صاعد في أول Replay؛ يحتاج دعم/سياق حتى لا يتحول إلى ضجيج.",
    },
    "elephant_trunk_drop": {
        "role": "risk_guard",
        "recommended_bucket": "continuation_pullback",
        "promotion_hint": "no_chase_risk_guard",
        "score_bonus": 10.0,
        "min_live_score": 60.0,
        "replay_win_rate_proxy": 65.62,
        "replay_avg_gain_proxy": 5.92,
        "replay_avg_drawdown_proxy": -4.78,
        "requires_confirmation": False,
        "activation_rule_ar": "حماية من المطاردة بعد اندفاع وذيل علوي وبيع لاحق؛ لا يُستخدم كشراء، بل يخفض الدرجة أو ينقل إلى No-Chase/Continuation Pullback.",
        "leaderboard_note_ar": "مفيد جدًا كحارس خطر؛ نجاحه يعني حمايتنا من دخول سيئ لا أنه فرصة شراء.",
    },
    "tweezer_top": {
        "role": "risk_guard",
        "recommended_bucket": "continuation_pullback",
        "promotion_hint": "top_rejection_guard",
        "score_bonus": 8.0,
        "min_live_score": 62.0,
        "replay_win_rate_proxy": 55.56,
        "replay_avg_gain_proxy": 8.03,
        "replay_avg_drawdown_proxy": -3.63,
        "requires_confirmation": False,
        "activation_rule_ar": "اختبار قمة مرتين بعد صعود ثم رفض؛ يُعامل كتحذير مطاردة أو انتظار Pullback لا كدخول شراء.",
        "leaderboard_note_ar": "يحمي من القمم ويحتاج ربطه بحالة الامتداد حتى لا يخفض فرصًا صحيحة مبكرًا.",
    },
    "gpt_second_wave_controlled_pullback": {
        "role": "bullish_setup",
        "recommended_bucket": "continuation_pullback",
        "promotion_hint": "second_wave_watch",
        "score_bonus": 10.0,
        "min_live_score": 66.0,
        "replay_win_rate_proxy": 52.94,
        "replay_avg_gain_proxy": 5.38,
        "replay_avg_drawdown_proxy": -3.79,
        "requires_confirmation": True,
        "activation_rule_ar": "لا نطارد الموجة الأولى؛ نراقب Pullback منضبط ثم تفعيل فوق قمة صغيرة/استرداد VWAP أو متوسط قريب.",
        "leaderboard_note_ar": "أفضل نمط GPT Alpha مبدئيًا للتداول العملي لأنه ينتظر موجة ثانية بدل المطاردة.",
    },
    "gpt_smart_pivot_reset": {
        "role": "bullish_setup_needs_confirmation",
        "recommended_bucket": "support_bounce",
        "promotion_hint": "smart_pivot_trigger_required",
        "score_bonus": 3.0,
        "min_live_score": 72.0,
        "replay_win_rate_proxy": 47.27,
        "replay_avg_gain_proxy": 5.81,
        "replay_avg_drawdown_proxy": -3.83,
        "requires_confirmation": True,
        "activation_rule_ar": "نمط ارتكاز ذكي مشروط: اندفاع سابق ثم Reset وقاع أعلى، لكن لا يترقى عمليًا إلا إذا كان قريبًا من trigger، ومخاطرة الوقف معقولة، أو حدث كسر/ثبات فوق trigger الارتكاز.",
        "leaderboard_note_ar": "V2W15b replay أظهر أن Confirmed أفضل من Trigger Ready؛ لذلك صار Confirmed فقط يذهب إلى Support Bounce، بينما Trigger Ready يبقى Pre-Trigger مراقبة لصيقة.",
    },
    "strong_bos_bullish": {
        "role": "bullish_setup_needs_confirmation",
        "recommended_bucket": "pre_trigger",
        "promotion_hint": "bos_hold_or_retest_required",
        "score_bonus": 2.0,
        "min_live_score": 72.0,
        "replay_win_rate_proxy": 51.47,
        "replay_avg_gain_proxy": 10.75,
        "replay_avg_drawdown_proxy": -3.90,
        "requires_confirmation": True,
        "activation_rule_ar": "كسر قوي واعد لكن لا يدخل وحده؛ يحتاج ثبات فوق مستوى الكسر أو إعادة اختبار ناجحة/حجم استمرار.",
        "leaderboard_note_ar": "أعلى متوسط صعود لكنه كثير الإشارات؛ نرفعه كـ Pre-Trigger مؤكد لا كدخول مباشر.",
    },
    "gpt_silent_compression_break": {
        "role": "early_watch",
        "recommended_bucket": "pre_trigger",
        "promotion_hint": "early_compression_watch",
        "score_bonus": -2.0,
        "min_live_score": 74.0,
        "replay_win_rate_proxy": 43.33,
        "replay_avg_gain_proxy": 6.50,
        "replay_avg_drawdown_proxy": -2.62,
        "requires_confirmation": True,
        "activation_rule_ar": "يراقب الضغط قبل الانفجار فقط؛ يحتاج اختراق نطاق الضغط أو دخول حجم جديد قبل الترقية.",
        "leaderboard_note_ar": "يلتقط مبكرًا لكن يعطي ضجيجًا؛ يبقى Early Watch حتى تزيد جلسات المحاكاة.",
    },
    "gpt_liquidity_coil_reclaim": {
        "role": "bullish_setup_needs_confirmation",
        "recommended_bucket": "reclaim",
        "promotion_hint": "reclaim_confirmation_required",
        "score_bonus": 0.0,
        "min_live_score": 70.0,
        "replay_win_rate_proxy": 43.24,
        "replay_avg_gain_proxy": 4.97,
        "replay_avg_drawdown_proxy": -2.82,
        "requires_confirmation": True,
        "activation_rule_ar": "مصيدة سيولة + استرداد؛ يحتاج شمعة تالية أو ثبات فوق مستوى الاسترداد قبل الرفع العملي.",
        "leaderboard_note_ar": "جيد للمراقبة؛ لا يكفي وحده بسبب نسبة نجاح أولية متوسطة.",
    },
    "tasuki_gap_bullish": {
        "role": "continuation_setup",
        "recommended_bucket": "continuation_pullback",
        "promotion_hint": "gap_hold_continuation_watch",
        "score_bonus": 3.0,
        "min_live_score": 70.0,
        "replay_win_rate_proxy": 50.0,
        "replay_avg_gain_proxy": 9.10,
        "replay_avg_drawdown_proxy": -3.66,
        "requires_confirmation": True,
        "activation_rule_ar": "يُقبل فقط إذا بقيت الفجوة محفوظة ولم يتحول السهم إلى مطاردة ممتدة.",
        "leaderboard_note_ar": "مفيد كاستمرار اتجاه لكن يحتاج بيانات فجوات دقيقة وسياق جلسة.",
    },
    "tasuki_gap_bearish": {
        "role": "risk_guard",
        "recommended_bucket": "continuation_pullback",
        "promotion_hint": "bearish_gap_guard",
        "score_bonus": 5.0,
        "min_live_score": 62.0,
        "replay_win_rate_proxy": 71.43,
        "replay_avg_gain_proxy": 5.79,
        "replay_avg_drawdown_proxy": -5.72,
        "requires_confirmation": False,
        "activation_rule_ar": "فجوة هابطة محفوظة تعني تحذير استمرار هبوط؛ لا تُستخدم كشراء.",
        "leaderboard_note_ar": "عدد إشارات قليل لكنه حارس خطر قوي؛ يحتاج عينة أكبر.",
    },
    "strong_bos_bearish": {
        "role": "risk_guard",
        "recommended_bucket": "continuation_pullback",
        "promotion_hint": "invalidated_structure_guard",
        "score_bonus": 4.0,
        "min_live_score": 62.0,
        "replay_win_rate_proxy": 30.43,
        "replay_avg_gain_proxy": 6.21,
        "replay_avg_drawdown_proxy": -2.16,
        "requires_confirmation": False,
        "activation_rule_ar": "كسر هيكل هابط يخفض أو يخرج فرصة الشراء حتى يظهر reclaim جديد.",
        "leaderboard_note_ar": "حارس خطر لا يعتمد عليه وحده بسبب نتائج أولية مختلطة.",
    },
    "weak_bos_bullish": {
        "role": "weak_watch",
        "recommended_bucket": "pre_trigger",
        "promotion_hint": "weak_bos_wait_confirmation",
        "score_bonus": -8.0,
        "min_live_score": 76.0,
        "replay_win_rate_proxy": 33.33,
        "replay_avg_gain_proxy": 4.59,
        "replay_avg_drawdown_proxy": -3.05,
        "requires_confirmation": True,
        "activation_rule_ar": "كسر ضعيف لا يرفع السهم وحده؛ ينتظر Strong BOS أو ثبات/حجم.",
        "leaderboard_note_ar": "تعليمي وتحذيري أكثر من كونه نمط دخول.",
    },
}


def _calibration_for(pattern_id: str) -> dict:
    return dict(_PATTERN_CALIBRATION.get(_s(pattern_id), {
        "role": "unranked_watch",
        "recommended_bucket": "pre_trigger",
        "promotion_hint": "unranked_pattern_watch",
        "score_bonus": 0.0,
        "min_live_score": 72.0,
        "requires_confirmation": True,
        "activation_rule_ar": "نمط غير معاير بعد؛ مراقبة فقط حتى تتوفر نتائج محاكاة أكثر.",
    }))


def _apply_match_calibration(match: dict) -> dict:
    if not isinstance(match, dict):
        return match
    pid = _s(match.get("pattern_id"))
    cal = _calibration_for(pid)
    base_score = _f(match.get("score"))
    calibrated_score = max(0.0, min(100.0, base_score + _f(cal.get("score_bonus"))))
    out = dict(match)
    out["calibration_version"] = GPT_PATTERN_CALIBRATION_VERSION
    out["lab_role"] = cal.get("role") or "unranked_watch"
    out["recommended_bucket"] = cal.get("recommended_bucket") or "pre_trigger"
    out["promotion_hint"] = cal.get("promotion_hint") or "pattern_watch"
    out["requires_confirmation"] = bool(cal.get("requires_confirmation", True))
    out["activation_rule_ar"] = cal.get("activation_rule_ar") or "مراقبة فقط حتى تأكيد إضافي."
    out["replay_win_rate_proxy"] = _round(cal.get("replay_win_rate_proxy"), 2)
    out["replay_avg_gain_proxy"] = _round(cal.get("replay_avg_gain_proxy"), 2)
    out["replay_avg_drawdown_proxy"] = _round(cal.get("replay_avg_drawdown_proxy"), 2)
    out["calibrated_score"] = _round(calibrated_score, 2)
    # V2W15c: Smart Pivot is stage-routed. Trigger Ready is not Support Bounce yet.
    if pid == "gpt_smart_pivot_reset":
        stage = _s(out.get("pivot_stage"))
        action = _s(out.get("action"))
        risk_pct = _f(out.get("risk_pct"), 99.0)
        if stage == "pivot_confirmed" and action == "smart_pivot_confirmed_watch" and risk_pct <= 8.0:
            out["recommended_bucket"] = "support_bounce"
            out["promotion_hint"] = "smart_pivot_confirmed_support_bounce"
        elif stage == "pivot_trigger_ready" and action == "smart_pivot_trigger_ready" and risk_pct <= 7.0:
            out["recommended_bucket"] = "pre_trigger"
            out["promotion_hint"] = "smart_pivot_trigger_ready_pre_trigger"
        else:
            out["recommended_bucket"] = "pre_trigger"
            out["promotion_hint"] = "smart_pivot_watch_pre_trigger_only"
    if out.get("lab_role") == "risk_guard" or pid in _BEARISH_PATTERN_IDS:
        out["risk_guard_strength"] = _round(max(calibrated_score, base_score), 2)
    else:
        out["bullish_setup_score"] = _round(calibrated_score, 2)
    if cal.get("leaderboard_note_ar"):
        out["leaderboard_note_ar"] = cal.get("leaderboard_note_ar")
    return out


def _calibrated_score(match: dict) -> float:
    if not isinstance(match, dict):
        return 0.0
    return max(_f(match.get("calibrated_score")), _f(match.get("score")))


def _match_is_bullish_setup(match: dict) -> bool:
    role = _s(match.get("lab_role"))
    return _s(match.get("direction")).lower() == "bullish" and role in {"bullish_setup", "bullish_setup_needs_confirmation", "continuation_setup", "early_watch", "weak_watch"}


def _match_is_risk_guard(match: dict) -> bool:
    return _s(match.get("lab_role")) == "risk_guard" or _s(match.get("pattern_id")) in _BEARISH_PATTERN_IDS


def _now_iso() -> str:
    try:
        return datetime.now(timezone.utc).isoformat()
    except Exception:
        return ""


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _pct(a: float, b: float) -> float:
    return ((a - b) / b * 100.0) if b else 0.0


def _round(v: Any, n: int = 2) -> float:
    try:
        return round(float(v), n)
    except Exception:
        return 0.0


def _bar(raw: dict) -> dict:
    """Normalize common Polygon/FMP/CSV bar shapes."""
    if not isinstance(raw, dict):
        raw = {}
    o = _f(raw.get("open", raw.get("o")))
    h = _f(raw.get("high", raw.get("h")))
    l = _f(raw.get("low", raw.get("l")))
    c = _f(raw.get("close", raw.get("c", raw.get("price"))))
    v = _f(raw.get("volume", raw.get("v")))
    ts = raw.get("bar_ts", raw.get("timestamp", raw.get("t", raw.get("datetime", raw.get("time", "")))))
    if h <= 0 and c > 0:
        h = max(o or c, c)
    if l <= 0 and c > 0:
        l = min(o or c, c)
    if o <= 0 and c > 0:
        o = c
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "ts": ts, "raw": raw}


def _bars(raw_bars: Iterable[dict] | None) -> list[dict]:
    return [_bar(x) for x in (raw_bars or []) if isinstance(x, dict)]


def _body(b: dict) -> float:
    return abs(_f(b.get("close")) - _f(b.get("open")))


def _range(b: dict) -> float:
    return max(0.0, _f(b.get("high")) - _f(b.get("low")))


def _upper_wick(b: dict) -> float:
    return max(0.0, _f(b.get("high")) - max(_f(b.get("open")), _f(b.get("close"))))


def _lower_wick(b: dict) -> float:
    return max(0.0, min(_f(b.get("open")), _f(b.get("close"))) - _f(b.get("low")))


def _is_green(b: dict) -> bool:
    return _f(b.get("close")) > _f(b.get("open"))


def _is_red(b: dict) -> bool:
    return _f(b.get("close")) < _f(b.get("open"))


def _avg(vals: list[float]) -> float:
    vals = [float(x) for x in vals if x is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _recent_trend_pct(bars: list[dict], lookback: int = 6) -> float:
    if len(bars) < 2:
        return 0.0
    part = bars[-min(len(bars), max(2, lookback)):]
    return _pct(_f(part[-1].get("close")), _f(part[0].get("open")) or _f(part[0].get("close")))


def _ema_like(values: list[float], span: int = 8) -> float:
    """Small EMA-like helper without pandas; used for pivot reset context."""
    vals = [float(x) for x in values if x and x > 0]
    if not vals:
        return 0.0
    alpha = 2.0 / (max(2, int(span)) + 1.0)
    ema = vals[0]
    for v in vals[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def _highest_before_tail(bars: list[dict], exclude_tail: int = 4) -> float:
    part = bars[:-max(1, int(exclude_tail))] if len(bars) > exclude_tail else bars[:-1]
    return max([_f(b.get("high")) for b in part] or [0.0])


def _lowest_before_tail(bars: list[dict], exclude_tail: int = 4) -> float:
    part = bars[:-max(1, int(exclude_tail))] if len(bars) > exclude_tail else bars[:-1]
    lows = [_f(b.get("low")) for b in part if _f(b.get("low")) > 0]
    return min(lows) if lows else 0.0


def _add(matches: list[dict], pattern_id: str, score: float, direction: str, reasons: list[str], *, action: str = "monitor", trigger: float = 0.0, stop: float = 0.0, target: float = 0.0, confidence: str = "medium", extra: dict | None = None) -> None:
    match = {
        "pattern_id": pattern_id,
        "pattern_name_ar": _PATTERN_AR.get(pattern_id, pattern_id),
        "family": "gpt_alpha" if pattern_id in GPT_ALPHA_PATTERN_IDS else "analyst_lesson",
        "score": _round(max(0.0, min(100.0, score)), 2),
        "direction": direction,
        "action": action,
        "confidence": confidence,
        "trigger": _round(trigger, 4),
        "stop": _round(stop, 4),
        "target1": _round(target, 4),
        "reasons_ar": [str(x) for x in reasons if x][:6],
        "execution_note_ar": "مختبر أنماط فقط؛ لا يتحول إلى BUY_NOW إلا بعد بوابات الشرعية/السيولة/الخطة/التأكيد.",
    }
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in match:
                match[k] = v
    matches.append(_apply_match_calibration(match))


def detect_patterns_from_bars(symbol: str, raw_bars: list[dict], previous_close: float = 0.0, timeframe: str = "5m") -> dict:
    bars = _bars(raw_bars)
    sym = _u(symbol)
    matches: list[dict] = []
    if len(bars) < 3:
        return {
            "ok": False,
            "version": GPT_PATTERN_LAB_VERSION,
            "symbol": sym,
            "timeframe": timeframe,
            "bar_count": len(bars),
            "matches": [],
            "best_pattern": {},
            "score": 0.0,
            "bias": "insufficient_bars",
            "note_ar": "لا توجد شموع كافية لاختبار النماذج.",
        }

    last = bars[-1]
    prev = bars[-2]
    third = bars[-3]
    closes = [_f(b.get("close")) for b in bars if _f(b.get("close")) > 0]
    vols = [_f(b.get("volume")) for b in bars if _f(b.get("volume")) > 0]
    recent = bars[-min(12, len(bars)):]
    avg_range = _avg([_range(b) for b in recent[:-1]]) or _range(last) or 1.0
    avg_vol = _avg([_f(b.get("volume")) for b in recent[:-1] if _f(b.get("volume")) > 0])
    price = _f(last.get("close"))
    recent_high = max([_f(b.get("high")) for b in recent[:-1]] or [0.0])
    recent_low = min([_f(b.get("low")) for b in recent[:-1] if _f(b.get("low")) > 0] or [0.0])
    trigger = max(_f(last.get("high")), recent_high)
    stop = min(_f(last.get("low")), recent_low) if recent_low else _f(last.get("low"))

    # Elephant Trunk Drop: quick liquidity run-up, long upper-wick rejection, bearish follow-through.
    if len(bars) >= 4:
        prior_window = bars[-8:-2] if len(bars) >= 8 else bars[:-2]
        prior_base = _f(prior_window[0].get("open")) if prior_window else _f(third.get("open"))
        pump_high = max([_f(b.get("high")) for b in bars[-5:-1]] or [0.0])
        pump_pct = _pct(pump_high, prior_base) if prior_base else 0.0
        reject = prev
        follow = last
        reject_upper = _upper_wick(reject)
        reject_body = max(_body(reject), 0.01)
        follow_body_ratio = _body(follow) / max(avg_range, 0.01)
        if pump_pct >= 3.0 and reject_upper >= reject_body * 1.45 and _is_red(follow) and _f(follow.get("close")) < _f(reject.get("close")) and follow_body_ratio >= 0.45:
            score = 55 + min(25, pump_pct * 2.0) + min(15, follow_body_ratio * 12.0)
            if avg_vol and _f(follow.get("volume")) > avg_vol * 1.25:
                score += 8
            _add(matches, "elephant_trunk_drop", score, "bearish", [
                f"ارتفاع سريع قبل الرفض بنحو {round(pump_pct,1)}% ثم ذيل علوي طويل.",
                "شمعة رفض ثم شمعة هابطة لاحقة تؤكد تحول الزخم للبائعين.",
                "لأداة الشراء: هذا نمط حماية/No-Chase وليس نمط دخول شراء.",
            ], action="risk_guard_no_chase", trigger=_f(reject.get("low")), stop=_f(reject.get("high")), target=recent_low, confidence="high" if score >= 78 else "medium")

    # BOS: compare last close to structural highs/lows.
    if len(bars) >= 7:
        weak_high = max(_f(b.get("high")) for b in bars[-6:-1])
        weak_low = min(_f(b.get("low")) for b in bars[-6:-1] if _f(b.get("low")) > 0)
        struct_high = max(_f(b.get("high")) for b in bars[-min(22, len(bars)):-1])
        struct_low = min(_f(b.get("low")) for b in bars[-min(22, len(bars)):-1] if _f(b.get("low")) > 0)
        vol_ratio = (_f(last.get("volume")) / avg_vol) if avg_vol else 1.0
        body_ratio = _body(last) / max(avg_range, 0.01)
        if price > struct_high and body_ratio >= 0.55:
            score = 66 + min(14, body_ratio * 8) + min(14, max(0, vol_ratio - 1) * 10)
            _add(matches, "strong_bos_bullish", score, "bullish", [
                "إغلاق فوق قمة هيكلية رئيسية وليس فقط قمة فرعية.",
                f"زخم الكسر واضح: body/range≈{round(body_ratio,2)}، volume ratio≈{round(vol_ratio,2)}.",
            ], action="pre_trigger_candidate", trigger=price, stop=max(struct_high * 0.985, _f(last.get("low"))), target=price + max(avg_range * 1.7, price * 0.035), confidence="high" if score >= 78 else "medium")
        elif price > weak_high:
            score = 48 + min(15, body_ratio * 7) + min(8, max(0, vol_ratio - 1) * 8)
            _add(matches, "weak_bos_bullish", score, "bullish", [
                "كسر قمة فرعية صغيرة؛ ليس كسرًا هيكليًا رئيسيًا.",
                "يفضل انتظار تأكيد فوق المستوى أو حجم أكبر قبل الترقية.",
            ], action="monitor_wait_confirmation", trigger=weak_high, stop=_f(last.get("low")), target=weak_high + max(avg_range * 1.2, weak_high * 0.025), confidence="low")
        if price < struct_low and body_ratio >= 0.55:
            score = 64 + min(15, body_ratio * 8) + min(14, max(0, vol_ratio - 1) * 10)
            _add(matches, "strong_bos_bearish", score, "bearish", [
                "كسر قاع هيكلي رئيسي بزخم واضح.",
                "لأداة الشراء: حماية من الدخول وإخراج/خفض درجة السهم.",
            ], action="risk_guard_invalidated", trigger=struct_low, stop=struct_low, target=price - max(avg_range * 1.5, price * 0.03), confidence="high" if score >= 76 else "medium")
        elif price < weak_low:
            score = 42 + min(12, body_ratio * 7)
            _add(matches, "weak_bos_bearish", score, "bearish", [
                "كسر قاع فرعي صغير؛ تحذير وليس حكمًا نهائيًا.",
                "ينتظر هل يسترد المستوى أم يتحول إلى كسر قوي.",
            ], action="monitor_risk", trigger=weak_low, stop=weak_low, target=price - avg_range, confidence="low")

    # Tasuki Gap: gap continuation with corrective candle that does not fill the gap.
    if len(bars) >= 3:
        a, b, c = third, prev, last
        gap_up = _f(b.get("low")) > _f(a.get("high"))
        gap_down = _f(b.get("high")) < _f(a.get("low"))
        if gap_up and _is_green(b) and _is_red(c) and _f(c.get("low")) > _f(a.get("high")):
            gap_size = _pct(_f(b.get("low")), _f(a.get("high")))
            score = 58 + min(20, gap_size * 7) + (8 if avg_vol and _f(b.get("volume")) > avg_vol else 0)
            _add(matches, "tasuki_gap_bullish", score, "bullish", [
                "فجوة صعودية ثم تصحيح صغير لم يكسر الفجوة.",
                "يدل على استمرار سيطرة المشترين ما دام قاع الفجوة محفوظًا.",
            ], action="continuation_watch", trigger=_f(b.get("high")), stop=_f(a.get("high")), target=_f(b.get("high")) + max(avg_range * 1.5, price * 0.03), confidence="medium")
        if gap_down and _is_red(b) and _is_green(c) and _f(c.get("high")) < _f(a.get("low")):
            gap_size = _pct(_f(a.get("low")), _f(b.get("high")))
            score = 58 + min(20, gap_size * 7)
            _add(matches, "tasuki_gap_bearish", score, "bearish", [
                "فجوة هابطة ثم تصحيح صغير لم يغلق الفجوة.",
                "لأداة الشراء: تحذير استمرار هبوط وليس دخول شراء.",
            ], action="risk_guard_no_chase", trigger=_f(b.get("low")), stop=_f(a.get("low")), target=_f(b.get("low")) - max(avg_range * 1.5, price * 0.03), confidence="medium")

    # Tweezer top/bottom: two bars test the same high/low after a directional move.
    if len(bars) >= 4 and price > 0:
        tol = max(price * 0.0035, avg_range * 0.18, 0.01)
        trend = _recent_trend_pct(bars[:-1], 6)
        same_low = abs(_f(prev.get("low")) - _f(last.get("low"))) <= tol
        same_high = abs(_f(prev.get("high")) - _f(last.get("high"))) <= tol
        if same_low and trend <= -2.0 and _is_green(last):
            score = 54 + min(22, abs(trend) * 2.2) + (8 if _lower_wick(last) > _body(last) else 0)
            _add(matches, "tweezer_bottom", score, "bullish", [
                "اختبار نفس القاع تقريبًا مرتين بعد هبوط.",
                "فشل كسر المستوى مع شمعة خضراء يعطي احتمال انعكاس/ارتداد.",
            ], action="support_bounce_watch", trigger=max(_f(prev.get("high")), _f(last.get("high"))), stop=min(_f(prev.get("low")), _f(last.get("low"))), target=price + max(avg_range * 1.6, price * 0.03), confidence="medium")
        if same_high and trend >= 2.0 and _is_red(last):
            score = 54 + min(22, abs(trend) * 2.2) + (8 if _upper_wick(last) > _body(last) else 0)
            _add(matches, "tweezer_top", score, "bearish", [
                "اختبار نفس القمة تقريبًا مرتين بعد صعود.",
                "فشل الاختراق مع شمعة حمراء يعطي تحذير انعكاس/تصريف.",
            ], action="risk_guard_no_chase", trigger=min(_f(prev.get("low")), _f(last.get("low"))), stop=max(_f(prev.get("high")), _f(last.get("high"))), target=price - max(avg_range * 1.4, price * 0.025), confidence="medium")

    # GPT Alpha: Silent Compression Break.
    if len(bars) >= 10 and price > 0:
        comp = bars[-8:-1]
        early_ranges = [_range(b) for b in bars[-14:-8]] if len(bars) >= 14 else [_range(b) for b in bars[:-8]]
        comp_ranges = [_range(b) for b in comp]
        early_avg = _avg(early_ranges) or avg_range
        comp_avg = _avg(comp_ranges) or avg_range
        compression = comp_avg / max(early_avg, 0.01)
        comp_high = max(_f(b.get("high")) for b in comp)
        comp_low = min(_f(b.get("low")) for b in comp if _f(b.get("low")) > 0)
        vol_slope = (_avg([_f(b.get("volume")) for b in comp[-3:]]) / max(_avg([_f(b.get("volume")) for b in comp[:3]]), 1.0)) if comp else 1.0
        if 0 < compression <= 0.72 and price >= comp_high * 0.995 and vol_slope >= 1.12:
            score = 60 + min(18, (0.72 - compression) * 45) + min(14, (vol_slope - 1.0) * 20)
            _add(matches, "gpt_silent_compression_break", score, "bullish", [
                "مدى الشموع ضاق بوضوح قبل اقتراب السعر من أعلى نطاق الضغط.",
                f"ارتفاع تدريجي في الحجم داخل الضغط: ratio≈{round(vol_slope,2)}.",
                "هدف النمط: التقاط الحركة قبل الشمعة الكبيرة لا بعدها.",
            ], action="pre_trigger_candidate", trigger=comp_high, stop=comp_low, target=comp_high + max((comp_high - comp_low) * 1.2, price * 0.035), confidence="high" if score >= 78 else "medium")

    # GPT Alpha: Liquidity Sweep + Reclaim + Compression.
    if len(bars) >= 8 and price > 0:
        prev_lows = [_f(b.get("low")) for b in bars[-8:-2] if _f(b.get("low")) > 0]
        prev_floor = min(prev_lows) if prev_lows else 0.0
        # Allow either the previous bar or the current bar to be the sweep/reclaim area.
        if prev_floor and (_f(prev.get("low")) < prev_floor * 0.998 or _f(last.get("low")) < prev_floor * 0.998):
            reclaim_close = price > prev_floor
            close_near_high = (_f(last.get("high")) - price) <= max(_range(last) * 0.35, price * 0.003)
            vol_ok = (not avg_vol) or _f(last.get("volume")) >= avg_vol * 1.05
            if reclaim_close and close_near_high and vol_ok:
                sweep_depth = max(_pct(prev_floor, min(_f(prev.get("low")), _f(last.get("low")))), 0.0)
                score = 64 + min(16, sweep_depth * 8) + (10 if _lower_wick(last) > _body(last) else 0) + (8 if avg_vol and _f(last.get("volume")) > avg_vol * 1.35 else 0)
                _add(matches, "gpt_liquidity_coil_reclaim", score, "bullish", [
                    "كسر/لمس قاع قريب ثم استرداد فوقه بدل الاستمرار هبوطًا.",
                    "إغلاق قريب من أعلى الشمعة يدل أن الارتداد لم يكن وهميًا بالكامل.",
                    "هذا نمط GPT Alpha يجمع مصيدة سيولة + استرداد + بداية ضغط إيجابي.",
                ], action="reclaim_watch", trigger=max(_f(last.get("high")), prev_floor), stop=min(_f(prev.get("low")), _f(last.get("low"))), target=price + max(avg_range * 1.8, price * 0.04), confidence="high" if score >= 78 else "medium")

    # GPT Alpha: Smart Pivot Reset / سهم الارتكاز الذكي — V2W15b tightened.
    # V2W15 replay showed the idea works, but the broad version fired 55 times with
    # only ~47% proxy success.  V2W15b separates: Pivot Watch -> Trigger Ready -> Confirmed.
    # The broad setup can be annotated, but only trigger-ready/confirmed setups get high scores.
    if len(bars) >= 18 and price > 0:
        look = bars[-min(34, len(bars)):]
        older = look[:-5]
        tail = look[-6:]
        prior_low = _lowest_before_tail(look, exclude_tail=6)
        prior_high = _highest_before_tail(look, exclude_tail=5)
        tail_lows = [_f(b.get("low")) for b in tail if _f(b.get("low")) > 0]
        reset_low = min(tail_lows) if tail_lows else 0.0
        tail_high = max([_f(b.get("high")) for b in tail] or [0.0])
        first_base = _f(older[0].get("open")) if older else prior_low
        impulse_pct = _pct(prior_high, min(first_base or prior_low, prior_low) or first_base) if prior_high else 0.0
        pullback_pct = _pct(prior_high, reset_low) if prior_high and reset_low else 0.0
        higher_low_pct = _pct(reset_low, prior_low) if prior_low and reset_low else 0.0
        ema_fast = _ema_like([_f(b.get("close")) for b in look], span=8)
        reclaim_fast = bool(ema_fast and price >= ema_fast * 0.997)
        recent_ranges = [_range(b) for b in tail[:-1]]
        earlier_ranges = [_range(b) for b in look[-14:-7]] if len(look) >= 14 else [_range(b) for b in older[-6:]]
        compression_ratio = (_avg(recent_ranges) / max(_avg(earlier_ranges), 0.01)) if earlier_ranges and recent_ranges else 1.0
        avg_tail_vol = _avg([_f(b.get("volume")) for b in tail[:-1] if _f(b.get("volume")) > 0])
        vol_ratio = (_f(last.get("volume")) / max(avg_tail_vol, 1.0)) if avg_tail_vol else 1.0
        vol_reclaim = (not avg_tail_vol) or vol_ratio >= 0.95
        prior_tail = tail[:-1]
        prior_tail_high = max([_f(b.get("high")) for b in prior_tail] or [0.0])
        prior_tail_close_high = max([_f(b.get("close")) for b in prior_tail] or [0.0])
        micro_trigger = max(prior_tail_high, prior_tail_close_high, ema_fast or 0.0)
        if not micro_trigger or micro_trigger <= 0:
            micro_trigger = max(tail_high, price * 1.004)
        triggered_now = price >= micro_trigger * 0.998
        trigger_near = price >= micro_trigger * 0.985
        close_position = (price - reset_low) / max(tail_high - reset_low, 0.01) if tail_high > reset_low else 0.0
        risk_pct = _pct(micro_trigger, reset_low) if micro_trigger and reset_low else 99.0
        deep_pullback = pullback_pct > 28.0
        very_deep_pullback = pullback_pct > 34.0
        tiny_higher_low = higher_low_pct < 3.0
        clean_structure = (
            impulse_pct >= 12.0
            and 4.0 <= pullback_pct <= 32.0
            and higher_low_pct >= 3.0
            and close_position >= 0.58
            and compression_ratio <= 1.05
            and vol_reclaim
            and (reclaim_fast or triggered_now or trigger_near)
        )
        # Broad pivot watch is allowed, but kept below replay promotion unless close to trigger.
        broad_watch = (
            impulse_pct >= 9.0
            and 3.0 <= pullback_pct <= 38.0
            and higher_low_pct >= 0.7
            and close_position >= 0.48
            and compression_ratio <= 1.15
            and vol_reclaim
            and (reclaim_fast or price >= prior_tail_close_high * 0.998 if prior_tail_close_high else reclaim_fast)
        )
        if broad_watch:
            score = 52.0
            score += min(14.0, impulse_pct * 0.35)
            score += min(12.0, max(0.0, higher_low_pct) * 1.8)
            score += min(8.0, max(0.0, 1.10 - compression_ratio) * 18.0)
            if triggered_now:
                score += 9.0
            elif trigger_near:
                score += 4.0
            if risk_pct <= 6.5:
                score += 8.0
            elif risk_pct <= 8.5:
                score += 4.0
            elif risk_pct > 10.0:
                score -= min(16.0, (risk_pct - 10.0) * 1.5)
            if deep_pullback:
                score -= 5.0
            if very_deep_pullback:
                score -= 7.0
            if tiny_higher_low:
                score -= 7.0
            if avg_tail_vol and vol_ratio >= 1.25:
                score += 3.0
            # Route stage: only confirmed/trigger-ready should meaningfully influence live ranking.
            if clean_structure and triggered_now and risk_pct <= 8.0:
                action = "smart_pivot_confirmed_watch"
                stage = "pivot_confirmed"
                confidence = "high"
                score = max(score, 76.0)
                rule_note = "ارتكاز مؤكد: السعر كسر/استرد trigger الارتكاز والمخاطرة إلى القاع الأعلى معقولة."
            elif clean_structure and trigger_near and risk_pct <= 9.5:
                action = "smart_pivot_trigger_ready"
                stage = "pivot_trigger_ready"
                confidence = "medium"
                score = max(score, 70.0)
                rule_note = "ارتكاز قريب من التفعيل: مراقبة لصيقة حتى كسر trigger أو ثبات فوقه."
            else:
                action = "smart_pivot_watch"
                stage = "pivot_watch"
                confidence = "low"
                score = min(score, 66.0)
                rule_note = "ارتكاز مبكر/واسع: مراقبة فقط ولا يترقى قبل trigger ومخاطرة وقف مقبولة."
            target_level = micro_trigger + max((prior_high - reset_low) * 0.45, price * 0.045, avg_range * 1.6)
            _add(matches, "gpt_smart_pivot_reset", score, "bullish", [
                f"اندفاع سابق بنحو {round(impulse_pct,1)}% ثم Reset/Pullback بنحو {round(pullback_pct,1)}%.",
                f"القاع الأخير أعلى من القاع السابق بنحو {round(higher_low_pct,1)}%، risk≈{round(risk_pct,1)}%، trigger≈{round(micro_trigger,4)}.",
                rule_note,
            ], action=action, trigger=micro_trigger, stop=reset_low, target=target_level, confidence=confidence, extra={
                "pivot_stage": stage,
                "setup_price": _round(price, 4),
                "trigger_price": _round(micro_trigger, 4),
                "stop_price": _round(reset_low, 4),
                "risk_pct": _round(risk_pct, 2),
                "impulse_pct": _round(impulse_pct, 2),
                "pullback_pct": _round(pullback_pct, 2),
                "higher_low_pct": _round(higher_low_pct, 2),
                "compression_ratio": _round(compression_ratio, 3),
                "triggered_now": bool(triggered_now),
                "trigger_near": bool(trigger_near),
                "clean_structure": bool(clean_structure),
            })

    # GPT Alpha: Second Wave Controlled Pullback.
    if len(bars) >= 10 and price > 0:
        look = bars[-10:]
        start_price = _f(look[0].get("open")) or _f(look[0].get("close"))
        high_price = max(_f(b.get("high")) for b in look)
        impulse = _pct(high_price, start_price) if start_price else 0.0
        pullback_low = min(_f(b.get("low")) for b in look[-4:] if _f(b.get("low")) > 0)
        pullback_pct = _pct(high_price, pullback_low) if high_price else 0.0
        held_mid = pullback_low >= start_price + (high_price - start_price) * 0.38 if start_price and high_price > start_price else False
        reclaim_last = price > _avg([_f(b.get("close")) for b in look[-4:-1]])
        if 7.0 <= impulse <= 35.0 and 2.0 <= pullback_pct <= 12.0 and held_mid and reclaim_last:
            score = 61 + min(18, impulse * 0.55) + min(12, (12.0 - pullback_pct) * 1.2)
            _add(matches, "gpt_second_wave_controlled_pullback", score, "bullish", [
                f"موجة أولى واضحة بنحو {round(impulse,1)}% ثم Pullback غير عميق.",
                "التراجع حافظ على جزء كبير من الحركة ثم بدأ يسترد متوسط الإغلاقات الأخيرة.",
                "مفيد لعدم مطاردة الشمعة الأولى وانتظار موجة ثانية محسوبة.",
            ], action="continuation_watch", trigger=max(_f(b.get("high")) for b in look[-4:]), stop=pullback_low, target=price + max((high_price - pullback_low) * 0.75, price * 0.04), confidence="medium")

    matches = [_apply_match_calibration(m) for m in matches]
    matches = sorted(matches, key=lambda x: (_calibrated_score(x), _f(x.get("score"))), reverse=True)
    best = matches[0] if matches else {}
    bullish = [m for m in matches if _match_is_bullish_setup(m)]
    guards = [m for m in matches if _match_is_risk_guard(m)]
    best_bullish = sorted(bullish, key=lambda x: (_calibrated_score(x), _f(x.get("score"))), reverse=True)[0] if bullish else {}
    best_guard = sorted(guards, key=lambda x: (_calibrated_score(x), _f(x.get("score"))), reverse=True)[0] if guards else {}
    bullish_score = max([_calibrated_score(m) for m in bullish] or [0.0])
    bearish_score = max([_calibrated_score(m) for m in guards] or [0.0])
    if bearish_score >= max(70, bullish_score + 6):
        bias = "risk_guard_bearish"
        recommended_bucket = _s(best_guard.get("recommended_bucket")) or "continuation_pullback"
        decision_mode = "guard_first"
    elif bullish_score >= 76:
        bias = "bullish_watch_high_quality"
        recommended_bucket = _s(best_bullish.get("recommended_bucket")) or "pre_trigger"
        decision_mode = "bullish_setup_requires_gates"
    elif bullish_score >= 62:
        bias = "bullish_needs_confirmation"
        recommended_bucket = _s(best_bullish.get("recommended_bucket")) or "pre_trigger"
        decision_mode = "wait_confirmation"
    elif matches:
        bias = "mixed_or_weak"
        recommended_bucket = _s(best.get("recommended_bucket")) or "pre_trigger"
        decision_mode = "observe_only"
    else:
        bias = "no_pattern"
        recommended_bucket = ""
        decision_mode = "no_pattern"
    return {
        "ok": True,
        "version": GPT_PATTERN_LAB_VERSION,
        "calibration_version": GPT_PATTERN_CALIBRATION_VERSION,
        "symbol": sym,
        "timeframe": timeframe,
        "bar_count": len(bars),
        "price": _round(price, 4),
        "previous_close": _round(previous_close, 4),
        "matches": matches[:12],
        "best_pattern": best,
        "best_bullish_pattern": best_bullish,
        "best_risk_guard_pattern": best_guard,
        "score": _round(max(bullish_score, bearish_score), 2),
        "bullish_score": _round(bullish_score, 2),
        "bearish_score": _round(bearish_score, 2),
        "recommended_bucket": recommended_bucket,
        "pattern_decision_mode": decision_mode,
        "bias": bias,
        "generated_at": _now_iso(),
    }


def _synthetic_bars_from_row(row: dict) -> list[dict]:
    """Create coarse pseudo-bars from one current row when real bars are absent.

    This is used only for UI annotation, not for final simulator metrics.  The
    simulator/backtest should use real Polygon/evidence bars.
    """
    price = _f(row.get("price", row.get("current_price", row.get("last_price"))))
    prev = _f(row.get("previous_close", row.get("prev_close", row.get("regular_close"))))
    high = _f(row.get("day_high", row.get("high", row.get("regular_high"))))
    low = _f(row.get("day_low", row.get("low", row.get("regular_low"))))
    open_price = _f(row.get("open", row.get("day_open", prev)))
    volume = _f(row.get("volume", row.get("regular_volume", row.get("display_volume"))))
    if price <= 0:
        return []
    if prev <= 0:
        prev = price / (1 + (_f(row.get("change_pct", row.get("change_percent"))) / 100.0)) if _f(row.get("change_pct", row.get("change_percent"))) else price
    if high <= 0:
        high = max(price, prev, open_price)
    if low <= 0:
        low = min(price, prev, open_price)
    # Build a rough progression so detector can tag high-level states without
    # pretending to know intraday candle details.
    return [
        {"open": prev, "high": max(prev, open_price), "low": min(prev, open_price), "close": open_price or prev, "volume": volume * 0.12},
        {"open": open_price or prev, "high": max(high * 0.96, open_price or prev), "low": min(low, open_price or prev), "close": (open_price or prev) + (price - (open_price or prev)) * 0.35, "volume": volume * 0.18},
        {"open": (open_price or prev) + (price - (open_price or prev)) * 0.35, "high": high, "low": low, "close": price, "volume": volume * 0.25},
        {"open": price * 0.995, "high": high, "low": max(low, price * 0.965), "close": price, "volume": volume * 0.20},
    ]


def analyze_row_pattern(row: dict) -> dict:
    if not isinstance(row, dict):
        return {"ok": False, "score": 0.0, "matches": []}
    sym = _u(row.get("symbol"))
    raw_bars = row.get("intraday_bars") or row.get("bars") or []
    if not isinstance(raw_bars, list) or len(raw_bars) < 3:
        raw_bars = _synthetic_bars_from_row(row)
        source = "row_snapshot_pseudo_bars"
    else:
        source = "row_intraday_bars"
    result = detect_patterns_from_bars(sym, raw_bars, previous_close=_f(row.get("previous_close", row.get("prev_close"))), timeframe=_s(row.get("pattern_timeframe") or "snapshot"))
    result["bar_source"] = source
    # Add row-level source context. Live-scan rows get extra attention but not automatic execution.
    text = " ".join(_s(row.get(k)) for k in ["source_layer", "source_origin", "source_reason", "first_source_layer", "first_source_reason", "opportunity_bucket"])
    is_live = any(k in text.lower() for k in ["live", "intraday", "v2v", "hot_lane", "fmp_live", "dynamic"])
    if is_live and result.get("bullish_score", 0) >= 50:
        result["live_scan_context_bonus"] = 8
        result["bullish_score"] = _round(min(100, _f(result.get("bullish_score")) + 8), 2)
        result["score"] = _round(max(_f(result.get("score")), _f(result.get("bullish_score"))), 2)
        result.setdefault("context_reasons_ar", []).append("مصدره Live Scan/حركة حية؛ لا يُدفن خلف احتياط أمس إذا كانت درجته أعلى.")
    return result


def enrich_rows_with_gpt_pattern_lab(rows: list[dict], *, apply_bucket_hints: bool = True) -> list[dict]:
    out_rows: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out_rows.append(row)
            continue
        out = dict(row)
        try:
            lab = analyze_row_pattern(out)
        except Exception as exc:
            lab = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}", "matches": [], "score": 0.0}
        out["gpt_pattern_lab_v2w13"] = lab
        out["gpt_pattern_lab_v2w13b"] = lab

        best = lab.get("best_pattern") if isinstance(lab.get("best_pattern"), dict) else {}
        best_bullish = lab.get("best_bullish_pattern") if isinstance(lab.get("best_bullish_pattern"), dict) else {}
        best_guard = lab.get("best_risk_guard_pattern") if isinstance(lab.get("best_risk_guard_pattern"), dict) else {}
        matches = lab.get("matches") if isinstance(lab.get("matches"), list) else []
        score = max(_f(lab.get("score")), _calibrated_score(best), _calibrated_score(best_bullish), _calibrated_score(best_guard))
        bullish_score = max(_f(lab.get("bullish_score")), _calibrated_score(best_bullish))
        guard_score = max(_f(lab.get("bearish_score")), _calibrated_score(best_guard))

        out["gpt_pattern_score"] = _round(score, 2)
        out["gpt_pattern_bullish_score"] = _round(bullish_score, 2)
        out["gpt_pattern_guard_score"] = _round(guard_score, 2)
        out["gpt_pattern_best"] = best.get("pattern_id") or ""
        out["gpt_pattern_best_ar"] = best.get("pattern_name_ar") or ""
        out["gpt_pattern_recommended_bucket"] = lab.get("recommended_bucket") or ""
        out["gpt_pattern_decision_mode"] = lab.get("pattern_decision_mode") or ""
        out["gpt_pattern_calibration_version"] = GPT_PATTERN_CALIBRATION_VERSION

        if matches:
            reasons = []
            for m in matches[:3]:
                note = _s(m.get("activation_rule_ar"))
                reasons.append(f"{m.get('pattern_name_ar')}: {', '.join((m.get('reasons_ar') or [])[:2])}" + (f" — {note}" if note else ""))
            existing = out.get("opportunity_reasons") if isinstance(out.get("opportunity_reasons"), list) else []
            out["opportunity_reasons"] = list(dict.fromkeys([str(x) for x in existing + reasons if x]))[:12]
            out["technical_explainer_reasons"] = out.get("opportunity_reasons")

        # Risk guards win over bullish tags only when they are meaningfully stronger.
        if best_guard and guard_score >= max(62.0, bullish_score + 6.0):
            flags = out.get("risk_flags") if isinstance(out.get("risk_flags"), list) else []
            flags.append(f"GPT Pattern Lab V2W13b: {best_guard.get('pattern_name_ar')} — {best_guard.get('activation_rule_ar', 'حماية من المطاردة/الدخول.')}" )
            out["risk_flags"] = list(dict.fromkeys([str(x) for x in flags if x]))[:10]
            out["pattern_risk_status"] = "bearish_guard_v2w13b"
            out["pattern_risk_label"] = "⚠️ نمط سلبي/رفض — No-Chase"
            out["gpt_pattern_route_reason_ar"] = best_guard.get("activation_rule_ar") or "حماية من المطاردة."
            cur = _s(out.get("opportunity_bucket"))
            if cur in {"pre_trigger", "support_bounce", "reclaim", "low_float_premarket", "early_movement", "watch", "learning_opportunity"}:
                out["opportunity_bucket"] = "continuation_pullback"
                out["opportunity_stage"] = "continuation_pullback"
                out["opportunity_stage_label"] = "⚠️ GPT Pattern Guard — انتظار Pullback / لا مطاردة"
        elif apply_bucket_hints and best_bullish:
            cal = _calibration_for(best_bullish.get("pattern_id"))
            min_score = _f(cal.get("min_live_score"), 72.0)
            recommended = _s(best_bullish.get("recommended_bucket") or cal.get("recommended_bucket"))
            action = _s(best_bullish.get("action"))
            cur_bucket = _s(out.get("opportunity_bucket"))
            should_route = bullish_score >= min_score and cur_bucket in {"", "watch", "early_movement", "learning_opportunity", "small_stock_classic", "raw_fast_lane"}
            # V2W15b: Smart Pivot Watch is only a setup. It may label/monitor, but
            # should not route into Support Bounce/Reclaim until trigger-ready/confirmed.
            pivot_stage = _s(best_bullish.get("pivot_stage"))
            pivot_risk = _f(best_bullish.get("risk_pct"), 99.0)
            if _s(best_bullish.get("pattern_id")) == "gpt_smart_pivot_reset":
                # V2W15c: confirmed pivots may route to Support Bounce;
                # trigger-ready pivots stay Pre-Trigger until live confirmation.
                if action == "smart_pivot_confirmed_watch" and pivot_risk <= 8.0:
                    recommended = "support_bounce"
                elif action == "smart_pivot_trigger_ready" and pivot_risk <= 7.0:
                    recommended = "pre_trigger"
                else:
                    recommended = "pre_trigger"
                    should_route = bullish_score >= min_score and cur_bucket in {"", "watch", "early_movement", "learning_opportunity", "small_stock_classic", "raw_fast_lane"}
            # Allow important calibrated patterns to improve a nearby bucket even if already classified.
            should_label_existing = bullish_score >= min_score and cur_bucket in {"pre_trigger", "support_bounce", "reclaim", "continuation_pullback", "low_float_premarket"}
            if should_route:
                if recommended == "reclaim" or action in {"reclaim_watch"}:
                    out["opportunity_bucket"] = "reclaim"
                    out["opportunity_stage"] = "reclaim"
                    out["opportunity_stage_label"] = "🔁 GPT Pattern Lab Reclaim — يحتاج تأكيد"
                elif recommended == "support_bounce" or action in {"support_bounce_watch"}:
                    out["opportunity_bucket"] = "support_bounce"
                    out["opportunity_stage"] = "support_bounce"
                    out["opportunity_stage_label"] = "↩️ GPT Pattern Lab Support Bounce — يحتاج تأكيد"
                elif recommended == "continuation_pullback" or action in {"continuation_watch"}:
                    out["opportunity_bucket"] = "continuation_pullback"
                    out["opportunity_stage"] = "continuation_pullback"
                    out["opportunity_stage_label"] = "📈 GPT Second Wave / Continuation — انتظار إعادة اختبار"
                else:
                    out["opportunity_bucket"] = "pre_trigger"
                    out["opportunity_stage"] = "pre_trigger"
                    out["opportunity_stage_label"] = "⏳ GPT Pattern Lab — مرشح تفعيل مشروط"
            if should_route or should_label_existing:
                out["gpt_pattern_route_reason_ar"] = best_bullish.get("activation_rule_ar") or "نمط صاعد معاير يحتاج تأكيدًا وبوابات السلامة."
                out["gpt_pattern_requires_confirmation"] = bool(best_bullish.get("requires_confirmation", True))

        # V2W13b ranking: patterns can move a row higher in monitoring, but never
        # bypass Sharia/plan/tradability gates.  Guard patterns reduce practical rank.
        current_rank = max(_f(out.get("opportunity_rank_score")), _f(out.get("live_rank_score")), _f(out.get("display_rank_score")))
        if guard_score >= max(62.0, bullish_score + 6.0):
            out["opportunity_rank_score"] = _round(max(0.0, current_rank - guard_score * 8.0), 2)
        elif bullish_score >= 60.0:
            # calibrated_score * 10 gives a useful boost without overwhelming live scan and liquidity scoring.
            out["opportunity_rank_score"] = _round(max(current_rank, bullish_score * 10.0), 2)
        out_rows.append(out)
    return out_rows

def pattern_lab_status() -> dict:
    return {
        "ok": True,
        "version": GPT_PATTERN_LAB_VERSION,
        "calibration_version": GPT_PATTERN_CALIBRATION_VERSION,
        "analyst_lesson_patterns": sorted(ANALYST_PATTERN_IDS),
        "gpt_alpha_patterns": sorted(GPT_ALPHA_PATTERN_IDS),
        "top_calibrated_lessons_ar": [
            "Tweezer Bottom أصبح Support Bounce/Reclaim قويًا لكن بشرط الدعم والتأكيد.",
            "Elephant Trunk Drop وTweezer Top أصبحا Risk Guards لا إشارات شراء.",
            "GPT Second Wave هو أفضل نمط GPT Alpha مبدئيًا للمراقبة العملية.",
            "Smart Pivot Reset أصبح ثلاث مراحل: Pivot Watch ثم Trigger Ready ثم Confirmed؛ لا يترقى من مجرد قاع أعلى.",
            "Strong BOS Bullish يحتاج hold/retest أو حجم استمرار ولا يدخل وحده.",
        ],
        "execution_rule_ar": "مختبر الأنماط يوسم ويرتب ويراقب فقط؛ لا يصنع BUY_NOW ولا يتجاوز الشرعية أو السيولة أو الخطة.",
        "weekend_use_ar": "مناسب للويكند: تحليل ومحاكاة بدون live polling ثقيل.",
    }


def pattern_lab_calibration_payload() -> dict:
    items = []
    for pid, cal in _PATTERN_CALIBRATION.items():
        item = dict(cal)
        item["pattern_id"] = pid
        item["pattern_name_ar"] = _PATTERN_AR.get(pid, pid)
        item["family"] = "gpt_alpha" if pid in GPT_ALPHA_PATTERN_IDS else "analyst_lesson"
        items.append(item)
    return {
        "ok": True,
        "version": GPT_PATTERN_LAB_VERSION,
        "calibration_version": GPT_PATTERN_CALIBRATION_VERSION,
        "items": sorted(items, key=lambda x: (_f(x.get("replay_win_rate_proxy")), _f(x.get("replay_avg_gain_proxy"))), reverse=True),
        "rule_ar": "معايرة V2W15d: تضيف تقرير جودة مراحل الارتكاز؛ Confirmed فقط يذهب Support Bounce، وTrigger Ready يبقى Pre-Trigger.",
    }


def analyze_bars_payload(payload: dict) -> dict:
    payload = payload or {}
    return detect_patterns_from_bars(
        _u(payload.get("symbol") or "TEST"),
        payload.get("bars") if isinstance(payload.get("bars"), list) else [],
        previous_close=_f(payload.get("previous_close")),
        timeframe=_s(payload.get("timeframe") or "custom"),
    )


def summarize_current_rows(rows: list[dict], limit: int = 80) -> dict:
    enriched = enrich_rows_with_gpt_pattern_lab(list(rows or [])[: max(1, int(limit or 80))], apply_bucket_hints=False)
    items = []
    agg: dict[str, dict] = {}
    for row in enriched:
        if not isinstance(row, dict):
            continue
        lab = row.get("gpt_pattern_lab_v2w13") if isinstance(row.get("gpt_pattern_lab_v2w13"), dict) else {}
        best = lab.get("best_pattern") if isinstance(lab.get("best_pattern"), dict) else {}
        if not best:
            continue
        pid = _s(best.get("pattern_id"))
        if pid:
            a = agg.setdefault(pid, {"pattern_id": pid, "pattern_name_ar": _PATTERN_AR.get(pid, pid), "count": 0, "max_score": 0, "symbols": []})
            a["count"] += 1
            a["max_score"] = max(_f(a.get("max_score")), _f(best.get("score")))
            if len(a["symbols"]) < 12:
                a["symbols"].append(_u(row.get("symbol")))
        items.append({
            "symbol": _u(row.get("symbol")),
            "price": _round(row.get("price", row.get("current_price", row.get("last_price"))), 4),
            "best_pattern": best,
            "score": lab.get("score", 0),
            "bias": lab.get("bias"),
            "bar_source": lab.get("bar_source"),
            "opportunity_bucket": row.get("opportunity_bucket"),
        })
    return {
        "ok": True,
        "version": GPT_PATTERN_LAB_VERSION,
        "rows_checked": len(rows or []),
        "items_count": len(items),
        "items": sorted(items, key=lambda x: _f(x.get("score")), reverse=True)[: max(1, int(limit or 80))],
        "pattern_counts": sorted(agg.values(), key=lambda x: (int(x.get("count") or 0), _f(x.get("max_score"))), reverse=True),
        "rule_ar": "هذا تشخيص للصفوف الحالية. المحاكاة الرسمية تستخدم شموع Polygon/SQLite عند توفرها.",
    }


def _connect_db() -> sqlite3.Connection | None:
    try:
        if not SQLITE_DB_PATH:
            return None
        path = Path(SQLITE_DB_PATH)
        if not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def run_pattern_replay_from_evidence(trade_date: str = "", limit_symbols: int = 80, horizon_bars: int = 12) -> dict:
    """Small no-lookahead replay using stored evidence_intraday_bars.

    At each bar index, only bars up to that point are analyzed.  Outcome is measured
    over the next horizon_bars bars.  This is intentionally lightweight and safe for
    weekend diagnostics.
    """
    conn = _connect_db()
    if conn is None:
        return {"ok": False, "version": GPT_PATTERN_LAB_VERSION, "error": "sqlite_db_not_found", "db_path": SQLITE_DB_PATH}
    try:
        dates = []
        if trade_date:
            dates = [trade_date]
        else:
            row = conn.execute("SELECT trade_date FROM evidence_intraday_bars GROUP BY trade_date ORDER BY trade_date DESC LIMIT 1").fetchone()
            if row:
                dates = [row[0]]
        if not dates:
            return {"ok": False, "version": GPT_PATTERN_LAB_VERSION, "error": "no_evidence_intraday_bars"}
        d = dates[0]
        syms = [r[0] for r in conn.execute("SELECT symbol FROM evidence_intraday_bars WHERE trade_date=? GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT ?", (d, max(1, int(limit_symbols or 80)))).fetchall()]
        signals = []
        for sym in syms:
            rows = conn.execute("SELECT bar_ts, bar_time_text, open, high, low, close, volume, dollar_volume FROM evidence_intraday_bars WHERE trade_date=? AND symbol=? ORDER BY bar_ts ASC", (d, sym)).fetchall()
            bars = [dict(r) for r in rows]
            if len(bars) < 16:
                continue
            seen_keys = set()
            # Start after enough context and avoid repeated signal spam by pattern.
            for idx in range(10, max(10, len(bars) - max(2, int(horizon_bars or 12)))):
                hist = bars[: idx + 1]
                res = detect_patterns_from_bars(sym, hist, timeframe="5m_replay")
                best = res.get("best_pattern") if isinstance(res.get("best_pattern"), dict) else {}
                pid = _s(best.get("pattern_id"))
                if not pid or _f(best.get("score")) < 68:
                    continue
                key = (sym, pid)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                setup_entry = _f(hist[-1].get("close"))
                trigger_px = _f(best.get("trigger_price") or best.get("trigger") or setup_entry)
                stop_px = _f(best.get("stop_price") or best.get("stop"))
                action = _s(best.get("action"))
                pivot_stage = _s(best.get("pivot_stage"))
                effective_entry = setup_entry
                # For Smart Pivot, replay should judge the trigger, not the early setup candle.
                # If trigger is not reached during the forward horizon, mark untriggered.
                future = bars[idx + 1: idx + 1 + max(2, int(horizon_bars or 12))]
                triggered_in_horizon = True
                trigger_bar_offset = 0
                if pid == "gpt_smart_pivot_reset" and trigger_px > 0 and action not in {"smart_pivot_confirmed_watch"}:
                    triggered_in_horizon = False
                    for off, fb in enumerate(future, start=1):
                        if _f(fb.get("high")) >= trigger_px:
                            triggered_in_horizon = True
                            trigger_bar_offset = off
                            effective_entry = trigger_px
                            future = future[off - 1:]
                            break
                elif pid == "gpt_smart_pivot_reset":
                    effective_entry = trigger_px if trigger_px > 0 and setup_entry >= trigger_px * 0.998 else setup_entry
                fut_high = max([_f(b.get("high")) for b in future] or [effective_entry])
                fut_low = min([_f(b.get("low")) for b in future if _f(b.get("low")) > 0] or [effective_entry])
                max_gain = _pct(fut_high, effective_entry) if effective_entry else 0.0
                max_drawdown = _pct(fut_low, effective_entry) if effective_entry else 0.0
                bearish = pid in _BEARISH_PATTERN_IDS
                if pid == "gpt_smart_pivot_reset":
                    risk_pct = _f(best.get("risk_pct")) or (_pct(trigger_px, stop_px) if trigger_px and stop_px else 99.0)
                    success = bool(triggered_in_horizon and max_gain >= 3.0 and max_drawdown > -7.0 and risk_pct <= 9.5)
                else:
                    success = (max_drawdown <= -2.0) if bearish else (max_gain >= 3.0 and max_drawdown > -8.0)
                signals.append({
                    "symbol": sym,
                    "trade_date": d,
                    "bar_index": idx,
                    "bar_time_text": _s(hist[-1].get("bar_time_text") or hist[-1].get("ts")),
                    "entry_price": _round(effective_entry, 4),
                    "setup_price": _round(setup_entry, 4),
                    "trigger_price": _round(trigger_px, 4),
                    "stop_price": _round(stop_px, 4),
                    "risk_pct": _round(best.get("risk_pct"), 2),
                    "triggered_in_horizon": bool(triggered_in_horizon),
                    "trigger_bar_offset": int(trigger_bar_offset),
                    "pivot_stage": best.get("pivot_stage"),
                    "pattern_id": pid,
                    "pattern_name_ar": _PATTERN_AR.get(pid, pid),
                    "family": best.get("family"),
                    "score": _round(best.get("score"), 2),
                    "direction": best.get("direction"),
                    "action": best.get("action"),
                    "lab_role": best.get("lab_role"),
                    "calibrated_score": _round(best.get("calibrated_score", best.get("score")), 2),
                    "recommended_bucket": best.get("recommended_bucket"),
                    "requires_confirmation": bool(best.get("requires_confirmation", True)),
                    "activation_rule_ar": best.get("activation_rule_ar"),
                    "max_gain_pct_next_horizon": _round(max_gain, 2),
                    "max_drawdown_pct_next_horizon": _round(max_drawdown, 2),
                    "success_proxy": bool(success),
                    "reasons_ar": best.get("reasons_ar", []),
                })
        agg: dict[str, dict] = {}
        for s in signals:
            pid = s["pattern_id"]
            a = agg.setdefault(pid, {"pattern_id": pid, "pattern_name_ar": _PATTERN_AR.get(pid, pid), "signals": 0, "wins": 0, "avg_gain": 0.0, "avg_drawdown": 0.0, "symbols": []})
            a["signals"] += 1
            a["wins"] += 1 if s.get("success_proxy") else 0
            a["avg_gain"] += _f(s.get("max_gain_pct_next_horizon"))
            a["avg_drawdown"] += _f(s.get("max_drawdown_pct_next_horizon"))
            if len(a["symbols"]) < 12:
                a["symbols"].append(s.get("symbol"))
        role_summary: dict[str, dict] = {}
        for a in agg.values():
            n = max(1, int(a.get("signals") or 0))
            pid = _s(a.get("pattern_id"))
            cal = _calibration_for(pid)
            role = _s(cal.get("role")) or "unranked_watch"
            avg_gain = _f(a.get("avg_gain", 0.0)) / n
            avg_dd = _f(a.get("avg_drawdown", 0.0)) / n
            win_rate = _f(a.get("wins", 0)) / n * 100.0
            a["win_rate_proxy"] = _round(win_rate, 2)
            a["avg_gain"] = _round(avg_gain, 2)
            a["avg_drawdown"] = _round(avg_dd, 2)
            a["lab_role"] = role
            a["recommended_bucket"] = cal.get("recommended_bucket")
            a["promotion_hint"] = cal.get("promotion_hint")
            a["requires_confirmation"] = bool(cal.get("requires_confirmation", True))
            a["activation_rule_ar"] = cal.get("activation_rule_ar")
            a["leaderboard_note_ar"] = cal.get("leaderboard_note_ar")
            if role == "risk_guard":
                leaderboard_score = win_rate * 0.55 + max(0.0, abs(avg_dd)) * 3.0 + min(12.0, n * 0.18)
                a["success_meaning_ar"] = "نجاح النمط هنا يعني حماية/No-Chase أو خفض فرصة الشراء، وليس إشارة شراء."
            else:
                leaderboard_score = win_rate * 0.45 + max(0.0, avg_gain) * 2.5 - max(0.0, abs(avg_dd)) * 0.9 + min(12.0, n * 0.12)
                a["success_meaning_ar"] = "نجاح النمط هنا يعني أن الإشارة أعطت صعودًا لاحقًا ضمن الأفق المحدد مع Drawdown مقبول."
            a["leaderboard_score"] = _round(leaderboard_score, 2)
            rs = role_summary.setdefault(role, {"lab_role": role, "signals": 0, "wins": 0, "avg_gain": 0.0, "avg_drawdown": 0.0, "patterns": []})
            rs["signals"] += n
            rs["wins"] += int(a.get("wins") or 0)
            rs["avg_gain"] += avg_gain * n
            rs["avg_drawdown"] += avg_dd * n
            if len(rs["patterns"]) < 8:
                rs["patterns"].append(pid)
        for rs in role_summary.values():
            n = max(1, int(rs.get("signals") or 0))
            rs["win_rate_proxy"] = _round(_f(rs.get("wins")) / n * 100.0, 2)
            rs["avg_gain"] = _round(_f(rs.get("avg_gain")) / n, 2)
            rs["avg_drawdown"] = _round(_f(rs.get("avg_drawdown")) / n, 2)

        # V2W15d: expose Smart Pivot quality by stage so the user can verify that
        # routing is behaving as intended without reading the long signal payload.
        pivot_stage_summary: dict[str, dict] = {}
        for s in signals:
            if s.get("pattern_id") != "gpt_smart_pivot_reset":
                continue
            stage = _s(s.get("pivot_stage")) or "pivot_watch"
            ps = pivot_stage_summary.setdefault(stage, {
                "pivot_stage": stage,
                "signals": 0,
                "wins": 0,
                "avg_gain": 0.0,
                "avg_drawdown": 0.0,
                "avg_risk_pct": 0.0,
                "avg_trigger_bar_offset": 0.0,
                "recommended_buckets": {},
                "actions": {},
                "symbols": [],
            })
            ps["signals"] += 1
            ps["wins"] += 1 if s.get("success_proxy") else 0
            ps["avg_gain"] += _f(s.get("max_gain_pct_next_horizon"))
            ps["avg_drawdown"] += _f(s.get("max_drawdown_pct_next_horizon"))
            ps["avg_risk_pct"] += _f(s.get("risk_pct"))
            ps["avg_trigger_bar_offset"] += _f(s.get("trigger_bar_offset"))
            bucket = _s(s.get("recommended_bucket")) or "unknown"
            action = _s(s.get("action")) or "unknown"
            ps["recommended_buckets"][bucket] = int(ps["recommended_buckets"].get(bucket, 0)) + 1
            ps["actions"][action] = int(ps["actions"].get(action, 0)) + 1
            if len(ps["symbols"]) < 12:
                ps["symbols"].append(s.get("symbol"))
        for ps in pivot_stage_summary.values():
            n = max(1, int(ps.get("signals") or 0))
            ps["win_rate_proxy"] = _round(_f(ps.get("wins")) / n * 100.0, 2)
            ps["avg_gain"] = _round(_f(ps.get("avg_gain")) / n, 2)
            ps["avg_drawdown"] = _round(_f(ps.get("avg_drawdown")) / n, 2)
            ps["avg_risk_pct"] = _round(_f(ps.get("avg_risk_pct")) / n, 2)
            ps["avg_trigger_bar_offset"] = _round(_f(ps.get("avg_trigger_bar_offset")) / n, 2)
            if ps.get("pivot_stage") == "pivot_confirmed":
                ps["routing_rule_ar"] = "ارتكاز مؤكد: يسمح له بدخول Support Bounce/Reclaim إذا بقيت المخاطرة مقبولة."
            elif ps.get("pivot_stage") == "pivot_trigger_ready":
                ps["routing_rule_ar"] = "قريب من التفعيل: يبقى Pre-Trigger ومراقبة لصيقة حتى يؤكد حيًا."
            else:
                ps["routing_rule_ar"] = "ارتكاز مراقبة فقط: لا يترقى قبل trigger واضح."
        pivot_stage_summary_sorted = sorted(
            pivot_stage_summary.values(),
            key=lambda x: (_f(x.get("win_rate_proxy")), _f(x.get("avg_gain")), -_f(x.get("avg_drawdown"))),
            reverse=True,
        )

        summary_sorted = sorted(agg.values(), key=lambda x: (_f(x.get("leaderboard_score")), _f(x.get("win_rate_proxy")), int(x.get("signals") or 0)), reverse=True)
        return {
            "ok": True,
            "version": GPT_PATTERN_LAB_VERSION,
            "calibration_version": GPT_PATTERN_CALIBRATION_VERSION,
            "trade_date": d,
            "symbols_checked": len(syms),
            "signals_count": len(signals),
            "horizon_bars": int(horizon_bars or 12),
            "summary_by_pattern": summary_sorted,
            "summary_by_role": sorted(role_summary.values(), key=lambda x: int(x.get("signals") or 0), reverse=True),
            "summary_by_pivot_stage": pivot_stage_summary_sorted,
            "leaderboard_top": summary_sorted[:8],
            "signals": sorted(signals, key=lambda x: (_f(x.get("calibrated_score", x.get("score"))), _f(x.get("max_gain_pct_next_horizon"))), reverse=True)[:300],
            "rule_ar": "محاكاة no-lookahead خفيفة: كل إشارة تُحسب من الشموع السابقة فقط. V2W15d يضيف تقرير جودة مراحل Smart Pivot ويفصل Confirmed عن Trigger Ready.",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass



def run_pattern_leaderboard_from_evidence(trade_date: str = "", limit_symbols: int = 80, horizon_bars: int = 12) -> dict:
    """Compact replay leaderboard without the long per-signal payload."""
    payload = run_pattern_replay_from_evidence(trade_date=trade_date, limit_symbols=limit_symbols, horizon_bars=horizon_bars)
    if not isinstance(payload, dict):
        return {"ok": False, "version": GPT_PATTERN_LAB_VERSION, "error": "invalid_replay_payload"}
    return {
        "ok": bool(payload.get("ok")),
        "version": payload.get("version", GPT_PATTERN_LAB_VERSION),
        "calibration_version": payload.get("calibration_version", GPT_PATTERN_CALIBRATION_VERSION),
        "trade_date": payload.get("trade_date"),
        "symbols_checked": payload.get("symbols_checked"),
        "signals_count": payload.get("signals_count"),
        "horizon_bars": payload.get("horizon_bars"),
        "leaderboard_top": payload.get("leaderboard_top", []),
        "summary_by_pattern": payload.get("summary_by_pattern", []),
        "summary_by_role": payload.get("summary_by_role", []),
        "summary_by_pivot_stage": payload.get("summary_by_pivot_stage", []),
        "rule_ar": "نسخة مختصرة للويكند والتشخيص بدون payload طويل؛ تتضمن تقرير مراحل Smart Pivot.",
        "error": payload.get("error"),
    }
