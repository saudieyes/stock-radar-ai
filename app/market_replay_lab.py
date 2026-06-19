"""Market Replay Lab V1.

Compact Polygon minute replay for testing the Opportunity Radar before enabling
new logic on the live market.  It never stores raw Polygon rows in SQLite/GitHub;
it streams a local /tmp CSV/ZIP and returns compact candidate/event summaries.
"""
from __future__ import annotations

import csv
import gzip
import io
import math
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from app.opportunity_radar import OPPORTUNITY_RADAR_VERSION, enrich_row_opportunity_radar

MARKET_REPLAY_LAB_VERSION = "market_replay_lab_v1e_exit_decision_overlay_2026_06_19"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _u(v: Any) -> str:
    return _s(v).upper()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        if isinstance(v, str):
            v = v.replace(",", "").replace("$", "").replace("%", "").strip()
        n = float(v)
        if math.isnan(n) or math.isinf(n):
            return default
        return n
    except Exception:
        return default


def _round(v: Any, nd: int = 2) -> float:
    try:
        return round(_num(v), nd)
    except Exception:
        return 0.0


def _sym(row: dict) -> str:
    for k in ["ticker", "symbol", "T", "sym"]:
        x = _u(row.get(k))
        if x and all(ch.isalnum() or ch in {".", "-"} for ch in x):
            return x
    return ""


def _bar_time(row: dict) -> tuple[str, str]:
    raw = row.get("window_start") or row.get("timestamp") or row.get("t") or row.get("sip_timestamp") or ""
    try:
        n = int(float(raw))
        # Polygon flat files often use nanoseconds.  Be forgiving.
        if n > 10**17:
            sec = n / 1_000_000_000
        elif n > 10**14:
            sec = n / 1_000_000
        elif n > 10**11:
            sec = n / 1000
        else:
            sec = n
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except Exception:
        txt = _s(raw)
        if len(txt) >= 10 and txt[:4].isdigit():
            return txt[:10], txt[11:16] if len(txt) >= 16 else ""
    return "unknown", ""


def _price(row: dict, *keys: str) -> float:
    for k in keys:
        n = _num(row.get(k), 0.0)
        if n > 0:
            return n
    return 0.0


def _source_date(name: str) -> str:
    import re
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", str(name or ""))
    return m.group(1) if m else ""


def _source_kind(name: str) -> str:
    low = str(name or "").lower()
    if "day_" in low or "daily" in low or "/day" in low or "day_aggs" in low:
        return "daily"
    if "minute" in low or "min_" in low or "minute_aggs" in low:
        return "minute"
    return "unknown"


def _include_source(name: str, kind: str | None) -> bool:
    if not kind or kind == "all":
        return True
    k = _source_kind(name)
    if kind == "minute":
        # For ad-hoc CSVs with no daily/minute label, assume minute bars.
        return k in {"minute", "unknown"}
    if kind == "daily":
        return k == "daily"
    return True


def _phase_utc(hhmm: str) -> str:
    # US market times in UTC during daylight-saving months.  This is sufficient
    # for current Polygon replay diagnostics and intentionally conservative.
    t = str(hhmm or "")[:5]
    if not t:
        return "unknown"
    if t < "08:00":
        return "overnight"
    if t < "13:30":
        return "premarket"
    if t == "13:30":
        return "open"
    if t < "20:00":
        return "regular"
    return "after_hours"


def _phase_label_ar(phase: str) -> str:
    return {
        "overnight": "قبل البري ماركت / ليلي",
        "premarket": "قبل الافتتاح",
        "open": "لحظة الافتتاح",
        "regular": "أثناء السوق الرسمي",
        "after_hours": "بعد الإغلاق",
    }.get(str(phase or ""), "غير معروف")


def _chase_risk(max_gain_before: float, change_at_detection: float) -> tuple[str, str]:
    g = max(abs(_num(max_gain_before)), abs(_num(change_at_detection)))
    if g >= 15:
        return "very_late", "متأخر جدًا / مطاردة محتملة"
    if g >= 8:
        return "late", "متأخر"
    if g >= 5:
        return "watch_carefully", "مقبول بحذر"
    return "early", "مبكر"


def _hhmm_to_minutes(hhmm: Any) -> int | None:
    try:
        txt = str(hhmm or "")[:5]
        if len(txt) != 5 or ":" not in txt:
            return None
        h, m = txt.split(":", 1)
        return int(h) * 60 + int(m)
    except Exception:
        return None


def _minutes_between(start: Any, end: Any) -> int | None:
    a = _hhmm_to_minutes(start)
    b = _hhmm_to_minutes(end)
    if a is None or b is None:
        return None
    return max(0, b - a)


def _time_to_label_ar(minutes: Any) -> str:
    m = _num(minutes, -1)
    if m < 0:
        return "غير متاح"
    if m <= 30:
        return "سريع جدًا (أقل من 30 دقيقة)"
    if m <= 60:
        return "سريع (30-60 دقيقة)"
    if m <= 120:
        return "متوسط السرعة (1-2 ساعة)"
    if m <= 240:
        return "يمتد لعدة ساعات (2-4 ساعات)"
    if m <= 480:
        return "Runner بطيء/ممتد (4-8 ساعات)"
    return "استمرار طويل حتى آخر اليوم/بعده"


def _distribution(values: list[float], bins: list[tuple[float, float, str]]) -> list[dict[str, Any]]:
    total = max(1, len(values))
    out: list[dict[str, Any]] = []
    for lo, hi, label in bins:
        count = 0
        for v in values:
            if lo <= v < hi:
                count += 1
        out.append({"range": label, "count": count, "pct": round(count / total * 100.0, 1)})
    return out


def _candidate_is_late(c: dict) -> bool:
    return str(c.get("chase_risk_at_detection") or "") in {"late", "very_late"}


def _candidate_is_clean_winner(c: dict) -> bool:
    return (
        bool(c.get("detected_previous_session"))
        and str(c.get("chase_risk_at_detection") or "") in {"early", "watch_carefully"}
        and _num(c.get("max_after_pct"), 0.0) >= 20.0
        and _num(c.get("min_after_pct"), 0.0) > -10.0
    )


def _candidate_is_danger_winner(c: dict) -> bool:
    return (
        _num(c.get("max_after_pct"), 0.0) >= 20.0
        and (
            _candidate_is_late(c)
            or _num(c.get("min_after_pct"), 0.0) <= -10.0
            or _num(c.get("post_peak_drawdown_pct"), 0.0) <= -20.0
        )
    )


def _candidate_is_clean_entry_winner(c: dict) -> bool:
    # Clean entry means the radar caught the setup early enough and the trade did
    # not first punish the entry with a deep drawdown.  It does NOT guarantee the
    # stock held its peak.
    return _candidate_is_clean_winner(c)


def _candidate_is_clean_runner_winner(c: dict) -> bool:
    # Clean runner is stricter: it was a clean entry AND it kept much of its move
    # after the peak.  This is the bucket useful for “hold longer / scale out”.
    return (
        _candidate_is_clean_entry_winner(c)
        and _num(c.get("post_peak_drawdown_pct"), 0.0) > -10.0
        and _num(c.get("gain_retention_pct"), 0.0) >= 70.0
        and _num(c.get("minutes_to_peak"), 0.0) >= 120.0
    )


def _candidate_is_quick_fade(c: dict) -> bool:
    return (
        _num(c.get("max_after_pct"), 0.0) >= 20.0
        and _num(c.get("minutes_to_peak"), 9999.0) <= 60.0
        and _num(c.get("post_peak_drawdown_pct"), 0.0) <= -10.0
    )


def _exit_decision_overlay(c: dict) -> dict[str, Any]:
    peak = _num(c.get("max_after_pct"), 0.0)
    worst = _num(c.get("min_after_pct"), 0.0)
    post_drop = _num(c.get("post_peak_drawdown_pct"), 0.0)
    retention = _num(c.get("gain_retention_pct"), 0.0)
    minutes_to_peak = _num(c.get("minutes_to_peak"), -1.0)
    drop10 = c.get("minutes_to_drop_10pct_from_peak")
    drop10_min = _num(drop10, 9999.0) if drop10 is not None else 9999.0
    chase = str(c.get("chase_risk_at_detection") or "")
    flags: list[str] = []
    if chase in {"late", "very_late"}:
        flags.append("التقاط متأخر/مطاردة محتملة")
    if worst <= -10.0:
        flags.append("هبط أكثر من 10% بعد الالتقاط")
    if post_drop <= -20.0:
        flags.append("تلاشى أكثر من 20% بعد القمة")
    if drop10_min <= 5.0:
        flags.append("فقد 10% من القمة خلال دقائق")
    if minutes_to_peak <= 60.0 and peak >= 20.0:
        flags.append("القمة جاءت بسرعة")

    runner_hold = (
        peak >= 20.0
        and minutes_to_peak >= 240.0
        and post_drop > -10.0
        and retention >= 70.0
        and chase not in {"late", "very_late"}
    )
    quick_take = (
        _candidate_is_quick_fade(c)
        or chase in {"late", "very_late"}
        or drop10_min <= 5.0
        or post_drop <= -20.0
    )

    if runner_hold:
        label = "Runner صالح للتدرج والاحتفاظ الجزئي"
        rule = "لا تبيع كل الكمية مبكرًا؛ بيع تدريجي عند الأهداف، واترك جزءًا صغيرًا ما دام فوق VWAP/قاع آخر 5د ولم يفقد 10% من القمة."
        plan = "بيع جزء عند +20%/+30%، ثم حماية الباقي بتريلينغ 8-10% من القمة أو كسر VWAP."
        score = 86
    elif quick_take:
        label = "خطفة / جني ربح سريع"
        rule = "هذه الحركة غالبًا تتبخر؛ لا تنتظر القمة المثالية. عند تسارع قوي أو ربح سريع، خذ جزءًا كبيرًا وارفع الوقف فورًا."
        plan = "بيع 50-70% عند أول اندفاع قوي، ثم اخرج من الباقي عند فقد 10% من القمة أو كسر VWAP/قاع 5د."
        score = 34
    elif peak >= 20.0 and retention >= 50.0 and post_drop > -20.0:
        label = "حركة جيدة تحتاج إدارة نشطة"
        rule = "يمكن أن تمتد، لكنها ليست Runner نظيفًا؛ الأفضل بيع تدريجي وحماية الربح بعد كل قمة جديدة."
        plan = "بيع جزء عند +15%/+25%، واترك جزءًا صغيرًا بشرط عدم فقد 10% من القمة."
        score = 62
    elif peak >= 10.0:
        label = "ربح متوسط / لا تطمع"
        rule = "الحركة ليست كافية لإعطاء مجال واسع؛ تعامل معها كفرصة يومية عادية."
        plan = "بيع تدريجي سريع عند +8% إلى +15% أو عند فشل VWAP."
        score = 48
    else:
        label = "مراقبة فقط"
        rule = "لم تظهر حركة كافية في الاختبار؛ لا تعتمد عليها كفرصة بيع/شراء."
        plan = "انتظر تأكيد جديد أو استبعد السهم من الأولوية."
        score = 20

    return {
        "exit_action_label_ar": label,
        "exit_action_rule_ar": rule,
        "suggested_sell_plan_ar": plan,
        "exit_action_score": score,
        "runner_hold_candidate": bool(runner_hold),
        "quick_take_profit_candidate": bool(quick_take),
        "fade_after_peak_flag": bool(post_drop <= -20.0 or drop10_min <= 5.0),
        "risk_exit_flags": flags,
    }


def _build_performance_summary(candidates: list[dict]) -> dict[str, Any]:
    total = max(1, len(candidates))
    peaks = [_num(c.get("max_after_pct"), 0.0) for c in candidates]
    worst = [_num(c.get("min_after_pct"), 0.0) for c in candidates]
    time_to_peak = [_num(c.get("minutes_to_peak"), -1.0) for c in candidates if _num(c.get("minutes_to_peak"), -1.0) >= 0]
    post_peak_drawdowns = [_num(c.get("post_peak_drawdown_pct"), 0.0) for c in candidates]
    retention = [_num(c.get("gain_retention_pct"), 0.0) for c in candidates if _num(c.get("gain_retention_pct"), -999.0) > -998.0]
    clean = [c for c in candidates if _candidate_is_clean_winner(c)]
    clean_runner = [c for c in candidates if _candidate_is_clean_runner_winner(c)]
    quick_fades = [c for c in candidates if _candidate_is_quick_fade(c)]
    danger = [c for c in candidates if _candidate_is_danger_winner(c)]

    def pct(pred) -> float:
        return round(sum(1 for c in candidates if pred(c)) / total * 100.0, 1)

    # Important: this is not the true all-market missed-opportunity rate.  The
    # run receives already-detected candidates; a separate missed-opportunity
    # replay over all symbols is required to measure winners the radar never saw.
    return {
        "note_ar": "هذه الإحصاءات من المرشحين الـ replay فقط، وليست قياسًا لكل السوق. قياس الأسهم الرابحة التي لم تلتقطها الأداة يحتاج مسح missed-opportunities على كل الرموز.",
        "candidate_count": len(candidates),
        "clean_winners_count": len(clean),
        "clean_winners_pct": round(len(clean) / total * 100.0, 1),
        "clean_winners_meaning_ar": "فائز من ناحية الالتقاط المبكر وعدم هبوطه بقوة قبل الصعود؛ قد يتلاشى بعد القمة لذلك لا يعني Hold.",
        "clean_runner_winners_count": len(clean_runner),
        "clean_runner_winners_pct": round(len(clean_runner) / total * 100.0, 1),
        "clean_runner_meaning_ar": "فائز حافظ على جزء كبير من الصعود بعد القمة؛ هذا أقرب لسهم يمكن التدرج في بيعه بدل الخروج السريع.",
        "quick_fade_winners_count": len(quick_fades),
        "quick_fade_winners_pct": round(len(quick_fades) / total * 100.0, 1),
        "danger_winners_count": len(danger),
        "danger_winners_pct": round(len(danger) / total * 100.0, 1),
        "weak_under_20pct_count": sum(1 for p in peaks if p < 20.0),
        "weak_under_20pct_pct": round(sum(1 for p in peaks if p < 20.0) / total * 100.0, 1),
        "risk_failure_drawdown_over_10pct_count": sum(1 for d in worst if d <= -10.0),
        "risk_failure_drawdown_over_10pct_pct": round(sum(1 for d in worst if d <= -10.0) / total * 100.0, 1),
        "peak_gain_distribution": _distribution(peaks, [
            (0, 10, "0-10%"), (10, 20, "10-20%"), (20, 30, "20-30%"),
            (30, 50, "30-50%"), (50, 10_000, "50%+")
        ]),
        "worst_drawdown_distribution_after_detection": _distribution(worst, [
            (0, 10_000, "لم ينزل تحت سعر الالتقاط"), (-3, 0, "0 إلى -3%"),
            (-5, -3, "-3 إلى -5%"), (-10, -5, "-5 إلى -10%"),
            (-20, -10, "-10 إلى -20%"), (-10_000, -20, "أكثر من -20%")
        ]),
        "post_peak_drawdown_distribution": _distribution(post_peak_drawdowns, [
            (-3, 1, "تراجع خفيف بعد القمة <3%"), (-5, -3, "تراجع 3-5% بعد القمة"),
            (-10, -5, "تراجع 5-10% بعد القمة"), (-20, -10, "تراجع 10-20% بعد القمة"),
            (-10_000, -20, "تراجع عنيف >20% بعد القمة")
        ]),
        "time_to_peak_distribution": _distribution(time_to_peak, [
            (0, 31, "أقل من 30 دقيقة"), (31, 61, "30-60 دقيقة"),
            (61, 121, "1-2 ساعة"), (121, 241, "2-4 ساعات"),
            (241, 481, "4-8 ساعات"), (481, 10_000, "أكثر من 8 ساعات")
        ]),
        "phase_performance": _phase_performance_summary(candidates),
        "exit_hint_ar": "لا تستخدم أعلى High كهدف بيع وحيد. راقب سرعة الوصول للقمة، ثم أي كسر VWAP/فشل قمة/هبوط 10% من القمة. V2f يضيف مقاييس تساعدك ترى هل الحركة سريعة وتتبخر أو تمتد لساعات.",
    }


def _phase_performance_summary(candidates: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    phases = ["premarket", "open", "regular", "after_hours", "overnight", "unknown"]
    for ph in phases:
        sub = [c for c in candidates if str(c.get("phase_at_detection") or "unknown") == ph]
        if not sub:
            continue
        total = len(sub)
        peaks = [_num(c.get("max_after_pct"), 0.0) for c in sub]
        worst = [_num(c.get("min_after_pct"), 0.0) for c in sub]
        times = [_num(c.get("minutes_to_peak"), -1.0) for c in sub if _num(c.get("minutes_to_peak"), -1.0) >= 0]
        out.append({
            "phase": ph,
            "label_ar": _phase_label_ar(ph),
            "count": total,
            "peak_20pct_plus_count": sum(1 for p in peaks if p >= 20.0),
            "peak_20pct_plus_pct": round(sum(1 for p in peaks if p >= 20.0) / total * 100.0, 1),
            "peak_30pct_plus_count": sum(1 for p in peaks if p >= 30.0),
            "peak_30pct_plus_pct": round(sum(1 for p in peaks if p >= 30.0) / total * 100.0, 1),
            "drawdown_10pct_plus_count": sum(1 for d in worst if d <= -10.0),
            "drawdown_10pct_plus_pct": round(sum(1 for d in worst if d <= -10.0) / total * 100.0, 1),
            "median_peak_gain_pct": _round(sorted(peaks)[len(peaks)//2] if peaks else 0.0, 2),
            "median_worst_drawdown_pct": _round(sorted(worst)[len(worst)//2] if worst else 0.0, 2),
            "median_minutes_to_peak": _round(sorted(times)[len(times)//2] if times else 0.0, 0),
        })
    return out


def _build_exit_behavior_summary(candidates: list[dict]) -> dict[str, Any]:
    total = max(1, len(candidates))
    def avg(vals: list[float]) -> float:
        return round(sum(vals) / max(1, len(vals)), 2)
    def med(vals: list[float]) -> float:
        if not vals:
            return 0.0
        vals = sorted(vals)
        return round(vals[len(vals)//2], 2)
    times = [_num(c.get("minutes_to_peak"), -1.0) for c in candidates if _num(c.get("minutes_to_peak"), -1.0) >= 0]
    post_drop = [_num(c.get("post_peak_drawdown_pct"), 0.0) for c in candidates]
    retention = [_num(c.get("gain_retention_pct"), 0.0) for c in candidates if _num(c.get("gain_retention_pct"), -999.0) > -998.0]
    fast = [c for c in candidates if 0 <= _num(c.get("minutes_to_peak"), -1) <= 60]
    extended = [c for c in candidates if _num(c.get("minutes_to_peak"), -1) >= 240]
    return {
        "note_ar": "مقاييس ما بعد القمة تقريبية لأنها مبنية على شموع دقيقة: نعرف High/Low للدقيقة وليس ترتيب الصفقات داخل نفس الدقيقة.",
        "median_minutes_to_peak": med(times),
        "avg_minutes_to_peak": avg(times),
        "median_post_peak_drawdown_pct": med(post_drop),
        "avg_post_peak_drawdown_pct": avg(post_drop),
        "median_gain_retention_pct_to_last_seen": med(retention),
        "fast_peak_within_60m_count": len(fast),
        "fast_peak_within_60m_pct": round(len(fast) / total * 100.0, 1),
        "extended_peak_after_4h_count": len(extended),
        "extended_peak_after_4h_pct": round(len(extended) / total * 100.0, 1),
        "quick_fade_candidates": [_summary_item_for_exit(c) for c in candidates if _num(c.get("minutes_to_peak"), 9999) <= 60 and _num(c.get("post_peak_drawdown_pct"), 0.0) <= -10.0][:20],
        "extended_runners": [_summary_item_for_exit(c) for c in candidates if _num(c.get("minutes_to_peak"), -1) >= 240 and _num(c.get("post_peak_drawdown_pct"), 0.0) > -10.0][:20],
        "sell_behavior_rule_ar": "إذا وصلت القمة سريعًا جدًا ثم بدأ السهم يفقد 10% من القمة أو يكسر VWAP/قمة شمعة 5د، فغالبًا الحركة سريعة وتحتاج جني ربح أسرع. أما المرشحات النظيفة التي تتأخر قمتها 4 ساعات+ ولا تتراجع أكثر من 10% من القمة فهي Runner أفضل للتدرج في البيع.",
    }


def _summary_item_for_exit(c: dict) -> dict[str, Any]:
    return {
        "symbol": c.get("symbol"),
        "date": c.get("date"),
        "phase_at_detection_ar": c.get("phase_at_detection_ar"),
        "first_seen_time_utc": c.get("first_seen_time_utc"),
        "first_seen_price": c.get("first_seen_price"),
        "peak_gain_after_detection_pct": c.get("max_after_pct"),
        "peak_time_utc": c.get("max_after_time_utc"),
        "minutes_to_peak": c.get("minutes_to_peak"),
        "time_to_peak_label_ar": c.get("time_to_peak_label_ar"),
        "post_peak_drawdown_pct": c.get("post_peak_drawdown_pct"),
        "minutes_to_drop_10pct_from_peak": c.get("minutes_to_drop_10pct_from_peak"),
        "gain_retention_pct": c.get("gain_retention_pct"),
        "chase_risk_label_ar": c.get("chase_risk_label_ar"),
        "detected_previous_session": c.get("detected_previous_session"),
        "exit_action_label_ar": c.get("exit_action_label_ar"),
        "suggested_sell_plan_ar": c.get("suggested_sell_plan_ar"),
        "runner_hold_candidate": c.get("runner_hold_candidate"),
        "quick_take_profit_candidate": c.get("quick_take_profit_candidate"),
        "risk_exit_flags": c.get("risk_exit_flags"),
    }


def _iter_zip_sources(path: Path, max_files: int, kind: str | None = None) -> Iterator[tuple[str, Iterable[dict]]]:
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if i.filename.lower().endswith((".csv", ".csv.gz")) and _include_source(i.filename, kind)]
        infos = sorted(infos, key=lambda i: _source_date(i.filename) or i.filename)[-max(1, int(max_files or 1)):]
        for info in infos:
            raw = z.open(info)
            low = info.filename.lower()
            if low.endswith(".gz"):
                gz = gzip.GzipFile(fileobj=raw)
                text = io.TextIOWrapper(gz, encoding="utf-8", errors="ignore", newline="")
            else:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="ignore", newline="")
            try:
                yield info.filename, csv.DictReader(text)
            finally:
                try:
                    text.close()
                except Exception:
                    pass


def _iter_sources(path: str | Path, max_files: int = 5, kind: str | None = None) -> Iterator[tuple[str, Iterable[dict]]]:
    p = Path(str(path or "")).expanduser()
    if not p.exists():
        return
    if p.is_dir():
        files = [x for x in p.glob("**/*") if x.is_file() and x.name.lower().endswith((".csv", ".csv.gz", ".zip")) and _include_source(x.name, kind)]
        files = sorted(files, key=lambda x: _source_date(x.name) or x.name)[-max(1, int(max_files or 1)):]
        for fp in files:
            yield from _iter_sources(fp, max_files=max_files, kind=kind)
    elif p.suffix.lower() == ".zip":
        yield from _iter_zip_sources(p, max_files=max_files, kind=kind)
    elif p.name.lower().endswith(".csv.gz"):
        f = gzip.open(p, "rt", encoding="utf-8", errors="ignore", newline="")
        try:
            yield p.name, csv.DictReader(f)
        finally:
            f.close()
    elif p.name.lower().endswith(".csv"):
        f = p.open("r", encoding="utf-8", errors="ignore", newline="")
        try:
            yield p.name, csv.DictReader(f)
        finally:
            f.close()


def _safe_path(path: str) -> tuple[bool, str]:
    if not path:
        return False, "path_missing"
    try:
        p = Path(path).expanduser().resolve()
        allowed_roots = [Path("/tmp").resolve(), Path("/mnt/data").resolve()]
        # Railway use should be /tmp.  /mnt/data is allowed only for local ChatGPT packaging tests.
        if not any(str(p).startswith(str(root)) for root in allowed_roots):
            return False, "path_not_allowed_use_tmp"
        if not p.exists():
            return False, "path_not_found"
        return True, str(p)
    except Exception as exc:
        return False, f"bad_path:{type(exc).__name__}"


def market_replay_lab_status() -> dict:
    return {
        "ok": True,
        "version": MARKET_REPLAY_LAB_VERSION,
        "opportunity_radar_version": OPPORTUNITY_RADAR_VERSION,
        "available_runs": [
            "/replay-lab/small-stock-classic/run?path=/tmp/your_polygon_minutes.zip",
            "/replay-lab/small-stock-classic/pull-run?end_date=2026-06-18&minute_days=5&daily_lookback_days=14&max_rows=250000",
        ],
        "storage_rule_ar": "المحاكي يقرأ raw Polygon من /tmp فقط ويعيد نتائج مختصرة؛ لا يحفظ raw في SQLite/GitHub/Railway volume. V2g يقرأ عدة أيام فعليًا، يستخدم daily lookback أوسع، ويضيف طبقة خروج لا تمنع ظهور الفرص: تميز بين Clean Entry وClean Runner، وتضيف خطة بيع مختصرة حسب سرعة القمة/التلاشي.",
        "small_stock_rules_ar": [
            "فريم 5د/15د عند المضاربة اللحظية.",
            "مستويات Fib 38.2/50/61.8/78.6 من آخر قاع إلى آخر قمة، مع تركيز على 61.8/78.6.",
            "VWAP: دخول فقط قربه أو بعد إغلاق شمعة فوقه.",
            "قمة اليوم السابق: منطقة تفعيل/شراء كلاسيكية بشرط إغلاق شمعة.",
            "لا تلحق الشمعة الخضراء؛ إذا ابتعد السعر ينتظر Pullback.",
        ],
    }


def _load_daily_context(resolved: str, max_files: int = 20) -> tuple[dict[str, dict[str, dict[str, float]]], list[str]]:
    """Load compact daily context from daily flat files in the same /tmp path."""
    daily_by_date: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
    files: list[str] = []
    try:
        for source_name, reader in _iter_sources(resolved, max_files=max_files, kind="daily"):
            files.append(source_name)
            file_date = _source_date(source_name)
            for raw in reader:
                sym = _sym(raw)
                if not sym:
                    continue
                dt, _ = _bar_time(raw)
                if dt == "unknown" and file_date:
                    dt = file_date
                if dt == "unknown":
                    continue
                o = _price(raw, "open", "o")
                h = _price(raw, "high", "h")
                l = _price(raw, "low", "l")
                c = _price(raw, "close", "c")
                v = _price(raw, "volume", "v")
                if h <= 0 or l <= 0 or c <= 0:
                    continue
                daily_by_date[dt][sym] = {"open": o or c, "high": h, "low": l, "close": c, "volume": v}
    except Exception:
        # Daily context is helpful, not required.  Replay must not fail because of it.
        pass
    return dict(daily_by_date), files


def _latest_prior_daily(daily_by_date: dict[str, dict[str, dict[str, float]]], date_s: str, sym: str) -> tuple[str, dict[str, float]]:
    for dt in sorted([d for d in daily_by_date.keys() if d < date_s], reverse=True):
        rec = (daily_by_date.get(dt) or {}).get(sym)
        if rec:
            return dt, rec
    return "", {}


def run_small_stock_classic_replay_from_path(path: str, max_files: int = 5, max_rows: int = 250_000, max_candidates: int = 120) -> dict:
    ok, resolved = _safe_path(path)
    if not ok:
        return {"ok": False, "version": MARKET_REPLAY_LAB_VERSION, "error": resolved, "hint_ar": "ضع ملف Polygon minute zip مؤقتًا في /tmp ثم مرر path=/tmp/file.zip."}

    # max_rows is now per file/day, not a global cap.  This prevents the replay
    # from silently stopping after the first large Polygon file.
    max_rows_per_file = max(10_000, min(500_000, int(max_rows or 250_000)))
    max_files_safe = max(1, min(10, int(max_files or 5)))

    daily_by_date, daily_files_seen = _load_daily_context(resolved, max_files=max_files_safe + 10)
    prev_day_high: dict[str, float] = {}
    prev_day_low: dict[str, float] = {}
    prev_day_close: dict[str, float] = {}
    prior_candidate_dates_by_symbol: dict[str, set[str]] = defaultdict(set)
    day_state: dict[tuple[str, str], dict] = {}
    recent_vol: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
    events: dict[tuple[str, str], dict] = {}
    rows_seen = 0
    rows_by_file: dict[str, int] = {}
    files_seen: list[str] = []
    dates_seen: set[str] = set()

    # Keep minute sources only.  Daily files are used for previous-day context.
    for source_name, reader in _iter_sources(resolved, max_files=max_files_safe, kind="minute"):
        files_seen.append(source_name)
        file_date = _source_date(source_name)
        file_rows = 0
        current_file_day_keys: set[tuple[str, str]] = set()
        for raw in reader:
            file_rows += 1
            if file_rows > max_rows_per_file:
                break
            rows_seen += 1
            sym = _sym(raw)
            if not sym:
                continue
            date, hhmm = _bar_time(raw)
            if date == "unknown" and file_date:
                date = file_date
            if date == "unknown":
                continue
            dates_seen.add(date)
            o = _price(raw, "open", "o")
            h = _price(raw, "high", "h")
            l = _price(raw, "low", "l")
            c = _price(raw, "close", "c")
            v = _price(raw, "volume", "v")
            if c <= 0 or h <= 0 or l <= 0 or v <= 0:
                continue
            if not (0.75 <= c <= 25.0):
                continue

            daily_dt, daily_rec = _latest_prior_daily(daily_by_date, date, sym)
            if daily_rec:
                p_high = _num(daily_rec.get("high"), 0.0)
                p_low = _num(daily_rec.get("low"), 0.0)
                p_close = _num(daily_rec.get("close"), 0.0)
                if p_high > 0:
                    prev_day_high[sym] = p_high
                if p_low > 0:
                    prev_day_low[sym] = p_low
                if p_close > 0:
                    prev_day_close[sym] = p_close

            key = (sym, date)
            current_file_day_keys.add(key)
            st = day_state.get(key)
            if not st:
                st = {"open": o or c, "high": h, "low": l, "close": c, "volume": 0.0, "dollar": 0.0, "vwap_num": 0.0, "first_time": hhmm}
                day_state[key] = st
            # Gain before detection candidate uses the high already seen before this bar.
            high_before = _num(st.get("high"), h)
            open_for_gain = _num(st.get("open"), o or c)
            max_gain_before = ((high_before - open_for_gain) / open_for_gain * 100.0) if open_for_gain > 0 else 0.0

            st["high"] = max(_num(st.get("high"), h), h)
            st["low"] = min(_num(st.get("low"), l), l)
            st["close"] = c
            st["volume"] = _num(st.get("volume"), 0.0) + v
            typical = (h + l + c) / 3.0
            st["dollar"] = _num(st.get("dollar"), 0.0) + c * v
            st["vwap_num"] = _num(st.get("vwap_num"), 0.0) + typical * v
            vwap = st["vwap_num"] / st["volume"] if st["volume"] > 0 else 0.0
            avg_bar_vol = (sum(recent_vol[sym]) / len(recent_vol[sym])) if recent_vol[sym] else v
            rv = (v / avg_bar_vol) if avg_bar_vol > 0 else 1.0
            recent_vol[sym].append(v)
            change = ((c - st["open"]) / st["open"] * 100.0) if st.get("open") else 0.0
            phase = _phase_utc(hhmm)
            chase_code, chase_label = _chase_risk(max_gain_before, change)
            pday_high = prev_day_high.get(sym, 0.0)
            pday_close = prev_day_close.get(sym, 0.0)
            prev_high_dist = ((c - pday_high) / pday_high * 100.0) if pday_high > 0 else 999.0
            prev_close_gap = ((c - pday_close) / pday_close * 100.0) if pday_close > 0 else 0.0
            was_prior_candidate = bool(prior_candidate_dates_by_symbol.get(sym))

            row = {
                "symbol": sym,
                "display_price": c,
                "current_price_live": c,
                "display_change_pct": change,
                "change_from_open_pct": change,
                "change_vs_prev_close_pct": prev_close_gap if pday_close > 0 else change,
                "day_low": st["low"],
                "day_high": st["high"],
                "session_low": st["low"],
                "session_high": st["high"],
                "vwap_proxy": vwap,
                "above_vwap_proxy": c >= vwap if vwap > 0 else False,
                "previous_day_high": pday_high,
                "previous_day_low": prev_day_low.get(sym, 0.0),
                "previous_close": pday_close,
                "effective_volume_ratio": rv,
                "volume": st["volume"],
                "dollar_volume": st["dollar"],
                "quality_score": 70,
                "execution_readiness_score": 55,
                "final_decision_code": "EARLY_WATCH",
                "decision": "مراقبة",
            }
            enriched = enrich_row_opportunity_radar(row, market_phase="replay")
            classic = enriched.get("small_stock_classic_setup") or {}
            event_key = (sym, date)
            if (classic.get("eligible") or enriched.get("opportunity_bucket") in {"small_stock_classic", "high_risk_day_trade", "low_float_premarket"}) and event_key not in events:
                events[event_key] = {
                    "symbol": sym,
                    "date": date,
                    "first_seen_time_utc": hhmm,
                    "phase_at_detection": phase,
                    "phase_at_detection_ar": _phase_label_ar(phase),
                    "first_seen_price": _round(c, 4),
                    "first_seen_change_pct": _round(change, 2),
                    "max_gain_before_detection_pct": _round(max_gain_before, 2),
                    "detected_premarket_before_5pct": bool(phase == "premarket" and max(abs(change), abs(max_gain_before)) <= 5.0),
                    "detected_before_regular_open": bool(phase in {"overnight", "premarket"}),
                    "detected_at_or_before_open": bool(phase in {"overnight", "premarket", "open"}),
                    "chase_risk_at_detection": chase_code,
                    "chase_risk_label_ar": chase_label,
                    "previous_session_date": daily_dt,
                    "previous_day_high": _round(pday_high, 4),
                    "previous_day_high_distance_pct": _round(prev_high_dist, 2) if pday_high > 0 else 999.0,
                    "previous_close_gap_pct": _round(prev_close_gap, 2) if pday_close > 0 else 0.0,
                    "detected_previous_session": bool(was_prior_candidate),
                    "previous_candidate_dates": sorted(list(prior_candidate_dates_by_symbol.get(sym) or []))[-5:],
                    "stage": enriched.get("opportunity_stage"),
                    "bucket": enriched.get("opportunity_bucket"),
                    "classic_state": classic.get("setup_state"),
                    "classic_score": classic.get("score"),
                    "vwap": classic.get("vwap"),
                    "fib_levels": classic.get("fib_levels"),
                    "behavior_tags": (classic.get("behavior_group") or {}).get("tags", []),
                    "reasons": classic.get("reasons", [])[:8],
                    "max_after_price": _round(h, 4),
                    "max_after_time_utc": hhmm,
                    "min_after_price": _round(l, 4),
                    "min_after_time_utc": hhmm,
                    "max_after_pct": _round(((h - c) / c * 100.0) if c > 0 else 0.0, 2),
                    "min_after_pct": _round(((l - c) / c * 100.0) if c > 0 else 0.0, 2),
                    "peak_gain_after_detection_pct": _round(((h - c) / c * 100.0) if c > 0 else 0.0, 2),
                    "peak_price_note_ar": "أعلى سعر بعد الالتقاط محسوب من High لشموع الدقيقة، وليس شرطًا أنه إغلاق.",
                    "last_seen_price": _round(c, 4),
                    "last_seen_time_utc": hhmm,
                    "minutes_to_peak": 0,
                    "time_to_peak_label_ar": "سريع جدًا (أقل من 30 دقيقة)",
                    "post_peak_low_price": _round(l, 4),
                    "post_peak_low_time_utc": hhmm,
                    "post_peak_drawdown_pct": _round(((l - h) / h * 100.0) if h > 0 else 0.0, 2),
                    "drop_10_from_peak_time_utc": "",
                    "drop_20_from_peak_time_utc": "",
                    "minutes_to_drop_10pct_from_peak": None,
                    "minutes_to_drop_20pct_from_peak": None,
                    "minutes_after_peak_observed": 0,
                    "near_peak_10pct_minutes_after_peak": 0,
                    "end_gain_after_detection_pct": _round(((c - c) / c * 100.0) if c > 0 else 0.0, 2),
                    "gain_retention_pct": 0.0,
                    "exit_behavior_label_ar": "قيد التقييم",
                }
                prior_candidate_dates_by_symbol[sym].add(date)
            if event_key in events:
                ev = events[event_key]
                old_max = _num(ev.get("max_after_price"), c)
                old_min = _num(ev.get("min_after_price"), c)
                new_peak = h > old_max
                if new_peak:
                    ev["max_after_price"] = _round(h, 4)
                    ev["max_after_time_utc"] = hhmm
                    # Reset post-peak tracking when a new final high is made.
                    ev["post_peak_low_price"] = _round(l, 4)
                    ev["post_peak_low_time_utc"] = hhmm
                    ev["drop_10_from_peak_time_utc"] = ""
                    ev["drop_20_from_peak_time_utc"] = ""
                    ev["minutes_to_drop_10pct_from_peak"] = None
                    ev["minutes_to_drop_20pct_from_peak"] = None
                    ev["near_peak_10pct_minutes_after_peak"] = 0
                else:
                    ev["max_after_price"] = _round(old_max, 4)
                    peak_px_now = _num(ev.get("max_after_price"), old_max)
                    post_low = _num(ev.get("post_peak_low_price"), peak_px_now)
                    if l < post_low:
                        ev["post_peak_low_price"] = _round(l, 4)
                        ev["post_peak_low_time_utc"] = hhmm
                    if peak_px_now > 0:
                        if not ev.get("drop_10_from_peak_time_utc") and l <= peak_px_now * 0.90:
                            ev["drop_10_from_peak_time_utc"] = hhmm
                            ev["minutes_to_drop_10pct_from_peak"] = _minutes_between(ev.get("max_after_time_utc"), hhmm)
                        if not ev.get("drop_20_from_peak_time_utc") and l <= peak_px_now * 0.80:
                            ev["drop_20_from_peak_time_utc"] = hhmm
                            ev["minutes_to_drop_20pct_from_peak"] = _minutes_between(ev.get("max_after_time_utc"), hhmm)
                        if c >= peak_px_now * 0.90:
                            ev["near_peak_10pct_minutes_after_peak"] = int(_num(ev.get("near_peak_10pct_minutes_after_peak"), 0)) + 1
                if l < old_min:
                    ev["min_after_price"] = _round(l, 4)
                    ev["min_after_time_utc"] = hhmm
                else:
                    ev["min_after_price"] = _round(old_min, 4)
                ev["last_seen_price"] = _round(c, 4)
                ev["last_seen_time_utc"] = hhmm
                base = _num(ev.get("first_seen_price"), c)
                peak_px = _num(ev.get("max_after_price"), c)
                ev["max_after_pct"] = _round(((peak_px - base) / base * 100.0) if base > 0 else 0.0, 2)
                ev["peak_gain_after_detection_pct"] = ev["max_after_pct"]
                ev["min_after_pct"] = _round(((_num(ev.get("min_after_price"), c) - base) / base * 100.0) if base > 0 else 0.0, 2)
                ev["minutes_to_peak"] = _minutes_between(ev.get("first_seen_time_utc"), ev.get("max_after_time_utc"))
                ev["time_to_peak_label_ar"] = _time_to_label_ar(ev.get("minutes_to_peak"))
                ev["minutes_after_peak_observed"] = _minutes_between(ev.get("max_after_time_utc"), hhmm)
                post_low_px = _num(ev.get("post_peak_low_price"), peak_px)
                ev["post_peak_drawdown_pct"] = _round(((post_low_px - peak_px) / peak_px * 100.0) if peak_px > 0 else 0.0, 2)
                end_gain = ((c - base) / base * 100.0) if base > 0 else 0.0
                ev["end_gain_after_detection_pct"] = _round(end_gain, 2)
                peak_gain = _num(ev.get("max_after_pct"), 0.0)
                ev["gain_retention_pct"] = _round((end_gain / peak_gain * 100.0) if peak_gain > 0 else 0.0, 1)
                if _num(ev.get("post_peak_drawdown_pct"), 0.0) <= -20.0:
                    ev["exit_behavior_label_ar"] = "تلاشى بقوة بعد القمة"
                elif _num(ev.get("minutes_to_peak"), 0.0) <= 60 and _num(ev.get("post_peak_drawdown_pct"), 0.0) <= -10.0:
                    ev["exit_behavior_label_ar"] = "قمة سريعة ثم هبوط"
                elif _num(ev.get("minutes_to_peak"), 0.0) >= 240 and _num(ev.get("post_peak_drawdown_pct"), 0.0) > -10.0:
                    ev["exit_behavior_label_ar"] = "Runner حافظ نسبيًا"
                else:
                    ev["exit_behavior_label_ar"] = "حركة عادية تحتاج إدارة"
                ev.update(_exit_decision_overlay(ev))
        rows_by_file[source_name] = file_rows
        # Finalize this file's daily high/low/close so the next downloaded minute
        # file can use it as previous session context even when daily files are unavailable.
        for (sym, dt) in current_file_day_keys:
            st = day_state.get((sym, dt)) or {}
            if _num(st.get("high"), 0.0) > 0:
                prev_day_high[sym] = _num(st.get("high"), 0.0)
                prev_day_low[sym] = _num(st.get("low"), 0.0)
                prev_day_close[sym] = _num(st.get("close"), 0.0)

    candidates = sorted(events.values(), key=lambda x: (_num(x.get("max_after_pct"), 0.0), -abs(_num(x.get("max_gain_before_detection_pct"), 0.0))), reverse=True)[:max(1, int(max_candidates or 120))]
    grouped: dict[str, int] = defaultdict(int)
    phase_counts: dict[str, int] = defaultdict(int)
    timing_counts: dict[str, int] = defaultdict(int)
    for c in candidates:
        for tag in c.get("behavior_tags") or ["غير مصنف"]:
            grouped[tag] += 1
        phase_counts[str(c.get("phase_at_detection") or "unknown")] += 1
        if c.get("detected_previous_session"):
            timing_counts["detected_previous_session"] += 1
        if c.get("detected_premarket_before_5pct"):
            timing_counts["premarket_before_5pct"] += 1
        if c.get("detected_at_or_before_open"):
            timing_counts["at_or_before_open"] += 1
        if str(c.get("chase_risk_at_detection")) in {"late", "very_late"}:
            timing_counts["late_or_chase"] += 1
        else:
            timing_counts["early_or_acceptable"] += 1

    def _summary_item(c: dict) -> dict:
        return {
            "symbol": c.get("symbol"),
            "date": c.get("date"),
            "phase_at_detection": c.get("phase_at_detection"),
            "phase_at_detection_ar": c.get("phase_at_detection_ar"),
            "first_seen_time_utc": c.get("first_seen_time_utc"),
            "first_seen_price": c.get("first_seen_price"),
            "first_seen_change_pct": c.get("first_seen_change_pct"),
            "max_gain_before_detection_pct": c.get("max_gain_before_detection_pct"),
            "peak_price": c.get("max_after_price"),
            "peak_time_utc": c.get("max_after_time_utc"),
            "peak_gain_after_detection_pct": c.get("max_after_pct"),
            "worst_pullback_after_detection_pct": c.get("min_after_pct"),
            "detected_previous_session": c.get("detected_previous_session"),
            "previous_candidate_dates": c.get("previous_candidate_dates"),
            "chase_risk_at_detection": c.get("chase_risk_at_detection"),
            "chase_risk_label_ar": c.get("chase_risk_label_ar"),
            "classic_state": c.get("classic_state"),
            "stage": c.get("stage"),
            "minutes_to_peak": c.get("minutes_to_peak"),
            "time_to_peak_label_ar": c.get("time_to_peak_label_ar"),
            "post_peak_drawdown_pct": c.get("post_peak_drawdown_pct"),
            "drop_10_from_peak_time_utc": c.get("drop_10_from_peak_time_utc"),
            "minutes_to_drop_10pct_from_peak": c.get("minutes_to_drop_10pct_from_peak"),
            "gain_retention_pct": c.get("gain_retention_pct"),
            "exit_behavior_label_ar": c.get("exit_behavior_label_ar"),
            "exit_action_label_ar": c.get("exit_action_label_ar"),
            "suggested_sell_plan_ar": c.get("suggested_sell_plan_ar"),
            "runner_hold_candidate": c.get("runner_hold_candidate"),
            "quick_take_profit_candidate": c.get("quick_take_profit_candidate"),
            "risk_exit_flags": c.get("risk_exit_flags"),
        }

    top_peak_movers = [_summary_item(c) for c in candidates[:20]]
    top_previous_session_peak_movers = [_summary_item(c) for c in candidates if c.get("detected_previous_session")][:20]
    late_or_chase_peak_movers = [_summary_item(c) for c in candidates if str(c.get("chase_risk_at_detection")) in {"late", "very_late"}][:20]
    clean_winner_peak_movers = [_summary_item(c) for c in candidates if _candidate_is_clean_winner(c)][:20]
    clean_runner_peak_movers = [_summary_item(c) for c in candidates if _candidate_is_clean_runner_winner(c)][:20]
    quick_take_profit_movers = [_summary_item(c) for c in candidates if c.get("quick_take_profit_candidate")][:20]
    danger_winner_peak_movers = [_summary_item(c) for c in candidates if _candidate_is_danger_winner(c)][:20]
    peak_summary = {
        "note_ar": "أعلى ارتفاع هنا هو أعلى High ظهر في شموع الدقيقة بعد الالتقاط، حتى لو لم يغلق السهم عليه. استخدمه لقياس الإمكانات، وليس كربح مضمون.",
        "top_peak_movers": top_peak_movers,
        "top_previous_session_peak_movers": top_previous_session_peak_movers,
        "late_or_chase_peak_movers": late_or_chase_peak_movers,
        "clean_winner_peak_movers": clean_winner_peak_movers,
        "clean_runner_peak_movers": clean_runner_peak_movers,
        "quick_take_profit_movers": quick_take_profit_movers,
        "danger_winner_peak_movers": danger_winner_peak_movers,
    }
    performance_summary = _build_performance_summary(candidates)
    exit_behavior_summary = _build_exit_behavior_summary(candidates)
    railway_usage_guard = {
        "raw_storage": "temporary_tmp_only_deleted_after_pull_run",
        "row_cap_mode": "per_file_day",
        "max_rows_per_file": max_rows_per_file,
        "minute_files_seen": len(files_seen),
        "daily_files_seen": len(daily_files_seen),
        "analytics_mode": "compact_online_exit_metrics_no_raw_series",
        "note_ar": "للحفاظ على Railway: لا يتم حفظ raw، القراءة محدودة لكل يوم، ولا نخزن سلاسل الشموع الخام لكل سهم؛ نحسب مقاييس الخروج أونلاين ثم نرجع الملخص فقط. زد max_rows أو minute_days تدريجيًا فقط عند الحاجة.",
    }
    return {
        "ok": True,
        "version": MARKET_REPLAY_LAB_VERSION,
        "opportunity_radar_version": OPPORTUNITY_RADAR_VERSION,
        "source_path": resolved,
        "files_seen": files_seen[:30],
        "daily_files_seen": daily_files_seen[:30],
        "dates_seen": sorted(list(dates_seen)),
        "rows_seen": rows_seen,
        "rows_by_file": rows_by_file,
        "row_cap_mode": "per_file_day",
        "max_rows_per_file": max_rows_per_file,
        "candidate_count": len(candidates),
        "timing_summary": dict(timing_counts),
        "peak_summary": peak_summary,
        "performance_summary": performance_summary,
        "exit_behavior_summary": exit_behavior_summary,
        "railway_usage_guard": railway_usage_guard,
        "phase_counts": [{"phase": k, "label_ar": _phase_label_ar(k), "count": v} for k, v in sorted(phase_counts.items())],
        "behavior_groups": sorted([{"tag": k, "count": v} for k, v in grouped.items()], key=lambda x: x["count"], reverse=True)[:20],
        "candidates": candidates,
        "rule_ar": "هذه نتائج Replay مختصرة لا تخزن raw؛ تقيس متى ظهر السهم، هل ظهر قبل الافتتاح/قبل المطاردة، وهل كان مرشحًا في الجلسة السابقة إذا توفرت أيام متعددة.",
    }



def run_small_stock_classic_replay_from_polygon(
    *,
    end_date: str = "",
    minute_days: int = 5,
    max_rows: int = 250_000,
    max_candidates: int = 120,
    daily_lookback_days: int = 14,
    force: bool = False,
) -> dict[str, Any]:
    """Pull Polygon minute flat files to /tmp, replay them, then delete raw files.

    This is the production-safe path for Railway: the user does not need to
    manually place a ZIP in /tmp.  It uses the existing Polygon/Massive flat-file
    credentials and the fetcher's attempt cap.  Raw files are temporary only and
    are cleaned after compact replay results are produced.
    """
    try:
        from app.polygon_flatfile_fetcher import cleanup_tmp_path, flatfiles_config_status, pull_flatfiles_for_window
    except Exception as exc:
        return {
            "ok": False,
            "version": MARKET_REPLAY_LAB_VERSION,
            "error": "polygon_fetcher_unavailable",
            "detail": f"{type(exc).__name__}: {str(exc)[:180]}",
        }

    days = max(1, min(10, int(minute_days or 5)))
    # Pull enough daily context to cover the previous trading session even when
    # the immediately prior calendar day is a weekend/holiday.  Clamp to the
    # fetcher's safe limit so this stays Railway-friendly.
    daily_days = max(days + 8, int(daily_lookback_days or 14), 10)
    daily_days = max(1, min(35, daily_days))
    pull = pull_flatfiles_for_window(end_date=end_date or None, minute_days=days, daily_days=daily_days, force=bool(force))
    tmp_dir = str(pull.get("tmp_dir") or "")
    try:
        if not pull.get("ok"):
            return {
                "ok": False,
                "version": MARKET_REPLAY_LAB_VERSION,
                "error": "polygon_pull_failed_or_empty",
                "pull_status": pull,
                "config": flatfiles_config_status(),
                "hint_ar": "تأكد من تفعيل POLYGON_FLATFILES_ENABLED ومفاتيح Flat Files S3. لن تُحفظ الملفات الخام؛ التحميل مؤقت في /tmp فقط.",
            }
        minute_paths = [str(x) for x in (pull.get("minute_paths") or []) if str(x)]
        if not minute_paths:
            return {
                "ok": False,
                "version": MARKET_REPLAY_LAB_VERSION,
                "error": "no_minute_files_downloaded",
                "pull_status": pull,
                "hint_ar": "تم الاتصال لكن لم يتم تنزيل ملفات minute. ربما التاريخ غير متاح بعد أو وصل حد المحاولات.",
            }
        replay = run_small_stock_classic_replay_from_path(
            path=tmp_dir or str(Path(minute_paths[0]).parent),
            max_files=days,
            max_rows=max_rows,
            max_candidates=max_candidates,
        )
        replay["polygon_pull"] = {
            "ok": True,
            "minute_dates": pull.get("minute_dates"),
            "daily_dates": pull.get("daily_dates"),
            "minute_files_downloaded": len(minute_paths),
            "daily_files_downloaded": len([str(x) for x in (pull.get("daily_paths") or []) if str(x)]),
            "daily_days_requested_for_previous_session_context": daily_days,
            "daily_lookback_days_param": daily_lookback_days,
            "results_summary": [
                {
                    "dataset": r.get("dataset"),
                    "trade_date": r.get("trade_date"),
                    "status": r.get("status"),
                    "ok": r.get("ok"),
                    "attempts": r.get("attempts"),
                    "skipped": r.get("skipped"),
                    "error": r.get("error", ""),
                }
                for r in (pull.get("results") or [])
                if str(r.get("dataset")) == "minute"
            ],
        }
        replay["storage_rule_ar"] = "تم تنزيل ملفات Polygon مؤقتًا إلى /tmp للتشغيل ثم تنظيفها؛ لا يتم حفظ raw في SQLite/GitHub/Railway volume."
        return replay
    finally:
        if tmp_dir:
            cleanup_tmp_path(tmp_dir)
