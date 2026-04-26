import time
from datetime import datetime, timedelta

from scanner import get_scan_universe

from .settings import (
    HISTORY_CACHE, HTTP_SESSION, INTRADAY_CACHE, INTRADAY_CACHE_TTL_CLOSED, INTRADAY_CACHE_TTL_OPEN,
    POLYGON_API_KEY, SNAPSHOT_CACHE, SNAPSHOT_CACHE_TTL_CLOSED, SNAPSHOT_CACHE_TTL_EXTENDED, SNAPSHOT_CACHE_TTL_OPEN,
)
from .utils import *
from .utils import _cache_get, _cache_set

def http_get_json(url, timeout=12):
    try:
        r = HTTP_SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except:
        return {}


def get_active_universe(max_symbols: int = 60):
    return get_scan_universe(max_symbols=max_symbols)


def get_daily_bars(symbol):
    try:
        today = datetime.utcnow().date()
        from_5y = (today - timedelta(days=365 * 5)).isoformat()
        to_date = today.isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{from_5y}/{to_date}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=22)
        return r.get("results", []) or []
    except:
        return []


def calculate_atr(daily_bars, period: int = 14) -> float:
    try:
        if not daily_bars or len(daily_bars) < period + 1:
            return 0.0
        true_ranges = []
        prev_close = None
        for row in daily_bars[-(period + 40):]:
            high = to_float(row.get("h"))
            low = to_float(row.get("l"))
            close = to_float(row.get("c"))
            if high <= 0 or low <= 0 or close <= 0:
                prev_close = close or prev_close
                continue
            if prev_close is None or prev_close <= 0:
                tr = high - low
            else:
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
            prev_close = close
        if len(true_ranges) < period:
            return 0.0
        return safe_round(sum(true_ranges[-period:]) / period, 4)
    except:
        return 0.0


def get_atr_overlay(entry_price: float, daily_bars) -> dict:
    try:
        entry_price = float(entry_price or 0)
        atr_14 = float(calculate_atr(daily_bars, 14) or 0)
        if entry_price <= 0:
            return {
                "atr_14": 0.0,
                "atr_pct": 0.0,
                "volatility_label": "لا توجد بيانات كافية",
                "volatility_detail": "لا توجد بيانات كافية لتحديد التذبذب.",
                "atr_stop_suggestion": 0.0,
                "atr_target_1_suggestion": 0.0,
                "atr_target_2_suggestion": 0.0,
            }
        effective_atr = atr_14 if atr_14 > 0 else entry_price * 0.03
        atr_pct = (effective_atr / entry_price) * 100 if entry_price > 0 else 0.0
        if atr_pct <= 2.0:
            label = "هادئ"
            detail = "تذبذب السهم منخفض نسبيًا، ويمكن أن يتحمل وقفًا أقرب."
        elif atr_pct <= 4.5:
            label = "متوازن"
            detail = "تذبذب السهم طبيعي ومناسب لمعظم الخطط."
        elif atr_pct <= 7.0:
            label = "نشط"
            detail = "السهم متذبذب نسبيًا ويحتاج وقفًا أوسع وإدارة أدق."
        else:
            label = "عنيف"
            detail = "السهم عالي التذبذب وقد يضرب الوقف بسهولة إذا كان ضيقًا."
        return {
            "atr_14": safe_round(effective_atr, 4),
            "atr_pct": safe_round(atr_pct, 2),
            "volatility_label": label,
            "volatility_detail": detail,
            "atr_stop_suggestion": safe_round(entry_price - (effective_atr * 1.5)),
            "atr_target_1_suggestion": safe_round(entry_price + (effective_atr * 2.0)),
            "atr_target_2_suggestion": safe_round(entry_price + (effective_atr * 4.0)),
        }
    except:
        return {
            "atr_14": 0.0,
            "atr_pct": 0.0,
            "volatility_label": "لا توجد بيانات كافية",
            "volatility_detail": "لا توجد بيانات كافية لتحديد التذبذب.",
            "atr_stop_suggestion": 0.0,
            "atr_target_1_suggestion": 0.0,
            "atr_target_2_suggestion": 0.0,
        }


def get_history_levels(symbol, prev_data=None, daily_bars=None):
    if symbol in HISTORY_CACHE:
        return HISTORY_CACHE[symbol]

    out = {
        "year_high": 0.0,
        "ath_high": 0.0,
        "near_52w_high": False,
        "near_ath": False,
        "ath_breakout_zone": False,
    }

    bars = daily_bars if daily_bars is not None else get_daily_bars(symbol)
    if bars:
        ny = ZoneInfo("America/New_York")
        cutoff_52w = datetime.utcnow().date() - timedelta(days=365)
        highs_5 = []
        highs_52 = []
        for row in bars:
            high = to_float(row.get("h"))
            if high <= 0:
                continue
            highs_5.append(high)
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None
            if row_date and row_date >= cutoff_52w:
                highs_52.append(high)

        if highs_52:
            out["year_high"] = max(highs_52)
        if highs_5:
            out["ath_high"] = max(highs_5)

    prev = prev_data if prev_data is not None else get_prev_from_daily_bars(bars) or get_prev(symbol)
    if prev:
        price = prev["price"]
        if out["year_high"] > 0:
            out["near_52w_high"] = price >= out["year_high"] * 0.97
        if out["ath_high"] > 0:
            out["near_ath"] = price >= out["ath_high"] * 0.97
            out["ath_breakout_zone"] = price >= out["ath_high"] * 0.995

    HISTORY_CACHE[symbol] = out
    return out


def get_trend(symbol, daily_bars=None):
    try:
        data = daily_bars if daily_bars is not None else get_daily_bars(symbol)
        closes = [to_float(x.get("c")) for x in data if to_float(x.get("c")) > 0]
        if len(closes) < 50:
            return {"trend": "unknown", "ma20": 0.0, "ma50": 0.0}

        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes[-50:]) / 50
        price = closes[-1]

        if price > ma20 > ma50:
            trend = "صاعد قوي"
        elif price > ma50:
            trend = "صاعد"
        elif price < ma20 < ma50:
            trend = "هابط"
        else:
            trend = "متذبذب"

        return {"trend": trend, "ma20": ma20, "ma50": ma50}
    except:
        return {"trend": "unknown", "ma20": 0.0, "ma50": 0.0}


def get_session_elapsed_ratio() -> float:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return 0.0
        session_start = 9 * 60 + 30
        session_end = 16 * 60
        current_minutes = now_ny.hour * 60 + now_ny.minute
        if current_minutes <= session_start:
            return 0.0
        if current_minutes >= session_end:
            return 1.0
        elapsed = (current_minutes - session_start) / float(session_end - session_start)
        return clamp(elapsed, 0.0, 1.0)
    except:
        return 0.0


def get_volume_ratio(symbol, intraday=None, daily_bars=None):
    try:
        data = daily_bars if daily_bars is not None else get_daily_bars(symbol)
        if not data:
            return 1.0

        market_open = is_market_open_now()
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date()

        historical_volumes = []
        current_session_daily_volume = 0.0

        for row in data:
            volume = to_float(row.get("v"))
            if volume <= 0:
                continue
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None

            if market_open and row_date == today_ny:
                current_session_daily_volume = volume
                continue

            historical_volumes.append(volume)

        if len(historical_volumes) < 20:
            if len(historical_volumes) >= 5:
                avg_volume = sum(historical_volumes) / len(historical_volumes)
            else:
                return 1.0
        else:
            avg_volume = sum(historical_volumes[-20:]) / 20.0

        if avg_volume <= 0:
            return 1.0

        if market_open:
            intraday = intraday or get_intraday_snapshot(symbol)
            session_volume = float((intraday or {}).get("session_volume", 0) or 0)
            if session_volume <= 0:
                session_volume = current_session_daily_volume
            elapsed_ratio = float((intraday or {}).get("session_elapsed_ratio", 0) or 0)
            if elapsed_ratio <= 0:
                elapsed_ratio = get_session_elapsed_ratio()
            if session_volume > 0 and elapsed_ratio > 0:
                normalized_elapsed = max(elapsed_ratio, 0.08)
                projected_day_volume = session_volume / normalized_elapsed
                return clamp(projected_day_volume / avg_volume, 0.2, 8.0)

        latest_complete_volume = historical_volumes[-1] if historical_volumes else current_session_daily_volume
        if latest_complete_volume <= 0:
            return 1.0
        return clamp(latest_complete_volume / avg_volume, 0.2, 8.0)
    except:
        return 1.0


def get_intraday_snapshot(symbol):
    symbol = str(symbol).upper().strip()
    market_open = is_market_open_now()
    cache_key = f"{symbol}:{'open' if market_open else 'closed'}"
    ttl = INTRADAY_CACHE_TTL_OPEN if market_open else INTRADAY_CACHE_TTL_CLOSED

    cached = _cache_get(INTRADAY_CACHE, cache_key)
    if cached:
        return cached

    out = {
        "available": False,
        "market_open": market_open,
        "current_price": 0.0,
        "session_open": 0.0,
        "session_high": 0.0,
        "session_low": 0.0,
        "session_volume": 0.0,
        "avg_5m_volume": 0.0,
        "latest_5m_volume": 0.0,
        "intraday_volume_ratio": 0.0,
        "vwap_proxy": 0.0,
        "above_vwap_proxy": False,
        "opening_drive": "unknown",
        "bars_count": 0,
        "session_elapsed_ratio": 0.0,
        "projected_day_volume": 0.0,
        "recent_red_bars": 0,
        "recent_green_bars": 0,
        "pullback_volume_dry": False,
        "pullback_volume_ratio": 0.0,
        "spike_from_open_pct": 0.0,
        "pullback_from_high_pct": 0.0,
        "session_position_pct": 0.0,
    }

    if not market_open:
        return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

    try:
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date().isoformat()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/5/minute/"
            f"{today_ny}/{today_ny}?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=15)
        bars = r.get("results", [])
        if not bars:
            return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

        volumes = [to_float(x.get("v")) for x in bars if to_float(x.get("v")) > 0]
        closes = [to_float(x.get("c")) for x in bars if to_float(x.get("c")) > 0]
        if not volumes or not closes:
            return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)

        session_open = to_float(bars[0].get("o"))
        session_high = max(to_float(x.get("h")) for x in bars)
        lows = [to_float(x.get("l")) for x in bars if to_float(x.get("l")) > 0]
        session_low = min(lows) if lows else 0.0
        session_volume = sum(volumes)
        latest_5m_volume = volumes[-1]
        avg_5m_volume = sum(volumes) / len(volumes) if volumes else 0.0
        intraday_volume_ratio = latest_5m_volume / avg_5m_volume if avg_5m_volume > 0 else 0.0

        weighted_total = 0.0
        volume_total = 0.0
        for bar in bars:
            typical = (to_float(bar.get("h")) + to_float(bar.get("l")) + to_float(bar.get("c"))) / 3
            vol = to_float(bar.get("v"))
            if vol > 0:
                weighted_total += typical * vol
                volume_total += vol

        vwap_proxy = weighted_total / volume_total if volume_total > 0 else closes[-1]
        current_price = closes[-1]
        first_close = to_float(bars[0].get("c"))
        elapsed_ratio = get_session_elapsed_ratio()
        normalized_elapsed = max(elapsed_ratio, 0.08) if elapsed_ratio > 0 else 0.0
        projected_day_volume = (session_volume / normalized_elapsed) if normalized_elapsed > 0 else 0.0

        if current_price > session_open and current_price >= first_close:
            opening_drive = "صاعد"
        elif current_price < session_open and current_price <= first_close:
            opening_drive = "هابط"
        else:
            opening_drive = "متذبذب"

        recent_red_bars = 0
        recent_green_bars = 0
        recent_slice = bars[-4:]
        for idx in range(1, len(recent_slice)):
            prev_close = to_float(recent_slice[idx - 1].get("c"))
            cur_close = to_float(recent_slice[idx].get("c"))
            if cur_close < prev_close:
                recent_red_bars += 1
            elif cur_close > prev_close:
                recent_green_bars += 1

        last3 = volumes[-3:] if len(volumes) >= 3 else volumes
        prior6 = volumes[-9:-3] if len(volumes) >= 9 else volumes[:-3]
        last3_avg = sum(last3) / len(last3) if last3 else 0.0
        prior6_avg = sum(prior6) / len(prior6) if prior6 else avg_5m_volume
        pullback_volume_ratio = (last3_avg / prior6_avg) if prior6_avg > 0 else 0.0
        pullback_volume_dry = pullback_volume_ratio <= 0.85 if prior6_avg > 0 else False

        spike_from_open_pct = ((session_high - session_open) / session_open) if session_open > 0 and session_high > 0 else 0.0
        pullback_from_high_pct = ((session_high - current_price) / session_high) if session_high > 0 and current_price > 0 else 0.0
        session_range = max(session_high - session_low, 0.0001)
        session_position_pct = ((current_price - session_low) / session_range) * 100 if session_range > 0 else 0.0

        out = {
            "available": True,
            "market_open": market_open,
            "current_price": current_price,
            "session_open": session_open,
            "session_high": session_high,
            "session_low": session_low,
            "session_volume": session_volume,
            "avg_5m_volume": avg_5m_volume,
            "latest_5m_volume": latest_5m_volume,
            "intraday_volume_ratio": intraday_volume_ratio,
            "vwap_proxy": vwap_proxy,
            "above_vwap_proxy": current_price >= vwap_proxy if vwap_proxy > 0 else False,
            "opening_drive": opening_drive,
            "bars_count": len(bars),
            "session_elapsed_ratio": safe_round(elapsed_ratio, 4),
            "projected_day_volume": safe_round(projected_day_volume),
            "recent_red_bars": recent_red_bars,
            "recent_green_bars": recent_green_bars,
            "pullback_volume_dry": pullback_volume_dry,
            "pullback_volume_ratio": safe_round(pullback_volume_ratio, 2),
            "spike_from_open_pct": safe_round(spike_from_open_pct * 100, 2),
            "pullback_from_high_pct": safe_round(pullback_from_high_pct * 100, 2),
            "session_position_pct": safe_round(session_position_pct, 2),
        }
    except:
        pass

    return _cache_set(INTRADAY_CACHE, cache_key, out, ttl)


def build_live_price_block(symbol, prev_data, intraday_data):
    phase = get_market_phase()
    prev_price = to_float(prev_data.get("price", 0)) if prev_data else 0.0
    prev_open = to_float(prev_data.get("open", 0)) if prev_data else 0.0
    prev_high = to_float(prev_data.get("high", 0)) if prev_data else 0.0
    prev_low = to_float(prev_data.get("low", 0)) if prev_data else 0.0
    prev_volume = to_float(prev_data.get("volume", 0)) if prev_data else 0.0

    snap = {}
    if not (phase == "open" and intraday_data.get("available") and to_float(intraday_data.get("current_price", 0)) > 0):
        snap = get_snapshot_quote(symbol)

    current_price = prev_price
    open_price = prev_open
    previous_close = prev_price
    change_vs_prev_close_pct = 0.0
    change_from_open_pct = 0.0
    price_source = "previous_close"
    price_reliable_for_execution = False

    if phase == "open" and intraday_data.get("available") and to_float(intraday_data.get("current_price", 0)) > 0:
        current_price = to_float(intraday_data.get("current_price", 0)) or prev_price
        open_price = to_float(intraday_data.get("session_open", 0)) or prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100
        if previous_close > 0 and current_price > 0:
            change_vs_prev_close_pct = ((current_price - previous_close) / previous_close) * 100
        price_source = "live_intraday"
        price_reliable_for_execution = True
    elif phase in {"after_hours", "pre_market"} and snap.get("available") and to_float(snap.get("current_price", 0)) > 0:
        current_price = to_float(snap.get("current_price", prev_price)) or prev_price
        open_price = to_float(snap.get("open", prev_open)) or prev_open
        previous_close = to_float(snap.get("previous_close", prev_price)) or prev_price
        change_from_open_pct = to_float(snap.get("change_from_open_pct", 0))
        change_vs_prev_close_pct = to_float(snap.get("change_vs_prev_close_pct", 0))
        price_source = phase
        price_reliable_for_execution = True
    elif phase == "closed":
        current_price = prev_price
        open_price = prev_open
        previous_close = prev_price
        if open_price > 0 and current_price > 0:
            change_from_open_pct = ((current_price - open_price) / open_price) * 100
        price_source = "previous_close"
        price_reliable_for_execution = False
    elif snap.get("available") and to_float(snap.get("current_price", 0)) > 0:
        current_price = to_float(snap.get("current_price", prev_price)) or prev_price
        open_price = to_float(snap.get("open", prev_open)) or prev_open
        previous_close = to_float(snap.get("previous_close", prev_price)) or prev_price
        change_from_open_pct = to_float(snap.get("change_from_open_pct", 0))
        change_vs_prev_close_pct = to_float(snap.get("change_vs_prev_close_pct", 0))
        price_source = str(snap.get("source", "snapshot") or "snapshot")
        price_reliable_for_execution = False
    else:
        current_price = 0.0 if phase in {"open", "after_hours", "pre_market"} else prev_price
        open_price = prev_open
        previous_close = prev_price
        price_source = "unavailable_realtime"
        price_reliable_for_execution = False

    price_source_label_map = {
        "live_intraday": "مباشر أثناء التداول",
        "after_hours": "بعد الإغلاق",
        "pre_market": "قبل الافتتاح",
        "previous_close": "آخر إغلاق",
        "unavailable_realtime": "بيانات لحظية غير متاحة",
        "snapshot": "لقطة سوق",
        "minute+snapshot": "دقيقة + لقطة",
    }

    display_price = current_price if current_price > 0 else previous_close
    display_price_label = "السعر الحالي" if current_price > 0 else "آخر إغلاق"
    live_price_available = current_price > 0
    display_change_pct = change_vs_prev_close_pct if previous_close > 0 else change_from_open_pct
    display_change_available = abs(display_change_pct) > 0 or live_price_available

    high_live = prev_high
    low_live = prev_low
    volume_live = prev_volume
    last_price_update_ms = int(time.time() * 1000) if phase == "open" and intraday_data.get("available") else int(to_float(snap.get("updated", 0)))

    if phase == "open" and intraday_data.get("available"):
        high_live = safe_round(to_float(intraday_data.get("session_high", 0)) or prev_high)
        low_live = safe_round(to_float(intraday_data.get("session_low", 0)) or prev_low)
        volume_live = safe_round(to_float(intraday_data.get("session_volume", 0)) or prev_volume)
    else:
        high_live = safe_round(to_float(snap.get("high", prev_high)) or prev_high)
        low_live = safe_round(to_float(snap.get("low", prev_low)) or prev_low)
        volume_live = safe_round(to_float(snap.get("volume", prev_volume)) or prev_volume)

    return {
        "market_phase": phase,
        "market_phase_label": market_phase_label(phase),
        "current_price_live": safe_round(current_price),
        "open_price_live": safe_round(open_price),
        "previous_close_live": safe_round(previous_close),
        "change_from_open_pct": safe_round(change_from_open_pct),
        "change_vs_prev_close_pct": safe_round(change_vs_prev_close_pct),
        "display_price": safe_round(display_price),
        "display_price_label": display_price_label,
        "display_change_pct": safe_round(display_change_pct),
        "display_change_available": display_change_available,
        "live_price_available": live_price_available,
        "high_live": high_live,
        "low_live": low_live,
        "volume_live": volume_live,
        "price_source": price_source,
        "price_source_label": price_source_label_map.get(price_source, price_source),
        "price_reliable_for_execution": price_reliable_for_execution,
        "last_price_update_ms": last_price_update_ms,
        "last_price_update_label": datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S"),
    }


def compute_volume_pace_ratio(intraday: dict, daily_volume_ratio: float) -> float:
    try:
        if not intraday or not intraday.get("available"):
            return float(daily_volume_ratio or 0)
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        latest_5m = float(intraday.get("latest_5m_volume", 0) or 0)
        avg_5m = float(intraday.get("avg_5m_volume", 0) or 0)
        pullback_volume_ratio = float(intraday.get("pullback_volume_ratio", 0) or 0)
        burst_ratio = (latest_5m / avg_5m) if avg_5m > 0 else intraday_ratio
        if pullback_volume_ratio > 0:
            return clamp(max(float(daily_volume_ratio or 0), intraday_ratio, burst_ratio, 1 / max(pullback_volume_ratio, 0.01) if pullback_volume_ratio < 1 else pullback_volume_ratio), 0.2, 8.0)
        return clamp(max(float(daily_volume_ratio or 0), intraday_ratio, burst_ratio), 0.2, 8.0)
    except:
        return float(daily_volume_ratio or 0)


def get_effective_volume_ratio(volume_ratio: float, intraday: dict) -> float:
    try:
        effective = float(volume_ratio or 0)
        if intraday and intraday.get("available"):
            intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
            pace_ratio = compute_volume_pace_ratio(intraday, volume_ratio)
            projected_bias = 0.0
            projected_day_volume = float(intraday.get("projected_day_volume", 0) or 0)
            session_volume = float(intraday.get("session_volume", 0) or 0)
            if projected_day_volume > 0 and session_volume > 0:
                projected_bias = projected_day_volume / max(session_volume, 1.0)
            effective = max(
                effective,
                intraday_ratio * 0.9,
                pace_ratio,
                min(max(float(volume_ratio or 0), 0.0) + (projected_bias * 0.02), 8.0)
            )
        return clamp(effective, 0.2, 8.0)
    except:
        return float(volume_ratio or 0)

