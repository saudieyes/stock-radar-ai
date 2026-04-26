from concurrent.futures import ThreadPoolExecutor, as_completed
from app.utils import *
from app.data_store import get_manual_sharia_exclusions_map
from app.performance_tracker import upsert_performance_signal
from app.strategy_engine import trade_plan_pro, get_prev, get_info
from app.sharia_filter import get_financials, assess_sharia
from app.market_data import get_history_levels, get_trend, get_intraday_snapshot, get_volume_ratio, build_live_price_block
from app.news_engine import get_news_bundle, news_scope_label
from app.display_contract import enrich_display_meta, display_rank_score
from scanner import apply_late_move_filter, assign_execution_mode, normalize_execution_labels, enrich_signal_stage, finalize_display_contract
from scanner import get_scan_universe as _unused_get_scan_universe
from app.market_data import get_active_universe

def scan_all():
    manual_sharia_exclusions = get_manual_sharia_exclusions_map()
    symbols = [s for s in get_active_universe(150) if normalize_symbol_text(s) not in manual_sharia_exclusions]
    rows = []

    def process_symbol(s):
        p = trade_plan_pro(s, manual_sharia_exclusions)
        if not p or p.get("type") == "Excluded":
            return None

        p = apply_late_move_filter(p)
        p = assign_execution_mode(p)
        p = normalize_execution_labels(p)
        p = enrich_signal_stage(p)
        p = finalize_display_contract(p)

        if not p.get("price_reliable_for_execution", True) and p.get("market_phase") in {"open", "pre_market", "after_hours"}:
            p["decision"] = "مراقبة"
            p["execution_mode"] = "مراقبة 👀"
            p["execution_note"] = "السعر اللحظي غير موثوق - لا تعتمد عليه للتنفيذ"
            p["owner_action"] = "👀 راقب فقط حتى تتوفر بيانات سعر لحظية موثوقة"
            p.setdefault("risk_flags", []).append("السعر اللحظي غير موثوق")
            p.setdefault("ai_summary", "")
            if p["ai_summary"]:
                p["ai_summary"] += " - "
            p["ai_summary"] += "السعر اللحظي غير موثوق"

        p = enrich_display_meta(p)
        # لا نقتل الفرص: ننظم القوي داخليًا، ونهبط فقط إذا كانت الجاهزية ضعيفة جدًا أو مطاردة واضحة.
        try:
            if str(p.get("decision", "") or "") == "دخول قوي":
                readiness_score = float(p.get("execution_readiness_score", 0) or 0)
                readiness_label = str(p.get("execution_readiness_label", "") or "")
                if readiness_score < 42 or readiness_label in {"مطاردة سعرية"}:
                    p["decision"] = "دخول بحذر"
                    p["signal_strength_label"] = "بحذر"
                    p["signal_strength_bucket"] = 0
            if str(p.get("decision", "") or "") == "دخول بحذر":
                readiness_score = float(p.get("execution_readiness_score", 0) or 0)
                if readiness_score < 24:
                    p["decision"] = "مراقبة"
                    p["signal_strength_label"] = "مراقبة"
                    p["signal_strength_bucket"] = -1
        except:
            pass
        return p

    max_workers = min(12, max(4, len(symbols)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_symbol, s) for s in symbols]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    rows.append(result)
            except:
                continue

    for item in rows:
        try:
            item["display_rank_score"] = display_rank_score(item)
        except:
            item["display_rank_score"] = float(item.get("quality_score", 0) or 0)

    rows.sort(
        key=lambda x: (
            decision_priority(x.get("decision", "")),
            float(x.get("signal_strength_bucket", -1) or -1),
            float(x.get("signal_strength_score", 0) or 0),
            float(x.get("display_rank_score", 0) or 0),
            float(x.get("quality_score", 0) or 0),
            float(x.get("execution_readiness_score", 0) or 0),
            float(x.get("rr_1", 0) or 0),
        ),
        reverse=True,
    )

    # تسجيل التتبع الأسبوعي بشكل تسلسلي بعد انتهاء الفحص كله لتجنب ضياع بعض الإشارات.
    for item in rows:
        try:
            if str(item.get("decision", "") or "") in {"دخول قوي", "دخول بحذر"}:
                upsert_performance_signal(item)
        except:
            continue

    return rows


def build_single_stock_response(symbol: str):
    symbol = str(symbol).upper().strip()
    overview = None
    trade_plan = None
    overview_error = None
    trade_error = None

    try:
        prev = get_prev(symbol)
        if not prev:
            overview = {"symbol": symbol, "available": False, "reason": "No daily data"}
        else:
            info = get_info(symbol)
            financials = get_financials(symbol)
            hist = get_history_levels(symbol)
            trend_data = get_trend(symbol)
            intraday = get_intraday_snapshot(symbol)
            volume_ratio = get_volume_ratio(symbol, intraday)
            news_bundle = get_news_bundle(symbol, info["company"], info.get("sector", ""), info.get("industry", ""))
            news_note = news_bundle.get("news_title") or news_bundle.get("news_context_note") or news_bundle.get("news_note", "لا يوجد خبر حديث")
            catalyst_score = news_bundle.get("catalyst_score", 0)
            sharia_map = get_manual_sharia_exclusions_map()
            sharia_assessment = assess_sharia(symbol, info["sector"], info["industry"], financials["total_assets"], financials["cash"], financials["total_debt"], sharia_map)
            halal_ok = bool(sharia_assessment.get("is_halal", True))
            halal_reason = str(sharia_assessment.get("reason", "") or "")
            live_block = build_live_price_block(symbol, prev, intraday)
            overview = {
                "symbol": symbol,
                "available": True,
                "company": info["company"],
                "sector": info["sector"],
                "industry": info["industry"],
                "price": prev["price"],
                "open": prev["open"],
                "high": prev["high"],
                "low": prev["low"],
                "volume": prev["volume"],
                "trend": trend_data["trend"],
                "volume_ratio": safe_round(volume_ratio),
                "news_note": news_note,
                "catalyst_score": catalyst_score,
                "news_scope": news_bundle.get("news_scope", "neutral"),
                "news_scope_label": news_bundle.get("news_scope_label", news_scope_label("neutral")),
                "news_context_note": news_bundle.get("news_context_note", ""),
                "near_ath": hist["near_ath"],
                "ath_breakout_zone": hist["ath_breakout_zone"],
                "intraday": intraday,
                "halal": halal_ok,
                "halal_reason": halal_reason,
                "sharia_status": sharia_assessment.get("status", "compliant"),
                "sharia_label": sharia_assessment.get("label", "متوافق مبدئيًا"),
                "sharia_manual_excluded": bool(sharia_assessment.get("manual_excluded", False)),
                "sharia_is_gray": bool(sharia_assessment.get("is_gray", False)),
                **live_block,
            }
    except Exception as e:
        overview_error = str(e)
        overview = {"symbol": symbol, "available": False}

    try:
        trade_plan = trade_plan_pro(symbol, get_manual_sharia_exclusions_map())
        if trade_plan:
            trade_plan = apply_late_move_filter(trade_plan)
            trade_plan = assign_execution_mode(trade_plan)
            trade_plan = normalize_execution_labels(trade_plan)
            trade_plan = enrich_signal_stage(trade_plan)
            trade_plan = finalize_display_contract(trade_plan)
            if not trade_plan.get("price_reliable_for_execution", True) and trade_plan.get("market_phase") in {"open", "pre_market", "after_hours"}:
                trade_plan["decision"] = "مراقبة"
                trade_plan["execution_mode"] = "مراقبة 👀"
                trade_plan["execution_note"] = "السعر اللحظي غير موثوق - لا تعتمد عليه للتنفيذ"
                trade_plan["owner_action"] = "👀 راقب فقط حتى تتوفر بيانات سعر لحظية موثوقة"
            if not trade_plan.get("price_reliable_for_execution", True) and trade_plan.get("market_phase") in {"open", "pre_market", "after_hours"}:
                trade_plan.setdefault("risk_flags", []).append("السعر اللحظي غير موثوق")
            trade_plan = enrich_display_meta(trade_plan)
    except Exception as e:
        trade_error = str(e)

    return {
        "symbol": symbol,
        "overview": overview,
        "trade_plan": trade_plan,
        "overview_error": overview_error,
        "trade_error": trade_error,
    }


