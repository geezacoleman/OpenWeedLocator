"""Tests for software-update status reporting.

Covers:
- OWLMQTTPublisher._read_update_status (mirrors owl_update.sh's status file
  into state['software_update'] — including the post-restart pickup of
  terminal complete/rolled_back statuses)
- Version info in the heartbeat state (version/git_branch/git_commit/pi_model)
- SystemInfo.get_git_info(cwd=...) — must not depend on process CWD
"""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _status_payload(**overrides):
    payload = {
        'request_id': 'req-1',
        'status': 'fetching',
        'ref': 'main',
        'from': '3.0.0@wireless-display/abc1234',
        'to': '',
        'error': '',
        'rollback_failed': False,
        'started_at': 1781136615,
        'updated_at': 1781136699,
        'pid': 12345,
        'log_file': '/home/owl/owl/logs/updates/x.log',
    }
    payload.update(overrides)
    return payload


def _point_at(publisher, tmp_path):
    """Point the publisher's status file at a tmp path and reset the cache."""
    publisher._update_status_path = tmp_path / '.update_status.json'
    publisher._update_status_stamp = None
    return publisher._update_status_path


@pytest.mark.unit
class TestReadUpdateStatus:

    def test_missing_file_leaves_state_idle(self, mqtt_publisher, tmp_path):
        _point_at(mqtt_publisher, tmp_path)
        mqtt_publisher._read_update_status()
        assert mqtt_publisher.state['software_update']['status'] == 'idle'

    def test_status_file_mirrored_into_state(self, mqtt_publisher, tmp_path):
        path = _point_at(mqtt_publisher, tmp_path)
        path.write_text(json.dumps(_status_payload()))

        mqtt_publisher._read_update_status()

        su = mqtt_publisher.state['software_update']
        assert su['status'] == 'fetching'
        assert su['request_id'] == 'req-1'
        assert su['ref'] == 'main'
        assert su['from'] == '3.0.0@wireless-display/abc1234'

    def test_terminal_rolled_back_surfaces_error(self, mqtt_publisher, tmp_path):
        """Post-restart pickup: the restarted process must publish the
        terminal status written by the update script."""
        path = _point_at(mqtt_publisher, tmp_path)
        path.write_text(json.dumps(_status_payload(
            status='rolled_back', error='pip install failed', rollback_failed=False
        )))

        mqtt_publisher._read_update_status()

        su = mqtt_publisher.state['software_update']
        assert su['status'] == 'rolled_back'
        assert su['error'] == 'pip install failed'
        assert su['rollback_failed'] is False

    def test_unchanged_file_not_reparsed(self, mqtt_publisher, tmp_path):
        path = _point_at(mqtt_publisher, tmp_path)
        path.write_text(json.dumps(_status_payload()))
        mqtt_publisher._read_update_status()
        first_stamp = mqtt_publisher._update_status_stamp
        assert first_stamp is not None

        mqtt_publisher._read_update_status()
        assert mqtt_publisher._update_status_stamp == first_stamp

    def test_changed_file_reread(self, mqtt_publisher, tmp_path):
        path = _point_at(mqtt_publisher, tmp_path)
        path.write_text(json.dumps(_status_payload(status='fetching')))
        mqtt_publisher._read_update_status()

        # Different length guarantees the (mtime, size) stamp changes even
        # on filesystems with coarse mtime resolution
        path.write_text(json.dumps(_status_payload(
            status='complete', to='3.0.0@main/4629653xxxx'
        )))
        mqtt_publisher._read_update_status()

        assert mqtt_publisher.state['software_update']['status'] == 'complete'

    def test_garbage_json_keeps_previous_state(self, mqtt_publisher, tmp_path):
        path = _point_at(mqtt_publisher, tmp_path)
        path.write_text(json.dumps(_status_payload(status='installing_deps')))
        mqtt_publisher._read_update_status()

        path.write_text('{this is not json')
        mqtt_publisher._read_update_status()  # must not raise

        assert mqtt_publisher.state['software_update']['status'] == 'installing_deps'

    def test_garbage_json_retried_after_fix(self, mqtt_publisher, tmp_path):
        """A failed parse must not cache the stamp — the next read retries."""
        path = _point_at(mqtt_publisher, tmp_path)
        path.write_text('{bad json')
        mqtt_publisher._read_update_status()
        assert mqtt_publisher._update_status_stamp is None

        path.write_text(json.dumps(_status_payload(status='complete')))
        mqtt_publisher._read_update_status()
        assert mqtt_publisher.state['software_update']['status'] == 'complete'

    def test_heartbeat_state_includes_software_update(self, mqtt_publisher, tmp_path):
        path = _point_at(mqtt_publisher, tmp_path)
        path.write_text(json.dumps(_status_payload(status='health_check')))
        mqtt_publisher._read_update_status()

        mqtt_publisher.client.publish.reset_mock()
        with mqtt_publisher.state_lock:
            mqtt_publisher._publish_state()

        published = json.loads(mqtt_publisher.client.publish.call_args[0][1])
        assert published['software_update']['status'] == 'health_check'


@pytest.mark.unit
class TestVersionInState:

    def test_state_has_version_fields(self, mqtt_publisher):
        for key in ('version', 'git_branch', 'git_commit', 'pi_model', 'os_pretty'):
            assert key in mqtt_publisher.state, f"missing state key: {key}"

    def test_version_populated_from_version_module(self, mqtt_publisher):
        from version import VERSION
        assert mqtt_publisher.state['version'] == str(VERSION)

    def test_git_info_populated_in_repo(self, mqtt_publisher):
        # Running from a git checkout — branch/commit must be real values
        assert mqtt_publisher.state['git_branch'] not in ('', None)
        assert mqtt_publisher.state['git_commit'] not in ('', None)

    def test_populate_never_raises(self, mqtt_publisher):
        with patch('version.SystemInfo.get_git_info', side_effect=RuntimeError('boom')):
            mqtt_publisher._populate_version_info()  # must not raise


@pytest.mark.unit
class TestGetGitInfoCwd:

    def test_default_cwd_is_repo_root(self):
        import version as version_mod
        with patch('version.subprocess.check_output', return_value=b'abc1234\n') as co:
            version_mod.SystemInfo.get_git_info()
        for call_obj in co.call_args_list:
            assert call_obj.kwargs['cwd'] == Path(version_mod.__file__).parent

    def test_explicit_cwd_passed_through(self, tmp_path):
        import version as version_mod
        with patch('version.subprocess.check_output', return_value=b'abc1234\n') as co:
            version_mod.SystemInfo.get_git_info(cwd=tmp_path)
        for call_obj in co.call_args_list:
            assert call_obj.kwargs['cwd'] == tmp_path

    def test_git_missing_returns_none(self):
        import version as version_mod
        with patch('version.subprocess.check_output', side_effect=FileNotFoundError('no git')):
            assert version_mod.SystemInfo.get_git_info() is None
