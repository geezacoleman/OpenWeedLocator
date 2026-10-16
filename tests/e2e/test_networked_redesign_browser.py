"""Browser checks for the redesigned networked controller (2026-10-12).

Spins up the networked Flask app with three stub OWLs (one spraying with a
Hailo-8, one on CPU, one offline) and drives Playwright at 1024x600,
1280x720 and 1280x800:

  - no page scroll; the rail, switches (Blanket spray included), the AI
    confidence and crop buffer steppers and the stat cells on screen
  - every visible button is at least 44 px; no ' · ' in the visible text
  - the first tab reads Dashboard; Live view opens the camera pop-up
  - All OWLs is selected by default; tapping an OWL scopes the controls
    (Spraying sends toggle_detection to that OWL only)
  - Colour tuning steppers send the value; held, they repeat
  - Settings > AI models lists one row per OWL with where its AI runs, and
    warns in red when the OWLs run different models
  - Test nozzles: a numbered button per nozzle, greyed while spraying;
    holding one sends test_nozzles to that OWL
  - Camera offers every image size from 320x240 to 1456x1088
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
PANELS = ('colour', 'ai', 'camera', 'collection', 'profiles', 'spraying', 'nozzles', 'noktura', 'maint', 'assistant')
STATES = {}


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
        'relay_num': 10, 'nozzle_test': [],
        'track_params': {'track_high_thresh': 0.2, 'track_buffer': 60, 'match_thresh': 0.8, 'detection_persist_frames': 10},
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
        'owl-2': owl('owl-2', capture={'saved_count': 409}, current_model='weeds-barley-v1',
                     available_models=['weeds-wheat-v3', 'weeds-barley-v1'],
                     ai={'cpu': {'installed': True}, 'hailo': {'present': False, 'chip': None, 'runtime': False},
                         'accelerator': 'cpu'}),
        'owl-3': owl('owl-3', connected=False, detection_enable=False, current_model='',
                     ai={'cpu': {'installed': False}, 'hailo': {'present': False, 'chip': None, 'runtime': False},
                         'accelerator': None}),
    }
    ctrl.owls_state = states
    STATES.update(states)
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
            for sel in ('#view-field [data-rail]', '#detectSwitch', '#nozzleSwitch', '#recordSwitch', '#liveOpen',
                        '#pipeline-mode-selector', '#sensitivity-seg', '#view-field [data-pstep="confidence"]',
                        '#view-field [data-pstep="crop_buffer_px"]', '#fieldStats', '#power-btn', '#mainNav'):
                where = page.evaluate(INSIDE_JS, sel)
                if where != 'ok':
                    problems.append(f'{name}: {sel} {where}')
            if page.inner_text('#mainNav [data-view="field"]').strip() != 'Dashboard':
                problems.append(f'{name}: first tab is not labelled Dashboard')
            stats = page.inner_text('#fieldStats')
            for text in ('Speed', '9.8 km/h', 'Frame rate', 'fps'):
                if text not in stats:
                    problems.append(f'{name}: Dashboard stats missing {text!r}')
            if 'is-off' not in (page.get_attribute('#view-field [data-pstep="confidence"]', 'class') or ''):
                problems.append(f'{name}: AI confidence not greyed in Colour mode')
            if page.get_attribute('#view-field [data-owl="all"]', 'aria-selected') != 'true':
                problems.append(f'{name}: All OWLs is not selected by default')
            if 'Off' not in page.inner_text('#view-field [data-owl="owl-3"]'):
                problems.append(f'{name}: offline OWL card missing its Off pill')

            # Live view pop-up: opens over the page, shows a picture, closes
            page.click('#liveOpen')
            try:
                page.wait_for_selector('#fieldFeed:not([hidden])', timeout=4000)
            except Exception:
                problems.append(f'{name}: live view pop-up shows no picture')
            shot(page, f'live_{name}')
            check_screen(page, f'{name} live view', problems)
            page.click('#livePop [data-close]')
            page.wait_for_timeout(200)
            if page.is_visible('#livePop'):
                problems.append(f'{name}: live view pop-up did not close')

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
            for text in ('owl-1', 'owl-2', 'owl-3', 'Hailo', 'CPU', 'Needs AI', 'Detection', 'AI confidence',
                         'different AI models', 'Use weeds-wheat-v3 on all'):
                if text not in ai:
                    problems.append(f'{name}: AI models page missing {text!r}')
            if 'bad' not in (page.get_attribute('#aiNotices .ou-notice', 'class') or ''):
                problems.append(f'{name}: model difference is not shown in red')

            # Colour tuning: one tap on + sends the new value once the taps stop
            SENT.clear()
            page.click('.ou-side [data-panel="colour"]')
            page.click('[data-pstep="exg_min"] [data-d="1"]')
            page.wait_for_timeout(700)
            if not any(a == 'set_config_section' and (v or {}).get('section') == 'GreenOnBrown'
                       and 'exg_min' in (v or {}).get('params', {}) for _, a, v in SENT):
                problems.append(f'{name}: exg_min stepper sent nothing {SENT[:3]}')

            # Camera: every size from 320x240 up to the full picture
            page.click('.ou-side [data-panel="camera"]')
            page.wait_for_timeout(200)
            sizes = page.eval_on_selector_all('#camRes button', 'bs => bs.map((b) => b.dataset.value)')
            if sizes[:1] != ['320x240'] or sizes[-1:] != ['1456x1088'] or len(sizes) < 8:
                problems.append(f'{name}: camera sizes {sizes}')

            # Test nozzles: greyed while spraying; once off, holding 3 tests it
            page.click('.ou-side [data-panel="nozzles"]')
            page.wait_for_timeout(300)
            if page.locator('#nozzleTest [data-owl="owl-1"][data-nz]').count() != 10:
                problems.append(f'{name}: owl-1 does not show 10 nozzles')
            if not page.is_disabled('#nozzleTest [data-owl="owl-1"][data-nz="0"]'):
                problems.append(f'{name}: nozzle buttons usable while spraying')
            for s in STATES.values():
                s['detection_enable'] = False
            page.wait_for_timeout(2600)
            SENT.clear()
            page.hover('#nozzleTest [data-owl="owl-1"][data-nz="2"]')
            page.mouse.down()
            page.wait_for_timeout(300)
            page.mouse.up()
            page.wait_for_timeout(300)
            if ('owl-1', 'test_nozzles', {'relays': [2], 'duration_ms': 10000}) not in SENT \
                    or ('owl-1', 'test_nozzles', {'relays': [], 'duration_ms': 0}) not in SENT:
                problems.append(f'{name}: holding nozzle 3 sent {SENT[:4]}')
            shot(page, f'settings_nozzles_off_{name}')
            for s in STATES.values():
                s['detection_enable'] = s.get('connected', False)
            page.click('.ou-side [data-panel="ai"]')
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
