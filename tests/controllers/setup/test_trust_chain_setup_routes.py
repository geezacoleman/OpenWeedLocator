"""Trust chain MVP on the first-boot setup API: `identity` on /setup/api/info
and POST /setup/api/cloud/provision.

The app reads `identity` to compare the unit with Noktura's record and posts
the bridge credential it received after claiming. The password must reach the
root helper on stdin only; bad payloads never reach root; the result survives
a reboot so the app can see it in /setup/api/info.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from controller.setup.firstboot_state import FirstBootState
from controller.setup.setup_app import create_app

GOOD = {'broker_host': 'mqtt.noktura.tech', 'broker_port': 8883,
        'device_id': 'owl-2026-000001', 'username': 'owl-2026-000001',
        'password': 'x' * 32, 'portal_url': 'https://app.noktura.tech'}


def completed(rc=0, stderr=''):
    return subprocess.CompletedProcess(['sudo'], rc, stdout='', stderr=stderr)


class CapturingTimer:
    pending = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.function = function
        self.daemon = False

    def start(self):
        CapturingTimer.pending.append(self)


@pytest.fixture
def nm():
    manager = MagicMock()
    manager.is_supported.return_value = True
    manager.get_hotspot_connection.return_value = 'OWL-C29B'
    manager.get_ip4.return_value = '10.42.0.1'
    manager.status.return_value = {'mode': 'hotspot', 'ssid': 'OWL-C29B',
                                   'ip': '10.42.0.1', 'hotspot_profile': 'OWL-C29B'}
    manager.scan.return_value = []
    return manager


@pytest.fixture
def cloud_runner(tmp_path):
    """Fake root helper. On success it writes [Cloud] into CONTROLLER.ini the
    way owl_cloud_provision.sh does: the setup API reports cloud status from
    that file (O-18), so a fake that skips it would test nothing real."""
    ini = tmp_path / 'CONTROLLER.ini'

    def run(argv, input=None, **kwargs):
        result = runner.return_value
        if result.returncode == 0:
            meta = json.loads(input.split(chr(10))[0])
            ini.write_text('[Cloud]{nl}enable = True{nl}device_id = {d}{nl}portal_url = {p}{nl}'
                           .format(nl=chr(10), d=meta['device_id'], p=meta['portal_url']))
        return result

    runner = MagicMock(side_effect=run, return_value=completed(0))
    return runner


def make_state(nm, tmp_path, cloud_runner=None):
    flag = tmp_path / 'owl-firstboot.flag'
    flag.touch()
    kwargs = {'cloud_runner': cloud_runner} if cloud_runner is not None else {}
    return FirstBootState(nm, flag_path=flag, state_file=tmp_path / 'state.json',
                          timer_factory=CapturingTimer, system_runner=MagicMock(),
                          exit_fn=MagicMock(), controller_ini=tmp_path / 'CONTROLLER.ini',
                          **kwargs)


@pytest.fixture
def cloud_client(nm, tmp_path, cloud_runner):
    fb = make_state(nm, tmp_path, cloud_runner)
    app = create_app(network_manager=nm, state=fb)
    app.config['TESTING'] = True
    client = app.test_client()
    # Contract 5: mutating setup routes require X-Setup-Session.
    client.environ_base['HTTP_X_SETUP_SESSION'] = client.post('/setup/api/session').get_json()['session']
    return client, fb


@pytest.mark.unit
class TestIdentityOnInfo:
    def test_info_carries_identity_and_cloud(self, cloud_client):
        client, _ = cloud_client
        with patch('controller.setup.setup_app.read_identity',
                   return_value={'hw_id': '10000000a3f1c29b', 'tier': 'otp',
                                 'fingerprint': 'f' * 64, 'noktura_serial': 'OWL-2026-000001'}):
            body = client.get('/setup/api/info').get_json()
        assert body['identity']['tier'] == 'otp'
        assert body['identity']['noktura_serial'] == 'OWL-2026-000001'
        assert body['cloud'] is None
        assert body['contract_version'] == 5   # HTTPS + setup session (3.19.0)

    def test_unit_without_identity_reports_tier_none(self, cloud_client, tmp_path):
        client, _ = cloud_client
        with patch('utils.device_identity.IDENTITY_DIR', tmp_path / 'nothing-here'):
            body = client.get('/setup/api/info').get_json()
        assert body['identity']['tier'] == 'none'
        assert body['identity']['fingerprint'] is None


@pytest.mark.unit
class TestCloudProvisionRoute:
    def test_runs_helper_with_password_on_stdin(self, cloud_client, cloud_runner):
        client, fb = cloud_client
        resp = client.post('/setup/api/cloud/provision', json=GOOD)
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body['cloud']['device_id'] == 'owl-2026-000001'
        assert 'x' * 32 not in json.dumps(body)
        argv = cloud_runner.call_args.args[0]
        assert argv == ['sudo', '-n', '/usr/local/sbin/owl-cloud-provision']
        assert 'x' * 32 not in ' '.join(argv)
        assert cloud_runner.call_args.kwargs['input'].split('\n')[1] == 'x' * 32
        # persisted + visible in info
        assert fb.cloud['device_id'] == 'owl-2026-000001'
        assert client.get('/setup/api/info').get_json()['cloud']['device_id'] == 'owl-2026-000001'
        assert json.loads(fb.state_file.read_text())['cloud']['device_id'] == 'owl-2026-000001'

    @pytest.mark.parametrize('bad', [
        {},
        {**GOOD, 'device_id': 'OWL-2026-000001'},   # serial, not slug
        {**GOOD, 'password': 'short'},
        {**GOOD, 'username': 'other'},
        {**GOOD, 'portal_url': 'http://plain'},
    ])
    def test_bad_payload_is_400_and_root_never_runs(self, cloud_client, cloud_runner, bad):
        client, _ = cloud_client
        resp = client.post('/setup/api/cloud/provision', json=bad)
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        cloud_runner.assert_not_called()

    def test_non_json_body_is_400(self, cloud_client, cloud_runner):
        client, _ = cloud_client
        resp = client.post('/setup/api/cloud/provision', data='not json',
                           content_type='text/plain')
        assert resp.status_code == 400
        cloud_runner.assert_not_called()

    def test_helper_failure_is_500_with_detail_and_nothing_persisted(self, cloud_client, cloud_runner):
        client, fb = cloud_client
        cloud_runner.return_value = completed(1, '[ERROR] mosquitto failed to start')
        resp = client.post('/setup/api/cloud/provision', json=GOOD)
        assert resp.status_code == 500
        assert 'mosquitto failed' in resp.get_json()['detail']
        assert fb.cloud is None
        assert client.get('/setup/api/info').get_json()['cloud'] is None

    def test_missing_sudoers_is_actionable(self, cloud_client, cloud_runner):
        client, _ = cloud_client
        cloud_runner.return_value = completed(1, 'sudo: a password is required')
        resp = client.post('/setup/api/cloud/provision', json=GOOD)
        assert resp.status_code == 500
        assert 'install_firstboot.sh --cloud-helper' in resp.get_json()['error']

    def test_cloud_result_survives_a_reboot(self, nm, tmp_path, cloud_runner):
        fb = make_state(nm, tmp_path, cloud_runner)
        fb.provision_cloud(GOOD)
        reloaded = make_state(nm, tmp_path)
        assert reloaded.cloud_status()['device_id'] == 'owl-2026-000001'
        assert reloaded.cloud_status()['portal_url'] == 'https://app.noktura.tech'

    def test_corrupt_persisted_cloud_is_ignored(self, nm, tmp_path):
        (tmp_path / 'state.json').write_text(json.dumps({'cloud': 'garbage'}))
        fb = make_state(nm, tmp_path)
        assert fb.cloud_status() is None


@pytest.mark.unit
class TestCloudSiteConflict:
    """O-5 (cross-repo review 2026-09-17): a connected unit is not re-pointed to
    a different Noktura site unless the app asks to replace it."""

    def test_same_site_reprovision_is_allowed(self, cloud_client, cloud_runner):
        client, _ = cloud_client
        assert client.post('/setup/api/cloud/provision', json=GOOD).status_code == 200
        # A retry after an app timeout, or a rotated password, for the same site
        assert client.post('/setup/api/cloud/provision', json={**GOOD, 'password': 'y' * 32}).status_code == 200
        assert cloud_runner.call_count == 2

    def test_other_site_is_409_without_replace_and_root_never_runs(self, cloud_client, cloud_runner):
        client, _ = cloud_client
        client.post('/setup/api/cloud/provision', json=GOOD)
        other = {**GOOD, 'device_id': 'owl-2026-000009', 'username': 'owl-2026-000009'}
        resp = client.post('/setup/api/cloud/provision', json=other)
        assert resp.status_code == 409
        assert resp.get_json()['error'] == 'cloud_site_conflict'
        assert resp.get_json()['existing_site'] == 'owl-2026-000001'
        assert cloud_runner.call_count == 1
        assert client.post('/setup/api/cloud/provision', json={**other, 'replace': True}).status_code == 200


@pytest.mark.unit
class TestCloudStatusSource:
    def test_status_comes_from_controller_ini_not_the_state_file(self, nm, tmp_path, cloud_runner):
        # O-18: after re-arming, the state file still named a site the unit had left.
        fb = make_state(nm, tmp_path, cloud_runner)
        fb.provision_cloud(GOOD)
        (tmp_path / 'CONTROLLER.ini').write_text('[Cloud]\nenable = False\ndevice_id = owl-2026-000001\n')
        assert make_state(nm, tmp_path).cloud_status() is None

    def test_info_reports_clock_sync(self, nm, tmp_path):
        fb = make_state(nm, tmp_path)
        app = create_app(network_manager=nm, state=fb, clock_probe=lambda: False)
        app.config['TESTING'] = True
        with patch('controller.setup.setup_app.check_camera', return_value={'ok': True, 'detail': ''}):
            assert app.test_client().get('/setup/api/info').get_json()['clock_synced'] is False


@pytest.mark.unit
class TestSetupSession:
    """Contract 5 (O-5): one phone drives setup; changes need X-Setup-Session."""

    @pytest.fixture
    def app_and_clock(self, nm, tmp_path, cloud_runner):
        from controller.setup.setup_app import SetupSession
        now = [1000.0]
        session = SetupSession(clock=lambda: now[0], idle_s=600)
        app = create_app(network_manager=nm, state=make_state(nm, tmp_path, cloud_runner), session=session)
        app.config['TESTING'] = True
        return app, now

    @pytest.mark.parametrize('path', ['/setup/api/wifi/join', '/setup/api/controller/join',
                                      '/setup/api/hotspot/restore', '/setup/api/finish',
                                      '/setup/api/cloud/provision'])
    def test_mutating_routes_refuse_without_a_session(self, app_and_clock, cloud_runner, path):
        app, _ = app_and_clock
        resp = app.test_client().post(path, json=GOOD)
        assert resp.status_code == 403
        assert resp.get_json()['error'] == 'setup_session_required'
        cloud_runner.assert_not_called()

    def test_read_only_routes_stay_open(self, app_and_clock):
        app, _ = app_and_clock
        with patch('controller.setup.setup_app.check_camera', return_value={'ok': True, 'detail': ''}):
            assert app.test_client().get('/setup/api/info').status_code == 200

    def test_second_phone_gets_409_until_idle_timeout(self, app_and_clock):
        app, now = app_and_clock
        first, second = app.test_client(), app.test_client()
        token = first.post('/setup/api/session').get_json()['session']
        assert second.post('/setup/api/session').status_code == 409
        # The holder re-presenting its token keeps the same session
        again = first.post('/setup/api/session', headers={'X-Setup-Session': token})
        assert again.status_code == 200 and again.get_json()['session'] == token
        now[0] += 601
        taken = second.post('/setup/api/session')
        assert taken.status_code == 201 and taken.get_json()['session'] != token
        # The old token no longer works
        resp = first.post('/setup/api/cloud/provision', json=GOOD, headers={'X-Setup-Session': token})
        assert resp.status_code == 403

    def test_wrong_token_is_refused(self, app_and_clock):
        app, _ = app_and_clock
        client = app.test_client()
        client.post('/setup/api/session')
        resp = client.post('/setup/api/finish', json={'mode': 'standalone'}, headers={'X-Setup-Session': 'guess'})
        assert resp.status_code == 403


@pytest.mark.unit
class TestSetupIdentityRoute:
    def test_identity_with_pem_is_readable_without_a_session(self, nm, tmp_path):
        app = create_app(network_manager=nm, state=make_state(nm, tmp_path))
        app.config['TESTING'] = True
        ident = {'hw_id': '10000000a3f1c29b', 'tier': 'disk', 'fingerprint': 'f' * 64,
                 'noktura_serial': None, 'public_key_pem': '-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----'}
        with patch('controller.setup.setup_app.read_identity', return_value=ident) as reader:
            body = app.test_client().get('/setup/api/identity').get_json()
        reader.assert_called_once_with(include_pem=True)
        assert body == {'success': True, 'identity': ident}
