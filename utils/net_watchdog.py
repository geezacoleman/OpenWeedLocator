"""Wi-Fi fallback: bring the OWL hotspot back whenever wlan0 has no network.

Runs as root (owl-net-watchdog.service, installed by
controller/setup/install_firstboot.sh --net-watchdog) from a root-owned copy
in /usr/local/lib/owl/net, never from the repo: a broken update must not
break the one thing that keeps the unit reachable. v3.26.0 did exactly that
to the dashboard, the in-process fallback died with it, and the unit stayed
off the air until someone plugged in a screen.

Standard library + utils.network_manager only (system python3, no venv).

Every TICK_S:
  - paused while first-boot setup is armed, during boot, and while the
    dashboard owns the radio (SWITCH_MARKER holds a live pid);
  - no hotspot profile (networked rig unit from setup.sh): nothing to do;
  - wlan0 down for FALLBACK_AFTER_S: raise the hotspot;
  - on the hotspot, every CLIENT_RETRY_S and only while nobody has been
    joined for STATION_QUIET_S: try the best saved client network (a rig
    or farm network that came back). Failed: the hotspot comes straight
    back. The phone-internet profile is never tried.

Pause it with: sudo systemctl stop owl-net-watchdog
"""

import logging
import os
import subprocess
import time

from utils.network_manager import (DEVICE_ACTIVATED, NetworkManager,
                                   PHONE_INTERNET_CON_NAME, SWITCH_MARKER)

logger = logging.getLogger('owl-net-watchdog')

TICK_S = 10
BOOT_GRACE_S = 120          # NetworkManager gets first go at autoconnect
FALLBACK_AFTER_S = 30
CLIENT_RETRY_S = 150
CLIENT_TRY_TIMEOUT_S = 30
STATION_QUIET_S = 60
MARKER_MAX_AGE_S = 600      # a hung dashboard does not hold the radio forever
FIRSTBOOT_FLAG = '/boot/firmware/owl-firstboot.flag'
PROC_DIR = '/proc'
# unknown, unmanaged, unavailable (radio off): not ours to fix
HANDS_OFF = (0, 10, 20)


def dashboard_switching(marker_path, now_wall):
    """True while the dashboard holds the radio: marker present, recent,
    and its pid alive."""
    try:
        with open(marker_path) as f:
            pid = int(f.read().strip())
        age = now_wall - os.path.getmtime(marker_path)
    except (OSError, ValueError):
        return False
    return age < MARKER_MAX_AGE_S and os.path.exists(os.path.join(PROC_DIR, str(pid)))


def has_stations(runner, device='wlan0'):
    """Anyone joined to the hotspot? Unknown counts as yes: a failed read
    must never kick a phone off."""
    try:
        result = runner(['iw', 'dev', device, 'station', 'dump'], timeout=10)
    except Exception:
        return True
    return result.returncode != 0 or 'Station ' in (result.stdout or '')


def uptime_s():
    with open(os.path.join(PROC_DIR, 'uptime')) as f:
        return float(f.read().split()[0])


class NetWatchdog:
    def __init__(self, nm, runner, uptime=uptime_s, flag_path=FIRSTBOOT_FLAG,
                 marker_path=SWITCH_MARKER):
        self.nm = nm
        self.runner = runner
        self.uptime = uptime
        self.flag_path = flag_path
        self.marker_path = marker_path
        self.down_since = None
        self.next_client_try = None
        self.last_station = float('-inf')

    def tick(self, now):
        """One check. Returns what it saw (keeps the tests readable)."""
        if (os.path.exists(self.flag_path) or self.uptime() < BOOT_GRACE_S
                or dashboard_switching(self.marker_path, time.time())):
            self.down_since = None
            return 'paused'
        hotspot = self.nm.get_hotspot_connection()
        if not hotspot:
            return 'no_hotspot'
        code, connection = self.nm.device_state()
        if code in HANDS_OFF:
            self.down_since = None
            return 'hands_off'
        if code == DEVICE_ACTIVATED and connection != hotspot:
            self.down_since = None
            self.next_client_try = None
            return 'client'
        if code == DEVICE_ACTIVATED:
            self.down_since = None
            self._maybe_try_client(now, hotspot)
            return 'hotspot'
        # disconnected, connecting, failing: give NetworkManager a moment
        if self.down_since is None:
            self.down_since = now
        if now - self.down_since < FALLBACK_AFTER_S:
            return 'down'
        logger.warning("wlan0 has had no network for %d s: raising hotspot '%s'",
                       now - self.down_since, hotspot)
        self.down_since = None
        self.next_client_try = now + CLIENT_RETRY_S
        self.nm.hotspot_up(hotspot)
        return 'fallback'

    def _maybe_try_client(self, now, hotspot):
        if has_stations(self.runner):
            self.last_station = now
        if self.next_client_try is None:
            self.next_client_try = now + CLIENT_RETRY_S
        if now < self.next_client_try or now - self.last_station < STATION_QUIET_S:
            return
        self.next_client_try = now + CLIENT_RETRY_S
        profiles = self.nm.wifi_profiles()
        hotspot_priority = next((p['priority'] for p in profiles if p['name'] == hotspot), 0)
        saved = [p for p in profiles
                 if p['autoconnect'] and p['priority'] > hotspot_priority
                 and p['name'] not in (hotspot, PHONE_INTERNET_CON_NAME)]
        if not saved:
            return
        name = max(saved, key=lambda p: p['priority'])['name']
        logger.info("Nobody on the hotspot: trying saved network '%s'", name)
        if not self.nm.activate_connection(name, timeout=CLIENT_TRY_TIMEOUT_S):
            logger.info("'%s' not there; hotspot back up", name)
            self.nm.hotspot_up(hotspot)


def run_command(argv, timeout=30):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def main(sleep=time.sleep, watchdog=None):
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    watchdog = watchdog or NetWatchdog(NetworkManager(runner=run_command), run_command)
    last_error = None
    while True:
        try:
            watchdog.tick(time.monotonic())
            last_error = None
        except Exception as e:
            # Once per distinct error: NetworkManager down would fill the journal
            if str(e) != last_error:
                logger.error("Watchdog check failed: %s", e)
                last_error = str(e)
        sleep(TICK_S)


if __name__ == '__main__':
    main()
