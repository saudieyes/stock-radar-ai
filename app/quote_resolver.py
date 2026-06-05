"""Quote Resolver V1.

One place to apply the agreed price-source rule:
FMP live/extended first; if missing or incomplete, Polygon snapshot/minute fallback;
if Polygon is used during active/pre/after-market, label it as delayed and
monitoring-only.  The module does not make trading decisions; it only returns a
clean quote contract and can safely overlay that contract on an existing row.
"""
from __future__ import annotations

from typing import Any

from .live_quotes import get_live_quotes
from .utils import safe_round, to_float

QUOTE_RESOLVER_VERSION = "quote_resolver_v1_fmp_then_polygon_delayed_2026_06_05"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace("$", "").replace(",", "").replace("%", "").strip()
            if not v:
                return default
        return float(v)
    except Exception:
        return default


def _active_phase(phase: str) -> bool:
    return str(phase or "").lower() in {"open", "pre_market", "premarket", "after_hours", "afterhours"}


def _is_polygon_source(source: str, label: str = "") -> bool:
    text = f"{source} {label}".lower()
    return any(x in text for x in ["polygon", "snapshot", "minute+snapshot", "minute"])


def normalize_quote(symbol: str, quote: dict | None, phase: str = "") -> dict:
    symbol = _s(symbol).upper()
    quote = quote or {}
    source = _s(quote.get("source")) or "unknown"
    source_label = _s(quote.get("source_label")) or source
    price = _f(quote.get("price"), 0.0)
    previous_close = _f(quote.get("previous_close"), 0.0)
    change = _f(quote.get("change_pct"), 0.0)
    change_reliable = bool(quote.get("change_pct_reliable", True))
    if previous_close > 0 and price > 0 and (not change_reliable or quote.get("change_pct") in {None, ""}):
        change = ((price - previous_close) / previous_close) * 100.0
        change_reliable = True
    polygon_delayed = bool(_is_polygon_source(source, source_label) and _active_phase(phase or _s(quote.get("market_phase"))))
    reliable = bool(source and price > 0 and change_reliable and not polygon_delayed and not _is_polygon_source(source, source_label))
    if str(source).startswith("fmp"):
        reliable = bool(price > 0 and change_reliable)
    if polygon_delayed:
        source_label = f"{source_label} — متأخر تقريبًا 15 دقيقة"
    missing: list[str] = []
    if price <= 0:
        missing.append("السعر غير متوفر")
    if not change_reliable:
        missing.append("نسبة التغير غير متوفرة")
    if not source or source == "unknown":
        missing.append("مصدر السعر غير محدد")
    return {
        "version": QUOTE_RESOLVER_VERSION,
        "symbol": symbol,
        "available": bool(price > 0),
        "complete": bool(price > 0 and change_reliable and source and source != "unknown"),
        "price": safe_round(price, 4),
        "previous_close": safe_round(previous_close, 4),
        "change_pct": safe_round(change, 3),
        "change_available": bool(change_reliable),
        "source": source,
        "source_label": source_label,
        "delayed": bool(polygon_delayed),
        "reliable_for_execution": bool(reliable),
        "monitoring_only": bool(polygon_delayed or not reliable),
        "updated_label": _s(quote.get("updated_label")),
        "updated_at": _f(quote.get("updated_at"), 0.0),
        "market_phase": phase or _s(quote.get("market_phase")),
        "volume": safe_round(_f(quote.get("volume"), 0.0)),
        "missing": missing,
    }


def resolve_symbol_quote(symbol: str, phase: str = "", prefer_cache: bool = False, allow_fallback: bool = True) -> dict:
    """Fetch one symbol through get_live_quotes and return a clean quote contract."""
    sym = _s(symbol).upper()
    try:
        bundle = get_live_quotes([sym], prefer_cache=bool(prefer_cache), allow_fallback=bool(allow_fallback))
        quotes = bundle.get("quotes", {}) if isinstance(bundle, dict) else {}
        quote = (quotes or {}).get(sym) or {}
        contract = normalize_quote(sym, quote, phase=phase)
        contract["bundle_diagnostics"] = bundle.get("diagnostics", {}) if isinstance(bundle, dict) else {}
        return contract
    except Exception as exc:
        return {
            "version": QUOTE_RESOLVER_VERSION,
            "symbol": sym,
            "available": False,
            "complete": False,
            "price": 0.0,
            "previous_close": 0.0,
            "change_pct": 0.0,
            "change_available": False,
            "source": "error",
            "source_label": "فشل مصدر السعر",
            "delayed": False,
            "reliable_for_execution": False,
            "monitoring_only": True,
            "updated_label": "",
            "market_phase": phase,
            "volume": 0.0,
            "missing": [f"فشل جلب السعر: {type(exc).__name__}"],
        }


def overlay_quote_contract(row: dict, quote_contract: dict | None) -> dict:
    """Copy quote contract fields onto a row without modifying entry/target/stop."""
    out = dict(row or {})
    qc = quote_contract or {}
    if not qc or not qc.get("available"):
        out["quote_resolver_contract"] = qc
        return out
    price = _f(qc.get("price"), 0.0)
    prev = _f(qc.get("previous_close"), 0.0)
    change = _f(qc.get("change_pct"), 0.0)
    source = _s(qc.get("source"))
    source_label = _s(qc.get("source_label"))
    if price > 0:
        out["current_price_live"] = safe_round(price, 4)
        out["display_price"] = safe_round(price, 4)
        out["live_price_available"] = True
        out["display_price_label"] = "السعر الحالي" if not qc.get("delayed") else "سعر Polygon المتأخر"
    if prev > 0:
        out["previous_close_live"] = safe_round(prev, 4)
    if qc.get("change_available"):
        out["display_change_pct"] = safe_round(change, 3)
        out["change_vs_prev_close_pct"] = safe_round(change, 3)
        out["display_change_available"] = True
    else:
        out["display_change_available"] = False
    out["price_source"] = source
    out["price_source_label"] = source_label
    out["price_source_delayed"] = bool(qc.get("delayed"))
    out["price_reliable_for_execution"] = bool(qc.get("reliable_for_execution"))
    out["price_monitoring_only"] = bool(qc.get("monitoring_only"))
    out["quote_resolver_version"] = QUOTE_RESOLVER_VERSION
    out["quote_resolver_contract"] = qc
    if qc.get("updated_label"):
        out["last_price_update_label"] = _s(qc.get("updated_label"))
    if qc.get("volume"):
        out["volume_live"] = safe_round(qc.get("volume"))
    if qc.get("delayed"):
        flags = list(out.get("risk_flags") or []) if isinstance(out.get("risk_flags"), list) else []
        note = "السعر من Polygon متأخر تقريبًا 15 دقيقة — مراقبة فقط وليس تنفيذًا مباشرًا"
        if note not in flags:
            flags.append(note)
        out["risk_flags"] = flags
        out["owner_action"] = out.get("owner_action") or "👀 سعر Polygon متأخر — استخدمه للمراقبة فقط حتى يعود FMP مباشر."
    return out
