"""Slim cloud feed (cloud data budget, v3.21.0).

The mosquitto bridge used to forward the full 2 s dashboard state and 1 Hz GPS
to Noktura (~20 MB per running hour for a 2-OWL rig). It now forwards only
cloud_out/... : a whitelisted snapshot sent on change plus a 20 s keepalive.
See docs/handover/CLOUD_DATA_BUDGET_HANDOVER-2026-10-08.md.
"""

import json
from pathlib import Path

import pytest

from utils.mqtt_manager import (
    CLOUD_KEEPALIVE_S, CLOUD_PROGRESS_THROTTLE_S, CLOUD_SESSIONS_REFRESH_S,
    CLOUD_STATE_FIELDS, CloudFeed, cloud_state_out_topic, trim_sessions,
)

REPO = Path(__file__).resolve().parents[2]

# Noktura apps/worker/src/mqtt/state-cache.ts OwlStatePayload (origin/main
# 81d17a1, 2026-10-09), minus the controller-only upload_requests and
# data_sessions (sent by CloudFeed only when it is known). Update both
# together, spec first (Noktura docs/owl/NOKTURA_INTEGRATION_SPEC.md).
NOKTURA_OWL_FIELDS = {
    'device_id', 'owl_running', 'detection_enable', 'cpu_percent', 'cpu_temp',
    'memory_percent', 'disk_percent', 'gps_latitude', 'gps_longitude', 'gps_available',
    'version', 'git_branch', 'git_commit', 'pi_model', 'os_pretty', 'software_update',
    'data_transfer', 'preview_upload', 'available_models', 'current_model', 'model_download',
}

SESSION = {'session_id': '20261009/session_101500', 'date': '20261009', 'time': '101500',
           'image_count': 12, 'video_count': 0, 'image_size': 3000, 'total_size': 3000}


def _snap(**overrides):
    snap = {'device_id': 'owl-1', 'owl_running': True, 'detection_enable': False,
            'cpu_percent': 12, 'cpu_temp': 51, 'version': '3.21.0',
            'data_transfer': {'status': 'idle', 'progress': 0}}
    snap.update(overrides)
    return snap


@pytest.mark.unit
class TestFieldSet:
    def test_matches_noktura_payload(self):
        assert set(CLOUD_STATE_FIELDS) == NOKTURA_OWL_FIELDS

    def test_topics_outside_owl_namespace(self):
        assert cloud_state_out_topic(True, 'owl-1') == 'cloud_out/owl-1/state'
        assert cloud_state_out_topic(False, 'owl-1') == 'cloud_out/state'

    def test_sessions_trimmed_to_manifest_keys(self):
        assert trim_sessions([SESSION]) == [{k: v for k, v in SESSION.items() if k != 'video_count'}]


@pytest.mark.unit
class TestCloudFeed:
    def test_first_message_is_qos1(self):
        payload, qos = CloudFeed().next_message(_snap(), now=0)
        assert qos == 1 and payload['device_id'] == 'owl-1'

    def test_quiet_until_keepalive(self):
        feed = CloudFeed()
        feed.next_message(_snap(), now=0)
        assert feed.next_message(_snap(), now=CLOUD_KEEPALIVE_S - 1) is None
        payload, qos = feed.next_message(_snap(), now=CLOUD_KEEPALIVE_S)
        assert qos == 0 and payload['owl_running'] is True

    def test_telemetry_jitter_is_not_a_change(self):
        feed = CloudFeed()
        feed.next_message(_snap(), now=0)
        assert feed.next_message(_snap(cpu_percent=80, cpu_temp=70, gps_latitude=-33.1), now=2) is None

    @pytest.mark.parametrize('change', [
        {'owl_running': False}, {'detection_enable': True}, {'version': '3.22.0'},
        {'current_model': 'weeds.ncnn'}, {'data_transfer': {'status': 'uploading', 'progress': 0}},
        {'software_update': {'status': 'running'}},
    ])
    def test_trigger_change_sends_now(self, change):
        feed = CloudFeed()
        feed.next_message(_snap(), now=0)
        payload, qos = feed.next_message(_snap(**change), now=1)
        assert qos == 1 and all(payload[k] == v for k, v in change.items())

    def test_progress_counters_throttled(self):
        feed = CloudFeed()
        feed.next_message(_snap(data_transfer={'status': 'uploading', 'progress': 1}), now=0)
        assert feed.next_message(_snap(data_transfer={'status': 'uploading', 'progress': 5}),
                                 now=CLOUD_PROGRESS_THROTTLE_S - 1) is None
        payload, qos = feed.next_message(_snap(data_transfer={'status': 'uploading', 'progress': 9}),
                                         now=CLOUD_PROGRESS_THROTTLE_S)
        assert qos == 1 and payload['data_transfer']['progress'] == 9

    def test_completion_not_throttled(self):
        feed = CloudFeed()
        feed.next_message(_snap(data_transfer={'status': 'uploading', 'progress': 99}), now=0)
        payload, qos = feed.next_message(_snap(data_transfer={'status': 'complete', 'progress': 100}), now=1)
        assert qos == 1 and payload['data_transfer']['status'] == 'complete'

    def test_unknown_sessions_never_sent(self):
        # None = not scanned yet; an empty list would tell Noktura every
        # session was deleted from the device.
        payload, _ = CloudFeed().next_message(_snap(), sessions=None, now=0)
        assert 'data_sessions' not in payload

    def test_sessions_only_on_change_and_refresh(self):
        feed = CloudFeed()
        sessions = trim_sessions([SESSION])
        payload, qos = feed.next_message(_snap(), sessions, now=0)
        assert payload['data_sessions'] == sessions and qos == 1

        payload, _ = feed.next_message(_snap(), sessions, now=CLOUD_KEEPALIVE_S)
        assert 'data_sessions' not in payload  # keepalive carries no list

        grown = trim_sessions([dict(SESSION, image_count=13)])
        payload, qos = feed.next_message(_snap(), grown, now=CLOUD_KEEPALIVE_S + 1)
        assert payload['data_sessions'] == grown and qos == 1

        payload, qos = feed.next_message(_snap(), grown, now=CLOUD_KEEPALIVE_S + 1 + CLOUD_SESSIONS_REFRESH_S)
        assert payload['data_sessions'] == grown and qos == 1

    def test_empty_list_is_sent_when_scanned(self):
        # A real scan that finds nothing is the truth and must reach Noktura.
        payload, _ = CloudFeed().next_message(_snap(), [], now=0)
        assert payload['data_sessions'] == []

    def test_reset_forces_full_message(self):
        feed = CloudFeed()
        sessions = trim_sessions([SESSION])
        feed.next_message(_snap(), sessions, now=0)
        feed.reset()
        payload, qos = feed.next_message(_snap(), sessions, now=1)
        assert qos == 1 and payload['data_sessions'] == sessions

    def test_controller_upload_requests_change_sends_now(self):
        feed = CloudFeed()
        base = {'device_id': 'owl-controller', 'type': 'controller', 'owl_count': 2,
                'timestamp': 0, 'upload_requests': []}
        feed.next_message(base, now=0)
        assert feed.next_message(dict(base, timestamp=2), now=2) is None
        payload, qos = feed.next_message(dict(base, timestamp=3, upload_requests=[{'id': 'r1'}]), now=3)
        assert qos == 1 and payload['upload_requests'] == [{'id': 'r1'}]


@pytest.mark.unit
class TestPublisherCloudState:
    def _cloud_calls(self, publisher):
        topic = publisher.topics['cloud_state']
        return [c for c in publisher.client.publish.call_args_list if c.args[0] == topic]

    def test_topic(self, mqtt_publisher):
        assert mqtt_publisher.topics['cloud_state'] == 'cloud_out/state'

    def test_snapshot_is_whitelisted_and_rounded(self, mqtt_publisher):
        with mqtt_publisher.state_lock:
            mqtt_publisher.state.update(cpu_percent=12.6, cpu_temp=51.44, memory_percent=40.2,
                                        gps_latitude=-33.8688123, gps_longitude=151.2092999)
        snap = mqtt_publisher._cloud_state()
        assert set(snap) <= set(CLOUD_STATE_FIELDS)
        assert 'data_sessions' not in snap and 'camera' not in snap
        assert snap['cpu_percent'] == 13 and snap['cpu_temp'] == 51 and snap['memory_percent'] == 40
        assert snap['gps_latitude'] == -33.86881 and snap['gps_longitude'] == 151.2093

    def test_absent_fields_omitted(self, mqtt_publisher):
        with mqtt_publisher.state_lock:
            mqtt_publisher.state.pop('pi_model', None)
        assert 'pi_model' not in mqtt_publisher._cloud_state()

    def test_publish_small_and_qos(self, mqtt_publisher):
        mqtt_publisher._publish_cloud_state()
        calls = self._cloud_calls(mqtt_publisher)
        assert len(calls) == 1 and calls[0].kwargs['qos'] == 1
        assert len(calls[0].args[1]) < 1500
        mqtt_publisher._publish_cloud_state()
        assert len(self._cloud_calls(mqtt_publisher)) == 1  # nothing changed, no send

    def test_sessions_follow_a_real_scan_only(self, mqtt_publisher, tmp_path):
        mqtt_publisher._publish_cloud_state()
        first = json.loads(self._cloud_calls(mqtt_publisher)[-1].args[1])
        assert 'data_sessions' not in first  # never scanned

        session_dir = tmp_path / '20261009' / 'session_101500'
        session_dir.mkdir(parents=True)
        (session_dir / 'a.jpg').write_bytes(b'x' * 10)
        mqtt_publisher.owl_instance.save_directory = str(tmp_path)
        mqtt_publisher._list_data_sessions()
        mqtt_publisher._publish_cloud_state()
        sent = json.loads(self._cloud_calls(mqtt_publisher)[-1].args[1])
        assert sent['data_sessions'][0]['session_id'] == '20261009/session_101500'
        assert set(sent['data_sessions'][0]) <= {'session_id', 'date', 'time', 'image_count',
                                                 'image_size', 'total_size'}

    def test_missing_storage_is_unknown_not_empty(self, mqtt_publisher, tmp_path):
        mqtt_publisher.owl_instance.save_directory = str(tmp_path / 'usb-not-present')
        mqtt_publisher._list_data_sessions()
        assert mqtt_publisher._cloud_sessions is None


@pytest.mark.unit
class TestBridgeLines:
    @pytest.fixture
    def cloud_sh(self):
        return (REPO / 'owl_cloud_provision.sh').read_text(encoding='utf-8')

    def test_state_bridged_from_slim_topic(self, cloud_sh):
        assert 'topic +/state out 1 cloud_out/ ${REMOTE_PREFIX}' in cloud_sh
        assert 'topic state out 1 cloud_out/ ${REMOTE_PREFIX}' in cloud_sh
        assert 'topic +/state out 1 ${LOCAL_PREFIX}' not in cloud_sh
        assert 'topic state out 1 ${LOCAL_PREFIX}' not in cloud_sh

    def test_gps_not_bridged(self, cloud_sh):
        assert 'topic +/gps' not in cloud_sh and 'topic gps' not in cloud_sh

    def test_status_and_commands_unchanged(self, cloud_sh):
        assert 'topic +/status out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}' in cloud_sh
        assert 'topic +/commands in 1 owl/cloud/ ${REMOTE_PREFIX}' in cloud_sh
        assert 'topic commands in 1 owl/cloud_ ${REMOTE_PREFIX}' in cloud_sh

    def test_queue_cap_before_connection(self, cloud_sh):
        assert cloud_sh.index('echo "max_queued_messages 200"') < cloud_sh.index('echo "connection noktura"')
