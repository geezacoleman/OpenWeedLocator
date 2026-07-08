"""Tests for the frozen-preset / autosave-working-file model.

Word-doc semantics: named presets and templates are never mutated by live
changes — everything unsaved lands in config/config_autosave.ini, which is
re-seeded whenever a config is loaded and records its provenance in
[Meta] source. Explicit Save As still creates/updates named files.
"""

import configparser
import os

import pytest

from utils.config_manager import AUTOSAVE_CONFIG, parse_config_meta, seed_autosave
from utils.sensitivity_manager import SensitivityManager


def _read_ini(path):
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(str(path))
    return cp


# ---------------------------------------------------------------------------
# seed_autosave helper
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSeedAutosave:
    def test_seeds_copy_with_source(self, tmp_path):
        preset = tmp_path / 'my_preset.ini'
        preset.write_text('[System]\nalgorithm = exhsv\n')
        autosave = seed_autosave(tmp_path, preset)

        assert os.path.basename(autosave) == AUTOSAVE_CONFIG
        cp = _read_ini(autosave)
        assert cp.get('System', 'algorithm') == 'exhsv'
        assert cp.get('Meta', 'source') == 'my_preset.ini'
        # source file untouched
        assert 'Meta' not in _read_ini(preset).sections()

    def test_noop_for_autosave_itself(self, tmp_path):
        autosave = tmp_path / AUTOSAVE_CONFIG
        autosave.write_text('[System]\nalgorithm = hsv\n')
        seed_autosave(tmp_path, autosave)
        cp = _read_ini(autosave)
        assert cp.get('System', 'algorithm') == 'hsv'
        assert not cp.has_option('Meta', 'source')

    def test_noop_for_missing_source(self, tmp_path):
        seed_autosave(tmp_path, tmp_path / 'ghost.ini')
        assert not (tmp_path / AUTOSAVE_CONFIG).exists()

    def test_parse_config_meta_reads_source(self, tmp_path):
        preset = tmp_path / 'p.ini'
        preset.write_text('[System]\nalgorithm = exhsv\n')
        autosave = seed_autosave(tmp_path, preset)
        assert parse_config_meta(autosave).get('source') == 'p.ini'


# ---------------------------------------------------------------------------
# SensitivityManager.persist diverts everything non-autosave
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSensitivityPersistFrozen:
    def _manager(self, tmp_path, filename):
        path = tmp_path / filename
        path.write_text(
            '[GreenOnBrown]\nexg_min = 25\n\n[Sensitivity]\nactive = medium\n')
        config = configparser.ConfigParser()
        config.read(str(path))
        return SensitivityManager(config, str(path)), path

    def test_named_preset_stays_frozen(self, tmp_path):
        mgr, preset_path = self._manager(tmp_path, 'field_day.ini')
        original = preset_path.read_text()
        mgr.config.set('GreenOnBrown', 'exg_min', '99')
        mgr.persist()

        assert preset_path.read_text() == original          # untouched
        autosave = tmp_path / AUTOSAVE_CONFIG
        assert autosave.exists()
        cp = _read_ini(autosave)
        assert cp.get('GreenOnBrown', 'exg_min') == '99'
        assert cp.get('Meta', 'source') == 'field_day.ini'
        # future writes go in place to the autosave file
        assert os.path.basename(mgr.config_path) == AUTOSAVE_CONFIG

    def test_template_stays_frozen(self, tmp_path):
        mgr, template_path = self._manager(tmp_path, 'GENERAL_CONFIG.ini')
        original = template_path.read_text()
        mgr.persist()
        assert template_path.read_text() == original
        assert (tmp_path / AUTOSAVE_CONFIG).exists()

    def test_autosave_written_in_place_no_new_files(self, tmp_path):
        mgr, _ = self._manager(tmp_path, AUTOSAVE_CONFIG)
        mgr.persist()
        mgr.persist()
        ini_files = [f for f in os.listdir(tmp_path) if f.endswith('.ini')]
        assert ini_files == [AUTOSAVE_CONFIG]


# ---------------------------------------------------------------------------
# Standalone dashboard flow (routes)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStandaloneAutosaveFlow:
    def test_persist_diverts_to_autosave_and_freezes_template(
            self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        template = tmp_dir / 'GENERAL_CONFIG.ini'
        original = template.read_text()

        # A live change (LUT sensitivity persists via _persist_config_change)
        resp = client.post('/api/painter/sensitivity', json={'value': 77})
        assert resp.get_json()['success']

        assert template.read_text() == original             # template frozen
        autosave = tmp_dir / AUTOSAVE_CONFIG
        assert autosave.exists()
        cp = _read_ini(autosave)
        assert cp.get('GreenOnBrown', 'lut_sensitivity') == '77'
        assert cp.get('Meta', 'source') == 'GENERAL_CONFIG.ini'
        active = (tmp_dir / 'active_config.txt').read_text().strip()
        assert active.endswith(AUTOSAVE_CONFIG)

        # Second change: still exactly one autosave file, updated in place
        client.post('/api/painter/sensitivity', json={'value': 33})
        working = [f for f in os.listdir(tmp_dir)
                   if f.startswith('config_') and f.endswith('.ini')]
        assert working == [AUTOSAVE_CONFIG]
        assert _read_ini(autosave).get('GreenOnBrown', 'lut_sensitivity') == '33'

    def test_set_active_reseeds_autosave(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        preset = tmp_dir / 'loaded_one.ini'
        preset.write_text('[System]\nalgorithm = exhsv\n'
                          '[GreenOnBrown]\nexg_min = 42\n')

        resp = client.post('/api/config/set-active',
                           json={'config': 'config/loaded_one.ini'})
        assert resp.get_json().get('success'), resp.get_json()

        autosave = tmp_dir / AUTOSAVE_CONFIG
        assert autosave.exists()
        cp = _read_ini(autosave)
        assert cp.get('GreenOnBrown', 'exg_min') == '42'
        assert cp.get('Meta', 'source') == 'loaded_one.ini'
