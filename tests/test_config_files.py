"""Tests for INI config file consistency and the Owl.config_path property."""

import configparser
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / 'config'

INI_FILES = [
    'DAY_SENSITIVITY_1.ini',
    'DAY_SENSITIVITY_2.ini',
    'DAY_SENSITIVITY_3.ini',
]

EXPECTED_SECTIONS = [
    'System', 'Controller', 'Visualisation', 'Camera', 'GreenOnGreen',
    'GreenOnBrown', 'DataCollection', 'Relays'
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

    def test_all_configs_have_identical_sections(self):
        """All 3 DAY_SENSITIVITY configs must share the same section names."""
        section_sets = []
        for ini_name in INI_FILES:
            config = configparser.ConfigParser()
            config.read(CONFIG_DIR / ini_name)
            section_sets.append(set(config.sections()))

        first = section_sets[0]
        for i, sections in enumerate(section_sets[1:], 1):
            assert sections == first, (
                f"{INI_FILES[i]} sections differ from {INI_FILES[0]}: "
                f"missing={first - sections}, extra={sections - first}"
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

    def test_sensitivity_configs_do_not_have_infrastructure_sections(self):
        """DAY_SENSITIVITY configs should NOT have MQTT/WebDashboard/Network/GPS."""
        for ini_name in INI_FILES:
            config = configparser.ConfigParser()
            config.read(CONFIG_DIR / ini_name)
            for section in CONTROLLER_INI_SECTIONS:
                assert not config.has_section(section), (
                    f"{ini_name} should not have [{section}] (moved to CONTROLLER.ini)"
                )

    def test_configs_have_medium_sensitivity_key(self):
        """All configs should have medium_sensitivity_config in [Controller]."""
        for ini_name in INI_FILES:
            config = configparser.ConfigParser()
            config.read(CONFIG_DIR / ini_name)
            assert config.has_option('Controller', 'medium_sensitivity_config'), (
                f"{ini_name} missing Controller.medium_sensitivity_config"
            )


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
