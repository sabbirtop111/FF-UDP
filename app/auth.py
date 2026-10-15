# -*- coding: utf-8 -*-
"""
Administrator Authentication & Endpoint Protection.
Uses Argon2id password hashing (via argon2-cffi), HMAC-SHA256 signed tokens,
HttpOnly session cookies + Bearer headers, and IP-based login rate limiting.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Dict, List, Optional
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Cookie, Depends, Header, HTTPException, Request, WebSocket, status

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME, SECRET_KEY, SESSION_EXPIRE_HOURS, admin_config_fingerprint
from app import database as db

_ph = PasswordHasher()
_ADMIN_STATE = {
    "username": ADMIN_USERNAME,
    "password_hash": _ph.hash(ADMIN_PASSWORD),
    "session_epoch": secrets.token_hex(16),
}

def init_admin_auth() -> None:
    """After schema migration, reload persistent password and revocation epoch."""
    # A fresh hash is used only when the configured deployment password has
    # changed. A prior in-app password change keeps its DB hash otherwise.
    password_hash, epoch = db.get_or_create_admin_auth(
        _ph.hash(ADMIN_PASSWORD), secrets.token_hex(16),
        admin_config_fingerprint(_ADMIN_STATE["username"], ADMIN_PASSWORD))
    _ADMIN_STATE["password_hash"] = password_hash
    _ADMIN_STATE["session_epoch"] = epoch


# Rate limiting: IP -> list of failed attempt timestamps
_FAILED_LOGINS: Dict[str, List[float]] = {}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 60.0


def check_rate_limit(client_ip: str) -> None:
    now = time.time()
    attempts = [t for t in _FAILED_LOGINS.get(client_ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    _FAILED_LOGINS[client_ip] = attempts
    if len(attempts) >= MAX_FAILED_ATTEMPTS:
        retry_after = int(LOCKOUT_WINDOW_SECONDS - (now - attempts[0])) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Try again in {retry_after}s.",
        )


def record_failed_login(client_ip: str) -> None:
    now = time.time()
    attempts = [t for t in _FAILED_LOGINS.get(client_ip, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    attempts.append(now)
    _FAILED_LOGINS[client_ip] = attempts


def clear_failed_logins(client_ip: str) -> None:
    _FAILED_LOGINS.pop(client_ip, None)


def verify_admin_credentials(username: str, password: str) -> bool:
    if not hmac.compare_digest(username.strip(), _ADMIN_STATE["username"]):
        return False
    try:
        return _ph.verify(_ADMIN_STATE["password_hash"], password)
    except (VerifyMismatchError, Exception):
        return False


def update_admin_password(current_password: str, new_password: str) -> bool:
    try:
        _ph.verify(_ADMIN_STATE["password_hash"], current_password)
    except (VerifyMismatchError, Exception):
        return False
    if len(new_password) < 12:
        return False
    new_hash, new_epoch = _ph.hash(new_password), secrets.token_hex(16)
    db.save_admin_auth(new_hash, new_epoch)  # fail closed if persistence fails
    _ADMIN_STATE["password_hash"] = new_hash
    _ADMIN_STATE["session_epoch"] = new_epoch  # revoke all older tokens
    return True


def invalidate_admin_sessions() -> None:
    """A single-admin workspace: signing out revokes every active session."""
    new_epoch = secrets.token_hex(16)
    db.save_admin_auth(_ADMIN_STATE["password_hash"], new_epoch)
    _ADMIN_STATE["session_epoch"] = new_epoch


def create_session_token(username: str) -> str:
    payload = {
        "sub": username,
        "role": "admin",
        "epoch": _ADMIN_STATE["session_epoch"],
        "iat": int(time.time()),
        "exp": int(time.time()) + SESSION_EXPIRE_HOURS * 3600,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    sig = hmac.new(SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_token(token: Optional[str]) -> Optional[Dict]:
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected_sig = hmac.new(
            SECRET_KEY.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8"))
        now = int(time.time())
        if (payload.get("exp", 0) <= now or payload.get("iat", now + 1) > now
                or payload.get("role") != "admin"
                or payload.get("sub") != _ADMIN_STATE["username"]
                or not hmac.compare_digest(str(payload.get("epoch", "")), _ADMIN_STATE["session_epoch"])):
            return None
        return payload
    except Exception:
        return None


async def require_admin(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    ff_admin_session: Optional[str] = Cookie(default=None),
) -> Dict:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif ff_admin_session:
        token = ff_admin_session

    payload = verify_session_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in as administrator.",
        )
    return payload


async def verify_websocket_admin(websocket: WebSocket) -> Optional[Dict]:
    # Browser WebSocket credentials arrive via HttpOnly same-origin cookie.
    # Do not accept URL query tokens (they leak through history/proxy logs).
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    if origin and host:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
        expected = "https" if websocket.url.scheme == "wss" else "http"
        if parsed.scheme != expected or parsed.netloc != host:
            return None
    return verify_session_token(websocket.cookies.get("ff_admin_session"))
