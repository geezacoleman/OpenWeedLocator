"""Tests for SensitivityManager — preset load, apply, save, delete, persist."""

import configparser
import os

import pytest

from utils.sensitivity_manager import SensitivityManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path, sections=True):
    """Write a GENERAL_CONFIG.ini with or without [Sensitivity_*] sections.
    Returns (config, config_path).
    """
    ini = """\
[System]
algorithm = exhsv
relay_num = 4

[Controller]
controller_type = none

[Camera]
resolution_width = 640
resolution_height = 480

[GreenOnBrown]
exg_min = 25
exg_max = 200
hue_min = 39
hue_max = 83
saturation_min = 50
saturation_max = 220
brightness_min = 60
brightness_max = 190
min_detection_area = 10
invert_hue = False

[Relays]
0 = 13
"""
    if sections:
        ini += """
[Sensitivity]
active = medium

[Sensitivity_Low]
exg_min = 25
exg_max = 200
hue_min = 41
hue_max = 80
saturation_min = 52
saturation_max = 218
brightness_min = 62
brightness_max = 188
min_detection_area = 20

[Sensitivity_Medium]
exg_min = 25
exg_max = 200
hue_min = 39
hue_max = 83
saturation_min = 50
saturation_max = 220
brightness_min = 60
brightness_max = 190
min_detection_area = 10

[Sensitivity_High]
exg_min = 22
exg_max = 210
hue_min = 35
hue_max = 85
saturation_min = 40
saturation_max = 225
brightness_min = 50
brightness_max = 200
min_detection_area = 5
"""
    path = tmp_path / 'GENERAL_CONFIG.ini'
    path.write_text(ini)
    config = configparser.ConfigParser()
    config.read(str(path))
    return config, str(path)


class _FakeOwl:
    """Minimal owl-like object with GreenOnBrown attributes."""
    def __init__(self):
        self.exg_min = 0
        self.exg_max = 0
        self.hue_min = 0
        self.hue_max = 0
        self.saturation_min = 0
        self.saturation_max = 0
        self.brightness_min = 0
        self.brightness_max = 0
        self.min_detection_area = 0
        self.show_display = False
        self._pending_trackbar_updates = {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSensitivityManagerInit:
    def test_loads_presets_from_config(self, tmp_path):
        config, path = _make_config(tmp_path, sections=True)
        sm = SensitivityManager(config, path)
        names = {p['name'] for p in sm.list_presets()}
        assert 'low' in names
        assert 'medium' in names
        assert 'high' in names

    def test_fallback_to_builtins_when_no_sections(self, tmp_path):
        config, path = _make_config(tmp_path, sections=False)
        sm = SensitivityManager(config, path)
        names = {p['name'] for p in sm.list_presets()}
        assert 'low' in names
        assert 'medium' in names
        assert 'high' in names

    def test_active_preset_defaults_to_medium(self, tmp_path):
        config, path = _make_config(tmp_path, sections=False)
        sm = SensitivityManager(config, path)
        assert sm.get_active_preset() == 'medium'

    def test_active_preset_reads_from_config(self, tmp_path):
        config, path = _make_config(tmp_path, sections=True)
        sm = SensitivityManager(config, path)
        assert sm.get_active_preset() == 'medium'


class TestGetPresetValues:
    def test_returns_values(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        vals = sm.get_preset_values('high')
        assert vals is not None
        assert vals['exg_min'] == 22
        assert vals['min_detection_area'] == 5

    def test_returns_none_for_unknown(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        assert sm.get_preset_values('nonexistent') is None


class TestApplyPreset:
    def test_applies_values_to_owl(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()
        result = sm.apply_preset('high', owl)
        assert result is True
        assert owl.exg_min == 22
        assert owl.hue_min == 35
        assert owl.min_detection_area == 5

    def test_updates_active_in_config(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()
        sm.apply_preset('low', owl)
        assert sm.get_active_preset() == 'low'

    def test_returns_false_for_unknown(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()
        result = sm.apply_preset('nonexistent', owl)
        assert result is False

    def test_queues_trackbar_updates(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()
        owl.show_display = True
        sm.apply_preset('high', owl)
        assert 'ExG-Min' in owl._pending_trackbar_updates
        assert owl._pending_trackbar_updates['ExG-Min'] == 22

    def test_case_insensitive(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()
        assert sm.apply_preset('HIGH', owl) is True
        assert sm.apply_preset('Low', owl) is True


class TestSaveCustomPreset:
    def test_save_and_retrieve(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        values = {k: 42 for k in SensitivityManager.SENSITIVITY_KEYS}
        result = sm.save_custom_preset('canola', values)
        assert result is True

        retrieved = sm.get_preset_values('canola')
        assert retrieved is not None
        assert all(v == 42 for v in retrieved.values())

    def test_persists_to_disk(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        values = {k: 99 for k in SensitivityManager.SENSITIVITY_KEYS}
        sm.save_custom_preset('test_preset', values)

        # Re-read from wherever persist() wrote (may be copy-on-write)
        config2 = configparser.ConfigParser()
        config2.read(sm.config_path)
        assert config2.has_section('Sensitivity_Test_preset')
        assert config2.getint('Sensitivity_Test_preset', 'exg_min') == 99

    def test_save_from_owl_instance(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()
        sm.apply_preset('high', owl)
        result = sm.save_custom_preset('my_high', owl_instance=owl)
        assert result is True
        vals = sm.get_preset_values('my_high')
        assert vals['exg_min'] == 22

    def test_empty_name_rejected(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        values = {k: 42 for k in SensitivityManager.SENSITIVITY_KEYS}
        assert sm.save_custom_preset('', values) is False

    def test_missing_keys_rejected(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        assert sm.save_custom_preset('bad', {'exg_min': 1}) is False

    def test_shows_in_list(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        values = {k: 42 for k in SensitivityManager.SENSITIVITY_KEYS}
        sm.save_custom_preset('canola', values)

        presets = sm.list_presets()
        names = {p['name'] for p in presets}
        assert 'canola' in names

        canola = next(p for p in presets if p['name'] == 'canola')
        assert canola['is_builtin'] is False


class TestDeleteCustomPreset:
    def test_delete_custom(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        values = {k: 42 for k in SensitivityManager.SENSITIVITY_KEYS}
        sm.save_custom_preset('temp', values)
        assert sm.delete_custom_preset('temp') is True
        assert sm.get_preset_values('temp') is None

    def test_cannot_delete_builtin(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        assert sm.delete_custom_preset('low') is False
        assert sm.delete_custom_preset('medium') is False
        assert sm.delete_custom_preset('high') is False
        # Verify still present
        assert sm.get_preset_values('low') is not None

    def test_delete_nonexistent(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        assert sm.delete_custom_preset('ghost') is False

    def test_deleted_active_falls_back(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        values = {k: 42 for k in SensitivityManager.SENSITIVITY_KEYS}
        sm.save_custom_preset('temp', values)
        owl = _FakeOwl()
        sm.apply_preset('temp', owl)
        assert sm.get_active_preset() == 'temp'
        sm.delete_custom_preset('temp')
        assert sm.get_active_preset() == 'medium'


class TestListPresets:
    def test_list_includes_builtins(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        presets = sm.list_presets()
        builtin_names = {p['name'] for p in presets if p['is_builtin']}
        assert builtin_names == {'low', 'medium', 'high'}

    def test_list_has_values(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        presets = sm.list_presets()
        for p in presets:
            assert len(p['values']) == 9
            assert 'exg_min' in p['values']


class TestPersist:
    def test_persist_survives_reload(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()
        sm.apply_preset('low', owl)
        sm.persist()

        # Re-read from wherever persist() wrote (may be copy-on-write)
        config2 = configparser.ConfigParser()
        config2.read(sm.config_path)
        assert config2.get('Sensitivity', 'active') == 'low'

    def test_hardware_settings_preserved(self, tmp_path):
        config, path = _make_config(tmp_path)
        sm = SensitivityManager(config, path)
        owl = _FakeOwl()

        # Apply different presets — relay_num must never change
        sm.apply_preset('low', owl)
        sm.apply_preset('high', owl)
        sm.persist()

        config2 = configparser.ConfigParser()
        config2.read(sm.config_path)
        assert config2.getint('System', 'relay_num') == 4
        assert config2.getint('Camera', 'resolution_width') == 640

    def test_protected_config_not_overwritten(self, tmp_path):
        """GENERAL_CONFIG.ini must never be modified — persist creates a copy."""
        config, path = _make_config(tmp_path)
        original_content = open(path).read()

        sm = SensitivityManager(config, path)
        values = {k: 77 for k in SensitivityManager.SENSITIVITY_KEYS}
        sm.save_custom_preset('field_test', values)

        # Original file must be untouched
        assert open(path).read() == original_content

        # persist() should have created a new file and updated config_path
        assert sm.config_path != path
        assert os.path.exists(sm.config_path)

        # New file should contain the preset
        config2 = configparser.ConfigParser()
        config2.read(sm.config_path)
        assert config2.has_section('Sensitivity_Field_test')

        # active_config.txt should point to the new file
        pointer = os.path.join(tmp_path, 'active_config.txt')
        assert os.path.exists(pointer)
        assert os.path.basename(sm.config_path) in open(pointer).read()
