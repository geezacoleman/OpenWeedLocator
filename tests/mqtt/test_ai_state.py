"""OWL MQTT state for honest AI: what runs, what is pending, the 'ai'
capability block, model_available per heartbeat, and the cloud subset."""

import json
import time
from unittest.mock import patch

import pytest

from utils import mqtt_manager as mm


@pytest.mark.unit
class TestRefreshAiState:
    def test_reports_the_running_algorithm_not_the_config(self, mqtt_publisher, mock_owl):
        mock_owl.config.set('System', 'algorithm', 'gog')
        mock_owl.running_algorithm = 'exhsv'
        mock_owl._pending_algorithm = None
        mqtt_publisher._refresh_ai_state()
        assert mqtt_publisher.state['algorithm'] == 'exhsv'
        assert mqtt_publisher.state['algorithm_pending'] is None

    def test_pending_switch_is_reported_separately(self, mqtt_publisher, mock_owl):
        mock_owl.running_algorithm = 'exhsv'
        mock_owl._pending_algorithm = 'gog'
        mqtt_publisher._refresh_ai_state()
        assert mqtt_publisher.state['algorithm'] == 'exhsv'
        assert mqtt_publisher.state['algorithm_pending'] == 'gog'

    def test_model_available_refreshed_every_heartbeat(self, mqtt_publisher, mock_owl):
        with patch.object(mqtt_publisher, '_check_model_available', return_value=True):
            mqtt_publisher._refresh_ai_state()
        assert mqtt_publisher.state['model_available'] is True
        with patch.object(mqtt_publisher, '_check_model_available', return_value=False):
            mqtt_publisher._refresh_ai_state()
        assert mqtt_publisher.state['model_available'] is False


@pytest.mark.unit
class TestAiBlock:
    def test_state_has_ai_and_clearable_error(self, mqtt_publisher):
        state = mqtt_publisher.state
        assert set(state['ai']) == {'cpu', 'hailo', 'accelerator'}
        # Always present so controllers merging state drop a stale error
        assert 'algorithm_error' in state and state['algorithm_error'] is None
        assert 'algorithm_pending' in state
        json.dumps(state, default=str)

    def test_ai_is_in_the_cloud_feed(self, mqtt_publisher):
        assert 'ai' in mm.CLOUD_STATE_FIELDS
        assert mqtt_publisher._cloud_state()['ai'] == mqtt_publisher.state['ai']

    def test_recomputed_when_an_update_completes(self, mqtt_publisher, tmp_path):
        mqtt_publisher._update_status_path = tmp_path / '.update_status.json'
        fresh = {'cpu': {'installed': True},
                 'hailo': {'present': False, 'chip': None, 'runtime': False},
                 'accelerator': 'cpu'}
        with patch.object(mm.ai_capability, 'detect', return_value=fresh) as detect:
            mqtt_publisher._update_status_path.write_text(json.dumps(
                {'request_id': 'r', 'status': 'installing_deps', 'updated_at': int(time.time())}))
            mqtt_publisher._read_update_status()
            assert not detect.called
            mqtt_publisher._update_status_path.write_text(json.dumps(
                {'request_id': 'r', 'status': 'complete', 'updated_at': int(time.time()),
                 'error': ''}))
            mqtt_publisher._read_update_status()
        assert mqtt_publisher.state['ai'] == fresh
