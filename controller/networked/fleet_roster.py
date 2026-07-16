"""
Fleet roster — the controller's persistent record of its OWL units.

Used by the phone app's "add an OWL to a rig" flow. Entirely additive to
the controller: if config/fleet.json does not exist (nobody has used the
app flow), nothing in networked.py changes behaviour.

Lifecycle per device_id:

    (absent) --reserve()------------------> reserved  {number, ip, expires}
    reserved --confirm() / first heartbeat-> registered
    reserved --TTL expiry (lazy prune)-----> expired (tombstone: the slot and
                                             IP stay off-limits, a late
                                             heartbeat still confirms; purged
                                             after TOMBSTONE_TTL_S)
    any      --remove()--------------------> (absent)

Numbering follows the documented convention: controller at .2, OWL N at
subnet .{10+N} (owl-1 -> 192.168.1.11). The roster allocates the lowest
free number, refusing collisions with registered units, live unregistered
devices, active reservations, the controller, and the gateway.

No Flask/MQTT imports — pure state + JSON persistence, injected clock for
tests (mirrors utils/network_manager.py testability style).
"""

import ipaddress
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RESERVATION_TTL_S = 1800        # 30 min covers the phone's hotspot excursion
TOMBSTONE_TTL_S = 86400         # expired reservations hold their slot for 24 h
IP_HOST_BASE = 10               # owl-N -> subnet host .{10+N}
MAX_NUMBER = 189                # keeps host octet under 200 in a /24
TOUCH_THROTTLE_S = 60           # heartbeat last_confirmed refresh rate

# A loaded record missing any of these is dropped (hand-edited/corrupt file)
REQUIRED_RECORD_KEYS = ('device_id', 'number', 'assigned_ip', 'status')

NAME_RE = re.compile(r'^[\w ."\'()-]{1,40}$')
HOSTNAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')

DEFAULT_ROSTER_PATH = Path(__file__).parent.parent.parent / 'config' / 'fleet.json'


def ping_probe(ip, timeout_s=1):
    """Default ip_probe: one ping, short wait. True when the address answers.

    Only ever called on the controller Pi (Linux ping flags); tests inject
    their own probe.
    """
    import subprocess
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', str(timeout_s), ip],
            capture_output=True, timeout=timeout_s + 2)
        return result.returncode == 0
    except Exception:
        return False


class FleetRosterError(Exception):
    """Raised on invalid roster operations (bad name, no free slot...)."""


class FleetPersistenceError(FleetRosterError):
    """The roster could not be written to disk — nothing was committed."""


class FleetRoster:
    """Persistent reserve/confirm registry keyed by device_id."""

    def __init__(self, controller_ip, path=None, gateway=None, clock=time.time,
                 subnet_prefix=24, ip_probe=None):
        self.controller_ip = controller_ip
        self.path = Path(path or DEFAULT_ROSTER_PATH)
        self._clock = clock
        self._lock = threading.RLock()
        self._devices = {}          # device_id -> record dict
        self._last_touch = {}       # device_id -> monotonic-ish clock value
        # Optional callable(ip) -> True when something already answers on
        # that address; keeps the allocator from assigning an IP an unmanaged
        # LAN device is sitting on. Injected (a ping) by networked.py.
        self._ip_probe = ip_probe

        self.subnet_prefix = int(subnet_prefix)
        network = ipaddress.ip_network(f'{controller_ip}/{self.subnet_prefix}',
                                       strict=False)
        self._network = network
        self.gateway = gateway or str(network.network_address + 1)
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            loaded = data.get('devices', {})
            if not isinstance(loaded, dict):
                raise ValueError('devices is not an object')
        except FileNotFoundError:
            self._devices = {}
            return
        except (OSError, ValueError) as e:
            logger.error("Fleet roster unreadable (%s) — starting empty; "
                         "the corrupt file is left in place", e)
            self._devices = {}
            return
        # Drop malformed records instead of KeyError-ing on first use
        self._devices = {}
        for device_id, record in loaded.items():
            if (isinstance(record, dict)
                    and all(key in record for key in REQUIRED_RECORD_KEYS)):
                self._devices[device_id] = record
            else:
                logger.error("Dropping malformed roster record %r", device_id)

    def _save(self):
        data = {'version': 1, 'devices': self._devices}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent),
                                            prefix='.fleet-', suffix='.json')
            with os.fdopen(fd, 'w') as handle:
                json.dump(data, handle, indent=2)
            # os.replace can transiently fail on Windows dev machines
            # (antivirus/indexer holding the target); retry briefly
            for attempt in range(3):
                try:
                    os.replace(tmp_path, self.path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
        except OSError as e:
            logger.error("Could not persist fleet roster: %s", e)
            raise FleetPersistenceError(
                f'Could not persist fleet roster: {e}') from e

    def _prune_expired(self):
        """Expire stale reservations into tombstones; purge old tombstones.

        Tombstones keep the device_id + IP out of reallocation so a phone
        that took longer than the TTL can't race a second phone into two
        OWLs with the same identity; a late heartbeat still confirms.
        """
        now = self._clock()
        changed = False
        for device_id, record in list(self._devices.items()):
            if (record['status'] == 'reserved'
                    and record.get('expires_at', 0) < now):
                logger.info("Reservation for %s expired (slot held %ds more)",
                            device_id, TOMBSTONE_TTL_S)
                record['status'] = 'expired'
                record['expired_at'] = now
                changed = True
            elif (record['status'] == 'expired'
                    and now - record.get('expired_at', 0) > TOMBSTONE_TTL_S):
                logger.info("Tombstone for %s purged", device_id)
                del self._devices[device_id]
                changed = True
        if changed:
            try:
                self._save()
            except FleetPersistenceError:
                pass  # already logged; prune retries on the next call

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------
    def _ip_for_number(self, number):
        return str(self._network.network_address + IP_HOST_BASE + number)

    def _next_free(self, live_ids=()):
        taken_ids = set(self._devices) | set(live_ids) | {'owl-controller'}
        taken_ips = {record['assigned_ip'] for record in self._devices.values()}
        taken_ips |= {self.controller_ip, self.gateway}
        for number in range(1, MAX_NUMBER + 1):
            device_id = f'owl-{number}'
            ip = self._ip_for_number(number)
            if ipaddress.ip_address(ip) not in self._network:
                break  # ran past the subnet (only possible on < /24 rigs)
            if device_id in taken_ids or ip in taken_ips:
                continue
            if self._ip_probe is not None and self._safe_probe(ip):
                logger.warning("Skipping %s for %s — something already "
                               "answers on that address", ip, device_id)
                taken_ips.add(ip)
                continue
            return number, device_id, ip
        raise FleetRosterError('No free OWL slots on this rig')

    def _safe_probe(self, ip):
        try:
            return bool(self._ip_probe(ip))
        except Exception as e:
            logger.warning("IP probe for %s failed (%s) — assuming free", ip, e)
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def reserve(self, name=None, live_ids=()):
        if name and not NAME_RE.match(name):
            raise FleetRosterError('Invalid name (letters, numbers and '
                                   'simple punctuation, max 40 characters)')
        with self._lock:
            self._prune_expired()
            number, device_id, ip = self._next_free(live_ids)
            now = self._clock()
            record = {
                'device_id': device_id, 'number': number, 'assigned_ip': ip,
                'name': name or device_id, 'status': 'reserved',
                'created_at': now, 'expires_at': now + RESERVATION_TTL_S,
                # Bearer token for later rename/remove from the phone —
                # never included in public snapshots
                'token': secrets.token_urlsafe(16),
            }
            self._devices[device_id] = record
            try:
                self._save()
            except FleetPersistenceError:
                # Nothing durable happened — don't leak the slot in memory
                del self._devices[device_id]
                raise
            logger.info("Reserved %s (%s)", device_id, ip)
            return dict(record)

    def confirm(self, device_id, observed_ip=None):
        """Promote a reservation to registered. Idempotent.

        Also revives an expired tombstone — the OWL finished its join after
        the TTL, but its slot was held, so the identity is still valid.
        """
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise FleetRosterError(f"'{device_id}' is not reserved or "
                                       'registered on this rig')
            now = self._clock()
            if record['status'] != 'registered':
                record['status'] = 'registered'
                record['registered_at'] = now
                record.pop('expires_at', None)
                record.pop('expired_at', None)
                logger.info("Registered %s", device_id)
            record['last_confirmed'] = now
            if observed_ip:
                record['observed_ip'] = observed_ip
            self._save()
            return self._public(record)

    def touch(self, device_id):
        """Heartbeat refresh for a registered device (throttled)."""
        with self._lock:
            record = self._devices.get(device_id)
            if record is None or record['status'] != 'registered':
                return
            now = self._clock()
            if now - self._last_touch.get(device_id, 0) < TOUCH_THROTTLE_S:
                return
            self._last_touch[device_id] = now
            record['last_confirmed'] = now
            try:
                self._save()
            except FleetPersistenceError:
                pass  # heartbeat bookkeeping only; already logged

    def rename(self, device_id, name):
        if not name or not NAME_RE.match(name):
            raise FleetRosterError('Invalid name (letters, numbers and '
                                   'simple punctuation, max 40 characters)')
        with self._lock:
            record = self._devices.get(device_id)
            if record is None:
                raise FleetRosterError(f"Unknown device '{device_id}'")
            record['name'] = name
            self._save()
            return self._public(record)

    def remove(self, device_id):
        """Cancel a reservation or unregister a device."""
        with self._lock:
            if device_id not in self._devices:
                raise FleetRosterError(f"Unknown device '{device_id}'")
            del self._devices[device_id]
            self._save()

    def get(self, device_id):
        with self._lock:
            record = self._devices.get(device_id)
            return dict(record) if record else None

    def is_reserved(self, device_id):
        """Awaiting confirmation — includes expired tombstones, whose slot
        is still held so a late first heartbeat must still confirm."""
        with self._lock:
            record = self._devices.get(device_id)
            return bool(record and record['status'] in ('reserved', 'expired'))

    def is_registered(self, device_id):
        with self._lock:
            record = self._devices.get(device_id)
            return bool(record and record['status'] == 'registered')

    def snapshot(self):
        """(registered devices, active reservations) — both pruned.

        Public view: bearer tokens are stripped, expired tombstones hidden.
        """
        with self._lock:
            self._prune_expired()
            devices = [self._public(record)
                       for record in self._devices.values()
                       if record['status'] == 'registered']
            reservations = [self._public(record)
                            for record in self._devices.values()
                            if record['status'] == 'reserved']
            devices.sort(key=lambda record: record['number'])
            reservations.sort(key=lambda record: record['number'])
            return devices, reservations

    @staticmethod
    def _public(record):
        return {key: value for key, value in record.items() if key != 'token'}

    def is_empty(self):
        with self._lock:
            return not self._devices
