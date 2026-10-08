"""
Cloud (Noktura) bridge provisioning from the phone app — OWL trust chain MVP.

The app claims the unit in Noktura, receives the mosquitto bridge credential
once (POST /api/v1/me/devices/<serial>/cloud-credential) and posts it to the
unit: during first boot to POST /setup/api/cloud/provision, afterwards to the
dashboard's POST /api/settings/cloud/provision. Both routes call
`provision(runner, payload)` here.

Root work happens in /usr/local/sbin/owl-cloud-provision, a root-owned helper
written by install_firstboot.sh --cloud-helper (run by --arm and by
controller/shared/setup.sh for every standalone unit) and permitted by
sudoers. The helper reads the credential on STDIN (one JSON line, then the
password line) so the password never appears in argv, ps or the journal,
re-validates every field and runs a ROOT-OWNED copy of owl_cloud_provision.sh
from /usr/local/lib/owl/ (never the repo file, which the owl user can edit).

A unit already connected to a DIFFERENT Noktura site is not re-pointed unless
the caller passes "replace": true (cross-repo review 2026-09-17, O-5). The
same site may be re-provisioned freely: that is how a rotated password or a
retry after an app timeout lands.

Spec: Noktura/docs/owl/OWL_IDENTITY_AND_CLAIM_SPEC.md §5.6.
"""

import configparser
import json
import logging
import re
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CLOUD_HELPER = '/usr/local/sbin/owl-cloud-provision'
CLOUD_HELPER_TIMEOUT_S = 120   # apt may install mosquitto on a networked OWL
PERMISSIONS_MISSING_ERROR = (
    'Cloud provisioning helper not installed on this OWL. Run: '
    'sudo bash controller/setup/install_firstboot.sh --cloud-helper')

# Shapes shared with owl_cloud_provision.sh, the helper and Noktura's
# fleet_sites.slug check constraint.
SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$')
HOST_RE = re.compile(r'^[A-Za-z0-9.-]{3,253}$')
PORTAL_RE = re.compile(r'^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/-]*)?$')
STORAGE_HOST_RE = re.compile(r'^[a-z0-9.-]{3,253}$')
PASSWORD_MIN, PASSWORD_MAX = 16, 128
# The rig's pairing route is reachable from the whole rig LAN, so it only
# accepts Noktura's own broker: nobody on the LAN can re-point the bridge
# elsewhere. Other brokers (staging, self-hosted) go through
# owl_cloud_provision.sh by hand. Storage is not pinned: it lives with a
# storage provider, and upload URLs only ever arrive through this broker.
NOKTURA_BROKER_DOMAIN = 'noktura.tech'


def require_noktura_broker(payload):
    host = str(payload.get('broker_host') or '').strip().lower().rstrip('.')
    if host != NOKTURA_BROKER_DOMAIN and not host.endswith('.' + NOKTURA_BROKER_DOMAIN):
        raise ValueError(f'broker_host must be a {NOKTURA_BROKER_DOMAIN} address. '
                         'Run owl_cloud_provision.sh by hand for any other broker.')


class CloudSiteConflict(ValueError):
    """The unit is already connected to another Noktura site (HTTP 409)."""

    def __init__(self, existing_site):
        super().__init__(f'This OWL is already connected to Noktura as {existing_site}. '
                         'Send "replace": true to connect it to a different site.')
        self.existing_site = existing_site


class NetworkedOwlRefused(ValueError):
    """This OWL is part of a networked rig: the in-cab controller holds the
    rig's one cloud link, so the OWL itself is never provisioned (HTTP 409).
    Path A rigs are paired from the controller's collect screen."""

    code = 'networked_mode'

    def __init__(self):
        super().__init__('This OWL is part of a networked rig. Connect the rig to Noktura '
                         'from the in-cab controller instead.')


def refuse_networked_owl(controller_ini):
    """Raise NetworkedOwlRefused when CONTROLLER.ini says [Network] mode =
    networked. Called by the OWL-side routes only (first-boot setup API and
    the standalone dashboard), never by the controller, which is networked
    too and is the unit that should be provisioned."""
    config = configparser.ConfigParser()
    try:
        config.read(str(controller_ini))
    except configparser.Error:
        return
    if config.get('Network', 'mode', fallback='').strip().lower() == 'networked':
        raise NetworkedOwlRefused()


class CloudProvisionError(RuntimeError):
    """The helper is missing or failed; `detail` carries its stderr tail."""

    def __init__(self, message, detail=''):
        super().__init__(message)
        self.detail = detail


def validate_payload(data):
    """Return the validated credential dict or raise ValueError.

    Same rules as the helper and owl_cloud_provision.sh, applied first so
    the app gets a 400 with a reason instead of a helper exit code.
    """
    if not isinstance(data, dict):
        raise ValueError('JSON object required')
    host = str(data.get('broker_host') or '').strip()
    device_id = str(data.get('device_id') or '').strip()
    username = str(data.get('username') or device_id).strip()
    password = data.get('password')
    portal = str(data.get('portal_url') or '').strip()
    storage_host = str(data.get('storage_host') or '').strip().lower()
    raw_port = data.get('broker_port', 8883)
    # bool is an int subclass: `true` must not become port 1 (O-14).
    if isinstance(raw_port, bool):
        raise ValueError('broker_port must be a number')
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise ValueError('broker_port must be a number')
    if not HOST_RE.match(host):
        raise ValueError('broker_host is required (hostname)')
    if not 1 <= port <= 65535:
        raise ValueError('broker_port out of range')
    if not SLUG_RE.match(device_id):
        raise ValueError('device_id must be a lowercase slug (3-32 chars: letters, '
                         'digits, hyphens)')
    if username != device_id:
        raise ValueError('username must equal device_id')
    if (not isinstance(password, str)
            or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX
            or any(c in password for c in '\r\n\x00')):
        raise ValueError(f'password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters '
                         'on one line')
    if portal and not PORTAL_RE.match(portal):
        raise ValueError('portal_url must be an https URL')
    if storage_host and not STORAGE_HOST_RE.match(storage_host):
        raise ValueError('storage_host must be a hostname')
    return {'broker_host': host, 'broker_port': port, 'device_id': device_id,
            'username': username, 'password': password, 'portal_url': portal,
            'storage_host': storage_host}


def read_cloud_config(controller_ini):
    """The cloud link as CONTROLLER.ini [Cloud] describes it right now:
    {'enabled', 'device_id', 'portal_url', 'storage_host'}. Read per request by
    the setup API and the dashboard, so a unit provisioned while they run is
    reported correctly (cross-repo review 2026-09-17, O-18)."""
    config = configparser.ConfigParser()
    try:
        config.read(str(controller_ini))
    except configparser.Error:
        pass
    enabled = config.getboolean('Cloud', 'enable', fallback=False)
    return {
        'enabled': enabled,
        'device_id': config.get('Cloud', 'device_id', fallback='').strip() or None,
        'portal_url': config.get('Cloud', 'portal_url', fallback='').strip().rstrip('/') or None,
        'storage_host': config.get('Cloud', 'storage_host', fallback='').strip() or None,
    }


def current_cloud_site(controller_ini):
    """device_id of the site this unit is connected to, or None."""
    cloud = read_cloud_config(controller_ini)
    return cloud['device_id'] if cloud['enabled'] else None


def clock_synced(runner=subprocess.run):
    """True/False from systemd-timesyncd, None when it cannot be asked.

    The Pi has no battery-backed clock. Until NTP sets the time, TLS to the
    cloud broker fails and mosquitto keeps retrying. Where NTP is blocked the
    bridge never connects, so this is reported next to the cloud status
    instead of being papered over (D3, decided 2026-09-17: no fake clock, no
    boot ordering that would delay the local broker).
    """
    try:
        result = runner(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                        capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = (getattr(result, 'stdout', '') or '').strip().lower()
    if value in ('yes', 'no'):
        return value == 'yes'
    return None


def helper_stdin(cred):
    """The two lines the root helper reads. The password is line 2 only."""
    meta = {k: cred[k] for k in
            ('broker_host', 'broker_port', 'device_id', 'username', 'portal_url',
             'storage_host')}
    return json.dumps(meta) + '\n' + cred['password'] + '\n'


def provision(runner, data, controller_ini=None):
    """Validate `data`, run the root helper through `runner`, return the
    public result {'device_id', 'portal_url', 'provisioned_at'}.

    runner(argv, input=..., capture_output=True, text=True, timeout=...) has
    subprocess.run's signature so tests can inject a fake. Raises ValueError
    (bad payload) or CloudProvisionError (helper missing/failed). Never logs
    the password.
    """
    cred = validate_payload(data)
    if controller_ini is not None:
        existing = current_cloud_site(controller_ini)
        if existing and existing != cred['device_id'] and data.get('replace') is not True:
            raise CloudSiteConflict(existing)
    try:
        result = runner(['sudo', '-n', CLOUD_HELPER], input=helper_stdin(cred),
                        capture_output=True, text=True,
                        timeout=CLOUD_HELPER_TIMEOUT_S)
    except OSError as e:
        raise CloudProvisionError(PERMISSIONS_MISSING_ERROR, str(e))
    except subprocess.TimeoutExpired:
        raise CloudProvisionError('Cloud provisioning timed out on the OWL', '')
    rc = getattr(result, 'returncode', 1)
    stderr = getattr(result, 'stderr', '') or ''
    if rc != 0:
        tail = '\n'.join(stderr.strip().splitlines()[-5:])
        if 'a password is required' in stderr or 'not allowed' in stderr \
                or 'No such file' in stderr:
            raise CloudProvisionError(PERMISSIONS_MISSING_ERROR, tail)
        raise CloudProvisionError('Cloud provisioning failed on the OWL', tail)
    logger.info("Cloud bridge provisioned for site %s (%s:%s)",
                cred['device_id'], cred['broker_host'], cred['broker_port'])
    return {'device_id': cred['device_id'],
            'portal_url': cred['portal_url'] or None,
            'provisioned_at': datetime.now(timezone.utc).isoformat()}
