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

    def test_named_preset_stays_frozen(self, standalone_test_client):
        """Frozen-preset model: live changes never mutate a named preset —
        they divert to the autosave working file with provenance."""
        client, dashboard, tmp_dir = standalone_test_client

        custom_path = os.path.join(str(tmp_dir), 'my_config.ini')
        shutil.copy(os.path.join(str(tmp_dir), 'GENERAL_CONFIG.ini'), custom_path)
        with open(custom_path) as f:
            original = f.read()

        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/my_config.ini')

        dashboard._persist_config_change('GreenOnBrown', 'exg_min', '42')

        # Named preset untouched
        with open(custom_path) as f:
            assert f.read() == original

        # Change landed in the autosave working file with provenance
        from utils.config_manager import AUTOSAVE_CONFIG
        config = configparser.ConfigParser()
        config.read(os.path.join(str(tmp_dir), AUTOSAVE_CONFIG))
        assert config.get('GreenOnBrown', 'exg_min') == '42'
        assert config.get('Meta', 'source') == 'my_config.ini'

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
        new_config = configparser.ConfigParser()
        assert new_config.read(new_path), f'the active pointer names a file that is not there: {new_path}'
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

        # Change diverts to the autosave working file (frozen-preset model)
        from utils.config_manager import AUTOSAVE_CONFIG
        config = configparser.ConfigParser()
        config.read(os.path.join(str(tmp_dir), AUTOSAVE_CONFIG))
        assert config.get('GreenOnGreen', 'confidence') == '0.75'

    def test_a_pointer_to_a_deleted_profile_heals_and_the_change_is_still_saved(
            self, standalone_test_client):
        """active_config.txt names a profile that is gone (deleted, or a USB
        restore): the setting the operator just changed must not be lost."""
        client, dashboard, tmp_dir = standalone_test_client

        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/NONEXISTENT.ini')

        assert dashboard._persist_config_change('GreenOnBrown', 'exg_min', '42')

        assert not os.path.exists(os.path.join(str(tmp_dir), 'NONEXISTENT.ini'))
        active = dashboard._resolve_config_path(dashboard._get_active_config_path())
        saved = configparser.ConfigParser()
        assert saved.read(active), f'active profile {active} does not exist'
        assert saved.get('GreenOnBrown', 'exg_min') == '42'
        # the shipped default is never the file that gets edited
        shipped = configparser.ConfigParser()
        shipped.read(os.path.join(str(tmp_dir), 'GENERAL_CONFIG.ini'))
        assert shipped.get('GreenOnBrown', 'exg_min') != '42'


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
