"""V18.2 safety rails: the hosting box stays light and the game servers are never hammered.

These tests pin the guarantees the operator was promised:
* profile reads always obey a floor, a ceiling, a concurrency cap and a per-account
  debounce for early checks;
* a failing game API makes the bot slower, never louder (exponential back off);
* the safety panel and /api/health read only in-memory counters — no extra requests.
"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import lv
from app import database as db, health as host_health
from app import bot_manager as bm
from app.config import ADMIN_PASSWORD, ADMIN_USERNAME
from app.main import app


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'safety.db'))
    db.init_db()
    return db


def _client():
    return TestClient(app)


# --------------------------------------------------------------------------------------
# 1. game API traffic: floor, ceiling, back off, debounce
# --------------------------------------------------------------------------------------
def test_poll_delay_never_goes_below_the_floor():
    assert lv.PROFILE_POLL_SECONDS >= 15
    assert lv.profile_poll_delay(0, base=5) == 15.0
    assert lv.profile_poll_delay(0, base=45) == 45.0


def test_poll_delay_backs_off_after_repeated_failures():
    base = 45.0
    assert lv.profile_poll_delay(lv.GAME_API_BACKOFF_THRESHOLD - 1, base=base) == base
    doubled = lv.profile_poll_delay(lv.GAME_API_BACKOFF_THRESHOLD, base=base)
    assert doubled > base
    louder = lv.profile_poll_delay(lv.GAME_API_BACKOFF_THRESHOLD * 3, base=base)
    assert louder >= doubled
    # and it always stops at the ceiling — retries can never pile up
    assert lv.profile_poll_delay(999, base=base) == float(lv.PROFILE_POLL_MAX_SECONDS)
    assert lv.PROFILE_POLL_MAX_SECONDS <= 3600


def test_forced_check_debounce_blocks_bursts():
    now = 1000.0
    interval = lv.PROFILE_FORCED_MIN_INTERVAL_SECONDS
    assert lv.forced_refresh_wait(None, now) == 0.0
    assert lv.forced_refresh_wait(now - interval - 5, now) == 0.0
    assert lv.forced_refresh_wait(now - 1, now) == pytest.approx(interval - 1, abs=0.01)
    assert lv.forced_refresh_wait(now, now) == pytest.approx(interval, abs=0.01)
    # a burst of join/end signals can never produce more than one call per window
    assert lv.forced_refresh_wait(now - 0.1, now) > 0


def test_concurrency_cap_is_small_and_configurable():
    assert 1 <= lv.PROFILE_CHECK_CONCURRENCY <= 20
    assert isinstance(lv._PROFILE_SEMAPHORE, asyncio.Semaphore)


def _run_udp_session(monkeypatch, account_data):
    """Start one play_game session and return every UDP byte string it sent."""
    sends = []

    class FakeSocket:
        def bind(self, addr): pass
        def setblocking(self, value): pass
        def close(self): pass

    real_socket = lv.socket.socket
    monkeypatch.setattr(lv.socket, 'socket', lambda *a, **k:
                        FakeSocket() if len(a) > 1 and a[1] == lv.socket.SOCK_DGRAM else real_socket(*a, **k))
    monkeypatch.setattr(lv, 'optimize_udp_socket', lambda sock: None)

    async def resolve(host): return '127.0.0.1'
    async def hello(*a): return 'cc'
    async def packet(*a, **k): return b'ack'
    async def ping(*a): await asyncio.Event().wait()

    async def send(loop, sock, data, addr):
        sends.append(data)
        if data == b'\xbb' and sends.count(b'\xbb') == 1:
            raise OSError('temporary SHARMA send failure')
        return len(data)

    received = 0

    async def recv(*a):
        nonlocal received
        received += 1
        if received == 1:
            return b'reply', ('127.0.0.1', 7777)
        await asyncio.Event().wait()

    async def decode(*a): return {'cmd': 2, 'content': b'\x01\x00'}
    async def classify(*a): return 'ACK'
    async def reply(*a, **k): return ('ACK', b'ack')
    for name, value in [('resolve_host_cloudflare', resolve), ('build_hello_packet', hello),
                        ('build_packet', packet), ('keepalive_ping', ping),
                        ('_udp_sendto', send), ('_udp_recvfrom', recv),
                        ('decode_packet', decode), ('classify', classify), ('reply_for', reply)]:
        monkeypatch.setattr(lv, name, value)

    ready = asyncio.Event()

    def capture(message):
        if 'UDP startup packets sent;' in message:
            ready.set()
    lv.register_log_callback(capture)

    async def run():
        worker = asyncio.create_task(lv.play_game('127.0.0.1:7777', 'aa', 'bb', '00' * 16, '12340',
                                                 '777', 'BD', 'OB55', b'0' * 16, b'1' * 16,
                                                 match_index=1, account_data=account_data))
        try:
            await asyncio.wait_for(ready.wait(), timeout=6)
            await asyncio.sleep(0.4)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    try:
        asyncio.run(run())
    finally:
        lv.unregister_log_callback(capture)
    return sends


def test_profile_refresh_hook_adds_zero_packets_to_a_match(monkeypatch):
    """The match traffic must be byte-for-byte identical whether or not the hook is present."""
    plain = _run_udp_session(monkeypatch, None)
    hooked = _run_udp_session(monkeypatch, {'_profile_refresh_event': asyncio.Event()})
    assert plain and hooked
    assert plain == hooked, "the profile hook must not change what is sent to the game server"
    assert plain.count(b'\xbb') == 2


# --------------------------------------------------------------------------------------
# 2. counters reported to the panel come from memory only
# --------------------------------------------------------------------------------------
def test_safety_payload_reports_rates_and_limits(isolated_db):
    manager = bm.BotManager()
    for ok in (True, True, False):
        manager._note_profile_result(ok=ok, forced=False, fail_streak=0)
    manager._note_profile_result(ok=True, forced=True, fail_streak=0)
    acc = db.create_account('guest-safe', 'password-123', display_name='Safe Bot', region='BD')
    manager.workers[acc['id']] = None  # type: ignore[assignment]
    manager._profile_fail_streaks[acc['id']] = lv.GAME_API_BACKOFF_THRESHOLD

    payload = manager.safety_payload()
    assert payload['profile_checks_per_minute'] == 4
    assert payload['profile_failures_per_minute'] == 1
    assert payload['profile_checks_total'] == 4 and payload['profile_failures_total'] == 1
    assert payload['profile_forced_total'] == 1
    assert payload['throttled_accounts'] == 1
    assert payload['game_api_state'] == 'throttled'
    assert payload['poll_floor_seconds'] >= 15
    assert payload['check_concurrency'] >= 1
    assert 'password-123' not in json.dumps(payload)


def test_counters_do_not_grow_without_bound(isolated_db):
    manager = bm.BotManager()
    for _ in range(1500):
        manager._note_profile_result(ok=True)
    assert len(manager._profile_check_times) == manager._profile_check_times.maxlen
    assert manager._profile_checks_total == 1500


def test_stopped_accounts_are_dropped_from_the_memory_maps(isolated_db):
    manager = bm.BotManager()
    acc = db.create_account('guest-clean', 'password-123', display_name='Clean Bot', region='BD')
    manager._exp_stall_logged_at[acc['id']] = 1.0
    manager._profile_error_logged_at[acc['id']] = 1.0
    manager._profile_fail_streaks[acc['id']] = 3
    manager._manual_profile_checked_at[acc['id']] = 1.0

    asyncio.run(manager.stop_account(acc['id'], "test stop", broadcast=False))

    assert acc['id'] not in manager._exp_stall_logged_at
    assert acc['id'] not in manager._profile_error_logged_at
    assert acc['id'] not in manager._profile_fail_streaks
    assert acc['id'] not in manager._manual_profile_checked_at


# --------------------------------------------------------------------------------------
# 3. host side: footprint is measured, bounded and secret-free
# --------------------------------------------------------------------------------------
def test_host_health_is_readable_and_safe():
    payload = host_health.collect_host_health(database_path=db.DATABASE_PATH, loop_lag_ms=12.5)
    for key in ('process_uptime_seconds', 'memory_mb', 'load_average', 'load_per_core', 'pressure',
                'loop_lag_ms', 'database_mb', 'data_dir_mb'):
        assert key in payload
    assert payload['pressure'] in ('low', 'moderate', 'high')
    assert payload['loop_lag_ms'] == 12.5
    assert host_health.pressure_note(payload)
    assert host_health.pressure_note({'pressure': 'high'}) != host_health.pressure_note({'pressure': 'low'})


def test_api_health_requires_admin_and_covers_both_sides(isolated_db):
    with _client() as client:
        assert client.get('/api/health').status_code == 401
        login = client.post('/api/auth/login', json={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD})
        assert login.status_code == 200
        res = client.get('/api/health')
        assert res.status_code == 200
        body = res.json()
        assert body['status'] == 'ok'
        assert body['host']['pressure'] in ('low', 'moderate', 'high')
        assert body['game_api']['poll_floor_seconds'] >= 15
        assert body['game_api']['game_api_state'] in ('normal', 'throttled')
        # the cheap liveness probe stays open for the hosting platform
        assert client.get('/health').status_code == 200
        overview = client.get('/api/dashboard/overview').json()
    assert overview['summary']['safety']['check_concurrency'] >= 1


def test_public_board_hides_the_safety_internals(isolated_db):
    db.create_account('guest-pub', 'password-123', display_name='Public Bot', region='BD')
    with _client() as client:
        public = json.dumps(client.get('/api/public/overview').json())
    for field in ('safety', 'poll_floor_seconds', 'check_concurrency', 'profile_checks_per_minute'):
        assert field not in public


def test_safety_panel_is_wired_without_a_new_timer():
    root = Path(__file__).resolve().parents[1]
    js = (root / 'static/js/app.js').read_text()
    html = (root / 'static/index.html').read_text()
    assert 'renderSafety' in js and 'safety-grid' in html and 'safety-grid' in js
    assert "api('/api/health')" in js
    assert 'hostHealthAt' in js, "host readings must be throttled to one per minute"
    # no new timer was introduced for the panel: it renders from the existing sync
    safety_block = js.split('function renderSafety')[1].split('function renderDailyWatch')[0]
    assert 'setInterval' not in safety_block and 'setTimeout' not in safety_block
    health_block = js.split('async function loadHostHealth')[1].split('function renderDailyWatch')[0]
    assert 'setInterval' not in health_block and 'setTimeout' not in health_block
