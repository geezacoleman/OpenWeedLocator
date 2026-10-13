"""active_config.txt is never seen empty while the dashboard rewrites it.

A plain open(..., 'w') truncates before writing, so a reader in that gap
(owl.py at boot, another request, a test polling the saved config) finds no
active config. CI hit it as a NoSectionError in
test_saved_once_the_owl_reports_it_running.
"""
import sys
import threading

import pytest


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == 'win32', reason='os.replace cannot swap a file another thread has open on Windows; the OWL runs Linux')
def test_pointer_is_never_read_empty_during_rewrites(standalone_test_client):
    _, dashboard, tmp_dir = standalone_test_client
    pointer = tmp_dir / 'active_config.txt'
    stop = threading.Event()
    seen_empty = []

    def read():
        while not stop.is_set():
            try:
                if not pointer.read_text().strip():
                    seen_empty.append(True)
            except FileNotFoundError:
                pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for i in range(1500):
            name = 'config/GENERAL_CONFIG.ini' if i % 2 else 'config/config_autosave.ini'
            dashboard._set_active_config(name, seed=False)
    finally:
        stop.set()
        reader.join(timeout=5)

    assert not seen_empty, f'pointer read empty {len(seen_empty)} times'
