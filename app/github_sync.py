import base64
import json
import os
from datetime import datetime
from typing import Any
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




def _repo_api_url(path: str) -> str:
    clean = str(path or "").strip().lstrip("/")
    return f"https://api.github.com/repos/{GITHUB_SYNC_REPO}/{clean}"


def _serialize_file_content(content: Any, *, is_json: bool = False) -> str:
    if is_json:
        return json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True)
    return str(content or "")


def push_multiple_files(files: list[dict], message: str = "Sync Stock Radar data batch") -> dict:
    """Create/update multiple repository files in one GitHub commit.

    Uses the Git Data API instead of the Contents API so evidence/market-fear/
    pattern exports do not create 6-7 separate commits/deploy triggers. Each
    item must include:
      - path: repo-relative file path
      - content: text content or JSON-serializable object
      - is_json: optional bool; when true content is pretty JSON serialized
      - label: optional result label used by callers
    """
    if not is_github_sync_configured():
        return {"ok": False, "configured": False, "error": "github_sync_not_configured"}
    safe_files: list[dict] = []
    for item in files or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().lstrip("/")
        if not path:
            continue
        safe_files.append({
            "path": path,
            "label": str(item.get("label") or path),
            "content": _serialize_file_content(item.get("content"), is_json=bool(item.get("is_json"))),
        })
    if not safe_files:
        return {"ok": False, "configured": True, "error": "no_files_to_sync"}

    # Stability guard: never build/send oversized GitHub batches from Railway.
    # Large evidence exports were the main risk for network-egress spikes, timeouts,
    # and memory pressure. Callers should sync compact manifests/summaries and keep
    # raw heavy data in SQLite until verified retention/prune is requested.
    try:
        max_file_bytes = int(float(os.getenv("GITHUB_BATCH_MAX_FILE_BYTES", "3000000") or 3000000))
    except Exception:
        max_file_bytes = 3000000
    try:
        max_total_bytes = int(float(os.getenv("GITHUB_BATCH_MAX_TOTAL_BYTES", "8000000") or 8000000))
    except Exception:
        max_total_bytes = 8000000
    byte_rows = []
    total_bytes = 0
    for item in safe_files:
        b = len(str(item.get("content", "")).encode("utf-8"))
        total_bytes += b
        byte_rows.append({"label": item.get("label"), "path": item.get("path"), "bytes": b})
    too_large = [x for x in byte_rows if int(x.get("bytes") or 0) > max_file_bytes]
    if too_large or total_bytes > max_total_bytes:
        return {
            "ok": False,
            "configured": True,
            "error": "github_batch_too_large",
            "file_count": len(safe_files),
            "total_bytes": total_bytes,
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "oversized_files": too_large[:10],
            "files": byte_rows[:20],
            "advice": "Use compact evidence sync or split archives before retrying. No network upload was attempted.",
        }

    branch = GITHUB_SYNC_BRANCH or "main"
    timeout = float(GITHUB_SYNC_TIMEOUT_SEC or 12)
    try:
        # 1) Resolve current branch HEAD and base tree.
        ref_url = _repo_api_url(f"git/ref/heads/{branch}")
        ref_resp = HTTP_SESSION.get(ref_url, headers=_headers(), timeout=timeout)
        ref_resp.raise_for_status()
        head_sha = (((ref_resp.json() or {}).get("object") or {}).get("sha") or "")
        if not head_sha:
            return {"ok": False, "configured": True, "error": "missing_branch_head_sha", "branch": branch}

        commit_url = _repo_api_url(f"git/commits/{head_sha}")
        commit_resp = HTTP_SESSION.get(commit_url, headers=_headers(), timeout=timeout)
        commit_resp.raise_for_status()
        base_tree_sha = (((commit_resp.json() or {}).get("tree") or {}).get("sha") or "")
        if not base_tree_sha:
            return {"ok": False, "configured": True, "error": "missing_base_tree_sha", "branch": branch, "head_sha": head_sha}

        # 2) Create one blob per file.
        tree_entries = []
        synced_files = []
        for item in safe_files:
            blob_resp = HTTP_SESSION.post(
                _repo_api_url("git/blobs"),
                headers=_headers(),
                json={"content": item["content"], "encoding": "utf-8"},
                timeout=timeout,
            )
            blob_resp.raise_for_status()
            blob_sha = (blob_resp.json() or {}).get("sha") or ""
            if not blob_sha:
                return {"ok": False, "configured": True, "error": "missing_blob_sha", "path": item["path"], "branch": branch}
            tree_entries.append({"path": item["path"], "mode": "100644", "type": "blob", "sha": blob_sha})
            synced_files.append({"label": item["label"], "path": item["path"], "bytes": len(item["content"].encode("utf-8"))})

        # 3) Create a tree containing all changes, then one commit, then move the branch ref.
        tree_resp = HTTP_SESSION.post(
            _repo_api_url("git/trees"),
            headers=_headers(),
            json={"base_tree": base_tree_sha, "tree": tree_entries},
            timeout=timeout,
        )
        tree_resp.raise_for_status()
        new_tree_sha = (tree_resp.json() or {}).get("sha") or ""
        if not new_tree_sha:
            return {"ok": False, "configured": True, "error": "missing_new_tree_sha", "branch": branch}

        new_commit_resp = HTTP_SESSION.post(
            _repo_api_url("git/commits"),
            headers=_headers(),
            json={"message": message, "tree": new_tree_sha, "parents": [head_sha]},
            timeout=timeout,
        )
        new_commit_resp.raise_for_status()
        new_commit_sha = (new_commit_resp.json() or {}).get("sha") or ""
        if not new_commit_sha:
            return {"ok": False, "configured": True, "error": "missing_new_commit_sha", "branch": branch}

        update_resp = HTTP_SESSION.patch(
            ref_url,
            headers=_headers(),
            json={"sha": new_commit_sha, "force": False},
            timeout=timeout,
        )
        update_resp.raise_for_status()
        return {
            "ok": True,
            "configured": True,
            "branch": branch,
            "commit_sha": new_commit_sha,
            "parent_sha": head_sha,
            "file_count": len(synced_files),
            "files": synced_files,
            "synced_at": datetime.now(ZoneInfo("America/New_York")).isoformat(),
        }
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"{type(exc).__name__}: {str(exc)[:260]}", "branch": branch, "file_count": len(safe_files)}


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
