#!/usr/bin/env python
"""Green-on-Green weed detection using Ultralytics YOLO.

Supports both detection and segmentation models in NCNN or PyTorch format.
NCNN is the recommended format for Raspberry Pi (fastest on ARM CPU).

Hybrid mode: YOLO identifies crop at low resolution, then GreenOnBrown
runs ExHSV at full resolution on non-crop areas to find weeds.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

logger = logging.getLogger(__name__)


class GreenOnGreen:
    def __init__(self, model_path='models', confidence=0.5, detect_classes=None,
                 hybrid_mode=False, inference_resolution=320, crop_buffer_px=20,
                 tracking_enabled=False, crop_stabilizer=None,
                 detection_persist_frames=0):
        """
        Args:
            model_path: Path to NCNN model dir, .pt file, or parent dir containing models.
            confidence: Detection confidence threshold (0.0-1.0).
            detect_classes: List of class names to detect (None = all).
            hybrid_mode: If True, use YOLO for crop masking + GreenOnBrown for weed detection.
            inference_resolution: YOLO input resolution for hybrid mode (lower = faster).
            crop_buffer_px: Dilation buffer around detected crop in pixels (hybrid mode).
        """
        if YOLO is None:
            raise ImportError(
                "ultralytics is required for Green-on-Green detection but is not installed. "
                "Install with: pip install -r requirements-gog.txt"
            )

        self.model_path = Path(model_path)
        self.confidence = confidence
        self.hybrid_mode = hybrid_mode
        self.inference_resolution = inference_resolution
        self.crop_buffer_px = crop_buffer_px
        self._model_filename = ''
        self.model = self._load_model()
        self.task = self.model.task  # 'detect' or 'segment'
        self.detection_mask = None  # Combined binary mask, set after inference (seg only)

        # Map class names to IDs after model is loaded
        self._detect_class_ids = self._resolve_classes(detect_classes)

        # Tracking state (ByteTrack via model.track())
        self.tracking_enabled = tracking_enabled
        self._crop_stabilizer = crop_stabilizer
        self.detection_persist_frames = detection_persist_frames
        # Raw detection attributes (populated each frame when tracking enabled)
        self.last_track_ids = []
        self.last_raw_boxes = []
        self.last_class_ids = []
        self.last_confidences = []

        # Hybrid mode: create internal GreenOnBrown, dilation kernel, thread pool
        self._gob = None
        self._dilate_kernel = None
        self._executor = None
        if self.hybrid_mode:
            from utils.greenonbrown import GreenOnBrown
            self._gob = GreenOnBrown(algorithm='exhsv')
            self._dilate_kernel = self._build_dilate_kernel(crop_buffer_px)
            self._executor = ThreadPoolExecutor(max_workers=1)
            logger.info(f'Hybrid mode enabled: YOLO crop mask + ExHSV weed detection, '
                        f'buffer={crop_buffer_px}px, imgsz={inference_resolution}')

        logger.info(f'GreenOnGreen initialized: task={self.task}, '
                     f'classes={list(self.model.names.values())}, '
                     f'filtering={detect_classes or "all"}')

    @staticmethod
    def _infer_task(name, model_path=None):
        """Infer YOLO task from filename or metadata.yaml."""
        if '-seg' in name.lower() or '_seg' in name.lower():
            return 'segment'
        # Fall back to metadata.yaml inside NCNN model directories
        if model_path is not None:
            meta = Path(model_path) / 'metadata.yaml'
            if meta.exists():
                try:
                    with open(meta) as f:
                        for line in f:
                            if line.startswith('task:'):
                                task = line.split(':', 1)[1].strip()
                                if task:
                                    logger.info(f'Task "{task}" read from metadata.yaml')
                                    return task
                except Exception:
                    pass
        return None

    def _load_model(self):
        """Load YOLO model -- supports NCNN dirs and .pt files."""
        if self.model_path.is_dir():
            # Check if this IS an NCNN model dir (has .param + .bin)
            if list(self.model_path.glob('*.param')):
                logger.info(f'Using NCNN model: {self.model_path.name}')
                self._model_filename = self.model_path.name
                task = self._infer_task(self.model_path.name, self.model_path)
                return YOLO(str(self.model_path), task=task)

            # Search for NCNN subdirs first, then .pt files
            ncnn_dirs = [d for d in self.model_path.iterdir()
                         if d.is_dir() and list(d.glob('*.param'))]
            if ncnn_dirs:
                selected = ncnn_dirs[0]
                logger.info(f'Using NCNN model: {selected.name}')
                self._model_filename = selected.name
                task = self._infer_task(selected.name, selected)
                return YOLO(str(selected), task=task)

            pt_files = list(self.model_path.glob('*.pt'))
            if pt_files:
                logger.info(f'Using PyTorch model: {pt_files[0].name}')
                self._model_filename = pt_files[0].name
                task = self._infer_task(pt_files[0].name)
                return YOLO(str(pt_files[0]), task=task)

            raise FileNotFoundError(f'No YOLO models found in {self.model_path}')

        elif self.model_path.exists():
            logger.info(f'Using model: {self.model_path.name}')
            self._model_filename = self.model_path.name
            task = self._infer_task(self.model_path.name)
            return YOLO(str(self.model_path), task=task)

        raise FileNotFoundError(f'Model path does not exist: {self.model_path}')

    def _resolve_classes(self, class_names):
        """Map class names to model class IDs. Returns None if all classes."""
        if not class_names:
            return None

        name_to_id = {v.lower(): k for k, v in self.model.names.items()}
        ids = []
        for name in class_names:
            name_lower = name.strip().lower()
            if name_lower in name_to_id:
                ids.append(name_to_id[name_lower])
            else:
                logger.warning(f"Class '{name}' not found in model. "
                               f"Available: {list(self.model.names.values())}")

        return ids if ids else None

    def _build_dilate_kernel(self, px):
        """Build elliptical dilation kernel for crop buffer."""
        if px <= 0:
            return None
        size = 2 * px + 1
        return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))

    def set_crop_buffer(self, px):
        """Update crop buffer and rebuild kernel (only if value changed)."""
        if px == self.crop_buffer_px:
            return
        self.crop_buffer_px = px
        self._dilate_kernel = self._build_dilate_kernel(px)
        logger.info(f'Crop buffer updated to {px}px')

    @staticmethod
    def _build_crop_mask(results, h, w):
        """Build crop mask from a single frame's YOLO results (no stabilization)."""
        mask = np.zeros((h, w), dtype=np.uint8)
        for result in results:
            if result.masks is not None:
                contours_full = [c.astype(np.int32).reshape(-1, 1, 2)
                                 for c in result.masks.xy]
                cv2.drawContours(mask, contours_full, -1, 255, -1)
            elif len(result.boxes):
                for box in result.boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    cv2.rectangle(mask, (x1, y1), (x2, y2), 255, -1)
        return mask

    # Track stability presets — map user-facing names to ByteTrack params
    TRACK_STABILITY_PRESETS = {
        'low': {
            'track_high_thresh': 0.3,
            'track_low_thresh': 0.15,
            'new_track_thresh': 0.3,
            'track_buffer': 30,
            'match_thresh': 0.8,
        },
        'medium': {
            'track_high_thresh': 0.2,
            'track_low_thresh': 0.05,
            'new_track_thresh': 0.2,
            'track_buffer': 60,
            'match_thresh': 0.7,
        },
        'high': {
            'track_high_thresh': 0.15,
            'track_low_thresh': 0.05,
            'new_track_thresh': 0.15,
            'track_buffer': 90,
            'match_thresh': 0.6,
        },
    }

    def update_tracker_params(self, stability_level):
        """Update ByteTrack params at runtime from a stability preset.

        Args:
            stability_level: 'low', 'medium', or 'high'
        """
        preset = self.TRACK_STABILITY_PRESETS.get(stability_level)
        if not preset:
            logger.warning(f"Unknown track stability level: {stability_level}")
            return

        if not hasattr(self.model, 'predictor') or self.model.predictor is None:
            logger.info(f"Track stability set to '{stability_level}' (will apply on next track call)")
            return

        for tracker in getattr(self.model.predictor, 'trackers', []):
            for key, value in preset.items():
                if hasattr(tracker, key):
                    setattr(tracker, key, value)
                # track_buffer sets max_time_lost in BYTETracker
                if key == 'track_buffer' and hasattr(tracker, 'max_time_lost'):
                    tracker.max_time_lost = value
            tracker.reset()

        logger.info(f"Track stability updated to '{stability_level}': {preset}")

    def update_tracker_params_direct(self, params):
        """Update ByteTrack params at runtime from a dict of individual values.

        Args:
            params: dict with keys like 'track_high_thresh', 'match_thresh', etc.
        """
        if not hasattr(self.model, 'predictor') or self.model.predictor is None:
            logger.info(f"Tracker params queued (will apply on next track call): {params}")
            return

        for tracker in getattr(self.model.predictor, 'trackers', []):
            for key, value in params.items():
                if hasattr(tracker, key):
                    setattr(tracker, key, value)
                if key == 'track_buffer' and hasattr(tracker, 'max_time_lost'):
                    tracker.max_time_lost = int(value)
            tracker.reset()

        logger.info(f"Tracker params updated directly: {params}")

    def get_lost_tracks(self, max_age=None):
        """Read Kalman-predicted positions for lost tracks from ByteTrack.

        ByteTrack internally maintains lost_stracks with Kalman-predicted
        positions updated every frame. This exposes them for detection
        persistence — boxes continue through YOLO flicker.

        Args:
            max_age: Max frames since last match (None = no limit).

        Returns:
            List of dicts: [{'track_id', 'xyxy', 'cls', 'score', 'age'}]
        """
        if not (hasattr(self.model, 'predictor') and self.model.predictor):
            return []
        trackers = getattr(self.model.predictor, 'trackers', [])
        if not trackers:
            return []

        tracker = trackers[0]  # single-stream
        lost = []
        try:
            for strack in tracker.lost_stracks:
                age = tracker.frame_id - strack.end_frame
                if max_age is not None and age > max_age:
                    continue
                xyxy = strack.xyxy
                lost.append({
                    'track_id': strack.track_id,
                    'xyxy': xyxy,
                    'cls': int(strack.cls),
                    'score': float(strack.score),
                    'age': age,
                })
        except Exception:
            # Defensive — Ultralytics internal API may change
            pass
        return lost

    def reset_tracker(self):
        """Reset ByteTrack state and tracking layers. Call when detection toggled off."""
        self.last_track_ids = []
        self.last_raw_boxes = []
        self.last_class_ids = []
        self.last_confidences = []
        if self._crop_stabilizer:
            self._crop_stabilizer.reset()
        # Reset ByteTrack internal state
        if hasattr(self.model, 'predictor') and self.model.predictor is not None:
            for tracker in getattr(self.model.predictor, 'trackers', []):
                tracker.reset()

    def update_detect_classes(self, class_names):
        """Hot-update detect_classes filter without reloading the model."""
        self._detect_class_ids = self._resolve_classes(class_names)
        logger.info(f'detect_classes updated: {class_names} -> IDs {self._detect_class_ids}')

    @property
    def class_names(self):
        """Return dict of {id: name} from loaded model."""
        return self.model.names

    def inference(self, image, confidence=0.5, show_display=False,
                  filter_id=None, label='WEED', build_mask=False,
                  # Hybrid params (ignored in non-hybrid mode):
                  exg_min=30, exg_max=250, hue_min=30, hue_max=90,
                  saturation_min=30, saturation_max=255,
                  brightness_min=5, brightness_max=200,
                  min_detection_area=1, invert_hue=False):
        """
        Run YOLO inference. Returns same tuple as GreenOnBrown.

        In hybrid mode, runs YOLO to find crop, masks it out, then runs
        GreenOnBrown ExHSV on the remaining area to find weeds.

        Args:
            image: BGR numpy array.
            confidence: Detection confidence threshold.
            show_display: If True, return annotated image copy.
            filter_id: Unused, kept for API compatibility.
            label: Fallback label for display.
            build_mask: If True and model is segmentation, build self.detection_mask
                        for zone-based actuation. Skipped when False to save CPU.
            exg_min..invert_hue: GreenOnBrown params, only used in hybrid mode.

        Returns:
            (contours, boxes, weed_centres, image_out)
            - contours: mask polygons (segmentation) or None (detection)
            - boxes: list of [x, y, w, h]
            - weed_centres: list of [cx, cy]
            - image_out: annotated image if show_display, else original image
        """
        if self.hybrid_mode:
            return self._hybrid_inference(
                image, confidence, show_display,
                exg_min=exg_min, exg_max=exg_max,
                hue_min=hue_min, hue_max=hue_max,
                saturation_min=saturation_min, saturation_max=saturation_max,
                brightness_min=brightness_min, brightness_max=brightness_max,
                min_detection_area=min_detection_area, invert_hue=invert_hue
            )

        # --- Pure GoG mode ---
        self.detection_mask = None  # Reset each frame

        if self.tracking_enabled:
            # Track ALL classes — ClassSmoother in owl.py does class filtering
            results = self.model.track(
                source=image,
                conf=confidence,
                persist=True,
                tracker='config/bytetrack_owl.yaml',
                verbose=False,
                device='cpu'
            )
        else:
            results = self.model.predict(
                source=image,
                conf=confidence,
                classes=self._detect_class_ids,
                verbose=False,
                device='cpu'
            )

        boxes = []
        weed_centres = []
        contours = None
        track_ids = []
        raw_class_ids = []
        raw_confidences = []

        for result in results:
            has_ids = (self.tracking_enabled
                       and result.boxes.id is not None
                       and len(result.boxes.id) > 0)

            # Process bounding boxes (works for both detect and segment)
            for i, box in enumerate(result.boxes):
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                w, h = x2 - x1, y2 - y1
                boxes.append([x1, y1, w, h])
                weed_centres.append([x1 + w // 2, y1 + h // 2])

                if has_ids:
                    track_ids.append(int(result.boxes.id[i]))
                    raw_class_ids.append(int(box.cls[0]))
                    raw_confidences.append(float(box.conf[0]))

            # Extract mask contours for segmentation models
            if result.masks is not None:
                contours = [c.astype(np.int32).reshape(-1, 1, 2)
                            for c in result.masks.xy]

                # Only build combined binary mask when zone actuation needs it
                if build_mask:
                    self.detection_mask = np.zeros(image.shape[:2], dtype=np.uint8)
                    cv2.drawContours(self.detection_mask, contours, -1, 255, -1)

        # Store raw tracking data for owl.py's ClassSmoother
        self.last_track_ids = track_ids
        self.last_raw_boxes = list(boxes)
        self.last_class_ids = raw_class_ids
        self.last_confidences = raw_confidences

        if show_display:
            image_out = image.copy()

            # Draw segmentation masks if available
            if contours is not None:
                overlay = image_out.copy()
                for contour in contours:
                    cv2.drawContours(overlay, [contour], -1, (0, 255, 0), -1)
                cv2.addWeighted(overlay, 0.3, image_out, 0.7, 0, image_out)

            # Draw bounding boxes + labels (green if tracked, red if not)
            for i, box_data in enumerate(boxes):
                x, y, w, h = box_data
                conf_val = float(result.boxes[i].conf[0]) if i < len(result.boxes) else confidence
                cls_id = int(result.boxes[i].cls[0]) if i < len(result.boxes) else 0
                cls_name = self.model.names.get(cls_id, label)

                has_track = (self.tracking_enabled and i < len(track_ids)
                             and track_ids[i] is not None)
                if has_track:
                    box_label = f'ID{track_ids[i]} {int(conf_val * 100)}% {cls_name}'
                    box_color = (0, 200, 0)   # green — tracked
                else:
                    box_label = f'{int(conf_val * 100)}% {cls_name}'
                    box_color = (0, 0, 255)   # red — untracked

                cv2.rectangle(image_out, (x, y), (x + w, y + h), box_color, 2)
                cv2.putText(image_out, box_label, (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)

            return contours, boxes, weed_centres, image_out

        return contours, boxes, weed_centres, image

    def _hybrid_inference(self, image, confidence, show_display,
                          exg_min=30, exg_max=250, hue_min=30, hue_max=90,
                          saturation_min=30, saturation_max=255,
                          brightness_min=5, brightness_max=200,
                          min_detection_area=1, invert_hue=False):
        """
        Hybrid pipeline: YOLO crop mask + ExHSV weed detection (parallel).

        ExHSV runs on the full (unmasked) image in a background thread while
        YOLO runs on the main thread. Both YOLO (NCNN) and ExHSV (OpenCV/NumPy)
        release the GIL, so they achieve true parallelism on separate cores.

        Step 1: Submit ExHSV on full image to thread pool
        Step 2: YOLO predict on main thread (the bottleneck)
        Step 3: Build crop_mask at full resolution from masks.xy or boxes.xyxy
        Step 4: Dilate crop_mask by buffer
        Step 5: Wait for ExHSV result
        Step 6: Filter out any detections whose centre falls in crop mask
        Step 7: Visualization (if show_display)
        """
        h_full, w_full = image.shape[:2]

        # Step 1: Submit ExHSV on full image to background thread
        exhsv_future = self._executor.submit(
            self._gob.inference,
            image,
            exg_min=exg_min, exg_max=exg_max,
            hue_min=hue_min, hue_max=hue_max,
            saturation_min=saturation_min, saturation_max=saturation_max,
            brightness_min=brightness_min, brightness_max=brightness_max,
            min_detection_area=min_detection_area,
            show_display=False,
            algorithm='exhsv',
            invert_hue=invert_hue
        )

        # Step 2: YOLO inference on main thread (imgsz handles resize)
        if self.tracking_enabled:
            results = self.model.track(
                source=image,
                conf=confidence,
                classes=self._detect_class_ids,
                persist=True,
                tracker='config/bytetrack_owl.yaml',
                imgsz=self.inference_resolution,
                verbose=False,
                device='cpu'
            )
        else:
            results = self.model.predict(
                source=image,
                conf=confidence,
                classes=self._detect_class_ids,
                imgsz=self.inference_resolution,
                verbose=False,
                device='cpu'
            )

        # Step 3: Build crop mask at full resolution
        if self.tracking_enabled and self._crop_stabilizer:
            # Feed tracked crop detections to stabilizer for temporal persistence
            crop_track_ids = []
            crop_boxes_xyxy = []
            crop_contours = []

            for result in results:
                has_ids = (result.boxes.id is not None
                           and len(result.boxes.id) > 0)

                if result.masks is not None:
                    crop_contours.extend(
                        c.astype(np.int32).reshape(-1, 1, 2)
                        for c in result.masks.xy)

                for i, box in enumerate(result.boxes):
                    if has_ids:
                        crop_track_ids.append(int(result.boxes.id[i]))
                        crop_boxes_xyxy.append(list(map(int, box.xyxy[0])))

            if crop_track_ids:
                self._crop_stabilizer.update(
                    crop_track_ids,
                    crop_boxes_xyxy,
                    contours=crop_contours if crop_contours else None
                )
                crop_mask = self._crop_stabilizer.build_stabilized_mask(
                    (h_full, w_full))
            else:
                # No track IDs available — fall back to per-frame mask
                crop_mask = self._build_crop_mask(results, h_full, w_full)

            # Paint Kalman-predicted lost crop tracks into the mask
            # ByteTrack predicts where dropped crops moved — fills mask holes
            persist = self.detection_persist_frames
            lost_crops = self.get_lost_tracks(
                max_age=persist if persist > 0
                else (self._crop_stabilizer.max_age if self._crop_stabilizer else 3))
            for lc in lost_crops:
                x1, y1, x2, y2 = [max(0, int(v)) for v in lc['xyxy']]
                x2, y2 = min(w_full, x2), min(h_full, y2)
                if x2 > x1 and y2 > y1:
                    crop_mask[y1:y2, x1:x2] = 255
        else:
            crop_mask = self._build_crop_mask(results, h_full, w_full)

        # Step 4: Dilate crop mask by buffer
        crop_mask_undilated = crop_mask.copy() if show_display else None
        if self._dilate_kernel is not None and np.any(crop_mask):
            crop_mask = cv2.dilate(crop_mask, self._dilate_kernel)

        # Step 5: Wait for ExHSV result
        cnts, boxes, weed_centres, _ = exhsv_future.result()

        # Step 6: Safety filter — drop detections whose centre falls in crop mask
        from utils.greenonbrown import MAX_DETECTIONS
        filtered_boxes = []
        filtered_centres = []
        for i, centre in enumerate(weed_centres):
            cx, cy = centre
            if 0 <= cy < h_full and 0 <= cx < w_full:
                if crop_mask[cy, cx] == 0:  # Not in crop zone
                    filtered_boxes.append(boxes[i])
                    filtered_centres.append(centre)

        # Cap after safety filter to limit downstream processing
        filtered_boxes = filtered_boxes[:MAX_DETECTIONS]
        filtered_centres = filtered_centres[:MAX_DETECTIONS]

        # Step 7: Visualization
        if show_display:
            image_out = image.copy()

            # Blue overlay on crop mask
            crop_overlay = image_out.copy()
            crop_overlay[crop_mask_undilated > 0] = (200, 150, 50)
            cv2.addWeighted(crop_overlay, 0.5, image_out, 0.5, 0, image_out)

            # Lighter blue on buffer zone (dilated - original)
            if self._dilate_kernel is not None:
                buffer_zone = cv2.subtract(crop_mask, crop_mask_undilated)
                if np.any(buffer_zone):
                    buffer_overlay = image_out.copy()
                    buffer_overlay[buffer_zone > 0] = (200, 180, 100)
                    cv2.addWeighted(buffer_overlay, 0.35, image_out, 0.65, 0, image_out)

            # Red boxes on weed detections
            for box_data in filtered_boxes:
                x, y, w, h = box_data
                cv2.rectangle(image_out, (x, y), (x + w, y + h), (0, 0, 255), 2)

            return cnts, filtered_boxes, filtered_centres, image_out

        return cnts, filtered_boxes, filtered_centres, image
