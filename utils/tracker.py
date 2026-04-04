"""
Weed tracking utilities for temporal smoothing of GoG/GoG-hybrid detections.

Sits on top of Ultralytics built-in ByteTrack (via model.track()) and adds:
  - ClassSmoother: majority-vote class assignment per tracked object
  - CropMaskStabilizer: persist crop mask positions through detection dropouts

ByteTrack provides stable track IDs and Kalman-predicted positions but
overwrites track.cls on every match (no class smoothing). These wrappers
fill that gap.

Usage:
    smoother = ClassSmoother(window=5)
    stabilizer = CropMaskStabilizer(max_age=3)

    # After model.track() returns results:
    smoothed_classes = smoother.update(track_ids, class_ids, confidences)
    stabilizer.update(crop_track_ids, crop_boxes)
    mask = stabilizer.build_stabilized_mask(frame_shape)
"""

from collections import Counter, deque

import cv2
import numpy as np


class ClassSmoother:
    """Majority-vote class assignment per tracked object.

    ByteTrack overwrites track.cls on every match. This wrapper maintains
    a sliding window of class observations per track_id and returns the
    majority-vote class instead.

    Args:
        window: Number of frames of class history to keep per track.
    """

    def __init__(self, window=5):
        self.window = window
        self._history = {}   # {track_id: deque of class_id}
        self._last_seen = {} # {track_id: frame_count} for stale pruning

    def update(self, track_ids, class_ids, confidences, frame_count=0):
        """Record class observations and return smoothed class per track_id.

        Args:
            track_ids: list of int track IDs from ByteTrack
            class_ids: list of int class IDs per detection
            confidences: list of float confidence per detection (unused for now,
                         reserved for confidence-weighted voting)
            frame_count: current frame number for stale pruning

        Returns:
            dict: {track_id: smoothed_class_id}
        """
        seen = set()
        for tid, cls in zip(track_ids, class_ids):
            tid = int(tid)
            cls = int(cls)
            seen.add(tid)

            if tid not in self._history:
                self._history[tid] = deque(maxlen=self.window)
            self._history[tid].append(cls)
            self._last_seen[tid] = frame_count

        # Prune tracks not seen for 2x window frames
        if frame_count > 0:
            stale_threshold = frame_count - (self.window * 2)
            stale = [k for k, v in self._last_seen.items()
                     if v < stale_threshold and k not in seen]
            for k in stale:
                del self._history[k]
                del self._last_seen[k]

        return {tid: self._majority(tid) for tid in seen}

    def get_class(self, track_id):
        """Return majority-vote class_id for a specific track."""
        return self._majority(int(track_id))

    def _majority(self, track_id):
        """Compute majority-vote class from history."""
        hist = self._history.get(track_id)
        if not hist:
            return -1
        counts = Counter(hist)
        # most_common(1) returns [(class_id, count)]; ties broken by insertion order
        return counts.most_common(1)[0][0]

    def reset(self):
        """Clear all history."""
        self._history.clear()
        self._last_seen.clear()


class CropMaskStabilizer:
    """Persist crop detection positions for stable hybrid-mode masking.

    When YOLO drops a crop detection for 1-2 frames, this class keeps
    the crop's last-known position in the mask, preventing weed detections
    from leaking through holes in the crop mask.

    Supports both bounding-box and contour-based masks (segmentation models).

    Args:
        max_age: Frames to persist a crop position after detection drops.
    """

    def __init__(self, max_age=3):
        self.max_age = max_age
        self._tracks = {}  # {track_id: {'box': [...], 'contour': ndarray|None, 'age': int}}

    def update(self, track_ids, boxes, contours=None):
        """Update with current frame's tracked crop detections.

        Args:
            track_ids: list of int track IDs for crop detections
            boxes: list of [x, y, w, h] bounding boxes (xyxy also accepted)
            contours: optional list of numpy contour arrays (for segmentation models)
        """
        seen = set()
        for i, (tid, box) in enumerate(zip(track_ids, boxes)):
            tid = int(tid)
            seen.add(tid)
            contour = contours[i] if contours and i < len(contours) else None
            self._tracks[tid] = {
                'box': list(box),
                'contour': contour,
                'age': 0,
            }

        # Age unseen tracks, prune expired
        to_remove = []
        for tid in self._tracks:
            if tid not in seen:
                self._tracks[tid]['age'] += 1
                if self._tracks[tid]['age'] > self.max_age:
                    to_remove.append(tid)
        for tid in to_remove:
            del self._tracks[tid]

    def get_all_crop_regions(self):
        """Return ALL crop regions (detected + persisted).

        Returns:
            list of dicts with 'box', 'contour', 'age' keys.
        """
        return list(self._tracks.values())

    def build_stabilized_mask(self, shape):
        """Build crop mask including persisted positions.

        Args:
            shape: (height, width) or (height, width, channels) tuple

        Returns:
            numpy uint8 mask (255 = crop, 0 = background)
        """
        h, w = shape[0], shape[1]
        mask = np.zeros((h, w), dtype=np.uint8)

        for info in self._tracks.values():
            if info['contour'] is not None:
                cv2.drawContours(mask, [info['contour']], -1, 255, -1)
            else:
                box = info['box']
                # Boxes are always xyxy format (from YOLO box.xyxy[0])
                if len(box) >= 4:
                    x1, y1 = int(box[0]), int(box[1])
                    x2, y2 = int(box[2]), int(box[3])

                    # Clamp to frame
                    x1, y1 = max(0, x1), max(0, y1)
                    x2, y2 = min(w, x2), min(h, y2)

                    if x2 > x1 and y2 > y1:
                        mask[y1:y2, x1:x2] = 255

        return mask

    @property
    def active_count(self):
        """Number of crop regions currently being tracked (including persisted)."""
        return len(self._tracks)

    @property
    def persisted_count(self):
        """Number of crop regions being persisted (not detected this frame)."""
        return sum(1 for info in self._tracks.values() if info['age'] > 0)

    def reset(self):
        """Clear all tracked crop positions."""
        self._tracks.clear()
