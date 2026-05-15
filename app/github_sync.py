import base64
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .settings import (
    GITHUB_SYNC_BRANCH,
    GITHUB_SYNC_ENABLED,
    GITHUB_SYNC_MANUAL_SHARIA_PATH,
    GITHUB_SYNC_REPO,
    GITHUB_SYNC_TIMEOUT_SEC,
    GITHUB_SYNC_TOKEN,
    HTTP_SESSION,
)


def _headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_SYNC_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "stock-radar-ai-data-sync",
    }


def is_github_sync_configured() -> bool:
    return bool(GITHUB_SYNC_ENABLED and GITHUB_SYNC_REPO and GITHUB_SYNC_TOKEN)


def github_sync_status() -> dict:
    return {
        "enabled": is_github_sync_configured(),
        "repo": GITHUB_SYNC_REPO if GITHUB_SYNC_REPO else "",
        "branch": GITHUB_SYNC_BRANCH or "main",
        "manual_sharia_path": GITHUB_SYNC_MANUAL_SHARIA_PATH,
    }


def _content_url(path: str) -> str:
    clean = str(path or "").strip().lstrip("/")
    return f"https://api.github.com/repos/{GITHUB_SYNC_REPO}/contents/{clean}"


def fetch_json_file(path: str) -> dict:
    """Fetch a JSON file from GitHub. Missing file returns ok=True with data=None."""
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured"}
    try:
        url = _content_url(path)
        r = HTTP_SESSION.get(
            url,
            headers=_headers(),
            params={"ref": GITHUB_SYNC_BRANCH or "main"},
            timeout=float(GITHUB_SYNC_TIMEOUT_SEC or 12),
        )
        if r.status_code == 404:
            return {"ok": True, "configured": True, "exists": False, "data": None, "sha": ""}
        r.raise_for_status()
        payload = r.json() or {}
        raw_b64 = payload.get("content", "") or ""
        raw = base64.b64decode(raw_b64).decode("utf-8") if raw_b64 else "null"
        return {"ok": True, "configured": True, "exists": True, "data": json.loads(raw), "sha": payload.get("sha", "") or ""}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def push_json_file(path: str, data, message: str = "Sync Stock Radar data") -> dict:
    """Create/update a JSON file in GitHub using the contents API."""
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured"}
    try:
        current = fetch_json_file(path)
        sha = current.get("sha") if current.get("ok") and current.get("exists") else ""
        raw = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        body = {
            "message": message,
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": GITHUB_SYNC_BRANCH or "main",
        }
        if sha:
            body["sha"] = sha
        url = _content_url(path)
        r = HTTP_SESSION.put(url, headers=_headers(), json=body, timeout=float(GITHUB_SYNC_TIMEOUT_SEC or 12))
        r.raise_for_status()
        payload = r.json() or {}
        return {
            "ok": True,
            "configured": True,
            "path": path,
            "branch": GITHUB_SYNC_BRANCH or "main",
            "commit_sha": ((payload.get("commit") or {}).get("sha") or ""),
            "synced_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        }
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"{type(exc).__name__}: {str(exc)[:220]}"}


def fetch_text_file(path: str) -> dict:
    """Fetch a UTF-8 text file from GitHub. Missing file returns ok=True with content=None."""
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured"}
    try:
        url = _content_url(path)
        r = HTTP_SESSION.get(
            url,
            headers=_headers(),
            params={"ref": GITHUB_SYNC_BRANCH or "main"},
            timeout=float(GITHUB_SYNC_TIMEOUT_SEC or 12),
        )
        if r.status_code == 404:
            return {"ok": True, "configured": True, "exists": False, "content": None, "sha": ""}
        r.raise_for_status()
        payload = r.json() or {}
        raw_b64 = payload.get("content", "") or ""
        raw = base64.b64decode(raw_b64).decode("utf-8") if raw_b64 else ""
        return {"ok": True, "configured": True, "exists": True, "content": raw, "sha": payload.get("sha", "") or ""}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def push_text_file(path: str, content: str, message: str = "Sync Stock Radar text data") -> dict:
    """Create/update a UTF-8 text/CSV file in GitHub using the contents API."""
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured"}
    try:
        current = fetch_text_file(path)
        sha = current.get("sha") if current.get("ok") and current.get("exists") else ""
        raw = str(content or "")
        body = {
            "message": message,
            "content": base64.b64encode(raw.encode("utf-8")).decode("ascii"),
            "branch": GITHUB_SYNC_BRANCH or "main",
        }
        if sha:
            body["sha"] = sha
        url = _content_url(path)
        r = HTTP_SESSION.put(url, headers=_headers(), json=body, timeout=float(GITHUB_SYNC_TIMEOUT_SEC or 12))
        r.raise_for_status()
        payload = r.json() or {}
        return {
            "ok": True,
            "configured": True,
            "path": path,
            "branch": GITHUB_SYNC_BRANCH or "main",
            "commit_sha": ((payload.get("commit") or {}).get("sha") or ""),
            "synced_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        }
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"{type(exc).__name__}: {str(exc)[:220]}"}
