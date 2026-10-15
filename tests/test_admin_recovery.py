"""Operator-only credential recovery never publishes secrets or deletes Bot data."""
from pathlib import Path
import secrets

import pytest
from fastapi.testclient import TestClient

from app import admin_access, auth, database as db
from app.main import app


@pytest.fixture
def isolated_recovery(tmp_path,monkeypatch):
    path=tmp_path/'saved.db'
    monkeypatch.setattr(db,'DATABASE_PATH',str(path))
    monkeypatch.setattr(admin_access,'DATABASE_PATH',str(path))
    mock_env=tmp_path/'.env'
    mock_env.write_text('ADMIN_PASSWORD=PRIVATE-TEST-VALUE\n')
    monkeypatch.setattr(admin_access,'ENV_PATH',mock_env)
    monkeypatch.setattr(admin_access,'ADMIN_PASSWORD','recovered-private-test-password-3492')
    original=auth._ADMIN_STATE.copy()
    yield path,mock_env
    auth._ADMIN_STATE.update(original)
    auth._FAILED_LOGINS.clear()


def test_saved_db_mismatch_can_be_reset_without_losing_bot_or_exposing_secret(isolated_recovery,capsys,monkeypatch):
    path,env=isolated_recovery
    with TestClient(app) as client:
        bot=db.create_account('game-login-private','game-secret-private',display_name='Saved Bot')
        assert admin_access.configured_password_matches_saved() is False
        original=auth.create_session_token(auth._ADMIN_STATE['username'])
        assert client.get('/api/auth/me',headers={'Authorization':'Bearer '+original}).status_code==200
    backup=admin_access.reset_from_config()
    assert backup is not None and Path(backup).is_file()
    assert (Path(backup).stat().st_mode & 0o077)==0
    assert admin_access.configured_password_matches_saved() is True
    assert db.get_account_by_id(bot['id'])['display_name']=='Saved Bot'
    assert db.get_account_by_id(bot['id'])['uid']=='game-login-private'
    # In production both modules load the same rotated environment secret.
    monkeypatch.setattr(auth,'ADMIN_PASSWORD',admin_access.ADMIN_PASSWORD)
    auth.init_admin_auth()
    assert not auth.verify_session_token(original)
    assert auth.verify_admin_credentials(auth._ADMIN_STATE['username'],admin_access.ADMIN_PASSWORD)
    assert admin_access.reset_from_config() is None
    with TestClient(app) as client:
        login=client.post('/api/auth/login',json={
            'username':auth._ADMIN_STATE['username'],'password':admin_access.ADMIN_PASSWORD})
        assert login.status_code==200
        assert client.get('/api/auth/me').status_code==200
    assert admin_access.ADMIN_PASSWORD not in capsys.readouterr().out


def test_duplicate_env_lines_rejected_before_any_reset(isolated_recovery):
    path,env=isolated_recovery
    db.init_db()
    auth.init_admin_auth()
    env.write_text('ADMIN_PASSWORD=first\nADMIN_PASSWORD=second\n')
    with pytest.raises(RuntimeError,match='Duplicate ADMIN_PASSWORD'):
        admin_access.reset_from_config()
    assert not list(path.parent.glob('*.auth-backup-*.db'))


def test_status_check_does_not_create_a_missing_db(isolated_recovery):
    path,_=isolated_recovery
    assert admin_access.configured_password_matches_saved() is None
    assert not path.exists()


def test_explicit_one_boot_hosted_reset_uses_configured_secret(isolated_recovery,monkeypatch):
    from app import main as main_module
    path,_=isolated_recovery
    db.init_db()
    auth.init_admin_auth()
    bot=db.create_account('existing-private-uid','existing-private-password',display_name='Keep Me')
    # A Settings change uses the saved DB hash while the bootstrap env is unchanged.
    db.save_admin_auth(auth._ph.hash('changed-in-settings-password-927'),secrets.token_hex(16))
    monkeypatch.setattr(admin_access,'ADMIN_PASSWORD',auth.ADMIN_PASSWORD)
    monkeypatch.setattr(main_module,'ADMIN_PASSWORD_RESET_ON_START',True)
    with TestClient(app) as client:
        login=client.post('/api/auth/login',json={
            'username':auth._ADMIN_STATE['username'],
            'password':auth.ADMIN_PASSWORD})
        assert login.status_code==200
        assert client.get('/api/dashboard/overview').status_code==200
    assert db.get_account_by_id(bot['id'])['display_name']=='Keep Me'
    assert len(list(path.parent.glob('*.auth-backup-*.db')))==1


def test_blank_template_password_row_does_not_block_reset(isolated_recovery):
    path,env=isolated_recovery
    db.init_db()
    auth.init_admin_auth()
    env.write_text('ADMIN_PASSWORD=\nADMIN_PASSWORD=private-nonempty-value\n')
    backup=admin_access.reset_from_config()
    assert backup is not None
    assert admin_access.configured_password_matches_saved() is True


def test_startup_banner_never_advertises_obsolete_default_password():
    source=(Path(__file__).resolve().parents[1]/'run.py').read_text()
    assert 'Default Login' not in source
    assert 'admin / admin123' not in source
    assert 'Admin password source:' in source
