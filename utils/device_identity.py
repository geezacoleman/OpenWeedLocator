"""
Stable device identity for OWL units.

Two layers:

1. `get_device_serial()` — the Raspberry Pi hardware serial (hw_id). The
   phone app needs an identifier that tells two physical units apart even
   when both were self-installed with the defaults (hostname owl-1, hotspot
   SSID OWL-1 — see controller/shared/setup.sh). The hardware serial is
   burned into the SoC, unique across all units, identical for
   factory-imaged and self-installed devices, and survives re-imaging.
   /etc/machine-id covers non-Pi platforms (dev machines).

2. `read_identity()` — the cryptographic identity written by owl_identity.sh
   into /etc/owl-identity (OWL trust chain, Noktura spec
   docs/owl/OWL_IDENTITY_AND_CLAIM_SPEC.md §3):
     device.pub       P-256 public key, SPKI PEM
     tier             otp  (key in CM5 OTP, firmware signs)
                      disk (key file on the unit, Pi 4/5 makers)
     noktura_serial   OWL-YYYY-NNNNNN issued at registration (public)
   The fingerprint is SHA-256 of the DER SPKI — exactly what Noktura stores,
   so the app can compare the unit with the record by comparing 64 hex chars.
   A PEM body IS the DER, base64-encoded, so no crypto library is needed.

Reported (additive, no contract bump) by:
  - GET /setup/api/info      device_serial + identity  (first-boot setup service)
  - GET /api/system_stats    device_serial + identity  (standalone dashboard)
  - GET /api/identity        identity + public_key_pem (standalone dashboard)
"""

import base64
import configparser
import hashlib
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_CPUINFO = '/proc/cpuinfo'
_MACHINE_ID = '/etc/machine-id'

IDENTITY_DIR = Path(os.environ.get('OWL_IDENTITY_DIR', '/etc/owl-identity'))
IDENTITY_TIERS = ('otp', 'disk', 'none')
NOKTURA_SERIAL_RE = re.compile(r'^OWL-\d{4}-\d{6}$')
CONTROLLER_INI = Path(__file__).resolve().parents[1] / 'config' / 'CONTROLLER.ini'

_cached = False
_serial = None


def get_device_serial():
    """The unit's stable unique id, or None when neither source exists.

    Pi hardware serial first, /etc/machine-id as the fallback. Cached —
    neither value can change without a reboot.
    """
    global _cached, _serial
    if _cached:
        return _serial
    _serial = _read_pi_serial() or _read_machine_id()
    _cached = True
    return _serial


def _read_pi_serial():
    try:
        with open(_CPUINFO) as f:
            for line in f:
                if line.startswith('Serial'):
                    serial = line.split(':', 1)[1].strip()
                    # All-zero serials appear on some non-Pi ARM boards
                    if serial and set(serial) != {'0'}:
                        return serial
    except OSError:
        pass
    return None


def _read_machine_id():
    try:
        with open(_MACHINE_ID) as f:
            return f.read().strip() or None
    except OSError:
        logger.warning("No hardware serial or machine-id available; "
                       "device_serial will be null")
        return None


# ---------------------------------------------------------------------------
# Cryptographic identity (/etc/owl-identity)
# ---------------------------------------------------------------------------

def pem_fingerprint(pem):
    """SHA-256 hex of the DER SPKI inside a PUBLIC KEY PEM, or None if the
    text is not a public-key PEM. Identical to
    `openssl pkey -pubin -outform DER | sha256sum` and to Noktura's
    lib/fleet/identity.ts."""
    if not pem or 'BEGIN PUBLIC KEY' not in pem or 'PRIVATE' in pem:
        return None
    body = re.sub(r'-----[^-]+-----', '', pem)
    body = re.sub(r'\s+', '', body)
    try:
        der = base64.b64decode(body, validate=True)
    except (ValueError, TypeError):
        return None
    if not der:
        return None
    return hashlib.sha256(der).hexdigest()


def _read_text(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def _serial_from_cloud(controller_ini):
    """OWL-YYYY-NNNNNN from [Cloud] device_id (owl-yyyy-nnnnnn), or ''.

    The phone app finds a unit's Noktura serial by its key fingerprint
    (GET /api/v1/devices/by-fingerprint), so nobody has to write it onto the
    unit. Once the app has provisioned the cloud link, the site slug IS the
    lower-cased serial, so the unit can report it without a local file
    (decided 2026-09-17: lookup first, owl_identity.sh --serial optional).
    """
    config = configparser.ConfigParser()
    try:
        config.read(str(controller_ini))
    except configparser.Error:
        return ''
    device_id = config.get('Cloud', 'device_id', fallback='').strip()
    candidate = device_id.upper()
    return candidate if NOKTURA_SERIAL_RE.match(candidate) else ''


def read_identity(identity_dir=None, include_pem=False, controller_ini=None):
    """The unit's identity as the API reports it.

    Never raises: a unit without /etc/owl-identity is `tier: none` with the
    hardware serial only, which is every unit before owl_identity.sh ran.

    noktura_serial: the optional local label (owl_identity.sh --serial) if
    present, otherwise derived from the cloud link. Noktura's lookup by
    fingerprint is authoritative; the app warns if the two disagree.
    """
    d = Path(identity_dir) if identity_dir else IDENTITY_DIR
    tier = _read_text(d / 'tier') or 'none'
    if tier not in IDENTITY_TIERS:
        logger.warning("Unknown identity tier %r in %s; reporting none", tier, d)
        tier = 'none'
    pem = _read_text(d / 'device.pub') if tier != 'none' else ''
    fingerprint = pem_fingerprint(pem)
    if tier != 'none' and not fingerprint:
        logger.warning("Identity tier %s but device.pub is missing or not a "
                       "public-key PEM; reporting none", tier)
        tier = 'none'
        pem = ''
    serial = _read_text(d / 'noktura_serial')
    if serial and not NOKTURA_SERIAL_RE.match(serial):
        logger.warning("Ignoring malformed noktura_serial %r", serial)
        serial = ''
    if not serial:
        serial = _serial_from_cloud(controller_ini or CONTROLLER_INI)
    identity = {
        'hw_id': get_device_serial(),
        'tier': tier,
        'fingerprint': fingerprint,
        'noktura_serial': serial or None,
    }
    if include_pem:
        identity['public_key_pem'] = pem or None
    return identity
