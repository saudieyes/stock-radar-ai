"""First-run username/password store for Stock Radar AI."""
from __future__ import annotations

import hashlib
import os
import secrets
import time

from .sqlite_store import SQLITE_ENABLED, _connect, init_db


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), 180_000).hex()


def has_auth_user() -> bool:
    if not SQLITE_ENABLED:
        return False
    try:
        init_db()
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM auth_users").fetchone()
        return bool(row and int(row["c"] or 0) > 0)
    except Exception:
        return False


def create_first_user(username: str, password: str) -> tuple[bool, str]:
    username = str(username or "").strip()
    password = str(password or "")
    if not username or len(username) < 3:
        return False, "username_too_short"
    if not password or len(password) < 6:
        return False, "password_too_short"
    if has_auth_user():
        return False, "user_already_exists"
    try:
        init_db()
        salt = secrets.token_hex(16)
        ph = _hash_password(password, salt)
        now = time.time()
        with _connect() as conn:
            conn.execute(
                "INSERT INTO auth_users(username, password_hash, salt, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (username, ph, salt, now, now),
            )
            conn.commit()
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def verify_db_user(username: str, password: str) -> bool:
    username = str(username or "").strip()
    password = str(password or "")
    if not username or not password:
        return False
    try:
        init_db()
        with _connect() as conn:
            row = conn.execute("SELECT password_hash, salt FROM auth_users WHERE username=?", (username,)).fetchone()
        if not row:
            return False
        expected = str(row["password_hash"] or "")
        salt = str(row["salt"] or "")
        actual = _hash_password(password, salt)
        return secrets.compare_digest(expected, actual)
    except Exception:
        return False

