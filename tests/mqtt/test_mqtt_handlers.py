"""OWLMQTTPublisher config-editor handlers: what get_config publishes and
what the save commands write.

Every test that saves uses the `owl_home` fixture (a private OWL home with an
empty config/) and asserts on the files that land there. Until 3.27.3 these
tests read back the fixture's own INI, which the handler never writes, while
the handler rewrote the checkout's real config/active_config.txt.
"""

import configparser
import json
import threading
from unittest.mock import MagicMock, patch

import pytest


def ini(path):
    parser = configparser.ConfigParser()
    parser.read_string(path.read_text())
    return parser


def config_files(home):
    return sorted(p.name for p in (home / 'config').iterdir())


POINTER = 'active_config.txt'
AUTOSAVE = 'config_autosave.ini'


@pytest.mark.unit
class TestGetConfig:
    def test_get_config_publishes_the_file_the_owl_loaded(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_get_config()

        mqtt_publisher.client.publish.assert_called_once()
        topic, payload_str = mqtt_publisher.client.publish.call_args[0][:2]
        assert topic == mqtt_publisher.topics['config']

        payload = json.loads(payload_str)
        assert payload['device_id'] == 'test-owl'
        assert payload['config_name'] == 'test_config.ini'
        assert payload['config_path'] == str(mock_owl.config_path)
        assert payload['config']['GreenOnBrown']['exg_min'] == '25'
        assert payload['config']['System']['relay_num'] == '4'

    def test_get_config_with_no_config_path_publishes_nothing(self):
        from utils.mqtt_manager import OWLMQTTPublisher

        publisher = OWLMQTTPublisher(broker_host='localhost', broker_port=1883,
                                     client_id='test_nopath', device_id='test-nopath')
        publisher.client = MagicMock()
        publisher.connected = True
        with patch.object(publisher, '_resolve_config_path', return_value=None):
            publisher._handle_get_config()
        publisher.client.publish.assert_not_called()


@pytest.mark.unit
class TestSetConfigSection:
    def test_thresholds_reach_the_instance_and_the_config(self, mqtt_publisher, mock_owl, owl_home):
        mqtt_publisher._handle_set_config_section('GreenOnBrown', {'exg_min': 30, 'exg_max': 180})
        assert mqtt_publisher.io_flush()          # the autosave it queues, into owl_home
        assert (mock_owl.exg_min, mock_owl.exg_max) == (30, 180)
        assert mock_owl.config.get('GreenOnBrown', 'exg_min') == '30'
        assert mock_owl.config.get('GreenOnBrown', 'exg_max') == '180'

    def test_a_threshold_edit_is_saved_to_the_working_file_by_itself(
            self, mqtt_publisher, mock_owl, owl_home):
        """Sliders have no Save button: the edit must survive a restart."""
        mqtt_publisher._handle_command({'action': 'set_config_section', 'section': 'GreenOnBrown',
                                        'params': {'exg_min': '31'}})
        assert mqtt_publisher.io_flush()
        assert ini(owl_home / 'config' / AUTOSAVE).get('GreenOnBrown', 'exg_min') == '31'
        assert (owl_home / 'config' / POINTER).read_text() == f'config/{AUTOSAVE}\n'

    def test_a_restart_only_key_is_kept_for_the_next_save_not_applied(self, mqtt_publisher, mock_owl):
        mqtt_publisher._handle_set_config_section('Camera', {'resolution_width': '640',
                                                             'resolution_height': '480'})
        assert mock_owl.config.get('Camera', 'resolution_width') == '640'
        assert mock_owl.config.get('Camera', 'resolution_height') == '480'
        assert mock_owl.resolution == (1456, 1088)
        assert mqtt_publisher.state['restart_required'] == 'resolution_height,resolution_width'


@pytest.mark.unit
class TestSaveConfig:
    def test_an_unnamed_save_goes_to_the_working_file_and_leaves_the_profile_alone(
            self, mqtt_publisher, mock_owl, owl_home):
        loaded = mock_owl.config_path.read_text()
        mock_owl.config.set('GreenOnBrown', 'exg_min', '33')

        mqtt_publisher._handle_command({'action': 'save_config', 'filename': None})
        assert mqtt_publisher.io_flush()

        assert config_files(owl_home) == [POINTER, AUTOSAVE]
        saved = ini(owl_home / 'config' / AUTOSAVE)
        assert saved.get('GreenOnBrown', 'exg_min') == '33'
        assert saved.get('Meta', 'source') == 'test_config.ini'
        assert (owl_home / 'config' / POINTER).read_text() == f'config/{AUTOSAVE}\n'
        assert mock_owl.config_path.read_text() == loaded, 'the loaded profile was rewritten'

    def test_a_named_save_writes_that_file_and_makes_it_the_active_config(
            self, mqtt_publisher, mock_owl, owl_home):
        """Otherwise the OWL keeps running the working file and the dashboard
        keeps showing '<old profile> - unsaved changes' (2026-07-10)."""
        mock_owl.config.set('GreenOnBrown', 'exg_min', '34')
        mock_owl.config.set('Camera', 'crop_left', '0.2')      # mount geometry: never in a profile

        mqtt_publisher._handle_command({'action': 'save_config', 'filename': 'my_saved.ini',
                                        'name': 'Wheat, dewy', 'notes': 'north paddock'})
        assert mqtt_publisher.io_flush()

        assert config_files(owl_home) == [POINTER, AUTOSAVE, 'my_saved.ini']
        saved = ini(owl_home / 'config' / 'my_saved.ini')
        assert saved.get('GreenOnBrown', 'exg_min') == '34'
        assert saved.get('Meta', 'display_name') == 'Wheat, dewy'
        assert saved.get('Meta', 'notes') == 'north paddock'
        assert not saved.has_option('Meta', 'source')
        assert not saved.has_option('Camera', 'crop_left')
        assert (owl_home / 'config' / POINTER).read_text() == 'config/my_saved.ini\n'
        # the working copy now mirrors the saved profile
        working = ini(owl_home / 'config' / AUTOSAVE)
        assert working.get('GreenOnBrown', 'exg_min') == '34'
        assert working.get('Meta', 'source') == 'my_saved.ini'

    def test_a_filename_cannot_leave_the_config_folder(self, mqtt_publisher, mock_owl, owl_home):
        mqtt_publisher._handle_save_config(filename='../../escaped.ini')
        assert (owl_home / 'config' / 'escaped.ini').exists()
        assert not (owl_home / 'escaped.ini').exists()
        assert not (owl_home.parent / 'escaped.ini').exists()

    @pytest.mark.parametrize('name', ['GENERAL_CONFIG.ini', 'CONTROLLER.ini', 'GEOMETRY.ini',
                                      'config/GENERAL_CONFIG.ini'])
    def test_a_save_never_overwrites_the_shipped_or_device_files(
            self, mqtt_publisher, mock_owl, owl_home, name):
        target = owl_home / 'config' / name.split('/')[-1]
        target.write_text('[Original]\nkeep = me\n')

        mqtt_publisher._handle_command({'action': 'save_config', 'filename': name})
        assert mqtt_publisher.io_flush()

        assert target.read_text() == '[Original]\nkeep = me\n'
        assert config_files(owl_home) == [target.name], 'a refused save still wrote something'

    def test_saving_after_a_profile_switch_follows_the_working_file(
            self, mqtt_publisher, mock_owl, owl_home):
        """Booted from the working file; the dashboard then pointed the OWL at
        a named profile without a restart. A live change saved now must be
        what the next boot loads, and still say which profile it came from."""
        config = owl_home / 'config'
        (config / AUTOSAVE).write_text('[Meta]\nsource = DAY.ini\n')
        (config / POINTER).write_text('config/NIGHT.ini\n')
        mock_owl._config_path = config / AUTOSAVE
        mqtt_publisher.set_owl_instance(mock_owl)       # the path is taken when the OWL attaches
        mock_owl.config.set('GreenOnBrown', 'exg_min', '35')

        mqtt_publisher._handle_save_config()

        assert (config / POINTER).read_text() == f'config/{AUTOSAVE}\n'
        saved = ini(config / AUTOSAVE)
        assert saved.get('GreenOnBrown', 'exg_min') == '35'
        assert saved.get('Meta', 'source') == 'DAY.ini'


@pytest.mark.unit
class TestSetActiveConfig:
    def test_points_the_owl_at_the_profile_and_seeds_the_working_copy(
            self, mqtt_publisher, owl_home):
        (owl_home / 'config' / 'DAY.ini').write_text('[GreenOnBrown]\nexg_min = 41\n')

        mqtt_publisher._handle_command({'action': 'set_active_config', 'config': 'config/DAY.ini'})

        assert (owl_home / 'config' / POINTER).read_text() == 'config/DAY.ini\n'
        working = ini(owl_home / 'config' / AUTOSAVE)
        assert working.get('GreenOnBrown', 'exg_min') == '41'
        assert working.get('Meta', 'source') == 'DAY.ini'
        assert config_files(owl_home) == ['DAY.ini', POINTER, AUTOSAVE]

    @pytest.mark.parametrize('command', [
        {'action': 'set_active_config'},
        {'action': 'set_active_config', 'config': ''},
        # only ever a detection profile: the pointer decides what owl.py
        # loads and what GET /api/config serves
        {'action': 'set_active_config', 'config': 'config/CONTROLLER.ini'},
        {'action': 'set_active_config', 'config': 'config/../owl.py'},
        {'action': 'set_active_config', 'config': 'config/../../etc/x.ini'},
        {'action': 'set_active_config', 'config': 'GEOMETRY.ini'},
        {'action': 'set_active_config', 'config': 'config/DAY_TEMPLATE.ini'},
    ])
    def test_anything_that_is_not_a_profile_writes_nothing(self, mqtt_publisher, owl_home, command):
        mqtt_publisher._handle_command(command)
        assert config_files(owl_home) == []


@pytest.mark.unit
class TestConcurrency:
    def test_edits_and_saves_from_two_threads_leave_a_whole_file(
            self, mqtt_publisher, mock_owl, owl_home):
        """The MQTT thread edits while the I/O worker saves (BUG 2)."""
        errors = []

        def edits():
            try:
                for i in range(20):
                    mqtt_publisher._handle_set_config_section('GreenOnBrown',
                                                              {'exg_min': str(25 + i)})
            except Exception as e:
                errors.append(('set', e))

        def saves():
            try:
                for _ in range(20):
                    mqtt_publisher._handle_save_config()
            except Exception as e:
                errors.append(('save', e))

        threads = [threading.Thread(target=edits), threading.Thread(target=saves)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not any(t.is_alive() for t in threads)
        assert not errors, f"Concurrency errors: {errors}"
        assert mqtt_publisher.io_flush()
        mqtt_publisher._handle_save_config()

        saved = ini(owl_home / 'config' / AUTOSAVE)       # parses: never half-written
        assert saved.get('GreenOnBrown', 'exg_min') == '44'
        assert saved.get('System', 'relay_num') == '4'
        assert config_files(owl_home) == [POINTER, AUTOSAVE], 'temp files left behind'


@pytest.mark.unit
class TestTrackbarQueue:
    """cv2 trackbars may only be touched from the main loop: the MQTT thread
    queues the change."""

    def test_trackbar_queued_when_display_active(self, mqtt_publisher, mock_owl):
        mock_owl.show_display = True
        mqtt_publisher._update_greenonbrown_param('exg_min', 42)
        assert mock_owl._pending_trackbar_updates == {'ExG-Min': 42}
        assert mock_owl.exg_min == 42

    def test_trackbar_not_queued_when_display_off(self, mqtt_publisher, mock_owl):
        mock_owl.show_display = False
        mqtt_publisher._update_greenonbrown_param('hue_max', 100)
        assert mock_owl._pending_trackbar_updates == {}
        assert mock_owl.hue_max == 100

    def test_sensitivity_preset_queues_all_eight_trackbars(self, mqtt_publisher, mock_owl):
        mock_owl.show_display = True
        mqtt_publisher._handle_command({'action': 'set_sensitivity_level', 'level': 'low'})
        # [Sensitivity_Low] in the fixture INI
        assert mock_owl._pending_trackbar_updates == {
            'ExG-Min': 25, 'ExG-Max': 200, 'Hue-Min': 41, 'Hue-Max': 80,
            'Sat-Min': 52, 'Sat-Max': 218, 'Bright-Min': 62, 'Bright-Max': 188}
        assert (mock_owl.hue_min, mock_owl.brightness_max) == (41, 188)


@pytest.mark.unit
class TestInvertHue:
    @pytest.mark.parametrize('sent, expected', [('true', True), ('1', True), ('false', False)])
    def test_invert_hue_values(self, mqtt_publisher, mock_owl, sent, expected):
        mock_owl.invert_hue = not expected
        mqtt_publisher._update_greenonbrown_param('invert_hue', sent)
        assert mock_owl.invert_hue is expected


@pytest.mark.unit
class TestStateFromTheOwl:
    def test_confidence_synced_from_owl(self, mqtt_publisher, mock_owl):
        mock_owl._gog_confidence = 0.85
        mqtt_publisher._sync_parameters_to_state()
        assert mqtt_publisher.state['confidence'] == 0.85

    def test_resolution_synced_from_owl(self, mqtt_publisher, mock_owl):
        mock_owl.resolution = (640, 480)
        mqtt_publisher._sync_parameters_to_state()
        assert (mqtt_publisher.state['resolution_width'],
                mqtt_publisher.state['resolution_height']) == (640, 480)

    def test_resolution_is_zero_until_an_owl_is_attached(self):
        from utils.mqtt_manager import OWLMQTTPublisher
        fresh = OWLMQTTPublisher(broker_host='localhost', client_id='fresh_test')
        assert (fresh.state['resolution_width'], fresh.state['resolution_height']) == (0, 0)
