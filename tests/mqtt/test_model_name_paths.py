"""Model names in set_model / download_model must stay inside models/.

A cloud or LAN command names the file the OWL writes and, for an archive, the
directory it rmtree's. `../owl.py` used to overwrite the unit's own code, so a
command from a compromised or mis-routed cloud link became code execution.
The name is checked before anything is fetched or deleted.
"""

import os
from unittest.mock import patch

import pytest

from utils.mqtt_manager import model_path_in


@pytest.mark.unit
class TestModelPathIn:
    def test_plain_name_resolves_inside(self, tmp_path):
        assert model_path_in(str(tmp_path), 'm.pt') == os.path.join(os.path.realpath(tmp_path), 'm.pt')

    @pytest.mark.parametrize('name', [None, '', '.', '..', '../x', 'a/b', 'a\\b',
                                      '/abs', 'nul\x00.pt', '.git', 7])
    def test_rejects(self, tmp_path, name):
        assert model_path_in(str(tmp_path), name) is None

    def test_symlink_out_of_models_rejected(self, tmp_path):
        models = tmp_path / 'models'
        models.mkdir()
        try:
            os.symlink(str(tmp_path), str(models / 'escape'))
        except (OSError, NotImplementedError):
            pytest.skip('symlinks unavailable')
        assert model_path_in(str(models), 'escape') is None


@pytest.mark.unit
class TestSetModelPaths:
    def test_plain_name_queued(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_command({'action': 'set_model', 'value': 'yolo26n-seg.pt'})
        assert mock_owl._pending_model == os.path.join('models', 'yolo26n-seg.pt')

    @pytest.mark.parametrize('name', ['../owl.py', 'a/../../owl.py', '/etc/passwd',
                                      '..', '.hidden.pt', 'sub/model.pt'])
    def test_path_outside_models_refused(self, mqtt_publisher, mock_owl, name):
        mock_owl._pending_model = None
        mqtt_publisher._handle_command({'action': 'set_model', 'value': name})
        assert mock_owl._pending_model is None
        assert mqtt_publisher.state.get('current_model') != name


@pytest.mark.unit
class TestDownloadModelPaths:
    @pytest.mark.parametrize('filename,is_archive', [
        ('../owl.py', False),
        ('a/../../owl.py', False),
        ('/home/owl/owl/owl.py', False),
        ('..\\owl.py', False),
        ('../../controller.zip', True),
        ('x/../../utils.zip', True),
        ('...zip', True),
    ])
    def test_traversal_refused_before_fetch(self, mqtt_publisher, filename, is_archive):
        with patch('urllib.request.urlopen',
                   side_effect=OSError('offline in test')) as urlopen:
            mqtt_publisher._download_model('https://storage.example/m', filename, '', is_archive)
        assert not urlopen.called
        status = mqtt_publisher.state['model_download']
        assert status['status'] == 'error'
        assert 'not a model file name' in status['error']

    @pytest.mark.parametrize('filename,is_archive', [
        ('yolo26n-seg.pt', False), ('weeds_ncnn.zip', True), ('v1.2.model.pt', False)])
    def test_plain_names_are_fetched(self, mqtt_publisher, filename, is_archive):
        with patch('urllib.request.urlopen',
                   side_effect=OSError('offline in test')) as urlopen:
            mqtt_publisher._download_model('https://storage.example/m', filename, '', is_archive)
        assert urlopen.called
        assert 'offline in test' in mqtt_publisher.state['model_download']['error']
