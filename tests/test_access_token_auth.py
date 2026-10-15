"""Offline Access Token auth coverage. Uses a dummy token and mocked HTTPS/game responses only."""
import asyncio
import re
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import lv
from app import bot_manager as bm
from app import database as db
from app.auth import create_session_token
from app.crypto_vault import redact_sensitive
from app.main import app
from app.schemas import AccountCreateRequest

DUMMY_ACCESS_TOKEN = "offline-test-access-token-not-valid"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", str(tmp_path / "access-auth.db"))
    monkeypatch.setattr(lv, "get_device_for_account", lambda uid: {"uid": uid})
    db.init_db()
    return db


def _admin_headers():
    return {"Authorization": f"Bearer {create_session_token('admin')}"}


def test_create_schema_keeps_both_authentication_modes():
    guest = AccountCreateRequest(uid="guest_001", password="valid-password")
    token = AccountCreateRequest(auth_type="access_token", access_token=DUMMY_ACCESS_TOKEN)
    assert guest.auth_type == "uid_password"
    assert guest.uid == "guest_001"
    assert token.auth_type == "access_token"
    assert token.uid is None
    with pytest.raises(ValueError, match="Access Token"):
        AccountCreateRequest(auth_type="access_token")


def test_token_account_is_encrypted_server_side_and_not_serialized(isolated_db):
    with TestClient(app) as client:
        response = client.post(
            "/api/accounts",
            headers=_admin_headers(),
            json={
                "auth_type": "access_token",
                "access_token": DUMMY_ACCESS_TOKEN,
                "display_name": "Token Test",
                "region": "BD",
            },
        )
        assert response.status_code == 201, response.text
        result = response.json()
        account = result["account"]
        assert account["auth_type"] == "access_token"
        assert account["has_credentials"] is True
        assert account["has_access_token"] is True
        assert DUMMY_ACCESS_TOKEN not in response.text
        assert "access_token" not in account
        exported = client.get("/api/accounts/export/metadata?format=json", headers=_admin_headers())
        public_cards = client.get("/api/public/overview")
    assert exported.status_code == 200
    assert exported.json()["accounts"][0]["auth_type"] == "access_token"
    assert DUMMY_ACCESS_TOKEN not in exported.text
    assert DUMMY_ACCESS_TOKEN not in public_cards.text

    internal = db.get_account_credentials_internal(account["id"])
    assert internal["access_token_plain"] == DUMMY_ACCESS_TOKEN
    assert internal["password_plain"] == ""
    with sqlite3.connect(db.DATABASE_PATH) as conn:
        encrypted = conn.execute(
            "SELECT encrypted_access_token FROM accounts WHERE id = ?", (account["id"],)
        ).fetchone()[0]
    assert encrypted != DUMMY_ACCESS_TOKEN
    assert encrypted
    assert redact_sensitive(f'{{"jwt":"example-jwt", "open_id":"private-id"}}') == (
        '{"jwt":"[REDACTED]", "open_id":"[REDACTED]"}'
    )
    message = "Access Token → JWT exchange failed after 3 attempts."
    assert redact_sensitive(message) == message


def test_token_metadata_import_preserves_method_but_never_imports_a_token(isolated_db):
    with TestClient(app) as client:
        response = client.post(
            "/api/accounts/import/metadata",
            headers=_admin_headers(),
            json={
                "default_region": "BD",
                "accounts": [{
                    "uid": "token_metadata_1",
                    "display_name": "Imported token account",
                    "auth_type": "access_token",
                    "region": "BD",
                }],
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["imported_count"] == 1
    account = next(a for a in db.list_accounts() if a["uid"] == "token_metadata_1")
    internal = db.get_account_credentials_internal(account["id"])
    assert account["auth_type"] == "access_token"
    assert account["status_message"] == "Imported metadata — re-enter Access Token before starting worker"
    assert internal["access_token_plain"] == ""


def test_update_switch_requires_new_credential_and_clears_old_secret(isolated_db):
    original = db.create_account("guest_002", "old-password", display_name="Switch")
    with TestClient(app) as client:
        missing = client.put(
            f"/api/accounts/{original['id']}",
            headers=_admin_headers(),
            json={"auth_type": "access_token"},
        )
        assert missing.status_code == 400
        assert db.get_account_by_id(original["id"])["auth_type"] == "uid_password"

        switched = client.put(
            f"/api/accounts/{original['id']}",
            headers=_admin_headers(),
            json={"auth_type": "access_token", "access_token": DUMMY_ACCESS_TOKEN},
        )
        assert switched.status_code == 200, switched.text
        assert DUMMY_ACCESS_TOKEN not in switched.text
        assert switched.json()["account"]["auth_type"] == "access_token"

        back_without_password = client.put(
            f"/api/accounts/{original['id']}",
            headers=_admin_headers(),
            json={"auth_type": "uid_password"},
        )
        assert back_without_password.status_code == 400

        back = client.put(
            f"/api/accounts/{original['id']}",
            headers=_admin_headers(),
            json={"auth_type": "uid_password", "uid": "guest_002", "password": "new-password"},
        )
        assert back.status_code == 200, back.text

    internal = db.get_account_credentials_internal(original["id"])
    assert internal["auth_type"] == "uid_password"
    assert internal["password_plain"] == "new-password"
    assert internal["access_token_plain"] == ""

    token_account = db.create_account(
        None, "", display_name="Token with generated key", auth_type="access_token",
        access_token=DUMMY_ACCESS_TOKEN,
    )
    with TestClient(app) as client:
        switched = client.put(
            f"/api/accounts/{token_account['id']}",
            headers=_admin_headers(),
            json={"auth_type": "uid_password", "uid": "new_guest_003", "password": "new-password"},
        )
    assert switched.status_code == 200, switched.text
    assert switched.json()["account"]["uid"] == "new_guest_003"
    converted = db.get_account_credentials_internal(token_account["id"])
    assert converted["password_plain"] == "new-password"
    assert converted["access_token_plain"] == ""


def test_admin_form_has_two_methods_without_prefilling_token():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "static/js/app.js").read_text()
    html = (root / "static/index.html").read_text()
    assert re.search(r'/static/js/app\.js\?v=[A-Za-z0-9.\-_]+', html), 'app.js must load with a cache-busting version'
    assert "Authentication method" in js
    assert "UID + password" in js
    assert "Access Token → JWT API" in js
    assert 'type="password" name="access_token"' in js
    assert "Leave blank to keep the current encrypted token." in js
    assert "data-required-on-create" in js


def test_access_jwt_helper_posts_json_and_sanitizes_result(monkeypatch):

    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "success",
                "jwt": "mock-game-jwt-value",
                "open_id": "mock-open-id",
                "platform_type_used": 8,
                "player_info": {
                    "account_id": "12345678",
                    "region": "BD",
                    "nickname": "Mock Player",
                },
            }

    async def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(lv.ACCESS_API_CLIENT, "post", fake_post)
    result = asyncio.run(lv.exchange_access_token_for_jwt(DUMMY_ACCESS_TOKEN))
    assert captured["url"] == lv.ACCESS_JWT_API_URL
    assert captured["json"] == {"access_token": DUMMY_ACCESS_TOKEN}
    assert "access_token=" not in captured["url"]
    assert result["jwt"] == "mock-game-jwt-value"
    assert result["open_id"] == "mock-open-id"
    assert result["platform_type_used"] == 8
    assert result["account_id"] == "12345678"
    assert result["region"] == "BD"
    assert "access_token" not in result


def test_majorlogin_builder_keeps_legacy_platform_and_can_honor_api_platform(monkeypatch):
    captured = {}

    async def capture_encrypt(data, key, iv):
        captured["data"] = data
        return b"encrypted-mock"

    monkeypatch.setattr(lv, "aes_encrypt", capture_encrypt)
    legacy = asyncio.run(lv.build_majorlogin_payload("oid", "token", 8, "client", {}))
    legacy_proto = lv.thunderFF_pb2.MajorLoginReq()
    legacy_proto.ParseFromString(captured["data"])
    assert legacy == b"encrypted-mock"
    assert legacy_proto.login_open_id_type == 4

    api = asyncio.run(lv.build_majorlogin_payload(
        "oid", "token", 8, "client", {}, honor_platform=True
    ))
    api_proto = lv.thunderFF_pb2.MajorLoginReq()
    api_proto.ParseFromString(captured["data"])
    assert api == b"encrypted-mock"
    assert api_proto.login_open_id_type == 8
    assert api_proto.open_id_type == "8"

    direct = asyncio.run(lv.build_majorlogin_payload(
        "", "mock-direct-game-jwt", 8, "client", {}, honor_platform=True
    ))
    direct_proto = lv.thunderFF_pb2.MajorLoginReq()
    direct_proto.ParseFromString(captured["data"])
    assert direct == b"encrypted-mock"
    assert direct_proto.open_id == ""
    assert direct_proto.access_token == "mock-direct-game-jwt"
    assert direct_proto.login_open_id_type == 8


def test_access_jwt_helper_accepts_direct_jwt_without_open_id(monkeypatch):
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "status": "success",
                "jwt": "eyJmYWtlLWhlYWRlciJ9.eyJhY2NvdW50X2lkIjoiMTIzNDU2NzgifQ.signature",
                "open_id": None,
                "platform_type_used": None,
                "open_id_source": "jwt_provided_directly",
                "player_info": {
                    "account_id": "12345678",
                    "region": "BD",
                    "platform_id": 8,
                    "nickname": "Mock Player",
                },
            }

    async def fake_post(url, **kwargs):
        assert kwargs["json"] == {"access_token": DUMMY_ACCESS_TOKEN}
        return Response()

    monkeypatch.setattr(lv.ACCESS_API_CLIENT, "post", fake_post)
    result = asyncio.run(lv.exchange_access_token_for_jwt(DUMMY_ACCESS_TOKEN))
    assert result["auth_mode"] == "direct_jwt"
    assert result["open_id"] == ""
    assert result["platform_type_used"] == 8
    assert result["account_id"] == "12345678"
    assert result["region"] == "BD"
    assert "access_token" not in result


def test_access_jwt_helper_refuses_http_endpoint(monkeypatch):
    async def must_not_post(*args, **kwargs):
        raise AssertionError("Access Token must not be sent over plain HTTP")

    monkeypatch.setattr(lv, "ACCESS_JWT_API_URL", "http://example.test/access-jwt")
    monkeypatch.setattr(lv.ACCESS_API_CLIENT, "post", must_not_post)
    with pytest.raises(lv.AccessJWTExchangeError, match="must use HTTPS"):
        asyncio.run(lv.exchange_access_token_for_jwt(DUMMY_ACCESS_TOKEN))


def test_access_jwt_helper_reports_sanitized_open_id_failure(monkeypatch):
    class Response:
        status_code = 422

    async def fake_post(*args, **kwargs):
        return Response()

    monkeypatch.setattr(lv.ACCESS_API_CLIENT, "post", fake_post)
    with pytest.raises(lv.AccessJWTExchangeError, match="extract an Open ID.*HTTP 422") as error:
        asyncio.run(lv.exchange_access_token_for_jwt(DUMMY_ACCESS_TOKEN))
    assert DUMMY_ACCESS_TOKEN not in str(error.value)


@pytest.mark.parametrize("direct_jwt", [False, True])
def test_access_token_worker_selects_region_login_for_raw_token_and_direct_jwt_path(
    isolated_db, monkeypatch, direct_jwt
):

    account = db.create_account(
        None,
        "",
        display_name="Mocked token worker",
        region="BD",
        auth_type="access_token",
        access_token=DUMMY_ACCESS_TOKEN,
    )
    manager = bm.BotManager()
    calls = {}

    async def no_broadcast(*args, **kwargs):
        return None

    async def version_config(region):
        return ("release-test", "client-test", "https://game.example.test/")

    async def exchange(token):
        assert token == DUMMY_ACCESS_TOKEN
        return {
            "jwt": "mock-game-jwt-value",
            "open_id": "" if direct_jwt else "mock-open-id",
            "auth_mode": "direct_jwt" if direct_jwt else "access_token",
            "platform_type_used": 8,
            "account_id": "12345678",
            "region": "BD",
            "nickname": "Mock Player",
            "player_info": {"account_id": "12345678", "platform_id": 8},
        }

    async def build_payload(open_id, access_token, platform, client_version, device_info, honor_platform=False):
        calls["payload_args"] = (open_id, access_token, platform, honor_platform)
        return b"mock-encrypted-login-payload"

    async def region_majorlogin(data, release_version, server_url):
        if direct_jwt:
            raise AssertionError("Direct-JWT mode must not guess a missing Open ID")
        calls["majorlogin"] = (data, release_version, server_url)
        return SimpleNamespace(
            account_id=12345678,
            region="BD",
            token="mock-region-majorlogin-jwt",
            server_time=1,
            aes_ak=lv.AES_KEY,
            iv_i=lv.AES_IV,
            url=server_url,
        )

    async def getlogin(data, base_url, token, release_version):
        calls["getlogin"] = (data, base_url, token, release_version)
        proto = SimpleNamespace(
            account_id=12345678,
            nickname="Mock Player",
            functional_addrs="127.0.0.1:9000",
            informational_addrs="",
        )
        return proto, {"mock": True}

    def get_proto_field(_data, field_no, default=None):
        return {6: 10, 7: 2500, 4: "Mock Player", 3: "BD"}.get(field_no, default)

    async def run_worker(account_data, label):
        calls["account_data"] = account_data
        calls["worker_label"] = label
        raise asyncio.CancelledError()

    monkeypatch.setattr(manager, "broadcast_state_update", no_broadcast)
    monkeypatch.setattr(lv, "version_config", version_config)
    monkeypatch.setattr(lv, "exchange_access_token_for_jwt", exchange)
    monkeypatch.setattr(lv, "build_majorlogin_payload", build_payload)
    monkeypatch.setattr(lv, "send_majorlogin", region_majorlogin)
    monkeypatch.setattr(lv, "send_getlogin", getlogin)
    monkeypatch.setattr(lv, "get_proto_field", get_proto_field)
    monkeypatch.setattr(lv, "run_account_worker", run_worker)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(manager._account_worker_lifecycle(account["id"]))

    expected_open_id = "" if direct_jwt else "mock-open-id"
    expected_session_jwt = "mock-game-jwt-value" if direct_jwt else "mock-region-majorlogin-jwt"
    assert calls["payload_args"] == (expected_open_id, DUMMY_ACCESS_TOKEN, 8, True)
    assert calls["getlogin"][2] == expected_session_jwt
    if direct_jwt:
        assert "majorlogin" not in calls
    else:
        assert calls["majorlogin"] == (
            b"mock-encrypted-login-payload", "release-test", "https://game.example.test/"
        )
    account_data = calls["account_data"]
    assert account_data["account_id"] == 12345678
    assert account_data["auth_type"] == "access_token"
    assert account_data["auth_password"] == ""
    assert account_data["access_token"] == ""
    assert account_data["token"] == expected_session_jwt
    assert DUMMY_ACCESS_TOKEN not in str(account_data)
