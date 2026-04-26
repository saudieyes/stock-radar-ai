import time
from app.utils import *
from app.performance_tracker import evaluate_performance_record
from app.strategy_engine import get_latest_minute_price

def get_alignment_meta(stock: dict) -> dict:
    try:
        intraday = stock.get("intraday", {}) or {}
        trend = str(stock.get("trend", "") or "")
        opening_drive = str(intraday.get("opening_drive", "unknown") or "unknown")
        intraday_ratio = float(intraday.get("intraday_volume_ratio", 0) or 0)
        above_vwap = bool(intraday.get("above_vwap_proxy", False))
        score = 50
        if trend == "صاعد قوي":
            score += 25
        elif trend == "صاعد":
            score += 18
        elif trend == "متذبذب":
            score += 4
        else:
            score -= 18
        if opening_drive == "صاعد":
            score += 12
        elif opening_drive == "متذبذب":
            score += 4
        elif opening_drive == "هابط":
            score -= 10
        if above_vwap:
            score += 8
        else:
            score -= 5
        if intraday_ratio >= 1.1:
            score += 6
        elif intraday_ratio < 0.85 and intraday_ratio > 0:
            score -= 6
        score = max(0, min(100, int(round(score))))
        if score >= 80:
            label = "متوافق جدًا"
            detail = "الاتجاه اليومي والحركة القريبة يدعمان بعضهما بشكل جيد."
        elif score >= 65:
            label = "متوافق"
            detail = "هناك توافق جيد لكنه ليس مثاليًا بالكامل."
        elif score >= 50:
            label = "متوسط"
            detail = "يوجد بعض التوافق لكن ما زال يحتاج حذرًا."
        else:
            label = "ضعيف"
            detail = "الحركة الحالية لا تتوافق جيدًا مع الاتجاه الأكبر."
        return {
            "alignment_score": score,
            "alignment_label": label,
            "alignment_detail": detail,
        }
    except:
        return {
            "alignment_score": 0,
            "alignment_label": "لا توجد بيانات كافية",
            "alignment_detail": "لا توجد بيانات كافية للتوافق الزمني.",
        }

def get_risk_profile_meta(stock: dict) -> dict:
    try:
        risk_pct = float(stock.get("display_risk_pct", stock.get("risk_pct", 0)) or 0)
        quality = float(stock.get("quality_score", 0) or 0)
        if risk_pct < 4 and quality >= 75:
            label = "منخفضة"
            detail = "الصفقة منخفضة المخاطرة نسبيًا مقارنة بجودتها."
            min_risk = 1.0
        elif (4 <= risk_pct <= 7) or (65 <= quality < 75):
            label = "متوسطة"
            detail = "الصفقة متوسطة المخاطرة وتحتاج التزامًا بالخطة."
            min_risk = 1.5
        else:
            label = "مرتفعة"
            detail = "الصفقة أعلى مخاطرة من المثالي، ولا تناسب إلا جزءًا صغيرًا من رأس المال."
            min_risk = 2.0
        fit_note = f"⚠️ هذه الصفقة غير مناسبة لك إذا كانت مخاطرتك أقل من {safe_round(min_risk, 1)}%." if min_risk >= 2 else f"✅ مناسبة لمخاطرة تقريبية بين {safe_round(min_risk, 1)}% و 2.0%."
        return {
            "risk_profile_label": label,
            "risk_profile_detail": detail,
            "risk_profile_fit_note": fit_note,
            "risk_profile_min_pct": min_risk,
        }
    except:
        return {
            "risk_profile_label": "لا توجد بيانات كافية",
            "risk_profile_detail": "لا توجد بيانات كافية لتوصيف المخاطرة.",
            "risk_profile_fit_note": "",
            "risk_profile_min_pct": 0.0,
        }

def get_price_freshness_meta(stock: dict) -> dict:
    try:
        phase = str(stock.get("market_phase", "") or "")
        reliable = bool(stock.get("price_reliable_for_execution", False))
        source = str(stock.get("price_source", "") or "")
        last_ms = int(float(stock.get("last_price_update_ms", 0) or 0))
        age_seconds = 0
        if last_ms > 0:
            age_seconds = max(0, int((time.time() * 1000 - last_ms) / 1000))

        if source == "previous_close" or phase == "closed":
            return {
                "price_freshness_label": "آخر إغلاق",
                "price_freshness_icon": "🌙",
                "price_freshness_score": 55,
                "price_freshness_detail": "السعر الحالي يمثل آخر إغلاق، وليس سعرًا مباشرًا أثناء التداول.",
            }
        if source == "pre_market":
            return {
                "price_freshness_label": "قبل الافتتاح",
                "price_freshness_icon": "🌅",
                "price_freshness_score": 78 if age_seconds <= 600 else 58,
                "price_freshness_detail": f"السعر من تداولات ما قبل الافتتاح. آخر تحديث تقريبي منذ {age_seconds} ثانية." if age_seconds > 0 else "السعر من تداولات ما قبل الافتتاح.",
            }
        if source == "after_hours":
            return {
                "price_freshness_label": "بعد الإغلاق",
                "price_freshness_icon": "🌙",
                "price_freshness_score": 78 if age_seconds <= 600 else 58,
                "price_freshness_detail": f"السعر من تداولات ما بعد الإغلاق. آخر تحديث تقريبي منذ {age_seconds} ثانية." if age_seconds > 0 else "السعر من تداولات ما بعد الإغلاق.",
            }
        if reliable:
            return {
                "price_freshness_label": "مباشر / حديث" if age_seconds <= 300 else "مباشر / متأخر قليلًا",
                "price_freshness_icon": "🟢" if age_seconds <= 300 else "🟡",
                "price_freshness_score": 96 if age_seconds <= 90 else 88 if age_seconds <= 300 else 70,
                "price_freshness_detail": f"السعر مباشر أثناء التداول. آخر تحديث تقريبي منذ {age_seconds} ثانية." if age_seconds > 0 else "السعر مباشر أثناء التداول.",
            }
        return {
            "price_freshness_label": "لا توجد بيانات كافية",
            "price_freshness_icon": "❓",
            "price_freshness_score": 0,
            "price_freshness_detail": "لا توجد بيانات كافية لتحديد حداثة السعر بثقة.",
        }
    except:
        return {
            "price_freshness_label": "لا توجد بيانات كافية",
            "price_freshness_icon": "❓",
            "price_freshness_score": 0,
            "price_freshness_detail": "لا توجد بيانات كافية لتحديد حداثة السعر.",
        }

def get_execution_readiness_meta(stock: dict) -> dict:
    try:
        decision = str(stock.get("decision", "") or "")
        trade_type = str(stock.get("type", "") or "")
        mode = str(stock.get("execution_mode", "") or "")
        reliable = bool(stock.get("price_reliable_for_execution", False))
        market_phase = str(stock.get("market_phase", "") or "")
        current_price = float(stock.get("display_price", stock.get("current_price_live", 0)) or 0)
        breakout_price = float(stock.get("breakout_price", 0) or 0)
        confirmation_price = float(stock.get("confirmation_price", 0) or 0)
        entry_price = float(stock.get("display_entry_price", stock.get("entry_price_real", stock.get("entry", 0))) or 0)
        stop_price = float(stock.get("display_stop_price", stock.get("stop_loss", 0)) or 0)
        zone_low = float(stock.get("pullback_zone_low", 0) or 0)
        zone_high = float(stock.get("pullback_zone_high", 0) or 0)
        quality = float(stock.get("quality_score", 0) or 0)
        rr = float(stock.get("rr_1", 0) or 0)
        volume_ratio = float(stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)) or 0)
        risk_pct = float(stock.get("display_risk_pct", stock.get("risk_pct", 0)) or 0)

        label = "مراقبة"
        icon = "👀"
        score = 28
        detail = "راقب السهم حتى تتضح إشارة التنفيذ."

        if not reliable and market_phase in {"open", "pre_market", "after_hours"}:
            label = "لا توجد بيانات كافية"
            icon = "❓"
            score = 12
            detail = "السعر اللحظي غير موثوق الآن، لذلك لا يُفضَّل اتخاذ قرار تنفيذ مباشر."
        elif trade_type == "Breakout":
            if current_price > 0 and stop_price > 0 and current_price <= stop_price:
                label = "خطة مكسورة"
                icon = "❌"
                score = 8
                detail = f"السعر كسر وقف الخطة السابق عند {safe_round(stop_price)}. انتظر إعادة تكوين فرصة جديدة."
            elif breakout_price > 0 and current_price < breakout_price:
                label = "انتظار اختراق"
                icon = "⏳"
                score = 55
                detail = f"انتظر اختراق {safe_round(breakout_price)} ثم تأكيد فوق {safe_round(confirmation_price or breakout_price)}."
            elif confirmation_price > 0 and current_price < confirmation_price:
                label = "اختراق أولي"
                icon = "📊"
                score = 62
                detail = f"تم الاختراق مبدئيًا، لكن الأفضل انتظار الثبات فوق {safe_round(confirmation_price)}."
            elif entry_price > 0 and current_price <= entry_price * 1.01:
                label = "دخول فوري"
                icon = "🔥"
                score = 90
                detail = f"السعر قريب من منطقة الدخول الحالية حول {safe_round(entry_price)} مع وقف عند {safe_round(stop_price)}."
            else:
                label = "مطاردة سعرية"
                icon = "⚠️"
                score = 40
                detail = f"السعر ابتعد عن منطقة الدخول ({safe_round(entry_price)}). الأفضل انتظار إعادة تمركز أو إعادة دخول."
        elif trade_type == "Pullback":
            if current_price > 0 and stop_price > 0 and current_price <= stop_price:
                label = "خطة ارتداد مكسورة"
                icon = "❌"
                score = 10
                detail = f"السعر كسر وقف الارتداد عند {safe_round(stop_price)}. انتظر ارتدادًا جديدًا من دعم أحدث."
            elif zone_low > 0 and zone_high > 0:
                if decision == "دخول قوي":
                    label = "دخول قوي"
                    icon = "🔥"
                    score = 82
                    detail = f"الخطة قوية كارتداد من الدعم. راقب الارتداد قرب منطقة {safe_round(zone_low)} - {safe_round(zone_high)} ثم تأكيد فوق {safe_round(entry_price or zone_high)}."
                elif decision == "دخول بحذر":
                    label = "دخول بحذر"
                    icon = "🟠"
                    score = 70
                    detail = f"الخطة بحذر كارتداد من الدعم. راقب الارتداد قرب منطقة {safe_round(zone_low)} - {safe_round(zone_high)} ثم تأكيد فوق {safe_round(entry_price or zone_high)}."
                else:
                    label = "ارتداد من الدعم"
                    icon = "↩️"
                    score = 72
                    detail = f"راقب الارتداد قرب منطقة {safe_round(zone_low)} - {safe_round(zone_high)} ثم تأكيد فوق {safe_round(entry_price or zone_high)}."
            elif entry_price > 0:
                label = "ارتداد قيد التكوين"
                icon = "🟠"
                score = 58
                detail = f"الخطة تميل لارتداد من الدعم. راقب التأكيد فوق {safe_round(entry_price)}."
        elif "إعادة دخول" in mode:
            label = "إعادة دخول"
            icon = "🔁"
            score = 48
            detail = "فات الدخول الأول. راقب إعادة دخول أفضل بدل مطاردة الحركة."
        elif decision in {"دخول قوي", "دخول بحذر"}:
            label = "دخول فوري"
            icon = "🔥" if decision == "دخول قوي" else "🟠"
            score = 82 if decision == "دخول قوي" else 70
            detail = f"الخطة صالحة حاليًا للدخول مع إدارة واضحة للمخاطر. نقطة الدخول الحالية قرب {safe_round(entry_price)}."

        if quality >= 85:
            score += 3
        elif quality < 60:
            score -= 8
        if rr >= 1.8:
            score += 4
        elif rr < 1.2:
            score -= 5
        if volume_ratio >= 1.2:
            score += 3
        elif volume_ratio < 0.9:
            score -= 4
        if risk_pct > 8:
            score -= 5
        score = int(max(0, min(score, 99)))
        return {
            "execution_readiness_score": score,
            "execution_readiness_label": label,
            "execution_readiness_icon": icon,
            "execution_readiness_detail": f"{detail} الدرجة رقم داخلي من 0 إلى 99: كلما ارتفعت كانت الخطة أقرب للتنفيذ الآن.",
        }
    except:
        return {
            "execution_readiness_score": 0,
            "execution_readiness_label": "لا توجد بيانات كافية",
            "execution_readiness_icon": "❓",
            "execution_readiness_detail": "لا توجد بيانات كافية لتحديد جاهزية التنفيذ.",
        }

def explain_metric_ar(name: str, value, stock: dict) -> dict:
    try:
        if name == "quality":
            v = float(value or 0)
            if v >= 85:
                return {"icon": "🏅", "label": "ممتازة", "detail": "الجودة مرتفعة جدًا وتدعم القرار."}
            if v >= 65:
                return {"icon": "✅", "label": "جيدة", "detail": "الجودة جيدة لكن ليست مثالية."}
            if v >= 50:
                return {"icon": "🟰", "label": "متوسطة", "detail": "الجودة متوسطة وتحتاج انتقاء أفضل."}
            return {"icon": "❌", "label": "ضعيفة", "detail": "الجودة منخفضة ولا تعطي ثقة كافية."}
        if name == "risk_pct":
            v = float(value or 0)
            if v <= 4:
                return {"icon": "🟢", "label": "منخفضة", "detail": "المخاطرة منخفضة نسبيًا."}
            if v <= 8:
                return {"icon": "🟡", "label": "متوسطة", "detail": "المخاطرة مقبولة مع إدارة جيدة."}
            if v <= 12:
                return {"icon": "🟠", "label": "مرتفعة نسبيًا", "detail": "المخاطرة أعلى من المثالي وتحتاج حذرًا."}
            return {"icon": "🔴", "label": "مرتفعة", "detail": "المخاطرة مرتفعة ولا تناسب معظم التداولات."}
        if name in {"volume", "volume_daily", "volume_pace"}:
            v = float(value or 0)
            if v >= 1.5:
                return {"icon": "🔥", "label": "قوية جدًا", "detail": "السيولة أعلى بكثير من المعتاد."}
            if v >= 1.2:
                return {"icon": "✅", "label": "إيجابية", "detail": "السيولة داعمة للحركة."}
            if v >= 0.95:
                return {"icon": "🟰", "label": "مقبولة", "detail": "السيولة مقبولة لكنها ليست انفجارية."}
            return {"icon": "❌", "label": "ضعيفة", "detail": "السيولة أقل من المطلوب غالبًا."}
        if name == "rr":
            v = float(value or 0)
            if v >= 2.0:
                return {"icon": "🏅", "label": "ممتاز", "detail": "العائد المتوقع جيد جدًا مقارنة بالخطر."}
            if v >= 1.5:
                return {"icon": "✅", "label": "جيد", "detail": "العائد إلى المخاطرة مناسب."}
            if v >= 1.2:
                return {"icon": "🟡", "label": "متوسط", "detail": "العائد مقبول لكنه ليس مريحًا جدًا."}
            return {"icon": "❌", "label": "ضعيف", "detail": "العائد لا يبرر الخطر غالبًا."}
        if name == "trend":
            t = str(value or "")
            mapping = {
                "صاعد قوي": {"icon": "⬆️", "label": "إيجابي جدًا", "detail": "السهم أعلى من المتوسطات الرئيسية."},
                "صاعد": {"icon": "🟢", "label": "إيجابي", "detail": "الاتجاه العام جيد."},
                "متذبذب": {"icon": "🟰", "label": "محايد", "detail": "السهم غير واضح الاتجاه."},
                "هابط": {"icon": "⬇️", "label": "سلبي", "detail": "الاتجاه الهابط يضعف الثقة."},
            }
            return mapping.get(t, {"icon": "❓", "label": "غير واضح", "detail": "لا توجد بيانات كافية لاتجاه واضح."})
        if name == "continuation":
            label = str(value or "")
            if "بقوة" in label or "مرجح" in label or "مرشح استمرار اليوم" in label:
                return {"icon": "🔥", "label": "إيجابي", "detail": "احتمال استمرار الحركة جيد."}
            if "محتمل" in label:
                return {"icon": "🟠", "label": "بحذر", "detail": "الاستمرار ممكن لكن ليس مضمونًا."}
            if label:
                return {"icon": "⚠️", "label": "غير مؤكد", "detail": "الاستمرار غير واضح حتى الآن."}
            return {"icon": "❓", "label": "لا توجد بيانات كافية", "detail": "لا توجد بيانات كافية لتقييم الاستمرار."}
        if name in {"runner_score", "continuation_score"}:
            v = float(value or 0)
            if v >= 80:
                return {"icon": "🔥", "label": "قوي جدًا", "detail": "الدرجة مرتفعة جدًا وتدعم الاستمرار."}
            if v >= 66:
                return {"icon": "✅", "label": "إيجابي", "detail": "الدرجة داعمة للاستمرار."}
            if v >= 50:
                return {"icon": "🟡", "label": "متوسطة", "detail": "الدرجة متوسطة وليست حاسمة."}
            return {"icon": "❌", "label": "ضعيف", "detail": "الدرجة ضعيفة ولا تعطي أفضلية كافية."}
        if name == "news":
            category = str(stock.get("news_category", "neutral") or "neutral")
            scope = str(stock.get("news_scope", "neutral") or "neutral")
            scope_label_ar = str(stock.get("news_scope_label", news_scope_label(scope)) or news_scope_label(scope))
            freshness = str(stock.get("news_freshness_label", "") or "")
            context_note = str(stock.get("news_context_note", "") or "")
            if scope == "company":
                if category == "positive":
                    return {"icon": "🟢", "label": f"شركة إيجابي {freshness}".strip(), "detail": context_note or "خبر مباشر يخص الشركة ويدعم الفكرة."}
                if category == "legal":
                    return {"icon": "⛔", "label": f"شركة قانوني سلبي {freshness}".strip(), "detail": context_note or "خبر قانوني مباشر على الشركة."}
                if category == "negative":
                    return {"icon": "🔴", "label": f"شركة سلبي {freshness}".strip(), "detail": context_note or "خبر سلبي مباشر على الشركة."}
                return {"icon": "⚪", "label": f"شركة محايد {freshness}".strip(), "detail": context_note or "خبر يخص الشركة لكنه غير محفز."}
            if scope == "sector":
                if category == "positive":
                    return {"icon": "🏭", "label": f"قطاعي داعم {freshness}".strip(), "detail": context_note or "خبر قطاعي داعم بوزن أخف من خبر الشركة."}
                if category in {"negative", "legal"}:
                    return {"icon": "🏭", "label": f"قطاعي ضاغط {freshness}".strip(), "detail": context_note or "خبر قطاعي ضاغط."}
                return {"icon": "🏭", "label": f"سياق قطاعي {freshness}".strip(), "detail": context_note or "سياق قطاعي غير مباشر."}
            if scope == "market":
                return {"icon": "📰", "label": f"سوق عام {freshness}".strip(), "detail": context_note or "سياق سوق عام وليس محفزًا مباشرًا للسهم."}
            if scope == "opinion" or category == "opinion":
                return {"icon": "🚫", "label": "رأي غير معتمد", "detail": context_note or "هذا رأي أو مقال عام وليس محفز تداول معتمد."}
            return {"icon": "⚪", "label": scope_label_ar if scope_label_ar else (freshness or "محايد"), "detail": context_note or "لا يوجد خبر محفز حديث."}
        if name == "index_context":
            score = float(stock.get("market_support_score", 0) or 0)
            label = str(stock.get("market_support_label", "محايد") or "محايد")
            detail = str(stock.get("market_sector_alignment_detail", "") or "")
            icon = "📈" if score > 0 else "📉" if score < 0 else "🟰"
            return {"icon": icon, "label": label, "detail": detail or "المؤشر المرجعي يوضح هل السوق العام داعم أو ضاغط على الفكرة."}
        if name == "sector_context":
            score = float(stock.get("sector_support_score", 0) or 0)
            label = str(stock.get("sector_support_label", "محايد") or "محايد")
            detail = str(stock.get("market_sector_alignment_detail", "") or "")
            if not str(stock.get("sector_etf_symbol", "") or ""):
                return {"icon": "🏭", "label": label, "detail": detail or "لم نجد ETF قطاع واضحًا لهذه الشركة، لذلك تقل الثقة قليلًا في قراءة القطاع."}
            return {"icon": "🏭", "label": label, "detail": detail or "القطاع يوضح هل البيئة القطاعية تساعد السهم أو تضغط عليه."}
    except:
        pass
    return {"icon": "❓", "label": "لا توجد بيانات كافية", "detail": "لا توجد بيانات كافية."}

def enrich_display_meta(stock: dict) -> dict:
    try:
        stock.update(get_price_freshness_meta(stock))
        stock.update(get_execution_readiness_meta(stock))
        stock.update(get_alignment_meta(stock))
        stock.update(get_risk_profile_meta(stock))
        stock["metric_quality"] = explain_metric_ar("quality", stock.get("quality_score"), stock)
        stock["metric_risk_pct"] = explain_metric_ar("risk_pct", stock.get("display_risk_pct", stock.get("risk_pct", 0)), stock)
        stock["metric_volume_daily"] = explain_metric_ar("volume_daily", stock.get("volume_ratio", 0), stock)
        stock["metric_volume_pace"] = explain_metric_ar("volume_pace", stock.get("volume_pace_ratio", 0), stock)
        stock["metric_volume"] = explain_metric_ar("volume", stock.get("effective_volume_ratio", stock.get("volume_ratio", 0)), stock)
        stock["metric_rr"] = explain_metric_ar("rr", stock.get("rr_1"), stock)
        stock["metric_trend"] = explain_metric_ar("trend", stock.get("trend"), stock)
        stock["metric_continuation"] = explain_metric_ar("continuation", stock.get("continuation_label", stock.get("runner_label", "")), stock)
        stock["metric_runner_score"] = explain_metric_ar("runner_score", stock.get("runner_score", 0), stock)
        stock["metric_continuation_score"] = explain_metric_ar("continuation_score", stock.get("continuation_score", 0), stock)
        stock["metric_news"] = explain_metric_ar("news", stock.get("news_badge"), stock)
        stock["metric_market_context"] = explain_metric_ar("index_context", stock.get("market_support_score", 0), stock)
        stock["metric_sector_context"] = explain_metric_ar("sector_context", stock.get("sector_support_score", 0), stock)
        stock["trade_type_label_ar"] = (
            "اختراق مقاومة" if str(stock.get("type", "")) == "Breakout"
            else "ارتداد من دعم" if str(stock.get("type", "")) == "Pullback"
            else "خطة متابعة"
        )
        summary_bits = [
            f"{stock['metric_quality'].get('icon')} الجودة: {stock['metric_quality'].get('label')}",
            f"{stock['metric_volume'].get('icon')} السيولة: {stock['metric_volume'].get('label')}",
            f"{stock['metric_trend'].get('icon')} الاتجاه: {stock['metric_trend'].get('label')}",
            f"{stock['metric_rr'].get('icon')} العائد/المخاطرة: {stock['metric_rr'].get('label')}",
            f"{stock.get('execution_readiness_icon', '👀')} الجاهزية: {stock.get('execution_readiness_label', '')}",
        ]
        if stock.get("historical_behavior_label"):
            summary_bits.append(f"📚 السلوك التاريخي: {stock.get('historical_behavior_label')} ({safe_round(stock.get('historical_behavior_score', 50), 0)})")
        if stock.get("historical_context_label"):
            summary_bits.append(f"🧩 التاريخ مع المؤشر/القطاع: {stock.get('historical_context_label')} ({safe_round(stock.get('historical_context_score', 50), 0)})")
        if stock.get("alignment_label"):
            summary_bits.append(f"🧭 التوافق الزمني: {stock.get('alignment_label')}")
        if stock.get("market_support_label"):
            summary_bits.append(f"📈 المؤشر: {stock.get('market_support_label')}")
        if stock.get("sector_etf_symbol"):
            summary_bits.append(f"🏭 القطاع: {stock.get('sector_support_label')}")
        else:
            summary_bits.append("🏭 القطاع: غير متوفر (ثقة أقل)")
        stock["quick_explainer"] = " | ".join([x for x in summary_bits if x])
        return stock
    except:
        return stock

def is_opening_window() -> bool:
    try:
        ny = ZoneInfo("America/New_York")
        now_ny = datetime.now(ny)
        if now_ny.weekday() >= 5:
            return False
        minutes = now_ny.hour * 60 + now_ny.minute
        return (4 * 60) <= minutes <= (10 * 60 + 15)
    except:
        return False

def build_opening_focus(results: list[dict]) -> list[dict]:
    portfolio_symbols = {str(x.get("symbol", "")).upper().strip() for x in load_portfolio_items()}
    watch_symbols = {str(x.get("symbol", "")).upper().strip() for x in load_manual_watchlist()}
    def opening_rank(item: dict):
        symbol = str(item.get("symbol", "")).upper().strip()
        priority = 0
        if symbol in portfolio_symbols:
            priority += 40
        if symbol in watch_symbols:
            priority += 20
        priority += int(float(item.get("execution_readiness_score", 0) or 0))
        priority += int(float(item.get("quality_score", 0) or 0) * 0.25)
        if item.get("decision") == "دخول قوي":
            priority += 20
        elif item.get("decision") == "دخول بحذر":
            priority += 10
        return priority
    return sorted(results, key=opening_rank, reverse=True)[:8]

def evaluate_portfolio_action(holding: dict, plan: dict) -> dict:
    current_price = float(plan.get("display_price", plan.get("current_price_live", 0)) or 0)
    buy_price = float(holding.get("buy_price", 0) or 0)
    target_1 = float(plan.get("display_target_price", plan.get("target_1", 0)) or 0)
    stop_loss = float(plan.get("display_stop_price", plan.get("stop_loss", 0)) or 0)
    decision = str(plan.get("decision", "") or "")
    trend = str(plan.get("trend", "") or "")
    readiness_score = int(float(plan.get("execution_readiness_score", 0) or 0))
    pnl_pct = ((current_price - buy_price) / buy_price) * 100 if current_price > 0 and buy_price > 0 else 0.0

    recommendation = "احتفاظ"
    note = "احتفظ بالسهم مع متابعة التحديثات."
    if stop_loss > 0 and current_price > 0 and current_price <= stop_loss:
        recommendation = "بيع"
        note = "السعر عند الوقف أو تحته، والأفضل الخروج والانضباط."
    elif target_1 > 0 and current_price >= target_1:
        recommendation = "تقليل"
        note = "السهم وصل للهدف الأول، وجني جزء من الربح منطقي."
    elif decision == "دخول قوي" and readiness_score >= 80 and trend in {"صاعد", "صاعد قوي"} and pnl_pct > -6:
        recommendation = "زيادة الكمية"
        note = "الإشارة الحالية تدعم زيادة جزئية مدروسة إذا كانت إدارة المخاطر مناسبة."
    elif decision == "دخول بحذر" and trend in {"صاعد", "صاعد قوي"}:
        recommendation = "احتفاظ"
        note = "الوضع جيد لكن الأفضل عدم التوسع بقوة."
    elif decision == "مراقبة" and trend == "هابط":
        recommendation = "تقليل"
        note = "القوة الحالية لا تدعم الاحتفاظ الكامل بنفس الثقة."
    elif pnl_pct <= -8:
        recommendation = "تقليل"
        note = "الخسارة اتسعت نسبيًا، ويستحق المركز تخفيفًا أو مراجعة وقفك."

    return {
        "recommendation": recommendation,
        "recommendation_note": note,
        "holding_change_pct": safe_round(pnl_pct),
        "target_price": safe_round(target_1),
        "stop_loss": safe_round(stop_loss),
    }

def summarize_outcomes(records: list[dict]) -> dict:
    rows = list(records or [])
    total = len(rows)
    out = {
        "count": total,
        "target_hit": 0,
        "above_target": 0,
        "partial_gain": 0,
        "ongoing": 0,
        "loss": 0,
        "expired": 0,
    }
    for row in rows:
        key = str(row.get("outcome", "ongoing") or "ongoing")
        if key in out:
            out[key] += 1
    if total > 0:
        out.update({k + "_pct": safe_round((v / total) * 100, 2) for k, v in out.items() if k != "count"})
    else:
        out.update({k + "_pct": 0.0 for k in ["target_hit", "above_target", "partial_gain", "ongoing", "loss", "expired"]})
    return out

def simulate_equal_weight(records: list[dict], per_trade: float = 1000.0) -> dict:
    rows = list(records or [])
    starting_capital = per_trade * len(rows)
    pnl = 0.0
    for row in rows:
        entry = float(row.get("entry_price", 0) or 0)
        target = float(row.get("target_price", 0) or 0)
        target2 = float(row.get("target_2_price", 0) or 0)
        stop = float(row.get("stop_loss", 0) or 0)
        current = float(row.get("current_price", 0) or 0)
        max_seen = float(row.get("max_price_seen", current) or current)
        if entry <= 0:
            continue
        outcome = str(row.get("outcome", "ongoing") or "ongoing")
        if outcome == "above_target" and target2 > 0:
            ret = (target2 - entry) / entry
        elif outcome == "target_hit" and target > 0:
            ret = (target - entry) / entry
        elif outcome == "loss" and stop > 0:
            ret = (stop - entry) / entry
        elif outcome == "partial_gain":
            ref = max(current, max_seen)
            ret = (ref - entry) / entry
        elif outcome == "ongoing":
            ref = current if current > 0 else max_seen
            ret = (ref - entry) / entry if ref > 0 else 0.0
        else:
            ret = 0.0
        pnl += per_trade * ret
    final_capital = starting_capital + pnl
    roi_pct = safe_round((pnl / starting_capital) * 100, 2) if starting_capital > 0 else 0.0
    return {
        "per_trade": per_trade,
        "starting_capital": safe_round(starting_capital),
        "pnl": safe_round(pnl),
        "final_capital": safe_round(final_capital),
        "roi_pct": roi_pct,
    }

def get_performance_live_price(symbol: str) -> dict:
    symbol = str(symbol or "").upper().strip()
    if not symbol:
        return {"current_price": 0.0, "price_source_label": ""}
    cache_key = f"perf::{symbol}"
    cached = _cache_get(PERFORMANCE_REFRESH_CACHE, cache_key)
    if cached is not None:
        return cached
    prev = get_prev(symbol)
    intraday = get_intraday_snapshot(symbol)
    live_block = build_live_price_block(symbol, prev or {}, intraday)
    value = {
        "current_price": float(live_block.get("display_price", 0) or 0),
        "price_source_label": live_block.get("price_source_label", ""),
    }
    ttl = 12 if is_market_open_now() else 90
    return _cache_set(PERFORMANCE_REFRESH_CACHE, cache_key, value, ttl)

def build_performance_dashboard(records: list[dict]) -> dict:
    rows = sorted(list(records or []), key=lambda r: (outcome_sort_rank(r.get("outcome")), r.get("first_seen_at", "")))
    strong = [x for x in rows if str(x.get("signal_type", "") or "") == "دخول قوي"]
    cautious = [x for x in rows if str(x.get("signal_type", "") or "") == "دخول بحذر"]
    return {
        "rows": rows,
        "summary": summarize_outcomes(rows),
        "groups": {
            "strong": {"items": strong, "summary": summarize_outcomes(strong), "simulation": simulate_equal_weight(strong)},
            "cautious": {"items": cautious, "summary": summarize_outcomes(cautious), "simulation": simulate_equal_weight(cautious)},
        },
        "simulation": simulate_equal_weight(rows),
    }
