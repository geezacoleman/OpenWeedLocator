# Green-on-Green Weed Detection with YOLO

The OWL supports AI-based weed detection using Ultralytics YOLO models. This enables in-crop (green-on-green) weed recognition, where colour-based detection alone cannot distinguish weeds from crop.

## Supported Model Formats

| Format | Extension | Speed on Pi | Recommended |
|--------|-----------|------------|-------------|
| NCNN | directory with `.param` + `.bin` | Fastest | Yes |
| PyTorch | `.pt` | Slower | For testing only |

**NCNN is the recommended format for Raspberry Pi deployment.** It provides the fastest CPU inference on ARM processors (~68ms for YOLO11n on Pi 5).

## Model Directory Structure

Place your model files in this `models/` directory:

```
models/
├── yolo11n_ncnn_model/    # NCNN format (recommended for Pi)
│   ├── model.ncnn.param
│   ├── model.ncnn.bin
│   └── metadata.yaml
├── yolo11n.pt             # PyTorch format (slower, for testing)
├── labels.txt             # Legacy file (YOLO uses metadata.yaml instead)
└── README.md              # This file
```

The OWL will auto-detect models in this directory:
1. NCNN subdirectories (highest priority)
2. `.pt` files (fallback)

You can also specify an exact path in config: `model_path = models/my_model_ncnn_model`

## Model Types

YOLO supports two task types, both work with the OWL:

- **Detection models** (`yolo11n.pt`) — output bounding boxes only. Use with `actuation_mode = centre`.
- **Segmentation models** (`yolo11n-seg.pt`) — output bounding boxes + pixel masks. Use with `actuation_mode = centre` or `actuation_mode = zone`.

Both model types also work with `gog-hybrid` mode. Segmentation models provide precise crop boundaries; detection models use filled bounding boxes as crop exclusion zones (coarser but functional). The buffer dilation smooths the edges either way.

The task type is auto-detected from the model's `metadata.yaml`.

## Exporting to NCNN

Export a PyTorch model to NCNN format using the Ultralytics CLI:

```bash
# Install ultralytics with export support
pip install ultralytics[export]

# Export detection model
yolo export model=yolo11n.pt format=ncnn

# Export segmentation model
yolo export model=yolo11n-seg.pt format=ncnn
```

This creates a directory (e.g., `yolo11n_ncnn_model/`) containing the NCNN files. Copy this directory to `models/` on your Pi.

## Training Custom Models

To train a weed detection model on your own data:

1. **Collect and label images** using [Roboflow](https://roboflow.com/) or [Label Studio](https://labelstud.io/)
2. **Train with Ultralytics:**
   ```bash
   yolo detect train data=your_dataset.yaml model=yolo11n.pt epochs=100
   ```
3. **Export to NCNN** (see above)
4. **Deploy:** Copy the NCNN model directory to `models/` on the Pi

For weed detection datasets, see [Weed-AI](https://weed-ai.sydney.edu.au/).

## Configuration

Set these parameters in your config INI file under `[GreenOnGreen]`:

```ini
[GreenOnGreen]
model_path = models                  # Path to model or directory
confidence = 0.5                     # Detection threshold (0.0-1.0)
detect_classes =                     # Comma-separated class names (empty = all)
actuation_mode = centre              # 'centre' or 'zone'
min_detection_pixels = 50            # Min pixels per lane for zone mode
inference_resolution = 320           # YOLO input resolution for hybrid mode (160-1280)
crop_buffer_px = 20                  # Buffer around crop regions in hybrid mode (0-50)
```

Then set `algorithm = gog` (pure AI detection) or `algorithm = gog-hybrid` (AI crop mask + colour weed detection) in the `[System]` section.

## Actuation Modes

- **`centre`** (default) — The centre X coordinate of each detection box determines which relay fires. Works with both detection and segmentation models.
- **`zone`** (segmentation models only) — The frame is divided into lanes (one per relay). If the number of weed pixels in a lane exceeds `min_detection_pixels`, that relay fires. A large weed spanning multiple lanes triggers multiple relays simultaneously.

## References

- [Ultralytics YOLO Documentation](https://docs.ultralytics.com/)
- [NCNN Export Guide](https://docs.ultralytics.com/integrations/ncnn/)
- [Weed-AI Dataset Repository](https://weed-ai.sydney.edu.au/)
