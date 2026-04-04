"""
Benchmark: Actuation lane assignment scaling.

Compares the old O(n * relay_num) Python loop (per-weed time.time() + no dedup)
vs the new set-deduplicated approach (single timestamp + set).

Run:  python benchmarks/bench_actuation_scaling.py
"""
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

IMAGE_WIDTH = 640
RELAY_NUM = 4
LANE_WIDTH = IMAGE_WIDTH / RELAY_NUM
LANE_COORDS_INT = {i: int(i * LANE_WIDTH) for i in range(RELAY_NUM)}
ITERATIONS = 2000


def make_centres(n):
    rng = np.random.RandomState(42)
    return [[rng.randint(0, IMAGE_WIDTH), rng.randint(0, 480)] for _ in range(n)]


def old_actuation(weed_centres):
    """Original O(n * relay_num) Python loop with per-weed time.time()."""
    fired = []
    for centre in weed_centres:
        _ = time.time()
        centre_x = centre[0]
        for i in range(RELAY_NUM):
            lane_start = LANE_COORDS_INT[i]
            lane_end = lane_start + LANE_WIDTH
            if lane_start <= centre_x < lane_end:
                fired.append(i)
    return fired


def new_actuation(weed_centres):
    """Set-deduplicated approach with single timestamp."""
    if not weed_centres:
        return []
    _ = time.time()
    fired = set()
    for centre in weed_centres:
        relay_id = min(int(centre[0] / LANE_WIDTH), RELAY_NUM - 1)
        fired.add(relay_id)
    return sorted(fired)


def benchmark(func, centres, iterations=ITERATIONS):
    # Warmup
    for _ in range(50):
        func(centres)

    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        func(centres)
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)

    arr = np.array(times)
    return {
        'mean': float(np.mean(arr)),
        'p95': float(np.percentile(arr, 95)),
        'min': float(np.min(arr)),
    }


def main():
    sizes = [1, 10, 50, 200, 1000]

    print(f"{'Weeds':>6} | {'Old mean':>10} | {'New mean':>10} | {'Speedup':>8} | {'Old p95':>10} | {'New p95':>10}")
    print("-" * 72)

    for n in sizes:
        centres = make_centres(n)
        old = benchmark(old_actuation, centres)
        new = benchmark(new_actuation, centres)
        speedup = old['mean'] / max(new['mean'], 0.00001)
        print(f"{n:>6} | {old['mean']:>9.4f}ms | {new['mean']:>9.4f}ms | {speedup:>7.1f}x | "
              f"{old['p95']:>9.4f}ms | {new['p95']:>9.4f}ms")

    print()

    # Verify correctness — both should fire the same relay set
    for n in sizes:
        centres = make_centres(n)
        old_set = set(old_actuation(centres))
        new_set = set(new_actuation(centres))
        status = "OK" if old_set == new_set else f"MISMATCH old={old_set} new={new_set}"
        print(f"  Correctness ({n:>4} weeds): {status}")


if __name__ == '__main__':
    main()
