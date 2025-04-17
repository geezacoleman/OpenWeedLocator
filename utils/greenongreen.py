#!/usr/bin/env python3
# green_on_green.py
"""
Unified Green‑on‑Green weed detector
-----------------------------------
• Hailo‑8/8L → .hef  • CPU‑TFLite → .tflite  • (optional) Coral Edge‑TPU
Author: CropCrusaders, 2025‑04
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np

_LOG = logging.getLogger("GreenOnGreen")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# -- Optional back‑end imports -------------------------------------------------
try:                         # Hailo‑8/8L
    from hailo_platform import HEF, VDevice, FormatType
    _HAS_HAILO = True
except Exception:            # noqa: BLE001
    _HAS_HAILO = False

try:                         # Coral Edge‑TPU
    if sys.version_info < (3, 10):
        from pycoral.adapters.common import input_size as tpu_input_size
        from pycoral.adapters.detect import get_objects as tpu_get_objects
        from pycoral.utils.edgetpu import (
            make_interpreter as tpu_make_intp,
            run_inference as tpu_run_inf,
        )
        _HAS_CORAL = True
    else:
        _HAS_CORAL = False
except Exception:            # noqa: BLE001
    _HAS_CORAL = False

try:                         # CPU‑TFLite
    from tflite_runtime.interpreter import Interpreter
    _HAS_TFLITE = True
except Exception:            # noqa: BLE001
    _HAS_TFLITE = False
# -----------------------------------------------------------------------------


def _read_labels(path: Path) -> dict[int, str]:
    with path.open("r", encoding="utf‑8") as fh:
        pairs = (l.strip().split(maxsplit=1) for l in fh)
    return {int(k): v for k, v in pairs}


@dataclass
class Detection:
    x: int
    y: int
    w: int
    h: int
    score: float
    cls: int


class GreenOnGreen:
    """
    Detector that works on Hailo‑8/8L first, then CPU; Coral only if supported.

    Parameters
    ----------
    model_dir : Path to directory containing `.hef` and/or `.tflite` models
    labels    : Path to label‑file `"id label\n"` per line
    accel     : `"hailo" | "cpu" | "coral" | "auto"`
    """

    def __init__(
        self,
        model_dir: str | Path = "models",
        labels: str | Path = "models/labels.txt",
        accel: str = "auto",
    ):
        self.model_dir = Path(model_dir)
        self.labels = _read_labels(Path(labels))
        self.accel = self._choose_backend(accel)
        _LOG.info("Selected back‑end: %s", self.accel.upper())
        getattr(self, f"_init_{self.accel}")()  # call _init_hailo / _init_cpu / _init_coral
        self.boxes: List[List[int]] = []
        self.centres: List[List[int]] = []

    # ------------------------ back‑end selection ------------------------
    def _choose_backend(self, user_choice: str) -> str:
        if user_choice not in {"auto", "hailo", "coral", "cpu"}:
            raise ValueError("accel must be auto|hailo|coral|cpu")

        if user_choice == "hailo" or (user_choice == "auto" and _HAS_HAILO):
            return "hailo"

        if user_choice == "coral" or (user_choice == "auto" and _HAS_CORAL):
            return "coral"

        return "cpu"

    # --------------------------- initialisers --------------------------
    def _init_hailo(self) -> None:
        hef = next(self.model_dir.glob("*.hef"), None)
        if hef is None:
            raise FileNotFoundError("No .hef model found for Hailo back‑end")
        self.hef = HEF(hef.as_posix())
        self.vd = VDevice()                         # auto‑detect Hailo device
        self.ng = self.hef.configure(self.vd)
        fmt: FormatType = self.ng.get_input_streams()[0].get_format()
        self.size = (fmt.width, fmt.height)         # (W, H)
        # simple YOLOX/NMS util bundled with Hailo examples ≥4.20
        from hailo_platform.postprocess import postprocess_yolov5  # type: ignore
        self._pp = postprocess_yolov5

    def _init_coral(self) -> None:
        tflite = next(self.model_dir.glob("*.tflite"), None)
        if tflite is None:
            raise FileNotFoundError("No .tflite model found for Coral back‑end")
        self.interpreter = tpu_make_intp(tflite.as_posix())
        self.interpreter.allocate_tensors()
        self.size = tpu_input_size(self.interpreter)

    def _init_cpu(self) -> None:
        if not _HAS_TFLITE:
            raise RuntimeError("tflite‑runtime not installed (`pip install tflite‑runtime`)")
        tflite = next(self.model_dir.glob("*.tflite"), None)
        if tflite is None:
            raise FileNotFoundError("No .tflite model found for CPU back‑end")
        self.interpreter = Interpreter(model_path=tflite.as_posix(), num_threads=4)
        self.interpreter.allocate_tensors()
        ih, iw = self.interpreter.get_input_details()[0]["shape"][1:3]
        self.size = (iw, ih)

    # ---------------------------- inference ----------------------------
    def inference(
        self,
        frame: np.ndarray,
        conf: float = 0.5,
        filter_id: int = 0,
    ) -> Tuple[None, List[List[int]], List[List[int]], np.ndarray]:
        self.boxes.clear()
        self.centres.clear()
        getattr(self, f"_infer_{self.accel}")(frame, conf, filter_id)
        return None, self.boxes, self.centres, frame

    # ----------------------- implementation: Hailo ---------------------
    def _infer_hailo(self, img: np.ndarray, conf: float, fid: int) -> None:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, self.size)
        with self.ng.activate({}):
            self.ng.get_input_streams()[0].send(rgb)
            raw = self.ng.get_output_streams()[0].receive()
        dets = self._pp(raw, threshold=conf)        # [{'bbox':(x,y,w,h),'score':..,'class_id':..}]
        self._draw(img, dets, fid)

    # ----------------------- implementation: Coral ---------------------
    def _infer_coral(self, img: np.ndarray, conf: float, fid: int) -> None:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, self.size)
        tpu_run_inf(self.interpreter, rgb.tobytes())
        objs = tpu_get_objects(self.interpreter, conf)
        dets = [
            {
                "bbox": (
                    o.bbox.xmin,
                    o.bbox.ymin,
                    o.bbox.xmax - o.bbox.xmin,
                    o.bbox.ymax - o.bbox.ymin,
                ),
                "score": o.score,
                "class_id": o.id,
            }
            for o in objs
        ]
        self._draw(img, dets, fid)

    # ----------------------- implementation: CPU -----------------------
    def _infer_cpu(self, img: np.ndarray, conf: float, fid: int) -> None:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(rgb, self.size)
        inp = self.interpreter.get_input_details()[0]
        self.interpreter.set_tensor(inp["index"], rgb[None])
        self.interpreter.invoke()
        # 👉 replace with proper decoder matching your CPU model
        from utils.yolo_postprocess import decode  # custom util
        dets = decode(self.interpreter, conf)
        self._draw(img, dets, fid)

    # ---------------------------- draw & log ---------------------------
    def _draw(self, img: np.ndarray, dets: List[dict], fid: int) -> None:
        h, w = img.shape[:2]
        for d in dets:
            if int(d["class_id"]) != fid:
                continue
            x, y, bw, bh = map(int, d["bbox"])
            cv2.rectangle(img, (x, y), (x + bw, y + bh), (0, 0, 255), 2)
            label = f"{int(100*d['score'])}% {self.labels.get(d['class_id'], d['class_id'])}"
            cv2.putText(img, label, (x, y + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 0, 0), 2)
            self.boxes.append([x, y, bw, bh])
            self.centres.append([x + bw // 2, y + bh // 2])
