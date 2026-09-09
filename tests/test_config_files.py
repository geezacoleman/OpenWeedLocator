"""Tests for INI config file consistency, the Owl.config_path property,
and cross-validation between backend config definitions and frontend field definitions."""

import configparser
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / 'config'

INI_FILES = [
    'GENERAL_CONFIG.ini',
]

EXPECTED_SECTIONS = [
    'System', 'Controller', 'Visualisation', 'Camera', 'GreenOnGreen',
    'GreenOnBrown', 'DataCollection', 'Relays', 'Sensitivity',
    'Sensitivity_Low', 'Sensitivity_Medium', 'Sensitivity_High',
    'Tracking',
]

CONTROLLER_INI_SECTIONS = ['MQTT', 'WebDashboard', 'Network', 'GPS']


@pytest.mark.unit
class TestConfigFiles:
    """Validate that all INI config files are well-formed and consistent."""

    @pytest.mark.parametrize('ini_name', INI_FILES)
    def test_ini_parses_without_errors(self, ini_name):
        """Each INI file should parse cleanly with configparser."""
        path = CONFIG_DIR / ini_name
        assert path.exists(), f"{ini_name} not found in config/"

        config = configparser.ConfigParser()
        config.read(path)
        assert len(config.sections()) > 0, f"{ini_name} has no sections"

    def test_general_config_has_all_sections(self):
        """GENERAL_CONFIG.ini must have all expected sections."""
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / 'GENERAL_CONFIG.ini')
        actual = set(config.sections())
        expected = set(EXPECTED_SECTIONS)
        missing = expected - actual
        assert not missing, (
            f"GENERAL_CONFIG.ini missing sections: {missing}"
        )

    def test_all_configs_have_expected_sections(self):
        """All configs should have all 8 expected detection sections."""
        for ini_name in INI_FILES:
            config = configparser.ConfigParser()
            config.read(CONFIG_DIR / ini_name)
            for section in EXPECTED_SECTIONS:
                assert config.has_section(section), (
                    f"{ini_name} missing section [{section}]"
                )

    def test_controller_ini_has_infrastructure_sections(self):
        """CONTROLLER.ini should have the 4 infrastructure sections."""
        path = CONFIG_DIR / 'CONTROLLER.ini'
        if not path.exists():
            pytest.skip("CONTROLLER.ini not present (created by setup scripts on Pi)")

        config = configparser.ConfigParser()
        config.read(path)
        for section in CONTROLLER_INI_SECTIONS:
            assert config.has_section(section), (
                f"CONTROLLER.ini missing section [{section}]"
            )

    def test_general_config_does_not_have_infrastructure_sections(self):
        """GENERAL_CONFIG.ini should NOT have MQTT/WebDashboard/Network/GPS."""
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / 'GENERAL_CONFIG.ini')
        for section in CONTROLLER_INI_SECTIONS:
            assert not config.has_section(section), (
                f"GENERAL_CONFIG.ini should not have [{section}] (moved to CONTROLLER.ini)"
            )

    def test_general_config_has_sensitivity_section(self):
        """GENERAL_CONFIG.ini should have [Sensitivity] with active preset."""
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / 'GENERAL_CONFIG.ini')
        assert config.has_section('Sensitivity'), (
            "GENERAL_CONFIG.ini missing [Sensitivity] section"
        )
        assert config.has_option('Sensitivity', 'active'), (
            "GENERAL_CONFIG.ini missing Sensitivity.active"
        )


@pytest.mark.unit
class TestHybridConfigValidation:
    """Validate hybrid detection config values."""

    @pytest.mark.parametrize('ini_name', INI_FILES)
    def test_new_gog_keys_in_presets(self, ini_name):
        """inference_resolution and crop_buffer_px present in all presets."""
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / ini_name)

        assert config.has_option('GreenOnGreen', 'inference_resolution'), \
            f'{ini_name} missing inference_resolution'
        assert config.has_option('GreenOnGreen', 'crop_buffer_px'), \
            f'{ini_name} missing crop_buffer_px'

        # Verify values are sensible
        res = config.getint('GreenOnGreen', 'inference_resolution')
        assert 160 <= res <= 1280, f'{ini_name} inference_resolution out of range: {res}'

        buf = config.getint('GreenOnGreen', 'crop_buffer_px')
        assert 0 <= buf <= 50, f'{ini_name} crop_buffer_px out of range: {buf}'

    def test_gog_hybrid_valid_algorithm(self):
        """gog-hybrid is accepted by ConfigValidator."""
        from utils.config_manager import ConfigValidator
        assert 'gog-hybrid' in ConfigValidator.VALID_ALGORITHMS


@pytest.mark.unit
class TestOwlConfigPath:
    """Verify the Owl class exposes config_path as a public property (BUG 1 fix)."""

    def test_owl_class_has_config_path_property(self):
        """Owl class should have a @property config_path in the source code.

        We use AST inspection because owl.py imports hardware-specific modules
        (picamera2, GPIO) that aren't available on Windows dev machines.
        """
        import ast

        owl_source = (PROJECT_ROOT / 'owl.py').read_text()
        tree = ast.parse(owl_source)

        # Find the Owl class
        owl_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == 'Owl':
                owl_class = node
                break
        assert owl_class is not None, "Could not find class Owl in owl.py"

        # Find a method named config_path decorated with @property
        found_property = False
        for item in owl_class.body:
            if isinstance(item, ast.FunctionDef) and item.name == 'config_path':
                for dec in item.decorator_list:
                    if isinstance(dec, ast.Name) and dec.id == 'property':
                        found_property = True
                        break
                break

        assert found_property, (
            "Owl class is missing '@property config_path' -- "
            "MQTT get_config will fail (BUG 1)"
        )


@pytest.mark.unit
class TestOwlFrameCountMonotonic:
    """Verify frame_count in owl.py is monotonic (no wrap-around)."""

    def test_no_frame_count_wrap(self):
        """frame_count must not wrap — wrapping breaks ClassSmoother stale pruning.

        Previous code had: frame_count = frame_count + 1 if frame_count < 900 else 1
        This caused stale tracks to leak memory. Python ints don't overflow.
        """
        owl_source = (PROJECT_ROOT / 'owl.py').read_text()
        assert 'frame_count < 900' not in owl_source, (
            "frame_count wraps at 900 — this breaks ClassSmoother stale pruning "
            "and causes memory leaks. Use monotonic frame_count += 1 instead."
        )


@pytest.mark.unit
class TestNetworkedTrackingOptimisticState:
    """Verify networked controller has optimistic state variable for tracking."""

    def test_global_tracking_enabled_exists(self):
        """Networked _core.js must declare globalTrackingEnabled
        to prevent button snap-back during 2s polling cycle."""
        core_js = (PROJECT_ROOT / 'controller' / 'networked' / 'static' /
                   'js' / 'modules' / '_core.js').read_text()
        assert 'globalTrackingEnabled' in core_js, (
            "globalTrackingEnabled must be declared in networked _core.js — "
            "without it, the tracking button flickers on 2s poll"
        )

    def test_toggle_tracking_sets_optimistic_state(self):
        """toggleTracking() must set globalTrackingEnabled before sending command."""
        controls_js = (PROJECT_ROOT / 'controller' / 'networked' / 'static' /
                       'js' / 'modules' / '_controls.js').read_text()
        assert 'globalTrackingEnabled = true' in controls_js
        assert 'globalTrackingEnabled = false' in controls_js


@pytest.mark.unit
class TestModeChipGating:
    """updateModeAvailability must gate EVERY copy of the mode selector —
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

    @pytest.mark.parametrize('controller_dir', ['networked', 'standalone'])
    def test_painted_chip_hint_exists(self, controller_dir):
        js = (PROJECT_ROOT / 'controller' / controller_dir / 'static' /
              'js' / 'modules' / '_controls.js').read_text(encoding='utf-8')
        assert 'function updatePaintedChipHint' in js


@pytest.mark.unit
class TestZoneTrackingWarning:
    """Verify owl.py logs a warning when zone actuation and tracking are both active."""

    def test_zone_tracking_warning_exists(self):
        """owl.py must warn when zone actuation is silently disabled by tracking."""
        owl_source = (PROJECT_ROOT / 'owl.py').read_text()
        assert 'Zone actuation disabled while tracking' in owl_source, (
            "owl.py must warn when tracking disables zone actuation — "
            "otherwise farmers won't know zone mode is being ignored"
        )


@pytest.mark.unit
class TestFrontendBackendConfigSync:
    """Cross-validate backend ConfigValidator definitions against frontend
    CONFIG_FIELD_DEFS in shared/js/config.js.

    These tests catch integration bugs where:
    - A config key exists in the INI file but has no frontend field definition
      (would render as text input instead of correct type)
    - A CSS selector in JS doesn't match actual DOM class names
    - Backend value constraints don't match frontend constraints
    """

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

    def test_controller_ini_keys_have_frontend_field_defs(self):
        """Every key in CONTROLLER.ini should also have a frontend definition."""
        controller_ini = CONFIG_DIR / 'CONTROLLER.ini'
        if not controller_ini.exists():
            pytest.skip("CONTROLLER.ini not found")

        config = configparser.ConfigParser()
        config.read(controller_ini)

        frontend_defs = self._parse_config_field_defs()

        missing = []
        for section in config.sections():
            if section not in frontend_defs:
                continue
            for key in config.options(section):
                if key not in frontend_defs[section]:
                    missing.append(f"{section}.{key}")

        assert not missing, (
            f"CONTROLLER.ini keys without frontend field definitions: {missing}"
        )

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

    def test_numpad_selector_matches_config_dom(self):
        """The numpad focusin selector must match the CSS class used by
        createConfigSection() for the section body.

        This test would have caught the .config-editor vs .config-section-body bug.
        """
        numpad_js = PROJECT_ROOT / 'controller' / 'shared' / 'js' / 'numpad.js'
        config_js = PROJECT_ROOT / 'controller' / 'shared' / 'js' / 'config.js'

        numpad_source = numpad_js.read_text()
        config_source = config_js.read_text()

        # Extract the CSS class the numpad looks for via el.closest()
        closest_match = re.search(r"el\.closest\(['\"]([^'\"]+)['\"]\)", numpad_source)
        assert closest_match, "Could not find el.closest() selector in numpad.js"
        numpad_selector = closest_match.group(1)

        # Strip leading dot from CSS selector to get the class name
        class_name = numpad_selector.lstrip('.')

        # The class name should appear as a className assignment in config.js
        # e.g. body.className = 'config-section-body'
        assert class_name in config_source, (
            f"Numpad selector class '{class_name}' (from '{numpad_selector}') "
            f"not found in config.js. The numpad will never open because the "
            f"DOM class doesn't match."
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

    def test_resolution_warning_js_loaded_in_both_templates(self):
        """resolution_warning.js must be included in both standalone and
        networked index.html templates for the recording resolution check."""
        templates = [
            PROJECT_ROOT / 'controller' / 'standalone' / 'templates' / 'index.html',
            PROJECT_ROOT / 'controller' / 'networked' / 'templates' / 'index.html',
        ]
        for template_path in templates:
            assert template_path.exists(), f"Template not found: {template_path}"
            source = template_path.read_text()
            assert 'resolution_warning.js' in source, (
                f"resolution_warning.js not included in {template_path.name}. "
                f"The recording resolution warning will not work."
            )

    def test_resolution_warning_js_exists(self):
        """The shared resolution_warning.js file must exist."""
        js_path = PROJECT_ROOT / 'controller' / 'shared' / 'js' / 'resolution_warning.js'
        assert js_path.exists(), (
            "controller/shared/js/resolution_warning.js not found"
        )


@pytest.mark.unit
class TestHighResolutionOverride:
    """Pi 3/4 silently clamp resolutions above 832x640 to 640x480 unless the
    user opts in via [Camera] allow_high_resolution. These tests guard the
    contract between the clamp, the config validator, the heartbeat field,
    and the warning modal."""

    def test_clamp_reads_allow_high_resolution_flag(self):
        """owl.py clamp must honour the allow_high_resolution config flag."""
        owl_source = (PROJECT_ROOT / 'owl.py').read_text()
        assert 'allow_high_resolution' in owl_source, (
            "owl.py must read [Camera] allow_high_resolution — otherwise the "
            "user has no way to opt out of the Pi 3/4 resolution clamp."
        )
        assert 'not allow_high_resolution' in owl_source, (
            "owl.py clamp condition must include 'not allow_high_resolution' "
            "so that True bypasses the clamp."
        )

    def test_general_config_has_allow_high_resolution(self):
        """GENERAL_CONFIG.ini must declare allow_high_resolution under [Camera]."""
        cfg = configparser.ConfigParser()
        cfg.read(PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini')
        assert cfg.has_option('Camera', 'allow_high_resolution'), (
            "GENERAL_CONFIG.ini [Camera] must include allow_high_resolution. "
            "Without it, the frontend cross-validation test will fail."
        )
        # Default must be safety-on (False)
        assert not cfg.getboolean('Camera', 'allow_high_resolution'), (
            "Default [Camera] allow_high_resolution must be False — the "
            "clamp is a safety default."
        )

    def test_validator_lists_allow_high_resolution(self):
        """ConfigValidator must list allow_high_resolution in [Camera] optional_keys
        so it doesn't emit a spurious 'unknown key' warning at startup."""
        from utils.config_manager import ConfigValidator
        camera = ConfigValidator.REQUIRED_CONFIG['Camera']
        assert 'allow_high_resolution' in camera['optional_keys'], (
            "allow_high_resolution missing from Camera optional_keys — "
            "ConfigValidator will warn at startup."
        )

    def test_heartbeat_publishes_rpi_version_and_flag(self):
        """OWLMQTTPublisher heartbeat must publish rpi_version and
        allow_high_resolution so the controllers can decide whether to show
        the warning modal."""
        mqtt_source = (PROJECT_ROOT / 'utils' / 'mqtt_manager.py').read_text()
        assert "'rpi_version'" in mqtt_source, (
            "rpi_version missing from OWLMQTTPublisher state dict — the "
            "warning modal cannot detect Pi 3/4 OWLs."
        )
        assert "'allow_high_resolution'" in mqtt_source, (
            "allow_high_resolution missing from heartbeat — the warning "
            "modal cannot detect when override is already active."
        )

    def test_heartbeat_publishes_requested_resolution_and_clamp_flag(self):
        """OWLMQTTPublisher must publish both the requested (config) resolution
        and a resolution_clamped flag. Without these, the dashboard sees only
        the post-clamp resolution and can't detect that a silent clamp happened."""
        mqtt_source = (PROJECT_ROOT / 'utils' / 'mqtt_manager.py').read_text()
        for field in ("'requested_resolution_width'", "'requested_resolution_height'", "'resolution_clamped'"):
            assert field in mqtt_source, (
                f"{field} missing from heartbeat — modal cannot detect silent clamps."
            )

    def test_owl_captures_requested_resolution(self):
        """owl.py must record self.requested_resolution + self.resolution_clamped
        so the heartbeat can report them."""
        owl_source = (PROJECT_ROOT / 'owl.py').read_text()
        assert 'self.requested_resolution' in owl_source, (
            "owl.py must capture requested_resolution before any clamp logic."
        )
        assert 'self.resolution_clamped' in owl_source, (
            "owl.py must set resolution_clamped to signal the dashboard."
        )

    def test_high_res_warning_js_exists(self):
        """The shared high_res_warning.js must exist."""
        js_path = PROJECT_ROOT / 'controller' / 'shared' / 'js' / 'high_res_warning.js'
        assert js_path.exists(), (
            "controller/shared/js/high_res_warning.js not found"
        )

    def test_high_res_warning_js_loaded_in_both_templates(self):
        """high_res_warning.js must be included in both controller templates."""
        templates = [
            PROJECT_ROOT / 'controller' / 'standalone' / 'templates' / 'index.html',
            PROJECT_ROOT / 'controller' / 'networked' / 'templates' / 'index.html',
        ]
        for template_path in templates:
            source = template_path.read_text()
            assert 'high_res_warning.js' in source, (
                f"high_res_warning.js not included in {template_path.name}. "
                f"The Pi 3/4 high-resolution warning will not appear."
            )


@pytest.mark.unit
class TestTrackingConfig:
    """Validate ByteTrack tracking parameters in config, validators, and frontend."""

    BYTETRACK_KEYS = [
        'track_high_thresh', 'track_low_thresh', 'new_track_thresh',
        'track_buffer', 'match_thresh',
    ]

    def test_general_config_has_bytetrack_params(self):
        """GENERAL_CONFIG.ini [Tracking] must have all ByteTrack params."""
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / 'GENERAL_CONFIG.ini')
        assert config.has_section('Tracking')
        for key in self.BYTETRACK_KEYS:
            assert config.has_option('Tracking', key), (
                f"Missing [Tracking].{key} in GENERAL_CONFIG.ini"
            )

    def test_bytetrack_param_values_are_valid(self):
        """Default ByteTrack values in GENERAL_CONFIG.ini must be within valid ranges."""
        config = configparser.ConfigParser()
        config.read(CONFIG_DIR / 'GENERAL_CONFIG.ini')

        # All threshold params should be 0.0-1.0
        for key in ['track_high_thresh', 'track_low_thresh', 'new_track_thresh', 'match_thresh']:
            val = config.getfloat('Tracking', key)
            assert 0.0 < val < 1.0, f"{key}={val} out of range (0, 1)"

        # track_buffer should be a positive integer
        buf = config.getint('Tracking', 'track_buffer')
        assert 10 <= buf <= 150, f"track_buffer={buf} out of range [10, 150]"

    def test_bytetrack_params_in_config_validator(self):
        """All ByteTrack params must be registered in ConfigValidator optional_keys."""
        from utils.config_manager import ConfigValidator
        tracking_opts = ConfigValidator.OPTIONAL_SECTIONS.get('Tracking', {})
        optional_keys = tracking_opts.get('optional_keys', set())
        for key in self.BYTETRACK_KEYS:
            assert key in optional_keys, (
                f"{key} missing from ConfigValidator.OPTIONAL_SECTIONS['Tracking']['optional_keys']"
            )

    def test_bytetrack_numeric_params_have_validators(self):
        """Numeric ByteTrack params must have VALUE_VALIDATORS entries."""
        from utils.config_manager import ConfigValidator
        for key in self.BYTETRACK_KEYS:
            assert key in ConfigValidator.VALUE_VALIDATORS, (
                f"{key} missing from ConfigValidator.VALUE_VALIDATORS"
            )
            val_type, min_val, max_val = ConfigValidator.VALUE_VALIDATORS[key]
            assert val_type in ('int', 'float'), (
                f"{key} validator type must be 'int' or 'float', got '{val_type}'"
            )

    def test_greenongreen_preset_values_are_valid(self):
        """All track stability preset values must be within validator ranges."""
        from utils.config_manager import ConfigValidator
        from utils.greenongreen import GreenOnGreen

        for level, preset in GreenOnGreen.TRACK_STABILITY_PRESETS.items():
            for key, value in preset.items():
                assert key in ConfigValidator.VALUE_VALIDATORS, (
                    f"Preset '{level}' key '{key}' not in VALUE_VALIDATORS"
                )
                val_type, min_val, max_val = ConfigValidator.VALUE_VALIDATORS[key]
                assert min_val <= value <= max_val, (
                    f"Preset '{level}' {key}={value} out of range [{min_val}, {max_val}]"
                )

    def test_presets_match_validator_ranges(self):
        """All preset values must be within ConfigValidator min/max ranges.

        This ensures the Low/Medium/High preset buttons produce values
        that won't be rejected by config validation.
        """
        from utils.config_manager import ConfigValidator
        from utils.greenongreen import GreenOnGreen

        for level, preset in GreenOnGreen.TRACK_STABILITY_PRESETS.items():
            for key, value in preset.items():
                val_type, min_val, max_val = ConfigValidator.VALUE_VALIDATORS[key]
                assert min_val <= value <= max_val, (
                    f"Preset '{level}' {key}={value} out of validator "
                    f"range [{min_val}, {max_val}]"
                )


@pytest.mark.unit
class TestSessionMetadataFlow:
    """Guard the session-metadata flow on the networked controller.

    The flow is: Start Recording click -> modal collects
    {field_name, crop, weather, vehicle} -> JS sendCommand('set_session_metadata')
    -> networked.py send_command() publishes MQTT -> OWL-side handler in
    utils/mqtt_manager.py writes session_metadata.json. A break in any layer
    silently loses metadata, so we pin the key names at each boundary.
    """

    METADATA_KEYS = ['field_name', 'crop', 'weather', 'vehicle']
    HTML_INPUT_IDS = ['meta-field-name', 'meta-crop', 'meta-weather', 'meta-vehicle']

    def test_networked_template_has_metadata_modal(self):
        html = (PROJECT_ROOT / 'controller' / 'networked' / 'templates' / 'index.html').read_text(encoding='utf-8')
        assert 'id="session-metadata-modal"' in html, "Modal element missing from index.html"
        for input_id in self.HTML_INPUT_IDS:
            assert f'id="{input_id}"' in html, f"Input #{input_id} missing from modal"

    def test_networked_controls_js_references_modal(self):
        js = (PROJECT_ROOT / 'controller' / 'networked' / 'static' / 'js' / 'modules' / '_controls.js').read_text(encoding='utf-8')
        assert 'openSessionMetadataModal' in js
        assert 'closeSessionMetadataModal' in js
        # Modal interception must be on the path into recording-on, not bypassed.
        assert 'openSessionMetadataModal(btn)' in js, (
            "toggleMainRecording must call openSessionMetadataModal instead of _doToggleRecordingOn directly"
        )
        for input_id in self.HTML_INPUT_IDS:
            assert input_id in js, f"JS does not reference input #{input_id}"

    def test_networked_send_command_has_metadata_case(self):
        """networked.py send_command must explicitly spread the four fields
        into the MQTT payload — the fallback handler nests value={} which
        the OWL handler won't read."""
        py = (PROJECT_ROOT / 'controller' / 'networked' / 'networked.py').read_text(encoding='utf-8')
        assert "action == 'set_session_metadata'" in py, (
            "networked.py send_command missing explicit set_session_metadata branch"
        )
        for key in self.METADATA_KEYS:
            assert f"'{key}'" in py, f"networked.py metadata payload missing key {key!r}"

    def test_owl_handler_reads_same_keys(self):
        """utils/mqtt_manager.py must read the same four keys it's sent."""
        py = (PROJECT_ROOT / 'utils' / 'mqtt_manager.py').read_text(encoding='utf-8')
        assert "action == 'set_session_metadata'" in py
        for key in self.METADATA_KEYS:
            assert f"command.get('{key}'" in py, (
                f"mqtt_manager.py handler does not read key {key!r} from command"
            )


@pytest.mark.unit
class TestSaveFoldsSliderValues:
    """Guard the 2026-07-10 field-bug fix: saving a named config used to
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


@pytest.mark.unit
class TestCameraWhiteBalance:
    """[Camera] awb_mode / awb_red_gain / awb_blue_gain (R3, v3.11.0).

    Live-tunable white balance for the red-rendering Arducam IMX296.
    Guards the contract between GENERAL_CONFIG.ini, the validator, owl.py,
    and the shared CONFIG_FIELD_DEFS (sync test enforces the JS side)."""

    def test_general_config_has_awb_keys_with_defaults(self):
        cfg = configparser.ConfigParser()
        cfg.read(PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini')
        assert cfg.get('Camera', 'awb_mode') == 'auto', (
            "Default awb_mode must be auto (v3.14): the old daylight lock "
            "pins the Pi AWB search to 5500-6500 K and renders Arducam "
            "IMX296 modules red."
        )
        assert cfg.getfloat('Camera', 'awb_red_gain') == 2.0
        assert cfg.getfloat('Camera', 'awb_blue_gain') == 2.0

    def test_code_fallbacks_match_general_config(self):
        """owl.py, video_manager and seed_autosave must agree on the default
        so a profile without the key behaves like a fresh GENERAL_CONFIG."""
        import inspect
        from utils import video_manager
        from utils.config_manager import CAMERA_WB_DEFAULTS
        assert CAMERA_WB_DEFAULTS['awb_mode'] == 'auto'
        sig = inspect.signature(video_manager.build_awb_controls)
        assert sig.parameters['awb_mode'].default == 'auto'
        assert inspect.signature(video_manager.VideoStream.__init__) \
            .parameters['awb_mode'].default == 'auto'
        owl_source = (PROJECT_ROOT / 'owl.py').read_text()
        assert "config.get('Camera', 'awb_mode', fallback='auto')" in owl_source

    def test_seed_autosave_injects_missing_wb_keys(self, tmp_path):
        """Profiles saved before v3.11 have no WB keys; the autosave working
        copy must carry them so the kiosk editor renders the WB fields."""
        from utils.config_manager import seed_autosave, AUTOSAVE_CONFIG
        src = tmp_path / 'old_profile.ini'
        src.write_text("[Camera]\nresolution_width = 416\nresolution_height = 320\n")
        seed_autosave(tmp_path, src)
        cfg = configparser.ConfigParser()
        cfg.read(tmp_path / AUTOSAVE_CONFIG)
        assert cfg.get('Camera', 'awb_mode') == 'auto'
        assert cfg.get('Camera', 'awb_red_gain') == '2.0'
        assert cfg.get('Camera', 'awb_blue_gain') == '2.0'

    def test_seed_autosave_keeps_existing_wb_keys(self, tmp_path):
        from utils.config_manager import seed_autosave, AUTOSAVE_CONFIG
        src = tmp_path / 'tuned.ini'
        src.write_text("[Camera]\nawb_mode = manual\nawb_red_gain = 1.4\n"
                       "awb_blue_gain = 3.1\n")
        seed_autosave(tmp_path, src)
        cfg = configparser.ConfigParser()
        cfg.read(tmp_path / AUTOSAVE_CONFIG)
        assert cfg.get('Camera', 'awb_mode') == 'manual'
        assert cfg.get('Camera', 'awb_red_gain') == '1.4'

    def test_validator_lists_awb_keys_as_optional(self):
        from utils.config_manager import ConfigValidator
        camera = ConfigValidator.REQUIRED_CONFIG['Camera']
        for key in ('awb_mode', 'awb_red_gain', 'awb_blue_gain'):
            assert key in camera['optional_keys'], (
                f"{key} missing from Camera optional_keys — ConfigValidator "
                f"will warn at startup."
            )

    def test_gains_in_value_validators_mode_not(self):
        """Gains are float-range validated; awb_mode is a string enum key and
        must NOT be in VALUE_VALIDATORS (int/float/pin 3-tuples only)."""
        from utils.config_manager import ConfigValidator
        assert ConfigValidator.VALUE_VALIDATORS['awb_red_gain'] == ('float', 0.1, 8.0)
        assert ConfigValidator.VALUE_VALIDATORS['awb_blue_gain'] == ('float', 0.1, 8.0)
        assert 'awb_mode' not in ConfigValidator.VALUE_VALIDATORS

    def test_gain_minimum_is_not_zero(self):
        """Min gain 0.1, never 0.0 — libcamera treats ColourGains=(0,0) as
        'let AWB choose', silently defeating manual mode."""
        from utils.config_manager import ConfigValidator
        assert ConfigValidator.VALUE_VALIDATORS['awb_red_gain'][1] > 0
        assert ConfigValidator.VALUE_VALIDATORS['awb_blue_gain'][1] > 0

    def test_owl_reads_all_three_keys(self):
        """owl.py must read the keys and pass them to VideoStream (source
        inspection — owl.py cannot import on Windows)."""
        owl_source = (PROJECT_ROOT / 'owl.py').read_text()
        for attr in ('self.awb_mode', 'self.awb_red_gain', 'self.awb_blue_gain'):
            assert attr in owl_source, (
                f"owl.py must capture {attr} from [Camera] config."
            )
        assert 'awb_mode=self.awb_mode' in owl_source, (
            "owl.py must pass awb_mode into VideoStream()."
        )

    def test_valid_awb_modes_set(self):
        from utils.config_manager import ConfigValidator
        assert ConfigValidator.VALID_AWB_MODES == {
            'auto', 'daylight', 'cloudy', 'tungsten', 'fluorescent',
            'indoor', 'manual'}

    def test_mqtt_manager_hot_applies_camera_keys(self):
        """CAMERA_LIVE_KEYS branch must exist and include exp_compensation
        (the pre-existing silent no-op this feature fixes)."""
        mqtt_source = (PROJECT_ROOT / 'utils' / 'mqtt_manager.py').read_text()
        assert 'CAMERA_LIVE_KEYS' in mqtt_source
        assert 'set_camera_controls' in mqtt_source
        from utils.mqtt_manager import CAMERA_LIVE_KEYS
        assert CAMERA_LIVE_KEYS == {'awb_mode', 'awb_red_gain',
                                    'awb_blue_gain', 'exp_compensation'}
