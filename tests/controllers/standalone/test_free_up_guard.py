"""Deleting a run while the OWL uploads it to Noktura itself.

The phone's "Free up space on the OWL" (Noktura spec §7.4/§7.5) calls
DELETE /api/downloads/session/<id>. Deleting a run, or its day folder, while
owl.py is zipping and uploading it would corrupt what Noktura receives, so the
route answers 409 with a farmer-readable reason, as it does while recording.
"""
import time

import pytest

from utils.mqtt_manager import transfer_covers


@pytest.mark.unit
class TestTransferCovers:
    @pytest.mark.parametrize('status', [None, '', 'idle', 'complete', 'error'])
    def test_an_idle_or_finished_transfer_covers_nothing(self, status):
        assert not transfer_covers({'status': status, 'session_date': '20260611'}, '20260611')

    @pytest.mark.parametrize('uploading,deleting', [
        ('20260611/session_083015', '20260611/session_083015'),   # the run itself
        ('20260611/session_083015', '20260611'),                  # its day folder
        ('20260611', '20260611/session_083015'),                  # a run inside an uploading day
    ])
    def test_an_active_upload_covers_the_run_its_day_and_its_runs(self, uploading, deleting):
        assert transfer_covers({'status': 'uploading', 'session_date': uploading}, deleting)

    def test_other_runs_and_lookalike_ids_are_free(self):
        active = {'status': 'zipping', 'session_date': '20260611/session_083015'}
        assert not transfer_covers(active, '20260611/session_090000')
        assert not transfer_covers(active, '20260612')
        # A prefix that is not a path boundary is a different run
        assert not transfer_covers({'status': 'uploading', 'session_date': '2026061'}, '20260611')

    def test_busy_without_a_name_refuses_rather_than_guesses(self):
        assert transfer_covers({'status': 'uploading', 'session_date': ''}, '20260611')

    def test_missing_state_covers_nothing(self):
        assert not transfer_covers(None, '20260611')


@pytest.mark.unit
class TestDownloadsDeleteGuard:
    def _client(self, standalone_test_client, tmp_path, data_transfer):
        client, dashboard, _ = standalone_test_client
        dashboard.token_reader = lambda: None   # legacy headerless delete allowed
        run = tmp_path / '20260611' / 'session_083015'
        run.mkdir(parents=True)
        (run / 'img_0001.jpg').write_bytes(b'\xff\xd8')
        dashboard._get_save_directory = lambda: str(tmp_path)
        dashboard.mqtt_client.last_heartbeat = time.time()
        dashboard.mqtt_client.current_state = {
            'owl_running': True,
            'image_sample_enable': False,
            'data_transfer': data_transfer,
        }
        return client, run

    def test_refuses_the_run_the_owl_is_uploading(self, standalone_test_client, tmp_path):
        client, run = self._client(standalone_test_client, tmp_path,
                                   {'status': 'uploading', 'session_date': '20260611/session_083015'})
        response = client.delete('/api/downloads/session/20260611/session_083015')
        assert response.status_code == 409
        assert 'uploading this run to Noktura' in response.get_json()['error']
        assert run.exists()

    def test_refuses_the_day_folder_of_an_uploading_run(self, standalone_test_client, tmp_path):
        client, run = self._client(standalone_test_client, tmp_path,
                                   {'status': 'zipping', 'session_date': '20260611/session_083015'})
        assert client.delete('/api/downloads/session/20260611').status_code == 409
        assert run.exists()

    def test_refuses_a_run_inside_a_day_being_uploaded(self, standalone_test_client, tmp_path):
        client, run = self._client(standalone_test_client, tmp_path,
                                   {'status': 'uploading', 'session_date': '20260611'})
        assert client.delete('/api/downloads/session/20260611/session_083015').status_code == 409
        assert run.exists()

    def test_deletes_once_the_upload_has_finished(self, standalone_test_client, tmp_path):
        client, run = self._client(standalone_test_client, tmp_path,
                                   {'status': 'complete', 'session_date': '20260611/session_083015'})
        response = client.delete('/api/downloads/session/20260611/session_083015')
        assert response.status_code == 200
        assert not run.exists()

    def test_a_silent_owl_does_not_block_deletes_forever(self, standalone_test_client, tmp_path):
        client, dashboard, _ = standalone_test_client
        client, run = self._client(standalone_test_client, tmp_path,
                                   {'status': 'uploading', 'session_date': '20260611/session_083015'})
        dashboard.mqtt_client.last_heartbeat = time.time() - 3600   # owl.py gone quiet
        assert client.delete('/api/downloads/session/20260611/session_083015').status_code == 200
        assert not run.exists()
