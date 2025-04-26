#!/usr/bin/env python3
# green_on_green.py
"""
Green‑on‑Green weed detector
---------------------------
• Hailo‑8/8L  → .hef
• CPU‑TFLite  → .tflite
• Coral Edge‑TPU (only if Python < 3.10 and Edge‑TPU present)

Author: CropCrusaders (2025‑04‑17)
License: MIT
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np

_LOG = logging.getLogger("GreenOnGreen")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ── Optional back‑end imports ───────────────────────────────────────────
try:  # Hailo‑8/8L
    from hailo_platform import HEF, VDevice, FormatType
    _HAS_HAILO = True
except Exception:  # noqa: BLE001
    _HAS_HAILO = False

try:  # Coral Edge‑TPU
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
except Exception:  # noqa: BLE001
    _HAS_CORAL = False

try:  # CPU‑TFLite
    from tflite_runtime.interpreter import Interpreter
    _HAS_TFLITE = True
except Exception:  # noqa: BLE001
    _HAS_TFLITE = False
# ────────────────────────────────────────────────────────────────────────


def _read_labels(path: Path) -> dict[int, str]:
    with open(path, "r", encoding="utf‑8") as fh:
        pairs = (ln.strip().split(maxsplit=1) for ln in fh)
    return {int(k): v for k, v in pairs}


@dataclass(slots=True)
class Detection:
    cls:    int
    score:  float
    x:      int
    y:      int
    w:      int
    h:      int


# ── Main class ──────────────────────────────────────────────────────────
class GreenOnGreen:
    """Back‑end‑agnostic detector.

    Parameters
    ----------
    model_dir  :  directory holding `.hef` / `.tflite`
    labels     :  path to labels.txt   (id label)
    accel      :  "auto" | "hailo" | "cpu" | "coral"
    colours    :  list of BGR tuples per class (optional)
    thickness  :  annotation line thickness
    """

    def __init__(
        self,
        model_dir: str | Path = "models",
        labels: str | Path = "models/labels.txt",
        accel: str = "auto",
        colours: list[tuple[int, int, int]] | None = None,
        thickness: int = 2,
    ):
        self.model_dir = Path(model_dir)
        self.labels = _read_labels(Path(labels))
        self.accel = self._choose_backend(accel)
        self.colours = colours
        self.thickness = thickness

        _LOG.info("Using back‑end: %s", self.accel.upper())
        getattr(self, f"_init_{self.accel}")()

        # public state mirrors original API
        self.boxes:   list[list[int]] = []
        self.centres: list[list[int]] = []
        self.dets:    list[Detection] = []

        self._t_last = time.perf_counter()

    # ── back‑end selection ────────────────────────────────────────────
    def _choose_backend(self, want: str) -> str:
        if want not in {"auto", "hailo", "coral", "cpu"}:
            raise ValueError("accel must be auto|hailo|coral|cpu")

        if want in {"hailo", "auto"} and _HAS_HAILO:
            return "hailo"
        if want in {"coral", "auto"} and _HAS_CORAL:
            return "coral"
        return "cpu"

    # ── initialisers ──────────────────────────────────────────────────
    def _init_hailo(self) -> None:
        hef = next(self.model_dir.glob("*.hef"), None)
        if hef is None:
            raise FileNotFoundError("No .hef model found")
        self.hef = HEF(hef.as_posix())
        try:
            self.vdev = VDevice()
        except Exception as exc:  # Hailo device busy/not found
            raise RuntimeError("Failed to open Hailo device") from exc
        self.ng = self.hef.configure(self.vdev)
        fmt: FormatType = self.ng.get_input_streams()[0].get_format()
        self.size = (fmt.width, fmt.height)
        from hailo_platform.postprocess import postprocess_yolov5  # type: ignore
        self._pp = postprocess_yolov5

    def _init_coral(self) -> None:
        tfl = next(self.model_dir.glob("*.tflite"), None)
        if tfl is None:
            raise FileNotFoundError("No .tflite model found for Coral")
        self.interpreter = tpu_make_intp(tfl.as_posix())
        self.interpreter.allocate_tensors()
        self.size = tpu_input_size(self.interpreter)

    def _init_cpu(self) -> None:
        if not _HAS_TFLITE:
            raise RuntimeError("Install tflite‑runtime for CPU back‑end")
        tfl = next(self.model_dir.glob("*.tflite"), None)
        if tfl is None:
            raise FileNotFoundError("No .tflite model found")
        self.interpreter = Interpreter(model_path=tfl.as_posix(), num_threads=4)
        self.interpreter.allocate_tensors()
        ih, iw = self.interpreter.get_input_details()[0]["shape"][1:3]
        self.size = (iw, ih)

    # ── public helpers ────────────────────────────────────────────────
    def fps(self) -> float:
        """Return instantaneous FPS since last call to `inference()`."""
        now, self._t_last = time.perf_counter(), time.perf_counter()
        return 1.0 / (now - self._t_last)

    # ── inference dispatcher ──────────────────────────────────────────
    def inference(
        self,
        frame: np.ndarray,
        conf: float = 0.5,
        filter_id: int = 0,
    ) -> Tuple[None, List[List[int]], List[List[int]], np.ndarray]:
        self.boxes.clear()
        self.centres.clear()
        self.dets.clear()

        getattr(self, f"_infer_{self.accel}")(frame, conf, filter_id)
        return None, self.boxes, self.centres, frame

    # ── inference back‑ends ───────────────────────────────────────────
    def _infer_hailo(self, img: np.ndarray, conf: float, fid: int) -> None:
        rgb, scale, pad = self._letterbox(img)
        with self.ng.activate({}):
            self.ng.get_input_streams()[0].send(rgb)
            raw = self.ng.get_output_streams()[0].receive()
        dets = self._pp(raw, threshold=conf)
        self._draw(img, dets, fid, scale, pad)

    def _infer_coral(self, img: np.ndarray, conf: float, fid: int) -> None:
        rgb, scale, pad = self._letterbox(img)
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
        self._draw(img, dets, fid, scale, pad)

    def _infer_cpu(self, img: np.ndarray, conf: float, fid: int) -> None:
        rgb, scale, pad = self._letterbox(img)
        inp = self.interpreter.get_input_details()[0]
        self.interpreter.set_tensor(inp["index"], rgb[None])
        self.interpreter.invoke()
        dets = self._simple_yolov5_decode(self.interpreter, conf)
        self._draw(img, dets, fid, scale, pad)

    # ── utils ─────────────────────────────────────────────────────────
    def _letterbox(self, img: np.ndarray) -> tuple[np.ndarray, float, tuple[int, int]]:
        """Resize with unchanged aspect ratio using padding."""
        h, w = img.shape[:2]
        iw, ih = self.size
        scale = min(iw / w, ih / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        pad_w, pad_h = (iw - nw) // 2, (ih - nh) // 2
        resized = cv2.resize(img, (nw, nh))
        canvas = np.full((ih, iw, 3), 114, dtype=np.uint8)
        canvas[pad_h:pad_h + nh, pad_w:pad_w + nw] = resized
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        return rgb, scale, (pad_w, pad_h)

    def _draw(
        self,
        img: np.ndarray,
        dets: List[dict] | List[Detection],
        fid: int,
        scale: float,
        pad: tuple[int, int],
    ) -> None:
        ih, iw = img.shape[:2]
        for d in dets:
            cls_id = int(d["class_id"]) if isinstance(d, dict) else d.cls
            if fid and cls_id != fid:
                continue
            if isinstance(d, dict):
                x0, y0, bw, bh = d["bbox"]
                score = d["score"]
            else:
                x0, y0, bw, bh, score = d.x, d.y, d.w, d.h, d.score

            # undo letter‑box
            x0 = int((x0 - pad[0]) / scale)
            y0 = int((y0 - pad[1]) / scale)
            bw = int(bw / scale)
            bh = int(bh / scale)

            x1, y1 = min(iw, x0 + bw), min(ih, y0 + bh)
            colour = (self.colours or [(0, 0, 255)])[cls_id % len(self.colours or [(0, 0, 255)])]

            cv2.rectangle(img, (x0, y0), (x1, y1), colour, self.thickness)
            label = f"{int(score*100)}% {self.labels.get(cls_id, cls_id)}"
            cv2.putText(img, label, (x0, max(0, y0 - 7)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, self.thickness)
            self.boxes.append([x0, y0, bw, bh])
            self.centres.append([x0 + bw // 2, y0 + bh // 2])
            self.dets.append(Detection(cls_id, score, x0, y0, bw, bh))

    # ── Minimal YOLO‑v5 decoder for CPU path ──────────────────────────
    @staticmethod
    def _simple_yolov5_decode(interp, conf_thres: float) -> list[dict]:
        """Naïve single‑head YOLOv5 decoder (assumes stride‑32, no NMS)."""
        out_details = interp.get_output_details()[0]
        raw = interp.get_tensor(out_details["index"])[0]  # shape = (N, 85)
        dets = []
        for row in raw:
            score = row[4] * row[5:].max()
            if score < conf_thres:
                continue
            cls_id = int(row[5:].argmax())
            cx, cy, w, h = row[:4]
            dets.append(
                {
                    "bbox": (
                        cx - w / 2,
                        cy - h / 2,
                        w,
                        h,
                    ),
                    "score": float(score),
                    "class_id": cls_id,
                }
            )
        return dets
