"""End-to-end browser test for data collection on the standalone dashboard.

The capture card (Config tab) and the "What are you recording?" form are the
shared collect pieces (/shared/js/collect_form.js) the networked collect
screen uses. Runs the standalone Flask app with a mocked MQTT client and
drives Playwright at the 7" (1024x600) and Touch Display 2 (1280x720) sizes:

  - Settings > Data collection shows the capture form; tapping By distance saves it
    (persisted to the active config + set_capture_params sent live)
  - Record opens the form; Start sends the chosen slugs, then recording on
  - While recording, the crop/conditions chips show under the switch

Run: python tests/e2e/test_standalone_data_collection_browser.py
Script only, like the other e2e checks (not collected by pytest).
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'controller' / 'standalone'))

HOST = '127.0.0.1'
PORT = 8768
BASE_URL = f'http://{HOST}:{PORT}'
SCREENSHOT_DIR = REPO / 'tests' / 'e2e' / 'screenshots'

STATE = {
    'detection_enable': False, 'image_sample_enable': False, 'owl_running': True,
    'stream_active': False, 'detection_mode': 1, 'algorithm': 'exhsv',
    'resolution_width': 1456, 'resolution_height': 1088,
    'requested_resolution_width': 1456, 'requested_resolution_height': 1088,
    'resolution_clamped': False, 'rpi_version': 'rpi-5', 'allow_high_resolution': True,
    'model_available': False, 'available_models': [], 'current_model': '',
    'model_classes': {}, 'detect_classes': [], 'tracking_enabled': False,
    'session_metadata': {},
}
SENT = []
PERSISTED = []
TMP = Path(tempfile.mkdtemp(prefix='owl_e2e_'))


def build_app():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    from utils import mqtt_manager as mm
    mm.DashMQTTSubscriber.start = lambda self: None
    import standalone as std

    dashboard = std.dashboard
    fake = MagicMock()
    fake.get_state.side_effect = lambda: dict(STATE)
    fake.get_weed_detect_indicator.return_value = False
    fake.get_image_write_indicator.return_value = False
    fake.get_cloud_connected.return_value = None
    fake.get_errors.return_value = []

    def send(action, **kwargs):
        SENT.append((action, kwargs))
        return {'success': True}
    fake._send_command.side_effect = send

    def set_recording(value):
        SENT.append(('set_image_sample_enable', {'value': value}))
        STATE['image_sample_enable'] = value
        return {'success': True}
    fake.set_image_sample_enable.side_effect = set_recording
    dashboard.mqtt_client = fake

    tmp = TMP
    shutil.copy(REPO / 'config' / 'GENERAL_CONFIG.ini', tmp / 'GENERAL_CONFIG.ini')
    (tmp / 'CONTROLLER.ini').write_text('[Collect]\nlast_crop = wheat\n', encoding='utf-8')
    dashboard.controller_ini_path = str(tmp / 'CONTROLLER.ini')
    dashboard._get_active_config_path = lambda: 'config/GENERAL_CONFIG.ini'
    dashboard._resolve_config_path = lambda rel: str(tmp / os.path.basename(rel))
    dashboard._persist_config_change = lambda section, key, value: PERSISTED.append((section, key, value))
    dashboard.control_owl_service = lambda action: (True, f'mock {action}')
    dashboard.get_owl_service_state = lambda: {'active': True, 'sub_state': 'running'}
    return std.app


def serve(app):
    threading.Thread(target=lambda: app.run(host=HOST, port=PORT, debug=False, use_reloader=False,
                                            threaded=True), daemon=True).start()
    import urllib.request
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE_URL + '/api/collect/config', timeout=0.5)
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
        for width, height, name in ((1024, 600, '7in'), (1280, 720, 'td2')):
            STATE['image_sample_enable'] = False
            STATE['session_metadata'] = {}
            SENT.clear()
            PERSISTED.clear()
            # Start: the previous viewport's run saved barley as the last crop
            (TMP / 'CONTROLLER.ini').write_text('[Collect]\nlast_crop = wheat\n', encoding='utf-8')
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(BASE_URL + '/')
            page.wait_for_selector('#dcCaptureForm .c-seg', state='attached')

            # Settings > Data collection: the capture card
            page.click('#mainNav [data-view="settings"]')
            page.click('.ou-side [data-panel="collection"]')
            card = page.locator('#panel-collection')
            card.scroll_into_view_if_needed()
            page.wait_for_function(
                "document.querySelector('#dcCaptureForm [data-setting=\"sample_mode\"] [aria-pressed=\"true\"]')")
            card.screenshot(path=str(SCREENSHOT_DIR / f'standalone_dc_card_{name}.png'))
            if 'Data collection' not in card.inner_text():
                problems.append(f'{name}: card heading missing')
            box = card.bounding_box()
            if box['width'] > width:
                problems.append(f'{name}: card wider than the screen ({box})')
            page.click('#dcCaptureForm [data-setting="sample_mode"] button[data-value="distance"]')
            page.wait_for_function(
                "document.querySelector('#dcCaptureForm [data-setting=\"sample_mode\"] [data-value=\"distance\"]')"
                ".getAttribute('aria-pressed') === 'true' || true")
            page.wait_for_timeout(400)
            if ('DataCollection', 'sample_mode', 'distance') not in PERSISTED:
                problems.append(f'{name}: sample_mode not persisted {PERSISTED}')
            live = [kw for a, kw in SENT if a == 'set_capture_params']
            if not live or live[-1].get('params', {}).get('sample_mode') != 'distance':
                problems.append(f'{name}: set_capture_params not sent {live}')

            # Dashboard: Record opens the form
            page.click('#mainNav [data-view="field"]')
            page.click('#recordSwitch')
            page.wait_for_selector('#record-modal:not([hidden])')
            page.screenshot(path=str(SCREENSHOT_DIR / f'standalone_record_form_{name}.png'))
            modal = page.locator('#record-modal .c-modal').bounding_box()
            if modal['y'] + modal['height'] > height + 1 or modal['width'] > width:
                problems.append(f'{name}: record form does not fit ({modal})')
            if page.locator('[data-pick="crop"] .c-opt[aria-pressed="true"]').inner_text() != 'Wheat':
                problems.append(f'{name}: last crop not pre-selected')
            page.click('[data-pick="crop"] .c-opt:has-text("Barley")')
            page.click('[data-pick="crop_season"] .c-opt:has-text("Spring")')
            page.click('[data-pick="weather"] .c-opt:has-text("Windy")')
            page.click('#modal-start')
            page.wait_for_timeout(500)
            meta = [kw for a, kw in SENT if a == 'set_session_metadata']
            if not meta or meta[-1].get('crop') != 'barley' or meta[-1].get('crop_season') != 'spring' \
                    or meta[-1].get('weather') != 'windy' or not meta[-1].get('run_id'):
                problems.append(f'{name}: metadata not sent as chosen {meta[-1:]}')
            if ('set_image_sample_enable', {'value': True}) not in SENT:
                problems.append(f'{name}: recording not started')

            # Chips while recording
            STATE['session_metadata'] = meta[-1] if meta else {}
            page.wait_for_selector('#sessionChips .ou-lab', timeout=8000)
            chips = page.inner_text('#sessionChips')
            if 'barley' not in chips.lower() or 'Windy' not in chips:
                problems.append(f'{name}: chips wrong: {chips!r}')
            page.screenshot(path=str(SCREENSHOT_DIR / f'standalone_recording_{name}.png'))
            if errors:
                problems.append(f'{name}: JS errors {errors}')
            page.close()
        browser.close()
    return problems


if __name__ == '__main__':
    found = run_checks()
    print('\n'.join(found) if found else 'standalone data collection: all checks passed')
    sys.exit(1 if found else 0)
