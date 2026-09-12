"""Standalone dashboard routes behind the phone app's Maintenance card:
reset network setup, factory reset of detection settings, and the version
fields in system_stats. Uses the standalone_test_client fixture."""

from unittest.mock import MagicMock, patch

import pytest

from test_standalone_routes import _stabilize_mqtt


@pytest.mark.unit
class TestResetNetworkSetupRoute:
    def test_404_when_helper_missing(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        with patch('controller.standalone.standalone.os.path.exists',
                   return_value=False):
            resp = client.post('/api/network/reset-setup')
        assert resp.status_code == 404
        assert 'not installed' in resp.get_json()['error']

    def test_runs_the_root_helper(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        ok = MagicMock(returncode=0, stdout='', stderr='')
        with patch('controller.standalone.standalone.os.path.exists',
                   return_value=True), \
                patch('controller.standalone.standalone.subprocess.run',
                      return_value=ok) as run:
            resp = client.post('/api/network/reset-setup')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        assert run.call_args[0][0] == ['/usr/bin/sudo', '-n',
                                       '/usr/local/sbin/owl-firstboot-arm']

    def test_helper_failure_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        bad = MagicMock(returncode=1, stdout='', stderr='flag write failed')
        with patch('controller.standalone.standalone.os.path.exists',
                   return_value=True), \
                patch('controller.standalone.standalone.subprocess.run',
                      return_value=bad):
            resp = client.post('/api/network/reset-setup')
        assert resp.status_code == 500
        assert 'flag write failed' in resp.get_json()['error']


@pytest.mark.unit
class TestVersionFields:
    def test_system_stats_carry_version_fields(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        data = client.get('/api/system_stats').get_json()
        for key in ('version', 'git_branch', 'git_commit'):
            assert key in data, key
        assert data['version'] == dashboard.version_info['version']
        assert data['contract_version'] == 4


@pytest.mark.unit
class TestFactoryReset:
    def test_removes_user_state_only(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        keep = ['GENERAL_CONFIG.ini', 'GEOMETRY.ini', 'CONTROLLER.ini',
                'GEOMETRY_TEMPLATE.ini', 'DESKTOP_TEST.ini']
        drop = ['config_autosave.ini', 'my_saved.ini', 'exhsv-setup_1.ini']
        for name in keep + drop:
            path = tmp_dir / name
            if not path.exists():
                path.write_text('[System]\nalgorithm = exhsv\n')
        (tmp_dir / 'active_config.txt').write_text('config/my_saved.ini')
        (tmp_dir / 'notes.txt').write_text('keep me')

        dashboard.lut_profile_manager = MagicMock()
        dashboard.lut_profile_manager.list_profiles.return_value = [
            {'name': 'starter', 'is_builtin': True},
            {'name': 'ryegrass', 'is_builtin': False},
        ]
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client.get_state.return_value = {
            'sensitivity_presets': [
                {'name': 'medium', 'is_builtin': True},
                {'name': 'dusty', 'is_builtin': False},
            ]}
        dashboard.mqtt_client._send_command.return_value = {'success': True}

        with patch.object(dashboard, '_apply_config_to_owl') as apply:
            resp = client.post('/api/config/factory-reset')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        removed = data['removed']
        assert removed['pointer'] is True
        assert removed['autosave'] is True
        # test_config.ini is the fixture's own user-style preset file
        assert sorted(removed['configs']) == ['exhsv-setup_1.ini', 'my_saved.ini',
                                              'test_config.ini']
        assert removed['lut_profiles'] == ['ryegrass']
        assert removed['presets'] == ['dusty']
        for name in keep:
            assert (tmp_dir / name).exists(), name
        for name in drop:
            assert not (tmp_dir / name).exists(), name
        assert not (tmp_dir / 'active_config.txt').exists()
        assert (tmp_dir / 'notes.txt').exists()
        dashboard.lut_profile_manager.delete.assert_called_once_with('ryegrass')
        dashboard.mqtt_client._send_command.assert_called_once_with(
            'delete_sensitivity_preset', name='dusty')
        apply.assert_called_once()

    def test_is_token_guarded(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.token_reader = lambda: 'secret-token'
        resp = client.post('/api/config/factory-reset')
        assert resp.status_code == 403
        with patch.object(dashboard, '_factory_reset_settings',
                          return_value={}), \
                patch.object(dashboard, '_apply_config_to_owl'):
            resp = client.post('/api/config/factory-reset',
                               headers={'X-Device-Token': 'secret-token'})
        assert resp.status_code == 200
