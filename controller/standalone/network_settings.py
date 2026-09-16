"""
In-place network reconfiguration for a provisioned OWL.

Mirrors the shape of controller/setup/firstboot_state.py (delayed switch,
state fields, revert tail) with one key difference: on a failed join this
manager reverts to the PREVIOUSLY ACTIVE connection (which may be a client
Wi-Fi network), not always the hotspot. The setup service self-destructs
after provisioning, so this lives behind the always-running standalone
dashboard instead.

Temporary joins (internet via the phone's own hotspot, for updates): the
OWL joins with a throwaway profile (autoconnect off, fixed name), keeps
retrying while the user turns their hotspot on, and a watchdog ends the
session when the hotspot vanishes, the TTL expires, or the app says
"revert". The profile is always deleted afterwards so the unit never
auto-joins the phone again on its own. While joined, the OWL sends a small
UDP beacon to its gateway (the phone) on every watchdog tick so the app can
learn the OWL's IP on the phone's randomised tether subnet.

No JSON persistence: a mid-switch reboot self-resolves via NetworkManager
autoconnect priorities (client profiles 200, hotspot 0). A single gunicorn
worker (--workers 1 --threads 8) makes one in-process machine safe.
"""

import json
import logging
import socket
import threading
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

SWITCH_DELAY_S = 5          # grace period so the join HTTP response reaches the phone
ACTIVATE_TIMEOUT_S = 45     # how long nmcli waits for the client network
REVERT_ATTEMPTS = 3         # tries to restore the previous connection after a failed join
SCAN_START_DELAY_S = 0.1    # lets the scan HTTP response flush before the AP drops

# Temporary join (phone-hotspot internet)
TEMP_CON_NAME = 'owl-phone-internet'   # never the SSID: revert must not collide with a real profile
TEMP_WAIT_DEFAULT_S = 90               # the user turns the hotspot on AFTER the 202 lands
TEMP_WAIT_MIN_S = 10
TEMP_WAIT_MAX_S = 180
TEMP_ACTIVATE_TIMEOUT_S = 20           # per attempt; the SSID usually isn't visible yet
TEMP_RETRY_GAP_S = 3
TEMP_AUTH_FAILURES_TO_ABORT = 2        # one auth error can be a not-yet-visible AP
TEMP_TTL_S = 1800                      # hard cap on a session
TEMP_WATCHDOG_S = 5
TEMP_REVERT_DELAY_S = 2                # lets the revert 202 reach the phone over the tether
# The app listens here for the OWL's beacon; keep in step with owl-app OwlBridge
BEACON_PORT = 47931


def send_udp_beacon(source_ip, gateway, port, payload):
    """Fire one UDP datagram at the gateway, from source_ip when given."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1)
        if source_ip:
            sock.bind((source_ip, 0))
        sock.sendto(payload, (gateway, port))
    finally:
        sock.close()


class NetworkSettingsManager:
    """State machine for scan / join / revert on a provisioned OWL.

    States: idle | pending | switching | connected | failed.
    All nmcli work is delegated to the injected NetworkManager; a
    timer_factory injection point makes the delayed transitions testable.
    """

    def __init__(self, network_manager, timer_factory=threading.Timer,
                 beacon_sender=None):
        self.nm = network_manager
        self._timer_factory = timer_factory
        self._beacon_sender = beacon_sender or send_udp_beacon
        self._lock = threading.Lock()
        self._timer = None
        self._pending_password = None

        self.state = 'idle'
        self.ssid = None
        self.ip = None
        self.gateway = None    # the phone, while a temporary session is connected
        self.error = None      # auth | not_found | timeout | error | link_lost | ttl_expired | cancelled
        self.warning = None    # 'previous_unavailable' if the revert failed
        self.previous = None   # {'ssid', 'mode'} captured at request_join

        # Temporary-join session
        self.temporary = False
        self.wait_s = None
        self.expires_at = None      # ISO, while connected
        self.ended_reason = None    # revert | link_lost | ttl_expired | timeout | auth | ...
        self._temp_deadline = None
        self._ttl_deadline = None
        self._watchdog = None
        self._end_requested = False
        self._ending = False
        self._beacon_error_logged = False

        self.scan_cache = []
        self.scanned_at = None
        self.scanning = False

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def scan_client(self, rescan=True):
        """Direct scan — valid when the radio is NOT in AP mode (or when
        cached results are acceptable). Returns the network list."""
        self.scan_cache = self.nm.scan(rescan=rescan)
        self.scanned_at = self._now()
        return self.scan_cache

    def start_hotspot_scan(self):
        """Hotspot-mode rescan: AP down -> rescan -> AP up, in the
        background (the phone drops for ~15 s). No-op if one is running."""
        with self._lock:
            if self.scanning:
                return
            self.scanning = True
        timer = self._timer_factory(SCAN_START_DELAY_S, self._do_hotspot_scan)
        timer.daemon = True
        timer.start()

    def _do_hotspot_scan(self):
        try:
            self.nm.hotspot_down()
            time.sleep(1)  # give the radio a moment to leave AP mode
            results = self.nm.scan(rescan=True)
            with self._lock:
                self.scan_cache = results
                self.scanned_at = self._now()
        except Exception as e:
            logger.error("Hotspot-cycle scan failed: %s", e)
        finally:
            try:
                self.nm.hotspot_up()
            except Exception as e:
                logger.error("Failed to restore hotspot after scan: %s", e)
            with self._lock:
                self.scanning = False

    def scan_results(self):
        with self._lock:
            return {'networks': list(self.scan_cache),
                    'scanned_at': self.scanned_at,
                    'scanning': self.scanning}

    # ------------------------------------------------------------------
    # Join
    # ------------------------------------------------------------------
    def request_join(self, ssid, password=None, temporary=False, wait_s=None):
        """Schedule the switch and return immediately — the HTTP 202 must
        reach the phone before the current network can drop.

        temporary=True: phone-hotspot internet session (see module doc).
        Only valid while the OWL is on its own hotspot, so the revert
        target is always the AP."""
        with self._lock:
            if self.state in ('pending', 'switching'):
                raise RuntimeError('A network switch is already in progress')
            status = self.nm.status()
            previous = {'ssid': status.get('ssid'), 'mode': status.get('mode')}
            if temporary and previous['mode'] != 'hotspot':
                raise RuntimeError('Temporary internet needs the OWL on its '
                                   'own hotspot')
            self.previous = previous
            self.state = 'pending'
            self.ssid = ssid
            self.ip = None
            self.error = None
            self.warning = None
            self._pending_password = password
            self.temporary = bool(temporary)
            self.wait_s = (self._clamp_wait(wait_s) if temporary else None)
            self.expires_at = None
            self.ended_reason = None
            self._end_requested = False

        target = self._do_temporary_switch if temporary else self._do_switch
        timer = self._timer_factory(SWITCH_DELAY_S, target)
        timer.daemon = True
        timer.start()
        self._timer = timer

    @staticmethod
    def _clamp_wait(wait_s):
        try:
            value = int(wait_s) if wait_s is not None else TEMP_WAIT_DEFAULT_S
        except (TypeError, ValueError):
            value = TEMP_WAIT_DEFAULT_S
        return max(TEMP_WAIT_MIN_S, min(TEMP_WAIT_MAX_S, value))

    def _do_switch(self):
        with self._lock:
            ssid = self.ssid
            password = self._pending_password
            self._pending_password = None
            previous = dict(self.previous or {})
            self.state = 'switching'

        try:
            self.nm.add_wifi_connection(ssid, password)
            if previous.get('mode') == 'hotspot':
                self.nm.hotspot_down()
            if self.nm.activate_connection(ssid, timeout=ACTIVATE_TIMEOUT_S):
                with self._lock:
                    self.state = 'connected'
                    self.ip = self.nm.get_ip4()
                # Drop the superseded client profile so the unit doesn't
                # autoconnect back to the old network later. The hotspot
                # profile is never deleted (built-in fallback).
                prev_ssid = previous.get('ssid')
                if (previous.get('mode') == 'client' and prev_ssid
                        and prev_ssid != ssid):
                    try:
                        self.nm.delete_connection(prev_ssid)
                    except Exception as e:
                        logger.warning("Could not delete old profile '%s': %s",
                                       prev_ssid, e)
                logger.info("Joined '%s' (%s)", ssid, self.ip)
                return
            error = getattr(self.nm, 'last_error', 'error') or 'error'
        except Exception as e:
            logger.error("Network switch failed: %s", e)
            error = getattr(e, 'kind', 'error')

        self._revert(ssid, error, previous)

    def _do_temporary_switch(self):
        """Temporary join: keep trying to reach the phone's hotspot until
        wait_s runs out (the user is still fumbling with Android's hotspot
        toggle), then either arm the watchdog or revert to the AP."""
        with self._lock:
            ssid = self.ssid
            password = self._pending_password
            self._pending_password = None
            previous = dict(self.previous or {})
            wait_s = self.wait_s or TEMP_WAIT_DEFAULT_S
            self.state = 'switching'

        error = 'timeout'
        try:
            self.nm.add_wifi_connection(ssid, password, con_name=TEMP_CON_NAME,
                                        autoconnect=False)
            self.nm.hotspot_down()
            deadline = time.monotonic() + wait_s
            auth_failures = 0
            while True:
                if self.nm.activate_connection(TEMP_CON_NAME,
                                               timeout=TEMP_ACTIVATE_TIMEOUT_S):
                    try:
                        gateway = self.nm.get_gateway4()
                    except Exception as e:
                        logger.warning("Could not read gateway: %s", e)
                        gateway = None
                    with self._lock:
                        self.state = 'connected'
                        self.ip = self.nm.get_ip4()
                        self.gateway = gateway
                        self._beacon_error_logged = False
                        self._ttl_deadline = time.monotonic() + TEMP_TTL_S
                        self.expires_at = (datetime.now(timezone.utc)
                                           + timedelta(seconds=TEMP_TTL_S)).isoformat()
                    self._arm_watchdog()
                    self._send_beacon()
                    logger.info("Temporary join to '%s' (%s), TTL %d s",
                                ssid, self.ip, TEMP_TTL_S)
                    return
                error = getattr(self.nm, 'last_error', 'error') or 'error'
                auth_failures = auth_failures + 1 if error == 'auth' else 0
                if auth_failures >= TEMP_AUTH_FAILURES_TO_ABORT:
                    break
                with self._lock:
                    cancelled = self._end_requested
                if cancelled:
                    error = 'cancelled'
                    break
                if time.monotonic() >= deadline:
                    error = 'timeout'
                    break
                time.sleep(TEMP_RETRY_GAP_S)
        except Exception as e:
            logger.error("Temporary network switch failed: %s", e)
            error = getattr(e, 'kind', 'error')

        self._revert(TEMP_CON_NAME, error, previous)
        with self._lock:
            self.ended_reason = error

    def _revert(self, ssid, error, previous):
        """Failure tail: delete the new profile, restore whatever was
        active before the join so the phone can reconnect and read why."""
        if ssid:
            try:
                self.nm.delete_connection(ssid)
            except Exception as e:
                logger.warning("Could not delete failed connection: %s", e)

        restored = False
        prev_mode = previous.get('mode')
        prev_ssid = previous.get('ssid')
        for attempt in range(1, REVERT_ATTEMPTS + 1):
            try:
                if prev_mode == 'client' and prev_ssid:
                    # The old client profile still exists (only deleted on a
                    # SUCCESSFUL join), so reactivate it.
                    if self.nm.activate_connection(prev_ssid,
                                                   timeout=ACTIVATE_TIMEOUT_S):
                        restored = True
                        break
                else:
                    self.nm.hotspot_up()
                    restored = True
                    break
            except Exception as e:
                logger.error("Revert attempt %d/%d failed: %s",
                             attempt, REVERT_ATTEMPTS, e)
            if attempt < REVERT_ATTEMPTS:
                time.sleep(2 * attempt)

        with self._lock:
            self.state = 'failed'
            self.error = error
            self.warning = None if restored else 'previous_unavailable'
        if restored:
            logger.warning("Join to '%s' failed (%s); previous connection "
                           "restored", ssid, error)
        else:
            logger.critical("Join to '%s' failed (%s) AND the previous "
                            "connection did not come back", ssid, error)

    # ------------------------------------------------------------------
    # Temporary session: watchdog + revert
    # ------------------------------------------------------------------
    def _arm_watchdog(self):
        timer = self._timer_factory(TEMP_WATCHDOG_S, self._watchdog_tick)
        timer.daemon = True
        timer.start()
        self._watchdog = timer

    def _watchdog_tick(self):
        with self._lock:
            if self.state != 'connected' or not self.temporary:
                return
            ttl_expired = (self._ttl_deadline is not None
                           and time.monotonic() >= self._ttl_deadline)
        if ttl_expired:
            self._end_temporary('ttl_expired')
            return
        try:
            alive = self.nm.connection_state(TEMP_CON_NAME) == 'activated'
        except Exception as e:
            logger.warning("Watchdog could not read link state: %s", e)
            alive = True
        if not alive:
            # The phone's hotspot went away; NetworkManager has already
            # dropped the client link (and may have raised the AP itself)
            self._end_temporary('link_lost')
            return
        self._send_beacon()
        self._arm_watchdog()

    def _send_beacon(self):
        """Tell the phone where the OWL is: the app listens on BEACON_PORT
        and uses the datagram's source address. HTTPS checks in the app
        remain the trust gate, so the payload is only a marker."""
        with self._lock:
            if not self.temporary or self.state != 'connected' or not self.gateway:
                return
            ip, gateway = self.ip, self.gateway
        payload = json.dumps({'owl': 'phone-internet', 'ip': ip}).encode()
        try:
            self._beacon_sender(ip, gateway, BEACON_PORT, payload)
        except Exception as e:
            if not self._beacon_error_logged:
                self._beacon_error_logged = True
                logger.debug("Beacon to %s:%d failed: %s", gateway, BEACON_PORT, e)

    def _end_temporary(self, reason):
        """Tear the session down: delete the throwaway profile, make sure
        the AP is up, and report why. reason: revert | link_lost |
        ttl_expired."""
        with self._lock:
            if self._ending:
                return
            self._ending = True
            if self._watchdog is not None:
                self._watchdog.cancel()
                self._watchdog = None

        try:
            self.nm.delete_connection(TEMP_CON_NAME)
        except Exception as e:
            logger.warning("Could not delete temporary profile: %s", e)

        restored = False
        try:
            hotspot = self.nm.get_hotspot_connection()
            already_up = (hotspot
                          and self.nm.connection_state(hotspot) == 'activated')
        except Exception:
            already_up = False
        if already_up:
            # Never `con up` an active AP: it would restart it and kick
            # the phone off for nothing
            restored = True
        else:
            for attempt in range(1, REVERT_ATTEMPTS + 1):
                try:
                    self.nm.hotspot_up()
                    restored = True
                    break
                except Exception as e:
                    logger.error("Hotspot restore attempt %d/%d failed: %s",
                                 attempt, REVERT_ATTEMPTS, e)
                if attempt < REVERT_ATTEMPTS:
                    time.sleep(2 * attempt)

        with self._lock:
            self.ended_reason = reason
            self.ip = None
            self.gateway = None
            self.expires_at = None
            self._ttl_deadline = None
            self._ending = False
            if reason == 'revert':
                self.state = 'idle'
                self.error = None
            else:
                self.state = 'failed'
                self.error = reason
            self.warning = None if restored else 'previous_unavailable'
        logger.warning("Temporary internet session ended (%s); hotspot %s",
                       reason, 'restored' if restored else 'NOT restored')

    def revert(self):
        """End a temporary session from the app. Returns what happens next:
        'cancelled' (never started), 'cancelling' (mid-join, the retry loop
        exits and reverts), 'reverting' (connected; teardown after
        TEMP_REVERT_DELAY_S so the response reaches the phone)."""
        with self._lock:
            if not self.temporary:
                raise RuntimeError('No temporary connection to revert')
            state = self.state
            if state == 'pending':
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                self._reset_fields()
                return 'cancelled'
            if state == 'switching':
                self._end_requested = True
                return 'cancelling'
            if state != 'connected':
                raise RuntimeError('Temporary connection already ended')
        timer = self._timer_factory(TEMP_REVERT_DELAY_S,
                                    lambda: self._end_temporary('revert'))
        timer.daemon = True
        timer.start()
        return 'reverting'

    def cleanup_stale_temporary(self):
        """Delete a temporary profile left behind by a crash/reboot mid
        session. Called once at dashboard start (not in __init__: fresh
        managers must not touch nmcli)."""
        try:
            self.nm.delete_connection(TEMP_CON_NAME)
        except Exception as e:
            logger.debug("No stale temporary profile to delete: %s", e)

    # ------------------------------------------------------------------
    # Cancel + result
    # ------------------------------------------------------------------
    def _reset_fields(self):
        """Caller holds the lock."""
        self.state = 'idle'
        self.ssid = None
        self.ip = None
        self.gateway = None
        self.error = None
        self.warning = None
        self.previous = None
        self._pending_password = None
        self.temporary = False
        self.wait_s = None
        self.expires_at = None
        self.ended_reason = None
        self._temp_deadline = None
        self._ttl_deadline = None
        self._end_requested = False

    def cancel(self):
        """pending -> idle (kills the timer); failed/connected -> idle.
        Raises while the switch itself is underway (cannot be unwound)."""
        with self._lock:
            if self.state == 'switching':
                raise RuntimeError('The network switch is already underway')
            if self.state == 'pending' and self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self.state != 'idle':
                if self._watchdog is not None:
                    self._watchdog.cancel()
                    self._watchdog = None
                self._reset_fields()

    def result(self):
        with self._lock:
            return {
                'state': self.state,
                'ssid': self.ssid,
                'ip': self.ip,
                'error': self.error,
                'warning': self.warning,
                'previous': dict(self.previous) if self.previous else None,
                'temporary': self.temporary,
                'expires_at': self.expires_at,
                'ended_reason': self.ended_reason,
                'con_name': TEMP_CON_NAME if self.temporary else None,
            }
