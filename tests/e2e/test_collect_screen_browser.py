"""End-to-end browser test for the data-collection screen (templates/collect.html).

Runs the networked Flask app with two fake recording OWLs (capture state,
quality summary, thumbnails, map points, sessions) and drives Playwright at
both kiosk sizes:

  - 7" EDATEC 1024x600 and 10" HMI3010 1280x800: no page scroll, record
    button, status rows, warnings and four gallery images visible
  - Start recording opens the record form (design B, 2026-10-16): a list
    of five sections, one section's choices beside it; no section scrolls
    on its own except the crop grid; + Add crop saves and selects the
    crop; artificial light greys out sky and weather; Start posts the
    chosen metadata
  - Pause / Resume beside the record button sends set_recording_paused
    and shows Paused
  - DK switches the labels
  - Settings, Sessions: photo and video counts, the upload queue states
    (Waiting n of m), ticking a session and Upload posts the request

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
                           'image_count': count, 'total_size': 180 * 1048576},
                          {'session_id': f'20261008/s091500_{device_id}_a3f9', 'date': '20261008', 'time': '091500',
                           'image_count': 0, 'video_count': 1, 'total_size': 146 * 1048576}],
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

    def write_collect(values):
        if not ctrl.config.has_section('Collect'):
            ctrl.config.add_section('Collect')
        for key, value in values.items():
            ctrl.config.set('Collect', key, str(value))
    nw._write_collect_section = write_collect

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

            # Turn off: in the bar, on screen, asks first, then stops the run
            power = page.locator('#power-btn').bounding_box()
            if not power or power['x'] + power['width'] > width or power['height'] < 44:
                problems.append(f'{name}: Turn off button not fully in the bar ({power})')
            shutdowns = []
            page.route('**/api/system/shutdown', lambda route: (
                shutdowns.append(1), route.fulfill(json={'success': True})))
            page.click('#power-btn')
            page.wait_for_selector('#power-modal:not([hidden])')
            if page.locator('#power-recording').is_hidden():
                problems.append(f'{name}: Turn off does not say recording will stop')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_power_{name}.png'))
            page.click('#power-no')
            if shutdowns:
                problems.append(f'{name}: Cancel still shut down')
            page.click('#power-btn')
            page.click('#power-yes')
            page.wait_for_selector('#power-done:not([hidden])')
            if not shutdowns or not [a for d, a, v in SENT if a == 'toggle_recording' and v is False]:
                problems.append(f'{name}: Turn off did not stop recording then shut down')
            page.reload()
            page.wait_for_selector('.c-owl-row')

            # Paused below the minimum speed: one plain line
            ctrl_paused = __import__('networked').controller
            for s in ctrl_paused.owls_state.values():
                s['capture'].update(paused_slow=True, min_speed_kmh=1.0, speed_kmh=0.4)
            page.wait_for_function("document.getElementById('warnings').innerText.includes('Paused below 1')",
                                   timeout=5000)
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_paused_{name}.png'))
            for s in ctrl_paused.owls_state.values():
                s['capture'].update(paused_slow=False, speed_kmh=9.8)

            # Pause at the headland, then resume
            page.click('#pause-btn')
            page.wait_for_timeout(300)
            if ('all', 'set_recording_paused', True) not in SENT:
                problems.append(f'{name}: Pause did not send set_recording_paused')
            for s in ctrl_paused.owls_state.values():
                s['capture']['paused'] = True
            page.wait_for_function("document.getElementById('pause-btn').textContent === 'Resume'", timeout=5000)
            if 'Paused' not in page.inner_text('#record-sub') or 'Nothing is saved' not in page.inner_text('#warnings'):
                problems.append(f'{name}: paused run does not say so')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_headland_{name}.png'))
            page.click('#pause-btn')
            page.wait_for_timeout(300)
            if ('all', 'set_recording_paused', False) not in SENT:
                problems.append(f'{name}: Resume did not send set_recording_paused false')
            for s in ctrl_paused.owls_state.values():
                s['capture']['paused'] = False

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
            for section in ('crop', 'light', 'soil', 'purpose', 'notes'):
                page.click(f'.c-rf-row[data-section="{section}"]')
                over = page.evaluate("(() => { const p = document.getElementById('rf-pane');"
                                     " return p.scrollHeight - p.clientHeight; })()")
                if over > 1:
                    problems.append(f'{name}: {section} section scrolls by {over}px')
                small = page.evaluate("[...document.querySelectorAll('#record-modal button')]"
                                      ".filter(b => b.offsetParent && b.getBoundingClientRect().height < 44).length")
                if small:
                    problems.append(f'{name}: {small} buttons under 44 px in the {section} section')
                page.screenshot(path=str(SCREENSHOT_DIR / f'collect_form_{section}_{name}.png'))

            # + Add crop: saved on the controller and chosen straight away
            page.click('.c-rf-row[data-section="crop"]')
            page.evaluate("(() => { const i = document.getElementById('meta-add-crop'); i.value = 'Pak choi';"
                          " i.dispatchEvent(new Event('change')); })()")
            page.wait_for_selector('[data-pick="crop"][data-value="pak_choi"][aria-pressed="true"]', timeout=5000)
            if 'Pak choi' not in page.inner_text('.c-rf-row[data-section="crop"]'):
                problems.append(f'{name}: added crop not shown as the choice')

            page.click('.c-rf-tabs [data-tab="arable"]')
            page.click('[data-pick="crop"][data-value="barley"]')
            page.click('[data-pick="crop_season"][data-value="spring"]')
            page.click('.c-rf-row[data-section="light"]')
            page.click('[data-pick="light_source"][data-value="artificial"]')
            if not page.locator('[data-pick="sky"][data-value="sunny"]').is_disabled():
                problems.append(f'{name}: sky still asked under artificial light')
            page.click('.c-rf-row[data-section="soil"]')
            page.click('[data-pick="soil_cover"][data-value="none"]')
            page.click('[data-pick="soil_moisture"][data-value="wet"]')
            page.click('[data-pick="recent_water"][data-value="irrigation"]')
            if 'Bare soil, wet after irrigation' not in page.inner_text('.c-rf-row[data-section="soil"]'):
                problems.append(f'{name}: soil summary wrong: {page.inner_text(".c-rf-row[data-section=soil]")}')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_form_filled_{name}.png'))
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
            rows_text = page.inner_text('#session-rows')
            if '1 video' not in rows_text or '412 photos' not in rows_text:
                problems.append(f'{name}: session counts wrong: {rows_text[:200]}')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_sessions_{name}.png'))
            page.locator('.c-tick').first.click()
            page.click('#upload-btn')
            page.wait_for_timeout(300)
            # Noktura's answer: waiting its turn in the rig's queue
            reqs = __import__('networked').controller.collect.upload_list()
            if reqs:
                __import__('networked').controller.collect.ack_upload(
                    reqs[0]['id'], 'queued', details={'position': 2, 'queue_length': 5})
                page.wait_for_function("document.getElementById('session-rows').innerText.includes('Waiting (2 of 5)')",
                                       timeout=5000)
                page.screenshot(path=str(SCREENSHOT_DIR / f'collect_queue_{name}.png'))

            page.click('.c-setnav [data-pane="capture"]')
            if not page.locator('#set-min-speed').is_visible():
                problems.append(f'{name}: pause speed setting missing from Capture')
            page.screenshot(path=str(SCREENSHOT_DIR / f'collect_capture_{name}.png'))
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
    want = {'crop': 'barley', 'crop_season': 'spring', 'light_source': 'artificial', 'sky': '',
            'weather': '', 'soil_cover': 'none', 'soil_moisture': 'wet', 'recent_water': 'irrigation',
            'lighting': 'artificial'}
    if not starts or any(starts[-1].get(k) != v for k, v in want.items()):
        problems.append(f'metadata not sent as chosen: {starts[-1:]}')
    if not nw.controller.collect.upload_list():
        problems.append('upload request not created')
    return problems


if __name__ == '__main__':
    found = run_checks()
    print('\n'.join(found) if found else 'collect screen: all checks passed')
    sys.exit(1 if found else 0)
