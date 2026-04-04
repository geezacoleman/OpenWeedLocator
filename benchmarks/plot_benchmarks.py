#!/usr/bin/env python
"""
Generate OWL-branded benchmark charts for the v3.0 release.

Reads benchmark JSON and produces publication-quality plots using
the OWL CSS design system colors (navy primary with lighter shades).
"""
import json
import os
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
# Chart 1: ExHSV Loop Time vs Image Size (line chart) — uses MEDIAN
# =============================================================================
def plot_exhsv_loop_time(data, output_dir):
    """Line chart: loop time (ms) vs image size with SEM error bars."""
    exhsv = [r for r in data['full_loop_results'] if r['algorithm'] == 'exhsv']
    exhsv.sort(key=lambda r: pixel_count(r['resolution']))

    resolutions = [r['resolution'] for r in exhsv]
    mpx = [megapixels(r['resolution']) for r in exhsv]
    v1_times = [r['v1_2021']['mean'] for r in exhsv]
    v2_times = [r['v2_main']['mean'] for r in exhsv]
    v3_times = [r['v3_current']['mean'] for r in exhsv]

    fig, ax = plt.subplots(figsize=(9, 5.5))

    # 30fps threshold line
    ax.axhline(y=33.3, color=OWL_DANGER, linewidth=1, linestyle='--', alpha=0.5)
    ax.text(mpx[-1] + 0.02, 34.5, '30 fps limit', fontfamily=FONT_FAMILY,
            fontsize=8, color=OWL_DANGER, alpha=0.7, va='bottom')

    # Plot lines — lightest to darkest
    ax.plot(mpx, v1_times, 'o-', color=V1_COLOR, linewidth=2, markersize=7,
            label='v1 (Aug 2021)', zorder=3)
    ax.plot(mpx, v2_times, 's-', color=V2_COLOR, linewidth=2, markersize=7,
            label='v2 (main)', zorder=3)
    ax.plot(mpx, v3_times, 'D-', color=V3_COLOR, linewidth=2.5, markersize=8,
            label='v3 (this release)', zorder=4)

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
              ylabel='Loop time per frame (ms)')

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
# Chart 2: FPS vs Image Size (line chart) — uses MEDIAN-derived FPS
# =============================================================================
def plot_exhsv_fps(data, output_dir):
    """Line chart: FPS vs image size for all three versions with SEM error bars."""
    exhsv = [r for r in data['full_loop_results'] if r['algorithm'] == 'exhsv']
    exhsv.sort(key=lambda r: pixel_count(r['resolution']))

    resolutions = [r['resolution'] for r in exhsv]
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
# Chart 3: Algorithm Comparison Bar Chart (grouped bars at 1280x720)
# =============================================================================
def plot_algorithm_comparison(data, output_dir):
    """Grouped bar chart: all algorithms at 640x480 across three versions."""
    results_640 = [r for r in data['full_loop_results']
                   if r['resolution'] == '640x480']
    results_640.sort(key=lambda r: r['v3_current']['mean'], reverse=True)

    algorithms = [r['algorithm'].upper() for r in results_640]
    v1_times = [r['v1_2021']['mean'] for r in results_640]
    v2_times = [r['v2_main']['mean'] for r in results_640]
    v3_times = [r['v3_current']['mean'] for r in results_640]

    x = np.arange(len(algorithms))
    bar_width = 0.25

    fig, ax = plt.subplots(figsize=(9, 5))

    ax.bar(x - bar_width, v1_times, bar_width, label='v1 (Aug 2021)',
           color=V1_COLOR, edgecolor='white', linewidth=0.5, zorder=3)
    ax.bar(x, v2_times, bar_width, label='v2 (main)',
           color=V2_COLOR, edgecolor='white', linewidth=0.5, zorder=3)
    bars3 = ax.bar(x + bar_width, v3_times, bar_width, label='v3 (this release)',
                   color=V3_COLOR, edgecolor='white', linewidth=0.5, zorder=3)

    # Speedup labels on v3 bars
    for bar, v2t, v3t in zip(bars3, v2_times, v3_times):
        speedup = v2t / v3t
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                f'{speedup:.1f}x', ha='center', va='bottom',
                fontsize=9, fontfamily=FONT_FAMILY, fontweight=600,
                color=V3_COLOR)

    ax.set_xticks(x)
    ax.set_xticklabels(algorithms)

    owl_style(ax, title='Algorithm performance at 640x480',
              ylabel='Loop time per frame (ms)')

    ax.legend(fontsize=10, loc='upper right', frameon=True,
              facecolor='white', edgecolor='black',
              prop={'family': FONT_FAMILY, 'size': 10})

    fig.tight_layout()
    path = os.path.join(output_dir, 'benchmark_algorithm_comparison.png')
    fig.savefig(path, dpi=150, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    print(f"  Saved: {path}")


# =============================================================================
# Chart 4: Speedup Factor Chart
# =============================================================================
def plot_speedup(data, output_dir):
    """Horizontal bar chart: speedup factors (v2 -> v3) at each resolution."""
    exhsv = [r for r in data['full_loop_results'] if r['algorithm'] == 'exhsv']
    exhsv.sort(key=lambda r: pixel_count(r['resolution']))

    resolutions = [r['resolution'] for r in exhsv]
    # Compute speedups from median for consistency
    v2_v3_speedups = [r['v2_main']['median'] / r['v3_current']['median'] for r in exhsv]
    v1_v3_speedups = [r['v1_2021']['median'] / r['v3_current']['median'] for r in exhsv]

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

    owl_style(ax, title='v3 speedup factor (ExHSV, median)',
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
# Main
# =============================================================================
def main():
    data = load_benchmark_data()
    output_dir = os.path.dirname(os.path.abspath(__file__))

    print("\nGenerating OWL benchmark charts...")
    plot_exhsv_loop_time(data, output_dir)
    plot_exhsv_fps(data, output_dir)
    plot_algorithm_comparison(data, output_dir)
    plot_speedup(data, output_dir)
    print("\nDone.")


if __name__ == '__main__':
    main()
