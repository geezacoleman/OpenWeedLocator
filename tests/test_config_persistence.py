"""Tests for config persistence — settings must survive power cycles.

Priority 5 — the snap-back bug showed config persistence is fragile.
"""

import configparser
import os
import shutil

import pytest


# ---------------------------------------------------------------------------
# _persist_config_change (standalone dashboard)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPersistConfigChange:
    """Tests for standalone OWLDashboard._persist_config_change()."""

    def test_writes_to_non_protected_file(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Create a custom config file (non-protected)
        custom_path = os.path.join(str(tmp_dir), 'my_config.ini')
        shutil.copy(os.path.join(str(tmp_dir), 'GENERAL_CONFIG.ini'), custom_path)

        # Set active config to the custom file
        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/my_config.ini')

        dashboard._persist_config_change('GreenOnBrown', 'exg_min', '42')

        # Verify the file was updated
        config = configparser.ConfigParser()
        config.read(custom_path)
        assert config.get('GreenOnBrown', 'exg_min') == '42'

    def test_protected_default_triggers_copy_on_write(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Active config points to a protected default
        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/GENERAL_CONFIG.ini')

        original_path = os.path.join(str(tmp_dir), 'GENERAL_CONFIG.ini')
        original_config = configparser.ConfigParser()
        original_config.read(original_path)
        original_exg_min = original_config.get('GreenOnBrown', 'exg_min')

        dashboard._persist_config_change('GreenOnBrown', 'exg_min', '99')

        # Original protected file should be UNCHANGED
        reread = configparser.ConfigParser()
        reread.read(original_path)
        assert reread.get('GreenOnBrown', 'exg_min') == original_exg_min

        # A new file should have been created (config_TIMESTAMP.ini)
        new_active = dashboard._get_active_config_path()
        assert 'config_' in new_active  # contains timestamp
        new_path = dashboard._resolve_config_path(new_active)
        if os.path.exists(new_path):
            new_config = configparser.ConfigParser()
            new_config.read(new_path)
            assert new_config.get('GreenOnBrown', 'exg_min') == '99'

    def test_creates_section_if_missing(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        custom_path = os.path.join(str(tmp_dir), 'minimal.ini')
        with open(custom_path, 'w') as f:
            f.write('[System]\nalgorithm = exhsv\n')

        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/minimal.ini')

        dashboard._persist_config_change('GreenOnGreen', 'confidence', '0.75')

        config = configparser.ConfigParser()
        config.read(custom_path)
        assert config.get('GreenOnGreen', 'confidence') == '0.75'

    def test_missing_config_file_logged(self, standalone_test_client):
        """If the active config file doesn't exist, method should not crash."""
        client, dashboard, tmp_dir = standalone_test_client

        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/NONEXISTENT.ini')

        # Should not crash
        dashboard._persist_config_change('GreenOnBrown', 'exg_min', '42')


# ---------------------------------------------------------------------------
# Config round-trip: change -> save -> reload -> verify
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestConfigRoundTrip:
    """Config round-trip via standalone API."""

    def test_save_and_reload_preserves_values(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Save a new config
        new_config = {
            'System': {'algorithm': 'gog'},
            'GreenOnBrown': {'exg_min': '42', 'exg_max': '180'}
        }
        resp = client.post('/api/config', json={
            'config': new_config,
            'filename': 'roundtrip_test.ini',
            'set_active': True
        })
        assert resp.status_code == 200

        # Reload it
        resp = client.get('/api/config')
        data = resp.get_json()
        assert data['success'] is True
        assert data['config']['GreenOnBrown']['exg_min'] == '42'
        assert data['config']['System']['algorithm'] == 'gog'


# ---------------------------------------------------------------------------
# active_config.txt management
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestActiveConfigPointer:
    """Tests for active_config.txt updates."""

    def test_set_active_writes_pointer(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/set-active', json={
            'config': 'GENERAL_CONFIG.ini'
        })
        assert resp.status_code == 200

        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'r') as f:
            assert 'GENERAL_CONFIG.ini' in f.read()

    def test_delete_active_config_resets_to_default(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Create and set a custom config as active
        custom = os.path.join(str(tmp_dir), 'to_delete.ini')
        with open(custom, 'w') as f:
            f.write('[System]\nalgorithm = exg\n')

        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/to_delete.ini')

        # Delete it
        resp = client.post('/api/config/delete', json={'config': 'to_delete.ini'})
        assert resp.status_code == 200

        # Pointer should have been removed (reverting to default)
        assert not os.path.exists(pointer)


# ---------------------------------------------------------------------------
# Two-file merge: detection preset + CONTROLLER.ini layering
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTwoFileMerge:
    """Tests for ConfigParser.read() merge order (detection + infrastructure)."""

    def test_later_file_wins(self, tmp_path):
        """CONTROLLER.ini values should override detection preset values."""
        # Detection preset
        detection = tmp_path / 'detection.ini'
        detection.write_text(
            '[MQTT]\nbroker_ip = 10.0.0.1\n'
            '[GreenOnBrown]\nexg_min = 25\n'
        )

        # Controller override
        controller = tmp_path / 'CONTROLLER.ini'
        controller.write_text(
            '[MQTT]\nbroker_ip = localhost\nbroker_port = 1883\n'
        )

        config = configparser.ConfigParser()
        config.read([str(detection), str(controller)])

        # CONTROLLER.ini value should win for broker_ip
        assert config.get('MQTT', 'broker_ip') == 'localhost'
        # broker_port only in CONTROLLER.ini
        assert config.get('MQTT', 'broker_port') == '1883'
        # GreenOnBrown only in detection preset
        assert config.get('GreenOnBrown', 'exg_min') == '25'

    def test_detection_preset_sections_preserved(self, tmp_path):
        """Detection-only sections remain when CONTROLLER.ini is merged."""
        detection = tmp_path / 'detection.ini'
        detection.write_text(
            '[System]\nalgorithm = exhsv\n'
            '[GreenOnBrown]\nexg_min = 25\n'
            '[Camera]\nresolution_width = 416\n'
        )

        controller = tmp_path / 'CONTROLLER.ini'
        controller.write_text('[MQTT]\nbroker_ip = localhost\n')

        config = configparser.ConfigParser()
        config.read([str(detection), str(controller)])

        assert config.has_section('System')
        assert config.has_section('GreenOnBrown')
        assert config.has_section('Camera')
        assert config.has_section('MQTT')
