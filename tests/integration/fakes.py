"""
Stateful fakes for first-boot integration testing.

FakeNmcli emulates the nmcli argv surface utils/network_manager.py uses;
FakeSystem stands in for systemctl/hostnamectl/ufw. Shared by
tests/integration/test_firstboot_integration.py (pytest) and
tests/integration/serve_setup_app.py (the real-HTTP server the owl-app
contract Playwright project talks to).
"""

from types import SimpleNamespace

HOTSPOT = 'OWL-TEST'


class Result(SimpleNamespace):
    """subprocess.CompletedProcess stand-in."""


def ok(stdout=''):
    return Result(returncode=0, stdout=stdout, stderr='')


def fail(stderr, code=4):
    return Result(returncode=code, stdout='', stderr=stderr)


class FakeNmcli:
    """Stateful nmcli emulation covering the argv surface NetworkManager uses.

    Profiles are dicts: {'type', 'mode', 'active', 'psk', 'props'}.
    Knobs:
      join_outcome[ssid]: 'ok' (default) | 'auth' | 'timeout'
      hotspot_up_failures: int — that many 'con up <hotspot>' calls fail
      visible: [(ssid, signal, security)] returned by scans
    """

    def __init__(self, with_hotspot=True):
        self.profiles = {}
        if with_hotspot:
            self.add_hotspot()
        self.visible = [('FarmWiFi', '82', 'WPA2'), ('Shed', '40', 'WPA2')]
        self.join_outcome = {}
        self.hotspot_up_failures = 0
        self.calls = []

    def add_hotspot(self):
        self.profiles[HOTSPOT] = {'type': '802-11-wireless', 'mode': 'ap',
                                  'active': True, 'psk': 'factorypass',
                                  'props': {}}

    # -- helpers -----------------------------------------------------------
    def _ap_active(self):
        return any(p['mode'] == 'ap' and p['active']
                   for p in self.profiles.values())

    def _deactivate_wifi(self):
        for profile in self.profiles.values():
            if profile['type'] == '802-11-wireless':
                profile['active'] = False

    def active_client(self):
        for name, p in self.profiles.items():
            if p['mode'] != 'ap' and p['active']:
                return name
        return None

    def hotspot_active(self):
        return self.profiles.get(HOTSPOT, {}).get('active', False)

    def profile_priority(self, name):
        return self.profiles[name]['props'].get('connection.autoconnect-priority')

    # -- the runner --------------------------------------------------------
    def __call__(self, argv, timeout=None):
        assert argv[0] == 'nmcli'
        args = argv[1:]
        self.calls.append(args)

        if args[:3] == ['-t', '-f', 'SSID,SIGNAL,SECURITY']:
            rescan = args[-1] == 'yes'
            if rescan and self._ap_active():
                return fail('Error: Scanning not allowed while already scanning '
                            'or in AP mode', code=1)
            lines = [f'{s}:{sig}:{sec}' for s, sig, sec in self.visible]
            return ok('\n'.join(lines) + '\n')

        if args == ['-t', '-f', 'NAME,TYPE', 'con', 'show']:
            lines = [f'{n}:{p["type"]}' for n, p in self.profiles.items()]
            return ok('\n'.join(lines) + '\n')

        if args == ['-t', '-f', 'NAME,TYPE,DEVICE', 'con', 'show', '--active']:
            lines = [f'{n}:{p["type"]}:wlan0'
                     for n, p in self.profiles.items() if p['active']]
            return ok('\n'.join(lines) + '\n')

        if args[:4] == ['-t', '-f', '802-11-wireless.mode', 'con']:
            name = args[5]
            profile = self.profiles.get(name)
            if profile is None:
                return fail(f'Error: {name} - no such connection profile.', 10)
            return ok(f'802-11-wireless.mode:{profile["mode"]}\n')

        if args[:4] == ['-t', '-f', 'IP4.ADDRESS', 'dev']:
            client = self.active_client()
            if client:
                static = self.profiles[client]['props'].get('ipv4.addresses')
                addr = static or '192.168.1.77/24'
                return ok(f'IP4.ADDRESS[1]:{addr}\n')
            if self._ap_active():
                return ok('IP4.ADDRESS[1]:10.42.0.1/24\n')
            return ok('')

        if args[0] == 'con' or args[:1] == ['--wait']:
            con_args = args if args[0] == 'con' else args[2:]
            verb = con_args[1]
            if verb == 'add':
                name = con_args[con_args.index('con-name') + 1]
                self.profiles[name] = {'type': '802-11-wireless',
                                       'mode': 'infrastructure',
                                       'active': False, 'psk': None,
                                       'props': {}}
                return ok()
            name = con_args[2]
            profile = self.profiles.get(name)
            if verb == 'delete':
                if profile is None:
                    return fail(f'Error: {name} - no such connection profile.', 10)
                del self.profiles[name]
                return ok()
            if profile is None:
                return fail(f'Error: {name} - no such connection profile.', 10)
            if verb == 'modify':
                pairs = con_args[3:]
                for key, value in zip(pairs[::2], pairs[1::2]):
                    profile['props'][key] = value
                    if key == 'wifi-sec.psk':
                        profile['psk'] = value
                return ok()
            if verb == 'down':
                profile['active'] = False
                return ok()
            if verb == 'up':
                if profile['mode'] == 'ap':
                    if self.hotspot_up_failures > 0:
                        self.hotspot_up_failures -= 1
                        return fail('Error: Connection activation failed: '
                                    'device busy')
                    self._deactivate_wifi()
                    profile['active'] = True
                    return ok()
                outcome = self.join_outcome.get(name, 'ok')
                if outcome == 'auth':
                    return fail('Error: Connection activation failed: '
                                'Secrets were required, but not provided.')
                if outcome == 'timeout':
                    return fail('Error: Timeout expired.')
                self._deactivate_wifi()
                profile['active'] = True
                return ok()

        raise AssertionError(f'FakeNmcli: unhandled argv {args}')


class FakeSystem:
    """system_runner stand-in for systemctl/hostnamectl/ufw calls."""

    def __init__(self):
        self.calls = []
        self.fail_owl_restart = False
        self.hostname = 'owl-fresh'

    def __call__(self, argv, timeout=15):
        self.calls.append(list(argv))
        if argv[:2] == ['hostnamectl', 'set-hostname']:
            self.hostname = argv[2]
            return ok()
        if argv == ['systemctl', 'restart', 'owl.service'] and self.fail_owl_restart:
            return fail('Job for owl.service failed.', 1)
        return ok()
