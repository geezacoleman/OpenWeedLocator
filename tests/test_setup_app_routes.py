"""
Unit tests for the first-boot setup API (controller/setup/).

The NetworkManager is a MagicMock and timers fire only when the test
says so, so the full join handoff (including failure rollback) runs
synchronously on any platform.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from controller.setup.firstboot_state import FirstBootState
from controller.setup.setup_app import create_app


class CapturingTimer:
    """threading.Timer stand-in — collects callbacks; tests fire them."""

    pending = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.daemon = False

    def start(self):
        CapturingTimer.pending.append(self)

    def fire(self):
        CapturingTimer.pending.remove(self)
        self.function()

    @classmethod
    def fire_all(cls):
        while cls.pending:
            cls.pending[0].fire()


@pytest.fixture(autouse=True)
def clear_timers():
    CapturingTimer.pending = []
    yield
    CapturingTimer.pending = []


@pytest.fixture
def nm():
    manager = MagicMock()
    manager.is_supported.return_value = True
    manager.get_hotspot_connection.return_value = 'OWL-4'
    manager.get_ip4.return_value = '10.42.0.1'
    manager.status.return_value = {'mode': 'hotspot', 'ssid': 'OWL-4',
                                   'ip': '10.42.0.1', 'hotspot_profile': 'OWL-4'}
    manager.scan.return_value = [
        {'ssid': 'HomeNet', 'signal': 80, 'security': 'WPA2'}]
    manager.activate_connection.return_value = True
    manager.last_error = None
    return manager


@pytest.fixture
def state(nm, tmp_path):
    flag = tmp_path / 'owl-firstboot.flag'
    flag.touch()
    # controller_ini MUST stay in tmp_path: finish() mints the device token
    # into it, and the default path is the real repo config/CONTROLLER.ini.
    return FirstBootState(nm, flag_path=flag,
                          state_file=tmp_path / 'state.json',
                          timer_factory=CapturingTimer,
                          system_runner=MagicMock(),
                          exit_fn=MagicMock(),
                          controller_ini=tmp_path / 'CONTROLLER.ini')


@pytest.fixture
def client(nm, state):
    app = create_app(network_manager=nm, state=state)
    app.config['TESTING'] = True
    return app.test_client()


@pytest.mark.unit
class TestInfo:
    def test_info_shape(self, client):
        with patch('controller.setup.setup_app.check_camera',
                   return_value={'ok': True, 'detail': 'frame received'}), \
                patch('controller.setup.setup_app.get_owl_service_status',
                      return_value='active'):
            data = client.get('/setup/api/info').get_json()
        assert data['success'] is True
        assert data['hotspot'] == {'ssid': 'OWL-4', 'ip': '10.42.0.1'}
        assert data['camera']['ok'] is True
        assert data['owl_service'] == 'active'
        assert 'version' in data and 'device_id' in data
        # Unique per-unit identity for the phone app's saved-device dedupe
        # (None off-hardware, so assert presence, not value)
        assert 'device_serial' in data
        # Contract version: the app blocks with an "update" message on a
        # mismatch — this field must never silently disappear
        from version import APP_CONTRACT_VERSION
        assert data['contract_version'] == APP_CONTRACT_VERSION
        assert isinstance(data['contract_version'], int)

    def test_camera_failure_reported_not_fatal(self, client):
        with patch('controller.setup.setup_app.check_camera',
                   return_value={'ok': False, 'detail': 'no frame from OWL'}):
            data = client.get('/setup/api/info').get_json()
        assert data['success'] is True
        assert data['camera']['ok'] is False

    def test_first_request_marks_in_setup(self, client, state):
        assert state.state == 'await_phone'
        client.get('/setup/api/info')
        assert state.state == 'in_setup'


@pytest.mark.unit
class TestCameraFrame:
    def test_proxies_jpeg(self, client):
        fake_response = MagicMock()
        fake_response.read.return_value = b'\xff\xd8jpegbytes'
        fake_response.__enter__ = lambda s: fake_response
        fake_response.__exit__ = MagicMock(return_value=False)
        with patch('controller.setup.setup_app.urllib.request.urlopen',
                   return_value=fake_response):
            response = client.get('/setup/api/camera/frame')
        assert response.status_code == 200
        assert response.mimetype == 'image/jpeg'
        assert response.data.startswith(b'\xff\xd8')

    def test_503_when_owl_not_streaming(self, client):
        import urllib.error
        with patch('controller.setup.setup_app.urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')):
            response = client.get('/setup/api/camera/frame')
        assert response.status_code == 503
        assert response.get_json()['success'] is False


@pytest.mark.unit
class TestWifiScan:
    def test_cached_scan_default(self, client, state, nm):
        state.scan_cache = [{'ssid': 'Cached', 'signal': 50, 'security': 'WPA2'}]
        data = client.get('/setup/api/wifi/scan').get_json()
        assert data['cached'] is True
        assert data['networks'][0]['ssid'] == 'Cached'
        nm.scan.assert_not_called()

    def test_rescan_cycles_hotspot(self, client, nm):
        with patch('controller.setup.firstboot_state.time.sleep'):
            data = client.get('/setup/api/wifi/scan?rescan=true').get_json()
        assert data['cached'] is False
        assert data['networks'][0]['ssid'] == 'HomeNet'
        # AP must come down before the scan and back up after
        call_order = [c[0] for c in
                      (nm.hotspot_down.mock_calls + nm.mock_calls) if c[0] in
                      ('hotspot_down', 'scan', 'hotspot_up')]
        assert call_order.index('hotspot_down') < call_order.index('scan') \
            < call_order.index('hotspot_up')


@pytest.mark.unit
class TestWifiJoin:
    def test_join_returns_202_with_verify_info(self, client):
        response = client.post('/setup/api/wifi/join',
                               json={'ssid': 'HomeNet', 'password': 'secret123'})
        assert response.status_code == 202
        data = response.get_json()
        assert data['state'] == 'pending'
        assert data['verify']['service_type'] == '_owl-setup._tcp'
        assert len(CapturingTimer.pending) == 1  # switch scheduled, not run

    def test_missing_ssid_rejected(self, client):
        response = client.post('/setup/api/wifi/join', json={'password': 'secret123'})
        assert response.status_code == 400

    def test_short_password_rejected(self, client):
        response = client.post('/setup/api/wifi/join',
                               json={'ssid': 'HomeNet', 'password': 'short'})
        assert response.status_code == 400

    def test_double_join_conflicts(self, client):
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        response = client.post('/setup/api/wifi/join',
                               json={'ssid': 'Other', 'password': 'secret123'})
        assert response.status_code == 409

    def test_successful_switch(self, client, state, nm):
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        nm.get_ip4.return_value = '192.168.1.57'
        CapturingTimer.fire_all()

        nm.add_wifi_connection.assert_called_once_with('HomeNet', 'secret123')
        nm.hotspot_down.assert_called_once()
        result = client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'connected'
        assert result['ip'] == '192.168.1.57'

    def test_failed_switch_rolls_back_to_hotspot(self, client, nm):
        nm.activate_connection.return_value = False
        nm.last_error = 'auth'
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'wrongpass1'})
        CapturingTimer.fire_all()

        nm.delete_connection.assert_called_once_with('HomeNet')
        nm.hotspot_up.assert_called_once()
        result = client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'failed'
        assert result['error'] == 'auth'

    def test_result_persists_across_app_recreation(self, client, state, nm, tmp_path):
        client.post('/setup/api/wifi/join',
                    json={'ssid': 'HomeNet', 'password': 'secret123'})
        nm.get_ip4.return_value = '192.168.1.57'
        CapturingTimer.fire_all()

        reloaded = FirstBootState(nm, flag_path=state.flag_path,
                                  state_file=state.state_file,
                                  timer_factory=CapturingTimer,
                                  system_runner=MagicMock(),
                                  exit_fn=MagicMock())
        assert reloaded.wifi_state == 'connected'
        assert reloaded.wifi_ssid == 'HomeNet'


@pytest.mark.unit
class TestHotspotRestore:
    def test_restores_and_clears_result(self, client, state, nm):
        state.wifi_state = 'failed'
        state.wifi_ssid = 'HomeNet'
        response = client.post('/setup/api/hotspot/restore')
        assert response.get_json()['success'] is True
        nm.delete_connection.assert_called_with('HomeNet')
        nm.hotspot_up.assert_called()
        assert client.get('/setup/api/wifi/result').get_json()['state'] == 'idle'


@pytest.mark.unit
class TestStatus:
    def test_status_shape(self, client, state):
        data = client.get('/setup/api/status').get_json()
        assert data['success'] is True
        assert data['flag_armed'] is True
        assert data['network']['mode'] == 'hotspot'

    def test_flag_absent_reported(self, client, state):
        state.flag_path.unlink()
        assert client.get('/setup/api/status').get_json()['flag_armed'] is False


@pytest.mark.unit
class TestFinish:
    def test_standalone_requires_new_password(self, client):
        response = client.post('/setup/api/finish', json={'mode': 'standalone'})
        assert response.status_code == 400

    def test_standalone_defers_rekey_until_response_sent(self, client, state, nm):
        response = client.post('/setup/api/finish',
                               json={'mode': 'standalone',
                                     'new_password': 'paddock-relay-42'})
        assert response.get_json()['success'] is True
        # The re-key restarts the AP and drops the phone — it must not run
        # before the finish response has been sent (the field bug: the app
        # always saw a dead request and reported failure)
        nm.set_hotspot_password.assert_not_called()
        assert state.flag_path.exists()
        assert len(CapturingTimer.pending) == 1  # delayed re-key scheduled
        CapturingTimer.fire_all()
        nm.set_hotspot_password.assert_called_once_with('paddock-relay-42')
        assert not state.flag_path.exists()
        state._system_runner.assert_called_once_with(
            ['ufw', 'delete', 'allow', '8088/tcp'])
        state._exit_fn.assert_called_once()

    def test_standalone_rekey_failure_keeps_setup_armed(self, client, state, nm):
        nm.set_hotspot_password.side_effect = Exception('nmcli died')
        response = client.post('/setup/api/finish',
                               json={'mode': 'standalone',
                                     'new_password': 'paddock-relay-42'})
        assert response.get_json()['success'] is True
        with patch('controller.setup.firstboot_state.time.sleep'):
            CapturingTimer.fire_all()
        # Setup password still works and the wizard can retry finish —
        # tearing down here would lock the user out
        assert state.flag_path.exists()
        state._exit_fn.assert_not_called()
        assert state.state != 'done'

    def test_wifi_finish_requires_connected(self, client):
        response = client.post('/setup/api/finish', json={'mode': 'wifi'})
        assert response.status_code == 409

    def test_wifi_finish_after_connected(self, client, state):
        state.wifi_state = 'connected'
        response = client.post('/setup/api/finish', json={'mode': 'wifi'})
        assert response.get_json()['success'] is True
        assert not state.flag_path.exists()

    def test_unknown_mode_rejected(self, client):
        response = client.post('/setup/api/finish', json={'mode': 'banana'})
        assert response.status_code == 400


@pytest.mark.unit
class TestCors:
    def test_allowed_origin_is_reflected(self, client):
        response = client.get('/setup/api/status',
                              headers={'Origin': 'http://localhost:4180'})
        assert response.headers['Access-Control-Allow-Origin'] \
            == 'http://localhost:4180'
        assert response.headers['Vary'] == 'Origin'

    def test_null_origin_allowed_for_file_pages(self, client):
        response = client.get('/setup/api/status', headers={'Origin': 'null'})
        assert response.headers['Access-Control-Allow-Origin'] == 'null'

    def test_unknown_origin_gets_no_cors_grant(self, client):
        response = client.get('/setup/api/status',
                              headers={'Origin': 'http://evil.example'})
        assert 'Access-Control-Allow-Origin' not in response.headers

    def test_preflight_options_for_allowed_origin(self, client):
        response = client.open('/setup/api/wifi/join', method='OPTIONS',
                               headers={'Origin': 'http://localhost:4180'})
        assert response.status_code in (200, 204)
        assert 'POST' in response.headers['Access-Control-Allow-Methods']


@pytest.mark.unit
class TestOwlDetectionSuppression:
    """owl.py can't import on Windows (cv2/GPIO) — inspect the source."""

    def test_owl_checks_first_boot_flag(self):
        from pathlib import Path
        source = (Path(__file__).parent.parent / 'owl.py').read_text(encoding='utf-8')
        assert 'OWL_FIRSTBOOT_FLAG' in source
        assert 'owl-firstboot.flag' in source
        # The flag must gate the value fed into the shared detection state
        flag_block = source.split('OWL_FIRSTBOOT_FLAG')[1][:600]
        assert 'detection_enable_config = False' in flag_block

    def test_flag_never_touches_the_hot_path(self):
        """HOT-PATH GUARD: first-boot code may exist ONLY in __init__ (one
        stat at boot) and the update_state thread (slow re-stat while armed).
        Any reference from another method — especially the detection loop —
        would add per-frame cost and must fail this test."""
        import ast
        from pathlib import Path
        source = (Path(__file__).parent.parent / 'owl.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        offenders = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                segment = ast.get_source_segment(source, node) or ''
                if 'firstboot' in segment.lower():
                    offenders.add(node.name)
        # update_state contains nested defs? No — but walk visits nested
        # functions separately, so only the directly-containing names appear
        assert offenders <= {'__init__', 'update_state'}, (
            f'first-boot references leaked into: {offenders}')

    def test_update_state_forces_detection_off_while_armed(self):
        """While the flag exists, a hardware switch or dashboard toggle must
        not re-enable detection (bench relay-click regression)."""
        import ast
        from pathlib import Path
        source = (Path(__file__).parent.parent / 'owl.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        update_state = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == 'update_state')
        segment = ast.get_source_segment(source, update_state)
        assert '_firstboot_active' in segment
        assert 'self._detection_enable = False' in segment


@pytest.mark.unit
class TestInstallScript:
    """install_firstboot.sh writes systemd units via heredoc (the same
    pattern as owl_setup.sh) — a template+sed step once shipped an
    ExecStart with a doubled bin/bin python path (field bug 2026-07-16)."""

    @pytest.fixture
    def script(self):
        from pathlib import Path
        return (Path(__file__).parent.parent / 'controller' / 'setup'
                / 'install_firstboot.sh').read_text(encoding='utf-8')

    def test_execstart_uses_venv_bin_directly(self, script):
        assert 'ExecStart=${VENV_BIN}/python' in script
        assert 'bin/bin' not in script
        # The template+sed pattern must not come back
        assert '__VENV' not in script
        assert 'sed -e' not in script

    def test_venv_bin_ends_in_bin_exactly_once(self, script):
        import re
        match = re.search(r'^VENV_BIN="([^"]+)"', script, re.MULTILINE)
        assert match, 'VENV_BIN definition missing'
        assert match.group(1).endswith('/owl/bin')

    def test_failure_responder_is_wired(self, script):
        assert 'OnFailure=owl-firstboot-fallback.service' in script
        assert 'StartLimitBurst' in script
        assert '/usr/bin/python3' in script          # system python on purpose
        assert 'fallback_server.py' in script
        # Disarm must remove the fallback unit too
        assert '"${UNIT_DEST}" "${FALLBACK_UNIT_DEST}"' in script

    def test_unit_heredoc_lines_are_flush_left(self, script):
        """systemd ignores indented directives — unit content inside the
        heredocs must start at column 0."""
        for block in script.split('<<EOF')[1:]:
            body = block.split('\nEOF')[0]
            for line in body.splitlines():
                if line.strip().startswith(('[', 'ExecStart', 'Description',
                                            'ConditionPathExists')):
                    assert line == line.lstrip(), (
                        f'indented unit line: {line!r}')


@pytest.mark.unit
class TestCameraBootConfig:
    """CM5 has no camera autodetection — install_firstboot.sh must write the
    sensor overlay to config.txt explicitly (a fresh flash otherwise never
    finds the camera: no error, no I2C bus — field bug 2026-08)."""

    @pytest.fixture
    def script(self):
        from pathlib import Path
        return (Path(__file__).parent.parent / 'controller' / 'setup'
                / 'install_firstboot.sh').read_text(encoding='utf-8')

    def test_overlay_variable_matches_python_sensor(self, script):
        """The dtoverlay written by the shell and the sensor the diagnostics
        look for must be the same sensor (a swap must change both)."""
        import re
        from controller.setup.firstboot_state import CAMERA_SENSOR
        match = re.search(r'^CAMERA_OVERLAY="([^"]+)"', script, re.MULTILINE)
        assert match, 'CAMERA_OVERLAY definition missing'
        assert match.group(1) == CAMERA_SENSOR

    def test_config_block_covers_both_ports_and_i2c(self, script):
        # Both ports on purpose (same sensor, unused port fails silently);
        # dtparams expose the camera I2C buses for i2cdetect debugging
        assert 'dtoverlay=${CAMERA_OVERLAY}"' in script
        assert 'dtoverlay=${CAMERA_OVERLAY},cam0' in script
        assert 'dtparam=i2c_csi_dsi=on' in script
        assert 'dtparam=i2c_csi_dsi0=on' in script
        # Lines must land under [all] — other sections are silently ignored
        assert '"[all]"' in script

    def test_autodetect_flipped_in_place_not_appended(self, script):
        assert "sed -i 's/^camera_auto_detect=.*/camera_auto_detect=0/'" in script

    def test_non_cm_boards_are_skipped(self, script):
        """Hardening: never force overlay lines on a Pi 4/5, where
        autodetect works and forced overlays can fight it."""
        assert '/proc/device-tree/model' in script
        assert 'Compute Module' in script

    def test_conflicting_sensor_overlay_refused(self, script):
        # IMX296 and IMX477 both sit at I2C 0x1a — never stack two sensors
        assert 'dtoverlay=(imx[0-9]+|ov[0-9]+' in script

    def test_arm_aborts_on_camera_config_error(self, script):
        assert 'Camera boot config failed. Nothing was armed.' in script

    def test_camera_config_mode_exists(self, script):
        # owl_setup.sh calls this standalone entry before its camera checks
        assert '--camera-config) configure_camera' in script

    def test_owl_setup_invokes_camera_config(self):
        from pathlib import Path
        owl_setup = (Path(__file__).parent.parent
                     / 'owl_setup.sh').read_text(encoding='utf-8')
        assert 'install_firstboot.sh" --camera-config' in owl_setup


@pytest.mark.unit
class TestCameraDiagnostics:
    """Triage for 'camera not found': overlay missing from config.txt vs
    sensor NACK (-121) — the two look identical from the phone app."""

    @staticmethod
    def _runner(responses):
        """argv[0] -> result namespace or exception to raise."""
        def run(argv, timeout=15):
            resp = responses[argv[0]]
            if isinstance(resp, Exception):
                raise resp
            return resp
        return run

    @staticmethod
    def _result(stdout='', stderr=''):
        from types import SimpleNamespace
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)

    def test_sensor_listed_is_ok(self):
        from controller.setup.firstboot_state import camera_diagnostics
        runner = self._runner({'rpicam-hello': self._result(
            stdout='Available cameras\n0 : imx296 [1456x1088] (/base/axi...)')})
        diag = camera_diagnostics(runner)
        assert diag['detected'] is True
        assert diag['status'] == 'ok'

    def test_no_kernel_messages_means_overlay_missing(self):
        from controller.setup.firstboot_state import camera_diagnostics
        runner = self._runner({
            'rpicam-hello': self._result(stderr='No cameras available!'),
            'dmesg': self._result(stdout='usb 1-1: new device\neth0: link up'),
        })
        diag = camera_diagnostics(runner)
        assert diag['detected'] is False
        assert diag['status'] == 'overlay_missing'
        assert 'config.txt' in diag['detail']

    def test_probe_nack_121_identified(self):
        from controller.setup.firstboot_state import camera_diagnostics
        runner = self._runner({
            'rpicam-hello': self._result(stderr='No cameras available!'),
            'dmesg': self._result(
                stdout='imx296 10-001a: failed to read chip id 296, '
                       'with error -121'),
        })
        diag = camera_diagnostics(runner)
        assert diag['status'] == 'probe_failed'
        assert '-121' in diag['detail'] or 'NACK' in diag['detail']

    def test_missing_rpicam_reported(self):
        from controller.setup.firstboot_state import camera_diagnostics
        runner = self._runner({'rpicam-hello': FileNotFoundError('rpicam-hello')})
        diag = camera_diagnostics(runner)
        assert diag['status'] == 'no_rpicam'

    def test_route_serves_diagnostics(self, client, state):
        state._system_runner = self._runner({'rpicam-hello': self._result(
            stdout='0 : imx296 [1456x1088]')})
        data = client.get('/setup/api/camera/diagnostics').get_json()
        assert data['success'] is True
        assert data['status'] == 'ok'
        assert data['sensor'] == 'imx296'


@pytest.mark.unit
class TestShipCleanup:
    """--ship must strip dev residue (a dev phone-hotspot password shipped
    on a bench unit, 2026-07-17) — dev/clean.sh wipes every WiFi profile
    except the OWL hotspot, so it must run non-interactively and LAST."""

    @pytest.fixture
    def owl_setup(self):
        from pathlib import Path
        return (Path(__file__).parent.parent
                / 'owl_setup.sh').read_text(encoding='utf-8')

    @pytest.fixture
    def clean(self):
        from pathlib import Path
        return (Path(__file__).parent.parent / 'dev'
                / 'clean.sh').read_text(encoding='utf-8')

    def test_clean_supports_non_interactive_yes(self, clean):
        assert '"${1:-}" == "--yes"' in clean       # set -u safe
        assert 'ASSUME_YES' in clean

    def test_clean_restarts_both_services_it_stopped(self, clean):
        assert 'systemctl start owl.service' in clean
        assert 'systemctl start owl-dash.service' in clean

    def test_ship_invokes_clean_detached_as_final_step(self, owl_setup):
        assert "dev/clean.sh' --yes" in owl_setup
        assert 'setsid' in owl_setup
        # Detached + last: the cleanup drops an SSH session running over a
        # personal WiFi profile, so nothing may depend on it afterwards
        ship_tail = owl_setup.split('[SHIP-READY]')[1]
        assert 'clean.sh' in ship_tail
        # Log must not use a .log suffix — clean.sh truncates /var/log/*.log
        assert '/var/log/owl-ship-clean.txt' in owl_setup
        assert 'owl-ship-clean.log' not in owl_setup


@pytest.mark.unit
class TestFallbackServer:
    """The OnFailure error responder — stdlib only, must run without the
    venv, and must satisfy the app's CORS preflight or the phone never
    sees the payload."""

    @pytest.fixture
    def server(self):
        import threading
        from http.server import ThreadingHTTPServer
        from controller.setup import fallback_server
        httpd = ThreadingHTTPServer(('127.0.0.1', 0),
                                    fallback_server.FallbackHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f'http://127.0.0.1:{httpd.server_address[1]}'
        httpd.shutdown()

    def test_stdlib_only(self):
        """Anything beyond the stdlib defeats the purpose (broken venv)."""
        import ast
        from pathlib import Path
        source = (Path(__file__).parent.parent / 'controller' / 'setup'
                  / 'fallback_server.py').read_text(encoding='utf-8')
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0]
                                for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split('.')[0])
        assert imported <= {'json', 'subprocess', 'http'}, imported

    def test_get_returns_503_json_with_journal_detail(self, server):
        import urllib.error
        import urllib.request
        request = urllib.request.Request(server + '/setup/api/info',
                                         headers={'Origin': 'null'})
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        response = raised.value
        assert response.code == 503
        assert response.headers['Access-Control-Allow-Origin'] == 'null'
        payload = json.loads(response.read())
        assert payload['success'] is False
        assert payload['fallback'] is True
        assert 'failed to start' in payload['error']
        assert isinstance(payload['detail'], list)

    def test_preflight_options_succeeds(self, server):
        import urllib.request
        request = urllib.request.Request(server + '/setup/api/info',
                                         method='OPTIONS',
                                         headers={'Origin': 'null'})
        response = urllib.request.urlopen(request)
        assert response.status == 204
        assert response.headers['Access-Control-Allow-Origin'] == 'null'

    def test_unknown_origin_gets_no_cors_grant(self, server):
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            server + '/setup/api/info',
            headers={'Origin': 'http://evil.example'})
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        assert raised.value.headers.get('Access-Control-Allow-Origin') is None

    def test_journal_tail_survives_missing_journalctl(self):
        from controller.setup import fallback_server
        with patch('controller.setup.fallback_server.subprocess.run',
                   side_effect=FileNotFoundError):
            assert fallback_server.journal_tail() == []


@pytest.mark.unit
class TestStartup:
    def test_dev_mode_without_nmcli(self, nm, state):
        nm.is_supported.return_value = False
        state.startup()
        assert state.state == 'in_setup'
        nm.hotspot_up.assert_not_called()

    def test_normal_startup_scans_then_raises_ap(self, nm, state):
        with patch('controller.setup.firstboot_state.time.sleep'):
            state.startup()
        assert state.state == 'await_phone'
        assert state.scan_cache[0]['ssid'] == 'HomeNet'
        nm.hotspot_up.assert_called()
        # Fixed setup PSK re-applied so a flag-only re-arm still works
        from controller.setup.firstboot_state import SETUP_HOTSPOT_PSK
        nm.set_hotspot_password.assert_called_once_with(SETUP_HOTSPOT_PSK)

    def test_startup_recreates_ufw_rule(self, nm, state):
        with patch('controller.setup.firstboot_state.time.sleep'):
            state.startup()
        ufw_calls = [c for c in state._system_runner.mock_calls
                     if c.args and c.args[0][:2] == ['ufw', 'allow']]
        assert ufw_calls, 'startup must re-add the setup port ufw rule'

    def test_reboot_onto_client_network_resumes(self, nm, state):
        nm.status.return_value = {'mode': 'client', 'ssid': 'HomeNet',
                                  'ip': '192.168.1.57', 'hotspot_profile': 'OWL-4'}
        state.startup()
        assert state.wifi_state == 'connected'
        assert state.wifi_ip == '192.168.1.57'
        nm.hotspot_up.assert_not_called()


# ---------------------------------------------------------------------------
# Controller join (networked rig provisioning)
# ---------------------------------------------------------------------------

JOIN_PAYLOAD = {
    'ssid': 'RigNet', 'password': 'rigpass123',
    'device_id': 'owl-3', 'static_ip': '192.168.1.13',
    'gateway': '192.168.1.1', 'subnet_prefix': 24,
    'broker_ip': '192.168.1.2', 'broker_port': 1883,
}


@pytest.fixture
def ctrl_state(nm, tmp_path):
    """FirstBootState with a real CONTROLLER.ini in tmp_path."""
    import configparser
    ini = tmp_path / 'CONTROLLER.ini'
    config = configparser.ConfigParser()
    config['WebDashboard'] = {'port': '8000'}          # must survive untouched
    config['MQTT'] = {'enable': 'True', 'broker_ip': 'localhost',
                      'device_id': 'auto'}
    with open(ini, 'w') as handle:
        config.write(handle)
    flag = tmp_path / 'owl-firstboot.flag'
    flag.touch()
    return FirstBootState(nm, flag_path=flag,
                          state_file=tmp_path / 'state.json',
                          timer_factory=CapturingTimer,
                          system_runner=MagicMock(
                              return_value=MagicMock(returncode=0)),
                          exit_fn=MagicMock(),
                          controller_ini=ini)


@pytest.fixture
def ctrl_client(nm, ctrl_state):
    app = create_app(network_manager=nm, state=ctrl_state)
    app.config['TESTING'] = True
    return app.test_client()


@pytest.mark.unit
class TestControllerJoinValidation:
    def test_missing_fields_rejected(self, ctrl_client):
        response = ctrl_client.post('/setup/api/controller/join',
                                    json={'ssid': 'RigNet'})
        assert response.status_code == 400
        assert 'Missing fields' in response.get_json()['error']

    def test_bad_device_id_rejected(self, ctrl_client):
        payload = dict(JOIN_PAYLOAD, device_id='OWL_3!')
        assert ctrl_client.post('/setup/api/controller/join',
                                json=payload).status_code == 400

    def test_bad_ip_rejected(self, ctrl_client):
        payload = dict(JOIN_PAYLOAD, static_ip='not-an-ip')
        assert ctrl_client.post('/setup/api/controller/join',
                                json=payload).status_code == 400

    def test_short_password_rejected(self, ctrl_client):
        payload = dict(JOIN_PAYLOAD, password='short')
        assert ctrl_client.post('/setup/api/controller/join',
                                json=payload).status_code == 400

    def test_double_join_conflicts(self, ctrl_client):
        assert ctrl_client.post('/setup/api/controller/join',
                                json=JOIN_PAYLOAD).status_code == 202
        assert ctrl_client.post('/setup/api/controller/join',
                                json=JOIN_PAYLOAD).status_code == 409


@pytest.mark.unit
class TestControllerJoinSwitch:
    def test_202_shape_points_at_static_ip(self, ctrl_client):
        data = ctrl_client.post('/setup/api/controller/join',
                                json=JOIN_PAYLOAD).get_json()
        assert data['state'] == 'pending'
        assert data['verify']['static_ip'] == '192.168.1.13'
        assert data['verify']['hostname'] == 'owl-3.local'

    def test_successful_switch_sequence(self, ctrl_client, ctrl_state, nm):
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        nm.get_ip4.return_value = '192.168.1.13'
        CapturingTimer.fire_all()

        nm.set_hostname.assert_called_once_with('owl-3',
                                                ctrl_state._system_runner)
        nm.add_wifi_connection.assert_called_once_with(
            'RigNet', 'rigpass123', static_ip='192.168.1.13',
            gateway='192.168.1.1', prefix=24, dns=None)
        nm.hotspot_down.assert_called_once()
        restart_calls = [c for c in ctrl_state._system_runner.mock_calls
                         if c.args and c.args[0] == ['systemctl', 'restart',
                                                     'owl.service']]
        assert restart_calls

        result = ctrl_client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'connected'
        assert result['mode'] == 'controller'
        assert result['device_id'] == 'owl-3'
        assert result['static_ip'] == '192.168.1.13'

    def test_ini_written_and_other_sections_preserved(self, ctrl_client,
                                                      ctrl_state):
        import configparser
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        CapturingTimer.fire_all()

        config = configparser.ConfigParser()
        config.read(ctrl_state.controller_ini)
        assert config.get('MQTT', 'broker_ip') == '192.168.1.2'
        assert config.get('MQTT', 'device_id') == 'owl-3'
        assert config.get('Network', 'mode') == 'networked'
        assert config.get('Network', 'static_ip') == '192.168.1.13'
        assert config.get('Network', 'controller_ip') == '192.168.1.2'
        assert config.get('WebDashboard', 'port') == '8000'

    def test_failed_activation_reverts_but_keeps_config(self, ctrl_client,
                                                        ctrl_state, nm):
        import configparser
        nm.activate_connection.return_value = False
        nm.last_error = 'auth'
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        CapturingTimer.fire_all()

        nm.delete_connection.assert_called_once_with('RigNet')
        nm.hotspot_up.assert_called_once()
        result = ctrl_client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'failed'
        assert result['error'] == 'auth'
        # Target config deliberately kept for the retry
        config = configparser.ConfigParser()
        config.read(ctrl_state.controller_ini)
        assert config.get('MQTT', 'device_id') == 'owl-3'

    def test_hostname_failure_reverts_without_profile_delete(self, ctrl_client,
                                                             nm):
        from utils.network_manager import NetworkManagerError
        nm.set_hostname.side_effect = NetworkManagerError('nope')
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        CapturingTimer.fire_all()

        nm.add_wifi_connection.assert_not_called()
        nm.delete_connection.assert_not_called()
        nm.hotspot_up.assert_called_once()
        result = ctrl_client.get('/setup/api/wifi/result').get_json()
        assert result['state'] == 'failed'

    def test_mode_fields_survive_reboot(self, ctrl_client, ctrl_state, nm):
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        nm.get_ip4.return_value = '192.168.1.13'
        CapturingTimer.fire_all()

        reloaded = FirstBootState(nm, flag_path=ctrl_state.flag_path,
                                  state_file=ctrl_state.state_file,
                                  timer_factory=CapturingTimer,
                                  system_runner=MagicMock(),
                                  exit_fn=MagicMock(),
                                  controller_ini=ctrl_state.controller_ini)
        assert reloaded.wifi_mode == 'controller'
        assert reloaded.join_device_id == 'owl-3'
        assert reloaded.join_static_ip == '192.168.1.13'


@pytest.mark.unit
class TestControllerFinish:
    def test_finish_requires_connected_controller_join(self, ctrl_client):
        response = ctrl_client.post('/setup/api/finish',
                                    json={'mode': 'controller'})
        assert response.status_code == 409

    def test_wifi_join_does_not_satisfy_controller_finish(self, ctrl_client,
                                                          ctrl_state):
        ctrl_state.wifi_state = 'connected'
        ctrl_state.wifi_mode = 'wifi'
        assert ctrl_client.post('/setup/api/finish',
                                json={'mode': 'controller'}).status_code == 409

    def test_finish_after_controller_join(self, ctrl_client, ctrl_state, nm):
        ctrl_client.post('/setup/api/controller/join', json=JOIN_PAYLOAD)
        nm.get_ip4.return_value = '192.168.1.13'
        CapturingTimer.fire_all()

        response = ctrl_client.post('/setup/api/finish',
                                    json={'mode': 'controller'})
        assert response.get_json()['success'] is True
        assert not ctrl_state.flag_path.exists()


@pytest.mark.unit
class TestDeviceToken:
    """finish() mints the per-device token (R3, contract 2): all three
    modes, returned in the finish response, written to CONTROLLER.ini
    [Security], rotated by a re-run."""

    def _read_token(self, state):
        import configparser
        config = configparser.ConfigParser()
        config.read(state.controller_ini)
        return config.get('Security', 'device_token', fallback=None)

    def test_standalone_finish_returns_and_persists_token(self, client, state):
        response = client.post('/setup/api/finish',
                               json={'mode': 'standalone',
                                     'new_password': 'paddock-relay-42'})
        data = response.get_json()
        token = data['device_token']
        assert token and len(token) > 30
        # Minted BEFORE the deferred re-key: the AP restart drops the phone
        # moments after this response, so the token must ride it.
        assert self._read_token(state) == token

    def test_wifi_finish_returns_and_persists_token(self, client, state):
        state.wifi_state = 'connected'
        data = client.post('/setup/api/finish',
                           json={'mode': 'wifi'}).get_json()
        assert data['device_token']
        assert self._read_token(state) == data['device_token']

    def test_refinish_rotates_token(self, client, state):
        first = client.post('/setup/api/finish',
                            json={'mode': 'standalone',
                                  'new_password': 'paddock-relay-42'}
                            ).get_json()['device_token']
        second = client.post('/setup/api/finish',
                             json={'mode': 'standalone',
                                   'new_password': 'paddock-relay-42'}
                             ).get_json()['device_token']
        assert first != second
        assert self._read_token(state) == second

    def test_mint_preserves_other_sections(self, client, state):
        state.controller_ini.write_text(
            '[MQTT]\nenable = True\nbroker_ip = localhost\n'
            '[GPS]\nenable = False\n')
        state.wifi_state = 'connected'
        client.post('/setup/api/finish', json={'mode': 'wifi'})
        import configparser
        config = configparser.ConfigParser()
        config.read(state.controller_ini)
        assert config.get('MQTT', 'broker_ip') == 'localhost'
        assert config.get('GPS', 'enable') == 'False'
        assert config.get('Security', 'device_token')

    def test_failed_finish_mints_nothing(self, client, state):
        client.post('/setup/api/finish', json={'mode': 'wifi'})  # 409
        assert self._read_token(state) is None


@pytest.mark.unit
class TestDeviceTokenControllerMode:
    def test_controller_finish_returns_and_persists_token(self, ctrl_client,
                                                          ctrl_state):
        ctrl_state.wifi_state = 'connected'
        ctrl_state.wifi_mode = 'controller'
        data = ctrl_client.post('/setup/api/finish',
                                json={'mode': 'controller'}).get_json()
        assert data['device_token']
        import configparser
        config = configparser.ConfigParser()
        config.read(ctrl_state.controller_ini)
        assert config.get('Security', 'device_token') == data['device_token']
        # Networked-mode identity keys survive the mint untouched
        assert config.get('WebDashboard', 'port') == '8000'


@pytest.mark.unit
class TestControllerIniOwnership:
    """v3.11.1: the firstboot service runs as root; mkstemp creates
    root:root 0600 files and configparser silently skips unreadable files,
    which disabled MQTT + the token guard on the whole device (2026-08-15
    bench). Every CONTROLLER.ini write must hand the file back."""

    def test_atomic_write_fixes_permissions(self, state):
        import configparser
        from unittest.mock import patch
        config = configparser.ConfigParser()
        config.add_section('MQTT')
        config.set('MQTT', 'enable', 'True')
        with patch.object(state, '_fix_ini_permissions') as fix:
            state._atomic_write_ini(config)
        fix.assert_called_once()
        assert state.controller_ini.exists()

    def test_mint_token_fixes_permissions(self, state):
        from unittest.mock import patch
        with patch.object(state, '_fix_ini_permissions') as fix:
            state._mint_device_token()
        fix.assert_called_once()

    def test_fix_ini_permissions_sets_mode(self, state, tmp_path):
        import os
        import stat as stat_mod
        state.controller_ini.write_text('[MQTT]\nenable = True\n')
        state._fix_ini_permissions()
        if hasattr(os, 'chown'):
            mode = stat_mod.S_IMODE(os.stat(state.controller_ini).st_mode)
            assert mode == 0o640
        # On Windows chmod is advisory — reaching here without raising is the test

    def test_fix_ini_permissions_survives_missing_file(self, state):
        # Never raises even when the file vanished (best-effort)
        assert not state.controller_ini.exists()
        state._fix_ini_permissions()
