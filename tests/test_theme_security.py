"""RGB colour mode and actual server-side administrator boundary; offline only."""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth, database as db
from app.config import ADMIN_PASSWORD, ADMIN_USERNAME, SECRET_KEY
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_auth_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DATABASE_PATH', str(tmp_path / 'boundary.db'))
    original = auth._ADMIN_STATE.copy()
    yield
    auth._ADMIN_STATE.update(original)
    auth._FAILED_LOGINS.clear()


def test_published_admin_password_refused_before_server_starts():
    env = os.environ.copy()
    env['ADMIN_PASSWORD'] = 'admin123'
    result = subprocess.run([sys.executable, '-c', 'import app.config'],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'Insecure ADMIN_PASSWORD' in result.stderr


def test_theme_is_dark_by_default_and_saved_across_both_pages():
    admin = (ROOT/'static/index.html').read_text()
    public = (ROOT/'static/public.html').read_text()
    theme = (ROOT/'static/js/theme.js').read_text()
    palette = (ROOT/'static/css/theme.css').read_text()
    for page in (admin, public):
        assert '/static/js/theme.js' in page
        assert '/static/css/theme.css' in page
        assert 'data-theme-toggle' in page
        assert page.index('theme.js') < page.index('theme.css')
    assert "current = mode === 'light' ? 'light' : 'dark'" in theme
    assert 'apply(stored)' in theme
    assert 'localStorage.setItem(key, mode)' in theme
    assert '[data-theme="light"]' in palette and '[data-theme="dark"]' in palette
    assert '--gc-cyan:' in palette and '--gc-pink:' in palette and '--gc-violet:' in palette
    assert '/api/accounts/' not in (ROOT/'static/js/public.js').read_text()


def sign(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    signature = hmac.new(SECRET_KEY.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f'{encoded}.{signature}'


def test_no_unauthenticated_admin_api_or_ws_access(isolated_auth_db):
    with TestClient(app) as client:
        routes = [('/api/auth/me', 'get', None),
                  ('/api/auth/change-password', 'post', {'current_password':'a', 'new_password':'LongEnough123!'}),
                  ('/api/dashboard/overview', 'get', None),
                  ('/api/activity', 'get', None),
                  ('/api/accounts', 'get', None),
                  ('/api/accounts', 'post', {'uid':'test123','password':'test123'}),
                  ('/api/accounts/bulk-add', 'post', {}),
                  ('/api/accounts/bulk-action', 'post', {}),
                  ('/api/accounts/import/metadata', 'post', {}),
                  ('/api/accounts/export/metadata', 'get', None),
                  ('/api/accounts/fake/start', 'post', None),
                  ('/api/accounts/fake/stop', 'post', None),
                  ('/api/accounts/fake/restart', 'post', None),
                  ('/api/accounts/fake', 'delete', None),
                  ('/api/system/diagnostics', 'post', None)]
        for path, method, data in routes:
            response = getattr(client, method)(path, json=data) if data is not None else getattr(client, method)(path)
            assert response.status_code == 401, (method, path, response.status_code)
        assert client.get('/').status_code == 200
        assert client.get('/admin').status_code == 200  # login HTML is public, the data is not
        assert client.get('/api/public/overview').status_code == 200
        assert client.get('/api/auth/me', headers={'Origin':'https://attacker.example'}).headers.get('access-control-allow-origin') is None
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect('/ws/live'):
                pass
        assert exc.value.code == 4401


def test_signed_tokens_must_be_current_admin_not_just_valid_signature(isolated_auth_db):
    with TestClient(app) as client:
        now = int(time.time())
        payload = {'sub':ADMIN_USERNAME,'role':'admin','epoch':auth._ADMIN_STATE['session_epoch'],
                   'iat':now,'exp':now+1000}
        for key, bad in [('sub','someone-else'),('role','viewer'),('epoch','old-epoch'),
                         ('iat',now+3600),('exp',now-1)]:
            forged = sign({**payload,key:bad})
            assert client.get('/api/auth/me', headers={'Authorization':'Bearer '+forged}).status_code == 401
        token = sign(payload)
        assert client.get('/api/auth/me', headers={'Authorization':'Bearer '+token}).status_code == 200
        assert client.get('/api/auth/me', headers={'Authorization':'Bearer '+token+'x'}).status_code == 401
        # URLs are not a safe place for secrets: even a valid query token is rejected.
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect('/ws/live?token='+token):
                pass
        assert exc.value.code == 4401
        client.cookies.set('ff_admin_session', token)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect('/ws/live', headers={'Origin':'https://attacker.example'}):
                pass
        assert exc.value.code == 4401
        with client.websocket_connect('/ws/live', headers={'Origin':'http://testserver'}) as ws:
            assert ws.receive_json()['type'] == 'dashboard_sync'


def test_cross_origin_write_rejected_even_with_admin_cookie(isolated_auth_db):
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={
            'username':ADMIN_USERNAME,'password':ADMIN_PASSWORD}).status_code == 200
        assert client.get('/api/auth/me').status_code == 200
        response = client.post('/api/auth/change-password',headers={'Origin':'https://other.example'},
            json={'current_password':ADMIN_PASSWORD,'new_password':'ShouldNotBeSaved123!'})
        assert response.status_code == 403
        response = client.post('/api/auth/change-password',headers={'Sec-Fetch-Site':'cross-site'},
            json={'current_password':ADMIN_PASSWORD,'new_password':'ShouldNotBeSaved123!'})
        assert response.status_code == 403
        assert auth.verify_admin_credentials(ADMIN_USERNAME, ADMIN_PASSWORD)
        assert client.get('/api/auth/me').headers['Cache-Control'] == 'no-store'


def test_password_change_persists_and_revokes_sessions_after_restart(isolated_auth_db):
    with TestClient(app) as client:
        login = client.post('/api/auth/login', json={'username':ADMIN_USERNAME,'password':ADMIN_PASSWORD})
        assert login.status_code == 200
        assert 'token' not in login.json()  # only an HttpOnly cookie; not a JS-readable token
        assert 'httponly' in login.headers['set-cookie'].lower()
        before = client.cookies.get('ff_admin_session')
        assert client.get('/api/auth/me').status_code == 200
        assert client.post('/api/auth/change-password',json={
            'current_password':ADMIN_PASSWORD,'new_password':'TestingNewAdminPassword!927'}).status_code == 200
        assert client.get('/api/auth/me').status_code == 401
        assert not auth.verify_session_token(before)
        assert db.get_or_create_admin_auth('bad','bad')[1] == auth._ADMIN_STATE['session_epoch']
        # Simulate module state reloading on restart: the saved DB hash wins over bootstrap env.
        auth._ADMIN_STATE['password_hash'] = auth._ph.hash(ADMIN_PASSWORD)
        auth._ADMIN_STATE['session_epoch'] = 'fresh-process'
        auth.init_admin_auth()
        assert not auth.verify_session_token(before)
        assert not auth.verify_admin_credentials(ADMIN_USERNAME, ADMIN_PASSWORD)
        assert auth.verify_admin_credentials(ADMIN_USERNAME, 'TestingNewAdminPassword!927')
        client.cookies.clear()
        assert client.post('/api/auth/login',json={'username':ADMIN_USERNAME,
                'password':'TestingNewAdminPassword!927'}).status_code == 200


def test_logout_revokes_previous_session(isolated_auth_db):
    with TestClient(app) as client:
        login = client.post('/api/auth/login', json={'username': ADMIN_USERNAME,'password':ADMIN_PASSWORD})
        assert login.status_code == 200
        token = client.cookies.get('ff_admin_session')
        assert client.post('/api/auth/logout').status_code == 200
        assert not auth.verify_session_token(token)
        assert client.get('/api/dashboard/overview',headers={'Authorization':'Bearer '+token}).status_code == 401
        auth._ADMIN_STATE['session_epoch'] = 'temporary-reset'
        auth.init_admin_auth()
        assert not auth.verify_session_token(token)


def test_every_private_api_route_has_admin_dependency():
    from app.auth import require_admin
    public = {'/api/public/overview','/api/auth/login','/api/auth/logout'}
    for route in app.routes:
        if not getattr(route,'path','').startswith('/api/') or route.path in public:
            continue
        assert any(dep.call is require_admin for dep in route.dependant.dependencies), route.path
