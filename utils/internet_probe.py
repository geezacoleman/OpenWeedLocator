"""
Internet reachability probe: a default route plus one ping.

Used by the dashboard (/api/settings/internet, update preflight) and by
the upload manager. Bounded to ~4.5 s worst case so it fits inside the
phone app's 6 s request timeout. No sudo needed.
"""

import subprocess
import time
from pathlib import Path

IP_CANDIDATES = ('/sbin/ip', '/usr/sbin/ip', '/bin/ip', '/usr/bin/ip')
PING_CANDIDATES = ('/bin/ping', '/usr/bin/ping', '/sbin/ping', '/usr/sbin/ping')
PING_HOST = '8.8.8.8'
ROUTE_TIMEOUT_S = 2
PING_WAIT_S = 2
PING_TIMEOUT_S = 4


def _first_existing(candidates):
    for path in candidates:
        if Path(path).exists():
            return path
    return None


def _default_route(stdout):
    """('has_route', 'device') from `ip route` output."""
    for line in (stdout or '').splitlines():
        parts = line.split()
        if parts and parts[0] == 'default':
            dev = None
            if 'dev' in parts:
                index = parts.index('dev')
                if index + 1 < len(parts):
                    dev = parts[index + 1]
            return True, dev
    return False, None


def probe_internet(runner=None, ping_host=PING_HOST):
    """{'reachable', 'has_route', 'can_ping', 'default_dev', 'checked_at',
    'error'} — never raises."""
    run = runner or subprocess.run
    result = {'reachable': False, 'has_route': False, 'can_ping': False,
              'default_dev': None, 'checked_at': int(time.time()),
              'error': None}
    try:
        ip_bin = _first_existing(IP_CANDIDATES)
        if not ip_bin:
            result['error'] = 'ip command not found'
            return result
        route = run([ip_bin, 'route'], capture_output=True, text=True,
                    timeout=ROUTE_TIMEOUT_S)
        if getattr(route, 'returncode', 1) != 0:
            result['error'] = 'Failed to check network routes'
            return result
        has_route, dev = _default_route(route.stdout)
        result['has_route'] = has_route
        result['default_dev'] = dev
        if not has_route:
            result['error'] = 'No default route'
            return result

        ping_bin = _first_existing(PING_CANDIDATES)
        if not ping_bin:
            result['error'] = 'ping command not found'
            return result
        ping = run([ping_bin, '-c', '1', '-W', str(PING_WAIT_S), ping_host],
                   capture_output=True, timeout=PING_TIMEOUT_S)
        result['can_ping'] = getattr(ping, 'returncode', 1) == 0
        result['reachable'] = result['can_ping']
        if not result['can_ping']:
            result['error'] = 'No internet connectivity'
    except subprocess.TimeoutExpired:
        result['error'] = 'Network check timed out'
    except Exception as e:
        result['error'] = str(e)
    return result
