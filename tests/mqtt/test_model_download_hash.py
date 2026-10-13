"""Model download: which SHA-256 an NCNN zip is checked against, and that a
failure keeps the model already on the unit.

Noktura hashes the zip file; the networked controller hashes the extracted
files (it deletes the zip after upload). The OWL tries the zip bytes first,
then the extracted files, and only then replaces the old folder.
"""

import hashlib
import io
import os
import zipfile
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

import utils.mqtt_manager as mm_mod
from utils.model_archive import dir_sha256, extract_model_zip

FILES = {'model.param': b'7767517\nparam data', 'model.bin': b'\x00\x01weights'}


def make_zip(files=FILES, folder='weeds_ncnn_model'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in files.items():
            zf.writestr(f'{folder}/{name}' if folder else name, data)
    return buf.getvalue()


def legacy_hash(files=FILES):
    """The controller's directory hash: contents in sorted-path order."""
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(files[name])
    return h.hexdigest()


@contextmanager
def models_in(tmp_path):
    """Point the publisher's models/ at tmp_path/models (it derives the path
    from the module file, as test_model_manager.py does)."""
    original = mm_mod.__file__
    (tmp_path / 'utils').mkdir(exist_ok=True)
    mm_mod.__file__ = str(tmp_path / 'utils' / 'mqtt_manager.py')
    try:
        yield tmp_path / 'models'
    finally:
        mm_mod.__file__ = original


def download(publisher, payload, sha, filename='weeds_ncnn.zip'):
    with patch('urllib.request.urlopen') as urlopen, \
            patch.object(publisher, '_publish_state'):
        response = MagicMock()
        response.read.side_effect = [payload, b'']
        response.headers = {'Content-Length': str(len(payload))}
        urlopen.return_value = response
        publisher._download_model('https://storage.example/m.zip', filename, sha, True)
    return publisher.state['model_download']


def old_model(models):
    old = models / 'weeds_ncnn'
    old.mkdir(parents=True)
    (old / 'model.param').write_bytes(b'old param')
    return old


@pytest.mark.unit
class TestZipHashOrder:
    def test_zip_bytes_hash_matches(self, mqtt_publisher, tmp_path):
        """Noktura's hash: the zip file itself."""
        payload = make_zip()
        with models_in(tmp_path) as models:
            status = download(mqtt_publisher, payload, hashlib.sha256(payload).hexdigest())
        assert status['status'] == 'complete', status
        # Single top-level folder stripped
        assert (models / 'weeds_ncnn' / 'model.param').read_bytes() == FILES['model.param']
        assert not (models / 'weeds_ncnn' / 'weeds_ncnn_model').exists()

    def test_legacy_extracted_files_hash_matches(self, mqtt_publisher, tmp_path):
        """The networked controller's hash: extracted files, flattened."""
        with models_in(tmp_path) as models:
            old_model(models)
            status = download(mqtt_publisher, make_zip(), legacy_hash())
        assert status['status'] == 'complete', status
        assert (models / 'weeds_ncnn' / 'model.bin').read_bytes() == FILES['model.bin']
        assert (models / 'weeds_ncnn' / 'model.param').read_bytes() == FILES['model.param']

    def test_mismatch_keeps_the_old_model(self, mqtt_publisher, tmp_path):
        with models_in(tmp_path) as models:
            old_model(models)
            status = download(mqtt_publisher, make_zip(), 'f' * 64)
            leftovers = sorted(os.listdir(models))
        assert status['status'] == 'error'
        assert 'mismatch' in status['error'].lower()
        assert (models / 'weeds_ncnn' / 'model.param').read_bytes() == b'old param'
        # No staging folder or temp zip left behind
        assert leftovers == ['weeds_ncnn']

    def test_damaged_zip_keeps_the_old_model(self, mqtt_publisher, tmp_path):
        with models_in(tmp_path) as models:
            old_model(models)
            status = download(mqtt_publisher, b'not a zip at all', '')
        assert status['status'] == 'error'
        assert 'damaged' in status['error']
        assert (models / 'weeds_ncnn' / 'model.param').read_bytes() == b'old param'

    def test_staging_folders_are_not_listed_as_models(self, mqtt_publisher, tmp_path):
        with models_in(tmp_path) as models:
            staging = models / '.staging-abc'
            staging.mkdir(parents=True)
            (staging / 'model.param').write_bytes(b'x')
            assert mqtt_publisher._list_available_models() == []
            assert mqtt_publisher._check_model_available() is False


@pytest.mark.unit
class TestExtractModelZip:
    def test_flat_zip_extracts_as_is(self, tmp_path):
        zip_path = tmp_path / 'm.zip'
        zip_path.write_bytes(make_zip(folder=''))
        extract_model_zip(zip_path, tmp_path / 'out')
        assert dir_sha256(tmp_path / 'out') == legacy_hash()

    def test_path_escaping_the_folder_is_refused(self, tmp_path):
        zip_path = tmp_path / 'evil.zip'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('model/../../escape.txt', b'x')
            zf.writestr('model/model.param', b'x')
        zip_path.write_bytes(buf.getvalue())
        with pytest.raises(ValueError):
            extract_model_zip(zip_path, tmp_path / 'out')
        assert not (tmp_path.parent / 'escape.txt').exists()
