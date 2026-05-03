from datetime import datetime, timedelta, time as dt_time
import json
from zoneinfo import ZoneInfo

from .settings import PERFORMANCE_FILE
from .sqlite_store import get_json, set_json
from .utils import ny_now, safe_round
def get_performance_week_window(base_dt=None):
    """
    Return the active performance week in New York trading terms.

    Weekend behavior:
    once the US week is finished, Saturday and Sunday are treated as the
    upcoming Monday-Friday performance week. This archives last week before
    the user prepares for Monday trading, instead of keeping stale signals
    as the active week until the first new signal appears.
    """
    dt = base_dt.astimezone(ZoneInfo("America/New_York")) if base_dt else ny_now()
    day = dt.date()
    weekday = day.weekday()

    if weekday >= 5:
        # Saturday/Sunday: prepare the upcoming Monday-Friday week.
        monday = day + timedelta(days=(7 - weekday))
    else:
        monday = day - timedelta(days=weekday)

    friday = monday + timedelta(days=4)
    return monday.isoformat(), friday.isoformat()


def get_performance_week_key(base_dt=None):
    week_start, week_end = get_performance_week_window(base_dt)
    return f"{week_start}_{week_end}"


def make_blank_performance_store(base_dt=None):
    week_start, week_end = get_performance_week_window(base_dt)
    return {
        "active_week_key": get_performance_week_key(base_dt),
        "active_week_start": week_start,
        "active_week_end": week_end,
        "active_records": [],
        "weekly_archive": [],
    }


def make_performance_summary(records):
    total = len(records)
    wins = sum(1 for r in records if str(r.get("outcome", "") or "") in {"target_hit", "above_target"})
    losses = sum(1 for r in records if str(r.get("outcome", "") or "") == "loss")
    pending = sum(1 for r in records if str(r.get("outcome", "") or "") not in {"target_hit", "above_target", "loss"})
    win_rate = safe_round((wins / total) * 100, 2) if total > 0 else 0.0
    return {
        "count": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate_pct": win_rate,
    }


def _count_outcomes(records):
    counts = {
        "above_target": 0,
        "target_hit": 0,
        "partial_gain": 0,
        "ongoing": 0,
        "loss": 0,
        "expired": 0,
    }
    for row in records or []:
        outcome = str(row.get("outcome", "ongoing") or "ongoing")
        if outcome not in counts:
            outcome = "ongoing"
        counts[outcome] += 1
    return counts


def build_archive_summary_for_week(week_start, week_end, records):
    summary = make_performance_summary(records)
    outcome_counts = _count_outcomes(records or [])
    return {
        "week_key": f"{week_start}_{week_end}",
        "week_start": week_start,
        "week_end": week_end,
        "count": summary["count"],
        "wins": summary["wins"],
        "losses": summary["losses"],
        "pending": summary["pending"],
        "win_rate_pct": summary["win_rate_pct"],
        "above_target": outcome_counts.get("above_target", 0),
        "target_hit": outcome_counts.get("target_hit", 0),
        "partial_gain": outcome_counts.get("partial_gain", 0),
        "ongoing": outcome_counts.get("ongoing", 0),
        "loss": outcome_counts.get("loss", 0),
        "expired": outcome_counts.get("expired", 0),
        "archived_at": ny_now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def normalize_performance_store(store):
    if not isinstance(store, dict):
        store = make_blank_performance_store()
    store.setdefault("active_week_key", get_performance_week_key())
    store.setdefault("active_week_start", get_performance_week_window()[0])
    store.setdefault("active_week_end", get_performance_week_window()[1])
    store.setdefault("active_records", [])
    store.setdefault("weekly_archive", [])
    if not isinstance(store.get("active_records"), list):
        store["active_records"] = []
    if not isinstance(store.get("weekly_archive"), list):
        store["weekly_archive"] = []
    return store


def migrate_legacy_performance_items(items):
    store = make_blank_performance_store()
    week_start = store["active_week_start"]
    week_end = store["active_week_end"]
    records = []
    for item in items if isinstance(items, list) else []:
        signal_type = str(item.get("signal_type", "") or "")
        if signal_type not in {"دخول قوي", "دخول بحذر"}:
            continue
        item_date = str(item.get("date", "") or "")
        if not item_date or item_date < week_start or item_date > week_end:
            continue
        entry_price = float(item.get("entry_price", 0) or 0)
        if entry_price <= 0:
            continue
        created_at = f"{item_date} {str(item.get('time', '') or '').strip()}".strip()
        records.append({
            "id": f"{store['active_week_key']}::{str(item.get('symbol', '')).upper().strip()}",
            "symbol": str(item.get("symbol", "") or "").upper().strip(),
            "signal_type": signal_type,
            "entry_price": safe_round(entry_price),
            "target_price": 0.0,
            "stop_loss": 0.0,
            "first_seen_at": created_at,
            "last_seen_at": created_at,
            "current_price": float(item.get("current_price", 0) or 0),
            "max_price_seen": float(item.get("current_price", 0) or 0),
            "min_price_seen": float(item.get("current_price", 0) or 0),
            "price_source_label": str(item.get("price_source", "") or ""),
            "strategy_label": str(item.get("strategy_label", "") or ""),
            "status_mark": "⏳",
            "status_label": "قيد المتابعة",
            "outcome": "pending",
            "closed_at": "",
            "market_phase": str(item.get("market_phase", "") or ""),
            "last_change_pct": float(item.get("change_pct", 0) or 0),
        })
    store["active_records"] = records[:200]
    return store


def collapse_performance_duplicates(records: list[dict]) -> list[dict]:
    collapsed = []
    for row in list(records or []):
        symbol = str(row.get("symbol", "") or "").upper().strip()
        signal_type = str(row.get("signal_type", "") or "")
        plan_family = str(row.get("plan_family", row.get("strategy_label", "")) or "")
        week_key = str(row.get("id", "") or "").split("::")[0] if row.get("id") else ""
        base_id = str(row.get("base_id", "") or "") or build_signal_record_base_id(week_key or get_performance_week_key(), symbol, signal_type, plan_family)
        row["base_id"] = base_id
        row["plan_signature"] = str(row.get("plan_signature", "") or "") or _plan_signature(row.get("entry_price", 0), row.get("target_price", 0), row.get("stop_loss", 0))
        existing = next((item for item in collapsed if item.get("base_id") == base_id and not _plan_change_significant(item, row.get("entry_price", 0), row.get("target_price", 0), row.get("stop_loss", 0))), None)
        if existing is None:
            row.setdefault("revision", 1)
            row.setdefault("times_seen_count", 1)
            collapsed.append(row)
            continue
        existing["last_seen_at"] = max(str(existing.get("last_seen_at", "") or ""), str(row.get("last_seen_at", "") or ""))
        existing["max_price_seen"] = max(float(existing.get("max_price_seen", 0) or 0), float(row.get("max_price_seen", 0) or 0))
        min_candidates = [float(x.get("min_price_seen", 0) or 0) for x in [existing, row] if float(x.get("min_price_seen", 0) or 0) > 0]
        if min_candidates:
            existing["min_price_seen"] = min(min_candidates)
        existing["times_seen_count"] = int(existing.get("times_seen_count", 1) or 1) + int(row.get("times_seen_count", 1) or 1)
        if str(row.get("closed_at", "") or ""):
            existing["closed_at"] = existing.get("closed_at") or row.get("closed_at")
        if outcome_sort_rank(row.get("outcome")) < outcome_sort_rank(existing.get("outcome")):
            existing["outcome"] = row.get("outcome")
            existing["status_mark"] = row.get("status_mark")
            existing["status_label"] = row.get("status_label")
        existing["current_price"] = row.get("current_price", existing.get("current_price", 0))
    return collapsed


def load_performance_store():
    raw = get_json("performance_store", None)
    if raw is None:
        try:
            with open(PERFORMANCE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except:
            raw = None

    if raw is None:
        return make_blank_performance_store()

    if isinstance(raw, list):
        store = migrate_legacy_performance_items(raw)
        store["active_records"] = collapse_performance_duplicates(store.get("active_records", []))[:500]
        set_json("performance_store", store)
        return store

    store = normalize_performance_store(raw)
    store["active_records"] = collapse_performance_duplicates(store.get("active_records", []))[:500]
    return store


def save_performance_store(store):
    try:
        payload = normalize_performance_store(store)
        set_json("performance_store", payload)
        tmp_path = f"{PERFORMANCE_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        try:
            import os
            os.replace(tmp_path, PERFORMANCE_FILE)
        except Exception:
            with open(PERFORMANCE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"PERFORMANCE_SAVE_ERROR: {type(e).__name__}: {str(e)[:240]}", flush=True)


def _archive_week_records(store, old_records, old_week_start, old_week_end):
    if not old_records or not old_week_start or not old_week_end:
        return store
    archive_entry = build_archive_summary_for_week(old_week_start, old_week_end, old_records)
    archive = list(store.get("weekly_archive", []) or [])
    existing_index = next((i for i, row in enumerate(archive) if row.get("week_key") == archive_entry["week_key"]), None)
    if existing_index is None:
        archive.insert(0, archive_entry)
    else:
        archive[existing_index] = archive_entry
    archive.sort(key=lambda row: str(row.get("week_start", "") or ""), reverse=True)
    store["weekly_archive"] = archive[:26]
    return store


def rollover_performance_store_if_needed(store, base_dt=None):
    store = normalize_performance_store(store)
    current_week_key = get_performance_week_key(base_dt)
    if store.get("active_week_key") == current_week_key:
        return store

    old_records = list(store.get("active_records", []))
    old_week_start = str(store.get("active_week_start", "") or "")
    old_week_end = str(store.get("active_week_end", "") or "")
    store = _archive_week_records(store, old_records, old_week_start, old_week_end)

    new_store = make_blank_performance_store(base_dt)
    new_store["weekly_archive"] = store.get("weekly_archive", [])[:26]
    return new_store


def parse_validity_days(valid_for: str) -> int:
    txt = str(valid_for or "")
    if "اليوم فقط" in txt:
        return 1
    if "الجلسة القادمة" in txt:
        return 2
    if "1-2" in txt:
        return 2
    if "1-3" in txt:
        return 3
    if "مراقبة" in txt:
        return 5
    return 3


def estimate_signal_expired(record: dict) -> bool:
    try:
        first_seen = str(record.get("first_seen_at", "") or "")
        if not first_seen:
            return False
        first_dt = datetime.strptime(first_seen[:19], "%Y-%m-%d %H:%M:%S")
        days_allowed = int(record.get("signal_ttl_days", 3) or 3)
        return (ny_now().replace(tzinfo=None) - first_dt).days >= days_allowed
    except:
        return False


def outcome_sort_rank(outcome: str) -> int:
    order = {
        "above_target": 0,
        "target_hit": 1,
        "partial_gain": 2,
        "ongoing": 3,
        "loss": 4,
        "expired": 5,
    }
    return order.get(str(outcome or "ongoing"), 9)


def evaluate_performance_record(record, current_price):
    entry_price = float(record.get("entry_price", 0) or 0)
    target_price = float(record.get("target_price", 0) or 0)
    target_2_price = float(record.get("target_2_price", 0) or 0)
    stop_loss = float(record.get("stop_loss", 0) or 0)
    current_price = float(current_price or 0)

    if current_price > 0:
        if float(record.get("max_price_seen", 0) or 0) <= 0:
            record["max_price_seen"] = current_price
        else:
            record["max_price_seen"] = max(float(record.get("max_price_seen", 0) or 0), current_price)

        if float(record.get("min_price_seen", 0) or 0) <= 0:
            record["min_price_seen"] = current_price
        else:
            record["min_price_seen"] = min(float(record.get("min_price_seen", current_price) or current_price), current_price)

        record["current_price"] = safe_round(current_price)

    change_pct = 0.0
    if entry_price > 0 and current_price > 0:
        change_pct = ((current_price - entry_price) / entry_price) * 100
    record["last_change_pct"] = safe_round(change_pct)

    max_seen = float(record.get("max_price_seen", 0) or 0)
    min_seen = float(record.get("min_price_seen", 0) or 0)
    now_str = ny_now().strftime("%Y-%m-%d %H:%M:%S")

    outcome = "ongoing"
    label = "مستمرة"
    mark = "⏳"

    if target_2_price > 0 and max_seen >= target_2_price:
        outcome, label, mark = "above_target", "تجاوزت الهدف", "🚀"
    elif target_price > 0 and max_seen >= target_price:
        outcome, label, mark = "target_hit", "وصلت الهدف", "✅"
    elif stop_loss > 0 and min_seen > 0 and min_seen <= stop_loss:
        outcome, label, mark = "loss", "خاسرة", "❌"
    else:
        expired = estimate_signal_expired(record)
        positive_move_pct = ((max_seen - entry_price) / entry_price) * 100 if entry_price > 0 and max_seen > 0 else 0.0
        if expired:
            if positive_move_pct >= 0.8 or change_pct >= 0.8:
                outcome, label, mark = "partial_gain", "صعد أقل من الهدف", "⚠️"
            else:
                outcome, label, mark = "expired", "منتهية بلا حسم", "🕓"
        else:
            outcome, label, mark = "ongoing", "مستمرة", "⏳"

    record["outcome"] = outcome
    record["status_mark"] = mark
    record["status_label"] = label
    if outcome in {"above_target", "target_hit", "loss", "partial_gain", "expired"}:
        record["closed_at"] = record.get("closed_at") or now_str
    return record


def _bucket_plan_price(value: float) -> float:
    try:
        value = float(value or 0)
        if value <= 0:
            return 0.0
        if value < 5:
            return safe_round(value, 2)
        if value < 25:
            return safe_round(value, 1)
        return safe_round(round(value * 2.0) / 2.0, 2)
    except:
        return 0.0


def _plan_signature(entry_price: float, target_price: float, stop_loss: float) -> str:
    return f"{_bucket_plan_price(entry_price)}::{_bucket_plan_price(target_price)}::{_bucket_plan_price(stop_loss)}"


def _plan_change_significant(existing: dict, entry_price: float, target_price: float, stop_loss: float) -> bool:
    try:
        old_sig = str(existing.get("plan_signature", "") or "")
        new_sig = _plan_signature(entry_price, target_price, stop_loss)
        if not old_sig:
            return True
        if old_sig == new_sig:
            return False
        old_entry = float(existing.get("entry_price", 0) or 0)
        old_target = float(existing.get("target_price", 0) or 0)
        old_stop = float(existing.get("stop_loss", 0) or 0)
        entry_diff = abs(float(entry_price or 0) - old_entry)
        target_diff = abs(float(target_price or 0) - old_target)
        stop_diff = abs(float(stop_loss or 0) - old_stop)
        base = max(old_entry, float(entry_price or 0), 1.0)
        if entry_diff <= max(0.12, base * 0.012) and target_diff <= max(0.18, base * 0.02) and stop_diff <= max(0.12, base * 0.012):
            return False
        return True
    except:
        return True


def build_signal_record_base_id(week_key: str, symbol: str, signal_type: str, plan_family: str) -> str:
    return f"{week_key}::{symbol}::{signal_type}::{plan_family}"


def upsert_performance_signal(stock: dict):
    try:
        now_str = ny_now().strftime("%Y-%m-%d %H:%M:%S")
        signal_type = str(stock.get("decision", "") or "")
        if signal_type not in {"دخول قوي", "دخول بحذر"}:
            return
        symbol = str(stock.get("symbol", "") or "").upper().strip()
        if not symbol:
            return
        entry_price = float(
            stock.get("display_entry_price",
            stock.get("entry_price_real",
            stock.get("entry", 0))) or 0
        )
        target_price = float(
            stock.get("display_target_price",
            stock.get("target_1",
            stock.get("breakout_target", 0))) or 0
        )
        stop_loss = float(
            stock.get("display_stop_price",
            stock.get("stop_loss",
            stock.get("atr_stop_price", 0))) or 0
        )
        target_2_price = float(stock.get("target_2", 0) or 0)
        current_price = float(stock.get("display_price", stock.get("current_price_live", 0)) or 0)
        if entry_price <= 0:
            return
        store = rollover_performance_store_if_needed(load_performance_store())
        records = store.get("active_records", [])
        plan_family = str(stock.get("display_plan_family", stock.get("type", "")) or "")
        base_id = build_signal_record_base_id(
            store['active_week_key'],
            symbol,
            signal_type,
            plan_family,
        )
        plan_signature = _plan_signature(entry_price, target_price, stop_loss)
        same_base = [item for item in records if item.get("base_id") == base_id]
        same_base.sort(key=lambda row: str(row.get("first_seen_at", "") or ""), reverse=True)
        open_existing = next((item for item in same_base if str(item.get("outcome", "ongoing") or "ongoing") in {"ongoing", "pending"}), None)
        latest_existing = same_base[0] if same_base else None
        existing = None
        if open_existing is not None and not _plan_change_significant(open_existing, entry_price, target_price, stop_loss):
            existing = open_existing
        elif latest_existing is not None and not _plan_change_significant(latest_existing, entry_price, target_price, stop_loss):
            existing = latest_existing
        revision = 1
        if same_base:
            revision = max(int(item.get("revision", 1) or 1) for item in same_base) + (0 if existing is not None else 1)
        record_id = f"{base_id}::R{revision}"
        if existing is None:
            existing = {
                "id": record_id,
                "base_id": base_id,
                "revision": revision,
                "plan_signature": plan_signature,
                "times_seen_count": 1,
                "symbol": symbol,
                "signal_type": signal_type,
                "plan_family": plan_family,
                "entry_price": safe_round(entry_price),
                "target_price": safe_round(target_price),
                "target_2_price": safe_round(target_2_price),
                "stop_loss": safe_round(stop_loss),
                "first_seen_at": now_str,
                "last_seen_at": now_str,
                "current_price": safe_round(current_price),
                "max_price_seen": safe_round(current_price if current_price > 0 else entry_price),
                "min_price_seen": safe_round(current_price if current_price > 0 else entry_price),
                "price_source_label": str(stock.get("price_source_label", stock.get("price_source", "")) or ""),
                "strategy_label": str(stock.get("strategy_label", "") or ""),
                "status_mark": "⏳",
                "status_label": "مستمرة",
                "outcome": "ongoing",
                "closed_at": "",
                "market_phase": str(stock.get("market_phase_label", stock.get("market_phase", "")) or ""),
                "last_change_pct": 0.0,
                "valid_for": str(stock.get("valid_for", "") or ""),
                "signal_ttl_days": parse_validity_days(stock.get("valid_for", "")),
            }
            records.insert(0, existing)
        else:
            existing["last_seen_at"] = now_str
            existing["price_source_label"] = str(stock.get("price_source_label", stock.get("price_source", "")) or existing.get("price_source_label", ""))
            existing["strategy_label"] = str(stock.get("strategy_label", "") or existing.get("strategy_label", ""))
            existing["market_phase"] = str(stock.get("market_phase_label", stock.get("market_phase", "")) or existing.get("market_phase", ""))
            existing["target_2_price"] = max(float(existing.get("target_2_price", 0) or 0), target_2_price)
            existing["plan_signature"] = plan_signature or existing.get("plan_signature", "")
            existing["times_seen_count"] = int(existing.get("times_seen_count", 1) or 1) + 1

        evaluate_performance_record(existing, current_price)
        store["active_records"] = records[:500]
        save_performance_store(store)
    except Exception as e:
        try:
            symbol = str((stock or {}).get("symbol", "") or "")
        except Exception:
            symbol = ""
        print(f"PERFORMANCE_TRACKER_ERROR: {symbol} | {type(e).__name__}: {str(e)[:240]}", flush=True)


