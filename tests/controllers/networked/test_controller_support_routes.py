"""Save logs on the networked controller: same routes as the OWL unit, open
to the rig LAN like /api/fleet (a token would stop nobody: reserve hands one
out). Even a locked-down install (DASHBOARD_OPEN unset) must let the phone
build and download, and nothing else under a lookalike path."""

import io
import zipfile
from unittest.mock import patch

import pytest

LAN = {'X-Real-IP': '192.168.1.50'}


def _fake_build(**kwargs):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zf:
        zf.writestr('summary.txt', 'ok')
    return 'owl_logs_owl-controller_20261029-1200.zip', buffer.getvalue()


@pytest.fixture
def client(networked_test_client):
    client, mock_ctrl = networked_test_client
    mock_ctrl.fleet_roster = None
    import controller.networked.networked as net_mod
    with patch.object(net_mod.support_bundle, 'build_bundle', side_effect=_fake_build) as build, \
            patch.object(net_mod, 'DASHBOARD_OPEN', False):
        yield client, build


@pytest.mark.unit
class TestControllerSupportBundle:

    def test_phone_on_the_rig_lan_can_build_and_download(self, client):
        client, build = client
        resp = client.post('/api/support/bundle', json={'app': {'events': []}}, headers=LAN)
        data = resp.get_json()
        assert resp.status_code == 200, resp.get_data(as_text=True)
        assert data['subject'].startswith('OWL controller logs ')
        assert build.call_args.kwargs['units'][0] == 'owl-controller'
        got = client.get(data['url'], headers=LAN)
        assert got.status_code == 200 and got.mimetype == 'application/zip'

    def test_lookalike_paths_stay_locked(self, client):
        client, _ = client
        assert client.get('/api/supportx', headers=LAN).status_code == 403
        assert client.post('/api/support/bundlex', headers=LAN).status_code == 403

    def test_controller_ini_redaction_is_used(self, client):
        client, build = client
        client.post('/api/support/bundle', json={}, headers=LAN)
        assert build.call_args.kwargs['controller_ini_path']
