# Plan: Replace cv2 Display Windows with tkinter Application

## Problem

When `--show-display` is used, OWL creates multiple disconnected windows:
1. **"Adjust Detection Thresholds"** — cv2 named window with 8 trackbars (owl.py:145)
2. **"Detection Output"** — cv2 imshow window with video feed (owl.py:794)
3. **"gndvi"** — accidental third window from stray imshow (algorithms.py:213)
4. **Terminal relay boxes** — ANSI-colored blocks printed to stdout (vis_manager.py)

These can't be positioned, resized, or styled. They clutter the desktop and the
terminal. Replace everything with a single tkinter application window.

## Existing Precedent

`desktop/focus_gui.py` already uses the exact pattern we need:
- Camera thread produces frames → writes to `self.frame`
- `root.after(33, self.update_gui)` polls on the main thread
- `cv2.cvtColor(BGR→RGB)` → `PIL.Image.fromarray()` → `ImageTk.PhotoImage`
- tkinter widgets (Labels, Buttons, Scales) for all controls

## Architecture

### Current (cv2-based)
```
Main thread:  Owl.__init__() → hoot() while-loop
              ├── cv2.getTrackbarPos() × 8     (read sliders)
              ├── weed_detector.inference()     (process frame)
              ├── cv2.imshow()                  (display)
              └── cv2.waitKey()                 (keyboard input)

Camera thread:     captures frames continuously
Relay threads:     control nozzles, update relay_vis.status_list
MQTT threads:      heartbeat, monitor, queue _pending_trackbar_updates
Controller proc:   hardware buttons (separate process via fork)
```

### Proposed (tkinter-based)
```
Main thread:  Owl.__init__() → hoot()
              ├── if show_display: create OWLDisplay, spawn detection thread,
              │   call display.run() (blocks on tkinter mainloop)
              └── else: run _detection_loop() directly (no GUI, unchanged)

Detection thread (new, only when show_display=True):
              while running:
              ├── read frame from camera
              ├── read threshold values from owl attributes (GIL-safe)
              ├── weed_detector.inference()
              ├── put result frame into display._frame_queue
              └── relay actuation, FPS tracking, etc. (all non-GUI work)

GUI update (root.after, ~30ms):
              ├── drain _frame_queue → convert BGR→RGB→PIL→ImageTk
              ├── update video label
              ├── drain _pending_slider_updates → sync Scale widgets
              ├── read relay_vis.status_list → update nozzle indicators
              └── update FPS, algorithm, recording status labels

Camera thread:     unchanged
Relay threads:     unchanged (relay_vis in silent mode, still tracks status_list)
MQTT threads:      unchanged (still writes _pending_trackbar_updates)
Controller proc:   unchanged (cv2.setTrackbarPos calls become no-ops — see note)
```

**Key insight**: When `show_display=False`, nothing changes at all. The detection
loop runs on the main thread exactly as it does today. tkinter is only imported
and used when `show_display=True`.

## Files to Create/Modify

### 1. NEW: `utils/display_manager.py` — OWLDisplay class

The main new file. Encapsulates all tkinter GUI logic.

```python
class OWLDisplay:
    def __init__(self, owl_instance):
        """
        owl_instance: reference to the Owl object.
        Reads: owl.exg_min, owl.relay_vis, owl.record_video, owl.algorithm, etc.
        Writes: owl.exg_min (etc.) when user drags sliders.
        """

    def _build_ui(self):
        """Create all tkinter widgets."""

    def put_frame(self, frame, algorithm, avg_fps):
        """Called from detection thread. Queues frame for display."""

    def _update(self):
        """Scheduled via root.after(33). Pulls from queue, refreshes GUI."""

    def _on_slider_change(self, param_name, value):
        """Scale command callback. Writes to owl.exg_min etc."""

    def _on_key_press(self, event):
        """Handles S (save), R (record), Escape (quit)."""

    def request_stop(self):
        """Signal detection thread to stop, then destroy root."""

    def run(self):
        """Start update loop, enter mainloop. Blocks until closed."""
```

**Window layout** (single window, stacked panels):

```
┌──────────────────────────────────────────────────────┐
│  OWL — [algorithm_name]                       [−][×] │
├──────────────────────────────────────────────────────┤
│  ExG ═══●═══  Hue ═══●═══  Sat ═══●═══  Bri ═══●═══│
│   25─200       39─83        50─220       60─190      │
│  (min/max pairs as paired horizontal Scale widgets)  │
├──────────────────────────────────────────────────────┤
│                                                      │
│                    Video Feed                        │
│                   (640 × 480)                        │
│             PIL ImageTk.PhotoImage                   │
│                on a tk.Label                         │
│                                                      │
├──────────────────────────────────────────────────────┤
│  EXHSV  32 FPS  │  ●1 ●2 ●3 ●4  │  DET ● │ REC ●  │
│  [S] Save   [R] Record   [Esc] Quit                 │
└──────────────────────────────────────────────────────┘
```

**Slider layout detail**: 8 sliders (4 min/max pairs) arranged as paired
horizontal `ttk.Scale` widgets. Each pair shares a row with a label showing
the parameter name and current values. This mirrors the cv2 trackbar UX but
is more compact.

**Nozzle indicators**: `tk.Canvas` circles, colored green (active) or gray
(inactive). Read from `relay_vis.status_list` each update cycle.

**Status bar**: `ttk.Frame` with `ttk.Label` widgets for algorithm name,
FPS counter, detection/recording indicators.

**Frame queue**: `queue.Queue(maxsize=2)` — detection thread puts frames,
GUI thread gets them. `maxsize=2` means at most 1 frame of latency; if the
GUI falls behind, old frames are dropped (put_nowait + discard on Full).

**Frame conversion pipeline** (per frame, ~2-3ms on Pi 4):
```python
frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
img = Image.fromarray(frame_rgb)
img = img.resize((display_w, display_h), Image.NEAREST)  # NEAREST for speed
self._photo = ImageTk.PhotoImage(image=img)
self._video_label.configure(image=self._photo)
```

### 2. MODIFY: `owl.py` — Restructure hoot()

**Changes to `__init__`**:
- Remove cv2.namedWindow, cv2.createTrackbar block (lines 142-153)
- Keep `self.window_name` as an attribute for backward compat with
  input_manager.py (set to None when show_display=False)
- Add `self.display = None`

**Changes to `hoot()`**:
- Extract the while-loop body into a new `_detection_loop()` method
- `hoot()` becomes the orchestrator:

```python
def hoot(self):
    # ... existing setup (camera, detector, etc.) ...

    if self.show_display:
        from utils.display_manager import OWLDisplay
        self.display = OWLDisplay(self)

        # Set relay_vis to silent mode (GUI draws nozzles instead of terminal)
        self.relay_vis = self.relay_controller.relay_vis
        self.relay_vis.silent = True
        self.relay_controller.vis = True

        # Run detection in background thread
        self._stop_detection = threading.Event()
        detection_thread = threading.Thread(
            target=self._detection_loop, daemon=True)
        detection_thread.start()

        # Block on tkinter mainloop (main thread)
        self.display.run()

        # After mainloop exits (user closed window)
        self._stop_detection.set()
        detection_thread.join(timeout=2.0)
    else:
        # No display — run detection loop directly on main thread
        self._detection_loop()
```

**`_detection_loop()` method** — extracted from current hoot() while-loop:
- Same frame read → detect → actuate → FPS logic
- Remove all cv2 display calls (namedWindow, getTrackbarPos, imshow, waitKey)
- Instead of cv2.getTrackbarPos: read self.exg_min etc. directly (set by
  OWLDisplay slider callbacks or MQTT)
- Instead of cv2.imshow: `self.display.put_frame(image_out, algorithm, fps)`
- Instead of cv2.waitKey: keyboard handled by tkinter bindings
- Loop condition: `while not self._stop_detection.is_set()` (when display
  active) or `while True` with try/except KeyboardInterrupt (when headless)
- Drain `_pending_slider_updates` handled by GUI thread, not detection thread

**Threshold flow**:
- Current: cv2 trackbar → getTrackbarPos → self.exg_min → detector
- New: tkinter Scale drag → command callback → self.exg_min → detector
- MQTT: writes self.exg_min + queues `_pending_slider_updates` → GUI drains
  queue → updates Scale widget positions (visual sync only, values already set)

### 3. MODIFY: `utils/algorithms.py` — Fix gndvi bug

- **Line 213**: Remove `cv2.imshow('gndvi', image_out)` — stray debug call
  that unconditionally creates a third window.

### 4. MODIFY: `utils/vis_manager.py` — Add silent mode

Add `silent` parameter to `RelayVis`:
- When `silent=True`: `setup()` and `update()` skip terminal printing but
  still maintain `status_list`
- When `silent=False`: unchanged behavior (terminal ANSI boxes)

This lets the GUI read nozzle states from `relay_vis.status_list` without
terminal noise when `--show-display` is active. When running headless (no
display), the terminal visualization works as before.

### 5. MODIFY: `utils/input_manager.py` — Replace cv2.setTrackbarPos

Lines 241-249 call `cv2.setTrackbarPos` from the controller process. This is
**already broken** — the controller runs as a `multiprocessing.Process` (fork),
so cv2 calls go to the child's window registry (which has no windows). The
values never reach the parent's trackbars.

**Change**: Replace the cv2.setTrackbarPos block with a write to
`_pending_slider_updates` (same pattern MQTT uses). This is also a no-op
across process boundaries, but it's consistent and removes the cv2 dependency
from input_manager.

Alternatively, just remove the block entirely — the owl attributes are already
set on lines 230-238 (in the child process copy), and the cv2 calls have no
effect. Either approach works; removing is simpler.

### 6. MODIFY: `utils/mqtt_manager.py` — Rename dict key (cosmetic)

Rename `_pending_trackbar_updates` → `_pending_slider_updates` across:
- `owl.py:415` (init)
- `owl.py:538-540` (drain loop — moves to display_manager)
- `mqtt_manager.py:879, 943` (write)
- `tests/conftest.py:148` (mock setup)
- `tests/test_mqtt_handlers.py:231, 241, 251, 235, 245, 258-261` (test assertions)

This is optional but clarifies that sliders are no longer cv2 trackbars.

### 7. MODIFY: `tests/conftest.py` and `tests/test_mqtt_handlers.py`

Update test references from `_pending_trackbar_updates` to
`_pending_slider_updates` (if we do the rename). Tests themselves don't need
structural changes — they test MQTT → owl attribute flow, which is unchanged.

## Thread Safety Analysis

| Data | Writer(s) | Reader(s) | Safety |
|------|-----------|-----------|--------|
| `owl.exg_min` etc. | GUI slider callback, MQTT thread | Detection thread | GIL-safe (simple int assignment) |
| `_pending_slider_updates` | MQTT thread | GUI thread (drain) | Dict swap pattern (already used) |
| `relay_vis.status_list` | Relay worker threads | GUI thread (read) | GIL-safe (bool writes are atomic) |
| `display._frame_queue` | Detection thread (put) | GUI thread (get) | `queue.Queue` (thread-safe by design) |
| `owl.record_video` | GUI key binding | Detection thread, GUI status | GIL-safe (bool) |
| `owl._stop_detection` | GUI (on close) | Detection thread | `threading.Event` (thread-safe) |

No additional locks needed. The existing `_pending_trackbar_updates` dict-swap
pattern is the only non-trivial synchronization, and it already works correctly.

## Performance Considerations

**Frame conversion overhead** (BGR → RGB → PIL → ImageTk):
- `cv2.cvtColor`: ~0.3ms for 640×480 on Pi 4
- `Image.fromarray`: ~0.1ms (numpy array → PIL, no copy with matching layout)
- `Image.resize` with NEAREST: ~0.2ms (if needed)
- `ImageTk.PhotoImage`: ~1.5ms (creates Tk-compatible bitmap)
- **Total: ~2.1ms per frame** — acceptable at 30fps (33ms budget)

**Comparison with cv2.imshow**: cv2.imshow is ~0.5ms (direct X11/Wayland blit).
The tkinter path is ~1.6ms slower. At 30fps this is 5% of the frame budget.
Detection itself takes 10-50ms, so this overhead is negligible.

**GUI update rate**: `root.after(33)` = ~30fps target. If detection runs at
15fps (typical on Pi 4 with algorithm processing), the GUI will display at
15fps (limited by frame production, not GUI refresh). This matches the current
behavior.

**Memory**: One extra frame copy in the queue (640×480×3 = ~900KB). Negligible.

## Dependencies

- **tkinter**: Pre-installed on Raspberry Pi OS (`python3-tk`). Already used
  by `focus_gui.py`. Standard library on most Linux distros.
- **Pillow (PIL)**: Already in requirements.txt (`Pillow>=11.2.1,<11.3.0`).
  Provides `Image` and `ImageTk`.
- **No new dependencies needed.**

## Implementation Order

1. Fix `algorithms.py:213` — remove stray imshow (trivial, independent)
2. Add silent mode to `vis_manager.py` RelayVis class
3. Create `utils/display_manager.py` with OWLDisplay class
4. Modify `owl.py`:
   a. Remove cv2 window/trackbar setup from `__init__`
   b. Extract detection loop into `_detection_loop()`
   c. Restructure `hoot()` to branch on show_display
5. Update `input_manager.py` — remove cv2.setTrackbarPos calls
6. Update `mqtt_manager.py` — rename dict key (optional)
7. Update tests — conftest.py and test_mqtt_handlers.py references
8. Manual testing on Pi with `--show-display` flag

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| tkinter not installed on target Pi | Low (comes with python3-tk) | Add try/import with helpful error message |
| Frame conversion too slow on Pi 3 | Medium | Use NEAREST resampling; can skip resize if frame already at display size |
| Detection thread timing changes | Low | No change to detection logic; just runs in thread instead of main |
| MQTT/controller updates miss slider sync | Low | Same dict-swap pattern; already tested |
| Keyboard shortcuts feel different | Low | tkinter bind_all is more responsive than cv2.waitKey(1) |

## Known Issues (Out of Scope)

1. **Controller process cv2 calls are already broken**: `input_manager.py:242-249`
   calls `cv2.setTrackbarPos` from a forked child process. This has no effect on
   the parent's windows. Pre-existing issue, not introduced by this change.

2. **No cv2.destroyAllWindows in cleanup**: The current code never calls
   `cv2.destroyAllWindows()`. With tkinter this is handled by `root.destroy()`.

3. **Video recording toggle from GUI**: Currently uses cv2.waitKey('r'). With
   tkinter, uses `bind_all('<KeyPress-r>')`. Functionally identical but the
   recording still writes raw frames from the detection thread (correct behavior —
   recordings should not include GUI overlays).
