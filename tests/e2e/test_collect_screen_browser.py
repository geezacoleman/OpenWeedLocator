"""End-to-end browser test for the data-collection screen (templates/collect.html).

Runs the networked Flask app with two fake recording OWLs (capture state,
quality summary, thumbnails, map points, sessions) and drives Playwright at
both kiosk sizes:

  - 7" EDATEC 1024x600 and 10" HMI3010 1280x800: no page scroll, record
    button, status rows, warnings and four gallery images visible
  - Start recording opens the modal; Start posts the chosen metadata
  - DK switches the labels
  - Settings, Sessions: ticking a session and Upload posts the request

Run: python tests/e2e/test_collect_screen_browser.py   (screenshots in tests/e2e/screenshots)
Script only, like the other e2e checks (not collected by pytest).
"""

import io
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'controller' / 'networked'))
os.environ.setdefault('OWL_SKIP_INIT', '1')

HOST = '127.0.0.1'
PORT = 8767
BASE_URL = f'http://{HOST}:{PORT}'
SCREENSHOT_DIR = REPO / 'tests' / 'e2e' / 'screenshots'

SENT = []


def _jpeg(colour):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (480, 360), colour).save(buf, format='JPEG')
    return buf.getvalue()


def _owl(device_id, count, smear):
    return {
        'device_id': device_id, 'connected': True, 'last_seen': time.time(),
        'image_sample_enable': True, 'detection_enable': False, 'owl_running': True,
        'storage_free_mb': 41000, 'internet_ok': True, 'clock_synced': True,
        'camera': {'colour_temperature': 5400.0},
        'capture': {
            'sample_mode': 'distance', 'sample_distance_m': 1.0, 'sample_interval_s': 2.0,
            'record_mode': 'images', 'saved_count': count, 'session_id': 'session_090000',
            'exposure_time_us': 800, 'analogue_gain': 3.2, 'speed_kmh': 9.8,
            'ground_smear_mm': smear, 'shutter_capped': False, 'max_gain': None,
            'qc': {'median': {'sharpness': 30.0, 'brightness': 110.0, 'clipped': 0.5, 'dark': 1.0},
                   'baseline_sharpness': 31.0, 'recent_sharpness': [30.0, 29.0, 30.5], 'count': 10},
        },
        'data_sessions': [{'session_id': '20261008/session_090000', 'date': '20261008', 'time': '090000',
                           'image_count': count, 'total_size': 180 * 1048576}],
        'data_transfer': {'status': 'idle'},
    }


def build_app():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    import networked as nw

    ctrl = nw.controller
    ctrl.owls_state = {'owl-1': _owl('owl-1', 412, 2.2), 'owl-2': _owl('owl-2', 409, 1.4)}
    ctrl.mqtt_connected = True
    ctrl.cloud_enable = True
    ctrl.cloud_connected = True
    ctrl.cloud_device_id = 'rig-test-1'

    def fake_send(device_id, action, value=None):
        SENT.append((device_id, action, value))
        return {'success': True, 'message': 'ok'}
    ctrl.send_command = fake_send
    ctrl._publish_controller_presence = lambda: None
    nw._write_collect_section = lambda values: None

    ctrl.collect.start({'crop': 'wheat'})
    for i in range(30):
        ctrl.collect.add_capture('owl-1', {'lat': 55.5 + i * 1e-5, 'lon': 9.7, 'ts': 't'})
        ctrl.collect.add_capture('owl-2', {'lat': 55.5 + i * 1e-5, 'lon': 9.70003, 'ts': 't'})
    for colour, owl in (((120, 90, 60), 'owl-1'), ((90, 120, 60), 'owl-2')):
        ctrl.collect.add_thumb(owl, _jpeg(colour))
        ctrl.collect.add_thumb(owl, _jpeg(colour))
    ctrl.collect.metadata = {'crop': 'wheat', 'crop_season': 'winter', 'weather': 'dry',
                             'lighting': 'natural_sun', 'purpose': 'herbicide'}

    def keep_alive():
        while True:
            for s in ctrl.owls_state.values():
                s['last_seen'] = time.time()
            time.sleep(1)
    threading.Thread(target=keep_alive, daemon=True).start()
    return nw.app


def serve(app):
    threading.Thread(target=lambda: app.run(host=HOST, port=PORT, debug=False, use_reloader=False,
                                            threaded=True), daemon=True).start()
    import urllib.request
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE_URL + '/collect', timeout=0.5)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError('server did not start')


def run_checks():
    from playwright.sync_api import sync_playwright
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    serve(build_app())
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, height, name in ((1024, 600, '7in'), (1280, 800, '10in')):
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(BASE_URL + '/collect')
            page.wait_for_selector('.c-owl-row')
            page.wait_for_function("document.querySelectorAll('.c-shot img:not([hidden])').length === 4")
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_record_{name}.png'))

            scroll = page.evaluate('[document.documentElement.scrollHeight, window.innerHeight,'
                                   ' document.documentElement.scrollWidth, window.innerWidth]')
            if scroll[0] > scroll[1] or scroll[2] > scroll[3]:
                problems.append(f'{name}: page scrolls {scroll}')
            for selector in ('#record-btn', '#warnings .c-warn', '.c-gallery .c-shot'):
                box = page.locator(selector).first.bounding_box()
                if not box or box['y'] + box['height'] > height + 1:
                    problems.append(f'{name}: {selector} not fully on screen ({box})')
            if 'blur risk' not in page.inner_text('#warnings').lower():
                problems.append(f'{name}: blur warning missing')

            # Stop then start: modal + metadata
            page.click('#record-btn')
            page.wait_for_selector('#stop-modal:not([hidden])')
            page.click('#stop-yes')
            ctrl_state = __import__('networked').controller
            for s in ctrl_state.owls_state.values():
                s['image_sample_enable'] = False
            page.wait_for_function("document.getElementById('record-label').textContent === 'Start recording'")
            page.click('#record-btn')
            page.wait_for_selector('#record-modal:not([hidden])')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_modal_{name}.png'))
            modal = page.locator('#record-modal .c-modal').bounding_box()
            if modal['y'] + modal['height'] > height + 1:
                problems.append(f'{name}: record modal taller than the screen')
            page.click('[data-pick="crop"] .c-opt:has-text("Barley")')
            page.click('[data-pick="crop_season"] .c-opt:has-text("Spring")')
            page.click('#modal-start')
            page.wait_for_timeout(300)

            # Danish
            page.click('.c-lang [data-lang="da"]')
            if page.inner_text('.c-tab[data-view="map"]') != 'Kort':
                problems.append(f'{name}: DK did not switch labels')
            page.click('.c-lang [data-lang="en"]')

            # Sessions + upload
            page.click('.c-tab[data-view="settings"]')
            page.wait_for_selector('.c-tick')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_sessions_{name}.png'))
            page.locator('.c-tick').first.click()
            page.click('#upload-btn')
            page.wait_for_timeout(300)

            page.click('.c-setnav [data-pane="camera"]')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_camera_{name}.png'))
            page.click('.c-tab[data-view="map"]')
            page.wait_for_timeout(500)
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_map_{name}.png'))

            for s in ctrl_state.owls_state.values():
                s['image_sample_enable'] = True
            if errors:
                problems.append(f'{name}: JS errors {errors}')
            page.close()
        browser.close()

    import networked as nw
    starts = [v for d, a, v in SENT if a == 'set_session_metadata']
    if not starts or starts[-1].get('crop') != 'barley' or starts[-1].get('crop_season') != 'spring':
        problems.append(f'metadata not sent as chosen: {starts[-1:]}')
    if not nw.controller.collect.upload_list():
        problems.append('upload request not created')
    return problems


if __name__ == '__main__':
    found = run_checks()
    print('\n'.join(found) if found else 'collect screen: all checks passed')
    sys.exit(1 if found else 0)
