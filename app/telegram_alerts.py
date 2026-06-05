"""Optional Telegram alerts for confirmed BUY_NOW signals.

Safe-by-default: if TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing, all calls
return disabled and never affect the trading scan.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import requests

from app.sqlite_store import get_json, set_json

TELEGRAM_ALERTS_VERSION = "telegram_buy_now_alerts_v2_buy_now_executable_2026_06_05"
NY_TZ = ZoneInfo("America/New_York")


def _cfg() -> dict[str, Any]:
    token = str(os.getenv("TELEGRAM_BOT_TOKEN", "") or "").strip()
    chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
    enabled = str(os.getenv("TELEGRAM_ALERTS_ENABLED", "true") or "true").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "version": TELEGRAM_ALERTS_VERSION,
        "enabled": bool(enabled and token and chat_id),
        "configured": bool(token and chat_id),
        "token": token,
        "chat_id": chat_id,
        "timeout_sec": float(os.getenv("TELEGRAM_ALERT_TIMEOUT_SEC", "6") or 6),
    }


def telegram_alert_status() -> dict[str, Any]:
    cfg = _cfg()
    last = get_json("telegram_alerts:last", {}) or {}
    return {
        "ok": True,
        "version": TELEGRAM_ALERTS_VERSION,
        "enabled": bool(cfg["enabled"]),
        "configured": bool(cfg["configured"]),
        "last_alert": last if isinstance(last, dict) else {},
        "dedupe_key_count": len(get_json("telegram_alerts:sent_keys", {}) or {}),
    }


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace("$", "").replace(",", "").replace("%", "").strip()
        return float(value)
    except Exception:
        return default


def _fmt_price(value: Any) -> str:
    n = _num(value, 0.0)
    return f"{n:.2f}" if n >= 1 else (f"{n:.4f}" if n > 0 else "—")




def _entry_distance_pct(stock: dict) -> float:
    try:
        val = stock.get("final_decision_entry_distance_pct")
        if val not in (None, ""):
            return float(val)
        price = _num(stock.get("current_price_live") or stock.get("display_price") or stock.get("price"), 0.0)
        entry = _num(stock.get("display_entry_price") or stock.get("entry_price") or stock.get("entry"), 0.0)
        if price > 0 and entry > 0:
            return ((price - entry) / entry) * 100.0
    except Exception:
        pass
    return 999.0


def _is_executable_buy_now(stock: dict) -> tuple[bool, list[str]]:
    """Telegram alert semantics: alert means buy-now executable, not wait."""
    blockers: list[str] = []
    if str(stock.get("final_decision_code") or "") != "BUY_NOW":
        blockers.append("final_decision_code_not_buy_now")
    if str(stock.get("decision") or "") != "دخول قوي":
        blockers.append("decision_not_strong")
    if not bool(stock.get("price_reliable_for_execution", False)):
        blockers.append("price_not_reliable_for_execution")
    plan_status = str(stock.get("plan_lifecycle_status") or "")
    if plan_status and plan_status != "execution_zone":
        blockers.append(f"plan_status_{plan_status}")
    dist = _entry_distance_pct(stock)
    if dist == 999.0 or dist < -0.75 or dist > 1.35:
        blockers.append(f"entry_distance_{round(dist, 3) if dist != 999.0 else 'missing'}")
    if stock.get("final_decision_blockers"):
        blockers.append("final_decision_has_blockers")
    if str(stock.get("final_decision_label") or "") not in {"دخول قوي مؤكد", ""}:
        blockers.append("label_not_confirmed_buy_now")
    text = " ".join(str(stock.get(k, "") or "") for k in ["owner_action", "execution_readiness_label", "plan_lifecycle_label"])
    if any(w in text for w in ["انتظر", "لا تطارد", "Pullback", "استعادة", "مكسورة", "غير مكتملة"]):
        blockers.append("action_text_is_wait_not_buy_now")
    return (not blockers), blockers[:8]

def _message(stock: dict) -> str:
    sym = str(stock.get("symbol") or "").upper().strip()
    price = stock.get("current_price_live") or stock.get("display_price") or stock.get("price")
    entry = stock.get("display_entry_price") or stock.get("entry_price") or stock.get("entry")
    target = stock.get("display_target_price") or stock.get("target_price") or stock.get("target_1")
    stop = stock.get("display_stop_price") or stock.get("stop_loss")
    reasons = stock.get("final_decision_liquidity_reasons") or stock.get("success_tags") or []
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    reason_text = "\n".join([f"- {str(x)}" for x in reasons[:4]]) or "- قرار نهائي مؤكد من الأداة"
    no_chase = stock.get("no_chase_above") or stock.get("display_no_chase_above") or ""
    invalidation = stock.get("plan_lifecycle_action") or stock.get("owner_action") or "يبطل التنبيه إذا ابتعد السعر عن منطقة الدخول أو ضعفت السيولة."
    return (
        "🚨 دخول قوي مؤكد — شراء الآن حسب شروط الأداة\n\n"
        f"السهم: {sym}\n"
        f"السعر الحالي الآن: {_fmt_price(price)}\n"
        f"منطقة/سعر الدخول: {_fmt_price(entry)}\n"
        f"الهدف الأول: {_fmt_price(target)}\n"
        f"وقف/إلغاء الخطة: {_fmt_price(stop)}\n"
        + (f"لا تطارد فوق: {_fmt_price(no_chase)}\n" if no_chase else "")
        + "\nالسبب:\n"
        + reason_text
        + "\n\nالإلغاء/التحذير:\n"
        + str(invalidation)[:220]
        + "\n\nهذا التنبيه لا يُرسل إلا عند BUY_NOW؛ إذا تغير السعر أو السيولة أعد فحص السهم."
    )


def maybe_send_buy_now_alerts(rows: list[dict], source: str = "trade_scan") -> dict[str, Any]:
    cfg = _cfg()
    if not cfg["enabled"]:
        return {"ok": True, "enabled": False, "configured": bool(cfg["configured"]), "sent": 0, "reason": "telegram_not_configured_or_disabled"}
    sent_keys = get_json("telegram_alerts:sent_keys", {}) or {}
    if not isinstance(sent_keys, dict):
        sent_keys = {}
    today = datetime.now(NY_TZ).strftime("%Y-%m-%d")
    sent = []
    errors = []
    for stock in rows or []:
        if not isinstance(stock, dict):
            continue
        sym = str(stock.get("symbol") or "").upper().strip()
        if not sym:
            continue
        executable, alert_blockers = _is_executable_buy_now(stock)
        if not executable:
            if os.getenv("TELEGRAM_ALERT_DEBUG", "false").lower() in {"1", "true", "yes"}:
                errors.append({"symbol": sym, "error": "blocked_alert:" + ",".join(alert_blockers[:4])})
            continue
        key = f"{today}:{sym}:BUY_NOW"
        if sent_keys.get(key):
            continue
        try:
            url = f"https://api.telegram.org/bot{cfg['token']}/sendMessage"
            resp = requests.post(
                url,
                json={"chat_id": cfg["chat_id"], "text": _message(stock), "disable_web_page_preview": True},
                timeout=cfg["timeout_sec"],
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"telegram_http_{resp.status_code}: {resp.text[:160]}")
            sent_keys[key] = {"symbol": sym, "sent_at": time.time(), "source": source}
            sent.append(sym)
            # Keep alert volume controlled even if many appear at once.
            if len(sent) >= int(os.getenv("TELEGRAM_ALERT_MAX_PER_SCAN", "5") or 5):
                break
        except Exception as exc:
            errors.append({"symbol": sym, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    # Keep only recent keys to avoid unbounded growth.
    if len(sent_keys) > 500:
        sent_keys = dict(list(sent_keys.items())[-300:])
    set_json("telegram_alerts:sent_keys", sent_keys)
    last = {"ok": not errors, "sent": sent, "errors": errors[:5], "source": source, "updated_at_ny": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S")}
    set_json("telegram_alerts:last", last)
    return {"ok": not errors, "enabled": True, "configured": True, "sent": len(sent), "symbols": sent, "errors": errors[:5]}
