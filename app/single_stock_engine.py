import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.utils import *
from app.data_store import get_manual_sharia_exclusions_map, get_manual_sharia_approvals_map
from app.performance_tracker import upsert_performance_signal
from app.strategy_engine import trade_plan_pro, get_prev, get_info
from app.sharia_filter import get_financials, assess_sharia
from app.market_data import get_history_levels, get_trend, get_intraday_snapshot, get_volume_ratio, build_live_price_block
from app.news_engine import get_news_bundle, news_scope_label
from app.display_contract import enrich_display_meta, display_rank_score
from app.opportunity_intelligence import enrich_opportunity_intelligence
from app.early_movement import enrich_stock_with_early_movement
from app.source_promotion_v2a import enrich_row_source_promotion_v2a
from app.detection_journal import enrich_stock_with_detection_journal
from app.source_promotion_engine_v2 import enrich_row_source_promotion_v2
from app.pre_move_engine import enrich_row_pre_move
from app.final_decision_engine import apply_final_decision
from app.quote_resolver import resolve_symbol_quote, overlay_quote_contract
from app.early_watch_lifecycle import enrich_early_watch_lifecycle
from scanner import apply_late_move_filter, assign_execution_mode, normalize_execution_labels, enrich_signal_stage, finalize_display_contract
from scanner import get_scan_universe as _unused_get_scan_universe
from scanner import get_last_source_diagnostics
from app.market_data import get_active_universe
from app.settings import SCAN_UNIVERSE_TARGET, SCAN_MAX_WORKERS
try:
    from app.missed_opportunities import record_missed_source_candidates, record_missed_seen_from_scan
except Exception:  # Keep radar resilient if diagnostic module is disabled/unavailable.
    record_missed_source_candidates = None
    record_missed_seen_from_scan = None

LAST_SCAN_DEBUG = {}

def get_last_scan_debug():
    return dict(LAST_SCAN_DEBUG or {})

def scan_all(debug: bool = False):
    global LAST_SCAN_DEBUG
    manual_sharia_exclusions = get_manual_sharia_exclusions_map()
    manual_sharia_approvals = get_manual_sharia_approvals_map()
    scan_started_at = time.time()
    requested_universe = int(SCAN_UNIVERSE_TARGET or 190)
    raw_symbols = list(get_active_universe(requested_universe) or [])
    source_diag = get_last_source_diagnostics()
    try:
        if record_missed_source_candidates is not None:
            record_missed_source_candidates(raw_symbols, source_diag, source="scan_all_source_universe")
    except Exception as exc:
        print(f"MISSED_SOURCE_RECORD_ERROR: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
    source_reasons = (source_diag or {}).get("reasons", {}) if isinstance(source_diag, dict) else {}
    symbols = [s for s in raw_symbols if normalize_symbol_text(s) not in manual_sharia_exclusions]
    rows = []
    diag = {
        "scan_updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "raw_count": len(raw_symbols),
        "after_manual_exclusion": len(symbols),
        "rows": 0,
        "excluded": 0,
        "no_plan": 0,
        "errors": 0,
        "manual_exclusions": len(manual_sharia_exclusions or {}),
        "manual_approvals": len(manual_sharia_approvals or {}),
        "sample_symbols": raw_symbols[:20],
        "source_target": int((source_diag or {}).get("active_target", len(raw_symbols)) or len(raw_symbols)),
        "source_active_count": int((source_diag or {}).get("active_count", len(raw_symbols)) or len(raw_symbols)),
        "source_engine_pool": int((source_diag or {}).get("target", len(raw_symbols)) or len(raw_symbols)),
        "source_engine_version": str((source_diag or {}).get("engine_version", "") or ""),
        "source_mode": str((source_diag or {}).get("source_mode", "") or ""),
        "source_market_mode": str((source_diag or {}).get("market_activity_mode", "") or ""),
        "dynamic_discovery_enabled": bool((source_diag or {}).get("dynamic_discovery_enabled", False)),
        "dynamic_discovery_mode": str((source_diag or {}).get("dynamic_discovery_mode", "") or ""),
        "dynamic_phase_detail": str((source_diag or {}).get("phase_detail", "") or ""),
        "dynamic_phase_label": str((source_diag or {}).get("phase_label", "") or ""),
        "dynamic_broad_market_count": int((source_diag or {}).get("broad_market_count", 0) or 0),
        "dynamic_reference_count": int((source_diag or {}).get("reference_count", 0) or 0),
        "dynamic_candidate_count_before_confirm": int((source_diag or {}).get("candidate_count_before_confirm", 0) or 0),
        "dynamic_candidate_count_after_confirm": int((source_diag or {}).get("candidate_count_after_confirm", 0) or 0),
        "dynamic_fmp_confirm_requested": int((source_diag or {}).get("fmp_confirm_requested", 0) or 0),
        "dynamic_fmp_confirmed": int((source_diag or {}).get("fmp_confirmed", 0) or 0),
        "dynamic_fmp_extended_confirmed": int((source_diag or {}).get("fmp_extended_confirmed", 0) or 0),
        "dynamic_fmp_movers_count": int((source_diag or {}).get("fmp_movers_count", 0) or 0),
        "dynamic_live_ignition_hot_lane_count": int((source_diag or {}).get("live_ignition_hot_lane_count", 0) or 0),
        "dynamic_pre_move_engine_v2_count": int((source_diag or {}).get("pre_move_engine_v2_count", 0) or 0),
        "dynamic_late_mover_review_count": int((source_diag or {}).get("late_mover_review_count", 0) or 0),
        "dynamic_next_scan_interval_sec": int((source_diag or {}).get("next_scan_interval_sec", 0) or 0),
        "dynamic_source_bucket_counts": (source_diag or {}).get("source_bucket_counts", {}),
        "dynamic_price_under_2_deprioritized": int((source_diag or {}).get("price_under_2_deprioritized", 0) or 0),
        "dynamic_price_under_2_exception": int((source_diag or {}).get("price_under_2_exception", 0) or 0),
        "dynamic_price_over_300_deprioritized": int((source_diag or {}).get("price_over_300_deprioritized", 0) or 0),
        "dynamic_discovery_elapsed_sec": (source_diag or {}).get("elapsed_sec", None),
        "scan_requested_universe": int(requested_universe),
        "manual_priority_count": int((source_diag or {}).get("manual_priority_count", 0) or 0),
        "sharia_source_filter_version": str((source_diag or {}).get("sharia_source_filter_version", "") or ""),
        "sharia_prefilter_candidates": int((source_diag or {}).get("sharia_prefilter_candidates", 0) or 0),
        "sharia_prefilter_blocked": int((source_diag or {}).get("sharia_prefilter_blocked", 0) or 0),
        "sharia_prefilter_gray_used": int((source_diag or {}).get("sharia_prefilter_gray_used", 0) or 0),
        "sharia_prefilter_gray_total": int((source_diag or {}).get("sharia_prefilter_gray_total", 0) or 0),
        "sharia_prefilter_refill_count": int((source_diag or {}).get("sharia_prefilter_refill_count", 0) or 0),
        "sharia_refill_reserve_size": int((source_diag or {}).get("sharia_refill_reserve_size", 0) or 0),
        "sharia_prefilter_clean_total": int((source_diag or {}).get("sharia_prefilter_clean_total", 0) or 0),
        "sharia_prefilter_clean_used": int((source_diag or {}).get("sharia_prefilter_clean_used", 0) or 0),
        "sharia_prefilter_gray_cap": int((source_diag or {}).get("sharia_prefilter_gray_cap", 0) or 0),
        "sharia_prefilter_clean_shortage": int((source_diag or {}).get("sharia_prefilter_clean_shortage", 0) or 0),
        "sharia_prefilter_final_shortage": int((source_diag or {}).get("sharia_prefilter_final_shortage", 0) or 0),
        "sample_sharia_prefilter_blocked": (source_diag or {}).get("sharia_prefilter_sample_blocked", [])[:15] if isinstance(source_diag, dict) else [],
        "sample_source_reasons": [source_reasons.get(str(x).upper(), []) for x in raw_symbols[:10]],
        "sample_excluded": [],
        "sample_no_plan": [],
        "sample_errors": [],
    }
    print(f"SCAN_START raw={diag['raw_count']} after_manual={diag['after_manual_exclusion']} manual_exclusions={diag['manual_exclusions']}", flush=True)

    def process_symbol(s):
        try:
            p = trade_plan_pro(s, manual_sharia_exclusions, manual_sharia_approvals)
            if not p:
                return {"kind": "no_plan", "symbol": s, "reason": "trade_plan_empty"}
            if p.get("type") == "Excluded":
                return {"kind": "excluded", "symbol": s, "reason": str(p.get("reason") or p.get("note") or "excluded")[:180]}

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

            try:
                src_reason = source_reasons.get(str(s).upper().strip(), [])
                if src_reason:
                    p["source_reason_tags"] = src_reason
                    p["source_reason"] = " + ".join(src_reason[:5])
            except Exception:
                pass

            p = enrich_display_meta(p)
            # Source / Early Discovery V2: record first detection before Early Movement
            # decides whether the row is truly early or already moved.
            try:
                p = enrich_stock_with_detection_journal(p, source_layer="scan_all_deep_analysis")
            except Exception:
                pass
            try:
                p = enrich_row_pre_move(p)
            except Exception:
                pass
            try:
                p = enrich_stock_with_early_movement(p)
            except Exception:
                pass
            try:
                p = enrich_row_source_promotion_v2a(p)
            except Exception:
                pass
            try:
                p = enrich_row_source_promotion_v2(p)
            except Exception:
                pass
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
            except Exception:
                pass
            try:
                p = enrich_opportunity_intelligence(p)
            except Exception:
                pass
            try:
                p = apply_final_decision(p)
            except Exception:
                pass
            try:
                p = enrich_early_watch_lifecycle(p)
            except Exception:
                pass
            return {"kind": "row", "symbol": s, "row": p}
        except Exception as e:
            return {"kind": "error", "symbol": s, "error": f"{type(e).__name__}: {str(e)[:260]}"}

    max_workers = min(int(SCAN_MAX_WORKERS or 16), max(4, len(symbols))) if symbols else 4
    diag["scan_max_workers"] = int(max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_symbol, s) for s in symbols]
        for future in as_completed(futures):
            result = future.result()
            kind = result.get("kind")
            if kind == "row":
                rows.append(result.get("row"))
            elif kind == "excluded":
                diag["excluded"] += 1
                if len(diag["sample_excluded"]) < 15:
                    diag["sample_excluded"].append({"symbol": result.get("symbol"), "reason": result.get("reason")})
            elif kind == "no_plan":
                diag["no_plan"] += 1
                if len(diag["sample_no_plan"]) < 15:
                    diag["sample_no_plan"].append({"symbol": result.get("symbol"), "reason": result.get("reason")})
            elif kind == "error":
                diag["errors"] += 1
                if len(diag["sample_errors"]) < 25:
                    diag["sample_errors"].append({"symbol": result.get("symbol"), "error": result.get("error")})
                print(f"SCAN_SYMBOL_ERROR: {result.get('symbol')} | {result.get('error')}", flush=True)

    for item in rows:
        try:
            item["display_rank_score"] = display_rank_score(item)
        except Exception:
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

    for item in rows:
        try:
            if str(item.get("decision", "") or "") in {"دخول قوي", "دخول بحذر"}:
                upsert_performance_signal(item)
        except Exception:
            continue

    diag["rows"] = len(rows)
    diag["strong"] = len([x for x in rows if str(x.get("decision", "")) == "دخول قوي"])
    diag["cautious"] = len([x for x in rows if str(x.get("decision", "")) == "دخول بحذر"])
    diag["watch"] = len([x for x in rows if str(x.get("decision", "")) == "مراقبة"])
    diag["news_with_title"] = len([x for x in rows if str(x.get("news_title", "") or "").strip()])
    diag["news_with_catalyst"] = len([x for x in rows if bool(x.get("news_is_catalyst", False))])
    diag["news_fetch_skipped"] = len([x for x in rows if bool(x.get("news_fetch_skipped", False))])
    diag["news_negative_or_legal"] = len([x for x in rows if str(x.get("news_sentiment", "") or "") in {"negative", "legal"}])
    try:
        if record_missed_seen_from_scan is not None:
            diag["missed_opportunities"] = record_missed_seen_from_scan(rows, diag, market_phase=str((rows[0] or {}).get("market_phase", "") if rows else ""), source="scan_all_full")
    except Exception as exc:
        diag["missed_opportunities"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    diag["scan_elapsed_sec"] = round(time.time() - scan_started_at, 2)
    diag["sample_rows"] = [
        {
            "symbol": x.get("symbol"),
            "decision": x.get("decision"),
            "quality_score": x.get("quality_score"),
            "execution_readiness_score": x.get("execution_readiness_score"),
            "display_rank_score": x.get("display_rank_score"),
        }
        for x in rows[:15]
    ]
    LAST_SCAN_DEBUG = diag
    print(f"SCAN_DONE raw={diag['raw_count']} after_manual={diag['after_manual_exclusion']} rows={diag['rows']} strong={diag['strong']} cautious={diag['cautious']} watch={diag['watch']} no_plan={diag['no_plan']} excluded={diag['excluded']} errors={diag['errors']}", flush=True)
    if debug:
        return rows, diag
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
            financials = get_financials(symbol, prev)
            hist = get_history_levels(symbol)
            trend_data = get_trend(symbol)
            intraday = get_intraday_snapshot(symbol)
            volume_ratio = get_volume_ratio(symbol, intraday)
            news_bundle = get_news_bundle(symbol, info["company"], info.get("sector", ""), info.get("industry", ""))
            news_note = news_bundle.get("news_title") or news_bundle.get("news_context_note") or news_bundle.get("news_note", "لا يوجد خبر حديث")
            catalyst_score = news_bundle.get("catalyst_score", 0)
            sharia_map = get_manual_sharia_exclusions_map()
            sharia_approvals = get_manual_sharia_approvals_map()
            sharia_assessment = assess_sharia(symbol, info["sector"], info["industry"], financials["total_assets"], financials["cash"], financials["total_debt"], sharia_map, sharia_approvals)
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
        trade_plan = trade_plan_pro(symbol, get_manual_sharia_exclusions_map(), get_manual_sharia_approvals_map())
        if trade_plan:
            # Big Clean Rebuild: enforce FMP -> Polygon fallback before any display/decision layer.
            # If FMP is incomplete and Polygon is used, it is labeled delayed/monitoring-only.
            try:
                phase_for_quote = str(trade_plan.get("market_phase", "") or "")
                quote_contract = resolve_symbol_quote(symbol, phase=phase_for_quote, prefer_cache=False, allow_fallback=True)
                trade_plan = overlay_quote_contract(trade_plan, quote_contract)
            except Exception:
                pass
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
            try:
                trade_plan = enrich_stock_with_early_movement(trade_plan)
            except Exception:
                pass
            try:
                trade_plan = enrich_row_source_promotion_v2a(trade_plan)
            except Exception:
                pass
            try:
                trade_plan = enrich_row_source_promotion_v2(trade_plan)
            except Exception:
                pass
            try:
                trade_plan = enrich_opportunity_intelligence(trade_plan)
            except Exception:
                pass
            try:
                # Re-apply quote contract after legacy display/enrichment layers,
                # so no later layer silently restores old previous-close/unknown fields.
                trade_plan = overlay_quote_contract(trade_plan, quote_contract)
            except Exception:
                pass
            try:
                trade_plan = apply_final_decision(trade_plan)
            except Exception:
                pass
            try:
                trade_plan = enrich_early_watch_lifecycle(trade_plan)
            except Exception:
                pass
    except Exception as e:
        trade_error = str(e)

    return {
        "symbol": symbol,
        "overview": overview,
        "trade_plan": trade_plan,
        "overview_error": overview_error,
        "trade_error": trade_error,
    }



