import json
import os
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .settings import (
    GITHUB_SYNC_MANUAL_SHARIA_PATH,
    GITHUB_SYNC_MANUAL_SHARIA_APPROVALS_PATH,
    GITHUB_SYNC_PULL_TTL_SEC,
    MANUAL_SHARIA_EXCLUSIONS_FILE,
    MANUAL_SHARIA_APPROVALS_FILE,
)
from .utils import normalize_symbol_text
from .sqlite_store import get_json, set_json

_GITHUB_PULL_CACHE = {"ts": 0.0, "ok": False, "count": 0, "error": ""}


def _now_text() -> str:
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_exclusion_items(raw) -> list[dict]:
    """Accept old list format or new dict format and return deduped newest-first rows."""
    rows = []
    if isinstance(raw, dict):
        iterable = []
        for key, value in raw.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("symbol", key)
                iterable.append(item)
            else:
                iterable.append({"symbol": key, "note": str(value or "")})
    elif isinstance(raw, list):
        iterable = raw
    else:
        iterable = []

    merged = {}
    order = []
    for row in iterable:
        if isinstance(row, str):
            symbol = normalize_symbol_text(row)
            item = {"symbol": symbol, "note": "", "reason": "", "excluded_at": "", "updated_at": "", "source": "manual"}
        elif isinstance(row, dict):
            symbol = normalize_symbol_text(row.get("symbol", ""))
            item = {
                "symbol": symbol,
                "note": str(row.get("note", "") or row.get("reason", "") or "").strip(),
                "reason": str(row.get("reason", "") or row.get("note", "") or "").strip(),
                "excluded_at": str(row.get("excluded_at", "") or row.get("created_at", "") or "").strip(),
                "updated_at": str(row.get("updated_at", "") or row.get("excluded_at", "") or "").strip(),
                "source": str(row.get("source", "") or "manual").strip(),
            }
        else:
            continue
        if not symbol:
            continue
        if not item.get("excluded_at"):
            item["excluded_at"] = _now_text()
        if not item.get("updated_at"):
            item["updated_at"] = item.get("excluded_at", "") or _now_text()
        if symbol not in merged:
            order.append(symbol)
            merged[symbol] = item
        else:
            # Preserve the newest note/date when duplicates exist.
            existing = merged[symbol]
            if str(item.get("updated_at", "")) >= str(existing.get("updated_at", "")):
                merged[symbol].update({k: v for k, v in item.items() if v})
    return [merged[s] for s in order if s in merged]


def _items_to_dict(items: list[dict]) -> dict:
    out = {}
    for item in _normalize_exclusion_items(items):
        symbol = normalize_symbol_text(item.get("symbol", ""))
        if not symbol:
            continue
        out[symbol] = {
            "symbol": symbol,
            "note": str(item.get("note", "") or "").strip(),
            "reason": str(item.get("reason", "") or item.get("note", "") or "").strip(),
            "excluded_at": str(item.get("excluded_at", "") or "").strip(),
            "updated_at": str(item.get("updated_at", "") or item.get("excluded_at", "") or "").strip(),
            "source": str(item.get("source", "") or "manual").strip(),
        }
    return out


def _safe_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _load_local_raw():
    try:
        with open(MANUAL_SHARIA_EXCLUSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merge_remote_once(local_items: list[dict], force: bool = False) -> list[dict]:
    now = time.time()
    if not force and now - float(_GITHUB_PULL_CACHE.get("ts", 0) or 0) < int(GITHUB_SYNC_PULL_TTL_SEC or 900):
        return local_items
    try:
        from .github_sync import fetch_json_file, is_github_sync_configured
        if not is_github_sync_configured():
            _GITHUB_PULL_CACHE.update({"ts": now, "ok": False, "count": 0, "error": "not_configured"})
            return local_items
        remote = fetch_json_file(GITHUB_SYNC_MANUAL_SHARIA_PATH)
        if not remote.get("ok") or remote.get("data") is None:
            _GITHUB_PULL_CACHE.update({"ts": now, "ok": bool(remote.get("ok")), "count": 0, "error": str(remote.get("error", ""))[:160]})
            return local_items
        merged = _items_to_dict(local_items)
        remote_items = _normalize_exclusion_items(remote.get("data"))
        for item in remote_items:
            symbol = normalize_symbol_text(item.get("symbol", ""))
            if not symbol:
                continue
            existing = merged.get(symbol)
            if not existing or str(item.get("updated_at", "")) >= str(existing.get("updated_at", "")):
                merged[symbol] = item
        merged_items = list(merged.values())
        _safe_write_json(MANUAL_SHARIA_EXCLUSIONS_FILE, _items_to_dict(merged_items))
        _GITHUB_PULL_CACHE.update({"ts": now, "ok": True, "count": len(remote_items), "error": ""})
        return _normalize_exclusion_items(merged)
    except Exception as exc:
        _GITHUB_PULL_CACHE.update({"ts": now, "ok": False, "count": 0, "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
        return local_items


def load_manual_sharia_exclusions(force_github_pull: bool = False):
    sqlite_items = _normalize_exclusion_items(get_json("manual_sharia_exclusions", []))
    file_items = _normalize_exclusion_items(_load_local_raw())
    merged_seed = _normalize_exclusion_items(sqlite_items + file_items)
    merged_items = _merge_remote_once(merged_seed, force=force_github_pull)
    cleaned = _normalize_exclusion_items(merged_items)
    if cleaned:
        set_json("manual_sharia_exclusions", cleaned)
    return cleaned


def save_manual_sharia_exclusions(items):
    cleaned = _normalize_exclusion_items(items)
    cleaned_dict = _items_to_dict(cleaned)
    set_json("manual_sharia_exclusions", cleaned)
    _safe_write_json(MANUAL_SHARIA_EXCLUSIONS_FILE, cleaned_dict)


def get_manual_sharia_exclusions_map():
    return {normalize_symbol_text(item.get("symbol", "")): item for item in load_manual_sharia_exclusions() if normalize_symbol_text(item.get("symbol", ""))}


def get_manual_sharia_sync_diagnostics() -> dict:
    return dict(_GITHUB_PULL_CACHE or {})


# -----------------------------
# Manual Sharia approvals (Fix14c)
# -----------------------------
_APPROVAL_PULL_CACHE = {"ts": 0.0, "ok": False, "count": 0, "error": ""}


def _normalize_approval_items(raw) -> list[dict]:
    """Accept list or dict and return deduped manual Sharia approval rows."""
    if isinstance(raw, dict):
        iterable = []
        for key, value in raw.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("symbol", key)
                iterable.append(item)
            else:
                iterable.append({"symbol": key, "note": str(value or "")})
    elif isinstance(raw, list):
        iterable = raw
    else:
        iterable = []

    merged = {}
    order = []
    for row in iterable:
        if isinstance(row, str):
            symbol = normalize_symbol_text(row)
            item = {"symbol": symbol, "note": "", "reason": "", "approved_at": "", "updated_at": "", "source": "manual"}
        elif isinstance(row, dict):
            symbol = normalize_symbol_text(row.get("symbol", ""))
            item = {
                "symbol": symbol,
                "note": str(row.get("note", "") or row.get("reason", "") or "").strip(),
                "reason": str(row.get("reason", "") or row.get("note", "") or "").strip(),
                "approved_at": str(row.get("approved_at", "") or row.get("created_at", "") or row.get("decided_at", "") or "").strip(),
                "updated_at": str(row.get("updated_at", "") or row.get("approved_at", "") or "").strip(),
                "source": str(row.get("source", "") or "manual").strip(),
            }
        else:
            continue
        if not symbol:
            continue
        if not item.get("approved_at"):
            item["approved_at"] = _now_text()
        if not item.get("updated_at"):
            item["updated_at"] = item.get("approved_at", "") or _now_text()
        if symbol not in merged:
            order.append(symbol)
            merged[symbol] = item
        else:
            existing = merged[symbol]
            if str(item.get("updated_at", "")) >= str(existing.get("updated_at", "")):
                merged[symbol].update({k: v for k, v in item.items() if v})
    return [merged[s] for s in order if s in merged]


def _approval_items_to_dict(items: list[dict]) -> dict:
    out = {}
    for item in _normalize_approval_items(items):
        symbol = normalize_symbol_text(item.get("symbol", ""))
        if not symbol:
            continue
        out[symbol] = {
            "symbol": symbol,
            "note": str(item.get("note", "") or "").strip(),
            "reason": str(item.get("reason", "") or item.get("note", "") or "").strip(),
            "approved_at": str(item.get("approved_at", "") or "").strip(),
            "updated_at": str(item.get("updated_at", "") or item.get("approved_at", "") or "").strip(),
            "source": str(item.get("source", "") or "manual").strip(),
        }
    return out


def _load_approval_local_raw():
    try:
        with open(MANUAL_SHARIA_APPROVALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _merge_approval_remote_once(local_items: list[dict], force: bool = False) -> list[dict]:
    now = time.time()
    if not force and now - float(_APPROVAL_PULL_CACHE.get("ts", 0) or 0) < int(GITHUB_SYNC_PULL_TTL_SEC or 900):
        return local_items
    try:
        from .github_sync import fetch_json_file, is_github_sync_configured
        if not is_github_sync_configured():
            _APPROVAL_PULL_CACHE.update({"ts": now, "ok": False, "count": 0, "error": "not_configured"})
            return local_items
        remote = fetch_json_file(GITHUB_SYNC_MANUAL_SHARIA_APPROVALS_PATH)
        if not remote.get("ok") or remote.get("data") is None:
            _APPROVAL_PULL_CACHE.update({"ts": now, "ok": bool(remote.get("ok")), "count": 0, "error": str(remote.get("error", ""))[:160]})
            return local_items
        merged = _approval_items_to_dict(local_items)
        remote_items = _normalize_approval_items(remote.get("data"))
        for item in remote_items:
            symbol = normalize_symbol_text(item.get("symbol", ""))
            if not symbol:
                continue
            existing = merged.get(symbol)
            if not existing or str(item.get("updated_at", "")) >= str(existing.get("updated_at", "")):
                merged[symbol] = item
        merged_items = list(merged.values())
        _safe_write_json(MANUAL_SHARIA_APPROVALS_FILE, _approval_items_to_dict(merged_items))
        _APPROVAL_PULL_CACHE.update({"ts": now, "ok": True, "count": len(remote_items), "error": ""})
        return _normalize_approval_items(merged)
    except Exception as exc:
        _APPROVAL_PULL_CACHE.update({"ts": now, "ok": False, "count": 0, "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
        return local_items


def load_manual_sharia_approvals(force_github_pull: bool = False):
    sqlite_items = _normalize_approval_items(get_json("manual_sharia_approvals", []))
    file_items = _normalize_approval_items(_load_approval_local_raw())
    merged_seed = _normalize_approval_items(sqlite_items + file_items)
    merged_items = _merge_approval_remote_once(merged_seed, force=force_github_pull)
    cleaned = _normalize_approval_items(merged_items)
    if cleaned:
        set_json("manual_sharia_approvals", cleaned)
    return cleaned


def save_manual_sharia_approvals(items):
    cleaned = _normalize_approval_items(items)
    cleaned_dict = _approval_items_to_dict(cleaned)
    set_json("manual_sharia_approvals", cleaned)
    _safe_write_json(MANUAL_SHARIA_APPROVALS_FILE, cleaned_dict)


def get_manual_sharia_approvals_map():
    return {normalize_symbol_text(item.get("symbol", "")): item for item in load_manual_sharia_approvals() if normalize_symbol_text(item.get("symbol", ""))}


def get_manual_sharia_approvals_sync_diagnostics() -> dict:
    return dict(_APPROVAL_PULL_CACHE or {})


