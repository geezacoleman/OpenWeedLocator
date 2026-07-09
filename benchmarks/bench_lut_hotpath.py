#!/usr/bin/env python
"""
Benchmark: painted-LUT hot path vs exhsv, on real hardware.

Times every stage the LUT feature touches (apply_lut, morphology,
contour extraction) alongside the exhsv baseline, plus candidate
optimisations, so LUT-vs-manual cost and any FPS regressions are
measured rather than guessed.

Run on the Pi:
    python benchmarks/bench_lut_hotpath.py
    python benchmarks/bench_lut_hotpath.py --image-size 1456x1088
    python benchmarks/bench_lut_hotpath.py --image path/to/field_frame.jpg
    python benchmarks/bench_lut_hotpath.py --profile my_paddock   # real profile

Defaults match the field pipeline: 1456x1088 capture with a 10%
left/right crop (strided view), starter LUT profile at sensitivity 50.
"""

import argparse
import os
import sys
import time

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from utils.lut_manager import (
    LUT_SHIFT,
    LUTProfileManager,
    apply_lut,
    bake_from_model,
    generate_starter_pixels,
    train_model,
)
from utils.greenonbrown import GreenOnBrown


def make_field_image(h, w):
    """Synthetic field image: green plants on brown soil with noise."""
    rng = np.random.RandomState(42)
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:, :, 0] = rng.randint(25, 55, (h, w))    # B
    img[:, :, 1] = rng.randint(60, 100, (h, w))   # G
    img[:, :, 2] = rng.randint(85, 135, (h, w))   # R
    for _ in range(40):
        cx, cy = rng.randint(30, w - 30), rng.randint(30, h - 30)
        cv2.circle(img, (cx, cy), rng.randint(8, 50), (35, 170, 45), -1)
    return img


def timeit(func, rounds=100, warmup=10, label=''):
    """Time a function, print and return median ms."""
    for _ in range(warmup):
        func()
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        func()
        times.append((time.perf_counter() - t0) * 1000)
    med = float(np.median(times))
    print(f'  {label:52s}  median={med:7.2f}ms  mean={np.mean(times):7.2f}ms  '
          f'min={min(times):7.2f}ms')
    return med


# ------------------------------------------------------------------
# apply_lut variants
# ------------------------------------------------------------------

def apply_lut_numpy_index(image, lut, idx_buf):
    """The pre-3.4.0 numpy shift/or index build, kept for comparison
    against the shipped cv2.LUT-based implementation."""
    np.right_shift(image[:, :, 2], LUT_SHIFT, out=idx_buf, casting='unsafe')
    idx_buf <<= 5
    idx_buf |= image[:, :, 1] >> LUT_SHIFT
    idx_buf <<= 5
    idx_buf |= image[:, :, 0] >> LUT_SHIFT
    return lut.take(idx_buf)


def main():
    parser = argparse.ArgumentParser(description='Painted-LUT hot-path benchmark')
    parser.add_argument('--rounds', type=int, default=100)
    parser.add_argument('--warmup', type=int, default=10)
    parser.add_argument('--image-size', type=str, default='1456x1088',
                        help='Capture WxH before crop (default: 1456x1088)')
    parser.add_argument('--image', type=str, default='',
                        help='Real field image to use instead of synthetic')
    parser.add_argument('--crop', type=float, default=0.10,
                        help='Left/right crop fraction, as in geometry (default 0.10)')
    parser.add_argument('--profile', type=str, default='',
                        help='Name of a real LUT profile in config/lut_profiles '
                             '(default: synthesised starter pixels)')
    parser.add_argument('--sensitivity', type=int, default=50)
    args = parser.parse_args()

    w, h = map(int, args.image_size.split('x'))
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            sys.exit(f'Could not read {args.image}')
        frame = cv2.resize(frame, (w, h))
        print(f'Frame: {args.image} resized to {w}x{h}')
    else:
        frame = make_field_image(h, w)
        print(f'Frame: synthetic {w}x{h}')

    # Crop exactly like owl.py: a strided view into the capture buffer
    crop_px = int(w * args.crop)
    crop_view = frame[:, crop_px:w - crop_px]
    ch, cw = crop_view.shape[:2]
    contig = np.ascontiguousarray(crop_view)
    print(f'Cropped detection frame: {cw}x{ch} '
          f'({cw * ch:,} px, strided view as in owl.py)')

    if args.profile:
        mgr = LUTProfileManager(os.path.join(PROJECT_ROOT, 'config', 'lut_profiles'))
        lut = mgr.load_and_bake(args.profile, args.sensitivity)
        prof = mgr.load(args.profile)
        model = {k: prof[k] for k in ('log_ratio', 'fg_gate', 'score_thresholds')}
        fg, bg = prof['fg_pixels'], prof['bg_pixels']
        print(f'LUT profile: {args.profile} @ sensitivity {args.sensitivity}')
    else:
        fg, bg = generate_starter_pixels()
        model = train_model(fg, bg)
        lut = bake_from_model(model, args.sensitivity)
        print(f'LUT profile: synthesised starter @ sensitivity {args.sensitivity}')

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    kernel5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    idx_buf = np.empty((ch, cw), np.uint16)
    rounds, warmup = args.rounds, args.warmup

    print()
    print('=== 1. End-to-end GreenOnBrown.inference (what owl.py calls) ===')
    gob = GreenOnBrown(algorithm='lut', lut_table=lut)
    t_lut_e2e = timeit(lambda: gob.inference(crop_view, algorithm='lut',
                                             show_display=False),
                       rounds, warmup, 'lut, show_display=False')
    timeit(lambda: gob.inference(crop_view, algorithm='lut', show_display=True),
           rounds, warmup, 'lut, show_display=True (dash connected)')
    t_ex_e2e = timeit(lambda: gob.inference(crop_view, algorithm='exhsv',
                                            show_display=False),
                      rounds, warmup, 'exhsv, show_display=False')
    timeit(lambda: gob.inference(crop_view, algorithm='exhsv', show_display=True),
           rounds, warmup, 'exhsv, show_display=True (dash connected)')

    print()
    print('=== 2. apply_lut variants ===')
    t_strided = timeit(lambda: apply_lut(crop_view, lut, idx_buf),
                       rounds, warmup, 'CURRENT (cv2.LUT index): strided view')
    t_contig = timeit(lambda: apply_lut(contig, lut, idx_buf),
                      rounds, warmup, 'contiguous input')
    t_copy_first = timeit(lambda: apply_lut(np.ascontiguousarray(crop_view), lut, idx_buf),
                          rounds, warmup, 'ascontiguousarray copy + apply')
    t_npidx = timeit(lambda: apply_lut_numpy_index(contig, lut, idx_buf),
                     rounds, warmup, 'pre-3.4.0 numpy shift/or index')

    def half_res():
        small = cv2.resize(crop_view, (cw // 2, ch // 2),
                           interpolation=cv2.INTER_NEAREST)
        return apply_lut(small, lut)
    t_half = timeit(half_res, rounds, warmup, 'half-res (resize NEAREST + apply)')

    # Correctness: variants must produce identical masks
    ref = apply_lut(contig, lut, idx_buf).copy()
    assert np.array_equal(ref, apply_lut(crop_view, lut))
    assert np.array_equal(ref, apply_lut_numpy_index(contig, lut, idx_buf))
    print('  correctness: all full-res variants produce identical masks')

    print()
    print('=== 3. Mask post-processing (pre-thresholded path) ===')
    mask = ref
    t_m5 = timeit(lambda: cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=5),
                  rounds, warmup, 'CURRENT: close 3x3 x5')
    timeit(lambda: cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2),
           rounds, warmup, 'close 3x3 x2')
    t_m1 = timeit(lambda: cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1),
                  rounds, warmup, 'close 3x3 x1')
    timeit(lambda: cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel5, iterations=1),
           rounds, warmup, 'close 5x5 x1')
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=5)
    t_cont = timeit(lambda: cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                             cv2.CHAIN_APPROX_SIMPLE),
                    rounds, warmup, 'findContours')
    timeit(lambda: cv2.connectedComponentsWithStats(closed, 8, cv2.CV_32S),
           rounds, warmup, 'connectedComponentsWithStats')

    print()
    print('=== 4. Bake / load / fit costs (not per-frame, sanity only) ===')
    if args.profile:
        timeit(lambda: mgr.load_and_bake(args.profile, args.sensitivity),
               20, 3, 'load_and_bake (profile switch)')
    timeit(lambda: bake_from_model(model, args.sensitivity),
           20, 3, 'bake_from_model (sensitivity re-bake)')
    timeit(lambda: train_model(fg, bg),
           5, 1, 'train_model (save / v1-migration fit)')

    print()
    print('=== 5. Loop overheads outside the detector ===')
    timeit(lambda: contig.copy(), rounds, warmup,
           'frame.copy() (image_out, every frame w/ dash)')
    try:
        timeit(lambda: cv2.waitKey(1), rounds, warmup,
               'cv2.waitKey(1) (runs every frame, headless)')
    except cv2.error:
        print('  cv2.waitKey(1): not available (headless build)')

    print()
    print('=' * 70)
    print('SUMMARY')
    print('=' * 70)
    print(f'  End-to-end lut:   {t_lut_e2e:7.2f}ms   exhsv: {t_ex_e2e:7.2f}ms   '
          f'(lut is {t_ex_e2e - t_lut_e2e:+.2f}ms vs exhsv)')
    print(f'  apply_lut:        {t_strided:7.2f}ms shipped vs {t_npidx:7.2f}ms '
          f'numpy index ({t_npidx - t_strided:+.2f}ms saved by cv2.LUT build)')
    print(f'  half-res apply:   {t_half:7.2f}ms ({t_strided - t_half:+.2f}ms; '
          f'boxes/areas need x2/x4 rescale)')
    print(f'  morphology:       {t_m5:7.2f}ms (x5) -> {t_m1:7.2f}ms (x1) '
          f'({t_m5 - t_m1:+.2f}ms if x1 is visually acceptable)')
    print(f'  contours:         {t_cont:7.2f}ms')
    est = t_lut_e2e
    print(f'  Implied max detection FPS (lut, this frame size): {1000.0 / est:5.1f}')
    print('=' * 70)


if __name__ == '__main__':
    main()
