"""Path confinement on the standalone dashboard (security review 2026-10-18).

Every route that takes a file or config name from a request must stay inside
its own directory. Before this, '/media/../home/owl/owl/config/CONTROLLER.ini'
passed the USB download's prefix check (device token + cloud password out),
'config/../owl.py' reached os.remove, and set-active accepted CONTROLLER.ini,
after which GET /api/config served the device token.
"""

import os
from unittest.mock import patch

import pytest

import controller.standalone.standalone as sa_mod
from utils.config_manager import config_profile_path


@pytest.mark.unit
class TestConfigProfilePath:

    def test_accepts_profiles_with_and_without_prefix(self, tmp_path):
        expected = os.path.realpath(tmp_path / 'DAY_SENSITIVITY_2.ini')
        assert config_profile_path(tmp_path, 'DAY_SENSITIVITY_2.ini') == expected
        assert config_profile_path(tmp_path, 'config/DAY_SENSITIVITY_2.ini') == expected
        assert config_profile_path(tmp_path, ' config/DAY_SENSITIVITY_2.ini\n') == expected

    def test_file_need_not_exist(self, tmp_path):
        assert config_profile_path(tmp_path, 'NOT_YET.ini') is not None

    @pytest.mark.parametrize('name', [
        'CONTROLLER.ini', 'config/CONTROLLER.ini', 'GEOMETRY.ini', 'GENERAL_TEMPLATE.ini',
        'config/../owl.py', '../owl.ini', 'config/../../etc/x.ini', 'sub/x.ini',
        '..\\x.ini', '/etc/passwd', 'x.txt', 'active_config.txt', '.hidden.ini',
        'x.ini\x00', '', '   ', 'config/', None, 42,
    ])
    def test_rejects_everything_else(self, tmp_path, name):
        assert config_profile_path(tmp_path, name) is None

    def test_rejects_symlink_out_of_config_dir(self, tmp_path):
        outside = tmp_path / 'outside.ini'
        outside.write_text('[Security]\ndevice_token = x\n')
        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        try:
            os.symlink(outside, config_dir / 'link.ini')
        except (OSError, NotImplementedError):
            pytest.skip('symlinks not available')
        assert config_profile_path(config_dir, 'link.ini') is None


@pytest.mark.unit
class TestUsbPath:

    def test_inside_media(self):
        assert sa_mod.usb_path('/media/owl/USB/session.zip') == os.path.realpath('/media/owl/USB/session.zip')
        assert sa_mod.usb_path('/media') == os.path.realpath('/media')

    @pytest.mark.parametrize('path', [
        '/media/../home/owl/owl/config/CONTROLLER.ini',
        '/media/owl/../../home/owl/.owl/cloud/credentials',
        '/mediafoo/x', '/home/owl/x', '', None, '/media/x\x00',
    ])
    def test_outside_media(self, path):
        assert sa_mod.usb_path(path) is None


@pytest.mark.unit
class TestUsbRoutes:

    def test_download_traversal_is_404_and_reads_nothing(self, standalone_test_client):
        client, _, _ = standalone_test_client
        with patch.object(sa_mod, 'send_file') as send:
            resp = client.get('/api/download_file?path=/media/../home/owl/owl/config/CONTROLLER.ini')
        assert resp.status_code == 404
        send.assert_not_called()

    def test_browse_traversal_denied(self, standalone_test_client):
        client, dashboard, _ = standalone_test_client
        with patch.object(dashboard, 'browse_files') as browse:
            resp = client.post('/api/browse_files', json={'directory': '/media/../home/owl'})
        assert resp.get_json()['error'] == 'Directory access denied'
        browse.assert_not_called()

    def test_navigate_traversal_denied(self, standalone_test_client):
        client, _, _ = standalone_test_client
        resp = client.post('/api/navigate_directory', json={'directory': '/media/../etc'})
        assert resp.status_code == 403


@pytest.mark.unit
class TestConfigRoutes:

    def test_delete_traversal_refused_and_file_kept(self, standalone_test_client):
        client, _, tmp_dir = standalone_test_client
        victim = tmp_dir.parent / 'owl.py'
        victim.write_text('# the unit\'s own code')
        resp = client.post('/api/config/delete', json={'config': 'config/../owl.py'})
        assert resp.status_code == 400
        assert victim.exists()

    def test_delete_controller_ini_refused(self, standalone_test_client):
        client, _, tmp_dir = standalone_test_client
        (tmp_dir / 'CONTROLLER.ini').write_text('[Security]\ndevice_token = secret\n')
        resp = client.post('/api/config/delete', json={'config': 'CONTROLLER.ini'})
        assert resp.status_code == 400
        assert (tmp_dir / 'CONTROLLER.ini').exists()

    def test_set_active_refuses_controller_ini(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        (tmp_dir / 'CONTROLLER.ini').write_text('[Security]\ndevice_token = secret\n')
        pointer = tmp_dir / 'active_config.txt'
        before = pointer.read_text()
        with patch.object(dashboard, '_apply_config_to_owl') as apply:
            resp = client.post('/api/config/set-active', json={'config': 'CONTROLLER.ini'})
        assert resp.status_code == 400
        assert pointer.read_text() == before
        apply.assert_not_called()

    def test_set_active_traversal_refused(self, standalone_test_client):
        client, dashboard, _ = standalone_test_client
        with patch.object(dashboard, '_set_active_config') as set_active:
            resp = client.post('/api/config/set-active', json={'config': 'config/../../etc/x.ini'})
        assert resp.status_code == 400
        set_active.assert_not_called()

    def test_controller_ini_not_listed(self, standalone_test_client):
        client, _, tmp_dir = standalone_test_client
        (tmp_dir / 'CONTROLLER.ini').write_text('[Security]\ndevice_token = secret\n')
        names = [c['name'] for c in client.get('/api/config').get_json()['available_configs']]
        assert 'CONTROLLER.ini' not in names
        assert 'GENERAL_CONFIG.ini' in names

    def test_hostile_pointer_falls_back_to_default(self, standalone_test_client, tmp_path, monkeypatch):
        """A pointer already naming CONTROLLER.ini (written by hand or by an
        old MQTT command) must not make GET /api/config serve it."""
        _, dashboard, _ = standalone_test_client
        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        (config_dir / 'CONTROLLER.ini').write_text('[Security]\ndevice_token = secret\n')
        (config_dir / 'active_config.txt').write_text('config/CONTROLLER.ini')
        # _get_active_config_path finds the pointer next to the module file
        monkeypatch.setattr(sa_mod, '__file__', str(tmp_path / 'standalone.py'))
        dashboard._get_config_dir = lambda: str(config_dir)
        # The fixture swaps in its own reader; test the real one.
        read_pointer = sa_mod.OWLDashboard._get_active_config_path
        assert read_pointer(dashboard) == 'config/GENERAL_CONFIG.ini'

        (config_dir / 'active_config.txt').write_text('config/DAY_SENSITIVITY_2.ini')
        assert read_pointer(dashboard) == 'config/DAY_SENSITIVITY_2.ini'

    @pytest.mark.parametrize('name', ['../owl', 'CONTROLLER.ini', 'GENERAL_TEMPLATE.ini', 'sub/x.ini'])
    def test_save_overwrite_refuses_non_profiles(self, standalone_test_client, name):
        client, _, _ = standalone_test_client
        with patch.object(sa_mod, 'atomic_write_config') as write:
            resp = client.post('/api/config', json={'config': {'System': {'algorithm': 'exg'}},
                                                    'overwrite_filename': name})
        assert resp.status_code == 400
        write.assert_not_called()
