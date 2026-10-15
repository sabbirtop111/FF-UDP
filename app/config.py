# -*- coding: utf-8 -*-
import os
import secrets
from typing import Any, Dict
import hmac
import hashlib
from pathlib import Path
from cryptography.fernet import Fernet
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Hosting/platform environment variables take precedence over .env by default.
# Capture the source before dotenv fills missing values; never print the value.
# Shown in the console header, in /api/health and in the startup log so an operator can
# always confirm which build a deployment is actually running (and cache-bust assets).
APP_BUILD = os.getenv("APP_BUILD", "v18.5-2026-10-14").strip() or "v18.5-2026-10-14"

ADMIN_PASSWORD_ENV_OVERRIDE = bool(os.getenv("ADMIN_PASSWORD", "").strip())
load_dotenv(ENV_PATH)


def _ensure_env_secrets() -> tuple[str, str]:
    """Ensure persistent SECRET_KEY and FERNET_KEY exist so encrypted accounts survive restarts."""
    secret_key = os.getenv("SECRET_KEY", "").strip()
    fernet_key = os.getenv("FERNET_KEY", "").strip()

    updated_lines = []
    need_write = False

    if not secret_key:
        secret_key = secrets.token_hex(32)
        os.environ["SECRET_KEY"] = secret_key
        updated_lines.append(f"SECRET_KEY={secret_key}")
        need_write = True

    if not fernet_key:
        fernet_key = Fernet.generate_key().decode("utf-8")
        os.environ["FERNET_KEY"] = fernet_key
        updated_lines.append(f"FERNET_KEY={fernet_key}")
        need_write = True

    if need_write:
        try:
            existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
            with open(ENV_PATH, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                for line in updated_lines:
                    f.write(line + "\n")
            ENV_PATH.chmod(0o600)
        except OSError as exc:
            raise RuntimeError("Set persistent SECRET_KEY and FERNET_KEY in hosting secrets; .env is not writable.") from exc

    return secret_key, fernet_key


SECRET_KEY, FERNET_KEY = _ensure_env_secrets()

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip()


def _admin_password() -> str:
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if password and (len(password) < 12 or password in {
        "admin123", "ChangeThisStrongPassword!2026", "password123456"
    }):
        raise RuntimeError("Insecure ADMIN_PASSWORD. Set a unique password of at least 12 characters in .env or hosting secrets.")
    if password:
        return password
    # First-run local installation: create a strong *private* persistent secret
    # instead of shipping a public/admin123 fallback. Never print it in logs.
    password = secrets.token_urlsafe(32)
    try:
        with open(ENV_PATH, "a", encoding="utf-8") as f:
            f.write(f"\nADMIN_PASSWORD={password}\n")
        ENV_PATH.chmod(0o600)
    except OSError as exc:
        raise RuntimeError("Set ADMIN_PASSWORD in the hosting secrets manager; .env is not writable.") from exc
    os.environ["ADMIN_PASSWORD"] = password
    return password


ADMIN_PASSWORD = _admin_password()


def admin_config_fingerprint(username: str, password: str) -> str:
    """Secret-keyed marker for deliberate deployment credential rotation.

    Never store a raw or unsalted password digest in the database.
    """
    message = ("admin-config-v1\0" + username + "\0" + password).encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


# Explicit one-boot operator recovery for hosts without a terminal; remove after reset.
ADMIN_PASSWORD_RESET_ON_START = os.getenv("ADMIN_PASSWORD_RESET_ON_START", "").strip() == "1"
SESSION_EXPIRE_HOURS = int(os.getenv("SESSION_EXPIRE_HOURS", "24"))

DATABASE_PATH = os.getenv("DATABASE_PATH", str(DATA_DIR / "dashboard.db"))
DEVICES_FILE_PATH = os.getenv("DEVICES_FILE_PATH", str(DATA_DIR / "devices.json"))
os.environ["DEVICES_FILE_PATH"] = DEVICES_FILE_PATH

# Limits are explicit: account workers are long-lived; logins are short bursts.
# Increase gradually only after monitoring CPU, memory, network and rate limits.
# V18.5: starting several accounts at once must not look like a burst to Garena.
# Each worker waits its turn (this many seconds apart) before it begins its login.
ACCOUNT_START_STAGGER_SECONDS = max(0.0, min(60.0, float(os.getenv("ACCOUNT_START_STAGGER_SECONDS", "4"))))
MAX_ACTIVE_WORKERS = max(1, min(1000, int(os.getenv("MAX_ACTIVE_WORKERS", "50"))))
MAX_CONCURRENT_LOGINS = max(1, min(100, int(os.getenv("MAX_CONCURRENT_LOGINS", "8"))))

DEFAULT_ACCOUNT_REGION = os.getenv("DEFAULT_ACCOUNT_REGION", "ME").upper()
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")

# Each account is cooled down independently after 100 consecutive successful
# StartMatch sends without a server-confirmed UDP JOIN_MATCH.
QUEUE_NO_JOIN_LIMIT = max(10, min(10000, int(os.getenv("QUEUE_NO_JOIN_LIMIT", "100"))))
QUEUE_COOLDOWN_SECONDS = max(60, min(3600, int(os.getenv("QUEUE_COOLDOWN_SECONDS", "600"))))

# ---------------------------------------------------------------------------------------
# Daily EXP target (auto-offline) — per-account daily gain cap chosen by the saved level.
# Levels 1-29 stop at 5,000 EXP/day, levels 30+ stop at 50,000 EXP/day by default.
# The chatty rule is configurable so an operator can retune it without code changes.
# ---------------------------------------------------------------------------------------
def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# EXP credit diagnosis (V18.1)
# The bot can only report what the game server returns in the profile. When an
# account keeps joining matches but the server profile EXP never moves, the
# stall watchdog records exactly that instead of leaving the operator guessing.
# ---------------------------------------------------------------------------
PROFILE_POLL_SECONDS = _env_int("PROFILE_POLL_SECONDS", 45, 15, 600)
EXP_STALL_MINUTES = _env_int("EXP_STALL_MINUTES", 30, 5, 1440)
EXP_STALL_RELOG_MINUTES = _env_int("EXP_STALL_RELOG_MINUTES", 60, 5, 1440)

DAILY_EXP_ENABLED = _env_flag("DAILY_EXP_ENABLED", True)
DAILY_EXP_LOW_LEVEL_MAX = _env_int("DAILY_EXP_LOW_LEVEL_MAX", 29, 1, 1000)
DAILY_EXP_LOW_LIMIT = _env_int("DAILY_EXP_LOW_LIMIT", 5000, 0, 100_000_000)
DAILY_EXP_HIGH_LIMIT = _env_int("DAILY_EXP_HIGH_LIMIT", 50000, 0, 100_000_000)
# Local day boundary for the daily counter; default UTC+6 (Asia/Dhaka) so "today"
# resets at local midnight instead of 06:00 local time.
DAILY_EXP_RESET_OFFSET_HOURS = _env_int("DAILY_EXP_RESET_OFFSET_HOURS", 6, -12, 14)
# Optional: automatically start accounts again on the next day once their counter resets.
DAILY_EXP_AUTO_RESUME = _env_flag("DAILY_EXP_AUTO_RESUME", False)
DAILY_EXP_HISTORY_DAYS = _env_int("DAILY_EXP_HISTORY_DAYS", 14, 1, 365)


def daily_limit_for_level(level: Any = None) -> int:
    """Daily EXP cap for a saved level. 0 means 'no daily cap'."""
    if not DAILY_EXP_ENABLED:
        return 0
    try:
        lvl = int(level)
    except (TypeError, ValueError):
        lvl = 1
    if lvl <= 0:
        lvl = 1
    return DAILY_EXP_LOW_LIMIT if lvl <= DAILY_EXP_LOW_LEVEL_MAX else DAILY_EXP_HIGH_LIMIT


def daily_rule_payload() -> Dict[str, Any]:
    """Non-secret description of the active daily rule for dashboards and cards."""
    return {
        "enabled": bool(DAILY_EXP_ENABLED),
        "low_level_max": DAILY_EXP_LOW_LEVEL_MAX,
        "low_limit": DAILY_EXP_LOW_LIMIT,
        "high_limit": DAILY_EXP_HIGH_LIMIT,
        "reset_offset_hours": DAILY_EXP_RESET_OFFSET_HOURS,
        "auto_resume": bool(DAILY_EXP_AUTO_RESUME),
        "summary": (
            f"LV 1-{DAILY_EXP_LOW_LEVEL_MAX}: {DAILY_EXP_LOW_LIMIT:,} EXP/day · "
            f"LV {DAILY_EXP_LOW_LEVEL_MAX + 1}+: {DAILY_EXP_HIGH_LIMIT:,} EXP/day"
        ),
    }
