# -*- coding: utf-8 -*-
"""
Encryption Vault & Log Sanitizer.
- Encrypts account passwords and Access Tokens at rest using Fernet (AES-128-CBC + HMAC-SHA256).
- Redacts tokens, passwords, and long hex secrets from all logs and error messages.
"""

import re
from cryptography.fernet import Fernet, InvalidToken
from app.config import FERNET_KEY

_fernet = Fernet(FERNET_KEY.encode("utf-8"))

_SENSITIVE_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-_\.~+/]+=*", re.IGNORECASE),
    re.compile(r"(access_token['\"\s:=]+)[^\s,'\"&}]+", re.IGNORECASE),
    re.compile(r"(password['\"\s:=]+)[^\s,'\"&}]+", re.IGNORECASE),
    re.compile(r"(client_secret['\"\s:=]+)[^\s,'\"&}]+", re.IGNORECASE),
    re.compile(r"((?:refresh_token|jwt|open_id)\s*['\"]?\s*[:=]\s*['\"]?)[^\s,'\"&}]+", re.IGNORECASE),
    re.compile(r"\b[0-9a-fA-F]{48,}\b"),
]


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a credential string before storing in SQLite."""
    if not plaintext:
        return ""
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored credential on the server side only when needed for login."""
    if not ciphertext:
        return ""
    try:
        return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        return ""


def redact_sensitive(text: str) -> str:
    """Ensure credentials, access tokens, and long hex payloads are never logged or sent to UI."""
    if not text:
        return ""
    cleaned = str(text)
    cleaned = _SENSITIVE_PATTERNS[0].sub(r"\1[REDACTED]", cleaned)
    cleaned = _SENSITIVE_PATTERNS[1].sub(r"\1[REDACTED]", cleaned)
    cleaned = _SENSITIVE_PATTERNS[2].sub(r"\1[REDACTED]", cleaned)
    cleaned = _SENSITIVE_PATTERNS[3].sub(r"\1[REDACTED]", cleaned)
    cleaned = _SENSITIVE_PATTERNS[4].sub(r"\1[REDACTED]", cleaned)
    cleaned = _SENSITIVE_PATTERNS[5].sub("[HEX_PAYLOAD_REDACTED]", cleaned)
    return cleaned
