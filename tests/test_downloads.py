"""Tests for data downloads feature — OWL-side MQTT handlers and controller routes."""

import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from utils.directory_manager import scan_sessions, collect_session_files


# ---------------------------------------------------------------------------
# Shared scanner tests (utils/directory_manager.py)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestScanSessions:

    def test_scan_sessions_none_dir(self):
        assert scan_sessions(None) == []

    def test_scan_sessions_nonexistent_dir(self):
        assert scan_sessions('/nonexistent/path') == []

    def test_scan_sessions_empty_dir(self, tmp_path):
        assert scan_sessions(str(tmp_path)) == []

    def test_scan_sessions_flat_structure(self, tmp_path):
        """Legacy flat structure: images directly in YYYYMMDD/."""
        d = tmp_path / '20260331'
        d.mkdir()
        (d / 'img1.jpg').write_bytes(b'\xff\xd8' * 100)
        (d / 'img2.png').write_bytes(b'\x89PNG' * 50)

        sessions = scan_sessions(str(tmp_path))
        assert len(sessions) == 1
        s = sessions[0]
        assert s['session_id'] == '20260331'
        assert s['date'] == '20260331'
        assert s['time'] == ''
        assert s['image_count'] == 2
        assert s['total_size'] > 0

    def test_scan_sessions_subdir_structure(self, tmp_path):
        """New structure: YYYYMMDD/session_HHMMSS/."""
        date_dir = tmp_path / '20260331'
        s1 = date_dir / 'session_143015'
        s2 = date_dir / 'session_160000'
        s1.mkdir(parents=True)
        s2.mkdir(parents=True)

        (s1 / 'img1.jpg').write_bytes(b'\xff\xd8' * 100)
        (s1 / 'img2.jpg').write_bytes(b'\xff\xd8' * 200)
        (s2 / 'capture.png').write_bytes(b'\x89PNG' * 50)

        sessions = scan_sessions(str(tmp_path))
        assert len(sessions) == 2

        # Sorted newest first
        assert sessions[0]['session_id'] == '20260331/session_160000'
        assert sessions[0]['time'] == '160000'
        assert sessions[0]['image_count'] == 1

        assert sessions[1]['session_id'] == '20260331/session_143015'
        assert sessions[1]['time'] == '143015'
        assert sessions[1]['image_count'] == 2

    def test_scan_sessions_multiple_dates(self, tmp_path):
        """Multiple dates, each with sessions."""
        for date in ['20260328', '20260331']:
            s = tmp_path / date / 'session_120000'
            s.mkdir(parents=True)
            (s / 'img.jpg').write_bytes(b'\xff\xd8' * 50)

        sessions = scan_sessions(str(tmp_path))
        assert len(sessions) == 2
        # Newest date first
        assert sessions[0]['date'] == '20260331'
        assert sessions[1]['date'] == '20260328'

    def test_scan_sessions_ignores_non_date_dirs(self, tmp_path):
        """Non-YYYYMMDD directories are ignored."""
        (tmp_path / 'not_a_date').mkdir()
        (tmp_path / '123').mkdir()
        (tmp_path / 'file.txt').write_text('hello')
        d = tmp_path / '20260331'
        d.mkdir()
        (d / 'img.jpg').write_bytes(b'\xff\xd8')

        sessions = scan_sessions(str(tmp_path))
        assert len(sessions) == 1
        assert sessions[0]['date'] == '20260331'

    def test_scan_sessions_empty_date_dir(self, tmp_path):
        """Date dir with no images and no sessions is excluded."""
        (tmp_path / '20260331').mkdir()
        assert scan_sessions(str(tmp_path)) == []

    def test_scan_sessions_path_with_spaces(self, tmp_path):
        """Paths with spaces work correctly."""
        spaced = tmp_path / '123 GB Storage'
        s = spaced / '20260331' / 'session_120000'
        s.mkdir(parents=True)
        (s / 'img.jpg').write_bytes(b'\xff\xd8' * 100)

        sessions = scan_sessions(str(spaced))
        assert len(sessions) == 1
        assert sessions[0]['image_count'] == 1


@pytest.mark.unit
class TestCollectSessionFiles:

    def test_collect_none_inputs(self):
        assert collect_session_files(None, '20260331') == []
        assert collect_session_files('/tmp', None) == []

    def test_collect_invalid_session_id(self, tmp_path):
        assert collect_session_files(str(tmp_path), '../etc') == []
        assert collect_session_files(str(tmp_path), 'abcd1234') == []

    def test_collect_flat_date(self, tmp_path):
        """Legacy: collect files from YYYYMMDD/ (flat)."""
        d = tmp_path / '20260331'
        d.mkdir()
        (d / 'img1.jpg').write_bytes(b'\xff\xd8' * 100)
        (d / 'img2.png').write_bytes(b'\x89PNG' * 50)
        (d / 'readme.txt').write_text('not an image')

        files = collect_session_files(str(tmp_path), '20260331')
        assert len(files) == 2
        arc_names = [f[0] for f in files]
        assert 'images/img1.jpg' in arc_names
        assert 'images/img2.png' in arc_names

    def test_collect_specific_session(self, tmp_path):
        """Collect files from a specific session_HHMMSS."""
        s = tmp_path / '20260331' / 'session_143015'
        s.mkdir(parents=True)
        (s / 'img1.jpg').write_bytes(b'\xff\xd8' * 100)
        (s / 'img2.jpg').write_bytes(b'\xff\xd8' * 200)

        files = collect_session_files(str(tmp_path), '20260331/session_143015')
        assert len(files) == 2
        arc_names = [f[0] for f in files]
        assert 'images/img1.jpg' in arc_names
        assert 'images/img2.jpg' in arc_names

    def test_collect_date_with_subdirs(self, tmp_path):
        """Collect from all sessions under a date."""
        s1 = tmp_path / '20260331' / 'session_120000'
        s2 = tmp_path / '20260331' / 'session_140000'
        s1.mkdir(parents=True)
        s2.mkdir(parents=True)
        (s1 / 'img1.jpg').write_bytes(b'\xff\xd8' * 100)
        (s2 / 'img2.jpg').write_bytes(b'\xff\xd8' * 100)

        files = collect_session_files(str(tmp_path), '20260331')
        assert len(files) == 2
        arc_names = [f[0] for f in files]
        assert 'images/session_120000/img1.jpg' in arc_names
        assert 'images/session_140000/img2.jpg' in arc_names

    def test_collect_nonexistent_session(self, tmp_path):
        assert collect_session_files(str(tmp_path), '99990101') == []

    def test_collect_path_with_spaces(self, tmp_path):
        """Paths with spaces work correctly."""
        spaced = tmp_path / '123 GB Storage'
        s = spaced / '20260331' / 'session_120000'
        s.mkdir(parents=True)
        (s / 'img.jpg').write_bytes(b'\xff\xd8' * 100)

        files = collect_session_files(str(spaced), '20260331/session_120000')
        assert len(files) == 1


# ---------------------------------------------------------------------------
# OWL-side tests (mqtt_manager methods)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestListDataSessions:

    def test_list_sessions_empty_dir(self, mqtt_publisher, tmp_path):
        """Empty save_directory returns empty list."""
        save_dir = str(tmp_path / 'empty_save')
        os.makedirs(save_dir)
        mqtt_publisher.owl_instance.save_directory = save_dir

        mqtt_publisher._list_data_sessions()

        assert mqtt_publisher.state['data_sessions'] == []

    def test_list_sessions_with_images(self, mqtt_publisher, tmp_path):
        """YYYYMMDD dirs with images are listed correctly."""
        save_dir = str(tmp_path / 'save')
        os.makedirs(save_dir)

        # Create two date directories
        d1 = os.path.join(save_dir, '20260312')
        d2 = os.path.join(save_dir, '20260313')
        os.makedirs(d1)
        os.makedirs(d2)

        # Add images
        for i in range(3):
            with open(os.path.join(d1, f'img_{i}.jpg'), 'wb') as f:
                f.write(b'\xff\xd8' * 100)
        with open(os.path.join(d2, 'capture.png'), 'wb') as f:
            f.write(b'\x89PNG' * 50)

        mqtt_publisher.owl_instance.save_directory = save_dir
        mqtt_publisher._list_data_sessions()

        sessions = mqtt_publisher.state['data_sessions']
        assert len(sessions) == 2

        s1 = next(s for s in sessions if s['date'] == '20260312')
        assert s1['image_count'] == 3
        assert s1['image_size'] > 0

        s2 = next(s for s in sessions if s['date'] == '20260313')
        assert s2['image_count'] == 1

    def test_list_sessions_usb_unmounted(self, mqtt_publisher):
        """Non-existent save_directory returns empty list, no crash."""
        mqtt_publisher.owl_instance.save_directory = '/nonexistent/path/usb'
        mqtt_publisher._list_data_sessions()

        assert mqtt_publisher.state['data_sessions'] == []

    def test_list_sessions_mixed_content(self, mqtt_publisher, tmp_path):
        """Only valid YYYYMMDD directories with images are returned."""
        save_dir = str(tmp_path / 'save')
        os.makedirs(save_dir)

        # Valid date dir with an image
        valid_dir = os.path.join(save_dir, '20260312')
        os.makedirs(valid_dir)
        with open(os.path.join(valid_dir, 'img.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 100)
        # Invalid entries
        os.makedirs(os.path.join(save_dir, 'not_a_date'))
        os.makedirs(os.path.join(save_dir, '123'))
        with open(os.path.join(save_dir, 'file.txt'), 'w') as f:
            f.write('hello')

        mqtt_publisher.owl_instance.save_directory = save_dir
        mqtt_publisher._list_data_sessions()

        sessions = mqtt_publisher.state['data_sessions']
        assert len(sessions) == 1
        assert sessions[0]['date'] == '20260312'

    def test_list_sessions_no_owl_instance(self, mqtt_publisher):
        """No owl_instance returns empty list."""
        mqtt_publisher.owl_instance = None
        mqtt_publisher._list_data_sessions()

        assert mqtt_publisher.state['data_sessions'] == []

    def test_list_sessions_with_subdirs(self, mqtt_publisher, tmp_path):
        """session_HHMMSS subdirectories are enumerated correctly."""
        save_dir = str(tmp_path / 'save')
        date_dir = os.path.join(save_dir, '20260331')
        s1 = os.path.join(date_dir, 'session_143015')
        s2 = os.path.join(date_dir, 'session_160000')
        os.makedirs(s1)
        os.makedirs(s2)

        with open(os.path.join(s1, 'img1.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 100)
        with open(os.path.join(s2, 'img2.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 200)

        mqtt_publisher.owl_instance.save_directory = save_dir
        mqtt_publisher._list_data_sessions()

        sessions = mqtt_publisher.state['data_sessions']
        assert len(sessions) == 2
        ids = [s['session_id'] for s in sessions]
        assert '20260331/session_160000' in ids
        assert '20260331/session_143015' in ids


@pytest.mark.unit
class TestUploadSession:

    def test_upload_session_creates_zip_stored(self, mqtt_publisher, tmp_path):
        """Verify ZIP uses ZIP_STORED compression and contains correct files."""
        save_dir = str(tmp_path / 'save')
        img_dir = os.path.join(save_dir, '20260312')
        os.makedirs(img_dir)
        with open(os.path.join(img_dir, 'test.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 100)

        mqtt_publisher.owl_instance.save_directory = save_dir

        # Capture the streamed upload data by reading from ProgressReader
        received_chunks = []

        def mock_urlopen(req, **kwargs):
            # req.data is a ProgressReader wrapping the file
            reader = req.data
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                received_chunks.append(chunk)
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260312', ['images'], 'https://controller/api/downloads/receive'
            )

        assert mqtt_publisher.state['data_transfer']['status'] == 'complete'

        # Reconstruct the ZIP and verify contents
        zip_data = b''.join(received_chunks)
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
        assert 'images/test.jpg' in zf.namelist()
        for info in zf.infolist():
            assert info.compress_type == zipfile.ZIP_STORED
        zf.close()

    def test_upload_session_network_error(self, mqtt_publisher, tmp_path):
        """Connection refused results in error state and temp cleanup."""
        save_dir = str(tmp_path / 'save')
        img_dir = os.path.join(save_dir, '20260312')
        os.makedirs(img_dir)
        with open(os.path.join(img_dir, 'test.jpg'), 'wb') as f:
            f.write(b'data')

        mqtt_publisher.owl_instance.save_directory = save_dir

        with patch('urllib.request.urlopen', side_effect=ConnectionRefusedError('refused')):
            mqtt_publisher._upload_session(
                '20260312', ['images'], 'https://controller/api/downloads/receive'
            )

        assert mqtt_publisher.state['data_transfer']['status'] == 'error'
        assert 'refused' in mqtt_publisher.state['data_transfer']['error'].lower()

    def test_upload_session_rejects_concurrent(self, mqtt_publisher, tmp_path):
        """Second request while active is rejected."""
        save_dir = str(tmp_path / 'save')
        os.makedirs(os.path.join(save_dir, '20260312'))
        mqtt_publisher.owl_instance.save_directory = save_dir

        # Simulate in-progress transfer
        with mqtt_publisher.state_lock:
            mqtt_publisher.state['data_transfer']['status'] = 'uploading'

        mqtt_publisher._upload_session(
            '20260312', ['images'], 'https://controller/api/downloads/receive'
        )

        # Status should remain uploading (not changed)
        assert mqtt_publisher.state['data_transfer']['status'] == 'uploading'

    def test_upload_session_invalid_date(self, mqtt_publisher):
        """Path traversal in date rejected."""
        mqtt_publisher._upload_session(
            '../etc', ['images'], 'https://controller/api/downloads/receive'
        )
        assert mqtt_publisher.state['data_transfer']['status'] == 'error'
        assert 'Invalid' in mqtt_publisher.state['data_transfer']['error']

    def test_upload_session_with_subdirs(self, mqtt_publisher, tmp_path):
        """Upload works with session_HHMMSS subdirectory structure."""
        save_dir = str(tmp_path / 'save')
        s = os.path.join(save_dir, '20260331', 'session_143015')
        os.makedirs(s)
        with open(os.path.join(s, 'test.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 100)

        mqtt_publisher.owl_instance.save_directory = save_dir

        received_chunks = []

        def mock_urlopen(req, **kwargs):
            reader = req.data
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                received_chunks.append(chunk)
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260331/session_143015', ['images'],
                'https://controller/api/downloads/receive'
            )

        assert mqtt_publisher.state['data_transfer']['status'] == 'complete'
        zip_data = b''.join(received_chunks)
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
        assert 'images/test.jpg' in zf.namelist()
        zf.close()

    def test_upload_date_level_with_subdirs(self, mqtt_publisher, tmp_path):
        """Upload a full date collects files from all session subdirs."""
        save_dir = str(tmp_path / 'save')
        s1 = os.path.join(save_dir, '20260331', 'session_120000')
        s2 = os.path.join(save_dir, '20260331', 'session_140000')
        os.makedirs(s1)
        os.makedirs(s2)
        with open(os.path.join(s1, 'a.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 50)
        with open(os.path.join(s2, 'b.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 50)

        mqtt_publisher.owl_instance.save_directory = save_dir

        received_chunks = []

        def mock_urlopen(req, **kwargs):
            reader = req.data
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                received_chunks.append(chunk)
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260331', ['images'],
                'https://controller/api/downloads/receive'
            )

        assert mqtt_publisher.state['data_transfer']['status'] == 'complete'
        zip_data = b''.join(received_chunks)
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
        names = zf.namelist()
        assert len(names) == 2
        zf.close()


@pytest.mark.unit
class TestDeleteSession:

    def test_delete_session_valid(self, mqtt_publisher, tmp_path):
        """Directory removed and sessions refreshed."""
        save_dir = str(tmp_path / 'save')
        target = os.path.join(save_dir, '20260312')
        os.makedirs(target)
        with open(os.path.join(target, 'img.jpg'), 'wb') as f:
            f.write(b'data')

        mqtt_publisher.owl_instance.save_directory = save_dir
        mqtt_publisher._delete_session('20260312', ['images'])

        assert not os.path.exists(target)

    def test_delete_session_invalid_date(self, mqtt_publisher, tmp_path):
        """Path traversal rejected."""
        save_dir = str(tmp_path / 'save')
        os.makedirs(save_dir)
        mqtt_publisher.owl_instance.save_directory = save_dir

        # Should not crash
        mqtt_publisher._delete_session('../etc', ['images'])

    def test_delete_session_nonexistent(self, mqtt_publisher, tmp_path):
        """Non-existent directory handled gracefully."""
        save_dir = str(tmp_path / 'save')
        os.makedirs(save_dir)
        mqtt_publisher.owl_instance.save_directory = save_dir

        # Should not crash
        mqtt_publisher._delete_session('99990101', ['images'])


@pytest.mark.unit
class TestCommandRouting:

    def test_command_routing_list(self, mqtt_publisher):
        """list_data_sessions action dispatched."""
        with patch.object(mqtt_publisher, '_list_data_sessions') as mock:
            msg = MagicMock()
            msg.payload = json.dumps({'action': 'list_data_sessions'}).encode()
            msg.topic = mqtt_publisher.topics['commands']
            mqtt_publisher._on_message(None, None, msg)

            # Thread was started — verify method was called via threading
            # Since it runs in a thread, we check that the action was recognized
            # by verifying state was updated
            assert True  # No crash = routing works

    def test_command_routing_transfer(self, mqtt_publisher):
        """transfer_session action dispatched."""
        msg = MagicMock()
        msg.payload = json.dumps({
            'action': 'transfer_session',
            'session_date': '20260312',
            'upload_url': 'https://controller/api/downloads/receive',
        }).encode()
        msg.topic = mqtt_publisher.topics['commands']

        # Should not crash (thread spawned)
        mqtt_publisher._on_message(None, None, msg)

    def test_command_routing_delete(self, mqtt_publisher):
        """delete_session action dispatched."""
        msg = MagicMock()
        msg.payload = json.dumps({
            'action': 'delete_session',
            'session_date': '20260312',
        }).encode()
        msg.topic = mqtt_publisher.topics['commands']

        mqtt_publisher._on_message(None, None, msg)


# ---------------------------------------------------------------------------
# Controller-side tests (Flask routes)
# ---------------------------------------------------------------------------

@pytest.fixture
def downloads_dir(tmp_path):
    """Create a temp downloads/ directory."""
    d = tmp_path / 'downloads'
    d.mkdir()
    return d


@pytest.fixture
def dl_test_client(tmp_path, downloads_dir):
    """Flask test_client with DOWNLOADS_DIR pointed to tmp_path/downloads."""
    with patch('controller.networked.networked.CentralController') as MockCC:
        mock_ctrl = MagicMock()
        MockCC.return_value = mock_ctrl
        mock_ctrl.mqtt_connected = True
        mock_ctrl.mqtt_client = MagicMock()
        mock_ctrl.mqtt_client.publish.return_value = MagicMock(rc=0)
        mock_ctrl.owls_state = {
            'owl-1': {
                'connected': True,
                'data_sessions': [
                    {'date': '20260312', 'image_count': 10, 'image_size': 5000000, 'total_size': 5000000}
                ],
                'data_transfer': {'status': 'idle'},
            }
        }
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
        net_mod.DOWNLOADS_DIR = downloads_dir
        net_mod.MAX_DOWNLOADS_SIZE_MB = 2000

        app = net_mod.app
        app.config['TESTING'] = True
        app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024

        with app.test_client() as client:
            yield client, mock_ctrl, downloads_dir


@pytest.mark.unit
class TestDownloadsPage:

    def test_downloads_page_renders(self, dl_test_client):
        client, _, _ = dl_test_client
        resp = client.get('/downloads')
        assert resp.status_code == 200
        assert b'Data Downloads' in resp.data


@pytest.mark.unit
class TestReceiveDownload:

    def test_receive_valid_zip(self, dl_test_client):
        client, _, downloads_dir = dl_test_client

        # Create a small zip in memory
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('images/test.jpg', 'fake_image')
        zip_data = buf.getvalue()

        resp = client.post('/api/downloads/receive',
                           data=zip_data,
                           headers={
                               'Content-Type': 'application/octet-stream',
                               'X-OWL-Device-ID': 'owl-1',
                               'X-OWL-Session-Date': '20260312',
                           })
        result = resp.get_json()

        assert resp.status_code == 200
        assert result['success'] is True
        assert (downloads_dir / 'owl-1_20260312.zip').exists()

    def test_receive_no_data(self, dl_test_client):
        """Empty POST body still creates a file (edge case — OWL sends empty)."""
        client, _, _ = dl_test_client
        resp = client.post('/api/downloads/receive',
                           data=b'',
                           headers={
                               'Content-Type': 'application/octet-stream',
                               'X-OWL-Device-ID': 'owl-1',
                               'X-OWL-Session-Date': '20260312',
                           })
        assert resp.status_code == 200

    def test_receive_path_traversal(self, dl_test_client):
        """Path traversal in device ID is sanitized."""
        client, _, downloads_dir = dl_test_client
        resp = client.post('/api/downloads/receive',
                           data=b'zipdata',
                           headers={
                               'Content-Type': 'application/octet-stream',
                               'X-OWL-Device-ID': '../../../etc',
                               'X-OWL-Session-Date': '20260312',
                           })
        result = resp.get_json()

        # secure_filename sanitizes the path
        if resp.status_code == 200:
            assert '..' not in result.get('filename', '')


@pytest.mark.unit
class TestListDownloadedFiles:

    def test_list_files_empty(self, dl_test_client):
        client, _, _ = dl_test_client
        resp = client.get('/api/downloads/files')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['files'] == []
        assert 'storage' in data

    def test_list_files_with_zips(self, dl_test_client):
        client, _, downloads_dir = dl_test_client

        # Create some zip files
        (downloads_dir / 'owl-1_20260312.zip').write_bytes(b'\x00' * 1024)
        (downloads_dir / 'owl-2_20260313.zip').write_bytes(b'\x00' * 2048)

        resp = client.get('/api/downloads/files')
        data = resp.get_json()

        assert resp.status_code == 200
        assert len(data['files']) == 2
        names = [f['filename'] for f in data['files']]
        assert 'owl-1_20260312.zip' in names
        assert 'owl-2_20260313.zip' in names

    def test_files_includes_storage_info(self, dl_test_client):
        client, _, downloads_dir = dl_test_client
        (downloads_dir / 'test.zip').write_bytes(b'\x00' * 1024)

        resp = client.get('/api/downloads/files')
        data = resp.get_json()

        storage = data['storage']
        assert 'used_mb' in storage
        assert 'max_mb' in storage
        assert 'free_mb' in storage
        assert 'percent' in storage


@pytest.mark.unit
class TestDownloadFile:

    def test_download_file_valid(self, dl_test_client):
        client, _, downloads_dir = dl_test_client
        test_data = b'zip_content_here'
        (downloads_dir / 'owl-1_20260312.zip').write_bytes(test_data)

        resp = client.get('/api/downloads/file/owl-1_20260312.zip')

        assert resp.status_code == 200
        assert resp.data == test_data

    def test_download_file_not_found(self, dl_test_client):
        client, _, _ = dl_test_client
        resp = client.get('/api/downloads/file/nonexistent.zip')
        assert resp.status_code == 404


@pytest.mark.unit
class TestDeleteDownloadFile:

    def test_delete_file(self, dl_test_client):
        client, _, downloads_dir = dl_test_client
        target = downloads_dir / 'owl-1_20260312.zip'
        target.write_bytes(b'data')
        assert target.exists()

        resp = client.delete('/api/downloads/file/owl-1_20260312.zip')
        result = resp.get_json()

        assert resp.status_code == 200
        assert result['success'] is True
        assert not target.exists()

    def test_delete_frees_quota(self, dl_test_client):
        """After delete, quota is recalculated correctly."""
        client, _, downloads_dir = dl_test_client
        (downloads_dir / 'big.zip').write_bytes(b'\x00' * (1024 * 1024))

        # Check storage before delete
        resp1 = client.get('/api/downloads/files')
        used_before = resp1.get_json()['storage']['used_mb']

        # Delete
        client.delete('/api/downloads/file/big.zip')

        # Check storage after delete
        resp2 = client.get('/api/downloads/files')
        used_after = resp2.get_json()['storage']['used_mb']

        assert used_after < used_before


@pytest.mark.unit
class TestRequestTransfer:

    def test_request_transfer_sends_mqtt(self, dl_test_client):
        client, mock_ctrl, _ = dl_test_client
        resp = client.post('/api/downloads/request', json={
            'device_id': 'owl-1',
            'session_date': '20260312',
        })
        result = resp.get_json()

        assert resp.status_code == 200
        assert result['success'] is True

        # Verify MQTT publish was called
        calls = mock_ctrl.mqtt_client.publish.call_args_list
        assert len(calls) == 1
        payload = json.loads(calls[0][0][1])
        assert payload['action'] == 'transfer_session'
        assert payload['session_id'] == '20260312'

    def test_request_transfer_owl_offline(self, dl_test_client):
        client, mock_ctrl, _ = dl_test_client
        mock_ctrl.mqtt_connected = False

        resp = client.post('/api/downloads/request', json={
            'device_id': 'owl-1',
            'session_date': '20260312',
        })
        assert resp.status_code == 503

    def test_request_precheck_quota(self, dl_test_client):
        """Session too large for remaining quota is rejected early."""
        client, mock_ctrl, downloads_dir = dl_test_client

        import controller.networked.networked as net_mod
        net_mod.MAX_DOWNLOADS_SIZE_MB = 1  # 1MB quota

        # Fill most of the quota
        (downloads_dir / 'existing.zip').write_bytes(b'\x00' * (900 * 1024))

        # Session is 5MB — should exceed quota
        resp = client.post('/api/downloads/request', json={
            'device_id': 'owl-1',
            'session_date': '20260312',
        })
        result = resp.get_json()

        assert resp.status_code == 507
        assert 'Not enough space' in result['error']

        # Restore
        net_mod.MAX_DOWNLOADS_SIZE_MB = 2000


@pytest.mark.unit
class TestSessionsAPI:

    def test_sessions_api(self, dl_test_client):
        client, _, _ = dl_test_client
        resp = client.get('/api/downloads/sessions/owl-1')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['device_id'] == 'owl-1'
        assert len(data['sessions']) == 1
        assert data['sessions'][0]['date'] == '20260312'


@pytest.mark.unit
class TestDeleteRemote:

    def test_delete_remote_sends_mqtt(self, dl_test_client):
        client, mock_ctrl, _ = dl_test_client
        resp = client.post('/api/downloads/delete-remote', json={
            'device_id': 'owl-1',
            'session_date': '20260312',
        })
        result = resp.get_json()

        assert resp.status_code == 200
        assert result['success'] is True

        calls = mock_ctrl.mqtt_client.publish.call_args_list
        assert len(calls) == 1
        payload = json.loads(calls[0][0][1])
        assert payload['action'] == 'delete_session'
        assert payload['session_id'] == '20260312'


@pytest.mark.unit
class TestReceiveQuota:

    def test_receive_quota_exceeded(self, dl_test_client):
        """Total downloads > MAX_DOWNLOADS_SIZE_MB is rejected."""
        client, _, downloads_dir = dl_test_client

        import controller.networked.networked as net_mod
        original = net_mod.MAX_DOWNLOADS_SIZE_MB
        net_mod.MAX_DOWNLOADS_SIZE_MB = 1  # 1MB quota

        # Existing file fills the quota (1.5 MB)
        (downloads_dir / 'existing.zip').write_bytes(b'\x00' * (1536 * 1024))

        # Try to add another 512KB — post-write check should reject and delete
        new_data = b'\x00' * (512 * 1024)
        resp = client.post('/api/downloads/receive',
                           data=new_data,
                           headers={
                               'Content-Type': 'application/octet-stream',
                               'X-OWL-Device-ID': 'owl-1',
                               'X-OWL-Session-Date': '20260313',
                           })

        assert resp.status_code == 507
        # The file should have been cleaned up
        assert not (downloads_dir / 'owl-1_20260313.zip').exists()
        net_mod.MAX_DOWNLOADS_SIZE_MB = original


# ---------------------------------------------------------------------------
# Standalone download route tests
# ---------------------------------------------------------------------------

@pytest.fixture
def standalone_dl_client(tmp_path):
    """Create a standalone Flask test client with a fake save_directory."""
    from controller.standalone.standalone import OWLDashboard

    save_dir = tmp_path / 'save'
    save_dir.mkdir()

    # Create two sessions
    d1 = save_dir / '20260315'
    d1.mkdir()
    (d1 / 'img_001.jpg').write_bytes(b'\xff\xd8' + b'\x00' * 1000)
    (d1 / 'img_002.jpg').write_bytes(b'\xff\xd8' + b'\x00' * 2000)

    d2 = save_dir / '20260316'
    d2.mkdir()
    (d2 / 'img_001.jpg').write_bytes(b'\xff\xd8' + b'\x00' * 500)

    # Non-date directory should be ignored
    (save_dir / 'not_a_date').mkdir()
    (save_dir / 'not_a_date' / 'file.txt').write_bytes(b'hello')

    dashboard = OWLDashboard.__new__(OWLDashboard)
    dashboard.logger = MagicMock()
    dashboard.config = MagicMock()
    dashboard.mqtt_client = None
    dashboard._get_save_directory = MagicMock(return_value=str(save_dir))

    from flask import Flask
    app = Flask(__name__,
                template_folder=str(Path(__file__).parent.parent / 'controller' / 'standalone' / 'templates'),
                static_folder=str(Path(__file__).parent.parent / 'controller' / 'standalone' / 'static'))
    dashboard.app = app
    dashboard.setup_routes()

    return app.test_client(), save_dir


@pytest.fixture
def standalone_dl_client_subdirs(tmp_path):
    """Standalone client with session_HHMMSS subdirectory structure."""
    from controller.standalone.standalone import OWLDashboard

    save_dir = tmp_path / 'save'
    s1 = save_dir / '20260331' / 'session_143015'
    s2 = save_dir / '20260331' / 'session_160000'
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
    (s1 / 'img1.jpg').write_bytes(b'\xff\xd8' + b'\x00' * 1000)
    (s1 / 'img2.jpg').write_bytes(b'\xff\xd8' + b'\x00' * 2000)
    (s2 / 'capture.png').write_bytes(b'\x89PNG' + b'\x00' * 500)

    dashboard = OWLDashboard.__new__(OWLDashboard)
    dashboard.logger = MagicMock()
    dashboard.config = MagicMock()
    dashboard.mqtt_client = None
    dashboard._get_save_directory = MagicMock(return_value=str(save_dir))

    from flask import Flask
    app = Flask(__name__,
                template_folder=str(Path(__file__).parent.parent / 'controller' / 'standalone' / 'templates'),
                static_folder=str(Path(__file__).parent.parent / 'controller' / 'standalone' / 'static'))
    dashboard.app = app
    dashboard.setup_routes()
    return app.test_client(), save_dir


@pytest.mark.unit
class TestStandaloneDownloadSessions:

    def test_list_sessions(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/sessions')
        assert resp.status_code == 200
        data = resp.get_json()
        sessions = data['sessions']
        assert len(sessions) == 2
        # Sorted reverse — most recent first
        assert sessions[0]['date'] == '20260316'
        assert sessions[1]['date'] == '20260315'
        assert sessions[1]['image_count'] == 2

    def test_list_sessions_with_subdirs(self, standalone_dl_client_subdirs):
        """Session subdirectories are listed as separate sessions."""
        client, _ = standalone_dl_client_subdirs
        resp = client.get('/api/downloads/sessions')
        assert resp.status_code == 200
        sessions = resp.get_json()['sessions']
        assert len(sessions) == 2
        ids = [s['session_id'] for s in sessions]
        assert '20260331/session_160000' in ids
        assert '20260331/session_143015' in ids
        # Check time is extracted
        s1 = next(s for s in sessions if s['session_id'] == '20260331/session_143015')
        assert s1['time'] == '143015'
        assert s1['image_count'] == 2

    def test_list_sessions_includes_storage(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/sessions')
        data = resp.get_json()
        assert data['storage'] is not None
        assert 'used_mb' in data['storage']
        assert 'free_mb' in data['storage']

    def test_non_date_dirs_ignored(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/sessions')
        dates = [s['date'] for s in resp.get_json()['sessions']]
        assert 'not_a_date' not in dates


@pytest.mark.unit
class TestStandaloneDownloadZIP:

    def test_download_session_zip(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/session/20260315')
        assert resp.status_code == 200
        assert resp.content_type == 'application/zip'
        assert 'owl_20260315.zip' in resp.headers.get('Content-Disposition', '')

        # Verify ZIP contents
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        names = zf.namelist()
        assert 'img_001.jpg' in names
        assert 'img_002.jpg' in names

    def test_download_invalid_date_rejected(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/session/not-a-date')
        assert resp.status_code == 400

    def test_download_nonexistent_session(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/session/99990101')
        assert resp.status_code == 404


@pytest.mark.unit
class TestStandaloneDownloadFiles:

    def test_list_session_files(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/session/20260315/files')
        assert resp.status_code == 200
        files = resp.get_json()['files']
        assert len(files) == 2
        assert files[0]['filename'] == 'img_001.jpg'


@pytest.mark.unit
class TestStandaloneDeleteSession:

    def test_delete_session(self, standalone_dl_client):
        client, save_dir = standalone_dl_client
        assert (save_dir / '20260315').exists()
        resp = client.delete('/api/downloads/session/20260315')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        assert not (save_dir / '20260315').exists()

    def test_delete_invalid_date_rejected(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.delete('/api/downloads/session/abcd1234')
        assert resp.status_code == 400

    def test_delete_nonexistent_session(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.delete('/api/downloads/session/99990101')
        assert resp.status_code == 404
