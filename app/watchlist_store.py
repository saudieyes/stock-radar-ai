import json
import os
import tempfile
from .settings import MANUAL_WATCHLIST_FILE
from .sqlite_store import get_json, set_json

STORE_KEY = "manual_watchlist"


def _safe_write_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        pass


def _load_json_file():
    try:
        with open(MANUAL_WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def load_manual_watchlist():
    data = get_json(STORE_KEY, None)
    if isinstance(data, list):
        return data
    data = _load_json_file()
    if data:
        set_json(STORE_KEY, data)
    return data


def save_manual_watchlist(items):
    data = items if isinstance(items, list) else []
    set_json(STORE_KEY, data)
    _safe_write_json(MANUAL_WATCHLIST_FILE, data)
