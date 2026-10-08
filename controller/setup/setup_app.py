"""
OWL first-boot setup API.

A deliberately small Flask app served only while the first-boot flag
exists (see owl-firstboot.service, ConditionPathExists). The OWL phone
app talks to it over the OWL-XXXX hotspot at https://10.42.0.1:8088.

Transport and session (contract 5, cross-repo review 2026-09-17, O-5):
  - HTTPS with the dashboard's self-signed certificate
    (/etc/ssl/certs/owl.crt). The hotspot PSK during setup is the public
    `owl-setup`, so plain HTTP let anyone nearby read the Wi-Fi and Noktura
    passwords the app sends. An active impersonator is still possible until
    the app pins the unit's certificate (trust chain Phase A).
  - A setup session: the first phone to POST /setup/api/session gets a
    token; every route that changes the unit requires it in X-Setup-Session.
    A second phone gets 409 until the session has been idle for
    SETUP_SESSION_IDLE_S or the service restarts. Read-only routes stay open
    so the app can identify the unit before starting.

It is NOT the standalone dashboard: no MQTT, no gunicorn/nginx, no
dashboard state. Camera frames are proxied from owl.py's streaming
server on :8001 (same source the dashboards use).

Run directly (dev, any platform — degrades gracefully without nmcli):
    python controller/setup/setup_app.py
"""

import functools
import ipaddress
import logging
import os
import re
import secrets
import socket
import threading
import time
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, Response, jsonify, request

# Project root on sys.path so utils/ and version.py import when run directly
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.network_manager import NetworkManager, NetworkManagerError  # noqa: E402
from utils.device_identity import get_device_serial, read_identity  # noqa: E402
from utils.cloud_provision import (  # noqa: E402
    CloudProvisionError, CloudSiteConflict, NetworkedOwlRefused, clock_synced)
from controller.setup.firstboot_state import FirstBootState, SETUP_PORT  # noqa: E402
from version import VERSION, APP_CONTRACT_VERSION  # noqa: E402

logger = logging.getLogger(__name__)

OWL_FRAME_URL = 'http://127.0.0.1:8001/latest_frame.jpg'
OWL_STREAM_URL = 'http://127.0.0.1:8001/stream.mjpg'

TLS_CERT = os.environ.get('OWL_SETUP_TLS_CERT', '/etc/ssl/certs/owl.crt')
TLS_KEY = os.environ.get('OWL_SETUP_TLS_KEY', '/etc/ssl/private/owl.key')
SETUP_SESSION_HEADER = 'X-Setup-Session'
SETUP_SESSION_IDLE_S = 600


class SetupSession:
    """One phone at a time drives setup. In memory: a service restart (reboot,
    re-arm) frees it, and so does SETUP_SESSION_IDLE_S without a request."""

    def __init__(self, clock=time.monotonic, idle_s=SETUP_SESSION_IDLE_S):
        self._clock = clock
        self._idle_s = idle_s
        self._token = None
        self._last_used = 0.0
        self._lock = threading.Lock()

    def _active(self):
        return self._token is not None and self._clock() - self._last_used < self._idle_s

    def open(self, presented):
        """(token, created) for the caller, or (None, False) when another
        phone holds an active session."""
        with self._lock:
            if self._active():
                if presented and secrets.compare_digest(presented, self._token):
                    self._last_used = self._clock()
                    return self._token, False
                return None, False
            self._token = secrets.token_urlsafe(24)
            self._last_used = self._clock()
            return self._token, True

    def check(self, presented):
        with self._lock:
            if not presented or not self._active() or \
                    not secrets.compare_digest(presented, self._token):
                return False
            self._last_used = self._clock()
            return True


def get_owl_service_status():
    """'active' | 'inactive' | 'unknown' for owl.service."""
    try:
        result = subprocess.run(['systemctl', 'is-active', 'owl.service'],
                                capture_output=True, text=True, timeout=5)
        return (result.stdout or '').strip() or 'unknown'
    except (OSError, subprocess.TimeoutExpired):
        return 'unknown'


def check_camera():
    """Camera works iff owl.py is serving frames on :8001."""
    try:
        with urllib.request.urlopen(OWL_FRAME_URL, timeout=2) as response:
            data = response.read()
        if data:
            return {'ok': True, 'detail': 'frame received'}
        return {'ok': False, 'detail': 'empty frame'}
    except (urllib.error.URLError, OSError) as e:
        return {'ok': False, 'detail': f'no frame from OWL ({e})'}


def create_app(network_manager=None, state=None, flag_path=None, state_file=None,
               session=None, clock_probe=None):
    """App factory. Tests inject a mocked network_manager/state/session."""
    app = Flask(__name__)

    nm = network_manager or NetworkManager()
    fb = state or FirstBootState(nm, flag_path=flag_path, state_file=state_file)
    setup_session = session or SetupSession()
    probe_clock = clock_probe or clock_synced
    app.config['firstboot_state'] = fb
    app.config['network_manager'] = nm
    app.config['setup_session'] = setup_session

    def require_session(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not setup_session.check(request.headers.get(SETUP_SESSION_HEADER, '')):
                return jsonify({'success': False, 'error': 'setup_session_required',
                                'detail': 'Start a setup session first '
                                          '(POST /setup/api/session).'}), 403
            return fn(*args, **kwargs)
        return wrapper

    # The phone app's WebView loads from file:// with universal access, so
    # it is not subject to CORS at all. These headers exist only for
    # browser-based development (npm run serve / Playwright); anything else
    # gets no CORS grant — a random website in a browser on the OWL's
    # network cannot script this API.
    allowed_cors_origins = {
        'null',                     # file:// pages that do send an Origin
        'http://localhost:4180',    # owl-app dev server
        'http://127.0.0.1:4180',
    }

    @app.after_request
    def add_cors_headers(response):
        origin = request.headers.get('Origin')
        if origin in allowed_cors_origins:
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Vary'] = 'Origin'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Setup-Session'
        return response

    # Flask answers OPTIONS preflights automatically for defined routes;
    # after_request above decorates them with the CORS headers.

    @app.before_request
    def mark_setup_started():
        if request.method != 'OPTIONS':
            fb.mark_in_setup()

    # ------------------------------------------------------------------
    # Device info + camera
    # ------------------------------------------------------------------
    @app.route('/setup/api/info')
    def info():
        hostname = socket.gethostname()
        try:
            hotspot = nm.get_hotspot_connection() if nm.is_supported() else None
            ip = nm.get_ip4() if nm.is_supported() else None
        except NetworkManagerError:
            hotspot, ip = None, None
        return jsonify({
            'success': True,
            'device_id': hostname,
            'hostname': hostname,
            # Unique per physical unit (hostnames like owl-1 are not) —
            # the phone app's saved-device identity. Additive field.
            'device_serial': get_device_serial(),
            # Trust-chain identity (additive): tier otp|disk|none, the
            # SHA-256 fingerprint of the unit's P-256 public key, and the
            # Noktura serial if the unit was registered before shipping.
            # The app compares the fingerprint with Noktura's record.
            'identity': read_identity(),
            'version': str(VERSION),
            'contract_version': APP_CONTRACT_VERSION,
            'hotspot': {'ssid': hotspot, 'ip': ip},
            'camera': check_camera(),
            'owl_service': get_owl_service_status(),
            'state': fb.state,
            'cloud': fb.cloud_status(),
            # False: NTP has not set the clock, so the cloud bridge's TLS
            # cannot connect yet (no RTC). None: unknown.
            'clock_synced': probe_clock(),
        })

    # ------------------------------------------------------------------
    # Setup session (one phone drives setup)
    # ------------------------------------------------------------------
    @app.route('/setup/api/session', methods=['POST'])
    def open_session():
        token, created = setup_session.open(request.headers.get(SETUP_SESSION_HEADER, ''))
        if token is None:
            return jsonify({'success': False, 'error': 'setup_in_use',
                            'detail': 'Setup is in progress on another phone.'}), 409
        return jsonify({'success': True, 'session': token,
                        'idle_timeout_s': SETUP_SESSION_IDLE_S}), (201 if created else 200)

    @app.route('/setup/api/identity')
    def identity():
        # Same body as the dashboard's GET /api/identity, available during
        # first boot: the phone app sends this PEM to Noktura when a maker
        # registers a unit Noktura has never seen (community registration,
        # decided 2026-09-17). A public key is public; read-only, no session.
        return jsonify({'success': True, 'identity': read_identity(include_pem=True)})

    @app.route('/setup/api/camera/frame')
    def camera_frame():
        # Proxy of owl.py's frame server — same pattern as
        # controller/standalone/standalone.py download_frame()
        try:
            with urllib.request.urlopen(OWL_FRAME_URL, timeout=2) as response:
                frame_data = response.read()
            return Response(frame_data, mimetype='image/jpeg')
        except (urllib.error.URLError, OSError) as e:
            logger.error("Error proxying frame: %s", e)
            return jsonify({'success': False,
                            'error': 'Failed to retrieve frame from OWL. '
                                     'Is it running?'}), 503

    @app.route('/setup/api/camera/diagnostics')
    def camera_diagnostics():
        # Sensor-level triage for when check_camera() fails: distinguishes
        # "overlay missing from config.txt" from an I2C probe failure
        # (see firstboot_state.camera_diagnostics). Also curl-able on the
        # bench, which is the point — after any re-flash, verify config.txt
        # state before debugging hardware.
        diag = fb.camera_diagnostics()
        diag['success'] = True
        return jsonify(diag)

    @app.route('/setup/api/camera/stream')
    def camera_stream():
        def generate():
            try:
                resp = urllib.request.urlopen(OWL_STREAM_URL, timeout=5)
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    yield chunk
            except (urllib.error.URLError, OSError):
                pass

        # Boundary must match the upstream stream's (--FRAME, see
        # utils/video_manager.py StreamingHandler) — the proxy passes the
        # body through verbatim, and a mismatch renders as a blank image.
        return Response(generate(),
                        mimetype='multipart/x-mixed-replace; boundary=FRAME')

    # ------------------------------------------------------------------
    # WiFi
    # ------------------------------------------------------------------
    @app.route('/setup/api/wifi/scan')
    def wifi_scan():
        rescan = request.args.get('rescan', '').lower() == 'true'
        try:
            if rescan:
                # Warned in the app UI: this drops the phone's connection
                networks = fb.refresh_scan_cache()
            else:
                networks = fb.scan_cache
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        return jsonify({'success': True, 'networks': networks,
                        'cached': not rescan, 'scanned_at': fb.scanned_at})

    @app.route('/setup/api/wifi/join', methods=['POST'])
    @require_session
    def wifi_join():
        data = request.get_json(silent=True) or {}
        ssid = (data.get('ssid') or '').strip()
        password = data.get('password') or ''
        if not ssid:
            return jsonify({'success': False, 'error': 'ssid is required'}), 400
        if password and len(password) < 8:
            return jsonify({'success': False,
                            'error': 'WiFi passwords are at least 8 characters'}), 400
        try:
            fb.request_join(ssid, password or None)
        except RuntimeError as e:
            return jsonify({'success': False, 'error': str(e)}), 409
        return jsonify({
            'success': True,
            'state': 'pending',
            'switch_delay_s': 5,
            'verify': {'port': SETUP_PORT,
                       'hostname': f'{socket.gethostname()}.local',
                       'service_type': '_owl-setup._tcp'},
        }), 202

    @app.route('/setup/api/controller/join', methods=['POST'])
    @require_session
    def controller_join():
        """Join a rig: identity + static IP assigned by the controller's
        /api/fleet/reserve, relayed by the phone app."""
        data = request.get_json(silent=True) or {}
        required = ('ssid', 'device_id', 'static_ip', 'gateway', 'broker_ip')
        missing = [key for key in required if not (data.get(key) or '').strip()]
        if missing:
            return jsonify({'success': False,
                            'error': f"Missing fields: {', '.join(missing)}"}), 400
        if not re.match(r'^[a-z0-9][a-z0-9-]{0,62}$', data['device_id']):
            return jsonify({'success': False,
                            'error': 'Invalid device_id'}), 400
        for key in ('static_ip', 'gateway', 'broker_ip'):
            try:
                ipaddress.ip_address(data[key])
            except ValueError:
                return jsonify({'success': False,
                                'error': f'Invalid {key}'}), 400
        password = data.get('password') or ''
        if password and len(password) < 8:
            return jsonify({'success': False,
                            'error': 'WiFi passwords are at least 8 characters'}), 400
        try:
            subnet_prefix = int(data.get('subnet_prefix', 24))
            broker_port = int(data.get('broker_port', 1883))
        except (TypeError, ValueError):
            return jsonify({'success': False,
                            'error': 'subnet_prefix and broker_port must be '
                                     'integers'}), 400
        if not 1 <= subnet_prefix <= 32:
            return jsonify({'success': False,
                            'error': 'Invalid subnet_prefix'}), 400
        if not 1 <= broker_port <= 65535:
            return jsonify({'success': False,
                            'error': 'Invalid broker_port'}), 400
        dns = data.get('dns')
        if dns:
            try:
                ipaddress.ip_address(dns)
            except ValueError:
                return jsonify({'success': False, 'error': 'Invalid dns'}), 400
        try:
            fb.request_join_controller({
                'ssid': data['ssid'].strip(),
                'password': password or None,
                'device_id': data['device_id'],
                'static_ip': data['static_ip'],
                'gateway': data['gateway'],
                'subnet_prefix': subnet_prefix,
                'dns': dns,
                'broker_ip': data['broker_ip'],
                'broker_port': broker_port,
            })
        except RuntimeError as e:
            return jsonify({'success': False, 'error': str(e)}), 409
        return jsonify({
            'success': True,
            'state': 'pending',
            'switch_delay_s': 5,
            'verify': {'port': SETUP_PORT,
                       'static_ip': data['static_ip'],
                       'hostname': f"{data['device_id']}.local",
                       'service_type': '_owl-setup._tcp'},
        }), 202

    @app.route('/setup/api/wifi/result')
    def wifi_result():
        result = fb.wifi_result()
        result['success'] = True
        return jsonify(result)

    @app.route('/setup/api/hotspot/restore', methods=['POST'])
    @require_session
    def hotspot_restore():
        try:
            fb.restore_hotspot()
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        return jsonify({'success': True})

    # ------------------------------------------------------------------
    # Status + finish
    # ------------------------------------------------------------------
    @app.route('/setup/api/status')
    def status():
        try:
            network = nm.status() if nm.is_supported() else \
                {'mode': 'unknown', 'ssid': None, 'ip': None}
        except NetworkManagerError as e:
            network = {'mode': 'unknown', 'ssid': None, 'ip': None,
                       'error': str(e)}
        return jsonify({'success': True, 'state': fb.state,
                        'flag_armed': fb.flag_path.exists(),
                        'network': network,
                        'device_id': socket.gethostname()})

    @app.route('/setup/api/finish', methods=['POST'])
    @require_session
    def finish():
        data = request.get_json(silent=True) or {}
        mode = data.get('mode')
        try:
            device_token = fb.finish(mode, new_password=data.get('new_password'))
        except ValueError as e:
            code = 409 if mode in ('wifi', 'controller') else 400
            return jsonify({'success': False, 'error': str(e)}), code
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        return jsonify({'success': True, 'mode': mode,
                        'device_token': device_token})

    # ------------------------------------------------------------------
    # Cloud (Noktura) — the app carries the bridge credential it received
    # from POST /api/v1/me/devices/<serial>/cloud-credential onto the
    # hotspot and hands it to the unit here. Root work happens in the
    # owl-cloud-provision helper (install_firstboot.sh) via sudo -n.
    # ------------------------------------------------------------------
    @app.route('/setup/api/cloud/provision', methods=['POST'])
    @require_session
    def cloud_provision():
        data = request.get_json(silent=True) or {}
        try:
            result = fb.provision_cloud(data)
        except NetworkedOwlRefused as e:
            return jsonify({'success': False, 'error': e.code, 'detail': str(e)}), 409
        except CloudSiteConflict as e:
            return jsonify({'success': False, 'error': 'cloud_site_conflict',
                            'detail': str(e), 'existing_site': e.existing_site}), 409
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        except CloudProvisionError as e:
            return jsonify({'success': False, 'error': str(e),
                            'detail': e.detail}), 500
        return jsonify({'success': True, 'cloud': result})

    return app


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    app = create_app()
    app.config['firstboot_state'].startup()
    port = int(os.environ.get('OWL_SETUP_PORT', SETUP_PORT))
    ssl_context = None
    if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        ssl_context = (TLS_CERT, TLS_KEY)
        logger.info("OWL first-boot setup API on https://0.0.0.0:%d", port)
    else:
        # Only dev machines lack the dashboard certificate: every unit gets it
        # from controller/shared/setup.sh before first boot can be armed.
        logger.error("TLS certificate %s or key %s missing: serving setup over "
                     "PLAIN HTTP. Re-run controller/shared/setup.sh on a real unit.",
                     TLS_CERT, TLS_KEY)
    # Flask dev server is fine here: one client, short-lived service;
    # threaded so the MJPEG proxy doesn't block API calls. Werkzeug runs the
    # TLS handshake inside accept() on the one serving thread, so a client
    # that connects and never says hello would freeze every request: the
    # default socket timeout bounds that stall.
    socket.setdefaulttimeout(10)
    app.run(host='0.0.0.0', port=port, threaded=True, ssl_context=ssl_context)


if __name__ == '__main__':
    main()
