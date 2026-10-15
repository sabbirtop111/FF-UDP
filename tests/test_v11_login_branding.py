"""Login vs standalone preview, exact branding and offline UI interaction checks."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import auth, database as db
from app.config import ADMIN_USERNAME, ADMIN_PASSWORD
from app.main import app

ROOT=Path(__file__).resolve().parents[1]
FULL='ＧＡＭＥＲ-ＣＯＲＰＯＲＡＴＩＯＮ'
SHORT='ＧＡＭＥＲ-ＣＯＲＰ'
CREDIT='Developer ! ＧＡＭＥＲ ＳＡＢＢＩＲ'


@pytest.fixture
def isolated_auth_db(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'login.db'))
    original=auth._ADMIN_STATE.copy()
    yield
    auth._ADMIN_STATE.update(original)
    auth._FAILED_LOGINS.clear()


def test_real_login_endpoint_and_authenticated_overview(isolated_auth_db):
    with TestClient(app) as client:
        assert client.get('/admin').status_code==200
        assert client.get('/static/js/app.js').status_code==200
        assert client.get('/api/dashboard/overview').status_code==401
        login=client.post('/api/auth/login',json={'username':ADMIN_USERNAME,'password':ADMIN_PASSWORD})
        assert login.status_code==200
        assert 'httponly' in login.headers['set-cookie'].lower()
        assert client.get('/api/auth/me').status_code==200
        assert client.get('/api/dashboard/overview').status_code==200


def test_preview_can_not_look_like_a_working_login():
    live=(ROOT/'static/index.html').read_text()
    preview=(ROOT/'ADMIN_LAYOUT_PREVIEW.html').read_text()
    workspace=(ROOT/'ADMIN_WORKSPACE_PREVIEW.html').read_text()
    assert re.search(r'<script src="/static/js/app\.js\?v=[A-Za-z0-9.\-_]+" defer></script>', live), \
        "the console script must stay deferred and cache-busted"
    assert '<button id="login-submit" class="button button-dark" type="submit">' in live
    assert 'id="login-submit"' in live and 'id="login-error"' in live
    assert 'id="login-status"' in live
    assert 'SIGN-IN DISABLED HERE' in preview
    assert 'login is disabled in this file' in preview
    assert 'type="submit" disabled' in preview
    assert 'app.js' not in preview
    assert 'CONTROLS ARE INACTIVE' in workspace
    assert 'Authentication required' in workspace
    assert 'FICTIONAL DESIGN PREVIEW' in (ROOT/'USER_PAGE_LAYOUT_PREVIEW.html').read_text()


def test_full_short_and_developer_branding_are_exact():
    for page in ('static/index.html','static/public.html'):
        text=(ROOT/page).read_text()
        assert FULL in text
        assert SHORT in text
        assert CREDIT in text
        assert '/static/css/command-v11.css' in text


def test_actual_admin_js_button_responds_and_errors_visible():
    if not shutil.which('node'):
        pytest.skip('Node is optional for offline frontend smoke tests')
    result=subprocess.run(['node',str(ROOT/'tests/check_login_ui.cjs')],
                          cwd=ROOT,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert 'Admin UI login: success' in result.stdout
