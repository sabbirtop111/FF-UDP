"""Run the jsdom feature harnesses from pytest (skipped when jsdom is not installed).

The harnesses load the real static/index.html + app.js + public.js, answer every API
call with canned data and assert that each feature actually renders and reacts —
the equivalent of a person clicking through the console.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESSES = ['tests/check_admin_features.cjs', 'tests/check_public_and_flows.cjs']


def _jsdom_ready() -> bool:
    return (ROOT / 'node_modules' / 'jsdom').exists() and shutil.which('node') is not None


@pytest.mark.skipif(not _jsdom_ready(), reason="jsdom is not installed in this environment")
@pytest.mark.parametrize('harness', HARNESSES)
def test_browser_features_render_and_react(harness):
    result = subprocess.run(['node', harness], cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    tail = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, tail[-2000:]
    assert '0 failed' in tail, tail[-1200:]


def test_console_assets_are_cache_busted():
    """Every script/style the console loads must carry a version, or a deploy can look stale."""
    import re
    for page in ('static/index.html', 'static/public.html'):
        html = (ROOT / page).read_text()
        refs = re.findall(r'(?:src|href)="(/static/(?:js|css)/[^"]+)"', html)
        assert refs, page
        for ref in refs:
            assert '?v=' in ref, f"{page} loads {ref} without a cache-busting version"
    build_tag = (ROOT / 'static/index.html').read_text()
    assert 'build-tag' in build_tag, "the console must show which build it is running"


def test_static_responses_are_never_stale():
    """A new deployment has to be visible on the next page load."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        for path in ('/', '/admin'):
            res = client.get(path)
            assert res.status_code == 200
            assert 'no-store' in res.headers.get('cache-control', ''), path
        asset = client.get('/static/js/app.js')
        assert asset.status_code == 200
        assert 'no-cache' in asset.headers.get('cache-control', '')
        health = client.get('/health').json()
        assert health.get('build'), "liveness probe reports the running build"


def test_health_and_public_payload_shapes_stay_stable():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        public = client.get('/api/public/overview').json()
        assert set(public.keys()) == {'observed_at', 'statuses', 'bots'}
        assert json.dumps(public).find('Cache-Control') == -1
