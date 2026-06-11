"""End-to-end browser test for the high-resolution warning modal.

Spins up the standalone Flask app with a mocked MQTT client that reports a
Pi 4 OWL running at 1456x1088 with allow_high_resolution=False. Drives a
headless Chromium through Playwright to verify:

  1. The page loads without JS errors.
  2. Clicking Start Recording triggers the high-res warning modal.
  3. The modal shows the correct Pi version + resolution.
  4. Clicking Cancel dismisses the modal without starting recording.
  5. Clicking Override hits /api/camera/set_allow_high_resolution.

Run: python tests/e2e/test_high_res_modal_browser.py
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'controller' / 'standalone'))

# Pick an unused port and bind early
HOST = '127.0.0.1'
PORT = 8765
BASE_URL = f'http://{HOST}:{PORT}'

SCREENSHOT_DIR = REPO / 'tests' / 'e2e' / 'screenshots'
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def build_app_with_mocks():
    """Import standalone and patch MQTT + service control so the page loads
    on Windows without owl.service or an MQTT broker."""
    # Silence Flask's request logger
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    # Avoid the real broker connection inside DashMQTTSubscriber.start()
    from utils import mqtt_manager as mm
    mm.DashMQTTSubscriber.start = lambda self: None

    import standalone as std

    dashboard = std.dashboard
    app = std.app

    fake_mqtt = MagicMock()
    fake_mqtt.get_state.return_value = {
        'detection_enable': False,
        'image_sample_enable': False,
        'sensitivity_level': 'medium',
        'stream_active': False,
        'owl_running': True,
        # The fields the new modal cares about: OWL is reporting that it
        # silently clamped a 1456x1088 config to 640x480 on this Pi 4.
        'resolution_width': 640,
        'resolution_height': 480,
        'requested_resolution_width': 1456,
        'requested_resolution_height': 1088,
        'resolution_clamped': True,
        'rpi_version': 'rpi-4',
        'allow_high_resolution': False,
        # Other heartbeat fields used by /api/system_stats
        'detection_mode': 1,
        'algorithm': 'exhsv',
        'model_available': False,
        'available_models': [],
        'current_model': '',
        'model_classes': {},
        'detect_classes': [],
        'exg_min': 25, 'exg_max': 200,
        'hue_min': 39, 'hue_max': 83,
        'saturation_min': 50, 'saturation_max': 220,
        'brightness_min': 60, 'brightness_max': 250,
        'min_detection_area': 10,
        'confidence': 0.5,
        'crop_buffer_px': 20,
        'algorithm_error': None,
        'tracking_enabled': False,
    }
    fake_mqtt.get_weed_detect_indicator.return_value = False
    fake_mqtt.get_image_write_indicator.return_value = False
    fake_mqtt.set_image_sample_enable.return_value = {'success': True}

    dashboard.mqtt_client = fake_mqtt

    # Track config writes so we can verify the override path
    persist_calls = []
    original_persist = getattr(dashboard, '_persist_config_change', None)
    def fake_persist(section, key, value):
        persist_calls.append((section, key, value))
    dashboard._persist_config_change = fake_persist

    # Patch the service control to be a no-op on Windows
    dashboard.control_owl_service = lambda action: (True, f'mock {action}')
    dashboard.get_owl_service_state = lambda: {'active': True, 'sub_state': 'running'}

    return app, dashboard, persist_calls


def serve_in_thread(app):
    """Start Flask in a daemon thread."""
    def _run():
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Wait for server to bind
    import urllib.request
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(BASE_URL, timeout=0.5)
            return t
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"Flask did not come up at {BASE_URL}")


def run_browser_test():
    from playwright.sync_api import sync_playwright

    console_errors = []
    js_errors = []
    api_calls = {'set_allow_high_resolution': 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = ctx.new_page()

        page.on('console', lambda msg: (
            console_errors.append(f'{msg.type}: {msg.text}')
            if msg.type in ('error', 'warning') else None
        ))
        page.on('pageerror', lambda exc: js_errors.append(str(exc)))

        # Capture the override POST so we can verify it fires
        def _on_request(req):
            if '/api/camera/set_allow_high_resolution' in req.url:
                api_calls['set_allow_high_resolution'] += 1
        page.on('request', _on_request)

        print(f'[1] Loading {BASE_URL} ...')
        page.goto(BASE_URL, wait_until='domcontentloaded', timeout=15000)

        # Wait for stats poll to populate cached resolution
        page.wait_for_function(
            "() => typeof lastResWidth !== 'undefined' && lastResWidth > 0",
            timeout=10000,
        )
        print(f'    actual={page.evaluate("lastResWidth")}x{page.evaluate("lastResHeight")}, '
              f'requested={page.evaluate("lastRequestedResWidth")}x{page.evaluate("lastRequestedResHeight")}, '
              f'clamped={page.evaluate("lastResolutionClamped")}, '
              f'rpi={page.evaluate("lastRpiVersion")}, '
              f'allow={page.evaluate("lastAllowHighResolution")}')

        # Verify the shared modal helpers are loaded
        assert page.evaluate("typeof showHighResWarningModal === 'function'"), \
            'showHighResWarningModal not loaded'
        assert page.evaluate("typeof isClampActive === 'function'"), \
            'isClampActive not loaded'

        # Pure-logic checks
        assert page.evaluate("isClampActive({resolution_clamped: true, allow_high_resolution: false})") is True
        assert page.evaluate("isClampActive({resolution_clamped: true, allow_high_resolution: true})") is False
        assert page.evaluate("isClampActive({resolution_clamped: false, allow_high_resolution: false})") is False
        print('[2] isClampActive logic verified in-browser')

        # Pre-screenshot of the dashboard
        page.screenshot(path=str(SCREENSHOT_DIR / '01_dashboard.png'), full_page=False)

        # Click Start Recording
        print('[3] Clicking Start Recording ...')
        page.locator('#recordSwitch').click()

        # Modal should appear
        modal = page.locator('.config-modal')
        modal.wait_for(state='visible', timeout=5000)
        page.screenshot(path=str(SCREENSHOT_DIR / '02_modal_open.png'))

        modal_text = modal.inner_text()
        print(f'    Modal text:\n      {modal_text.replace(chr(10), chr(10) + "      ")}')
        assert 'Pi 4' in modal_text, f'Modal should name Pi 4, got:\n{modal_text}'
        assert '1456x1088' in modal_text, f'Modal should show configured 1456x1088, got:\n{modal_text}'
        assert '640x480' in modal_text, f'Modal should show running 640x480, got:\n{modal_text}'

        # Test 1: Cancel button dismisses modal
        print('[4] Clicking Cancel ...')
        page.locator('.high-res-cancel').click()
        modal.wait_for(state='detached', timeout=3000)
        print('    Modal dismissed by Cancel')

        # Reopen by clicking Start Recording again
        print('[5] Clicking Start Recording again ...')
        page.locator('#recordSwitch').click()
        modal = page.locator('.config-modal')
        modal.wait_for(state='visible', timeout=5000)

        # Test 2: Override button hits the API
        print('[6] Clicking Override ...')
        page.screenshot(path=str(SCREENSHOT_DIR / '03_modal_before_override.png'))
        page.locator('.high-res-override').click()
        modal.wait_for(state='detached', timeout=5000)
        # Give the fetch a moment to land
        page.wait_for_timeout(800)
        page.screenshot(path=str(SCREENSHOT_DIR / '04_after_override.png'), full_page=False)

        browser.close()

    return {
        'console_errors': console_errors,
        'js_errors': js_errors,
        'api_calls': api_calls,
    }


def main():
    print('Building app with mocks ...')
    app, dashboard, persist_calls = build_app_with_mocks()

    print('Starting Flask ...')
    serve_in_thread(app)

    print('Running browser test ...')
    result = run_browser_test()

    print()
    print('=' * 60)
    print('RESULT')
    print('=' * 60)
    print(f'JS pageerrors:   {len(result["js_errors"])}')
    for err in result['js_errors']:
        print(f'  - {err}')
    real_console_errors = [e for e in result['console_errors'] if 'error' in e.split(':', 1)[0].lower()]
    print(f'Console errors:  {len(real_console_errors)}')
    for err in real_console_errors:
        print(f'  - {err}')
    print(f'API hits:        set_allow_high_resolution = {result["api_calls"]["set_allow_high_resolution"]}')
    print(f'Persist calls:   {persist_calls}')
    print(f'Screenshots in:  {SCREENSHOT_DIR}')

    ok = (
        len(result['js_errors']) == 0
        and result['api_calls']['set_allow_high_resolution'] == 1
        and ('Camera', 'allow_high_resolution', 'True') in persist_calls
    )
    print()
    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
