"""Active Tradability Gate V2W14.

A conservative safety layer that prevents inactive, stale, delisted, merged, or
badly-mapped symbols from reaching visible/actionable Stock Radar AI lists.

Design rules:
- Hard-block only high-confidence inactive/bad-data cases.
- Warn, but do not erase, rows when the evidence is incomplete.
- Keep the audit payload compact and UI/diagnostics friendly.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, date, timezone
from typing import Any
from zoneinfo import ZoneInfo

ACTIVE_TRADABILITY_GATE_VERSION = "active_tradability_gate_v2w14_full_visible_safety_2026_06_27"
NY_TZ = ZoneInfo("America/New_York")

# High-confidence known inactive/legacy mappings.  Env lets the user add symbols
# immediately without a code change: INACTIVE_TRADABILITY_SYMBOLS=LTHM,ALTM,XYZ
_DEFAULT_INACTIVE_SYMBOLS = {
    "LTHM",  # Livent -> Arcadium -> acquired; no longer current tradable Livent ticker.
    "ALTM",  # Arcadium legacy context after acquisition; keep out unless explicitly replaced.
}
_ENV_INACTIVE_SYMBOLS = {
    x.strip().upper()
    for x in str(os.getenv("INACTIVE_TRADABILITY_SYMBOLS", "") or "").split(",")
    if x.strip()
}
INACTIVE_SYMBOLS = set(_DEFAULT_INACTIVE_SYMBOLS) | set(_ENV_INACTIVE_SYMBOLS)

# Keep symbol validation permissive enough for US tickers such as BRK.B, but
# strict enough to reject malformed payloads, URLs, crypto pairs, and stale labels.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z])?$")
_BAD_SYMBOL_TOKENS = {"N/A", "NA", "NULL", "NONE", "TEST", "TBD", "UNKNOWN", "-"}

_INACTIVE_STATUS_WORDS = {
    "delisted", "inactive", "acquired", "merged", "suspended", "halted_permanent",
    "halted permanent", "no longer trading", "ceased trading", "expired", "terminated",
    "liquidated", "winding down", "deactivated",
}
_INACTIVE_NAME_PHRASES = {
    "formerly known as", "merged with", "acquired by", "no longer trades",
    "ceased trading", "delisted", "liquidation", "livent",
}
_FALSEY_STRINGS = {"false", "no", "0", "inactive", "delisted", "acquired", "merged", "not_tradable", "not tradable"}
_TRUTHY_STRINGS = {"true", "yes", "1", "active", "tradable", "actively_trading", "actively trading"}

_PRICE_KEYS = [
    "price", "current_price", "currentPrice", "last_price", "lastPrice", "last",
    "regularMarketPrice", "extended_price", "afterHoursPrice", "preMarketPrice", "close",
]
_VOLUME_KEYS = ["volume", "regular_volume", "regularMarketVolume", "latestVolume"]
_DOLLAR_VOLUME_KEYS = ["dollar_volume", "dollarVolume", "regular_dollar_volume"]
_TIME_KEYS = [
    "quote_time", "quoteTime", "quote_updated_at", "price_updated_at", "last_updated",
    "lastUpdated", "updated_at", "updatedAt", "timestamp", "last_timestamp",
    "latest_quote_time", "latestQuoteTime", "last_trade_time", "lastTradeTime",
    "trade_time", "tradeTime", "as_of", "asOf", "datetime", "bar_time_text",
    "source_snapshot_at", "scan_updated_at", "analysis_updated_at",
]
_STATUS_KEYS = [
    "status", "quote_status", "listing_status", "market_status", "security_status",
    "asset_status", "symbol_status", "exchange_status", "fmp_status",
]
_ACTIVE_FLAG_KEYS = [
    "is_active", "active", "tradable", "isTradable", "is_tradable",
    "isActivelyTrading", "activelyTrading", "is_actively_trading", "isEnabled",
]
_EXCHANGE_KEYS = ["exchange", "exchangeShortName", "primaryExchange", "market", "mic", "venue"]
_NAME_KEYS = ["company_name", "companyName", "name", "security_name", "description"]


def _s(value: Any) -> str:
    return str(value or "").strip()


def normalize_symbol(symbol: Any) -> str:
    raw = _s(symbol).upper()
    raw = raw.replace(" ", "").replace("/", ".")
    return raw


def symbol_candidate_allowed(symbol: Any) -> bool:
    """Cheap symbol-only prefilter for source discovery.

    This intentionally does not need price/quote context.  It removes only
    malformed or known-inactive symbols so live scan budget is not wasted.
    """
    sym = normalize_symbol(symbol)
    if not sym or sym in _BAD_SYMBOL_TOKENS or sym in INACTIVE_SYMBOLS:
        return False
    if not _SYMBOL_RE.match(sym):
        return False
    return True


def _first(row: dict, keys: list[str], default: Any = None) -> Any:
    for k in keys:
        if isinstance(row, dict) and row.get(k) not in (None, ""):
            return row.get(k)
    return default


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if not value:
                return default
        val = float(value)
        if val != val:
            return default
        return val
    except Exception:
        return default


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Support seconds or milliseconds epochs.
        try:
            x = float(value)
            if x > 10_000_000_000:
                x = x / 1000.0
            if x > 0:
                return datetime.fromtimestamp(x, tz=timezone.utc).astimezone(NY_TZ)
        except Exception:
            return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=NY_TZ)
        return dt.astimezone(NY_TZ)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=NY_TZ)
    txt = _s(value)
    if not txt:
        return None
    # Common FMP/Polygon/UI forms.
    variants = [txt, txt.replace("Z", "+00:00")]
    if " " in txt and "T" not in txt:
        variants.append(txt.replace(" ", "T"))
    for v in variants:
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=NY_TZ)
            return dt.astimezone(NY_TZ)
        except Exception:
            pass
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"]:
        try:
            dt = datetime.strptime(txt, fmt)
            return dt.replace(tzinfo=NY_TZ)
        except Exception:
            pass
    return None


def _now_ny() -> datetime:
    return datetime.now(NY_TZ)


def _market_is_activeish(market_phase: str) -> bool:
    phase = _s(market_phase).lower().replace("-", "_")
    return phase in {"open", "regular", "pre_market", "premarket", "after_hours", "afterhours"}


def _max_quote_age_minutes(market_phase: str) -> float:
    # During the live windows stale explicit quote timestamps are dangerous.
    # During weekend/closed sessions, previous close/after-hours snapshots are OK
    # if they are recent enough for planning.
    phase = _s(market_phase).lower().replace("-", "_")
    if phase in {"open", "regular"}:
        return 75.0
    if phase in {"pre_market", "premarket", "after_hours", "afterhours"}:
        return 150.0
    return 7 * 24 * 60.0


def audit_row(row: dict | None, *, market_phase: str = "", now: datetime | None = None, strict: bool = False) -> dict[str, Any]:
    """Return a compact active-tradability audit for one candidate row.

    visible_allowed: False means the row must not appear in visible practical lists.
    actionable_allowed: False means it must not be Strong/Cautious/Telegram even
    if kept in diagnostics/learning.
    """
    now = now or _now_ny()
    row = row or {}
    sym = normalize_symbol(row.get("symbol") if isinstance(row, dict) else "")
    checks: dict[str, Any] = {}
    warnings: list[str] = []
    reasons: list[dict[str, str]] = []

    def hard(code: str, ar: str) -> None:
        reasons.append({"code": code, "ar": ar, "severity": "hard_block"})

    def warn(code: str, ar: str) -> None:
        warnings.append(code)
        reasons.append({"code": code, "ar": ar, "severity": "warning"})

    if not sym:
        hard("missing_symbol", "لا يوجد رمز صالح للتداول.")
    elif sym in _BAD_SYMBOL_TOKENS or not _SYMBOL_RE.match(sym):
        hard("invalid_symbol_shape", "صيغة الرمز غير صالحة لسهم أمريكي قابل للعرض.")
    elif sym in INACTIVE_SYMBOLS:
        hard("inactive_symbol_denylist", "الرمز في قائمة منع التداول النشط لأنه قديم/مستحوذ عليه/غير نشط.")
    checks["symbol"] = sym

    name = " ".join(_s(row.get(k)) for k in _NAME_KEYS if isinstance(row, dict) and row.get(k)).strip().lower()
    if sym in {"LTHM", "ALTM"} or ("livent" in name and sym in {"LTHM", "ALTM"}):
        hard("old_livent_arcadium_ticker", "رمز Livent/Arcadium قديم أو غير قابل للتداول الحالي؛ لا يظهر في القوائم العملية.")
    elif name and any(p in name for p in _INACTIVE_NAME_PHRASES) and any(p in name for p in ["acquired", "merged", "delisted", "ceased trading", "no longer"]):
        hard("inactive_company_name_text", "اسم/وصف الشركة يشير إلى اندماج أو استحواذ أو توقف تداول.")

    # Explicit active/tradable flags are high-confidence.
    active_flag_seen = False
    active_false = False
    for k in _ACTIVE_FLAG_KEYS:
        if not isinstance(row, dict) or k not in row:
            continue
        val = row.get(k)
        if val in (None, ""):
            continue
        active_flag_seen = True
        if isinstance(val, bool):
            active_false = active_false or (val is False)
        else:
            txt = _s(val).lower()
            if txt in _FALSEY_STRINGS:
                active_false = True
    checks["active_flag_seen"] = active_flag_seen
    if active_false:
        hard("inactive_tradability_flag", "مزود البيانات أو الصف نفسه يعلّم الرمز بأنه غير نشط/غير قابل للتداول.")

    status_text = " ".join(_s(row.get(k)) for k in _STATUS_KEYS if isinstance(row, dict) and row.get(k)).lower()
    checks["status_text"] = status_text[:120]
    if status_text and any(word in status_text for word in _INACTIVE_STATUS_WORDS):
        hard("inactive_status_text", "حالة الرمز تشير إلى delisted/inactive/acquired/merged أو توقف تداول.")

    exchange = _s(_first(row, _EXCHANGE_KEYS, "")).upper()
    checks["exchange"] = exchange
    if exchange and any(x in exchange for x in ["CRYPTO", "FOREX", "INDEX"]):
        hard("non_equity_venue", "مصدر الرمز ليس سهمًا أمريكيًا عمليًا للقوائم الحالية.")
    elif exchange in {"OTC", "OTCQB", "OTCQX", "PINK"}:
        warn("otc_or_pink_venue", "الرمز يبدو OTC/Pink؛ يبقى مراقبة عالية المخاطر ولا يترقى بدون تأكيد إضافي.")

    price = max(_num(row.get(k), 0.0) for k in _PRICE_KEYS if isinstance(row, dict)) if isinstance(row, dict) else 0.0
    volume = max(_num(row.get(k), 0.0) for k in _VOLUME_KEYS if isinstance(row, dict)) if isinstance(row, dict) else 0.0
    dollar_volume = max(_num(row.get(k), 0.0) for k in _DOLLAR_VOLUME_KEYS if isinstance(row, dict)) if isinstance(row, dict) else 0.0
    checks["price"] = round(price, 6) if price else 0.0
    checks["volume"] = round(volume, 0) if volume else 0.0
    checks["dollar_volume"] = round(dollar_volume, 0) if dollar_volume else 0.0

    if strict and price <= 0:
        hard("missing_or_zero_price", "لا يوجد سعر صالح؛ لا يمكن عرض خطة عملية.")
    elif price <= 0:
        warn("missing_or_zero_price", "لا يوجد سعر صالح؛ لا يترقى قبل وصول quote حديث.")

    # Explicit stale timestamp is high-confidence. Missing timestamp is warning
    # only, because many upstream rows omit it while still carrying valid prices.
    parsed_times: list[datetime] = []
    for k in _TIME_KEYS:
        if not isinstance(row, dict) or row.get(k) in (None, ""):
            continue
        dt = _parse_time(row.get(k))
        if dt:
            parsed_times.append(dt)
    latest_dt = max(parsed_times) if parsed_times else None
    if latest_dt:
        age_min = max(0.0, (now - latest_dt).total_seconds() / 60.0)
        checks["latest_time"] = latest_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        checks["age_minutes"] = round(age_min, 1)
        max_age = _max_quote_age_minutes(market_phase)
        if age_min > max_age:
            hard("stale_quote_timestamp", f"آخر وقت بيانات معروف قديم جدًا لهذه الجلسة ({round(age_min, 1)} دقيقة).")
        elif _market_is_activeish(market_phase) and age_min > max_age * 0.65:
            warn("aging_quote_timestamp", "الـ quote ليس حديثًا بما يكفي للتنفيذ؛ يحتاج تحديث سعر قبل الترقية.")
    else:
        checks["latest_time"] = "unknown"
        warn("missing_quote_timestamp", "لا يوجد وقت واضح للسعر؛ يسمح بالمراقبة فقط ولا يترقى بدون تأكيد سعر حي.")

    # Very old source sessions are usually stale snapshots.  Do not block current
    # weekend planning that uses the prior trading day, but block old leftovers.
    source_date_txt = _s(row.get("source_session_date") or row.get("target_trading_date") or row.get("trade_date")) if isinstance(row, dict) else ""
    source_dt = _parse_time(source_date_txt) if source_date_txt else None
    if source_dt:
        source_age_days = max(0.0, (now.date() - source_dt.date()).days)
        checks["source_date"] = source_dt.date().isoformat()
        checks["source_age_days"] = source_age_days
        if source_age_days > 14:
            hard("old_source_session", "مصدر الصف قديم جدًا؛ غالبًا بقايا snapshot أو ذاكرة قديمة.")
        elif source_age_days > 5:
            warn("aging_source_session", "مصدر الصف قديم نسبيًا؛ يحتاج live confirmation قبل الظهور العملي.")

    hard_reasons = [r for r in reasons if r.get("severity") == "hard_block"]
    visible_allowed = not hard_reasons
    actionable_allowed = visible_allowed and not any(w in warnings for w in ["missing_or_zero_price", "missing_quote_timestamp", "aging_quote_timestamp", "otc_or_pink_venue"])
    if strict and warnings:
        actionable_allowed = False

    primary = hard_reasons[0] if hard_reasons else (reasons[0] if reasons else {"code": "ok", "ar": "الرمز اجتاز بوابة التداول النشط.", "severity": "ok"})
    return {
        "ok": bool(visible_allowed),
        "version": ACTIVE_TRADABILITY_GATE_VERSION,
        "symbol": sym,
        "visible_allowed": bool(visible_allowed),
        "actionable_allowed": bool(actionable_allowed),
        "severity": primary.get("severity", "ok"),
        "reason_code": primary.get("code", "ok"),
        "reason_ar": primary.get("ar", "الرمز اجتاز بوابة التداول النشط."),
        "warnings": warnings[:8],
        "reasons": reasons[:12],
        "checks": checks,
        "rule_ar": "تمنع هذه البوابة الرموز غير النشطة/stale/delisted/merged من الظهور العملي، وتسمح بالتحذير فقط عند نقص دليل غير قاتل.",
    }


def enrich_row(row: dict, *, market_phase: str = "") -> dict:
    item = dict(row or {})
    audit = audit_row(item, market_phase=market_phase)
    item["active_tradability_gate_v2w14"] = audit
    item["active_tradability_ok"] = bool(audit.get("visible_allowed"))
    item["active_tradability_actionable_ok"] = bool(audit.get("actionable_allowed"))
    if not audit.get("visible_allowed"):
        item["inactive_tradability_reason_v2w14"] = audit.get("reason_code")
        item["inactive_tradability_reason_ar_v2w14"] = audit.get("reason_ar")
        item["blocked_learning_only"] = True
    elif audit.get("warnings"):
        item["active_tradability_warning_v2w14"] = audit.get("warnings")
    return item


def enrich_rows(rows: list[dict] | None, *, market_phase: str = "") -> list[dict]:
    return [enrich_row(r, market_phase=market_phase) if isinstance(r, dict) else r for r in (rows or [])]


def summarize_rows(rows: list[dict] | None, *, market_phase: str = "", limit: int = 80) -> dict[str, Any]:
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    blocked: list[dict] = []
    warnings: list[dict] = []
    reason_counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    actionable_blocked_count = 0
    for r in rows:
        audit = r.get("active_tradability_gate_v2w14") if isinstance(r.get("active_tradability_gate_v2w14"), dict) else audit_row(r, market_phase=market_phase)
        code = _s(audit.get("reason_code") or "ok")
        if not audit.get("visible_allowed"):
            reason_counts[code] = int(reason_counts.get(code, 0) or 0) + 1
            if len(blocked) < limit:
                blocked.append({
                    "symbol": audit.get("symbol"),
                    "reason_code": code,
                    "reason_ar": audit.get("reason_ar"),
                    "checks": audit.get("checks", {}),
                })
        elif not audit.get("actionable_allowed"):
            actionable_blocked_count += 1
        for w in audit.get("warnings") or []:
            warning_counts[_s(w)] = int(warning_counts.get(_s(w), 0) or 0) + 1
            if len(warnings) < limit:
                warnings.append({"symbol": audit.get("symbol"), "warning": _s(w), "reason_ar": audit.get("reason_ar")})
    return {
        "ok": True,
        "version": ACTIVE_TRADABILITY_GATE_VERSION,
        "rows_checked": len(rows),
        "visible_blocked_count": len(blocked) if len(blocked) < limit else sum(reason_counts.values()),
        "actionable_blocked_warning_count": actionable_blocked_count,
        "reason_counts": sorted([{"reason_code": k, "count": v} for k, v in reason_counts.items()], key=lambda x: -x["count"]),
        "warning_counts": sorted([{"warning": k, "count": v} for k, v in warning_counts.items()], key=lambda x: -x["count"]),
        "blocked_sample": blocked[:limit],
        "warning_sample": warnings[:limit],
        "rule_ar": "Hard blocks لا تظهر في القوائم العملية. التحذيرات تبقى مراقبة فقط ولا تترقى إلى شراء حتى يصل تأكيد سعر/جلسة.",
    }
