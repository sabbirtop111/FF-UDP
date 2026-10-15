"""V10 public read-only telemetry and honest level-line coverage (no game network)."""
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import database as db
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'observatory.db'))
    db.init_db()
    return db


def test_public_level_line_uses_only_verified_chart_and_allowlisted_telemetry(isolated_db):
    active = db.create_account('PRIVATE-LOGIN-UID', 'PRIVATE-PASSWORD',
                               display_name='Aster', notes='PRIVATE-NOTE')
    db.update_account_runtime_state(active['id'], game_account_id='PUBLIC-GAME-ID',
                                    level=8, exp=4081, start_session=True)
    db.update_account_runtime_state(active['id'], status='searching_match', exp=4091,
                                    inc_matches_searched=25, inc_queue_no_join=10,
                                    inc_udp_joins=2, inc_udp_timeouts=1,
                                    inc_reconnects=3, server_finish=True, udp_phase='ack_received')
    db.record_profile_check(active['id'], ok=True, changed=True, exp_delta=10,
                            message='PRIVATE-DIAGNOSTIC')
    unknown = db.create_account('SECOND-PRIVATE-UID', 'OTHER-PRIVATE-PASSWORD',
                                display_name='Pending')
    mismatch = db.create_account('THIRD-PRIVATE-UID', 'OTHER-PRIVATE-PASSWORD',
                                 display_name='Mismatch')
    db.update_account_runtime_state(mismatch['id'], game_account_id='OTHER-PUBLIC-ID',
                                    level=2, exp=500)
    with TestClient(app) as client:
        result = client.get('/api/public/overview')
        assert result.status_code == 200
        cards = {b['display_name']: b for b in result.json()['bots']}
    a = cards['Aster']
    assert a['exp_chart_status'] == 'calculated'
    assert (a['level'],a['exp'],a['exp_level_start'],a['exp_next_level_total']) == (8,4091,3800,4870)
    assert a['exp_needed_for_level'] == 1070
    assert a['exp_to_next_level'] == 779
    assert a['exp_level_progress_percent'] == 27.2
    assert a['session_exp_gained'] == 10 and a['session_exp_gain_events'] == 1
    assert a['session_last_exp_gain_at'] and a['profile_last_changed_at']
    assert a['last_udp_join_at'] and a['last_udp_timeout_at'] and a['last_server_finish_at']
    assert a['reconnect_count'] == 3
    assert cards['Pending']['exp_level_progress_percent'] is None
    assert cards['Pending']['exp'] is None and cards['Pending']['level'] is None
    assert cards['Mismatch']['exp_chart_status'] == 'mismatch'
    assert cards['Mismatch']['exp_level_progress_percent'] is None
    for secret in ('PRIVATE-LOGIN-UID','SECOND-PRIVATE-UID','THIRD-PRIVATE-UID',
                   'PRIVATE-PASSWORD','PRIVATE-NOTE','PRIVATE-DIAGNOSTIC',
                   'encrypted_password','device_profile','status_message','uid'):
        assert secret not in str(cards)


def test_public_template_and_renderer_stay_read_only():
    html = (ROOT/'static/public.html').read_text()
    js = (ROOT/'static/js/public.js').read_text()
    assert '<div id="bot-grid"' in html
    assert 'role="progressbar"' in js
    assert 'exp_level_progress_percent' in js
    assert 'exp_chart_status' in js
    assert '/api/public/overview' in js
    assert '/api/accounts/' not in js
    assert 'type="password"' not in html
    assert '/static/css/control-v10.css' in (ROOT/'static/index.html').read_text()


def test_renderer_escapes_and_never_draws_fake_level_line():
    if not shutil.which('node'):
        pytest.skip('Node is optional for offline frontend smoke tests')
    result = subprocess.run(['node',str(ROOT/'tests/check_card_renderer.cjs')],
                            cwd=ROOT,text=True,capture_output=True,timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'valid LV line' in result.stdout
