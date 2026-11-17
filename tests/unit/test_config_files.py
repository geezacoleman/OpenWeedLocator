"""The shipped config files against the code that reads them.

Three kinds of test, in this order:
  1. the shipped files and bad values through the real ConfigValidator;
  2. real functions (autosave seeding, live camera value coercion);
  3. contract checks between files and, last, source pins on dashboard
     JavaScript. Those read files rather than run code and say so.

What the OWL does with a config at boot (resolution clamp, minimum weed
size) is tested on a real Owl:
tests/owl/test_min_weed_size_and_resolution_at_the_relay.py
"""

import configparser
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / 'config'

CONTROLLER_INI_SECTIONS = ['MQTT', 'WebDashboard', 'Network', 'GPS']


def shipped():
    config = configparser.ConfigParser()
    config.read(CONFIG_DIR / 'GENERAL_CONFIG.ini')
    return config


def profile(tmp_path, changes=(), drop=()):
    """A copy of the shipped config with changes, as a file to load."""
    config = shipped()
    for section, key, value in changes:
        config.set(section, key, value)
    for section, key in drop:
        config.remove_option(section, key)
    path = tmp_path / 'profile.ini'
    with open(path, 'w') as f:
        config.write(f)
    return path


@pytest.mark.unit
class TestShippedConfig:
    def test_the_shipped_config_passes_validation(self, tmp_path):
        """Every section and key the OWL requires is there and in range. A
        key added to the validator but not to the file fails here."""
        from utils.config_manager import ConfigValidator
        loaded = ConfigValidator.load_and_validate_config(profile(tmp_path))
        assert loaded.sections() == shipped().sections()

    def test_a_missing_required_section_is_refused(self, tmp_path):
        from utils.config_manager import ConfigValidator
        config = shipped()
        config.remove_section('Relays')
        path = tmp_path / 'profile.ini'
        with open(path, 'w') as f:
            config.write(f)
        with pytest.raises(Exception, match='Relays'):
            ConfigValidator.load_and_validate_config(path)

    def test_detection_profile_and_controller_file_do_not_overlap(self):
        """MQTT, WebDashboard, Network, GPS (and Actuation) belong to
        CONTROLLER.ini, which owl.py reads on top of the profile. A profile
        carrying its own copy would be silently overridden, and loading it
        on another OWL would carry one unit's broker address to the next."""
        from utils.config_manager import ConfigValidator
        assert set(CONTROLLER_INI_SECTIONS) <= ConfigValidator.CONTROLLER_INI_SECTIONS
        assert not ConfigValidator.CONTROLLER_INI_SECTIONS & set(shipped().sections())

        template = configparser.ConfigParser()
        template.read(CONFIG_DIR / 'CONTROLLER_TEMPLATE.ini', encoding='utf-8')
        assert set(CONTROLLER_INI_SECTIONS) <= set(template.sections())

    @pytest.mark.xfail(strict=True, reason=(
        "ConfigValidator.CONTROLLER_INI_SECTIONS (utils/config_manager.py:363) lists "
        "'Actuation', which config/CONTROLLER_TEMPLATE.ini does not have, so "
        "validate_controller_ini (utils/config_manager.py:1122) warns 'CONTROLLER.ini missing "
        "sections: Actuation' at every boot of a unit set up from the template."))
    def test_a_controller_file_made_from_the_template_raises_no_warning(self, tmp_path, caplog):
        import logging
        import shutil
        from utils.config_manager import ConfigValidator
        shutil.copy(CONFIG_DIR / 'CONTROLLER_TEMPLATE.ini', tmp_path / 'CONTROLLER.ini')
        with caplog.at_level(logging.WARNING):
            ConfigValidator.validate_controller_ini(profile(tmp_path))
        assert [r.getMessage() for r in caplog.records] == []

    def test_shipped_defaults_that_are_safety_decisions(self):
        config = shipped()
        # Pi 3/4 are only run above 832x640 when the operator opts in
        assert config.get('Camera', 'allow_high_resolution') == 'False'
        # v3.14: a daylight lock renders Arducam IMX296 modules red
        assert config.get('Camera', 'awb_mode') == 'auto'
        # Tracking changes which boxes reach the relays: off unless chosen
        assert config.get('Tracking', 'tracking_enabled') == 'False'
        # The minimum weed size ships as a percent; the px key is legacy
        assert config.getfloat('GreenOnBrown', 'min_detection_area_percent') > 0
        assert not config.has_option('GreenOnBrown', 'min_detection_area')

    def test_code_defaults_for_white_balance_are_the_shipped_ones(self):
        """A profile saved before the keys existed gets these; they must
        behave like a fresh GENERAL_CONFIG."""
        from utils.config_manager import CAMERA_WB_DEFAULTS
        config = shipped()
        assert {key: config.get('Camera', key) for key in CAMERA_WB_DEFAULTS} == \
            {key: str(value) for key, value in CAMERA_WB_DEFAULTS.items()}


@pytest.mark.unit
class TestValidatorRefusesBadValues:
    """A value outside its range stops the OWL at boot with the key named,
    rather than running with it."""

    @pytest.mark.parametrize('section, key, value', [
        ('System', 'algorithm', 'colour'),
        ('GreenOnGreen', 'actuation_mode', 'lane'),
        ('GreenOnGreen', 'inference_resolution', '159'),
        ('GreenOnGreen', 'inference_resolution', '1281'),
        ('GreenOnGreen', 'crop_buffer_px', '-1'),
        ('GreenOnGreen', 'crop_buffer_px', '51'),
        ('GreenOnBrown', 'min_detection_area_percent', '5.1'),
        ('Tracking', 'track_high_thresh', '0'),
        ('Tracking', 'match_thresh', '1.5'),
        ('Tracking', 'track_buffer', '9'),
        ('Tracking', 'track_buffer', '151'),
        ('Camera', 'awb_mode', 'sunset'),
        # libcamera reads ColourGains (0, 0) as "let AWB choose", which
        # silently defeats manual white balance
        ('Camera', 'awb_red_gain', '0'),
        ('Camera', 'awb_blue_gain', '8.1'),
    ])
    def test_refused(self, tmp_path, section, key, value):
        from utils import error_manager as errors
        from utils.config_manager import ConfigValidator
        with pytest.raises(errors.ConfigValueError, match=rf'\[{section}\] {key}'):
            ConfigValidator.load_and_validate_config(profile(tmp_path, [(section, key, value)]))

    @pytest.mark.parametrize('section, key, value', [
        ('System', 'algorithm', 'gog-hybrid'),
        ('System', 'algorithm', 'lut'),
        ('GreenOnGreen', 'actuation_mode', 'zone'),
        ('GreenOnGreen', 'inference_resolution', '160'),
        ('GreenOnGreen', 'inference_resolution', '1280'),
        ('GreenOnGreen', 'crop_buffer_px', '0'),
        ('GreenOnGreen', 'crop_buffer_px', '50'),
        ('Tracking', 'track_buffer', '10'),
        ('Tracking', 'track_buffer', '150'),
        ('Camera', 'awb_mode', 'manual'),
        ('Camera', 'awb_red_gain', '0.1'),
        ('Camera', 'awb_blue_gain', '8.0'),
        ('Camera', 'allow_high_resolution', 'True'),
    ])
    def test_accepted_at_the_edge_of_the_range(self, tmp_path, section, key, value):
        from utils.config_manager import ConfigValidator
        loaded = ConfigValidator.load_and_validate_config(profile(tmp_path, [(section, key, value)]))
        assert loaded.get(section, key) == value


@pytest.mark.unit
class TestCameraSettings:
    def test_seed_autosave_injects_missing_wb_keys(self, tmp_path):
        """Profiles saved before v3.11 have no WB keys; the autosave working
        copy must carry them so the kiosk editor renders the WB fields."""
        from utils.config_manager import seed_autosave, AUTOSAVE_CONFIG
        src = tmp_path / 'old_profile.ini'
        src.write_text("[Camera]\nresolution_width = 416\nresolution_height = 320\n")
        seed_autosave(tmp_path, src)
        cfg = configparser.ConfigParser()
        cfg.read(tmp_path / AUTOSAVE_CONFIG)
        assert dict(cfg['Camera']) == {
            'resolution_width': '416', 'resolution_height': '320',
            'awb_mode': 'auto', 'awb_red_gain': '2.0', 'awb_blue_gain': '2.0'}

    def test_seed_autosave_keeps_existing_wb_keys(self, tmp_path):
        from utils.config_manager import seed_autosave, AUTOSAVE_CONFIG
        src = tmp_path / 'tuned.ini'
        src.write_text("[Camera]\nawb_mode = manual\nawb_red_gain = 1.4\n"
                       "awb_blue_gain = 3.1\n")
        seed_autosave(tmp_path, src)
        cfg = configparser.ConfigParser()
        cfg.read(tmp_path / AUTOSAVE_CONFIG)
        assert dict(cfg['Camera']) == {
            'awb_mode': 'manual', 'awb_red_gain': '1.4', 'awb_blue_gain': '3.1'}

    def test_coerce_camera_live_value_single_source(self):
        """One rule for the OWL's live-apply branch AND the dashboard's
        persist route: clamp gains/exposure to VALUE_VALIDATORS, normalise
        the mode, reject garbage, KeyError for non-live keys."""
        from utils.config_manager import ConfigValidator as CV
        assert CV.coerce_camera_live_value('awb_red_gain', '50') == 8.0
        assert CV.coerce_camera_live_value('awb_blue_gain', 0) == 0.1
        assert CV.coerce_camera_live_value('awb_red_gain', '1.75') == 1.75
        assert CV.coerce_camera_live_value('exp_compensation', '3.7') == 3
        assert CV.coerce_camera_live_value('exp_compensation', -99) == -10
        assert CV.coerce_camera_live_value('awb_mode', ' Manual ') == 'manual'
        with pytest.raises(ValueError):
            CV.coerce_camera_live_value('awb_mode', 'sunset')
        with pytest.raises(ValueError):
            CV.coerce_camera_live_value('awb_red_gain', 'abc')
        with pytest.raises(KeyError):
            CV.coerce_camera_live_value('crop_left', '0.02')

    def test_every_live_camera_key_can_be_coerced(self):
        """A key in CAMERA_LIVE_KEYS without a coercion rule raises KeyError
        inside the MQTT handler and the change is dropped."""
        from utils.config_manager import ConfigValidator as CV
        from utils.mqtt_manager import CAMERA_LIVE_KEYS
        samples = {'awb_mode': 'auto', 'awb_red_gain': '2.0', 'awb_blue_gain': '2.0',
                   'exp_compensation': '0'}
        assert set(samples) == CAMERA_LIVE_KEYS
        for key, value in samples.items():
            CV.coerce_camera_live_value(key, value)


@pytest.mark.unit
class TestFrontendBackendConfigSync:
    """Contract checks between files (they read config.js, they do not run
    it): a config key the OWL reads but the editor has no field definition
    for is rendered as a free-text box, which is how actuation_zone once
    accepted letters."""

    @staticmethod
    def _find_matching_brace(text, start):
        """Find the position of the closing brace matching the opening at start."""
        depth = 0
        for i in range(start, len(text)):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    return i
        return len(text) - 1

    @classmethod
    def _parse_config_field_defs(cls):
        """Parse CONFIG_FIELD_DEFS from config.js into a Python dict.

        Uses brace-counting to extract sections and field keys reliably,
        without trying to parse JS as JSON.

        Returns dict: {section_name: set of key names}
        """
        config_js = PROJECT_ROOT / 'controller' / 'shared' / 'js' / 'config.js'
        assert config_js.exists(), "controller/shared/js/config.js not found"
        source = config_js.read_text()

        # Find the CONFIG_FIELD_DEFS block
        match = re.search(r'const CONFIG_FIELD_DEFS\s*=\s*\{', source)
        assert match, "CONFIG_FIELD_DEFS not found in config.js"

        obj_start = match.end() - 1  # position of opening {
        obj_end = cls._find_matching_brace(source, obj_start)
        obj_text = source[obj_start:obj_end + 1]

        # Find each section: 'SectionName': { ... }
        result = {}
        section_re = re.compile(r"'([A-Za-z]\w*)'\s*:\s*\{")

        for sm in section_re.finditer(obj_text):
            section_name = sm.group(1)
            section_brace = sm.end() - 1
            section_end = cls._find_matching_brace(obj_text, section_brace)
            section_body = obj_text[section_brace:section_end + 1]

            keys = set()

            # Find field keys: 'key_name': { ... }
            field_re = re.compile(r"'([\w_]+)'\s*:\s*\{")
            for fm in field_re.finditer(section_body):
                key_name = fm.group(1)
                if key_name.startswith('_'):
                    # Virtual field — extract real keys from keys: { ... }
                    field_start = fm.end() - 1
                    field_end = cls._find_matching_brace(section_body, field_start)
                    field_body = section_body[field_start:field_end + 1]
                    keys_match = re.search(
                        r"keys:\s*\{[^}]*width:\s*'(\w+)'[^}]*height:\s*'(\w+)'",
                        field_body
                    )
                    if keys_match:
                        keys.add(keys_match.group(1))
                        keys.add(keys_match.group(2))
                else:
                    keys.add(key_name)

            # Check for _isRelaySection marker
            if '_isRelaySection' in section_body:
                keys.add('_isRelaySection')

            result[section_name] = keys

        return result

    def test_every_ini_key_has_frontend_field_def(self):
        """Every config key in GENERAL_CONFIG.ini should have a matching
        field definition in CONFIG_FIELD_DEFS (or be in a Sensitivity/Relay section).

        This test would have caught the actuation_zone bug where a number field
        was rendered as a text input because it had no field definition.
        """
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / 'GENERAL_CONFIG.ini')

        frontend_defs = self._parse_config_field_defs()

        # Sections that are handled specially (not regular field defs)
        skip_sections = {'Relays', 'Sensitivity', 'Sensitivity_Low',
                         'Sensitivity_Medium', 'Sensitivity_High'}

        missing = []
        for section in config.sections():
            if section in skip_sections:
                continue
            if section not in frontend_defs:
                # Entire section missing from frontend — could be intentional
                # for infrastructure sections loaded from CONTROLLER.ini
                continue
            for key in config.options(section):
                if key not in frontend_defs[section]:
                    missing.append(f"{section}.{key}")

        assert not missing, (
            f"Config keys without frontend field definitions (will render as "
            f"text input instead of correct type): {missing}"
        )

    def test_cloud_section_three_way_parity(self):
        """[Cloud] keys must agree across the CONTROLLER template, the
        ConfigValidator registration, and the frontend field defs —
        a key missing from any layer is a silent integration failure."""
        from utils.config_manager import ConfigValidator

        template = CONFIG_DIR / 'CONTROLLER_TEMPLATE.ini'
        config = configparser.ConfigParser()
        config.read(template, encoding='utf-8')
        assert config.has_section('Cloud'), "CONTROLLER_TEMPLATE.ini missing [Cloud]"
        template_keys = set(config.options('Cloud'))

        validator_keys = ConfigValidator.OPTIONAL_SECTIONS['Cloud']['optional_keys']
        frontend_keys = self._parse_config_field_defs().get('Cloud', set())

        assert template_keys == validator_keys, (
            f"template vs ConfigValidator mismatch: "
            f"only in template: {template_keys - validator_keys}, "
            f"only in validator: {validator_keys - template_keys}"
        )
        assert template_keys <= frontend_keys, (
            f"[Cloud] keys missing frontend field defs: {template_keys - frontend_keys}"
        )

    @pytest.mark.xfail(strict=True, reason=(
        "[WebDashboard] ui (config/CONTROLLER_TEMPLATE.ini:58, values full|collect) has no "
        "entry in CONFIG_FIELD_DEFS (controller/shared/js/config.js:135), so the config "
        "editor shows it as a free-text box. Not seen before: the old test read the "
        "developer's CONTROLLER.ini and skipped in CI."))
    def test_controller_template_keys_have_frontend_field_defs(self):
        """CONTROLLER.ini is created from the template on the unit (CI has
        none, and a test never reads the developer's own)."""
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / 'CONTROLLER_TEMPLATE.ini', encoding='utf-8')
        frontend_defs = self._parse_config_field_defs()

        covered = [s for s in config.sections() if s in frontend_defs]
        assert set(CONTROLLER_INI_SECTIONS) <= set(covered)
        missing = [f'{section}.{key}' for section in covered
                   for key in config.options(section) if key not in frontend_defs[section]]
        assert not missing, f'CONTROLLER keys the editor would show as free text: {missing}'

    def test_backend_sections_have_frontend_coverage(self):
        """Every section in ConfigValidator.REQUIRED_CONFIG should have a
        corresponding section in CONFIG_FIELD_DEFS."""
        from utils.config_manager import ConfigValidator

        frontend_defs = self._parse_config_field_defs()

        missing_sections = []
        for section in ConfigValidator.REQUIRED_CONFIG:
            if section == 'Relays':
                continue  # Handled by _isRelaySection
            if section not in frontend_defs:
                missing_sections.append(section)

        assert not missing_sections, (
            f"Backend config sections with no frontend field definitions: "
            f"{missing_sections}"
        )

    def test_sensitivity_keys_match_between_manager_and_validator(self):
        """SensitivityManager.SENSITIVITY_KEYS must match
        ConfigValidator.SENSITIVITY_SECTION_KEYS."""
        from utils.sensitivity_manager import SensitivityManager
        from utils.config_manager import ConfigValidator

        manager_keys = SensitivityManager.SENSITIVITY_KEYS
        validator_keys = ConfigValidator.SENSITIVITY_SECTION_KEYS

        assert manager_keys == validator_keys, (
            f"Key mismatch between SensitivityManager and ConfigValidator.\n"
            f"Manager only: {manager_keys - validator_keys}\n"
            f"Validator only: {validator_keys - manager_keys}"
        )


@pytest.mark.unit
class TestModeChipGating:
    """SOURCE PIN, not behaviour (no JavaScript harness).

    updateModeAvailability must gate EVERY copy of the mode selector —
    the networked config tab duplicates the chip row, and querySelector
    (first match) left those copies enabled when no AI model was present."""

    @pytest.mark.parametrize('controller_dir', ['networked', 'standalone'])
    def test_update_mode_availability_uses_query_selector_all(self, controller_dir):
        js = (PROJECT_ROOT / 'controller' / controller_dir / 'static' /
              'js' / 'modules' / '_controls.js').read_text(encoding='utf-8')
        body = js.split('function updateModeAvailability')[1].split('\nfunction ')[0]
        assert 'querySelectorAll' in body, (
            "updateModeAvailability must use querySelectorAll so duplicated "
            "mode selectors (config tab) get the disabled state too"
        )


@pytest.mark.unit
class TestSaveFoldsSliderValues:
    """SOURCE PINS, not behaviour: the dashboards' JavaScript has no test
    harness, so these only prove the calls are written in the right order.

    Guard the 2026-07-10 field-bug fix: saving a named config used to
    serialize the stale browser-side config object — slider moves apply live
    to the OWL but never touched that object, so saved files carried the old
    thresholds and Load restored them over the operator's adjustments.

    JS has no unit harness here, so pin the wiring at source level:
    - networked confirmSaveToAll() must fold sliders (syncConfigFromSliders)
      before POSTing the library copy
    - standalone saveConfig() must fold sliders (syncCurrentConfigFromSliders)
      before POSTing /api/config
    - every fold's GoB key list must match the apply path's list exactly
    """

    NETWORKED_TAB = PROJECT_ROOT / 'controller' / 'networked' / 'static' / 'js' / 'modules' / '_config_tab.js'
    STANDALONE_CFG = PROJECT_ROOT / 'controller' / 'standalone' / 'static' / 'js' / 'modules' / '_config.js'

    # min_detection_area_percent is the canonical min weed size key (the legacy
    # px key is read-only: migrated at load, stripped on save — never applied/folded)
    GOB_SLIDER_KEYS = {
        'exg_min', 'exg_max', 'hue_min', 'hue_max',
        'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
        'min_detection_area_percent',
    }

    def test_networked_save_folds_sliders_before_library_post(self):
        src = self.NETWORKED_TAB.read_text(encoding='utf-8')
        assert 'function syncConfigFromSliders()' in src, (
            "syncConfigFromSliders() missing from networked _config_tab.js"
        )
        save_fn = src[src.index('async function confirmSaveToAll'):]
        lib_post = save_fn.index('/api/config/library')
        assert 'syncConfigFromSliders()' in save_fn[:lib_post], (
            "confirmSaveToAll must fold slider values into deviceConfig BEFORE "
            "the /api/config/library POST, or the library copy is stale"
        )

    def test_networked_save_works_without_editor_loaded(self):
        """Field bug 2026-07-10 (round 2): deviceConfig is only populated when
        the Advanced editor loads, so saving straight from the sliders silently
        skipped the library file — no new profile in the list — and the OWLs
        diverted the null-filename save to their autosave working file
        ('<old profile> - unsaved changes'). confirmSaveToAll must fetch the
        running config from an OWL when deviceConfig is empty, and abort loudly
        rather than fire a null-filename device save."""
        src = self.NETWORKED_TAB.read_text(encoding='utf-8')
        save_fn = src[src.index('async function confirmSaveToAll'):]
        before_lib_post = save_fn[:save_fn.index('/api/config/library')]
        assert "apiRequest('/api/config/' + srcId" in before_lib_post, (
            "confirmSaveToAll must fetch the device config when deviceConfig is empty"
        )
        assert 'Cannot save profile' in before_lib_post, (
            "confirmSaveToAll must abort with a visible error when no config source exists"
        )
        # The device save must never run without a library filename (a null
        # filename makes the OWL divert to its autosave working file).
        before_device_save = save_fn[:save_fn.index("'/save'")]
        assert 'if (!savedFilename)' in before_device_save, (
            "confirmSaveToAll must abort before the device save when the library save failed"
        )

    def test_standalone_save_folds_sliders_before_post(self):
        src = self.STANDALONE_CFG.read_text(encoding='utf-8')
        assert 'function syncCurrentConfigFromSliders()' in src, (
            "syncCurrentConfigFromSliders() missing from standalone _config.js"
        )
        save_fn = src[src.index('async function saveConfig'):]
        post = save_fn.index("fetch('/api/config'")
        assert 'syncCurrentConfigFromSliders()' in save_fn[:post], (
            "saveConfig must fold slider values into currentConfig BEFORE "
            "the /api/config POST, or the saved file is stale"
        )

    def _gob_key_lists(self, source):
        """Extract every `gobKeys = [...]` array in a JS source as a set-list."""
        lists = []
        for m in re.finditer(r"gobKeys\s*=\s*\[([^\]]*)\]", source):
            lists.append(set(re.findall(r"'([\w_]+)'", m.group(1))))
        return lists

    def test_networked_fold_and_apply_key_lists_match(self):
        lists = self._gob_key_lists(self.NETWORKED_TAB.read_text(encoding='utf-8'))
        assert len(lists) >= 2, "expected gobKeys in sendAllToDevice and syncConfigFromSliders"
        for key_list in lists:
            assert key_list == self.GOB_SLIDER_KEYS, (
                f"networked gobKeys drifted: {sorted(key_list ^ self.GOB_SLIDER_KEYS)}"
            )

    def test_standalone_fold_key_list_matches(self):
        lists = self._gob_key_lists(self.STANDALONE_CFG.read_text(encoding='utf-8'))
        assert lists, "expected gobKeys in syncCurrentConfigFromSliders"
        for key_list in lists:
            assert key_list == self.GOB_SLIDER_KEYS, (
                f"standalone gobKeys drifted: {sorted(key_list ^ self.GOB_SLIDER_KEYS)}"
            )


