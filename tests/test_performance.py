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


def run_actuation_logic(weed_centres, lane_coords_int, lane_width, relay_num):
    """Simulate the actuation logic from owl.py hoot() loop."""
    fired = []
    for centre in weed_centres:
        centre_x = centre[0]
        for i in range(relay_num):
            lane_start = lane_coords_int[i]
            lane_end = lane_start + lane_width
            if lane_start <= centre_x < lane_end:
                fired.append(i)
    return fired


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
