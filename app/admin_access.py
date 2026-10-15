"""Offline server-operator credential diagnostic and explicit recovery.

Never prints passwords, hashes, session tokens, or connection secrets.
Use on the SAME installation/data volume as the running application.
"""

import argparse
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME, DATABASE_PATH, ENV_PATH, ADMIN_PASSWORD_ENV_OVERRIDE, admin_config_fingerprint

_ph = PasswordHasher()


def _password_values() -> list[str]:
    try:
        lines=ENV_PATH.read_text(encoding='utf-8').splitlines()
        return [line.lstrip().partition('=')[2].strip() for line in lines
                if line.lstrip().startswith('ADMIN_PASSWORD=')]
    except FileNotFoundError:
        return []  # e.g. hosting secrets manager rather than a local .env


def _password_lines() -> int:
    return len(_password_values())


def _conflicting_password_lines() -> bool:
    # A blank .env.example template row + one generated value is harmless.
    # Two nonempty rows are ambiguous and require an operator to review them.
    return sum(bool(value) for value in _password_values()) > 1


def _auth_row(conn: sqlite3.Connection):
    try:
        return conn.execute('SELECT password_hash,session_epoch FROM admin_auth WHERE id=1').fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc).lower():
            return None
        raise


def configured_password_matches_saved() -> bool | None:
    """True/False for an existing DB; None before the admin table is initialized."""
    path = Path(DATABASE_PATH).resolve()
    if not path.exists():
        return None
    from urllib.parse import quote
    uri = 'file:' + quote(str(path)) + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        row = _auth_row(conn)
        if not row:
            return None
        try:
            return _ph.verify(row[0], ADMIN_PASSWORD)
        except VerificationError:
            return False
    finally:
        conn.close()


def reset_from_config() -> Path | None:
    """Reset ONLY admin_auth with private configured password; preserve all Bot rows.

    Creates a private, SQLite-consistent DB backup first. Run while server is
    stopped and restart it afterwards; live workers retain their old in-memory hash.
    Returns backup path, or None if configured password already matches.
    """
    if _conflicting_password_lines():
        raise RuntimeError('Duplicate ADMIN_PASSWORD lines in .env. Remove duplicates before reset; never print the values.')
    path = Path(DATABASE_PATH).resolve()
    if not path.exists():
        raise RuntimeError('Database is not present at the configured DATABASE_PATH. Confirm the deployment volume and start the server once.')
    conn = sqlite3.connect(str(path), timeout=15)
    backup_path = None
    try:
        row = _auth_row(conn)
        if not row:
            raise RuntimeError('admin_auth row not found. Check the database path and run the server once before recovery.')
        try:
            if _ph.verify(row[0], ADMIN_PASSWORD):
                return None
        except VerificationError:
            pass
        # SQLite backup includes committed WAL data; 0600 from creation onward.
        suffix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+secrets.token_hex(4)
        backup_path=path.with_name(path.name+'.auth-backup-'+suffix+'.db')
        fd=os.open(str(backup_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            backup=sqlite3.connect(str(backup_path))
            try:
                conn.backup(backup)
            finally:
                backup.close()
            with conn:
                cursor=conn.execute('UPDATE admin_auth SET password_hash=?,session_epoch=?,bootstrap_fingerprint=? WHERE id=1',
                                    (_ph.hash(ADMIN_PASSWORD),secrets.token_hex(16),
                                     admin_config_fingerprint(ADMIN_USERNAME, ADMIN_PASSWORD)))
                if cursor.rowcount != 1:
                    raise RuntimeError('Admin row changed during reset. Stop the service and try again.')
            return backup_path
        except Exception:
            backup_path.unlink(missing_ok=True)
            raise
    finally:
        conn.close()


def main() -> int:
    parser=argparse.ArgumentParser(description='Private admin credential check/recovery. Never outputs passwords.')
    sub=parser.add_subparsers(dest='action',required=True)
    sub.add_parser('check',help='Check whether current configured password matches the saved admin password hash')
    reset=sub.add_parser('reset',help='Back up the DB and reset only admin_auth using the configured password')
    reset.add_argument('--confirm',action='store_true',help='Confirm server is stopped and private .env/hosting secret is ready')
    args=parser.parse_args()
    print('Administrator username configured:', ADMIN_USERNAME)
    print('Active password source:', 'hosting/process environment (overrides .env)' if ADMIN_PASSWORD_ENV_OVERRIDE else 'local .env or generated local secret')
    print('ADMIN_PASSWORD entries in local .env:', _password_lines())
    if args.action=='check':
        match=configured_password_matches_saved()
        if match is None:
            print('No initialized admin_auth record at this DATABASE_PATH. Check the deployment volume.')
        elif match:
            print('Configured admin password MATCHES the saved database hash.')
        else:
            print('Configured admin password DOES NOT MATCH the saved database hash. An earlier password change or wrong database may be the cause.')
        if _conflicting_password_lines():
            print('WARNING: duplicate ADMIN_PASSWORD entries. Resolve them before using reset.')
        return 0
    if not args.confirm:
        parser.error('Stop the server, back up .env and data, then pass --confirm to reset.')
    try:
        backup=reset_from_config()
    except (RuntimeError,sqlite3.Error,OSError) as exc:
        print('Reset not performed:',exc)
        return 1
    if backup is None:
        print('No reset needed: the configured password already matches the database.')
    else:
        print('Admin credential replaced; prior sessions revoked. Bot accounts/data unchanged.')
        print('Private SQLite backup created at:',backup)
        print('Restart the service and remove any one-time reset flag.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
