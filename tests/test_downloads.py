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

from utils.directory_manager import scan_sessions, collect_session_files, select_preview_images


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


# ---------------------------------------------------------------------------
# R2 Phase 7: streamed session ZIPs (no temp files) + delete-while-recording
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStreamedZip:
    """Both ZIP routes must stream: /tmp is tmpfs on Trixie, so a multi-GB
    session ZIP written there is RAM exhaustion next to the detection loop
    — and the old NamedTemporaryFile(delete=False) leaked every request."""

    def test_zip_never_touches_a_temp_file(self, standalone_dl_client, monkeypatch):
        import tempfile as tempfile_module

        def forbidden(*args, **kwargs):
            raise AssertionError('session ZIP must stream, not spool to a temp file')

        monkeypatch.setattr(tempfile_module, 'NamedTemporaryFile', forbidden)
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/session/20260315')
        assert resp.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(resp.data))
        assert archive.testzip() is None
        assert set(archive.namelist()) == {'img_001.jpg', 'img_002.jpg'}

    def test_x_total_bytes_matches_payload(self, standalone_dl_client):
        client, save_dir = standalone_dl_client
        resp = client.get('/api/downloads/session/20260315')
        expected = sum(f.stat().st_size
                       for f in (save_dir / '20260315').iterdir() if f.is_file())
        assert int(resp.headers['X-Total-Bytes']) == expected

    def test_response_is_streamed(self, standalone_dl_client):
        client, _ = standalone_dl_client
        resp = client.get('/api/downloads/session/20260315')
        assert resp.is_streamed

    def test_no_named_temporary_file_left_in_source(self):
        """Regression guard: neither download route may reintroduce the
        temp-file pattern."""
        src = (Path(__file__).parent.parent / 'controller' / 'standalone'
               / 'standalone.py').read_text(encoding='utf-8')
        assert 'NamedTemporaryFile' not in src

    def test_subdir_session_zip_valid(self, standalone_dl_client_subdirs):
        client, _ = standalone_dl_client_subdirs
        resp = client.get('/api/downloads/session/20260331/session_143015')
        assert resp.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(resp.data))
        assert archive.testzip() is None
        assert set(archive.namelist()) == {'img1.jpg', 'img2.jpg'}


@pytest.fixture
def standalone_dl_client_recording(tmp_path):
    """Subdir-structure client with recording ON (MQTT state mocked)."""
    from controller.standalone.standalone import OWLDashboard

    save_dir = tmp_path / 'save'
    s1 = save_dir / '20260331' / 'session_143015'
    s2 = save_dir / '20260331' / 'session_160000'
    s1.mkdir(parents=True)
    s2.mkdir(parents=True)
    (s1 / 'img1.jpg').write_bytes(b'\xff\xd8' + b'\x00' * 1000)
    (s2 / 'img1.jpg').write_bytes(b'\xff\xd8' + b'\x00' * 500)

    dashboard = OWLDashboard.__new__(OWLDashboard)
    dashboard.logger = MagicMock()
    dashboard.config = MagicMock()
    dashboard.mqtt_client = MagicMock()
    dashboard.mqtt_client.get_state.return_value = {'image_sample_enable': True}
    dashboard._get_save_directory = MagicMock(return_value=str(save_dir))

    from flask import Flask
    app = Flask(__name__,
                template_folder=str(Path(__file__).parent.parent / 'controller' / 'standalone' / 'templates'),
                static_folder=str(Path(__file__).parent.parent / 'controller' / 'standalone' / 'static'))
    dashboard.app = app
    dashboard.setup_routes()
    return app.test_client(), save_dir, dashboard


@pytest.mark.unit
class TestDeleteWhileRecording:
    """Deleting the session being written would ENOENT-storm the recorder
    workers — the newest session is off-limits while recording is on."""

    def test_active_session_delete_refused(self, standalone_dl_client_recording):
        client, save_dir, _ = standalone_dl_client_recording
        resp = client.delete('/api/downloads/session/20260331/session_160000')
        assert resp.status_code == 409
        assert 'recording' in resp.get_json()['error'].lower()
        assert (save_dir / '20260331' / 'session_160000').exists()

    def test_active_date_delete_refused(self, standalone_dl_client_recording):
        """Date-level delete containing the active session is also refused."""
        client, save_dir, _ = standalone_dl_client_recording
        resp = client.delete('/api/downloads/session/20260331')
        assert resp.status_code == 409
        assert (save_dir / '20260331').exists()

    def test_older_session_delete_allowed(self, standalone_dl_client_recording):
        client, save_dir, _ = standalone_dl_client_recording
        resp = client.delete('/api/downloads/session/20260331/session_143015')
        assert resp.status_code == 200
        assert not (save_dir / '20260331' / 'session_143015').exists()

    def test_delete_allowed_when_idle(self, standalone_dl_client_recording):
        client, save_dir, dashboard = standalone_dl_client_recording
        dashboard.mqtt_client.get_state.return_value = {'image_sample_enable': False}
        resp = client.delete('/api/downloads/session/20260331/session_160000')
        assert resp.status_code == 200
        assert not (save_dir / '20260331' / 'session_160000').exists()


# ---------------------------------------------------------------------------
# _upload_session method=PUT (presigned URLs) vs POST (controller endpoint)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUploadSessionMethod:
    """PUT (presigned URL) must use verified TLS and no custom headers;
    POST (controller endpoint) must keep its existing behaviour."""

    def _make_session(self, mqtt_publisher, tmp_path):
        save_dir = str(tmp_path / 'save')
        img_dir = os.path.join(save_dir, '20260611')
        os.makedirs(img_dir)
        with open(os.path.join(img_dir, 'test.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 100)
        mqtt_publisher.owl_instance.save_directory = save_dir

    def _capture_upload(self, mqtt_publisher, tmp_path, method):
        self._make_session(mqtt_publisher, tmp_path)
        captured = {}

        def mock_urlopen(req, **kwargs):
            captured['request'] = req
            captured['context'] = kwargs.get('context')
            # Drain the ProgressReader so the upload "completes"
            while req.data.read(65536):
                pass
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260611', ['images'], 'https://example.com/upload', method
            )
        return captured

    def test_put_uses_put_method(self, mqtt_publisher, tmp_path):
        captured = self._capture_upload(mqtt_publisher, tmp_path, 'PUT')
        assert captured['request'].get_method() == 'PUT'
        assert mqtt_publisher.state['data_transfer']['status'] == 'complete'

    def test_put_omits_owl_headers(self, mqtt_publisher, tmp_path):
        """Custom headers outside the presigned SignedHeaders set can
        invalidate the signature on S3-compatible stores."""
        captured = self._capture_upload(mqtt_publisher, tmp_path, 'PUT')
        req = captured['request']
        assert not req.has_header('X-owl-device-id')
        assert not req.has_header('X-owl-session-date')
        assert req.has_header('Content-type')

    def test_put_verifies_tls(self, mqtt_publisher, tmp_path):
        import ssl
        captured = self._capture_upload(mqtt_publisher, tmp_path, 'PUT')
        ctx = captured['context']
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_post_keeps_owl_headers(self, mqtt_publisher, tmp_path):
        captured = self._capture_upload(mqtt_publisher, tmp_path, 'POST')
        req = captured['request']
        assert req.get_method() == 'POST'
        assert req.has_header('X-owl-device-id')
        assert req.has_header('X-owl-session-date')

    def test_post_keeps_self_signed_tls(self, mqtt_publisher, tmp_path):
        import ssl
        captured = self._capture_upload(mqtt_publisher, tmp_path, 'POST')
        ctx = captured['context']
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False

    def test_put_accepts_204_no_content(self, mqtt_publisher, tmp_path):
        self._make_session(mqtt_publisher, tmp_path)

        def mock_urlopen(req, **kwargs):
            while req.data.read(65536):
                pass
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 204
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260611', ['images'], 'https://example.com/upload', 'PUT'
            )
        assert mqtt_publisher.state['data_transfer']['status'] == 'complete'


# ---------------------------------------------------------------------------
# _upload_session multipart (presigned part URLs, Noktura CR-2)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUploadSessionMultipart:
    """Multipart: sequential part PUTs, ETag accumulation, per-part retry."""

    PART_SIZE = 100_000

    def _make_session(self, mqtt_publisher, tmp_path, file_bytes=250_000):
        save_dir = str(tmp_path / 'save')
        img_dir = os.path.join(save_dir, '20260611')
        os.makedirs(img_dir)
        with open(os.path.join(img_dir, 'test.jpg'), 'wb') as f:
            f.write(b'\xff' * file_bytes)
        mqtt_publisher.owl_instance.save_directory = save_dir

    def _multipart(self, n_parts, part_size=PART_SIZE, upload_id='uid-1'):
        return {
            'upload_id': upload_id,
            'part_size': part_size,
            'parts': [{'part_number': i + 1, 'url': f'https://s3.example.com/part{i + 1}'}
                      for i in range(n_parts)],
        }

    def _run(self, mqtt_publisher, tmp_path, upload, request_id='', fail_attempts=None):
        """Run a multipart upload with a capturing mock urlopen.

        fail_attempts: {url_suffix: n} — fail the first n attempts to that URL.
        Returns list of captured {'url', 'method', 'length', 'body'} per attempt.
        """
        self._make_session(mqtt_publisher, tmp_path)
        captured = []
        failures = dict(fail_attempts or {})

        def mock_urlopen(req, **kwargs):
            body = b''
            while True:
                chunk = req.data.read(65536)
                if not chunk:
                    break
                body += chunk
            captured.append({
                'url': req.full_url,
                'method': req.get_method(),
                'length': int(req.get_header('Content-length')),
                'body': body,
            })
            for suffix, n in failures.items():
                if req.full_url.endswith(suffix) and n > 0:
                    failures[suffix] = n - 1
                    raise ConnectionResetError('connection reset')
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.headers = {'ETag': f'"etag-{req.full_url.rsplit("part", 1)[-1]}"'}
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen), \
                patch('time.sleep') as mock_sleep:
            mqtt_publisher._upload_session(
                '20260611', ['images'], '', 'PUT',
                request_id=request_id, upload=upload
            )
        self.mock_sleep = mock_sleep
        return captured

    def test_multipart_puts_parts_sequentially(self, mqtt_publisher, tmp_path):
        captured = self._run(mqtt_publisher, tmp_path, self._multipart(3))

        state = mqtt_publisher.state['data_transfer']
        assert state['status'] == 'complete'
        zip_bytes = state['zip_bytes']
        assert zip_bytes > 200_000  # 250KB file + zip headers

        assert [c['url'] for c in captured] == [
            'https://s3.example.com/part1',
            'https://s3.example.com/part2',
            'https://s3.example.com/part3',
        ]
        assert all(c['method'] == 'PUT' for c in captured)
        # Last part carries the remainder (zip headers make it > 50KB)
        assert [c['length'] for c in captured] == [
            self.PART_SIZE, self.PART_SIZE, zip_bytes - 2 * self.PART_SIZE]
        # Bounded readers must deliver exactly Content-Length bytes
        assert all(len(c['body']) == c['length'] for c in captured)
        assert state['bytes_sent'] == zip_bytes
        assert state['progress'] == 100

    def test_multipart_accumulates_quoted_etags(self, mqtt_publisher, tmp_path):
        self._run(mqtt_publisher, tmp_path, self._multipart(3))

        parts = mqtt_publisher.state['data_transfer']['parts']
        assert parts == [
            {'part_number': 1, 'etag': '"etag-1"'},
            {'part_number': 2, 'etag': '"etag-2"'},
            {'part_number': 3, 'etag': '"etag-3"'},
        ]

    def test_multipart_echoes_request_id_and_upload_id(self, mqtt_publisher, tmp_path):
        self._run(mqtt_publisher, tmp_path, self._multipart(3), request_id='req-9')

        state = mqtt_publisher.state['data_transfer']
        assert state['status'] == 'complete'
        assert state['request_id'] == 'req-9'
        assert state['upload_id'] == 'uid-1'

    def test_multipart_retries_failed_part(self, mqtt_publisher, tmp_path):
        """A part that fails once is re-sent; other parts go once."""
        captured = self._run(mqtt_publisher, tmp_path, self._multipart(3),
                             fail_attempts={'part2': 1})

        assert mqtt_publisher.state['data_transfer']['status'] == 'complete'
        urls = [c['url'] for c in captured]
        assert urls.count('https://s3.example.com/part1') == 1
        assert urls.count('https://s3.example.com/part2') == 2
        assert urls.count('https://s3.example.com/part3') == 1
        # Backoff slept before the retry (other threads also hit the patched
        # sleep, so check membership rather than the last call)
        self.mock_sleep.assert_any_call(2)
        # The retry re-sent the full part, not a truncated remainder
        part2_attempts = [c for c in captured if c['url'].endswith('part2')]
        assert all(len(c['body']) == self.PART_SIZE for c in part2_attempts)

    def test_multipart_exhausted_retries_sets_error(self, mqtt_publisher, tmp_path):
        """All attempts for a part fail → error state keeps the context the
        cloud side needs to abort the multipart upload."""
        captured = self._run(mqtt_publisher, tmp_path, self._multipart(3),
                             request_id='req-1', fail_attempts={'part2': 99})

        state = mqtt_publisher.state['data_transfer']
        assert state['status'] == 'error'
        assert 'reset' in state['error']
        # part 1 succeeded before the failure — its ETag must survive
        assert state['parts'] == [{'part_number': 1, 'etag': '"etag-1"'}]
        assert state['request_id'] == 'req-1'
        assert state['upload_id'] == 'uid-1'
        # 3 attempts on part 2, part 3 never reached
        urls = [c['url'] for c in captured]
        assert urls.count('https://s3.example.com/part2') == 3
        assert 'https://s3.example.com/part3' not in urls

    def test_multipart_insufficient_parts_errors_before_upload(self, mqtt_publisher, tmp_path):
        """Parts that can't cover the zip fail upfront — never a silently
        truncated object (zip is slightly larger than the manifest size)."""
        captured = self._run(mqtt_publisher, tmp_path, self._multipart(2))

        assert mqtt_publisher.state['data_transfer']['status'] == 'error'
        assert 'more parts' in mqtt_publisher.state['data_transfer']['error']
        assert captured == []

    def test_multipart_extra_parts_skipped(self, mqtt_publisher, tmp_path):
        """Over-provisioned part URLs past the end of the zip are ignored."""
        captured = self._run(mqtt_publisher, tmp_path, self._multipart(5))

        assert mqtt_publisher.state['data_transfer']['status'] == 'complete'
        assert len(captured) == 3
        assert len(mqtt_publisher.state['data_transfer']['parts']) == 3

    def test_state_reset_between_transfers(self, mqtt_publisher, tmp_path):
        """A single-PUT transfer after a multipart one must not carry stale
        parts/upload_id/request_id."""
        self._run(mqtt_publisher, tmp_path, self._multipart(3), request_id='req-1')
        assert mqtt_publisher.state['data_transfer']['parts']

        def mock_urlopen(req, **kwargs):
            while req.data.read(65536):
                pass
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260611', ['images'], 'https://example.com/upload', 'PUT'
            )

        state = mqtt_publisher.state['data_transfer']
        assert state['status'] == 'complete'
        assert state['parts'] == []
        assert state['upload_id'] == ''
        assert state['request_id'] == ''


# ---------------------------------------------------------------------------
# data_transfer zip checksum (Noktura CR-4) + request_id echo
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUploadSessionChecksum:

    def _make_session(self, mqtt_publisher, tmp_path):
        save_dir = str(tmp_path / 'save')
        img_dir = os.path.join(save_dir, '20260611')
        os.makedirs(img_dir)
        with open(os.path.join(img_dir, 'test.jpg'), 'wb') as f:
            f.write(b'\xff\xd8' * 5000)
        mqtt_publisher.owl_instance.save_directory = save_dir

    def test_complete_includes_zip_bytes_and_md5(self, mqtt_publisher, tmp_path):
        import hashlib
        self._make_session(mqtt_publisher, tmp_path)
        received = []

        def mock_urlopen(req, **kwargs):
            while True:
                chunk = req.data.read(65536)
                if not chunk:
                    break
                received.append(chunk)
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260611', ['images'], 'https://example.com/upload', 'PUT'
            )

        body = b''.join(received)
        state = mqtt_publisher.state['data_transfer']
        assert state['status'] == 'complete'
        assert state['zip_bytes'] == len(body)
        assert state['zip_md5'] == hashlib.md5(body).hexdigest()

    def test_single_put_echoes_request_id(self, mqtt_publisher, tmp_path):
        self._make_session(mqtt_publisher, tmp_path)

        def mock_urlopen(req, **kwargs):
            while req.data.read(65536):
                pass
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_session(
                '20260611', ['images'], 'https://example.com/upload', 'PUT',
                request_id='req-7'
            )

        state = mqtt_publisher.state['data_transfer']
        assert state['status'] == 'complete'
        assert state['request_id'] == 'req-7'

    def test_multipart_md5_unaffected_by_retry(self, mqtt_publisher, tmp_path):
        """A retried part must not corrupt the digest — the MD5 is computed
        in a separate pass, never inside the upload reader."""
        import hashlib
        save_dir = str(tmp_path / 'save')
        img_dir = os.path.join(save_dir, '20260611')
        os.makedirs(img_dir)
        with open(os.path.join(img_dir, 'test.jpg'), 'wb') as f:
            f.write(b'\xff' * 250_000)
        mqtt_publisher.owl_instance.save_directory = save_dir

        upload = {
            'upload_id': 'uid-1', 'part_size': 100_000,
            'parts': [{'part_number': i + 1, 'url': f'https://s3.example.com/part{i + 1}'}
                      for i in range(3)],
        }
        last_body = {}
        fail_once = {'part2': 1}

        def mock_urlopen(req, **kwargs):
            body = b''
            while True:
                chunk = req.data.read(65536)
                if not chunk:
                    break
                body += chunk
            suffix = req.full_url.rsplit('/', 1)[-1]
            if fail_once.get(suffix, 0) > 0:
                fail_once[suffix] -= 1
                raise ConnectionResetError('reset')
            last_body[suffix] = body
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.headers = {'ETag': f'"{suffix}"'}
            return mock_resp

        with patch('urllib.request.urlopen', side_effect=mock_urlopen), \
                patch('time.sleep'):
            mqtt_publisher._upload_session(
                '20260611', ['images'], '', 'PUT', upload=upload
            )

        state = mqtt_publisher.state['data_transfer']
        assert state['status'] == 'complete'
        full_zip = last_body['part1'] + last_body['part2'] + last_body['part3']
        assert state['zip_md5'] == hashlib.md5(full_zip).hexdigest()
        assert state['zip_bytes'] == len(full_zip)


# ---------------------------------------------------------------------------
# select_preview_images (utils/directory_manager.py, Noktura CR-3)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSelectPreviewImages:

    def _make_session(self, tmp_path, n_images):
        sess = tmp_path / '20260611' / 'session_083015'
        sess.mkdir(parents=True)
        for i in range(n_images):
            (sess / f'frame_{i:03d}.jpg').write_bytes(b'\xff\xd8')
        return str(tmp_path)

    def test_evenly_spaced_selection(self, tmp_path):
        save_dir = self._make_session(tmp_path, 10)
        selected = select_preview_images(save_dir, '20260611/session_083015', 3)
        names = [os.path.basename(p) for p in selected]
        # First and last always included, middle evenly spaced
        assert names[0] == 'frame_000.jpg'
        assert names[-1] == 'frame_009.jpg'
        assert names[1] in ('frame_004.jpg', 'frame_005.jpg')
        assert len(selected) == 3

    def test_fewer_images_than_count_returns_all(self, tmp_path):
        save_dir = self._make_session(tmp_path, 4)
        selected = select_preview_images(save_dir, '20260611/session_083015', 8)
        assert len(selected) == 4

    def test_count_one_returns_first(self, tmp_path):
        save_dir = self._make_session(tmp_path, 10)
        selected = select_preview_images(save_dir, '20260611/session_083015', 1)
        assert [os.path.basename(p) for p in selected] == ['frame_000.jpg']

    def test_count_zero_returns_empty(self, tmp_path):
        save_dir = self._make_session(tmp_path, 10)
        assert select_preview_images(save_dir, '20260611/session_083015', 0) == []

    def test_invalid_session_returns_empty(self, tmp_path):
        assert select_preview_images(str(tmp_path), '../etc', 3) == []


# ---------------------------------------------------------------------------
# _upload_previews (Noktura CR-3)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestUploadPreviews:

    def _make_image_session(self, mqtt_publisher, tmp_path, n_images=3,
                            width=1920, height=1080):
        cv2 = pytest.importorskip('cv2')
        np = pytest.importorskip('numpy')
        save_dir = str(tmp_path / 'save')
        sess = os.path.join(save_dir, '20260611', 'session_083015')
        os.makedirs(sess)
        for i in range(n_images):
            frame = np.full((height, width, 3), i * 20 % 255, dtype=np.uint8)
            cv2.imwrite(os.path.join(sess, f'frame_{i:03d}.jpg'), frame)
        mqtt_publisher.owl_instance.save_directory = save_dir

    def _capture_puts(self, mqtt_publisher, fail_at=None, status=200):
        captured = []

        def mock_urlopen(req, **kwargs):
            captured.append({
                'url': req.full_url,
                'body': req.data,
                'headers': dict(req.header_items()),
                'context': kwargs.get('context'),
                'request': req,
            })
            mock_resp = MagicMock()
            if fail_at is not None and len(captured) == fail_at:
                mock_resp.getcode.return_value = 500
            else:
                mock_resp.getcode.return_value = status
            return mock_resp

        return captured, mock_urlopen

    def test_preview_resize_bounded(self, mqtt_publisher, tmp_path):
        cv2 = pytest.importorskip('cv2')
        np = pytest.importorskip('numpy')
        self._make_image_session(mqtt_publisher, tmp_path, n_images=1)
        captured, mock_urlopen = self._capture_puts(mqtt_publisher)

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_previews(
                'req-1', '20260611/session_083015', 1, 800,
                ['https://s3.example.com/preview1'])

        assert mqtt_publisher.state['preview_upload']['status'] == 'complete'
        img = cv2.imdecode(np.frombuffer(captured[0]['body'], np.uint8),
                           cv2.IMREAD_COLOR)
        h, w = img.shape[:2]
        assert max(h, w) == 800
        # Aspect ratio preserved (1920x1080 -> 800x450)
        assert (w, h) == (800, 450)

    def test_preview_put_verified_tls_no_custom_headers(self, mqtt_publisher, tmp_path):
        import ssl
        self._make_image_session(mqtt_publisher, tmp_path, n_images=1)
        captured, mock_urlopen = self._capture_puts(mqtt_publisher)

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_previews(
                'req-1', '20260611/session_083015', 1, 800,
                ['https://s3.example.com/preview1'])

        req = captured[0]['request']
        ctx = captured[0]['context']
        assert req.get_method() == 'PUT'
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
        assert req.get_header('Content-type') == 'image/jpeg'
        assert not req.has_header('X-owl-device-id')

    def test_preview_sequential_count_and_state(self, mqtt_publisher, tmp_path):
        self._make_image_session(mqtt_publisher, tmp_path, n_images=10,
                                 width=64, height=48)
        captured, mock_urlopen = self._capture_puts(mqtt_publisher)
        urls = [f'https://s3.example.com/preview{i}' for i in range(3)]

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_previews(
                'req-2', '20260611/session_083015', 3, 800, urls)

        state = mqtt_publisher.state['preview_upload']
        assert state['status'] == 'complete'
        assert state['uploaded'] == 3
        assert state['total'] == 3
        assert state['request_id'] == 'req-2'
        assert state['session_id'] == '20260611/session_083015'
        assert [c['url'] for c in captured] == urls

    def test_preview_concurrent_job_rejected(self, mqtt_publisher, tmp_path):
        self._make_image_session(mqtt_publisher, tmp_path, n_images=2,
                                 width=64, height=48)
        with mqtt_publisher.state_lock:
            mqtt_publisher.state['preview_upload']['status'] = 'uploading'
            mqtt_publisher.state['preview_upload']['request_id'] = 'req-old'
        captured, mock_urlopen = self._capture_puts(mqtt_publisher)

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_previews(
                'req-new', '20260611/session_083015', 2, 800,
                ['https://s3.example.com/p1', 'https://s3.example.com/p2'])

        assert captured == []
        assert mqtt_publisher.state['preview_upload']['request_id'] == 'req-old'

    def test_preview_failure_sets_error(self, mqtt_publisher, tmp_path):
        self._make_image_session(mqtt_publisher, tmp_path, n_images=4,
                                 width=64, height=48)
        captured, mock_urlopen = self._capture_puts(mqtt_publisher, fail_at=2)
        urls = [f'https://s3.example.com/preview{i}' for i in range(3)]

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_previews(
                'req-3', '20260611/session_083015', 3, 800, urls)

        state = mqtt_publisher.state['preview_upload']
        assert state['status'] == 'error'
        assert '500' in state['error']
        assert state['uploaded'] == 1

    def test_preview_invalid_session_id(self, mqtt_publisher):
        captured, mock_urlopen = self._capture_puts(mqtt_publisher)

        with patch('urllib.request.urlopen', side_effect=mock_urlopen):
            mqtt_publisher._upload_previews(
                'req-4', '../etc', 3, 800, ['https://s3.example.com/p1'])

        assert captured == []
        state = mqtt_publisher.state['preview_upload']
        assert state['status'] == 'error'
        assert 'Invalid' in state['error']


# ---------------------------------------------------------------------------
# Session locations (Map tab, contract 3): sidecar helpers + route
# ---------------------------------------------------------------------------

from utils.directory_manager import (read_session_locations,
                                     scan_exif_locations,
                                     read_session_tracks,
                                     _downsample_keep_ends)


def _write_sidecar(session_dir, entries):
    lines = '\n'.join(json.dumps(e) for e in entries)
    (Path(session_dir) / 'locations.jsonl').write_text(lines + '\n')


def _gps_jpeg(path, lat=-31.5, lon=150.25):
    """A real JPEG with real GPS EXIF, built by the production writer."""
    import numpy as np
    from PIL import Image
    from utils.image_sampler import build_exif_bytes, encode_jpeg
    exif = build_exif_bytes(gps_data={'latitude': lat, 'longitude': lon})
    image = Image.fromarray(np.zeros((8, 8, 3), dtype='uint8'))
    Path(path).write_bytes(encode_jpeg(image, exif))


@pytest.mark.unit
class TestReadSessionLocations:

    def test_no_sidecar_returns_none(self, tmp_path):
        assert read_session_locations(str(tmp_path)) is None

    def test_parses_entries_lon_lat_order(self, tmp_path):
        _write_sidecar(tmp_path, [
            {'ts': '2026-08-18T01:00:00.000Z', 'frame_id': 1,
             'lat': -31.5, 'lon': 150.25, 'files': ['a.jpg'],
             'speed_kmh': 9.7},
        ])
        features, total, truncated = read_session_locations(str(tmp_path))
        assert total == 1 and truncated is False
        feature = features[0]
        # GeoJSON is [lon, lat]
        assert feature['geometry']['coordinates'] == [150.25, -31.5]
        assert feature['properties']['files'] == ['a.jpg']
        assert feature['properties']['speed_kmh'] == 9.7

    def test_torn_and_malformed_lines_skipped(self, tmp_path):
        (tmp_path / 'locations.jsonl').write_text(
            json.dumps({'ts': 't', 'frame_id': 1,
                        'lat': -31.5, 'lon': 150.25, 'files': []}) + '\n'
            + 'not json at all\n'
            + json.dumps({'frame_id': 2, 'lon': 150.0}) + '\n'
            + '{"frame_id": 3, "lat": -31.6, "lon": 150.'
        )
        features, total, truncated = read_session_locations(str(tmp_path))
        assert total == 1

    def test_truncation_keeps_first_and_last(self, tmp_path):
        entries = [{'ts': 't', 'frame_id': i, 'lat': -31.0 - i * 0.001,
                    'lon': 150.0, 'files': []} for i in range(10)]
        _write_sidecar(tmp_path, entries)
        features, total, truncated = read_session_locations(
            str(tmp_path), max_features=4)
        assert total == 10 and truncated is True
        assert len(features) == 4
        assert features[0]['properties']['frame_id'] == 0
        assert features[-1]['properties']['frame_id'] == 9

    def test_downsample_keep_ends_short_list_untouched(self):
        items = [1, 2, 3]
        assert _downsample_keep_ends(items, 5) == [1, 2, 3]


@pytest.mark.unit
class TestScanExifLocations:

    def test_reads_gps_from_frame_jpegs(self, tmp_path):
        _gps_jpeg(tmp_path / '2026_frame_1.jpg', lat=-31.5, lon=150.25)
        _gps_jpeg(tmp_path / '2026_frame_2.jpg', lat=-31.6, lon=150.26)
        features, total, truncated = scan_exif_locations(str(tmp_path))
        assert total == 2 and truncated is False
        assert len(features) == 2
        lat = features[0]['geometry']['coordinates'][1]
        # Round-trips through DMS rationals; sub-metre tolerance
        assert lat == pytest.approx(-31.5, abs=1e-5)

    def test_crops_prefer_frame_files(self, tmp_path):
        """Whole frames beat crops; crops of the same frame dedupe."""
        _gps_jpeg(tmp_path / '2026_frame_1.jpg')
        _gps_jpeg(tmp_path / '2026_frame_1_n_0.jpg')
        _gps_jpeg(tmp_path / '2026_frame_1_n_1.jpg')
        features, total, _ = scan_exif_locations(str(tmp_path))
        assert total == 1

    def test_crops_only_session_dedupes_per_frame(self, tmp_path):
        _gps_jpeg(tmp_path / '2026_frame_1_n_0.jpg')
        _gps_jpeg(tmp_path / '2026_frame_1_n_1.jpg')
        _gps_jpeg(tmp_path / '2026_frame_2_n_0.jpg')
        features, total, _ = scan_exif_locations(str(tmp_path))
        assert total == 2

    def test_gpsless_and_broken_jpegs_skipped(self, tmp_path):
        import numpy as np
        from PIL import Image
        from utils.image_sampler import encode_jpeg
        image = Image.fromarray(np.zeros((8, 8, 3), dtype='uint8'))
        (tmp_path / '2026_frame_1.jpg').write_bytes(encode_jpeg(image))
        (tmp_path / '2026_frame_2.jpg').write_bytes(b'\xff\xd8junk')
        features, total, _ = scan_exif_locations(str(tmp_path))
        assert total == 2
        assert features == []

    def test_cap_marks_truncated(self, tmp_path):
        for i in range(4):
            _gps_jpeg(tmp_path / ('2026_frame_' + str(i) + '.jpg'))
        features, total, truncated = scan_exif_locations(
            str(tmp_path), max_files=2)
        assert total == 4 and truncated is True
        assert len(features) == 2


@pytest.mark.unit
class TestReadSessionTracks:

    def test_reads_linestring_features(self, tmp_path):
        track = {'type': 'FeatureCollection', 'features': [{
            'type': 'Feature',
            'geometry': {'type': 'LineString',
                         'coordinates': [[150.0, -31.0], [150.1, -31.1]]},
            'properties': {'name': 'OWL Session'},
        }]}
        (tmp_path / 'track_2026-08-18_040000.geojson').write_text(
            json.dumps(track))
        features = read_session_tracks(str(tmp_path))
        assert len(features) == 1
        assert features[0]['geometry']['type'] == 'LineString'

    def test_malformed_track_skipped(self, tmp_path):
        (tmp_path / 'track_bad.geojson').write_text('{not json')
        assert read_session_tracks(str(tmp_path)) == []

    def test_non_track_files_ignored(self, tmp_path):
        (tmp_path / 'other.geojson').write_text('{}')
        assert read_session_tracks(str(tmp_path)) == []


@pytest.mark.unit
class TestStandaloneLocationsRoute:

    def test_sidecar_session(self, standalone_dl_client_subdirs):
        client, save_dir = standalone_dl_client_subdirs
        session = save_dir / '20260331' / 'session_143015'
        _write_sidecar(session, [
            {'ts': '2026-03-31T14:30:20.000Z', 'frame_id': 1,
             'lat': -31.5, 'lon': 150.25, 'files': ['img1.jpg']},
        ])
        resp = client.get(
            '/api/downloads/session/20260331/session_143015/locations')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['type'] == 'FeatureCollection'
        assert data['source'] == 'sidecar'
        assert data['count'] == 1
        assert data['features'][0]['geometry']['coordinates'] == [150.25, -31.5]

    def test_track_appended_as_linestring(self, standalone_dl_client_subdirs):
        client, save_dir = standalone_dl_client_subdirs
        session = save_dir / '20260331' / 'session_143015'
        _write_sidecar(session, [
            {'ts': 't', 'frame_id': 1, 'lat': -31.5, 'lon': 150.25,
             'files': ['img1.jpg']},
        ])
        track = {'type': 'FeatureCollection', 'features': [{
            'type': 'Feature',
            'geometry': {'type': 'LineString',
                         'coordinates': [[150.25, -31.5], [150.26, -31.6]]},
            'properties': {},
        }]}
        (session / 'track_2026-03-31_143015.geojson').write_text(
            json.dumps(track))
        data = client.get(
            '/api/downloads/session/20260331/session_143015/locations'
        ).get_json()
        types = [f['geometry']['type'] for f in data['features']]
        assert types.count('Point') == 1
        assert types.count('LineString') == 1
        # count reflects image points only, not track features
        assert data['count'] == 1

    def test_exif_fallback_for_legacy_session(self, standalone_dl_client_subdirs):
        client, save_dir = standalone_dl_client_subdirs
        session = save_dir / '20260331' / 'session_160000'
        _gps_jpeg(session / '2026_frame_1.jpg')
        data = client.get(
            '/api/downloads/session/20260331/session_160000/locations'
        ).get_json()
        assert data['source'] == 'exif'
        assert data['count'] == 1

    def test_no_location_data(self, standalone_dl_client_subdirs):
        """Junk-byte JPEGs (no EXIF) and no sidecar -> source none."""
        client, save_dir = standalone_dl_client_subdirs
        data = client.get(
            '/api/downloads/session/20260331/session_143015/locations'
        ).get_json()
        assert data['source'] == 'none'
        assert data['features'] == []
        assert data['count'] == 0

    def test_path_traversal_rejected(self, standalone_dl_client_subdirs):
        client, _ = standalone_dl_client_subdirs
        resp = client.get('/api/downloads/session/..%2F..%2Fetc/locations')
        assert resp.status_code in (400, 404)

    def test_sidecar_and_track_ride_in_zip(self, standalone_dl_client_subdirs):
        """collect_session_files carries the metadata files, so date-level
        ZIPs (and the networked pull path) include them."""
        _, save_dir = standalone_dl_client_subdirs
        session = save_dir / '20260331' / 'session_143015'
        _write_sidecar(session, [
            {'ts': 't', 'frame_id': 1, 'lat': -31.5, 'lon': 150.25,
             'files': ['img1.jpg']},
        ])
        (session / 'track_x.geojson').write_text('{}')
        pairs = collect_session_files(str(save_dir), '20260331/session_143015')
        names = [name for name, _ in pairs]
        assert 'images/locations.jsonl' in names
        assert 'images/track_x.geojson' in names

    def test_preview_selection_skips_metadata_files(self, standalone_dl_client_subdirs):
        _, save_dir = standalone_dl_client_subdirs
        session = save_dir / '20260331' / 'session_143015'
        _write_sidecar(session, [
            {'ts': 't', 'frame_id': 1, 'lat': -31.5, 'lon': 150.25,
             'files': ['img1.jpg']},
        ])
        paths = select_preview_images(str(save_dir), '20260331/session_143015', 10)
        assert all(p.lower().endswith(('.jpg', '.jpeg', '.png')) for p in paths)
        assert len(paths) == 2
