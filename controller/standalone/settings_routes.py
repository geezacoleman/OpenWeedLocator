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
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from utils.network_manager import NetworkManagerError
from utils import software_update
from utils.internet_probe import probe_internet
from controller.standalone.network_settings import (
    SWITCH_DELAY_S, TEMP_CON_NAME, TEMP_REVERT_DELAY_S, TEMP_TTL_S)

PASSWORD_HELPER = '/usr/local/sbin/owl-set-user-password'
MIN_PASSWORD_LENGTH = 8
HOTSPOT_APPLY_DELAY_S = 5   # lets the 202 reach the phone before the AP re-keys
POWER_DELAY_S = 3           # lets the response flush before shutdown/reboot
POWER_BACKSTOP_DELAY_S = 10  # direct sudo backstop after a successful MQTT send

PERMISSIONS_MISSING_ERROR = ('Device settings permissions not installed. '
                             'Re-run controller/shared/setup.sh on the OWL.')
UPDATE_PERMISSIONS_MISSING_ERROR = ('Software update permissions not installed. '
                                    'Re-run controller/shared/setup.sh on the OWL.')

# The dashboard runs OLD code after owl_update.sh finishes (the script only
# restarts owl.service), so the status route restarts it once it has
# reported `complete` to the caller.
DASHBOARD_UNIT = 'owl-dash.service'
DASHBOARD_RESTART_DELAY_S = 2

# Token recovery brute-force guard: after this many wrong hotspot passwords,
# the route locks for TOKEN_RECOVERY_LOCK_S.
TOKEN_RECOVERY_MAX_FAILURES = 5
TOKEN_RECOVERY_LOCK_S = 300


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
            read_ok = config.read(controller_ini_path)
            if not read_ok:
                # configparser silently skips unreadable files — that would
                # quietly disable the token guard (legacy-allow) on a device
                # that HAS a token, so shout once per mtime change.
                logging.getLogger(__name__).error(
                    "CONTROLLER.ini exists but could not be read (check "
                    "ownership/permissions: ls -l %s) - device token guard "
                    "is disabled", controller_ini_path)
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


def _git_info():
    """{'branch', 'commit'} of the running checkout; empty when git is
    unavailable. Computed once per process: it restarts after an update."""
    try:
        from version import SystemInfo
        return dict(SystemInfo.get_git_info() or {})
    except Exception:
        return {}


def _software_version():
    try:
        from version import VERSION
        return str(VERSION)
    except Exception:
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
                              timer_factory=threading.Timer,
                              internet_probe=None, update_state_getter=None,
                              status_path=None, popen=None,
                              dashboard_unit=DASHBOARD_UNIT,
                              process_started_at=None, git_info_getter=None):
    """Build the device-settings blueprint.

    manager: NetworkSettingsManager (owns the nmcli wrapper as manager.nm)
    token_reader: callable -> current device token or None
    mqtt_getter: callable -> DashMQTTSubscriber or None
    controller_ini_path: used for the networked-mode join guard
    owl_active_getter: callable -> bool, is owl.service active (MQTT-first
        power path only works when owl.py is listening)
    runner/timer_factory: injectable for tests
    internet_probe: zero-arg callable -> utils.internet_probe result
    update_state_getter: zero-arg callable -> owl.py's published state (the
        data_transfer status gates an update)
    status_path/popen/dashboard_unit/process_started_at/git_info_getter:
        software-update plumbing (see /api/settings/update/*)
    """
    log = logger or logging.getLogger(__name__)
    bp = Blueprint('settings', __name__)
    guard = require_device_token(token_reader)

    probe = internet_probe or (lambda: probe_internet())
    update_status_file = (Path(status_path) if status_path
                          else software_update.status_path())
    started_at = (process_started_at if process_started_at is not None
                  else time.time())
    git_info = dict(git_info_getter() or {}) if git_info_getter else _git_info()
    restart_state = {'scheduled': False}

    def _fail(status, code, message):
        return jsonify({'success': False, 'error': message,
                        'code': code}), status

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
        temporary = bool(data.get('temporary', False))
        wait_s = data.get('wait_s')
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
            if temporary:
                # Phone-hotspot internet session: throwaway profile, long
                # retry window, watchdog-ended (see NetworkSettingsManager)
                manager.request_join(ssid, password or None, temporary=True,
                                     wait_s=wait_s)
            else:
                manager.request_join(ssid, password or None)
        except RuntimeError as e:
            return jsonify({'success': False, 'error': str(e)}), 409
        except NetworkManagerError as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        body = {
            'success': True,
            'state': 'pending',
            'switch_delay_s': SWITCH_DELAY_S,
            'verify': {'hostname': f'{socket.gethostname()}.local',
                       'port': 443},
        }
        if temporary:
            body.update({'temporary': True, 'wait_s': manager.wait_s,
                         'ttl_s': TEMP_TTL_S, 'con_name': TEMP_CON_NAME})
        return jsonify(body), 202

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

    @bp.route('/api/settings/network/revert', methods=['POST'])
    @guard
    def network_revert():
        """End a temporary (phone-hotspot) session: back to the OWL's own
        hotspot, throwaway profile deleted. 202: the teardown runs after
        a short delay so this response reaches the phone over the tether."""
        try:
            outcome = manager.revert()
        except RuntimeError as e:
            return jsonify({'success': False, 'error': str(e)}), 409
        return jsonify({'success': True, 'state': outcome,
                        'delay_s': TEMP_REVERT_DELAY_S}), 202

    @bp.route('/api/settings/internet')
    def internet_status():
        """Default route + one ping; ~4.5 s worst case."""
        return jsonify({'success': True, 'internet': probe()})

    # ------------------------------------------------------------------
    # Software update (owl_update.sh in a transient systemd unit)
    # ------------------------------------------------------------------
    def _default_ref():
        branch = git_info.get('branch')
        if not branch or branch == 'HEAD':
            return 'main'
        return branch

    def _version_payload():
        return {'version': _software_version(),
                'git': {'branch': git_info.get('branch'),
                        'commit': git_info.get('commit')}}

    @bp.route('/api/settings/update/start', methods=['POST'])
    @guard
    def update_start():
        data = request.get_json(silent=True) or {}
        ref = str(data.get('ref') or _default_ref())
        if not software_update.valid_ref(ref):
            return _fail(400, 'invalid_ref', f'Invalid ref: {ref!r}')
        current = software_update.read_status(update_status_file)
        if software_update.in_progress(current):
            return _fail(409, 'in_progress',
                         f"Update already in progress (status: {current['status']})")
        state = (update_state_getter() if update_state_getter else None) or {}
        transfer = (state.get('data_transfer') or {}).get('status', 'idle')
        if transfer not in ('idle', 'complete', 'error'):
            return _fail(409, 'data_transfer_busy',
                         f'Data transfer in progress (status: {transfer})')
        if not probe().get('reachable'):
            return _fail(409, 'no_internet',
                         'The OWL has no internet connection')
        request_id = secrets.token_hex(8)
        argv = software_update.update_argv(ref, request_id,
                                           status_file=update_status_file)
        if not software_update.sudo_permits(argv, runner):
            return _fail(503, 'permissions_missing',
                         UPDATE_PERMISSIONS_MISSING_ERROR)
        try:
            software_update.write_seed_status(update_status_file, request_id, ref)
            software_update.launch(argv, popen)
        except Exception as e:
            log.error(f"Software update failed to launch: {e}")
            return _fail(500, 'launch_failed',
                         f'Failed to launch the updater: {e}')
        log.warning(f"Software update launched from dashboard: ref={ref} "
                    f"request_id={request_id}")
        return jsonify({'success': True, 'request_id': request_id, 'ref': ref,
                        'status': 'starting'}), 202

    @bp.route('/api/settings/update/status')
    def update_status():
        """Mirror of the status file plus version/git. The first read of a
        fresh `complete` schedules the dashboard's own restart (the script
        only restarts owl.service) so the caller sees it coming."""
        current = (software_update.read_status(update_status_file)
                   or software_update.idle_status())
        payload = {'success': True, 'update': current,
                   'dashboard_restart_pending': False}
        payload.update(_version_payload())
        fresh_complete = (current['status'] == 'complete'
                          and current['updated_at'] > started_at)
        if fresh_complete and not restart_state['scheduled']:
            systemctl = shutil.which('systemctl') or '/usr/bin/systemctl'
            argv = ['/usr/bin/sudo', '-n', systemctl, 'restart', dashboard_unit]
            if software_update.sudo_permits(argv, runner):
                restart_state['scheduled'] = True

                def _restart_dashboard():
                    try:
                        runner(argv, capture_output=True, text=True, timeout=30)
                    except Exception as e:
                        log.error(f"Dashboard restart after update failed: {e}")

                timer = timer_factory(DASHBOARD_RESTART_DELAY_S, _restart_dashboard)
                timer.daemon = True
                timer.start()
                log.warning("Update complete: restarting the dashboard")
                payload['dashboard_restart_pending'] = True
            else:
                payload['warning'] = 'dashboard_restart_unavailable'
        elif fresh_complete:
            payload['dashboard_restart_pending'] = True
        return jsonify(payload)

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
        to hear the command.

        _send_command reports failure by RETURNING {'success': False} —
        it never raises — so the result dict must be inspected. Even on a
        successful send the direct path is armed as a delayed backstop:
        if the box is already going down it's a no-op, and it covers the
        case where owl.py received the command but its sudo failed
        silently."""
        binary = shutil.which(action) or f'/usr/sbin/{action}'
        argv = ['/usr/bin/sudo', '-n', binary]
        if action == 'shutdown':
            argv.append('now')

        def _arm_direct(delay_s):
            def _delayed():
                try:
                    result = runner(argv, capture_output=True, text=True,
                                    timeout=15)
                    if result.returncode != 0:
                        log.error(
                            f"Direct {action} failed "
                            f"(rc={result.returncode}): "
                            f"{(result.stderr or '').strip()}")
                except Exception as e:
                    log.error(f"Direct {action} failed: {e}")
            timer = timer_factory(delay_s, _delayed)
            timer.daemon = True
            timer.start()

        mqtt = mqtt_getter() if mqtt_getter else None
        owl_active = bool(owl_active_getter()) if owl_active_getter else False
        if mqtt and owl_active:
            try:
                result = mqtt._send_command(action)
            except Exception as e:
                result = {'success': False, 'error': str(e)}
            if isinstance(result, dict) and result.get('success'):
                _arm_direct(POWER_BACKSTOP_DELAY_S)
                return jsonify({'success': True, 'via': 'mqtt',
                                'backstop_delay_s': POWER_BACKSTOP_DELAY_S})
            error = (result.get('error', 'unknown error')
                     if isinstance(result, dict) else result)
            log.error(f"MQTT {action} failed, using direct path: {error}")

        _arm_direct(POWER_DELAY_S)
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

    # ------------------------------------------------------------------
    # Token recovery
    # ------------------------------------------------------------------
    # An app that lost its stored token (OWL deleted/re-added in the app,
    # phone replaced, token rotated by re-provisioning) can re-earn it by
    # proving it knows the hotspot password — the same trust level the
    # token was originally granted under. Deliberately NOT token-guarded.
    recovery_lock = {'failures': 0, 'lock_until': 0.0}

    @bp.route('/api/settings/token', methods=['POST'])
    def recover_token():
        now = time.time()
        if now < recovery_lock['lock_until']:
            wait_s = int(recovery_lock['lock_until'] - now) + 1
            return jsonify({'success': False,
                            'error': f'Too many attempts. Try again in {wait_s} s'}), 429

        data = request.get_json(silent=True) or {}
        provided = str(data.get('hotspot_password') or '')
        if not provided:
            return jsonify({'success': False,
                            'error': 'hotspot_password is required'}), 400

        try:
            actual = manager.nm.get_hotspot_password()
        except Exception as e:
            log.error(f"Token recovery: could not read hotspot PSK: {e}")
            actual = None
        if not actual:
            return jsonify({'success': False,
                            'error': 'Could not verify the hotspot password '
                                     'on this OWL'}), 503

        if not secrets.compare_digest(provided, actual):
            recovery_lock['failures'] += 1
            if recovery_lock['failures'] >= TOKEN_RECOVERY_MAX_FAILURES:
                recovery_lock['failures'] = 0
                recovery_lock['lock_until'] = now + TOKEN_RECOVERY_LOCK_S
            log.warning("Token recovery: wrong hotspot password")
            return jsonify({'success': False,
                            'error': 'Wrong hotspot password'}), 403

        recovery_lock['failures'] = 0
        token = token_reader() if token_reader else None
        log.info("Device token re-issued via hotspot-password verification")
        return jsonify({'success': True, 'device_token': token})

    return bp
