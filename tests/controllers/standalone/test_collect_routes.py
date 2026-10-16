"""Standalone /api/collect/* routes and per-session metadata.

Same contract as the networked collect screen (controller/networked), so the
shared collect_form.js and owl-app work against either controller:
GET /api/collect/config, POST /api/collect/settings, /start, /stop, plus
GET/POST /api/downloads/session/<sid>/metadata for the Data tab.
"""

import configparser
import json
from unittest.mock import patch

import pytest

from utils.session_metadata import (CAPTURE_PARAM_KEYS, COLLECT_CAPTURE_DEFAULTS, DEFAULT_CROPS,
                                    SESSION_METADATA_FIELDS)


@pytest.fixture
def ctx(standalone_test_client, tmp_path):
    client, dashboard, tmp_dir = standalone_test_client
    dashboard.mqtt_client.connected = True
    # Never touch the repo's CONTROLLER.ini (tests/README.md)
    ini = tmp_dir / 'CONTROLLER.ini'
    ini.write_text('[MQTT]\nbroker_ip = localhost\n', encoding='utf-8')
    dashboard.controller_ini_path = str(ini)
    save_dir = tmp_path / 'owl_images'
    (save_dir / '20261011' / 'session_191454').mkdir(parents=True)
    (save_dir / '20261011' / 'session_191454' / 'img_1.jpg').write_bytes(b'x')
    dashboard._get_save_directory = lambda: str(save_dir)
    return client, dashboard, tmp_dir, ini, save_dir


def _published(dashboard):
    """(action, payload) for every MQTT command the dashboard sent."""
    out = []
    for call in dashboard.mqtt_client.client.publish.call_args_list:
        payload = json.loads(call.args[1])
        out.append((payload['action'], payload))
    return out


def _active_config(tmp_dir):
    """The file live changes land in (the autosave working copy)."""
    pointer = (tmp_dir / 'active_config.txt').read_text().strip()
    cfg = configparser.ConfigParser()
    cfg.read(tmp_dir / pointer.split('/')[-1])
    return cfg


@pytest.mark.unit
class TestCollectConfig:

    def test_shape_matches_the_networked_contract(self, ctx):
        client, dashboard, *_ = ctx
        data = client.get('/api/collect/config').get_json()
        assert set(data['presets']) == {
            'crops', 'custom_crops', 'crop_groups', 'seasonal_crops', 'weather', 'lighting',
            'light_sources', 'skies', 'soil_covers', 'soil_moistures', 'recent_water', 'purposes'}
        assert tuple(data['presets']['crops']) == DEFAULT_CROPS
        assert set(data['capture']) == set(COLLECT_CAPTURE_DEFAULTS)
        assert data['language'] == 'en'
        assert data['last_pick'] == {}
        assert data['recording'] is False
        assert set(data['metadata']) == set(SESSION_METADATA_FIELDS)

    def test_blank_profile_keys_use_the_owl_defaults(self, ctx):
        """A profile without the capture keys runs owl.py's fallbacks
        (every Nth frame), not the collect screen's distance default."""
        client, *_ = ctx
        capture = client.get('/api/collect/config').get_json()['capture']
        assert capture['sample_mode'] == 'frames'
        assert capture['shutter_cap'] is False
        assert capture['max_gain'] == 'auto'

    def test_presets_last_pick_and_language_from_controller_ini(self, ctx):
        client, dashboard, tmp_dir, ini, _ = ctx
        ini.write_text('[Collect]\ncrops = wheat, lupins\nlanguage = da\n'
                       'last_crop = lupins\nlast_weather = wet\n', encoding='utf-8')
        data = client.get('/api/collect/config').get_json()
        assert data['presets']['crops'] == ['wheat', 'lupins']
        assert data['language'] == 'da'
        assert data['last_pick'] == {'crop': 'lupins', 'weather': 'wet'}

    def test_unknown_language_falls_back_to_english(self, ctx):
        client, dashboard, tmp_dir, ini, _ = ctx
        ini.write_text('[Collect]\nlanguage = xx\n', encoding='utf-8')
        assert client.get('/api/collect/config').get_json()['language'] == 'en'

    def test_reports_the_current_run(self, ctx):
        client, dashboard, *_ = ctx
        with patch.object(dashboard.mqtt_client, 'get_state', return_value={
                'image_sample_enable': True, 'session_metadata': {'crop': 'barley'}}):
            data = client.get('/api/collect/config').get_json()
        assert data['recording'] is True and data['metadata']['crop'] == 'barley'


@pytest.mark.unit
class TestCollectSettings:

    def test_saves_to_active_config_and_applies_live(self, ctx):
        client, dashboard, tmp_dir, *_ = ctx
        resp = client.post('/api/collect/settings', json={'capture': {
            'sample_mode': 'distance', 'sample_distance_m': '1.5', 'shutter_cap': 'true', 'max_gain': '8'}})
        data = resp.get_json()
        assert resp.status_code == 200 and data['success'] is True and data['live'] is True
        assert data['capture']['sample_mode'] == 'distance'
        assert data['capture']['sample_distance_m'] == 1.5
        assert data['capture']['shutter_cap'] is True
        assert data['capture']['max_gain'] == 8.0
        cfg = _active_config(tmp_dir)
        assert cfg.get('DataCollection', 'sample_mode') == 'distance'
        assert cfg.get('DataCollection', 'shutter_cap') == 'true'
        assert cfg.get('Camera', 'max_gain') == '8.0'
        live = [p for a, p in _published(dashboard) if a == 'set_capture_params']
        assert live and live[-1]['params'] == {'sample_mode': 'distance', 'sample_distance_m': 1.5,
                                               'shutter_cap': True, 'max_gain': 8.0}
        assert set(live[-1]['params']) <= set(CAPTURE_PARAM_KEYS)

    @pytest.mark.parametrize('key,value', [
        ('sample_distance_m', '0'), ('sample_distance_m', '51'), ('jpeg_quality', '49'),
        ('video_max_s', '61'), ('sample_mode', 'sometimes'), ('shutter_cap', 'maybe'),
        ('max_gain', '100'), ('smear_limit_mm', 'abc')])
    def test_rejects_out_of_range_values(self, ctx, key, value):
        client, dashboard, *_ = ctx
        resp = client.post('/api/collect/settings', json={'capture': {key: value}})
        assert resp.status_code == 400 and resp.get_json()['success'] is False
        assert not [a for a, _ in _published(dashboard) if a == 'set_capture_params']

    def test_unknown_keys_alone_are_rejected(self, ctx):
        client, *_ = ctx
        resp = client.post('/api/collect/settings', json={'capture': {'save_directory': '/tmp'}})
        assert resp.status_code == 400

    def test_language_saved_to_controller_ini(self, ctx):
        client, dashboard, tmp_dir, ini, _ = ctx
        assert client.post('/api/collect/settings', json={'language': 'da'}).status_code == 200
        assert 'language = da' in ini.read_text()
        assert '[MQTT]' in ini.read_text()
        assert client.post('/api/collect/settings', json={'language': 'fr'}).status_code == 400

    def test_saved_when_the_owl_is_not_running(self, ctx):
        client, dashboard, tmp_dir, *_ = ctx
        dashboard.mqtt_client.connected = False
        data = client.post('/api/collect/settings', json={'capture': {'jpeg_quality': 90}}).get_json()
        assert data['success'] is True and data['live'] is False
        assert _active_config(tmp_dir).get('DataCollection', 'jpeg_quality') == '90'


@pytest.mark.unit
class TestCollectStartStop:

    META = {'crop': 'barley', 'crop_season': 'spring', 'weather': 'windy',
            'lighting': 'natural_sun', 'purpose': 'survey', 'notes': 'north block'}

    def test_start_sends_metadata_then_recording(self, ctx):
        client, dashboard, tmp_dir, ini, _ = ctx
        resp = client.post('/api/collect/start', json={'metadata': self.META})
        data = resp.get_json()
        assert resp.status_code == 200 and data['success'] is True and len(data['run_id']) == 12
        sent = _published(dashboard)
        actions = [a for a, _ in sent]
        assert actions.index('set_session_metadata') < actions.index('set_image_sample_enable')
        meta = [p for a, p in sent if a == 'set_session_metadata'][-1]
        assert meta['crop'] == 'barley' and meta['run_id'] == data['run_id']
        assert [p for a, p in sent if a == 'set_image_sample_enable'][-1]['value'] is True
        # Detection is left alone: recording while spraying still works
        assert 'set_detection_enable' not in actions and 'toggle_detection' not in actions
        text = ini.read_text()
        assert 'last_crop = barley' in text and 'last_weather = windy' in text
        assert 'notes' not in text

    def test_odd_values_are_not_remembered(self, ctx):
        client, dashboard, tmp_dir, ini, _ = ctx
        client.post('/api/collect/start', json={'metadata': {'crop': '100% wheat'}})
        assert 'last_crop = \n' in ini.read_text() or 'last_crop =\n' in ini.read_text()

    def test_unwritable_controller_ini_does_not_block_start(self, ctx):
        client, dashboard, tmp_dir, ini, _ = ctx
        dashboard.controller_ini_path = str(tmp_dir / 'missing-dir' / 'CONTROLLER.ini')
        assert client.post('/api/collect/start', json={'metadata': self.META}).status_code == 200

    def test_mqtt_down_is_503(self, ctx):
        client, dashboard, *_ = ctx
        dashboard.mqtt_client.connected = False
        resp = client.post('/api/collect/start', json={'metadata': self.META})
        assert resp.status_code == 503 and resp.get_json()['success'] is False

    def test_hardware_switch_owns_recording(self, ctx):
        client, dashboard, *_ = ctx
        dashboard._is_hardware_locked = lambda control: control == 'recording'
        assert client.post('/api/collect/start', json={'metadata': self.META}).status_code == 409
        assert client.post('/api/collect/stop').status_code == 409
        assert not _published(dashboard)

    def test_stop(self, ctx):
        client, dashboard, *_ = ctx
        assert client.post('/api/collect/stop').get_json() == {'success': True}
        assert _published(dashboard)[-1][1]['value'] is False


@pytest.mark.unit
class TestSessionMetadataPartialUpdate:

    def test_omitted_fields_keep_their_values(self, ctx):
        """Older app builds send four free-text fields; they must not blank
        the crop season, light, purpose or notes set by the record form."""
        client, dashboard, *_ = ctx
        current = {'crop': 'wheat', 'crop_season': 'winter', 'lighting': 'natural_sun', 'notes': 'n'}
        with patch.object(dashboard.mqtt_client, 'get_state', return_value={'session_metadata': current}):
            client.post('/api/session/metadata', json={'field_name': 'North', 'weather': 'dry'})
        sent = [p for a, p in _published(dashboard) if a == 'set_session_metadata'][-1]
        assert sent['field_name'] == 'North' and sent['weather'] == 'dry'
        assert sent['crop_season'] == 'winter' and sent['lighting'] == 'natural_sun' and sent['notes'] == 'n'

    def test_sent_empty_string_clears(self, ctx):
        client, dashboard, *_ = ctx
        with patch.object(dashboard.mqtt_client, 'get_state', return_value={'session_metadata': {'crop': 'wheat'}}):
            client.post('/api/session/metadata', json={'crop': ''})
        assert [p for a, p in _published(dashboard) if a == 'set_session_metadata'][-1]['crop'] == ''


@pytest.mark.unit
class TestPerSessionMetadata:

    SID = '20261011/session_191454'

    def test_missing_file_returns_empty_fields(self, ctx):
        client, *_ = ctx
        data = client.get(f'/api/downloads/session/{self.SID}/metadata').get_json()
        assert data['success'] is True
        assert data['metadata'] == {k: '' for k in SESSION_METADATA_FIELDS}

    def test_merge_keeps_fixed_and_untouched_keys(self, ctx):
        client, dashboard, tmp_dir, ini, save_dir = ctx
        path = save_dir / '20261011' / 'session_191454' / 'session_metadata.json'
        path.write_text(json.dumps({'crop': 'wheat', 'run_id': 'abc123', 'recorded_at': 't',
                                    'owl_id': 'owl-1', 'weather': 'dry'}))
        resp = client.post(f'/api/downloads/session/{self.SID}/metadata',
                           json={'metadata': {'weather': 'wet', 'lighting': 'natural_overcast',
                                              'run_id': 'hacked', 'bogus': 'x'}})
        data = resp.get_json()
        assert resp.status_code == 200 and data['metadata']['weather'] == 'wet'
        saved = json.loads(path.read_text())
        assert saved['crop'] == 'wheat' and saved['lighting'] == 'natural_overcast'
        assert saved['run_id'] == 'abc123' and saved['owl_id'] == 'owl-1' and saved['recorded_at'] == 't'
        assert 'bogus' not in saved
        assert client.get(f'/api/downloads/session/{self.SID}/metadata').get_json()['metadata']['weather'] == 'wet'

    def test_values_are_capped(self, ctx):
        client, *_ = ctx
        data = client.post(f'/api/downloads/session/{self.SID}/metadata',
                           json={'metadata': {'notes': 'x' * 900}}).get_json()
        assert len(data['metadata']['notes']) == 500

    def test_corrupt_file_is_replaced(self, ctx):
        client, dashboard, tmp_dir, ini, save_dir = ctx
        path = save_dir / '20261011' / 'session_191454' / 'session_metadata.json'
        path.write_text('{not json')
        assert client.get(f'/api/downloads/session/{self.SID}/metadata').status_code == 200
        assert client.post(f'/api/downloads/session/{self.SID}/metadata',
                           json={'metadata': {'crop': 'oats'}}).status_code == 200
        assert json.loads(path.read_text()) == {'crop': 'oats'}

    def test_recording_session_is_refused(self, ctx):
        client, dashboard, *_ = ctx
        with patch.object(dashboard.mqtt_client, 'get_state', return_value={'image_sample_enable': True}):
            resp = client.post(f'/api/downloads/session/{self.SID}/metadata', json={'metadata': {'crop': 'oats'}})
        assert resp.status_code == 409

    @pytest.mark.parametrize('sid,status', [('20261011/session_000000', 404), ('../etc', 400),
                                            ('2026/session_1', 400)])
    def test_bad_session_ids(self, ctx, sid, status):
        client, *_ = ctx
        assert client.get(f'/api/downloads/session/{sid}/metadata').status_code == status
        assert client.post(f'/api/downloads/session/{sid}/metadata',
                           json={'metadata': {'crop': 'x'}}).status_code == status

    def test_metadata_must_be_an_object(self, ctx):
        client, *_ = ctx
        assert client.post(f'/api/downloads/session/{self.SID}/metadata',
                           json={'metadata': 'wheat'}).status_code == 400

    def test_zip_route_still_serves_the_session(self, ctx):
        """The ZIP route shares the <path:> prefix; /metadata must not shadow it."""
        client, *_ = ctx
        resp = client.get(f'/api/downloads/session/{self.SID}')
        assert resp.status_code == 200 and resp.mimetype == 'application/zip'


@pytest.mark.unit
def test_system_stats_reports_capture_and_metadata(ctx):
    client, dashboard, *_ = ctx
    with patch.object(dashboard.mqtt_client, 'get_state', return_value={
            'capture': {'sample_mode': 'distance'}, 'session_metadata': {'crop': 'rye'}}):
        data = client.get('/api/system_stats').get_json()
    assert data['capture'] == {'sample_mode': 'distance'}
    assert data['session_metadata']['crop'] == 'rye'


@pytest.mark.unit
class TestCustomCropsRoutes:
    """Same contract as the networked controller; saved in this OWL's own
    CONTROLLER.ini [Collect] custom_crops."""

    def test_add_and_remove(self, ctx):
        client, dashboard, tmp_dir, ini, _ = ctx
        resp = client.post('/api/collect/crops', json={'name': 'Pak choi'})
        assert resp.status_code == 200 and resp.get_json()['crop'] == 'pak_choi'
        assert 'custom_crops = pak_choi' in ini.read_text(encoding='utf-8')
        assert client.get('/api/collect/config').get_json()['presets']['crops'][0] == 'pak_choi'
        assert client.delete('/api/collect/crops/pak_choi').status_code == 200
        assert client.get('/api/collect/config').get_json()['presets']['custom_crops'] == []

    def test_bad_names(self, ctx):
        client, *_ = ctx
        assert client.post('/api/collect/crops', json={'name': ''}).status_code == 400
        assert client.delete('/api/collect/crops/wheat').status_code == 404


@pytest.mark.unit
class TestPauseRoutes:

    def test_pause_needs_a_recording(self, ctx):
        client, dashboard, *_ = ctx
        with patch.object(dashboard.mqtt_client, 'get_state', return_value={'image_sample_enable': False}):
            assert client.post('/api/collect/pause').status_code == 409
        assert not [a for a, _ in _published(dashboard) if a == 'set_recording_paused']

    def test_pause_and_resume(self, ctx):
        client, dashboard, *_ = ctx
        with patch.object(dashboard.mqtt_client, 'get_state', return_value={'image_sample_enable': True}):
            assert client.post('/api/collect/pause').get_json()['paused'] is True
            assert client.post('/api/collect/resume').get_json()['paused'] is False
        sent = [p['value'] for a, p in _published(dashboard) if a == 'set_recording_paused']
        assert sent == [True, False]
