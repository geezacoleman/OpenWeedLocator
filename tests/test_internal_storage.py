"""R2 (v3.10.0) internal (eMMC) storage mode.

OWL 3.0 is a CM5 in a sealed enclosure: no USB port, 16 GB eMMC shared with
the OS. Recording must work on internal storage behind a hard free-space
floor (min_free_gb) so images can never brick the root filesystem.
"""

import configparser
import os
import re
from collections import namedtuple
from unittest.mock import MagicMock, patch

import pytest

import utils.error_manager as errors
from utils.directory_manager import DirectorySetup, GB

DiskUsage = namedtuple('usage', ['total', 'used', 'free'])


# ---------------------------------------------------------------------------
# DirectorySetup: internal + auto modes (Phase 5)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDirectorySetupInternal:

    def test_internal_mode_creates_and_returns(self, tmp_path):
        internal = str(tmp_path / 'owl_images')
        ds = DirectorySetup(save_directory='ignored', storage_location='internal',
                            internal_save_directory=internal)
        save_dir, subdir = ds.setup_directories(max_retries=1)
        assert save_dir == internal
        assert os.path.isdir(subdir)
        assert re.match(r'^\d{8}$', os.path.basename(subdir))

    def test_internal_mode_skips_mount_gate(self, tmp_path):
        ds = DirectorySetup(save_directory='ignored', storage_location='internal',
                            internal_save_directory=str(tmp_path / 'owl_images'))
        with patch('utils.directory_manager.os.path.ismount') as mount_check:
            ds.setup_directories(max_retries=1)
        mount_check.assert_not_called()

    def test_internal_mode_without_path_raises(self):
        ds = DirectorySetup(save_directory='ignored', storage_location='internal',
                            internal_save_directory=None)
        with pytest.raises(errors.StorageSystemError):
            ds.setup_directories(max_retries=1)

    def test_internal_floor_refuses_setup(self, tmp_path):
        """Recording must be refused when free space is already below the
        floor: the watchdog would snap it off within 15 s anyway."""
        ds = DirectorySetup(save_directory='ignored', storage_location='internal',
                            internal_save_directory=str(tmp_path), min_free_gb=4)
        low = DiskUsage(total=16 * GB, used=15 * GB, free=1 * GB)
        with patch('utils.directory_manager.shutil.disk_usage', return_value=low):
            with pytest.raises(errors.StorageSystemError, match='floor'):
                ds.setup_directories(max_retries=1)

    def test_internal_floor_allows_above(self, tmp_path):
        ds = DirectorySetup(save_directory='ignored', storage_location='internal',
                            internal_save_directory=str(tmp_path), min_free_gb=4)
        roomy = DiskUsage(total=16 * GB, used=6 * GB, free=10 * GB)
        with patch('utils.directory_manager.shutil.disk_usage', return_value=roomy):
            save_dir, subdir = ds.setup_directories(max_retries=1)
        assert save_dir == str(tmp_path)

    def test_auto_prefers_usb_when_mounted(self, tmp_path):
        usb = str(tmp_path / 'usb')
        ds = DirectorySetup(save_directory=usb, storage_location='auto',
                            internal_save_directory=str(tmp_path / 'internal'))
        with patch('utils.directory_manager.os.path.ismount', return_value=True):
            save_dir, subdir = ds.setup_directories(max_retries=1)
        assert save_dir == usb
        assert not os.path.exists(str(tmp_path / 'internal'))

    def test_auto_falls_back_to_internal(self, tmp_path):
        internal = str(tmp_path / 'internal')
        ds = DirectorySetup(save_directory=str(tmp_path / 'usb'),
                            storage_location='auto',
                            internal_save_directory=internal)
        # Simulate a Pi (Linux) with nothing mounted in /media
        with patch('utils.directory_manager.os.path.ismount', return_value=False), \
                patch('utils.directory_manager.platform.system', return_value='Linux'), \
                patch.object(DirectorySetup, '_find_mounted_drives', return_value=[]):
            save_dir, subdir = ds.setup_directories(max_retries=1)
        assert save_dir == internal
        assert os.path.isdir(subdir)

    def test_usb_mode_unchanged_raises_without_drive(self, tmp_path):
        """Legacy default: no internal fallback unless auto/internal is set."""
        ds = DirectorySetup(save_directory=str(tmp_path / 'usb'))
        with patch('utils.directory_manager.os.path.ismount', return_value=False), \
                patch('utils.directory_manager.platform.system', return_value='Linux'), \
                patch.object(DirectorySetup, '_find_mounted_drives', return_value=[]):
            with pytest.raises(errors.NoWritableUSBError):
                ds.setup_directories(max_retries=1)


# ---------------------------------------------------------------------------
# Config validation for the new keys (Phase 5)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStorageLocationConfig:

    def _config_with(self, value):
        config = configparser.ConfigParser()
        config.add_section('DataCollection')
        if value is not None:
            config.set('DataCollection', 'storage_location', value)
        return config

    @pytest.mark.parametrize('value', ['usb', 'internal', 'auto', 'INTERNAL'])
    def test_valid_locations_pass(self, value):
        from utils.config_manager import ConfigValidator
        ok, errs = ConfigValidator.validate_storage_location(self._config_with(value))
        assert ok, errs

    def test_absent_key_passes(self):
        from utils.config_manager import ConfigValidator
        ok, errs = ConfigValidator.validate_storage_location(self._config_with(None))
        assert ok

    def test_typo_flagged_with_options(self):
        from utils.config_manager import ConfigValidator
        ok, errs = ConfigValidator.validate_storage_location(self._config_with('emmc'))
        assert not ok
        message = errs['DataCollection']['storage_location']
        for option in ('usb', 'internal', 'auto'):
            assert option in message

    def test_new_keys_registered(self):
        """String keys live in optional_keys only; numeric keys also get
        VALUE_VALIDATORS 3-tuples (project rule)."""
        from utils.config_manager import ConfigValidator
        optional = ConfigValidator.REQUIRED_CONFIG['DataCollection']['optional_keys']
        for key in ('storage_location', 'internal_save_directory',
                    'min_free_gb', 'image_quota_gb'):
            assert key in optional, key
        assert ConfigValidator.VALUE_VALIDATORS['min_free_gb'][0] == 'int'
        assert ConfigValidator.VALUE_VALIDATORS['image_quota_gb'][0] == 'int'
        assert 'storage_location' not in ConfigValidator.VALUE_VALIDATORS
        assert 'internal_save_directory' not in ConfigValidator.VALUE_VALIDATORS


# ---------------------------------------------------------------------------
# Floor watchdog: status indicator internal mode (Phase 6)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestInternalFloorWatchdog:

    def _indicator(self, tmp_path, min_free_gb=4):
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=str(tmp_path))
        indicator.storage_location = 'internal'
        indicator.min_free_gb = min_free_gb
        return indicator

    def _update_with_free(self, indicator, free_gb):
        from utils.output_manager import BaseStatusIndicator
        usage = DiskUsage(total=16 * GB, used=(16 - free_gb) * GB,
                          free=int(free_gb * GB))
        with patch('utils.output_manager.shutil.disk_usage', return_value=usage):
            indicator.update()

    def test_breach_latches_drive_full(self, tmp_path):
        indicator = self._indicator(tmp_path)
        self._update_with_free(indicator, 3)
        assert indicator.DRIVE_FULL is True
        assert indicator.storage_warning == 'full'
        indicator.stop()

    def test_warn_band_below_floor_plus_two(self, tmp_path):
        indicator = self._indicator(tmp_path)
        self._update_with_free(indicator, 5)   # floor 4, warn band < 6
        assert indicator.DRIVE_FULL is False
        assert indicator.storage_warning == 'low'
        indicator.stop()

    def test_ok_above_warn_band(self, tmp_path):
        indicator = self._indicator(tmp_path)
        self._update_with_free(indicator, 10)
        assert indicator.DRIVE_FULL is False
        assert indicator.storage_warning == 'ok'
        indicator.stop()

    def test_recovery_clears_drive_full(self, tmp_path):
        """Farmer deletes sessions: recording must become available again
        without a restart."""
        indicator = self._indicator(tmp_path)
        self._update_with_free(indicator, 3)
        assert indicator.DRIVE_FULL is True
        self._update_with_free(indicator, 10)
        assert indicator.DRIVE_FULL is False
        assert indicator.error_code is None
        assert indicator.storage_warning == 'ok'
        indicator.stop()

    def test_percent_rule_not_applied_internally(self, tmp_path):
        """16 GB eMMC at 80% used still has only 3.2 GB free — the 90%
        percent rule would allow free space under the floor."""
        indicator = self._indicator(tmp_path)
        self._update_with_free(indicator, 3.2)   # 80% used on 16 GB
        assert indicator.DRIVE_FULL is True
        indicator.stop()

    def test_transition_logging_once(self, tmp_path, caplog):
        import logging
        indicator = self._indicator(tmp_path)
        with caplog.at_level(logging.INFO, logger='utils.output_manager'):
            self._update_with_free(indicator, 3)
            self._update_with_free(indicator, 3)
            self._update_with_free(indicator, 3)
            self._update_with_free(indicator, 10)
        full_logs = [r for r in caplog.records if 'Storage full' in r.message]
        recovered_logs = [r for r in caplog.records if 'Storage recovered' in r.message]
        assert len(full_logs) == 1
        assert len(recovered_logs) == 1
        indicator.stop()

    def test_usb_mode_warning_levels(self, tmp_path):
        from utils.output_manager import HeadlessStatusIndicator
        indicator = HeadlessStatusIndicator(save_directory=str(tmp_path))
        assert indicator.storage_location == 'usb'

        usage = DiskUsage(total=100 * GB, used=91 * GB, free=9 * GB)
        with patch('utils.output_manager.shutil.disk_usage', return_value=usage):
            indicator.update()
        assert indicator.storage_warning == 'full'
        assert indicator.DRIVE_FULL is True

        usage = DiskUsage(total=100 * GB, used=86 * GB, free=14 * GB)
        with patch('utils.output_manager.shutil.disk_usage', return_value=usage):
            indicator.update()
        assert indicator.storage_warning == 'low'
        assert indicator.DRIVE_FULL is False

        usage = DiskUsage(total=100 * GB, used=50 * GB, free=50 * GB)
        with patch('utils.output_manager.shutil.disk_usage', return_value=usage):
            indicator.update()
        assert indicator.storage_warning == 'ok'
        indicator.stop()


# ---------------------------------------------------------------------------
# State plumbing: OWLMQTTPublisher.set_storage_status (Phase 6)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStorageStatusState:

    def _publisher(self):
        from utils.mqtt_manager import OWLMQTTPublisher
        publisher = OWLMQTTPublisher(broker_host='localhost', broker_port=1883,
                                     client_id='test_storage', device_id='test-owl')
        publisher.client = MagicMock()
        publisher.connected = True
        publisher._publish_state = MagicMock()
        return publisher

    def test_state_seeds_additive_fields(self):
        publisher = self._publisher()
        assert publisher.state['storage_location'] == 'usb'
        assert publisher.state['storage_free_mb'] is None
        assert publisher.state['storage_warning'] == 'ok'
        assert publisher.state['image_quota_gb'] == 12

    def test_change_guard_no_republish(self):
        publisher = self._publisher()
        publisher.set_storage_status('internal', 9801, 'ok', 12)
        publisher.set_storage_status('internal', 9803, 'ok', 12)  # same 50 MB bucket
        assert publisher._publish_state.call_count == 1
        assert publisher.state['storage_free_mb'] == 9800  # quantized

    def test_warning_transition_publishes(self):
        publisher = self._publisher()
        publisher.set_storage_status('internal', 5000, 'ok', 12)
        publisher.set_storage_status('internal', 5000, 'low', 12)
        publisher.set_storage_status('internal', 3000, 'full', 12)
        assert publisher._publish_state.call_count == 3
        assert publisher.state['storage_warning'] == 'full'

    def test_none_free_mb_allowed(self):
        publisher = self._publisher()
        publisher.set_storage_status('usb', None, 'ok', 12)
        assert publisher.state['storage_free_mb'] is None
