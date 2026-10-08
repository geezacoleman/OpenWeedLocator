#!/usr/bin/env python3
"""Bench probe: can this camera hold a short shutter while the gain stays automatic?

The data-collection shutter cap (utils/exposure_governor.py) fixes the
exposure time and relies on the AGC to raise the analogue gain. This probe
checks that the libcamera build on the unit actually behaves that way,
before the cap is trusted in the field.

Cases (each settles for a few seconds, then logs metadata for 2 s)
    auto            AE + AGC, no override (what OWL does today)
    cap_modes       ExposureTimeMode=Manual + ExposureTime=<cap>, AnalogueGainMode=Auto
                    (Trixie libcamera 0.5+)
    cap_legacy      ExposureTime=<cap> only (older libcamera: non-zero = manual)
    release         back to auto (ExposureTimeMode=Auto or ExposureTime=0)
    gain_clamp      cap + AnalogueGainMode=Manual / AnalogueGain=<max gain>

What to look for in probe.txt
    cap_*:   ExposureTime ~= cap AND AnalogueGain higher than in `auto`
             (in a dim room) -> the cap works with auto gain
    release: ExposureTime back to the `auto` value
    If ExposureTime ignores the cap, or AnalogueGain is stuck at 1.0 under
    the cap, send probe.txt to Guy before using the cap in the field.

Run on the OWL (stop owl.service first so the camera is free):

    sudo systemctl stop owl.service
    workon owl
    python tools/shutter_cap_probe.py                 # cap 500 us, max gain 8
    python tools/shutter_cap_probe.py --cap-us 250 --max-gain 4
    sudo systemctl start owl.service

Output lands in ~/shutter_cap_probe/<timestamp>/ (probe.txt + one JPEG per
case). Nothing in OWL's config is touched.
"""

import argparse
import datetime as _dt
import json
import os
import statistics
import sys
import time

SETTLE_SECONDS = 3.0
SAMPLE_SECONDS = 2.0


def _sample(cam, seconds):
    exposures, gains = [], []
    end = time.time() + seconds
    while time.time() < end:
        meta = cam.capture_metadata()
        if meta.get('ExposureTime'):
            exposures.append(meta['ExposureTime'])
        if meta.get('AnalogueGain'):
            gains.append(meta['AnalogueGain'])
    return {
        'exposure_us_median': int(statistics.median(exposures)) if exposures else None,
        'analogue_gain_median': round(statistics.median(gains), 2) if gains else None,
        'samples': len(exposures),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--cap-us', type=int, default=500, help='shutter cap in microseconds')
    parser.add_argument('--max-gain', type=float, default=8.0, help='gain clamp for the last case')
    parser.add_argument('--size', default='1456x1088')
    args = parser.parse_args()

    try:
        from picamera2 import Picamera2
        import libcamera
    except ImportError:
        sys.exit('picamera2 not available - run this on the OWL inside the owl virtualenv')

    width, height = (int(v) for v in args.size.lower().split('x'))
    out_dir = os.path.expanduser(
        os.path.join('~', 'shutter_cap_probe', _dt.datetime.now().strftime('%Y%m%d_%H%M%S')))
    os.makedirs(out_dir, exist_ok=True)

    cam = Picamera2()
    model = cam.camera_properties.get('Model')
    available = cam.camera_controls
    has_modes = 'ExposureTimeMode' in available
    cam.configure(cam.create_preview_configuration(main={'format': 'RGB888', 'size': (width, height)},
                                                   controls={'AeExposureMode': 1}))
    cam.start()

    cases = [('auto', {})]
    if has_modes:
        cases.append(('cap_modes', {
            'ExposureTimeMode': libcamera.controls.ExposureTimeModeEnum.Manual,
            'ExposureTime': args.cap_us,
            'AnalogueGainMode': libcamera.controls.AnalogueGainModeEnum.Auto}))
    cases.append(('cap_legacy', {'ExposureTime': args.cap_us}))
    if has_modes:
        cases.append(('release', {'ExposureTimeMode': libcamera.controls.ExposureTimeModeEnum.Auto,
                                  'AnalogueGainMode': libcamera.controls.AnalogueGainModeEnum.Auto}))
    else:
        cases.append(('release', {'ExposureTime': 0, 'AnalogueGain': 0}))
    clamp = {'ExposureTime': args.cap_us, 'AnalogueGain': args.max_gain}
    if has_modes:
        clamp.update({'ExposureTimeMode': libcamera.controls.ExposureTimeModeEnum.Manual,
                      'AnalogueGainMode': libcamera.controls.AnalogueGainModeEnum.Manual})
    cases.append(('gain_clamp', clamp))

    lines = [f'camera: {model}', f'has ExposureTimeMode: {has_modes}',
             f'cap_us: {args.cap_us}  max_gain: {args.max_gain}', '']
    for name, controls in cases:
        try:
            if controls:
                cam.set_controls(controls)
            time.sleep(SETTLE_SECONDS)
            result = _sample(cam, SAMPLE_SECONDS)
            cam.capture_file(os.path.join(out_dir, f'{name}.jpg'))
            error = None
        except Exception as e:  # report every case, even a refused control
            result, error = {}, str(e)
        line = f'{name:<11} ' + json.dumps(result) + (f'  ERROR: {error}' if error else '')
        print(line)
        lines.append(line)
    cam.stop()

    with open(os.path.join(out_dir, 'probe.txt'), 'w') as fh:
        fh.write('\n'.join(lines) + '\n')
    print(f'\nWrote {out_dir}/probe.txt')


if __name__ == '__main__':
    main()
