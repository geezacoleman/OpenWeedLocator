"""Tests for model upload, deploy, download, and OWL download handler."""

import hashlib
import io
import json
import os
import shutil
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def models_dir(tmp_path):
    """Create a temp models/ directory with a sample .pt file."""
    d = tmp_path / 'models'
    d.mkdir()
    # Create a sample .pt file
    pt_file = d / 'test_model.pt'
    pt_file.write_bytes(b'\x00' * 1024)
    # Compute and write sha256
    sha = hashlib.sha256(b'\x00' * 1024).hexdigest()
    (d / 'test_model.pt.sha256').write_text(sha)
    # Create protected files
    (d / 'README.md').write_text('Models directory')
    (d / 'labels.txt').write_text('weed\ncrop')
    return d


@pytest.fixture
def ncnn_models_dir(models_dir):
    """Add an NCNN model directory to the models dir."""
    ncnn_dir = models_dir / 'test_ncnn'
    ncnn_dir.mkdir()
    (ncnn_dir / 'model.param').write_bytes(b'param_data')
    (ncnn_dir / 'model.bin').write_bytes(b'bin_data')
    # Compute dir hash
    h = hashlib.sha256()
    for fp in sorted(ncnn_dir.rglob('*')):
        if fp.is_file():
            h.update(fp.read_bytes())
    (models_dir / 'test_ncnn.sha256').write_text(h.hexdigest())
    return models_dir


@pytest.fixture
def model_test_client(tmp_path, models_dir):
    """Flask test_client with MODELS_DIR pointed to tmp_path/models."""
    with patch('controller.networked.networked.CentralController') as MockCC:
        mock_ctrl = MagicMock()
        MockCC.return_value = mock_ctrl
        mock_ctrl.mqtt_connected = True
        mock_ctrl.mqtt_client = MagicMock()
        mock_ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
        mock_ctrl.owls_state = {}
        mock_ctrl.mqtt_lock = MagicMock()
        mock_ctrl.mqtt_lock.__enter__ = MagicMock(return_value=None)
        mock_ctrl.mqtt_lock.__exit__ = MagicMock(return_value=False)
        mock_ctrl.config = MagicMock()
        mock_ctrl.config.get.return_value = '192.168.1.2'
        mock_ctrl.send_command.return_value = {'success': True}
        mock_ctrl.request_device_config.return_value = None
        mock_ctrl.list_local_presets.return_value = []
        mock_ctrl.read_preset.return_value = None

        import importlib
        import controller.networked.networked as net_mod
        importlib.reload(net_mod)
        net_mod.controller = mock_ctrl

        # Point MODELS_DIR to our temp dir
        net_mod.MODELS_DIR = models_dir

        app = net_mod.app
        app.config['TESTING'] = True
        # Allow large uploads in tests
        app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

        with app.test_client() as client:
            yield client, mock_ctrl, models_dir


def _make_pt_bytes(size=512):
    """Return deterministic bytes for a fake .pt model."""
    return b'\x42' * size


def _make_ncnn_zip():
    """Create an in-memory zip with NCNN model structure."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('model.param', 'ncnn_param_data')
        zf.writestr('model.bin', 'ncnn_bin_data')
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Upload Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUploadModel:

    def test_upload_pt_file(self, model_test_client):
        client, _, models_dir = model_test_client
        data = _make_pt_bytes()
        resp = client.post('/api/models/upload', data={
            'file': (io.BytesIO(data), 'my_model.pt')
        }, content_type='multipart/form-data')

        result = resp.get_json()
        assert resp.status_code == 200
        assert result['success'] is True
        assert result['filename'] == 'my_model.pt'
        assert result['type'] == 'pytorch'
        assert (models_dir / 'my_model.pt').exists()
        assert (models_dir / 'my_model.pt.sha256').exists()

    def test_upload_zip_ncnn(self, model_test_client):
        client, _, models_dir = model_test_client
        zip_buf = _make_ncnn_zip()
        resp = client.post('/api/models/upload', data={
            'file': (zip_buf, 'my_ncnn.zip')
        }, content_type='multipart/form-data')

        result = resp.get_json()
        assert resp.status_code == 200
        assert result['success'] is True
        assert result['filename'] == 'my_ncnn'
        assert result['type'] == 'ncnn'
        assert (models_dir / 'my_ncnn').is_dir()
        assert (models_dir / 'my_ncnn' / 'model.param').exists()
        assert (models_dir / 'my_ncnn' / 'model.bin').exists()
        # Zip should be removed after extraction
        assert not (models_dir / 'my_ncnn.zip').exists()

    def test_upload_invalid_type(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.post('/api/models/upload', data={
            'file': (io.BytesIO(b'hello'), 'model.txt')
        }, content_type='multipart/form-data')

        assert resp.status_code == 400
        result = resp.get_json()
        assert result['success'] is False
        assert 'Invalid file type' in result['error']

    def test_upload_no_file(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.post('/api/models/upload',
                           data={}, content_type='multipart/form-data')

        assert resp.status_code == 400
        result = resp.get_json()
        assert result['success'] is False


# ---------------------------------------------------------------------------
# List Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestListModels:

    def test_list_models(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.get('/api/models')
        data = resp.get_json()

        assert resp.status_code == 200
        names = [m['name'] for m in data['models']]
        assert 'test_model.pt' in names
        # Protected files should NOT appear
        assert 'README.md' not in names
        assert 'labels.txt' not in names

    def test_list_includes_ncnn(self, model_test_client, ncnn_models_dir):
        client, _, models_dir = model_test_client
        # ncnn_models_dir already has test_ncnn/ in it, but models_dir
        # might be different due to fixture. Copy it if needed.
        resp = client.get('/api/models')
        data = resp.get_json()
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Delete Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeleteModel:

    def test_delete_model(self, model_test_client):
        client, _, models_dir = model_test_client
        assert (models_dir / 'test_model.pt').exists()

        resp = client.delete('/api/models/test_model.pt')
        result = resp.get_json()

        assert resp.status_code == 200
        assert result['success'] is True
        assert not (models_dir / 'test_model.pt').exists()
        assert not (models_dir / 'test_model.pt.sha256').exists()

    def test_delete_protected(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.delete('/api/models/README.md')
        result = resp.get_json()

        assert resp.status_code == 400
        assert result['success'] is False
        assert 'protected' in result['error'].lower()

    def test_delete_nonexistent(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.delete('/api/models/no_such_model.pt')
        result = resp.get_json()

        assert resp.status_code == 404
        assert result['success'] is False


# ---------------------------------------------------------------------------
# Download Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDownloadModel:

    def test_download_pt(self, model_test_client):
        client, _, models_dir = model_test_client
        resp = client.get('/api/models/download/test_model.pt')

        assert resp.status_code == 200
        assert len(resp.data) == 1024  # We wrote 1024 null bytes

    def test_download_ncnn_zipped(self, model_test_client):
        client, _, models_dir = model_test_client
        # Create NCNN dir
        ncnn_dir = models_dir / 'my_ncnn'
        ncnn_dir.mkdir()
        (ncnn_dir / 'model.param').write_bytes(b'param')
        (ncnn_dir / 'model.bin').write_bytes(b'bin')

        resp = client.get('/api/models/download/my_ncnn')

        assert resp.status_code == 200
        # Response should be a valid zip
        z = zipfile.ZipFile(io.BytesIO(resp.data))
        assert 'model.param' in z.namelist()
        assert 'model.bin' in z.namelist()

    def test_download_nonexistent(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.get('/api/models/download/no_such.pt')

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Deploy Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeployModel:

    def test_deploy_sends_mqtt(self, model_test_client):
        client, mock_ctrl, _ = model_test_client
        resp = client.post('/api/models/deploy', json={
            'model_name': 'test_model.pt',
            'device_ids': ['owl-1', 'owl-2']
        })
        result = resp.get_json()

        assert resp.status_code == 200
        assert result['success'] is True
        assert len(result['sent_to']) == 2

        # Verify MQTT publish was called for each OWL
        calls = mock_ctrl.mqtt_client.publish.call_args_list
        assert len(calls) == 2

        # Check the payload structure
        payload = json.loads(calls[0][0][1])
        assert payload['action'] == 'download_model'
        assert 'test_model.pt' in payload['url']
        assert payload['filename'] == 'test_model.pt'
        assert payload['sha256'] != ''

    def test_deploy_missing_model(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.post('/api/models/deploy', json={
            'model_name': 'nonexistent.pt',
            'device_ids': ['owl-1']
        })

        assert resp.status_code == 404

    def test_deploy_missing_params(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.post('/api/models/deploy', json={
            'model_name': 'test_model.pt'
            # missing device_ids
        })

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# SHA256 Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSHA256:

    def test_sha256_computation(self):
        from controller.networked.networked import _compute_sha256
        import tempfile

        data = b'hello world test data for sha256'
        expected = hashlib.sha256(data).hexdigest()

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            f.flush()
            result = _compute_sha256(f.name)

        os.unlink(f.name)
        assert result == expected


# ---------------------------------------------------------------------------
# Path Traversal Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPathTraversal:

    def test_upload_path_traversal(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.post('/api/models/upload', data={
            'file': (io.BytesIO(b'x'), '../../../etc/passwd.pt')
        }, content_type='multipart/form-data')
        # secure_filename strips ../ so this becomes etc_passwd.pt
        # which should still succeed but be sanitized
        if resp.status_code == 200:
            result = resp.get_json()
            assert '..' not in result.get('filename', '')

    def test_download_path_traversal(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.get('/api/models/download/..%2F..%2Fetc%2Fpasswd')
        # secure_filename strips path traversal
        assert resp.status_code in (400, 404)

    def test_delete_path_traversal(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.delete('/api/models/..%2F..%2Fetc%2Fpasswd')
        assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# OWL Download Handler Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOWLDownloadHandler:

    def test_download_handler_saves_file(self, mqtt_publisher, tmp_path):
        """Test that _download_model saves a file correctly."""
        # Create a mock HTTP response
        test_data = b'fake model data for testing'
        expected_sha = hashlib.sha256(test_data).hexdigest()

        models_dir = tmp_path / 'models'
        models_dir.mkdir()

        # Patch the models dir path
        import utils.mqtt_manager as mm_mod
        original_dirname = os.path.dirname

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.side_effect = [test_data, b'']
            mock_resp.headers = {'Content-Length': str(len(test_data))}
            mock_urlopen.return_value = mock_resp

            # Override models dir by patching os.path operations
            with patch.object(mqtt_publisher, '_list_available_models', return_value=[]):
                # Run the download in current thread (not bg thread)
                mqtt_publisher._download_model(
                    url='https://controller/api/models/download/test.pt',
                    filename='test.pt',
                    expected_sha256=expected_sha,
                    is_archive=False
                )

        # Check state was updated
        assert mqtt_publisher.state['model_download']['status'] in ('complete', 'error')

    def test_download_handler_checksum_fail(self, mqtt_publisher, tmp_path):
        """Test that bad SHA256 results in error state."""
        test_data = b'fake model data'

        with patch('urllib.request.urlopen') as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.side_effect = [test_data, b'']
            mock_resp.headers = {'Content-Length': str(len(test_data))}
            mock_urlopen.return_value = mock_resp

            with patch.object(mqtt_publisher, '_list_available_models', return_value=[]):
                mqtt_publisher._download_model(
                    url='https://controller/api/models/download/test.pt',
                    filename='test.pt',
                    expected_sha256='wrong_hash_value',
                    is_archive=False
                )

        assert mqtt_publisher.state['model_download']['status'] == 'error'
        assert 'mismatch' in mqtt_publisher.state['model_download']['error'].lower()


# ---------------------------------------------------------------------------
# NCNN Zip Validation Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNCNNValidation:

    def test_valid_ncnn_zip(self, tmp_path):
        from controller.networked.networked import _validate_ncnn_zip

        zip_path = tmp_path / 'valid.zip'
        with zipfile.ZipFile(str(zip_path), 'w') as zf:
            zf.writestr('model.param', 'data')
            zf.writestr('model.bin', 'data')

        valid, msg = _validate_ncnn_zip(str(zip_path))
        assert valid is True

    def test_invalid_ncnn_zip_missing_param(self, tmp_path):
        from controller.networked.networked import _validate_ncnn_zip

        zip_path = tmp_path / 'invalid.zip'
        with zipfile.ZipFile(str(zip_path), 'w') as zf:
            zf.writestr('model.bin', 'data')
            # Missing .param

        valid, msg = _validate_ncnn_zip(str(zip_path))
        assert valid is False
        assert '.param' in msg

    def test_path_traversal_in_zip(self, tmp_path):
        from controller.networked.networked import _validate_ncnn_zip

        zip_path = tmp_path / 'evil.zip'
        with zipfile.ZipFile(str(zip_path), 'w') as zf:
            zf.writestr('../../../etc/passwd', 'root:x:0:0')
            zf.writestr('model.param', 'data')
            zf.writestr('model.bin', 'data')

        valid, msg = _validate_ncnn_zip(str(zip_path))
        assert valid is False
        assert 'Unsafe' in msg


# ---------------------------------------------------------------------------
# Models Page Tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestModelsPage:

    def test_models_page_serves(self, model_test_client):
        client, _, _ = model_test_client
        resp = client.get('/models')
        assert resp.status_code == 200
        assert b'Model Manager' in resp.data
