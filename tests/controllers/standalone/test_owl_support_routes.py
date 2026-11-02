"""Save logs on the OWL unit: POST /api/support/bundle + the ticket download.

The build needs the device token (like reboot and delete). A unit with no
token is unguarded by design (settings_routes.check_device_token): the
hotspot password still gates the network and the bundle is redacted.
"""

import io
import threading
import zipfile
from unittest.mock import patch

import pytest

TOKEN = 'tok-123'


def _fake_build(**kwargs):
    return 'owl_logs_TEST_20261029-1200.zip', _zip({'summary.txt': 'ok'})


def _zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return buffer.getvalue()


@pytest.fixture
def client(standalone_test_client):
    client, dashboard, _ = standalone_test_client
    dashboard.token_reader = lambda: TOKEN
    with patch('controller.standalone.standalone.support_bundle.build_bundle',
               side_effect=_fake_build) as build:
        yield client, dashboard, build


@pytest.mark.unit
class TestSupportBundleRoutes:

    def test_build_needs_the_device_token(self, client):
        client, _, build = client
        assert client.post('/api/support/bundle', json={}).status_code == 403
        assert not build.called

    def test_tokenless_unit_may_build(self, client):
        """Pinned: no token configured = legacy allow, like every guarded route."""
        client, dashboard, _ = client
        dashboard.token_reader = lambda: None
        assert client.post('/api/support/bundle', json={}).status_code == 200

    def test_build_then_download_by_ticket(self, client):
        client, _, build = client
        report = {'app_version': '0.4.2', 'events': []}
        resp = client.post('/api/support/bundle', json={'app': report},
                           headers={'X-Device-Token': TOKEN})
        data = resp.get_json()
        assert resp.status_code == 200 and data['success'] is True
        assert data['url'].startswith('/api/support/bundle/')
        assert data['support_email'] == 'support@noktura.tech'
        assert data['subject'].startswith('OWL logs ')
        assert build.call_args.kwargs['app_report'] == report
        # a dropped download may retry within the TTL
        for _ in range(2):
            got = client.get(data['url'])
            assert got.status_code == 200
            assert got.mimetype == 'application/zip'
            assert 'owl_logs_TEST_20261029-1200.zip' in got.headers['Content-Disposition']
            assert zipfile.ZipFile(io.BytesIO(got.data)).namelist() == ['summary.txt']

    def test_unknown_ticket_404(self, client):
        client, _, _ = client
        assert client.get('/api/support/bundle/not-a-ticket').status_code == 404

    def test_one_build_at_a_time(self, client):
        client, dashboard, _ = client
        bp = dashboard.app.blueprints['support']
        assert bp.store.building.acquire(blocking=False)
        try:
            resp = client.post('/api/support/bundle', json={}, headers={'X-Device-Token': TOKEN})
            assert resp.status_code == 429 and resp.get_json()['error'] == 'busy'
        finally:
            bp.store.building.release()

    def test_build_failure_is_a_plain_500(self, client):
        client, _, build = client
        build.side_effect = RuntimeError('boom')
        resp = client.post('/api/support/bundle', json={}, headers={'X-Device-Token': TOKEN})
        assert resp.status_code == 500
        assert 'boom' not in resp.get_data(as_text=True)

    def test_old_tokenless_log_download_is_gone(self, client):
        """It zipped logs/ unredacted (Wi-Fi passwords) for anyone on the hotspot."""
        client, _, _ = client
        assert client.get('/api/download_logs').status_code == 404


@pytest.mark.unit
def test_real_build_runs_on_a_dev_box(standalone_test_client):
    """The dashboard's own build: no journal or vcgencmd here, still a zip."""
    _, dashboard, _ = standalone_test_client
    filename, data, email, subject = dashboard._build_support_bundle(
        {'app_version': '0.4.2', 'events': []})
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert filename.startswith('owl_logs_') and 'summary.txt' in names and 'app.txt' in names
    assert 'stats.json' in names
    assert email == 'support@noktura.tech'
