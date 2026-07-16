"""
Serve the REAL first-boot setup API against fake nmcli/system backends.

Contract-test backend for the owl-app repo's Playwright `contract` project:
the phone app's real JS drives this real Flask app over real HTTP, so any
drift between the two repos fails a test instead of surfacing in a paddock.

    python tests/integration/serve_setup_app.py --port 8123

Behaviour knobs are baked in and deterministic:
  - hotspot OWL-TEST up, scan cache: FarmWiFi (82), Shed (40)
  - joining any network with password 'badpass99' fails with kind 'auth'
    (mirrors the app's mock-mode failure password); anything else succeeds
  - timings are shortened (switch 0.5 s) so wizard runs stay fast
  - finish() re-arms instead of exiting, so one server instance survives a
    whole Playwright suite
"""

import argparse
import logging
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeNmcli, FakeSystem  # noqa: E402
from controller.setup import firstboot_state as fbs  # noqa: E402
from controller.setup.setup_app import create_app  # noqa: E402
from utils.network_manager import NetworkManager  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8123)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(name)s %(levelname)s %(message)s')

    fbs.SWITCH_DELAY_S = 0.5
    fbs.EXIT_DELAY_S = 0.1
    fbs.HOTSPOT_RETRY_S = 0.5
    fbs.REVERT_HOTSPOT_ATTEMPTS = 1

    workdir = Path(tempfile.mkdtemp(prefix='owl-contract-'))
    flag = workdir / 'owl-firstboot.flag'
    flag.touch()
    controller_ini = workdir / 'CONTROLLER.ini'
    controller_ini.write_text(
        '[MQTT]\nenable = False\nbroker_ip = localhost\n'
        'broker_port = 1883\ndevice_id = owl-fresh\n\n'
        '[WebDashboard]\nport = 8000\n')
    fbs.AVAHI_SERVICE_FILE = str(workdir / 'owl-setup.service')

    fake = FakeNmcli()
    system = FakeSystem()

    def runner(argv, timeout=None):
        # 'badpass99' mirrors the app mock's failure password: a PSK set on
        # a client profile decides that profile's join outcome
        args_list = argv[1:]
        if (len(args_list) >= 4 and args_list[0] == 'con'
                and args_list[1] == 'modify' and 'wifi-sec.psk' in args_list):
            psk = args_list[args_list.index('wifi-sec.psk') + 1]
            name = args_list[2]
            if fake.profiles.get(name, {}).get('mode') != 'ap':
                fake.join_outcome[name] = 'auth' if psk == 'badpass99' else 'ok'
        return fake(argv, timeout=timeout)

    nm = NetworkManager(runner=runner)
    nm.is_supported = lambda: True

    def rearm():
        """finish() called _teardown: instead of exiting, become a fresh
        armed unit so the next Playwright test starts clean."""
        flag.touch()
        with fb._lock:
            fb.state = 'in_setup'
            fb.wifi_state = 'idle'
            fb.wifi_mode = None
            fb.wifi_ssid = None
            fb.wifi_ip = None
            fb.wifi_error = None
            fb.wifi_warning = None
            fb._persist()
        for name in [n for n, p in fake.profiles.items() if p['mode'] != 'ap']:
            del fake.profiles[name]
        if not fake.profiles:
            fake.add_hotspot()
        fb._raise_setup_hotspot()

    fb = fbs.FirstBootState(
        nm, flag_path=flag, state_file=workdir / 'state.json',
        system_runner=system, controller_ini=controller_ini,
        hostname_getter=lambda: system.hostname,
        exit_fn=rearm)

    app = create_app(network_manager=nm, state=fb)
    fb.startup()
    print(f'Contract setup API on http://127.0.0.1:{args.port} '
          f'(workdir {workdir})', flush=True)
    app.run(host='127.0.0.1', port=args.port, threaded=True)


if __name__ == '__main__':
    main()
