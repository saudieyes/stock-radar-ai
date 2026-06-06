"""Safe Polygon/Massive flat-file fetcher.

Design rules for Stock Radar AI:
- never store raw flat files in Railway volume, GitHub, or SQLite;
- download only to /tmp and let the caller delete the temporary directory;
- do not pull on weekends or US market holidays;
- cap availability/download attempts per trade_date + dataset.

Massive/Polygon Flat Files are S3-compatible.  The normal REST POLYGON_API_KEY
is not always the same as the Flat Files S3 Access Key/Secret.  This module
therefore accepts several env var aliases and reports configuration clearly.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .settings import DATA_DIR

POLYGON_FLATFILE_FETCHER_VERSION = "polygon_flatfile_fetcher_v1a_stale_processed_recovery_2026_06_06"
STATE_PATH = Path(DATA_DIR) / "polygon_flatfile_pull_state.json"

DATASET_MINUTE = "minute"
DATASET_DAILY = "daily"
DATASET_CONFIG = {
    DATASET_MINUTE: "us_stocks_sip/minute_aggs_v1",
    DATASET_DAILY: "us_stocks_sip/day_aggs_v1",
}


def _env_bool(name: str, default: bool = False) -> bool:
    v = str(os.getenv(name, "") or "").strip().lower()
    if not v:
        return bool(default)
    return v in {"1", "true", "yes", "on", "y"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except Exception:
        return int(default)


def _env_str(*names: str, default: str = "") -> str:
    for name in names:
        v = str(os.getenv(name, "") or "").strip()
        if v:
            return v
    return default


def flatfiles_enabled() -> bool:
    return _env_bool("POLYGON_FLATFILES_ENABLED", False) or _env_bool("MASSIVE_FLATFILES_ENABLED", False)


def max_attempts() -> int:
    return max(1, min(10, _env_int("POLYGON_FLATFILES_MAX_ATTEMPTS", 3)))


def flatfiles_endpoint() -> str:
    return _env_str(
        "POLYGON_FLATFILES_ENDPOINT",
        "MASSIVE_FLATFILES_ENDPOINT",
        default="https://files.massive.com",
    ).rstrip("/")


def flatfiles_bucket() -> str:
    return _env_str("POLYGON_FLATFILES_BUCKET", "MASSIVE_FLATFILES_BUCKET", default="flatfiles")


def flatfiles_access_key() -> str:
    return _env_str(
        "POLYGON_FLATFILES_ACCESS_KEY",
        "POLYGON_S3_ACCESS_KEY",
        "MASSIVE_FLATFILES_ACCESS_KEY",
        "MASSIVE_S3_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
    )


def flatfiles_secret_key() -> str:
    return _env_str(
        "POLYGON_FLATFILES_SECRET_KEY",
        "POLYGON_S3_SECRET_KEY",
        "MASSIVE_FLATFILES_SECRET_KEY",
        "MASSIVE_S3_SECRET_KEY",
        "AWS_SECRET_ACCESS_KEY",
    )


def flatfiles_config_status() -> dict[str, Any]:
    access = flatfiles_access_key()
    secret = flatfiles_secret_key()
    return {
        "version": POLYGON_FLATFILE_FETCHER_VERSION,
        "enabled": flatfiles_enabled(),
        "configured": bool(access and secret),
        "endpoint": flatfiles_endpoint(),
        "bucket": flatfiles_bucket(),
        "max_attempts_per_trade_date_dataset": max_attempts(),
        "has_access_key": bool(access),
        "has_secret_key": bool(secret),
        "api_key_present_for_rest_not_flatfiles": bool(os.getenv("POLYGON_API_KEY")),
        "required_secret_names": [
            "POLYGON_FLATFILES_ACCESS_KEY or MASSIVE_FLATFILES_ACCESS_KEY",
            "POLYGON_FLATFILES_SECRET_KEY or MASSIVE_FLATFILES_SECRET_KEY",
        ],
    }


def _parse_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _observed(d: date) -> date:
    if d.weekday() == 5:  # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:  # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d + timedelta(days=(n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def _easter_date(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_holidays(year: int) -> set[date]:
    holidays: set[date] = set()
    holidays.add(_observed(date(year, 1, 1)))  # New Year
    holidays.add(_nth_weekday(year, 1, 0, 3))  # MLK Day
    holidays.add(_nth_weekday(year, 2, 0, 3))  # Presidents Day
    holidays.add(_easter_date(year) - timedelta(days=2))  # Good Friday
    holidays.add(_last_weekday(year, 5, 0))  # Memorial Day
    holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    holidays.add(_observed(date(year, 7, 4)))  # Independence Day
    holidays.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    holidays.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    holidays.add(_observed(date(year, 12, 25)))  # Christmas

    # Manual override for unusual closures/half-day avoidance. Format: YYYY-MM-DD,YYYY-MM-DD
    extra = _env_str("POLYGON_FLATFILES_MARKET_HOLIDAYS", "MARKET_HOLIDAYS_US", default="")
    for part in extra.replace(";", ",").split(","):
        dd = _parse_date(part.strip())
        if dd:
            holidays.add(dd)
    return holidays


def is_us_market_trading_day(value: str | date | datetime | None) -> bool:
    d = _parse_date(value)
    if not d:
        return False
    if d.weekday() >= 5:
        return False
    return d not in us_market_holidays(d.year)


def previous_trading_day(value: str | date | datetime | None = None) -> date:
    d = _parse_date(value) or datetime.utcnow().date()
    d -= timedelta(days=1)
    while not is_us_market_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_ending(end_date: str | date | datetime | None, count: int) -> list[date]:
    end = _parse_date(end_date) or previous_trading_day()
    while not is_us_market_trading_day(end):
        end -= timedelta(days=1)
    out: list[date] = []
    d = end
    limit = max(1, min(60, int(count or 1)))
    while len(out) < limit:
        if is_us_market_trading_day(d):
            out.append(d)
        d -= timedelta(days=1)
    return list(reversed(out))


def flatfile_key(dataset: str, trade_date: str | date | datetime) -> str:
    d = _parse_date(trade_date)
    if not d:
        raise ValueError("invalid_trade_date")
    kind = str(dataset or "").strip().lower()
    if kind in {"minute", "minutes", "min"}:
        base = DATASET_CONFIG[DATASET_MINUTE]
    elif kind in {"daily", "day", "days"}:
        base = DATASET_CONFIG[DATASET_DAILY]
    else:
        raise ValueError(f"unknown_dataset: {dataset}")
    return f"{base}/{d.year:04d}/{d.month:02d}/{d.isoformat()}.csv.gz"


def _load_state() -> dict[str, Any]:
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _state_key(dataset: str, trade_date: date) -> str:
    return f"{trade_date.isoformat()}:{dataset}"


def _record_attempt(dataset: str, trade_date: date, status: str, error: str = "") -> dict[str, Any]:
    state = _load_state()
    key = _state_key(dataset, trade_date)
    rec = dict(state.get(key) or {})
    rec["trade_date"] = trade_date.isoformat()
    rec["dataset"] = dataset
    rec["attempt_count"] = int(rec.get("attempt_count", 0) or 0) + 1
    rec["last_attempt_at"] = datetime.utcnow().isoformat() + "Z"
    rec["status"] = status
    if error:
        rec["error"] = str(error)[:300]
    state[key] = rec
    _save_state(state)
    return rec


def mark_flatfile_processed(dataset: str, trade_date: str | date | datetime, key_path: str = "") -> dict[str, Any]:
    d = _parse_date(trade_date)
    if not d:
        return {}
    kind = str(dataset or "").strip().lower()
    state = _load_state()
    key = _state_key(kind, d)
    rec = dict(state.get(key) or {})
    rec.update({
        "trade_date": d.isoformat(),
        "dataset": kind,
        "status": "processed",
        "processed_at": datetime.utcnow().isoformat() + "Z",
        "s3_key": key_path or rec.get("s3_key", ""),
    })
    state[key] = rec
    _save_state(state)
    return rec


def _record_downloaded_tmp(dataset: str, trade_date: date, key_path: str, file_size: int = 0) -> dict[str, Any]:
    state = _load_state()
    key = _state_key(dataset, trade_date)
    rec = dict(state.get(key) or {})
    rec.update({
        "trade_date": trade_date.isoformat(),
        "dataset": dataset,
        "status": "downloaded_to_tmp",
        "downloaded_at": datetime.utcnow().isoformat() + "Z",
        "s3_key": key_path,
        "last_file_size_bytes": int(file_size or 0),
    })
    state[key] = rec
    _save_state(state)
    return rec


def attempt_state(dataset: str, trade_date: str | date | datetime) -> dict[str, Any]:
    d = _parse_date(trade_date)
    if not d:
        return {}
    return dict((_load_state() or {}).get(_state_key(str(dataset), d)) or {})


def make_tmp_dir(prefix: str = "stock_radar_polygon_") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp" if Path("/tmp").exists() else None))


def cleanup_tmp_path(path: str | Path | None) -> None:
    if not path:
        return
    p = Path(path)
    try:
        if p.exists() and p.is_dir() and str(p).startswith("/tmp/"):
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists() and p.is_file() and str(p).startswith("/tmp/"):
            p.unlink(missing_ok=True)
    except Exception:
        pass


@dataclass
class FetchResult:
    ok: bool
    status: str
    dataset: str
    trade_date: str
    path: str = ""
    s3_key: str = ""
    error: str = ""
    skipped: bool = False
    attempts: int = 0
    file_size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "dataset": self.dataset,
            "trade_date": self.trade_date,
            "path": self.path,
            "s3_key": self.s3_key,
            "error": self.error,
            "skipped": self.skipped,
            "attempts": self.attempts,
            "file_size_bytes": self.file_size_bytes,
        }


def _client():
    try:
        import boto3  # type: ignore
    except Exception as exc:
        return None, f"boto3_missing: {type(exc).__name__}: {str(exc)[:120]}"
    access = flatfiles_access_key()
    secret = flatfiles_secret_key()
    if not access or not secret:
        return None, "missing_flatfiles_s3_credentials"
    try:
        client = boto3.client(
            "s3",
            endpoint_url=flatfiles_endpoint(),
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name="us-east-1",
        )
        return client, ""
    except Exception as exc:
        return None, f"s3_client_error: {type(exc).__name__}: {str(exc)[:160]}"


def fetch_flatfile_to_tmp(
    dataset: str,
    trade_date: str | date | datetime,
    *,
    tmp_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Download a single flat file to /tmp and return its path.

    The caller is responsible for cleanup via cleanup_tmp_path(tmp_dir).  If the
    file is unavailable, the attempt count is incremented.  Once the cap is
    reached, later calls for the same date/dataset are skipped unless force=True.
    """
    kind = str(dataset or "").strip().lower()
    if kind in {"minutes", "min"}:
        kind = DATASET_MINUTE
    if kind in {"day", "days"}:
        kind = DATASET_DAILY
    d = _parse_date(trade_date)
    if not d:
        return FetchResult(False, "invalid_trade_date", kind or "unknown", str(trade_date), error="invalid_trade_date").to_dict()
    if kind not in DATASET_CONFIG:
        return FetchResult(False, "unknown_dataset", kind, d.isoformat(), error=f"unknown_dataset: {dataset}").to_dict()
    if not flatfiles_enabled():
        return FetchResult(False, "disabled", kind, d.isoformat(), skipped=True, error="POLYGON_FLATFILES_ENABLED is not true").to_dict()
    if not is_us_market_trading_day(d):
        return FetchResult(True, "market_closed_no_pull", kind, d.isoformat(), skipped=True).to_dict()

    prev = attempt_state(kind, d)
    attempts = int(prev.get("attempt_count", 0) or 0)
    if not force and str(prev.get("status") or "") == "processed":
        return FetchResult(True, "already_processed", kind, d.isoformat(), skipped=True, attempts=attempts).to_dict()
    cap_statuses = {"checking", "not_available_yet", "unavailable_after_retries", "download_failed", "download_failed_after_retries"}
    if not force and attempts >= max_attempts() and str(prev.get("status") or "") in cap_statuses:
        return FetchResult(False, "attempt_cap_reached", kind, d.isoformat(), skipped=True, attempts=attempts, error="max attempts reached; will wait for next scheduled day").to_dict()

    client, client_error = _client()
    if client is None:
        return FetchResult(False, "not_configured", kind, d.isoformat(), error=client_error, attempts=attempts).to_dict()

    key = flatfile_key(kind, d)
    bucket = flatfiles_bucket()
    rec = _record_attempt(kind, d, "checking")
    attempts = int(rec.get("attempt_count", attempts + 1) or attempts + 1)
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        size = int(head.get("ContentLength", 0) or 0)
    except Exception as exc:
        err = f"not_available_or_head_failed: {type(exc).__name__}: {str(exc)[:180]}"
        status = "unavailable_after_retries" if attempts >= max_attempts() else "not_available_yet"
        rec = _record_attempt(kind, d, status, err)
        # _record_attempt increments a second time for the failed download phase; normalize user-facing attempts.
        rec["attempt_count"] = attempts
        state = _load_state(); state[_state_key(kind, d)] = rec; _save_state(state)
        return FetchResult(False, status, kind, d.isoformat(), s3_key=key, error=err, attempts=attempts).to_dict()

    out_dir = Path(tmp_dir) if tmp_dir else make_tmp_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{kind}_{d.isoformat()}.csv.gz"
    try:
        with out_path.open("wb") as f:
            client.download_fileobj(bucket, key, f)
        _record_downloaded_tmp(kind, d, key, size)
        return FetchResult(True, "downloaded_to_tmp", kind, d.isoformat(), path=str(out_path), s3_key=key, attempts=attempts, file_size_bytes=size).to_dict()
    except Exception as exc:
        err = f"download_failed: {type(exc).__name__}: {str(exc)[:180]}"
        status = "download_failed_after_retries" if attempts >= max_attempts() else "download_failed"
        _record_attempt(kind, d, status, err)
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        return FetchResult(False, status, kind, d.isoformat(), s3_key=key, error=err, attempts=attempts, file_size_bytes=size).to_dict()


def pull_flatfiles_for_window(
    *,
    end_date: str | date | datetime | None = None,
    minute_days: int = 10,
    daily_days: int = 25,
    force: bool = False,
) -> dict[str, Any]:
    tmp_dir = make_tmp_dir()
    minute_dates = trading_days_ending(end_date, max(1, min(14, int(minute_days or 10))))
    daily_dates = trading_days_ending(end_date, max(1, min(35, int(daily_days or 25))))
    minute_paths: list[str] = []
    daily_paths: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        for d in minute_dates:
            r = fetch_flatfile_to_tmp(DATASET_MINUTE, d, tmp_dir=tmp_dir, force=force)
            results.append(r)
            if r.get("ok") and r.get("path"):
                minute_paths.append(str(r.get("path")))
        for d in daily_dates:
            r = fetch_flatfile_to_tmp(DATASET_DAILY, d, tmp_dir=tmp_dir, force=force)
            results.append(r)
            if r.get("ok") and r.get("path"):
                daily_paths.append(str(r.get("path")))
        return {
            "ok": bool(minute_paths or daily_paths),
            "version": POLYGON_FLATFILE_FETCHER_VERSION,
            "tmp_dir": str(tmp_dir),
            "minute_paths": minute_paths,
            "daily_paths": daily_paths,
            "minute_dates": [d.isoformat() for d in minute_dates],
            "daily_dates": [d.isoformat() for d in daily_dates],
            "results": results,
            "config": flatfiles_config_status(),
        }
    except Exception as exc:
        cleanup_tmp_path(tmp_dir)
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}", "tmp_dir": str(tmp_dir), "results": results}
