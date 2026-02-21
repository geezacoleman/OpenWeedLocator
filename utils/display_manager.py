"""
Tkinter-based display window for OWL.

Replaces cv2.namedWindow / cv2.createTrackbar / cv2.imshow / cv2.waitKey
with a tabbed GUI: Detection (video + threshold sliders + Smart Set) and
Focus (live FFT-based focus meter with history graph).

Threading model
---------------
- Main thread: tkinter ``mainloop()`` + ``root.after()`` for GUI updates.
- Background thread: detection loop (camera read, inference, actuation).
- ALL tkinter / Tcl access happens on the main thread only.
- Data flows between threads via plain Python attributes (atomic ref under
  CPython GIL).  No ``IntVar.get()`` / ``IntVar.set()`` from the bg thread.
"""

import collections
import logging
import os
import sys
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

logger = logging.getLogger(__name__)

# Conditional matplotlib import (heavy; only needed for Focus tab)
try:
    import matplotlib
    matplotlib.use('TkAgg')
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


def is_display_available():
    """Check whether a GUI display can be opened."""
    if sys.platform != 'win32' and not os.environ.get('DISPLAY'):
        return False
    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Name mappings between slider attribute keys and MQTT display names
# ---------------------------------------------------------------------------
_SLIDER_TO_DISPLAY = {
    'exg_min': 'ExG-Min',
    'exg_max': 'ExG-Max',
    'hue_min': 'Hue-Min',
    'hue_max': 'Hue-Max',
    'saturation_min': 'Sat-Min',
    'saturation_max': 'Sat-Max',
    'brightness_min': 'Bright-Min',
    'brightness_max': 'Bright-Max',
}
_DISPLAY_TO_SLIDER = {v: k for k, v in _SLIDER_TO_DISPLAY.items()}


# ---------------------------------------------------------------------------
# ThresholdPanel
# ---------------------------------------------------------------------------
class ThresholdPanel(ttk.LabelFrame):
    """Panel of Scale widgets replacing cv2 trackbars."""

    SLIDER_DEFS = [
        ('ExG-Min',    'exg_min',        0, 255),
        ('ExG-Max',    'exg_max',        0, 255),
        ('Hue-Min',    'hue_min',        0, 179),
        ('Hue-Max',    'hue_max',        0, 179),
        ('Sat-Min',    'saturation_min', 0, 255),
        ('Sat-Max',    'saturation_max', 0, 255),
        ('Bright-Min', 'brightness_min', 0, 255),
        ('Bright-Max', 'brightness_max', 0, 255),
    ]

    def __init__(self, parent, initial_values):
        super().__init__(parent, text='Adjust Detection Thresholds')
        self.vars = {}
        self.scales = {}

        for row, (label, key, from_, to) in enumerate(self.SLIDER_DEFS):
            var = tk.IntVar(value=initial_values.get(key, 0))
            self.vars[key] = var
            ttk.Label(self, text=label).grid(row=row, column=0, sticky=tk.W, padx=5)
            scale = tk.Scale(
                self, from_=from_, to=to, orient=tk.HORIZONTAL,
                variable=var, length=300,
            )
            scale.grid(row=row, column=1, sticky=tk.EW, padx=5, pady=2)
            self.scales[key] = scale

        self.columnconfigure(1, weight=1)

    def get_values(self):
        """Return a dict of all current slider values.  Main thread only."""
        return {key: var.get() for key, var in self.vars.items()}

    def set_values(self, values):
        """Programmatically update slider positions.  Main thread only."""
        for key, val in values.items():
            if key in self.vars:
                self.vars[key].set(val)


# ---------------------------------------------------------------------------
# FocusPanel
# ---------------------------------------------------------------------------
class FocusPanel(ttk.Frame):
    """Focus-quality tab using FFT blur metric.

    Receives raw camera frames via :meth:`push_frame` from the detection
    thread; all GUI work (FFT computation, graph update) happens on the
    main thread in :meth:`_update_gui`.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._frame = None              # raw BGR frame, set from bg thread
        self._is_active = True
        self._graph_tick = 0

        self.focus_history = collections.deque(maxlen=250)
        self.focus_moving_avg = collections.deque(maxlen=10)
        self.best_focus = float('-inf')
        self.best_frame = None
        self.last_avg_focus = float('-inf')

        self._build_widgets()

    def _build_widgets(self):
        # Video feed
        video_frame = ttk.LabelFrame(self, text='Camera Feed')
        video_frame.pack(pady=(0, 10))
        self._video_container = tk.Frame(video_frame, width=640, height=480)
        self._video_container.pack(padx=5, pady=5)
        self._video_container.pack_propagate(False)
        self._video_label = tk.Label(self._video_container)
        self._video_label.pack(fill=tk.BOTH, expand=True)
        self._photo = None

        # Focus readout
        mid = ttk.Frame(self)
        mid.pack(fill=tk.X, pady=(0, 10))
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)
        mid.columnconfigure(2, weight=1)

        cur = ttk.Frame(mid)
        cur.grid(row=0, column=0, padx=10)
        ttk.Label(cur, text='Current:', font=('Arial', 12, 'bold')).pack(side=tk.LEFT)
        self._focus_bg = tk.Frame(cur, bg='#f0f0f0', padx=5, pady=2)
        self._focus_bg.pack(side=tk.LEFT, padx=(5, 0))
        self._focus_label = tk.Label(self._focus_bg, text='0.0', font=('Arial', 16), bg='#f0f0f0')
        self._focus_label.pack()

        best = ttk.Frame(mid)
        best.grid(row=0, column=1, padx=10)
        ttk.Label(best, text='Best:', font=('Arial', 12, 'bold')).pack(side=tk.LEFT)
        self._best_label = ttk.Label(best, text='0.0', font=('Arial', 16))
        self._best_label.pack(side=tk.LEFT, padx=(5, 0))

        btns = ttk.Frame(mid)
        btns.grid(row=0, column=2, padx=10)
        ttk.Button(btns, text='Reset Best', command=self._reset_best).pack(side=tk.LEFT, padx=5)
        ttk.Button(btns, text='Display Best', command=self._display_best).pack(side=tk.LEFT, padx=5)

        # Graph (if matplotlib available)
        if HAS_MATPLOTLIB:
            graph_frame = ttk.LabelFrame(self, text='Focus History')
            graph_frame.pack(fill=tk.BOTH, expand=True)
            self._fig = Figure(figsize=(5, 2), dpi=100)
            self._fig.patch.set_facecolor('#f0f0f0')
            self._ax = self._fig.add_subplot(111)
            self._ax.set_facecolor('#f0f0f0')
            self._ax.set_xlabel('Frame')
            self._ax.set_ylabel('Focus')
            self._ax.grid(True, alpha=0.3)
            self._canvas = FigureCanvasTkAgg(self._fig, master=graph_frame)
            self._canvas.draw()
            self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        else:
            self._fig = None

    # ---- bg thread API ----

    def push_frame(self, frame):
        """Store a raw BGR frame.  Thread-safe (atomic ref)."""
        self._frame = frame

    # ---- main-thread update (called by OWLDisplay._update_gui) ----

    def update_gui(self):
        """Process frame, update video + focus display.  Main thread only."""
        frame = self._frame
        if frame is None:
            return

        # Compute focus metric (main thread, ~2-5ms at 640x480)
        from utils.algorithms import fft_blur
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        focus_val = fft_blur(grey, size=30)

        self.focus_history.append(focus_val)
        self.focus_moving_avg.append(focus_val)
        current_avg = float(np.mean(self.focus_moving_avg))

        if current_avg > self.best_focus:
            self.best_focus = current_avg
            self.best_frame = frame.copy()
        self.last_avg_focus = current_avg

        # Update video label
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img = img.resize((640, 480), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(image=img)
        self._video_label.configure(image=self._photo)

        # Update focus readout
        self._focus_label.configure(text=f'{current_avg:.1f}')
        self._best_label.configure(text=f'{self.best_focus:.1f}')

        if current_avg >= self.best_focus:
            bg = '#90EE90'
        elif current_avg < self.last_avg_focus:
            bg = '#FFB6C1'
        else:
            bg = '#f0f0f0'
        self._focus_bg.configure(bg=bg)
        self._focus_label.configure(bg=bg)

        # Update graph every 5th frame to reduce matplotlib overhead
        self._graph_tick += 1
        if self._fig and self._graph_tick % 5 == 0:
            self._update_graph()

    def _update_graph(self):
        if len(self.focus_history) < 2:
            return
        self._ax.clear()
        y = list(self.focus_history)
        x = list(range(len(y)))
        self._ax.plot(x, y, 'b-', linewidth=1, alpha=0.7)
        if len(y) >= 10:
            ws = 10
            y_avg = np.convolve(y, np.ones(ws) / ws, mode='valid')
            x_avg = list(range(ws - 1, len(y)))
            self._ax.plot(x_avg, y_avg, 'r-', linewidth=2, label='10-frame Avg')
        if self.best_focus > float('-inf'):
            self._ax.axhline(y=self.best_focus, color='g', linestyle='--', linewidth=1, label='Best')
        self._ax.set_xlabel('Frame')
        self._ax.set_ylabel('Focus')
        self._ax.grid(True, alpha=0.3)
        self._ax.legend(loc='upper right', fontsize='small')
        if y:
            mn, mx = min(y), max(y)
            margin = max((mx - mn) * 0.1, 1)
            self._ax.set_ylim(mn - margin, mx + margin)
        self._canvas.draw()

    def _reset_best(self):
        self.best_focus = float('-inf')
        self.best_frame = None

    def _display_best(self):
        if self.best_frame is None:
            return
        win = tk.Toplevel(self.winfo_toplevel())
        win.title('Best Focus Frame')
        rgb = cv2.cvtColor(self.best_frame, cv2.COLOR_BGR2RGB)
        photo = ImageTk.PhotoImage(image=Image.fromarray(rgb))
        lbl = ttk.Label(win, image=photo)
        lbl.image = photo  # prevent GC
        lbl.pack()

    def cleanup(self):
        self.focus_history.clear()
        self.focus_moving_avg.clear()
        if self._fig:
            import matplotlib.pyplot as plt
            plt.close(self._fig)


# ---------------------------------------------------------------------------
# Smart Set — compute thresholds from a clicked pixel region
# ---------------------------------------------------------------------------
def compute_smart_thresholds(frame_bgr, x, y, patch_radius=5):
    """Compute detection thresholds from a region around pixel (x, y).

    Parameters
    ----------
    frame_bgr : ndarray
        Raw BGR camera frame (the *uncropped* frame from the camera).
    x, y : int
        Pixel coordinates in the frame.
    patch_radius : int
        Half-size of the sampling patch (default 5 → 11x11 region).

    Returns
    -------
    dict
        Keys: exg_min, exg_max, hue_min, hue_max, saturation_min,
        saturation_max, brightness_min, brightness_max.
    """
    h, w = frame_bgr.shape[:2]
    y0 = max(0, y - patch_radius)
    y1 = min(h, y + patch_radius + 1)
    x0 = max(0, x - patch_radius)
    x1 = min(w, x + patch_radius + 1)

    patch = frame_bgr[y0:y1, x0:x1]

    # ExG
    B, G, R = cv2.split(patch)
    exg = np.clip(2 * G.astype(int) - R.astype(int) - B.astype(int), 0, 255)
    exg_min = max(0, int(exg.min()) - 15)
    exg_max = min(255, int(exg.max()) + 15)

    # HSV
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    h_vals = hsv[:, :, 0]
    s_vals = hsv[:, :, 1]
    v_vals = hsv[:, :, 2]

    hue_min = max(0, int(h_vals.min()) - 10)
    hue_max = min(179, int(h_vals.max()) + 10)
    sat_min = max(0, int(s_vals.min()) - 25)
    sat_max = min(255, int(s_vals.max()) + 25)
    brt_min = max(5, int(v_vals.min()) - 25)
    brt_max = min(255, int(v_vals.max()) + 25)

    return {
        'exg_min': exg_min, 'exg_max': exg_max,
        'hue_min': hue_min, 'hue_max': hue_max,
        'saturation_min': sat_min, 'saturation_max': sat_max,
        'brightness_min': brt_min, 'brightness_max': brt_max,
    }


# ---------------------------------------------------------------------------
# OWLDisplay — the main window
# ---------------------------------------------------------------------------
class OWLDisplay:
    """Tabbed tkinter window for OWL (Detection + Focus).

    Threading contract
    ------------------
    The background detection thread may call ONLY these methods:

    - ``push_frame(bgr_annotated)``   — annotated detection output
    - ``push_raw_frame(bgr_raw)``     — raw camera frame (for focus tab)
    - ``request_slider_update(d)``    — queue MQTT slider updates
    - ``request_algorithm_label(s)``  — queue algorithm label change

    Everything else (slider reads, widget updates) happens on the main
    thread inside ``_update_gui()``.
    """

    def __init__(self, initial_values, algorithm='exhsv',
                 on_save=None, on_record=None, on_quit=None):
        self.root = tk.Tk()
        self.root.title('OWL Detection Display')

        self._on_save = on_save
        self._on_record = on_record
        self._on_quit = on_quit
        self._destroyed = False

        # --- bg thread → main thread communication (plain Python, no Tcl) ---
        self._latest_frame = None       # annotated BGR frame
        self._latest_raw_frame = None   # raw BGR frame (for focus + smart set)
        self._frame_seq = 0             # sequence number to detect new frames
        self._last_drawn_seq = -1
        self._pending_slider_queue = {}  # display-name → int value
        self._pending_algo_label = None  # string or None

        # --- main thread → bg thread communication (plain Python dict) ---
        self._cached_slider_values = dict(initial_values)

        # --- smart set state ---
        self._smart_set_marker = None   # (x, y) in display coords or None

        self._build_ui(initial_values, algorithm)
        self._bind_keys()

    # ---- UI construction ----

    def _build_ui(self, initial_values, algorithm):
        # Status bar at top
        self._status_var = tk.StringVar(value=f'OWL-gorithm: {algorithm}')
        ttk.Label(self.root, textvariable=self._status_var, relief=tk.SUNKEN) \
            .pack(side=tk.TOP, fill=tk.X)

        # Button bar at bottom
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)
        ttk.Button(btn_frame, text='Save (S)', command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='Record (R)', command=self._record).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='Quit (Esc)', command=self._quit).pack(side=tk.RIGHT, padx=5)

        # Tabbed notebook
        self._notebook = ttk.Notebook(self.root)
        self._notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # --- Detection tab ---
        det_tab = ttk.Frame(self._notebook)
        self._notebook.add(det_tab, text='Detection')

        # Video container with fixed pixel dimensions
        video_outer = ttk.LabelFrame(det_tab, text='Detection Output')
        video_outer.pack(side=tk.LEFT, padx=(0, 5), pady=5)
        self._video_container = tk.Frame(video_outer, width=600, height=450)
        self._video_container.pack(padx=5, pady=5)
        self._video_container.pack_propagate(False)
        self._video_label = tk.Label(self._video_container)
        self._video_label.pack(fill=tk.BOTH, expand=True)
        self._photo = None

        # Click handler for Smart Set
        self._video_label.bind('<Button-1>', self._on_video_click)

        # Right panel: sliders + Smart Set button
        right_panel = ttk.Frame(det_tab)
        right_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(5, 0), pady=5)

        self.threshold_panel = ThresholdPanel(right_panel, initial_values)
        self.threshold_panel.pack(fill=tk.X)

        ttk.Button(right_panel, text='Smart Set (click image first)',
                   command=self._apply_smart_set).pack(fill=tk.X, pady=(10, 0))

        # --- Focus tab ---
        self.focus_panel = FocusPanel(self._notebook)
        self._notebook.add(self.focus_panel, text='Focus')

    def _bind_keys(self):
        self.root.bind_all('<s>', lambda e: self._save())
        self.root.bind_all('<r>', lambda e: self._record())
        self.root.bind_all('<Escape>', lambda e: self._quit())
        self.root.protocol('WM_DELETE_WINDOW', self._quit)

    # ---- keyboard / button callbacks (main thread) ----

    def _save(self):
        if self._on_save:
            self._on_save()

    def _record(self):
        if self._on_record:
            self._on_record()

    def _quit(self):
        if self._on_quit:
            self._on_quit()

    # ---- Smart Set (main thread) ----

    def _on_video_click(self, event):
        """Record click position on the video label."""
        self._smart_set_marker = (event.x, event.y)

    def _apply_smart_set(self):
        """Compute thresholds from the last-clicked pixel and apply."""
        raw = self._latest_raw_frame
        if raw is None or self._smart_set_marker is None:
            return

        click_x, click_y = self._smart_set_marker
        # Map display coords → original frame coords
        label_w = self._video_label.winfo_width()
        label_h = self._video_label.winfo_height()
        if label_w <= 0 or label_h <= 0:
            return

        frame_h, frame_w = raw.shape[:2]
        orig_x = int(click_x * frame_w / label_w)
        orig_y = int(click_y * frame_h / label_h)
        orig_x = max(0, min(orig_x, frame_w - 1))
        orig_y = max(0, min(orig_y, frame_h - 1))

        thresholds = compute_smart_thresholds(raw, orig_x, orig_y)
        self.threshold_panel.set_values(thresholds)
        self._cached_slider_values = dict(thresholds)
        self._smart_set_marker = None

    # ---- bg thread API (plain Python only, no Tcl) ----

    def push_frame(self, frame):
        """Store annotated BGR frame for detection tab. Thread-safe."""
        self._latest_frame = frame
        self._frame_seq += 1

    def push_raw_frame(self, frame):
        """Store raw BGR frame for focus tab + smart set. Thread-safe."""
        self._latest_raw_frame = frame
        self.focus_panel.push_frame(frame)

    def get_slider_values(self):
        """Read cached slider values. Safe from any thread.

        Returns a plain dict snapshot — does NOT touch tkinter.
        """
        return dict(self._cached_slider_values)

    def request_slider_update(self, pending):
        """Queue slider updates from MQTT/controller. Thread-safe.

        *pending* maps display names (e.g. ``'ExG-Min'``) to int values.
        These will be applied on the next main-thread GUI tick.
        """
        self._pending_slider_queue.update(pending)

    def request_algorithm_label(self, name):
        """Queue algorithm label change. Thread-safe."""
        self._pending_algo_label = name

    # ---- main-thread update loop ----

    def start_update_loop(self, interval_ms=33):
        """Schedule periodic GUI updates.  Call once before ``mainloop``."""
        self._update_interval = interval_ms
        self._update_gui()

    def _update_gui(self):
        if self._destroyed:
            return

        # 1. Drain pending slider updates (MQTT → IntVars)
        if self._pending_slider_queue:
            pending = self._pending_slider_queue
            self._pending_slider_queue = {}
            translated = {}
            for display_name, value in pending.items():
                attr = _DISPLAY_TO_SLIDER.get(display_name)
                if attr:
                    translated[attr] = value
            if translated:
                self.threshold_panel.set_values(translated)

        # 2. Drain pending algorithm label
        algo = self._pending_algo_label
        if algo is not None:
            self._pending_algo_label = None
            self._status_var.set(f'OWL-gorithm: {algo}')

        # 3. Cache slider values for the bg thread to read
        self._cached_slider_values = self.threshold_panel.get_values()

        # 4. Update detection video feed (only when new frame arrives)
        seq = self._frame_seq
        if seq != self._last_drawn_seq:
            self._last_drawn_seq = seq
            frame = self._latest_frame
            if frame is not None:
                h, w = frame.shape[:2]
                display = cv2.resize(frame, (600, int(h * 600 / w))) if w != 600 else frame

                # Draw smart-set crosshair if a click is pending
                if self._smart_set_marker is not None:
                    cx, cy = self._smart_set_marker
                    # Scale crosshair coords to display size
                    cv2.drawMarker(display, (cx, cy), (0, 255, 255),
                                   cv2.MARKER_CROSS, 20, 2)

                rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
                self._photo = ImageTk.PhotoImage(image=Image.fromarray(rgb))
                self._video_label.configure(image=self._photo)

        # 5. Update focus panel
        self.focus_panel.update_gui()

        # 6. Schedule next tick
        try:
            self.root.after(self._update_interval, self._update_gui)
        except tk.TclError:
            pass  # window already destroyed

    def mainloop(self):
        """Enter the tkinter event loop (blocks the calling thread)."""
        self.root.mainloop()

    def destroy(self):
        """Tear down the window.  Safe to call from any thread."""
        self._destroyed = True
        self.focus_panel.cleanup()
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            pass
