# Green‑on‑Green (In‑Crop) Detection – _Beta_

> **Status:** early beta · expect rough edges · API and install steps may still change  
> **Tested on:**  
> • Raspberry Pi 4 (64‑bit Bookworm) with **Hailo‑8L M.2 HAT+**  
> • Libre‑Computer AML‑S905X‑CC (“Le Potato”)  
> • Windows 11 desktop (CPU‑only)

The Green‑on‑Green (GoG) module lets the **Open Weed Locator (OWL)** detect weeds _inside_ the crop canopy.  
It supports three execution back‑ends:

| Priority | Back‑end | Model | Notes |
|----------|----------|-------|-------|
| 1 | **Hailo‑8/8L** | `.hef` | Works on Pi 4/5 with Hailo‑8L M.2 HAT+ and on x86 with Hailo‑8 PCIe. |
| 2 | **CPU (TFLite)** | `.tflite` | Always available – slower. |
| 3 | **Coral Edge‑TPU** | `.tflite` (Edge‑TPU‑compiled) | _Only_ if Python ≤ 3.9; PyCoral does **not** support the default Pi Bookworm (Python 3.11). |

The detector auto‑selects the best back‑end; you can override with `--accel hailo|cpu|coral`.

---

## 1 · Install OWL & GoG

### 1.1 Clone the repo

```bash
git clone https://github.com/CropCrusaders/OpenWeedLocator.git owl
cd owl
```

> All commands below assume you are inside the `owl` root.

### 1.2 Set up the Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requirements include `tflite-runtime`; Hailo/Coral extras are separate (see below).

---

## 2 · Hardware / Driver setup

### 2.1 Hailo‑8/8L (recommended)

```bash
# Enable the Hailo repo
echo "deb https://hailo.ai/repo/apt stable main" | sudo tee /etc/apt/sources.list.d/hailo.list
wget -qO - https://hailo.ai/repo/apt/public.key | sudo apt-key add -

# Install platform 4.20 (works with Python 3.11)
sudo apt update
sudo apt install hailo-all=4.20.*
```

Reboot, then verify:

```bash
hailortcli info          # should list your device
```

### 2.2 Coral Edge‑TPU (optional)

> **Pi Bookworm users:** PyCoral wheels are only built for Python 3.7–3.9.  
> You must create a **Python 3.9 virtualenv** or use the legacy “Bullseye AIY” image.

```bash
# Example inside a Python 3.9 venv
pip install pycoral
```

Follow the official [Coral USB Accelerator guide](https://coral.ai/docs/accelerator/) for udev rules and firmware.

---

## 3 · Quick smoke test

1.  Drop a demo model into `owl/models/`:

    ```bash
    cd models
    wget https://github.com/google-coral/test_data/raw/master/ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite
    wget https://github.com/google-coral/test_data/raw/master/coco_labels.txt -O labels.txt
    cd ..
    ```

2.  Run OWL in display mode with GoG:

    ```bash
    python owl.py --show-display --algorithm gog --accel auto
    ```

    * A window should open with the camera feed and red boxes around “potted plants”  
      (class ID 63 in the COCO set).  
    * Pass `--filter-id 0` to show **all** classes.

> Inside under low light? Add `--exp-compensation 4 --exp-mode auto`.

---

## 4 · Training your own GoG model

### 4.1 TensorFlow Object Detection API → Edge‑TPU

*   Follow the Colab by **EdjeElectronics**  
    <https://colab.research.google.com/github/EdjeElectronics/TensorFlow-Lite-Object-Detection-on-Android-and-Raspberry-Pi/blob/master/Train_TFLite2_Object_Detction_Model.ipynb>  
    It walks from labelling → training → quantisation → Edge‑TPU compile.

### 4.2 YOLO v5 / v8 → Edge‑TPU  (**experimental**)

Ultralytics export:

```bash
# YOLOv5
python export.py --weights best.pt --include edgetpu

# YOLOv8
yolo export model=best.pt format=edgetpu
```

> Export sometimes fails with current tool‑chain – track  
> <https://github.com/ultralytics/ultralytics/issues/1185>.

### 4.3 Hailo .hef compilation

1.  Quantise your model to `.onnx` or `.tflite`.  
2.  Run the **Hailo Dataflow Compiler** (`hef_generator`) to produce `your_model.hef`.  
    See Hailo docs §“Deploy a network”.

Place the resulting `.hef` **or** Edge‑TPU‑compiled `.tflite` in `owl/models/` and
update `labels.txt` so every class ID used by the model has a name.

---

## 5 · Command‑line reference (GoG‑specific)

| Flag | Description | Default |
|------|-------------|---------|
| `--algorithm gog` | activate Green‑on‑Green | _n/a_ |
| `--model-path path/to/model.hef|.tflite` | override auto‑selected model | first (α‑sort) in `models/` |
| `--accel auto|hailo|cpu|coral` | force back‑end | `auto` |
| `--filter-id N` | draw detections only for class N | `0` (show all) |

---

## 6 · Troubleshooting

| Symptom | Cause / Fix |
|---------|-------------|
| `RuntimeError: Failed to open Hailo device` | Check ribbon cable / M.2 seating; make sure another process isn’t using the accelerator. |
| `ImportError: No module named 'pycoral'` on Pi OS Bookworm | Create a Python 3.9 venv or use Hailo/CPU back‑end instead. |
| Detections look squashed / shifted | Camera aspect ratio ≠ model input. Use `--res (W H)` or retrain model at native ratio. |
| Low FPS on CPU | Reduce `frame-size`, prune model, or add Hailo 8L hardware. |

---

## References

1. PyImageSearch – “Object Detection with Google Coral”  
   <https://pyimagesearch.com/2019/05/13/object-detection-and-image-classification-with-google-coral-usb-accelerator/>
2. Google Coral documentation  
   <https://coral.ai/docs/>
3. Hailo RT documentation  
   <https://hailo.ai/developer-zone/>
