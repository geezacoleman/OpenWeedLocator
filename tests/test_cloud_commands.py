"""Cloud command allowlist (owl/cloud_commands) and the TLS verification rule.

Commands from the Noktura bridge land on owl/cloud_commands (standalone) or
owl/cloud/<id>/commands (networked) and pass an allowlist. A stolen bridge
credential must not be able to spray, change config, install code, or point
uploads and downloads at a foreign host.

The command payloads below are the shapes the Noktura worker actually builds
(Noktura apps/worker/src/mqtt/command-publisher.ts and transfer-orchestrator.ts,
read 2026-09-17). The v3.18.0 tests used shapes nothing sends (`part_urls`,
`uploads[]`), so the host check passed its tests while never checking a real
upload (cross-repo review 2026-09-17: O-2, O-3, O-6, O-8, O-9; see
Noktura/docs/owl/OWL_CLAIM_MVP_REVIEW-2026-09-17.md).
"""

import json
import ssl
from unittest.mock import MagicMock, patch

import pytest

from utils import software_update
from utils.mqtt_manager import (
    CLOUD_ALLOWED_ACTIONS, cloud_command_rejection, cloud_command_topic,
    cloud_command_urls, cloud_storage_host, is_private_host, ssl_context_for_url)

STORAGE = 'hel1.your-objectstorage.com'
PRESIGNED = f'https://{STORAGE}/noktura/owl-sessions/dev/20260917/session_101500.zip?X-Amz-Signature=abc'


def worker_transfer_single():
    return {'action': 'transfer_session', 'session_id': '20260917/session_101500',
            'data_types': ['images', 'logs', 'tracks', 'config'],
            'request_id': 'c0ffee00-0000-4000-8000-000000000001', 'method': 'PUT',
            'upload_url': PRESIGNED}


def worker_transfer_multipart(urls=None):
    urls = urls or [f'https://{STORAGE}/noktura/k?partNumber={i}&uploadId=u' for i in (1, 2, 3)]
    return {'action': 'transfer_session', 'session_id': '20260917/session_101500',
            'data_types': ['images'], 'request_id': 'r', 'method': 'PUT',
            'upload': {'upload_id': 'u', 'part_size': 16 * 1024 * 1024,
                       'parts': [{'part_number': i + 1, 'url': u} for i, u in enumerate(urls)]}}


def worker_previews(urls=None):
    return {'action': 'upload_previews', 'request_id': 'r', 'session_id': '20260917/session_101500',
            'count': 2, 'max_dimension': 800,
            'upload_urls': urls or [f'https://{STORAGE}/noktura/p0.jpg?sig', f'https://{STORAGE}/noktura/p1.jpg?sig']}


def worker_model_download(url=None):
    return {'action': 'download_model', 'url': url or f'https://{STORAGE}/noktura/models/m.ncnn.zip?sig',
            'filename': 'm.ncnn.zip', 'sha256': 'a' * 64, 'is_archive': True}


@pytest.mark.unit
class TestCloudCommandTopic:
    def test_topic_names(self):
        assert cloud_command_topic(False, 'owl-1') == 'owl/cloud_commands'
        assert cloud_command_topic(True, 'owl-3') == 'owl/cloud/owl-3/commands'

    def test_publisher_subscribes_cloud_topic(self, mqtt_publisher):
        assert mqtt_publisher.topics['cloud_commands'] == 'owl/cloud_commands'
        mqtt_publisher._on_connect(mqtt_publisher.client, None, None, 0)
        subscribed = [c.args[0] for c in mqtt_publisher.client.subscribe.call_args_list]
        assert 'owl/cloud_commands' in subscribed
        assert 'owl/commands' in subscribed   # dashboard path unchanged


@pytest.mark.unit
class TestCloudCommandAllowlist:
    def test_allowlist_is_exactly_what_the_noktura_portal_sends(self):
        # O-2 (decided 2026-09-17): every command the worker's enrich() builds.
        assert CLOUD_ALLOWED_ACTIONS == {
            'list_data_sessions', 'transfer_session', 'delete_session', 'upload_previews',
            'update_software', 'download_model', 'set_model', 'reboot', 'restart_service'}

    @pytest.mark.parametrize('command', [
        {'action': 'list_data_sessions'},
        {'action': 'restart_service'},
        {'action': 'reboot'},
        {'action': 'delete_session', 'session_id': '20260917/session_101500', 'request_id': 'r'},
        {'action': 'set_model', 'value': 'yolo11n_ncnn_model'},
        {'action': 'update_software', 'ref': 'v3.19.0', 'request_id': 'r'},
        worker_transfer_single(), worker_transfer_multipart(), worker_previews(), worker_model_download(),
    ], ids=lambda c: c['action'] if 'upload' not in c else 'transfer_multipart')
    def test_every_portal_command_passes_with_storage_host_pinned(self, command):
        assert cloud_command_rejection(command, STORAGE) is None

    def test_spray_command_from_cloud_is_dropped(self, mqtt_publisher, caplog):
        msg = MagicMock(topic='owl/cloud_commands',
                        payload=json.dumps({'action': 'set_detection_enable', 'value': True}).encode())
        with caplog.at_level('WARNING'):
            mqtt_publisher._on_message(mqtt_publisher.client, None, msg)
        assert mqtt_publisher.state['detection_enable'] is False
        assert 'cloud command dropped' in caplog.text

    def test_same_command_on_the_dashboard_topic_still_works(self, mqtt_publisher):
        msg = MagicMock(topic='owl/commands',
                        payload=json.dumps({'action': 'set_detection_enable', 'value': True}).encode())
        mqtt_publisher._on_message(mqtt_publisher.client, None, msg)
        assert mqtt_publisher.state['detection_enable'] is True

    @pytest.mark.parametrize('action', [
        'set_detection_enable', 'set_image_sample_enable', 'set_sensitivity_level',
        'install_algorithm', 'set_config_section', 'save_config', 'set_active_config',
        'shutdown', 'set_actuation_params', 'calibrate_awb', None, '',
    ])
    def test_everything_else_is_dropped(self, action):
        assert cloud_command_rejection({'action': action}) is not None

    @pytest.mark.parametrize('ref', ['main', 'display-bug', 'v3', '3.18.0', 'v3.18', 'v3.18.0-rc1',
                                     'v3.18.0/../x', '', None, 319, 'v1.2.3\n', 'v1.2.3 ', ' v1.2.3'])
    def test_cloud_updates_are_release_tags_only(self, ref):
        # 'v1.2.3\n' passed the old re.match('^...$') check (O-6).
        assert cloud_command_rejection({'action': 'update_software', 'ref': ref}) is not None

    def test_local_dashboard_updates_keep_branch_refs(self):
        # The allowlist is cloud-only: the kiosk/app update path is unchanged
        assert software_update.valid_ref('display-bug')

    def test_cloud_transfers_must_be_presigned_put(self):
        post = dict(worker_transfer_single(), method='POST')
        assert cloud_command_rejection(post, STORAGE) is not None
        no_method = {k: v for k, v in worker_transfer_single().items() if k != 'method'}
        assert cloud_command_rejection(no_method, STORAGE) is not None  # handler default is POST

    def test_storage_host_pins_every_url_the_worker_sends(self):
        evil = 'https://evil.example/x'
        assert cloud_command_rejection(dict(worker_transfer_single(), upload_url=evil), STORAGE)
        good_parts = [p['url'] for p in worker_transfer_multipart()['upload']['parts']]
        assert cloud_command_rejection(worker_transfer_multipart(good_parts[:2] + [evil]), STORAGE)
        assert cloud_command_rejection(worker_previews([worker_previews()['upload_urls'][0], evil]), STORAGE)
        assert cloud_command_rejection(worker_model_download(evil), STORAGE)
        # exact host: a bucket subdomain is a different host; compare is case-insensitive
        assert cloud_command_rejection(worker_model_download(f'https://bucket.{STORAGE}/m'), STORAGE)
        assert cloud_command_rejection(worker_model_download(f'https://{STORAGE.upper()}/m'), STORAGE) is None

    def test_urls_must_be_https_even_without_a_storage_host(self):
        assert cloud_command_rejection(worker_model_download(f'http://{STORAGE}/m'), '')
        assert cloud_command_rejection(worker_previews([f'http://{STORAGE}/p']), '')

    def test_no_storage_host_configured_accepts_any_https_host(self):
        # Manual provisioning (rigs, admin-issued credentials) may not set it;
        # the app path always does (Noktura credential storage_host).
        command = dict(worker_transfer_single(), upload_url='https://any.example/x')
        assert cloud_command_rejection(command, '') is None

    def test_urls_extracted_in_worker_shapes(self):
        assert cloud_command_urls(worker_transfer_single()) == [PRESIGNED]
        assert len(cloud_command_urls(worker_transfer_multipart())) == 3
        assert len(cloud_command_urls(worker_previews())) == 2
        assert cloud_command_urls({'action': 'reboot'}) == []

    def test_validate_the_validator_real_multipart_shape_is_checked(self):
        # The v3.18.0 check read upload.part_urls; the worker sends
        # upload.parts[].url. A foreign part URL must be caught in the REAL shape.
        foreign = worker_transfer_multipart([f'https://{STORAGE}/a', 'https://evil.example/b'])
        assert 'part_urls' not in foreign['upload']
        assert cloud_command_rejection(foreign, STORAGE) is not None

    def test_non_object_payload_is_dropped(self):
        assert cloud_command_rejection(['not', 'a', 'dict']) is not None
        assert cloud_command_rejection(None) is not None

    def test_allowed_cloud_command_reaches_handler_and_rejected_does_not(self, mqtt_publisher):
        with patch.object(mqtt_publisher, '_handle_command') as hc:
            mqtt_publisher._handle_cloud_command({'action': 'list_data_sessions'})
            hc.assert_called_once_with({'action': 'list_data_sessions'})
            mqtt_publisher._handle_cloud_command({'action': 'set_detection_enable', 'value': True})
            assert hc.call_count == 1

    def test_storage_host_is_read_per_command_not_cached_at_start(self, mqtt_publisher):
        # The app provisions the cloud while owl.py runs; a value cached at
        # startup stayed empty (accept any host) until the next restart.
        with patch.object(mqtt_publisher, '_handle_command') as hc, \
                patch('utils.mqtt_manager.cloud_storage_host', return_value=STORAGE):
            mqtt_publisher._handle_cloud_command(worker_model_download('https://evil.example/m'))
            assert hc.call_count == 0
            mqtt_publisher._handle_cloud_command(worker_model_download())
            assert hc.call_count == 1

    def test_storage_host_read_from_controller_ini(self, tmp_path):
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text('[Cloud]\nenable = True\nstorage_host = FSN1.Your-Objectstorage.com\n')
        assert cloud_storage_host(ini) == 'fsn1.your-objectstorage.com'
        assert cloud_storage_host(tmp_path / 'missing.ini') == ''
        (tmp_path / 'nocloud.ini').write_text('[MQTT]\nenable = True\n')
        assert cloud_storage_host(tmp_path / 'nocloud.ini') == ''


@pytest.mark.unit
class TestTlsVerificationRule:
    """Downloads and uploads verify TLS by default; only LAN hosts (the in-cab
    controller's self-signed dashboard) are exempt."""

    @pytest.mark.parametrize('url', [
        'https://fsn1.your-objectstorage.com/b/k', 'https://github.com/x.zip',
        'https://app.noktura.tech/models/m.pt', 'https://8.8.8.8/x',
        # O-8: dotless names the resolver maps to public addresses
        'https://16843009/x', 'https://0x01010101/x', 'https://owl-controller/x',
    ])
    def test_public_or_ambiguous_hosts_are_verified(self, url):
        ctx = ssl_context_for_url(url)
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    @pytest.mark.parametrize('url', [
        'https://192.168.1.2/uploads/m.pt', 'https://10.42.0.1/x',
        'https://owl-controller.local/x', 'https://127.0.0.1/x',
    ])
    def test_lan_hosts_are_exempt(self, url):
        assert ssl_context_for_url(url).verify_mode == ssl.CERT_NONE

    def test_garbage_url_is_verified(self):
        assert ssl_context_for_url('not a url').verify_mode == ssl.CERT_REQUIRED
        assert ssl_context_for_url('').verify_mode == ssl.CERT_REQUIRED

    def test_is_private_host(self):
        assert is_private_host('10.0.0.5') and is_private_host('192.168.0.1') and is_private_host('172.16.0.9')
        assert is_private_host('owl-1.local')
        assert not is_private_host('owl-1') and not is_private_host('16843009')
        assert not is_private_host('1.1.1.1') and not is_private_host('noktura.tech') and not is_private_host('')
