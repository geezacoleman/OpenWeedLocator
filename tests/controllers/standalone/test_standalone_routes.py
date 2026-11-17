"""Tests for standalone controller Flask API routes (config editor endpoints)."""

import configparser
import json
import os
import time
from unittest.mock import patch, MagicMock

import pytest


def _stabilize_mqtt(dashboard):
    """Mark the fixture's DashMQTTSubscriber connected, as if the broker
    were up (the fixture never connects it; its paho client is a mock)."""
    mqtt = dashboard.mqtt_client
    if mqtt and hasattr(mqtt, 'client'):
        try:
            mqtt.client.loop_stop()
        except Exception:
            pass
    if mqtt:
        mqtt.connected = True


def _published(dashboard, connected=True):
    """What the dashboard puts on the wire: a list of (topic, payload) that
    fills as routes run. The real DashMQTTSubscriber builds each command;
    only the broker socket (its paho client) is a stand-in."""
    sent = []
    dashboard.mqtt_client.connected = connected
    dashboard.mqtt_client.client.publish = (
        lambda topic, payload, *a, **k: sent.append((topic, json.loads(payload))))
    return sent


def _active_ini(dashboard):
    """The config file the OWL would boot from after the request."""
    config = configparser.ConfigParser()
    config.read(dashboard._resolve_config_path(dashboard._get_active_config_path()))
    return config


@pytest.mark.unit
class TestGetConfig:
    """Tests for GET /api/config."""

    def test_returns_config_on_success(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'config' in data
        assert 'GreenOnBrown' in data['config']
        assert 'config_name' in data
        assert 'available_configs' in data

    def test_config_contains_all_sections(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config')
        data = resp.get_json()

        config = data['config']
        for section in ['System', 'GreenOnBrown', 'Camera', 'Controller', 'DataCollection', 'Relays']:
            assert section in config, f"Missing section: {section}"

    def test_merges_geometry_ini_into_config(self, standalone_test_client):
        """GET /api/config must return GEOMETRY.ini values so the geometry editor
        seeds from what the OWL actually runs (2026-07-10 field bug: editor
        opened on defaults, Done re-saved defaults over the real geometry)."""
        client, dashboard, tmp_dir = standalone_test_client
        (tmp_dir / 'GEOMETRY.ini').write_text(
            '[Camera]\ncrop_left = 0.11\ncrop_right = 0.07\n'
            '[System]\nactuation_top = 0.25\nactuation_bottom = 0.9\n'
        )

        resp = client.get('/api/config')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['config']['Camera']['crop_left'] == '0.11'
        assert data['config']['System']['actuation_top'] == '0.25'
        # Other keys from the active config must survive the merge
        assert 'exg_min' in data['config']['GreenOnBrown']

    def test_missing_config_self_heals(self, standalone_test_client):
        """A dangling active pointer (e.g. a hand-deleted autosave) must
        re-seed the working copy from GENERAL_CONFIG.ini instead of sitting
        in a dead 'Config file not found' loop (2026-08-15 bench bug)."""
        client, dashboard, tmp_dir = standalone_test_client

        # Point to a nonexistent file
        dashboard._get_active_config_path = lambda: 'config/NONEXISTENT.ini'

        resp = client.get('/api/config')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'GreenOnBrown' in data['config']
        assert os.path.exists(os.path.join(str(tmp_dir),
                                           'config_autosave.ini'))


@pytest.mark.unit
class TestSaveConfig:
    """Tests for POST /api/config."""

    def test_saves_new_config_file(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        new_config = {
            'GreenOnBrown': {'exg_min': '30', 'exg_max': '180'},
            'System': {'algorithm': 'exg'}
        }

        resp = client.post('/api/config', json={
            'config': new_config,
            'filename': 'my_custom.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['filename'] == 'my_custom.ini'

        # Verify file was actually written
        saved_path = os.path.join(str(tmp_dir), 'my_custom.ini')
        assert os.path.exists(saved_path)

        saved = configparser.ConfigParser()
        saved.read(saved_path)
        assert saved.get('GreenOnBrown', 'exg_min') == '30'

    def test_blocks_overwrite_of_default_presets(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}},
            'filename': 'GENERAL_CONFIG.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False
        assert 'Cannot overwrite' in data['error']

    def test_returns_400_on_missing_config_data(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config', json={'filename': 'test.ini'})
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False

    def test_save_with_set_active(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}},
            'filename': 'activated.ini',
            'set_active': True
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['is_active'] is True
        assert data['restart_required'] is False  # Config pushed live via MQTT

        # Verify active_config.txt was updated
        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'r') as f:
            assert 'activated.ini' in f.read()

    def test_save_named_writes_meta(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}},
            'name': 'High sensitivity - wheat',
            'notes': 'dewy mornings'
        })
        data = resp.get_json()
        assert data['success'] is True
        cp = configparser.ConfigParser()
        cp.read(os.path.join(str(tmp_dir), data['filename']))
        assert cp.get('Meta', 'display_name') == 'High sensitivity - wheat'
        assert cp.get('Meta', 'notes') == 'dewy mornings'

    def test_save_update_in_place_overwrites_same_file(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        r1 = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}}, 'name': 'wheat'})
        fn = r1.get_json()['filename']
        r2 = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exhsv'}}, 'overwrite_filename': fn})
        assert r2.get_json()['filename'] == fn   # same file, no new timestamp
        cp = configparser.ConfigParser()
        cp.read(os.path.join(str(tmp_dir), fn))
        assert cp.get('System', 'algorithm') == 'exhsv'

    def test_save_strips_geometry(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/config', json={
            'config': {
                'Camera': {'crop_left': '0.1', 'resolution_width': '1456'},
                'System': {'actuation_top': '0.2', 'algorithm': 'exg'},
            }, 'name': 'geomtest'})
        cp = configparser.ConfigParser()
        cp.read(os.path.join(str(tmp_dir), resp.get_json()['filename']))
        assert not cp.has_option('Camera', 'crop_left')      # geometry stripped
        assert cp.get('Camera', 'resolution_width') == '1456'
        assert not cp.has_option('System', 'actuation_top')

    def test_resave_config_carrying_meta_does_not_crash(self, standalone_test_client):
        # Repro of the DuplicateSectionError: GET returns a [Meta] section, the
        # frontend round-trips it in data['config'], and the re-save must not 500.
        client, dashboard, tmp_dir = standalone_test_client
        r1 = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}}, 'name': 'wheat'})
        fn = r1.get_json()['filename']
        r2 = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exhsv'},
                       'Meta': {'display_name': 'wheat', 'created': '2020-01-01T00:00:00'}},
            'name': 'wheat', 'overwrite_filename': fn})
        assert r2.status_code == 200
        assert r2.get_json()['success'] is True
        cp = configparser.ConfigParser()
        cp.read(os.path.join(str(tmp_dir), fn))
        assert cp.get('System', 'algorithm') == 'exhsv'
        assert cp.get('Meta', 'display_name') == 'wheat'   # re-stamped once, no dup

    def test_update_without_name_preserves_existing_meta(self, standalone_test_client):
        # An update-in-place that omits name/notes must not blank the friendly name.
        client, dashboard, tmp_dir = standalone_test_client
        r1 = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}}, 'name': 'wheat', 'notes': 'am'})
        fn = r1.get_json()['filename']
        r2 = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exhsv'}}, 'overwrite_filename': fn})
        assert r2.get_json()['success'] is True
        cp = configparser.ConfigParser()
        cp.read(os.path.join(str(tmp_dir), fn))
        assert cp.get('Meta', 'display_name') == 'wheat'
        assert cp.get('Meta', 'notes') == 'am'

    def test_overwrite_filename_traversal_rejected(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/config', json={
            'config': {'System': {'algorithm': 'exg'}},
            'overwrite_filename': '../../evil'})
        assert resp.status_code == 400
        assert 'Invalid' in resp.get_json()['error']

    def test_template_files_excluded_from_config_listing(self, standalone_test_client):
        # A *_TEMPLATE.ini must never appear as a selectable detection config.
        client, dashboard, tmp_dir = standalone_test_client
        with open(os.path.join(str(tmp_dir), 'GEOMETRY_TEMPLATE.ini'), 'w') as f:
            f.write('[Camera]\ncrop_left = 0.02\n')
        resp = client.get('/api/config')
        names = [c['name'] for c in resp.get_json().get('available_configs', [])]
        assert 'GEOMETRY_TEMPLATE.ini' not in names


@pytest.mark.unit
class TestSetActiveConfig:
    """Tests for POST /api/config/set-active."""

    def test_sets_active_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/set-active', json={
            'config': 'GENERAL_CONFIG.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert data['restart_required'] is False  # Config pushed live via MQTT

    def test_returns_400_on_missing_config_name(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/set-active', json={})
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False

    def test_returns_404_for_nonexistent_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/set-active', json={
            'config': 'NONEXISTENT.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 404
        assert data['success'] is False


@pytest.mark.unit
class TestResetDefault:
    """Tests for POST /api/config/reset-default."""

    def test_resets_to_default(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # First set a non-default active config
        pointer = os.path.join(str(tmp_dir), 'active_config.txt')
        with open(pointer, 'w') as f:
            f.write('config/my_custom.ini')

        resp = client.post('/api/config/reset-default')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'GENERAL_CONFIG' in data['active_config']

        # Pointer file should be removed
        assert not os.path.exists(pointer)


@pytest.mark.unit
class TestDeleteConfig:
    """Tests for POST /api/config/delete."""

    def test_deletes_custom_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Create a custom config to delete
        custom = os.path.join(str(tmp_dir), 'my_custom.ini')
        with open(custom, 'w') as f:
            f.write('[System]\nalgorithm = exg\n')

        resp = client.post('/api/config/delete', json={
            'config': 'my_custom.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert not os.path.exists(custom)

    def test_blocks_deletion_of_default_presets(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/delete', json={
            'config': 'GENERAL_CONFIG.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False
        assert 'Cannot delete' in data['error']

        # File should still exist
        assert os.path.exists(os.path.join(str(tmp_dir), 'GENERAL_CONFIG.ini'))

    def test_returns_404_for_nonexistent_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/delete', json={
            'config': 'NONEXISTENT.ini'
        })
        data = resp.get_json()

        assert resp.status_code == 404
        assert data['success'] is False

    def test_returns_400_on_missing_name(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.post('/api/config/delete', json={})
        data = resp.get_json()

        assert resp.status_code == 400
        assert data['success'] is False


@pytest.mark.unit
class TestListConfigs:
    """Tests for GET /api/config/list."""

    def test_returns_config_list(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.get('/api/config/list')
        data = resp.get_json()

        assert resp.status_code == 200
        assert data['success'] is True
        assert 'configs' in data
        assert 'active_config' in data

        # Should find the GENERAL_CONFIG preset
        names = [c['name'] for c in data['configs']]
        assert 'GENERAL_CONFIG.ini' in names

    def test_default_flag_set_correctly(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        # Create a non-default config
        custom = os.path.join(str(tmp_dir), 'CUSTOM.ini')
        with open(custom, 'w') as f:
            f.write('[System]\nalgorithm = exg\n')

        resp = client.get('/api/config/list')
        data = resp.get_json()

        configs_by_name = {c['name']: c for c in data['configs']}
        assert configs_by_name['GENERAL_CONFIG.ini']['is_default'] is True
        assert configs_by_name['CUSTOM.ini']['is_default'] is False


@pytest.mark.unit
class TestControllerConfig:
    """Tests for GET /api/controller_config."""

    def test_returns_controller_type(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client

        resp = client.get('/api/controller_config')
        data = resp.get_json()

        assert resp.status_code == 200
        assert 'controller_type' in data
        assert 'hardware_active' in data
        assert data['controller_type'] == 'none'
        assert data['hardware_active'] is False


# ===========================================================================
# Tracking route
# ===========================================================================

@pytest.mark.unit
class TestTrackingRoute:
    """Tests for POST /api/tracking/set."""

    def test_enable_tracking(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/tracking/set', json={'value': True})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert data['tracking_enabled'] is True
        assert sent == [('owl/commands', {'action': 'set_tracking', 'value': True})]

    def test_disable_tracking(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/tracking/set', json={'value': False})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert data['tracking_enabled'] is False
        assert sent == [('owl/commands', {'action': 'set_tracking', 'value': False})]

    def test_unsent_command_is_reported(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        dashboard.mqtt_client._send_command = lambda *a, **k: {'success': False, 'error': 'Not connected'}
        resp = client.post('/api/tracking/set', json={'value': True})
        assert resp.status_code == 503

    def test_tracking_persists_to_config(self, standalone_test_client):
        """Tracking toggle should write to INI file for reboot persistence.

        _persist_config_change uses copy-on-write for protected defaults
        (GENERAL_CONFIG.ini), so we read the active config via active_config.txt.
        """
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        client.post('/api/tracking/set', json={'value': True})

        # Follow active_config.txt to find the written file
        active_path = dashboard._get_active_config_path()
        resolved = dashboard._resolve_config_path(active_path)

        import configparser
        config = configparser.ConfigParser()
        config.read(resolved)
        assert config.has_section('Tracking')
        assert config.get('Tracking', 'tracking_enabled') == 'True'

    def test_tracking_no_mqtt_returns_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = client.post('/api/tracking/set', json={'value': True})
        assert resp.status_code == 500


@pytest.mark.unit
class TestSystemStatsTracking:
    """Tests for tracking_enabled in GET /api/system_stats response."""

    def test_stats_include_tracking_enabled(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        assert 'tracking_enabled' in data

    def test_stats_tracking_reflects_mqtt_state(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        if dashboard.mqtt_client:
            dashboard.mqtt_client.current_state['tracking_enabled'] = True
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        assert data['tracking_enabled'] is True


# ===========================================================================
# Operational routes — field-critical endpoints
# ===========================================================================

@pytest.mark.unit
class TestSensitivityRoute:
    """Tests for POST /api/sensitivity/set."""

    def test_set_sensitivity_valid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/sensitivity/set', json={'level': 'High'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert sent == [('owl/commands', {'action': 'set_sensitivity_level', 'level': 'high'})]

    def test_set_sensitivity_missing_level(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/sensitivity/set', json={})
        data = resp.get_json()
        assert resp.status_code == 400
        assert data['success'] is False

    def test_set_sensitivity_custom_level(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        # DashMQTTSubscriber.set_sensitivity_level now accepts any name
        # (validation happens on the OWL side via SensitivityManager)
        resp = client.post('/api/sensitivity/set', json={'level': 'custom_preset'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert sent == [('owl/commands', {'action': 'set_sensitivity_level',
                                          'level': 'custom_preset'})]

    def test_set_sensitivity_locked_by_advanced_controller(self, standalone_test_client):
        """Advanced controller locks sensitivity — it has a hardware switch for it."""
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        dashboard._get_controller_type = lambda: 'advanced'
        resp = client.post('/api/sensitivity/set', json={'level': 'high'})
        assert resp.status_code == 423  # Locked
        assert sent == [], 'a locked control still sent its command'

    def test_set_sensitivity_allowed_with_ute_controller(self, standalone_test_client):
        """UTE controller does NOT lock sensitivity — it only controls recording."""
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        dashboard._get_controller_type = lambda: 'ute'
        resp = client.post('/api/sensitivity/set', json={'level': 'high'})
        assert resp.status_code == 200
        assert sent == [('owl/commands', {'action': 'set_sensitivity_level', 'level': 'high'})]

    def test_set_sensitivity_broker_down_is_an_error(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard, connected=False)
        resp = client.post('/api/sensitivity/set', json={'level': 'high'})
        assert resp.status_code == 500
        assert resp.get_json() == {'success': False, 'error': 'Not connected to MQTT broker'}
        assert sent == []


@pytest.mark.unit
class TestAlgorithmRoute:
    """Tests for POST /api/algorithm/set."""

    def test_set_algorithm_valid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/algorithm/set', json={'algorithm': 'exhsv'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True

    def test_set_algorithm_invalid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.post('/api/algorithm/set', json={'algorithm': 'invalid'})
        data = resp.get_json()
        assert resp.status_code == 400
        assert data['success'] is False

    def test_set_algorithm_gog_hybrid(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        resp = client.post('/api/algorithm/set', json={'algorithm': 'gog-hybrid'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True


NO_AI = {'cpu': {'installed': False},
         'hailo': {'present': False, 'chip': None, 'runtime': False},
         'accelerator': None}
CPU_AI = dict(NO_AI, cpu={'installed': True}, accelerator='cpu')


def _owl_reports(dashboard, **state):
    """The OWL's last published state, as the dashboard's subscriber holds it."""
    sub = dashboard.mqtt_client
    with sub.state_lock:
        sub.current_state = dict(state)
        sub.last_heartbeat = time.time() + 3600  # never goes silent mid-test


def _saved_algorithm(tmp_dir):
    active = (tmp_dir / 'active_config.txt').read_text().strip()
    config = configparser.ConfigParser()
    config.read(str(tmp_dir / active.split('/')[-1]))
    return config.get('System', 'algorithm')


@pytest.mark.unit
class TestAlgorithmRouteHonesty:
    """/api/algorithm/set refuses what the OWL cannot run, accepts lut, and
    saves only once the OWL reports the new detector running."""

    @pytest.fixture(autouse=True)
    def fast_confirm(self, monkeypatch):
        import controller.standalone.standalone as sa_mod
        monkeypatch.setattr(sa_mod, 'ALGORITHM_CONFIRM_POLL_S', 0.01)
        monkeypatch.setattr(sa_mod, 'ALGORITHM_CONFIRM_TIMEOUT_S', 0.5)

    @pytest.mark.parametrize('algorithm', ['gog', 'gog-hybrid'])
    def test_ai_refused_when_the_owl_has_no_ai(self, standalone_test_client, algorithm):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        _owl_reports(dashboard, algorithm='exhsv', ai=NO_AI)
        dashboard.mqtt_client.client.publish.reset_mock()

        resp = client.post('/api/algorithm/set', json={'algorithm': algorithm})

        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert 'not installed' in data['error']
        dashboard.mqtt_client.client.publish.assert_not_called()

    def test_ai_allowed_with_cpu_ai(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        _owl_reports(dashboard, algorithm='exhsv', ai=CPU_AI)
        resp = client.post('/api/algorithm/set', json={'algorithm': 'gog'})
        assert resp.status_code == 200

    def test_lut_accepted_with_a_profile(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        _owl_reports(dashboard, algorithm='exhsv', lut_profile='paddock_a')
        resp = client.post('/api/algorithm/set', json={'algorithm': 'lut'})
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

    def test_lut_refused_without_a_profile(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        _owl_reports(dashboard, algorithm='exhsv', lut_profile='')
        resp = client.post('/api/algorithm/set', json={'algorithm': 'lut'})
        assert resp.status_code == 400
        assert 'profile' in resp.get_json()['error']

    def test_not_saved_when_the_owl_never_runs_it(self, standalone_test_client):
        """The owl-1 case: AI requested, load fails, OWL stays on exhsv."""
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        before = _saved_algorithm(tmp_dir)
        _owl_reports(dashboard, algorithm='exhsv', ai=CPU_AI,
                     algorithm_error='ultralytics is required')

        assert client.post('/api/algorithm/set', json={'algorithm': 'gog'}).status_code == 200
        time.sleep(0.8)  # past the confirm timeout

        assert _saved_algorithm(tmp_dir) == before

    def test_saved_once_the_owl_reports_it_running(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        _owl_reports(dashboard, algorithm='exhsv', algorithm_pending='hsv')

        assert client.post('/api/algorithm/set', json={'algorithm': 'hsv'}).status_code == 200
        time.sleep(0.05)
        assert _saved_algorithm(tmp_dir) != 'hsv', 'still loading: nothing saved yet'

        _owl_reports(dashboard, algorithm='hsv', algorithm_pending=None)
        deadline = time.time() + 2
        while _saved_algorithm(tmp_dir) != 'hsv' and time.time() < deadline:
            time.sleep(0.02)
        assert _saved_algorithm(tmp_dir) == 'hsv'


@pytest.mark.unit
class TestSystemStatsAiFields:
    def test_relays_the_owl_ai_state(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        download = {'status': 'error', 'model_name': 'weeds.zip', 'progress': 0,
                    'error': 'Checksum mismatch'}
        _owl_reports(dashboard, algorithm='exhsv', algorithm_pending='gog', ai=CPU_AI,
                     model_download=download, algorithm_error='ultralytics is required')
        data = client.get('/api/system_stats').get_json()
        assert data['ai'] == CPU_AI
        assert data['model_download'] == download
        assert data['algorithm_error'] == 'ultralytics is required'
        assert data['algorithm_pending'] == 'gog'
        assert data['algorithm'] == 'exhsv'


# Every button that can open or close a nozzle, and the one command it must
# put on the wire. The 2026-11-16 audit changed "all off" to send blanket and
# "stop" to send start: both passed, because the tests here read only the
# HTTP status. tests/owl/test_dashboard_to_relay.py follows the same buttons
# to the relay of a real Owl.
SPRAY_BUTTONS = [
    ('/api/detection/start', None, {'action': 'set_detection_enable', 'value': True}),
    ('/api/detection/stop', None, {'action': 'set_detection_enable', 'value': False}),
    ('/api/nozzles/all-on', None, {'action': 'set_detection_mode', 'value': 2}),
    ('/api/nozzles/all-off', None, {'action': 'set_detection_mode', 'value': 1}),
    ('/api/nozzles/hold', {'ms': 1500}, {'action': 'hold_spray', 'value': 1500}),
    ('/api/nozzles/hold', {'ms': 0}, {'action': 'hold_spray', 'value': 0}),
    ('/api/nozzles/hold', {}, {'action': 'hold_spray', 'value': 0}),
    # the lease is bounded on the way out: never longer than 1.5 s
    ('/api/nozzles/hold', {'ms': 99999}, {'action': 'hold_spray', 'value': 1500}),
    ('/api/nozzles/hold', {'ms': -5}, {'action': 'hold_spray', 'value': 0}),
    ('/api/nozzles/test', {'relays': [3], 'duration_ms': 1000},
     {'action': 'test_nozzles', 'value': {'relays': [3], 'duration_ms': 1000}}),
    ('/api/nozzles/test', {'relays': [0, 2]},
     {'action': 'test_nozzles', 'value': {'relays': [0, 2], 'duration_ms': 10000}}),
    # an empty list is how the page stops a running test
    ('/api/nozzles/test', {'relays': []},
     {'action': 'test_nozzles', 'value': {'relays': [], 'duration_ms': 10000}}),
]
# one press of each of the six routes
SPRAY_ROUTES = [SPRAY_BUTTONS[i] for i in (0, 1, 2, 3, 4, 9)]


def _press(client, path, body):
    return client.post(path, json=body) if body is not None else client.post(path)


@pytest.mark.unit
class TestSprayButtons:
    """/api/detection/start|stop and /api/nozzles/all-on|all-off|hold|test."""

    @pytest.mark.parametrize('path, body, command', SPRAY_BUTTONS)
    def test_button_sends_exactly_its_command(self, standalone_test_client, path, body, command):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = _press(client, path, body)
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        assert sent == [('owl/commands', command)]

    @pytest.mark.parametrize('controller', ['advanced', 'ute'])
    @pytest.mark.parametrize('path, body, command', SPRAY_ROUTES)
    def test_a_switch_panel_does_not_change_what_is_sent(self, standalone_test_client,
                                                         controller, path, body, command):
        """v3.27: the OWL arbitrates (switch OFF wins; while it is ON the
        dashboard chooses spot, blanket or off). A 423 here made a Ute
        switch that was ON unusable from the dashboard. What the OWL then
        does with the command: tests/owl/test_relay_matrix.py."""
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        dashboard._get_controller_type = lambda: controller
        dashboard._get_switch_purpose = lambda: 'detection'
        resp = _press(client, path, body)
        assert resp.status_code == 200
        assert sent == [('owl/commands', command)]

    @pytest.mark.parametrize('path, body, command', SPRAY_ROUTES)
    def test_broker_down_is_503_and_nothing_is_sent(self, standalone_test_client,
                                                    path, body, command):
        """The page must not show a button as pressed when the command never
        left: an unchecked result used to come back as HTTP 200."""
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard, connected=False)
        resp = _press(client, path, body)
        assert resp.status_code == 503
        assert resp.get_json() == {'success': False, 'error': 'Not connected to MQTT broker'}
        assert sent == []

    @pytest.mark.parametrize('path, body, command', SPRAY_ROUTES)
    def test_a_publish_the_broker_refuses_is_503(self, standalone_test_client,
                                                 path, body, command):
        client, dashboard, tmp_dir = standalone_test_client
        _published(dashboard)
        dashboard.mqtt_client.client.publish = MagicMock(side_effect=OSError('socket closed'))
        resp = _press(client, path, body)
        assert resp.status_code == 503
        assert resp.get_json() == {'success': False, 'error': 'socket closed'}

    @pytest.mark.parametrize('path, body, command', SPRAY_ROUTES)
    def test_no_mqtt_client_is_500(self, standalone_test_client, path, body, command):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = _press(client, path, body)
        assert resp.status_code == 500
        assert resp.get_json() == {'success': False, 'error': 'MQTT not connected'}

    @pytest.mark.parametrize('path, body', [
        ('/api/nozzles/test', {'relays': '3'}),
        ('/api/nozzles/test', {'relays': ['3']}),
        ('/api/nozzles/test', {'relays': [True]}),
        ('/api/nozzles/test', {'relays': [1.5]}),
        ('/api/nozzles/test', {'relays': [1], 'duration_ms': 'soon'}),
        ('/api/nozzles/hold', {'ms': 'soon'}),
        ('/api/nozzles/hold', {'ms': [1500]}),
    ])
    def test_bad_input_is_400_and_nothing_is_sent(self, standalone_test_client, path, body):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post(path, json=body)
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert sent == []


@pytest.mark.unit
class TestRecordingRoutes:
    """Tests for /api/recording/start and /api/recording/stop."""

    @pytest.mark.parametrize('path, value', [('/api/recording/start', True),
                                             ('/api/recording/stop', False)])
    def test_recording_button_sends_its_command(self, standalone_test_client, path, value):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post(path)
        assert resp.status_code == 200
        assert sent == [('owl/commands', {'action': 'set_image_sample_enable', 'value': value})]

    @pytest.mark.parametrize('controller, purpose, locked', [
        ('advanced', 'recording', True),
        ('ute', 'recording', True),
        ('ute', 'detection', False),     # the Ute's one switch is the spray switch
    ])
    @pytest.mark.parametrize('path', ['/api/recording/start', '/api/recording/stop'])
    def test_recording_belongs_to_the_switch_that_owns_it(self, standalone_test_client,
                                                          path, controller, purpose, locked):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        dashboard._get_controller_type = lambda: controller
        dashboard._get_switch_purpose = lambda: purpose
        resp = client.post(path)
        if locked:
            assert resp.status_code == 423
            assert sent == [], 'a locked control still sent its command'
        else:
            assert resp.status_code == 200
            assert len(sent) == 1

    def test_recording_broker_down_is_503(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard, connected=False)
        assert client.post('/api/recording/start').status_code == 503
        assert sent == []


@pytest.mark.unit
class TestConfigParamRoutes:
    """Tests for config slider routes."""

    def test_set_config_param_reaches_the_owl_and_the_ini(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/config/param', json={'param': 'exg_min', 'value': 42})
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        assert sent == [('owl/commands', {'action': 'set_config', 'key': 'exg_min', 'value': 42})]
        assert _active_ini(dashboard).get('GreenOnBrown', 'exg_min') == '42'

    def _capture(self, dashboard):
        sent = []
        dashboard.mqtt_client._send_command = (
            lambda action, **kw: sent.append(dict(kw, action=action)) or {'success': True})
        dashboard._persist_config_change = lambda section, key, value: None
        return sent

    def test_widget_action_is_forwarded(self, standalone_test_client):
        """Widget buttons send {action, ...}; standalone answered 400."""
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        sent = self._capture(dashboard)
        resp = client.post('/api/config/param', json={'action': 'set_sensitivity', 'value': 'High'})
        assert resp.status_code == 200
        assert sent == [{'action': 'set_sensitivity_level', 'level': 'high'}]

    def test_widget_toggle_on_another_section(self, standalone_test_client):
        """Toggle widgets send 'true'/'false'; int('true') was a 500."""
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        sent = self._capture(dashboard)
        resp = client.post('/api/config/param',
                           json={'section': 'Tracking', 'param': 'tracking_enabled', 'value': 'true'})
        assert resp.status_code == 200
        assert sent == [{'action': 'set_config_section', 'section': 'Tracking',
                         'params': {'tracking_enabled': 'true'}}]

    def test_nozzle_distance_is_sent_to_the_owl_and_saved(self, standalone_test_client):
        """Test nozzles panel: [Actuation] offset_cm through /api/config/section."""
        import configparser
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        sent = []
        dashboard.mqtt_client._send_command = (
            lambda action, **kw: sent.append(dict(kw, action=action)) or {'success': True})
        resp = client.post('/api/config/section',
                           json={'section': 'Actuation', 'params': {'offset_cm': 45}})
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        assert sent == [{'action': 'set_config_section', 'section': 'Actuation',
                         'params': {'offset_cm': '45'}}]
        active = (tmp_dir / 'active_config.txt').read_text().strip()
        saved = configparser.ConfigParser()
        saved.read(tmp_dir / os.path.basename(active))
        assert saved.get('Actuation', 'offset_cm') == '45'

    @pytest.mark.parametrize('value, sent_value', [(-3, '0'), (900, '200')])
    def test_nozzle_distance_is_clamped_before_it_is_sent(self, standalone_test_client,
                                                          value, sent_value):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        sent = self._capture(dashboard)
        client.post('/api/config/section', json={'section': 'Actuation', 'params': {'offset_cm': value}})
        assert sent[0]['params'] == {'offset_cm': sent_value}

    def test_nozzle_distance_that_is_not_a_number_is_refused(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        sent = self._capture(dashboard)
        resp = client.post('/api/config/section',
                           json={'section': 'Actuation', 'params': {'offset_cm': 'far'}})
        assert resp.status_code == 400
        assert sent == []

    def test_system_stats_carry_nozzle_distance_state(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        state = dict(dashboard.mqtt_client.get_state(),
                     offset_cm=30.0, actuation_length_cm=10.0, offset_configured=False)
        with patch.object(dashboard.mqtt_client, 'get_state', return_value=state):
            data = client.get('/api/system_stats').get_json()
        assert (data['offset_cm'], data['actuation_length_cm'], data['offset_configured']) == (30.0, 10.0, False)

    def test_set_config_param_missing_fields(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/config/param', json={'param': 'exg_min'})
        assert resp.status_code == 400
        assert sent == []

    def test_float_param_survives_round_trip(self, standalone_test_client):
        """min_detection_area_percent is a small float (e.g. 0.0005); the
        route used int(value), truncating it to 0 live AND persisting "0"
        (field bug, R1 Phase 2). Both the MQTT command and the persisted
        string must carry the float."""
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        sent = {}
        persisted = {}
        dashboard.mqtt_client._send_command = (
            lambda action, **kw: (sent.update({'action': action}, **kw) or {'success': True}))
        dashboard._persist_config_change = (
            lambda section, key, value: persisted.update({section: (key, value)}))

        resp = client.post('/api/config/param',
                           json={'param': 'min_detection_area_percent', 'value': 0.0005})

        assert resp.status_code == 200
        assert sent['value'] == pytest.approx(0.0005)
        assert isinstance(sent['value'], float)
        assert persisted['GreenOnBrown'] == ('min_detection_area_percent', '0.0005')

    def test_int_param_stays_int(self, standalone_test_client):
        """The 0-255 threshold keys keep integer parsing."""
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        sent = {}
        dashboard.mqtt_client._send_command = (
            lambda action, **kw: (sent.update({'action': action}, **kw) or {'success': True}))
        dashboard._persist_config_change = lambda section, key, value: None

        resp = client.post('/api/config/param', json={'param': 'exg_min', 'value': 42})

        assert resp.status_code == 200
        assert sent['value'] == 42
        assert isinstance(sent['value'], int)

    def test_set_crop_buffer(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/config/crop_buffer', json={'value': 25})
        assert resp.status_code == 200
        assert sent == [('owl/commands', {'action': 'set_crop_buffer', 'value': 25})]

    def test_set_confidence(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/config/confidence', json={'value': 0.75})
        assert resp.status_code == 200
        assert sent == [('owl/commands', {'action': 'set_greenongreen_param',
                                          'key': 'confidence', 'value': 0.75})]
        assert _active_ini(dashboard).get('GreenOnGreen', 'confidence') == '0.75'


@pytest.mark.unit
class TestAIRoutes:
    """Tests for AI model routes."""

    def test_set_ai_model(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/ai/set_model', json={'model': 'yolo26n-seg.pt'})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert sent == [('owl/commands', {'action': 'set_model', 'value': 'yolo26n-seg.pt'})]

    def test_set_ai_model_missing(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/ai/set_model', json={})
        assert resp.status_code == 400
        assert sent == []

    def test_set_detect_classes(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/ai/set_detect_classes', json={'classes': ['weed', 'crop']})
        assert resp.status_code == 200
        assert sent == [('owl/commands', {'action': 'set_detect_classes',
                                          'value': ['weed', 'crop']})]


@pytest.mark.unit
class TestSystemStats:
    """Tests for GET /api/system_stats."""

    def test_returns_stats(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/system_stats')
        data = resp.get_json()

        assert resp.status_code == 200
        assert 'cpu_percent' in data
        assert 'memory_percent' in data
        assert 'owl_running' in data
        assert 'detection_enable' in data
        # App checks this on dashboard connect — must never silently disappear
        assert isinstance(data['contract_version'], int)
        # The phone app dedupes saved OWLs on these — additive, but the app
        # falls back to weaker password matching if they disappear.
        # device_serial may be None off-hardware (no /proc/cpuinfo or
        # /etc/machine-id), so assert presence, not value.
        import socket
        assert data['hostname'] == socket.gethostname()
        assert 'device_serial' in data

    def test_passes_through_recording_storage_fields(self, standalone_test_client):
        """v3.11.1: the app explains recording snap-backs and internal
        fallback from these — they must survive the stats merge."""
        client, dashboard, tmp_dir = standalone_test_client
        from unittest.mock import MagicMock as _MM
        dashboard.mqtt_client = _MM()
        dashboard.mqtt_client.get_state.return_value = {
            'recording_location': 'internal',
            'storage_fallback': True,
            'recording_blocked_reason': None,
        }
        dashboard.mqtt_client.get_weed_detect_indicator.return_value = False
        dashboard.mqtt_client.get_image_write_indicator.return_value = False
        dashboard.mqtt_client.get_cloud_connected.return_value = None
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        assert data['recording_location'] == 'internal'
        assert data['storage_fallback'] is True
        assert data['recording_blocked_reason'] is None

    def _stats_with_state(self, dashboard, state):
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client.get_state.return_value = state
        dashboard.mqtt_client.get_weed_detect_indicator.return_value = False
        dashboard.mqtt_client.get_image_write_indicator.return_value = False
        dashboard.mqtt_client.get_cloud_connected.return_value = None

    def test_gps_speed_and_source_pass_through(self, standalone_test_client):
        """The app's Speed cell shows gps_speed_kmh (any source, display
        only) and where it comes from. Spray-timing speed_kmh stays apart."""
        client, dashboard, tmp_dir = standalone_test_client
        self._stats_with_state(dashboard, {'gps_fix': True, 'gps_source': 'phone',
                                           'gps_speed_kmh': 7.5, 'speed_kmh': None})
        data = client.get('/api/system_stats').get_json()
        assert data['gps_speed_kmh'] == 7.5
        assert data['gps_source'] == 'phone'
        assert data['speed_kmh'] is None

    def test_gps_speed_hidden_without_fix(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        self._stats_with_state(dashboard, {'gps_fix': False, 'gps_speed_kmh': 7.5})
        assert client.get('/api/system_stats').get_json()['gps_speed_kmh'] is None

    def test_hung_helpers_do_not_hang_the_poll(self, standalone_test_client):
        """A hung lsusb/vcgencmd (USB drive dropping off the bus) held a
        gunicorn thread per 2 s poll until every route stopped answering."""
        import subprocess as sp
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.lsusb_path = '/usr/bin/lsusb'
        dashboard.vcgencmd_path = '/usr/bin/vcgencmd'
        # systemctl is-active also goes through subprocess.run where systemctl
        # exists (Linux CI, not Windows): count only the two helpers
        dashboard.get_owl_service_state = lambda: {'active': True, 'status': 'active'}
        with patch('controller.standalone.standalone.subprocess.run',
                   side_effect=sp.TimeoutExpired('lsusb', 2)) as run:
            resp = client.get('/api/system_stats')
        assert resp.status_code == 200
        assert resp.get_json()['usb_devices'] == []
        assert run.call_count == 2
        assert all(c.kwargs.get('timeout') == 2 for c in run.call_args_list)

    def test_includes_resolution(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/system_stats')
        data = resp.get_json()
        assert 'resolution_width' in data
        assert 'resolution_height' in data

    def test_resolution_from_mqtt_state(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _stabilize_mqtt(dashboard)
        # Patch get_state on the mqtt_client to return a controlled dict.
        # dashboard.mqtt_client may be a real DashMQTTSubscriber (importlib.reload
        # re-binds the name after the patch context exits), so we cannot use
        # .return_value directly on a bound method — use patch.object instead.
        mqtt_state = {
            'resolution_width': 1456,
            'resolution_height': 1088,
            'owl_running': True,
        }
        if dashboard.mqtt_client:
            with patch.object(dashboard.mqtt_client, 'get_state', return_value=mqtt_state):
                resp = client.get('/api/system_stats')
                data = resp.get_json()
        else:
            # No mqtt_client: resolution falls back to defaults (0)
            resp = client.get('/api/system_stats')
            data = resp.get_json()
        assert data['resolution_width'] == 1456
        assert data['resolution_height'] == 1088


@pytest.mark.unit
class TestOWLServiceRoutes:
    """/api/owl/start and /api/owl/stop: the systemctl commands they run.
    subprocess.run is the stand-in; control_owl_service is real."""

    def _systemctl(self, dashboard, returncode=0, stderr=''):
        from types import SimpleNamespace
        ran = []
        dashboard._get_systemctl_path = lambda: '/bin/sh'     # any file that exists

        def run(cmd, **kwargs):
            ran.append(cmd)
            return SimpleNamespace(returncode=returncode, stdout='', stderr=stderr)

        return ran, patch('controller.standalone.standalone.subprocess.run', side_effect=run)

    def test_start_clears_a_failed_state_then_starts_the_service(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        ran, patched = self._systemctl(dashboard)
        with patched:
            resp = client.post('/api/owl/start')
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        assert ran == [['sudo', '/bin/sh', 'reset-failed', 'owl.service'],
                       ['sudo', '/bin/sh', 'start', 'owl.service']]

    def test_stop_stops_the_service(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        ran, patched = self._systemctl(dashboard)
        with patched:
            resp = client.post('/api/owl/stop')
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        assert ran == [['sudo', '/bin/sh', 'stop', 'owl.service']]

    @pytest.mark.parametrize('path', ['/api/owl/start', '/api/owl/stop'])
    def test_systemctl_failure_is_500_with_its_message(self, standalone_test_client, path):
        client, dashboard, tmp_dir = standalone_test_client
        ran, patched = self._systemctl(dashboard, returncode=1, stderr='Unit owl.service not found.')
        with patched:
            resp = client.post(path)
        assert resp.status_code == 500
        assert resp.get_json() == {'success': False, 'error': 'Unit owl.service not found.'}

    def test_missing_systemctl_is_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard._get_systemctl_path = lambda: str(tmp_dir / 'no-systemctl')
        resp = client.post('/api/owl/start')
        assert resp.status_code == 500
        assert 'systemctl executable not found' in resp.get_json()['error']


# ===========================================================================
# GPS, error polling, frame proxy, video feed, directories
# ===========================================================================

@pytest.mark.unit
class TestUpdateGPS:
    """Tests for POST /api/update_gps."""

    def test_no_mqtt_returns_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = client.post('/api/update_gps', json={
            'latitude': 0, 'longitude': 0, 'accuracy': 0
        })
        assert resp.status_code == 500

    # ---- full pass-through (phone GPS -> EXIF, 2026-08-18) ----

    def _capture_publish(self, dashboard):
        """MagicMock the paho publish so tests can read the payload sent
        to owl/gps (the fixture's DashMQTTSubscriber is real)."""
        _stabilize_mqtt(dashboard)
        dashboard.mqtt_client.client.publish = MagicMock()
        return dashboard.mqtt_client.client.publish

    def _payload(self, publish_mock):
        args, _ = publish_mock.call_args
        return json.loads(args[1])

    def test_missing_coordinates_is_error(self, standalone_test_client):
        """No more silent lat/lon 0.0 defaults — coordinates are required
        (a 0,0 position written into EXIF is worse than none)."""
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        resp = client.post('/api/update_gps', json={})
        data = resp.get_json()
        assert data['success'] is False
        publish.assert_not_called()

    def test_garbage_coordinates_is_error(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        self._capture_publish(dashboard)
        resp = client.post('/api/update_gps', json={
            'latitude': 'nope', 'longitude': 151.2})
        assert resp.get_json()['success'] is False

    def test_known_source_passes_through(self, standalone_test_client):
        """The phone app says it is the phone, so the OWL can show
        "Phone GPS" instead of a generic pushed position."""
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        client.post('/api/update_gps', json={
            'latitude': -31.5, 'longitude': 150.25, 'source': 'phone'})
        assert self._payload(publish)['source'] == 'phone'

    @pytest.mark.parametrize('source', ['serial', 'controller', '', 7, None])
    def test_unknown_source_is_dropped(self, standalone_test_client, source):
        """A push can never claim to be the OWL's own receiver."""
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        client.post('/api/update_gps', json={
            'latitude': -31.5, 'longitude': 150.25, 'source': source})
        assert 'source' not in self._payload(publish)

    def test_speed_ms_converted_to_kmh(self, standalone_test_client):
        """Browser/Android speed is m/s; the EXIF pipeline speaks km/h."""
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        client.post('/api/update_gps', json={
            'latitude': -31.5, 'longitude': 150.25, 'speed': 2.7})
        payload = self._payload(publish)
        assert payload['speed_kmh'] == pytest.approx(9.72)
        assert 'speed' not in payload

    def test_pre_converted_speed_kmh_accepted(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        client.post('/api/update_gps', json={
            'latitude': -31.5, 'longitude': 150.25, 'speed_kmh': 12.0})
        assert self._payload(publish)['speed_kmh'] == pytest.approx(12.0)

    def test_optional_fields_pass_through(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        client.post('/api/update_gps', json={
            'latitude': -31.5, 'longitude': 150.25, 'accuracy': 4.2,
            'altitude': 12.3, 'heading': 184.0, 'timestamp': 1755400000.1})
        payload = self._payload(publish)
        assert payload['altitude'] == pytest.approx(12.3)
        assert payload['heading'] == pytest.approx(184.0)
        assert payload['accuracy'] == pytest.approx(4.2)
        assert payload['timestamp'] == pytest.approx(1755400000.1)

    def test_absent_optional_fields_omitted(self, standalone_test_client):
        """Fail-safe metadata: absent means absent, never a default."""
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        client.post('/api/update_gps', json={
            'latitude': -31.5, 'longitude': 150.25})
        payload = self._payload(publish)
        for key in ('accuracy', 'altitude', 'heading', 'speed_kmh'):
            assert key not in payload

    def test_garbage_optional_field_dropped_not_fatal(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        publish = self._capture_publish(dashboard)
        resp = client.post('/api/update_gps', json={
            'latitude': -31.5, 'longitude': 150.25,
            'heading': 'north', 'speed': 'fast'})
        assert resp.get_json()['success'] is True
        payload = self._payload(publish)
        assert 'heading' not in payload
        assert 'speed_kmh' not in payload


@pytest.mark.unit
class TestGetErrors:
    """Tests for GET /api/get_errors."""

    def test_returns_empty_when_no_errors(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/get_errors')
        data = resp.get_json()
        assert isinstance(data, list)
        assert len(data) == 0

    def test_returns_empty_when_no_mqtt(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = client.get('/api/get_errors')
        data = resp.get_json()
        assert data == []


@pytest.mark.unit
class TestDownloadFrame:
    """Tests for POST /api/download_frame."""

    def test_returns_503_when_owl_not_running(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        # No OWL running = urllib can't connect to port 8001
        with patch('urllib.request.urlopen', side_effect=Exception("Connection refused")):
            resp = client.post('/api/download_frame')
            assert resp.status_code == 500

    def test_returns_jpeg_on_success(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        mock_response = MagicMock()
        mock_response.read.return_value = b'\xff\xd8\xff\xe0fake_jpeg'
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch('urllib.request.urlopen', return_value=mock_response):
            resp = client.post('/api/download_frame')
            assert resp.status_code == 200
            assert resp.content_type == 'image/jpeg'
            assert b'\xff\xd8' in resp.data


@pytest.mark.unit
class TestVideoFeed:
    """Tests for GET /video_feed."""

    def test_returns_multipart_content_type(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        import urllib.error
        # Even if stream fails, the response should have correct MIME type
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("no stream")):
            resp = client.get('/video_feed')
            assert 'multipart/x-mixed-replace' in resp.content_type

    def test_streams_data_from_owl(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        mock_response = MagicMock()
        # Simulate two chunks then EOF
        mock_response.read.side_effect = [b'chunk1', b'chunk2', b'']
        with patch('urllib.request.urlopen', return_value=mock_response):
            resp = client.get('/video_feed')
            assert resp.status_code == 200
            assert resp.data == b'chunk1chunk2'


@pytest.mark.unit
class TestConfigDirectories:
    """Tests for GET /api/config/directories."""

    def test_returns_directory_list(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = client.get('/api/config/directories')
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert 'directories' in data
        assert isinstance(data['directories'], list)


@pytest.mark.unit
class TestApplyConfigPushesCameraSection:
    """_apply_config_to_owl must forward [Camera] WB/exposure keys as one
    set_config_section command so a kiosk config switch/save hot-applies
    through mqtt_manager's CAMERA_LIVE_KEYS branch."""

    def _camera_call(self, dashboard):
        calls = [c for c in dashboard.mqtt_client._send_command.call_args_list
                 if c.args and c.args[0] == 'set_config_section'
                 and c.kwargs.get('section') == 'Camera']
        return calls[0] if calls else None

    def test_exp_compensation_pushed_from_active_config(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard._apply_config_to_owl()
        cam = self._camera_call(dashboard)
        assert cam is not None, "no Camera set_config_section command sent"
        assert cam.kwargs['params']['exp_compensation'] == -2

    def test_awb_keys_pushed_when_present(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        # Add WB keys to the active config file
        active = tmp_dir / 'GENERAL_CONFIG.ini'
        cfg = configparser.ConfigParser()
        cfg.read(active)
        cfg.set('Camera', 'awb_mode', 'manual')
        cfg.set('Camera', 'awb_red_gain', '1.4')
        cfg.set('Camera', 'awb_blue_gain', '3.2')
        with open(active, 'w') as handle:
            cfg.write(handle)

        dashboard.mqtt_client = MagicMock()
        dashboard._apply_config_to_owl()
        cam = self._camera_call(dashboard)
        assert cam is not None
        params = cam.kwargs['params']
        assert params['awb_mode'] == 'manual'
        assert params['awb_red_gain'] == 1.4
        assert params['awb_blue_gain'] == 3.2

    def test_switching_config_pushes_algorithm_and_thresholds_to_the_running_owl(
            self, standalone_test_client):
        """Without this the page shows the new profile while the OWL keeps
        detecting with the old one until it restarts."""
        client, dashboard, tmp_dir = standalone_test_client
        profile = configparser.ConfigParser()
        profile.read(tmp_dir / 'GENERAL_CONFIG.ini')
        profile.set('System', 'algorithm', 'hsv')
        profile.set('GreenOnBrown', 'exg_min', '31')
        profile.set('GreenOnBrown', 'hue_max', '77')
        profile.set('GreenOnBrown', 'min_detection_area_percent', '0.0005')
        if not profile.has_section('GreenOnGreen'):
            profile.add_section('GreenOnGreen')
        profile.set('GreenOnGreen', 'confidence', '0.6')
        with open(tmp_dir / 'PADDOCK.ini', 'w') as handle:
            profile.write(handle)

        sent = _published(dashboard)
        resp = client.post('/api/config/set-active', json={'config': 'PADDOCK.ini'})
        assert resp.status_code == 200 and resp.get_json()['active_config'] == 'config/PADDOCK.ini'
        commands = [command for topic, command in sent if topic == 'owl/commands']
        assert commands[0] == {'action': 'set_algorithm', 'value': 'hsv'}
        for expected in ({'action': 'set_config', 'key': 'exg_min', 'value': 31},
                         {'action': 'set_config', 'key': 'hue_max', 'value': 77},
                         {'action': 'set_config', 'key': 'min_detection_area_percent',
                          'value': 0.0005},
                         {'action': 'set_greenongreen_param', 'key': 'confidence', 'value': 0.6}):
            assert expected in commands


@pytest.mark.unit
class TestTimeSync:
    """POST /api/time/sync - phone-sourced clock correction for RTC-less units."""

    def _post(self, client, payload):
        return client.post('/api/time/sync', json=payload)

    def test_missing_epoch_rejected(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        resp = self._post(client, {})
        assert resp.status_code == 400

    def test_implausible_epoch_rejected(self, standalone_test_client):
        """A garbage timestamp must never be allowed to hurl the clock."""
        client, dashboard, tmp_dir = standalone_test_client
        resp = self._post(client, {'epoch_ms': 123456})
        assert resp.status_code == 400

    def test_small_drift_is_noop(self, standalone_test_client):
        import time as _time
        client, dashboard, tmp_dir = standalone_test_client
        with patch.object(dashboard, '_ntp_synchronized', return_value=False), \
                patch('controller.standalone.standalone.subprocess.run') as run:
            resp = self._post(client, {'epoch_ms': _time.time() * 1000})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['success'] is True
        assert data['adjusted'] is False
        run.assert_not_called()

    def test_ntp_synced_never_touches_clock(self, standalone_test_client):
        """A live NTP sync is authoritative - phone drift is reported only."""
        import time as _time
        client, dashboard, tmp_dir = standalone_test_client
        with patch.object(dashboard, '_ntp_synchronized', return_value=True), \
                patch('controller.standalone.standalone.subprocess.run') as run:
            resp = self._post(client, {'epoch_ms': (_time.time() + 3600) * 1000})
        data = resp.get_json()
        assert data['adjusted'] is False
        assert data['ntp_synced'] is True
        run.assert_not_called()

    def test_large_drift_steps_clock(self, standalone_test_client):
        import time as _time
        client, dashboard, tmp_dir = standalone_test_client
        ok = MagicMock(returncode=0, stderr='')
        with patch.object(dashboard, '_ntp_synchronized', return_value=False), \
                patch('controller.standalone.standalone.subprocess.run',
                      return_value=ok) as run:
            resp = self._post(client, {'epoch_ms': (_time.time() + 3600) * 1000})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data['adjusted'] is True
        cmd = run.call_args[0][0]
        assert cmd[0] == 'sudo' and '-u' in cmd and '-s' in cmd

    def test_sudo_denied_returns_503_with_hint(self, standalone_test_client):
        """Old installs without the date sudo rule must say so, not 500."""
        import time as _time
        client, dashboard, tmp_dir = standalone_test_client
        denied = MagicMock(returncode=1, stderr='sudo: a password is required')
        with patch.object(dashboard, '_ntp_synchronized', return_value=False), \
                patch('controller.standalone.standalone.subprocess.run',
                      return_value=denied):
            resp = self._post(client, {'epoch_ms': (_time.time() + 3600) * 1000})
        assert resp.status_code == 503
        assert 'setup.sh' in resp.get_json()['error']

    def test_timezone_set_when_valid_and_different(self, standalone_test_client):
        import time as _time
        client, dashboard, tmp_dir = standalone_test_client
        ok = MagicMock(returncode=0, stderr='')
        with patch.object(dashboard, '_ntp_synchronized', return_value=True), \
                patch.object(dashboard, '_current_timezone', return_value='Etc/UTC'), \
                patch('controller.standalone.standalone.os.path.isfile',
                      return_value=True), \
                patch('controller.standalone.standalone.subprocess.run',
                      return_value=ok) as run:
            resp = self._post(client, {'epoch_ms': _time.time() * 1000,
                                       'timezone': 'Australia/Perth'})
        data = resp.get_json()
        assert data['timezone_set'] is True
        cmd = run.call_args[0][0]
        assert 'set-timezone' in cmd and 'Australia/Perth' in cmd

    def test_traversal_timezone_ignored(self, standalone_test_client):
        import time as _time
        client, dashboard, tmp_dir = standalone_test_client
        with patch.object(dashboard, '_ntp_synchronized', return_value=True), \
                patch('controller.standalone.standalone.subprocess.run') as run:
            resp = self._post(client, {'epoch_ms': _time.time() * 1000,
                                       'timezone': '../../etc/passwd'})
        assert resp.get_json()['timezone_set'] is False
        run.assert_not_called()


@pytest.mark.unit
class TestCalibrateAwbRoute:
    """POST /api/camera/calibrate-awb -> MQTT calibrate_awb; progress and
    live WB flow back through /api/system_stats (camera, awb_calibration)."""

    def test_sends_calibrate_command(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client._send_command.return_value = {'success': True}
        resp = client.post('/api/camera/calibrate-awb')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        dashboard.mqtt_client._send_command.assert_called_once_with('calibrate_awb')

    def test_mqtt_send_failure_is_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client._send_command.return_value = {
            'success': False, 'error': 'Not connected to MQTT broker'}
        resp = client.post('/api/camera/calibrate-awb')
        assert resp.status_code == 500
        assert resp.get_json()['success'] is False

    def test_no_mqtt_client_is_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        resp = client.post('/api/camera/calibrate-awb')
        assert resp.status_code == 500

    def test_system_stats_exposes_camera_and_calibration(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        if not dashboard.mqtt_client:
            pytest.skip('fixture has no MQTT subscriber')
        dashboard.mqtt_client.current_state['camera'] = {
            'awb_mode': 'auto', 'colour_gains': [1.4, 3.2],
            'colour_temperature': 5200.0}
        dashboard.mqtt_client.current_state['awb_calibration'] = {
            'status': 'complete', 'red_gain': 1.4, 'blue_gain': 3.2,
            'updated_at': 1.0}
        resp = client.get('/api/system_stats')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['camera']['colour_gains'] == [1.4, 3.2]
        assert data['awb_calibration']['status'] == 'complete'


@pytest.mark.unit
class TestConfigSectionCameraCoercion:
    """POST /api/config/section clamps/validates live camera keys with the
    same rule the OWL applies before persisting them. A raw value written to
    the INI that the OWL only clamped in memory (awb_red_gain = 50) would
    fail load_and_validate_config on the next boot."""

    def _post(self, client, params):
        return client.post('/api/config/section', json={
            'section': 'Camera', 'params': params})

    def _camera_file_value(self, dashboard, key):
        active = dashboard._resolve_config_path(dashboard._get_active_config_path())
        cfg = configparser.ConfigParser()
        cfg.read(active)
        return cfg.get('Camera', key, fallback=None)

    def test_gain_clamped_before_persist(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client._send_command.return_value = {'success': True}
        resp = self._post(client, {'awb_red_gain': '50', 'awb_mode': 'Manual'})
        assert resp.status_code == 200
        sent = dashboard.mqtt_client._send_command.call_args.kwargs['params']
        assert sent == {'awb_red_gain': 8.0, 'awb_mode': 'manual'}
        assert self._camera_file_value(dashboard, 'awb_red_gain') == '8.0'
        assert self._camera_file_value(dashboard, 'awb_mode') == 'manual'

    def test_invalid_mode_rejected_not_persisted(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client._send_command.return_value = {'success': True}
        resp = self._post(client, {'awb_mode': 'sunset'})
        assert resp.status_code == 400
        assert 'awb_mode' in resp.get_json()['rejected']
        dashboard.mqtt_client._send_command.assert_not_called()
        assert self._camera_file_value(dashboard, 'awb_mode') != 'sunset'

    def test_partial_rejection_reports_and_applies_rest(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client._send_command.return_value = {'success': True}
        resp = self._post(client, {'awb_blue_gain': 'abc', 'exp_compensation': '3'})
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'awb_blue_gain' in data['rejected']
        sent = dashboard.mqtt_client._send_command.call_args.kwargs['params']
        assert sent == {'exp_compensation': 3}

    def test_non_live_camera_keys_pass_through(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client._send_command.return_value = {'success': True}
        resp = self._post(client, {'crop_left': '0.05'})
        assert resp.status_code == 200
        sent = dashboard.mqtt_client._send_command.call_args.kwargs['params']
        assert sent == {'crop_left': '0.05'}

    def test_system_stats_forwards_restart_required(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        if not dashboard.mqtt_client:
            pytest.skip('fixture has no MQTT subscriber')
        dashboard.mqtt_client.current_state['restart_required'] = 'resolution_width'
        data = client.get('/api/system_stats').get_json()
        assert data['restart_required'] == 'resolution_width'


@pytest.mark.unit
class TestLocksFollowTheRunningOwl:
    """The recording/sensitivity locks read the INI, which can differ from
    the running OWL until it restarts (e.g. controller_type just changed)."""

    def test_running_owl_wins_over_the_ini(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client.get_state.return_value = {
            'owl_running': True, 'controller_type': 'advanced', 'switch_purpose': 'recording'}
        assert dashboard._get_controller_type() == 'advanced'
        assert dashboard._is_hardware_locked('recording') is True
        assert dashboard._is_hardware_locked('detection') is False

    def test_falls_back_to_the_ini_when_the_owl_is_silent(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = MagicMock()
        dashboard.mqtt_client.get_state.return_value = {'owl_running': False,
                                                        'controller_type': 'advanced'}
        assert dashboard._get_controller_type() == 'none'


@pytest.mark.unit
class TestConfigPersistence:
    """The dashboard's INI writes: 8 gunicorn threads did read-modify-write
    with no lock (one of two quick changes was lost), and values the OWL's
    config load would refuse were written anyway."""

    def _active(self, dashboard):
        import configparser
        config = configparser.ConfigParser()
        config.read(dashboard._resolve_config_path(dashboard._get_active_config_path()))
        return config

    def test_out_of_range_value_is_not_written(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        assert dashboard._persist_config_change('GreenOnBrown', 'exg_max', '900') is False
        config = self._active(dashboard)
        assert config.get('GreenOnBrown', 'exg_max', fallback=None) != '900'

    @pytest.mark.parametrize('key, value, written', [
        ('actuation_duration', '600', 5.0),     # "15" for "1.5" class of typo: minutes of spray
        ('actuation_duration', '0', 0.01),
        ('actuation_duration', '0.15', 0.15),
        ('delay', '600', 5.0),                  # a spray ten minutes after the weed
        ('delay', '-3', 0.0),
        ('delay', '0.4', 0.4),
    ])
    def test_actuation_is_clamped_before_it_is_written(self, standalone_test_client,
                                                       key, value, written):
        client, dashboard, tmp_dir = standalone_test_client
        assert dashboard._persist_config_change('System', key, value) is True
        assert float(self._active(dashboard).get('System', key)) == written

    @pytest.mark.parametrize('key', ['actuation_duration', 'delay'])
    def test_actuation_that_is_not_a_number_is_not_written(self, standalone_test_client, key):
        client, dashboard, tmp_dir = standalone_test_client
        before = self._active(dashboard).get('System', key)
        assert dashboard._persist_config_change('System', key, 'soon') is False
        assert self._active(dashboard).get('System', key) == before

    def test_concurrent_changes_are_all_kept(self, standalone_test_client):
        import threading as _threading
        client, dashboard, tmp_dir = standalone_test_client
        keys = [f'flag_{i}' for i in range(8)]
        threads = [_threading.Thread(target=dashboard._persist_config_change,
                                     args=('Extra', k, 'True')) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        config = self._active(dashboard)
        assert all(config.get('Extra', k, fallback=None) == 'True' for k in keys)


# ===========================================================================
# Spray timing and geometry through the routes
# ===========================================================================

@pytest.mark.unit
class TestActuationThroughConfigSection:
    """POST /api/config/section with [System] actuation_duration / delay:
    how long a nozzle stays open and how long after the weed it opens."""

    def _post(self, client, params):
        return client.post('/api/config/section', json={'section': 'System', 'params': params})

    def test_timing_is_sent_to_the_owl_and_saved(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = self._post(client, {'actuation_duration': 0.3, 'delay': 0.2})
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        assert sent == [('owl/commands', {'action': 'set_config_section', 'section': 'System',
                                          'params': {'actuation_duration': 0.3, 'delay': 0.2}})]
        saved = _active_ini(dashboard)
        assert float(saved.get('System', 'actuation_duration')) == 0.3
        assert float(saved.get('System', 'delay')) == 0.2

    @pytest.mark.parametrize('key, value, written', [('actuation_duration', 600, 5.0),
                                                     ('delay', 600, 5.0)])
    def test_out_of_range_timing_is_saved_clamped(self, standalone_test_client,
                                                  key, value, written):
        """What the next boot reads. The live value is clamped by the OWL
        (tests/mqtt, tests/owl/test_relay_matrix.py)."""
        client, dashboard, tmp_dir = standalone_test_client
        _published(dashboard)
        assert self._post(client, {key: value}).status_code == 200
        assert float(_active_ini(dashboard).get('System', key)) == written

    def test_timing_that_is_not_a_number_is_reported_and_not_saved(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        _published(dashboard)
        resp = self._post(client, {'delay': 'soon'})
        assert resp.get_json()['rejected'] == {'delay': 'Not saved: outside the allowed range'}
        assert _active_ini(dashboard).get('System', 'delay') == '0'

    @pytest.mark.xfail(strict=True, reason=(
        "controller/standalone/standalone.py set_config_section (the "
        "`payload = result ...; return jsonify(payload)` tail, line 2125): with the "
        "broker down it answers HTTP 200 (body success:false) and still writes the INI, "
        "so the page shows the change as made while the running OWL never got it. The "
        "spray buttons and /api/tracking/set answer 503 for the same failure."))
    def test_broker_down_is_reported_as_a_failure(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard, connected=False)
        resp = self._post(client, {'actuation_duration': 0.3})
        assert sent == []
        assert resp.get_json()['success'] is False
        assert resp.status_code == 503

    def test_broker_down_today_still_saves_for_the_next_start(self, standalone_test_client):
        """Pins what happens now (see the xfail above): the body says
        success:false and the value is in the INI, clamped."""
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard, connected=False)
        resp = self._post(client, {'actuation_duration': 600})
        assert sent == []
        assert resp.get_json() == {'success': False, 'error': 'Not connected to MQTT broker'}
        assert float(_active_ini(dashboard).get('System', 'actuation_duration')) == 5.0


@pytest.mark.unit
class TestGeometryRoute:
    """POST /api/geometry: the crop edges go to [Camera] and the actuation
    band (where in the frame a detection may open a nozzle) to [System]."""

    BAND = {'actuation_top': 0.3, 'actuation_bottom': 0.7}
    CROP = {'crop_left': 0.05, 'crop_right': 0.1}

    def test_actuation_band_goes_to_system_and_crop_to_camera(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        resp = client.post('/api/geometry', json={'params': {**self.BAND, **self.CROP}})
        assert resp.status_code == 200 and resp.get_json() == {'success': True}
        assert sent == [
            ('owl/commands', {'action': 'set_config_section', 'section': 'Camera',
                              'params': self.CROP}),
            ('owl/commands', {'action': 'set_config_section', 'section': 'System',
                              'params': self.BAND}),
        ]

    @pytest.mark.parametrize('params, section', [(BAND, 'System'), (CROP, 'Camera')])
    def test_only_the_section_that_changed_is_sent(self, standalone_test_client, params, section):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        client.post('/api/geometry', json={'params': params})
        assert sent == [('owl/commands', {'action': 'set_config_section', 'section': section,
                                          'params': params})]

    def test_persist_sends_one_save_geometry_with_everything(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        params = {**self.BAND, **self.CROP}
        resp = client.post('/api/geometry', json={'params': params, 'persist': True})
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        assert sent == [('owl/commands', {'action': 'save_geometry', 'params': params})]

    @pytest.mark.parametrize('body', [{}, {'params': {}}, {'persist': True}])
    def test_no_params_is_400_and_nothing_is_sent(self, standalone_test_client, body):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard)
        assert client.post('/api/geometry', json=body).status_code == 400
        assert sent == []

    def test_no_mqtt_client_is_500(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard.mqtt_client = None
        assert client.post('/api/geometry', json={'params': self.BAND}).status_code == 500

    @pytest.mark.xfail(strict=True, reason=(
        "controller/standalone/standalone.py set_geometry (lines 2144-2153): the live "
        "branch throws away the result of both _send_command calls and answers "
        "{'success': True}; the persist branch answers HTTP 200 with success:false. With "
        "the broker down the geometry editor shows the band as applied when it was not."))
    @pytest.mark.parametrize('persist', [False, True])
    def test_broker_down_is_reported_as_a_failure(self, standalone_test_client, persist):
        client, dashboard, tmp_dir = standalone_test_client
        sent = _published(dashboard, connected=False)
        resp = client.post('/api/geometry', json={'params': self.BAND, 'persist': persist})
        assert sent == []
        assert resp.get_json()['success'] is False
        assert resp.status_code == 503


@pytest.mark.unit
class TestSwitchPurposeRoute:
    """POST /api/controller/switch_purpose: whether the Ute's one switch owns
    spraying or recording after the next restart."""

    def _purpose(self, dashboard):
        return _active_ini(dashboard).get('Controller', 'switch_purpose')

    @pytest.mark.parametrize('sent_as, saved', [('detection', 'detection'),
                                                ('recording', 'recording'),
                                                (' Detection ', 'detection')])
    def test_purpose_is_saved_for_the_next_start(self, standalone_test_client, sent_as, saved):
        client, dashboard, tmp_dir = standalone_test_client
        # start from the other one so the write is what is being seen
        other = 'recording' if saved == 'detection' else 'detection'
        dashboard._persist_config_change('Controller', 'switch_purpose', other)
        assert self._purpose(dashboard) == other

        resp = client.post('/api/controller/switch_purpose', json={'purpose': sent_as})
        assert resp.status_code == 200 and resp.get_json()['success'] is True
        assert self._purpose(dashboard) == saved
        # and the dashboard reads it back for its own lock and toggle
        assert client.get('/api/controller_config').get_json()['switch_purpose'] == saved

    @pytest.mark.parametrize('body', [{}, {'purpose': ''}, {'purpose': 'spraying'},
                                      {'purpose': 'detection; rm'}])
    def test_anything_else_is_400_and_nothing_is_written(self, standalone_test_client, body):
        client, dashboard, tmp_dir = standalone_test_client
        before = self._purpose(dashboard)
        resp = client.post('/api/controller/switch_purpose', json=body)
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert self._purpose(dashboard) == before

    @pytest.mark.xfail(strict=True, reason=(
        "controller/standalone/standalone.py set_switch_purpose (line 1920): the return "
        "of _persist_config_change is ignored, so a write that failed is answered "
        "'Switch purpose set to detection. Restart OWL to apply.'"))
    def test_a_failed_write_is_not_reported_as_saved(self, standalone_test_client):
        client, dashboard, tmp_dir = standalone_test_client
        dashboard._persist_config_change_locked = lambda section, key, value: False
        resp = client.post('/api/controller/switch_purpose', json={'purpose': 'detection'})
        assert resp.get_json()['success'] is False
        assert resp.status_code >= 500
