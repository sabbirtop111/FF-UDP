import asyncio

import pytest

import lv


DUMMY_BEARER = "offline-test-bearer-not-valid"


@pytest.mark.parametrize("status_code", [401, 403, 502])
def test_getlogindata_http_failure_reports_only_status(monkeypatch, status_code):
    captured = {}

    class Response:
        def __init__(self, code):
            self.status_code = code

    async def fake_post(url, headers, data):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = data
        return Response(status_code)

    monkeypatch.setattr(lv.client, "post", fake_post)
    with pytest.raises(lv.GetLoginDataError, match=f"HTTP {status_code}") as exc:
        asyncio.run(lv.send_getlogin(
            b"mock-encrypted-body", "https://game.example.test/", DUMMY_BEARER, "release-test"
        ))

    assert captured["url"] == "https://game.example.test/GetLoginData"
    assert captured["headers"]["Authorization"] == f"Bearer {DUMMY_BEARER}"
    assert DUMMY_BEARER not in str(exc.value)


def test_getlogindata_network_failure_hides_exception_text(monkeypatch):
    async def fake_post(*args, **kwargs):
        raise RuntimeError(f"failure with bearer {DUMMY_BEARER}")

    monkeypatch.setattr(lv.client, "post", fake_post)
    with pytest.raises(lv.GetLoginDataError, match="RuntimeError") as exc:
        asyncio.run(lv.send_getlogin(
            b"mock-encrypted-body", "https://game.example.test/", DUMMY_BEARER, "release-test"
        ))
    assert DUMMY_BEARER not in str(exc.value)
