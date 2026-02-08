"""
Shared fixtures for OWL config editor tests.

Windows integration testing (full stack):
    1. Start MQTT broker: docker run -d -p 1883:1883 eclipse-mosquitto
       (or install Mosquitto locally)
    2. Start owl.py: python owl.py --input test_images
       (auto-detects Windows, uses mock GPIO)
    3. Start networked.py: python controller/networked/networked.py
    4. Open http://localhost:8000 -- page 6 is the config editor
    5. Run tests: pytest tests/ -v
"""

import configparser
import importlib
import os
import shutil
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# All 12 sections present in the OWL INI configs
EXPECTED_SECTIONS = [
    'System', 'MQTT', 'WebDashboard', 'Network', 'GPS',
    'Controller', 'Visualisation', 'Camera', 'GreenOnGreen',
    'GreenOnBrown', 'DataCollection', 'Relays'
]


@pytest.fixture
def tmp_config_dir(tmp_path):
    """Create a temp directory with a valid 12-section INI config file."""
    ini_content = """\
[System]
algorithm = exhsv
input_file_or_directory =
relay_num = 4
actuation_duration = 0.15
delay = 0

[MQTT]
enable = False
broker_ip = localhost
broker_port = 1883
device_id = test-owl

[WebDashboard]
port = 8000

[Network]
mode = standalone
static_ip = 192.168.1.11
controller_ip = 192.168.1.2

[GPS]
source = none
port = /dev/ttyUSB0
baudrate = 9600

[Controller]
controller_type = none
detection_mode_pin_up = 36
detection_mode_pin_down = 35
recording_pin = 38
sensitivity_pin = 40
low_sensitivity_config = config/DAY_SENSITIVITY_1.ini
medium_sensitivity_config = config/DAY_SENSITIVITY_2.ini
high_sensitivity_config = config/DAY_SENSITIVITY_3.ini
switch_purpose = recording
switch_pin = 37

[Visualisation]
image_loop_time = 5

[Camera]
resolution_width = 416
resolution_height = 320
exp_compensation = -2
crop_factor_horizontal = 0.02
crop_factor_vertical = 0.02

[GreenOnGreen]
model_path = models
confidence = 0.5
class_filter_id = None

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

[DataCollection]
image_sample_enable = False
detection_enable = True
sample_method = whole
sample_frequency = 30
save_directory = /media/owl/SanDisk
log_fps = False
camera_name = cam1

[Relays]
0 = 13
1 = 15
2 = 16
3 = 18
"""
    config_file = tmp_path / 'test_config.ini'
    config_file.write_text(ini_content)

    # Also create an active_config.txt pointing to it
    active_file = tmp_path / 'active_config.txt'
    active_file.write_text(str(config_file) + '\n')

    return tmp_path


@pytest.fixture
def mock_owl(tmp_config_dir):
    """Mock Owl instance with config_path, config, and detection params."""
    config_path = tmp_config_dir / 'test_config.ini'

    config = configparser.ConfigParser()
    config.read(config_path)

    owl = MagicMock()
    owl._config_path = config_path
    # Expose config_path as a property (like the real Owl class)
    type(owl).config_path = property(lambda self: self._config_path)
    owl.config = config

    # GreenOnBrown params as real attributes
    owl.exg_min = 25
    owl.exg_max = 200
    owl.hue_min = 39
    owl.hue_max = 83
    owl.saturation_min = 50
    owl.saturation_max = 220
    owl.brightness_min = 60
    owl.brightness_max = 190

    return owl


@pytest.fixture
def mqtt_publisher(mock_owl):
    """Real OWLMQTTPublisher wired to a mock MQTT client and mock Owl."""
    from utils.mqtt_manager import OWLMQTTPublisher

    publisher = OWLMQTTPublisher(
        broker_host='localhost',
        broker_port=1883,
        client_id='test_owl',
        device_id='test-owl'
    )

    # Replace the real MQTT client with a mock
    publisher.client = MagicMock()
    publisher.connected = True
    publisher.running = True

    # Wire up the owl instance
    publisher.set_owl_instance(
        mock_owl,
        low_config='config/DAY_SENSITIVITY_1.ini',
        medium_config='config/DAY_SENSITIVITY_2.ini',
        high_config='config/DAY_SENSITIVITY_3.ini'
    )

    return publisher


@pytest.fixture
def networked_test_client(tmp_config_dir):
    """Flask test_client with a mocked CentralController.

    Patches the module-level `controller` in networked.py so the Flask
    routes use our mock instead of trying to connect to a real MQTT broker.
    """
    # Create a preset INI in the tmp dir so list_local_presets can find it
    preset_ini = tmp_config_dir / 'DAY_SENSITIVITY_1.ini'
    config = configparser.ConfigParser()
    config.read(tmp_config_dir / 'test_config.ini')
    with open(preset_ini, 'w') as f:
        config.write(f)

    # Also create a non-default preset
    custom_ini = tmp_config_dir / 'CUSTOM.ini'
    with open(custom_ini, 'w') as f:
        config.write(f)

    # Patch CentralController so importing networked.py doesn't connect to MQTT
    with patch('controller.networked.networked.CentralController') as MockCC:
        mock_ctrl = MagicMock()
        MockCC.return_value = mock_ctrl

        # Configure mock methods
        mock_ctrl.mqtt_connected = True
        mock_ctrl.mqtt_client = MagicMock()
        mock_ctrl.owls_state = {}

        # Make request_device_config return sample data by default
        mock_ctrl.request_device_config.return_value = {
            'config': {'GreenOnBrown': {'exg_min': '25', 'exg_max': '200'}},
            'config_name': 'test_config.ini',
            'config_path': str(tmp_config_dir / 'test_config.ini')
        }

        # send_command returns success
        mock_ctrl.send_command.return_value = {'success': True}

        # list_local_presets returns preset list from tmp dir
        mock_ctrl.list_local_presets.return_value = [
            {'name': 'DAY_SENSITIVITY_1', 'filename': 'DAY_SENSITIVITY_1.ini',
             'path': str(preset_ini), 'is_default': True},
            {'name': 'CUSTOM', 'filename': 'CUSTOM.ini',
             'path': str(custom_ini), 'is_default': False}
        ]

        # read_preset uses real configparser on tmp files
        def _read_preset(filename):
            filepath = tmp_config_dir / filename
            if not filepath.exists():
                return None
            cp = configparser.ConfigParser()
            cp.read(filepath)
            return {
                'config': {s: dict(cp[s]) for s in cp.sections()},
                'config_name': filename,
                'config_path': str(filepath)
            }
        mock_ctrl.read_preset.side_effect = _read_preset

        # Force re-import to pick up the mock
        import importlib
        import controller.networked.networked as net_mod
        importlib.reload(net_mod)
        # Point the module's controller to our mock
        net_mod.controller = mock_ctrl

        app = net_mod.app
        app.config['TESTING'] = True

        with app.test_client() as client:
            yield client, mock_ctrl


@pytest.fixture
def standalone_test_client(tmp_config_dir):
    """Flask test_client for the standalone OWLDashboard config editor.

    Creates a real OWLDashboard instance but:
    - Mocks DashMQTTSubscriber to avoid MQTT connection
    - Redirects all config file operations to tmp_config_dir
    - Populates tmp dir with preset INI files for testing
    """
    # Create preset files in the temp config dir
    src_ini = tmp_config_dir / 'test_config.ini'
    for name in ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini']:
        shutil.copy(str(src_ini), str(tmp_config_dir / name))

    # Write active_config.txt pointing to DAY_SENSITIVITY_2
    (tmp_config_dir / 'active_config.txt').write_text(
        f'config/DAY_SENSITIVITY_2.ini'
    )

    config_dir_str = str(tmp_config_dir)

    # Patch MQTT to avoid broker connection
    with patch('controller.standalone.standalone.DashMQTTSubscriber') as MockMQTT:
        mock_mqtt = MagicMock()
        MockMQTT.return_value = mock_mqtt

        # Also patch get_rpi_version to avoid GPIO imports
        with patch('controller.standalone.standalone.get_rpi_version', return_value='unknown'):
            import controller.standalone.standalone as sa_mod
            importlib.reload(sa_mod)

            dashboard = sa_mod.OWLDashboard(
                config_file=str(tmp_config_dir / 'DAY_SENSITIVITY_2.ini')
            )

    # Redirect file operations to temp dir
    dashboard._get_config_dir = lambda: config_dir_str

    def _resolve(relative_path):
        """Resolve config paths relative to temp dir."""
        basename = os.path.basename(relative_path)
        full = os.path.join(config_dir_str, basename)
        if os.path.exists(full):
            return full
        return full

    dashboard._resolve_config_path = _resolve

    def _get_active():
        pointer = os.path.join(config_dir_str, 'active_config.txt')
        if os.path.exists(pointer):
            with open(pointer, 'r') as f:
                active = f.read().strip()
                if active:
                    return active
        return 'config/DAY_SENSITIVITY_2.ini'

    dashboard._get_active_config_path = _get_active

    dashboard.app.config['TESTING'] = True

    with dashboard.app.test_client() as client:
        yield client, dashboard, tmp_config_dir
