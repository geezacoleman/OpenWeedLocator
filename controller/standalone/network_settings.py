"""
In-place network reconfiguration for a provisioned OWL.

Mirrors the shape of controller/setup/firstboot_state.py (delayed switch,
state fields, revert tail) with one key difference: on a failed join this
manager reverts to the PREVIOUSLY ACTIVE connection (which may be a client
Wi-Fi network), not always the hotspot. The setup service self-destructs
after provisioning, so this lives behind the always-running standalone
dashboard instead.

No JSON persistence: a mid-switch reboot self-resolves via NetworkManager
autoconnect priorities (client profiles 200, hotspot 0). A single gunicorn
worker (--workers 1 --threads 8) makes one in-process machine safe.
"""

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SWITCH_DELAY_S = 5          # grace period so the join HTTP response reaches the phone
ACTIVATE_TIMEOUT_S = 45     # how long nmcli waits for the client network
REVERT_ATTEMPTS = 3         # tries to restore the previous connection after a failed join
SCAN_START_DELAY_S = 0.1    # lets the scan HTTP response flush before the AP drops


class NetworkSettingsManager:
    """State machine for scan / join / revert on a provisioned OWL.

    States: idle | pending | switching | connected | failed.
    All nmcli work is delegated to the injected NetworkManager; a
    timer_factory injection point makes the delayed transitions testable.
    """

    def __init__(self, network_manager, timer_factory=threading.Timer):
        self.nm = network_manager
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._timer = None
        self._pending_password = None

        self.state = 'idle'
        self.ssid = None
        self.ip = None
        self.error = None      # auth | not_found | timeout | error
        self.warning = None    # 'previous_unavailable' if the revert failed
        self.previous = None   # {'ssid', 'mode'} captured at request_join

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
    def request_join(self, ssid, password=None):
        """Schedule the switch and return immediately — the HTTP 202 must
        reach the phone before the current network can drop."""
        with self._lock:
            if self.state in ('pending', 'switching'):
                raise RuntimeError('A network switch is already in progress')
            status = self.nm.status()
            self.previous = {'ssid': status.get('ssid'),
                             'mode': status.get('mode')}
            self.state = 'pending'
            self.ssid = ssid
            self.ip = None
            self.error = None
            self.warning = None
            self._pending_password = password

        timer = self._timer_factory(SWITCH_DELAY_S, self._do_switch)
        timer.daemon = True
        timer.start()
        self._timer = timer

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
    # Cancel + result
    # ------------------------------------------------------------------
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
                self.state = 'idle'
                self.ssid = None
                self.ip = None
                self.error = None
                self.warning = None
                self.previous = None
                self._pending_password = None

    def result(self):
        with self._lock:
            return {
                'state': self.state,
                'ssid': self.ssid,
                'ip': self.ip,
                'error': self.error,
                'warning': self.warning,
                'previous': dict(self.previous) if self.previous else None,
            }
