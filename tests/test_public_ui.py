"""UI routes and public data boundaries; no game-server traffic."""
from html.parser import HTMLParser
from pathlib import Path
import re
from fastapi.testclient import TestClient
import pytest
from app import database as db
from app.main import app

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'cards.db'))
    db.init_db()
    return db


def test_public_bot_cards_are_read_only_and_allowlisted(isolated_db):
    acc = db.create_account('secret-guest-login', 'secret-password',
                            display_name='Visible Bot', region='BD',
                            notes='INTERNAL-ONLY-NOTE')
    db.update_account_runtime_state(acc['id'], status='cooldown',
        status_message='PRIVATE-DIAGNOSTIC', game_account_id='987654',
        level=8, exp=4091, start_session=True, inc_matches_searched=100,
        inc_queue_no_join=100, udp_phase='join_timeout')
    db.add_activity_log(acc['id'], 'Visible Bot', 'warning', 'worker_output',
                        'PRIVATE-WORKER-LOG')
    with TestClient(app) as client:
        resp = client.get('/api/public/overview')
        assert resp.status_code == 200
        assert resp.headers['cache-control'] == 'no-store'
        body = resp.json()
        assert set(body) == {'observed_at', 'statuses', 'bots'}
        assert body['statuses']['cooldown'] == body['statuses']['total'] == 1
        card = body['bots'][0]
        assert card['display_name'] == 'Visible Bot'
        assert card['game_account_id'] == '987654'
        assert card['exp'] == 4091 and card['level'] == 8
        assert card['queue_no_join_streak'] == 100
        assert card['exp_to_next_level'] == 779
        assert card['status'] == 'cooldown'
        for secret in ('secret-guest-login', 'secret-password', 'PRIVATE-DIAGNOSTIC',
                       'PRIVATE-WORKER-LOG', 'INTERNAL-ONLY-NOTE', 'encrypted_password',
                       'auth_uid', 'uid', 'notes', 'status_message', 'device_profile'):
            assert secret not in str(card)
        assert client.get('/api/dashboard/overview').status_code == 401
        assert client.get('/api/activity').status_code == 401
        assert client.post(f"/api/accounts/{acc['id']}/stop").status_code == 401


def test_public_without_server_profile_does_not_invent_exp(isolated_db):
    acc = db.create_account('newguest', 'testpass', display_name='New Bot')
    with TestClient(app) as client:
        card = client.get('/api/public/overview').json()['bots'][0]
    assert card['game_account_id'] is None
    assert card['level'] is None and card['exp'] is None
    assert card['exp_to_next_level'] is None
    assert card['session_exp_gained'] is None
    assert card['profile_check_status'] == 'never'


def test_new_admin_html_has_matching_script_targets():
    html = (ROOT/'static/index.html').read_text()
    js = (ROOT/'static/js/app.js').read_text()
    refs = set(re.findall(r"\$\('([^']+)'\)", js))
    ids = set(re.findall(r'id="([^"]+)"', html))
    assert not refs - ids
    assert 'mission-hero' not in html
    assert 'view-overview' in html and 'view-settings' in html
    assert '/api/dashboard/overview' in js
    assert '/api/accounts/bulk-add' in js


def test_public_page_only_has_read_only_controls():
    html = (ROOT/'static/public.html').read_text()
    js = (ROOT/'static/js/public.js').read_text()
    refs = set(re.findall(r"\$\('([^']+)'\)", js))
    ids = set(re.findall(r'id="([^"]+)"', html))
    assert not refs - ids
    assert 'bot-grid' in html
    assert '/api/public/overview' in js
    assert '/api/accounts/' not in js
    assert 'data-action="delete"' not in html
    assert 'password' not in html.lower()
