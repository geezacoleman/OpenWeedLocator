#!/usr/bin/env python
"""
Benchmark: Sequential vs Parallel hybrid inference.

Runs the old (sequential mask-then-ExHSV) and new (parallel ExHSV + YOLO)
approaches side by side on the same synthetic image with a real YOLO model.

Usage:
    python benchmarks/bench_hybrid_parallel.py
    python benchmarks/bench_hybrid_parallel.py --rounds 50
    python benchmarks/bench_hybrid_parallel.py --resolution 640
"""

import argparse
import os
import sys
import time

import cv2
import numpy as np

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from concurrent.futures import ThreadPoolExecutor
from utils.greenonbrown import GreenOnBrown
from ultralytics import YOLO


# ---------------------------------------------------------------------------
# Shared setup
# ---------------------------------------------------------------------------

def find_model():
    """Find a YOLO model in models/."""
    models_dir = os.path.join(PROJECT_ROOT, 'models')
    for f in os.listdir(models_dir):
        if f.endswith('.pt'):
            return os.path.join(models_dir, f)
    # Check for NCNN subdirs
    for d in os.listdir(models_dir):
        path = os.path.join(models_dir, d)
        if os.path.isdir(path) and any(f.endswith('.param') for f in os.listdir(path)):
            return path
    raise FileNotFoundError('No YOLO model found in models/')


def make_test_image(h=480, w=640):
    """Create a synthetic BGR image with green blobs (simulates field view)."""
    img = np.zeros((h, w, 3), dtype=np.uint8)
    # Brown soil background
    img[:] = (40, 80, 120)  # BGR brown
    # Scatter green patches (simulates plants)
    rng = np.random.RandomState(42)
    for _ in range(15):
        cx, cy = rng.randint(50, w - 50), rng.randint(50, h - 50)
        radius = rng.randint(15, 40)
        cv2.circle(img, (cx, cy), radius, (30, 180, 30), -1)  # Green
    return img


# ---------------------------------------------------------------------------
# OLD approach: sequential (mask image, then ExHSV on masked image)
# ---------------------------------------------------------------------------

def hybrid_sequential(model, gob, image, dilate_kernel, inference_resolution,
                      detect_class_ids, confidence, exhsv_kwargs):
    """Old sequential pipeline: YOLO -> mask -> ExHSV on masked image."""
    h_full, w_full = image.shape[:2]

    # YOLO
    results = model.predict(
        source=image, conf=confidence, classes=detect_class_ids,
        imgsz=inference_resolution, verbose=False, device='cpu'
    )

    # Build crop mask
    crop_mask = np.zeros((h_full, w_full), dtype=np.uint8)
    for result in results:
        if result.masks is not None:
            contours_full = [c.astype(np.int32).reshape(-1, 1, 2)
                             for c in result.masks.xy]
            cv2.drawContours(crop_mask, contours_full, -1, 255, -1)
        elif len(result.boxes):
            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cv2.rectangle(crop_mask, (x1, y1), (x2, y2), 255, -1)

    # Dilate
    if dilate_kernel is not None and np.any(crop_mask):
        crop_mask = cv2.dilate(crop_mask, dilate_kernel)

    # Mask image
    weed_zone = cv2.bitwise_not(crop_mask)
    masked_image = cv2.bitwise_and(image, image, mask=weed_zone)

    # ExHSV on masked image
    cnts, boxes, weed_centres, _ = gob.inference(masked_image, **exhsv_kwargs)

    # Filter
    filtered_boxes = []
    filtered_centres = []
    for i, centre in enumerate(weed_centres):
        cx, cy = centre
        if 0 <= cy < h_full and 0 <= cx < w_full:
            if crop_mask[cy, cx] == 0:
                filtered_boxes.append(boxes[i])
                filtered_centres.append(centre)

    return filtered_boxes, filtered_centres


# ---------------------------------------------------------------------------
# NEW approach: parallel (ExHSV on full image in thread, YOLO on main)
# ---------------------------------------------------------------------------

def hybrid_parallel(model, gob, executor, image, dilate_kernel, inference_resolution,
                    detect_class_ids, confidence, exhsv_kwargs):
    """New parallel pipeline: ExHSV in thread + YOLO on main thread."""
    h_full, w_full = image.shape[:2]

    # Submit ExHSV on full image
    future = executor.submit(gob.inference, image, **exhsv_kwargs)

    # YOLO on main thread
    results = model.predict(
        source=image, conf=confidence, classes=detect_class_ids,
        imgsz=inference_resolution, verbose=False, device='cpu'
    )

    # Build crop mask
    crop_mask = np.zeros((h_full, w_full), dtype=np.uint8)
    for result in results:
        if result.masks is not None:
            contours_full = [c.astype(np.int32).reshape(-1, 1, 2)
                             for c in result.masks.xy]
            cv2.drawContours(crop_mask, contours_full, -1, 255, -1)
        elif len(result.boxes):
            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cv2.rectangle(crop_mask, (x1, y1), (x2, y2), 255, -1)

    # Dilate
    if dilate_kernel is not None and np.any(crop_mask):
        crop_mask = cv2.dilate(crop_mask, dilate_kernel)

    # Wait for ExHSV
    cnts, boxes, weed_centres, _ = future.result()

    # Filter
    filtered_boxes = []
    filtered_centres = []
    for i, centre in enumerate(weed_centres):
        cx, cy = centre
        if 0 <= cy < h_full and 0 <= cx < w_full:
            if crop_mask[cy, cx] == 0:
                filtered_boxes.append(boxes[i])
                filtered_centres.append(centre)

    return filtered_boxes, filtered_centres


# ---------------------------------------------------------------------------
# Individual component timing
# ---------------------------------------------------------------------------

def time_yolo_only(model, image, inference_resolution, detect_class_ids, confidence, rounds):
    """Time YOLO predict alone."""
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        model.predict(source=image, conf=confidence, classes=detect_class_ids,
                      imgsz=inference_resolution, verbose=False, device='cpu')
        times.append((time.perf_counter() - t0) * 1000)
    return times


def time_exhsv_only(gob, image, exhsv_kwargs, rounds):
    """Time ExHSV inference alone."""
    times = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        gob.inference(image, **exhsv_kwargs)
        times.append((time.perf_counter() - t0) * 1000)
    return times


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Benchmark hybrid inference: sequential vs parallel')
    parser.add_argument('--rounds', type=int, default=30, help='Number of timing rounds (default: 30)')
    parser.add_argument('--warmup', type=int, default=5, help='Warmup rounds (default: 5)')
    parser.add_argument('--resolution', type=int, default=320, help='YOLO inference resolution (default: 320)')
    parser.add_argument('--image-size', type=str, default='640x480', help='Image WxH (default: 640x480)')
    args = parser.parse_args()

    w, h = map(int, args.image_size.split('x'))

    print(f'=== Hybrid Inference Benchmark ===')
    print(f'Image: {w}x{h}, YOLO imgsz: {args.resolution}, Rounds: {args.rounds}, Warmup: {args.warmup}')
    print()

    # Setup
    model_path = find_model()
    model_name = os.path.basename(model_path)
    print(f'Model: {model_name}')
    task = 'segment' if '-seg' in model_name.lower() or '_seg' in model_name.lower() else None
    model = YOLO(model_path, task=task)
    gob = GreenOnBrown(algorithm='exhsv')
    executor = ThreadPoolExecutor(max_workers=1)

    crop_buffer_px = 20
    dilate_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                               (2 * crop_buffer_px + 1, 2 * crop_buffer_px + 1))

    image = make_test_image(h, w)
    confidence = 0.5
    detect_class_ids = None

    exhsv_kwargs = dict(
        exg_min=30, exg_max=250, hue_min=30, hue_max=90,
        saturation_min=30, saturation_max=255,
        brightness_min=5, brightness_max=200,
        min_detection_area=1, show_display=False,
        algorithm='exhsv', invert_hue=False
    )

    # Warmup (first YOLO calls are slow due to model compilation)
    print(f'Warming up ({args.warmup} rounds each)...')
    for _ in range(args.warmup):
        hybrid_sequential(model, gob, image, dilate_kernel, args.resolution,
                          detect_class_ids, confidence, exhsv_kwargs)
        hybrid_parallel(model, gob, executor, image, dilate_kernel, args.resolution,
                        detect_class_ids, confidence, exhsv_kwargs)

    # --- Time individual components ---
    print('\nTiming individual components...')
    yolo_times = time_yolo_only(model, image, args.resolution, detect_class_ids, confidence, args.rounds)
    exhsv_times = time_exhsv_only(gob, image, exhsv_kwargs, args.rounds)

    yolo_med = np.median(yolo_times)
    exhsv_med = np.median(exhsv_times)

    print(f'  YOLO only:  median={yolo_med:.1f}ms  mean={np.mean(yolo_times):.1f}ms  '
          f'std={np.std(yolo_times):.1f}ms  min={min(yolo_times):.1f}ms  max={max(yolo_times):.1f}ms')
    print(f'  ExHSV only: median={exhsv_med:.1f}ms  mean={np.mean(exhsv_times):.1f}ms  '
          f'std={np.std(exhsv_times):.1f}ms  min={min(exhsv_times):.1f}ms  max={max(exhsv_times):.1f}ms')
    print(f'  Theoretical sequential: {yolo_med + exhsv_med:.1f}ms')
    print(f'  Theoretical parallel:   {max(yolo_med, exhsv_med):.1f}ms (limited by slower component)')

    # --- Time sequential pipeline ---
    print(f'\nBenchmarking sequential pipeline ({args.rounds} rounds)...')
    seq_times = []
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        hybrid_sequential(model, gob, image, dilate_kernel, args.resolution,
                          detect_class_ids, confidence, exhsv_kwargs)
        seq_times.append((time.perf_counter() - t0) * 1000)

    # --- Time parallel pipeline ---
    print(f'Benchmarking parallel pipeline ({args.rounds} rounds)...')
    par_times = []
    for _ in range(args.rounds):
        t0 = time.perf_counter()
        hybrid_parallel(model, gob, executor, image, dilate_kernel, args.resolution,
                        detect_class_ids, confidence, exhsv_kwargs)
        par_times.append((time.perf_counter() - t0) * 1000)

    # --- Results ---
    seq_med = np.median(seq_times)
    par_med = np.median(par_times)
    speedup = seq_med / par_med if par_med > 0 else 0
    saved = seq_med - par_med
    pct = (saved / seq_med * 100) if seq_med > 0 else 0

    print()
    print('=' * 60)
    print('RESULTS')
    print('=' * 60)
    print(f'  Sequential: median={seq_med:.1f}ms  mean={np.mean(seq_times):.1f}ms  '
          f'std={np.std(seq_times):.1f}ms')
    print(f'  Parallel:   median={par_med:.1f}ms  mean={np.mean(par_times):.1f}ms  '
          f'std={np.std(par_times):.1f}ms')
    print(f'  Speedup:    {speedup:.2f}x  ({saved:.1f}ms saved, {pct:.0f}% faster)')
    print(f'  Sequential FPS: {1000/seq_med:.1f}')
    print(f'  Parallel FPS:   {1000/par_med:.1f}')
    print('=' * 60)

    # Verify both produce same detection count (sanity check)
    seq_boxes, _ = hybrid_sequential(model, gob, image, dilate_kernel, args.resolution,
                                     detect_class_ids, confidence, exhsv_kwargs)
    par_boxes, _ = hybrid_parallel(model, gob, executor, image, dilate_kernel, args.resolution,
                                   detect_class_ids, confidence, exhsv_kwargs)
    print(f'\nSanity check: sequential found {len(seq_boxes)} weeds, parallel found {len(par_boxes)} weeds')

    executor.shutdown(wait=False)


if __name__ == '__main__':
    main()
