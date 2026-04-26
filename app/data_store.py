import json

from .settings import MANUAL_SHARIA_EXCLUSIONS_FILE
from .utils import normalize_symbol_text
def load_manual_sharia_exclusions():
    try:
        with open(MANUAL_SHARIA_EXCLUSIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except:
        raw = []

    items = []
    seen = set()
    for row in raw if isinstance(raw, list) else []:
        if isinstance(row, str):
            symbol = normalize_symbol_text(row)
            item = {"symbol": symbol, "note": "", "excluded_at": ""}
        elif isinstance(row, dict):
            symbol = normalize_symbol_text(row.get("symbol", ""))
            item = {
                "symbol": symbol,
                "note": str(row.get("note", "") or "").strip(),
                "excluded_at": str(row.get("excluded_at", "") or "").strip(),
            }
        else:
            continue
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        items.append(item)
    return items


def save_manual_sharia_exclusions(items):
    try:
        cleaned = []
        seen = set()
        for row in items if isinstance(items, list) else []:
            symbol = normalize_symbol_text((row or {}).get("symbol", ""))
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            cleaned.append({
                "symbol": symbol,
                "note": str((row or {}).get("note", "") or "").strip(),
                "excluded_at": str((row or {}).get("excluded_at", "") or "").strip(),
            })
        with open(MANUAL_SHARIA_EXCLUSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    except:
        pass


def get_manual_sharia_exclusions_map():
    return {normalize_symbol_text(item.get("symbol", "")): item for item in load_manual_sharia_exclusions() if normalize_symbol_text(item.get("symbol", ""))}
