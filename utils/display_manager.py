"""
Tkinter-based display window for OWL.

Replaces cv2.namedWindow / cv2.createTrackbar / cv2.imshow / cv2.waitKey
with a proper tkinter GUI that works reliably on Raspberry Pi.
"""

import logging
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

logger = logging.getLogger(__name__)


def is_display_available():
    """Check whether a GUI display can be opened."""
    import os
    import sys

    if sys.platform != 'win32' and not os.environ.get('DISPLAY'):
        return False

    try:
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


class ThresholdPanel(ttk.LabelFrame):
    """Panel of Scale widgets replacing cv2 trackbars."""

    TRACKBAR_DEFS = [
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

        for row, (label, key, from_, to) in enumerate(self.TRACKBAR_DEFS):
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
        """Return a dict of all current slider values."""
        return {key: var.get() for key, var in self.vars.items()}

    def set_values(self, values):
        """Programmatically update slider positions (e.g. from MQTT)."""
        for key, val in values.items():
            if key in self.vars:
                self.vars[key].set(val)


class OWLDisplay:
    """Tkinter display window for the OWL detection pipeline.

    Responsibilities:
    - Show the detection output video feed
    - Provide threshold sliders (replacing cv2 trackbars)
    - Handle keyboard shortcuts (s=save, r=record, Escape=quit)

    The GUI runs on the *main* thread via ``root.mainloop()``.
    Frame updates are pushed from the detection loop running on a
    background thread via ``push_frame()``, and polled by a
    ``root.after()`` callback.
    """

    # Map from slider attribute names to the display names used by MQTT
    _SLIDER_TO_TRACKBAR = {
        'exg_min': 'ExG-Min',
        'exg_max': 'ExG-Max',
        'hue_min': 'Hue-Min',
        'hue_max': 'Hue-Max',
        'saturation_min': 'Sat-Min',
        'saturation_max': 'Sat-Max',
        'brightness_min': 'Bright-Min',
        'brightness_max': 'Bright-Max',
    }

    _TRACKBAR_TO_SLIDER = {v: k for k, v in _SLIDER_TO_TRACKBAR.items()}

    def __init__(self, initial_values, algorithm='exhsv',
                 on_save=None, on_record=None, on_quit=None):
        self.root = tk.Tk()
        self.root.title('OWL Detection Display')

        self._on_save = on_save
        self._on_record = on_record
        self._on_quit = on_quit

        self._latest_frame = None   # written from bg thread
        self._photo = None          # prevent GC of PhotoImage

        self._build_ui(initial_values, algorithm)
        self._bind_keys()

    # ---- UI construction ----

    def _build_ui(self, initial_values, algorithm):
        # Status bar
        self._status_var = tk.StringVar(value=f'OWL-gorithm: {algorithm}')
        status_bar = ttk.Label(self.root, textvariable=self._status_var, relief=tk.SUNKEN)
        status_bar.pack(side=tk.TOP, fill=tk.X)

        # Button bar at bottom
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=5)
        ttk.Button(btn_frame, text='Save (S)', command=self._save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='Record (R)', command=self._record).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='Quit (Esc)', command=self._quit).pack(side=tk.RIGHT, padx=5)

        # Main content area
        main = ttk.Frame(self.root)
        main.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Video feed on the left
        video_frame = ttk.LabelFrame(main, text='Detection Output')
        video_frame.pack(side=tk.LEFT, padx=(0, 5))
        self._video_label = tk.Label(video_frame, width=600, height=450)
        self._video_label.pack(padx=5, pady=5)

        # Threshold sliders on the right
        self.threshold_panel = ThresholdPanel(main, initial_values)
        self.threshold_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(5, 0))

    def _bind_keys(self):
        self.root.bind_all('<s>', lambda e: self._save())
        self.root.bind_all('<r>', lambda e: self._record())
        self.root.bind_all('<Escape>', lambda e: self._quit())
        self.root.protocol('WM_DELETE_WINDOW', self._quit)

    # ---- callbacks ----

    def _save(self):
        if self._on_save:
            self._on_save()

    def _record(self):
        if self._on_record:
            self._on_record()

    def _quit(self):
        if self._on_quit:
            self._on_quit()

    # ---- public API (called from background thread) ----

    def push_frame(self, frame):
        """Store the latest BGR frame for display.  Thread-safe (atomic ref)."""
        self._latest_frame = frame

    def get_slider_values(self):
        """Read current slider positions. Call from main thread only."""
        return self.threshold_panel.get_values()

    def apply_pending_slider_updates(self, pending):
        """Apply a dict of queued MQTT/controller updates to sliders.

        *pending* maps display names (e.g. ``'ExG-Min'``) to int values.
        """
        translated = {}
        for display_name, value in pending.items():
            attr = self._TRACKBAR_TO_SLIDER.get(display_name)
            if attr:
                translated[attr] = value
        if translated:
            self.threshold_panel.set_values(translated)

    def set_algorithm_label(self, name):
        self._status_var.set(f'OWL-gorithm: {name}')

    # ---- main-thread update loop ----

    def start_update_loop(self, interval_ms=33):
        """Schedule periodic frame redraws.  Call once before ``mainloop``."""
        self._update_interval = interval_ms
        self._update_gui()

    def _update_gui(self):
        frame = self._latest_frame
        if frame is not None:
            h, w = frame.shape[:2]
            display = cv2.resize(frame, (600, int(h * 600 / w))) if w != 600 else frame
            rgb = cv2.cvtColor(display, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            self._photo = ImageTk.PhotoImage(image=img)
            self._video_label.configure(image=self._photo)
        self.root.after(self._update_interval, self._update_gui)

    def mainloop(self):
        """Enter the tkinter event loop (blocks the calling thread)."""
        self.root.mainloop()

    def destroy(self):
        """Tear down the window.  Safe to call from any thread."""
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            pass
