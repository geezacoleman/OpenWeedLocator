"""provision() only connects the bridge to Noktura's broker.

Every route into it is open to someone other than the owner (rig LAN, the
first-boot hotspot with its printed PSK, a holder of the device token), so
the pin lives in provision() itself, not in each route. Route-level tests:
tests/controllers/*/test_*broker_pin.py.
"""

from unittest.mock import MagicMock

import pytest

from utils import cloud_provision

GOOD = {'broker_host': 'mqtt.noktura.tech', 'broker_port': 8883,
        'device_id': 'owl-2026-000001', 'username': 'owl-2026-000001',
        'password': 'z' * 32, 'portal_url': 'https://app.noktura.tech'}


def ok_runner():
    return MagicMock(return_value=MagicMock(returncode=0, stdout='', stderr=''))


@pytest.mark.unit
class TestProvisionPinsBroker:
    @pytest.mark.parametrize('host', ['mqtt.attacker.example', 'noktura.tech.evil.io',
                                      'evilnoktura.tech', '10.42.0.5'])
    def test_foreign_broker_never_reaches_root(self, host):
        runner = ok_runner()
        with pytest.raises(ValueError, match='noktura.tech'):
            cloud_provision.provision(runner, {**GOOD, 'broker_host': host})
        runner.assert_not_called()

    @pytest.mark.parametrize('host', ['mqtt.noktura.tech', 'MQTT.Noktura.Tech'])
    def test_noktura_broker_runs_helper(self, host):
        runner = ok_runner()
        assert cloud_provision.provision(runner, {**GOOD, 'broker_host': host})['device_id'] \
            == 'owl-2026-000001'
        runner.assert_called_once()
