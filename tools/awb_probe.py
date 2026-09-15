#!/usr/bin/env python3
"""Bench probe: why does the camera look red through OWL but fine in rpicam?

Captures the SAME scene through picamera2 under several control sets and
writes one JPEG + one metadata line per case, so the images can be compared
side by side with an `rpicam-still -o ref.jpg` taken from the same spot.

Cases
    bare          picamera2 defaults (what rpicam-hello / rpicam-still use)
    owl_daylight  OWL's historical controls: Short AE, EV -2, AwbMode Daylight
    owl_auto      same, but AwbMode Auto
    owl_auto_ev0  AwbMode Auto only (no EV, no Short AE)
    swap          the `bare` frame written with red/blue channels swapped

Run on the OWL (stop owl.service first so the camera is free):

    sudo systemctl stop owl.service
    workon owl
    python tools/awb_probe.py            # default 416x320 like OWL
    python tools/awb_probe.py --size 1456x1088
    sudo systemctl start owl.service

Output lands in ~/awb_probe/<timestamp>/ with a probe.txt summary. Nothing
in OWL's config is touched.
"""

import argparse
import datetime as _dt
import json
import os
import platform
import subprocess
import sys
import time

SETTLE_SECONDS = 3.0   # AWB/AGC convergence before each capture


def _versions():
    info = {'python': sys.version.split()[0], 'machine': platform.machine()}
    for pkg in ('python3-picamera2', 'libcamera0.5', 'libcamera0.4', 'libcamera0.3',
                'rpicam-apps', 'rpicam-apps-lite', 'libcamera-apps'):
        try:
            out = subprocess.check_output(
                ['dpkg-query', '-W', '-f=${Version}', pkg],
                stderr=subprocess.DEVNULL, text=True).strip()
            if out:
                info[pkg] = out
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    try:
        with open('/proc/device-tree/model') as fh:
            info['board'] = fh.read().strip('\x00')
    except OSError:
        pass
    return info


def _tuning_files(model):
    found = []
    for plat in ('pisp', 'vc4'):
        for base in ('/usr/share/libcamera/ipa/rpi', '/usr/local/share/libcamera/ipa/rpi'):
            path = os.path.join(base, plat, f'{model}.json')
            if os.path.isfile(path):
                found.append(path)
    return found


def _fmt(md, key, digits=3):
    val = md.get(key)
    if val is None:
        return '-'
    if isinstance(val, (tuple, list)):
        return '(' + ', '.join(f'{v:.{digits}f}' for v in val) + ')'
    if isinstance(val, float):
        return f'{val:.{digits}f}'
    return str(val)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--size', default='416x320',
                        help='main stream size WxH (default 416x320, OWL default)')
    parser.add_argument('--out', default=os.path.expanduser('~/awb_probe'),
                        help='output root directory')
    parser.add_argument('--settle', type=float, default=SETTLE_SECONDS,
                        help='seconds to let AWB/AGC settle before capture')
    args = parser.parse_args()

    try:
        import cv2
        import numpy as np
        from picamera2 import Picamera2
        from libcamera import controls as lc
    except ImportError as e:
        print(f'[ERROR] {e}. Run inside the owl virtualenv on the Pi (workon owl).')
        return 1

    width, height = (int(v) for v in args.size.lower().split('x'))
    stamp = _dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(args.out, stamp)
    os.makedirs(out_dir, exist_ok=True)

    cases = [
        ('bare', {}),
        ('owl_daylight', {'AeExposureMode': lc.AeExposureModeEnum.Short,
                          'ExposureValue': -2,
                          'AwbEnable': True,
                          'AwbMode': lc.AwbModeEnum.Daylight}),
        ('owl_auto', {'AeExposureMode': lc.AeExposureModeEnum.Short,
                      'ExposureValue': -2,
                      'AwbEnable': True,
                      'AwbMode': lc.AwbModeEnum.Auto}),
        ('owl_auto_ev0', {'AwbEnable': True,
                          'AwbMode': lc.AwbModeEnum.Auto}),
    ]

    lines = []
    header = (f"{'case':<14}{'ColourGains':<18}{'CT(K)':<8}{'Lux':<10}"
              f"{'Exposure(us)':<14}{'AGain':<8}{'DGain':<8}")
    lines.append(header)
    lines.append('-' * len(header))

    picam = Picamera2()
    model = picam.camera_properties.get('Model', 'unknown')
    platform_name = str(getattr(Picamera2, 'platform', 'unknown'))
    bare_frame = None
    try:
        for name, ctrls in cases:
            config = picam.create_preview_configuration(
                main={'format': 'RGB888', 'size': (width, height)},
                queue=False, controls=ctrls)
            picam.configure(config)
            picam.start()
            time.sleep(args.settle)
            request = picam.capture_request()
            try:
                frame = request.make_array('main')
                md = request.get_metadata()
            finally:
                request.release()
            picam.stop()

            jpg = os.path.join(out_dir, f'{name}.jpg')
            cv2.imwrite(jpg, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if name == 'bare':
                bare_frame = frame
            lines.append(f"{name:<14}{_fmt(md, 'ColourGains'):<18}"
                         f"{_fmt(md, 'ColourTemperature', 0):<8}{_fmt(md, 'Lux', 1):<10}"
                         f"{_fmt(md, 'ExposureTime', 0):<14}{_fmt(md, 'AnalogueGain', 2):<8}"
                         f"{_fmt(md, 'DigitalGain', 2):<8}")
            with open(os.path.join(out_dir, f'{name}.json'), 'w') as fh:
                json.dump({k: (list(v) if isinstance(v, tuple) else v)
                           for k, v in md.items()
                           if isinstance(v, (int, float, str, tuple, list, bool))},
                          fh, indent=1, default=str)
            print(f'[OK] {name}: {jpg}')
    finally:
        try:
            picam.close()
        except Exception:
            pass

    if bare_frame is not None:
        swapped = bare_frame[:, :, ::-1].copy()
        cv2.imwrite(os.path.join(out_dir, 'swap.jpg'), swapped,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        lines.append(f"{'swap':<14}(bare frame, R and B channels swapped)")
        print(f"[OK] swap: {os.path.join(out_dir, 'swap.jpg')}")

    summary = [
        f'awb_probe {stamp}',
        f'sensor model: {model}   picamera2 platform: {platform_name}   size: {width}x{height}',
        'tuning files on disk: ' + (', '.join(_tuning_files(model)) or 'none found'),
        'versions: ' + json.dumps(_versions()),
        '',
    ] + lines + [
        '',
        'Next: rpicam-still -o ' + os.path.join(out_dir, 'rpicam_ref.jpg') +
        '  (same spot, same light), then compare the JPEGs.',
        'Reading the table: if owl_daylight is red but owl_auto matches bare, the',
        'daylight preset is the cause. If owl_auto is ALSO off versus bare, the',
        'module sits off the Pi GS tuning curve and needs a tuning file. If swap.jpg',
        'looks right and bare.jpg wrong, the channel order is wrong (unlikely).',
    ]
    text = '\n'.join(summary)
    with open(os.path.join(out_dir, 'probe.txt'), 'w') as fh:
        fh.write(text + '\n')
    print()
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
