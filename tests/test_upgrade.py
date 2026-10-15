"""Offline checks: no Garena credentials, sockets or game-success simulation."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import lv
from app import database as db
from app import bot_manager as bm
from app.auth import create_session_token
from app.main import app


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'test.db'))
    monkeypatch.setattr(lv, 'get_device_for_account', lambda uid: {})
    db.init_db()
    return db


def test_twelve_managed_workers_and_capacity(isolated_db, monkeypatch):
    manager = bm.BotManager()
    stop = asyncio.Event()

    async def idle_worker(account_id):
        await stop.wait()

    monkeypatch.setattr(manager, '_account_worker_lifecycle', idle_worker)
    monkeypatch.setattr(bm, 'MAX_ACTIVE_WORKERS', 12)
    accounts = [db.create_account(f'guest{i:04}', 'testpass') for i in range(13)]

    async def run():
        for acc in accounts[:12]:
            await manager.start_account(acc['id'], broadcast=False)
        await asyncio.sleep(0)
        assert manager.get_system_status()['active_workers'] == 12
        with pytest.raises(ValueError, match='capacity'):
            await manager.start_account(accounts[12]['id'], broadcast=False)
        assert db.get_account_by_id(accounts[12]['id'])['status'] == 'inactive'
        stop.set()
        await asyncio.gather(*manager.workers.values(), return_exceptions=True)
        for acc in accounts[:12]:
            await manager.stop_account(acc['id'])

    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_real_logs_isolated_redacted_and_honest_status(isolated_db):
    manager = bm.BotManager()
    a = db.create_account('12345', 'secretpass', display_name='Alpha')
    b = db.create_account('123456', 'secretpass', display_name='Beta')
    manager.uid_to_account_id.update({'12345': a['id'], '123456': b['id']})
    manager.account_labels.update({a['id']: 'Alpha', b['id']: 'Beta'})
    # A matched candidate alone must not be reported as an active UDP session.
    manager._on_lv_log('[+] Match found [BR] | UID: 12345 | size=600')
    assert db.get_account_by_id(a['id'])['status'] == 'searching_match'
    assert db.get_account_by_id(b['id'])['status'] == 'inactive'
    manager._on_lv_log('[MATCH #1] UDP startup packets sent; awaiting server confirmation | UID: 12345')
    assert db.get_account_by_id(a['id'])['status'] == 'searching_match'
    manager._on_lv_log('[MATCH #1] UDP JOIN_MATCH received from server | UID: 12345')
    assert db.get_account_by_id(a['id'])['status'] == 'in_match'
    manager._on_lv_log('[-] Failed for UID: 123456 password=secretpass')
    events = [e for e in db.get_activity_page(account_id=b['id']) if e['event_type'] == 'worker_output']
    assert len(events) == 1 and events[0]['level'] == 'error'
    assert 'secretpass' not in events[0]['message']
    assert '[REDACTED]' in events[0]['message']
    manager._on_lv_log('[DEBUG] UID 12345 parsed: token=YES')
    assert len([e for e in db.get_activity_page(account_id=a['id']) if e['event_type'] in ('worker_output', 'udp_join')]) == 2
    assert len(db.get_activity_page(account_id=a['id'], event_type='udp_phase')) == 1
    assert len(db.get_activity_page(account_id=a['id'], event_type='udp_join')) == 1
    assert db.get_account_by_id(a['id'])['udp_join_count'] == 1
    lv.unregister_log_callback(manager._on_lv_log)


def test_startup_timeout_deadline_is_not_an_actual_udp_timeout(isolated_db):
    manager = bm.BotManager()
    account = db.create_account('deadline_guest', 'testpass')
    manager.uid_to_account_id['deadline_guest'] = account['id']

    # This message announces a 75s deadline; it is not a timeout until the
    # separate expiry log is emitted. A JOIN may still arrive afterward.
    manager._on_lv_log(
        '[MATCH #6] UDP startup packets sent; awaiting JOIN_MATCH (timeout 75s) | UID: deadline_guest'
    )
    events = db.get_activity_page(account_id=account['id'])
    assert events[0]['event_type'] == 'udp_phase'
    assert not db.get_activity_page(account_id=account['id'], event_type='udp_timeout')
    assert db.get_account_by_id(account['id'])['udp_timeout_count'] == 0

    manager._on_lv_log(
        '[MATCH #6] UDP JOIN_MATCH received from server | UID: deadline_guest'
    )
    assert db.get_account_by_id(account['id'])['udp_join_count'] == 1
    assert not db.get_activity_page(account_id=account['id'], event_type='udp_timeout')

    manager._on_lv_log(
        '[!] Match #7 UDP JOIN_MATCH timeout (75s after startup; no confirmation) | UID: deadline_guest'
    )
    assert len(db.get_activity_page(account_id=account['id'], event_type='udp_timeout')) == 1
    assert db.get_account_by_id(account['id'])['udp_timeout_count'] == 1
    lv.unregister_log_callback(manager._on_lv_log)


def test_authenticated_paginated_log_api(isolated_db):
    for i in range(4):
        db.add_activity_log(None, 'Test', 'info', 'worker_output', f'event {i}')
    with TestClient(app) as client:
        assert client.get('/api/activity').status_code == 401
        token = create_session_token('admin')
        page = client.get('/api/activity?limit=2', headers={'Authorization': f'Bearer {token}'})
        assert page.status_code == 200
        body = page.json()
        assert len(body['events']) == 2 and body['next_before_id']
        older = client.get(f"/api/activity?limit=2&before_id={body['next_before_id']}", headers={'Authorization': f'Bearer {token}'})
        assert older.status_code == 200
        assert older.json()['events'][0]['id'] < body['events'][-1]['id']
        assert client.get('/api/activity?level=invalid', headers={'Authorization': f'Bearer {token}'}).status_code == 422


def test_auth_retry_releases_login_slot(isolated_db, monkeypatch):
    """A failed guest login must not block all other IDs for the 15-second retry."""
    manager = bm.BotManager()
    acc = db.create_account('guest_42', 'testpass')
    backoff_started = asyncio.Event()
    hold = asyncio.Event()
    original_sleep = asyncio.sleep

    async def version_config(region):
        return 'OB55', '1.132.8', 'https://example.invalid/'

    async def fail_oauth(uid, password):
        return None

    async def sleep(seconds):
        if seconds == 15:
            backoff_started.set()
            await hold.wait()
        else:
            await original_sleep(seconds)

    monkeypatch.setattr(lv, 'version_config', version_config)
    monkeypatch.setattr(lv, 'get_access_token', fail_oauth)
    monkeypatch.setattr(bm.asyncio, 'sleep', sleep)
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(lv, '_LOGIN_SEMAPHORE', semaphore)

    async def run():
        await manager.start_account(acc['id'], broadcast=False)
        await asyncio.wait_for(backoff_started.wait(), timeout=2)
        assert semaphore._value == 1  # worker is retrying, not holding the slot
        await manager.stop_account(acc['id'], broadcast=False)
    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_exp_chart_all_levels_and_unverified_profiles(isolated_db):
    from app.exp_chart import LEVEL_START_EXP, exp_progress

    supplied_needs = [
        48,154,342,468,832,948,1008,1070,1134,1188,1256,1312,1380,1426,
        1494,1550,1614,1678,1730,1792,1854,1914,1974,2034,2094,2154,
        3356,3470,3596,3712,4812,5172,5530,5898,6466,7046,7622,8194,
        8770,12052,13122,14206,15288,16366,17446,18796,20148,21494,
        22850,24196,44262,46664,49062,51484,53856,84392,87988,91592,
        95186,132916,138914,144914,150918,158116,206646,
    ]
    assert len(supplied_needs) == 65
    for level, start_exp in enumerate(LEVEL_START_EXP, start=1):
        progress = exp_progress(level, start_exp, verified_profile=True)
        assert progress['exp_chart_status'] == 'calculated'
        assert progress['exp_to_next_level'] == (LEVEL_START_EXP[level] - start_exp if level in (19, 20, 54) else supplied_needs[level - 1])
        assert progress['exp_next_level_total'] == start_exp + progress['exp_to_next_level']
    assert exp_progress(30, 45000, True)['exp_to_next_level'] == 3582
    assert exp_progress(35, 69994, False)['exp_to_next_level'] is None
    assert exp_progress(30, 999999, True)['exp_chart_status'] == 'mismatch'
    assert exp_progress(66, 2000000, True)['exp_chart_status'] == 'outside_chart'

    acc = db.create_account('check_exp_01', 'testpass')
    assert db.get_account_by_id(acc['id'])['exp_chart_status'] == 'awaiting_profile'
    db.update_account_runtime_state(acc['id'], game_account_id='98765', level=30, exp=45000)
    projected = db.get_account_by_id(acc['id'])
    assert projected['exp_to_next_level'] == 3582
    assert projected['exp_level_progress_percent'] > 0


def test_chart_endpoint_is_protected(isolated_db):
    with TestClient(app) as client:
        assert client.get('/api/exp/chart').status_code == 401
        token = create_session_token('admin')
        data = client.get('/api/exp/chart', headers={'Authorization': f'Bearer {token}'}).json()
        assert data['source'] == 'user_supplied_levels_1_65'
        assert len(data['levels']) == 65
        assert data['levels'][-1]['next_level_total'] == 1840946
        assert [row['level'] for row in data['levels'] if row['chart_row_conflict']] == [19, 20, 54]


def test_brand_assets_and_no_fake_profile_values(isolated_db):
    with TestClient(app) as client:
        home = client.get('/')
        assert home.status_code == 200
        assert 'ＧＡＭＥＲ-ＣＯＲＰ' in home.text
        assert 'bot-grid' in home.text
        assert 'href="/admin"' in home.text
        admin = client.get('/admin')
        assert admin.status_code == 200
        assert 'view-accounts' in admin.text
        assert 'view-activity' in admin.text
        assert 'mission-hero' not in admin.text  # deliberately rebuilt UI
        assert '84FF' not in home.text + admin.text
        assert client.get('/static/css/styles.css').status_code == 200
        assert client.get('/static/css/public.css').status_code == 200
        assert client.get('/static/js/app.js').status_code == 200
        assert client.get('/static/js/public.js').status_code == 200
    acc = db.create_account('freshguest', 'testpass')
    public = db.get_account_by_id(acc['id'])
    assert public['exp_chart_status'] == 'awaiting_profile'
    assert public['exp_to_next_level'] is None


def test_zero_exp_is_kept_as_initial_baseline(isolated_db):
    acc = db.create_account('level_one_guest', 'testpass')
    db.update_account_runtime_state(acc['id'], game_account_id='123450', level=1,
                                    exp=0, start_session=True)
    first = db.get_account_by_id(acc['id'])
    assert first['exp_baseline_status'] == 'verified'
    assert first['initial_exp'] == 0
    assert first['session_start_exp'] == 0
    db.update_account_runtime_state(acc['id'], level=2, exp=48)
    updated = db.get_account_by_id(acc['id'])
    assert updated['initial_exp'] == 0  # old code incorrectly changed this to 48
    assert updated['exp_gained'] == 48
    assert updated['session_exp_gained'] == 48
    assert updated['exp_to_next_level'] == 154


def test_existing_database_marks_legacy_baseline_uncertain(tmp_path, monkeypatch):
    import sqlite3
    dbpath = str(tmp_path / 'old.db')
    with sqlite3.connect(dbpath) as conn:
        conn.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY, game_account_id TEXT, status TEXT, status_message TEXT, initial_exp INTEGER)")
        conn.execute("INSERT INTO accounts VALUES ('old_account', '123', 'online', 'Running', 48)")
    monkeypatch.setattr(db, 'DATABASE_PATH', dbpath)
    db.init_db()
    with sqlite3.connect(dbpath) as conn:
        row = conn.execute("SELECT initial_exp, exp_baseline_set, session_start_exp, status FROM accounts WHERE id='old_account'").fetchone()
    assert row == (48, 2, None, 'inactive')


def test_profile_refresh_rejects_missing_fields_and_reports_real_change(monkeypatch):
    from types import SimpleNamespace
    account = dict(server_url='https://example.invalid', token='token',
                   release_version='OB55', login_payload_data=b'payload',
                   account_id=123, level=1, exp=0)

    async def missing(*args):
        return SimpleNamespace(nickname=''), {'6': {'data': 2}}

    async def valid(*args):
        return SimpleNamespace(nickname='New'), {
            '1': {'data': 123}, '6': {'data': 2}, '7': {'data': 48}}

    async def run():
        monkeypatch.setattr(lv, 'send_getlogin', missing)
        result = await lv.refresh_account_profile(account)
        assert result['ok'] is False
        assert account['level'] == 1 and account['exp'] == 0
        monkeypatch.setattr(lv, 'send_getlogin', valid)
        result = await lv.refresh_account_profile(account)
        assert result['ok'] and result['changed']
        assert (result['level'], result['exp'], result['exp_delta']) == (2, 48, 48)
        repeat = await lv.refresh_account_profile(account)
        assert repeat['ok'] and not repeat['changed'] and repeat['exp_delta'] == 0
    asyncio.run(run())


def test_profile_result_records_error_and_unchanged_without_fake_gain(isolated_db):
    manager = bm.BotManager()
    acc = db.create_account('profile_test_user', 'testpass')
    db.update_account_runtime_state(acc['id'], game_account_id='333', level=2,
                                    exp=48, start_session=True)
    manager._on_profile_result(acc['id'], {'ok': False, 'reason': 'GetLoginData returned no valid response'})
    failed = db.get_account_by_id(acc['id'])
    assert failed['profile_check_status'] == 'error'
    assert failed['profile_last_checked_at']
    assert failed['session_exp_gained'] == 0
    manager._on_profile_result(acc['id'], {'ok': True, 'changed': False, 'level': 2, 'exp': 48})
    assert db.get_account_by_id(acc['id'])['profile_check_status'] == 'unchanged'
    assert db.get_account_by_id(acc['id'])['exp'] == 48
    lv.unregister_log_callback(manager._on_lv_log)


def test_manual_profile_check_updates_saved_exp(isolated_db, monkeypatch):
    from types import SimpleNamespace
    manager = bm.BotManager()
    acc = db.create_account('manual_sync_guest', 'testpass')
    db.update_account_runtime_state(acc['id'], game_account_id='123451', level=1,
                                    exp=0, start_session=True)
    hold = asyncio.Event()
    account_data = dict(server_url='https://example.invalid', token='token',
                        release_version='OB55', login_payload_data=b'payload',
                        account_id=123451, level=1, exp=0,
                        _profile_refresh_lock=asyncio.Lock())
    account_data['_profile_result_callback'] = lambda result: manager._on_profile_result(acc['id'], result)

    async def valid(*args):
        return SimpleNamespace(nickname='Player'), {
            '1': {'data': 123451}, '6': {'data': 2}, '7': {'data': 48}}
    monkeypatch.setattr(lv, 'send_getlogin', valid)

    async def run():
        worker = asyncio.create_task(hold.wait())
        manager.workers[acc['id']] = worker
        manager.active_account_data[acc['id']] = account_data
        result = await manager.refresh_profile_now(acc['id'])
        assert result['ok'] and result['check']['changed']
        assert result['account']['session_exp_gained'] == 48
        assert result['account']['exp_gained'] == 48
        assert result['account']['profile_check_status'] == 'changed'
        with pytest.raises(ValueError, match='12 seconds'):
            await manager.refresh_profile_now(acc['id'])
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_profile_refresh_route_is_protected_and_idle_worker_cannot_be_queried(isolated_db):
    acc = db.create_account('refresh_route_guest', 'testpass')
    with TestClient(app) as client:
        route = f"/api/accounts/{acc['id']}/refresh-profile"
        assert client.post(route).status_code == 401
        token = create_session_token('admin')
        response = client.post(route, headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == 409
        assert 'Start the account worker' in response.json()['detail']


def test_join_timeout_retries_without_claiming_match_reward(isolated_db):
    assert lv.udp_timeout_reason(70, 70, False, 5) is None
    assert lv.udp_timeout_reason(80, 80, False, 2) == 'join_timeout'
    assert lv.udp_timeout_reason(300, 300, True, 181) == 'idle_timeout'
    assert lv.udp_timeout_reason(1600, 1600, True, 10) == 'session_limit'

    manager = bm.BotManager()
    acc = db.create_account('timeout_worker', 'testpass')
    manager.uid_to_account_id['timeout_worker'] = acc['id']
    manager._on_lv_log('[!] Match #1 UDP JOIN_MATCH timeout (75s after startup; no confirmation) | UID: timeout_worker')
    state = db.get_account_by_id(acc['id'])
    assert state['status'] == 'reconnecting'
    assert state['udp_timeout_count'] == 1
    assert state['matches_completed'] == 0
    assert state['exp'] == 0
    manager._on_lv_log('[MATCH #2] UDP JOIN_MATCH received from server | UID: timeout_worker')
    state = db.get_account_by_id(acc['id'])
    assert state['udp_join_count'] == 1
    assert state['matches_completed'] == 0  # UDP join is NOT a game reward
    manager._on_lv_log('[★] Match #2 finished (cmd 103) | UID: timeout_worker')
    state = db.get_account_by_id(acc['id'])
    assert state['matches_completed'] == 1
    assert state['server_finish_count'] == 1
    assert state['last_server_finish_at']
    assert state['exp'] == 0  # even end packet cannot invent server EXP
    lv.unregister_log_callback(manager._on_lv_log)


def test_fleet_event_type_filter_is_authenticated(isolated_db):
    db.add_activity_log(None, 'System', 'warning', 'udp_timeout', 'UDP timeout')
    db.add_activity_log(None, 'System', 'success', 'udp_join', 'UDP join')
    with TestClient(app) as client:
        assert client.get('/api/activity?event_type=udp_timeout').status_code == 401
        token = create_session_token('admin')
        headers = {'Authorization': f'Bearer {token}'}
        events = client.get('/api/activity?event_type=udp_timeout', headers=headers).json()['events']
        assert len(events) == 1 and events[0]['event_type'] == 'udp_timeout'
        assert client.get('/api/activity?event_type=not_valid', headers=headers).status_code == 422


def test_multi_account_sessions_keep_separate_modes_maps_and_game_ids(isolated_db, monkeypatch):
    from types import SimpleNamespace
    manager = bm.BotManager()
    monkeypatch.setattr(bm, 'ACCOUNT_START_STAGGER_SECONDS', 0)  # pacing is covered by test_multi_account_start.py
    a = db.create_account('30001', 'testpass', region='BD', mode_id=2, map_id=3)
    b = db.create_account('30002', 'testpass', region='BD', mode_id=4, map_id=5)
    observed = {}
    stop = asyncio.Event()
    both_ready = asyncio.Event()

    async def config(region): return 'OB55', '1.132.8', 'https://example.invalid/'
    async def token(uid, password): return uid, 'not_a_real_token', 4
    async def payload(open_id, *args): return open_id.encode()
    async def major(data, release, url):
        gid = int(data.decode()) + 100000
        return SimpleNamespace(account_id=gid, aes_ak=b'0' * 16, iv_i=b'1' * 16,
                               region='BD', token='token', server_time=123456789, url=url)
    async def getlogin(data, base_url, token, release):
        gid = int(data.decode()) + 100000
        return SimpleNamespace(nickname=f'P{gid}', functional_addrs='127.0.0.1:8080', informational_addrs=''), {
            '1': {'data': gid}, '6': {'data': 2}, '7': {'data': 48}}
    async def worker(data, uid):
        observed[uid] = (data['account_id'], data['mode_id'], data['map_id'])
        if len(observed) == 2: both_ready.set()
        await stop.wait()

    for name, value in [('version_config', config), ('get_access_token', token),
                        ('build_majorlogin_payload', payload), ('send_majorlogin', major),
                        ('send_getlogin', getlogin), ('run_account_worker', worker)]:
        monkeypatch.setattr(lv, name, value)

    async def run():
        await manager.start_account(a['id'], broadcast=False)
        await manager.start_account(b['id'], broadcast=False)
        await asyncio.wait_for(both_ready.wait(), timeout=3)
        assert observed == {'30001': (130001, 2, 3), '30002': (130002, 4, 5)}
        assert manager.game_id_to_account_id == {'130001': a['id'], '130002': b['id']}
        stop.set()
        await manager.stop_account(a['id'], broadcast=False)
        await manager.stop_account(b['id'], broadcast=False)

    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_duplicate_game_id_does_not_steal_another_worker(isolated_db, monkeypatch):
    from types import SimpleNamespace
    manager = bm.BotManager()
    monkeypatch.setattr(bm, 'ACCOUNT_START_STAGGER_SECONDS', 0)  # pacing is covered by test_multi_account_start.py
    a = db.create_account('40001', 'testpass', region='BD')
    b = db.create_account('40002', 'testpass', region='BD')
    first_ready = asyncio.Event()
    stop = asyncio.Event()

    async def config(region): return 'OB55', '1.132.8', 'https://example.invalid/'
    async def token(uid, password): return uid, 'dummy', 4
    async def payload(open_id, *args): return open_id.encode()
    async def major(data, release, url):
        return SimpleNamespace(account_id=555000, aes_ak=b'0' * 16, iv_i=b'1' * 16,
                               region='BD', token='dummy', server_time=12345, url=url)
    async def getlogin(data, base_url, token, release):
        return SimpleNamespace(nickname='SameGame', functional_addrs='127.0.0.1:8080', informational_addrs=''), {
            '1': {'data': 555000}, '6': {'data': 2}, '7': {'data': 48}}
    async def worker(data, uid):
        first_ready.set()
        await stop.wait()
    for name, value in [('version_config', config), ('get_access_token', token),
                        ('build_majorlogin_payload', payload), ('send_majorlogin', major),
                        ('send_getlogin', getlogin), ('run_account_worker', worker)]:
        monkeypatch.setattr(lv, name, value)

    async def run():
        await manager.start_account(a['id'], broadcast=False)
        await asyncio.wait_for(first_ready.wait(), timeout=3)
        await manager.start_account(b['id'], broadcast=False)
        for _ in range(40):
            await asyncio.sleep(0.01)
            if db.get_account_by_id(b['id'])['status'] == 'error': break
        assert db.get_account_by_id(b['id'])['status'] == 'error'
        assert 'already connected' in db.get_account_by_id(b['id'])['status_message']
        assert manager.game_id_to_account_id['555000'] == a['id']
        stop.set()
        await manager.stop_account(a['id'], broadcast=False)
    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_legacy_match_counter_is_not_treated_as_server_finish(isolated_db):
    acc = db.create_account('old_finish_guest', 'testpass')
    db.update_account_runtime_state(acc['id'], inc_matches_completed=3)
    public = db.get_account_by_id(acc['id'])
    assert public['matches_completed'] == 3  # legacy raw count preserved
    assert public['server_finish_count'] == 0  # no server end signal observed in V5


def test_region_keepalive_matches_configured_gateway_family():
    # Before this fix BD (031400) silently fell back to 0215, unlike IND.
    async def run():
        for region, prefix in lv.REGION_PREFIX.items():
            packet = await lv.send_keep_alive(region)
            assert packet == bytes.fromhex('02' + prefix[2:4]), region
        assert await lv.send_keep_alive('BD') == bytes.fromhex('0214')
    asyncio.run(run())


def test_two_joined_accounts_only_verified_profile_gain_is_attributed(isolated_db):
    manager = bm.BotManager()
    a = db.create_account('bd_guest_a', 'testpass', region='BD')
    b = db.create_account('bd_guest_b', 'testpass', region='BD')
    for acc, gid in ((a, '901'), (b, '902')):
        db.update_account_runtime_state(acc['id'], game_account_id=gid,
                                        level=8, exp=3800, start_session=True)
        manager.uid_to_account_id[acc['uid']] = acc['id']
        manager._on_lv_log(f'[MATCH #1] UDP JOIN_MATCH received from server | UID: {acc["uid"]}')
    manager._on_profile_result(a['id'], {'ok': True, 'changed': False, 'level': 8, 'exp': 3800,
                                          'exp_delta': 0})
    manager._on_profile_result(b['id'], {'ok': True, 'changed': True, 'level': 8, 'exp': 4451,
                                          'exp_delta': 651})
    first, second = (db.get_account_by_id(x['id']) for x in (a, b))
    assert first['game_account_id'] == '901' and second['game_account_id'] == '902'
    assert first['udp_join_count'] == second['udp_join_count'] == 1
    assert first['server_finish_count'] == second['server_finish_count'] == 0
    assert first['session_exp_gained'] == 0 and second['session_exp_gained'] == 651
    assert first['session_last_exp_gain_at'] is None and second['session_last_exp_gain_at']
    assert first['session_exp_gain_events'] == 0 and second['session_exp_gain_events'] == 1
    assert not db.get_activity_page(account_id=a['id'], event_type='profile_gain')
    assert len(db.get_activity_page(account_id=b['id'], event_type='profile_gain')) == 1
    assert '651' in db.get_activity_page(account_id=b['id'], event_type='profile_gain')[0]['message']
    db.update_account_runtime_state(b['id'], level=8, exp=4451, start_session=True)
    reset = db.get_account_by_id(b['id'])
    assert reset['session_exp_gained'] == 0 and reset['session_last_exp_gain_at'] is None
    assert reset['session_exp_gain_events'] == 0
    lv.unregister_log_callback(manager._on_lv_log)


def test_verified_gain_filter_requires_auth(isolated_db):
    db.add_activity_log(None, 'System', 'success', 'profile_gain', 'Server profile increase')
    with TestClient(app) as client:
        assert client.get('/api/activity?event_type=profile_gain').status_code == 401
        token = create_session_token('admin')
        response = client.get('/api/activity?event_type=profile_gain',
                              headers={'Authorization': f'Bearer {token}'})
        assert response.status_code == 200
        assert len(response.json()['events']) == 1


def test_100_no_join_requests_cool_only_that_worker_then_resume(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'QUEUE_NO_JOIN_LIMIT', 3)
    monkeypatch.setattr(bm, 'QUEUE_OBSERVATION_SECONDS', 0.02)
    monkeypatch.setattr(bm, 'QUEUE_COOLDOWN_SECONDS', 1)
    manager = bm.BotManager()
    a = db.create_account('queue_guest_a', 'testpass')
    b = db.create_account('queue_guest_b', 'testpass')
    spawned = {a['id']: 0, b['id']: 0}
    async def sleeping_worker(aid):
        spawned[aid] += 1
        await asyncio.Event().wait()
    monkeypatch.setattr(manager, '_account_worker_lifecycle', sleeping_worker)

    async def wait_status(aid, target):
        for _ in range(240):
            if db.get_account_by_id(aid)['status'] == target:
                return
            await asyncio.sleep(.01)
        raise AssertionError(f'{target} not reached: {db.get_account_by_id(aid)}')

    async def run():
        await manager.start_account(a['id'], broadcast=False)
        await manager.start_account(b['id'], broadcast=False)
        manager.active_account_data[a['id']] = {'_queue_cooldown_pending': False}
        for i in range(3):
            manager._on_lv_log(f'[BR] UID queue_guest_a -> BR match #{i+1} (request sent)')
        manager._on_lv_log('[BR] UID queue_guest_b -> BR match #1 (request sent)')
        await wait_status(a['id'], 'cooldown')
        assert not manager.is_account_running(a['id'])
        assert manager.is_account_running(b['id'])
        state = db.get_account_by_id(a['id'])
        assert state['queue_no_join_streak'] == 3
        assert state['queue_cooldown_count'] == 1 and state['cooldown_until']
        assert len(db.get_activity_page(account_id=a['id'], event_type='queue_cooldown')) == 1
        with pytest.raises(ValueError, match='cooldown'):
            await manager.start_account(a['id'], broadcast=False)
        await wait_status(a['id'], 'authenticating')
        await asyncio.sleep(.03)  # give the newly scheduled worker one event-loop turn
        assert spawned[a['id']] == 2
        assert db.get_account_by_id(a['id'])['queue_no_join_streak'] == 0
        assert db.get_account_by_id(a['id'])['cooldown_until'] is None
        assert len(db.get_activity_page(account_id=a['id'], event_type='queue_resume')) == 1
        await manager.stop_account(a['id'], broadcast=False)
        await manager.stop_account(b['id'], broadcast=False)
    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_join_cancels_pending_cooldown_and_manual_stop_cancels_timer(isolated_db, monkeypatch):
    monkeypatch.setattr(bm, 'QUEUE_NO_JOIN_LIMIT', 2)
    monkeypatch.setattr(bm, 'QUEUE_OBSERVATION_SECONDS', 0.01)
    monkeypatch.setattr(bm, 'QUEUE_COOLDOWN_SECONDS', 600)
    manager = bm.BotManager()
    a = db.create_account('join_after_queue', 'testpass')
    async def sleeping_worker(aid): await asyncio.Event().wait()
    monkeypatch.setattr(manager, '_account_worker_lifecycle', sleeping_worker)

    async def run():
        await manager.start_account(a['id'], broadcast=False)
        manager.active_account_data[a['id']] = {}
        for i in range(2):
            manager._on_lv_log(f'[BR] UID join_after_queue -> BR match #{i+1} (request sent)')
        manager._on_lv_log('[MATCH #1] UDP JOIN_MATCH received from server | UID: join_after_queue')
        await asyncio.sleep(.06)
        assert manager.is_account_running(a['id'])
        assert db.get_account_by_id(a['id'])['queue_no_join_streak'] == 0
        assert not db.get_account_by_id(a['id'])['cooldown_until']
        for i in range(2):
            manager._on_lv_log(f'[BR] UID join_after_queue -> BR match #{i+3} (request sent)')
        for _ in range(100):
            if db.get_account_by_id(a['id'])['status'] == 'cooldown': break
            await asyncio.sleep(.01)
        assert db.get_account_by_id(a['id'])['status'] == 'cooldown'
        await manager.stop_account(a['id'], broadcast=False)
        assert db.get_account_by_id(a['id'])['status'] == 'inactive'
        assert db.get_account_by_id(a['id'])['cooldown_until'] is None
        assert not manager.queue_cooldown_tasks
    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_cooldown_survives_app_restart_until_deadline(isolated_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    acc = db.create_account('persisted_pause', 'testpass')
    until = (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    db.update_account_runtime_state(acc['id'], status='cooldown',
                                    status_message='All sockets closed')
    db.set_queue_cooldown(acc['id'], until, increment=True)
    db.init_db()  # Startup migration must not change a deliberately offline ID.
    assert db.get_account_by_id(acc['id'])['status'] == 'cooldown'
    manager = bm.BotManager()
    async def sleeping_worker(aid): await asyncio.Event().wait()
    monkeypatch.setattr(manager, '_account_worker_lifecycle', sleeping_worker)

    async def run():
        await manager.resume_active_accounts_on_startup()
        assert acc['id'] in manager.queue_cooldown_tasks
        assert not manager.is_account_running(acc['id'])
        await manager.shutdown_all()  # shutdown cancels timer but keeps saved deadline
        assert db.get_account_by_id(acc['id'])['status'] == 'cooldown'
        assert db.get_account_by_id(acc['id'])['cooldown_until'] == until
        await manager.resume_active_accounts_on_startup()
        for _ in range(300):
            if manager.is_account_running(acc['id']): break
            await asyncio.sleep(.01)
        assert manager.is_account_running(acc['id'])
        assert db.get_account_by_id(acc['id'])['cooldown_until'] is None
        await manager.stop_account(acc['id'], broadcast=False)
    asyncio.run(run())
    lv.unregister_log_callback(manager._on_lv_log)


def test_recovery_fields_are_public_without_secrets_and_logs_stay_protected(isolated_db):
    from datetime import datetime, timezone
    acc = db.create_account('recovery_api_guest', 'private-password')
    db.update_account_runtime_state(acc['id'], status='cooldown',
                                    inc_matches_searched=100, inc_queue_no_join=100,
                                    udp_phase='join_timeout')
    until = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    db.set_queue_cooldown(acc['id'], until, increment=True)
    db.add_activity_log(acc['id'], 'Recovery', 'warning', 'queue_cooldown', 'Offline 10 minutes')
    with TestClient(app) as client:
        assert client.get('/api/dashboard/overview').status_code == 401
        token = create_session_token('admin')
        headers = {'Authorization': f'Bearer {token}'}
        response = client.get('/api/dashboard/overview', headers=headers)
        assert response.status_code == 200
        data = response.json()
        found = next(a for a in data['accounts'] if a['id'] == acc['id'])
        assert found['queue_no_join_streak'] == 100 and found['cooldown_until'] == until
        assert found['udp_phase'] == 'join_timeout'
        assert data['summary']['cooldown_accounts'] == 1
        assert data['status_breakdown']['cooldown'] == 1
        assert 'private-password' not in response.text
        assert client.get('/api/activity?event_type=queue_cooldown').status_code == 401
        assert client.get('/api/activity?event_type=queue_cooldown', headers=headers).status_code == 200


def test_partial_udp_startup_send_retries_before_claiming_success(monkeypatch):
    class FakeSocket:
        def bind(self, addr): pass
        def setblocking(self, value): pass
        def close(self): pass
    real_socket = lv.socket.socket
    monkeypatch.setattr(lv.socket, 'socket', lambda *a, **k:
                        FakeSocket() if len(a) > 1 and a[1] == lv.socket.SOCK_DGRAM
                        else real_socket(*a, **k))
    monkeypatch.setattr(lv, 'optimize_udp_socket', lambda sock: None)
    async def resolve(host): return '127.0.0.1'
    async def hello(*a): return 'cc'
    async def packet(*a, **k): return b'ack'
    async def ping(*a): await asyncio.Event().wait()
    sends = []
    async def send(loop, sock, data, addr):
        sends.append(data)
        if data == b'\xbb' and sends.count(b'\xbb') == 1:
            raise OSError('temporary SHARMA send failure')
        return len(data)
    received = 0
    async def recv(*a):
        nonlocal received
        received += 1
        if received == 1: return b'reply', ('127.0.0.1', 7777)
        await asyncio.Event().wait()
    async def decode(*a): return {'cmd': 2, 'content': b'\x01\x00'}
    async def classify(*a): return 'ACK'
    async def reply(*a, **k): return ('ACK', b'ack')
    for name, value in [('resolve_host_cloudflare', resolve),
                        ('build_hello_packet', hello), ('build_packet', packet),
                        ('keepalive_ping', ping), ('_udp_sendto', send),
                        ('_udp_recvfrom', recv), ('decode_packet', decode),
                        ('classify', classify), ('reply_for', reply)]:
        monkeypatch.setattr(lv, name, value)
    messages = []
    ready = asyncio.Event()
    def capture(message):
        messages.append(message)
        if 'UDP startup packets sent;' in message: ready.set()
    lv.register_log_callback(capture)

    async def run():
        worker = asyncio.create_task(lv.play_game('127.0.0.1:7777', 'aa', 'bb',
            '00' * 16, '12340', '777', 'BD', 'OB55', b'0'*16, b'1'*16,
            match_index=1, log_uid='guest-udp'))
        try:
            await asyncio.wait_for(ready.wait(), timeout=6)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    try:
        asyncio.run(run())
    finally:
        lv.unregister_log_callback(capture)
    assert sends.count(b'\xbb') == 2
    assert sum('UDP startup packets sent;' in m for m in messages) == 1
    assert any('[SEND-FAIL] Startup:' in m for m in messages)
