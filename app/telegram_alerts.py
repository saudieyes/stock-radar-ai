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

TELEGRAM_ALERTS_VERSION = "telegram_buy_now_alerts_v1_2026_05_30"
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
    return (
        "🚨 دخول قوي مؤكد\n\n"
        f"السهم: {sym}\n"
        f"السعر الحالي: {_fmt_price(price)}\n"
        f"الدخول/Entry: {_fmt_price(entry)}\n"
        f"الهدف الأول: {_fmt_price(target)}\n"
        f"الوقف: {_fmt_price(stop)}\n"
        + (f"لا تطارد فوق: {_fmt_price(no_chase)}\n" if no_chase else "")
        + "\nالسبب:\n"
        + reason_text
        + "\n\nتنبيه: لا تدخل إذا تغيرت السيولة أو ابتعد السعر عن منطقة الدخول."
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
        if str(stock.get("final_decision_code") or "") != "BUY_NOW":
            continue
        if str(stock.get("decision") or "") != "دخول قوي":
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
