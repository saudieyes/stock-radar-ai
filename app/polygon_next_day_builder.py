"""Polygon Next-Day Candidate Builder V2W.

Additive builder for Stock Radar AI.

Design goals:
- do NOT replace existing Weekly Priority / Prepared / V2V lists;
- use Polygon after close / pre-open / weekend learning to widen the source universe;
- store only compact candidate summaries in app_data;
- keep Sharia blocks out of actionable monitoring lanes;
- provide lightweight learning/logging without changing Strong/Cautious decisions.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Iterable

from .settings import DATA_DIR, HTTP_SESSION, POLYGON_API_KEY
from .utils import safe_round, to_float
from .data_store import get_manual_sharia_exclusions_map, get_manual_sharia_approvals_map
from .polygon_flatfile_fetcher import previous_trading_day, is_us_market_trading_day, trading_days_ending
from .polygon_weekly_builder import _clean_symbol, _company_symbol_set, _company_profile_map, _is_common_stock_symbol, _iter_sources

POLYGON_NEXT_DAY_BUILDER_VERSION = "polygon_next_day_builder_v2w_additive_compact_learning_2026_06_21"
DEFAULT_OUTPUT_PATH = Path(DATA_DIR) / "polygon_next_day_candidates.json"
LEARNING_OUTPUT_PATH = Path(DATA_DIR) / "polygon_next_day_learning_log.json"


def _now_text() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool = False) -> bool:
    text = str(os.getenv(name, "") or "").strip().lower()
    if not text:
        return bool(default)
    return text in {"1", "true", "yes", "on", "y"}


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data
    except Exception:
        pass
    return default


def _write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _parse_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _default_trade_date() -> date:
    # Use the latest completed trading day.  On weekends this intentionally points
    # back to Friday so Polygon can still build next-day learning/prep without
    # repeatedly polling a closed session.
    return previous_trading_day(datetime.utcnow().date() + timedelta(days=1))


def _row_symbol(row: dict) -> str:
    for key in ("T", "ticker", "symbol", "sym"):
        sym = _clean_symbol((row or {}).get(key))
        if sym:
            return sym
    return ""


def _field(row: dict, *keys: str) -> float:
    for key in keys:
        val = to_float((row or {}).get(key))
        if val != 0:
            return float(val)
    return 0.0


def _polygon_grouped_rows(trade_date: str | date | datetime | None) -> tuple[list[dict], dict]:
    d = _parse_date(trade_date) or _default_trade_date()
    if not POLYGON_API_KEY:
        return [], {"ok": False, "configured": False, "error": "POLYGON_API_KEY missing", "trade_date": d.isoformat()}
    try:
        url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}"
        r = HTTP_SESSION.get(url, params={"adjusted": "true", "apiKey": POLYGON_API_KEY}, timeout=18)
        status = int(getattr(r, "status_code", 0) or 0)
        if status != 200:
            return [], {"ok": False, "configured": True, "status_code": status, "error": str(getattr(r, "text", ""))[:180], "trade_date": d.isoformat()}
        data = r.json()
        rows = data.get("results") or []
        return list(rows or []), {"ok": True, "configured": True, "status_code": status, "count": len(rows or []), "trade_date": d.isoformat(), "source": "polygon_grouped_daily"}
    except Exception as exc:
        return [], {"ok": False, "configured": True, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "trade_date": d.isoformat()}


def _local_daily_rows(path: str | Path) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    sources: list[str] = []
    try:
        for name, kind, reader in _iter_sources(path, fallback_kind="daily"):
            if str(kind or "").lower() not in {"daily", "unknown"}:
                continue
            sources.append(str(name))
            for row in reader or []:
                if isinstance(row, dict):
                    rows.append(row)
        return rows, {"ok": True, "source": "local_daily", "count": len(rows), "sources": sources[:20]}
    except Exception as exc:
        return rows, {"ok": False, "source": "local_daily", "count": len(rows), "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def _classify_sharia(sym: str, exclusions: dict, approvals: dict) -> tuple[str, str]:
    s = _clean_symbol(sym)
    if s in exclusions:
        item = exclusions.get(s) or {}
        return "blocked", str(item.get("reason") or item.get("note") or "محجوب شرعيًا من القائمة اليدوية")[:180]
    if s in approvals:
        return "approved", "معتمد يدويًا"
    return "needs_review", "يحتاج مراجعة شرعية قبل أي إجراء"


def _score_row(row: dict, profile: dict | None = None) -> dict:
    sym = _row_symbol(row)
    open_p = _field(row, "o", "open")
    close = _field(row, "c", "close", "price")
    high = _field(row, "h", "high")
    low = _field(row, "l", "low")
    volume = _field(row, "v", "volume")
    vwap = _field(row, "vw", "vwap")
    txns = _field(row, "n", "transactions")
    if close <= 0 and vwap > 0:
        close = vwap
    rng = max(0.0, high - low) if high > 0 and low > 0 else 0.0
    range_pct = (rng / close * 100.0) if close > 0 and rng > 0 else 0.0
    change_pct = ((close - open_p) / open_p * 100.0) if open_p > 0 and close > 0 else 0.0
    close_pos = ((close - low) / rng) if rng > 0 else 0.0
    dollar_volume = close * volume if close > 0 and volume > 0 else 0.0

    reasons: list[str] = []
    tags: list[str] = []
    score = 0.0

    if 0.75 <= close <= 30:
        score += 16
        tags.append("cheap_stock_focus")
        reasons.append("سعر منخفض/مناسب للمتابعة")
    elif 30 < close <= 80:
        score += 7
    elif close > 150:
        score -= 15
        reasons.append("سعر مرتفع؛ أولوية أقل إلا إذا التأكيد قوي")

    if close_pos >= 0.82:
        score += 18
        tags.append("close_near_high")
        reasons.append("إغلاق قريب من قمة اليوم")
    elif close_pos >= 0.65:
        score += 9
        reasons.append("إغلاق في النصف العلوي من النطاق")

    if 2.0 <= change_pct <= 12.0:
        score += 14
        tags.append("controlled_green_day")
        reasons.append("ارتفاع مضبوط وليس مطاردة كبيرة")
    elif 0.2 <= change_pct < 2.0 and close_pos >= 0.65:
        score += 10
        tags.append("quiet_accumulation")
        reasons.append("تجميع هادئ مع إغلاق مقبول")
    elif change_pct > 18:
        score -= 8
        tags.append("extended_watch_only")
        reasons.append("تحرك ممتد؛ متابعة Pullback فقط")
    elif change_pct < -4 and close_pos >= 0.70:
        score += 6
        tags.append("reclaim_from_weakness")
        reasons.append("محاولة استعادة بعد ضعف")

    if 3.0 <= range_pct <= 18.0:
        score += 10
        tags.append("tradeable_range")
        reasons.append("مدى يومي قابل للمراقبة")
    elif range_pct > 25:
        score -= 6
        tags.append("very_wide_range_risk")
        reasons.append("مدى واسع جدًا؛ مخاطرة أعلى")

    if 250_000 <= dollar_volume <= 40_000_000:
        score += 16
        tags.append("tradable_dollar_volume")
        reasons.append("دولار فوليوم كافٍ للمتابعة")
    elif 80_000 <= dollar_volume < 250_000:
        score += 6
        tags.append("thin_but_watchable")
        reasons.append("سيولة خفيفة؛ مرشح مراقبة فقط")
    elif dollar_volume > 150_000_000:
        score -= 6
        reasons.append("سيولة مؤسسية كبيرة؛ ليس هدف small-stock الأساسي")

    if 100_000 <= volume <= 25_000_000:
        score += 8
    elif volume < 50_000:
        score -= 8
        reasons.append("حجم ضعيف جدًا")

    # Low-float proxy: we do not infer float.  This is only a source-priority proxy
    # for the next-day builder.
    if 0.75 <= close <= 15 and 4 <= range_pct <= 22 and dollar_volume >= 120_000:
        score += 18
        tags.append("low_float_proxy")
        reasons.append("مرشح Small/Low-Float proxy من السعر والمدى والسيولة")

    if txns >= 2000 and close <= 40:
        score += 5
        tags.append("active_prints")

    sector = str((profile or {}).get("sector") or "").strip()
    industry = str((profile or {}).get("industry") or "").strip()
    if sector.lower() in {"financial services", "financial", "banks", "insurance"}:
        tags.append("financial_sector_review")
        reasons.append("قطاع مالي/يحتاج مراجعة شرعية إضافية")
        score -= 5

    if not reasons:
        reasons.append("مرشح Polygon للغد يحتاج تأكيد حي")

    lane = "next_day_watch"
    if "low_float_proxy" in tags:
        lane = "small_stock_next_day_watch"
    elif "quiet_accumulation" in tags:
        lane = "quiet_accumulation_next_day"
    elif "extended_watch_only" in tags:
        lane = "continuation_pullback_only"
    elif change_pct < -4 and close_pos >= 0.70:
        lane = "reclaim_next_day_watch"

    return {
        "symbol": sym,
        "score": round(float(score), 4),
        "lane": lane,
        "price": safe_round(close, 4),
        "open": safe_round(open_p, 4),
        "high": safe_round(high, 4),
        "low": safe_round(low, 4),
        "vwap": safe_round(vwap, 4),
        "volume": int(volume or 0),
        "dollar_volume": safe_round(dollar_volume, 2),
        "change_pct": safe_round(change_pct, 2),
        "range_pct": safe_round(range_pct, 2),
        "close_position": safe_round(close_pos, 3),
        "transactions": int(txns or 0),
        "sector": sector,
        "industry": industry,
        "tags": tags[:12],
        "reasons_ar": reasons[:8],
    }


def _build_from_rows(rows: Iterable[dict], trade_date: str, top_n: int = 160, source: str = "polygon") -> dict:
    company_symbols = _company_symbol_set()
    profiles = _company_profile_map()
    exclusions = get_manual_sharia_exclusions_map()
    approvals = get_manual_sharia_approvals_map()
    clean: list[dict] = []
    needs_review: list[dict] = []
    blocked_learning: list[dict] = []
    rejected_count = 0
    seen: set[str] = set()

    max_scan = max(500, min(12000, _env_int("POLYGON_NEXT_DAY_MAX_ROWS", 7000)))
    min_score = float(os.getenv("POLYGON_NEXT_DAY_MIN_SCORE", "30") or 30)

    for i, row in enumerate(rows or []):
        if i >= max_scan:
            break
        sym = _row_symbol(row)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        if not _is_common_stock_symbol(sym, company_symbols):
            rejected_count += 1
            continue
        profile = profiles.get(sym, {}) or {}
        item = _score_row(row, profile=profile)
        if not item.get("symbol"):
            rejected_count += 1
            continue
        if float(item.get("price") or 0) <= 0:
            rejected_count += 1
            continue
        sharia_status, sharia_note = _classify_sharia(sym, exclusions, approvals)
        item.update({
            "trade_date": trade_date,
            "source": source,
            "builder_version": POLYGON_NEXT_DAY_BUILDER_VERSION,
            "sharia_status": sharia_status,
            "sharia_note": sharia_note,
            "actionability": "watch_only",
            "not_buy_reason_ar": "قائمة تجهيز للغد من Polygon؛ تحتاج تأكيد FMP/قرار نهائي لاحقًا.",
        })
        if sharia_status == "blocked":
            item["actionability"] = "learning_only"
            item["blocked_learning_only"] = True
            blocked_learning.append(item)
            continue
        if float(item.get("score") or 0) < min_score:
            rejected_count += 1
            continue
        if sharia_status == "approved":
            clean.append(item)
        else:
            needs_review.append(item)

    clean = sorted(clean, key=lambda x: float(x.get("score") or 0), reverse=True)
    needs_review = sorted(needs_review, key=lambda x: float(x.get("score") or 0), reverse=True)
    blocked_learning = sorted(blocked_learning, key=lambda x: float(x.get("score") or 0), reverse=True)

    limit = max(20, min(600, int(top_n or 160)))
    out_items = (clean + needs_review)[:limit]
    low_float = [x for x in out_items if "low_float_proxy" in (x.get("tags") or [])][:120]
    quiet = [x for x in out_items if x.get("lane") == "quiet_accumulation_next_day"][:80]
    reclaim = [x for x in out_items if x.get("lane") == "reclaim_next_day_watch"][:80]
    continuation = [x for x in out_items if x.get("lane") == "continuation_pullback_only"][:60]

    result = {
        "ok": True,
        "version": POLYGON_NEXT_DAY_BUILDER_VERSION,
        "trade_date": trade_date,
        "built_at": _now_text(),
        "source": source,
        "execute_safe": True,
        "counts": {
            "scanned_rows": min(len(list(rows)) if isinstance(rows, list) else max_scan, max_scan),
            "unique_seen": len(seen),
            "rejected_or_low_score": rejected_count,
            "clean_approved": len(clean),
            "needs_sharia_review": len(needs_review),
            "blocked_learning_only": len(blocked_learning),
            "selected_total": len(out_items),
            "low_float_proxy": len(low_float),
            "quiet_accumulation": len(quiet),
            "reclaim_watch": len(reclaim),
            "continuation_pullback_only": len(continuation),
        },
        "candidates": out_items,
        "sections": {
            "clean_approved": clean[:limit],
            "needs_sharia_review": needs_review[:limit],
            "low_float_proxy": low_float,
            "quiet_accumulation": quiet,
            "reclaim_watch": reclaim,
            "continuation_pullback_only": continuation,
            "learning_only_sharia_blocked": blocked_learning[:120],
        },
        "rule_ar": "V2W: قائمة Polygon للغد مصدر تحضير فقط؛ لا تستبدل القوائم الحالية ولا تفتح شراء مباشر. المحجوب شرعيًا يبقى تعلم فقط.",
    }
    return result


def _append_learning_log(result: dict) -> None:
    try:
        old = _read_json(LEARNING_OUTPUT_PATH, [])
        if not isinstance(old, list):
            old = []
        summary = {
            "version": POLYGON_NEXT_DAY_BUILDER_VERSION,
            "trade_date": result.get("trade_date"),
            "built_at": result.get("built_at"),
            "counts": result.get("counts", {}),
            "top_symbols": [str((x or {}).get("symbol") or "") for x in (result.get("candidates") or [])[:60]],
            "note_ar": "سجل تعلم فقط؛ لا يغير القرار النهائي حتى تتوفر عينة أيام كافية.",
        }
        old.append(summary)
        _write_json(LEARNING_OUTPUT_PATH, old[-80:])
    except Exception:
        pass


def load_polygon_next_day_candidates() -> dict:
    data = _read_json(DEFAULT_OUTPUT_PATH, {})
    if isinstance(data, dict) and data:
        return data
    return {"ok": False, "version": POLYGON_NEXT_DAY_BUILDER_VERSION, "candidates": [], "sections": {}, "reason": "no_saved_polygon_next_day_candidates"}


def polygon_next_day_status() -> dict:
    saved = load_polygon_next_day_candidates()
    learning = _read_json(LEARNING_OUTPUT_PATH, [])
    return {
        "ok": True,
        "version": POLYGON_NEXT_DAY_BUILDER_VERSION,
        "configured": bool(POLYGON_API_KEY),
        "output_path": str(DEFAULT_OUTPUT_PATH),
        "learning_path": str(LEARNING_OUTPUT_PATH),
        "saved": saved,
        "learning_runs": len(learning) if isinstance(learning, list) else 0,
        "last_learning_run": (learning[-1] if isinstance(learning, list) and learning else {}),
        "rule_ar": "القائمة الحالية لا تُستبدل؛ هذه طبقة Polygon إضافية لتوسيع منبع الغد والتعلم.",
    }


def build_polygon_next_day_from_polygon(trade_date: str | None = None, top_n: int = 160, execute: bool = False, force: bool = False) -> dict:
    d = _parse_date(trade_date) or _default_trade_date()
    # Avoid repeated accidental weekend polling unless caller explicitly asks.  On
    # weekends we build from the previous trading day if available; force only
    # affects same-day re-pull behavior, not storage safety.
    rows, diag = _polygon_grouped_rows(d)
    if not rows:
        return {"ok": False, "version": POLYGON_NEXT_DAY_BUILDER_VERSION, "trade_date": d.isoformat(), "fetch": diag, "candidates": [], "rule_ar": "لم يتم بناء القائمة؛ لا يوجد حفظ خام."}
    result = _build_from_rows(rows, trade_date=d.isoformat(), top_n=top_n, source="polygon_grouped_daily")
    result["fetch"] = diag
    result["execute"] = bool(execute)
    if execute:
        _write_json(DEFAULT_OUTPUT_PATH, result)
        _append_learning_log(result)
        result["saved_to"] = str(DEFAULT_OUTPUT_PATH)
    return result


def build_polygon_next_day_from_local(path: str, top_n: int = 160, execute: bool = False) -> dict:
    rows, diag = _local_daily_rows(path)
    if not rows:
        return {"ok": False, "version": POLYGON_NEXT_DAY_BUILDER_VERSION, "path": path, "fetch": diag, "candidates": []}
    trade_date = ""
    try:
        for row in rows[:20]:
            for key in ("date", "day", "t", "timestamp"):
                val = row.get(key)
                if val:
                    trade_date = str(val)[:10]
                    break
            if trade_date:
                break
    except Exception:
        pass
    trade_date = trade_date if len(trade_date) == 10 and trade_date[:4].isdigit() else _default_trade_date().isoformat()
    result = _build_from_rows(rows, trade_date=trade_date, top_n=top_n, source="local_daily_polygon")
    result["fetch"] = diag
    result["execute"] = bool(execute)
    if execute:
        _write_json(DEFAULT_OUTPUT_PATH, result)
        _append_learning_log(result)
        result["saved_to"] = str(DEFAULT_OUTPUT_PATH)
    return result


def format_polygon_next_day_brief(data: dict) -> str:
    if not isinstance(data, dict) or not data.get("ok"):
        return "V2W — Polygon Next-Day\nلا توجد قائمة محفوظة أو فشل البناء."
    counts = data.get("counts", {}) or {}
    lines = [
        "V2W — Polygon Next-Day Candidate Builder",
        f"تاريخ البيانات: {data.get('trade_date', '-')}",
        f"الإجمالي المختار: {counts.get('selected_total', 0)} | Low-Float proxy: {counts.get('low_float_proxy', 0)} | يحتاج شرعية: {counts.get('needs_sharia_review', 0)} | محجوب تعلم فقط: {counts.get('blocked_learning_only', 0)}",
        "",
        "أفضل المرشحين:",
    ]
    for item in (data.get("candidates") or [])[:25]:
        sym = item.get("symbol")
        score = item.get("score")
        price = item.get("price")
        lane = item.get("lane")
        sh = item.get("sharia_status")
        reasons = "، ".join(list(item.get("reasons_ar") or [])[:2])
        lines.append(f"- {sym}: score={score} | price={price} | lane={lane} | sharia={sh} | {reasons}")
    lines.append("")
    lines.append("هذه قائمة تحضير فقط؛ لا تفتح شراء مباشر ولا تستبدل القوائم الحالية.")
    return "\n".join(lines)
