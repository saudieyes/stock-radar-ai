"""Paper Trading & Learning Engine V2.

Backend-only virtual portfolio used to measure tool decisions without touching
real money. V2 operates all agreed buckets, not Strong only:
- Strong Entry bucket: buys every qualified BUY_NOW/Strong signal.
- Cautious Entry bucket: buys selected Cautious signals.
- Day Trade bucket: buys selected intraday watch/early-movement candidates and
  exits quickly.
- Weekly bucket: buys selected Polygon Weekly / early-watch swing candidates for
  an approximate 10-session test.

Every buy/sell is recorded with reason, setup type, price, P/L and source so
future reports can learn which patterns win or fail.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

from app.sqlite_store import get_json, set_json

PAPER_TRADING_VERSION = "paper_trading_engine_v2_multibucket_auto_buy_2026_06_14"
NY_TZ = ZoneInfo("America/New_York")
STATE_KEY = "paper_trading:state_v2"
EVENTS_KEY = "paper_trading:events_v2"

DEFAULT_BUCKETS = {
    "strong": {
        "label_ar": "دخول قوي",
        "initial_cash": 6000.0,
        "default_trade_size": 1200.0,
        "max_positions": 5,
        "max_new_buys_per_scan": 10,
        "max_hold_days": 7,
    },
    "cautious": {
        "label_ar": "دخول بحذر",
        "initial_cash": 5000.0,
        "default_trade_size": 1000.0,
        "max_positions": 5,
        "max_new_buys_per_scan": 2,
        "max_hold_days": 5,
    },
    "day_trade": {
        "label_ar": "مضاربة يومية",
        "initial_cash": 2000.0,
        "default_trade_size": 500.0,
        "max_positions": 4,
        "max_new_buys_per_scan": 2,
        "max_hold_hours": 6.75,
    },
    "weekly": {
        "label_ar": "سوينغ/Polygon 10 جلسات",
        "initial_cash": 2000.0,
        "default_trade_size": 500.0,
        "max_positions": 4,
        "max_new_buys_per_scan": 2,
        "max_hold_days": 14,  # Approx. 10 trading sessions.
    },
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


def _now_dt() -> datetime:
    return datetime.now(NY_TZ)


def _now_str() -> str:
    return _now_dt().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return _now_dt().strftime("%Y-%m-%d")


def _price(row: dict) -> float:
    return _num(
        row.get("current_price_live")
        or row.get("display_price")
        or row.get("price")
        or row.get("current_price")
        or row.get("last_close")
        or row.get("close"),
        0.0,
    )


def _entry(row: dict) -> float:
    return _num(
        row.get("display_entry_price")
        or row.get("smart_entry_price")
        or row.get("entry_price_real")
        or row.get("entry")
        or row.get("suggested_watch_zone_high")
        or row.get("last_close")
        or row.get("price"),
        0.0,
    )


def _stop(row: dict) -> float:
    return _num(row.get("display_stop_price") or row.get("smart_stop_loss") or row.get("stop_loss") or row.get("stop") or row.get("invalidation"), 0.0)


def _target1(row: dict) -> float:
    return _num(row.get("display_target_price") or row.get("smart_target_1") or row.get("target_price") or row.get("target_1") or row.get("target1") or row.get("target") or row.get("first_target"), 0.0)


def _score(row: dict) -> float:
    return _num(row.get("quality_score") or row.get("signal_strength_score") or row.get("rank_score") or row.get("score") or row.get("raw_score"), 0.0)


def _setup_type(row: dict) -> str:
    text = " ".join(
        _s(row.get(k))
        for k in [
            "type",
            "trade_type",
            "plan_type",
            "setup_type",
            "pattern",
            "stage",
            "quality_bucket",
            "breakout_status",
            "owner_action",
            "active_strong_plan_action_ar",
        ]
    )
    text_l = text.lower()
    reasons_text = " ".join(_s(x) for x in (row.get("reasons") or []) if isinstance(x, (str, int, float))).lower()
    combined = f"{text_l} {reasons_text}"
    if "اختراق" in text or "breakout" in combined or "قمة" in text:
        return "breakout"
    if "استعادة" in text or "reclaim" in combined:
        return "reclaim"
    if "ارتداد" in text or "pullback" in combined or "support" in combined or "دعم" in text:
        return "support_bounce"
    if "weekly" in combined or "polygon" in combined:
        return "weekly_priority"
    if "زخم" in text or "momentum" in combined:
        return "momentum"
    return "unknown"


def _row_key(row: dict) -> str:
    sym = _u(row.get("symbol"))
    return sym


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
        "closed_trades_count": 0,
        "notes_ar": "محفظة وهمية للتعلم فقط؛ لا تنفذ أوامر حقيقية.",
    }


def _load_state() -> dict:
    state = get_json(STATE_KEY, None)
    if not isinstance(state, dict) or not isinstance(state.get("buckets"), dict):
        state = _initial_state()
        set_json(STATE_KEY, state)
    for key, cfg in DEFAULT_BUCKETS.items():
        if key not in state["buckets"]:
            state["buckets"][key] = {"cash": float(cfg["initial_cash"]), "initial_cash": float(cfg["initial_cash"]), "positions": []}
        state["buckets"][key].setdefault("positions", [])
        state["buckets"][key].setdefault("cash", float(cfg["initial_cash"]))
        state["buckets"][key].setdefault("initial_cash", float(cfg["initial_cash"]))
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
    if len(hist) > 3000:
        hist = hist[-1800:]
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


def _age_hours(pos: dict) -> float:
    ts = _num(pos.get("entry_ts"), 0.0)
    if ts <= 0:
        return 0.0
    return max(0.0, (_now_ts() - ts) / 3600.0)


def _age_days(pos: dict) -> float:
    return _age_hours(pos) / 24.0


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
    events.append({
        "event": "sell",
        "at": _now_str(),
        "symbol": pos.get("symbol"),
        "bucket": pos.get("bucket"),
        "price": round(price, 4),
        "reason": reason,
        "pnl_pct": round(pnl_pct, 3),
        "pnl": round(pnl, 4),
        "setup_type": pos.get("setup_type"),
    })


def _mark_and_exit_positions(state: dict, rows_by_symbol: dict[str, dict]) -> list[dict]:
    events: list[dict] = []
    today = _today_str()
    for bucket_name, bucket in (state.get("buckets") or {}).items():
        cfg = DEFAULT_BUCKETS.get(bucket_name, {})
        open_positions = []
        for pos in list(bucket.get("positions") or []):
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
            if bucket_name == "day_trade":
                max_h = _num(cfg.get("max_hold_hours"), 6.75)
                if _age_hours(pos) >= max_h or _s(pos.get("entry_day")) != today:
                    _sell_position(bucket, pos, price, "day_trade_time_exit", events)
                    continue
            else:
                max_d = _num(cfg.get("max_hold_days"), 0.0)
                if max_d > 0 and _age_days(pos) >= max_d:
                    _sell_position(bucket, pos, price, f"max_hold_days_{int(max_d)}", events)
                    continue
            open_positions.append(pos)
        bucket["positions"] = open_positions
    return events


def _find_position(bucket: dict, symbol: str) -> dict | None:
    for pos in bucket.get("positions") or []:
        if _u(pos.get("symbol")) == symbol and _s(pos.get("status") or "open") == "open":
            return pos
    return None


def _open_count(bucket: dict) -> int:
    return len([p for p in (bucket.get("positions") or []) if _s(p.get("status") or "open") == "open"])


def _liquidate_for_cash(bucket: dict, needed_cash: float, new_symbol: str, events: list[dict], bucket_name: str) -> float:
    """Sell weakest existing positions until needed cash is available.

    V2 keeps liquidity management inside the same bucket first. It sells the
    weakest P/L / lowest quality position, and documents the opportunity-cost
    decision.
    """
    positions = [p for p in (bucket.get("positions") or []) if _s(p.get("status") or "open") == "open" and _u(p.get("symbol")) != new_symbol]
    positions.sort(key=lambda p: (_unrealized_pct(p), _num(p.get("quality_score"), 0.0)))
    freed = 0.0
    while _num(bucket.get("cash"), 0.0) < needed_cash and positions:
        pos = positions.pop(0)
        price = _num(pos.get("last_price") or pos.get("entry_price"), 0.0)
        before_cash = _num(bucket.get("cash"), 0.0)
        _sell_position(bucket, pos, price, f"liquidity_needed_for_{bucket_name}_{new_symbol}", events)
        freed += max(0.0, _num(bucket.get("cash"), 0.0) - before_cash)
        bucket["positions"] = [p for p in (bucket.get("positions") or []) if p is not pos and _s(p.get("status") or "open") == "open"]
    return freed


def _buy(bucket_name: str, bucket: dict, row: dict, amount: float, events: list[dict], source: str, reason: str) -> bool:
    sym = _u(row.get("symbol"))
    price = _price(row) or _entry(row)
    if not sym or price <= 0 or amount <= 0:
        return False
    if _find_position(bucket, sym):
        return False
    cfg = DEFAULT_BUCKETS.get(bucket_name, {})
    max_positions = int(_num(cfg.get("max_positions"), 99))
    if _open_count(bucket) >= max_positions:
        # Make room only when this new row is not lower quality than the weakest.
        _liquidate_for_cash(bucket, amount, sym, events, bucket_name)
        if _open_count(bucket) >= max_positions and _num(bucket.get("cash"), 0.0) < amount:
            return False
    if _num(bucket.get("cash"), 0.0) < amount:
        _liquidate_for_cash(bucket, amount, sym, events, bucket_name)
    cash = _num(bucket.get("cash"), 0.0)
    if cash <= 0:
        return False
    amount = min(amount, cash)
    shares = amount / price
    pos = {
        "symbol": sym,
        "bucket": bucket_name,
        "bucket_label_ar": DEFAULT_BUCKETS.get(bucket_name, {}).get("label_ar", bucket_name),
        "status": "open",
        "entry_at": _now_str(),
        "entry_day": _today_str(),
        "entry_ts": _now_ts(),
        "entry_price": round(price, 4),
        "last_price": round(price, 4),
        "shares": round(shares, 8),
        "cost": round(amount, 4),
        "stop": _stop(row),
        "target_1": _target1(row),
        "target_2": _num(row.get("target_2") or row.get("second_target"), 0.0),
        "setup_type": _setup_type(row),
        "decision": _s(row.get("decision")),
        "final_decision_code": _s(row.get("final_decision_code")),
        "reason": reason,
        "source": source,
        "quality_score": _score(row),
        "plan_type": _s(row.get("type") or row.get("plan_type") or row.get("pattern") or row.get("quality_bucket")),
        "expected_hold_ar": "نفس اليوم" if bucket_name == "day_trade" else ("حوالي 10 جلسات" if bucket_name == "weekly" else "حتى الهدف/الوقف/تغير الخطة"),
    }
    bucket.setdefault("positions", []).append(pos)
    bucket["cash"] = round(cash - amount, 4)
    events.append({
        "event": "buy",
        "at": _now_str(),
        "symbol": sym,
        "bucket": bucket_name,
        "price": round(price, 4),
        "amount": round(amount, 2),
        "shares": round(shares, 6),
        "reason": reason,
        "setup_type": pos["setup_type"],
        "source": source,
    })
    return True


def _is_clean_row(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if bool(row.get("no_chase")):
        return False
    status = _s(row.get("sharia_manual_status") or row.get("manual_sharia_status"))
    if status in {"excluded", "blocked", "rejected"}:
        return False
    text = " ".join([_s(row.get("decision")), _s(row.get("final_decision_code")), _s(row.get("quality_bucket")), _s(row.get("stage"))])
    if "لا تطارد" in text or "مستبعد" in text or "غير شرعي" in text:
        return False
    return bool(_u(row.get("symbol"))) and (_price(row) > 0 or _entry(row) > 0)


def _rank_rows(rows: list[dict], limit: int = 50) -> list[dict]:
    seen = set()
    clean = []
    for row in rows or []:
        if not _is_clean_row(row):
            continue
        sym = _u(row.get("symbol"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        clean.append(row)
    clean.sort(key=lambda r: (_score(r), _num(r.get("volume_ratio_last_vs_prev20") or r.get("effective_volume_ratio") or r.get("volume_ratio"), 0.0)), reverse=True)
    return clean[:limit]


def _is_strong_buy(row: dict) -> bool:
    return _s(row.get("final_decision_code")) == "BUY_NOW" or _s(row.get("decision")) == "دخول قوي"


def _is_cautious_buy(row: dict) -> bool:
    txt = " ".join([_s(row.get("decision")), _s(row.get("final_decision_code")), _s(row.get("owner_action"))])
    return "دخول بحذر" in txt or "WAIT_TRIGGER" in txt or "تأكيد مبكر" in txt


def _is_day_trade_candidate(row: dict) -> bool:
    txt = " ".join([_s(row.get("decision")), _s(row.get("stage")), _s(row.get("pattern")), _s(row.get("early_movement_status")), _s(row.get("move_stage"))]).lower()
    if "دخول قوي" in txt or "دخول بحذر" in txt:
        return False
    return any(token in txt for token in ["early", "زخم", "حركة", "confirmed", "active breakout", "مراقبة"])


def _is_weekly_candidate(row: dict) -> bool:
    txt = " ".join([_s(row.get("stage")), _s(row.get("pattern")), _s(row.get("quality_bucket")), _s(row.get("source"))]).lower()
    return "weekly" in txt or "polygon" in txt or "clean weekly priority" in txt


def process_paper_trading_scan(
    strong_rows: list[dict] | None = None,
    cautious_rows: list[dict] | None = None,
    watch_rows: list[dict] | None = None,
    weekly_rows: list[dict] | None = None,
    source: str = "scan",
) -> dict[str, Any]:
    if not _enabled():
        return {"ok": True, "enabled": False, "version": PAPER_TRADING_VERSION, "processed": 0}
    strong_rows = strong_rows or []
    cautious_rows = cautious_rows or []
    watch_rows = watch_rows or []
    weekly_rows = weekly_rows or []
    all_rows = [r for r in (strong_rows + cautious_rows + watch_rows + weekly_rows) if isinstance(r, dict)]
    rows_by_symbol = {_u(r.get("symbol")): r for r in all_rows if _u(r.get("symbol"))}

    state = _load_state()
    events = _mark_and_exit_positions(state, rows_by_symbol)
    recently_closed = {_u(e.get("symbol")) for e in events if isinstance(e, dict) and e.get("event") == "sell"}
    buys_by_bucket = {"strong": 0, "cautious": 0, "day_trade": 0, "weekly": 0}

    def _bucket_buy(bucket_name: str, rows: list[dict], predicate, reason: str) -> None:
        bucket = state["buckets"].setdefault(bucket_name, {"cash": DEFAULT_BUCKETS[bucket_name]["initial_cash"], "initial_cash": DEFAULT_BUCKETS[bucket_name]["initial_cash"], "positions": []})
        amount = float(os.getenv(f"PAPER_{bucket_name.upper()}_TRADE_SIZE", DEFAULT_BUCKETS[bucket_name]["default_trade_size"]) or DEFAULT_BUCKETS[bucket_name]["default_trade_size"])
        max_new = int(_num(os.getenv(f"PAPER_{bucket_name.upper()}_MAX_NEW_BUYS", DEFAULT_BUCKETS[bucket_name]["max_new_buys_per_scan"]), DEFAULT_BUCKETS[bucket_name]["max_new_buys_per_scan"]))
        for row in _rank_rows(rows):
            if buys_by_bucket[bucket_name] >= max_new:
                break
            sym = _u(row.get("symbol"))
            if not sym or sym in recently_closed:
                continue
            if not predicate(row):
                continue
            if _buy(bucket_name, bucket, row, amount, events, source, reason):
                buys_by_bucket[bucket_name] += 1

    # User rule: every Strong Entry shown by the tool must be paper-bought.
    _bucket_buy("strong", strong_rows, _is_strong_buy, "paper_buy_strong_entry")

    # User rule: V2 must also buy Cautious, day-trade watch candidates and weekly/Polygon candidates.
    _bucket_buy("cautious", cautious_rows, _is_cautious_buy, "paper_buy_cautious_entry")
    _bucket_buy("day_trade", watch_rows, _is_day_trade_candidate, "paper_buy_day_trade_watch")
    _bucket_buy("weekly", weekly_rows, _is_weekly_candidate, "paper_buy_weekly_10_session")

    state["updated_at"] = _now_str()
    _save_state(state)
    _append_events(events)
    return {
        "ok": True,
        "enabled": True,
        "version": PAPER_TRADING_VERSION,
        "source": source,
        "buys_by_bucket": buys_by_bucket,
        "events": events[-20:],
        "open_positions": sum(len(b.get("positions") or []) for b in state.get("buckets", {}).values()),
        "rule_ar": "المحفظة الوهمية تشتري من الدخول القوي والحذر والمضاربة اليومية والسوينغ حسب ميزانية كل قسم، وتبيع أضعف مركز عند نقص السيولة.",
    }


def paper_trading_status() -> dict[str, Any]:
    state = _load_state()
    total_cash = 0.0
    open_positions = []
    bucket_summaries = {}
    for bucket_name, bucket in (state.get("buckets") or {}).items():
        cash = _num(bucket.get("cash"), 0.0)
        total_cash += cash
        bucket_value = cash
        positions_out = []
        for pos in bucket.get("positions") or []:
            if _s(pos.get("status") or "open") != "open":
                continue
            val = _position_value(pos)
            bucket_value += val
            item = dict(pos)
            item["unrealized_pct"] = round(_unrealized_pct(pos), 3)
            item["market_value"] = round(val, 2)
            item["age_hours"] = round(_age_hours(pos), 2)
            positions_out.append(item)
            open_positions.append(item)
        bucket["market_value_estimate"] = round(bucket_value, 2)
        bucket_summaries[bucket_name] = {
            "label_ar": DEFAULT_BUCKETS.get(bucket_name, {}).get("label_ar", bucket_name),
            "cash": round(cash, 2),
            "initial_cash": _num(bucket.get("initial_cash"), 0.0),
            "market_value_estimate": round(bucket_value, 2),
            "open_count": len(positions_out),
            "positions": positions_out,
        }
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
        "buckets": bucket_summaries,
        "open_positions": open_positions,
        "recent_events": events[-120:],
        "rule_ar": "تشتري المحفظة الوهمية من كل الأقسام: قوي، حذر، مضاربة يومية، وسوينغ/Polygon لمدة تقارب 10 جلسات. كل عملية شراء/بيع تحفظ السبب والربح/الخسارة.",
    }
