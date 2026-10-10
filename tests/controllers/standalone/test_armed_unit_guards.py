"""Standalone dashboard routes an armed (first-boot) unit must not serve:

- POST /api/settings/token re-earns the device token with the hotspot PSK.
  On an armed unit that PSK is the printed one every unit shares, so it
  proves nothing; the setup API hands the token to the phone that finishes.
- POST /api/settings/cloud/provision refuses any broker but Noktura's, even
  with a valid token.
"""

import logging
from unittest.mock import MagicMock

import pytest
from flask import Flask

from controller.standalone.settings_routes import create_settings_blueprint, make_token_reader

TOKEN = 'test-device-token-12345'
GOOD = {'broker_host': 'mqtt.noktura.tech', 'broker_port': 8883,
        'device_id': 'owl-2026-000001', 'username': 'owl-2026-000001',
        'password': 'y' * 32, 'portal_url': 'https://app.noktura.tech'}


def dashboard(tmp_path, psk='owl-setup', runner=None):
    ini = tmp_path / 'CONTROLLER.ini'
    ini.write_text(f'[Network]\nmode = standalone\n[Security]\ndevice_token = {TOKEN}\n')
    manager = MagicMock()
    manager.nm.get_hotspot_password.return_value = psk
    flag = tmp_path / 'owl-firstboot.flag'
    app = Flask(__name__)
    app.config['TESTING'] = True
    app.register_blueprint(create_settings_blueprint(
        manager=manager, token_reader=make_token_reader(str(ini)),
        mqtt_getter=lambda: None, logger=logging.getLogger('test-armed'),
        controller_ini_path=str(ini), owl_active_getter=lambda: False,
        runner=runner or MagicMock(), timer_factory=MagicMock(),
        internet_probe=lambda: {}, update_state_getter=lambda: {},
        status_path=str(tmp_path / '.update_status.json'), popen=MagicMock(),
        git_info_getter=lambda: {}, firstboot_flag_path=str(flag)))
    return app.test_client(), flag


@pytest.mark.unit
class TestTokenRecoveryWhileArmed:
    def test_armed_unit_refuses_the_printed_psk(self, tmp_path):
        client, flag = dashboard(tmp_path)
        flag.touch()
        resp = client.post('/api/settings/token', json={'hotspot_password': 'owl-setup'})
        assert resp.status_code == 409
        assert resp.get_json()['error'] == 'setup_in_progress'
        assert TOKEN not in resp.get_data(as_text=True)

    def test_finished_unit_still_recovers(self, tmp_path):
        client, flag = dashboard(tmp_path)
        flag.touch()
        client.post('/api/settings/token', json={'hotspot_password': 'owl-setup'})
        flag.unlink()   # setup finished
        resp = client.post('/api/settings/token', json={'hotspot_password': 'owl-setup'})
        assert resp.status_code == 200
        assert resp.get_json()['device_token'] == TOKEN


@pytest.mark.unit
class TestDashboardBrokerPin:
    def test_foreign_broker_refused_even_with_token(self, tmp_path):
        runner = MagicMock()
        client, _ = dashboard(tmp_path, runner=runner)
        resp = client.post('/api/settings/cloud/provision',
                           json={**GOOD, 'broker_host': 'mqtt.attacker.example'},
                           headers={'X-Device-Token': TOKEN})
        assert resp.status_code == 400
        assert 'noktura.tech' in resp.get_json()['error']
        runner.assert_not_called()
