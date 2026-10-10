"""First-boot setup API: POST /setup/api/cloud/provision refuses any broker
but Noktura's. Anyone on the hotspot knows the printed PSK; they must not be
able to point the unit's command channel at their own broker."""

import subprocess
from unittest.mock import MagicMock

import pytest

from controller.setup.firstboot_state import FirstBootState
from controller.setup.setup_app import create_app

GOOD = {'broker_host': 'mqtt.noktura.tech', 'broker_port': 8883,
        'device_id': 'owl-2026-000001', 'username': 'owl-2026-000001',
        'password': 'x' * 32, 'portal_url': 'https://app.noktura.tech'}


class _NoTimer:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


@pytest.fixture
def armed(tmp_path):
    nm = MagicMock()
    nm.is_supported.return_value = True
    nm.get_hotspot_connection.return_value = 'OWL-C29B'
    nm.get_ip4.return_value = '10.42.0.1'
    nm.status.return_value = {'mode': 'hotspot', 'ssid': 'OWL-C29B',
                              'ip': '10.42.0.1', 'hotspot_profile': 'OWL-C29B'}
    flag = tmp_path / 'owl-firstboot.flag'
    flag.touch()
    runner = MagicMock(return_value=subprocess.CompletedProcess(['sudo'], 0, '', ''))
    fb = FirstBootState(nm, flag_path=flag, state_file=tmp_path / 'state.json',
                        timer_factory=_NoTimer, system_runner=MagicMock(),
                        exit_fn=MagicMock(), controller_ini=tmp_path / 'CONTROLLER.ini',
                        cloud_runner=runner)
    app = create_app(network_manager=nm, state=fb)
    app.config['TESTING'] = True
    client = app.test_client()
    client.environ_base['HTTP_X_SETUP_SESSION'] = client.post('/setup/api/session').get_json()['session']
    return client, fb, runner


@pytest.mark.unit
class TestFirstBootBrokerPin:
    @pytest.mark.parametrize('host', ['mqtt.attacker.example', 'noktura.tech.evil.io',
                                      'evilnoktura.tech', '10.42.0.5'])
    def test_foreign_broker_is_400_and_root_never_runs(self, armed, host):
        client, fb, runner = armed
        resp = client.post('/setup/api/cloud/provision', json={**GOOD, 'broker_host': host})
        assert resp.status_code == 400
        assert 'noktura.tech' in resp.get_json()['error']
        runner.assert_not_called()
        assert fb.cloud is None

    def test_noktura_broker_provisions(self, armed):
        client, _, runner = armed
        assert client.post('/setup/api/cloud/provision', json=GOOD).status_code == 200
        runner.assert_called_once()
