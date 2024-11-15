#!/usr/bin/env python
from pathlib import Path
from typing import Tuple, List, Optional
import numpy as np
import logging
import cv2
from ultralytics import YOLO

logger = logging.getLogger(__name__)

class GreenOnGreen:
    def __init__(self, model_path: str = 'models', label_file: Optional[str] = None) -> None:
        """Initialize YOLO model for weed detection."""
        self.model_path = Path(model_path)
        self.model = self._load_model()
        self.weed_centers: List[List[int]] = []
        self.boxes: List[List[int]] = []

    def _load_model(self) -> YOLO:
        """Load YOLO model, supporting both .pt and NCNN formats."""
        if self.model_path.is_dir():
            # Check for NCNN model first (model.param and model.bin)
            ncnn_param = list(self.model_path.glob('*.param'))
            if ncnn_param:
                ncnn_dir = ncnn_param[0].parent
                logger.info(f'Using NCNN model from {ncnn_dir}')
                return YOLO(self.model_path)

            # Fall back to .pt files
            pt_files = list(self.model_path.glob('*.pt'))
            if not pt_files:
                raise FileNotFoundError('No valid model files found (.pt or NCNN)')

            self.model_path = pt_files[0]
            logger.info(f'Using PyTorch model {self.model_path.stem}')

        elif self.model_path.suffix == '.pt':
            logger.info(f'Loading PyTorch model {self.model_path}')
        else:
            # Assume NCNN model directory
            if not self.model_path.exists():
                raise FileNotFoundError(f'Model path {self.model_path} does not exist')
            logger.info(f'Loading NCNN model from {self.model_path}')

        return YOLO(str(self.model_path))

    def inference(self,
                  image: np.ndarray,
                  confidence: float = 0.5) -> Tuple[None, List[List[int]], List[List[int]], np.ndarray]:
        """Run inference on image and return detections."""
        self.weed_centers = []
        self.boxes = []
        results = self.model.predict(source=image, conf=confidence, verbose=False)

        # Process each detection
        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                w = x2 - x1
                h = y2 - y1

                self.boxes.append([x1, y1, w, h])
                center_x = x1 + w // 2
                center_y = y1 + h // 2
                self.weed_centers.append([center_x, center_y])

                conf = float(box.conf[0])
                label = f'{int(conf * 100)}% weed'
                cv2.rectangle(image, (x1, y1), (x2, y2), (0, 0, 255), 2)
                cv2.putText(image, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX,
                            1.0, (255, 0, 0), 2)

        return None, self.boxes, self.weed_centers, image