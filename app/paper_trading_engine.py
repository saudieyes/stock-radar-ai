"""Paper Trading & Learning Engine V1.

Backend-only virtual portfolio used to measure tool decisions without touching
real money. It records every paper buy/sell with reason, setup type, price and
P/L so future reports can learn which patterns win or fail.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from app.sqlite_store import get_json, set_json

PAPER_TRADING_VERSION = "paper_trading_engine_v1_strong_bucket_2026_06_14"
NY_TZ = ZoneInfo("America/New_York")
STATE_KEY = "paper_trading:state_v1"
EVENTS_KEY = "paper_trading:events_v1"

DEFAULT_BUCKETS = {
    "strong": {"label_ar": "دخول قوي", "initial_cash": 6000.0, "default_trade_size": 1500.0},
    "cautious": {"label_ar": "دخول بحذر", "initial_cash": 5000.0, "default_trade_size": 1000.0},
    "day_trade": {"label_ar": "مضاربة يومية", "initial_cash": 2000.0, "default_trade_size": 500.0},
    "weekly": {"label_ar": "سوينغ/Polygon", "initial_cash": 2000.0, "default_trade_size": 500.0},
}


def _enabled() -> bool:
    return str(os.getenv("PAPER_TRADING_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}


def _s(value: Any) -> str:
    return str(value or "").strip()


def _u(value: Any) -> str:
    return _s(value).upper()


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").replace("%", "").strip()
        return float(value)
    except Exception:
        return default


def _now_ts() -> float:
    return time.time()


def _now_str() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _price(row: dict) -> float:
    return _num(row.get("current_price_live") or row.get("display_price") or row.get("price") or row.get("current_price"), 0.0)


def _entry(row: dict) -> float:
    return _num(row.get("display_entry_price") or row.get("smart_entry_price") or row.get("entry_price_real") or row.get("entry") or row.get("price"), 0.0)


def _stop(row: dict) -> float:
    return _num(row.get("display_stop_price") or row.get("smart_stop_loss") or row.get("stop_loss") or row.get("stop"), 0.0)


def _target1(row: dict) -> float:
    return _num(row.get("display_target_price") or row.get("smart_target_1") or row.get("target_price") or row.get("target_1") or row.get("target1") or row.get("target"), 0.0)


def _setup_type(row: dict) -> str:
    text = " ".join(_s(row.get(k)) for k in ["type", "trade_type", "plan_type", "setup_type", "breakout_status", "owner_action", "active_strong_plan_action_ar"])
    if "اختراق" in text or "breakout" in text.lower():
        return "breakout"
    if "ارتداد" in text or "pullback" in text.lower() or "support" in text.lower():
        return "support_bounce"
    if "استعادة" in text or "reclaim" in text.lower():
        return "reclaim"
    return "unknown"


def _initial_state() -> dict:
    buckets: dict[str, dict] = {}
    total = 0.0
    for key, cfg in DEFAULT_BUCKETS.items():
        cash = float(cfg["initial_cash"])
        total += cash
        buckets[key] = {"cash": cash, "initial_cash": cash, "positions": []}
    return {
        "version": PAPER_TRADING_VERSION,
        "created_at": _now_str(),
        "updated_at": _now_str(),
        "initial_capital": total,
        "buckets": buckets,
        "realized_pnl": 0.0,
        "realized_pnl_pct": 0.0,
        "closed_trades_count": 0,
        "notes_ar": "محفظة وهمية للتعلم فقط؛ لا تنفذ أوامر حقيقية.",
    }


def _load_state() -> dict:
    state = get_json(STATE_KEY, None)
    if not isinstance(state, dict) or not isinstance(state.get("buckets"), dict):
        state = _initial_state()
        set_json(STATE_KEY, state)
    # Ensure any new buckets exist without resetting old state.
    for key, cfg in DEFAULT_BUCKETS.items():
        if key not in state["buckets"]:
            cash = float(cfg["initial_cash"])
            state["buckets"][key] = {"cash": cash, "initial_cash": cash, "positions": []}
    return state


def _save_state(state: dict) -> bool:
    state["version"] = PAPER_TRADING_VERSION
    state["updated_at"] = _now_str()
    return set_json(STATE_KEY, state)


def _append_events(events: list[dict]) -> None:
    if not events:
        return
    hist = get_json(EVENTS_KEY, []) or []
    if not isinstance(hist, list):
        hist = []
    hist.extend(events)
    if len(hist) > 2000:
        hist = hist[-1200:]
    set_json(EVENTS_KEY, hist)


def _position_value(pos: dict, mark: float | None = None) -> float:
    price = mark if mark is not None else _num(pos.get("last_price") or pos.get("entry_price"), 0.0)
    return max(0.0, _num(pos.get("shares"), 0.0) * price)


def _unrealized_pct(pos: dict) -> float:
    entry = _num(pos.get("entry_price"), 0.0)
    last = _num(pos.get("last_price") or entry, entry)
    if entry <= 0:
        return 0.0
    return ((last - entry) / entry) * 100.0


def _sell_position(bucket: dict, pos: dict, price: float, reason: str, events: list[dict]) -> None:
    shares = _num(pos.get("shares"), 0.0)
    if shares <= 0 or price <= 0:
        return
    proceeds = shares * price
    cost = _num(pos.get("cost"), 0.0)
    pnl = proceeds - cost
    pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
    bucket["cash"] = _num(bucket.get("cash"), 0.0) + proceeds
    pos["status"] = "closed"
    pos["exit_price"] = round(price, 4)
    pos["exit_at"] = _now_str()
    pos["exit_reason"] = reason
    pos["pnl"] = round(pnl, 4)
    pos["pnl_pct"] = round(pnl_pct, 3)
    events.append({"event": "sell", "at": _now_str(), "symbol": pos.get("symbol"), "bucket": pos.get("bucket"), "price": round(price, 4), "reason": reason, "pnl_pct": round(pnl_pct, 3), "pnl": round(pnl, 4), "setup_type": pos.get("setup_type")})


def _mark_and_exit_positions(state: dict, rows_by_symbol: dict[str, dict]) -> list[dict]:
    events: list[dict] = []
    for bucket_name, bucket in (state.get("buckets") or {}).items():
        positions = list(bucket.get("positions") or [])
        open_positions = []
        for pos in positions:
            if _s(pos.get("status") or "open") != "open":
                continue
            sym = _u(pos.get("symbol"))
            row = rows_by_symbol.get(sym, {})
            price = _price(row) if row else _num(pos.get("last_price") or pos.get("entry_price"), 0.0)
            if price > 0:
                pos["last_price"] = round(price, 4)
                pos["last_mark_at"] = _now_str()
            stop = _num(pos.get("stop"), 0.0)
            target1 = _num(pos.get("target_1"), 0.0)
            if price > 0 and stop > 0 and price <= stop:
                _sell_position(bucket, pos, price, "stop_loss_hit", events)
                continue
            if price > 0 and target1 > 0 and price >= target1:
                _sell_position(bucket, pos, price, "target_1_hit", events)
                continue
            open_positions.append(pos)
        bucket["positions"] = open_positions
    return events


def _find_position(bucket: dict, symbol: str) -> dict | None:
    for pos in bucket.get("positions") or []:
        if _u(pos.get("symbol")) == symbol and _s(pos.get("status") or "open") == "open":
            return pos
    return None


def _liquidate_for_cash(bucket: dict, needed_cash: float, new_symbol: str, events: list[dict]) -> float:
    """Sell weakest existing positions until needed cash is available."""
    positions = [p for p in (bucket.get("positions") or []) if _s(p.get("status") or "open") == "open" and _u(p.get("symbol")) != new_symbol]
    positions.sort(key=lambda p: (_unrealized_pct(p), _num(p.get("quality_score"), 0.0)))
    freed = 0.0
    while _num(bucket.get("cash"), 0.0) < needed_cash and positions:
        pos = positions.pop(0)
        price = _num(pos.get("last_price") or pos.get("entry_price"), 0.0)
        before_cash = _num(bucket.get("cash"), 0.0)
        _sell_position(bucket, pos, price, f"liquidity_needed_for_strong_entry_{new_symbol}", events)
        freed += max(0.0, _num(bucket.get("cash"), 0.0) - before_cash)
        bucket["positions"] = [p for p in (bucket.get("positions") or []) if p is not pos and _s(p.get("status") or "open") == "open"]
    return freed


def _buy(bucket_name: str, bucket: dict, row: dict, amount: float, events: list[dict], source: str) -> bool:
    sym = _u(row.get("symbol"))
    price = _price(row) or _entry(row)
    if not sym or price <= 0 or amount <= 0:
        return False
    if _find_position(bucket, sym):
        return False
    if _num(bucket.get("cash"), 0.0) < amount:
        _liquidate_for_cash(bucket, amount, sym, events)
    cash = _num(bucket.get("cash"), 0.0)
    if cash <= 0:
        return False
    amount = min(amount, cash)
    shares = amount / price
    pos = {
        "symbol": sym,
        "bucket": bucket_name,
        "status": "open",
        "entry_at": _now_str(),
        "entry_ts": _now_ts(),
        "entry_price": round(price, 4),
        "last_price": round(price, 4),
        "shares": round(shares, 8),
        "cost": round(amount, 4),
        "stop": _stop(row),
        "target_1": _target1(row),
        "target_2": _num(row.get("target_2"), 0.0),
        "setup_type": _setup_type(row),
        "decision": _s(row.get("decision")),
        "final_decision_code": _s(row.get("final_decision_code")),
        "reason": "paper_buy_strong_entry" if bucket_name == "strong" else "paper_buy",
        "source": source,
        "quality_score": _num(row.get("quality_score") or row.get("signal_strength_score") or row.get("score"), 0.0),
    }
    bucket.setdefault("positions", []).append(pos)
    bucket["cash"] = cash - amount
    events.append({"event": "buy", "at": _now_str(), "symbol": sym, "bucket": bucket_name, "price": round(price, 4), "amount": round(amount, 2), "shares": round(shares, 6), "reason": pos["reason"], "setup_type": pos["setup_type"], "source": source})
    return True


def process_paper_trading_scan(strong_rows: list[dict] | None = None, cautious_rows: list[dict] | None = None, source: str = "scan") -> dict[str, Any]:
    if not _enabled():
        return {"ok": True, "enabled": False, "version": PAPER_TRADING_VERSION, "processed": 0}
    strong_rows = strong_rows or []
    cautious_rows = cautious_rows or []
    rows_by_symbol = {_u(r.get("symbol")): r for r in (strong_rows + cautious_rows) if isinstance(r, dict) and _u(r.get("symbol"))}
    state = _load_state()
    events = _mark_and_exit_positions(state, rows_by_symbol)
    recently_closed = {_u(e.get("symbol")) for e in events if isinstance(e, dict) and e.get("event") == "sell"}
    buys = 0
    # User rule: every Strong Entry shown by the tool must be paper-bought. If
    # cash is unavailable, sell weakest existing position and document it.
    strong_bucket = state["buckets"].setdefault("strong", {"cash": 6000.0, "initial_cash": 6000.0, "positions": []})
    default_amount = float(os.getenv("PAPER_STRONG_TRADE_SIZE", DEFAULT_BUCKETS["strong"]["default_trade_size"]) or DEFAULT_BUCKETS["strong"]["default_trade_size"])
    for row in strong_rows:
        if not isinstance(row, dict):
            continue
        sym = _u(row.get("symbol"))
        if _s(row.get("final_decision_code")) != "BUY_NOW" or _s(row.get("decision")) != "دخول قوي":
            continue
        if sym in recently_closed:
            events.append({"event": "skip_rebuy_same_scan", "at": _now_str(), "symbol": sym, "bucket": "strong", "reason": "position_closed_this_scan"})
            continue
        if _buy("strong", strong_bucket, row, default_amount, events, source):
            buys += 1
    state["updated_at"] = _now_str()
    _save_state(state)
    _append_events(events)
    return {"ok": True, "enabled": True, "version": PAPER_TRADING_VERSION, "source": source, "buys": buys, "events": events[-10:], "open_positions": sum(len(b.get("positions") or []) for b in state.get("buckets", {}).values())}


def paper_trading_status() -> dict[str, Any]:
    state = _load_state()
    total_cash = 0.0
    total_value = 0.0
    open_positions = []
    for bucket_name, bucket in (state.get("buckets") or {}).items():
        cash = _num(bucket.get("cash"), 0.0)
        total_cash += cash
        bucket_value = cash
        for pos in bucket.get("positions") or []:
            if _s(pos.get("status") or "open") != "open":
                continue
            val = _position_value(pos)
            bucket_value += val
            item = dict(pos)
            item["unrealized_pct"] = round(_unrealized_pct(pos), 3)
            item["market_value"] = round(val, 2)
            open_positions.append(item)
        bucket["market_value_estimate"] = round(bucket_value, 2)
    total_value = sum(_num(b.get("market_value_estimate"), 0.0) for b in (state.get("buckets") or {}).values())
    initial = _num(state.get("initial_capital"), 15000.0)
    events = get_json(EVENTS_KEY, []) or []
    if not isinstance(events, list):
        events = []
    return {
        "ok": True,
        "enabled": _enabled(),
        "version": PAPER_TRADING_VERSION,
        "initial_capital": initial,
        "cash": round(total_cash, 2),
        "market_value_estimate": round(total_value, 2),
        "total_pnl_pct_estimate": round(((total_value - initial) / initial * 100.0), 3) if initial > 0 else 0.0,
        "buckets": state.get("buckets", {}),
        "open_positions": open_positions,
        "recent_events": events[-100:],
        "rule_ar": "كل دخول قوي يتم شراؤه وهميًا؛ إذا لا توجد سيولة يبيع المحرك أضعف مركز ويسجل السبب.",
    }
