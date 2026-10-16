"""Session folder names (field round 2026-10-15): sHHMMSS_<owl>_<serial4>, so
two OWLs started by one tap on the controller no longer share a name. Old
session_HHMMSS folders keep working everywhere. Noktura validates the same
shapes (SESSION_ID_RE, its spec 6.2a)."""

import os
from datetime import datetime

import pytest

from utils.directory_manager import (collect_session_files, is_session_id, scan_sessions,
                                     session_folder_name, session_time)


@pytest.mark.unit
class TestSessionFolderName:

    def test_shape(self):
        when = datetime(2026, 10, 15, 16, 40, 25)
        assert session_folder_name(when, 'owl-1', '10000000a3f9') == 's164025_owl-1_a3f9'

    @pytest.mark.parametrize('device,owl', [
        ('OWL 2', 'owl-2'), ('owl_3', 'owl-3'), ('', 'owl'), (None, 'owl'),
        ('--x--', 'x'), ('a' * 40, 'a' * 24)])
    def test_device_cleaned(self, device, owl):
        name = session_folder_name(datetime(2026, 1, 1, 1, 2, 3), device, 'abcd')
        assert name == f's010203_{owl}_abcd'
        assert is_session_id('20260101/' + name)

    @pytest.mark.parametrize('serial,tail', [
        ('10000000A3F9', 'a3f9'), ('3f', '003f'), (None, '0000'), ('zz', '0000')])
    def test_serial_tail(self, serial, tail):
        assert session_folder_name(datetime(2026, 1, 1), 'owl-1', serial).endswith('_' + tail)


@pytest.mark.unit
class TestSessionIds:

    @pytest.mark.parametrize('value', [
        '20261015', '20261015/session_164025', '20261015/s164025_owl-1_a3f9'])
    def test_valid(self, value):
        assert is_session_id(value)

    @pytest.mark.parametrize('value', [
        '', None, '2026101', '20261015/', '20261015/session_16402',
        '20261015/s164025_OWL-1_a3f9', '20261015/s164025_owl-1_a3f', '20261015/s164025_owl-1_a3fg',
        '20261015/../etc', '20261015/s164025_owl-1_a3f9\n', '20261015/s164025_owl_1/x_a3f9'])
    def test_invalid(self, value):
        assert not is_session_id(value)

    def test_time(self):
        assert session_time('s164025_owl-1_a3f9') == '164025'
        assert session_time('session_090001') == '090001'
        assert session_time('notes') == ''


@pytest.mark.unit
class TestScanWithBothNames:

    def _session(self, root, date, name, files=('a.jpg',)):
        path = os.path.join(root, date, name)
        os.makedirs(path)
        for f in files:
            with open(os.path.join(path, f), 'wb') as fh:
                fh.write(b'x' * 10)
        return path

    def test_newest_first_by_start_time_across_name_forms(self, tmp_path):
        root = str(tmp_path)
        self._session(root, '20261015', 'session_090000')
        self._session(root, '20261015', 's164025_owl-1_a3f9')
        self._session(root, '20261015', 'session_170000')
        ids = [s['session_id'] for s in scan_sessions(root)]
        # 's1…' sorts before 'se…' by name; the newest must still lead
        assert ids == ['20261015/session_170000', '20261015/s164025_owl-1_a3f9',
                       '20261015/session_090000']
        times = [s['time'] for s in scan_sessions(root)]
        assert times == ['170000', '164025', '090000']

    def test_video_counted_and_zipped(self, tmp_path):
        root = str(tmp_path)
        self._session(root, '20261015', 's164025_owl-1_a3f9', files=('clip_164030.mp4',))
        (session,) = scan_sessions(root)
        assert (session['image_count'], session['video_count']) == (0, 1)
        assert session['total_size'] == 10
        files = collect_session_files(root, session['session_id'])
        assert [a for a, _ in files] == ['images/clip_164030.mp4']

    def test_zip_order_is_fixed(self, tmp_path):
        root = str(tmp_path)
        self._session(root, '20261015', 's164025_owl-1_a3f9', files=('c.jpg', 'a.jpg', 'b.jpg'))
        files = collect_session_files(root, '20261015/s164025_owl-1_a3f9')
        assert [a for a, _ in files] == sorted(a for a, _ in files)
