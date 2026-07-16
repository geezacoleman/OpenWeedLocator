"""
OWL first-boot setup API.

A deliberately small Flask app served only while the first-boot flag
exists (see owl-firstboot.service, ConditionPathExists). The OWL phone
app talks to it over the OWL-XXXX hotspot at http://10.42.0.1:8088.

It is NOT the standalone dashboard: no MQTT, no gunicorn/nginx, no
dashboard state. Camera frames are proxied from owl.py's streaming
server on :8001 (same source the dashboards use).

Run directly (dev, any platform — degrades gracefully without nmcli):
    python controller/setup/setup_app.py
"""

import ipaddress
import logging
import os
import re
import socket
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
from controller.setup.firstboot_state import FirstBootState, SETUP_PORT  # noqa: E402
from version import VERSION, APP_CONTRACT_VERSION  # noqa: E402

logger = logging.getLogger(__name__)

OWL_FRAME_URL = 'http://127.0.0.1:8001/latest_frame.jpg'
OWL_STREAM_URL = 'http://127.0.0.1:8001/stream.mjpg'


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


def create_app(network_manager=None, state=None, flag_path=None, state_file=None):
    """App factory. Tests inject a mocked network_manager/state."""
    app = Flask(__name__)

    nm = network_manager or NetworkManager()
    fb = state or FirstBootState(nm, flag_path=flag_path, state_file=state_file)
    app.config['firstboot_state'] = fb
    app.config['network_manager'] = nm

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
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
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
            'version': str(VERSION),
            'contract_version': APP_CONTRACT_VERSION,
            'hotspot': {'ssid': hotspot, 'ip': ip},
            'camera': check_camera(),
            'owl_service': get_owl_service_status(),
            'state': fb.state,
        })

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

        return Response(generate(),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

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
    def finish():
        data = request.get_json(silent=True) or {}
        mode = data.get('mode')
        try:
            fb.finish(mode, new_password=data.get('new_password'))
        except ValueError as e:
            code = 409 if mode in ('wifi', 'controller') else 400
            return jsonify({'success': False, 'error': str(e)}), code
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        return jsonify({'success': True, 'mode': mode})

    return app


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')
    app = create_app()
    app.config['firstboot_state'].startup()
    port = int(os.environ.get('OWL_SETUP_PORT', SETUP_PORT))
    logger.info("OWL first-boot setup API on 0.0.0.0:%d", port)
    # Flask dev server is fine here: one client, short-lived service;
    # threaded so the MJPEG proxy doesn't block API calls.
    app.run(host='0.0.0.0', port=port, threaded=True)


if __name__ == '__main__':
    main()
