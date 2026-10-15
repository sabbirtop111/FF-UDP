# -*- coding: utf-8 -*-
"""
SQLite Persistence Layer for Multi-Account Management, Error History, and Activity Logs.
Never exposes encrypted_password or tokens in any public response dictionary.
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import lv
from app.config import (
    DATABASE_PATH,
    DAILY_EXP_ENABLED,
    DAILY_EXP_HISTORY_DAYS,
    DAILY_EXP_RESET_OFFSET_HOURS,
    daily_limit_for_level,
    daily_rule_payload,
)
from app.exp_chart import exp_progress
from app.crypto_vault import decrypt_secret, encrypt_secret, redact_sensitive

_DB_LOCK = threading.RLock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    uid TEXT UNIQUE NOT NULL,
                    game_account_id TEXT DEFAULT 'Pending',
                    display_name TEXT NOT NULL,
                    nickname TEXT DEFAULT '',
                    encrypted_password TEXT NOT NULL,
                    auth_type TEXT NOT NULL DEFAULT 'uid_password',
                    encrypted_access_token TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT 'ME',
                    status TEXT NOT NULL DEFAULT 'inactive',
                    status_message TEXT DEFAULT 'Stopped / Ready',
                    level INTEGER DEFAULT 1,
                    initial_level INTEGER DEFAULT 1,
                    exp INTEGER DEFAULT 0,
                    initial_exp INTEGER DEFAULT 0,
                    exp_baseline_set INTEGER DEFAULT 0,
                    session_start_exp INTEGER,
                    session_started_at TEXT,
                    profile_last_checked_at TEXT,
                    profile_last_changed_at TEXT,
                    session_last_exp_gain_at TEXT,
                    session_exp_gain_events INTEGER DEFAULT 0,
                    profile_check_status TEXT DEFAULT 'never',
                    profile_check_message TEXT DEFAULT '',
                    matches_searched INTEGER DEFAULT 0,
                    queue_no_join_streak INTEGER DEFAULT 0,
                    cooldown_until TEXT,
                    queue_cooldown_count INTEGER DEFAULT 0,
                    udp_phase TEXT DEFAULT 'none',
                    udp_phase_at TEXT,
                    udp_startup_at TEXT,
                    matches_completed INTEGER DEFAULT 0,
                    server_finish_count INTEGER DEFAULT 0,
                    udp_join_count INTEGER DEFAULT 0,
                    udp_timeout_count INTEGER DEFAULT 0,
                    last_udp_join_at TEXT,
                    last_udp_timeout_at TEXT,
                    last_server_finish_at TEXT,
                    reconnect_count INTEGER DEFAULT 0,
                    total_uptime_seconds INTEGER DEFAULT 0,
                    mode_id INTEGER DEFAULT 1,
                    map_id INTEGER DEFAULT 1,
                    device_profile_json TEXT DEFAULT '{}',
                    notes TEXT DEFAULT '',
                    last_activity TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS error_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_error_history_account ON error_history(account_id, id DESC);

                CREATE TABLE IF NOT EXISTS admin_auth (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    password_hash TEXT NOT NULL,
                    session_epoch TEXT NOT NULL,
                    bootstrap_fingerprint TEXT
                );

                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT,
                    account_label TEXT,
                    level TEXT NOT NULL DEFAULT 'info',
                    event_type TEXT NOT NULL DEFAULT 'system',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_activity_logs_created ON activity_logs(id DESC);

                -- V18: daily EXP gain per account. One row per account per local day.
                CREATE TABLE IF NOT EXISTS daily_exp_progress (
                    account_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    exp_gained INTEGER NOT NULL DEFAULT 0,
                    auto_stopped INTEGER NOT NULL DEFAULT 0,
                    reached_at TEXT,
                    stopped_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, day)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_exp_day ON daily_exp_progress(day, exp_gained DESC);
                """
            )
            # Existing installations keep their saved data; no table replacement.
            admin_columns = {row[1] for row in conn.execute("PRAGMA table_info(admin_auth)")}
            if "bootstrap_fingerprint" not in admin_columns:
                conn.execute("ALTER TABLE admin_auth ADD COLUMN bootstrap_fingerprint TEXT")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
            new_columns = {
                "auth_type": "TEXT NOT NULL DEFAULT 'uid_password'",
                "encrypted_access_token": "TEXT NOT NULL DEFAULT ''",
                "exp_baseline_set": "INTEGER DEFAULT 0",
                "session_start_exp": "INTEGER",
                "session_started_at": "TEXT",
                "profile_last_checked_at": "TEXT",
                "profile_last_changed_at": "TEXT",
                "session_last_exp_gain_at": "TEXT",
                "exp_stall_since": "TEXT",
                "exp_stall_checks": "INTEGER DEFAULT 0",
                "exp_stall_message": "TEXT DEFAULT ''",
                "session_exp_gain_events": "INTEGER DEFAULT 0",
                "profile_check_status": "TEXT DEFAULT 'never'",
                "profile_check_message": "TEXT DEFAULT ''",
                "queue_no_join_streak": "INTEGER DEFAULT 0",
                "cooldown_until": "TEXT",
                "queue_cooldown_count": "INTEGER DEFAULT 0",
                "udp_phase": "TEXT DEFAULT 'none'",
                "udp_phase_at": "TEXT",
                "udp_startup_at": "TEXT",
                "server_finish_count": "INTEGER DEFAULT 0",
                "udp_join_count": "INTEGER DEFAULT 0",
                "udp_timeout_count": "INTEGER DEFAULT 0",
                "last_udp_join_at": "TEXT",
                "last_udp_timeout_at": "TEXT",
                "last_server_finish_at": "TEXT",
            }
            for name, kind in new_columns.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE accounts ADD COLUMN {name} {kind}")
            # Legacy initial_exp may have been overwritten on first gain from zero.
            # Mark it uncertain, never reconstruct a made-up historical EXP gain.
            if "exp_baseline_set" not in columns:
                conn.execute("""
                    UPDATE accounts SET exp_baseline_set = 2
                    WHERE game_account_id NOT IN ('Pending', '0', 'None', '')
                """)
            # Reset any stale 'online' / 'authenticating' states from previous process run to 'inactive'
            conn.execute(
                """
                UPDATE accounts
                SET status = 'inactive',
                    status_message = 'Idle (Worker stopped)'
                WHERE status IN ('online', 'authenticating', 'searching_match', 'in_match', 'reconnecting');
                """
            )
            conn.commit()
        finally:
            conn.close()


def _sanitize_device_profile(dev: Dict[str, Any]) -> Dict[str, Any]:
    """Return safe device hardware metadata without exposing raw unique identifiers."""
    if not isinstance(dev, dict):
        return {}
    raw_id = str(dev.get("unique_device_id", ""))
    masked_id = f"{raw_id[:13]}...{raw_id[-4:]}" if len(raw_id) > 18 else "Google|****"
    return {
        "brand": dev.get("brand", "Android"),
        "model": dev.get("model", "Generic"),
        "gpu_renderer": dev.get("gpu_renderer", "Adreno"),
        "system_software": dev.get("system_software", "Android OS"),
        "screen_resolution": f"{dev.get('screen_width', 1080)}x{dev.get('screen_height', 2400)}",
        "memory_mb": dev.get("memory", 4096),
        "device_fingerprint_masked": masked_id,
    }


# ---------------------------------------------------------------------------------------
# Daily EXP tracking (V18) — real gains only, taken from server profile differences.
# ---------------------------------------------------------------------------------------
def daily_day_key(moment: Optional[datetime] = None) -> str:
    """Local day key for the configured daily reset offset (default UTC+6)."""
    base = moment or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(hours=DAILY_EXP_RESET_OFFSET_HOURS)).strftime("%Y-%m-%d")


def daily_reset_at(day: Optional[str] = None) -> str:
    """UTC ISO timestamp of the next daily reset for a given local day key."""
    key = day or daily_day_key()
    try:
        start = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        start = datetime.now(timezone.utc)
    next_local_midnight = start + timedelta(days=1)
    reset_utc = next_local_midnight - timedelta(hours=DAILY_EXP_RESET_OFFSET_HOURS)
    return reset_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _daily_row(conn: sqlite3.Connection, account_id: str, day: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT account_id, day, exp_gained, auto_stopped, reached_at, stopped_at, updated_at"
        " FROM daily_exp_progress WHERE account_id = ? AND day = ?",
        (account_id, day),
    ).fetchone()


def add_daily_exp(account_id: str, delta: int, day: Optional[str] = None) -> Dict[str, Any]:
    """Add a verified EXP increase to today's counter and return the updated row."""
    day = day or daily_day_key()
    delta = int(delta or 0)
    now = utc_now_iso()
    with _DB_LOCK:
        conn = get_connection()
        try:
            if delta:
                conn.execute(
                    "INSERT INTO daily_exp_progress (account_id, day, exp_gained, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(account_id, day) DO UPDATE SET"
                    " exp_gained = exp_gained + excluded.exp_gained, updated_at = excluded.updated_at",
                    (account_id, day, delta, now),
                )
                conn.commit()
            row = _daily_row(conn, account_id, day)
            return dict(row) if row else {"account_id": account_id, "day": day, "exp_gained": 0,
                                          "auto_stopped": 0, "reached_at": None, "stopped_at": None, "updated_at": now}
        finally:
            conn.close()


def mark_daily_limit_reached(account_id: str, day: Optional[str] = None, *, total: Optional[int] = None) -> Dict[str, Any]:
    """Flag that the account hit its daily target today (does not mark it stopped yet)."""
    day = day or daily_day_key()
    now = utc_now_iso()
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO daily_exp_progress (account_id, day, exp_gained, reached_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(account_id, day) DO UPDATE SET"
                " reached_at = COALESCE(daily_exp_progress.reached_at, excluded.reached_at),"
                " exp_gained = MAX(daily_exp_progress.exp_gained, excluded.exp_gained),"
                " updated_at = excluded.updated_at",
                (account_id, day, int(total or 0), now, now),
            )
            conn.commit()
            row = _daily_row(conn, account_id, day)
            return dict(row) if row else {}
        finally:
            conn.close()


def mark_daily_auto_stopped(account_id: str, day: Optional[str] = None) -> Dict[str, Any]:
    """Record that the worker really was taken offline by the daily target."""
    day = day or daily_day_key()
    now = utc_now_iso()
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO daily_exp_progress (account_id, day, auto_stopped, stopped_at, updated_at)"
                " VALUES (?, ?, 1, ?, ?)"
                " ON CONFLICT(account_id, day) DO UPDATE SET auto_stopped = 1,"
                " stopped_at = COALESCE(daily_exp_progress.stopped_at, excluded.stopped_at),"
                " updated_at = excluded.updated_at",
                (account_id, day, now, now),
            )
            conn.commit()
            row = _daily_row(conn, account_id, day)
            return dict(row) if row else {}
        finally:
            conn.close()


def reset_daily_exp(account_id: str, day: Optional[str] = None) -> int:
    """Clear today's counter for one account (admin action). Returns rows removed."""
    day = day or daily_day_key()
    with _DB_LOCK:
        conn = get_connection()
        try:
            cur = conn.execute("DELETE FROM daily_exp_progress WHERE account_id = ? AND day = ?", (account_id, day))
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


def reset_all_daily_exp(day: Optional[str] = None) -> int:
    """Clear today's counter for every account (admin action). Returns rows removed."""
    day = day or daily_day_key()
    with _DB_LOCK:
        conn = get_connection()
        try:
            cur = conn.execute("DELETE FROM daily_exp_progress WHERE day = ?", (day,))
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


def get_daily_exp(account_id: str, day: Optional[str] = None) -> Dict[str, Any]:
    day = day or daily_day_key()
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = _daily_row(conn, account_id, day)
            return dict(row) if row else {"account_id": account_id, "day": day, "exp_gained": 0,
                                          "auto_stopped": 0, "reached_at": None, "stopped_at": None, "updated_at": None}
        finally:
            conn.close()


def get_daily_exp_map(day: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    day = day or daily_day_key()
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT account_id, day, exp_gained, auto_stopped, reached_at, stopped_at, updated_at"
                " FROM daily_exp_progress WHERE day = ?",
                (day,),
            ).fetchall()
            return {r["account_id"]: dict(r) for r in rows}
        finally:
            conn.close()


def daily_exp_totals(day: Optional[str] = None) -> Dict[str, int]:
    """Today's fleet totals: verified EXP gained, accounts at target, accounts auto-stopped."""
    day = day or daily_day_key()
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(exp_gained), 0) AS total,"
                " COALESCE(SUM(CASE WHEN reached_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS reached,"
                " COALESCE(SUM(auto_stopped), 0) AS stopped"
                " FROM daily_exp_progress WHERE day = ?",
                (day,),
            ).fetchone()
            return {"total_exp": int(row["total"] or 0), "reached": int(row["reached"] or 0),
                    "auto_stopped": int(row["stopped"] or 0), "day": day}
        finally:
            conn.close()


def purge_old_daily_exp(keep_days: Optional[int] = None) -> int:
    """Keep the daily history small; old day rows are removed."""
    keep = int(keep_days or DAILY_EXP_HISTORY_DAYS)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep)).strftime("%Y-%m-%d")
    with _DB_LOCK:
        conn = get_connection()
        try:
            cur = conn.execute("DELETE FROM daily_exp_progress WHERE day < ?", (cutoff,))
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


def with_daily_fields(account: Dict[str, Any], daily: Optional[Dict[str, Any]] = None,
                      day: Optional[str] = None) -> Dict[str, Any]:
    """Attach today's daily-target fields to an account dictionary."""
    day = day or daily_day_key()
    if daily is None:
        try:
            daily = get_daily_exp(account.get("id"), day=day)
        except Exception:
            daily = {}
    row = daily or {}
    level = account.get("level")
    limit = daily_limit_for_level(level)
    gained = int(row.get("exp_gained") or 0)
    reached = bool(row.get("reached_at")) or (bool(limit) and gained >= limit)
    account.update({
        "daily_limit": limit,
        "daily_exp_gained": gained,
        "daily_exp_remaining": max(0, limit - gained) if limit else None,
        "daily_exp_percent": (min(100, round(gained * 100 / limit)) if limit else None),
        "daily_exp_reached": bool(reached and limit),
        "daily_auto_stopped": bool(row.get("auto_stopped")),
        "daily_reached_at": row.get("reached_at"),
        "daily_stopped_at": row.get("stopped_at"),
        "daily_day": day,
        "daily_reset_at": daily_reset_at(day),
    })
    return account


def row_to_public_account(row: sqlite3.Row, error_count: Optional[int] = None) -> Dict[str, Any]:
    """Convert a DB row into a safe dictionary for the API/UI. Never includes passwords or tokens."""
    d = dict(row)
    has_password = bool(d.pop("encrypted_password", None))
    has_access_token = bool(d.pop("encrypted_access_token", None))
    auth_type = d.get("auth_type") or "uid_password"
    raw_dev = {}
    try:
        raw_dev = json.loads(d.pop("device_profile_json", "{}") or "{}")
    except Exception:
        raw_dev = {}

    reg = d.get("region", "ME")
    exp_now = int(d.get("exp") or 0)
    exp_init = int(d.get("initial_exp") or 0)
    baseline_state = int(d.get("exp_baseline_set") or 0)
    session_start = d.get("session_start_exp")
    exp_gained = max(0, exp_now - exp_init) if baseline_state == 1 else None
    session_exp_gained = max(0, exp_now - int(session_start)) if session_start is not None else None

    result = {
        "id": d["id"],
        "uid": d["uid"],
        "game_account_id": d.get("game_account_id") or "Pending",
        "display_name": d["display_name"],
        "nickname": d.get("nickname") or d["display_name"],
        "region": reg,
        "region_prefix": lv.REGION_PREFIX.get(reg, "031500"),
        "region_lang": lv.REGION_LANG.get(reg, "en"),
        "status": d.get("status", "inactive"),
        "status_message": redact_sensitive(d.get("status_message") or ""),
        "level": int(d.get("level") or 1),
        "initial_level": int(d.get("initial_level") or 1),
        "exp": exp_now,
        "initial_exp": exp_init,
        "exp_gained": exp_gained,
        "exp_baseline_status": "verified" if baseline_state == 1 else "legacy_unknown" if baseline_state == 2 else "awaiting_profile",
        "session_start_exp": session_start,
        "session_started_at": d.get("session_started_at"),
        "session_exp_gained": session_exp_gained,
        "profile_last_checked_at": d.get("profile_last_checked_at"),
        "profile_last_changed_at": d.get("profile_last_changed_at"),
        "session_last_exp_gain_at": d.get("session_last_exp_gain_at"),
        "exp_stall_since": d.get("exp_stall_since"),
        "exp_stall_checks": int(d.get("exp_stall_checks") or 0),
        "exp_stall_message": d.get("exp_stall_message") or "",
        "exp_stalled": bool(d.get("exp_stall_since")),
        "session_exp_gain_events": int(d.get("session_exp_gain_events") or 0),
        "profile_check_status": d.get("profile_check_status") or "never",
        "profile_check_message": redact_sensitive(d.get("profile_check_message") or ""),
        "matches_searched": int(d.get("matches_searched") or 0),
        "queue_no_join_streak": int(d.get("queue_no_join_streak") or 0),
        "cooldown_until": d.get("cooldown_until"),
        "queue_cooldown_count": int(d.get("queue_cooldown_count") or 0),
        "udp_phase": d.get("udp_phase") or "none",
        "udp_phase_at": d.get("udp_phase_at"),
        "udp_startup_at": d.get("udp_startup_at"),
        "matches_completed": int(d.get("matches_completed") or 0),
        "server_finish_count": int(d.get("server_finish_count") or 0),
        "udp_join_count": int(d.get("udp_join_count") or 0),
        "udp_timeout_count": int(d.get("udp_timeout_count") or 0),
        "last_udp_join_at": d.get("last_udp_join_at"),
        "last_udp_timeout_at": d.get("last_udp_timeout_at"),
        "last_server_finish_at": d.get("last_server_finish_at"),
        "reconnect_count": int(d.get("reconnect_count") or 0),
        "total_uptime_seconds": int(d.get("total_uptime_seconds") or 0),
        "mode_id": int(d.get("mode_id") or 1),
        "map_id": int(d.get("map_id") or 1),
        "notes": d.get("notes") or "",
        "auth_type": auth_type,
        "has_credentials": has_password or has_access_token,
        "has_password": has_password,
        "has_access_token": has_access_token,
        "device_profile": _sanitize_device_profile(raw_dev),
        "last_activity": d.get("last_activity"),
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
    }
    result.update(exp_progress(
        result["level"], exp_now,
        verified_profile=result["game_account_id"] not in ("Pending", "0", "None", ""),
    ))
    if error_count is not None:
        result["error_count"] = error_count
    return with_daily_fields(result)
    


def public_status_counts() -> Dict[str, int]:
    """Aggregate operational states only. Never query/return identities or profile data."""
    allowed = ('authenticating', 'online', 'searching_match', 'in_match',
               'reconnecting', 'inactive', 'cooldown', 'error')
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM accounts GROUP BY status").fetchall()
            raw = {str(row['status']): int(row['n']) for row in rows}
            counts = {name: raw.get(name, 0) for name in allowed}
            counts['total'] = sum(raw.values())
            counts['active'] = sum(counts[k] for k in ('authenticating', 'online',
                'searching_match', 'in_match', 'reconnecting'))
            return counts
        finally:
            conn.close()


def public_bot_cards() -> List[Dict[str, Any]]:
    """Explicit safe allowlist for read-only public cards, not admin row serialization."""
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute("""
                SELECT id, display_name, nickname, game_account_id, region, status,
                       level, exp, session_start_exp, session_started_at,
                       profile_last_checked_at, profile_check_status,
                       matches_searched, server_finish_count, udp_join_count,
                       udp_timeout_count, queue_no_join_streak, cooldown_until,
                       last_udp_join_at, last_udp_timeout_at, last_server_finish_at,
                       udp_phase, udp_phase_at, mode_id, map_id, queue_cooldown_count,
                       reconnect_count, profile_last_changed_at,
                       session_last_exp_gain_at, session_exp_gain_events
                FROM accounts ORDER BY created_at DESC, id DESC
            """).fetchall()
            cards = []
            daily_map_by_id = get_daily_exp_map()
            for row in rows:
                d = dict(row)
                exp = int(d['exp'] or 0)
                game_id = d['game_account_id']
                verified = game_id not in (None, 'Pending', '0', 'None', '')
                progress = exp_progress(int(d['level'] or 1), exp, verified_profile=verified)
                cards.append({
                    'display_name': d['display_name'],
                    'nickname': d['nickname'] or d['display_name'],
                    'game_account_id': game_id if verified else None,
                    'region': d['region'], 'status': d['status'],
                    'level': int(d['level'] or 1) if verified else None,
                    'exp': exp if verified else None,
                    'exp_to_next_level': progress.get('exp_to_next_level'),
                    'exp_chart_status': progress.get('exp_chart_status'),
                    'exp_level_start': progress.get('exp_level_start'),
                    'exp_next_level_total': progress.get('exp_next_level_total'),
                    'exp_needed_for_level': progress.get('exp_needed_for_level'),
                    'exp_level_progress_percent': progress.get('exp_level_progress_percent'),
                    'session_exp_gained': max(0, exp - int(d['session_start_exp'])) if d['session_start_exp'] is not None else None,
                    'session_started_at': d['session_started_at'],
                    'profile_last_checked_at': d['profile_last_checked_at'],
                    'profile_check_status': d['profile_check_status'] or 'never',
                    'profile_last_changed_at': d['profile_last_changed_at'],
                    'session_last_exp_gain_at': d['session_last_exp_gain_at'],
                    'session_exp_gain_events': int(d['session_exp_gain_events'] or 0),
                    'matches_searched': int(d['matches_searched'] or 0),
                    'server_finish_count': int(d['server_finish_count'] or 0),
                    'udp_join_count': int(d['udp_join_count'] or 0),
                    'udp_timeout_count': int(d['udp_timeout_count'] or 0),
                    'queue_no_join_streak': int(d['queue_no_join_streak'] or 0),
                    'cooldown_until': d['cooldown_until'] if d['status'] == 'cooldown' else None,
                    'last_udp_join_at': d['last_udp_join_at'],
                    'last_udp_timeout_at': d['last_udp_timeout_at'],
                    'last_server_finish_at': d['last_server_finish_at'],
                    'udp_phase': d['udp_phase'] or 'none',
                    'udp_phase_at': d['udp_phase_at'],
                    'queue_cooldown_count': int(d['queue_cooldown_count'] or 0),
                    'reconnect_count': int(d['reconnect_count'] or 0),
                    'mode_id': int(d['mode_id'] or 1),
                    'map_id': int(d['map_id'] or 1),
                })
                day_row = daily_map_by_id.get(str(d.get('id') or ''), {})
                cards[-1].update({
                    'daily_exp_gained': int(day_row.get('exp_gained') or 0),
                    'daily_exp_limit': daily_limit_for_level(d['level']),
                    'daily_exp_reached': bool(day_row.get('reached_at')) or (
                        bool(daily_limit_for_level(d['level'])) and int(day_row.get('exp_gained') or 0) >= daily_limit_for_level(d['level'])),
                    'daily_auto_stopped': bool(day_row.get('auto_stopped')),
                    'daily_reset_at': daily_reset_at(),
                })
            return cards
        finally:
            conn.close()


def list_accounts(
    search: Optional[str] = None,
    region: Optional[str] = None,
    status_filter: Optional[str] = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
) -> List[Dict[str, Any]]:
    allowed_sort = {
        "created_at": "a.created_at",
        "updated_at": "a.updated_at",
        "last_activity": "a.last_activity",
        "display_name": "LOWER(a.display_name)",
        "uid": "a.uid",
        "region": "a.region",
        "status": "a.status",
        "level": "a.level",
        "exp": "a.exp",
        "matches_completed": "a.matches_completed",
    }
    order_col = allowed_sort.get(sort_by, "a.created_at")
    order_dir = "ASC" if sort_dir.lower() == "asc" else "DESC"

    where_clauses = []
    params: List[Any] = []

    if search and search.strip():
        q = f"%{search.strip()}%"
        where_clauses.append(
            "(a.display_name LIKE ? OR a.uid LIKE ? OR a.nickname LIKE ? OR a.game_account_id LIKE ? OR a.id LIKE ? OR a.status_message LIKE ?)"
        )
        params.extend([q, q, q, q, q, q])

    if region and region.strip().upper() != "ALL":
        where_clauses.append("a.region = ?")
        params.append(region.strip().upper())

    if status_filter and status_filter.strip().lower() != "all":
        sf = status_filter.strip().lower()
        if sf == "active":
            where_clauses.append("a.status IN ('authenticating', 'online', 'searching_match', 'in_match', 'reconnecting')")
        elif sf == "inactive":
            where_clauses.append("a.status = 'inactive'")
        else:
            where_clauses.append("a.status = ?")
            params.append(sf)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    sql = f"""
        SELECT a.*,
               (SELECT COUNT(*) FROM error_history e WHERE e.account_id = a.id) AS err_cnt
        FROM accounts a
        {where_sql}
        ORDER BY {order_col} {order_dir};
    """
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [row_to_public_account(r, error_count=int(r["err_cnt"])) for r in rows]
        finally:
            conn.close()


def get_account_by_id(account_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT a.*,
                       (SELECT COUNT(*) FROM error_history e WHERE e.account_id = a.id) AS err_cnt
                FROM accounts a
                WHERE a.id = ?;
                """,
                (account_id,),
            ).fetchone()
            if not row:
                return None
            acc = row_to_public_account(row, error_count=int(row["err_cnt"]))
            errs = conn.execute(
                "SELECT id, stage, error_message, created_at FROM error_history WHERE account_id = ? ORDER BY id DESC LIMIT 100;",
                (account_id,),
            ).fetchall()
            acc["error_history"] = [dict(e) for e in errs]
            logs = conn.execute(
                "SELECT id, level, event_type, message, created_at FROM activity_logs WHERE account_id = ? ORDER BY id DESC LIMIT 100;",
                (account_id,),
            ).fetchall()
            acc["recent_logs"] = [dict(l) for l in logs]
            return acc
        finally:
            conn.close()


def get_account_credentials_internal(account_id: str) -> Optional[Dict[str, Any]]:
    """Server-internal helper to decrypt the selected account credential for worker startup."""
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?;", (account_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["auth_type"] = d.get("auth_type") or "uid_password"
            d["password_plain"] = decrypt_secret(d.get("encrypted_password") or "")
            d["access_token_plain"] = decrypt_secret(d.get("encrypted_access_token") or "")
            return d
        finally:
            conn.close()


def create_account(
    uid: Optional[str],
    password: str,
    display_name: Optional[str] = None,
    region: str = "ME",
    mode_id: int = 1,
    map_id: int = 1,
    notes: str = "",
    auth_type: str = "uid_password",
    access_token: Optional[str] = None,
    allow_incomplete_credentials: bool = False,
) -> Dict[str, Any]:
    auth_type = (auth_type or "uid_password").strip().lower()
    if auth_type not in {"uid_password", "access_token"}:
        raise ValueError("Unsupported authentication method.")
    password = password or ""
    access_token = access_token or ""
    if not allow_incomplete_credentials:
        if auth_type == "uid_password" and (not uid or not uid.strip() or not password.strip()):
            raise ValueError("UID and password are required for UID/password login.")
        if auth_type == "access_token" and not access_token.strip():
            raise ValueError("An Access Token is required for Access Token login.")

    uid_clean = (uid or "").strip()
    if not uid_clean:
        uid_clean = f"token_{uuid.uuid4().hex[:16]}"
    reg_clean = region.strip().upper()
    name_clean = (display_name or "").strip() or f"Account_{uid_clean[-6:]}"
    acc_id = f"acc_{uuid.uuid4().hex[:10]}"
    now = utc_now_iso()
    enc_pw = encrypt_secret(password) if password else ""
    enc_access_token = encrypt_secret(access_token) if access_token else ""
    dev_profile = lv.get_device_for_account(uid_clean)

    with _DB_LOCK:
        conn = get_connection()
        try:
            existing = conn.execute("SELECT id FROM accounts WHERE uid = ?;", (uid_clean,)).fetchone()
            if existing:
                raise ValueError(f"An account with UID '{uid_clean}' already exists.")

            conn.execute(
                """
                INSERT INTO accounts (
                    id, uid, game_account_id, display_name, nickname,
                    encrypted_password, auth_type, encrypted_access_token,
                    region, status, status_message,
                    level, initial_level, exp, initial_exp,
                    matches_searched, matches_completed, reconnect_count,
                    total_uptime_seconds, mode_id, map_id,
                    device_profile_json, notes, last_activity, created_at, updated_at
                ) VALUES (?, ?, 'Pending', ?, '', ?, ?, ?, ?, 'inactive', 'Added — Ready to start',
                          1, 1, 0, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    acc_id, uid_clean, name_clean, enc_pw, auth_type, enc_access_token,
                    reg_clean, mode_id, map_id, json.dumps(dev_profile), notes or "",
                    now, now, now
                ),
            )
            conn.commit()
        finally:
            conn.close()

    add_activity_log(
        account_id=acc_id,
        account_label=f"{name_clean} ({uid_clean})",
        level="info",
        event_type="account_created",
        message=f"Account '{name_clean}' ({auth_type}) registered in region {reg_clean}.",
    )
    return get_account_by_id(acc_id)


def update_account(
    account_id: str,
    display_name: Optional[str] = None,
    password: Optional[str] = None,
    region: Optional[str] = None,
    mode_id: Optional[int] = None,
    map_id: Optional[int] = None,
    notes: Optional[str] = None,
    auth_type: Optional[str] = None,
    access_token: Optional[str] = None,
    uid: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    now = utc_now_iso()
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?;", (account_id,)).fetchone()
            if not row:
                return None
            updates = ["updated_at = ?"]
            params: List[Any] = [now]

            normalized_auth_type = None
            if auth_type is not None:
                normalized_auth_type = auth_type.strip().lower()
                if normalized_auth_type not in {"uid_password", "access_token"}:
                    raise ValueError("Unsupported authentication method.")
                updates.append("auth_type = ?")
                params.append(normalized_auth_type)
                # A switched auth method must not leave the old secret stored unnecessarily.
                if normalized_auth_type == "access_token":
                    updates.append("encrypted_password = ?")
                    params.append("")
                else:
                    updates.append("encrypted_access_token = ?")
                    params.append("")

            if display_name is not None and display_name.strip():
                updates.append("display_name = ?")
                params.append(display_name.strip())
            if uid is not None and uid.strip():
                uid_clean = uid.strip()
                updates.append("uid = ?")
                params.append(uid_clean)
                updates.append("device_profile_json = ?")
                params.append(json.dumps(lv.get_device_for_account(uid_clean)))
            if password is not None and password.strip():
                updates.append("encrypted_password = ?")
                params.append(encrypt_secret(password.strip()))
            if access_token is not None and access_token.strip():
                updates.append("encrypted_access_token = ?")
                params.append(encrypt_secret(access_token.strip()))
            if region is not None and region.strip():
                updates.append("region = ?")
                params.append(region.strip().upper())
            if mode_id is not None:
                updates.append("mode_id = ?")
                params.append(int(mode_id))
            if map_id is not None:
                updates.append("map_id = ?")
                params.append(int(map_id))
            if notes is not None:
                updates.append("notes = ?")
                params.append(notes)

            params.append(account_id)
            conn.execute(f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?;", params)
            conn.commit()
        finally:
            conn.close()

    acc = get_account_by_id(account_id)
    if acc:
        add_activity_log(
            account_id=account_id,
            account_label=f"{acc['display_name']} ({acc['uid']})",
            level="info",
            event_type="account_updated",
            message=f"Updated configuration for account '{acc['display_name']}' (Region: {acc['region']}).",
        )
    return acc


def update_account_runtime_state(
    account_id: str,
    *,
    status: Optional[str] = None,
    status_message: Optional[str] = None,
    game_account_id: Optional[str] = None,
    nickname: Optional[str] = None,
    region: Optional[str] = None,
    level: Optional[int] = None,
    exp: Optional[int] = None,
    inc_matches_searched: int = 0,
    inc_queue_no_join: int = 0,
    reset_queue_no_join: bool = False,
    udp_phase: Optional[str] = None,
    inc_matches_completed: int = 0,
    inc_udp_joins: int = 0,
    inc_udp_timeouts: int = 0,
    server_finish: bool = False,
    inc_reconnects: int = 0,
    inc_uptime_seconds: int = 0,
    start_session: bool = False,
) -> None:
    now = utc_now_iso()
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT initial_level, initial_exp, exp_baseline_set, level, exp, game_account_id FROM accounts WHERE id = ?;",
                (account_id,),
            ).fetchone()
            if not row:
                return

            updates = ["updated_at = ?", "last_activity = ?"]
            params: List[Any] = [now, now]

            if status is not None:
                updates.append("status = ?")
                params.append(status)
            if status_message is not None:
                updates.append("status_message = ?")
                params.append(redact_sensitive(status_message))
            if game_account_id is not None:
                updates.append("game_account_id = ?")
                params.append(str(game_account_id))
            if nickname is not None and nickname:
                updates.append("nickname = ?")
                params.append(str(nickname))
            if region is not None and region in lv.REGION_PREFIX:
                updates.append("region = ?")
                params.append(region)
            if level is not None and level > 0:
                updates.append("level = ?")
                params.append(int(level))
                if int(row["initial_level"] or 1) <= 1 and int(row["level"] or 1) <= 1:
                    updates.append("initial_level = ?")
                    params.append(int(level))
            if exp is not None and exp >= 0:
                updates.append("exp = ?")
                params.append(int(exp))
                if int(row["exp_baseline_set"] or 0) == 0 and (
                    game_account_id not in (None, "Pending", "0", "None", "")
                    or row["game_account_id"] not in (None, "Pending", "0", "None", "")
                ):
                    # Zero is a valid baseline; do not replace it on the first gain.
                    updates.extend(["initial_exp = ?", "exp_baseline_set = 1"])
                    params.append(int(exp))
            if start_session and exp is not None and exp >= 0:
                updates.extend(["session_start_exp = ?", "session_started_at = ?", "profile_check_status = 'never'", "profile_check_message = ''", "profile_last_checked_at = NULL", "session_last_exp_gain_at = NULL", "session_exp_gain_events = 0", "exp_stall_since = NULL", "exp_stall_checks = 0", "exp_stall_message = ''"])
                params.extend([int(exp), now])
            if inc_matches_searched > 0:
                updates.append("matches_searched = matches_searched + ?")
                params.append(int(inc_matches_searched))
            if reset_queue_no_join:
                updates.append("queue_no_join_streak = 0")
            elif inc_queue_no_join > 0:
                updates.append("queue_no_join_streak = queue_no_join_streak + ?")
                params.append(int(inc_queue_no_join))
            if udp_phase is not None:
                updates.extend(["udp_phase = ?", "udp_phase_at = ?"])
                params.extend([udp_phase, now])
                if udp_phase == "startup_sent":
                    updates.append("udp_startup_at = ?")
                    params.append(now)
            if inc_matches_completed > 0:
                updates.append("matches_completed = matches_completed + ?")
                params.append(int(inc_matches_completed))
            if inc_udp_joins > 0:
                updates.extend(["udp_join_count = udp_join_count + ?", "last_udp_join_at = ?"])
                params.extend([int(inc_udp_joins), now])
            if inc_udp_timeouts > 0:
                updates.extend(["udp_timeout_count = udp_timeout_count + ?", "last_udp_timeout_at = ?"])
                params.extend([int(inc_udp_timeouts), now])
            if server_finish:
                updates.extend(["last_server_finish_at = ?", "server_finish_count = server_finish_count + 1"])
                params.append(now)
            if inc_reconnects > 0:
                updates.append("reconnect_count = reconnect_count + ?")
                params.append(int(inc_reconnects))
            if inc_uptime_seconds > 0:
                updates.append("total_uptime_seconds = total_uptime_seconds + ?")
                params.append(int(inc_uptime_seconds))

            params.append(account_id)
            conn.execute(f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?;", params)
            conn.commit()
        finally:
            conn.close()


def set_queue_cooldown(account_id: str, until: Optional[str], *, increment: bool = False) -> None:
    """Persist a per-account offline deadline; restart-safe and never stores secrets."""
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute("UPDATE accounts SET cooldown_until = ?, "
                         "queue_cooldown_count = queue_cooldown_count + ? WHERE id = ?",
                         (until, 1 if increment else 0, account_id))
            conn.commit()
        finally:
            conn.close()


def delete_account(account_id: str) -> bool:
    with _DB_LOCK:
        conn = get_connection()
        try:
            row = conn.execute("SELECT display_name, uid FROM accounts WHERE id = ?;", (account_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM accounts WHERE id = ?;", (account_id,))
            conn.commit()
            add_activity_log(
                account_id=None,
                account_label=f"{row['display_name']} ({row['uid']})",
                level="warning",
                event_type="account_deleted",
                message=f"Removed account '{row['display_name']}' (UID {row['uid']}) from dashboard.",
            )
            return True
        finally:
            conn.close()


def record_account_error(account_id: str, stage: str, error_message: str) -> None:
    now = utc_now_iso()
    clean_msg = redact_sensitive(error_message)
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO error_history (account_id, stage, error_message, created_at) VALUES (?, ?, ?, ?);",
                (account_id, stage, clean_msg, now),
            )
            conn.commit()
        finally:
            conn.close()


def clear_account_errors(account_id: str) -> int:
    with _DB_LOCK:
        conn = get_connection()
        try:
            cur = conn.execute("DELETE FROM error_history WHERE account_id = ?;", (account_id,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def add_activity_log(
    account_id: Optional[str],
    account_label: str,
    level: str,
    event_type: str,
    message: str,
) -> Dict[str, Any]:
    now = utc_now_iso()
    clean_msg = redact_sensitive(message)
    with _DB_LOCK:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO activity_logs (account_id, account_label, level, event_type, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (account_id, account_label, level, event_type, clean_msg, now),
            )
            # Trim in batches; doing a full-table DELETE on every packet stalls workers.
            if cur.lastrowid % 100 == 0:
                conn.execute("DELETE FROM activity_logs WHERE id <= ?;", (cur.lastrowid - 10000,))
            conn.commit()
            return {
                "id": cur.lastrowid,
                "account_id": account_id,
                "account_label": account_label,
                "level": level,
                "event_type": event_type,
                "message": clean_msg,
                "created_at": now,
            }
        finally:
            conn.close()


def get_recent_activity(limit: int = 40) -> List[Dict[str, Any]]:
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, account_id, account_label, level, event_type, message, created_at FROM activity_logs ORDER BY id DESC LIMIT ?;",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_activity_page(limit: int = 150, before_id: Optional[int] = None,
                      level: Optional[str] = None, account_id: Optional[str] = None,
                      event_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest actual events with indexed keyset pagination (no growing OFFSET scans)."""
    clauses = []
    params: List[Any] = []
    if before_id is not None:
        clauses.append("id < ?")
        params.append(before_id)
    if level:
        clauses.append("level = ?")
        params.append(level)
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _DB_LOCK:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT id, account_id, account_label, level, event_type, message, created_at "
                "FROM activity_logs" + where + " ORDER BY id DESC LIMIT ?;",
                (*params, limit),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def record_profile_check(account_id: str, *, ok: bool, changed: bool = False,
                         message: str = "", exp_delta: int = 0) -> None:
    """Record verified per-session profile increases, not inferred UDP rewards."""
    now = utc_now_iso()
    state = "changed" if ok and changed else "unchanged" if ok else "error"
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute("""
                UPDATE accounts SET profile_last_checked_at = ?, profile_check_status = ?,
                    profile_check_message = ?,
                    profile_last_changed_at = CASE WHEN ? = 'changed' THEN ? ELSE profile_last_changed_at END,
                    session_last_exp_gain_at = CASE WHEN ? THEN ? ELSE session_last_exp_gain_at END,
                    session_exp_gain_events = session_exp_gain_events + CASE WHEN ? THEN 1 ELSE 0 END
                WHERE id = ?
            """, (now, state, redact_sensitive(message)[:300], state, now,
                  bool(ok and changed and exp_delta > 0), now,
                  bool(ok and changed and exp_delta > 0), account_id))
            conn.commit()
        finally:
            conn.close()


def set_exp_stall_state(account_id: str, *, since: Optional[str], checks: int = 0,
                        message: str = "") -> None:
    """Record (or clear) the 'running but the server profile EXP never moved' state."""
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute(
                """UPDATE accounts SET exp_stall_since = ?, exp_stall_checks = ?,
                       exp_stall_message = ? WHERE id = ?""",
                (since, int(checks), redact_sensitive(message or "")[:400], account_id),
            )
            conn.commit()
        finally:
            conn.close()


def clear_exp_stall(account_id: str) -> None:
    set_exp_stall_state(account_id, since=None, checks=0, message="")


def set_exp_stall_message(account_id: str, message: str) -> None:
    """Refresh the explanation text without touching the stall start time."""
    with _DB_LOCK:
        conn = get_connection()
        try:
            conn.execute("UPDATE accounts SET exp_stall_message = ? WHERE id = ?",
                         (redact_sensitive(message or "")[:400], account_id))
            conn.commit()
        finally:
            conn.close()


def get_or_create_admin_auth(initial_hash: str, initial_epoch: str,
                             fingerprint: Optional[str] = None) -> tuple[str, str]:
    """Sync an intentionally changed deployment secret; keep UI changes otherwise.

    NULL fingerprint marks pre-V13 installations. On their first V13 startup the
    configured private deployment secret takes precedence once, so an operator
    whose .env changed can sign in without deleting Bot data. Thereafter a
    Settings password change persists until the deployment secret itself changes.
    """
    with _DB_LOCK:
        conn = get_connection()
        try:
            with conn:
                conn.execute("INSERT OR IGNORE INTO admin_auth (id,password_hash,session_epoch,bootstrap_fingerprint) VALUES (1,?,?,?)",
                             (initial_hash, initial_epoch, fingerprint))
                row = conn.execute("SELECT password_hash,session_epoch,bootstrap_fingerprint FROM admin_auth WHERE id=1").fetchone()
                if fingerprint is not None and row["bootstrap_fingerprint"] != fingerprint:
                    conn.execute("UPDATE admin_auth SET password_hash=?,session_epoch=?,bootstrap_fingerprint=? WHERE id=1",
                                 (initial_hash, initial_epoch, fingerprint))
                    return initial_hash, initial_epoch
                return row["password_hash"], row["session_epoch"]
        finally:
            conn.close()


def save_admin_auth(password_hash: str, session_epoch: str) -> None:
    """Persist password change and session revocation atomically."""
    with _DB_LOCK:
        conn = get_connection()
        try:
            with conn:
                cursor = conn.execute("UPDATE admin_auth SET password_hash=?,session_epoch=? WHERE id=1",
                                      (password_hash, session_epoch))
                if cursor.rowcount != 1:
                    raise RuntimeError("Administrator auth storage is not initialized")
        finally:
            conn.close()
