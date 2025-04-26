# Raspberry Pi 5 + Hailo‑8L + Ultralytics YOLO End‑to‑End Workflow

*(Windows 11 + WSL 2 Ubuntu 22.04 / 24.04, last validated 17 Apr 2025)*

> **Scope**  This guide walks you from a clean Windows‑11 system to an object‑detection model running in real‑time on a Raspberry Pi 5 equipped with the Hailo‑8L M.2 AI accelerator. It covers both stock **YOLOv8‑n** and custom‑label **YOLOv8‑n / YOLOv11‑n** training, conversion, optimization, compilation, and deployment. Where commands differ between Ubuntu 22.04 and 24.04 or between stock and custom datasets, both variants are shown.

> **Naming Conventions** 
> • `$HOME/Hailo8l` — project root (clone of [https://github.com/BetaUtopia/Hailo8l](https://github.com/BetaUtopia/Hailo8l)).\
> • `$DATASET` — path to your image/label dataset.\
> • `$VENV_YOLO`, `$VENV_HAILO` — Python virtual‑envs for training and compilation respectively.\
> • `$ARCH` — `hailo8l` unless you own a different Hailo SKU.\
> • Replace **\$USER** with your Linux user name when paths are absolute.

---

## 0. Host & WSL Preparation

1. **Install/Upgrade WSL 2**
   ```powershell
   wsl --install -d Ubuntu-22.04   # or ubuntu-24.04 if already released
   wsl --update
   wsl --shutdown  # restart the WSL engine
   ```
2. **Enable USB/IP & GPIO pass‑through** (Pi debugger & camera over USB). In Windows Features, tick *Virtual Machine Platform* and *Windows Subsystem for Linux*.
3. **Install a recent NVIDIA / Intel driver** if you intend to accelerate training on the Windows side (optional; most training runs inside WSL CPU‑only).

---

## 1. Clone template repo

```bash
cd $HOME
git clone https://github.com/BetaUtopia/Hailo8l.git
cd Hailo8l
```

The repo already contains helper scripts under `steps/` for dataset ingest and Hailo Model Zoo (HMZ) automations.

---

## 2. Prepare Training Environment (Ultralytics YOLO)

### 2.1 Ubuntu 22.04 (Py 3.11)

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv libgl1-mesa-glx
python3.11 -m venv venv_yolov8
source venv_yolov8/bin/activate
pip install --upgrade pip
pip install ultralytics==8.3.4  # pinned for reproducibility
```

### 2.2 Ubuntu 24.04 (Py 3.12)

```bash
sudo apt install -y python3.12 python3.12-venv libgl1-mesa-glx
python3.12 -m venv venv_yolov11
source venv_yolov11/bin/activate
pip install --upgrade pip
pip install ultralytics
```

---

## 3. Dataset & Training

### 3.1 Stock COCO / Public Dataset

```bash
cd $HOME/Hailo8l/model
# edit config.yaml if needed (image size, classes)
yolo detect train \
    data=config.yaml \
    model=yolov8n.pt \
    epochs=300 \
    imgsz=640 \
    batch=16 \
    name=retrain_yolov8n \
    project=./runs/detect
```

### 3.2 Custom Dataset (Label Studio → YOLO)

1. **Label images**
   ```bash
   sudo apt install -y python3-pip
   python3 -m venv venv_labelstudio && source venv_labelstudio/bin/activate
   pip install label-studio
   label-studio start  # http://localhost:8080
   ```
2. **Export YOLO format** from Label Studio: *Export → YOLO 1.X* → unzip under `$DATASET`.
3. **Create Ultralytics data‑config** (example `config_custom.yaml`):
   ```yaml
   ```

task: detect
path: \$DATASET  # absolute or relative
train: images/train
val:   images/val
names:
0: weed
1: crop

````
4. **Train**
```bash
yolo detect train \
    data=config_custom.yaml \
    model=yolov8n.pt \
    epochs=500 \
    imgsz=640 \
    batch=32 \
    name=weed_v8n \
    project=./model/runs/detect
````

> **Tip – Resume training**: `yolo detect train resume=True` if it crashes mid‑run.

---

## 4. Export to ONNX

After training completes:

```bash
cd $HOME/Hailo8l/model/runs/detect/<run‑name>/weights
# Choose either best.pt or last.pt
source $VENV_YOLO/bin/activate

# opset 11 is the sweet spot for Hailo DFC 3.30
yolo export model=best.pt format=onnx imgsz=640 opset=11
```

Produces `best.onnx`.

---

## 5. Set‑up Hailo Compiler Environment

Hailo requires **Python 3.8 (DFC ≤3.27)** or **Python 3.10 (DFC ≥3.30)**. Match your firmware version (check with `hailortcli fw-control identify`).

```bash
sudo apt install -y python3.10 python3.10-venv python3.10-dev build-essential graphviz graphviz-dev python3-tk
python3.10 -m venv venv_hailo
source venv_hailo/bin/activate
pip install --upgrade pip
```

### 5.1 Install Hailo Dataflow Compiler & Model Zoo

```bash
pip install ~/Hailo8l/whl/hailo_dataflow_compiler-3.30.0-py3-none-linux_x86_64.whl
pip install ~/Hailo8l/whl/hailo_model_zoo-2.14.0-py3-none-any.whl
```

Add the CLI to PATH:

```bash
echo "export PATH=\$PATH:$(python - <<'PY' ; import site, sys, pathlib, json, os; print(next(p for p in site.getsitepackages() if (pathlib.Path(p)/'hailomz').exists())+'/hailomz'); PY)" >> ~/.bashrc && source ~/.bashrc
```

---

## 6. Convert Dataset to TFRecord

### 6.1 COCO Example

```bash
python -m hailo_model_zoo.datasets.create_coco_tfrecord calib2017
```

### 6.2 Custom Dataset

The repo provides wrappers that respect your `config_custom.yaml`:

```bash
python steps/2_install_dataset/create_custom_tfrecord.py train
python steps/2_install_dataset/create_custom_tfrecord.py val
```

TFRecord files live under `~/.hailomz/data/models_files/<dataset>/…`.

---

## 7. Parse → Optimize → Compile

### 7.1 Generate .HAR (Parse)

```bash
cd $HOME/Hailo8l/model/runs/detect/<run‑name>/weights
hailomz parse \
    --hw-arch $ARCH \
    --ckpt best.onnx \
    yolov8n   # network nickname (keep consistent)
```

Produces `yolov8n.har`.

### 7.2 Optimize quantisation

```bash
hailomz optimize \
    --hw-arch $ARCH \
    --har yolov8n.har \
    --calib-path ~/.hailomz/data/models_files/<dataset>/coco_calib2017.tfrecord \
    --model-script $HOME/Hailo8l/hailo_model_zoo/hailo_model_zoo/cfg/alls/generic/yolov8n.alls \
    yolov8n
```

Quantised output: `yolov8n_optimized.har`.

### 7.3 Compile to HEF

```bash
hailomz compile \
    yolov8n \
    --hw-arch $ARCH \
    --har yolov8n_optimized.har
```

Outputs: `yolov8n.hef` (+ `.bin` profiling files).

---

## 8. Deploy to Raspberry Pi 5

### 8.1 Prepare Pi OS

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y hailo-all  # installs HailoRT & firmware
sudo reboot
```

Verify:

```bash
hailortcli fw-control identify
```

### 8.2 Install example pipelines

```bash
cd ~
git clone https://github.com/hailo-ai/hailo-rpi5-examples.git
cd hailo-rpi5-examples
./install.sh  # pulls Python deps and builds HailoRT wheels
source setup_env.sh
```

> **Picamera2 Fix** (Bookworm):
>
> ```bash
> pip uninstall -y opencv-python
> pip install opencv-python-headless --break-system-packages
> ```

### 8.3 Run Inference

```bash
python basic_pipelines/detection.py \
       -i rpi \
       --hef $HOME/yolov8n.hef \
       --labels-json $HOME/Hailo8l/labels.json
```

Camera preview pops at 30 FPS; CPU stays <35 %.

---

## 9. Troubleshooting & Tips

| Symptom                               | Likely Cause                    | Fix                                                        |
| ------------------------------------- | ------------------------------- | ---------------------------------------------------------- |
| `Could not find libGL.so` inside WSL  | Missing Mesa libs               | `sudo apt install libgl1-mesa-glx`                         |
| `hash mismatch` during optimize       | Wrong TFRecord / class count    | Regenerate TFRecord matching the number & order of classes |
| `Unsupported op: GridSample` at parse | Ultralytics version too new     | Pin `ultralytics==8.3.4`, re‑export ONNX                   |
| Runtime FPS low                       | Pi operating in 32‑bit userland | Re‑flash 64‑bit Bookworm, enable `arm_64bit=1`             |

---

## 10. Automating the Pipeline

A sample bash helper (`build_yolo8n_hailo.sh`) lives under `scripts/`. It chains **train → export → parse → optimize → compile** and requires only dataset path and run‑name:

```bash
./scripts/build_yolo8n_hailo.sh --data config_custom.yaml --run weed_v8n --epochs 500
```

---

## 11. Integrating with Open Weed Locator

- The compiled `*.hef` and `labels.json` can be dropped into `OpenWeedLocator/bots/vision/models/`.
- Use the OWL device‑service `pipeline_hailo.py` (see branch `hailo‑support`) which wraps `hailort` inference and publishes MQTT bounding‑box messages.

---

## 12. Resources

- Hailo Docs: [https://docs.hailo.ai/](https://docs.hailo.ai/)
- Ultralytics Docs: [https://docs.ultralytics.com](https://docs.ultralytics.com)
- Open Weed Locator: [https://github.com/geezacoleman/OpenWeedLocator](https://github.com/geezacoleman/OpenWeedLocator)

* **Note – This guide doubles as the official README for model development in the [Open Weed Locator](https://github.com/geezacoleman/OpenWeedLocator) project. Follow the workflow above to train, quantise, and deploy new detection models, then copy the resulting `.hef` and `labels.json` into `bots/vision/models/` on your OWL units.** 

