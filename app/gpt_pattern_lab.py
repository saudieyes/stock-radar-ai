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

GPT_PATTERN_LAB_VERSION = "gpt_pattern_lab_v2w13_candles_bos_tasuki_tweezer_gptalpha_2026_06_27"

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
}

_BEARISH_PATTERN_IDS = {"elephant_trunk_drop", "strong_bos_bearish", "weak_bos_bearish", "tasuki_gap_bearish", "tweezer_top"}


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


def _add(matches: list[dict], pattern_id: str, score: float, direction: str, reasons: list[str], *, action: str = "monitor", trigger: float = 0.0, stop: float = 0.0, target: float = 0.0, confidence: str = "medium") -> None:
    matches.append({
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
    })


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

    matches = sorted(matches, key=lambda x: float(x.get("score") or 0), reverse=True)
    best = matches[0] if matches else {}
    bullish = [m for m in matches if m.get("direction") == "bullish"]
    bearish = [m for m in matches if m.get("direction") == "bearish"]
    bullish_score = max([_f(m.get("score")) for m in bullish] or [0.0])
    bearish_score = max([_f(m.get("score")) for m in bearish] or [0.0])
    if bearish_score >= max(65, bullish_score + 8):
        bias = "risk_guard_bearish"
    elif bullish_score >= 70:
        bias = "bullish_watch"
    elif bullish_score >= 52:
        bias = "bullish_needs_confirmation"
    elif matches:
        bias = "mixed_or_weak"
    else:
        bias = "no_pattern"
    return {
        "ok": True,
        "version": GPT_PATTERN_LAB_VERSION,
        "symbol": sym,
        "timeframe": timeframe,
        "bar_count": len(bars),
        "price": _round(price, 4),
        "previous_close": _round(previous_close, 4),
        "matches": matches[:12],
        "best_pattern": best,
        "score": _round(max(bullish_score, bearish_score), 2),
        "bullish_score": _round(bullish_score, 2),
        "bearish_score": _round(bearish_score, 2),
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
        best = lab.get("best_pattern") if isinstance(lab.get("best_pattern"), dict) else {}
        matches = lab.get("matches") if isinstance(lab.get("matches"), list) else []
        score = _f(lab.get("score"))
        out["gpt_pattern_score"] = _round(score, 2)
        out["gpt_pattern_best"] = best.get("pattern_id") or ""
        out["gpt_pattern_best_ar"] = best.get("pattern_name_ar") or ""
        if matches:
            reasons = []
            for m in matches[:3]:
                reasons.append(f"{m.get('pattern_name_ar')}: {', '.join((m.get('reasons_ar') or [])[:2])}")
            existing = out.get("opportunity_reasons") if isinstance(out.get("opportunity_reasons"), list) else []
            out["opportunity_reasons"] = list(dict.fromkeys([str(x) for x in existing + reasons if x]))[:12]
            out["technical_explainer_reasons"] = out.get("opportunity_reasons")
        # Bearish/risk patterns are guards, not short signals.
        if best.get("pattern_id") in _BEARISH_PATTERN_IDS and _f(best.get("score")) >= 62:
            flags = out.get("risk_flags") if isinstance(out.get("risk_flags"), list) else []
            flags.append(f"GPT Pattern Lab: {best.get('pattern_name_ar')} — حماية من المطاردة/الدخول.")
            out["risk_flags"] = list(dict.fromkeys([str(x) for x in flags if x]))[:10]
            out["pattern_risk_status"] = "bearish_guard"
            out["pattern_risk_label"] = "⚠️ نمط سلبي/رفض — لا مطاردة"
            if _s(out.get("opportunity_bucket")) in {"pre_trigger", "support_bounce", "reclaim", "low_float_premarket"}:
                out["opportunity_bucket"] = "continuation_pullback"
                out["opportunity_stage"] = "continuation_pullback"
                out["opportunity_stage_label"] = "⚠️ تحول إلى حماية/انتظار Pullback بعد نمط سلبي"
        elif apply_bucket_hints and _f(lab.get("bullish_score")) >= 72:
            # Feed strong pattern candidates into monitoring/pre-trigger if they had no specific stage.
            cur_bucket = _s(out.get("opportunity_bucket"))
            action = _s(best.get("action"))
            if cur_bucket in {"", "watch", "early_movement", "learning_opportunity"}:
                if action in {"reclaim_watch"}:
                    out["opportunity_bucket"] = "reclaim"
                    out["opportunity_stage"] = "reclaim"
                    out["opportunity_stage_label"] = "🔁 GPT Pattern Lab Reclaim — مراقبة لا شراء مباشر"
                elif action in {"support_bounce_watch"}:
                    out["opportunity_bucket"] = "support_bounce"
                    out["opportunity_stage"] = "support_bounce"
                    out["opportunity_stage_label"] = "↩️ GPT Pattern Lab Support Bounce — مراقبة"
                elif action in {"continuation_watch"}:
                    out["opportunity_bucket"] = "continuation_pullback"
                    out["opportunity_stage"] = "continuation_pullback"
                    out["opportunity_stage_label"] = "📈 GPT Pattern Lab Continuation — انتظار إعادة اختبار"
                else:
                    out["opportunity_bucket"] = "pre_trigger"
                    out["opportunity_stage"] = "pre_trigger"
                    out["opportunity_stage_label"] = "⏳ GPT Pattern Lab — قريب من نمط تفعيل"
        out["opportunity_rank_score"] = _round(max(_f(out.get("opportunity_rank_score")), _f(out.get("live_rank_score")), score * 12.0), 2) if score >= 55 else out.get("opportunity_rank_score", 0)
        out_rows.append(out)
    return out_rows


def pattern_lab_status() -> dict:
    return {
        "ok": True,
        "version": GPT_PATTERN_LAB_VERSION,
        "analyst_lesson_patterns": sorted(ANALYST_PATTERN_IDS),
        "gpt_alpha_patterns": sorted(GPT_ALPHA_PATTERN_IDS),
        "execution_rule_ar": "مختبر الأنماط يوسم ويرتب ويراقب فقط؛ لا يصنع BUY_NOW ولا يتجاوز الشرعية أو السيولة أو الخطة.",
        "weekend_use_ar": "مناسب للويكند: تحليل ومحاكاة بدون live polling ثقيل.",
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
                entry = _f(hist[-1].get("close"))
                future = bars[idx + 1: idx + 1 + max(2, int(horizon_bars or 12))]
                fut_high = max([_f(b.get("high")) for b in future] or [entry])
                fut_low = min([_f(b.get("low")) for b in future if _f(b.get("low")) > 0] or [entry])
                max_gain = _pct(fut_high, entry) if entry else 0.0
                max_drawdown = _pct(fut_low, entry) if entry else 0.0
                bearish = pid in _BEARISH_PATTERN_IDS
                success = (max_drawdown <= -2.0) if bearish else (max_gain >= 3.0 and max_drawdown > -8.0)
                signals.append({
                    "symbol": sym,
                    "trade_date": d,
                    "bar_index": idx,
                    "bar_time_text": _s(hist[-1].get("bar_time_text") or hist[-1].get("ts")),
                    "entry_price": _round(entry, 4),
                    "pattern_id": pid,
                    "pattern_name_ar": _PATTERN_AR.get(pid, pid),
                    "family": best.get("family"),
                    "score": _round(best.get("score"), 2),
                    "direction": best.get("direction"),
                    "action": best.get("action"),
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
        for a in agg.values():
            n = max(1, int(a.get("signals") or 0))
            a["win_rate_proxy"] = _round(a.get("wins", 0) / n * 100.0, 2)
            a["avg_gain"] = _round(a.get("avg_gain", 0.0) / n, 2)
            a["avg_drawdown"] = _round(a.get("avg_drawdown", 0.0) / n, 2)
        return {
            "ok": True,
            "version": GPT_PATTERN_LAB_VERSION,
            "trade_date": d,
            "symbols_checked": len(syms),
            "signals_count": len(signals),
            "horizon_bars": int(horizon_bars or 12),
            "summary_by_pattern": sorted(agg.values(), key=lambda x: (int(x.get("signals") or 0), _f(x.get("win_rate_proxy"))), reverse=True),
            "signals": sorted(signals, key=lambda x: (_f(x.get("score")), _f(x.get("max_gain_pct_next_horizon"))), reverse=True)[:300],
            "rule_ar": "محاكاة no-lookahead خفيفة: كل إشارة تُحسب من الشموع السابقة فقط، ثم تقيس الحركة اللاحقة.",
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass
