"""
Device-settings routes for the standalone dashboard.

Network reconfig, password changes, and power control for the phone app.
Mutating routes require the per-device token minted at provisioning
(CONTROLLER.ini [Security] device_token) via the X-Device-Token header.

Token semantics:
- No token on the device -> allow (legacy units keep working headerless).
- Token present + wrong/missing header -> 403.
- NO localhost bypass: nginx proxies every request from 127.0.0.1, so a
  bypass would neuter the auth entirely.

Built as a blueprint factory (like setup_app.create_app) so tests can
exercise it against a bare Flask app with injected fakes.
"""

import configparser
import functools
import logging
import os
import secrets
import shutil
import socket
import subprocess
import threading

from flask import Blueprint, jsonify, request

from utils.network_manager import NetworkManagerError
from controller.standalone.network_settings import SWITCH_DELAY_S

PASSWORD_HELPER = '/usr/local/sbin/owl-set-user-password'
MIN_PASSWORD_LENGTH = 8
HOTSPOT_APPLY_DELAY_S = 5   # lets the 202 reach the phone before the AP re-keys
POWER_DELAY_S = 3           # lets the response flush before shutdown/reboot

PERMISSIONS_MISSING_ERROR = ('Device settings permissions not installed. '
                             'Re-run controller/shared/setup.sh on the OWL.')


def make_token_reader(controller_ini_path):
    """Return a zero-arg callable yielding the current device token (or None).

    mtime-cached: CONTROLLER.ini is only re-parsed when it changes, so
    per-request calls cost one stat().
    """
    cache = {'mtime': None, 'token': None}

    def read_token():
        try:
            mtime = os.path.getmtime(controller_ini_path)
        except OSError:
            cache['mtime'] = None
            cache['token'] = None
            return None
        if mtime != cache['mtime']:
            config = configparser.ConfigParser()
            config.read(controller_ini_path)
            cache['token'] = config.get('Security', 'device_token',
                                        fallback=None) or None
            cache['mtime'] = mtime
        return cache['token']

    return read_token


def check_device_token(token_reader):
    """Shared guard body: returns a 403 Flask response when a token exists
    on the device and the request's X-Device-Token doesn't match; None when
    the request may proceed (match, or legacy no-token device)."""
    expected = token_reader() if token_reader else None
    if expected:
        provided = request.headers.get('X-Device-Token', '')
        if not secrets.compare_digest(provided, expected):
            return jsonify({'success': False,
                            'error': 'Invalid or missing device token'}), 403
    return None


def require_device_token(token_reader):
    """Decorator factory wrapping check_device_token."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            denied = check_device_token(token_reader)
            if denied is not None:
                return denied
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def create_settings_blueprint(manager, token_reader, mqtt_getter, logger=None,
                              controller_ini_path=None, owl_active_getter=None,
                              runner=subprocess.run,
                              timer_factory=threading.Timer):
    """Build the device-settings blueprint.

    manager: NetworkSettingsManager (owns the nmcli wrapper as manager.nm)
    token_reader: callable -> current device token or None
    mqtt_getter: callable -> DashMQTTSubscriber or None
    controller_ini_path: used for the networked-mode join guard
    owl_active_getter: callable -> bool, is owl.service active (MQTT-first
        power path only works when owl.py is listening)
    runner/timer_factory: injectable for tests
    """
    log = logger or logging.getLogger(__name__)
    bp = Blueprint('settings', __name__)
    guard = require_device_token(token_reader)

    def _network_mode():
        if not controller_ini_path:
            return 'standalone'
        config = configparser.ConfigParser()
        config.read(controller_ini_path)
        return config.get('Network', 'mode', fallback='standalone').strip().lower()

    def _nm_status():
        nm = manager.nm
        if not nm.is_supported():
            return {'mode': 'unknown', 'ssid': None, 'ip': None}
        try:
            return nm.status()
        except NetworkManagerError as e:
            return {'mode': 'unknown', 'ssid': None, 'ip': None,
                    'error': str(e)}

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------
    @bp.route('/api/settings/network/status')
    def network_status():
        return jsonify({'success': True, 'network': _nm_status(),
                        'switch': manager.result()})

    @bp.route('/api/settings/network/scan', methods=['POST'])
    @guard
    def network_scan():
        data = request.get_json(silent=True) or {}
        rescan = bool(data.get('rescan', True))
        mode = _nm_status().get('mode')
        if mode == 'hotspot' and rescan:
            # The radio cannot scan in AP mode: cycle it in the background.
            # The phone drops for ~15 s and re-fetches results afterwards.
            manager.start_hotspot_scan()
            return jsonify({'success': True, 'state': 'scanning',
                            'warning': 'hotspot_cycles'}), 202
        try:
            networks = manager.scan_client(rescan=rescan)
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        return jsonify({'success': True, 'networks': networks,
                        'scanned_at': manager.scanned_at})

    @bp.route('/api/settings/network/scan')
    def network_scan_results():
        results = manager.scan_results()
        results['success'] = True
        return jsonify(results)

    @bp.route('/api/settings/network/join', methods=['POST'])
    @guard
    def network_join():
        data = request.get_json(silent=True) or {}
        ssid = (data.get('ssid') or '').strip()
        password = data.get('password') or ''
        if not ssid:
            return jsonify({'success': False, 'error': 'ssid is required'}), 400
        if password and len(password) < MIN_PASSWORD_LENGTH:
            return jsonify({'success': False,
                            'error': 'WiFi passwords are at least 8 characters'}), 400
        if _network_mode() == 'networked':
            # Fleet OWLs hold controller-assigned static IPs; changing the
            # network here would break rig membership. The rig controller
            # owns that flow.
            return jsonify({'success': False,
                            'error': 'This OWL is part of a rig. Manage its '
                                     'network from the rig controller.'}), 409
        try:
            manager.request_join(ssid, password or None)
        except RuntimeError as e:
            return jsonify({'success': False, 'error': str(e)}), 409
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        return jsonify({
            'success': True,
            'state': 'pending',
            'switch_delay_s': SWITCH_DELAY_S,
            'verify': {'hostname': f'{socket.gethostname()}.local',
                       'port': 443},
        }), 202

    @bp.route('/api/settings/network/result')
    def network_result():
        return jsonify(manager.result())

    @bp.route('/api/settings/network/cancel', methods=['POST'])
    @guard
    def network_cancel():
        try:
            manager.cancel()
        except RuntimeError as e:
            return jsonify({'success': False, 'error': str(e)}), 409
        return jsonify({'success': True})

    # ------------------------------------------------------------------
    # Passwords
    # ------------------------------------------------------------------
    @bp.route('/api/settings/password/user', methods=['POST'])
    @guard
    def set_user_password():
        data = request.get_json(silent=True) or {}
        password = data.get('password') or ''
        if len(password) < MIN_PASSWORD_LENGTH:
            return jsonify({'success': False,
                            'error': 'Password must be at least 8 characters'}), 400
        if not os.path.exists(PASSWORD_HELPER):
            return jsonify({'success': False,
                            'error': PERMISSIONS_MISSING_ERROR}), 503
        try:
            # Password travels on stdin only: never argv, never logged.
            result = runner(['/usr/bin/sudo', '-n', PASSWORD_HELPER],
                            input=password + '\n', capture_output=True,
                            text=True, timeout=15)
        except Exception as e:
            log.error(f"User password change failed: {type(e).__name__}")
            return jsonify({'success': False,
                            'error': 'Password change failed'}), 500
        if result.returncode != 0:
            error = (result.stderr or '').strip() or 'Password change failed'
            log.error(f"User password change failed: {error}")
            return jsonify({'success': False, 'error': error}), 500
        log.info("Pi user password changed via device settings")
        return jsonify({'success': True})

    @bp.route('/api/settings/password/hotspot', methods=['POST'])
    @guard
    def set_hotspot_password():
        data = request.get_json(silent=True) or {}
        password = data.get('password') or ''
        if len(password) < MIN_PASSWORD_LENGTH:
            return jsonify({'success': False,
                            'error': 'Password must be at least 8 characters'}), 400
        mode = _nm_status().get('mode')
        if mode == 'hotspot':
            # Re-keying restarts the AP and drops the phone, so the 202 must
            # reach it first (same delayed-switch trick as join).
            def _apply():
                try:
                    manager.nm.set_hotspot_password(password)
                    log.info("Hotspot password re-keyed")
                except Exception as e:
                    log.error(f"Delayed hotspot re-key failed: {e}")

            timer = timer_factory(HOTSPOT_APPLY_DELAY_S, _apply)
            timer.daemon = True
            timer.start()
            return jsonify({'success': True,
                            'applied_in_s': HOTSPOT_APPLY_DELAY_S}), 202
        try:
            # Client mode: the AP is dormant, so modify the profile only.
            # con up here would steal the radio from the client network.
            manager.nm.set_hotspot_password(password, reapply=False)
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        log.info("Hotspot password updated (profile only, AP dormant)")
        return jsonify({'success': True})

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------
    def _power_action(action):
        """MQTT-first (owl.py runs the sudo command with clean shutdown of
        detection); direct delayed sudo fallback when owl.py isn't there
        to hear the command."""
        mqtt = mqtt_getter() if mqtt_getter else None
        owl_active = bool(owl_active_getter()) if owl_active_getter else False
        if mqtt and owl_active:
            try:
                mqtt._send_command(action)
                return jsonify({'success': True, 'via': 'mqtt'})
            except Exception as e:
                log.error(f"MQTT {action} failed, using direct path: {e}")

        binary = shutil.which(action) or f'/usr/sbin/{action}'
        argv = ['/usr/bin/sudo', '-n', binary]
        if action == 'shutdown':
            argv.append('now')

        def _delayed():
            try:
                runner(argv, capture_output=True, text=True, timeout=15)
            except Exception as e:
                log.error(f"Direct {action} failed: {e}")

        timer = timer_factory(POWER_DELAY_S, _delayed)
        timer.daemon = True
        timer.start()
        return jsonify({'success': True, 'via': 'direct',
                        'delay_s': POWER_DELAY_S})

    @bp.route('/api/system/shutdown', methods=['POST'])
    @guard
    def system_shutdown():
        return _power_action('shutdown')

    @bp.route('/api/system/reboot', methods=['POST'])
    @guard
    def system_reboot():
        return _power_action('reboot')

    return bp
