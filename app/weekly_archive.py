"""Weekly archive and retention job for Stock Radar AI.

Archives weekly Tracking Intelligence and Missed Opportunities reports to GitHub
and optionally prunes Railway/SQLite data only after successful verification.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.settings import (
    GITHUB_WEEKLY_ARCHIVE_PATH,
    WEEKLY_ARCHIVE_ENABLED,
    WEEKLY_ARCHIVE_PRUNE_AFTER_SUCCESS,
    WEEKLY_ARCHIVE_RETENTION_WEEKS,
)
from app.github_sync import is_github_sync_configured, push_json_file, push_text_file, fetch_json_file, fetch_text_file
from app.tracking_intelligence import export_tracking_json, export_tracking_csv, build_tracking_weekly_brief
from app.missed_opportunities import (
    missed_status,
    export_missed_json,
    export_missed_csv,
    build_missed_weekly_report,
    build_missed_weekly_brief,
    build_late_promotions_report,
    build_pre_move_evidence_report,
    build_loss_analysis_report,
)
from app.sqlite_store import get_json, set_json

try:
    from app.sqlite_store import _connect, SQLITE_ENABLED
except Exception:  # pragma: no cover
    _connect = None
    SQLITE_ENABLED = False


def _now_riyadh() -> str:
    return datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d %H:%M:%S")


def _safe_week_key(week_key: str | None = None) -> str:
    wk = str(week_key or "").strip()
    if wk:
        return wk
    try:
        st = missed_status()
        wk = str(st.get("week_key", "") or "").strip()
        if wk:
            return wk
    except Exception:
        pass
    # Fallback: Tracking export resolves current week key internally.
    try:
        exp = export_tracking_json(limit=1, include_items=False)
        wk = str(exp.get("week_key", "") or "").strip()
        if wk:
            return wk
    except Exception:
        pass
    return datetime.now(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d")


def _archive_path(week_key: str, filename: str) -> str:
    base = str(GITHUB_WEEKLY_ARCHIVE_PATH or "app_data/weekly_tracking_archive").strip().strip("/")
    return f"{base}/{week_key}/{filename}"


def _json_safe(obj) -> dict:
    if isinstance(obj, dict):
        return obj
    return {"ok": False, "value": str(obj)}


def _verify_json(path: str) -> dict:
    fetched = fetch_json_file(path)
    return {"path": path, "ok": bool(fetched.get("ok") and fetched.get("exists")), "error": fetched.get("error", "")}


def _verify_text(path: str) -> dict:
    fetched = fetch_text_file(path)
    return {"path": path, "ok": bool(fetched.get("ok") and fetched.get("exists") and fetched.get("content") is not None), "error": fetched.get("error", "")}


def prune_week_data(week_key: str) -> dict:
    """Delete one archived week from Railway SQLite after verified archive.

    This is intentionally scoped to one week_key and never runs unless archive code
    explicitly requests it. It keeps kv snapshots and user/manual lists untouched.
    """
    if not (SQLITE_ENABLED and _connect and week_key):
        return {"ok": False, "enabled": bool(SQLITE_ENABLED), "error": "sqlite_unavailable_or_missing_week_key"}
    tables = [
        "tracking_signal_events",
        "tracking_signals",
        "tracking_weekly_insights",
        "missed_seen_symbols",
        "missed_source_candidates",
        "missed_symbol_timeline",
        "missed_pre_move_snapshots",
        "missed_weekly_movers",
    ]
    deleted = {}
    try:
        with _connect() as conn:
            for table in tables:
                try:
                    cur = conn.execute(f"DELETE FROM {table} WHERE week_key=?", (week_key,))
                    deleted[table] = int(cur.rowcount if cur.rowcount is not None else 0)
                except Exception as exc:
                    deleted[table] = f"ERROR: {type(exc).__name__}: {str(exc)[:120]}"
            conn.commit()
        return {"ok": True, "week_key": week_key, "deleted": deleted, "pruned_at_riyadh": _now_riyadh()}
    except Exception as exc:
        return {"ok": False, "week_key": week_key, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "deleted": deleted}


def archive_weekly_tracking(week_key: str | None = None, prune: bool | None = None, include_items: bool = True) -> dict:
    if not WEEKLY_ARCHIVE_ENABLED:
        return {"ok": False, "enabled": False, "error": "weekly_archive_disabled"}
    if not is_github_sync_configured():
        return {"ok": False, "enabled": True, "configured": False, "error": "github_sync_not_configured"}

    wk = _safe_week_key(week_key)
    started = time.time()
    prune_requested = WEEKLY_ARCHIVE_PRUNE_AFTER_SUCCESS if prune is None else bool(prune)

    tracking_json = export_tracking_json(week_key=wk, include_items=include_items, limit=20000)
    tracking_csv = export_tracking_csv(week_key=wk, limit=20000)
    tracking_brief = build_tracking_weekly_brief(week_key=wk, include_items=True)
    missed_json = export_missed_json(week_key=wk, threshold=20.0, include_items=True, limit=5000)
    missed_csv = export_missed_csv(week_key=wk, threshold=20.0, limit=5000)
    missed_weekly = build_missed_weekly_report(week_key=wk, threshold=20.0, include_items=True)
    missed_brief = build_missed_weekly_brief(week_key=wk, threshold=20.0, include_items=True)
    late_promotions = build_late_promotions_report(week_key=wk, threshold=10.0, format="json")
    pre_move = build_pre_move_evidence_report(week_key=wk, threshold=10.0, format="json", limit=200)
    loss_analysis = build_loss_analysis_report(week_key=wk, format="json", limit=500, detail="full", top=80)
    status_payload = missed_status()

    index_payload = {
        "ok": True,
        "version": "weekly_archive_v1",
        "week_key": wk,
        "created_at_riyadh": _now_riyadh(),
        "files": [],
        "notes": {
            "safe_delete_rule": "Railway pruning is allowed only after GitHub archive and verification succeed.",
            "prune_requested": prune_requested,
        },
    }

    jobs = [
        ("tracking_export.json", "json", _json_safe(tracking_json)),
        ("tracking_export.csv", "text", tracking_csv),
        ("tracking_weekly_brief.txt", "text", tracking_brief),
        ("missed_export.json", "json", _json_safe(missed_json)),
        ("missed_export.csv", "text", missed_csv),
        ("missed_weekly.json", "json", _json_safe(missed_weekly)),
        ("missed_weekly_brief.txt", "text", missed_brief),
        ("late_promotions.json", "json", _json_safe(late_promotions)),
        ("pre_move_analysis.json", "json", _json_safe(pre_move)),
        ("loss_analysis_full.json", "json", _json_safe(loss_analysis)),
        ("status.json", "json", _json_safe(status_payload)),
    ]

    uploads = []
    for filename, kind, payload in jobs:
        path = _archive_path(wk, filename)
        message = f"Archive Stock Radar weekly tracking {wk}: {filename}"
        if kind == "json":
            res = push_json_file(path, payload, message=message)
        else:
            res = push_text_file(path, str(payload or ""), message=message)
        uploads.append({"filename": filename, "kind": kind, "path": path, **res})
        index_payload["files"].append({"filename": filename, "kind": kind, "path": path, "upload_ok": bool(res.get("ok"))})

    index_path = _archive_path(wk, "archive_index.json")
    index_upload = push_json_file(index_path, index_payload, message=f"Archive Stock Radar weekly index {wk}")

    verifications = []
    for item in uploads:
        if not item.get("ok"):
            verifications.append({"path": item.get("path"), "ok": False, "error": item.get("error", "upload_failed")})
            continue
        if item.get("kind") == "json":
            verifications.append(_verify_json(str(item.get("path"))))
        else:
            verifications.append(_verify_text(str(item.get("path"))))
    verifications.append(_verify_json(index_path))

    all_uploaded = all(bool(x.get("ok")) for x in uploads) and bool(index_upload.get("ok"))
    all_verified = all(bool(x.get("ok")) for x in verifications)
    prune_result = {"ok": False, "skipped": True, "reason": "prune_not_requested_or_archive_not_verified"}
    if prune_requested and all_uploaded and all_verified:
        prune_result = prune_week_data(wk)

    result = {
        "ok": bool(all_uploaded and all_verified and (not prune_requested or prune_result.get("ok"))),
        "enabled": True,
        "configured": True,
        "week_key": wk,
        "archive_base_path": _archive_path(wk, "").rstrip("/"),
        "index_path": index_path,
        "uploads": uploads,
        "index_upload": index_upload,
        "verifications": verifications,
        "all_uploaded": all_uploaded,
        "all_verified": all_verified,
        "prune_requested": prune_requested,
        "prune_result": prune_result,
        "elapsed_sec": round(time.time() - started, 2),
        "created_at_riyadh": _now_riyadh(),
        "schedule_hint": "Run every Saturday 05:00 Asia/Riyadh via Railway Cron or GitHub Actions hitting /admin/archive-weekly-tracking.",
    }
    try:
        set_json(f"weekly_archive_status:{wk}", result)
        set_json("weekly_archive_last_status", result)
    except Exception:
        pass
    return result


def weekly_archive_status(week_key: str | None = None) -> dict:
    wk = str(week_key or "").strip()
    if wk:
        return get_json(f"weekly_archive_status:{wk}", {"ok": False, "error": "no_status_for_week", "week_key": wk})
    return get_json("weekly_archive_last_status", {"ok": False, "error": "no_archive_status_yet"})
