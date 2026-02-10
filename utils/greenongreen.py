#!/usr/bin/env python
"""Green-on-Green weed detection using Ultralytics YOLO.

Supports both detection and segmentation models in NCNN or PyTorch format.
NCNN is the recommended format for Raspberry Pi (fastest on ARM CPU).
"""

import logging
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

logger = logging.getLogger(__name__)


class GreenOnGreen:
    def __init__(self, model_path='models', confidence=0.5, detect_classes=None):
        """
        Args:
            model_path: Path to NCNN model dir, .pt file, or parent dir containing models.
            confidence: Detection confidence threshold (0.0-1.0).
            detect_classes: List of class names to detect (None = all).
        """
        self.model_path = Path(model_path)
        self.confidence = confidence
        self.model = self._load_model()
        self.task = self.model.task  # 'detect' or 'segment'
        self.detection_mask = None  # Combined binary mask, set after inference (seg only)

        # Map class names to IDs after model is loaded
        self._detect_class_ids = self._resolve_classes(detect_classes)

        logger.info(f'GreenOnGreen initialized: task={self.task}, '
                     f'classes={list(self.model.names.values())}, '
                     f'filtering={detect_classes or "all"}')

    def _load_model(self):
        """Load YOLO model -- supports NCNN dirs and .pt files."""
        if self.model_path.is_dir():
            # Check if this IS an NCNN model dir (has .param + .bin)
            if list(self.model_path.glob('*.param')):
                logger.info(f'Using NCNN model: {self.model_path.name}')
                return YOLO(str(self.model_path))

            # Search for NCNN subdirs first, then .pt files
            ncnn_dirs = [d for d in self.model_path.iterdir()
                         if d.is_dir() and list(d.glob('*.param'))]
            if ncnn_dirs:
                selected = ncnn_dirs[0]
                logger.info(f'Using NCNN model: {selected.name}')
                return YOLO(str(selected))

            pt_files = list(self.model_path.glob('*.pt'))
            if pt_files:
                logger.info(f'Using PyTorch model: {pt_files[0].name}')
                return YOLO(str(pt_files[0]))

            raise FileNotFoundError(f'No YOLO models found in {self.model_path}')

        elif self.model_path.exists():
            logger.info(f'Using model: {self.model_path.name}')
            return YOLO(str(self.model_path))

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

    @property
    def class_names(self):
        """Return dict of {id: name} from loaded model."""
        return self.model.names

    def inference(self, image, confidence=0.5, show_display=False,
                  filter_id=None, label='WEED', build_mask=False):
        """
        Run YOLO inference. Returns same tuple as GreenOnBrown.

        Args:
            image: BGR numpy array.
            confidence: Detection confidence threshold.
            show_display: If True, return annotated image copy.
            filter_id: Unused, kept for API compatibility.
            label: Fallback label for display.
            build_mask: If True and model is segmentation, build self.detection_mask
                        for zone-based actuation. Skipped when False to save CPU.

        Returns:
            (contours, boxes, weed_centres, image_out)
            - contours: mask polygons (segmentation) or None (detection)
            - boxes: list of [x, y, w, h]
            - weed_centres: list of [cx, cy]
            - image_out: annotated image if show_display, else original image
        """
        self.detection_mask = None  # Reset each frame

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

        for result in results:
            # Process bounding boxes (works for both detect and segment)
            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                w, h = x2 - x1, y2 - y1
                boxes.append([x1, y1, w, h])
                weed_centres.append([x1 + w // 2, y1 + h // 2])

            # Extract mask contours for segmentation models
            if result.masks is not None:
                contours = [c.astype(np.int32).reshape(-1, 1, 2)
                            for c in result.masks.xy]

                # Only build combined binary mask when zone actuation needs it
                if build_mask:
                    self.detection_mask = np.zeros(image.shape[:2], dtype=np.uint8)
                    cv2.drawContours(self.detection_mask, contours, -1, 255, -1)

        if show_display:
            image_out = image.copy()

            # Draw segmentation masks if available
            if contours is not None:
                overlay = image_out.copy()
                for contour in contours:
                    cv2.drawContours(overlay, [contour], -1, (0, 255, 0), -1)
                cv2.addWeighted(overlay, 0.3, image_out, 0.7, 0, image_out)

            # Draw bounding boxes + labels
            for i, box_data in enumerate(boxes):
                x, y, w, h = box_data
                conf_val = float(result.boxes[i].conf[0]) if i < len(result.boxes) else confidence
                cls_id = int(result.boxes[i].cls[0]) if i < len(result.boxes) else 0
                cls_name = self.model.names.get(cls_id, label)
                box_label = f'{int(conf_val * 100)}% {cls_name}'
                cv2.rectangle(image_out, (x, y), (x + w, y + h), (0, 0, 255), 2)
                cv2.putText(image_out, box_label, (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)

            return contours, boxes, weed_centres, image_out

        return contours, boxes, weed_centres, image
