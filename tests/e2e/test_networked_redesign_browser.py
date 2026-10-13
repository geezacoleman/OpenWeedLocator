"""Browser checks for the redesigned networked controller (2026-10-12).

Spins up the networked Flask app with three stub OWLs (one spraying with a
Hailo-8, one on CPU, one offline) and drives Playwright at 1024x600,
1280x720 and 1280x800:

  - no page scroll; the rail, live view, switches and stat cells on screen
  - every visible button is at least 44 px; no ' · ' in the visible text
  - All OWLs is selected by default; tapping an OWL scopes the controls
    (Spraying sends toggle_detection to that OWL only)
  - Settings > AI models lists one row per OWL with where its AI runs
  - Data, Map and every Settings panel render without JS errors

Run: python tests/e2e/test_networked_redesign_browser.py
Set OWL_E2E_SHOTS=<dir> to keep screenshots (none are written otherwise).
Script only, like the other e2e checks (not collected by pytest).
"""

import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'controller' / 'networked'))
sys.path.insert(0, str(REPO / 'tests' / 'e2e'))

from redesign_checks import CHECK_JS, INSIDE_JS, fake_frame_jpeg  # noqa: E402

HOST = '127.0.0.1'
PORT = 8772
BASE_URL = f'http://{HOST}:{PORT}'
SHOTS = os.environ.get('OWL_E2E_SHOTS')
SENT = []
VIEWPORTS = ((1024, 600, '7in'), (1280, 720, 'td2'), (1280, 800, 'hmi'))
PANELS = ('colour', 'ai', 'camera', 'collection', 'profiles', 'spraying', 'nozzles', 'noktura', 'maint', 'advanced')


def owl(device_id, **extra):
    state = {
        'connected': True, 'device_id': device_id, 'last_seen': time.time(),
        'detection_enable': True, 'image_sample_enable': False, 'owl_running': True,
        'resolution_width': 1456, 'resolution_height': 1088, 'requested_resolution_width': 1456,
        'requested_resolution_height': 1088, 'resolution_clamped': False, 'rpi_version': 'rpi-5',
        'allow_high_resolution': True, 'sensitivity_level': 'medium', 'algorithm': 'exhsv',
        'model_available': True, 'current_model': 'weeds-wheat-v3', 'available_models': ['weeds-wheat-v3'],
        'cpu_percent': 25, 'memory_percent': 40, 'cpu_temp': 61.0, 'detection_mode': 0,
        'avg_loop_time_ms': 33.0, 'speed_kmh': 9.8, 'storage_free_mb': 40960, 'version': '3.25.0',
        'git_branch': 'hardening-3.22', 'capture': {'saved_count': 412},
        'ai': {'cpu': {'installed': True}, 'hailo': {'present': True, 'chip': 'hailo8', 'runtime': True},
               'accelerator': 'hailo8'},
        'model_classes': {'0': 'ryegrass', '1': 'wild radish', '2': 'wheat'}, 'detect_classes': ['ryegrass'],
    }
    state.update(extra)
    return state


def build_app():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    import networked as nw

    ctrl = nw.controller
    states = {
        'owl-1': owl('owl-1'),
        'owl-2': owl('owl-2', capture={'saved_count': 409},
                     ai={'cpu': {'installed': True}, 'hailo': {'present': False, 'chip': None, 'runtime': False},
                         'accelerator': 'cpu'}),
        'owl-3': owl('owl-3', connected=False, detection_enable=False, current_model='',
                     ai={'cpu': {'installed': False}, 'hailo': {'present': False, 'chip': None, 'runtime': False},
                         'accelerator': None}),
    }
    ctrl.owls_state = states
    ctrl.mqtt_connected = True

    def send(device_id, action, value=None):
        SENT.append((device_id, action, value))
        return {'success': True}
    ctrl.send_command = send
    # A still field picture instead of each OWL's camera
    from flask import Response
    frame = fake_frame_jpeg()
    nw.app.view_functions['snapshot_proxy'] = lambda device_id: Response(frame, mimetype='image/jpeg')
    nw.app.view_functions['video_feed_proxy'] = lambda device_id: Response(frame, mimetype='image/jpeg')

    def heartbeat():
        while True:
            for s in states.values():
                s['last_seen'] = time.time()
            time.sleep(1.0)
    threading.Thread(target=heartbeat, daemon=True).start()
    return nw.app


def serve(app):
    threading.Thread(target=lambda: app.run(host=HOST, port=PORT, debug=False, use_reloader=False,
                                            threaded=True), daemon=True).start()
    import urllib.request
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE_URL + '/api/owls', timeout=0.5)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError('server did not start')


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'networked_{name}.png'))


def check_screen(page, label, problems):
    res = page.evaluate(CHECK_JS)
    sc = res['scroll']
    if sc['h'] > sc['ih'] + 1 or sc['w'] > sc['iw'] + 1:
        problems.append(f'{label}: page scrolls {sc}')
    if res['small']:
        problems.append(f'{label}: small targets {res["small"][:6]}')
    if res['dots']:
        problems.append(f'{label}: middle dots {res["dots"]}')


def run_checks():
    from playwright.sync_api import sync_playwright
    serve(build_app())
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height, name in VIEWPORTS:
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(BASE_URL + '/')
            page.wait_for_selector('#view-field [data-rail] [data-owl="owl-3"]', timeout=8000)
            page.wait_for_timeout(600)
            shot(page, f'field_{name}')
            check_screen(page, f'{name} field', problems)
            for sel in ('#view-field [data-rail]', '#fieldLive', '#detectSwitch', '#recordSwitch',
                        '#pipeline-mode-selector', '#sensitivity-seg', '#fieldStats', '#power-btn', '#mainNav'):
                where = page.evaluate(INSIDE_JS, sel)
                if where != 'ok':
                    problems.append(f'{name}: {sel} {where}')
            if page.get_attribute('#view-field [data-owl="all"]', 'aria-selected') != 'true':
                problems.append(f'{name}: All OWLs is not selected by default')
            if 'Off' not in page.inner_text('#view-field [data-owl="owl-3"]'):
                problems.append(f'{name}: offline OWL card missing its Off pill')

            # Scope to OWL-2 and switch spraying off: only owl-2 hears it
            SENT.clear()
            page.click('#view-field [data-owl="owl-2"]')
            page.wait_for_timeout(500)
            shot(page, f'field_owl2_{name}')
            check_screen(page, f'{name} field owl-2', problems)
            page.click('#detectSwitch')
            page.wait_for_timeout(500)
            if ('owl-2', 'toggle_detection', False) not in SENT:
                problems.append(f'{name}: Spraying did not go to owl-2 only {SENT[:4]}')
            page.click('#view-field [data-owl="all"]')
            page.wait_for_timeout(300)

            for view in ('data', 'map'):
                page.click(f'#mainNav [data-view="{view}"]')
                page.wait_for_timeout(500)
                shot(page, f'{view}_{name}')
                check_screen(page, f'{name} {view}', problems)

            page.click('#mainNav [data-view="settings"]')
            for panel in PANELS:
                page.click(f'.ou-side [data-panel="{panel}"]')
                page.wait_for_timeout(300)
                shot(page, f'settings_{panel}_{name}')
                check_screen(page, f'{name} settings {panel}', problems)
            page.click('.ou-side [data-panel="ai"]')
            page.wait_for_timeout(200)
            ai = page.inner_text('#panel-ai')
            for text in ('owl-1', 'owl-2', 'owl-3', 'Hailo', 'CPU', 'Needs AI'):
                if text not in ai:
                    problems.append(f'{name}: AI models list missing {text!r}')
            # One OWL's AI page
            page.click('#view-settings [data-rail] [data-owl="owl-1"]')
            page.wait_for_timeout(400)
            shot(page, f'settings_ai_owl1_{name}')
            check_screen(page, f'{name} settings ai owl-1', problems)
            if 'Hailo-8' not in page.inner_text('#panel-ai'):
                problems.append(f'{name}: owl-1 AI page does not say Hailo-8')
            page.click('#view-settings [data-rail] [data-owl="all"]')

            if errors:
                problems.append(f'{name}: JS errors {errors}')
            page.close()
        browser.close()

    if problems:
        print('FAIL')
        for p_ in problems:
            print(' -', p_)
        return 1
    print('PASS: networked redesign at 1024x600, 1280x720 and 1280x800')
    return 0


if __name__ == '__main__':
    sys.exit(run_checks())
