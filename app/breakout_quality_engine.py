"""Breakout Quality / Readiness Engine V1.

Backend-only layer that separates:
- قريب من اختراق مهم (Readiness / Pre-Trigger)
- اختراق مؤكد قابل للترقية (Trigger)
- اختراق ضعيف أو فاشل (Demotion / blockers)

It is intentionally conservative for BUY_NOW, but not passive: it annotates
near-breakout rows early so the tool can monitor them before the move is gone.
"""
from __future__ import annotations

import os
import time
from typing import Any

from app.market_data import get_daily_bars, calculate_atr

BREAKOUT_QUALITY_VERSION = "breakout_quality_v1_readiness_trigger_2026_06_14"
_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SEC = int(os.getenv("BREAKOUT_QUALITY_CACHE_TTL_SEC", "900") or 900)
MAX_ROWS = int(os.getenv("BREAKOUT_QUALITY_MAX_ROWS", "160") or 160)


def _s(v: Any) -> str:
    return str(v or "").strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace("$", "").replace(",", "").replace("%", "").strip()
        return float(v)
    except Exception:
        return default


def _price(row: dict) -> float:
    return _num(row.get("current_price_live") or row.get("display_price") or row.get("price") or row.get("current_price") or row.get("last_close") or row.get("close"), 0.0)


def _entry(row: dict) -> float:
    return _num(row.get("display_entry_price") or row.get("smart_entry_price") or row.get("entry_price_real") or row.get("entry") or row.get("entry_price") or row.get("suggested_watch_zone_high") or row.get("last_close"), 0.0)


def _target1(row: dict) -> float:
    return _num(row.get("display_target_price") or row.get("smart_target_1") or row.get("target_price") or row.get("target_1") or row.get("target1") or row.get("first_target"), 0.0)


def _stop(row: dict) -> float:
    return _num(row.get("display_stop_price") or row.get("smart_stop_loss") or row.get("stop_loss") or row.get("stop") or row.get("invalidation"), 0.0)


def _is_breakout_text(row: dict) -> bool:
    text = " ".join(_s(row.get(k)) for k in [
        "type", "trade_type", "plan_type", "setup_type", "pattern", "quality_bucket", "breakout_status", "owner_action", "execution_status", "final_decision_label",
    ])
    if "اختراق" in text or "breakout" in text.lower():
        return True
    for k in ["breakout_price", "required_breakout_price", "breakout_required", "confirmation_price", "resistance_price", "nearest_resistance"]:
        if _num(row.get(k), 0.0) > 0:
            return True
    return False


def _breakout_level(row: dict) -> float:
    vals: list[float] = []
    for k in [
        "required_breakout_price", "breakout_required", "breakout_price", "confirmation_price", "resistance_price", "nearest_resistance",
        "display_resistance_price", "buy_above", "display_entry_price", "smart_entry_price", "entry_price_real", "entry", "entry_price", "suggested_watch_zone_high",
    ]:
        n = _num(row.get(k), 0.0)
        if n > 0:
            vals.append(n)
    if not vals:
        return 0.0
    price = _price(row) or _entry(row) or min(vals)
    sane = [x for x in vals if price <= 0 or x <= price * 1.15]
    return max(sane or vals)


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1.0)
    e = values[0]
    for v in values[1:]:
        e = (v * k) + (e * (1.0 - k))
    return e


def _rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    gains = gains[-period:]
    losses = losses[-period:]
    avg_gain = sum(gains) / max(1, len(gains))
    avg_loss = sum(losses) / max(1, len(losses))
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(values: list[float]) -> tuple[float, float, float]:
    if len(values) < 35:
        return 0.0, 0.0, 0.0
    # Build approximate MACD line series for signal.
    macd_series = []
    for i in range(26, len(values) + 1):
        subset = values[:i]
        macd_series.append(_ema(subset[-60:], 12) - _ema(subset[-80:], 26))
    macd_val = macd_series[-1]
    sig = _ema(macd_series[-35:], 9)
    hist = macd_val - sig
    return macd_val, sig, hist


def _daily_indicators(symbol: str) -> dict:
    sym = _u(symbol)
    now = time.time()
    cached = _CACHE.get(sym)
    if cached and now - cached[0] < CACHE_TTL_SEC:
        return cached[1]
    bars = get_daily_bars(sym)
    closes = [_num(b.get("c"), 0.0) for b in bars if _num(b.get("c"), 0.0) > 0][-260:]
    highs = [_num(b.get("h"), 0.0) for b in bars if _num(b.get("h"), 0.0) > 0][-80:]
    lows = [_num(b.get("l"), 0.0) for b in bars if _num(b.get("l"), 0.0) > 0][-80:]
    vols = [_num(b.get("v"), 0.0) for b in bars if _num(b.get("v"), 0.0) > 0][-30:]
    last = closes[-1] if closes else 0.0
    ema9 = _ema(closes[-80:], 9)
    ema21 = _ema(closes[-120:], 21)
    ema50 = _ema(closes[-160:], 50)
    ema200 = _ema(closes[-260:], 200) if len(closes) >= 80 else 0.0
    rsi14 = _rsi(closes[-80:], 14)
    macd_val, macd_sig, macd_hist = _macd(closes[-120:])
    atr14 = calculate_atr(bars, 14)
    avg_vol20 = sum(vols[-20:]) / 20.0 if len(vols) >= 20 else (sum(vols) / len(vols) if vols else 0.0)
    high20 = max(highs[-20:]) if highs else 0.0
    low20 = min(lows[-20:]) if lows else 0.0
    # Tightness: last 5 closes range relative to price.
    tight5_pct = 0.0
    if len(closes) >= 5 and last > 0:
        tight5_pct = ((max(closes[-5:]) - min(closes[-5:])) / last) * 100.0
    out = {
        "symbol": sym,
        "last_close": round(last, 4),
        "ema9": round(ema9, 4),
        "ema21": round(ema21, 4),
        "ema50": round(ema50, 4),
        "ema200": round(ema200, 4),
        "rsi14": round(rsi14, 2),
        "macd": round(macd_val, 5),
        "macd_signal": round(macd_sig, 5),
        "macd_hist": round(macd_hist, 5),
        "atr14": round(float(atr14 or 0), 4),
        "atr_pct": round((float(atr14 or 0) / last * 100.0) if last > 0 else 0.0, 2),
        "avg_volume_20": round(avg_vol20, 2),
        "high_20": round(high20, 4),
        "low_20": round(low20, 4),
        "tight5_pct": round(tight5_pct, 2),
        "bars_count": len(closes),
    }
    _CACHE[sym] = (now, out)
    return out


def _volume_ratio(row: dict, ind: dict) -> float:
    for k in ["effective_volume_ratio", "volume_ratio", "volume_pace_ratio", "volume_ratio_last_vs_prev20"]:
        n = _num(row.get(k), 0.0)
        if n > 0:
            return n
    v = _num(row.get("volume") or row.get("last_volume"), 0.0)
    avg = _num(ind.get("avg_volume_20"), 0.0)
    return (v / avg) if v > 0 and avg > 0 else 0.0


def _rr_ok(row: dict, price: float, trigger: float) -> bool:
    stop = _stop(row)
    target = _target1(row)
    if stop <= 0 or target <= 0:
        return True  # Do not block when plan data is missing; other layers handle incomplete plan.
    entry = max(price, trigger)
    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return False
    return (reward / risk) >= _num(os.getenv("BREAKOUT_MIN_RR", "1.20"), 1.20)


def _relative_strength_hint(row: dict) -> bool:
    # Use available fields if present; otherwise do not block.
    for k in ["relative_strength_score", "rs_score", "market_relative_strength_score"]:
        if k in row:
            return _num(row.get(k), 50.0) >= 50.0
    # If last day outperformed market values carried in row, use it.
    r = _num(row.get("last_day_return_pct"), 0.0)
    m = _num(row.get("market_last_day_return_pct") or row.get("spy_return_pct") or row.get("market_return_pct"), 0.0)
    if r or m:
        return r >= m - 0.25
    return True


def evaluate_breakout_row(row: dict) -> dict:
    if not isinstance(row, dict):
        return row
    row["breakout_quality_version"] = BREAKOUT_QUALITY_VERSION
    if not _is_breakout_text(row):
        return row
    sym = _u(row.get("symbol"))
    if not sym:
        return row
    price = _price(row)
    trigger = _breakout_level(row)
    if price <= 0 or trigger <= 0:
        return row
    ind = _daily_indicators(sym)
    vol_ratio = _volume_ratio(row, ind)
    buffer_pct = _num(os.getenv("BREAKOUT_TRIGGER_BUFFER_PCT", "0.12"), 0.12) / 100.0
    near_pct = abs(price - trigger) / trigger * 100.0 if trigger > 0 else 999.0
    over_trigger = price >= trigger * (1.0 + max(0.0, buffer_pct))
    not_too_far = price <= trigger * (1.0 + _num(os.getenv("BREAKOUT_MAX_EXTENDED_FROM_TRIGGER_PCT", "2.20"), 2.20) / 100.0)
    above_ema50 = price >= _num(ind.get("ema50"), 0.0) if _num(ind.get("ema50"), 0.0) > 0 else True
    ema_alignment = (_num(ind.get("ema9"), 0.0) >= _num(ind.get("ema21"), 0.0) >= _num(ind.get("ema50"), 0.0)) if _num(ind.get("ema50"), 0.0) > 0 else True
    macd_ok = _num(ind.get("macd_hist"), 0.0) >= -0.03
    rsi = _num(ind.get("rsi14"), 50.0)
    rsi_ok = rsi <= _num(os.getenv("BREAKOUT_MAX_RSI", "76"), 76.0)
    volume_ok = vol_ratio >= _num(os.getenv("BREAKOUT_TRIGGER_MIN_VOL_RATIO", "1.05"), 1.05)
    rs_ok = _relative_strength_hint(row)
    rr_ok = _rr_ok(row, price, trigger)
    atr_pct = _num(ind.get("atr_pct"), 0.0)
    # Very high ATR is not a hard blocker, but raises risk.
    atr_ok = atr_pct <= _num(os.getenv("BREAKOUT_MAX_ATR_PCT", "8.5"), 8.5) if atr_pct > 0 else True
    ready = near_pct <= _num(os.getenv("BREAKOUT_READINESS_NEAR_PCT", "1.00"), 1.0) and above_ema50 and not over_trigger

    blockers: list[str] = []
    if not over_trigger:
        blockers.append(f"السعر لم يخترق مستوى التفعيل {round(trigger, 2)} بوضوح")
    if not not_too_far:
        blockers.append("السعر ابتعد كثيرًا عن نقطة الاختراق — خطر مطاردة")
    if not above_ema50:
        blockers.append("السعر تحت EMA50؛ الاختراق أضعف")
    if not ema_alignment:
        blockers.append("EMA9/21/50 لا تدعم اختراقًا نظيفًا")
    if not volume_ok:
        blockers.append(f"حجم الاختراق غير كافٍ بعد ({round(vol_ratio, 2)}x)")
    if not macd_ok:
        blockers.append("MACD لا يؤكد الزخم")
    if not rsi_ok:
        blockers.append(f"RSI مرتفع جدًا للاختراق ({round(rsi, 1)})")
    if not rs_ok:
        blockers.append("القوة النسبية لا تدعم الاختراق")
    if not rr_ok:
        blockers.append("العائد/المخاطرة غير كافٍ بعد الاختراق")
    if not atr_ok:
        blockers.append("التذبذب مرتفع جدًا مقارنة بالخطة")

    row["breakout_quality"] = {
        "symbol": sym,
        "trigger": round(trigger, 4),
        "price": round(price, 4),
        "near_pct": round(near_pct, 3),
        "over_trigger": over_trigger,
        "ready": ready,
        "volume_ratio": round(vol_ratio, 3),
        "ema9": ind.get("ema9"),
        "ema21": ind.get("ema21"),
        "ema50": ind.get("ema50"),
        "ema200": ind.get("ema200"),
        "rsi14": ind.get("rsi14"),
        "macd_hist": ind.get("macd_hist"),
        "atr_pct": ind.get("atr_pct"),
        "blockers": blockers[:10],
    }
    row["breakout_trigger_price"] = round(trigger, 4)

    if ready:
        row["breakout_readiness_status"] = "near_breakout"
        row["breakout_readiness_label_ar"] = f"قريب من اختراق مهم — راقب {round(trigger, 2)}"
        row.setdefault("watch_reason_ar", f"قريب من اختراق مهم عند {round(trigger, 2)}؛ لا شراء قبل التفعيل بسيولة.")
        # Keep non-BUY rows visible as pre-trigger, but do not promote to BUY_NOW.
        if _s(row.get("decision")) not in {"دخول قوي", "دخول بحذر"}:
            row["decision"] = "مراقبة"
            row["owner_action"] = row.get("owner_action") or f"راقب فقط؛ يتحول إذا اخترق {round(trigger, 2)} بسيولة."

    # Strong breakout must satisfy all core guards.
    if _s(row.get("decision")) == "دخول قوي" or _s(row.get("final_decision_code")) == "BUY_NOW":
        if blockers:
            row["breakout_quality_demoted_buy_now"] = True
            row["breakout_quality_blockers"] = blockers[:10]
            row["final_decision_code"] = "WAIT_TRIGGER"
            row["decision"] = "دخول بحذر" if over_trigger and volume_ok and len(blockers) <= 2 else "مراقبة"
            row["effective_decision"] = row["decision"]
            row["final_decision_label"] = "اختراق غير مكتمل"
            row["execution_readiness_label"] = "قريب من الاختراق — لا BUY_NOW بعد"
            row["owner_action"] = f"لا شراء الآن؛ اختراق المقاومة يحتاج تأكيدًا فوق {round(trigger, 2)} مع حجم وثبات."
            existing = list(row.get("final_decision_blockers") or [])
            row["final_decision_blockers"] = (existing + blockers)[:12]
        else:
            row["breakout_quality_confirmed"] = True
            row["breakout_readiness_label_ar"] = "اختراق مؤكد بشروط جودة"
    return row


def enrich_breakout_quality_rows(rows: list[dict]) -> list[dict]:
    count = 0
    for row in rows or []:
        if count >= MAX_ROWS:
            break
        try:
            if isinstance(row, dict) and _is_breakout_text(row):
                evaluate_breakout_row(row)
                count += 1
        except Exception as exc:
            try:
                row["breakout_quality_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
            except Exception:
                pass
    return rows


def breakout_quality_status() -> dict:
    return {
        "ok": True,
        "version": BREAKOUT_QUALITY_VERSION,
        "cache_size": len(_CACHE),
        "max_rows": MAX_ROWS,
        "rule_ar": "لا يتحول اختراق المقاومة إلى دخول قوي إلا بعد تجاوز مستوى التفعيل بوضوح مع حجم وزخم واتجاه ومخاطرة مقبولة؛ وقبلها يظهر كقريب من الاختراق.",
    }
