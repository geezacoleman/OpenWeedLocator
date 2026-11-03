"""
Network Manager for OWL first-boot setup.

Thin Python wrapper over NetworkManager's nmcli CLI. Used by the
first-boot setup app (controller/setup/) to scan for WiFi networks,
join a customer's network, and raise/lower the OWL-XXXX hotspot that
controller/shared/setup.sh creates at image time.

All state lives in NetworkManager itself (connection profiles); this
class only issues nmcli commands and parses their terse (-t) output.

The hotspot profile is never deleted: client connections are added with
autoconnect-priority 200 and the hotspot has 10, so NetworkManager raises
the hotspot when no client network is around. Except after `con down`:
that blocks the profile's autoconnect until reboot, so every switch that
takes the hotspot down unblocks it once the client is up
(allow_hotspot_autoconnect). utils/net_watchdog.py (in owl.py) is the backstop.

Testable on any platform: pass a `runner` callable (same signature as
subprocess.run) and no real nmcli is ever invoked.
"""

import logging
import platform
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

HOSTNAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
ETC_HOSTS = '/etc/hosts'

# Client connections win over the hotspot on boot. The hotspot sits above
# the Raspberry Pi Imager 'preconfigured' profile (0, hidden, so NM always
# thinks it is in range) and below setup.sh networked profiles (100).
CLIENT_CONNECTION_PRIORITY = 200
HOTSPOT_AUTOCONNECT_PRIORITY = '10'
# Throwaway profile of the phone-hotspot internet session; never rejoined
PHONE_INTERNET_CON_NAME = 'owl-phone-internet'
# Written by the dashboard while it owns the radio (switch, scan, teardown);
# the watchdog in owl.py keeps its hands off meanwhile. Both run as the OWL
# user; /dev/shm is writable by it and empty after every boot.
SWITCH_MARKER = '/dev/shm/owl-net-switching'
# nmcli GENERAL.STATE code of a device with an active connection
DEVICE_ACTIVATED = 100
DEFAULT_ACTIVATE_TIMEOUT = 45
MIN_PSK_LENGTH = 8

# Hotspot radio settings (see ensure_hotspot_radio_settings). Channel 6 is
# legal in every regulatory domain, the world default (00) included.
HOTSPOT_CHANNEL = '6'
HOTSPOT_PMF_DISABLE = '1'
HOTSPOT_POWERSAVE_DISABLE = '2'


class NetworkManagerError(Exception):
    """Raised when an nmcli command fails or nmcli is unavailable."""

    def __init__(self, message, kind='error'):
        super().__init__(message)
        # 'auth' | 'not_found' | 'timeout' | 'error' — lets the setup API
        # tell the phone app why a join failed
        self.kind = kind


def classify_nmcli_error(stderr):
    """Map nmcli stderr text to a coarse failure kind."""
    text = (stderr or '').lower()
    if 'secrets were required' in text or 'no secrets' in text:
        return 'auth'
    if 'no network with ssid' in text or 'not found' in text:
        return 'not_found'
    if 'timeout' in text or 'timed out' in text:
        return 'timeout'
    return 'error'


def _split_terse(line):
    """Split one line of `nmcli -t` output on unescaped colons.

    nmcli escapes ':' inside values as '\\:' and '\\' as '\\\\'.
    """
    fields = []
    current = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == ':':
            fields.append(''.join(current))
            current = []
        else:
            current.append(char)
    fields.append(''.join(current))
    return fields


class NetworkManager:
    """nmcli wrapper for WiFi scan/join and hotspot control."""

    def __init__(self, runner=None, interface='wlan0'):
        self.interface = interface
        self._runner = runner
        self._hotspot_name = None  # cached after first lookup
        self.last_error = None  # kind of the most recent activation failure

    @staticmethod
    def is_supported():
        return platform.system() == 'Linux' and shutil.which('nmcli') is not None

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------
    def _run(self, args, timeout=30, check=True):
        if self._runner is None:
            if not self.is_supported():
                raise NetworkManagerError('nmcli is not available on this platform')
            self._runner = self._default_runner

        try:
            result = self._runner(['nmcli'] + args, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise NetworkManagerError(f"nmcli timed out: {self._shown(args)}", kind='timeout')

        if check and result.returncode != 0:
            stderr = (result.stderr or '').strip()
            logger.error("nmcli failed (%s): %s", self._shown(args), stderr)
            raise NetworkManagerError(stderr or f"nmcli failed: {self._shown(args)}",
                                      kind=classify_nmcli_error(stderr))
        return result

    @staticmethod
    def _shown(args):
        """nmcli args for a log line or an error: the value after a psk or
        password argument is hidden (it used to land in the dashboard log)."""
        shown = []
        hide_next = False
        for arg in args:
            shown.append('[hidden]' if hide_next else arg)
            hide_next = arg in ('password', 'wifi-sec.psk', '802-11-wireless-security.psk')
        return ' '.join(shown)

    @staticmethod
    def _default_runner(argv, timeout=30):
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def scan(self, rescan=False):
        """Return visible networks as [{'ssid', 'signal', 'security'}].

        NOTE: rescan=True fails while the hotspot is active (the radio
        cannot scan in AP mode) — callers must bring the hotspot down
        first. rescan=False returns NetworkManager's cached results.
        """
        result = self._run(['-t', '-f', 'SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list',
                            '--rescan', 'yes' if rescan else 'no'],
                           timeout=60 if rescan else 30)

        networks = {}
        for line in (result.stdout or '').splitlines():
            if not line.strip():
                continue
            fields = _split_terse(line)
            if len(fields) < 3:
                continue
            ssid, signal, security = fields[0], fields[1], fields[2]
            if not ssid:
                continue  # hidden networks
            try:
                signal = int(signal)
            except ValueError:
                signal = 0
            existing = networks.get(ssid)
            if existing is None or signal > existing['signal']:
                networks[ssid] = {'ssid': ssid, 'signal': signal,
                                  'security': security or 'open'}

        return sorted(networks.values(), key=lambda n: n['signal'], reverse=True)

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------
    def get_active_connections(self):
        result = self._run(['-t', '-f', 'NAME,TYPE,DEVICE', 'con', 'show', '--active'])
        connections = []
        for line in (result.stdout or '').splitlines():
            if not line.strip():
                continue
            fields = _split_terse(line)
            if len(fields) >= 3:
                connections.append({'name': fields[0], 'type': fields[1],
                                    'device': fields[2]})
        return connections

    def wifi_profiles(self):
        """Saved wifi profiles: [{'name', 'autoconnect', 'priority'}]."""
        result = self._run(['-t', '-f', 'NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY',
                            'con', 'show'])
        profiles = []
        for line in (result.stdout or '').splitlines():
            fields = _split_terse(line)
            if len(fields) < 4 or 'wireless' not in fields[1]:
                continue
            try:
                priority = int(fields[3])
            except ValueError:
                priority = 0
            profiles.append({'name': fields[0], 'autoconnect': fields[2] == 'yes',
                             'priority': priority})
        return profiles

    def get_hotspot_connection(self):
        """Name of the AP-mode wifi connection profile, or None."""
        if self._hotspot_name:
            return self._hotspot_name

        result = self._run(['-t', '-f', 'NAME,TYPE', 'con', 'show'])
        for line in (result.stdout or '').splitlines():
            fields = _split_terse(line)
            if len(fields) < 2 or 'wireless' not in fields[1]:
                continue
            name = fields[0]
            mode = self._run(['-t', '-f', '802-11-wireless.mode', 'con', 'show', name],
                             check=False)
            if mode.returncode == 0 and (mode.stdout or '').strip().endswith(':ap'):
                self._hotspot_name = name
                return name
        return None

    def connection_state(self, name):
        """'activated' | 'activating' | 'inactive' | 'missing'."""
        result = self._run(['-t', '-f', 'GENERAL.STATE', 'con', 'show', name],
                           check=False)
        if result.returncode != 0:
            return 'missing'
        for line in (result.stdout or '').splitlines():
            if line.startswith('GENERAL.STATE:'):
                return line.split(':', 1)[1].strip() or 'inactive'
        return 'inactive'  # profile exists but is not active

    def device_state(self, device=None):
        """(GENERAL.STATE code, active connection or None) for the wifi
        device. The number, not the text: the text is translated.
        (0, None) when nmcli cannot say."""
        result = self._run(['-t', '-f', 'GENERAL.STATE,GENERAL.CONNECTION', 'dev',
                            'show', device or self.interface], check=False)
        if result.returncode != 0:
            return 0, None
        code, connection = 0, None
        for line in (result.stdout or '').splitlines():
            key, _, value = line.partition(':')
            if key == 'GENERAL.STATE':
                try:
                    code = int(value.split()[0])
                except (IndexError, ValueError):
                    code = 0
            elif key == 'GENERAL.CONNECTION':
                connection = value.strip() if value.strip() not in ('', '--') else None
        return code, connection

    def hotspot_up(self, name=None):
        name = name or self.get_hotspot_connection()
        if not name:
            raise NetworkManagerError('No hotspot connection profile found',
                                      kind='not_found')
        self._run(['con', 'up', name], timeout=60)
        logger.info("Hotspot '%s' up", name)

    def hotspot_down(self, name=None):
        name = name or self.get_hotspot_connection()
        if not name:
            raise NetworkManagerError('No hotspot connection profile found',
                                      kind='not_found')
        # check=False: bringing down an inactive connection is not an error
        self._run(['con', 'down', name], timeout=60, check=False)
        logger.info("Hotspot '%s' down", name)

    def allow_hotspot_autoconnect(self, name=None):
        """Undo the autoconnect block `con down` leaves on the hotspot, so
        NetworkManager raises it by itself when the client network goes.
        Modify only: nothing changes while a client holds the radio."""
        name = name or self.get_hotspot_connection()
        if not name:
            return
        self._run(['con', 'modify', name, 'connection.autoconnect', 'yes'])

    def add_wifi_connection(self, ssid, password=None, priority=CLIENT_CONNECTION_PRIORITY,
                            con_name=None, static_ip=None, gateway=None,
                            prefix=24, dns=None, autoconnect=True):
        """Create (or replace) a client WiFi profile. Does not activate it.

        With static_ip/gateway the profile uses a manual IPv4 config
        (networked-rig OWLs get controller-assigned static addresses);
        otherwise DHCP. autoconnect=False is for throwaway profiles (the
        phone-hotspot internet session) that must never win on boot.
        """
        con_name = con_name or ssid
        # Replace any stale profile with the same name
        self._run(['con', 'delete', con_name], check=False)

        self._run(['con', 'add', 'type', 'wifi', 'ifname', self.interface,
                   'con-name', con_name, 'ssid', ssid])
        if password:
            self._run(['con', 'modify', con_name,
                       'wifi-sec.key-mgmt', 'wpa-psk', 'wifi-sec.psk', password])
        if static_ip:
            if not gateway:
                raise NetworkManagerError('gateway is required for a static IP')
            # Same recipe controller/shared/setup.sh uses for networked OWLs
            self._run(['con', 'modify', con_name,
                       'ipv4.addresses', f'{static_ip}/{prefix}',
                       'ipv4.gateway', gateway,
                       'ipv4.dns', dns or gateway,
                       'ipv4.method', 'manual'])
        self._run(['con', 'modify', con_name,
                   'connection.autoconnect', 'yes' if autoconnect else 'no',
                   'connection.autoconnect-priority', str(priority)])
        logger.info("Added wifi connection '%s' (priority %s%s%s)", con_name, priority,
                    f', static {static_ip}' if static_ip else '',
                    '' if autoconnect else ', no autoconnect')

    def activate_connection(self, name, timeout=DEFAULT_ACTIVATE_TIMEOUT):
        """Activate a profile, blocking until connected. Returns True/False.

        On failure the caller decides whether to delete the profile and
        restore the hotspot (see firstboot_state.py).
        """
        self.last_error = None  # a stale kind from a previous attempt must not leak
        result = self._run(['--wait', str(timeout), 'con', 'up', name],
                           timeout=timeout + 15, check=False)
        if result.returncode == 0:
            logger.info("Connection '%s' activated", name)
            return True
        self.last_error = classify_nmcli_error(result.stderr)
        logger.warning("Connection '%s' failed to activate (%s): %s",
                       name, self.last_error, (result.stderr or '').strip())
        return False

    def delete_connection(self, name):
        self._run(['con', 'delete', name], check=False)

    def set_hotspot_password(self, password, name=None, reapply=True):
        """Change the hotspot PSK and re-apply it (used at setup finish).

        reapply=False only modifies the profile — required when the radio
        is on a client network (con up would steal it for the AP); the new
        PSK takes effect the next time the hotspot comes up.
        """
        if not password or len(password) < MIN_PSK_LENGTH:
            raise NetworkManagerError(
                f'Hotspot password must be at least {MIN_PSK_LENGTH} characters')
        name = name or self.get_hotspot_connection()
        if not name:
            raise NetworkManagerError('No hotspot connection profile found',
                                      kind='not_found')
        self._run(['con', 'modify', name, 'wifi-sec.psk', password])
        if reapply:
            self._run(['con', 'up', name], timeout=60)
        logger.info("Hotspot '%s' password updated", name)

    def get_hotspot_password(self, name=None):
        """Read the hotspot PSK from the connection profile (secrets need
        --show-secrets). Used by the token-recovery route to verify the
        caller knows the hotspot password. Returns None when unavailable."""
        name = name or self.get_hotspot_connection()
        if not name:
            return None
        result = self._run(['--show-secrets', '-t', '-f',
                            '802-11-wireless-security.psk', 'con', 'show', name],
                           check=False)
        if result.returncode != 0:
            logger.warning("Could not read hotspot PSK: %s",
                           (result.stderr or '').strip())
            return None
        for line in (result.stdout or '').splitlines():
            if line.startswith('802-11-wireless-security.psk:'):
                psk = line.split(':', 1)[1].strip()
                return psk or None
        return None

    def ensure_hotspot_radio_settings(self, name=None):
        """Pin the hotspot settings the Pi's Wi-Fi chip needs. Returns True
        when the profile was changed.

        Since wpasupplicant 2.10-24+rpt1 (Raspberry Pi, 2026) NetworkManager
        runs a wpa-psk hotspot as WPA2/WPA3 transition. The CYW43455 fails
        WPA3 (SAE) auth in AP mode, so phones, which try WPA3 first, cannot
        join, and repeated failures can take the AP down. pmf=disable drops
        SAE. A fixed channel 6 stops the auto pick landing on 12/13, which
        many phones never scan. Power save off keeps the AP responsive.
        Autoconnect priority 10 puts it above the Imager profile at boot.

        Only properties still at their default are changed, so a channel or
        pmf set by hand survives. Modify only: it takes effect the next time
        the hotspot comes up, so a connected phone is not dropped.
        """
        name = name or self.get_hotspot_connection()
        if not name:
            return False
        result = self._run(['-t', '-f',
                            '802-11-wireless.band,802-11-wireless.channel,'
                            '802-11-wireless.powersave,802-11-wireless-security.pmf,'
                            'connection.autoconnect-priority',
                            'con', 'show', name])
        current = {}
        for line in (result.stdout or '').splitlines():
            key, _, value = line.partition(':')
            current[key] = value.strip()

        def at_default(prop):
            # nmcli prints a default enum as '0', 'default' or '0 (default)'
            return (current.get(prop, '').split() or [''])[0] in ('', '0', 'default')

        changes = []
        if at_default('802-11-wireless-security.pmf'):
            changes += ['802-11-wireless-security.pmf', HOTSPOT_PMF_DISABLE]
        if at_default('802-11-wireless.powersave'):
            changes += ['802-11-wireless.powersave', HOTSPOT_POWERSAVE_DISABLE]
        # A channel needs a band; never put channel 6 on a 5 GHz profile
        if current.get('802-11-wireless.band') == 'bg' and at_default('802-11-wireless.channel'):
            changes += ['802-11-wireless.channel', HOTSPOT_CHANNEL]
        if at_default('connection.autoconnect-priority'):
            changes += ['connection.autoconnect-priority', HOTSPOT_AUTOCONNECT_PRIORITY]
        if not changes:
            return False
        self._run(['con', 'modify', name] + changes)
        logger.info("Hotspot '%s' radio settings updated: %s", name, ' '.join(changes))
        return True

    # ------------------------------------------------------------------
    # Hostname (used by first-boot controller-join only)
    # ------------------------------------------------------------------
    @staticmethod
    def set_hostname(name, system_runner, hosts_path=ETC_HOSTS):
        """Rename the device (owl-N identity for networked rigs).

        system_runner: callable(argv, ...) -> CompletedProcess (injected —
        this is a system op, not an nmcli op, so it doesn't use self._run).
        Mirrors controller/shared/setup.sh: hostnamectl, /etc/hosts,
        avahi restart so the new <name>.local advertises immediately.
        """
        if not HOSTNAME_RE.match(name or ''):
            raise NetworkManagerError(f"Invalid hostname '{name}'")

        result = system_runner(['hostnamectl', 'set-hostname', name])
        if getattr(result, 'returncode', 0) != 0:
            raise NetworkManagerError(
                (result.stderr or '').strip() or 'hostnamectl failed')

        try:
            hosts = Path(hosts_path)
            lines = [line for line in hosts.read_text().splitlines()
                     if not (line.startswith(('127.0.0.1', '127.0.1.1'))
                             and len(line.split()) > 1)]
            lines += ['127.0.0.1\tlocalhost', f'127.0.1.1\t{name}']
            hosts.write_text('\n'.join(lines) + '\n')
        except OSError as e:
            logger.warning("Could not rewrite %s: %s", hosts_path, e)

        try:
            system_runner(['systemctl', 'restart', 'avahi-daemon'])
        except Exception as e:
            logger.warning("Could not restart avahi: %s", e)
        logger.info("Hostname set to '%s'", name)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def get_ip4(self, device=None):
        device = device or self.interface
        result = self._run(['-t', '-f', 'IP4.ADDRESS', 'dev', 'show', device],
                           check=False)
        if result.returncode != 0:
            return None
        for line in (result.stdout or '').splitlines():
            if line.startswith('IP4.ADDRESS'):
                address = line.split(':', 1)[1].strip()
                return address.split('/')[0] if address else None
        return None

    def get_gateway4(self, device=None):
        """Default IPv4 gateway on the device (on a phone hotspot join this
        is the phone itself), or None."""
        device = device or self.interface
        result = self._run(['-t', '-f', 'IP4.GATEWAY', 'dev', 'show', device],
                           check=False)
        if result.returncode != 0:
            return None
        for line in (result.stdout or '').splitlines():
            if line.startswith('IP4.GATEWAY'):
                gateway = line.split(':', 1)[1].strip()
                return gateway if gateway and gateway != '--' else None
        return None

    def status(self):
        """Aggregate network status for /setup/api/status."""
        hotspot = self.get_hotspot_connection()
        active = self.get_active_connections()
        active_names = {c['name'] for c in active}

        if hotspot and hotspot in active_names:
            mode = 'hotspot'
            ssid = hotspot
        else:
            wifi = next((c for c in active if 'wireless' in c['type']), None)
            mode = 'client' if wifi else 'none'
            ssid = wifi['name'] if wifi else None

        return {'mode': mode, 'ssid': ssid, 'ip': self.get_ip4(),
                'hotspot_profile': hotspot}
