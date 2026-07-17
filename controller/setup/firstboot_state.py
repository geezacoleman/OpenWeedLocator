"""
First-boot state machine for the OWL setup service.

Owns the lifecycle of a factory-fresh (or factory-reset) OWL:

    [flag armed] boot
        AWAIT_PHONE   hotspot up, WiFi scan cached (scan requires AP down,
                      so it happens once at startup before any phone connects)
        IN_SETUP      first API request received
        wifi/join ->  PENDING --(switch_delay)--> SWITCHING
                          activated -> CONNECTED
                          failure   -> FAILED (client profile deleted,
                                       hotspot restored, phone retries)
        finish     ->  DONE (flag cleared, service exits; systemd
                       ConditionPathExists prevents restart)

State that must survive a mid-setup reboot (join result, chosen ssid)
is persisted to a small JSON file.
"""

import configparser
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_FLAG_PATH = '/boot/firmware/owl-firstboot.flag'
DEFAULT_STATE_FILE = '/var/lib/owl/firstboot_state.json'
AVAHI_SERVICE_FILE = '/etc/avahi/services/owl-setup.service'
SETUP_PORT = int(os.environ.get('OWL_SETUP_PORT', 8088))

SWITCH_DELAY_S = 5          # grace period so the join HTTP response reaches the phone
ACTIVATE_TIMEOUT_S = 45     # how long nmcli waits for the client network
EXIT_DELAY_S = 1.0          # lets the finish response flush before the process exits
HOTSPOT_RETRY_S = 30        # retry cadence when the setup AP cannot come up
REVERT_HOTSPOT_ATTEMPTS = 3  # hotspot_up tries before a failed join gives up

# Fixed password every unconfigured OWL uses; baked into the phone app.
# Replaced by a user-chosen password at finish(standalone).
SETUP_HOTSPOT_PSK = 'owl-setup'

AVAHI_SERVICE_XML = """<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">OWL Setup on %h</name>
  <service>
    <type>_owl-setup._tcp</type>
    <port>{port}</port>
  </service>
</service-group>
"""


class FirstBootState:
    """Setup lifecycle: hotspot management, join handoff, teardown."""

    def __init__(self, network_manager, flag_path=None, state_file=None,
                 timer_factory=threading.Timer, system_runner=None,
                 exit_fn=None, controller_ini=None, hostname_getter=None):
        self.nm = network_manager
        self.flag_path = Path(flag_path or os.environ.get('OWL_FIRSTBOOT_FLAG',
                                                          DEFAULT_FLAG_PATH))
        self.state_file = Path(state_file or os.environ.get('OWL_FIRSTBOOT_STATE',
                                                            DEFAULT_STATE_FILE))
        self.controller_ini = Path(controller_ini or
                                   Path(__file__).resolve().parents[2]
                                   / 'config' / 'CONTROLLER.ini')
        self._timer_factory = timer_factory
        self._system_runner = system_runner or self._default_system_runner
        self._exit_fn = exit_fn or (lambda: os._exit(0))
        self._hostname_getter = hostname_getter or socket.gethostname
        self._lock = threading.Lock()

        self.state = 'await_phone'
        self.scan_cache = []
        self.scanned_at = None

        # Join result — persisted so it survives a mid-setup reboot
        self.wifi_state = 'idle'   # idle | pending | switching | connected | failed
        self.wifi_mode = None      # 'wifi' | 'controller'
        self.wifi_ssid = None
        self.wifi_ip = None
        self.wifi_error = None
        self.wifi_warning = None   # non-fatal problem on an otherwise good join
        self.join_device_id = None    # controller mode: assigned owl-N
        self.join_static_ip = None    # controller mode: assigned address
        self.prior_hostname = None    # controller mode: hostname before the join
        self._pending_password = None
        self._pending_join = None     # controller mode: full join params
        self._load_persisted()

    @property
    def _controller_ini_backup(self):
        return Path(str(self.controller_ini) + '.pre-join.bak')

    @staticmethod
    def _default_system_runner(argv, timeout=15):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_persisted(self):
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return
        self.wifi_state = data.get('wifi_state', 'idle')
        self.wifi_mode = data.get('wifi_mode')
        self.wifi_ssid = data.get('wifi_ssid')
        self.wifi_ip = data.get('wifi_ip')
        self.wifi_error = data.get('wifi_error')
        self.wifi_warning = data.get('wifi_warning')
        self.join_device_id = data.get('join_device_id')
        self.join_static_ip = data.get('join_static_ip')
        self.prior_hostname = data.get('prior_hostname')
        # A reboot mid-switch means the outcome is unknown; startup() re-checks
        if self.wifi_state in ('pending', 'switching'):
            self.wifi_state = 'idle'

    def _persist(self):
        data = {'wifi_state': self.wifi_state, 'wifi_mode': self.wifi_mode,
                'wifi_ssid': self.wifi_ssid,
                'wifi_ip': self.wifi_ip, 'wifi_error': self.wifi_error,
                'wifi_warning': self.wifi_warning,
                'join_device_id': self.join_device_id,
                'join_static_ip': self.join_static_ip,
                'prior_hostname': self.prior_hostname,
                'updated_at': datetime.now(timezone.utc).isoformat()}
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(data, indent=2))
        except OSError as e:
            logger.warning("Could not persist first-boot state: %s", e)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def startup(self):
        """Bring the radio to a known state before serving requests.

        Scan requires the AP to be down (the radio cannot scan in AP
        mode), so: AP down -> scan -> cache -> AP up. Runs once, before
        any phone can be connected, so nobody gets dropped.
        """
        if not self.nm.is_supported():
            logger.warning("nmcli unavailable — running in dev mode (no radio control)")
            self.state = 'in_setup'
            return

        self._ensure_setup_surface()

        # Mid-setup reboot after a successful join: the client profile
        # (priority 200) outranks the hotspot and reconnects on its own.
        try:
            status = self.nm.status()
        except Exception as e:
            logger.error("Could not read network status at startup: %s", e)
            status = {'mode': 'unknown', 'ssid': None, 'ip': None}
        if status['mode'] == 'client':
            logger.info("Booted onto client network '%s' — resuming setup",
                        status['ssid'])
            self.wifi_state = 'connected'
            self.wifi_ssid = status['ssid']
            self.wifi_ip = status['ip']
            self.state = 'in_setup'
            self._persist()
            return

        try:
            self.refresh_scan_cache()
        except Exception as e:
            logger.error("Startup WiFi scan failed (%s) — continuing with an "
                         "empty scan cache", e)

        if not self._raise_setup_hotspot():
            self._schedule_hotspot_retry()
        self.state = 'await_phone'
        logger.info("First-boot hotspot up; %d networks cached", len(self.scan_cache))

    def _raise_setup_hotspot(self):
        """Apply the fixed setup PSK, which re-applies the profile and
        brings the AP up. Returns success instead of raising: a crash here
        would restart-loop the service with no AP for the phone to join,
        and retrying is the only self-service recovery an unattended unit
        has."""
        try:
            self.nm.set_hotspot_password(SETUP_HOTSPOT_PSK)
            return True
        except Exception as e:
            logger.critical("Could not raise the setup hotspot: %s", e)
            return False

    def _schedule_hotspot_retry(self):
        logger.warning("Retrying setup hotspot in %ds", HOTSPOT_RETRY_S)
        timer = self._timer_factory(HOTSPOT_RETRY_S, self._hotspot_retry)
        timer.daemon = True
        timer.start()

    def _hotspot_retry(self):
        # A join in progress (or completed) owns the radio — stop retrying
        if self.wifi_state in ('pending', 'switching', 'connected'):
            return
        if not self._raise_setup_hotspot():
            self._schedule_hotspot_retry()

    def _ensure_setup_surface(self):
        """Recreate what teardown removed, so touching the flag file from
        a PC is a complete re-arm (avahi advert + firewall rule)."""
        avahi_file = Path(AVAHI_SERVICE_FILE)
        try:
            if not avahi_file.exists():
                avahi_file.write_text(AVAHI_SERVICE_XML.format(port=SETUP_PORT))
                self._system_runner(['systemctl', 'reload', 'avahi-daemon'])
        except Exception as e:
            logger.warning("Could not write avahi service file: %s", e)
        try:
            self._system_runner(['ufw', 'allow', f'{SETUP_PORT}/tcp'])
        except Exception as e:
            logger.warning("Could not add ufw rule: %s", e)

    def refresh_scan_cache(self):
        """AP down -> rescan -> AP up. Drops any connected phone."""
        try:
            self.nm.hotspot_down()
            time.sleep(1)  # give the radio a moment to leave AP mode
            self.scan_cache = self.nm.scan(rescan=True)
            self.scanned_at = datetime.now(timezone.utc).isoformat()
        finally:
            try:
                self.nm.hotspot_up()
            except Exception as e:
                logger.error("Failed to restore hotspot after scan: %s", e)
        return self.scan_cache

    def mark_in_setup(self):
        if self.state == 'await_phone':
            self.state = 'in_setup'

    # ------------------------------------------------------------------
    # WiFi join handoff
    # ------------------------------------------------------------------
    def request_join(self, ssid, password):
        """Schedule the hotspot->client switch and return immediately.

        The HTTP 202 response must reach the phone before the hotspot
        drops, hence the delayed switch.
        """
        with self._lock:
            if self.wifi_state in ('pending', 'switching'):
                raise RuntimeError('A network switch is already in progress')
            self.wifi_state = 'pending'
            self.wifi_mode = 'wifi'
            self.wifi_ssid = ssid
            self.wifi_ip = None
            self.wifi_error = None
            self._pending_password = password
            self._persist()

        timer = self._timer_factory(SWITCH_DELAY_S, self._do_switch)
        timer.daemon = True
        timer.start()

    def request_join_controller(self, params):
        """Schedule the hotspot->rig-network switch for a controller join.

        params: {ssid, password, device_id, static_ip, gateway,
                 subnet_prefix, dns, broker_ip, broker_port} — produced by
        the controller's /api/fleet/reserve and validated by the route.
        """
        with self._lock:
            if self.wifi_state in ('pending', 'switching'):
                raise RuntimeError('A network switch is already in progress')
            self.wifi_state = 'pending'
            self.wifi_mode = 'controller'
            self.wifi_ssid = params['ssid']
            self.wifi_ip = None
            self.wifi_error = None
            self.join_device_id = params['device_id']
            self.join_static_ip = params['static_ip']
            self._pending_join = dict(params)
            self._persist()

        timer = self._timer_factory(SWITCH_DELAY_S, self._do_switch_controller)
        timer.daemon = True
        timer.start()

    def _do_switch(self):
        with self._lock:
            ssid = self.wifi_ssid
            password = self._pending_password
            self._pending_password = None
            self.wifi_state = 'switching'
            self._persist()

        try:
            self.nm.add_wifi_connection(ssid, password)
            self.nm.hotspot_down()
            if self.nm.activate_connection(ssid, timeout=ACTIVATE_TIMEOUT_S):
                with self._lock:
                    self.wifi_state = 'connected'
                    self.wifi_ip = self.nm.get_ip4()
                    self._persist()
                logger.info("Joined '%s' (%s)", ssid, self.wifi_ip)
                return
            error = getattr(self.nm, 'last_error', 'error')
        except Exception as e:
            logger.error("Network switch failed: %s", e)
            error = getattr(e, 'kind', 'error')

        self._revert_to_hotspot(ssid, error)

    def _do_switch_controller(self):
        """Controller join: identity + config first, then the radio switch.

        Hostname and CONTROLLER.ini are deliberately NOT reverted on a
        failed switch — they describe the target state, the reservation is
        still held by the controller, and a retry re-runs only the switch.
        """
        with self._lock:
            params = self._pending_join or {}
            self._pending_join = None
            ssid = params.get('ssid')
            self.wifi_state = 'switching'
            self._persist()

        try:
            # Remember the pre-join identity so an abandoned wizard can be
            # rolled back (restore_hotspot). A retry must not overwrite it
            # with the already-applied owl-N name.
            if not self.prior_hostname:
                self.prior_hostname = self._hostname_getter()
                self._persist()
            self.nm.set_hostname(params['device_id'], self._system_runner)
            self._write_controller_ini(params)
        except Exception as e:
            logger.error("Controller-join preparation failed: %s", e)
            self._revert_to_hotspot(ssid, getattr(e, 'kind', 'error'),
                                    delete_profile=False)
            return

        try:
            self.nm.add_wifi_connection(
                ssid, params.get('password'),
                static_ip=params['static_ip'], gateway=params['gateway'],
                prefix=params.get('subnet_prefix', 24), dns=params.get('dns'))
            self.nm.hotspot_down()
            if self.nm.activate_connection(ssid, timeout=ACTIVATE_TIMEOUT_S):
                # owl.py must reread broker_ip/device_id and start
                # heartbeating to the controller (which auto-confirms the
                # reservation on the first message it sees). If the restart
                # fails, the join still worked but no heartbeat will come —
                # surface it so the app doesn't report a clean success.
                warning = None
                try:
                    result = self._system_runner(
                        ['systemctl', 'restart', 'owl.service'])
                    if getattr(result, 'returncode', 0) != 0:
                        warning = 'owl_restart_failed'
                except Exception as e:
                    logger.warning("Could not restart owl.service: %s", e)
                    warning = 'owl_restart_failed'
                if warning:
                    logger.warning("owl.service did not restart — the rig "
                                   "will not see a heartbeat until reboot")
                with self._lock:
                    self.wifi_state = 'connected'
                    self.wifi_warning = warning
                    self.wifi_ip = self.nm.get_ip4()
                    # Join committed: the pre-join identity is history, a
                    # later restore_hotspot must not half-revert to it.
                    self.prior_hostname = None
                    self._persist()
                self._clear_join_backup()
                logger.info("Joined rig '%s' as %s (%s)", ssid,
                            params['device_id'], self.wifi_ip)
                return
            error = getattr(self.nm, 'last_error', 'error')
        except Exception as e:
            logger.error("Rig network switch failed: %s", e)
            error = getattr(e, 'kind', 'error')

        self._revert_to_hotspot(ssid, error)

    def _clear_join_backup(self):
        try:
            self._controller_ini_backup.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not remove CONTROLLER.ini backup: %s", e)

    def _write_controller_ini(self, params):
        """Point this OWL at the rig: only the seven networked-mode keys.

        Everything else in CONTROLLER.ini (WebDashboard, GPS, Actuation...)
        is preserved untouched.
        """
        # One-time pre-join backup so an abandoned wizard can roll back.
        # A retry must not overwrite it with the already-rig-pointed file.
        backup = self._controller_ini_backup
        if self.controller_ini.exists() and not backup.exists():
            try:
                shutil.copy2(self.controller_ini, backup)
            except OSError as e:
                logger.warning("Could not back up CONTROLLER.ini: %s", e)

        config = configparser.ConfigParser()
        config.read(self.controller_ini)
        for section in ('MQTT', 'Network'):
            if not config.has_section(section):
                config.add_section(section)
        config.set('MQTT', 'enable', 'True')
        config.set('MQTT', 'broker_ip', params['broker_ip'])
        config.set('MQTT', 'broker_port', str(params.get('broker_port', 1883)))
        config.set('MQTT', 'device_id', params['device_id'])
        config.set('Network', 'mode', 'networked')
        config.set('Network', 'static_ip', params['static_ip'])
        config.set('Network', 'controller_ip', params['broker_ip'])

        self.controller_ini.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.controller_ini.parent),
                                        prefix='.controller-', suffix='.ini')
        with os.fdopen(fd, 'w') as handle:
            config.write(handle)
        os.replace(tmp_path, self.controller_ini)
        logger.info("CONTROLLER.ini updated for %s -> broker %s",
                    params['device_id'], params['broker_ip'])

    def _revert_to_hotspot(self, ssid, error, delete_profile=True):
        """Failure tail shared by both switch paths: the hotspot must come
        back so the phone can reconnect and read the error."""
        if delete_profile and ssid:
            try:
                self.nm.delete_connection(ssid)
            except Exception as e:
                logger.warning("Could not delete failed connection: %s", e)
        restored = False
        for attempt in range(1, REVERT_HOTSPOT_ATTEMPTS + 1):
            try:
                self.nm.hotspot_up()
                restored = True
                break
            except Exception as e:
                logger.error("Hotspot restore attempt %d/%d failed: %s",
                             attempt, REVERT_HOTSPOT_ATTEMPTS, e)
                if attempt < REVERT_HOTSPOT_ATTEMPTS:
                    time.sleep(2 * attempt)
        with self._lock:
            self.wifi_state = 'failed'
            self.wifi_error = error
            # No AP and no client — the unit is unreachable until the
            # retry timer (or a power cycle) brings the hotspot back.
            self.wifi_warning = None if restored else 'hotspot_unavailable'
            self._persist()
        if restored:
            logger.warning("Join to '%s' failed (%s); hotspot restored",
                           ssid, error)
        else:
            logger.critical("Join to '%s' failed (%s) AND the hotspot did "
                            "not come back — retrying in background",
                            ssid, error)
            self._schedule_hotspot_retry()

    def restore_hotspot(self):
        """Escape hatch: abandon any client connection, re-raise the AP.

        Abandoning a controller join also rolls back the identity that
        _do_switch_controller applied before the radio switch (hostname +
        CONTROLLER.ini), so an abandoned wizard doesn't leave the OWL
        pointed at a rig it never joined.
        """
        with self._lock:
            ssid = self.wifi_ssid
            abandoned_controller = self.wifi_mode == 'controller'
            self.wifi_state = 'idle'
            self.wifi_ssid = None
            self.wifi_ip = None
            self.wifi_error = None
            self.wifi_warning = None
            if abandoned_controller:
                self.wifi_mode = None
                self.join_device_id = None
                self.join_static_ip = None
            self._persist()
        if ssid:
            try:
                self.nm.delete_connection(ssid)
            except Exception as e:
                logger.warning("Could not delete connection '%s': %s", ssid, e)
        if abandoned_controller:
            self._restore_controller_identity()
        self.nm.hotspot_up()

    def _restore_controller_identity(self):
        """Put back the pre-join CONTROLLER.ini and hostname (best-effort)."""
        backup = self._controller_ini_backup
        if backup.exists():
            try:
                os.replace(backup, self.controller_ini)
                logger.info("CONTROLLER.ini restored from pre-join backup")
            except OSError as e:
                logger.warning("Could not restore CONTROLLER.ini: %s", e)
        if self.prior_hostname:
            try:
                self.nm.set_hostname(self.prior_hostname, self._system_runner)
                logger.info("Hostname restored to '%s'", self.prior_hostname)
            except Exception as e:
                logger.warning("Could not restore hostname '%s': %s",
                               self.prior_hostname, e)
            self.prior_hostname = None
            self._persist()

    def wifi_result(self):
        with self._lock:
            result = {'state': self.wifi_state, 'mode': self.wifi_mode,
                      'ssid': self.wifi_ssid,
                      'ip': self.wifi_ip, 'error': self.wifi_error,
                      'warning': self.wifi_warning}
            if self.wifi_mode == 'controller':
                result['device_id'] = self.join_device_id
                result['static_ip'] = self.join_static_ip
            return result

    # ------------------------------------------------------------------
    # Finish + teardown
    # ------------------------------------------------------------------
    def finish(self, mode, new_password=None):
        """Complete setup. Raises ValueError on a bad request.

        standalone: apply the user's permanent hotspot password.
        wifi:       only valid once the join actually succeeded.
        controller: like wifi, but requires a controller-mode join.
        """
        if mode == 'standalone':
            if not new_password or len(new_password) < 8:
                raise ValueError('A new hotspot password of at least 8 characters '
                                 'is required to finish standalone setup')
            # Re-keying the hotspot restarts the AP and drops the phone, so
            # the finish response must reach it first — same delayed-switch
            # trick as request_join. Teardown runs after the re-key.
            timer = self._timer_factory(
                SWITCH_DELAY_S, lambda: self._finish_standalone(new_password))
            timer.daemon = True
            timer.start()
            return
        elif mode == 'wifi':
            if self.wifi_state != 'connected':
                raise ValueError('Cannot finish: the OWL has not joined a WiFi '
                                 'network yet')
        elif mode == 'controller':
            if self.wifi_state != 'connected' or self.wifi_mode != 'controller':
                raise ValueError('Cannot finish: the OWL has not joined the '
                                 'rig network yet')
        else:
            raise ValueError(f"Unknown mode '{mode}'")

        self.state = 'done'
        self._persist()
        self._teardown()

    def _finish_standalone(self, new_password):
        """Deferred tail of finish(standalone): re-key the hotspot, then
        tear down. The phone rejoins with the new password."""
        if self.nm.is_supported():
            applied = False
            for attempt in range(1, REVERT_HOTSPOT_ATTEMPTS + 1):
                try:
                    self.nm.set_hotspot_password(new_password)
                    applied = True
                    break
                except Exception as e:
                    logger.error("Hotspot re-key attempt %d/%d failed: %s",
                                 attempt, REVERT_HOTSPOT_ATTEMPTS, e)
                    if attempt < REVERT_HOTSPOT_ATTEMPTS:
                        time.sleep(2 * attempt)
            if not applied:
                # Stay armed: the setup password still works and the phone
                # can retry finish rather than being locked out.
                logger.critical("Could not apply the permanent hotspot "
                                "password — setup stays armed")
                return
        self.state = 'done'
        self._persist()
        self._teardown()

    def _teardown(self):
        """Remove all first-boot surface, then exit cleanly.

        systemd's ConditionPathExists on the flag prevents a restart.
        The exit is delayed so the finish HTTP response flushes first.
        """
        try:
            self.flag_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.error("Could not remove first-boot flag: %s", e)

        avahi_file = Path(AVAHI_SERVICE_FILE)
        try:
            avahi_file.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not remove avahi service file: %s", e)

        try:
            self._system_runner(['ufw', 'delete', 'allow', f'{SETUP_PORT}/tcp'])
        except Exception as e:
            logger.warning("Could not remove ufw rule: %s", e)

        logger.info("First-boot setup complete — exiting")
        timer = self._timer_factory(EXIT_DELAY_S, self._exit_fn)
        timer.daemon = True
        timer.start()
