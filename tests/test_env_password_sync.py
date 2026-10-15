"""A changed deployment password can fix 401 without making old env secrets bypass UI changes."""
import secrets

import pytest
from fastapi.testclient import TestClient

from app import auth, database as db
from app.main import app


@pytest.fixture
def isolated_auth(tmp_path,monkeypatch):
    original=auth._ADMIN_STATE.copy()
    monkeypatch.setattr(db,'DATABASE_PATH',str(tmp_path/'saved-bots.db'))
    db.init_db()
    yield
    auth._ADMIN_STATE.update(original)
    auth._FAILED_LOGINS.clear()


def test_legacy_saved_hash_migrates_to_configured_env_password_without_deleting_bots(isolated_auth,monkeypatch):
    bot=db.create_account('PRIVATE-UID-LEGACY','PRIVATE-GUEST-PASS',display_name='Keep Bot')
    older=auth._ph.hash('older-admin-password-123')
    epoch=secrets.token_hex(16)
    with db.get_connection() as conn:
        conn.execute('INSERT INTO admin_auth (id,password_hash,session_epoch) VALUES (1,?,?)',(older,epoch))
    configured='new-private-configured-admin-password-769'
    monkeypatch.setattr(auth,'ADMIN_PASSWORD',configured)
    auth.init_admin_auth()
    assert auth.verify_admin_credentials(auth._ADMIN_STATE['username'],configured)
    assert not auth.verify_admin_credentials(auth._ADMIN_STATE['username'],'older-admin-password-123')
    assert auth._ADMIN_STATE['session_epoch'] != epoch
    with db.get_connection() as conn:
        saved=conn.execute('SELECT bootstrap_fingerprint FROM admin_auth WHERE id=1').fetchone()[0]
    assert saved and configured not in saved
    assert db.get_account_by_id(bot['id'])['uid']=='PRIVATE-UID-LEGACY'
    with TestClient(app) as client:
        resp=client.post('/api/auth/login',json={'username':auth._ADMIN_STATE['username'],'password':configured})
        assert resp.status_code==200
        assert client.get('/api/auth/me').status_code==200
        assert client.get('/api/dashboard/overview').status_code==200


def test_changed_env_rotates_password_and_revokes_old_session(isolated_auth,monkeypatch):
    original=auth.ADMIN_PASSWORD
    auth.init_admin_auth()
    previous=auth.create_session_token(auth._ADMIN_STATE['username'])
    changed='rotated-environment-password-98765'
    monkeypatch.setattr(auth,'ADMIN_PASSWORD',changed)
    auth.init_admin_auth()  # a new server process would reload the changed env
    assert auth.verify_admin_credentials(auth._ADMIN_STATE['username'],changed)
    assert not auth.verify_admin_credentials(auth._ADMIN_STATE['username'],original)
    assert auth.verify_session_token(previous) is None


def test_settings_password_survives_restart_until_env_itself_changes(isolated_auth,monkeypatch):
    configured='environment-secret-first-13579'
    monkeypatch.setattr(auth,'ADMIN_PASSWORD',configured)
    auth.init_admin_auth()
    settings='settings-only-secret-24680'
    assert auth.update_admin_password(configured,settings)
    auth.init_admin_auth()  # same configuration -> keep Settings password
    assert auth.verify_admin_credentials(auth._ADMIN_STATE['username'],settings)
    assert not auth.verify_admin_credentials(auth._ADMIN_STATE['username'],configured)
    rotated='environment-newer-secret-13579'
    monkeypatch.setattr(auth,'ADMIN_PASSWORD',rotated)
    auth.init_admin_auth()
    assert auth.verify_admin_credentials(auth._ADMIN_STATE['username'],rotated)
    assert not auth.verify_admin_credentials(auth._ADMIN_STATE['username'],settings)


def test_process_environment_overrides_dotenv_without_printing_secret(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    secret='deployment-secret-private-for-test-7824'
    env=os.environ.copy()
    env['ADMIN_PASSWORD']=secret
    result=subprocess.run([sys.executable,'-c',
        'from app.config import ADMIN_PASSWORD, ADMIN_PASSWORD_ENV_OVERRIDE; print(ADMIN_PASSWORD_ENV_OVERRIDE, len(ADMIN_PASSWORD)>12)'],
        cwd=root,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==0
    assert result.stdout.strip()=='True True'
    assert secret not in result.stdout+result.stderr
