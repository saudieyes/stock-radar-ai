"""Paper Trading Outcome / Learning Report V1.

Summarizes virtual buys/sells by bucket and setup type. It does not change
weights automatically yet; it exposes evidence so the tool can start learning
this week and later graduate to controlled weight updates.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.sqlite_store import get_json
from app.paper_trading_engine import PAPER_TRADING_VERSION

PAPER_LEARNING_REPORT_VERSION = "paper_learning_report_v1_outcome_evaluator_2026_06_14"
EVENTS_KEY = "paper_trading:events_v2"
STATE_KEY = "paper_trading:state_v2"


def _s(v: Any) -> str:
    return str(v or "").strip()


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def build_paper_learning_report() -> dict:
    events = get_json(EVENTS_KEY, []) or []
    state = get_json(STATE_KEY, {}) or {}
    if not isinstance(events, list):
        events = []
    closed = [e for e in events if isinstance(e, dict) and e.get("event") == "sell"]
    buys = [e for e in events if isinstance(e, dict) and e.get("event") == "buy"]
    by_bucket = defaultdict(lambda: {"buys": 0, "sells": 0, "wins": 0, "losses": 0, "pnl_pct_sum": 0.0, "pnl_sum": 0.0})
    by_setup = defaultdict(lambda: {"buys": 0, "sells": 0, "wins": 0, "losses": 0, "pnl_pct_sum": 0.0, "pnl_sum": 0.0})
    for e in buys:
        b = _s(e.get("bucket") or "unknown")
        st = _s(e.get("setup_type") or "unknown")
        by_bucket[b]["buys"] += 1
        by_setup[st]["buys"] += 1
    for e in closed:
        b = _s(e.get("bucket") or "unknown")
        st = _s(e.get("setup_type") or "unknown")
        pnl_pct = _num(e.get("pnl_pct"), 0.0)
        pnl = _num(e.get("pnl"), 0.0)
        for d in (by_bucket[b], by_setup[st]):
            d["sells"] += 1
            d["pnl_pct_sum"] += pnl_pct
            d["pnl_sum"] += pnl
            if pnl_pct > 0:
                d["wins"] += 1
            elif pnl_pct < 0:
                d["losses"] += 1
    def finalize(d: dict) -> dict:
        sells = max(1, int(d.get("sells", 0)))
        return {
            **d,
            "avg_pnl_pct": round(float(d.get("pnl_pct_sum", 0.0)) / sells, 3) if d.get("sells", 0) else 0.0,
            "win_rate_pct": round((float(d.get("wins", 0)) / sells) * 100.0, 1) if d.get("sells", 0) else 0.0,
            "pnl_sum": round(float(d.get("pnl_sum", 0.0)), 4),
        }
    open_positions = []
    for bucket_name, bucket in (state.get("buckets") or {}).items():
        for p in bucket.get("positions") or []:
            if _s(p.get("status") or "open") == "open":
                open_positions.append({
                    "symbol": p.get("symbol"),
                    "bucket": bucket_name,
                    "setup_type": p.get("setup_type"),
                    "entry_price": p.get("entry_price"),
                    "last_price": p.get("last_price"),
                    "unrealized_pct": round(((_num(p.get("last_price"), _num(p.get("entry_price"), 0)) - _num(p.get("entry_price"), 0)) / _num(p.get("entry_price"), 1)) * 100.0, 3) if _num(p.get("entry_price"), 0) > 0 else 0.0,
                    "reason": p.get("reason"),
                })
    return {
        "ok": True,
        "version": PAPER_LEARNING_REPORT_VERSION,
        "paper_engine_version": PAPER_TRADING_VERSION,
        "events_count": len(events),
        "buy_count": len(buys),
        "closed_count": len(closed),
        "open_count": len(open_positions),
        "by_bucket": {k: finalize(v) for k, v in by_bucket.items()},
        "by_setup_type": {k: finalize(v) for k, v in by_setup.items()},
        "open_positions": open_positions[:100],
        "recent_events": events[-60:],
        "rule_ar": "هذا تقرير تعلم أولي: يقارن الأنماط والأقسام الرابحة والخاسرة بدون تعديل تلقائي للأوزان حتى تتجمع بيانات كافية.",
    }
