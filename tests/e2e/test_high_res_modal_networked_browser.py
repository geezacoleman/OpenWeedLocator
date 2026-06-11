"""End-to-end browser test for the networked controller's high-res warning flow.

Spins up the networked Flask app with a stub `owls_state` containing a Pi 4
OWL at 1456x1088 with allow_high_resolution=False, then drives Playwright
through Start Recording to verify:

  1. The networked page loads without JS errors.
  2. `_findHighResOWL()` returns the fake OWL.
  3. Clicking Start Recording opens the high-res warning modal.

Run: python tests/e2e/test_high_res_modal_networked_browser.py
"""

import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'controller' / 'networked'))

HOST = '127.0.0.1'
PORT = 8766
BASE_URL = f'http://{HOST}:{PORT}'

SCREENSHOT_DIR = REPO / 'tests' / 'e2e' / 'screenshots'
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def build_app_with_mocks():
    import logging
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

    import networked as nw

    ctrl = nw.controller
    fake_state = {
        'connected': True,
        'device_id': 'owl-pi4-test',
        'last_seen': time.time(),
        'detection_enable': False,
        'image_sample_enable': False,
        'owl_running': True,
        # Pi 4 with a 1456x1088 config that the OWL clamped to 640x480
        'resolution_width': 640,
        'resolution_height': 480,
        'requested_resolution_width': 1456,
        'requested_resolution_height': 1088,
        'resolution_clamped': True,
        'rpi_version': 'rpi-4',
        'allow_high_resolution': False,
        'sensitivity_level': 'medium',
        'algorithm': 'exhsv',
        'model_available': False,
        'current_model': '',
        'cpu_percent': 25,
        'memory_percent': 40,
        'detection_mode': 1,
        'avg_loop_time_ms': 12.5,
    }
    ctrl.owls_state = {'owl-pi4-test': fake_state}
    ctrl.mqtt_connected = True
    ctrl.send_command = lambda *a, **kw: {'success': True}

    # Keep last_seen fresh so the 8s TTL in get_recent_owls() never purges
    # our fake OWL during the test.
    def _heartbeat():
        while True:
            fake_state['last_seen'] = time.time()
            time.sleep(1.0)
    threading.Thread(target=_heartbeat, daemon=True).start()

    return nw.app


def serve_in_thread(app):
    def _run():
        app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    t = threading.Thread(target=_run, daemon=True)
    t.start()

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
    mqtt_set_config_seen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = ctx.new_page()

        page.on('console', lambda msg: (
            console_errors.append(f'{msg.type}: {msg.text}')
            if msg.type in ('error', 'warning') else None
        ))
        page.on('pageerror', lambda exc: js_errors.append(str(exc)))

        def _on_request(req):
            if '/api/send_command' in req.url:
                try:
                    body = req.post_data or ''
                    if 'set_config_section' in body and 'allow_high_resolution' in body:
                        mqtt_set_config_seen.append(body)
                except Exception:
                    pass
        page.on('request', _on_request)

        print(f'[1] Loading {BASE_URL} ...')
        page.goto(BASE_URL, wait_until='domcontentloaded', timeout=15000)

        # Wait for owlsData to populate from /api/owls
        page.wait_for_function(
            "() => owlsData && owlsData['owl-pi4-test'] && owlsData['owl-pi4-test'].connected",
            timeout=10000,
        )
        print('    owlsData populated from /api/owls')

        # Verify the helper functions exist
        assert page.evaluate("typeof showHighResWarningModal === 'function'"), \
            'showHighResWarningModal not loaded'
        assert page.evaluate("typeof isClampActive === 'function'"), \
            'isClampActive not loaded'
        assert page.evaluate("typeof _findClampedOWLs === 'function'"), \
            '_findClampedOWLs not loaded'

        # Verify the helper finds the Pi 4 OWL
        found = page.evaluate("_findClampedOWLs()")
        assert isinstance(found, list) and len(found) == 1, \
            f'_findClampedOWLs should return exactly one entry, got {found}'
        owl = found[0]
        assert owl['rpi'] == 'rpi-4', f'Expected rpi-4, got {owl}'
        assert owl['requestedW'] == 1456 and owl['requestedH'] == 1088, f'Unexpected requested: {owl}'
        assert owl['actualW'] == 640 and owl['actualH'] == 480, f'Unexpected actual: {owl}'
        assert owl['deviceId'] == 'owl-pi4-test', f'Unexpected deviceId: {owl}'
        print(f'[2] _findClampedOWLs returned: {owl}')

        # Click Start Recording
        rec_btn = page.locator('#main-recording-btn')
        rec_btn.wait_for(state='visible', timeout=5000)
        print('[3] Clicking Start Recording ...')
        rec_btn.click()

        # Modal should appear
        modal = page.locator('.config-modal')
        modal.wait_for(state='visible', timeout=5000)
        modal_text = modal.inner_text()
        print(f'    Modal text snippet: {modal_text.splitlines()[0] if modal_text else "<empty>"}')
        assert 'Pi 4' in modal_text, f'Modal should name Pi 4:\n{modal_text}'
        assert '1456x1088' in modal_text, f'Modal should show configured 1456x1088:\n{modal_text}'
        assert '640x480' in modal_text, f'Modal should show running 640x480:\n{modal_text}'

        page.screenshot(path=str(SCREENSHOT_DIR / 'networked_01_modal_open.png'))

        # Cancel and verify no recording state change
        page.locator('.high-res-cancel').click()
        modal.wait_for(state='detached', timeout=3000)
        rec_active = page.evaluate("document.getElementById('main-recording-btn').classList.contains('active')")
        assert not rec_active, 'Recording should NOT be active after cancel'
        print('[4] Cancel verified — recording not started')

        # Verify the Pi-version badge mechanism: inject a fake context node
        # (the same class the config editor would create on render) and
        # confirm setHighResContextBadge updates it. The full config-editor
        # round trip requires a live MQTT OWL response so we can't easily
        # exercise the auto-render path here, but the updater itself is
        # what we own.
        page.evaluate("""() => {
            var n = document.createElement('span');
            n.className = 'js-pi-version-context';
            n.id = 'test-badge';
            document.body.appendChild(n);
            setHighResContextBadge(typeof lastRpiVersion === 'undefined' ?
                (owlsData[Object.keys(owlsData)[0]]||{}).rpi_version : lastRpiVersion);
        }""")
        badge_text = page.evaluate("document.getElementById('test-badge').textContent")
        assert 'Pi 4' in badge_text, f'Expected Pi 4 badge, got: {badge_text!r}'
        print(f'[5] Pi-version badge updater works: {badge_text!r}')

        browser.close()

    return {
        'console_errors': console_errors,
        'js_errors': js_errors,
        'mqtt_set_config_seen': mqtt_set_config_seen,
    }


def main():
    print('Building networked app with mocks ...')
    app = build_app_with_mocks()

    print('Starting Flask ...')
    serve_in_thread(app)

    print('Running browser test ...')
    result = run_browser_test()

    print()
    print('=' * 60)
    print('RESULT')
    print('=' * 60)
    print(f'JS pageerrors: {len(result["js_errors"])}')
    for err in result['js_errors']:
        print(f'  - {err}')
    real_console_errors = [e for e in result['console_errors'] if 'error' in e.split(':', 1)[0].lower()]
    print(f'Console errors: {len(real_console_errors)}')
    for err in real_console_errors[:10]:
        print(f'  - {err}')
    print(f'Screenshots in: {SCREENSHOT_DIR}')

    ok = len(result['js_errors']) == 0
    print()
    print('PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
