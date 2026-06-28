"""One-click SEC Sharia setup/admin routes.

V2W19b goal: the user should only deploy the code and open one admin URL.
The admin job downloads SEC bulk files into /data/sec, imports them into SQLite,
and activates SEC-primary Sharia only after a successful full import.

V2W19c adds a lightweight calibration action that re-screens already-imported
SEC rows without re-downloading companyfacts.zip.
"""
from __future__ import annotations

import html
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .sec_sharia_store import (
    SEC_COMPANYFACTS_ZIP,
    SEC_DIR,
    SEC_SHARIA_ACTIVE_FLAG,
    SEC_SHARIA_VERSION,
    SEC_TICKERS_EXCHANGE_JSON,
    import_companyfacts_zip,
    import_sec_company_map,
    mark_sec_sharia_active,
    recalibrate_sec_screen_results,
    sec_formula_calibration_report,
    sec_sharia_status,
)

SEC_COMPANYFACTS_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

ADMIN_JOB_FILE = SEC_DIR / "sec_sharia_admin_job.json"
ADMIN_LOCK = threading.Lock()
ADMIN_THREAD: threading.Thread | None = None
ADMIN_STATE: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "message": "جاهز",
    "progress_pct": None,
    "started_at": "",
    "finished_at": "",
    "mode": "",
    "error": "",
    "downloads": {},
    "map": None,
    "facts": None,
    "activated": False,
}

DEFAULT_TEST_SYMBOLS = ["AAPL", "NVDA", "MSFT", "TSLA", "HOUR", "EHGO", "ICCM", "NIXX"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _admin_token() -> str:
    return str(os.getenv("SEC_SHARIA_ADMIN_TOKEN", "") or "").strip()


def _sec_user_agent() -> str:
    # SEC asks automated tools to identify themselves.  This default is still
    # explicit; users can override with SEC_USER_AGENT in Railway variables.
    return str(os.getenv("SEC_USER_AGENT", "StockRadarAI/1.0 contact: admin@stock-radar.local") or "").strip()


def _check_token(token: str | None) -> tuple[bool, str]:
    required = _admin_token()
    if not required:
        return True, ""
    if str(token or "") == required:
        return True, ""
    return False, "SEC_SHARIA_ADMIN_TOKEN is set, but token was missing/wrong."


def _load_state_from_disk() -> dict[str, Any]:
    try:
        if ADMIN_JOB_FILE.exists():
            with open(ADMIN_JOB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _persist_state() -> None:
    try:
        SEC_DIR.mkdir(parents=True, exist_ok=True)
        with open(ADMIN_JOB_FILE, "w", encoding="utf-8") as f:
            json.dump(ADMIN_STATE, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _update_state(**kwargs: Any) -> None:
    with ADMIN_LOCK:
        ADMIN_STATE.update(kwargs)
        ADMIN_STATE["updated_at"] = _now_iso()
        _persist_state()


def _file_info(path: Path) -> dict[str, Any]:
    try:
        exists = path.exists()
        return {
            "path": str(path),
            "exists": exists,
            "size_mb": round(path.stat().st_size / 1024 / 1024, 1) if exists else 0,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if exists else "",
        }
    except Exception as exc:
        return {"path": str(path), "exists": False, "size_mb": 0, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


def _download_file(url: str, target: Path, *, refresh: bool = False, min_existing_mb: float = 0.1, label: str = "file") -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not refresh and target.stat().st_size >= int(min_existing_mb * 1024 * 1024):
        info = _file_info(target)
        info.update({"ok": True, "skipped": True, "reason": "already_exists"})
        return info

    tmp = target.with_suffix(target.suffix + ".tmp")
    headers = {"User-Agent": _sec_user_agent(), "Accept-Encoding": "gzip, deflate", "Connection": "close"}
    started = time.time()
    downloaded = 0
    total = 0
    _update_state(phase="download", message=f"تحميل {label} من SEC...", progress_pct=0)
    try:
        with requests.get(url, headers=headers, stream=True, timeout=(30, 180)) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as f:
                last_update = 0.0
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if now - last_update >= 2.0:
                        pct = round(downloaded / total * 100, 1) if total else None
                        _update_state(
                            phase="download",
                            message=f"تحميل {label}: {round(downloaded/1024/1024,1)}MB" + (f" / {round(total/1024/1024,1)}MB" if total else ""),
                            progress_pct=pct,
                            downloads={**ADMIN_STATE.get("downloads", {}), label: {"downloaded_mb": round(downloaded/1024/1024, 1), "total_mb": round(total/1024/1024, 1) if total else None, "pct": pct}},
                        )
                        last_update = now
        tmp.replace(target)
        out = _file_info(target)
        out.update({"ok": True, "skipped": False, "downloaded_mb": round(downloaded / 1024 / 1024, 1), "duration_sec": round(time.time() - started, 1), "url": url})
        return out
    except Exception as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return {"ok": False, "path": str(target), "url": url, "error": f"{type(exc).__name__}: {str(exc)[:260]}", "downloaded_mb": round(downloaded / 1024 / 1024, 1)}


def _run_setup_job(mode: str, *, refresh: bool = False, symbols: list[str] | None = None, limit: int | None = None) -> None:
    mode = (mode or "full").lower().strip()
    SEC_DIR.mkdir(parents=True, exist_ok=True)
    _update_state(
        running=True,
        phase="start",
        message="بدأ إعداد SEC Sharia في الخلفية",
        progress_pct=0,
        started_at=_now_iso(),
        finished_at="",
        mode=mode,
        error="",
        downloads={},
        map=None,
        facts=None,
        activated=False,
    )
    try:
        # V2W19c calibration uses existing SQLite SEC facts only. It does not
        # download companyfacts.zip and does not re-run the heavy full importer.
        if mode == "calibrate":
            _update_state(phase="calibrate", message="إعادة معايرة معادلة SEC من قاعدة SQLite الحالية بدون تحميل جديد...", progress_pct=None)
            facts_result = recalibrate_sec_screen_results(limit=limit or None)
            calibration = sec_formula_calibration_report(sample_limit=12)
            _update_state(facts=facts_result, calibration=calibration, activated=True, phase="finished", message="اكتملت معايرة V2W19c: تحذيرات SEC المالية أصبحت مراجعة محدودة وليست حظرًا نهائيًا.", progress_pct=100, running=False, finished_at=_now_iso())
            return

        # 1) Download required SEC files.  For full setup, companyfacts.zip is the expensive step.
        tickers_download = _download_file(SEC_TICKERS_EXCHANGE_URL, SEC_TICKERS_EXCHANGE_JSON, refresh=refresh, min_existing_mb=0.01, label="company_tickers_exchange.json")
        if not tickers_download.get("ok"):
            raise RuntimeError(f"Ticker mapping download failed: {tickers_download.get('error')}")

        facts_download = None
        if mode in {"test", "full", "download"}:
            facts_download = _download_file(SEC_COMPANYFACTS_URL, SEC_COMPANYFACTS_ZIP, refresh=refresh, min_existing_mb=100.0, label="companyfacts.zip")
            if not facts_download.get("ok"):
                raise RuntimeError(f"companyfacts download failed: {facts_download.get('error')}")
        _update_state(downloads={"tickers": tickers_download, "companyfacts": facts_download})

        # 2) Import ticker map.
        _update_state(phase="import_map", message="حقن خريطة ticker → CIK في SQLite...", progress_pct=None)
        map_result = import_sec_company_map(SEC_TICKERS_EXCHANGE_JSON)
        _update_state(map=map_result)
        if not map_result.get("ok"):
            raise RuntimeError(f"SEC map import failed: {map_result.get('error')}")

        if mode == "download":
            _update_state(phase="finished", message="اكتمل التحميل وحقن خريطة الرموز. لم يتم تشغيل حقن القوائم المالية.", progress_pct=100, running=False, finished_at=_now_iso())
            return

        # 3) Import financial facts.
        if mode == "test":
            use_symbols = symbols or DEFAULT_TEST_SYMBOLS
            _update_state(phase="import_test", message=f"تشغيل تجربة SEC على {len(use_symbols)} رموز...", progress_pct=None)
            facts_result = import_companyfacts_zip(SEC_COMPANYFACTS_ZIP, symbols=use_symbols, limit=limit or None, progress_every=1)
            # Test imports do not activate SEC primary globally.
            _update_state(facts=facts_result, activated=False)
            if not facts_result.get("ok"):
                raise RuntimeError(f"SEC test import failed: {facts_result.get('errors')}")
            _update_state(phase="finished", message="اكتملت تجربة SEC بنجاح. لم يتم تفعيل SEC كفلتر أساسي إلا بعد full import.", progress_pct=100, running=False, finished_at=_now_iso())
            return

        # Full mode: import the full map into local SQLite, then activate SEC primary.
        _update_state(phase="import_full", message="بدأ الحقن الكامل للقوائم المالية SEC. قد يستغرق وقتًا حسب حجم الملف وسرعة Railway...", progress_pct=None)
        facts_result = import_companyfacts_zip(SEC_COMPANYFACTS_ZIP, symbols=symbols or None, limit=limit or None, progress_every=250)
        _update_state(facts=facts_result)
        if not facts_result.get("ok"):
            raise RuntimeError(f"SEC full import failed: {facts_result.get('errors')}")
        if int(facts_result.get("inserted") or 0) < int(os.getenv("SEC_SHARIA_MIN_FULL_IMPORT_ROWS", "500") or 500):
            raise RuntimeError(f"SEC full import inserted too few rows: {facts_result.get('inserted')}")
        active = mark_sec_sharia_active(mode="full", details={"map": map_result, "facts": facts_result})
        _update_state(activated=bool(active.get("ok")), phase="finished", message="اكتمل SEC full import وتم تفعيل SEC كفلتر الشريعة المالي الأساسي.", progress_pct=100, running=False, finished_at=_now_iso())
    except Exception as exc:
        _update_state(running=False, phase="error", error=f"{type(exc).__name__}: {str(exc)[:500]}", message="فشل إعداد SEC Sharia. النسخة تبقى على الفلتر القديم لأن SEC لم يتم تفعيله.", finished_at=_now_iso())


def _start_job(mode: str, *, refresh: bool = False, symbols: list[str] | None = None, limit: int | None = None) -> dict[str, Any]:
    global ADMIN_THREAD
    with ADMIN_LOCK:
        if ADMIN_STATE.get("running") and ADMIN_THREAD and ADMIN_THREAD.is_alive():
            return {"ok": False, "already_running": True, "state": dict(ADMIN_STATE)}
        ADMIN_THREAD = threading.Thread(target=_run_setup_job, args=(mode,), kwargs={"refresh": refresh, "symbols": symbols, "limit": limit}, daemon=True)
        ADMIN_THREAD.start()
    return {"ok": True, "started": True, "mode": mode}


def _merged_status(sample_limit: int = 6) -> dict[str, Any]:
    disk_state = _load_state_from_disk()
    state = {**disk_state, **ADMIN_STATE}
    if ADMIN_THREAD and ADMIN_THREAD.is_alive():
        state["running"] = True
    sec_status = sec_sharia_status(sample_limit=sample_limit)
    return {
        "ok": True,
        "version": SEC_SHARIA_VERSION,
        "admin_job": state,
        "files": {
            "sec_dir": str(SEC_DIR),
            "companyfacts": _file_info(SEC_COMPANYFACTS_ZIP),
            "tickers_exchange": _file_info(SEC_TICKERS_EXCHANGE_JSON),
            "active_flag": _file_info(SEC_SHARIA_ACTIVE_FLAG),
        },
        "sec_status": sec_status,
        "links": {
            "status": "/admin/sec-sharia/status",
            "start_full": "/admin/sec-sharia/setup?mode=full&confirm=YES",
            "start_test": "/admin/sec-sharia/setup?mode=test&confirm=YES",
            "calibrate_v2w19c": "/admin/sec-sharia/calibrate?confirm=YES",
            "diagnostics_json": "/diagnostics/sharia-sec-refresh",
            "formula_calibration_json": "/diagnostics/sharia-formula-calibration",
        },
    }


def _html_status(payload: dict[str, Any]) -> str:
    job = payload.get("admin_job") or {}
    files = payload.get("files") or {}
    sec = payload.get("sec_status") or {}
    counts = (sec.get("counts") or {}) if isinstance(sec, dict) else {}
    by_status = counts.get("by_final_status") or {}
    running = bool(job.get("running"))
    title = "SEC Sharia Admin — V2W19c"
    safe_json = html.escape(json.dumps(payload, ensure_ascii=False, indent=2)[:20000])
    refresh = '<meta http-equiv="refresh" content="15">' if running else ''
    phase = html.escape(str(job.get("phase") or "idle"))
    message = html.escape(str(job.get("message") or ""))
    error = html.escape(str(job.get("error") or ""))
    pct = job.get("progress_pct")
    pct_txt = "" if pct is None else f" — {pct}%"
    active = "نعم" if sec.get("sec_primary_ready") else "لا"
    facts_size = files.get("companyfacts", {}).get("size_mb", 0)
    map_count = counts.get("sec_company_map", 0)
    fin_count = counts.get("sec_latest_financials", 0)
    rows = "".join([f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>" for k, v in by_status.items()]) or "<tr><td colspan='2'>لا توجد نتائج بعد</td></tr>"
    buttons = "" if running else """
      <p>
        <a class="btn primary" href="/admin/sec-sharia/calibrate?confirm=YES">معايرة V2W19c بدون تحميل</a>
        <a class="btn" href="/diagnostics/sharia-formula-calibration">تقرير المعادلة</a>
        <a class="btn" href="/diagnostics/sharia-sec-refresh">JSON diagnostics</a>
        <a class="btn" href="/admin/sec-sharia/setup?mode=full&confirm=YES">إعادة الإعداد الكامل فقط عند الحاجة</a>
      </p>
    """
    return f"""
<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh}
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; background: #0b1020; color: #eef2ff; }}
    .card {{ background: #111936; border: 1px solid #263257; border-radius: 14px; padding: 18px; margin: 14px 0; }}
    .muted {{ color: #aeb8d6; }}
    .ok {{ color: #86efac; font-weight: 700; }}
    .warn {{ color: #fde68a; font-weight: 700; }}
    .bad {{ color: #fca5a5; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; }}
    td, th {{ border-bottom: 1px solid #263257; padding: 8px; text-align: right; }}
    pre {{ white-space: pre-wrap; direction: ltr; text-align: left; background: #060914; color: #dbeafe; padding: 12px; border-radius: 10px; overflow:auto; }}
    a {{ color: #bfdbfe; }}
    .btn {{ display: inline-block; padding: 10px 14px; border: 1px solid #3b82f6; border-radius: 10px; margin: 6px; text-decoration: none; }}
    .primary {{ background: #1d4ed8; color: white; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class="card">
    <h2>الحالة الحالية</h2>
    <p>المرحلة: <b>{phase}</b>{html.escape(str(pct_txt))}</p>
    <p>{message}</p>
    <p>SEC مفعل كفلتر أساسي: <span class="{'ok' if sec.get('sec_primary_ready') else 'warn'}">{active}</span></p>
    {f'<p class="bad">الخطأ: {error}</p>' if error else ''}
    {buttons}
    <p class="muted">إذا كانت العملية تعمل فالصفحة تتحدث تلقائيًا كل 15 ثانية. لا تحتاج تفتح Railway shell.</p>
  </div>
  <div class="card">
    <h2>الملفات والحقن</h2>
    <table>
      <tr><th>العنصر</th><th>القيمة</th></tr>
      <tr><td>مسار SEC</td><td>{html.escape(str(files.get('sec_dir', '')))}</td></tr>
      <tr><td>حجم companyfacts.zip</td><td>{html.escape(str(facts_size))} MB</td></tr>
      <tr><td>عدد رموز CIK</td><td>{html.escape(str(map_count))}</td></tr>
      <tr><td>عدد القوائم المالية المحقونة</td><td>{html.escape(str(fin_count))}</td></tr>
    </table>
  </div>
  <div class="card">
    <h2>توزيع حكم SEC</h2>
    <table><tr><th>الحالة</th><th>العدد</th></tr>{rows}</table>
  </div>
  <div class="card">
    <h2>تفاصيل JSON للمراجعة</h2>
    <pre>{safe_json}</pre>
  </div>
</body>
</html>
"""


def register_sec_sharia_admin_routes(app) -> None:
    @app.get("/admin/sec-sharia/status")
    def admin_sec_sharia_status(format: str = Query(default="html"), sample_limit: int = Query(default=6)):
        payload = _merged_status(sample_limit=max(1, min(20, int(sample_limit or 6))))
        if str(format or "").lower() == "json":
            return JSONResponse(payload)
        return HTMLResponse(_html_status(payload))

    @app.get("/admin/sec-sharia/setup")
    def admin_sec_sharia_setup(
        mode: str = Query(default="full"),
        confirm: str = Query(default=""),
        refresh: bool = Query(default=False),
        token: str = Query(default=""),
        symbols: str = Query(default=""),
        limit: int = Query(default=0),
    ):
        ok, err = _check_token(token)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        mode_clean = str(mode or "full").lower().strip()
        if mode_clean not in {"full", "test", "download", "calibrate"}:
            return JSONResponse({"ok": False, "error": "mode must be full, test, download, or calibrate"}, status_code=400)
        if str(confirm or "").upper() != "YES":
            # Show status page with safe links instead of starting accidentally.
            return HTMLResponse(_html_status(_merged_status(sample_limit=6)))
        symbol_list = [x.strip().upper() for x in str(symbols or "").replace(";", ",").split(",") if x.strip()]
        started = _start_job(mode_clean, refresh=bool(refresh), symbols=symbol_list or None, limit=int(limit or 0) or None)
        if not started.get("ok") and started.get("already_running"):
            return RedirectResponse(url="/admin/sec-sharia/status", status_code=303)
        return RedirectResponse(url="/admin/sec-sharia/status", status_code=303)

    @app.get("/admin/sec-sharia/calibrate")
    def admin_sec_sharia_calibrate(
        confirm: str = Query(default=""),
        token: str = Query(default=""),
        limit: int = Query(default=0),
    ):
        ok, err = _check_token(token)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, status_code=403)
        if str(confirm or "").upper() != "YES":
            return HTMLResponse(_html_status(_merged_status(sample_limit=6)))
        started = _start_job("calibrate", refresh=False, symbols=None, limit=int(limit or 0) or None)
        if not started.get("ok") and started.get("already_running"):
            return RedirectResponse(url="/admin/sec-sharia/status", status_code=303)
        return RedirectResponse(url="/admin/sec-sharia/status", status_code=303)

    @app.get("/admin/sec-sharia")
    def admin_sec_sharia_home():
        return RedirectResponse(url="/admin/sec-sharia/status", status_code=303)
