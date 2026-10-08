"""Fixes from the cross-repo review of 2026-09-17
(Noktura/docs/owl/OWL_CLAIM_MVP_REVIEW-2026-09-17.md). Each test names the
finding it guards. Where a script can run on a dev machine (bash + python3),
the test RUNS the generated code instead of string-matching it: the v3.18.0
helper tests only checked text, which is how a root escalation shipped.
"""

import configparser
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from utils import device_identity
from utils.cloud_provision import validate_payload, helper_stdin

REPO = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
needs_bash = pytest.mark.skipif(BASH is None, reason='bash not available')

GOOD = {'broker_host': 'mqtt.noktura.tech', 'broker_port': 8883,
        'device_id': 'owl-2026-000001', 'username': 'owl-2026-000001',
        'password': 'p' * 32, 'portal_url': 'https://app.noktura.tech',
        'storage_host': 'hel1.your-objectstorage.com'}


def posix(path):
    return Path(path).as_posix()


# ---------------------------------------------------------------------------
# O-1 / O-7 / O-14 / O-16: the generated root helper
# ---------------------------------------------------------------------------

def render_cloud_helper(tmp_path, script_copy, owl_dir, user='owl'):
    """Render the helper exactly as install_cloud_helper() writes it: the
    unquoted heredoc expands the install-time variables, nothing else."""
    source = (REPO / 'controller' / 'setup' / 'install_firstboot.sh').read_text(encoding='utf-8')
    body = source.split('cat > "${CLOUD_HELPER}" <<EOF || return 1\n', 1)[1].split('\nEOF\n', 1)[0]
    # /run is root-only on a real unit; keep the temp file inside tmp_path.
    body = body.replace('/run/owl-cloud-pw.XXXXXX', posix(tmp_path / 'owl-cloud-pw.XXXXXX'))
    template = tmp_path / 'render.sh'
    template.write_text(
        f'CURRENT_USER={user}\nOWL_DIR={posix(owl_dir)}\nCLOUD_SCRIPT_COPY={posix(script_copy)}\n'
        'cat <<EOF\n' + body + '\nEOF\n', newline='\n')
    rendered = subprocess.run([BASH, posix(template)], capture_output=True, text=True, timeout=30)
    assert rendered.returncode == 0, rendered.stderr
    helper = tmp_path / 'owl-cloud-provision'
    helper.write_text(rendered.stdout, newline='\n')
    return helper


@needs_bash
class TestGeneratedCloudHelper:
    @pytest.fixture
    def rig(self, tmp_path):
        record = tmp_path / 'record.json'
        fake_copy = tmp_path / 'lib' / 'owl_cloud_provision.sh'
        fake_copy.parent.mkdir()
        # Stands in for the root-owned copy: records how it was called and what
        # the password file held while it ran.
        fake_copy.write_text(
            '#!/bin/bash\n'
            'pwfile=""; prev=""\n'
            'for a in "$@"; do [[ "$prev" == "--password-file" ]] && pwfile="$a"; prev="$a"; done\n'
            f'python3 - "$pwfile" "$@" > {posix(record)} <<PY\n'
            'import json, os, sys\n'
            'print(json.dumps({"args": sys.argv[2:], "pwfile": sys.argv[1],\n'
            '  "password": open(sys.argv[1]).read(),\n'
            '  "owl_user": os.environ.get("OWL_USER"), "repo": os.environ.get("OWL_REPO_DIR")}))\n'
            'PY\n', newline='\n')
        owl_dir = tmp_path / 'home' / 'owl' / 'owl'
        helper = render_cloud_helper(tmp_path, fake_copy, owl_dir)
        return helper, record, owl_dir

    def run_helper(self, helper, payload):
        # Bytes, not text=True: on Windows text mode turns stdin LF into CRLF,
        # which a Pi never sees (setup_app passes plain LF).
        stdin = helper_stdin(validate_payload(payload)).encode()
        result = subprocess.run([BASH, posix(helper)], input=stdin, capture_output=True, timeout=60)
        result.stderr = result.stderr.decode(errors='replace')
        return result

    def test_rendered_helper_parses_and_never_execs_the_repo_script(self, rig):
        helper, _, owl_dir = rig
        text = helper.read_text()
        assert subprocess.run([BASH, '-n', posix(helper)], capture_output=True).returncode == 0
        # O-1: nothing the owl user can edit is executed as root
        assert f'{posix(owl_dir)}/owl_cloud_provision.sh' not in text
        # O-7: `exec` would replace the shell and skip the EXIT trap
        assert not re.search(r'^\s*exec\s', text, re.M)

    def test_runs_the_copy_as_the_install_user_and_removes_the_password_file(self, rig):
        helper, record, owl_dir = rig
        result = self.run_helper(helper, GOOD)
        assert result.returncode == 0, result.stderr
        seen = json.loads(record.read_text())
        assert seen['password'] == 'p' * 32
        assert seen['owl_user'] == 'owl'                   # O-16, not SUDO_USER=root
        assert seen['repo'] == posix(owl_dir)
        assert seen['args'][seen['args'].index('--storage-host') + 1] == 'hel1.your-objectstorage.com'
        assert '--system-ca' in seen['args']
        assert 'p' * 32 not in ' '.join(seen['args'])     # password never in argv
        assert not Path(seen['pwfile']).exists()           # O-7: trap ran

    @pytest.mark.parametrize('bad', [
        {**GOOD, 'broker_port': 70000}, {**GOOD, 'broker_port': 0},
    ])
    def test_bad_port_is_refused_not_replaced_with_8883(self, rig, bad):
        # O-14: the helper used to fall back to 8883 silently. Bypass the
        # Python validator to hit the helper's own check.
        helper, record, _ = rig
        stdin = json.dumps({k: bad[k] for k in ('broker_host', 'broker_port', 'device_id',
                                                'username', 'portal_url')}) + '\n' + 'p' * 32 + '\n'
        result = subprocess.run([BASH, posix(helper)], input=stdin.encode(), capture_output=True, timeout=60)
        assert result.returncode != 0
        assert 'bad broker_port' in result.stderr.decode(errors='replace')
        assert not record.exists()

    def test_install_copies_the_script_root_owned_before_writing_the_helper(self):
        source = (REPO / 'controller' / 'setup' / 'install_firstboot.sh').read_text(encoding='utf-8')
        fn = source.split('install_cloud_helper() {', 1)[1].split('\narm() {', 1)[0]
        copy = fn.index('install -m 755 -o root -g root "${OWL_DIR}/owl_cloud_provision.sh" "${CLOUD_SCRIPT_COPY}"')
        assert copy < fn.index('cat > "${CLOUD_HELPER}"')
        assert 'visudo -cf' in fn
        assert 'NOPASSWD: ${CLOUD_HELPER}' in fn
        assert '--cloud-helper) install_cloud_helper; exit $? ;;' in source
        # --arm and setup.sh both use the one implementation
        assert 'if ! install_cloud_helper; then' in source
        setup_sh = (REPO / 'controller' / 'shared' / 'setup.sh').read_text(encoding='utf-8')
        assert 'bash "${FIRSTBOOT_SCRIPT}" --cloud-helper' in setup_sh

    def test_install_user_comes_from_the_repo_owner(self):
        source = (REPO / 'controller' / 'setup' / 'install_firstboot.sh').read_text(encoding='utf-8')
        assert 'CURRENT_USER="$(stat -c %U "${OWL_DIR}"' in source


# ---------------------------------------------------------------------------
# O-17 + O-10: setup.sh keeps [Cloud]/[Security] and gives every unit a token
# ---------------------------------------------------------------------------

@needs_bash
class TestSetupShPreservesCloudAndToken:
    def extract(self, start, end):
        source = (REPO / 'controller' / 'shared' / 'setup.sh').read_text(encoding='utf-8')
        return source.split(start, 1)[1].split(end, 1)[0]

    def run_update(self, tmp_path, existing, mode='standalone'):
        ini = tmp_path / 'CONTROLLER.ini'
        if existing is not None:
            ini.write_text(existing, newline='\n')
        keep = self.extract('    PRESERVED_SECTIONS=""\n', '    cat > "$CTRL_INI" <<EOF')
        after = self.extract('speed_avg_window = 2.0\nEOF\n', '    # The owl/owl-dash services run as')
        script = tmp_path / 'update.sh'
        script.write_text(
            f'CTRL_INI={posix(ini)}\nOWL_MODE={mode}\nGREEN=""; NC=""\nPRESERVED_SECTIONS=""\n'
            + keep
            + 'printf "[MQTT]\\nenable = True\\n\\n[GPS]\\nsource = none\\nspeed_avg_window = 2.0\\n" > "$CTRL_INI"\n'
            + after, newline='\n')
        result = subprocess.run([BASH, posix(script)], capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        config = configparser.ConfigParser()
        config.read(ini)
        return config

    def test_rerun_keeps_cloud_and_security_sections(self, tmp_path):
        config = self.run_update(tmp_path, '[MQTT]\nenable = False\n\n[Cloud]\nenable = True\n'
                                           'device_id = owl-2026-000001\nstorage_host = hel1.x.com\n\n'
                                           '[Security]\ndevice_token = keep-me\n')
        assert config.get('Cloud', 'device_id') == 'owl-2026-000001'
        assert config.get('Cloud', 'storage_host') == 'hel1.x.com'
        assert config.get('Security', 'device_token') == 'keep-me'   # not rotated
        assert config.get('MQTT', 'enable') == 'True'                 # rest rewritten

    def test_fresh_standalone_install_gets_a_token(self, tmp_path):
        config = self.run_update(tmp_path, None)
        assert len(config.get('Security', 'device_token')) >= 32
        assert not config.has_section('Cloud')

    def test_networked_install_gets_no_token(self, tmp_path):
        config = self.run_update(tmp_path, None, mode='networked')
        assert not config.has_section('Security')


# ---------------------------------------------------------------------------
# O-4 + storage_host: owl_cloud_provision.sh
# ---------------------------------------------------------------------------

class TestProvisionScript:
    @pytest.fixture
    def cloud_sh(self):
        return (REPO / 'owl_cloud_provision.sh').read_text(encoding='utf-8')

    def test_networked_cloud_commands_never_reach_the_controller_command_topic(self, cloud_sh):
        networked = cloud_sh.split('if [[ "$NET_MODE" == "networked" ]]; then', 1)[1].split('\nelse\n', 1)[0]
        assert 'topic +/commands in 1 owl/cloud/ ${REMOTE_PREFIX}' in networked
        assert 'topic +/commands in 1 ${LOCAL_PREFIX}' not in networked

    def test_storage_host_is_validated_and_written(self, cloud_sh):
        assert '--storage-host)  STORAGE_HOST="$2"' in cloud_sh
        assert '^[a-z0-9.-]{3,253}$' in cloud_sh
        assert '_update_ini_key "$CTRL_INI" "Cloud" "storage_host" "$STORAGE_HOST"' in cloud_sh

    def test_helper_environment_overrides_user_and_repo(self, cloud_sh):
        assert 'CURRENT_USER="${OWL_USER:-${SUDO_USER:-$(whoami)}}"' in cloud_sh
        assert 'REPO_DIR="${OWL_REPO_DIR:-$SCRIPT_DIR}"' in cloud_sh


class TestPayload:
    def test_storage_host_is_lowercased_and_passed_to_the_helper(self):
        cred = validate_payload({**GOOD, 'storage_host': 'HEL1.Your-ObjectStorage.com'})
        assert cred['storage_host'] == 'hel1.your-objectstorage.com'
        assert json.loads(helper_stdin(cred).split('\n')[0])['storage_host'] == 'hel1.your-objectstorage.com'

    @pytest.mark.parametrize('bad', [{'broker_port': True}, {'broker_port': 'x'},
                                     {'storage_host': 'not a host!'}])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_payload({**GOOD, **bad})


# ---------------------------------------------------------------------------
# Serial: lookup first, local label optional, derived from [Cloud]
# ---------------------------------------------------------------------------

class TestSerialFromCloud:
    @pytest.fixture(autouse=True)
    def hw(self, monkeypatch):
        monkeypatch.setattr(device_identity, '_cached', True)
        monkeypatch.setattr(device_identity, '_serial', '10000000a3f1c29b')

    def test_derived_from_cloud_device_id_when_no_local_label(self, tmp_path):
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Cloud]\nenable = True\ndevice_id = owl-2026-000042\n')
        ident = device_identity.read_identity(tmp_path / 'no-identity', controller_ini=ini)
        assert ident['noktura_serial'] == 'OWL-2026-000042'

    def test_local_label_wins_locally(self, tmp_path):
        ident_dir = tmp_path / 'identity'
        ident_dir.mkdir()
        (ident_dir / 'noktura_serial').write_text('OWL-2026-000001\n')
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Cloud]\nenable = True\ndevice_id = owl-2026-000042\n')
        assert device_identity.read_identity(ident_dir, controller_ini=ini)['noktura_serial'] == 'OWL-2026-000001'

    def test_manual_site_slugs_are_not_serials(self, tmp_path):
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Cloud]\nenable = True\ndevice_id = willow-creek\n')
        assert device_identity.read_identity(tmp_path / 'x', controller_ini=ini)['noktura_serial'] is None


# ---------------------------------------------------------------------------
# O-19 + O-18 + D3 on the dashboard
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDashboard:
    def test_reset_setup_is_token_guarded(self, standalone_test_client):
        client, dashboard, _ = standalone_test_client
        with patch.object(dashboard, 'token_reader', return_value='secret-token'), \
                patch('controller.standalone.standalone.subprocess.run') as run:
            assert client.post('/api/network/reset-setup').status_code == 403
            run.assert_not_called()
            with patch('controller.standalone.standalone.os.path.exists', return_value=True):
                run.return_value = MagicMock(returncode=0, stdout='', stderr='')
                ok = client.post('/api/network/reset-setup', headers={'X-Device-Token': 'secret-token'})
            assert ok.status_code == 200

    def test_system_stats_cloud_fields_are_read_live(self, standalone_test_client):
        client, dashboard, _ = standalone_test_client
        live = {'enabled': True, 'device_id': 'owl-2026-000001',
                'portal_url': 'https://app.noktura.tech', 'storage_host': None}
        with patch('controller.standalone.standalone.cloud_provision.read_cloud_config', return_value=live), \
                patch('controller.standalone.standalone.cloud_provision.clock_synced', return_value=False):
            data = client.get('/api/system_stats').get_json()
        assert data['cloud_enabled'] is True
        assert data['cloud_device_id'] == 'owl-2026-000001'
        assert data['clock_synced'] is False


class TestClockSynced:
    @pytest.mark.parametrize('stdout,expected', [('yes\n', True), ('no\n', False), ('', None), ('maybe', None)])
    def test_parses_timedatectl(self, stdout, expected):
        from utils.cloud_provision import clock_synced
        runner = MagicMock(return_value=MagicMock(stdout=stdout))
        assert clock_synced(runner) is expected
        assert runner.call_args[0][0] == ['timedatectl', 'show', '-p', 'NTPSynchronized', '--value']

    def test_missing_timedatectl_is_unknown(self):
        from utils.cloud_provision import clock_synced
        assert clock_synced(MagicMock(side_effect=OSError('no timedatectl'))) is None
