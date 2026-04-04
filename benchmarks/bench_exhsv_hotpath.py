#!/usr/bin/env python
"""
Benchmark: ExHSV hot-path micro-optimizations.

Profiles each stage of the exg_standardised_hue + GreenOnBrown.inference
pipeline and tests optimized alternatives.

Usage:
    python benchmarks/bench_exhsv_hotpath.py
    python benchmarks/bench_exhsv_hotpath.py --image-size 416x320
"""

import argparse
import os
import sys
import time

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def make_field_image(h=720, w=1280):
    """Synthetic field image with green plants on brown soil."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (40, 80, 120)  # Brown soil (BGR)
    rng = np.random.RandomState(42)
    for _ in range(30):
        cx, cy = rng.randint(50, w - 50), rng.randint(50, h - 50)
        radius = rng.randint(20, 60)
        cv2.circle(img, (cx, cy), radius, (30, 180, 30), -1)
    return img


def timeit(func, rounds=100, warmup=10, label=''):
    """Time a function, return median ms."""
    for _ in range(warmup):
        func()
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        func()
        times.append((time.perf_counter() - t0) * 1000)
    med = np.median(times)
    mean = np.mean(times)
    mn = min(times)
    print(f'  {label:45s}  median={med:6.2f}ms  mean={mean:6.2f}ms  min={mn:6.2f}ms')
    return med


# ============================================================
# CURRENT implementations (copied for fair comparison)
# ============================================================

def exhsv_current(image, hue_min=30, hue_max=90, brightness_min=5, brightness_max=200,
                  saturation_min=30, saturation_max=255, invert_hue=False):
    """Current exg_standardised_hue from algorithms.py."""
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    channel_sum = red + green + blue
    channel_sum[channel_sum == 0] = 1  # boolean index

    image_out = 255 * (2 * green / channel_sum - red / channel_sum - blue / channel_sum)
    np.clip(image_out, 0, 255, out=image_out)
    image_out = image_out.astype('uint8')

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
    upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
    hsv_thresh = cv2.inRange(hsv_image, lower, upper)

    image_out = hsv_thresh & image_out
    return image_out


def gob_inference_current(image, output, kernel, exg_min=30, exg_max=250, min_detection_area=1):
    """Current GreenOnBrown.inference post-processing."""
    output = np.clip(output, exg_min, exg_max)
    output = np.uint8(np.abs(output))
    threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                           cv2.THRESH_BINARY_INV, 31, 2)
    threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(threshold_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    weed_centres = []
    for c in contours:
        if cv2.contourArea(c) > min_detection_area:
            x, y, w, h = cv2.boundingRect(c)
            boxes.append([x, y, w, h])
            weed_centres.append([x + w // 2, y + h // 2])
    return boxes, weed_centres


# ============================================================
# OPTIMIZED implementations
# ============================================================

def exhsv_opt1_single_div(image, hue_min=30, hue_max=90, brightness_min=5, brightness_max=200,
                           saturation_min=30, saturation_max=255, invert_hue=False):
    """Optimization 1: Combine 3 divisions into 1."""
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    channel_sum = red + green + blue
    channel_sum[channel_sum == 0] = 1

    # ONE division instead of three: (2g - r - b) / sum
    image_out = 255.0 * (2.0 * green - red - blue) / channel_sum
    np.clip(image_out, 0, 255, out=image_out)
    image_out = image_out.astype('uint8')

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
    upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
    hsv_thresh = cv2.inRange(hsv_image, lower, upper)

    image_out = hsv_thresh & image_out
    return image_out


def exhsv_opt2_maximum(image, hue_min=30, hue_max=90, brightness_min=5, brightness_max=200,
                        saturation_min=30, saturation_max=255, invert_hue=False):
    """Optimization 2: np.maximum instead of boolean indexing for zero-guard."""
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    channel_sum = red + green + blue
    np.maximum(channel_sum, 1.0, out=channel_sum)  # branchless, no boolean mask

    image_out = 255.0 * (2.0 * green - red - blue) / channel_sum
    np.clip(image_out, 0, 255, out=image_out)
    image_out = image_out.astype('uint8')

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
    upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
    hsv_thresh = cv2.inRange(hsv_image, lower, upper)

    image_out = hsv_thresh & image_out
    return image_out


def exhsv_opt3_inplace(image, hue_min=30, hue_max=90, brightness_min=5, brightness_max=200,
                        saturation_min=30, saturation_max=255, invert_hue=False):
    """Optimization 3: Reduce allocations with in-place ops."""
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    channel_sum = red + green + blue
    np.maximum(channel_sum, 1.0, out=channel_sum)

    # In-place: reuse green buffer for numerator
    np.multiply(green, 2.0, out=green)
    np.subtract(green, red, out=green)
    np.subtract(green, blue, out=green)  # green is now (2g - r - b)
    np.divide(green, channel_sum, out=green)
    np.multiply(green, 255.0, out=green)
    np.clip(green, 0, 255, out=green)
    image_out = green.astype('uint8')

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
    upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
    hsv_thresh = cv2.inRange(hsv_image, lower, upper)

    image_out = hsv_thresh & image_out
    return image_out


def exhsv_opt4_cv2_math(image, hue_min=30, hue_max=90, brightness_min=5, brightness_max=200,
                         saturation_min=30, saturation_max=255, invert_hue=False):
    """Optimization 4: Use cv2 arithmetic which uses SIMD internally."""
    blue, green, red = cv2.split(image)
    blue = blue.astype(np.float32)
    green = green.astype(np.float32)
    red = red.astype(np.float32)

    channel_sum = cv2.add(cv2.add(red, green), blue)
    np.maximum(channel_sum, 1.0, out=channel_sum)

    # (2g - r - b) / sum * 255
    numerator = cv2.subtract(cv2.subtract(cv2.multiply(green, 2.0), red), blue)
    cv2.divide(numerator, channel_sum, dst=numerator)
    cv2.multiply(numerator, 255.0, dst=numerator)
    np.clip(numerator, 0, 255, out=numerator)
    image_out = numerator.astype('uint8')

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
    upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
    hsv_thresh = cv2.inRange(hsv_image, lower, upper)

    image_out = hsv_thresh & image_out
    return image_out


def exhsv_opt5_uint16_nodiv(image, hue_min=30, hue_max=90, brightness_min=5, brightness_max=200,
                             saturation_min=30, saturation_max=255, invert_hue=False):
    """Optimization 5: uint16 arithmetic, approximate division with LUT/shift."""
    blue, green, red = cv2.split(image)

    # uint16 to avoid overflow: max = 2*255 + 255 + 255 = 1020
    g16 = green.astype(np.uint16)
    r16 = red.astype(np.uint16)
    b16 = blue.astype(np.uint16)

    channel_sum = r16 + g16 + b16
    np.maximum(channel_sum, 1, out=channel_sum)

    # (2g - r - b) can be negative, use int16
    numerator = (2 * g16 - r16 - b16).astype(np.int16)
    # Approximate: (numerator * 255) / channel_sum
    # Use float32 for the division (unavoidable) but start from int16
    result = (numerator.astype(np.float32) * 255.0) / channel_sum.astype(np.float32)
    np.clip(result, 0, 255, out=result)
    image_out = result.astype('uint8')

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower = np.array([hue_min, saturation_min, brightness_min], dtype=np.uint8)
    upper = np.array([hue_max, saturation_max, brightness_max], dtype=np.uint8)
    hsv_thresh = cv2.inRange(hsv_image, lower, upper)

    image_out = hsv_thresh & image_out
    return image_out


def gob_inference_opt(image, output, kernel, exg_min=30, exg_max=250, min_detection_area=1):
    """Optimized GreenOnBrown.inference post-processing.

    Changes:
    - In-place clip (no allocation)
    - Skip np.abs (output already clipped to positive range)
    - Direct uint8 view
    """
    np.clip(output, exg_min, exg_max, out=output)
    output = output.astype(np.uint8)  # already positive after clip, no abs needed
    threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                           cv2.THRESH_BINARY_INV, 31, 2)
    threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, kernel, iterations=1)
    contours, _ = cv2.findContours(threshold_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    weed_centres = []
    for c in contours:
        if cv2.contourArea(c) > min_detection_area:
            x, y, w, h = cv2.boundingRect(c)
            boxes.append([x, y, w, h])
            weed_centres.append([x + w // 2, y + h // 2])
    return boxes, weed_centres


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='ExHSV hot-path benchmark')
    parser.add_argument('--rounds', type=int, default=100, help='Timing rounds (default: 100)')
    parser.add_argument('--warmup', type=int, default=20, help='Warmup rounds (default: 20)')
    parser.add_argument('--image-size', type=str, default='1280x720', help='Image WxH (default: 1280x720)')
    args = parser.parse_args()

    w, h = map(int, args.image_size.split('x'))
    image = make_field_image(h, w)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    print(f'=== ExHSV Hot-Path Benchmark ===')
    print(f'Image: {w}x{h} ({w*h:,} pixels), Rounds: {args.rounds}')
    print()

    # --- Stage profiling: break down where time goes ---
    print('--- Stage breakdown (current implementation) ---')

    def stage_split():
        return cv2.split(image)
    timeit(stage_split, args.rounds, args.warmup, 'cv2.split (BGR->3 channels)')

    blue, green, red = cv2.split(image)

    def stage_astype():
        blue.astype(np.float32)
        green.astype(np.float32)
        red.astype(np.float32)
    timeit(stage_astype, args.rounds, args.warmup, '3x .astype(float32)')

    bf = blue.astype(np.float32)
    gf = green.astype(np.float32)
    rf = red.astype(np.float32)

    def stage_channel_sum():
        s = rf + gf + bf
        s[s == 0] = 1
        return s
    timeit(stage_channel_sum, args.rounds, args.warmup, 'channel_sum + zero-guard (boolean)')

    def stage_channel_sum_max():
        s = rf + gf + bf
        np.maximum(s, 1.0, out=s)
        return s
    timeit(stage_channel_sum_max, args.rounds, args.warmup, 'channel_sum + zero-guard (np.maximum)')

    csum = rf + gf + bf
    np.maximum(csum, 1.0, out=csum)

    def stage_3div():
        return 255 * (2 * gf / csum - rf / csum - bf / csum)
    timeit(stage_3div, args.rounds, args.warmup, 'ExG normalized (3 divisions)')

    def stage_1div():
        return 255.0 * (2.0 * gf - rf - bf) / csum
    timeit(stage_1div, args.rounds, args.warmup, 'ExG normalized (1 division)')

    def stage_1div_inplace():
        tmp = gf.copy()
        np.multiply(tmp, 2.0, out=tmp)
        np.subtract(tmp, rf, out=tmp)
        np.subtract(tmp, bf, out=tmp)
        np.divide(tmp, csum, out=tmp)
        np.multiply(tmp, 255.0, out=tmp)
        return tmp
    timeit(stage_1div_inplace, args.rounds, args.warmup, 'ExG normalized (in-place ops)')

    exg_out = 255.0 * (2.0 * gf - rf - bf) / csum
    np.clip(exg_out, 0, 255, out=exg_out)
    exg_uint8 = exg_out.astype('uint8')

    def stage_cvtcolor():
        return cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    timeit(stage_cvtcolor, args.rounds, args.warmup, 'cv2.cvtColor BGR->HSV')

    hsv_image = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

    def stage_inrange():
        lower = np.array([30, 30, 5], dtype=np.uint8)
        upper = np.array([90, 255, 200], dtype=np.uint8)
        return cv2.inRange(hsv_image, lower, upper)
    timeit(stage_inrange, args.rounds, args.warmup, 'cv2.inRange (HSV threshold)')

    hsv_thresh = stage_inrange()

    def stage_and():
        return hsv_thresh & exg_uint8
    timeit(stage_and, args.rounds, args.warmup, 'bitwise AND (HSV & ExG)')

    print()
    print('--- Full ExHSV function variants ---')

    t_current = timeit(lambda: exhsv_current(image), args.rounds, args.warmup,
                       'CURRENT (3 divisions, bool index)')
    t_opt1 = timeit(lambda: exhsv_opt1_single_div(image), args.rounds, args.warmup,
                    'OPT1: single division')
    t_opt2 = timeit(lambda: exhsv_opt2_maximum(image), args.rounds, args.warmup,
                    'OPT2: + np.maximum zero-guard')
    t_opt3 = timeit(lambda: exhsv_opt3_inplace(image), args.rounds, args.warmup,
                    'OPT3: + in-place arithmetic')
    t_opt4 = timeit(lambda: exhsv_opt4_cv2_math(image), args.rounds, args.warmup,
                    'OPT4: cv2 arithmetic (SIMD)')
    t_opt5 = timeit(lambda: exhsv_opt5_uint16_nodiv(image), args.rounds, args.warmup,
                    'OPT5: uint16 start, late float32')

    # Verify correctness: outputs should match (or be very close)
    out_current = exhsv_current(image)
    out_opt1 = exhsv_opt1_single_div(image)
    out_opt2 = exhsv_opt2_maximum(image)
    out_opt3 = exhsv_opt3_inplace(image)
    out_opt4 = exhsv_opt4_cv2_math(image)
    out_opt5 = exhsv_opt5_uint16_nodiv(image)

    print()
    print('--- Correctness check (max pixel diff vs current) ---')
    for name, out in [('OPT1', out_opt1), ('OPT2', out_opt2), ('OPT3', out_opt3),
                      ('OPT4', out_opt4), ('OPT5', out_opt5)]:
        diff = np.max(np.abs(out_current.astype(int) - out.astype(int)))
        match = 'EXACT' if diff == 0 else f'max diff={diff}'
        print(f'  {name}: {match}')

    print()
    print('--- GreenOnBrown post-processing ---')
    exhsv_out = exhsv_current(image).astype(np.float32)  # simulate what GoB receives

    def gob_current():
        out = exhsv_out.copy()  # don't mutate
        return gob_inference_current(image, out, kernel)
    t_gob_cur = timeit(gob_current, args.rounds, args.warmup,
                       'CURRENT (clip + abs + uint8)')

    def gob_opt():
        out = exhsv_out.copy()
        return gob_inference_opt(image, out, kernel)
    t_gob_opt = timeit(gob_opt, args.rounds, args.warmup,
                       'OPT: in-place clip, skip abs')

    print()
    print('=' * 65)
    print('SUMMARY')
    print('=' * 65)
    best_exhsv = min(t_opt1, t_opt2, t_opt3, t_opt4, t_opt5)
    best_name = ['OPT1', 'OPT2', 'OPT3', 'OPT4', 'OPT5'][
        [t_opt1, t_opt2, t_opt3, t_opt4, t_opt5].index(best_exhsv)]
    saved_exhsv = t_current - best_exhsv
    pct_exhsv = (saved_exhsv / t_current * 100) if t_current > 0 else 0
    saved_gob = t_gob_cur - t_gob_opt
    pct_gob = (saved_gob / t_gob_cur * 100) if t_gob_cur > 0 else 0

    print(f'  ExHSV current:      {t_current:.2f}ms')
    print(f'  ExHSV best ({best_name}): {best_exhsv:.2f}ms  ({saved_exhsv:+.2f}ms, {pct_exhsv:.0f}% faster)')
    print(f'  GoB current:        {t_gob_cur:.2f}ms')
    print(f'  GoB optimized:      {t_gob_opt:.2f}ms  ({saved_gob:+.2f}ms, {pct_gob:.0f}% faster)')
    total_cur = t_current + t_gob_cur
    total_opt = best_exhsv + t_gob_opt
    total_saved = total_cur - total_opt
    total_pct = (total_saved / total_cur * 100) if total_cur > 0 else 0
    print(f'  ---')
    print(f'  Combined current:   {total_cur:.2f}ms')
    print(f'  Combined optimized: {total_opt:.2f}ms  ({total_saved:+.2f}ms, {total_pct:.0f}% faster)')
    print('=' * 65)


if __name__ == '__main__':
    main()
