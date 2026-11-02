"""
Recording storage found again after boot (owl.py _retry_storage_setup).

Pi 5 bench 2026-10-29: Save to changed in Settings while the old location was
still writable kept recording (and the Data page listing) on the old location,
and storage found by the record-time re-scan never reached the published
save_directory, so the Data and Map pages showed "No sessions".
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import utils.error_manager as errors
from owl import Owl


def _owl(**overrides):
    fake = SimpleNamespace(
        config=MagicMock(),
        storage_location='internal',
        internal_save_directory='/home/owl/owl_images',
        min_free_gb=4,
        sample_method='whole',
        jpeg_quality=95,
        status_indicator=MagicMock(error_code=0),
        dash=MagicMock(),
        logger=MagicMock(),
        save_directory='/media/owl/SanDisk',
        save_subdirectory='/media/owl/SanDisk',
        image_recorder=MagicMock(),
        _storage_resolved_for=('usb', '/home/owl/owl_images'),
        _classify_storage_error=Owl._classify_storage_error,
        _is_storage_fallback=lambda: False,
    )
    fake._storage_choice = lambda: Owl._storage_choice(fake)
    for key, value in overrides.items():
        setattr(fake, key, value)
    return fake


@pytest.mark.unit
class TestStorageReresolve:

    def test_choice_tracks_live_settings(self):
        fake = _owl()
        assert Owl._storage_choice(fake) == ('internal', '/home/owl/owl_images')
        fake.storage_location = 'usb'
        assert Owl._storage_choice(fake) != ('internal', '/home/owl/owl_images')

    def test_found_storage_is_published_with_its_path(self):
        fake = _owl()
        with patch('owl.DirectorySetup') as setup, patch('owl.ImageRecorder'):
            setup.return_value.setup_directories.return_value = (
                '/home/owl/owl_images', '/home/owl/owl_images')
            setup.return_value.resolved_location = 'internal'
            assert Owl._retry_storage_setup(fake) is True
        assert fake.save_directory == '/home/owl/owl_images'
        assert fake._storage_resolved_for == ('internal', '/home/owl/owl_images')
        fake.dash.set_recording_storage.assert_called_once_with(
            'internal', fallback=False, save_directory='/home/owl/owl_images')

    def test_nothing_writable_stops_recording_into_the_old_place(self):
        fake = _owl(storage_location='usb')
        with patch('owl.DirectorySetup') as setup:
            setup.return_value.setup_directories.side_effect = errors.NoWritableUSBError()
            assert Owl._retry_storage_setup(fake) is False
        assert fake.save_directory is None
        assert fake.image_recorder is None
        # Remembered, so the idle loop does not re-scan every frame
        assert fake._storage_resolved_for == ('usb', '/home/owl/owl_images')
        fake.dash.set_recording_blocked.assert_called_once_with('no_drive')
