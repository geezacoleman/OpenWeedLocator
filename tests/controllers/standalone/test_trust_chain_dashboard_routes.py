"""Trust chain MVP on the standalone dashboard: GET /api/identity and
POST /api/settings/cloud/provision (token-guarded, same helper as first boot).

Fixtures are the settings-route ones (tests/test_settings_routes.py) so the
blueprint is built exactly as the dashboard builds it.
"""

from unittest.mock import MagicMock, patch

import pytest

# tests/ is not a package; pytest puts it on sys.path (rootdir/testpaths).
from test_settings_routes import (  # noqa: F401  (fixtures by name)
    HEADERS, TOKEN, build_client, clear_timers, mgr, nm)

GOOD = {'broker_host': 'mqtt.noktura.tech', 'broker_port': 8883,
        'device_id': 'owl-2026-000001', 'username': 'owl-2026-000001',
        'password': 'y' * 32, 'portal_url': 'https://app.noktura.tech'}

HELPER = '/usr/local/sbin/owl-cloud-provision'


def ok_runner():
    return MagicMock(return_value=MagicMock(returncode=0, stdout='', stderr=''))


@pytest.mark.unit
class TestIdentityRoute:
    def test_identity_is_public_and_includes_pem(self, mgr, tmp_path):
        client = build_client(mgr, tmp_path, token=TOKEN)
        fake = {'hw_id': 'abc', 'tier': 'otp', 'fingerprint': 'f' * 64,
                'noktura_serial': 'OWL-2026-000001',
                'public_key_pem': '-----BEGIN PUBLIC KEY-----\nAA\n-----END PUBLIC KEY-----'}
        with patch('controller.standalone.settings_routes.read_identity', return_value=fake) as ri:
            resp = client.get('/api/identity')   # no token header on purpose
        assert resp.status_code == 200
        assert resp.get_json()['identity']['public_key_pem'].startswith('-----BEGIN PUBLIC KEY')
        ri.assert_called_once_with(include_pem=True)


@pytest.mark.unit
class TestCloudProvisionRoute:
    def test_requires_token_when_set(self, mgr, tmp_path):
        runner = ok_runner()
        client = build_client(mgr, tmp_path, token=TOKEN, runner=runner)
        assert client.post('/api/settings/cloud/provision', json=GOOD).status_code == 403
        assert client.post('/api/settings/cloud/provision', json=GOOD,
                           headers={'X-Device-Token': 'wrong'}).status_code == 403
        runner.assert_not_called()

    def test_runs_helper_with_password_on_stdin(self, mgr, tmp_path):
        runner = ok_runner()
        client = build_client(mgr, tmp_path, token=TOKEN, runner=runner)
        resp = client.post('/api/settings/cloud/provision', json=GOOD, headers=HEADERS)
        assert resp.status_code == 200, resp.get_json()
        argv = runner.call_args.args[0]
        assert argv == ['sudo', '-n', HELPER]
        assert 'y' * 32 not in ' '.join(argv)
        assert runner.call_args.kwargs['input'].split('\n')[1] == 'y' * 32
        assert resp.get_json()['cloud']['device_id'] == 'owl-2026-000001'

    def test_validates_before_root(self, mgr, tmp_path):
        runner = MagicMock()
        client = build_client(mgr, tmp_path, token=TOKEN, runner=runner)
        resp = client.post('/api/settings/cloud/provision',
                           json={**GOOD, 'broker_host': 'not a host'}, headers=HEADERS)
        assert resp.status_code == 400
        runner.assert_not_called()

    def test_helper_failure_reports_detail(self, mgr, tmp_path):
        runner = MagicMock(return_value=MagicMock(returncode=2, stdout='', stderr='bad device_id'))
        client = build_client(mgr, tmp_path, token=TOKEN, runner=runner)
        resp = client.post('/api/settings/cloud/provision', json=GOOD, headers=HEADERS)
        assert resp.status_code == 500
        assert resp.get_json()['detail'] == 'bad device_id'

    def test_unit_without_a_token_fails_closed(self, mgr, tmp_path):
        # O-10 (cross-repo review 2026-09-17): other routes keep the legacy
        # allow, but re-pointing the cloud bridge must not be open to anyone
        # on the network. setup.sh now gives every standalone unit a token.
        runner = ok_runner()
        client = build_client(mgr, tmp_path, token=None, runner=runner)
        resp = client.post('/api/settings/cloud/provision', json=GOOD)
        assert resp.status_code == 403
        assert resp.get_json()['error'] == 'device_token_missing'
        runner.assert_not_called()
