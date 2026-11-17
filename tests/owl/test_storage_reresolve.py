"""
Recording storage found again after boot (owl.py _retry_storage_setup), on a
real Owl with a dashboard.

Pi 5 bench 2026-10-29: Save to changed in Settings while the old location was
still writable kept recording (and the Data page listing) on the old location,
and storage found by the record-time re-scan never reached the published
save_directory, so the Data and Map pages showed "No sessions".

Only the missing-USB-drive case stubs anything (DirectorySetup raising what it
raises with no drive): a real scan would look at whatever is plugged into the
machine running the tests.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import utils.error_manager as errors
from harness import DASH, run_hoot


@pytest.fixture
def owl(owl_factory, config_factory, input_dir):
    config = config_factory(overrides=[('DataCollection', 'image_sample_enable', 'True'),
                                       ('DataCollection', 'sample_frequency', '1'),
                                       ('DataCollection', 'detection_enable', 'True'),
                                       *DASH['standalone']])
    return owl_factory(config, input_file_or_directory=str(input_dir))


def test_choice_tracks_live_settings(owl):
    first = owl._storage_choice()
    owl.storage_location = 'usb'
    assert owl._storage_choice() != first
    assert owl._storage_choice()[0] == 'usb'


def test_a_changed_save_location_is_where_images_land_and_what_is_published(owl, tmp_path):
    old = Path(owl.save_directory)
    new = tmp_path / 'moved_here'
    owl.internal_save_directory = str(new)          # as set_config_section does

    assert owl._retry_storage_setup() is True
    assert Path(owl.save_directory).resolve() == new.resolve()
    assert owl._storage_resolved_for == ('internal', str(new))
    assert Path(owl.dash.state['save_directory']).resolve() == new.resolve()
    assert owl.dash.state['recording_location'] == 'internal'
    assert owl.dash.state.get('recording_blocked_reason') in (None, '')

    with owl.dash.state_lock:
        owl.dash.state['image_sample_enable'] = True
    owl._update_state_once()
    run_hoot(owl, frames=4)
    assert sorted(new.rglob('*.jpg')), 'no image was saved in the new location'
    assert not sorted(old.rglob('*.jpg')), 'images still went to the old location'


@pytest.mark.parametrize('error, reason', [
    (errors.NoWritableUSBError(), 'no_drive'),
])
def test_nothing_writable_stops_recording_into_the_old_place(owl, error, reason):
    old = Path(owl.save_directory)
    owl.storage_location = 'usb'
    with patch('owl.DirectorySetup') as setup:
        setup.return_value.setup_directories.side_effect = error
        assert owl._retry_storage_setup() is False

    assert owl.save_directory is None
    assert owl.image_recorder is None
    # Remembered, so the idle loop does not re-scan every frame
    assert owl._storage_resolved_for == owl._storage_choice()
    assert owl.dash.state['recording_blocked_reason'] == reason
    assert owl.dash.state['recording_location'] is None

    with owl.dash.state_lock:
        owl.dash.state['image_sample_enable'] = True
    with patch('owl.DirectorySetup') as setup:
        setup.return_value.setup_directories.side_effect = error
        owl._update_state_once()
        run_hoot(owl, frames=4)
    assert not sorted(old.rglob('*.jpg')), 'recording carried on into the old location'
