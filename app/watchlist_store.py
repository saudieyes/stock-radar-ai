import json
from .settings import MANUAL_WATCHLIST_FILE
def load_manual_watchlist():
    try:
        with open(MANUAL_WATCHLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except:
        return []

def save_manual_watchlist(items):
    try:
        with open(MANUAL_WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except:
        pass
