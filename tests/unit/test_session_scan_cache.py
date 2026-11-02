"""scan_sessions must stay cheap on a big USB drive (field, 2026-11-02): the
app's timed-out retries stacked whole-drive walks until every dashboard
thread was stuck and only the video (served by owl.py) still worked."""

import os
import time

import pytest

from utils import directory_manager as dm


@pytest.fixture
def session(tmp_path):
    folder = tmp_path / '20261102' / 's171455_owl-1_9390'
    folder.mkdir(parents=True)
    (folder / 'a.jpg').write_bytes(b'x' * 100)
    return tmp_path, folder


def settle(folder):
    old = time.time() - dm.SCAN_SETTLE_S - 10
    os.utime(folder, (old, old))


@pytest.mark.unit
class TestSessionScanCache:
    def test_settled_folder_is_not_read_again(self, session):
        save_dir, folder = session
        settle(folder)
        assert dm.scan_sessions(str(save_dir))[0]['image_size'] == 100
        stamp = os.stat(folder).st_mtime
        (folder / 'a.jpg').write_bytes(b'x' * 500)   # no new file: folder mtime kept
        os.utime(folder, (stamp, stamp))
        assert dm.scan_sessions(str(save_dir))[0]['image_size'] == 100

    def test_a_new_image_is_counted(self, session):
        save_dir, folder = session
        settle(folder)
        dm.scan_sessions(str(save_dir))
        (folder / 'b.jpg').write_bytes(b'x' * 50)
        listed = dm.scan_sessions(str(save_dir))[0]
        assert listed['image_count'] == 2
        assert listed['total_size'] == 150

    def test_the_recording_folder_is_always_read(self, session):
        # Changed within SCAN_SETTLE_S: its newest JPEG may still be growing
        save_dir, folder = session
        dm.scan_sessions(str(save_dir))
        (folder / 'a.jpg').write_bytes(b'x' * 500)
        assert dm.scan_sessions(str(save_dir))[0]['image_size'] == 500

    def test_caller_during_a_scan_gets_the_last_listing(self, session):
        save_dir, folder = session
        first = dm.scan_sessions(str(save_dir))
        (folder / 'b.jpg').write_bytes(b'x' * 50)
        assert dm._scan_lock.acquire(blocking=False)
        try:
            assert dm.scan_sessions(str(save_dir)) == first   # no second walk
        finally:
            dm._scan_lock.release()
        assert dm.scan_sessions(str(save_dir))[0]['image_count'] == 2
