import time
from app.utils import *
from app.utils import _cache_get, _cache_set
from app.settings import PERFORMANCE_REFRESH_CACHE, NEWS_SCORE_ENABLED
from app.performance_tracker import evaluate_performance_record, outcome_sort_rank
from app.strategy_engine import get_latest_minute_price
from app.market_data import get_prev, get_intraday_snapshot, build_live_price_block
from app.portfolio_store import load_portfolio_items
from app.watchlist_store import load_manual_watchlist

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
        display_price = float(stock.get("display_price", stock.get("current_price_live", 0)) or 0)
        source_label = str(stock.get("price_source_label", "") or "")
        if display_price > 0 and phase in {"open", "pre_market", "after_hours"}:
            return {
                "price_freshness_label": "سعر غير كافٍ للتنفيذ",
                "price_freshness_icon": "🟡",
                "price_freshness_score": 38,
                "price_freshness_detail": f"السعر المعروض متاح من {source_label or source or 'مصدر غير لحظي'}، لكنه غير موثوق كفاية لتنفيذ فوري. يصلح للمتابعة العامة حتى يصل تحديث لحظي أو تأكيد من المنصة.",
            }
        return {
            "price_freshness_label": "لا توجد بيانات سعر لحظية",
            "price_freshness_icon": "❓",
            "price_freshness_score": 15,
            "price_freshness_detail": "لم تصل بيانات سعر لحظية كافية. التحليل العام قد يبقى متاحًا، لكن التنفيذ يحتاج تأكيدًا من السعر الحي أو منصة التداول.",
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
            label = "تنفيذ غير مؤكد"
            icon = "🟡"
            score = 22
            detail = "السعر المعروض غير كافٍ لتأكيد تنفيذ مباشر الآن. راقب السهم أو قارنه بمنصة التداول حتى تصل بيانات لحظية أو يتأكد الاختراق/الارتداد."
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
            "execution_readiness_label": "تنفيذ غير مؤكد",
            "execution_readiness_icon": "🟡",
            "execution_readiness_detail": "تعذر حساب جاهزية التنفيذ بدقة. لا تعتمد على هذه الخانة للتنفيذ حتى تتضح بيانات السعر والخطة.",
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
                if category == "mixed":
                    return {"icon": "🟡", "label": f"شركة مختلط {freshness}".strip(), "detail": context_note or "خبر يخص الشركة لكنه مختلط ولا يُحسب كمحفز إيجابي."}
                return {"icon": "⚪", "label": f"شركة محايد {freshness}".strip(), "detail": context_note or "خبر يخص الشركة لكنه غير محفز."}
            if scope == "sector":
                if category == "positive":
                    return {"icon": "🏭", "label": f"سياق قطاعي فقط {freshness}".strip(), "detail": context_note or "سياق قطاعي داعم فقط، لا يضيف نقاط Catalyst ولا يُعامل كخبر شركة مباشر."}
                if category in {"negative", "legal"}:
                    return {"icon": "🏭", "label": f"سياق قطاعي ضاغط {freshness}".strip(), "detail": context_note or "سياق قطاعي ضاغط فقط، وليس خبر شركة مباشر."}
                if category == "mixed":
                    return {"icon": "🏭", "label": f"سياق قطاعي مختلط {freshness}".strip(), "detail": context_note or "سياق قطاعي مختلط لا يُحسب كمحفز مباشر."}
                return {"icon": "🏭", "label": f"سياق قطاعي فقط {freshness}".strip(), "detail": context_note or "سياق قطاعي غير مباشر ولا يضيف نقاطًا."}
            if scope == "market":
                return {"icon": "📰", "label": f"سياق سوق فقط {freshness}".strip(), "detail": context_note or "سياق سوق عام وليس محفزًا مباشرًا للسهم."}
            if scope == "opinion" or category == "opinion":
                return {"icon": "🚫", "label": "رأي غير معتمد", "detail": context_note or "هذا رأي أو مقال عام وليس محفز تداول معتمد ولا يضيف نقاطًا."}
            if scope == "unrelated":
                return {"icon": "⚪", "label": "غير ذي صلة", "detail": context_note or "الخبر لا يخص الشركة مباشرة ولا يُستخدم كمحفز."}
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
        if stock.get("market_sector_live_label"):
            summary_bits.append(f"⏱️ السوق/القطاع الآن: {stock.get('market_sector_live_label')}")
        if stock.get("support_broken_flag") and stock.get("broken_support_level"):
            summary_bits.append(f"⚠️ دعم مكسور: {safe_round(stock.get('broken_support_level'))} يحتاج استعادة")
        elif stock.get("support_reclaimed_flag") and stock.get("reclaimed_support_level"):
            summary_bits.append(f"🟢 دعم مستعاد: {safe_round(stock.get('reclaimed_support_level'))} راقب الثبات")
        elif stock.get("nearest_support"):
            summary_bits.append(f"🟢 دعم {stock.get('nearest_support_strength', '')}: {safe_round(stock.get('nearest_support'))}")
        if stock.get("resistance_reclaimed_flag") and stock.get("reclaimed_resistance_level"):
            summary_bits.append(f"🟢 مقاومة مخترقة: {safe_round(stock.get('reclaimed_resistance_level'))} تحولت لمستوى ثبات")
            if stock.get("next_resistance_above"):
                summary_bits.append(f"🔴 المقاومة التالية: {safe_round(stock.get('next_resistance_above'))}")
        elif stock.get("nearest_resistance"):
            summary_bits.append(f"🔴 مقاومة {stock.get('nearest_resistance_strength', '')}: {safe_round(stock.get('nearest_resistance'))}")
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

# --- Runtime refactor compatibility helpers ---
# These helpers originally lived in main.py and are used by /trade-scan and scan_all
# to keep the same display ordering after the refactor.
def _display_text_label(value) -> str:
    return str(value or "").strip()


def historical_confidence_bonus(label: str) -> float:
    label = _display_text_label(label)
    if "عالية" in label:
        return 6.0
    if "متوسطة" in label:
        return 3.0
    return 0.0


def historical_behavior_bonus(label: str) -> float:
    label = _display_text_label(label)
    if "يدعم" in label:
        return 4.0
    if "محايد" in label:
        return 0.0
    if "ضعيف" in label or "لا يدعم" in label:
        return -2.0
    return 0.0


def display_rank_score(item: dict) -> float:
    try:
        quality = float(item.get("quality_score", 0) or 0)
        readiness = float(item.get("execution_readiness_score", 0) or 0)
        rr = float(item.get("rr_1", 0) or 0)
        continuation = float(item.get("continuation_score", 0) or 0)
        runner = float(item.get("runner_score", 0) or 0)
        alignment = float(item.get("alignment_score", 0) or 0)
        risk_pct = float(item.get("display_risk_pct", item.get("risk_pct", 0)) or 0)
        effective_volume = float(item.get("effective_volume_ratio", 0) or 0)
        volume_pace = float(item.get("volume_pace_ratio", 0) or 0)
        news_effect = float(item.get("news_effect_score", item.get("catalyst_score", 0)) or 0) if NEWS_SCORE_ENABLED else 0.0
        news_scope = _display_text_label(item.get("news_scope"))

        phase = _display_text_label(item.get("market_phase"))
        readiness_label = _display_text_label(item.get("execution_readiness_label"))
        freshness_label = _display_text_label(item.get("price_freshness_label"))
        execution_status = _display_text_label(item.get("execution_status"))
        signal_stage = _display_text_label(item.get("signal_stage"))
        hist_conf = _display_text_label(item.get("historical_confidence_label"))
        hist_behavior = _display_text_label(item.get("historical_behavior_label"))
        historical_behavior_score = float(item.get("historical_behavior_score", 50) or 50)
        historical_context_score = float(item.get("historical_context_score", 50) or 50)

        score = quality
        score += readiness * 0.45
        score += max(-4.0, min(rr, 3.0)) * 7.0
        score += continuation * 0.10
        score += runner * 0.08
        score += alignment * 0.08
        score += min(effective_volume, 2.5) * 4.0

        if phase == "open":
            if volume_pace >= 1.2:
                score += 3.0
            elif volume_pace >= 1.0:
                score += 1.5
            elif volume_pace < 0.9:
                score -= 2.0

        score += historical_confidence_bonus(hist_conf)
        score += historical_behavior_bonus(hist_behavior)
        score += (historical_behavior_score - 50.0) * 0.18
        score += (historical_context_score - 50.0) * 0.14

        if readiness_label in {"جاهز", "اختراق مؤكد", "ارتداد مؤكد"}:
            score += 5.0
        elif "انتظار" in readiness_label:
            score += 1.5
        elif "مطاردة" in readiness_label:
            score -= 8.0

        if execution_status == "READY":
            score += 4.0
        elif execution_status == "WAIT_CONFIRM":
            score += 1.0
        elif execution_status == "AVOID":
            score -= 10.0

        if news_effect != 0:
            if news_scope == "company":
                score += news_effect * 0.45
            elif news_scope == "sector":
                score += news_effect * 0.30
            elif news_scope == "market":
                score += news_effect * 0.15

        if phase == "open" and "آخر إغلاق" in freshness_label:
            score -= 4.0

        if signal_stage == "إنذار مبكر":
            score -= 1.5

        if risk_pct > 8:
            score -= 5.0
        elif risk_pct > 6:
            score -= 2.0

        return round(score, 2)
    except Exception:
        try:
            return float(item.get("quality_score", 0) or 0)
        except Exception:
            return 0.0


def sort_display_bucket(items):
    return sorted(
        list(items or []),
        key=lambda x: (
            float(x.get("display_rank_score", display_rank_score(x)) or 0),
            float(x.get("quality_score", 0) or 0),
            float(x.get("execution_readiness_score", 0) or 0),
            float(x.get("rr_1", 0) or 0),
            float(x.get("continuation_score", 0) or 0),
            float(x.get("runner_score", 0) or 0),
            float(x.get("historical_behavior_score", 50) or 50),
            float(x.get("historical_context_score", 50) or 50),
        ),
        reverse=True,
    )


