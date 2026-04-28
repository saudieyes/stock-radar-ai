from .utils import *

def compute_execution_status(trade_type: str, decision: str, trend: str, volume_ratio: float, catalyst_score: float, breakout_quality: str) -> str:
    if breakout_quality == "FAILED" and trade_type == "Breakout":
        return "AVOID"

    if decision == "دخول قوي" and trend == "صاعد قوي" and volume_ratio >= 1.2 and breakout_quality in {"STRONG", "WEAK"} and catalyst_score >= 0:
        return "READY"

    if decision in {"دخول قوي", "دخول بحذر"}:
        return "WAIT"

    if decision == "مراقبة":
        return "WAIT"

    return "AVOID"


def owner_decision(decision: str, trend: str, breakout_quality: str, volume_ratio: float, catalyst_score: float) -> str:
    if breakout_quality == "FAILED":
        return "لا تزد الكمية الآن - الأفضل الاحتفاظ بحذر أو التخفيف إذا كسر الدعم"
    if decision == "دخول قوي" and trend == "صاعد قوي" and volume_ratio >= 1.2:
        return "يمكن الشراء أو زيادة الكمية بشكل جزئي"
    if decision == "دخول بحذر":
        return "احتفاظ أو زيادة جزئية بحذر بعد تأكيد الحركة"
    if trend == "هابط":
        return "الأفضل عدم زيادة الكمية ومراقبة الدعم"
    return "احتفاظ ومراقبة - لا توجد زيادة واضحة الآن"



def breakout_quality_label(trade_type: str, momentum: str, body_strength: float, close_strength: float, volume_ratio: float) -> str:
    """تصنيف جودة الاختراق.

    أعيدت هنا لأنها كانت موجودة في main.py قبل التفريغ،
    وتحتاجها strategy_engine عند بناء خطة الاختراق.
    """
    try:
        if trade_type != "Breakout":
            return "N/A"
        if momentum == "صاعد" and body_strength >= 0.6 and close_strength >= 0.75 and volume_ratio >= 1.2:
            return "STRONG"
        if body_strength < 0.35 or close_strength < 0.5 or volume_ratio < 0.8:
            return "FAILED"
        return "WEAK"
    except Exception:
        return "WEAK"

def apply_news_decision_guard(decision: str, news_scope: str, news_sentiment: str, news_sessions_since: int, core_quality: float = 0.0) -> str:
    decision = str(decision or "مراقبة")
    scope = str(news_scope or "neutral")
    sentiment = str(news_sentiment or "neutral")
    try:
        sessions = int(float(news_sessions_since))
    except Exception:
        sessions = 999
    quality = float(core_quality or 0)

    if scope == "company" and sentiment == "legal":
        if sessions <= NEGATIVE_NEWS_MAX_SESSIONS:
            return "مراقبة"
    if scope == "company" and sentiment == "negative":
        if sessions <= NEGATIVE_NEWS_MAX_SESSIONS and decision == "دخول قوي":
            return "دخول بحذر"
        if sessions <= 2 and decision == "دخول بحذر" and quality < 80:
            return "مراقبة"
    if scope == "sector" and sentiment in {"negative", "legal"} and sessions <= min(NEGATIVE_NEWS_MAX_SESSIONS, 3) and decision == "دخول قوي":
        return "دخول بحذر"
    return decision


def apply_market_sector_decision_guard(decision: str, market_sector_score: float, market_support_label: str = "", sector_support_label: str = "", sector_symbol: str = "") -> str:
    try:
        decision = str(decision or "")
        score = float(market_sector_score or 0)
        market_label = str(market_support_label or "")
        sector_label = str(sector_support_label or "")
        sector_symbol = str(sector_symbol or "")
        if decision == "دخول قوي" and score <= -12:
            return "مراقبة"
        if decision == "دخول قوي" and score <= -7:
            return "دخول بحذر"
        if decision == "دخول بحذر" and score <= -14:
            return "مراقبة"
        if decision == "دخول قوي" and ("ضاغط قوي" in market_label and "ضاغط" in sector_label):
            return "دخول بحذر"
        if decision == "دخول قوي" and not sector_symbol and score < 8:
            return "دخول بحذر"
        return decision
    except:
        return decision


def apply_safety_decision_guard(stock: dict, decision: str) -> tuple[str, list[str]]:
    """Final safety gate for strong entries.

    This gate should not change the technical score. It only prevents a clean
    "دخول قوي" label when execution risk is not clean enough. Gray Sharia is
    intentionally not penalized here; it is handled as a separate display bucket.
    """
    reasons: list[str] = []
    try:
        decision = str(decision or "مراقبة")
        if decision not in {"دخول قوي", "دخول بحذر"}:
            return decision, reasons

        news_scope = str(stock.get("news_scope", "neutral") or "neutral")
        news_sentiment = str(stock.get("news_sentiment", stock.get("news_category", "neutral")) or "neutral")
        try:
            news_sessions = int(float(stock.get("news_sessions_since", 999)))
        except Exception:
            news_sessions = 999
        rr_1 = float(stock.get("rr_1", 0) or 0)
        risk_pct = float(stock.get("risk_pct", 0) or 0)
        data_quality = str(stock.get("data_quality", "") or "")
        late_flag = str(stock.get("late_move_flag", "") or "")
        breakout_status = str(stock.get("breakout_status", "") or "")
        near_res = bool(stock.get("near_strong_resistance", False))
        res_dist = float(stock.get("nearest_resistance_distance_pct", 999) or 999)
        res_strength = str(stock.get("nearest_resistance_strength", "") or "")
        support_strength = str(stock.get("nearest_support_strength", "") or "")
        support_dist = float(stock.get("nearest_support_distance_pct", 999) or 999)
        target_1 = float(stock.get("target_1", 0) or 0)
        entry = float(stock.get("entry", 0) or 0)

        if news_scope == "company" and news_sentiment in {"negative", "legal"} and news_sessions <= NEGATIVE_NEWS_MAX_SESSIONS:
            reasons.append("خبر شركة تحذيري/سلبي حديث")
            if decision == "دخول قوي":
                decision = "دخول بحذر"
            elif news_sessions <= 1:
                decision = "مراقبة"

        if near_res and res_dist <= 0.45 and res_strength in {"قوي", "قوي جدًا"}:
            reasons.append(f"قريب جدًا من مقاومة {res_strength} ({safe_round(res_dist)}%)")
            if decision == "دخول قوي":
                decision = "دخول بحذر"

        if entry > 0 and target_1 > 0:
            target_room_pct = ((target_1 - entry) / entry) * 100
            if decision == "دخول قوي" and target_room_pct < 1.15:
                reasons.append(f"مساحة الهدف الأول ضيقة ({safe_round(target_room_pct)}%)")
                decision = "دخول بحذر"

        if decision == "دخول قوي" and rr_1 < 0.95:
            reasons.append(f"العائد/المخاطرة غير مريح للدخول القوي ({safe_round(rr_1)})")
            decision = "دخول بحذر"
        elif decision == "دخول بحذر" and rr_1 < 0.55:
            reasons.append(f"العائد/المخاطرة ضعيف ({safe_round(rr_1)})")
            decision = "مراقبة"

        if decision == "دخول قوي" and risk_pct > 7.5:
            reasons.append(f"المخاطرة مرتفعة نسبيًا للدخول القوي ({safe_round(risk_pct)}%)")
            decision = "دخول بحذر"

        if decision == "دخول قوي" and ("متأخر" in breakout_status or late_flag in {"LATE", "TOO_LATE"}):
            reasons.append("الدخول متأخر بعد الحركة")
            decision = "دخول بحذر"

        if decision == "دخول قوي" and data_quality == "low" and not (support_strength in {"قوي", "قوي جدًا"} and support_dist <= 1.0):
            reasons.append("جودة البيانات ضعيفة ولا يوجد دعم قوي قريب يؤكد الخطة")
            decision = "دخول بحذر"

        return decision, reasons[:5]
    except Exception as exc:
        return decision, [f"تعذر تطبيق بوابة الأمان: {type(exc).__name__}"]


def compute_core_quality_score(
    trend: str,
    effective_volume_ratio: float,
    catalyst_score: float,
    hist: dict,
    breakout_quality: str,
    pullback_score: int,
    trade_type: str,
    price_penalty: int,
    risk_pct: float,
    news_scope: str = "neutral",
    news_sentiment: str = "neutral",
    news_sessions_since: int = 999,
    market_sector_score: float = 0.0,
    historical_behavior_score: float = 50.0,
    historical_context_score: float = 50.0,
) -> int:
    quality = 50

    if trend == "صاعد قوي":
        quality += 18
    elif trend == "صاعد":
        quality += 10
    elif trend == "متذبذب":
        quality -= 5
    else:
        quality -= 18

    if effective_volume_ratio >= 1.5:
        quality += 12
    elif effective_volume_ratio >= 1.2:
        quality += 8
    elif effective_volume_ratio >= 1.0:
        quality += 4
    else:
        quality -= 6

    if catalyst_score != 0:
        quality += float(catalyst_score or 0)

    news_scope = str(news_scope or "neutral")
    news_sentiment = str(news_sentiment or "neutral")
    news_sessions_since = int(news_sessions_since or 999)
    if news_scope == "company" and news_sentiment == "legal":
        if news_sessions_since <= 2:
            quality -= 14
        elif news_sessions_since <= 5:
            quality -= 10
        else:
            quality -= 6
    elif news_scope == "company" and news_sentiment == "negative":
        if news_sessions_since <= 1:
            quality -= 7
        elif news_sessions_since <= 3:
            quality -= 4
    elif news_scope == "sector" and news_sentiment in {"negative", "legal"} and news_sessions_since <= 2:
        quality -= 3

    if bool((hist or {}).get("ath_breakout_zone", False)):
        quality -= 6
    elif bool((hist or {}).get("near_52w_high", False)):
        quality -= 2

    if breakout_quality == "FAILED":
        quality -= 25
    elif breakout_quality == "WEAK":
        quality -= 8
    elif breakout_quality == "STRONG":
        quality += 6

    if trade_type == "Pullback":
        if pullback_score >= 70:
            quality += 12
        elif pullback_score >= 58:
            quality += 6
        else:
            quality -= 6
    elif trade_type == "Breakout":
        if pullback_score >= 65:
            quality += 3

    quality += int(price_penalty or 0)

    if risk_pct > 12:
        quality -= 18
    elif risk_pct > 8:
        quality -= 10
    elif risk_pct > 5:
        quality -= 4

    quality += int(round((float(historical_behavior_score or 50) - 50.0) * 0.14))
    quality += int(round((float(historical_context_score or 50) - 50.0) * 0.10))

    # Market/sector context was previously computed but barely affected the
    # core score. Keep the impact conservative so it supports good setups
    # without overpowering price/volume/risk.
    try:
        ms_adjustment = max(-6.0, min(6.0, float(market_sector_score or 0) * 0.35))
        quality += int(round(ms_adjustment))
    except Exception:
        pass

    return max(1, min(99, int(round(quality))))


def compute_execution_layer_score(stock: dict) -> tuple[int, str, int]:
    try:
        intraday = stock.get("intraday", {}) or {}
        market_open = bool(intraday.get("market_open", False))
        if not market_open:
            return 50, "محايد", 0

        score = 50
        volume_pace_ratio = float(stock.get("volume_pace_ratio", stock.get("effective_volume_ratio", 0)) or 0)
        above_vwap = bool(intraday.get("above_vwap_proxy", False))
        session_position = float(intraday.get("session_position_pct", 0) or 0)
        continuation_score = float(stock.get("continuation_score", 0) or 0)
        runner_score = float(stock.get("runner_score", 0) or 0)
        opening_drive = str(intraday.get("opening_drive", "unknown") or "unknown")

        if volume_pace_ratio >= 1.3:
            score += 8
        elif volume_pace_ratio >= 1.0:
            score += 4
        elif volume_pace_ratio < 0.9:
            score -= 5

        if above_vwap:
            score += 4
        else:
            score -= 4

        if session_position >= 75:
            score += 3
        elif session_position < 35:
            score -= 3

        if continuation_score >= 75:
            score += 4
        elif continuation_score >= 65:
            score += 2

        if runner_score >= 80:
            score += 3
        elif runner_score >= 66:
            score += 1

        if opening_drive == "صاعد":
            score += 2
        elif opening_drive == "هابط":
            score -= 3

        score = max(1, min(99, int(round(score))))
        adjustment = int(round((score - 50) * 0.35))

        if score >= 68:
            label = "داعم"
        elif score >= 56:
            label = "إيجابي"
        elif score >= 44:
            label = "محايد"
        else:
            label = "ضعيف"

        return score, label, adjustment
    except:
        return 50, "محايد", 0


def apply_decision_layers(stock: dict) -> dict:
    try:
        trend = str(stock.get("trend", "") or "")
        trade_type = str(stock.get("type", "") or "")
        breakout_quality = str(stock.get("breakout_quality", "") or "")
        pullback_score = int(float(stock.get("pullback_score", 0) or 0))
        hist = {
            "ath_breakout_zone": bool(stock.get("ath_breakout_zone", False)),
            "near_52w_high": bool(stock.get("near_52w_high", False)),
        }
        effective_volume_ratio = float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)) or 0)
        catalyst_score = float(stock.get("catalyst_score", 0) or 0)
        price_penalty = int(float(stock.get("price_penalty_points", 0) or 0))
        risk_pct = float(stock.get("risk_pct", 0) or 0)
        rr_1 = float(stock.get("rr_1", 0) or 0)
        news_scope = str(stock.get("news_scope", "neutral") or "neutral")
        news_sentiment = str(stock.get("news_sentiment", stock.get("news_category", "neutral")) or "neutral")
        try:
            news_sessions_since = int(float(stock.get("news_sessions_since", 999)))
        except Exception:
            news_sessions_since = 999
        historical_behavior_score = float(stock.get("historical_behavior_score", 50) or 50)
        historical_context_score = float(stock.get("historical_context_score", 50) or 50)

        core_quality = compute_core_quality_score(
            trend,
            effective_volume_ratio,
            catalyst_score,
            hist,
            breakout_quality,
            pullback_score,
            trade_type,
            price_penalty,
            risk_pct,
            news_scope,
            news_sentiment,
            news_sessions_since,
            float(stock.get("market_sector_score", 0) or 0),
            historical_behavior_score,
            historical_context_score,
        )
        execution_layer_score, execution_layer_label, execution_adjustment = compute_execution_layer_score(stock)
        blended_quality = max(1, min(99, int(round(core_quality + execution_adjustment))))

        stock["quality_core_score"] = core_quality
        stock["execution_layer_score"] = execution_layer_score
        stock["execution_layer_label"] = execution_layer_label
        stock["execution_layer_adjustment"] = execution_adjustment
        stock["quality_score"] = blended_quality
        stock["rank_label"] = make_rank_label(blended_quality)

        market_open = bool((stock.get("intraday", {}) or {}).get("market_open", False))
        in_pullback_zone = bool(stock.get("in_pullback_zone", False))

        if core_quality >= 84:
            core_band = "ممتاز"
        elif core_quality >= 76:
            core_band = "قوي"
        elif core_quality >= 64:
            core_band = "جيد"
        else:
            core_band = "ضعيف"

        if not market_open:
            execution_band = "محايد"
        elif execution_layer_score >= 60:
            execution_band = "داعم قوي"
        elif execution_layer_score >= 50:
            execution_band = "داعم"
        elif execution_layer_score >= 42:
            execution_band = "محايد"
        else:
            execution_band = "ضعيف"

        stock["quality_core_band"] = core_band
        stock["execution_layer_band"] = execution_band

        positive_trend = trend in {"صاعد", "صاعد قوي"}
        breakout_ok = breakout_quality in {"STRONG", "WEAK"}
        breakout_elite = breakout_quality == "STRONG"
        pullback_ok = pullback_score >= 58
        pullback_strong = pullback_score >= 66 and (in_pullback_zone or effective_volume_ratio >= 1.0 or trend == "صاعد قوي")

        strong_ready = (
            core_quality >= 78
            and risk_pct <= 8.5
            and rr_1 >= 0.72
            and effective_volume_ratio >= 0.95
            and positive_trend
            and breakout_quality != "FAILED"
        )

        if trade_type == "Breakout":
            strong_ready = strong_ready and breakout_ok and (
                breakout_elite
                or (core_quality >= 84 and effective_volume_ratio >= 1.0)
            )
        elif trade_type == "Pullback":
            strong_ready = strong_ready and pullback_strong

        if market_open:
            strong_ready = strong_ready and (
                execution_layer_score >= 54
                or (core_quality >= 84 and execution_layer_score >= 48)
            )

        cautious_ready = (
            core_quality >= 62
            and risk_pct <= 12
            and positive_trend
            and breakout_quality != "FAILED"
        )

        if trade_type == "Breakout":
            cautious_ready = cautious_ready and breakout_ok and effective_volume_ratio >= 0.9
        elif trade_type == "Pullback":
            cautious_ready = cautious_ready and (pullback_ok or in_pullback_zone)

        if market_open:
            cautious_ready = cautious_ready and execution_layer_score >= 36

        decision = "مراقبة"
        if strong_ready:
            decision = "دخول قوي"
        elif cautious_ready:
            decision = "دخول بحذر"

        decision = apply_news_decision_guard(decision, news_scope, news_sentiment, news_sessions_since, core_quality)
        decision = apply_market_sector_decision_guard(
            decision,
            float(stock.get("market_sector_score", 0) or 0),
            str(stock.get("market_support_label", "") or ""),
            str(stock.get("sector_support_label", "") or ""),
        )

        decision, safety_gate_reasons = apply_safety_decision_guard(stock, decision)
        stock["safety_gate_reasons"] = safety_gate_reasons

        if decision == "دخول قوي" and historical_behavior_score < 42 and historical_context_score < 44 and str(stock.get("historical_confidence_label", "") or "") in {"متوسطة", "عالية"}:
            decision = "دخول بحذر"
        elif decision == "دخول بحذر" and historical_behavior_score < 36 and historical_context_score < 38 and str(stock.get("historical_confidence_label", "") or "") == "عالية":
            decision = "مراقبة"

        stock["decision"] = decision
        stock["decision_layer_note"] = f"Core: {core_band} | Execution: {execution_band}"
        stock["execution_status"] = compute_execution_status(
            trade_type, decision, trend, effective_volume_ratio, catalyst_score, breakout_quality
        )

        signal_strength_score = float(blended_quality or 0)
        signal_strength_score += max(-8.0, min(8.0, (historical_behavior_score - 50.0) * 0.18))
        signal_strength_score += max(-6.0, min(6.0, (historical_context_score - 50.0) * 0.14))
        signal_strength_score += max(-6.0, min(6.0, (float(stock.get("market_sector_score", 0) or 0)) * 0.35))
        signal_strength_score += max(-6.0, min(6.0, (execution_layer_score - 50.0) * 0.10))
        signal_strength_score = max(1.0, min(99.0, round(signal_strength_score, 2)))
        if decision == "دخول قوي":
            if signal_strength_score >= 84:
                signal_strength_label = "قوي جدًا"
                signal_strength_bucket = 3
            elif signal_strength_score >= 72:
                signal_strength_label = "قوي"
                signal_strength_bucket = 2
            else:
                signal_strength_label = "قوي مبكر"
                signal_strength_bucket = 1
        elif decision == "دخول بحذر":
            signal_strength_label = "بحذر" if signal_strength_score < 70 else "بحذر مرتفع"
            signal_strength_bucket = 0
        else:
            signal_strength_label = "مراقبة"
            signal_strength_bucket = -1
        stock["signal_strength_score"] = signal_strength_score
        stock["signal_strength_label"] = signal_strength_label
        stock["signal_strength_bucket"] = signal_strength_bucket

        stock["owner_action"] = owner_decision(decision, trend, breakout_quality, effective_volume_ratio, catalyst_score)
        return stock
    except:
        return stock


def compute_pullback_context(current_price: float, high_price: float, low_price: float, intraday: dict, trend: str) -> dict:
    try:
        session_high = float((intraday or {}).get("session_high", 0) or 0)
        session_low = float((intraday or {}).get("session_low", 0) or 0)
        session_open = float((intraday or {}).get("session_open", 0) or 0)
        above_vwap = bool((intraday or {}).get("above_vwap_proxy", False))
        spike_from_open_pct = float((intraday or {}).get("spike_from_open_pct", 0) or 0)
        pullback_volume_dry = bool((intraday or {}).get("pullback_volume_dry", False))
        recent_red_bars = int((intraday or {}).get("recent_red_bars", 0) or 0)
        session_position_pct = float((intraday or {}).get("session_position_pct", 0) or 0)

        swing_high = session_high if session_high > 0 else high_price
        base_low = session_low if session_low > 0 else low_price
        if session_open > 0 and base_low > 0:
            base_low = min(base_low, session_open)
        swing_low = base_low if base_low > 0 else low_price
        swing_range = max(swing_high - swing_low, 0.0)

        fib_38 = 0.0
        fib_50 = 0.0
        fib_62 = 0.0
        in_pullback_zone = False
        near_support = current_price <= low_price * 1.05 if low_price > 0 else False
        strong_spike = False
        pullback_score = 0
        pattern_label = ""

        if swing_high > 0 and swing_low > 0 and swing_range > 0:
            fib_38 = swing_high - (swing_range * 0.382)
            fib_50 = swing_high - (swing_range * 0.5)
            fib_62 = swing_high - (swing_range * 0.618)
            zone_low = min(fib_38, fib_62)
            zone_high = max(fib_38, fib_62)
            in_pullback_zone = zone_low <= current_price <= zone_high if current_price > 0 else False
            strong_spike = spike_from_open_pct >= 3.0 or ((swing_high - swing_low) / max(swing_low, 0.01)) >= 0.04
            if trend in {"صاعد", "صاعد قوي"}:
                if strong_spike:
                    pullback_score += 25
                if in_pullback_zone:
                    pullback_score += 24
                if pullback_volume_dry:
                    pullback_score += 18
                if 2 <= recent_red_bars <= 4:
                    pullback_score += 10
                if above_vwap:
                    pullback_score += 8
                if session_position_pct >= 45:
                    pullback_score += 8
                elif session_position_pct < 30:
                    pullback_score -= 8
                if near_support:
                    pullback_score += 10

            if strong_spike and in_pullback_zone and pullback_volume_dry:
                pattern_label = "ارتداد فيبوناتشي بعد قفزة قوية"
            elif in_pullback_zone:
                pattern_label = "ارتداد داخل منطقة فيبوناتشي"
            elif near_support:
                pattern_label = "ارتداد قرب دعم يومي"

            return {
                "fib_38": safe_round(fib_38),
                "fib_50": safe_round(fib_50),
                "fib_62": safe_round(fib_62),
                "pullback_zone_low": safe_round(zone_low),
                "pullback_zone_high": safe_round(zone_high),
                "in_pullback_zone": in_pullback_zone,
                "near_support": near_support,
                "strong_spike_detected": strong_spike,
                "pullback_score": max(0, min(99, int(round(pullback_score)))),
                "pullback_pattern_label": pattern_label,
                "pullback_volume_label": "جفاف سيولة على الارتداد ✅" if pullback_volume_dry else "سيولة الارتداد ما زالت مرتفعة ⚠️",
                "pullback_multi_bar_label": f"{recent_red_bars} شموع تراجع" if recent_red_bars > 0 else "لا يوجد تراجع متعدد واضح",
                "pullback_candidate": trend in {"صاعد", "صاعد قوي"} and (in_pullback_zone or near_support),
            }

        return {
            "fib_38": 0.0,
            "fib_50": 0.0,
            "fib_62": 0.0,
            "pullback_zone_low": 0.0,
            "pullback_zone_high": 0.0,
            "in_pullback_zone": False,
            "near_support": near_support,
            "strong_spike_detected": False,
            "pullback_score": 0,
            "pullback_pattern_label": "",
            "pullback_volume_label": "",
            "pullback_multi_bar_label": "",
            "pullback_candidate": trend in {"صاعد", "صاعد قوي"} and near_support,
        }
    except:
        return {
            "fib_38": 0.0,
            "fib_50": 0.0,
            "fib_62": 0.0,
            "pullback_zone_low": 0.0,
            "pullback_zone_high": 0.0,
            "in_pullback_zone": False,
            "near_support": False,
            "strong_spike_detected": False,
            "pullback_score": 0,
            "pullback_pattern_label": "",
            "pullback_volume_label": "",
            "pullback_multi_bar_label": "",
            "pullback_candidate": False,
        }


def compute_breakout_levels(current_price: float, high_price: float, low_price: float, intraday: dict, trade_type: str, pullback_context: dict | None = None):
    breakout_price = 0.0
    confirmation_price = 0.0
    entry_price_real = 0.0
    late_entry_price = 0.0
    breakout_status = ""
    pullback_context = pullback_context or {}

    if trade_type == "Breakout" and high_price > 0:
        breakout_price = high_price
        confirmation_price = high_price * 1.0025
        entry_price_real = high_price * 1.005
        late_entry_price = high_price * 1.015

        if current_price < breakout_price:
            breakout_status = "قبل الاختراق"
        elif breakout_price <= current_price < confirmation_price:
            breakout_status = "اختراق أولي"
        elif confirmation_price <= current_price <= entry_price_real:
            breakout_status = "تأكيد الاختراق"
        elif entry_price_real < current_price <= late_entry_price:
            breakout_status = "اختراق مؤكد - دخول بحذر"
        else:
            breakout_status = "اختراق متأخر"
    elif trade_type == "Pullback":
        fib_38 = float(pullback_context.get("fib_38", 0) or 0)
        fib_50 = float(pullback_context.get("fib_50", 0) or 0)
        fib_62 = float(pullback_context.get("fib_62", 0) or 0)
        zone_low = float(pullback_context.get("pullback_zone_low", 0) or 0)
        zone_high = float(pullback_context.get("pullback_zone_high", 0) or 0)

        breakout_price = high_price if high_price > 0 else (fib_38 if fib_38 > 0 else low_price * 1.03)
        confirmation_price = fib_38 if fib_38 > 0 else low_price * 1.02
        entry_price_real = fib_50 if fib_50 > 0 else (current_price if current_price > 0 else low_price * 1.01)
        if current_price > 0 and zone_high > 0 and current_price < zone_low:
            entry_price_real = zone_low
        late_entry_price = zone_high if zone_high > 0 else (fib_38 if fib_38 > 0 else low_price * 1.025)
        breakout_status = str(pullback_context.get("pullback_pattern_label", "ارتداد من دعم") or "ارتداد من دعم")
        if fib_62 > 0 and current_price > 0 and current_price < fib_62 * 0.995:
            breakout_status = "كسر منطقة الارتداد"

    return {
        "breakout_price": safe_round(breakout_price),
        "confirmation_price": safe_round(confirmation_price),
        "entry_price_real": safe_round(entry_price_real),
        "late_entry_price": safe_round(late_entry_price),
        "breakout_status": breakout_status,
    }


def compute_timing_layer(current_price: float, intraday: dict, effective_volume_ratio: float, levels: dict, market_phase: str):
    breakout_price = float(levels.get("breakout_price", 0) or 0)
    confirmation_price = float(levels.get("confirmation_price", 0) or 0)
    entry_price_real = float(levels.get("entry_price_real", 0) or 0)
    late_entry_price = float(levels.get("late_entry_price", 0) or 0)

    intraday_ratio = float((intraday or {}).get("intraday_volume_ratio", 0) or 0)
    vwap_proxy = float((intraday or {}).get("vwap_proxy", 0) or 0)
    above_vwap = bool((intraday or {}).get("above_vwap_proxy", False))
    opening_drive = str((intraday or {}).get("opening_drive", "unknown") or "unknown")
    market_open = bool((intraday or {}).get("market_open", False))

    strong_volume = effective_volume_ratio >= 1.1 or intraday_ratio >= 1.2
    excellent_volume = effective_volume_ratio >= 1.25 or intraday_ratio >= 1.5

    if market_phase == "open":
        if market_open and vwap_proxy > 0:
            vwap_status = "فوق VWAP ✅" if above_vwap else "تحت VWAP ❌"
        else:
            vwap_status = "VWAP غير متاح"
    else:
        vwap_status = "VWAP يكتمل أثناء السوق"

    if excellent_volume:
        volume_status = "سيولة قوية جدًا ✅"
    elif strong_volume:
        volume_status = "سيولة داعمة ✅"
    elif effective_volume_ratio >= 0.9 or intraday_ratio >= 0.95:
        volume_status = "سيولة متوسطة ⚠️"
    else:
        volume_status = "سيولة ضعيفة ❌"

    timing_signal = "مراقبة 👀"
    timing_reason = "تحت المراقبة"
    smart_entry_price = entry_price_real if entry_price_real > 0 else confirmation_price
    smart_stop_price = 0.0
    smart_target_1 = 0.0

    if confirmation_price > 0:
        if current_price < breakout_price:
            timing_signal = "انتظار اختراق ⏳"
            timing_reason = f"السعر ما زال تحت الاختراق {safe_round(breakout_price)}"
            smart_entry_price = confirmation_price
        elif breakout_price <= current_price < confirmation_price:
            timing_signal = "انتظار تأكيد 📊"
            timing_reason = f"تم الكسر الأولي ويحتاج الثبات فوق {safe_round(confirmation_price)}"
            smart_entry_price = confirmation_price
        elif confirmation_price <= current_price <= entry_price_real:
            if market_phase == "open":
                if above_vwap and strong_volume and opening_drive != "هابط":
                    timing_signal = "جاهز 🔥"
                    timing_reason = "السعر فوق التأكيد وفوق VWAP والسيولة داعمة"
                elif strong_volume:
                    timing_signal = "دخول بحذر 🟠"
                    timing_reason = "السعر فوق التأكيد لكن يحتاج ثباتًا لحظيًا أفضل"
                else:
                    timing_signal = "انتظار تأكيد 📊"
                    timing_reason = "السعر في منطقة جيدة لكن السيولة ليست كافية بعد"
            else:
                timing_signal = "انتظار تأكيد 📊"
                timing_reason = "السهم في منطقة جيدة، وقرار التنفيذ الأفضل يكون مع افتتاح السوق"
            smart_entry_price = entry_price_real
        elif entry_price_real < current_price <= late_entry_price:
            if market_phase == "open" and above_vwap and excellent_volume:
                timing_signal = "دخول بحذر 🟠"
                timing_reason = "السعر تجاوز الدخول المثالي لكن ما زال ضمن آخر دخول مناسب"
            else:
                timing_signal = "متأخر ⚠️"
                timing_reason = "السعر تجاوز الدخول المثالي وأصبح أقل جاذبية"
            smart_entry_price = late_entry_price
        elif late_entry_price > 0 and current_price > late_entry_price:
            timing_signal = "متأخر ⚠️"
            timing_reason = "السعر تجاوز آخر دخول مناسب - لا تطارد"
            smart_entry_price = late_entry_price

    if entry_price_real > 0:
        smart_stop_price = max(0.0, entry_price_real * 0.97)
        smart_target_1 = entry_price_real * 1.04

    return {
        "timing_signal": timing_signal,
        "timing_reason": timing_reason,
        "vwap_status": vwap_status,
        "volume_status": volume_status,
        "smart_entry_price": safe_round(smart_entry_price),
        "smart_stop_price": safe_round(smart_stop_price),
        "smart_target_1": safe_round(smart_target_1),
    }
