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
        assert path.exists(), "CONTROLLER.ini not found in config/"

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
