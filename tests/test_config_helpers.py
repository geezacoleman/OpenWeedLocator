"""Unit tests for the shared config_manager helpers introduced by the audit fixes:
atomic_write_config (crash-safe INI writes) and stamp_config_meta (authoritative
[Meta] re-stamp that fixes the DuplicateSectionError round-trip)."""

import configparser
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config_manager import atomic_write_config, stamp_config_meta, parse_config_meta


@pytest.mark.unit
class TestAtomicWriteConfig:
    def test_writes_config_atomically(self, tmp_path):
        target = tmp_path / 'out.ini'
        cp = configparser.ConfigParser()
        cp.add_section('System')
        cp.set('System', 'algorithm', 'exg')
        atomic_write_config(target, cp.write)
        assert target.exists()
        rp = configparser.ConfigParser()
        rp.read(target)
        assert rp.get('System', 'algorithm') == 'exg'

    def test_failure_leaves_original_intact_and_no_temp(self, tmp_path):
        target = tmp_path / 'keep.ini'
        target.write_text('[System]\nalgorithm = original\n')

        def boom(f):
            f.write('[System]\nalgorithm = new\n')
            raise RuntimeError('write failed mid-way')

        with pytest.raises(RuntimeError):
            atomic_write_config(target, boom)

        # Original untouched, and no temp files left behind.
        assert target.read_text() == '[System]\nalgorithm = original\n'
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith('.owl_cfg_')]
        assert leftovers == []


@pytest.mark.unit
class TestStampConfigMeta:
    def _cfg(self):
        cp = configparser.ConfigParser()
        cp.optionxform = str
        return cp

    def test_stamps_name_and_notes(self):
        cp = self._cfg()
        stamp_config_meta(cp, 'Wheat', 'dewy')
        assert cp.get('Meta', 'display_name') == 'Wheat'
        assert cp.get('Meta', 'notes') == 'dewy'
        assert cp.has_option('Meta', 'created')

    def test_noop_when_nothing_to_record(self):
        cp = self._cfg()
        stamp_config_meta(cp, '', '')
        assert not cp.has_section('Meta')

    def test_never_duplicates_existing_meta(self):
        # Simulates the round-trip: a [Meta] already present must not raise.
        cp = self._cfg()
        cp.add_section('Meta')
        cp.set('Meta', 'display_name', 'stale')
        stamp_config_meta(cp, 'Fresh', '')   # must not raise DuplicateSectionError
        assert cp.get('Meta', 'display_name') == 'Fresh'

    def test_update_preserves_existing_name_and_created(self, tmp_path):
        existing = tmp_path / 'wheat.ini'
        existing.write_text(
            '[Meta]\ndisplay_name = Wheat\nnotes = am\ncreated = 2020-01-01T00:00:00\n')
        cp = self._cfg()
        # Overwrite with no name/notes supplied -> preserve from the existing file.
        stamp_config_meta(cp, '', '', existing_path=str(existing))
        assert cp.get('Meta', 'display_name') == 'Wheat'
        assert cp.get('Meta', 'notes') == 'am'
        assert cp.get('Meta', 'created') == '2020-01-01T00:00:00'   # original kept
