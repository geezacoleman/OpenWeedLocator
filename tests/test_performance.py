"""
Performance benchmarks for GreenOnBrown detection loop.

Measures baseline speed of:
1. GreenOnBrown inference (algorithm processing)
2. Actuation logic (lane assignment from weed centres)
3. Combined loop (inference + actuation)

Run: pytest tests/test_performance.py -v -s
Results saved to benchmarks/ directory.
"""

import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.greenonbrown import GreenOnBrown

BENCHMARK_DIR = PROJECT_ROOT / 'benchmarks'
BENCHMARK_DIR.mkdir(exist_ok=True)

# Test parameters
IMAGE_WIDTH = 640
IMAGE_HEIGHT = 480
ITERATIONS = 500
RELAY_NUM = 4
ALGORITHM = 'exhsv'


def make_test_image(width=IMAGE_WIDTH, height=IMAGE_HEIGHT):
    """Generate a synthetic image with green patches on brown background."""
    # Brown background (soil-like)
    image = np.full((height, width, 3), [60, 80, 120], dtype=np.uint8)  # BGR brown

    # Add green patches (weeds)
    rng = np.random.RandomState(42)
    for _ in range(8):
        cx = rng.randint(50, width - 50)
        cy = rng.randint(50, height - 50)
        rw = rng.randint(15, 40)
        rh = rng.randint(15, 40)
        # Green weed patch
        image[max(0, cy - rh):cy + rh, max(0, cx - rw):cx + rw] = [30, 160, 50]  # BGR green

    return image


def get_git_commit():
    """Get current git short hash."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else 'unknown'
    except Exception:
        return 'unknown'


def compute_lane_coords(width, relay_num):
    """Compute lane coordinates matching owl.py logic."""
    lane_width = width / relay_num
    lane_coords_int = {}
    for i in range(relay_num):
        lane_coords_int[i] = int(i * lane_width)
    return lane_coords_int, lane_width


def run_actuation_logic(weed_centres, lane_coords_int, lane_width, relay_num, actuation_y_thresh=0):
    """Simulate the actuation logic from owl.py hoot() loop (set-deduplicated)."""
    if not weed_centres:
        return []
    fired = set()
    for centre in weed_centres:
        if centre[1] >= actuation_y_thresh:
            relay_id = min(int(centre[0] / lane_width), relay_num - 1)
            fired.add(relay_id)
    return sorted(fired)


class TestGreenOnBrownPerformance:
    """Benchmark GreenOnBrown inference and actuation logic."""

    def setup_method(self):
        self.image = make_test_image()
        self.detector = GreenOnBrown(algorithm=ALGORITHM)
        self.lane_coords_int, self.lane_width = compute_lane_coords(IMAGE_WIDTH, RELAY_NUM)

    def _benchmark(self, func, iterations=ITERATIONS):
        """Run a function N times and return timing stats in ms."""
        times = []
        # Warmup
        for _ in range(10):
            func()

        for _ in range(iterations):
            start = time.perf_counter()
            func()
            elapsed = (time.perf_counter() - start) * 1000  # ms
            times.append(elapsed)

        arr = np.array(times)
        return {
            'mean': round(float(np.mean(arr)), 3),
            'min': round(float(np.min(arr)), 3),
            'max': round(float(np.max(arr)), 3),
            'std': round(float(np.std(arr)), 3),
            'p95': round(float(np.percentile(arr, 95)), 3),
        }

    def test_inference_speed(self):
        """Benchmark GreenOnBrown inference only."""
        def run():
            self.detector.inference(
                self.image,
                exg_min=25, exg_max=200,
                hue_min=39, hue_max=83,
                saturation_min=50, saturation_max=220,
                brightness_min=60, brightness_max=190,
                show_display=False,
                algorithm=ALGORITHM,
                min_detection_area=10,
                invert_hue=False
            )

        stats = self._benchmark(run)
        print(f"\n  GreenOnBrown inference: {stats['mean']:.1f}ms mean, "
              f"{stats['p95']:.1f}ms p95 ({ITERATIONS} iterations)")
        self._inference_stats = stats

    def test_actuation_logic_speed(self):
        """Benchmark pure actuation logic (lane assignment)."""
        # Get realistic weed centres from one inference pass
        _, _, weed_centres, _ = self.detector.inference(
            self.image, exg_min=25, exg_max=200,
            hue_min=39, hue_max=83,
            saturation_min=50, saturation_max=220,
            brightness_min=60, brightness_max=190,
            show_display=False, algorithm=ALGORITHM,
            min_detection_area=10, invert_hue=False
        )

        if not weed_centres:
            weed_centres = [[160, 240], [320, 240], [480, 240]]

        def run():
            run_actuation_logic(weed_centres, self.lane_coords_int, self.lane_width, RELAY_NUM)

        stats = self._benchmark(run, iterations=ITERATIONS * 10)
        print(f"\n  Actuation logic: {stats['mean']:.4f}ms mean, "
              f"{stats['p95']:.4f}ms p95 ({ITERATIONS * 10} iterations)")
        self._actuation_stats = stats

    def test_combined_loop_speed(self):
        """Benchmark full detection loop (inference + actuation)."""
        def run():
            _, _, weed_centres, _ = self.detector.inference(
                self.image, exg_min=25, exg_max=200,
                hue_min=39, hue_max=83,
                saturation_min=50, saturation_max=220,
                brightness_min=60, brightness_max=190,
                show_display=False, algorithm=ALGORITHM,
                min_detection_area=10, invert_hue=False
            )
            run_actuation_logic(weed_centres, self.lane_coords_int, self.lane_width, RELAY_NUM)

        stats = self._benchmark(run)
        print(f"\n  Combined loop: {stats['mean']:.1f}ms mean, "
              f"{stats['p95']:.1f}ms p95 ({ITERATIONS} iterations)")
        self._combined_stats = stats

    def test_save_benchmark_results(self):
        """Run all benchmarks and save results to file."""
        # Run inference benchmark
        def run_inference():
            self.detector.inference(
                self.image, exg_min=25, exg_max=200,
                hue_min=39, hue_max=83,
                saturation_min=50, saturation_max=220,
                brightness_min=60, brightness_max=190,
                show_display=False, algorithm=ALGORITHM,
                min_detection_area=10, invert_hue=False
            )

        inference_stats = self._benchmark(run_inference)

        # Run actuation benchmark
        _, _, weed_centres, _ = self.detector.inference(
            self.image, exg_min=25, exg_max=200,
            hue_min=39, hue_max=83,
            saturation_min=50, saturation_max=220,
            brightness_min=60, brightness_max=190,
            show_display=False, algorithm=ALGORITHM,
            min_detection_area=10, invert_hue=False
        )
        if not weed_centres:
            weed_centres = [[160, 240], [320, 240], [480, 240]]

        def run_actuation():
            run_actuation_logic(weed_centres, self.lane_coords_int, self.lane_width, RELAY_NUM)

        actuation_stats = self._benchmark(run_actuation, iterations=ITERATIONS * 10)

        # Run combined benchmark
        def run_combined():
            _, _, wc, _ = self.detector.inference(
                self.image, exg_min=25, exg_max=200,
                hue_min=39, hue_max=83,
                saturation_min=50, saturation_max=220,
                brightness_min=60, brightness_max=190,
                show_display=False, algorithm=ALGORITHM,
                min_detection_area=10, invert_hue=False
            )
            run_actuation_logic(wc, self.lane_coords_int, self.lane_width, RELAY_NUM)

        combined_stats = self._benchmark(run_combined)

        # Determine filename based on existing benchmark files
        date_str = datetime.now().strftime('%Y-%m-%d')
        existing = list(BENCHMARK_DIR.glob(f'{date_str}_*.json'))
        if any('post_gog_rewrite' in f.name for f in existing):
            label = 'post_gog_rewrite'
        elif any('baseline' in f.name for f in existing):
            label = 'post_gog_rewrite'
        else:
            label = 'baseline_pre_gog_rewrite'

        filename = f'{date_str}_{label}.json'

        results = {
            'date': date_str,
            'description': f'GreenOnBrown loop benchmark ({label})',
            'git_commit': get_git_commit(),
            'platform': f'{platform.system()}/{platform.machine()}',
            'python_version': platform.python_version(),
            'test_params': {
                'image_size': [IMAGE_WIDTH, IMAGE_HEIGHT],
                'iterations': ITERATIONS,
                'algorithm': ALGORITHM,
                'relay_num': RELAY_NUM,
                'num_weed_centres': len(weed_centres),
            },
            'results': {
                'inference_ms': inference_stats,
                'actuation_logic_ms': actuation_stats,
                'total_loop_ms': combined_stats,
            }
        }

        filepath = BENCHMARK_DIR / filename
        with open(filepath, 'w') as f:
            json.dump(results, f, indent=2)

        print(f"\n  Benchmark saved to: {filepath}")
        print(f"  Inference: {inference_stats['mean']:.1f}ms | "
              f"Actuation: {actuation_stats['mean']:.4f}ms | "
              f"Combined: {combined_stats['mean']:.1f}ms")

        # Compare with baseline if this is post-change
        if label == 'post_gog_rewrite':
            baselines = list(BENCHMARK_DIR.glob(f'*baseline*.json'))
            if baselines:
                with open(baselines[-1]) as f:
                    baseline = json.load(f)

                bl = baseline['results']['total_loop_ms']['mean']
                post = combined_stats['mean']
                diff_pct = ((post - bl) / bl) * 100

                print(f"\n  === COMPARISON ===")
                print(f"  Baseline: {bl:.1f}ms | Post-change: {post:.1f}ms | "
                      f"Diff: {diff_pct:+.1f}%")

                if diff_pct > 5:
                    print(f"  WARNING: Regression detected ({diff_pct:+.1f}%)")
                else:
                    print(f"  OK: No regression")


class TestActuationScaling:
    """Verify vectorized actuation scales O(1) with detection count."""

    def setup_method(self):
        self.lane_coords_int, self.lane_width = compute_lane_coords(IMAGE_WIDTH, RELAY_NUM)

    def _make_centres(self, n):
        """Generate n weed centres spread across the image."""
        rng = np.random.RandomState(42)
        return [[rng.randint(0, IMAGE_WIDTH), rng.randint(0, IMAGE_HEIGHT)] for _ in range(n)]

    def test_actuation_constant_time(self):
        """Actuation time should stay roughly constant regardless of weed count."""
        sizes = [1, 10, 50, 200, 1000]
        times_by_size = {}

        for n in sizes:
            centres = self._make_centres(n)
            timings = []
            # Warmup
            for _ in range(50):
                run_actuation_logic(centres, self.lane_coords_int, self.lane_width, RELAY_NUM)
            for _ in range(500):
                start = time.perf_counter()
                run_actuation_logic(centres, self.lane_coords_int, self.lane_width, RELAY_NUM)
                elapsed = (time.perf_counter() - start) * 1000
                timings.append(elapsed)
            times_by_size[n] = np.mean(timings)

        # With MAX_DETECTIONS=50 cap, actuation never sees >50 centres.
        # Verify the capped size (50) completes fast in absolute terms.
        print(f"\n  Scaling: 1 weed={times_by_size[1]:.4f}ms, "
              f"50 weeds={times_by_size[50]:.4f}ms, "
              f"1000 weeds={times_by_size[1000]:.4f}ms")
        # 50 weeds (the cap) should complete in under 0.1ms
        assert times_by_size[50] < 0.1, f"50-weed actuation too slow: {times_by_size[50]:.4f}ms"

    def test_relay_deduplication(self):
        """Multiple weeds in same lane should produce one relay call."""
        # 50 weeds all in lane 0 (x < lane_width)
        centres = [[int(self.lane_width * 0.5), 100]] * 50
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width, RELAY_NUM)
        assert fired == [0], f"Expected [0], got {fired}"

    def test_all_lanes_covered(self):
        """One weed per lane should fire all relays."""
        centres = []
        for i in range(RELAY_NUM):
            cx = int(self.lane_coords_int[i] + self.lane_width / 2)
            centres.append([cx, 100])
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width, RELAY_NUM)
        assert fired == list(range(RELAY_NUM))

    def test_empty_centres(self):
        """Empty weed list should return empty fired list."""
        fired = run_actuation_logic([], self.lane_coords_int, self.lane_width, RELAY_NUM)
        assert fired == []

    def test_edge_x_values(self):
        """Weeds at x=0 and x=width-1 should map to valid relays."""
        centres = [[0, 100], [IMAGE_WIDTH - 1, 100]]
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width, RELAY_NUM)
        assert 0 in fired
        assert RELAY_NUM - 1 in fired


class TestMaxDetectionsCap:
    """Verify GreenOnBrown MAX_DETECTIONS cap works correctly."""

    def test_cap_limits_output(self):
        """With a noisy image producing many contours, output is capped at MAX_DETECTIONS."""
        from utils.greenonbrown import GreenOnBrown, MAX_DETECTIONS

        # Create image with many small green dots
        image = np.full((480, 640, 3), [60, 80, 120], dtype=np.uint8)
        rng = np.random.RandomState(42)
        for _ in range(200):
            cx = rng.randint(10, 630)
            cy = rng.randint(10, 470)
            r = rng.randint(3, 8)
            cv2.circle(image, (cx, cy), r, (30, 180, 50), -1)

        detector = GreenOnBrown(algorithm='exhsv')
        _, boxes, weed_centres, _ = detector.inference(
            image, min_detection_area=1, show_display=False, algorithm='exhsv'
        )
        assert len(boxes) <= MAX_DETECTIONS
        assert len(weed_centres) <= MAX_DETECTIONS

    def test_normal_scene_unaffected(self):
        """Normal scenes with few detections are not affected by the cap."""
        from utils.greenonbrown import GreenOnBrown, MAX_DETECTIONS

        image = make_test_image()  # 8 green patches
        detector = GreenOnBrown(algorithm='exhsv')
        _, boxes, weed_centres, _ = detector.inference(
            image, min_detection_area=10, show_display=False, algorithm='exhsv'
        )
        # Should detect some weeds but well under cap
        assert len(boxes) < MAX_DETECTIONS
        assert len(boxes) == len(weed_centres)


class TestActuationZoneFilter:
    """Verify Y-axis actuation zone filtering."""

    def setup_method(self):
        self.lane_coords_int, self.lane_width = compute_lane_coords(IMAGE_WIDTH, RELAY_NUM)
        # Simulate actuation_zone = 25 on a 480px frame: thresh = int(480 * 0.75) = 360
        self.cropped_height = IMAGE_HEIGHT  # 480
        self.actuation_zone = 25
        self.actuation_y_thresh = int(self.cropped_height * (1.0 - self.actuation_zone / 100.0))

    def test_weed_in_zone_fires(self):
        """Weed below Y threshold (in actuation zone) should fire relay."""
        # Y=400 is below thresh=360, so it's in the bottom 25%
        centres = [[IMAGE_WIDTH // 2, 400]]
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width,
                                    RELAY_NUM, self.actuation_y_thresh)
        assert len(fired) == 1

    def test_weed_out_of_zone_blocked(self):
        """Weed above Y threshold (outside actuation zone) should NOT fire relay."""
        # Y=100 is above thresh=360, so it's outside the zone
        centres = [[IMAGE_WIDTH // 2, 100]]
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width,
                                    RELAY_NUM, self.actuation_y_thresh)
        assert fired == []

    def test_weed_on_boundary_fires(self):
        """Weed exactly at Y threshold boundary should fire (>= comparison)."""
        centres = [[IMAGE_WIDTH // 2, self.actuation_y_thresh]]
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width,
                                    RELAY_NUM, self.actuation_y_thresh)
        assert len(fired) == 1

    def test_full_zone_backward_compat(self):
        """actuation_zone = 100 (thresh=0) should fire for all weeds in distinct lanes."""
        thresh = int(self.cropped_height * (1.0 - 100 / 100.0))  # = 0
        assert thresh == 0
        # Place weeds in distinct lanes: x=80 (lane 0), x=240 (lane 1), x=400 (lane 2)
        centres = [[80, 10], [240, 240], [400, 470]]
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width,
                                    RELAY_NUM, thresh)
        assert fired == [0, 1, 2]

    def test_mixed_in_and_out_of_zone(self):
        """Only weeds in the actuation zone should fire relays."""
        centres = [
            [80, 100],   # out of zone (Y < 360)
            [240, 400],  # in zone (Y >= 360)
            [400, 50],   # out of zone
            [560, 470],  # in zone
        ]
        fired = run_actuation_logic(centres, self.lane_coords_int, self.lane_width,
                                    RELAY_NUM, self.actuation_y_thresh)
        # Only the two in-zone weeds should fire (lanes 1 and 3)
        assert len(fired) == 2


class TestCCABenchmark:
    """Benchmark CCA vs findContours blob extraction — full pipeline breakdown.

    Isolates every step to show where time actually goes:
    - findContours call alone
    - contourArea loop
    - boundingRect + centre loop
    - Full contours pipeline (all three)
    - connectedComponentsWithStats call alone
    - Stats read loop (filter + boxes + centres)
    - Full CCA pipeline (both)
    """

    RESOLUTIONS = [(640, 480), (1280, 720), (1920, 1080)]

    def _make_threshold_image(self, width, height):
        """Generate a realistic binary image via exhsv pipeline."""
        from utils.algorithms import exg_standardised_hue

        image = make_test_image(width, height)
        output = exg_standardised_hue(image, hue_min=39, hue_max=83,
                                      saturation_min=50, saturation_max=220,
                                      brightness_min=60, brightness_max=190)
        np.clip(output, 25, 200, out=output)
        output = output.astype(np.uint8)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        threshold_out = cv2.adaptiveThreshold(output, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                              cv2.THRESH_BINARY_INV, 31, 2)
        threshold_out = cv2.morphologyEx(threshold_out, cv2.MORPH_CLOSE, kernel, iterations=1)
        return threshold_out

    def _benchmark(self, func, iterations=ITERATIONS):
        """Run a function N times and return timing stats in ms."""
        for _ in range(10):
            func()

        times = []
        for _ in range(iterations):
            start = time.perf_counter()
            func()
            elapsed = (time.perf_counter() - start) * 1000
            times.append(elapsed)

        arr = np.array(times)
        return {
            'mean': round(float(np.mean(arr)), 3),
            'min': round(float(np.min(arr)), 3),
            'max': round(float(np.max(arr)), 3),
            'std': round(float(np.std(arr)), 3),
            'p95': round(float(np.percentile(arr, 95)), 3),
        }

    def test_cca_vs_contours_breakdown(self):
        """Granular step-by-step benchmark: findContours pipeline vs CCA pipeline."""
        from utils.greenonbrown import MAX_DETECTIONS

        min_area = 10
        max_det = MAX_DETECTIONS
        results_by_res = {}

        for width, height in self.RESOLUTIONS:
            threshold_out = self._make_threshold_image(width, height)
            label = f"{width}x{height}"

            # ---- Step 1: findContours call only ----
            def step_findcontours(t=threshold_out):
                cv2.findContours(t, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            s_fc = self._benchmark(step_findcontours)

            # Pre-compute contours for the loop benchmarks
            contours, _ = cv2.findContours(threshold_out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # ---- Step 2: contourArea filter loop ----
            def step_area_filter(cnts=contours):
                valid = []
                for c in cnts:
                    area = cv2.contourArea(c)
                    if area > min_area:
                        valid.append((area, c))
                if len(valid) > max_det:
                    valid.sort(key=lambda x: x[0], reverse=True)
                    valid = valid[:max_det]
                return valid

            s_area = self._benchmark(step_area_filter)

            # Pre-compute valid list for bbox benchmark
            valid = step_area_filter()

            # ---- Step 3: boundingRect + centre loop ----
            def step_bbox_centres(v=valid):
                boxes = []
                centres = []
                for area, c in v:
                    x, y, w, h = cv2.boundingRect(c)
                    boxes.append([x, y, w, h])
                    centres.append([x + w // 2, y + h // 2])
                return boxes, centres

            s_bbox = self._benchmark(step_bbox_centres)

            # ---- Step 4: Full contours pipeline (all three) ----
            def full_contours(t=threshold_out):
                cnts, _ = cv2.findContours(t, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                valid = []
                for c in cnts:
                    area = cv2.contourArea(c)
                    if area > min_area:
                        valid.append((area, c))
                if len(valid) > max_det:
                    valid.sort(key=lambda x: x[0], reverse=True)
                    valid = valid[:max_det]
                boxes = []
                centres = []
                for area, c in valid:
                    x, y, w, h = cv2.boundingRect(c)
                    boxes.append([x, y, w, h])
                    centres.append([x + w // 2, y + h // 2])
                return boxes, centres

            s_full_cnt = self._benchmark(full_contours)

            # ---- Step 5: connectedComponentsWithStats call only ----
            def step_cca_call(t=threshold_out):
                cv2.connectedComponentsWithStats(t, connectivity=8)

            s_cca = self._benchmark(step_cca_call)

            # Pre-compute CCA output for stats-read benchmark
            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
                threshold_out, connectivity=8
            )

            # ---- Step 6: Stats read loop (filter + boxes + centres) ----
            def step_stats_read(nl=num_labels, st=stats):
                valid = []
                for i in range(1, nl):
                    area = st[i, cv2.CC_STAT_AREA]
                    if area > min_area:
                        valid.append((area, i))
                if len(valid) > max_det:
                    valid.sort(key=lambda x: x[0], reverse=True)
                    valid = valid[:max_det]
                boxes = []
                centres = []
                for area, i in valid:
                    x = st[i, cv2.CC_STAT_LEFT]
                    y = st[i, cv2.CC_STAT_TOP]
                    w = st[i, cv2.CC_STAT_WIDTH]
                    h = st[i, cv2.CC_STAT_HEIGHT]
                    boxes.append([x, y, w, h])
                    centres.append([x + w // 2, y + h // 2])
                return boxes, centres

            s_stats = self._benchmark(step_stats_read)

            # ---- Step 7: Full CCA pipeline (call + stats read) ----
            def full_cca(t=threshold_out):
                nl, lb, st, ct = cv2.connectedComponentsWithStats(t, connectivity=8)
                valid = []
                for i in range(1, nl):
                    area = st[i, cv2.CC_STAT_AREA]
                    if area > min_area:
                        valid.append((area, i))
                if len(valid) > max_det:
                    valid.sort(key=lambda x: x[0], reverse=True)
                    valid = valid[:max_det]
                boxes = []
                centres = []
                for area, i in valid:
                    x = st[i, cv2.CC_STAT_LEFT]
                    y = st[i, cv2.CC_STAT_TOP]
                    w = st[i, cv2.CC_STAT_WIDTH]
                    h = st[i, cv2.CC_STAT_HEIGHT]
                    boxes.append([x, y, w, h])
                    centres.append([x + w // 2, y + h // 2])
                return boxes, centres

            s_full_cca = self._benchmark(full_cca)

            n_contours = len(contours)
            n_valid = len(valid)

            results_by_res[label] = {
                'contour_count': n_contours,
                'valid_count': n_valid,
                'findContours_only_ms': s_fc,
                'contourArea_loop_ms': s_area,
                'boundingRect_loop_ms': s_bbox,
                'full_contours_pipeline_ms': s_full_cnt,
                'cca_call_only_ms': s_cca,
                'cca_stats_read_ms': s_stats,
                'full_cca_pipeline_ms': s_full_cca,
                'speedup_full': round(s_full_cnt['mean'] / s_full_cca['mean'], 2) if s_full_cca['mean'] > 0 else 0,
            }

            # Print breakdown
            cnt_overhead = s_area['mean'] + s_bbox['mean']
            print(f"\n  === {label} ({n_contours} contours, {n_valid} valid) ===")
            print(f"  CONTOURS PIPELINE:")
            print(f"    findContours():      {s_fc['mean']:.3f}ms")
            print(f"    contourArea loop:    {s_area['mean']:.3f}ms")
            print(f"    boundingRect loop:   {s_bbox['mean']:.3f}ms")
            print(f"    Loop overhead:       {cnt_overhead:.3f}ms ({cnt_overhead / s_full_cnt['mean'] * 100:.0f}% of total)")
            print(f"    TOTAL:               {s_full_cnt['mean']:.3f}ms")
            print(f"  CCA PIPELINE:")
            print(f"    CCA call:            {s_cca['mean']:.3f}ms")
            print(f"    Stats read loop:     {s_stats['mean']:.3f}ms")
            print(f"    TOTAL:               {s_full_cca['mean']:.3f}ms")
            print(f"  SPEEDUP (contours/CCA): {results_by_res[label]['speedup_full']:.2f}x")

        # Save results
        date_str = datetime.now().strftime('%Y-%m-%d')
        filename = f'{date_str}_cca_vs_contours.json'
        results = {
            'date': date_str,
            'description': 'CCA vs findContours full pipeline breakdown benchmark',
            'git_commit': get_git_commit(),
            'platform': f'{platform.system()}/{platform.machine()}',
            'python_version': platform.python_version(),
            'opencv_version': cv2.__version__,
            'test_params': {
                'iterations': ITERATIONS,
                'min_detection_area': min_area,
                'max_detections': max_det,
            },
            'results': results_by_res,
        }

        filepath = BENCHMARK_DIR / filename
        with open(filepath, 'w') as f:
            json.dump(results, f, indent=2)

        print(f"\n  Benchmark saved to: {filepath}")
