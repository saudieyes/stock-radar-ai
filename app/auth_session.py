import hashlib
import secrets
import time
from fastapi import Request

from .settings import (
    APP_AUTH_COOKIE_NAME,
    APP_AUTH_ENABLED,
    APP_AUTH_SESSION_DAYS,
    APP_AUTH_USERNAME,
    APP_SESSION_SECRET,
)
def _auth_cookie_sign(payload: str) -> str:
    return hashlib.sha256(f"{payload}|{APP_SESSION_SECRET}".encode("utf-8")).hexdigest()


def build_auth_cookie_value(username: str) -> str:
    expires_at = int(time.time()) + (APP_AUTH_SESSION_DAYS * 24 * 60 * 60)
    payload = f"{username}|{expires_at}"
    signature = _auth_cookie_sign(payload)
    return f"{payload}|{signature}"


def read_auth_cookie(request: Request):
    token = str(request.cookies.get(APP_AUTH_COOKIE_NAME, "") or "").strip()
    if not token:
        return None
    parts = token.split("|")
    if len(parts) != 3:
        return None
    username, expires_at_raw, signature = parts
    if not username or not expires_at_raw or not signature:
        return None
    try:
        expires_at = int(expires_at_raw)
    except:
        return None
    if expires_at < int(time.time()):
        return None
    expected = _auth_cookie_sign(f"{username}|{expires_at}")
    if not secrets.compare_digest(signature, expected):
        return None
    if APP_AUTH_ENABLED and username != APP_AUTH_USERNAME:
        return None
    return {"username": username, "expires_at": expires_at}
