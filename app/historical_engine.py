from .utils import *
from .market_data import get_daily_bars

def _avg(values):
    vals = [float(x) for x in values if float(x or 0) > 0]
    return (sum(vals) / len(vals)) if vals else 0.0


def _historical_confidence_weight(label: str) -> float:
    txt = str(label or "")
    if "عالية" in txt:
        return 1.0
    if "متوسطة" in txt:
        return 0.8
    if "محدودة" in txt:
        return 0.55
    return 0.35


def _historical_pct_score(pct: float, cases: int, confidence_label: str, speed_label: str = "") -> float:
    try:
        pct = float(pct or 0)
        cases = int(cases or 0)
        confidence_weight = _historical_confidence_weight(confidence_label)
        score = 50.0 + ((pct - 50.0) * 0.6 * confidence_weight)
        if cases >= 30:
            score += 8
        elif cases >= 15:
            score += 5
        elif cases >= 8:
            score += 2
        elif cases <= 3:
            score -= 4
        speed = str(speed_label or "")
        if "سريع" in speed:
            score += 4
        elif "متوسط" in speed:
            score += 2
        elif "بطيء" in speed:
            score -= 2
        return max(1.0, min(99.0, round(score, 2)))
    except:
        return 50.0


def _support_mode_from_score(score: float) -> str:
    score = float(score or 0)
    if score >= 3:
        return "positive"
    if score <= -3:
        return "negative"
    return "neutral"


def _series_close_at(bars, idx: int) -> float:
    try:
        row = bars[idx] if 0 <= idx < len(bars or []) else {}
        return float((row or {}).get("c", 0) or 0)
    except:
        return 0.0


def _return_mode_from_series(bars, idx: int, lookback: int = 20) -> str:
    try:
        if not bars or idx < lookback:
            return "neutral"
        start = _series_close_at(bars, idx - lookback)
        end = _series_close_at(bars, idx)
        if start <= 0 or end <= 0:
            return "neutral"
        ret = ((end - start) / start) * 100.0
        if ret >= 1.5:
            return "positive"
        if ret <= -1.5:
            return "negative"
        return "neutral"
    except:
        return "neutral"


def analyze_historical_context_behavior(
    stock_bars,
    benchmark_symbol: str = "",
    sector_symbol: str = "",
    current_setup: str = "",
    market_support_score: float = 0.0,
    sector_support_score: float = 0.0
) -> dict:
    base = {
        "historical_context_ready": False,
        "historical_market_context_success_pct": 0.0,
        "historical_market_context_cases": 0,
        "historical_sector_context_success_pct": 0.0,
        "historical_sector_context_cases": 0,
        "historical_combined_context_success_pct": 0.0,
        "historical_combined_context_cases": 0,
        "historical_context_score": 50.0,
        "historical_context_label": "محايد",
        "historical_context_detail": "لا توجد بيانات كافية لربط السهم تاريخيًا مع المؤشر والقطاع.",
        "historical_context_partial": False,
    }

    try:
        def _support_text(score: float) -> str:
            score = float(score or 0)
            if score >= 3:
                return "داعم"
            if score <= -3:
                return "ضاغط"
            return "محايد"

        def _partial_label(score: float) -> str:
            score = float(score or 0)
            if score >= 58:
                return "قراءة جزئية داعمة"
            if score >= 52:
                return "قراءة جزئية إيجابية"
            if score >= 46:
                return "قراءة جزئية محايدة"
            return "قراءة جزئية حذرة"

        def _partial_fallback(reason: str) -> dict:
            partial = dict(base)

            market_bias = clamp(market_support_score, -12, 12)
            sector_bias = clamp(sector_support_score, -12, 12) if sector_symbol else 0.0

            score = 50.0
            detail_parts = []

            if benchmark_symbol:
                score += market_bias * 0.55
                detail_parts.append(
                    f"المؤشر {benchmark_symbol}: {_support_text(market_support_score)} حاليًا."
                )
            else:
                detail_parts.append("المؤشر المرجعي غير متوفر.")

            if sector_symbol:
                score += sector_bias * 0.45
                detail_parts.append(
                    f"القطاع {sector_symbol}: {_support_text(sector_support_score)} حاليًا."
                )
            else:
                detail_parts.append("ETF القطاع غير متوفر، لذا اعتمدنا على المؤشر فقط.")

            score = round(max(35.0, min(65.0, score - 4.0)), 2)

            partial.update({
                "historical_context_ready": False,
                "historical_context_score": score,
                "historical_context_label": _partial_label(score),
                "historical_context_detail": (
                    " ".join(detail_parts)
                    + f" {reason} خُفِّضت الثقة قليلًا لغياب الحالات التاريخية المطابقة."
                ),
                "historical_context_partial": True,
            })
            return partial

        if not stock_bars or len(stock_bars) < 120:
            return _partial_fallback("بيانات السهم التاريخية نفسها غير كافية لبناء مقارنة كاملة.")

        bench_bars = get_daily_bars(benchmark_symbol) if benchmark_symbol else []
        sector_bars = get_daily_bars(sector_symbol) if sector_symbol else []

        min_len = len(stock_bars)
        if bench_bars:
            min_len = min(min_len, len(bench_bars))
        if sector_bars:
            min_len = min(min_len, len(sector_bars))

        if min_len < 120:
            return _partial_fallback("لا توجد سلاسل مشتركة كافية بين السهم والمؤشر/القطاع لبناء مقارنة كاملة.")

        bars = list(stock_bars)[-min_len:]
        bench = list(bench_bars)[-min_len:] if bench_bars else []
        sect = list(sector_bars)[-min_len:] if sector_bars else []

        market_mode = _support_mode_from_score(market_support_score)
        sector_mode = _support_mode_from_score(sector_support_score)

        market_cases = market_success = 0
        sector_cases = sector_success = 0
        combined_cases = combined_success = 0

        for i in range(60, len(bars) - 6):
            row = bars[i]
            o = to_float((row or {}).get("o"))
            h = to_float((row or {}).get("h"))
            l = to_float((row or {}).get("l"))
            c = to_float((row or {}).get("c"))
            v = to_float((row or {}).get("v"))

            if min(o, h, l, c) <= 0:
                continue

            prev20_high = max(to_float((b or {}).get("h", 0)) for b in bars[i - 20:i])
            avg20_vol = _avg([to_float((b or {}).get("v", 0)) for b in bars[i - 20:i]])
            sma20 = _avg([to_float((b or {}).get("c", 0)) for b in bars[i - 20:i]])
            sma50 = _avg([to_float((b or {}).get("c", 0)) for b in bars[i - 50:i]])
            future = bars[i + 1:i + 6]
            future_highs = [to_float((b or {}).get("h", 0)) for b in future if to_float((b or {}).get("h", 0)) > 0]

            if not future_highs:
                continue

            breakout_case = c >= prev20_high * 1.002 and avg20_vol > 0 and v >= avg20_vol * 1.15
            near_support = (
                (sma20 > 0 and l <= sma20 * 1.01 and c >= sma20)
                or
                (sma50 > 0 and l <= sma50 * 1.01 and c >= sma50)
            )
            pullback_case = near_support and c > o

            setup_match = (
                breakout_case if str(current_setup or "") == "Breakout"
                else pullback_case if str(current_setup or "") == "Pullback"
                else (breakout_case or pullback_case)
            )
            if not setup_match:
                continue

            success = max(future_highs) >= c * 1.03

            bench_mode = _return_mode_from_series(bench, i, 20) if bench else "neutral"
            sec_mode = _return_mode_from_series(sect, i, 20) if sect else "neutral"

            market_match = (market_mode == "neutral") or (bench_mode == market_mode)
            sector_match = (sector_mode == "neutral") or (sec_mode == sector_mode)

            if market_match:
                market_cases += 1
                if success:
                    market_success += 1

            if sect and sector_match:
                sector_cases += 1
                if success:
                    sector_success += 1

            if market_match and (not sect or sector_match):
                combined_cases += 1
                if success:
                    combined_success += 1

        market_pct = safe_round((market_success / market_cases) * 100, 1) if market_cases > 0 else 0.0
        sector_pct = safe_round((sector_success / sector_cases) * 100, 1) if sector_cases > 0 else 0.0
        combined_pct = safe_round((combined_success / combined_cases) * 100, 1) if combined_cases > 0 else 0.0

        reference_pct = (
            combined_pct if combined_cases > 0
            else market_pct if market_cases > 0
            else sector_pct if sector_cases > 0
            else 50.0
        )
        reference_cases = (
            combined_cases if combined_cases > 0
            else max(market_cases, sector_cases, 0)
        )

        if reference_cases == 0:
            return _partial_fallback("لم نعثر على حالات تاريخية مشابهة مطابقة لهذا السياق الحالي.")

        score = _historical_pct_score(
            reference_pct,
            reference_cases,
            "عالية" if reference_cases >= 20 else "متوسطة" if reference_cases >= 10 else "محدودة"
        )

        if score >= 68:
            label = "يدعم بقوة"
        elif score >= 58:
            label = "يدعم"
        elif score >= 45:
            label = "محايد"
        else:
            label = "ضعيف"

        detail_parts = []

        if market_cases > 0:
            detail_parts.append(
                f"حالات مشابهة مع {benchmark_symbol}: نجحت في {market_pct}% من {market_cases} حالة."
            )
        elif benchmark_symbol:
            detail_parts.append(
                f"المؤشر {benchmark_symbol}: لا توجد حالات تاريخية كافية، لكن قراءته الحالية {_support_text(market_support_score)}."
            )

        if sector_symbol:
            if sector_cases > 0:
                detail_parts.append(
                    f"ومع {sector_symbol}: نجحت في {sector_pct}% من {sector_cases} حالة."
                )
            else:
                detail_parts.append(
                    f"القطاع {sector_symbol}: لا توجد حالات تاريخية كافية، لكن قراءته الحالية {_support_text(sector_support_score)}."
                )

        if combined_cases > 0 and sector_symbol:
            detail_parts.append(
                f"وعند توافقهما معًا نجحت في {combined_pct}% من {combined_cases} حالة."
            )

        if reference_cases < 6:
            score = round(max(1.0, min(99.0, score - 3.0)), 2)
            detail_parts.append("عدد الحالات محدود، لذلك خُفِّضت الثقة قليلًا.")

        detail_parts.append("هذه القراءة تربط نجاح السهم تاريخيًا بسياق المؤشر والقطاع الحاليين.")

        base.update({
            "historical_context_ready": bool(reference_cases >= 6),
            "historical_market_context_success_pct": market_pct,
            "historical_market_context_cases": market_cases,
            "historical_sector_context_success_pct": sector_pct,
            "historical_sector_context_cases": sector_cases,
            "historical_combined_context_success_pct": combined_pct,
            "historical_combined_context_cases": combined_cases,
            "historical_context_score": score,
            "historical_context_label": label,
            "historical_context_detail": " ".join([x for x in detail_parts if x]),
            "historical_context_partial": False,
        })
        return base

    except:
        return base


def analyze_historical_behavior(daily_bars, current_setup: str = "") -> dict:
    base = {
        "historical_behavior_ready": False,
        "historical_breakout_success_pct": 0.0,
        "historical_pullback_success_pct": 0.0,
        "historical_volume_followthrough_pct": 0.0,
        "historical_breakout_cases": 0,
        "historical_pullback_cases": 0,
        "historical_volume_cases": 0,
        "historical_confidence_label": "منخفضة",
        "historical_breakout_speed_label": "لا توجد بيانات كافية",
        "historical_pullback_speed_label": "لا توجد بيانات كافية",
        "historical_behavior_score": 50.0,
        "historical_behavior_label": "لا توجد بيانات كافية",
        "historical_behavior_detail": "لا توجد بيانات تاريخية كافية لبناء رأي سلوكي موثوق.",
    }
    try:
        if not daily_bars or len(daily_bars) < 120:
            return base
        bars = []
        for row in daily_bars:
            bars.append({
                "o": to_float(row.get("o")),
                "h": to_float(row.get("h")),
                "l": to_float(row.get("l")),
                "c": to_float(row.get("c")),
                "v": to_float(row.get("v")),
            })
        breakout_cases = breakout_success = 0
        breakout_days = []
        pullback_cases = pullback_success = 0
        pullback_days = []
        volume_cases = volume_success = 0
        for i in range(60, len(bars) - 6):
            close_i = bars[i]["c"]
            open_i = bars[i]["o"]
            high_i = bars[i]["h"]
            low_i = bars[i]["l"]
            vol_i = bars[i]["v"]
            if close_i <= 0 or high_i <= 0 or low_i <= 0 or open_i <= 0:
                continue
            prev20_high = max(b["h"] for b in bars[i-20:i])
            avg20_vol = _avg([b["v"] for b in bars[i-20:i]])
            sma20 = _avg([b["c"] for b in bars[i-20:i]])
            sma50 = _avg([b["c"] for b in bars[i-50:i]])
            future = bars[i+1:i+6]
            future_highs = [b["h"] for b in future if b["h"] > 0]
            future_lows = [b["l"] for b in future if b["l"] > 0]
            if not future_highs or not future_lows:
                continue

            if close_i >= prev20_high * 1.002 and avg20_vol > 0 and vol_i >= avg20_vol * 1.15:
                breakout_cases += 1
                for days_ahead, fb in enumerate(future, start=1):
                    if fb["h"] >= close_i * 1.03:
                        breakout_success += 1
                        breakout_days.append(days_ahead)
                        break

            near_support = False
            if sma20 > 0 and low_i <= sma20 * 1.01 and close_i >= sma20:
                near_support = True
            if not near_support and sma50 > 0 and low_i <= sma50 * 1.01 and close_i >= sma50:
                near_support = True
            if near_support and close_i > open_i:
                pullback_cases += 1
                for days_ahead, fb in enumerate(future, start=1):
                    if fb["h"] >= close_i * 1.03:
                        pullback_success += 1
                        pullback_days.append(days_ahead)
                        break

            day_change = ((close_i - open_i) / open_i) * 100 if open_i > 0 else 0.0
            if avg20_vol > 0 and vol_i >= avg20_vol * 1.5 and day_change >= 2.0:
                volume_cases += 1
                if max(future_highs) >= close_i * 1.02:
                    volume_success += 1

        breakout_pct = safe_round((breakout_success / breakout_cases) * 100, 1) if breakout_cases > 0 else 0.0
        pullback_pct = safe_round((pullback_success / pullback_cases) * 100, 1) if pullback_cases > 0 else 0.0
        volume_pct = safe_round((volume_success / volume_cases) * 100, 1) if volume_cases > 0 else 0.0

        def speed_label(days_list):
            if not days_list:
                return "لا توجد بيانات كافية"
            avg_days = sum(days_list) / len(days_list)
            if avg_days <= 2.0:
                return "سريع"
            if avg_days <= 3.5:
                return "متوسط"
            return "بطيء"

        sample_size = max(breakout_cases, pullback_cases, volume_cases)
        if sample_size >= 30:
            confidence = "عالية"
        elif sample_size >= 15:
            confidence = "متوسطة"
        elif sample_size >= 8:
            confidence = "محدودة"
        else:
            confidence = "منخفضة"

        breakout_speed = speed_label(breakout_days)
        pullback_speed = speed_label(pullback_days)

        setup = str(current_setup or "")
        if setup == "Breakout":
            main_pct = breakout_pct
            main_label = "يدعم الاختراق" if breakout_pct >= 60 else "محايد" if breakout_pct >= 45 else "ضعيف في الاختراق"
            parts = [f"في مثل حالة هذه الفرصة (اختراق)، نجحت الاختراقات المشابهة في {breakout_pct}% من {breakout_cases} حالة، وسرعة الحركة غالبًا {breakout_speed}."]
            if volume_cases > 0:
                parts.append(f"ومع السيولة القوية نجح الاستمرار في {volume_pct}% من {volume_cases} حالة.")
            parts.append(f"درجة الثقة {confidence}.")
            detail = " ".join(parts)
        elif setup == "Pullback":
            main_pct = pullback_pct
            main_label = "يحترم الارتداد" if pullback_pct >= 60 else "محايد" if pullback_pct >= 45 else "ضعيف في الارتداد"
            parts = [f"في مثل حالة هذه الفرصة (ارتداد)، نجحت الارتدادات المشابهة في {pullback_pct}% من {pullback_cases} حالة، وسرعة التعافي غالبًا {pullback_speed}."]
            if volume_cases > 0:
                parts.append(f"ومع السيولة القوية نجح الاستمرار في {volume_pct}% من {volume_cases} حالة.")
            parts.append(f"درجة الثقة {confidence}.")
            detail = " ".join(parts)
        else:
            main_pct = max(breakout_pct, pullback_pct, volume_pct)
            main_label = "سلوك تاريخي داعم" if main_pct >= 60 else "محايد" if main_pct >= 45 else "ضعيف"
            detail = f"نجاح الاختراقات {breakout_pct}% ({breakout_cases} حالة)، والارتدادات {pullback_pct}% ({pullback_cases} حالة)، واستجابة السيولة {volume_pct}% ({volume_cases} حالة). درجة الثقة {confidence}."

        main_speed = breakout_speed if setup == "Breakout" else pullback_speed if setup == "Pullback" else breakout_speed
        historical_behavior_score = _historical_pct_score(main_pct, max(breakout_cases, pullback_cases, volume_cases), confidence, main_speed)
        return {
            "historical_behavior_ready": True,
            "historical_breakout_success_pct": breakout_pct,
            "historical_pullback_success_pct": pullback_pct,
            "historical_volume_followthrough_pct": volume_pct,
            "historical_breakout_cases": breakout_cases,
            "historical_pullback_cases": pullback_cases,
            "historical_volume_cases": volume_cases,
            "historical_confidence_label": confidence,
            "historical_breakout_speed_label": breakout_speed,
            "historical_pullback_speed_label": pullback_speed,
            "historical_behavior_score": historical_behavior_score,
            "historical_behavior_label": main_label,
            "historical_behavior_detail": detail,
        }
    except:
        return base


