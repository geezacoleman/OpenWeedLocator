#!/usr/bin/env python
"""
Benchmark: how well does a painted LUT profile generalise?

Answers "why is it hard to get everything 100%?" with numbers, and tests
whether a GMM-trained bake (same 32KB LUT at runtime, different trainer)
would generalise better — using the raw pixel samples already stored in
the profile, so it runs against YOUR paddock profiles.

Metrics (all on a held-out 20% split of the profile's stored samples):
  1. Coverage vs sensitivity — is the slider responsive or dead?
  2. Weed recall at a matched background false-positive rate.
  3. Robustness: recall/FPR under brightness (+/-30%) and white-balance
     shifts — i.e. does the profile survive a cloud passing over.

Run on the Pi (or anywhere with the profile file):
    python benchmarks/bench_lut_generalisation.py --profile my_paddock
    python benchmarks/bench_lut_generalisation.py            # starter profile
"""

import argparse
import os
import sys

import cv2
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from utils.lut_manager import (
    LUT_BINS,
    LUT_SHIFT,
    LUT_SIZE,
    LUTProfileManager,
    _smooth3,
    bake_from_model,
    generate_starter_pixels,
    pixel_histogram,
    sensitivity_to_miss_rate,
    train_model,
)

MATCHED_FPR = 0.01          # compare recall at 1% background false positives
GMM_COMPONENTS = 5
GMM_TRAIN_CAP = 50_000      # EM fit cap per class (keeps Pi runtime sane)

# Field-realistic colour shifts: brightness scaling and white-balance tilt
SHIFTS = [
    ('unshifted', (1.00, 1.00, 1.00)),
    ('bright -30%', (0.70, 0.70, 0.70)),
    ('bright -15%', (0.85, 0.85, 0.85)),
    ('bright +15%', (1.15, 1.15, 1.15)),
    ('bright +30%', (1.30, 1.30, 1.30)),
    ('warm WB', (0.90, 1.00, 1.10)),   # (B, G, R) gains
    ('cool WB', (1.10, 1.00, 0.90)),
]


def shift_pixels(pixels, gains):
    out = pixels.astype(np.float32) * np.asarray(gains, np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


def bin_indices(pixels):
    """Flat LUT index for (N, 3) BGR pixels — same maths as apply_lut."""
    b = pixels[:, 0].astype(np.int64) >> LUT_SHIFT
    g = pixels[:, 1].astype(np.int64) >> LUT_SHIFT
    r = pixels[:, 2].astype(np.int64) >> LUT_SHIFT
    return (r << 10) | (g << 5) | b


def bin_centres():
    """(LUT_SIZE, 3) BGR uint8 centres of every LUT bin, in flat-index order."""
    r, g, b = np.meshgrid(np.arange(LUT_BINS), np.arange(LUT_BINS),
                          np.arange(LUT_BINS), indexing='ij')
    centres = np.stack([b, g, r], axis=-1).reshape(-1, 3).astype(np.uint8)
    return (centres << LUT_SHIFT) + (1 << (LUT_SHIFT - 1))


# ------------------------------------------------------------------
# Model 1: the current histogram bake, expressed as a per-bin score so a
# threshold can be swept (bake_lut internals, kept in sync by test below).
# ------------------------------------------------------------------

def current_ratio_grid(train_fg, train_bg):
    hf = _smooth3(pixel_histogram(train_fg))
    hb = _smooth3(pixel_histogram(train_bg))
    alpha = 1.0 / LUT_SIZE
    ratio = ((hf / hf.sum() + alpha) / (hb / hb.sum() + alpha)).ravel()
    ratio[hf.ravel() <= 0] = 0.0    # the hf > 0 gate in bake_lut
    return ratio


# ------------------------------------------------------------------
# Model 2: GMM per class (cv2.ml.EM — ships with OpenCV, no new deps),
# scored at bin centres exactly as it would be baked into the LUT.
# ------------------------------------------------------------------

def fit_gmm(pixels, rng):
    if pixels.shape[0] > GMM_TRAIN_CAP:
        pick = rng.choice(pixels.shape[0], GMM_TRAIN_CAP, replace=False)
        pixels = pixels[pick]
    em = cv2.ml.EM_create()
    em.setClustersNumber(GMM_COMPONENTS)
    em.setCovarianceMatrixType(cv2.ml.EM_COV_MAT_GENERIC)
    em.trainEM(pixels.astype(np.float32))
    return em


def gmm_score_grid(em_fg, em_bg):
    """Per-bin log-likelihood ratio grid, evaluated at bin centres."""
    centres = bin_centres().astype(np.float32)
    ll_fg = np.array([em_fg.predict2(c.reshape(1, 3))[0][0] for c in centres])
    ll_bg = np.array([em_bg.predict2(c.reshape(1, 3))[0][0] for c in centres])
    return ll_fg - ll_bg


def evaluate(name, score_grid, test_fg, test_bg):
    """Threshold at MATCHED_FPR on unshifted bg, report recall/FPR per shift."""
    bg_scores = score_grid[bin_indices(test_bg)]
    threshold = np.quantile(bg_scores, 1.0 - MATCHED_FPR)
    coverage = float(np.mean(score_grid > threshold))

    print(f'\n--- {name} ---')
    print(f'  threshold set for {MATCHED_FPR:.0%} background FPR (unshifted); '
          f'colour-space coverage {coverage:.1%}')
    print(f'  {"shift":14s} {"weed recall":>12s} {"bg FPR":>8s}')
    for label, gains in SHIFTS:
        fg_s = score_grid[bin_indices(shift_pixels(test_fg, gains))]
        bg_s = score_grid[bin_indices(shift_pixels(test_bg, gains))]
        recall = float(np.mean(fg_s > threshold))
        fpr = float(np.mean(bg_s > threshold))
        print(f'  {label:14s} {recall:12.1%} {fpr:8.2%}')


def main():
    parser = argparse.ArgumentParser(description='LUT generalisation benchmark')
    parser.add_argument('--profile', type=str, default='',
                        help='LUT profile name in config/lut_profiles '
                             '(default: synthesised starter pixels)')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    rng = np.random.RandomState(args.seed)
    if args.profile:
        mgr = LUTProfileManager(os.path.join(PROJECT_ROOT, 'config', 'lut_profiles'))
        prof = mgr.load(args.profile)
        fg = np.asarray(prof['fg_pixels'], np.uint8).reshape(-1, 3)
        bg = np.asarray(prof['bg_pixels'], np.uint8).reshape(-1, 3)
        print(f'Profile: {args.profile} (fg={fg.shape[0]:,}, bg={bg.shape[0]:,} stored px)')
    else:
        fg, bg = generate_starter_pixels()
        print(f'Profile: synthesised starter (fg={fg.shape[0]:,}, bg={bg.shape[0]:,} px)')

    def split(pixels):
        idx = rng.permutation(pixels.shape[0])
        cut = int(0.8 * len(idx))
        return pixels[idx[:cut]], pixels[idx[cut:]]

    train_fg, test_fg = split(fg)
    train_bg, test_bg = split(bg)
    print(f'Split: train fg={train_fg.shape[0]:,} bg={train_bg.shape[0]:,} / '
          f'test fg={test_fg.shape[0]:,} bg={test_bg.shape[0]:,}')

    # 1. Slider responsiveness of the shipped trainer
    print('\n=== Coverage & held-out recall vs sensitivity (shipped trainer) ===')
    model = train_model(train_fg, train_bg)
    fg_idx, bg_idx = bin_indices(test_fg), bin_indices(test_bg)
    print(f'  {"sens":>4s} {"coverage":>9s} {"recall":>8s} {"bg FPR":>8s} '
          f'{"miss rate":>10s}')
    for s in range(0, 101, 10):
        lut = bake_from_model(model, s)
        print(f'  {s:4d} {np.mean(lut > 0):9.2%} {np.mean(lut[fg_idx] > 0):8.1%} '
              f'{np.mean(lut[bg_idx] > 0):8.2%} {sensitivity_to_miss_rate(s):10.3f}')

    # 2/3. Matched-FPR comparison: legacy histogram vs shipped Lab-GMM
    print('\n=== Matched-FPR comparison (held-out test pixels) ===')
    evaluate('LEGACY: smoothed-histogram Bayes ratio (pre-3.4.0)',
             current_ratio_grid(train_fg, train_bg), test_fg, test_bg)

    shipped_grid = model['log_ratio'].astype(np.float64).copy()
    shipped_grid[model['fg_gate'] == 0] = -1e30
    evaluate('SHIPPED: Lab-GMM trainer (utils.lut_manager.train_model)',
             shipped_grid, test_fg, test_bg)

    print('\n  fitting BGR-GMM prototype for reference...')
    em_fg = fit_gmm(train_fg, rng)
    em_bg = fit_gmm(train_bg, rng)
    evaluate(f'REFERENCE: {GMM_COMPONENTS}-component BGR GMM (no Lab, no gate)',
             gmm_score_grid(em_fg, em_bg), test_fg, test_bg)

    print('\nReading the numbers:')
    print('  - Recall that collapses under brightness shifts = the profile only')
    print('    knows the exact painted colours; paint extra frames per light.')
    print('  - The SHIPPED row should hold recall under shifts at matched FPR,')
    print('    beating LEGACY. Its gate may cost a little vs the ungated BGR row.')
    print('  - Coverage/recall that barely move across sensitivity = dead slider.')


if __name__ == '__main__':
    main()
