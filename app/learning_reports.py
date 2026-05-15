"""Weekly Pattern Learning reports for Stock Radar AI.

Read-only reports: they analyze Tracking Intelligence/Missed Opportunities rows
and do not alter scan results, scoring, Sharia status, or prices.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.tracking_intelligence import export_tracking_json
from app.missed_opportunities import build_loss_analysis_report, build_late_promotions_report, build_pre_move_evidence_report


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, str) and not value.strip():
            return default
        return float(value)
    except Exception:
        return default


def _s(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x or "").strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split("|") if x.strip()]
    return []


def _signal_outcome(row: dict) -> str:
    status = _s(row.get("status"))
    if _s(row.get("target_2_hit_at")) or status == "above_target":
        return "exceeded_target"
    if _s(row.get("target_hit_at")) or status == "target_hit":
        return "target_hit"
    if _s(row.get("stopped_at")) or status == "stopped":
        return "activated_loss"
    if status in {"plan_broken_before_activation", "disappeared_before_activation"} or _s(row.get("closed_at")):
        return "not_activated_or_broken"
    if _f(row.get("max_gain_pct"), 0) > 0.4:
        return "partial_gain"
    if status in {"pending", "activated"}:
        return "ongoing_or_pending"
    return status or "unknown"


def _pattern_signature(row: dict) -> str:
    plan = _s(row.get("plan_family")) or "plan_unknown"
    bucket = _s(row.get("signal_bucket")) or _s(row.get("signal_label")) or "bucket_unknown"
    risk = _as_list(row.get("risk_tags"))[:4]
    success = _as_list(row.get("success_tags"))[:3]
    support = _s(row.get("nearest_support_strength"))
    resistance = _s(row.get("nearest_resistance_strength"))
    parts = [bucket, plan]
    if risk:
        parts.append("risk=" + "+".join(sorted(set(risk))[:4]))
    if success:
        parts.append("success=" + "+".join(sorted(set(success))[:3]))
    if support:
        parts.append("support=" + support)
    if resistance:
        parts.append("resistance=" + resistance)
    return " | ".join(parts)


def _row_brief(row: dict) -> dict:
    return {
        "symbol": row.get("symbol", ""),
        "bucket": row.get("signal_bucket", row.get("signal_label", "")),
        "status": row.get("status", ""),
        "status_label": row.get("status_label", ""),
        "plan_family": row.get("plan_family", ""),
        "first_seen_at": row.get("first_seen_at", ""),
        "entry_price": row.get("entry_price", 0),
        "target_price": row.get("target_price", 0),
        "stop_loss": row.get("stop_loss", 0),
        "quality_score": row.get("quality_score", 0),
        "execution_readiness_score": row.get("execution_readiness_score", 0),
        "max_gain_pct": row.get("max_gain_pct", 0),
        "max_loss_pct": row.get("max_loss_pct", 0),
        "risk_tags": row.get("risk_tags", []),
        "success_tags": row.get("success_tags", []),
    }


def _top_patterns(rows: list[dict], outcome: str, limit: int = 8) -> list[dict]:
    selected = [r for r in rows if _signal_outcome(r) == outcome]
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in selected:
        groups[_pattern_signature(row)].append(row)
    out = []
    for sig, items in groups.items():
        risk_counter = Counter()
        success_counter = Counter()
        symbols = []
        for r in items:
            symbols.append(_s(r.get("symbol")))
            risk_counter.update(_as_list(r.get("risk_tags")))
            success_counter.update(_as_list(r.get("success_tags")))
        out.append({
            "pattern": sig,
            "signals": len(items),
            "unique_symbols": len(set([x for x in symbols if x])),
            "avg_max_gain_pct": round(sum(_f(x.get("max_gain_pct"), 0) for x in items) / max(1, len(items)), 2),
            "avg_max_loss_pct": round(sum(_f(x.get("max_loss_pct"), 0) for x in items) / max(1, len(items)), 2),
            "top_success_tags": success_counter.most_common(5),
            "top_risk_tags": risk_counter.most_common(5),
            "examples": [_row_brief(x) for x in items[:5]],
        })
    return sorted(out, key=lambda x: (x["unique_symbols"], x["signals"], x["avg_max_gain_pct"]), reverse=True)[:limit]


def build_pattern_learning_report(week_key: str | None = None, format: str = "json", limit: int = 5000) -> dict | str:
    export = export_tracking_json(week_key=week_key, include_items=False, limit=limit)
    rows = export.get("signals", []) if isinstance(export, dict) else []
    wk = export.get("week_key", week_key or "") if isinstance(export, dict) else (week_key or "")
    outcomes = Counter(_signal_outcome(r) for r in rows)
    result = {
        "ok": True,
        "version": "pattern_learning_v1_read_only",
        "week_key": wk,
        "rows_count": len(rows),
        "outcome_counts": dict(outcomes),
        "priority_framework": [
            {"rank": 1, "key": "exceeded_target", "label": "تجاوز الهدف 🚀", "action": "استخرج نمط الرابحين الممتازين وارفع المشابهين لاحقًا"},
            {"rank": 2, "key": "target_hit", "label": "وصل الهدف ✅", "action": "استخرج نمط النجاح الطبيعي"},
            {"rank": 3, "key": "partial_gain", "label": "ارتفع ولم يصل الهدف 🟡", "action": "يدخل Exit Intelligence: هدف أول/جني جزئي/وقف متحرك"},
            {"rank": 4, "key": "activated_loss", "label": "تفعل ثم خسر 🔴", "action": "تحذير قوي وطلب تأكيد استمرار"},
            {"rank": 5, "key": "not_activated_or_broken", "label": "لم يتفعل/كسر الخطة ⚫", "action": "آخر القائمة وتحسين نقطة الدخول/الدعم/المقاومة"},
        ],
        "top_patterns": {
            "exceeded_target": _top_patterns(rows, "exceeded_target"),
            "target_hit": _top_patterns(rows, "target_hit"),
            "partial_gain": _top_patterns(rows, "partial_gain"),
            "activated_loss": _top_patterns(rows, "activated_loss"),
            "not_activated_or_broken": _top_patterns(rows, "not_activated_or_broken"),
        },
        "notes": {
            "safe_mode": "تقرير قراءة فقط؛ لا يغير السكور أو الفلاتر.",
            "next_step": "بعد أسبوع إضافي، يمكن تحويل الأنماط المتكررة إلى guard/quality-tier تدريجيًا.",
        },
    }
    fmt = str(format or "json").lower()
    if fmt in {"brief", "text", "txt", "chatgpt"}:
        lines = ["تقرير Pattern Learning V1", f"الأسبوع: {wk}", "", "توزيع النتائج:"]
        labels = {
            "exceeded_target": "تجاوز الهدف 🚀",
            "target_hit": "وصل الهدف ✅",
            "partial_gain": "ارتفع ولم يصل الهدف 🟡",
            "activated_loss": "تفعل ثم خسر 🔴",
            "not_activated_or_broken": "لم يتفعل/كسر الخطة ⚫",
            "ongoing_or_pending": "مستمر/معلق",
        }
        for k, v in outcomes.most_common():
            lines.append(f"- {labels.get(k, k)}: {v}")
        lines.append("")
        lines.append("أولوية التعلم:")
        for item in result["priority_framework"]:
            lines.append(f"{item['rank']}. {item['label']}: {item['action']}")
        lines.append("")
        for key, title in [("exceeded_target", "أهم أنماط تجاوز الهدف"), ("target_hit", "أهم أنماط الوصول للهدف"), ("partial_gain", "أنماط صعدت ولم تصل الهدف"), ("activated_loss", "أنماط الخسارة بعد التفعيل")]:
            lines.append(title + ":")
            pats = result["top_patterns"].get(key) or []
            if not pats:
                lines.append("- لا توجد عينة كافية")
                continue
            for p in pats[:5]:
                lines.append(f"- {p['signals']} إشارة / {p['unique_symbols']} رموز | متوسط ربح {p['avg_max_gain_pct']}% | {p['pattern'][:180]}")
            lines.append("")
        return "\n".join(lines)
    return result


def build_failure_patterns_report(week_key: str | None = None, format: str = "json") -> dict | str:
    return build_loss_analysis_report(week_key=week_key, format=format, limit=500, detail="full", top=50)


def build_winner_patterns_report(week_key: str | None = None, format: str = "json") -> dict | str:
    result = build_pattern_learning_report(week_key=week_key, format="json")
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        wk = result.get("week_key", "") if isinstance(result, dict) else ""
        lines = ["تقرير Winner Patterns V1", f"الأسبوع: {wk}", ""]
        for key, title in [("exceeded_target", "تجاوز الهدف 🚀"), ("target_hit", "وصل الهدف ✅"), ("partial_gain", "ارتفع ولم يصل الهدف 🟡")]:
            lines.append(title + ":")
            for p in (result.get("top_patterns", {}).get(key, []) if isinstance(result, dict) else [])[:8]:
                lines.append(f"- {p['signals']} إشارة / {p['unique_symbols']} رموز | متوسط ربح {p['avg_max_gain_pct']}% | {p['pattern'][:180]}")
            lines.append("")
        return "\n".join(lines)
    if isinstance(result, dict):
        return {"ok": True, "week_key": result.get("week_key"), "winner_patterns": {k: result.get("top_patterns", {}).get(k, []) for k in ["exceeded_target", "target_hit", "partial_gain"]}}
    return {"ok": False, "error": "pattern_report_failed"}


def build_promotion_funnel_report(week_key: str | None = None, format: str = "json") -> dict | str:
    late = build_late_promotions_report(week_key=week_key, threshold=10.0, format="json")
    pre = build_pre_move_evidence_report(week_key=week_key, threshold=10.0, format="json", limit=120)
    if str(format or "json").lower() in {"brief", "text", "txt", "chatgpt"}:
        wk = late.get("week_key", week_key or "") if isinstance(late, dict) else (week_key or "")
        lines = ["تقرير Promotion Funnel V1", f"الأسبوع: {wk}", ""]
        if isinstance(late, dict):
            lines.append(f"حالات الترقية المتأخرة المهمة: {late.get('count', 0)}")
        if isinstance(pre, dict):
            counts = pre.get("counts", {}) or {}
            lines.append(f"لقطات محفوظة قبل الحركة: {counts.get('with_snapshots', 0)}")
            lines.append(f"ظهرت قبل/حول +5%: {counts.get('before_5pct', 0)}")
            lines.append(f"أول دخول متأخر: {counts.get('late_first_entry', 0)}")
        lines.append("")
        lines.append("الاستخدام: يحدد أين تضيع الأسهم بين المنبع → التحليل العميق → المراقبة → الدخول.")
        return "\n".join(lines)
    return {"ok": True, "week_key": (late or {}).get("week_key", week_key or ""), "late_promotions": late, "pre_move": pre}
