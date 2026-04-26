from app.settings import REF_INFO_CACHE
import time
from app.utils import *
from app.utils import _cache_get, _cache_set
from app.market_data import *
from app.historical_engine import *
from app.market_sector_engine import *
from app.news_engine import *
from app.sharia_filter import *
from app.scoring_engine import *

print("FIX11_STRATEGY_ENGINE_LOCAL_BREAKOUT loaded")

# Local guard to prevent scan-wide failure if breakout_quality_label is missing from imports.
def breakout_quality_label(trade_type: str, momentum: str, body_strength: float, close_strength: float, volume_ratio: float) -> str:
    try:
        vr = float(volume_ratio or 0)
        bs = float(body_strength or 0)
        cs = float(close_strength or 0)
        t = str(trade_type or "")
        m = str(momentum or "")
        score = 0
        if "اختراق" in t:
            score += 2
        if "ارتداد" in t:
            score += 1
        if "صاعد" in m or "قوي" in m:
            score += 2
        if bs >= 0.65:
            score += 1
        if cs >= 0.65:
            score += 1
        if vr >= 2.0:
            score += 2
        elif vr >= 1.3:
            score += 1
        if score >= 7:
            return "اختراق قوي جدًا" if "اختراق" in t else "ارتداد قوي جدًا"
        if score >= 5:
            return "اختراق قوي" if "اختراق" in t else "ارتداد قوي"
        if score >= 3:
            return "جيد"
        return "متوسط"
    except Exception:
        return "جيد"

try:
    from scanner import enrich_strategy_profile
except Exception:
    def enrich_strategy_profile(stock: dict) -> dict:
        return stock

def get_prev_from_daily_bars(daily_bars):
    if not daily_bars:
        return None
    try:
        ny = ZoneInfo("America/New_York")
        today_ny = datetime.now(ny).date()
        market_open = is_market_open_now()
        candidates = []
        for row in daily_bars:
            close_price = to_float(row.get("c"))
            if close_price <= 0:
                continue
            row_date = None
            ts = row.get("t")
            try:
                if ts:
                    row_date = datetime.fromtimestamp(float(ts) / 1000.0, ny).date()
            except:
                row_date = None
            if market_open and row_date == today_ny:
                continue
            candidates.append(row)
        source = candidates[-1] if candidates else daily_bars[-1]
        return {
            "price": to_float(source.get("c")),
            "high": to_float(source.get("h")),
            "low": to_float(source.get("l")),
            "volume": to_float(source.get("v")),
            "open": to_float(source.get("o")),
        }
    except:
        return None

def get_prev(symbol):
    try:
        r = http_get_json(
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/prev?apiKey={POLYGON_API_KEY}",
            timeout=12
        )
        results = r.get("results", [])
        if not results:
            return None
        d = results[0]
        return {
            "price": to_float(d.get("c")),
            "high": to_float(d.get("h")),
            "low": to_float(d.get("l")),
            "volume": to_float(d.get("v")),
            "open": to_float(d.get("o")),
        }
    except:
        return None

def get_latest_minute_price(symbol):
    try:
        today_ny = latest_market_date_str()
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/"
            f"{today_ny}/{today_ny}?adjusted=true&sort=desc&limit=5&apiKey={POLYGON_API_KEY}"
        )
        r = http_get_json(url, timeout=12)
        bars = r.get("results", []) or []
        if not bars:
            return {
                "available": False,
                "current_price": 0.0,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "volume": 0.0,
                "updated": 0,
            }

        bar = bars[0]
        return {
            "available": True,
            "current_price": to_float(bar.get("c")),
            "open": to_float(bar.get("o")),
            "high": to_float(bar.get("h")),
            "low": to_float(bar.get("l")),
            "volume": to_float(bar.get("v")),
            "updated": int(to_float(bar.get("t"))),
        }
    except:
        return {
            "available": False,
            "current_price": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0.0,
            "updated": 0,
        }

def get_snapshot_quote(symbol):
    try:
        symbol = str(symbol).upper().strip()
        phase = get_market_phase()
        cache_key = f"{symbol}:{phase}"
        cached = _cache_get(SNAPSHOT_CACHE, cache_key)
        if cached:
            return cached

        url = f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        t = (r.get("ticker") or {})
        day = (t.get("day") or {})
        prev_day = (t.get("prevDay") or {})
        last_trade = (t.get("lastTrade") or {})
        min_data = (t.get("min") or {})

        last_price = to_float(last_trade.get("p"))
        prev_close = to_float(prev_day.get("c")) or 0.0
        day_open = to_float(day.get("o")) or 0.0
        day_high = to_float(day.get("h")) or 0.0
        day_low = to_float(day.get("l")) or 0.0
        day_volume = to_float(day.get("v")) or 0.0

        minute = get_latest_minute_price(symbol)

        current_price = last_price or to_float(min_data.get("c")) or to_float(day.get("c")) or 0.0
        if minute.get("available") and minute.get("current_price", 0) > 0:
            minute_price = to_float(minute.get("current_price", 0))
            if phase in {"open", "after_hours", "pre_market"}:
                current_price = minute_price or current_price
                if minute.get("high", 0) > 0:
                    day_high = max(day_high, to_float(minute.get("high", 0)))
                if minute.get("low", 0) > 0:
                    minute_low = to_float(minute.get("low", 0))
                    day_low = min(day_low, minute_low) if day_low > 0 else minute_low
                if minute.get("volume", 0) > 0:
                    day_volume = max(day_volume, to_float(minute.get("volume", 0)))

        if phase == "open":
            ttl = SNAPSHOT_CACHE_TTL_OPEN
        elif phase in {"after_hours", "pre_market"}:
            ttl = SNAPSHOT_CACHE_TTL_EXTENDED
        else:
            ttl = SNAPSHOT_CACHE_TTL_CLOSED

        change_vs_prev_close_pct = 0.0
        if current_price > 0 and prev_close > 0:
            change_vs_prev_close_pct = ((current_price - prev_close) / prev_close) * 100

        change_from_open_pct = 0.0
        if current_price > 0 and day_open > 0:
            change_from_open_pct = ((current_price - day_open) / day_open) * 100

        out = {
            "available": current_price > 0,
            "current_price": current_price,
            "previous_close": prev_close,
            "open": day_open,
            "high": day_high,
            "low": day_low,
            "volume": day_volume,
            "change_vs_prev_close_pct": change_vs_prev_close_pct,
            "change_from_open_pct": change_from_open_pct,
            "updated": int(time.time() * 1000),
            "source": "minute+snapshot" if minute.get("available") else "snapshot",
        }
        return _cache_set(SNAPSHOT_CACHE, cache_key, out, ttl)
    except:
        return {
            "available": False,
            "current_price": 0.0,
            "previous_close": 0.0,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0.0,
            "change_vs_prev_close_pct": 0.0,
            "change_from_open_pct": 0.0,
            "updated": 0,
            "source": "error",
        }

def get_reference_info(symbol):
    symbol = str(symbol).upper().strip()
    if not symbol:
        return {"company": "", "sector": "", "industry": "", "industry_id": ""}

    if symbol in REF_INFO_CACHE:
        return REF_INFO_CACHE[symbol]

    out = {"company": "", "sector": "", "industry": "", "industry_id": ""}
    try:
        url = f"https://api.polygon.io/v3/reference/tickers/{symbol}?apiKey={POLYGON_API_KEY}"
        r = http_get_json(url, timeout=12)
        res = r.get("results", {}) or {}
        sic_description = str(res.get("sic_description", "")).strip()
        sector = ""
        industry = sic_description
        if " - " in sic_description:
            parts = [p.strip() for p in sic_description.split(" - ") if p.strip()]
            if len(parts) >= 2:
                sector = parts[0]
                industry = parts[-1]

        out = {
            "company": str(res.get("name", "")).strip(),
            "sector": sector,
            "industry": industry,
            "industry_id": ""
        }
    except:
        pass

    REF_INFO_CACHE[symbol] = out
    return out

def get_info(symbol):
    c = COMPANIES_DATA.get(symbol, {})
    industry_id = str(c.get("IndustryId", "")).strip()
    s = SECTOR_DATA.get(industry_id, {})

    company = str(c.get("Company Name", "")).strip()
    sector = str(s.get("sector", "")).strip()
    industry = str(s.get("industry", "")).strip()

    if company and sector and industry:
        return {
            "company": company,
            "sector": sector,
            "industry": industry,
            "industry_id": industry_id
        }

    ref = get_reference_info(symbol)
    return {
        "company": company or ref["company"],
        "sector": sector or ref["sector"],
        "industry": industry or ref["industry"],
        "industry_id": industry_id
    }

def _context_closes_from_bars(bars: list[dict]) -> list[float]:
    closes = []
    for row in bars or []:
        c = to_float((row or {}).get("c", 0))
        if c > 0:
            closes.append(c)
    return closes

def _context_return_pct(bars: list[dict], lookback: int = 20) -> float:
    closes = _context_closes_from_bars(bars)
    if len(closes) < lookback + 1:
        return 0.0
    start = float(closes[-(lookback + 1)] or 0)
    end = float(closes[-1] or 0)
    if start <= 0 or end <= 0:
        return 0.0
    return ((end - start) / start) * 100.0

def trade_plan_pro(symbol, manual_sharia_exclusions=None):
    daily_bars = get_daily_bars(symbol)
    prev = get_prev_from_daily_bars(daily_bars) or get_prev(symbol)
    if not prev:
        return None

    info = get_info(symbol)
    financials = get_financials(symbol, prev)
    hist = get_history_levels(symbol, prev, daily_bars)
    trend_data = get_trend(symbol, daily_bars)
    market_sector_context = get_market_sector_context(symbol, info.get("sector", ""), info.get("industry", ""), daily_bars)
    intraday = get_intraday_snapshot(symbol)
    volume_ratio = get_volume_ratio(symbol, intraday, daily_bars)
    news_bundle = get_news_bundle(symbol, info["company"], info.get("sector", ""), info.get("industry", ""))
    news_note = news_bundle.get("news_note", "لا يوجد خبر حديث")
    catalyst_score = news_bundle.get("catalyst_score", 0)

    sharia_assessment = assess_sharia(
        symbol,
        info["sector"], info["industry"],
        financials["total_assets"], financials["cash"], financials["total_debt"],
        manual_sharia_exclusions,
    )
    halal_ok = bool(sharia_assessment.get("is_halal", True))
    halal_reason = str(sharia_assessment.get("reason", "") or "")

    if sharia_assessment.get("should_block", False):
        phase = get_market_phase()
        current_price = float(prev.get("price", 0) or 0)
        exclusion_decision = "مستبعد يدويًا" if sharia_assessment.get("manual_excluded") else "مرفوض شرعياً"
        owner_text = "↩️ يمكنك إعادة السهم يدويًا إذا رغبت" if sharia_assessment.get("manual_excluded") else "تجنب السهم"
        return {
            "symbol": symbol,
            "type": "Excluded",
            "decision": exclusion_decision,
            "entry": 0,
            "stop_loss": 0,
            "target_1": 0,
            "target_2": 0,
            "risk_pct": 0,
            "quality_score": 0,
            "rank_label": "-",
            "valid_for": "-",
            "trend": trend_data["trend"],
            "volume_ratio": volume_ratio,
            "effective_volume_ratio": volume_ratio,
            "data_quality": "high",
            "catalyst_score": catalyst_score,
            "news_note": news_note,
            "news_title": news_bundle.get("news_title", ""),
            "news_badge": news_bundle.get("news_badge", ""),
            "news_category": news_bundle.get("news_category", "neutral"),
            "news_sentiment": news_bundle.get("news_sentiment", "neutral"),
            "news_scope": news_bundle.get("news_scope", "neutral"),
            "news_scope_label": news_bundle.get("news_scope_label", news_scope_label("neutral")),
            "news_context_note": news_bundle.get("news_context_note", ""),
            "display_price": safe_round(current_price),
            "current_price_live": safe_round(current_price),
            "market_phase": phase,
            "market_phase_label": market_phase_label(phase),
            "benchmark_symbol": "SPY",
            "market_support_label": "غير واضح",
            "sector_etf_symbol": "",
            "sector_support_label": "غير متوفر",
            "market_sector_score": 0,
            "market_sector_alignment_label": "محايد",
            "market_sector_alignment_detail": "لا توجد بيانات كافية عن المؤشر والقطاع.",
            "historical_behavior_score": 50.0,
            "historical_context_score": 50.0,
            "historical_context_label": "محايد",
            "historical_context_detail": "لا توجد بيانات كافية لربط السهم بالمؤشر والقطاع.",
            "price_source_label": "آخر إغلاق",
            "display_entry_label": "—",
            "display_entry_price": 0,
            "display_target_label": "—",
            "display_target_price": 0,
            "display_stop_label": "—",
            "display_stop_price": 0,
            "risk_flags": [halal_reason],
            "ai_summary": halal_reason,
            "breakout_quality": "N/A",
            "execution_status": "AVOID",
            "owner_action": owner_text,
            "company": info["company"],
            "sector": info["sector"],
            "industry": info["industry"],
            "financials": financials,
            "sharia_status": sharia_assessment.get("status", "non_compliant"),
            "sharia_label": sharia_assessment.get("label", "غير متوافق"),
            "sharia_reason": halal_reason,
            "sharia_manual_excluded": bool(sharia_assessment.get("manual_excluded", False)),
            "sharia_is_gray": bool(sharia_assessment.get("is_gray", False)),
        }

    live_block = build_live_price_block(symbol, prev, intraday)
    atr_overlay = get_atr_overlay(prev.get("price", 0), daily_bars)
    current_price = live_block["current_price_live"] if live_block["current_price_live"] > 0 else prev["price"]
    high = max(prev["high"], live_block["high_live"] if live_block["high_live"] > 0 else prev["high"])
    low = min(prev["low"], live_block["low_live"] if live_block["low_live"] > 0 else prev["low"])

    pullback_context = compute_pullback_context(current_price, high, low, intraday, trend_data["trend"])
    trade_type = "Pullback" if pullback_context.get("pullback_candidate") else "Breakout"
    historical_behavior = analyze_historical_behavior(daily_bars, trade_type)
    historical_context = analyze_historical_context_behavior(
        daily_bars,
        market_sector_context.get("benchmark_symbol", ""),
        market_sector_context.get("sector_etf_symbol", ""),
        trade_type,
        market_sector_context.get("market_support_score", 0),
        market_sector_context.get("sector_support_score", 0),
    )

    price_penalty, price_flag = dynamic_price_penalty(current_price, trade_type)
    volume_pace_ratio = compute_volume_pace_ratio(intraday, volume_ratio)
    effective_volume_ratio = get_effective_volume_ratio(volume_ratio, intraday)

    if trade_type == "Breakout":
        entry = high * 1.01
        stop = high * 0.95
        target1 = high * 1.07
        target2 = high * 1.10
    else:
        fib_50 = float(pullback_context.get("fib_50", 0) or 0)
        fib_62 = float(pullback_context.get("fib_62", 0) or 0)
        zone_high = float(pullback_context.get("pullback_zone_high", 0) or 0)
        entry = fib_50 if fib_50 > 0 else current_price
        if current_price > 0 and zone_high > 0 and current_price < zone_high:
            entry = max(current_price, fib_62 if fib_62 > 0 else current_price)
        stop = min(low * 0.985, fib_62 * 0.985) if fib_62 > 0 and low > 0 else low * 0.97
        target1 = max(high * 0.995, entry * 1.04)
        target2 = max(high * 1.02, entry * 1.08)

    risk_pct = ((entry - stop) / entry) * 100 if entry > 0 else 0

    breakout_quality = breakout_quality_label(
        trade_type,
        "صاعد" if trend_data["trend"] in ["صاعد", "صاعد قوي"] else trend_data["trend"],
        0.7,
        0.75,
        effective_volume_ratio,
    )

    pullback_score = int(pullback_context.get("pullback_score", 0) or 0)
    core_quality = compute_core_quality_score(
        trend_data["trend"],
        effective_volume_ratio,
        catalyst_score,
        hist,
        breakout_quality,
        pullback_score,
        trade_type,
        price_penalty,
        risk_pct,
        news_bundle.get("news_scope", "neutral"),
        news_bundle.get("news_sentiment", news_bundle.get("news_category", "neutral")),
        news_bundle.get("news_sessions_since", 999),
        market_sector_context.get("market_sector_score", 0),
        historical_behavior.get("historical_behavior_score", 50),
        historical_context.get("historical_context_score", 50),
    )
    quality = core_quality
    rank_label = make_rank_label(quality)

    rr_1_preview = 0.0
    if entry > 0 and stop > 0 and target1 > 0 and entry > stop:
        rr_1_preview = (target1 - entry) / (entry - stop) if (entry - stop) > 0 else 0.0

    decision = "مراقبة"
    if quality >= 82 and rr_1_preview >= 0.75 and risk_pct <= 8.5 and breakout_quality != "FAILED":
        decision = "دخول قوي"
    elif quality >= 66 and risk_pct <= 12 and breakout_quality != "FAILED":
        decision = "دخول بحذر"

    decision = apply_news_decision_guard(
        decision,
        news_bundle.get("news_scope", "neutral"),
        news_bundle.get("news_sentiment", news_bundle.get("news_category", "neutral")),
        news_bundle.get("news_sessions_since", 999),
        quality,
    )
    decision = apply_market_sector_decision_guard(
        decision,
        market_sector_context.get("market_sector_score", 0),
        market_sector_context.get("market_support_label", ""),
        market_sector_context.get("sector_support_label", ""),
        market_sector_context.get("sector_etf_symbol", ""),
    )
    if decision == "دخول قوي" and historical_behavior.get("historical_behavior_score", 50) < 42 and historical_context.get("historical_context_score", 50) < 44 and str(historical_behavior.get("historical_confidence_label", "") or "") in {"متوسطة", "عالية"}:
        decision = "دخول بحذر"

    execution_status = compute_execution_status(
        trade_type, decision, trend_data["trend"], effective_volume_ratio, catalyst_score, breakout_quality
    )
    owner_action_text = owner_decision(decision, trend_data["trend"], breakout_quality, effective_volume_ratio, catalyst_score)
    valid_for = estimate_validity(trade_type, trend_data["trend"], effective_volume_ratio, catalyst_score)

    risk_flags = []
    if price_flag:
        risk_flags.append(price_flag)
    if hist["near_ath"]:
        risk_flags.append("قريب من القمة التاريخية")
    if hist["ath_breakout_zone"]:
        risk_flags.append("منطقة اختراق قمة تاريخية")
    news_scope = str(news_bundle.get("news_scope", "neutral") or "neutral")
    news_sentiment = str(news_bundle.get("news_sentiment", "neutral") or "neutral")
    if catalyst_score > 0:
        if news_scope == "company":
            risk_flags.append("خبر شركة داعم")
        elif news_scope == "sector":
            risk_flags.append("خبر قطاعي داعم")
    elif catalyst_score < 0:
        if news_sentiment == "legal" and news_scope == "company":
            risk_flags.append("خبر قانوني مباشر")
        elif news_scope == "company":
            risk_flags.append("خبر شركة سلبي")
        elif news_scope == "sector":
            risk_flags.append("خبر قطاعي ضاغط")
        elif news_scope == "market":
            risk_flags.append("سياق سوق عام ضاغط")
    if str(market_sector_context.get("market_support_label", "") or "") in {"ضاغط", "ضاغط قوي"}:
        risk_flags.append(f"المؤشر {market_sector_context.get('benchmark_symbol', 'SPY')} {market_sector_context.get('market_support_label', '')}")
    if str(market_sector_context.get("sector_support_label", "") or "") in {"ضاغط", "ضاغط قوي"}:
        risk_flags.append(f"القطاع {market_sector_context.get('sector_support_label', '')}")
    if not str(market_sector_context.get("sector_etf_symbol", "") or ""):
        risk_flags.append("ETF القطاع غير متوفر: الثقة أقل قليلًا")
    if sharia_assessment.get("is_gray"):
        risk_flags.append("الحكم الشرعي غير محسوم")
    if info["sector"] == "":
        risk_flags.append("بيانات القطاع/الصناعة ناقصة")
    if financials["total_assets"] <= 0:
        risk_flags.append("إجمالي الأصول غير متوفر")
    if financials["shares"] <= 0:
        risk_flags.append("عدد الأسهم غير متوفر")
    if financials["approx_market_cap"] <= 0:
        risk_flags.append("القيمة السوقية التقريبية غير متوفرة")
    if intraday.get("market_open") and intraday.get("intraday_volume_ratio", 0) >= 1.5:
        risk_flags.append("سيولة لحظية قوية")
    if breakout_quality == "FAILED":
        risk_flags.append("سلوك اختراق فاشل")
    if trade_type == "Pullback" and not pullback_context.get("in_pullback_zone"):
        risk_flags.append("الارتداد خارج المنطقة المثالية")

    ai_summary_parts = [
        f"الاتجاه {trend_data['trend']}",
        f"السيولة {'مرتفعة' if effective_volume_ratio >= 1.2 else 'ضعيفة' if effective_volume_ratio < 0.9 else 'متوسطة'}",
    ]
    if intraday.get("market_open"):
        ai_summary_parts.append(f"افتتاح اليوم: {intraday.get('opening_drive', 'unknown')}")
        if intraday.get("above_vwap_proxy"):
            ai_summary_parts.append("فوق VWAP اللحظي")
        if intraday.get("intraday_volume_ratio", 0) >= 1.2:
            ai_summary_parts.append("السيولة اللحظية داعمة")
    if market_sector_context.get("market_support_label"):
        ai_summary_parts.append(f"المؤشر {market_sector_context.get('benchmark_symbol', 'SPY')}: {market_sector_context.get('market_support_label')}")
    if market_sector_context.get("sector_etf_symbol"):
        ai_summary_parts.append(f"القطاع {market_sector_context.get('sector_etf_symbol')}: {market_sector_context.get('sector_support_label')}")
    if trade_type == "Pullback" and pullback_context.get("pullback_pattern_label"):
        ai_summary_parts.append(str(pullback_context.get("pullback_pattern_label")))
    if catalyst_score > 0:
        if news_scope == "company":
            ai_summary_parts.append("يوجد محفز شركة إيجابي")
        elif news_scope == "sector":
            ai_summary_parts.append("يوجد دعم قطاعي")
    elif catalyst_score < 0:
        if news_sentiment == "legal" and news_scope == "company":
            ai_summary_parts.append("يوجد ضغط قانوني مباشر")
        elif news_scope == "company":
            ai_summary_parts.append("يوجد خبر شركة سلبي")
        elif news_scope == "sector":
            ai_summary_parts.append("يوجد خبر قطاعي ضاغط")
        elif news_scope == "market":
            ai_summary_parts.append("السياق العام ضاغط")
    elif news_scope == "market":
        ai_summary_parts.append("سياق سوق عام فقط")
    if hist["ath_breakout_zone"]:
        ai_summary_parts.append("في منطقة قمة تاريخية")
    if breakout_quality == "FAILED":
        ai_summary_parts.append("شمعة الاختراق فشلت")
    elif breakout_quality == "STRONG":
        ai_summary_parts.append("اختراق قوي")

    if sharia_assessment.get("is_gray"):
        ai_summary_parts.append("الحكم الشرعي غير محسوم")
    if info["sector"] == "" or financials["total_assets"] <= 0 or financials["shares"] <= 0:
        ai_summary_parts.append("جودة البيانات ضعيفة")
    elif financials["approx_market_cap"] <= 0:
        ai_summary_parts.append("جودة البيانات متوسطة")

    data_quality = "low" if (info["sector"] == "" or financials["total_assets"] <= 0 or financials["shares"] <= 0) else ("medium" if financials["approx_market_cap"] <= 0 else "high")

    levels = compute_breakout_levels(live_block["current_price_live"], high, low, intraday, trade_type, pullback_context)
    timing = compute_timing_layer(live_block["current_price_live"], intraday, effective_volume_ratio, levels, live_block.get("market_phase", "closed"))

    plan = {
        "symbol": symbol,
        "type": trade_type,
        "decision": decision,
        "entry": safe_round(entry),
        "stop_loss": safe_round(stop),
        "target_1": safe_round(target1),
        "target_2": safe_round(target2),
        "risk_pct": safe_round(risk_pct),
        "quality_score": quality,
        "quality_core_score": core_quality,
        "execution_layer_score": 50,
        "execution_layer_label": "محايد",
        "execution_layer_adjustment": 0,
        "price_penalty_points": price_penalty,
        "rank_label": rank_label,
        "valid_for": valid_for,
        "trend": trend_data["trend"],
        "volume_ratio": safe_round(volume_ratio),
        "volume_pace_ratio": safe_round(volume_pace_ratio),
        "effective_volume_ratio": safe_round(effective_volume_ratio),
        "data_quality": data_quality,
        "catalyst_score": catalyst_score,
        "news_note": news_note,
        "news_title": news_bundle.get("news_title", ""),
        "news_badge": news_bundle.get("news_badge", ""),
        "news_category": news_bundle.get("news_category", "neutral"),
        "news_sentiment": news_bundle.get("news_sentiment", "neutral"),
        "news_scope": news_bundle.get("news_scope", "neutral"),
        "news_scope_label": news_bundle.get("news_scope_label", news_scope_label("neutral")),
        "news_effect_score": news_bundle.get("news_effect_score", 0),
        "news_is_catalyst": news_bundle.get("news_is_catalyst", False),
        "news_context_note": news_bundle.get("news_context_note", ""),
        "news_related_tickers_count": news_bundle.get("news_related_tickers_count", 0),
        "news_freshness_label": news_bundle.get("news_freshness_label", ""),
        "news_published_utc": news_bundle.get("news_published_utc", ""),
        "news_sessions_since": news_bundle.get("news_sessions_since", 999),
        "risk_flags": risk_flags,
        "ai_summary": " - ".join(ai_summary_parts),
        "breakout_quality": breakout_quality,
        "execution_status": execution_status,
        "owner_action": owner_action_text,
        "intraday": intraday,
        **pullback_context,
        **levels,
        **timing,
        **live_block,
        **atr_overlay,
        **historical_behavior,
        **historical_context,
        **market_sector_context,
        "company": info["company"],
        "sector": info["sector"],
        "industry": info["industry"],
        "financials": financials,
        "sharia_status": sharia_assessment.get("status", "compliant"),
        "sharia_label": sharia_assessment.get("label", "متوافق مبدئيًا"),
        "sharia_reason": halal_reason,
        "sharia_manual_excluded": bool(sharia_assessment.get("manual_excluded", False)),
        "sharia_is_gray": bool(sharia_assessment.get("is_gray", False)),
    }
    plan = enrich_strategy_profile(plan)
    plan["rr_1"] = safe_round(rr_1_preview)
    plan = apply_decision_layers(plan)
    return plan

