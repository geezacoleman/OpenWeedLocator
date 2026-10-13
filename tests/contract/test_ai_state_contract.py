"""Producer <-> consumer contract for the AI read fields.

The OWL publishes them in its MQTT state (OWLMQTTPublisher.state); the
standalone /api/system_stats relays them to the app and dashboard unchanged.
The networked controller merges the whole state per OWL (owls_state), so it
only needs the OWL to always publish them, including algorithm_error = None
when there is no error (a merge never removes a key).
"""

import json
import time

import pytest

AI_READ_FIELDS = ('algorithm', 'algorithm_pending', 'algorithm_error',
                  'model_available', 'model_download', 'ai')


@pytest.fixture
def owl_state():
    from utils.mqtt_manager import OWLMQTTPublisher
    publisher = OWLMQTTPublisher(broker_host='localhost', broker_port=1,
                                 client_id='contract', device_id='owl-1')
    # What the dashboard receives: the JSON the OWL publishes
    return json.loads(json.dumps(publisher.state, default=str))


def test_owl_always_publishes_the_ai_read_fields(owl_state):
    missing = [k for k in AI_READ_FIELDS if k not in owl_state]
    assert not missing, f'OWL state lacks {missing}'
    assert set(owl_state['ai']) == {'cpu', 'hailo', 'accelerator'}
    assert set(owl_state['ai']['hailo']) == {'present', 'chip', 'runtime'}
    assert set(owl_state['model_download']) >= {'status', 'model_name', 'progress', 'error'}


def test_system_stats_relays_them_unchanged(standalone_test_client, owl_state):
    client, dashboard, _ = standalone_test_client
    owl_state.update(algorithm_error='ultralytics is required', algorithm_pending='gog')
    sub = dashboard.mqtt_client
    with sub.state_lock:
        sub.current_state = owl_state
        sub.last_heartbeat = time.time() + 3600
    stats = client.get('/api/system_stats').get_json()
    for key in AI_READ_FIELDS:
        assert stats[key] == owl_state[key], key
