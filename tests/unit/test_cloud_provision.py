"""utils/cloud_provision.py + the shell side of cloud provisioning.

The password must never reach argv or logs; every field is validated before
root runs anything; the scripts must parse; setup.sh must have closed the
broker to the hotspot (the whole point of the cloud allowlist).
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from utils import cloud_provision
from utils.cloud_provision import (
    CloudProvisionError, PERMISSIONS_MISSING_ERROR, helper_stdin, provision, validate_payload)

REPO = Path(__file__).resolve().parents[2]

GOOD = {
    'broker_host': 'mqtt.noktura.tech', 'broker_port': 8883,
    'device_id': 'owl-2026-000001', 'username': 'owl-2026-000001',
    'password': 'p' * 32, 'portal_url': 'https://app.noktura.tech',
}


def completed(rc=0, stderr=''):
    return subprocess.CompletedProcess(['sudo'], rc, stdout='', stderr=stderr)


@pytest.mark.unit
class TestValidatePayload:
    def test_good_payload_normalises_types(self):
        cred = validate_payload({**GOOD, 'broker_port': '8883'})
        assert cred['broker_port'] == 8883
        assert cred['username'] == 'owl-2026-000001'

    def test_username_defaults_to_device_id(self):
        d = dict(GOOD)
        del d['username']
        assert validate_payload(d)['username'] == 'owl-2026-000001'

    @pytest.mark.parametrize('field, value, msg', [
        ('broker_host', '', 'broker_host'),
        ('broker_host', 'bad host', 'broker_host'),
        ('broker_port', 'eighty', 'number'),
        ('broker_port', 70000, 'range'),
        ('device_id', 'OWL-2026-000001', 'lowercase'),      # uppercase serial, not slug
        ('device_id', 'ab', 'lowercase'),
        ('device_id', 'a' * 40, 'lowercase'),
        ('username', 'someone-else', 'username must equal'),
        ('password', 'short', 'password'),
        ('password', 'p' * 200, 'password'),
        ('password', 'with\nnewline' + 'x' * 20, 'password'),
        ('password', 12345678901234567890, 'password'),
        ('portal_url', 'http://insecure', 'https'),
        ('portal_url', 'javascript:alert(1)', 'https'),
    ])
    def test_rejects_bad_fields(self, field, value, msg):
        with pytest.raises(ValueError, match=msg):
            validate_payload({**GOOD, field: value})

    def test_rejects_non_object(self):
        with pytest.raises(ValueError):
            validate_payload(['not', 'a', 'dict'])

    def test_portal_optional(self):
        assert validate_payload({**GOOD, 'portal_url': ''})['portal_url'] == ''


@pytest.mark.unit
class TestHelperStdin:
    def test_password_is_line_two_only_and_json_has_no_password(self):
        lines = helper_stdin(validate_payload(GOOD)).split('\n')
        assert lines[1] == 'p' * 32
        assert 'p' * 32 not in lines[0]
        assert '"device_id": "owl-2026-000001"' in lines[0]


@pytest.mark.unit
class TestProvision:
    def test_runs_helper_with_password_on_stdin_never_argv(self):
        runner = MagicMock(return_value=completed(0))
        result = provision(runner, GOOD)
        argv = runner.call_args.args[0]
        assert argv == ['sudo', '-n', cloud_provision.CLOUD_HELPER]
        assert 'p' * 32 not in ' '.join(argv)
        assert runner.call_args.kwargs['input'].split('\n')[1] == 'p' * 32
        assert runner.call_args.kwargs['timeout'] == cloud_provision.CLOUD_HELPER_TIMEOUT_S
        assert result['device_id'] == 'owl-2026-000001'
        assert result['portal_url'] == 'https://app.noktura.tech'
        assert result['provisioned_at']
        assert 'password' not in result

    def test_bad_payload_never_reaches_root(self):
        runner = MagicMock()
        with pytest.raises(ValueError):
            provision(runner, {**GOOD, 'device_id': 'BAD'})
        runner.assert_not_called()

    def test_missing_sudoers_is_the_permissions_error(self):
        runner = MagicMock(return_value=completed(1, 'sudo: a password is required'))
        with pytest.raises(CloudProvisionError) as e:
            provision(runner, GOOD)
        assert str(e.value) == PERMISSIONS_MISSING_ERROR

    def test_missing_helper_binary(self):
        runner = MagicMock(side_effect=FileNotFoundError('sudo'))
        with pytest.raises(CloudProvisionError) as e:
            provision(runner, GOOD)
        assert str(e.value) == PERMISSIONS_MISSING_ERROR

    def test_helper_failure_reports_stderr_tail_without_password(self):
        stderr = '\n'.join(f'line {i}' for i in range(20)) + '\n[ERROR] mosquitto failed to start'
        runner = MagicMock(return_value=completed(1, stderr))
        with pytest.raises(CloudProvisionError) as e:
            provision(runner, GOOD)
        assert 'mosquitto failed to start' in e.value.detail
        assert e.value.detail.count('\n') <= 4
        assert 'p' * 32 not in e.value.detail

    def test_timeout(self):
        runner = MagicMock(side_effect=subprocess.TimeoutExpired('sudo', 120))
        with pytest.raises(CloudProvisionError, match='timed out'):
            provision(runner, GOOD)


@pytest.mark.unit
class TestShellSide:
    """Source-level guards (the scripts run on the Pi, not in CI)."""

    @pytest.fixture
    def setup_sh(self):
        return (REPO / 'controller' / 'shared' / 'setup.sh').read_text(encoding='utf-8')

    @pytest.fixture
    def cloud_sh(self):
        return (REPO / 'owl_cloud_provision.sh').read_text(encoding='utf-8')

    @pytest.fixture
    def firstboot_sh(self):
        return (REPO / 'controller' / 'setup' / 'install_firstboot.sh').read_text(encoding='utf-8')

    def test_standalone_broker_is_loopback_only(self, setup_sh):
        assert 'listener 1883 127.0.0.1' in setup_sh
        assert 'listener 1883 0.0.0.0' not in setup_sh
        assert 'sudo ufw allow 1883/tcp' not in setup_sh

    def test_cloud_commands_land_on_their_own_topic(self, cloud_sh):
        assert 'topic commands in 1 owl/cloud_ ${REMOTE_PREFIX}' in cloud_sh
        # never remap cloud commands straight onto the dashboard topic
        assert 'topic commands in 1 ${LOCAL_PREFIX}' not in cloud_sh

    def test_cloud_script_enforces_slug_and_username(self, cloud_sh):
        assert '"$USERNAME" != "$DEVICE_ID"' in cloud_sh
        assert '^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$' in cloud_sh
        assert '--portal-url' in cloud_sh

    def test_helper_reads_password_from_stdin_and_is_root_owned(self, firstboot_sh):
        assert 'CLOUD_HELPER="/usr/local/sbin/owl-cloud-provision"' in firstboot_sh
        assert 'IFS= read -r password' in firstboot_sh
        assert 'chown root:root "${CLOUD_HELPER}"' in firstboot_sh
        assert 'NOPASSWD: ${CLOUD_HELPER}' in firstboot_sh
        # the sudoers line must name the root-owned helper, never the repo script
        assert 'NOPASSWD: ${OWL_DIR}/owl_cloud_provision.sh' not in firstboot_sh

    def test_clean_sh_protects_controller_ini(self):
        clean = (REPO / 'dev' / 'clean.sh').read_text(encoding='utf-8')
        assert 'chmod 640 /home/owl/owl/config/CONTROLLER.ini' in clean

    @pytest.mark.skipif(shutil.which('bash') is None, reason='bash not available')
    @pytest.mark.parametrize('script', [
        'owl_identity.sh', 'owl_cloud_provision.sh', 'tools/noktura_register.sh',
        'controller/setup/install_firstboot.sh', 'controller/shared/setup.sh', 'dev/clean.sh',
    ])
    def test_scripts_parse(self, script):
        result = subprocess.run(['bash', '-n', str(REPO / script)],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(shutil.which('bash') is None, reason='bash not available')
    def test_identity_script_refuses_bad_serial_without_root_work(self, tmp_path):
        env = {'OWL_IDENTITY_DIR': str(tmp_path), 'PATH': '/usr/bin:/bin'}
        result = subprocess.run(['bash', str(REPO / 'owl_identity.sh'), '--serial', 'OWL-1'],
                                capture_output=True, text=True, timeout=30, env=env)
        assert result.returncode != 0
        assert not (tmp_path / 'noktura_serial').exists()


class TestRigBrokerPin:
    """The rig's pairing route is open to the rig LAN, so it only accepts
    Noktura's broker (anyone on the LAN could otherwise re-point the bridge)."""

    @pytest.mark.parametrize('host', ['mqtt.noktura.tech', 'MQTT.Noktura.Tech', 'noktura.tech',
                                      'mqtt.noktura.tech.'])
    def test_noktura_hosts_pass(self, host):
        cloud_provision.require_noktura_broker({'broker_host': host})

    @pytest.mark.parametrize('host', ['evil.example.com', 'noktura.tech.evil.com',
                                      'evilnoktura.tech', '', None, '10.0.0.5'])
    def test_other_hosts_refused(self, host):
        with pytest.raises(ValueError):
            cloud_provision.require_noktura_broker({'broker_host': host})
