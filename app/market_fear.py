"""VIX / Market Fear decision-support layer for Stock Radar AI.

V4d goal
---------
This is a real risk-context layer, not a cosmetic label:
- fetch VIX/fear data from paid market-data providers when configured;
- store recent fear snapshots for later validation;
- provide practical execution guidance (position size, no-chase strictness,
  confirmation strictness) without changing stock scoring/ranking yet.

Safe by design: this module never changes stock decisions, Sharia filters, or
opportunity classification. It returns context to the UI and to Evidence reports.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .settings import FMP_API_KEY, HTTP_SESSION, POLYGON_API_KEY
from .sqlite_store import get_json, set_json
from .utils import safe_round, to_float

NY_TZ = ZoneInfo("America/New_York")

FMP_BASE_URL = str(os.getenv("FMP_BASE_URL", "https://financialmodelingprep.com") or "https://financialmodelingprep.com").rstrip("/")
MARKET_FEAR_ENABLED = str(os.getenv("MARKET_FEAR_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
MARKET_FEAR_CACHE_TTL_SEC = int(float(os.getenv("MARKET_FEAR_CACHE_TTL_SEC", "900") or 900))
MARKET_FEAR_TIMEOUT_SEC = float(os.getenv("MARKET_FEAR_TIMEOUT_SEC", "7") or 7)
MARKET_FEAR_HISTORY_LIMIT = int(float(os.getenv("MARKET_FEAR_HISTORY_LIMIT", "260") or 260))

# Try real VIX first, then a liquid VIX ETF fallback. The fallback is clearly marked.
VIX_SYMBOL_CANDIDATES = ["^VIX", "VIX", "I:VIX", "VIXY"]


def _now_dt() -> datetime:
    return datetime.now(NY_TZ)


def _now_text() -> str:
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _pct_change(cur: float, prev: float) -> float:
    try:
        if float(prev) <= 0:
            return 0.0
        return ((float(cur) - float(prev)) / float(prev)) * 100.0
    except Exception:
        return 0.0


def _first_number(row: dict, keys: list[str]) -> float:
    for key in keys:
        try:
            val = _safe_float(row.get(key), 0.0)
            if val != 0:
                return float(val)
        except Exception:
            continue
    return 0.0


def _extract_fmp_quote_row(data: Any) -> dict:
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else {}
    if isinstance(data, dict):
        # FMP stable endpoints may wrap rows in a list-ish key depending on plan.
        for key in ["data", "results", "quotes"]:
            val = data.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val[0]
        return data
    return {}


def _fetch_fmp_quote(symbol: str) -> dict:
    if not FMP_API_KEY:
        return {"ok": False, "error": "fmp_key_missing"}
    encoded = quote(symbol, safe="")
    urls = [
        f"{FMP_BASE_URL}/api/v3/quote/{encoded}?apikey={FMP_API_KEY}",
        f"{FMP_BASE_URL}/stable/quote?symbol={encoded}&apikey={FMP_API_KEY}",
        f"{FMP_BASE_URL}/api/v3/quote-short/{encoded}?apikey={FMP_API_KEY}",
    ]
    for url in urls:
        try:
            r = HTTP_SESSION.get(url, timeout=MARKET_FEAR_TIMEOUT_SEC)
            if r.status_code >= 400:
                continue
            row = _extract_fmp_quote_row(r.json())
            if not row:
                continue
            price = _first_number(row, ["price", "last", "lastSalePrice", "close", "previousClose"])
            prev = _first_number(row, ["previousClose", "previous_close", "prevClose", "open"])
            if price <= 0:
                continue
            change_pct = _safe_float(row.get("changesPercentage") or row.get("changePercentage") or row.get("change_pct"), 0)
            if not change_pct and prev > 0:
                change_pct = _pct_change(price, prev)
            change_points = _safe_float(row.get("change") or row.get("changes"), 0)
            if not change_points and prev > 0:
                change_points = price - prev
            return {
                "ok": True,
                "provider": "FMP",
                "symbol": str(row.get("symbol") or symbol),
                "price": safe_round(price, 4),
                "previous_close": safe_round(prev, 4),
                "change_pct": safe_round(change_pct, 2),
                "change_points": safe_round(change_points, 4),
                "volume": _safe_float(row.get("volume"), 0),
                "raw_source": "fmp_quote",
            }
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            continue
    return {"ok": False, "error": locals().get("last_error", "fmp_quote_unavailable")}


def _fetch_polygon_prev_vix() -> dict:
    if not POLYGON_API_KEY:
        return {"ok": False, "error": "polygon_key_missing"}
    try:
        url = f"https://api.polygon.io/v2/aggs/ticker/I:VIX/prev?adjusted=true&apiKey={POLYGON_API_KEY}"
        r = HTTP_SESSION.get(url, timeout=MARKET_FEAR_TIMEOUT_SEC)
        if r.status_code >= 400:
            return {"ok": False, "error": f"polygon_http_{r.status_code}"}
        data = r.json()
        results = data.get("results") or []
        if not results:
            return {"ok": False, "error": "polygon_no_results"}
        row = results[0]
        close = _safe_float(row.get("c"), 0)
        open_px = _safe_float(row.get("o"), 0)
        if close <= 0:
            return {"ok": False, "error": "polygon_close_missing"}
        return {
            "ok": True,
            "provider": "Polygon",
            "symbol": "I:VIX",
            "price": safe_round(close, 4),
            "previous_close": safe_round(open_px, 4),
            "change_pct": safe_round(_pct_change(close, open_px), 2) if open_px > 0 else 0.0,
            "change_points": safe_round(close - open_px, 4) if open_px > 0 else 0.0,
            "raw_source": "polygon_prev_aggregate",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}


def _fetch_vix_quote() -> dict:
    errors = []
    for symbol in VIX_SYMBOL_CANDIDATES:
        q = _fetch_fmp_quote(symbol)
        if q.get("ok") and _safe_float(q.get("price"), 0) > 0:
            q["is_vix_proxy"] = symbol == "VIXY"
            q["requested_symbol"] = symbol
            return q
        errors.append({symbol: q.get("error", "unavailable")})
    q = _fetch_polygon_prev_vix()
    if q.get("ok"):
        q["is_vix_proxy"] = False
        q["requested_symbol"] = "I:VIX"
        return q
    errors.append({"I:VIX": q.get("error", "unavailable")})
    return {"ok": False, "errors": errors, "error": "vix_quote_unavailable"}


def _fetch_vix_history(days: int = 8) -> list[dict]:
    """Best-effort daily VIX history. Used for trend only; safe to be empty."""
    out: list[dict] = []
    if FMP_API_KEY:
        for symbol in ["^VIX", "VIX"]:
            try:
                encoded = quote(symbol, safe="")
                url = f"{FMP_BASE_URL}/api/v3/historical-price-full/{encoded}?timeseries={max(5, int(days))}&apikey={FMP_API_KEY}"
                r = HTTP_SESSION.get(url, timeout=MARKET_FEAR_TIMEOUT_SEC)
                if r.status_code >= 400:
                    continue
                data = r.json()
                rows = data.get("historical") if isinstance(data, dict) else []
                if not isinstance(rows, list) or not rows:
                    continue
                for row in rows[:days]:
                    if not isinstance(row, dict):
                        continue
                    close = _safe_float(row.get("close"), 0)
                    if close > 0:
                        out.append({"date": str(row.get("date") or ""), "close": safe_round(close, 4), "source": "FMP"})
                if out:
                    return out
            except Exception:
                continue
    if POLYGON_API_KEY:
        try:
            end = _now_dt().date()
            start = end - timedelta(days=max(12, int(days) * 2))
            url = f"https://api.polygon.io/v2/aggs/ticker/I:VIX/range/1/day/{start.isoformat()}/{end.isoformat()}?adjusted=true&sort=desc&limit={max(10, int(days))}&apiKey={POLYGON_API_KEY}"
            r = HTTP_SESSION.get(url, timeout=MARKET_FEAR_TIMEOUT_SEC)
            if r.status_code < 400:
                data = r.json()
                rows = data.get("results") or []
                for row in rows[:days]:
                    close = _safe_float(row.get("c"), 0)
                    ts = _safe_float(row.get("t"), 0)
                    d = ""
                    try:
                        d = datetime.fromtimestamp(ts / 1000.0, NY_TZ).date().isoformat() if ts else ""
                    except Exception:
                        d = ""
                    if close > 0:
                        out.append({"date": d, "close": safe_round(close, 4), "source": "Polygon"})
        except Exception:
            pass
    return out


def _regime_for_vix(vix: float, change_pct: float = 0.0, trend_5d_pct: float = 0.0) -> dict:
    # Base score from VIX level.
    if vix >= 35:
        score, key, label = 95, "extreme_fear", "خوف شديد جدًا"
    elif vix >= 28:
        score, key, label = 84, "high_stress", "ضغط/خوف مرتفع"
    elif vix >= 22:
        score, key, label = 70, "stress", "سوق متوتر"
    elif vix >= 18:
        score, key, label = 58, "caution", "حذر متوسط"
    elif vix >= 14:
        score, key, label = 42, "normal", "طبيعي"
    else:
        score, key, label = 28, "calm", "هادئ"

    if change_pct >= 10:
        score += 8
    elif change_pct >= 5:
        score += 4
    elif change_pct <= -8:
        score -= 4
    if trend_5d_pct >= 15:
        score += 6
    elif trend_5d_pct <= -15:
        score -= 4
    score = max(0, min(100, score))

    if score >= 85:
        strictness = "very_strict"
        position_mult = 0.35
        no_chase = "very_strict"
        badge = "🔴"
    elif score >= 70:
        strictness = "strict"
        position_mult = 0.50
        no_chase = "strict"
        badge = "🟠"
    elif score >= 55:
        strictness = "elevated"
        position_mult = 0.70
        no_chase = "elevated"
        badge = "🟡"
    elif score <= 35:
        strictness = "normal"
        position_mult = 1.00
        no_chase = "normal"
        badge = "🟢"
    else:
        strictness = "normal"
        position_mult = 0.85
        no_chase = "normal"
        badge = "⚪"

    return {
        "regime_key": key,
        "regime_label": label,
        "stress_score": safe_round(score, 1),
        "confirmation_strictness": strictness,
        "no_chase_strictness": no_chase,
        "position_size_multiplier": position_mult,
        "badge": badge,
    }


def _build_guidance(regime: dict, vix: float, change_pct: float, trend_5d_pct: float) -> list[str]:
    strictness = regime.get("confirmation_strictness")
    if strictness == "very_strict":
        return [
            "خفّض حجم الصفقة بوضوح ولا تدخل بكامل المخاطرة.",
            "لا تطارد أي سهم صاعد بقوة؛ انتظر Pullback أو إعادة اختبار واضحة.",
            "اختراقات المقاومة تحتاج سيولة مؤكدة وثباتًا بعد الاختراق، لا مجرد لمس السعر.",
            "فضّل الأسهم ذات دعم قريب ووقف واضح وتجنب الخطط ذات التذبذب العالي.",
        ]
    if strictness == "strict":
        return [
            "استخدم حجم دخول أصغر من المعتاد.",
            "ارفع شرط تأكيد السيولة والثبات فوق الدخول.",
            "تجنب الأسهم القريبة من مقاومة/قمة إذا كانت السيولة غير مؤكدة.",
            "لا تدخل Breakout إلا بعد تأكيد واضح أو إعادة اختبار ناجحة.",
        ]
    if strictness == "elevated":
        return [
            "السوق يحتاج حذرًا إضافيًا؛ لا تطارد الحركة المتأخرة.",
            "يفضل الدخول قرب الدعم أو بعد تأكيد السيولة.",
            "خفض حجم الدخول قليلًا إذا كانت الخطة عالية المخاطرة.",
        ]
    if vix < 14 and change_pct <= 5:
        return [
            "الخوف منخفض نسبيًا، لكن لا يزال الالتزام بالدخول والوقف ضروريًا.",
            "لا تجعل هدوء السوق سببًا لمطاردة الأسهم المتأخرة.",
        ]
    return [
        "الخوف العام طبيعي تقريبًا؛ قرارات السهم الفردية والسيولة تبقى الأهم.",
        "استمر في طلب تأكيد الاختراق والسيولة خصوصًا قرب المقاومات.",
    ]


def _history_trend(history: list[dict], current_vix: float) -> dict:
    values = [_safe_float(x.get("close"), 0) for x in history if _safe_float(x.get("close"), 0) > 0]
    trend_5d_pct = 0.0
    avg_5d = 0.0
    if values:
        recent = values[:5]
        avg_5d = sum(recent) / max(1, len(recent))
        # If history is sorted desc, compare current/latest to oldest available in first five.
        base = recent[-1] if recent else 0
        if base > 0:
            trend_5d_pct = _pct_change(current_vix or recent[0], base)
    return {"avg_5d": safe_round(avg_5d, 2), "trend_5d_pct": safe_round(trend_5d_pct, 2), "history_points": len(values)}


def _store_market_fear_snapshot(payload: dict) -> None:
    if not isinstance(payload, dict) or not payload.get("ok"):
        return
    try:
        set_json("last_market_fear", payload)
        hist = get_json("market_fear_history", [])
        if not isinstance(hist, list):
            hist = []
        key = f"{payload.get('captured_at','')}::{payload.get('source_symbol','')}::{payload.get('vix',0)}"
        existing = {f"{x.get('captured_at','')}::{x.get('source_symbol','')}::{x.get('vix',0)}" for x in hist if isinstance(x, dict)}
        if key not in existing:
            hist.insert(0, {
                "captured_at": payload.get("captured_at"),
                "vix": payload.get("vix"),
                "change_pct": payload.get("change_pct"),
                "stress_score": payload.get("stress_score"),
                "regime_label": payload.get("regime_label"),
                "source": payload.get("source"),
                "source_symbol": payload.get("source_symbol"),
            })
        hist = hist[:max(30, MARKET_FEAR_HISTORY_LIMIT)]
        set_json("market_fear_history", hist)
    except Exception:
        pass


def get_market_fear_snapshot(force_refresh: bool = False, store: bool = True) -> dict:
    if not MARKET_FEAR_ENABLED:
        return {"ok": False, "enabled": False, "error": "market_fear_disabled"}

    cached = get_json("last_market_fear", {})
    now_ts = time.time()
    try:
        cached_ts = float((cached or {}).get("captured_ts", 0) or 0)
        if (not force_refresh) and isinstance(cached, dict) and cached.get("ok") and now_ts - cached_ts < MARKET_FEAR_CACHE_TTL_SEC:
            out = dict(cached)
            out["cache_hit"] = True
            return out
    except Exception:
        pass

    quote = _fetch_vix_quote()
    if not quote.get("ok"):
        if isinstance(cached, dict) and cached.get("ok"):
            out = dict(cached)
            out["stale"] = True
            out["cache_hit"] = True
            out["fetch_error"] = quote.get("error") or quote.get("errors")
            return out
        return {
            "ok": False,
            "enabled": True,
            "error": quote.get("error", "vix_unavailable"),
            "errors": quote.get("errors", []),
            "source": "unavailable",
            "note": "تعذر جلب VIX/Market Fear من المصادر المتاحة.",
        }

    vix = _safe_float(quote.get("price"), 0)
    change_pct = _safe_float(quote.get("change_pct"), 0)
    history = _fetch_vix_history(days=8)
    trend = _history_trend(history, vix)
    regime = _regime_for_vix(vix, change_pct=change_pct, trend_5d_pct=_safe_float(trend.get("trend_5d_pct"), 0))
    guidance = _build_guidance(regime, vix, change_pct, _safe_float(trend.get("trend_5d_pct"), 0))

    payload = {
        "ok": True,
        "version": "market_fear_v4d_decision_support_no_scoring",
        "enabled": True,
        "captured_at": _now_text(),
        "captured_ts": now_ts,
        "source": quote.get("provider", "market_data"),
        "source_symbol": quote.get("symbol") or quote.get("requested_symbol") or "^VIX",
        "requested_symbol": quote.get("requested_symbol", ""),
        "is_proxy": bool(quote.get("is_vix_proxy")),
        "vix": safe_round(vix, 2),
        "previous_close": safe_round(_safe_float(quote.get("previous_close"), 0), 2),
        "change_pct": safe_round(change_pct, 2),
        "change_points": safe_round(_safe_float(quote.get("change_points"), 0), 2),
        "vix_avg_5d": trend.get("avg_5d", 0),
        "vix_trend_5d_pct": trend.get("trend_5d_pct", 0),
        "history_points": trend.get("history_points", 0),
        **regime,
        "guidance_ar": guidance,
        "summary_ar": f"{regime.get('badge','')} {regime.get('regime_label')} | VIX {safe_round(vix,2)} | التغير {safe_round(change_pct,2)}% | حجم الدخول المقترح ×{regime.get('position_size_multiplier')}",
        "execution_rules": {
            "position_size_multiplier": regime.get("position_size_multiplier"),
            "confirmation_strictness": regime.get("confirmation_strictness"),
            "no_chase_strictness": regime.get("no_chase_strictness"),
            "breakout_requires_liquidity_confirmation": regime.get("stress_score", 0) >= 55,
            "avoid_late_entries_when_stressed": regime.get("stress_score", 0) >= 55,
            "prefer_near_support_entries_when_stressed": regime.get("stress_score", 0) >= 55,
        },
        "tracking_fields": {
            "market_fear_regime": regime.get("regime_key"),
            "market_fear_score": regime.get("stress_score"),
            "vix": safe_round(vix, 2),
            "vix_change_pct": safe_round(change_pct, 2),
            "vix_trend_5d_pct": trend.get("trend_5d_pct", 0),
        },
        "note": "طبقة VIX/Market Fear للدعم التنفيذي والتتبع فقط في V4d؛ لا تغير نقاط الأسهم أو الفلتر الشرعي أو الترتيب مباشرة.",
    }
    if store:
        _store_market_fear_snapshot(payload)
    return payload


def market_fear_status() -> dict:
    cached = get_json("last_market_fear", {})
    hist = get_json("market_fear_history", [])
    return {
        "ok": True,
        "version": "market_fear_status_v4d",
        "enabled": bool(MARKET_FEAR_ENABLED),
        "fmp_configured": bool(FMP_API_KEY),
        "polygon_configured": bool(POLYGON_API_KEY),
        "cache_ttl_sec": MARKET_FEAR_CACHE_TTL_SEC,
        "last_snapshot": cached if isinstance(cached, dict) else {},
        "history_count": len(hist) if isinstance(hist, list) else 0,
        "history_tail": (hist[:12] if isinstance(hist, list) else []),
        "notes": "VIX/Market Fear is a decision-support layer; no scoring/ranking changes are applied here.",
    }
