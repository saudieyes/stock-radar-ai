"""Lightweight Railway/RAM/storage diagnostics without external dependencies."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from app.settings import BASE_DIR, DATA_DIR
from app.sqlite_store import SQLITE_DB_PATH, sqlite_status
from app.weekly_archive import weekly_archive_status
try:
    from app.evidence_collector import evidence_retention_status, evidence_auto_sync_status
except Exception:
    evidence_retention_status = None
    evidence_auto_sync_status = None

SYSTEM_COST_HEALTH_VERSION = "system_cost_health_v1_2026_05_30"


def _dir_size(path: Path, limit_files: int = 20000) -> tuple[int, int]:
    total = 0
    count = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                count += 1
                try:
                    total += p.stat().st_size
                except Exception:
                    pass
                if count >= limit_files:
                    break
    except Exception:
        pass
    return total, count


def _bytes_mb(n: int | float) -> float:
    return round(float(n or 0) / (1024 * 1024), 2)


def _current_rss_mb() -> float | None:
    try:
        txt = Path("/proc/self/status").read_text()
        for line in txt.splitlines():
            if line.startswith("VmRSS:"):
                kb = float(line.split()[1])
                return round(kb / 1024, 2)
    except Exception:
        return None
    return None


def _sqlite_table_counts(limit: int = 30) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "tables": {}}
    try:
        path = Path(SQLITE_DB_PATH)
        if not path.exists():
            out.update({"ok": True, "missing": True})
            return out
        conn = sqlite3.connect(str(path), timeout=5)
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        tables = {}
        for (name,) in rows[:limit]:
            try:
                c = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                tables[name] = int(c)
            except Exception as exc:
                tables[name] = f"ERR:{type(exc).__name__}"
        conn.close()
        out.update({"ok": True, "tables": tables})
    except Exception as exc:
        out.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return out


def build_system_cost_health() -> dict[str, Any]:
    data_size, data_files = _dir_size(Path(DATA_DIR))
    appdata_size, appdata_files = _dir_size(Path(BASE_DIR) / "app_data")
    evidence_archive_size, evidence_archive_files = _dir_size(Path(BASE_DIR) / "app_data" / "evidence_archive")
    db_path = Path(SQLITE_DB_PATH)
    db_size = db_path.stat().st_size if db_path.exists() else 0
    retention = evidence_retention_status() if callable(evidence_retention_status) else {"ok": False, "error": "evidence_retention_unavailable"}
    autosync = evidence_auto_sync_status() if callable(evidence_auto_sync_status) else {"ok": False, "error": "evidence_auto_sync_unavailable"}
    warnings = []
    rss = _current_rss_mb()
    if rss and rss > 1800:
        warnings.append("RAM baseline مرتفع؛ راجع الكاش والتقارير والـ workers")
    if evidence_archive_size > 5 * 1024 * 1024:
        warnings.append("app_data/evidence_archive موجود داخل نسخة التطبيق؛ يفضل إخراجه من deploy")
    if db_size > 250 * 1024 * 1024:
        warnings.append("SQLite كبير؛ فعّل الأرشفة ثم الحذف الآمن/VACUUM")
    return {
        "ok": True,
        "version": SYSTEM_COST_HEALTH_VERSION,
        "memory": {"rss_mb": rss, "note": "يعرض RAM الحالية من داخل العملية إن توفر /proc/self/status"},
        "storage": {
            "data_dir": str(DATA_DIR),
            "data_dir_mb": _bytes_mb(data_size),
            "data_dir_files": data_files,
            "repo_app_data_mb": _bytes_mb(appdata_size),
            "repo_app_data_files": appdata_files,
            "repo_evidence_archive_mb": _bytes_mb(evidence_archive_size),
            "repo_evidence_archive_files": evidence_archive_files,
            "sqlite_db_path": str(SQLITE_DB_PATH),
            "sqlite_db_mb": _bytes_mb(db_size),
        },
        "sqlite": sqlite_status(),
        "sqlite_tables": _sqlite_table_counts(),
        "retention": retention,
        "evidence_auto_sync": autosync,
        "weekly_archive": weekly_archive_status(),
        "warnings": warnings,
        "cost_control_rules": [
            "لا raw minute files داخل Railway بعد التحليل",
            "أرشفة GitHub قبل أي حذف",
            "لا GitHub sync عند فتح الصفحة",
            "تقارير كبيرة عند الطلب فقط",
            "الفحص العميق فقط للمرشحين الجيدين",
        ],
    }
