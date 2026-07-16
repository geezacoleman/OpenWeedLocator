"""Tests for the 2026-07-13 config fixes:

- config_unsaved_state(): 'unsaved' is a content diff between the autosave
  working file and its [Meta] source profile — not merely "running from the
  autosave file" (the autosave pointer persists across reboots, which used to
  make every boot read as 'unsaved changes').
- min_detection_area migration: min_detection_area_percent is the canonical
  min weed size key; the legacy px key is accepted on read, converted at load/
  apply, and stripped from files on save.
- Wiring pins for the JS/route changes (no JS unit harness — source-level).
"""

import os
import re
from pathlib import Path

import pytest

from utils.config_manager import (
    AUTOSAVE_CONFIG, config_unsaved_state, strip_legacy_min_area,
    _UNSAVED_STATE_CACHE,
)

PROJECT_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE_INI = """\
[System]
algorithm = exhsv
relay_num = 4

[GreenOnBrown]
exg_min = 25
exg_max = 200
min_detection_area_percent = 0.0075
invert_hue = False
"""


def _write(path, text):
    path.write_text(text)
    return str(path)


def _autosave(tmp_path, body, source='profile.ini'):
    text = body + f"\n[Meta]\nsource = {source}\n" if source else body
    return _write(tmp_path / AUTOSAVE_CONFIG, text)


@pytest.fixture(autouse=True)
def _clear_unsaved_cache():
    _UNSAVED_STATE_CACHE.clear()
    yield
    _UNSAVED_STATE_CACHE.clear()


# ---------------------------------------------------------------------------
# config_unsaved_state
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestConfigUnsavedState:
    def test_named_file_is_never_unsaved(self, tmp_path):
        path = _write(tmp_path / 'profile.ini', BASE_INI)
        assert config_unsaved_state(path) == (False, '')

    def test_boot_clean_regression(self, tmp_path):
        """The core bug: an autosave file identical to its source profile must
        NOT read as unsaved (the active pointer staying on the autosave file
        across reboots used to flag 'unsaved changes' forever)."""
        _write(tmp_path / 'profile.ini', BASE_INI)
        auto = _autosave(tmp_path, BASE_INI)
        unsaved, source = config_unsaved_state(auto)
        assert unsaved is False
        assert source == 'profile.ini'

    def test_real_diff_is_unsaved(self, tmp_path):
        _write(tmp_path / 'profile.ini', BASE_INI)
        auto = _autosave(tmp_path, BASE_INI.replace('exg_min = 25', 'exg_min = 30'))
        unsaved, source = config_unsaved_state(auto)
        assert unsaved is True
        assert source == 'profile.ini'

    def test_numeric_and_boolean_normalization(self, tmp_path):
        """Formatting noise must not read as a diff: '0' == '0.0',
        'True' == 'true', trailing whitespace ignored."""
        _write(tmp_path / 'profile.ini', BASE_INI)
        noisy = (BASE_INI
                 .replace('exg_min = 25', 'exg_min = 25.0')
                 .replace('invert_hue = False', 'invert_hue = false'))
        auto = _autosave(tmp_path, noisy)
        assert config_unsaved_state(auto) == (False, 'profile.ini')

    def test_geometry_and_legacy_px_keys_excluded(self, tmp_path):
        """Geometry lives in GEOMETRY.ini and the legacy px min-area key is
        stripped on save — neither may create a phantom diff."""
        _write(tmp_path / 'profile.ini',
               BASE_INI + 'min_detection_area = 10\n\n[Camera]\ncrop_left = 0.1\n')
        auto = _autosave(tmp_path, BASE_INI + '\n[Camera]\ncrop_left = 0.3\n')
        assert config_unsaved_state(auto) == (False, 'profile.ini')

    def test_missing_source_meta_is_unsaved(self, tmp_path):
        auto = _autosave(tmp_path, BASE_INI, source=None)
        unsaved, _ = config_unsaved_state(auto)
        assert unsaved is True

    def test_missing_source_file_is_unsaved(self, tmp_path):
        auto = _autosave(tmp_path, BASE_INI, source='deleted.ini')
        assert config_unsaved_state(auto) == (True, 'deleted.ini')

    def test_cache_invalidates_on_change(self, tmp_path):
        src = _write(tmp_path / 'profile.ini', BASE_INI)
        auto = _autosave(tmp_path, BASE_INI)
        assert config_unsaved_state(auto)[0] is False

        # Edit the autosave (bump mtime past filesystem granularity)
        Path(auto).write_text(
            BASE_INI.replace('exg_min = 25', 'exg_min = 99')
            + '\n[Meta]\nsource = profile.ini\n')
        stat = os.stat(auto)
        os.utime(auto, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        assert config_unsaved_state(auto)[0] is True

        # And back in sync via the source side
        Path(src).write_text(BASE_INI.replace('exg_min = 25', 'exg_min = 99'))
        stat = os.stat(src)
        os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        assert config_unsaved_state(auto)[0] is False


# ---------------------------------------------------------------------------
# Legacy px min-area migration
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestStripLegacyMinArea:
    def test_px_removed_when_percent_present(self):
        out = strip_legacy_min_area({
            'GreenOnBrown': {'min_detection_area': '10',
                             'min_detection_area_percent': '0.0075',
                             'exg_min': '25'},
        })
        assert 'min_detection_area' not in out['GreenOnBrown']
        assert out['GreenOnBrown']['min_detection_area_percent'] == '0.0075'
        assert out['GreenOnBrown']['exg_min'] == '25'

    def test_px_kept_when_percent_missing_or_zero(self):
        legacy = {'GreenOnBrown': {'min_detection_area': '10'}}
        assert strip_legacy_min_area(legacy)['GreenOnBrown']['min_detection_area'] == '10'
        zero = {'GreenOnBrown': {'min_detection_area': '10',
                                 'min_detection_area_percent': '0'}}
        assert strip_legacy_min_area(zero)['GreenOnBrown']['min_detection_area'] == '10'

    def test_sensitivity_sections_strip_per_section(self):
        out = strip_legacy_min_area({
            'Sensitivity_Low': {'min_detection_area': '20',
                                'min_detection_area_percent': '0.015'},
            'Sensitivity_Old': {'min_detection_area': '5'},
        })
        assert 'min_detection_area' not in out['Sensitivity_Low']
        assert out['Sensitivity_Old']['min_detection_area'] == '5'


@pytest.mark.unit
class TestMinAreaMigrationWiring:
    """Source-level pins for the migration paths (owl.py cannot be imported on
    Windows — cv2/GPIO)."""

    def test_owl_boot_migration(self):
        src = (PROJECT_ROOT / 'owl.py').read_text(encoding='utf-8')
        block = src[src.index("min_detection_area_percent <= 0"):]
        assert 'resolution_width' in block[:600], (
            "owl.py must derive min_detection_area_percent from the legacy px "
            "value and the camera resolution when the percent key is unset"
        )
        # px read must not crash on percent-only configs
        assert re.search(
            r"getint\(\s*'GreenOnBrown',\s*'min_detection_area',\s*fallback=", src), (
            "owl.py must read min_detection_area with a fallback — percent-only "
            "configs carry no px key"
        )

    def test_owl_write_path_strips_legacy_px(self):
        src = (PROJECT_ROOT / 'utils' / 'mqtt_manager.py').read_text(encoding='utf-8')
        fn = src[src.index('def _write_config_without_geometry'):]
        fn = fn[:fn.index('\n    def ')]
        assert 'min_detection_area' in fn, (
            "_write_config_without_geometry must drop the legacy px key when "
            "the section carries a real percent value"
        )

    def test_live_px_set_converts_to_percent(self):
        src = (PROJECT_ROOT / 'utils' / 'mqtt_manager.py').read_text(encoding='utf-8')
        fn = src[src.index('def _update_greenonbrown_param'):]
        fn = fn[:fn.index('\n    def _update_greenongreen_param')]
        assert '_convert_px_min_area_to_percent' in fn, (
            "a live min_detection_area (px) set must convert to the canonical "
            "percent key or the change is shadowed in the detection loop"
        )

    def test_networked_param_route_persists(self):
        """/api/config/param used to dispatch single-key 'set_config', which
        applies live but never persists — widget slider changes were lost on
        reboot. It must dispatch set_config_section (auto-persist)."""
        src = (PROJECT_ROOT / 'controller' / 'networked' / 'networked.py'
               ).read_text(encoding='utf-8')
        fn = src[src.index('def set_config_param'):]
        fn = fn[:fn.index('\ndef ')]
        assert "'set_config_section'" in fn
        assert "'set_config'," not in fn, (
            "/api/config/param must not use the non-persisting single-key "
            "set_config action"
        )

    def test_shipped_default_config_is_percent_only(self):
        text = (PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini').read_text(encoding='utf-8')
        assert 'min_detection_area_percent' in text
        assert not re.search(r'^min_detection_area\s*=', text, re.MULTILINE), (
            "GENERAL_CONFIG.ini must not ship the legacy px key"
        )


# ---------------------------------------------------------------------------
# Save dialog + overlay + keyboard wiring pins
# ---------------------------------------------------------------------------

NETWORKED_TAB = PROJECT_ROOT / 'controller' / 'networked' / 'static' / 'js' / 'modules' / '_config_tab.js'


@pytest.mark.unit
class TestSaveDialogWiring:
    def test_update_mode_keys_off_loaded_profile(self):
        src = NETWORKED_TAB.read_text(encoding='utf-8')
        assert 'function getLoadedProfileFilename()' in src
        open_fn = src[src.index('function openSaveModal'):]
        assert 'getLoadedProfileFilename()' in open_fn[:open_fn.index('\nfunction ')], (
            "openSaveModal must offer 'Update <profile>' for the loaded/active "
            "profile, not only the library dropdown selection"
        )

    def test_save_as_new_uses_clean_names_with_collision_confirm(self):
        src = NETWORKED_TAB.read_text(encoding='utf-8')
        assert 'function sanitizeConfigFilename' in src
        save_fn = src[src.index('async function confirmSaveToAll'):]
        before_post = save_fn[:save_fn.index('/api/config/library')]
        assert 'sanitizeConfigFilename' in before_post, (
            "save-as-new must write a clean '<name>.ini' (no timestamp)"
        )
        assert 'showSaveCollisionWarning' in before_post, (
            "a name collision must require a second confirming tap"
        )

    def test_load_and_save_track_loaded_profile(self):
        src = NETWORKED_TAB.read_text(encoding='utf-8')
        assert src.count('loadedProfileFilename =') >= 3, (
            "loadedProfileFilename must be set on load/save and cleared on reset"
        )


@pytest.mark.unit
class TestMinSizeOverlayWiring:
    def test_overlay_fn_exists_and_hooks_interactions(self):
        src = NETWORKED_TAB.read_text(encoding='utf-8')
        assert 'function minSizeOverlayPing' in src
        # sqrt of the area fraction — the box represents % of frame AREA
        fn = src[src.index('function minSizeOverlayPing'):]
        fn = fn[:fn.index('\nfunction ') if '\nfunction ' in fn else len(fn)]
        assert 'Math.sqrt' in fn
        for host in ('function onKnobDrag', 'function adjustParameter'):
            body = src[src.index(host):]
            body = body[:body.index('\nfunction ')]
            assert 'minSizeOverlayPing(' in body, f"{host} must ping the overlay"

    def test_overlay_css_present(self):
        css = (PROJECT_ROOT / 'controller' / 'networked' / 'static' / 'css' /
               'components' / '_config_tab.css').read_text(encoding='utf-8')
        assert '.minsize-overlay-box' in css
        assert 'dotted #f59e0b' in css, "the reference box must be orange dotted"


@pytest.mark.unit
class TestKeyboardSizing:
    """Kiosk keyboard must stay glove-sized (farmer-first: no small buttons)."""

    CSS = PROJECT_ROOT / 'controller' / 'shared' / 'css' / 'numpad.css'

    def _block(self, css, selector):
        m = re.search(re.escape(selector) + r'\s*\{([^}]*)\}', css)
        assert m, f"{selector} missing from numpad.css"
        return m.group(1)

    def test_key_heights(self):
        css = self.CSS.read_text(encoding='utf-8')
        kb = re.search(r'height:\s*(\d+)px', self._block(css, '.kb-btn'))
        assert kb and int(kb.group(1)) >= 64, "QWERTY keys must be >= 64px tall"
        np = re.search(r'height:\s*(\d+)px', self._block(css, '.numpad-btn'))
        assert np and int(np.group(1)) >= 72, "numpad keys must be >= 72px tall"

    def test_keyboard_panel_near_full_width(self):
        css = self.CSS.read_text(encoding='utf-8')
        panel = self._block(css, '.numpad-panel--keyboard')
        assert re.search(r'width:\s*min\(9\dvw', panel), (
            "the QWERTY panel must span nearly the full kiosk width"
        )

    def test_bottom_sheet_keeps_dim_backdrop(self):
        css = self.CSS.read_text(encoding='utf-8')
        overlay = self._block(css, '.numpad-overlay')
        assert 'flex-end' in overlay, "panel must anchor to the bottom"
        assert 'rgba(0, 0, 0' in overlay, "the dimmed backdrop must stay"
