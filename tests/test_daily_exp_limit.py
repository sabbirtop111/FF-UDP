"""V18 daily EXP target tests: level rules, day boundaries, auto-offline and admin resets.

No game-server traffic is involved: only the database helpers, the manager's daily
tracker and the HTTP endpoints are exercised.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, database as db
from app import bot_manager as bm
from app.config import ADMIN_PASSWORD, ADMIN_USERNAME
from app.main import app


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'daily.db'))
    db.init_db()
    return db


def _client():
    return TestClient(app)


# --------------------------------------------------------------------------------------
# 1. level -> daily cap rule
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("level,expected", [
    (1, 5000), (5, 5000), (28, 5000), (29, 5000),
    (30, 50000), (55, 50000), (100, 50000), (250, 50000),
])
def test_daily_limit_follows_the_saved_level(level, expected):
    assert config.daily_limit_for_level(level) == expected


def test_daily_limit_defaults_safely_for_missing_level():
    assert config.daily_limit_for_level(None) == 5000
    assert config.daily_limit_for_level("") == 5000
    assert config.daily_limit_for_level(-4) == 5000


def test_rule_payload_is_public_safe():
    payload = config.daily_rule_payload()
    assert payload["enabled"] is True
    assert payload["low_limit"] == 5000 and payload["high_limit"] == 50000
    assert payload["low_level_max"] == 29
    assert "5,000 EXP/day" in payload["summary"] and "50,000 EXP/day" in payload["summary"]
    assert "password" not in json.dumps(payload).lower()


# --------------------------------------------------------------------------------------
# 2. day key / reset boundary
# --------------------------------------------------------------------------------------
def test_day_key_uses_the_configured_offset():
    offset = config.DAILY_EXP_RESET_OFFSET_HOURS
    # 23:30 UTC is already the next day when the offset is positive (e.g. UTC+6).
    late = datetime(2026, 3, 9, 23, 30, tzinfo=timezone.utc)
    shifted = late + timedelta(hours=offset)
    assert db.daily_day_key(late) == shifted.strftime('%Y-%m-%d')


def test_reset_timestamp_is_the_next_local_midnight_in_utc():
    day = db.daily_day_key()
    reset = db.daily_reset_at(day)
    reset_dt = datetime.fromisoformat(reset.replace('Z', '+00:00'))
    local_midnight = datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1)
    expected_utc = (local_midnight - timedelta(hours=config.DAILY_EXP_RESET_OFFSET_HOURS)).replace(tzinfo=timezone.utc)
    assert reset_dt == expected_utc
    assert reset_dt > datetime.now(timezone.utc)
    assert (reset_dt - datetime.now(timezone.utc)) <= timedelta(days=1)


# --------------------------------------------------------------------------------------
# 3. counters
# --------------------------------------------------------------------------------------
def test_counter_accumulates_and_marks_target(isolated_db):
    acc = db.create_account('guest-low', 'password-123', display_name='Low Bot', region='BD')
    row = db.add_daily_exp(acc['id'], 3200)
    assert row['exp_gained'] == 3200
    row = db.add_daily_exp(acc['id'], 1800)
    assert row['exp_gained'] == 5000
    info = db.with_daily_fields(db.get_account_by_id(acc['id']))
    assert info['daily_exp_reached'] is False or info['daily_exp_gained'] == 5000

    db.mark_daily_limit_reached(acc['id'], total=5000)
    assert db.get_daily_exp(acc['id'])['reached_at'] is not None
    db.mark_daily_auto_stopped(acc['id'])
    assert db.get_daily_exp(acc['id'])['auto_stopped'] == 1

    account = db.get_account_by_id(acc['id'])
    assert account['daily_limit'] == 5000
    assert account['daily_exp_gained'] == 5000
    assert account['daily_exp_remaining'] == 0
    assert account['daily_exp_reached'] is True
    assert account['daily_auto_stopped'] is True
    assert account['daily_exp_percent'] == 100
    assert account['daily_reset_at'] and account['daily_day'] == db.daily_day_key()


def test_limits_differ_between_low_and_high_level_accounts(isolated_db):
    low = db.create_account('guest-low-2', 'password-123', display_name='Low', region='BD')
    high = db.create_account('guest-high', 'password-123', display_name='High', region='BD')
    db.update_account_runtime_state(low['id'], level=12, exp=900)
    db.update_account_runtime_state(high['id'], level=64, exp=900000)
    db.add_daily_exp(low['id'], 5000)
    db.add_daily_exp(high['id'], 5000)
    low_row = db.get_account_by_id(low['id'])
    high_row = db.get_account_by_id(high['id'])
    assert low_row['daily_limit'] == 5000 and low_row['daily_exp_reached'] is True
    assert high_row['daily_limit'] == 50000 and high_row['daily_exp_reached'] is False
    assert high_row['daily_exp_remaining'] == 45000


def test_reset_clears_only_today_and_total_helpers(isolated_db):
    acc = db.create_account('guest-reset', 'password-123', display_name='Reset Bot', region='BD')
    db.add_daily_exp(acc['id'], 5000)
    db.mark_daily_limit_reached(acc['id'], total=5000)
    totals = db.daily_exp_totals()
    assert totals['total_exp'] == 5000 and totals['reached'] == 1
    assert db.reset_daily_exp(acc['id']) == 1
    assert db.get_daily_exp(acc['id'])['exp_gained'] == 0
    assert db.get_account_by_id(acc['id'])['daily_exp_reached'] is False
    assert db.reset_all_daily_exp() == 0
    assert db.reset_daily_exp(acc['id']) == 0


# --------------------------------------------------------------------------------------
# 4. manager tracker + automatic offline
# --------------------------------------------------------------------------------------
def test_tracker_records_gain_and_marks_the_target(isolated_db):
    manager = bm.BotManager()
    acc = db.create_account('guest-track', 'password-123', display_name='Track Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=7, exp=1000)
    manager._track_daily_exp(acc['id'], 2600, 7, 3600)
    status = manager.daily_status_for_account(acc['id'], 7)
    assert status['exp_gained'] == 2600 and status['reached'] is False
    assert db.get_daily_exp(acc['id'])['reached_at'] is None


def test_auto_offline_scheduled_when_target_is_reached(isolated_db):
    manager = bm.BotManager()
    acc = db.create_account('guest-stop', 'password-123', display_name='Stop Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=3, exp=1000, status='searching_match')
    stopped = {}

    async def fake_stop(account_id, reason="", broadcast=True, cancel_cooldown=True):
        stopped['id'] = account_id
        stopped['reason'] = reason
        db.update_account_runtime_state(account_id, status='inactive', status_message=reason)
        return {"ok": True, "worker_stopped": True, "message": "stopped"}

    async def scenario():
        manager.stop_account = fake_stop  # type: ignore[assignment]
        manager._track_daily_exp(acc['id'], 4990, 3, 5990)
        assert stopped == {}, "the target is not reached yet"
        manager._track_daily_exp(acc['id'], 10, 3, 6000)
        for _ in range(6):
            await asyncio.sleep(0)
            if stopped:
                break
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert stopped.get('id') == acc['id']
    assert 'daily EXP target reached' in stopped['reason']
    row = db.get_daily_exp(acc['id'])
    assert row['exp_gained'] == 5000
    assert row['reached_at'] is not None
    assert row['auto_stopped'] == 1
    events = [e['event_type'] for e in db.get_activity_page(limit=50)['events']] if isinstance(db.get_activity_page(limit=50), dict) else [e['event_type'] for e in db.get_activity_page(limit=50)]
    assert 'daily_limit_reached' in events
    assert 'daily_auto_offline' in events
    assert db.get_account_by_id(acc['id'])['status'] == 'inactive'
    assert acc['id'] not in manager._daily_stop_inflight


def test_counter_never_exceeds_the_daily_target(isolated_db):
    manager = bm.BotManager()
    acc = db.create_account('guest-clamp', 'password-123', display_name='Clamp Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=16, exp=50000)
    manager._track_daily_exp(acc['id'], 4900, 16, 54900)
    manager._track_daily_exp(acc['id'], 870, 16, 55770)   # crosses the target
    assert db.get_daily_exp(acc['id'])['exp_gained'] == 5000
    manager._track_daily_exp(acc['id'], 250, 16, 55800)   # earlier day would have allowed it
    manager._track_daily_exp(acc['id'], 9999, 16, 55900)
    assert db.get_daily_exp(acc['id'])['exp_gained'] == 5000
    assert db.get_account_by_id(acc['id'])['daily_exp_remaining'] == 0


def test_high_level_account_is_not_stopped_at_five_thousand(isolated_db):
    manager = bm.BotManager()
    acc = db.create_account('guest-high-2', 'password-123', display_name='High Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=61, exp=900000)
    manager._track_daily_exp(acc['id'], 5000, 61, 905000)
    status = manager.daily_status_for_account(acc['id'], 61)
    assert status['exp_gained'] == 5000
    assert status['limit'] == 50000
    assert status['reached'] is False
    assert db.get_daily_exp(acc['id'])['auto_stopped'] == 0


# --------------------------------------------------------------------------------------
# 5. HTTP surface
# --------------------------------------------------------------------------------------
def test_start_is_blocked_once_the_daily_target_is_reached(isolated_db):
    acc = db.create_account('guest-block', 'password-123', display_name='Block Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=8, exp=2000, status='inactive')
    db.add_daily_exp(acc['id'], 5000)
    db.mark_daily_limit_reached(acc['id'], total=5000)
    db.mark_daily_auto_stopped(acc['id'])

    with _client() as client:
        login = client.post('/api/auth/login', json={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD})
        assert login.status_code == 200
        resp = client.post(f"/api/accounts/{acc['id']}/start")
        assert resp.status_code == 400
        assert 'Daily EXP target already reached' in resp.json()['detail']


def test_reset_endpoint_and_bulk_action_clear_the_counter(isolated_db):
    acc = db.create_account('guest-api', 'password-123', display_name='Api Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=31, exp=500000, status='inactive')
    db.add_daily_exp(acc['id'], 50000)
    db.mark_daily_limit_reached(acc['id'], total=50000)
    db.mark_daily_auto_stopped(acc['id'])

    with _client() as client:
        login = client.post('/api/auth/login', json={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD})
        assert login.status_code == 200
        overview = client.get('/api/dashboard/overview')
        assert overview.status_code == 200
        summary = overview.json()['summary']
        assert summary['daily_rules']['low_limit'] == 5000
        assert summary['daily_totals']['accounts_at_target'] >= 1
        account_row = next(a for a in overview.json()['accounts'] if a['id'] == acc['id'])
        assert account_row['daily_limit'] == 50000
        assert account_row['daily_exp_reached'] is True

        daily = client.get('/api/daily/summary')
        assert daily.status_code == 200
        body = daily.json()
        assert body['rules']['high_limit'] == 50000
        assert any(row['id'] == acc['id'] and row['daily_exp_reached'] for row in body['accounts'])

        reset = client.post(f"/api/accounts/{acc['id']}/reset-daily")
        assert reset.status_code == 200
        assert reset.json()['account']['daily_exp_gained'] == 0
        assert reset.json()['account']['daily_exp_reached'] is False

        db.add_daily_exp(acc['id'], 50000)
        bulk = client.post('/api/accounts/bulk-action',
                           json={'account_ids': [acc['id']], 'action': 'reset_daily'})
        assert bulk.status_code == 200
        assert bulk.json()['results'][0]['status'] == 'daily_reset'
        assert db.get_daily_exp(acc['id'])['exp_gained'] == 0


def test_bulk_action_rejects_unknown_actions(isolated_db):
    acc = db.create_account('guest-bad', 'password-123', display_name='Bad Bot', region='BD')
    with _client() as client:
        login = client.post('/api/auth/login', json={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD})
        assert login.status_code == 200
        resp = client.post('/api/accounts/bulk-action', json={'account_ids': [acc['id']], 'action': 'nuke'})
        assert resp.status_code == 422


# --------------------------------------------------------------------------------------
# 6. public board stays read-only and secret-free while showing today's gain
# --------------------------------------------------------------------------------------
def test_public_card_exposes_only_safe_daily_numbers(isolated_db):
    acc = db.create_account('guest-public', 'top-secret-password', display_name='Public Bot',
                            region='BD', notes='INTERNAL-ONLY-NOTE')
    db.update_account_runtime_state(acc['id'], status='inactive', game_account_id='555111', level=44, exp=700000)
    db.add_daily_exp(acc['id'], 12000)

    with _client() as client:
        body = client.get('/api/public/overview').json()
    card = body['bots'][0]
    assert card['daily_exp_gained'] == 12000
    assert card['daily_exp_limit'] == 50000
    assert card['daily_exp_reached'] is False
    assert card['daily_auto_stopped'] is False
    assert card['daily_reset_at']
    rendered = json.dumps(card)
    for secret in ('guest-public', 'top-secret-password', 'INTERNAL-ONLY-NOTE', 'encrypted_password', 'uid'):
        assert secret not in rendered


def test_public_page_has_no_daily_admin_controls():
    root = Path(__file__).resolve().parents[1]
    html = (root / 'static/public.html').read_text()
    js = (root / 'static/js/public.js').read_text()
    assert 'reset-daily' not in html and 'reset-daily' not in js
    assert '/api/accounts/' not in js
    assert 'daily_exp' in js


# --------------------------------------------------------------------------------------
# 7. "No EXP from the server" diagnosis (running account, profile never moves)
# --------------------------------------------------------------------------------------
def _age_session(account_id, hours):
    from app.database import get_connection
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    conn = get_connection()
    try:
        conn.execute("UPDATE accounts SET session_started_at = ? WHERE id = ?", (old, account_id))
        conn.commit()
    finally:
        conn.close()


def test_stall_is_recorded_when_the_server_never_moves_exp(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'EXP_STALL_MINUTES', 5)
    monkeypatch.setattr(bm, 'EXP_STALL_RELOG_MINUTES', 5)
    manager = bm.BotManager()
    acc = db.create_account('guest-stall', 'password-123', display_name='Stall Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=20, exp=5000, status='online', start_session=True)
    _age_session(acc['id'], 5)
    manager.account_labels[acc['id']] = 'Stall Bot'

    for _ in range(3):
        manager._on_profile_result(acc['id'], {'ok': True, 'changed': False, 'level': 20,
                                               'exp': 5000, 'reason': 'Server profile unchanged'})

    row = db.get_account_by_id(acc['id'])
    assert row['exp_stalled'] is True
    assert row['exp_stall_since']
    assert row['exp_stall_checks'] == 3
    assert 'No verified EXP increase' in row['exp_stall_message']
    assert '5,000' in row['exp_stall_message']
    events = [e['event_type'] for e in db.get_activity_page(limit=30)]
    assert 'exp_stall' in events
    # the diagnosis must stay honest: no invented reward, no password material
    assert 'password-123' not in row['exp_stall_message']


def test_stall_clears_and_reports_when_the_server_credits_exp_again(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'EXP_STALL_MINUTES', 5)
    manager = bm.BotManager()
    acc = db.create_account('guest-resume', 'password-123', display_name='Resume Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=33, exp=900000, status='searching_match', start_session=True)
    _age_session(acc['id'], 6)
    manager._on_profile_result(acc['id'], {'ok': True, 'changed': False, 'level': 33,
                                           'exp': 900000, 'reason': 'Server profile unchanged'})
    assert db.get_account_by_id(acc['id'])['exp_stalled'] is True

    manager._on_profile_result(acc['id'], {'ok': True, 'changed': True, 'level': 33,
                                           'exp': 900420, 'exp_delta': 420, 'reason': 'Profile changed'})
    row = db.get_account_by_id(acc['id'])
    assert row['exp_stalled'] is False
    assert row['exp_stall_message'] == ''
    events = [e['event_type'] for e in db.get_activity_page(limit=30)]
    assert 'exp_gain_resumed' in events


def test_first_stall_warning_is_logged_even_right_after_boot(isolated_db, monkeypatch):
    """A freshly booted host reports a small monotonic() value — the first warning must still log."""
    monkeypatch.setattr(bm, 'EXP_STALL_MINUTES', 5)
    monkeypatch.setattr(bm, 'EXP_STALL_RELOG_MINUTES', 60)
    monkeypatch.setattr(bm.time, 'monotonic', lambda: 42.0)
    manager = bm.BotManager()
    acc = db.create_account('guest-boot', 'password-123', display_name='Boot Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=21, exp=3300, status='online', start_session=True)
    _age_session(acc['id'], 4)
    manager._on_profile_result(acc['id'], {'ok': True, 'changed': False, 'level': 21, 'exp': 3300})
    events = [e['event_type'] for e in db.get_activity_page(limit=20)]
    assert 'exp_stall' in events, "the first warning must not depend on the host uptime"
    # ...and the relog rate limit still applies afterwards
    manager._on_profile_result(acc['id'], {'ok': True, 'changed': False, 'level': 21, 'exp': 3300})
    assert [e['event_type'] for e in db.get_activity_page(limit=20)].count('exp_stall') == 1


def test_stall_is_not_raised_for_stopped_accounts_or_failing_checks(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'EXP_STALL_MINUTES', 5)
    manager = bm.BotManager()
    idle = db.create_account('guest-idle', 'password-123', display_name='Idle Bot', region='BD')
    db.update_account_runtime_state(idle['id'], level=9, exp=100, status='inactive', start_session=True)
    blind = db.create_account('guest-blind', 'password-123', display_name='Blind Bot', region='BD')
    db.update_account_runtime_state(blind['id'], level=9, exp=100, status='online', start_session=True)
    _age_session(idle['id'], 8); _age_session(blind['id'], 8)

    manager._on_profile_result(idle['id'], {'ok': True, 'changed': False, 'level': 9, 'exp': 100})
    manager._on_profile_result(blind['id'], {'ok': False, 'changed': False, 'reason': 'GetLoginData returned no valid response'})

    assert db.get_account_by_id(idle['id'])['exp_stalled'] is False
    assert db.get_account_by_id(blind['id'])['exp_stalled'] is False, "failing profile checks are a different problem"
    assert 'exp_stall' not in [e['event_type'] for e in db.get_activity_page(limit=30)]


def test_fresh_session_is_never_flagged_as_stalled(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'EXP_STALL_MINUTES', 30)
    manager = bm.BotManager()
    acc = db.create_account('guest-fresh', 'password-123', display_name='Fresh Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=44, exp=12000, status='online', start_session=True)
    manager._on_profile_result(acc['id'], {'ok': True, 'changed': False, 'level': 44, 'exp': 12000})
    assert db.get_account_by_id(acc['id'])['exp_stalled'] is False


def test_stall_fields_reach_the_dashboard_and_public_allowlist(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'EXP_STALL_MINUTES', 5)
    manager = bm.BotManager()
    acc = db.create_account('guest-shape', 'password-123', display_name='Shape Bot', region='BD')
    db.update_account_runtime_state(acc['id'], level=18, exp=4400, status='in_match', start_session=True,
                                    game_account_id='661199')
    _age_session(acc['id'], 7)
    manager._on_profile_result(acc['id'], {'ok': True, 'changed': False, 'level': 18, 'exp': 4400})

    with _client() as client:
        login = client.post('/api/auth/login', json={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD})
        assert login.status_code == 200
        row = next(a for a in client.get('/api/dashboard/overview').json()['accounts'] if a['id'] == acc['id'])
        assert row['exp_stalled'] is True
        assert row['exp_stall_checks'] >= 1
        assert 'No verified EXP increase' in row['exp_stall_message']
        public = client.get('/api/public/overview').json()['bots'][0]
    assert 'password-123' not in json.dumps(public)
