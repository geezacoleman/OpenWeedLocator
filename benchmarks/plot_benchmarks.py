#!/usr/bin/env python
"""
Generate OWL-branded benchmark charts for the v3.0 release.

Reads benchmark JSON and produces publication-quality plots using
the OWL CSS design system colors (navy primary with lighter shades).
"""
import json
import os
import statistics
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# =============================================================================
# OWL Brand Colors (from controller/shared/css/_variables.css)
# =============================================================================
OWL_PRIMARY = '#022775'       # --owl-primary (navy)
OWL_DANGER = '#e74c3c'

# Version colors — navy shades: darkest = newest, lightest = oldest
V1_COLOR = '#8ca4d4'          # Light navy (oldest)
V2_COLOR = '#4a6faf'          # Medium navy
V3_COLOR = '#022775'          # Full navy (current — darkest, most prominent)

FONT_FAMILY = 'Segoe UI'


def owl_style(ax, title=None, xlabel=None, ylabel=None):
    """Apply OWL brand styling to a matplotlib axes."""
    ax.set_facecolor('white')
    ax.figure.set_facecolor('white')

    # Black axes, no top/right spines
    for spine in ['bottom', 'left']:
        ax.spines[spine].set_color('black')
        ax.spines[spine].set_linewidth(0.8)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)

    # No grid
    ax.grid(False)

    # Labels
    if title:
        ax.set_title(title, fontfamily=FONT_FAMILY, fontsize=14,
                     fontweight=600, color=OWL_PRIMARY, pad=14)
    if xlabel:
        ax.set_xlabel(xlabel, fontfamily=FONT_FAMILY, fontsize=11,
                      color='black', labelpad=8)
    if ylabel:
        ax.set_ylabel(ylabel, fontfamily=FONT_FAMILY, fontsize=11,
                      color='black', labelpad=8)

    # Tick styling
    ax.tick_params(colors='black', labelsize=10)
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontfamily(FONT_FAMILY)


def load_benchmark_data():
    """Load the latest benchmark JSON."""
    bench_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = sorted([f for f in os.listdir(bench_dir)
                         if f.endswith('_v1_vs_current.json')], reverse=True)
    if not candidates:
        raise FileNotFoundError("No v1_vs_current benchmark JSON found")

    path = os.path.join(bench_dir, candidates[0])
    print(f"Loading: {path}")
    with open(path) as f:
        return json.load(f)


def pixel_count(res_str):
    """Convert '1280x720' to total pixel count."""
    w, h = res_str.split('x')
    return int(w) * int(h)


def megapixels(res_str):
    """Convert '1280x720' to megapixels."""
    return pixel_count(res_str) / 1_000_000


# =============================================================================
# Chart 1: ExHSV Loop Time vs Image Size (line chart)
# =============================================================================
def plot_exhsv_loop_time(data, output_dir):
    """Line chart: mean loop time (ms) vs image size with SEM error bars."""
    exhsv = [r for r in data['full_loop_results'] if r['algorithm'] == 'exhsv']
    exhsv.sort(key=lambda r: pixel_count(r['resolution']))

    resolutions = [r['resolution'] for r in exhsv]
    mpx = [megapixels(r['resolution']) for r in exhsv]
    v1_times = [r['v1_2021']['mean'] for r in exhsv]
    v2_times = [r['v2_main']['mean'] for r in exhsv]
    v3_times = [r['v3_current']['mean'] for r in exhsv]
    v1_sem = [r['v1_2021']['sem'] for r in exhsv]
    v2_sem = [r['v2_main']['sem'] for r in exhsv]
    v3_sem = [r['v3_current']['sem'] for r in exhsv]

    fig, ax = plt.subplots(figsize=(9, 5.5))

    # 30fps threshold line
    ax.axhline(y=33.3, color=OWL_DANGER, linewidth=1, linestyle='--', alpha=0.5)
    ax.text(mpx[-1] + 0.02, 34.5, '30 fps limit', fontfamily=FONT_FAMILY,
            fontsize=8, color=OWL_DANGER, alpha=0.7, va='bottom')

    # Plot lines with SEM error bars — lightest to darkest
    ax.errorbar(mpx, v1_times, yerr=v1_sem, fmt='o-', color=V1_COLOR,
                linewidth=2, markersize=7, capsize=3, label='v1 (Aug 2021)', zorder=3)
    ax.errorbar(mpx, v2_times, yerr=v2_sem, fmt='s-', color=V2_COLOR,
                linewidth=2, markersize=7, capsize=3, label='v2 (main)', zorder=3)
    ax.errorbar(mpx, v3_times, yerr=v3_sem, fmt='D-', color=V3_COLOR,
                linewidth=2.5, markersize=8, capsize=3, label='v3 (this release)', zorder=4)

    # Resolution labels on v3 points
    for mx, t, res in zip(mpx, v3_times, resolutions):
        ax.annotate(res, (mx, t), textcoords="offset points",
                    xytext=(0, -15), ha='center', fontsize=8,
                    fontfamily=FONT_FAMILY, color=V3_COLOR, fontweight=600)

    # Speedup annotations (v2 -> v3)
    for mx, v2t, v3t in zip(mpx, v2_times, v3_times):
        speedup = v2t / v3t
        mid_y = (v2t + v3t) / 2
        ax.annotate(f'{speedup:.1f}x', (mx, mid_y),
                    textcoords="offset points", xytext=(14, 0),
                    ha='left', fontsize=9, fontfamily=FONT_FAMILY,
                    color=V3_COLOR, fontweight=600)

    owl_style(ax, title='Detection loop time vs image size (ExHSV)',
              xlabel='Image size (megapixels)',
              ylabel='Mean loop time per frame (ms)')

    ax.legend(fontsize=10, loc='upper left', frameon=True,
              facecolor='white', edgecolor='black',
              prop={'family': FONT_FAMILY, 'size': 10})
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    path = os.path.join(output_dir, 'benchmark_exhsv_loop_time.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"  Saved: {path}")


# =============================================================================
# Chart 2: FPS vs Image Size (line chart)
# =============================================================================
def plot_exhsv_fps(data, output_dir):
    """Line chart: FPS vs image size for all three versions."""
    exhsv = [r for r in data['full_loop_results'] if r['algorithm'] == 'exhsv']
    exhsv.sort(key=lambda r: pixel_count(r['resolution']))

    mpx = [megapixels(r['resolution']) for r in exhsv]

    v1_fps = [1000.0 / r['v1_2021']['mean'] for r in exhsv]
    v2_fps = [1000.0 / r['v2_main']['mean'] for r in exhsv]
    v3_fps = [1000.0 / r['v3_current']['mean'] for r in exhsv]

    fig, ax = plt.subplots(figsize=(9, 5.5))

    # 30fps threshold
    ax.axhline(y=30, color=OWL_DANGER, linewidth=1, linestyle='--', alpha=0.4)
    ax.text(mpx[-1] + 0.02, 32, '30 fps', fontfamily=FONT_FAMILY,
            fontsize=8, color=OWL_DANGER, alpha=0.6)

    ax.plot(mpx, v1_fps, 'o-', color=V1_COLOR, linewidth=2, markersize=7,
            label='v1 (Aug 2021)', zorder=3)
    ax.plot(mpx, v2_fps, 's-', color=V2_COLOR, linewidth=2, markersize=7,
            label='v2 (main)', zorder=3)
    ax.plot(mpx, v3_fps, 'D-', color=V3_COLOR, linewidth=2.5, markersize=8,
            label='v3 (this release)', zorder=4)

    # Shaded improvement region between v2 and v3
    ax.fill_between(mpx, v2_fps, v3_fps, alpha=0.10, color=V3_COLOR)

    # FPS annotations on v3 line
    for mx, fps in zip(mpx, v3_fps):
        ax.annotate(f'{fps:.0f}', (mx, fps), textcoords="offset points",
                    xytext=(0, 12), ha='center', fontsize=9,
                    fontfamily=FONT_FAMILY, color=V3_COLOR, fontweight=600)

    owl_style(ax, title='Detection throughput vs image size (ExHSV)',
              xlabel='Image size (megapixels)',
              ylabel='Frames per second (FPS)')

    ax.legend(fontsize=10, loc='upper right', frameon=True,
              facecolor='white', edgecolor='black',
              prop={'family': FONT_FAMILY, 'size': 10})
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    path = os.path.join(output_dir, 'benchmark_exhsv_fps.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"  Saved: {path}")


# =============================================================================
# Chart 3: Speedup Factor Chart (uses mean)
# =============================================================================
def plot_speedup(data, output_dir):
    """Horizontal bar chart: speedup factors (v2 -> v3) at each resolution."""
    exhsv = [r for r in data['full_loop_results'] if r['algorithm'] == 'exhsv']
    exhsv.sort(key=lambda r: pixel_count(r['resolution']))

    resolutions = [r['resolution'] for r in exhsv]
    v2_v3_speedups = [r['v2_main']['mean'] / r['v3_current']['mean'] for r in exhsv]
    v1_v3_speedups = [r['v1_2021']['mean'] / r['v3_current']['mean'] for r in exhsv]

    y = np.arange(len(resolutions))
    bar_height = 0.35

    fig, ax = plt.subplots(figsize=(9, 4.5))

    bars1 = ax.barh(y + bar_height / 2, v1_v3_speedups, bar_height,
                    label='vs v1 (2021)', color=V1_COLOR, edgecolor='white',
                    linewidth=0.5, zorder=3)
    bars2 = ax.barh(y - bar_height / 2, v2_v3_speedups, bar_height,
                    label='vs v2 (main)', color=V3_COLOR, edgecolor='white',
                    linewidth=0.5, zorder=3)

    # Value labels
    for bar, val in zip(bars1, v1_v3_speedups):
        ax.text(bar.get_width() + 0.05, bar.get_y() + bar.get_height() / 2,
                f'{val:.1f}x', ha='left', va='center', fontsize=10,
                fontfamily=FONT_FAMILY, fontweight=600, color=V1_COLOR)
    for bar, val in zip(bars2, v2_v3_speedups):
        ax.text(bar.get_width() + 0.05, bar.get_y() + bar.get_height() / 2,
                f'{val:.1f}x', ha='left', va='center', fontsize=10,
                fontfamily=FONT_FAMILY, fontweight=600, color=V3_COLOR)

    # 1x reference line
    ax.axvline(x=1, color='black', linewidth=0.5, linestyle='-', zorder=1)

    ax.set_yticks(y)
    ax.set_yticklabels(resolutions)
    ax.invert_yaxis()

    owl_style(ax, title='v3 speedup factor (ExHSV, mean)',
              xlabel='Speedup (higher is better)')

    ax.legend(fontsize=10, loc='lower right', frameon=True,
              facecolor='white', edgecolor='black',
              prop={'family': FONT_FAMILY, 'size': 10})

    fig.tight_layout()
    path = os.path.join(output_dir, 'benchmark_speedup.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"  Saved: {path}")


# =============================================================================
# Chart 4: Weed Density vs Loop Time
# =============================================================================
def plot_weed_density(data, output_dir):
    """Line chart: mean loop time vs weed count, binned in groups of 5.

    Shows whether older pipeline versions degrade more at higher weed density.
    Uses the largest resolution that has per_iteration data.
    """
    # Pick the largest resolution with per-iteration data
    exhsv = [r for r in data['full_loop_results']
             if r['algorithm'] == 'exhsv' and 'per_iteration' in r]
    if not exhsv:
        print("  [SKIP] No per-iteration data for weed density chart")
        return

    exhsv.sort(key=lambda r: pixel_count(r['resolution']), reverse=True)
    result = exhsv[0]
    per_iter = result['per_iteration']
    resolution = result['resolution']

    weed_counts = np.array(per_iter['weed_counts'])
    v1_times = np.array(per_iter['v1_times_ms'])
    v2_times = np.array(per_iter['v2_times_ms'])
    v3_times = np.array(per_iter['v3_times_ms'])

    # Bin into groups of 5 weeds: 0-4, 5-9, 10-14, ...
    bin_size = 5
    max_count = int(weed_counts.max())
    bin_edges = list(range(0, max_count + bin_size + 1, bin_size))

    bin_centres = []
    v1_means, v2_means, v3_means = [], [], []
    v1_sems, v2_sems, v3_sems = [], [], []

    for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
        mask = (weed_counts >= lo) & (weed_counts < hi)
        n = mask.sum()
        if n < 3:
            continue

        bin_centres.append((lo + hi) / 2)

        for times, means, sems in [(v1_times, v1_means, v1_sems),
                                   (v2_times, v2_means, v2_sems),
                                   (v3_times, v3_means, v3_sems)]:
            vals = times[mask]
            m = vals.mean()
            means.append(m)
            sems.append(vals.std() / np.sqrt(n))

    fig, ax = plt.subplots(figsize=(9, 5.5))

    ax.errorbar(bin_centres, v1_means, yerr=v1_sems, fmt='o-', color=V1_COLOR,
                linewidth=2, markersize=7, capsize=3, label='v1 (Aug 2021)', zorder=3)
    ax.errorbar(bin_centres, v2_means, yerr=v2_sems, fmt='s-', color=V2_COLOR,
                linewidth=2, markersize=7, capsize=3, label='v2 (main)', zorder=3)
    ax.errorbar(bin_centres, v3_means, yerr=v3_sems, fmt='D-', color=V3_COLOR,
                linewidth=2.5, markersize=8, capsize=3, label='v3 (this release)', zorder=4)

    # Slope annotations — show how much each version slows per 10 extra weeds
    if len(bin_centres) >= 2:
        for label, means, color, y_off in [
            ('v1', v1_means, V1_COLOR, 8),
            ('v2', v2_means, V2_COLOR, 8),
            ('v3', v3_means, V3_COLOR, -14),
        ]:
            slope = (means[-1] - means[0]) / (bin_centres[-1] - bin_centres[0]) * 10
            sign = '+' if slope >= 0 else ''
            ax.annotate(f'{sign}{slope:.2f} ms / 10 weeds',
                        (bin_centres[-1], means[-1]),
                        textcoords="offset points", xytext=(10, y_off),
                        ha='left', fontsize=8, fontfamily=FONT_FAMILY,
                        fontweight=600, color=color)

    owl_style(ax, title=f'Loop time vs weed density (ExHSV, {resolution})',
              xlabel='Weed count per frame',
              ylabel='Mean loop time (ms)')

    ax.legend(fontsize=10, loc='upper left', frameon=True,
              facecolor='white', edgecolor='black',
              prop={'family': FONT_FAMILY, 'size': 10})
    ax.set_ylim(bottom=0)
    ax.set_xlim(left=0)

    fig.tight_layout()
    path = os.path.join(output_dir, 'benchmark_weed_density.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"  Saved: {path}")


# =============================================================================
# Main
# =============================================================================
def main():
    data = load_benchmark_data()
    output_dir = os.path.dirname(os.path.abspath(__file__))

    print("\nGenerating OWL benchmark charts...")
    plot_exhsv_loop_time(data, output_dir)
    plot_exhsv_fps(data, output_dir)
    plot_speedup(data, output_dir)
    plot_weed_density(data, output_dir)
    print("\nDone.")


if __name__ == '__main__':
    main()
