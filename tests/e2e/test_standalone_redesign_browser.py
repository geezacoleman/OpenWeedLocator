"""Browser checks for the redesigned standalone dashboard (2026-10-12).

Runs the standalone Flask app with a mocked MQTT client and drives Playwright
at the kiosk sizes (EDATEC 7" 1024x600, Touch Display 2 1280x720, HMI3010
1280x800) and a phone (390x844):

  - no page scroll on the kiosk sizes; Field controls fully on screen
  - every visible button is at least 44 px (gloves)
  - no ' · ' joining items anywhere in the visible text
  - Settings side list opens each panel; AI models shows "AI isn't
    installed" and Install AI when the OWL reports no AI
  - Tapping AI on a unit without AI keeps Colour and shows the notice
  - narrow: one column, tabs at the bottom, Settings list opens a page

Run: python tests/e2e/test_standalone_redesign_browser.py
Set OWL_E2E_SHOTS=<dir> to keep screenshots (none are written otherwise).
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from redesign_checks import CHECK_JS, INSIDE_JS, fake_frame_jpeg  # noqa: E402

HOST = '127.0.0.1'
PORT = 8771
BASE_URL = f'http://{HOST}:{PORT}'
SHOTS = os.environ.get('OWL_E2E_SHOTS')

STATE = {
    'detection_enable': True, 'image_sample_enable': True, 'owl_running': True,
    'stream_active': False, 'detection_mode': 0, 'algorithm': 'exhsv',
    'resolution_width': 1456, 'resolution_height': 1088,
    'requested_resolution_width': 1456, 'requested_resolution_height': 1088,
    'resolution_clamped': False, 'rpi_version': 'rpi-5', 'allow_high_resolution': True,
    'model_available': False, 'available_models': ['weeds-wheat-v3_ncnn_model'], 'current_model': '',
    'model_classes': {}, 'detect_classes': [], 'tracking_enabled': False,
    'ai': {'cpu': {'installed': False}, 'hailo': {'present': False, 'chip': None, 'runtime': False},
           'accelerator': None},
    'session_metadata': {'crop': 'wheat', 'crop_season': 'winter', 'weather': 'dry',
                         'lighting': 'natural_sun', 'purpose': 'herbicide'},
    'capture': {'saved_count': 214, 'paused_slow': False, 'record_mode': 'images', 'min_speed_kmh': 1.0},
    'speed_kmh': 9.8, 'storage_free_mb': 9830, 'storage_warning': 'ok',
    'config_name': 'GENERAL_CONFIG.ini', 'sensitivity_level': 'medium',
}
TMP = Path(tempfile.mkdtemp(prefix='owl_e2e_'))
VIEWPORTS = ((1024, 600, '7in'), (1280, 720, 'td2'), (1280, 800, 'hmi'))
PANELS = ('colour', 'ai', 'camera', 'collection', 'profiles', 'nozzles', 'noktura', 'maint', 'advanced')

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
    fake.get_and_clear_errors.return_value = []
    fake._send_command.side_effect = lambda action, **kw: {'success': True}
    dashboard.mqtt_client = fake

    shutil.copy(REPO / 'config' / 'GENERAL_CONFIG.ini', TMP / 'GENERAL_CONFIG.ini')
    (TMP / 'CONTROLLER.ini').write_text('[Collect]\nlast_crop = wheat\n', encoding='utf-8')
    dashboard.controller_ini_path = str(TMP / 'CONTROLLER.ini')
    dashboard._get_active_config_path = lambda: 'config/GENERAL_CONFIG.ini'
    dashboard._resolve_config_path = lambda rel: str(TMP / os.path.basename(rel))
    dashboard._persist_config_change = lambda section, key, value: None
    dashboard.control_owl_service = lambda action: (True, f'mock {action}')
    dashboard.get_owl_service_state = lambda: {'active': True, 'sub_state': 'running'}
    # A recordings folder with one session so Data has a row
    save = TMP / 'rec'
    sess = save / '20261012' / 'session_191400'
    sess.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (sess / f'img_{i}.jpg').write_bytes(b'\xff\xd8' + b'0' * 2048)
    dashboard._get_save_directory = lambda: str(save)
    # A still field picture instead of the OWL's MJPEG stream
    from flask import Response
    frame = fake_frame_jpeg()
    std.app.view_functions['video_feed'] = lambda: Response(frame, mimetype='image/jpeg')
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


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f'standalone_{name}.png'))


def check_screen(page, label, problems, kiosk=True):
    res = page.evaluate(CHECK_JS)
    sc = res['scroll']
    if kiosk and (sc['h'] > sc['ih'] + 1 or sc['w'] > sc['iw'] + 1):
        problems.append(f'{label}: page scrolls {sc}')
    if not kiosk and sc['w'] > sc['iw'] + 1:
        problems.append(f'{label}: sideways scroll {sc}')
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
            page.wait_for_function("document.getElementById('stateText').textContent !== 'Connecting'")
            page.wait_for_timeout(500)
            shot(page, f'field_{name}')
            check_screen(page, f'{name} field', problems)
            for sel in ('#detectSwitch', '#recordSwitch', '#pipeline-mode-selector', '#sensitivity-buttons',
                        '#fieldStats', '#powerBtn', '#mainNav'):
                where = page.evaluate(INSIDE_JS, sel)
                if where != 'ok':
                    problems.append(f'{name}: {sel} {where}')
            if '214 photos' not in page.inner_text('#recordSwitch'):
                problems.append(f'{name}: recording hint missing the photo count')
            if 'Wheat' not in page.inner_text('#sessionChips') and 'wheat' not in page.inner_text('#sessionChips').lower():
                problems.append(f'{name}: recording chips missing')

            # AI on a unit without AI: stays on Colour, notice explains
            page.click('#pipeline-mode-selector [data-mode="gog"]')
            page.wait_for_timeout(200)
            if "isn't installed" not in page.inner_text('#detectNotice'):
                problems.append(f'{name}: no AI notice after tapping AI')
            if 'active' not in (page.get_attribute('#pipeline-mode-selector [data-mode="gob"]', 'class') or ''):
                problems.append(f'{name}: Colour lost its selection')
            shot(page, f'field_ai_blocked_{name}')
            check_screen(page, f'{name} field with notice', problems)

            # Data
            page.click('#mainNav [data-view="data"]')
            page.wait_for_selector('.ou-sess', timeout=5000)
            shot(page, f'data_{name}')
            check_screen(page, f'{name} data', problems)

            # Settings panels
            page.click('#mainNav [data-view="settings"]')
            for panel in PANELS:
                page.click(f'.ou-side [data-panel="{panel}"]')
                page.wait_for_timeout(250)
                shot(page, f'settings_{panel}_{name}')
                check_screen(page, f'{name} settings {panel}', problems)
            page.click('.ou-side [data-panel="ai"]')
            ai_text = page.inner_text('#panel-ai')
            if "AI isn't installed" not in ai_text or 'Install AI' not in ai_text:
                problems.append(f'{name}: AI models page does not offer Install AI')

            # Record modal opens from Field when recording is off
            STATE['image_sample_enable'] = False
            page.click('#mainNav [data-view="field"]')
            page.wait_for_timeout(2300)
            page.click('#recordSwitch')
            page.wait_for_selector('#record-modal:not([hidden])', timeout=3000)
            shot(page, f'record_modal_{name}')
            check_screen(page, f'{name} record modal', problems)
            page.click('#modal-cancel')
            STATE['image_sample_enable'] = True

            if errors:
                problems.append(f'{name}: JS errors {errors}')
            page.close()

        # Phone width: one column, bottom tabs
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(BASE_URL + '/')
        page.wait_for_function("document.getElementById('stateText').textContent !== 'Connecting'")
        page.wait_for_timeout(400)
        shot(page, 'field_narrow')
        check_screen(page, 'narrow field', problems, kiosk=False)
        nav_box = page.locator('#mainNav').bounding_box()
        if not nav_box or nav_box['y'] + nav_box['height'] < 800:
            problems.append(f'narrow: tabs not at the bottom {nav_box}')
        page.click('#mainNav [data-view="settings"]')
        page.wait_for_timeout(200)
        shot(page, 'settings_narrow')
        check_screen(page, 'narrow settings list', problems, kiosk=False)
        page.click('.ou-side [data-panel="collection"]')
        page.wait_for_timeout(300)
        shot(page, 'settings_collection_narrow')
        check_screen(page, 'narrow data collection', problems, kiosk=False)
        if not page.is_visible('#panel-collection .ou-back'):
            problems.append('narrow: panel has no Back link')
        page.click('#panel-collection .ou-back')
        if not page.is_visible('.ou-side'):
            problems.append('narrow: Back does not return to the list')
        if errors:
            problems.append(f'narrow: JS errors {errors}')
        browser.close()

    shutil.rmtree(TMP, ignore_errors=True)
    if problems:
        print('FAIL')
        for p_ in problems:
            print(' -', p_)
        return 1
    print('PASS: standalone redesign at 1024x600, 1280x720, 1280x800 and 390x844')
    return 0


if __name__ == '__main__':
    sys.exit(run_checks())
