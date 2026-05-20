"""Source/Promotion audit layer for Stock Radar AI.

This module is read-only. It does not change scoring, Sharia filtering, ranking, or
signals. It answers the user's core diagnostic questions:

1) Why did a symbol enter the source/universe?
2) Why was it promoted from source to Strong/Cautious/Watch, or not promoted?
3) Are there cleaner alternatives with similar technical strength but lower risk?

The reports intentionally combine the latest radar snapshot with Evidence V2
snapshots where available, so we can compare source/promotion choices with
pre-market/regular-session behavior, liquidity, gaps, resistance/high proximity,
and no-chase risks.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .sqlite_store import SQLITE_DB_PATH, SQLITE_ENABLED, get_json
from .utils import safe_round, to_float


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SQLITE_DB_PATH), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=15000")
    except Exception:
        pass
    return conn


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str):
            txt = value.replace("%", "").replace(",", "").strip()
            if not txt:
                return default
            return float(txt)
        return float(value)
    except Exception:
        return default


def _s(value: Any) -> str:
    return str(value or "").strip()


def _clean_symbol(value: Any) -> str:
    sym = str(value or "").upper().strip().replace(" ", "")
    if not sym:
        return ""
    if not all(ch.isalnum() or ch in {".", "-"} for ch in sym):
        return ""
    return sym[:24]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x or "").strip()]
    if isinstance(value, tuple):
        return [str(x).strip() for x in value if str(x or "").strip()]
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return []
        if txt.startswith("["):
            try:
                loaded = json.loads(txt)
                if isinstance(loaded, list):
                    return [str(x).strip() for x in loaded if str(x or "").strip()]
            except Exception:
                pass
        return [x.strip() for x in txt.replace(",", "|").split("|") if x.strip()]
    return []


def _first_text(row: dict, keys: list[str]) -> str:
    for key in keys:
        val = (row or {}).get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _first_float(row: dict, keys: list[str]) -> float:
    for key in keys:
        val = _f((row or {}).get(key), 0.0)
        if val:
            return val
    return 0.0


def _pct_distance(price: float, level: float) -> float:
    try:
        price = float(price or 0)
        level = float(level or 0)
        if price <= 0 or level <= 0:
            return 0.0
        return safe_round(((level - price) / price) * 100.0, 2)
    except Exception:
        return 0.0


def _latest_tool_rows() -> list[dict]:
    snap = get_json("last_trade_scan_snapshot", {})
    rows = snap.get("rows", []) if isinstance(snap, dict) else []
    return rows if isinstance(rows, list) else []


def _row_symbol(row: dict) -> str:
    return _clean_symbol((row or {}).get("symbol"))


def _row_decision(row: dict) -> str:
    return _first_text(row, ["decision", "signal_label", "display_decision"])


def _row_bucket(row: dict) -> str:
    decision = _row_decision(row)
    if decision == "دخول قوي":
        return "strong"
    if decision == "دخول بحذر":
        return "cautious"
    if "رمادي" in _s((row or {}).get("sharia_label")) or "gray" in _s((row or {}).get("sharia_status")).lower():
        return "gray_or_unresolved"
    if decision:
        return "watch_or_other"
    return "unknown"


def _evidence_latest_by_symbol(week_key: str = "", trade_date: str = "") -> dict[str, dict]:
    if not SQLITE_ENABLED:
        return {}
    where = []
    args: list[Any] = []
    if week_key:
        where.append("week_key=?")
        args.append(str(week_key))
    if trade_date:
        where.append("trade_date=?")
        args.append(str(trade_date)[:10])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        with _connect() as conn:
            # max(id) is reliable enough because snapshots are appended over time.
            rows = conn.execute(
                f"""
                SELECT es.* FROM evidence_snapshots es
                INNER JOIN (
                    SELECT symbol, MAX(id) AS max_id FROM evidence_snapshots {where_sql} GROUP BY symbol
                ) latest ON es.symbol=latest.symbol AND es.id=latest.max_id
                """,
                tuple(args),
            ).fetchall()
        return {str(r["symbol"]): dict(r) for r in rows or []}
    except Exception:
        return {}


def _risk_tags(row: dict, ev: dict | None = None) -> list[str]:
    tags = []
    for key in ["risk_tags", "risk_flags", "pattern_risk_reasons", "structure_guard_reasons"]:
        tags.extend(_as_list((row or {}).get(key)))
    if ev:
        tags.extend(_as_list(ev.get("risk_tags_json")))
    # de-duplicate preserving order
    out = []
    for tag in tags:
        if tag and tag not in out:
            out.append(tag)
    return out


def _success_tags(row: dict, ev: dict | None = None) -> list[str]:
    tags = []
    for key in ["success_tags", "winner_pattern_reasons", "strong_entry_tier_reasons"]:
        tags.extend(_as_list((row or {}).get(key)))
    if ev:
        tags.extend(_as_list(ev.get("success_tags_json")))
    out = []
    for tag in tags:
        if tag and tag not in out:
            out.append(tag)
    return out


def _quality(row: dict) -> float:
    return _first_float(row, ["quality_score", "core_quality_score", "display_rank_score", "live_rank_score"])


def _liquidity_score(row: dict, ev: dict | None = None) -> float:
    return _first_float(row, ["liquidity_persistence_score", "volume_score", "effective_volume_ratio", "volume_ratio"]) or _f((ev or {}).get("liquidity_score"), 0)


def _price(row: dict, ev: dict | None = None) -> float:
    return _first_float(row, ["current_price_live", "display_price", "live_price", "price", "current_price"]) or _f((ev or {}).get("price"), 0)


def _resistance_distance(row: dict, ev: dict | None = None) -> float:
    p = _price(row, ev)
    direct = _first_float(row, ["nearest_resistance_distance_pct", "distance_to_resistance_pct"])
    if direct:
        return direct
    return _pct_distance(p, _first_float(row, ["nearest_resistance", "resistance_price", "major_resistance"]) or _f((ev or {}).get("resistance_price"), 0))


def _support_distance(row: dict, ev: dict | None = None) -> float:
    p = _price(row, ev)
    direct = _first_float(row, ["nearest_support_distance_pct", "distance_to_support_pct"])
    if direct:
        return direct
    # negative means support below price. We return absolute practical distance.
    val = _pct_distance(p, _first_float(row, ["nearest_support", "support_price"]) or _f((ev or {}).get("support_price"), 0))
    return abs(val) if val else 0.0


def _near_high(row: dict, tags: list[str]) -> bool:
    dist_52 = abs(_first_float(row, ["distance_to_52w_high_pct", "distance_to_year_high_pct"]))
    dist_ath = abs(_first_float(row, ["distance_to_ath_pct", "distance_to_all_time_high_pct"]))
    return any("قمة سنوية" in t or "قمة تاريخية" in t for t in tags) or (0 < dist_52 <= 3) or (0 < dist_ath <= 3)


def _source_reasons(row: dict, ev: dict | None, tags: list[str]) -> list[str]:
    reasons = []
    for key in ["source_reason", "special_bucket_reason", "live_rank_reason", "quick_explainer", "ai_summary"]:
        txt = _s((row or {}).get(key))
        if txt and txt not in reasons:
            reasons.append(txt[:220])
    if ev and ev.get("in_big_movers"):
        reasons.append("ظهر ضمن الرابحين/المتحركين الكبار في Evidence")
    if ev and ev.get("source_group"):
        reasons.append(f"مصدر Evidence: {ev.get('source_group')}")
    if tags:
        reasons.append("عوامل مخاطرة/سياق: " + "، ".join(tags[:4]))
    return reasons[:7]


def _candidate_profile(row: dict, ev: dict | None = None) -> dict:
    sym = _row_symbol(row) or _clean_symbol((ev or {}).get("symbol"))
    tags = _risk_tags(row, ev)
    success = _success_tags(row, ev)
    q = _quality(row)
    liq = _liquidity_score(row, ev)
    res = _resistance_distance(row, ev)
    sup = _support_distance(row, ev)
    near_high = _near_high(row, tags)
    no_chase = _s((row or {}).get("no_chase_guard_status")) == "no_chase" or bool((ev or {}).get("no_chase_flag"))
    tier = _s((row or {}).get("strong_entry_tier"))
    pattern_score = _first_float(row, ["pattern_risk_score"]) or _f((ev or {}).get("pattern_risk_score"), 0)
    clean_score = 100.0
    clean_score -= min(pattern_score * 0.35, 35)
    clean_score -= 22 if no_chase else 0
    clean_score -= 18 if near_high else 0
    # V2: a stock is not a clean alternative if it is sitting directly under
    # resistance. It can still be technically strong, but it must be treated as
    # breakout-confirmation / no-chase, not as clean.
    resistance_guard_status = "clear"
    if res and res <= 0.75:
        clean_score -= 45
        resistance_guard_status = "blocked_until_breakout_confirmed"
    elif res and res <= 1.5:
        clean_score -= 30
        resistance_guard_status = "requires_breakout_confirmation"
    elif res and res <= 2.5:
        clean_score -= 15
        resistance_guard_status = "watch_resistance"
    if liq and liq < 55:
        clean_score -= 18
    if any("السيولة لم تستمر" in t for t in tags):
        clean_score -= 16
    if any("كسر الدعم" in t for t in tags):
        clean_score -= 18
    if any("قريب من مقاومة قوية" in t for t in tags):
        clean_score -= 18
    clean_score = max(0.0, min(100.0, safe_round(clean_score, 1)))
    if resistance_guard_status == "blocked_until_breakout_confirmed" and q >= 70:
        cleanliness = "technical_but_risky"
        label = "⚠️ قريب جدًا من مقاومة — يحتاج اختراق وثبات"
    elif clean_score >= 75 and q >= 70:
        cleanliness = "clean_candidate"
        label = "✅ بديل نظيف نسبيًا"
    elif clean_score >= 55:
        cleanliness = "acceptable_candidate"
        label = "🟡 مقبول مع متابعة"
    elif q >= 70:
        cleanliness = "technical_but_risky"
        label = "⚠️ قوي فنيًا لكن مخاطره أعلى"
    else:
        cleanliness = "weak_or_unclear"
        label = "⚪ غير واضح/ضعيف"
    return {
        "symbol": sym,
        "decision": _row_decision(row),
        "bucket": _row_bucket(row),
        "quality_score": safe_round(q, 1),
        "execution_readiness_score": _first_float(row, ["execution_readiness_score"]),
        "display_rank_score": _first_float(row, ["display_rank_score"]),
        "signal_strength_score": _first_float(row, ["signal_strength_score"]),
        "liquidity_score": safe_round(liq, 1),
        "resistance_distance_pct": safe_round(res, 2),
        "resistance_guard_status": resistance_guard_status,
        "support_distance_pct": safe_round(sup, 2),
        "near_year_or_ath_high": bool(near_high),
        "no_chase": bool(no_chase),
        "strong_entry_tier": tier,
        "pattern_risk_score": safe_round(pattern_score, 1),
        "clean_score": clean_score,
        "cleanliness": cleanliness,
        "cleanliness_label": label,
        "risk_tags": tags[:10],
        "success_tags": success[:10],
        "source_reasons": _source_reasons(row, ev, tags),
        "evidence_seen": bool(ev),
        "evidence_source_group": (ev or {}).get("source_group", "") if ev else "",
        "evidence_session": (ev or {}).get("session", "") if ev else "",
        "evidence_change_pct": _f((ev or {}).get("change_pct"), 0),
    }


def _collect_current_profiles(week_key: str = "", trade_date: str = "") -> list[dict]:
    rows = _latest_tool_rows()
    evmap = _evidence_latest_by_symbol(week_key=week_key, trade_date=trade_date)
    profiles: list[dict] = []
    seen = set()
    for row in rows:
        sym = _row_symbol(row)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        profiles.append(_candidate_profile(row, evmap.get(sym)))
    # Include Evidence-only symbols, mostly big winners, so clean alternatives and source audits can see them too.
    for sym, ev in evmap.items():
        if sym in seen:
            continue
        seen.add(sym)
        profiles.append(_candidate_profile({"symbol": sym}, ev))
    return profiles


def _summarize_profiles(profiles: list[dict]) -> dict:
    by_bucket: dict[str, int] = {}
    by_clean: dict[str, int] = {}
    high_risk = []
    clean = []
    for p in profiles:
        by_bucket[p.get("bucket") or "unknown"] = by_bucket.get(p.get("bucket") or "unknown", 0) + 1
        by_clean[p.get("cleanliness") or "unknown"] = by_clean.get(p.get("cleanliness") or "unknown", 0) + 1
        if p.get("cleanliness") == "technical_but_risky" or p.get("no_chase") or p.get("near_year_or_ath_high"):
            high_risk.append(p)
        if p.get("cleanliness") in {"clean_candidate", "acceptable_candidate"}:
            clean.append(p)
    return {
        "total_candidates": len(profiles),
        "by_bucket": by_bucket,
        "by_cleanliness": by_clean,
        "high_risk_count": len(high_risk),
        "clean_or_acceptable_count": len(clean),
    }


def build_source_entry_audit(week_key: str | None = None, trade_date: str | None = None, format: str = "json", limit: int = 80) -> dict | str:
    profiles = _collect_current_profiles(week_key=str(week_key or ""), trade_date=str(trade_date or ""))
    profiles_sorted = sorted(profiles, key=lambda x: (x.get("evidence_seen"), x.get("quality_score", 0), x.get("clean_score", 0)), reverse=True)
    result = {
        "ok": True,
        "version": "source_entry_audit_v1_read_only",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "week_key": week_key or "",
        "trade_date": trade_date or "",
        "summary": _summarize_profiles(profiles),
        "items": profiles_sorted[: max(1, min(int(limit or 80), 300))],
        "notes": "تشخيص فقط: يوضح لماذا ظهر السهم في المنبع/اللقطات، ولا يغيّر الترتيب أو السكور.",
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        s = result["summary"]
        lines = [
            "تقرير Source Entry Audit V1",
            f"إجمالي المرشحين/اللقطات: {s.get('total_candidates', 0)}",
            f"مرشحون نظيفون/مقبولون: {s.get('clean_or_acceptable_count', 0)}",
            f"قوي فنيًا لكن عالي المخاطرة: {s.get('high_risk_count', 0)}",
            "",
            "أمثلة مهمة:",
        ]
        for p in result["items"][:20]:
            lines.append(f"- {p['symbol']}: {p.get('decision') or p.get('bucket')} | جودة {p.get('quality_score')} | نظافة {p.get('clean_score')}/100 | {p.get('cleanliness_label')}")
        return "\n".join(lines)
    return result


def build_promotion_audit(week_key: str | None = None, trade_date: str | None = None, format: str = "json", limit: int = 120) -> dict | str:
    profiles = _collect_current_profiles(week_key=str(week_key or ""), trade_date=str(trade_date or ""))
    promoted = [p for p in profiles if p.get("bucket") in {"strong", "cautious"}]
    watch = [p for p in profiles if p.get("bucket") not in {"strong", "cautious"}]
    risky_promoted = [p for p in promoted if p.get("cleanliness") == "technical_but_risky" or p.get("no_chase") or (p.get("resistance_distance_pct") and p.get("resistance_distance_pct") <= 1.2)]
    clean_watch = [p for p in watch if p.get("cleanliness") in {"clean_candidate", "acceptable_candidate"} and p.get("quality_score", 0) >= 65]
    result = {
        "ok": True,
        "version": "promotion_audit_v1_read_only",
        "summary": {
            "promoted_count": len(promoted),
            "risky_promoted_count": len(risky_promoted),
            "watch_or_unpromoted_count": len(watch),
            "clean_watch_or_unpromoted_count": len(clean_watch),
        },
        "risky_promoted": sorted(risky_promoted, key=lambda x: (x.get("quality_score", 0), -x.get("clean_score", 0)), reverse=True)[: max(1, min(int(limit or 120), 300))],
        "clean_not_promoted_or_watch": sorted(clean_watch, key=lambda x: (x.get("clean_score", 0), x.get("quality_score", 0)), reverse=True)[: max(1, min(int(limit or 120), 300))],
        "notes": "يعرض ما إذا كانت الترقية تُقدّم أسهمًا عالية المخاطرة بينما توجد أسماء أنظف لم تُرقَّ. لا يغير القرار.",
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        s = result["summary"]
        lines = [
            "تقرير Promotion Audit V1",
            f"المرقّى Strong/Cautious: {s['promoted_count']}",
            f"مرقّى لكنه عالي المخاطرة: {s['risky_promoted_count']}",
            f"نظيف/مقبول لكنه غير مرقّى أو Watch: {s['clean_watch_or_unpromoted_count']}",
            "",
            "أسماء مرقّاة عالية المخاطرة:",
        ]
        for p in result["risky_promoted"][:15]:
            lines.append(f"- {p['symbol']}: {p.get('decision')} | جودة {p.get('quality_score')} | نظافة {p.get('clean_score')}/100 | مقاومة {p.get('resistance_distance_pct')}% | سيولة {p.get('liquidity_score')}")
        lines.append("")
        lines.append("بدائل أنظف لم تظهر كأولوية كافية:")
        for p in result["clean_not_promoted_or_watch"][:15]:
            lines.append(f"- {p['symbol']}: {p.get('decision') or p.get('bucket')} | جودة {p.get('quality_score')} | نظافة {p.get('clean_score')}/100")
        return "\n".join(lines)
    return result


def build_clean_alternatives(symbol: str = "", week_key: str | None = None, trade_date: str | None = None, format: str = "json", limit: int = 30) -> dict | str:
    profiles = _collect_current_profiles(week_key=str(week_key or ""), trade_date=str(trade_date or ""))
    sym = _clean_symbol(symbol)
    target = None
    if sym:
        for p in profiles:
            if p.get("symbol") == sym:
                target = p
                break
    q_floor = max(60.0, _f((target or {}).get("quality_score"), 75.0) - 10.0) if target else 70.0
    alternatives = [
        p for p in profiles
        if (not sym or p.get("symbol") != sym)
        and p.get("quality_score", 0) >= q_floor
        and p.get("cleanliness") in {"clean_candidate", "acceptable_candidate"}
    ]
    alternatives = sorted(alternatives, key=lambda x: (x.get("clean_score", 0), x.get("liquidity_score", 0), x.get("quality_score", 0)), reverse=True)
    result = {
        "ok": True,
        "version": "clean_alternatives_v1_read_only",
        "symbol": sym,
        "target": target,
        "quality_floor_used": safe_round(q_floor, 1),
        "alternatives_count": len(alternatives),
        "alternatives": alternatives[: max(1, min(int(limit or 30), 200))],
        "interpretation": "إذا وُجدت بدائل أنظف بجودة قريبة، لا ينبغي أن يتصدر سهم عالي المخاطرة إلا كفرصة تحتاج تأكيد.",
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        lines = ["تقرير Clean Alternatives Check V1"]
        if target:
            lines.append(f"السهم المرجعي: {target['symbol']} | جودة {target.get('quality_score')} | نظافة {target.get('clean_score')}/100 | {target.get('cleanliness_label')}")
        else:
            lines.append("بدون سهم مرجعي: عرض أفضل البدائل الأنظف من آخر لقطة.")
        lines.append(f"عدد البدائل الأنظف بجودة قريبة: {len(alternatives)}")
        for p in alternatives[:20]:
            lines.append(f"- {p['symbol']}: {p.get('decision') or p.get('bucket')} | جودة {p.get('quality_score')} | نظافة {p.get('clean_score')}/100 | مقاومة {p.get('resistance_distance_pct')}% | سيولة {p.get('liquidity_score')}")
        return "\n".join(lines)
    return result

# ---------------------------------------------------------------------------
# V3a - Source Freshness / Discovery Coverage
# ---------------------------------------------------------------------------
# This answers the user's current question: are there opportunities outside the
# displayed radar / active source that deserve attention?  It is diagnostic-only
# and does not change source selection, promotion, ranking, Sharia, or scoring.


def _json_load(value: Any, default: Any = None) -> Any:
    if default is None:
        default = []
    try:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        txt = str(value or "").strip()
        if not txt:
            return default
        return json.loads(txt)
    except Exception:
        return default


def _latest_week_from_tables() -> str:
    if not SQLITE_ENABLED:
        return ""
    try:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT week_key FROM (
                    SELECT week_key, COALESCE(MAX(updated_ts),0) AS ts FROM missed_source_candidates GROUP BY week_key
                    UNION ALL
                    SELECT week_key, COALESCE(MAX(updated_at),0) AS ts FROM evidence_winner_profiles GROUP BY week_key
                ) x
                WHERE week_key!=''
                ORDER BY ts DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["week_key"] or "") if row else ""
    except Exception:
        return ""


def _source_candidate_rows_from_db(week_key: str = "", limit: int = 1600) -> list[dict]:
    if not SQLITE_ENABLED:
        return []
    wk = str(week_key or "").strip() or _latest_week_from_tables()
    where = "WHERE week_key=?" if wk else ""
    args: list[Any] = [wk] if wk else []
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM missed_source_candidates
                {where}
                ORDER BY discovery_rank ASC, source_score DESC, updated_ts DESC
                LIMIT ?
                """,
                tuple(args + [max(50, min(int(limit or 1600), 5000))]),
            ).fetchall()
        out = []
        for r in rows or []:
            d = dict(r)
            d["source_reasons"] = _json_load(d.get("source_reasons_json"), [])
            d["source_tags"] = _json_load(d.get("source_tags_json"), [])
            d["metrics"] = _json_load(d.get("metrics_json"), {})
            out.append(d)
        return out
    except Exception:
        return []


def _winner_profile_rows_from_db(week_key: str = "", trade_date: str = "", limit: int = 1200) -> list[dict]:
    if not SQLITE_ENABLED:
        return []
    wk = str(week_key or "").strip() or _latest_week_from_tables()
    td = str(trade_date or "").strip()[:10]
    where = []
    args: list[Any] = []
    if wk:
        where.append("week_key=?")
        args.append(wk)
    if td:
        where.append("trade_date=?")
        args.append(td)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM evidence_winner_profiles
                {where_sql}
                ORDER BY trade_date DESC, winner_change_pct DESC
                LIMIT ?
                """,
                tuple(args + [max(50, min(int(limit or 1200), 5000))]),
            ).fetchall()
        out = []
        for r in rows or []:
            d = dict(r)
            d["profile"] = _json_load(d.get("profile_json"), {})
            out.append(d)
        return out
    except Exception:
        return []


def _scanner_source_diagnostics() -> dict:
    try:
        import scanner as _scanner  # local import avoids startup coupling
        return dict(getattr(_scanner, "LAST_SOURCE_DIAGNOSTICS", {}) or {})
    except Exception:
        return {}


def _radar_symbol_set() -> set[str]:
    return {p.get("symbol") for p in _collect_current_profiles() if p.get("symbol")}


def _diag_ranked_candidates(diag: dict) -> list[dict]:
    rows = diag.get("ranked_candidates") or diag.get("candidate_rows") or []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _candidate_from_diag_row(row: dict, rank: int = 0) -> dict:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    tags = row.get("sources") if isinstance(row.get("sources"), list) else []
    change = _first_float(metrics, ["live_change_pct", "fmp_change_pct", "day_change_pct", "change_pct"])
    return {
        "symbol": _clean_symbol(row.get("symbol")),
        "candidate_stage": "live_discovery_diagnostics",
        "discovery_rank": int(rank or 0),
        "source_score": _f(row.get("score"), 0),
        "change_pct": change,
        "volume": _first_float(metrics, ["live_volume", "fmp_volume", "volume"]),
        "dollar_volume": _first_float(metrics, ["dollar_volume"]),
        "price": _first_float(metrics, ["live_price", "fmp_price", "price"]),
        "source_tags": tags,
        "source_reasons": row.get("reasons") if isinstance(row.get("reasons"), list) else [],
        "metrics": metrics,
        "from_diagnostics": True,
    }


def _candidate_from_source_db(row: dict) -> dict:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    return {
        "symbol": _clean_symbol(row.get("symbol")),
        "candidate_stage": str(row.get("candidate_stage") or "source_db"),
        "discovery_rank": int(_f(row.get("discovery_rank"), 0)),
        "source_score": _f(row.get("source_score"), 0),
        "change_pct": _f(row.get("change_pct"), 0),
        "volume": _f(row.get("volume"), 0),
        "dollar_volume": _f(row.get("dollar_volume"), 0),
        "price": _f(row.get("price"), 0),
        "source_tags": row.get("source_tags") or [],
        "source_reasons": row.get("source_reasons") or [],
        "metrics": metrics,
        "sharia_status": str(row.get("sharia_status") or ""),
        "sharia_label": str(row.get("sharia_label") or ""),
        "sharia_action": str(row.get("sharia_action") or ""),
        "from_source_db": True,
    }


def _candidate_hot_score(c: dict) -> float:
    score = 0.0
    source_score = _f(c.get("source_score"), 0)
    change = abs(_f(c.get("change_pct"), 0))
    dollar = _f(c.get("dollar_volume"), 0)
    rank = int(_f(c.get("discovery_rank"), 9999) or 9999)
    tags = set(str(x) for x in (c.get("source_tags") or []))
    score += min(max(source_score, 0) / 2.0, 55)
    score += min(change * 2.0, 35)
    score += min(dollar / 20_000_000, 22)
    if rank and rank <= 50:
        score += 18
    elif rank <= 150:
        score += 10
    if tags & {"fmp_movers", "fmp_live_confirmed", "top_mover", "runner", "volume_spike", "live_mover"}:
        score += 20
    if str(c.get("sharia_action") or "").lower() in {"block", "blocked", "reject"}:
        score -= 35
    return safe_round(max(0.0, min(100.0, score)), 1)


def _compact_candidate(c: dict) -> dict:
    return {
        "symbol": c.get("symbol"),
        "stage": c.get("candidate_stage", ""),
        "rank": c.get("discovery_rank", 0),
        "source_score": safe_round(c.get("source_score", 0), 2),
        "hot_score": _candidate_hot_score(c),
        "change_pct": safe_round(c.get("change_pct", 0), 2),
        "price": safe_round(c.get("price", 0), 3),
        "dollar_volume": safe_round(c.get("dollar_volume", 0), 0),
        "source_tags": list(c.get("source_tags") or [])[:8],
        "source_reasons": list(c.get("source_reasons") or [])[:5],
        "sharia_status": c.get("sharia_status", ""),
        "sharia_label": c.get("sharia_label", ""),
        "sharia_action": c.get("sharia_action", ""),
    }


def _compact_winner(w: dict) -> dict:
    return {
        "symbol": _clean_symbol(w.get("symbol")),
        "trade_date": str(w.get("trade_date") or ""),
        "winner_change_pct": safe_round(w.get("winner_change_pct", 0), 2),
        "gap_pct": safe_round(w.get("gap_pct", 0), 2),
        "likely_pattern": str(w.get("likely_pattern") or ""),
        "move_quality_label": str(w.get("move_quality_label") or ""),
        "source_seen": bool(w.get("source_seen")),
        "tool_seen": bool(w.get("tool_seen")),
        "tool_stage": str(w.get("tool_stage") or ""),
        "tool_first_seen_change_pct": safe_round(w.get("tool_first_seen_change_pct", 0), 2),
        "liquidity_acceleration_score": safe_round(w.get("liquidity_acceleration_score", 0), 1),
        "liquidity_persistence_score": safe_round(w.get("liquidity_persistence_score", 0), 1),
    }


def build_source_discovery_coverage(week_key: str | None = None, trade_date: str | None = None, format: str = "json", limit: int = 40) -> dict | str:
    """Diagnostic report: are there worthwhile opportunities outside the shown radar/source?"""
    wk = str(week_key or "").strip() or _latest_week_from_tables()
    td = str(trade_date or "").strip()[:10]
    radar_syms = _radar_symbol_set()
    diag = _scanner_source_diagnostics()
    final_syms = set(_clean_symbol(x) for x in (diag.get("final_symbols") or diag.get("final_sample") or []) if _clean_symbol(x))

    candidates: dict[str, dict] = {}
    for idx, row in enumerate(_diag_ranked_candidates(diag), start=1):
        c = _candidate_from_diag_row(row, idx)
        if c.get("symbol"):
            candidates[c["symbol"]] = {**candidates.get(c["symbol"], {}), **c}
    for row in _source_candidate_rows_from_db(wk, limit=2500):
        c = _candidate_from_source_db(row)
        if c.get("symbol"):
            old = candidates.get(c["symbol"], {})
            # Preserve stronger score/rank data when both live diag and db are available.
            merged = {**c, **old}
            if _f(c.get("source_score"), 0) > _f(old.get("source_score"), 0):
                merged["source_score"] = c.get("source_score", 0)
            if int(_f(c.get("discovery_rank"), 99999)) < int(_f(old.get("discovery_rank"), 99999)):
                merged["discovery_rank"] = c.get("discovery_rank", 0)
            merged["source_tags"] = list(dict.fromkeys((old.get("source_tags") or []) + (c.get("source_tags") or [])))[:10]
            merged["source_reasons"] = list(dict.fromkeys((old.get("source_reasons") or []) + (c.get("source_reasons") or [])))[:8]
            candidates[c["symbol"]] = merged

    candidate_list = list(candidates.values())
    for c in candidate_list:
        c["hot_score"] = _candidate_hot_score(c)
        c["in_current_radar"] = c.get("symbol") in radar_syms
        c["in_final_source"] = c.get("symbol") in final_syms

    external_candidates = [c for c in candidate_list if not c.get("in_current_radar") and c.get("hot_score", 0) >= 45]
    source_selected_not_radar = [c for c in candidate_list if c.get("in_final_source") and not c.get("in_current_radar") and c.get("hot_score", 0) >= 40]
    discovery_not_selected = [c for c in candidate_list if not c.get("in_final_source") and not c.get("in_current_radar") and c.get("hot_score", 0) >= 50]

    winners = _winner_profile_rows_from_db(wk, td, limit=2500)
    winners_outside_source = [w for w in winners if not bool(w.get("source_seen"))]
    winners_seen_source_not_tool = [w for w in winners if bool(w.get("source_seen")) and not bool(w.get("tool_seen"))]
    winners_tool_seen = [w for w in winners if bool(w.get("tool_seen"))]

    external_candidates_sorted = sorted(external_candidates, key=lambda x: (x.get("hot_score", 0), x.get("source_score", 0), x.get("change_pct", 0)), reverse=True)
    source_selected_sorted = sorted(source_selected_not_radar, key=lambda x: (x.get("hot_score", 0), x.get("source_score", 0)), reverse=True)
    discovery_not_selected_sorted = sorted(discovery_not_selected, key=lambda x: (x.get("hot_score", 0), x.get("source_score", 0)), reverse=True)
    winners_outside_sorted = sorted(winners_outside_source, key=lambda x: _f(x.get("winner_change_pct"), 0), reverse=True)
    winners_source_not_tool_sorted = sorted(winners_seen_source_not_tool, key=lambda x: _f(x.get("winner_change_pct"), 0), reverse=True)

    lim = max(5, min(int(limit or 40), 150))
    result = {
        "ok": True,
        "version": "source_discovery_coverage_v1_read_only",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "week_key": wk,
        "trade_date": td,
        "dynamic_diagnostics": {
            "engine_version": diag.get("engine_version", ""),
            "market_date": diag.get("market_date", ""),
            "phase_label": diag.get("phase_label", ""),
            "selected_count": diag.get("selected_count", 0),
            "candidate_count_after_confirm": diag.get("candidate_count_after_confirm", 0),
            "fmp_movers_count": diag.get("fmp_movers_count", 0),
            "fmp_confirmed": diag.get("fmp_confirmed", 0),
            "source_bucket_counts": diag.get("source_bucket_counts", {}),
            "updated_at": diag.get("updated_at", ""),
        },
        "summary": {
            "current_radar_symbols": len(radar_syms),
            "final_source_symbols_known": len(final_syms),
            "candidate_rows_available": len(candidate_list),
            "external_hot_candidates_not_in_radar": len(external_candidates),
            "source_selected_but_not_displayed": len(source_selected_not_radar),
            "discovery_hot_not_selected": len(discovery_not_selected),
            "winner_profiles_total": len(winners),
            "winners_outside_source": len(winners_outside_source),
            "winners_seen_source_not_tool": len(winners_seen_source_not_tool),
            "winners_tool_seen": len(winners_tool_seen),
        },
        "external_hot_candidates_not_in_radar": [_compact_candidate(c) for c in external_candidates_sorted[:lim]],
        "source_selected_but_not_displayed": [_compact_candidate(c) for c in source_selected_sorted[:lim]],
        "discovery_hot_not_selected": [_compact_candidate(c) for c in discovery_not_selected_sorted[:lim]],
        "winners_outside_source": [_compact_winner(w) for w in winners_outside_sorted[:lim]],
        "winners_seen_source_not_tool": [_compact_winner(w) for w in winners_source_not_tool_sorted[:lim]],
        "interpretation": "تشخيص فقط. إذا كانت external/discovery lists عالية أثناء السوق، فهذا يعني أن المنبع أو الترقية لا تلتقط كل البدائل النظيفة بسرعة كافية.",
    }
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        sm = result["summary"]
        dd = result["dynamic_diagnostics"]
        lines = [
            "تقرير Source Freshness / Discovery Coverage V1",
            f"آخر تحديث منبع: {dd.get('updated_at') or 'غير متاح'} | المرحلة: {dd.get('phase_label') or 'غير محددة'}",
            f"رموز الرادار الحالية: {sm['current_radar_symbols']} | مرشحو المنبع المعروفون: {sm['candidate_rows_available']} | النهائي المعروف: {sm['final_source_symbols_known']}",
            f"مرشحون حارون خارج الرادار: {sm['external_hot_candidates_not_in_radar']}",
            f"دخلوا المنبع/الاختيار ولم يظهروا بالرادار: {sm['source_selected_but_not_displayed']}",
            f"مرشحون حارون اكتشفوا لكن لم يدخلوا الاختيار النهائي: {sm['discovery_hot_not_selected']}",
            f"رابحون تاريخيون خارج المنبع: {sm['winners_outside_source']} | دخلوا المنبع ولم تظهرهم الأداة: {sm['winners_seen_source_not_tool']}",
            "",
            "أهم مرشحين خارج الرادار الآن:",
        ]
        for c in result["external_hot_candidates_not_in_radar"][:15]:
            lines.append(f"- {c['symbol']}: hot {c['hot_score']}/100 | rank {c['rank']} | تغير {c['change_pct']}% | مصادر {','.join(c.get('source_tags') or [])[:80]}")
        lines.append("")
        lines.append("أهم رابحين سابقين لم يدخلوا المنبع:")
        for w in result["winners_outside_source"][:12]:
            lines.append(f"- {w['symbol']}: {w['winner_change_pct']}% | gap {w['gap_pct']}% | {w['likely_pattern']} | {w['move_quality_label']}")
        return "\n".join(lines)
    return result
