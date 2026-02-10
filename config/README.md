# Configuration Files

OWL uses INI configuration files to control both detection behaviour and system infrastructure. These are split into two types: **detection presets** and **infrastructure config**.

## Detection Presets

These files control how OWL detects weeds -- algorithm choice, colour thresholds, camera settings, relay wiring, and data collection options. Three presets are included out of the box:

| File | Description |
|------|-------------|
| `DAY_SENSITIVITY_1.ini` | Low sensitivity -- fewer false positives, may miss smaller weeds |
| `DAY_SENSITIVITY_2.ini` | Medium sensitivity -- good general-purpose starting point |
| `DAY_SENSITIVITY_3.ini` | High sensitivity -- catches more weeds, more likely to spray non-targets |

These are **protected defaults** -- the dashboard won't let you overwrite or delete them. You can create your own presets by saving from the dashboard or copying and editing one of these files.

### Which preset is active?

`active_config.txt` contains the path to the detection preset OWL loads on boot. If this file doesn't exist, OWL defaults to `DAY_SENSITIVITY_2.ini`. You can change the active preset from the dashboard or by editing this file directly.

### Key sections in detection presets

| Section | What it controls |
|---------|-----------------|
| `[System]` | Algorithm selection, relay count, actuation timing |
| `[Camera]` | Resolution, exposure compensation, crop factors |
| `[GreenOnBrown]` | Colour thresholds (ExG, Hue, Saturation, Brightness min/max), minimum detection area |
| `[GreenOnGreen]` | YOLO model path, confidence, class filtering, actuation mode (for in-crop weed detection) |
| `[Controller]` | Hardware controller type (`none`, `ute`, `advanced`) and pin mappings |
| `[DataCollection]` | Image sampling settings, save directory, logging |
| `[Relays]` | Maps relay IDs to GPIO pin numbers |
| `[Visualisation]` | Display settings when running with `--show-display` |

---

## All configurable options

Defaults shown are from `DAY_SENSITIVITY_2.ini` (the medium sensitivity preset). The low and high presets differ mainly in threshold ranges and resolution -- see the preset files for exact values.

### `[System]`

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `algorithm` | `exhsv` | `exg`, `exgr`, `maxg`, `nexg`, `exhsv`, `hsv`, `gndvi`, `gog`, `gog-hybrid` | Detection algorithm (see table below) |
| `input_file_or_directory` | *(empty)* | File or directory path | Path to video, image, or directory for offline processing. Leave empty for live camera |
| `relay_num` | `4` | 0+ (integer) | Number of relays connected to the OWL. Must match entries in `[Relays]` |
| `actuation_duration` | `0.15` | Seconds (float) | How long each relay stays on when a weed is detected |
| `delay` | `0` | Seconds (float) | Delay between detection and relay actuation. Use for speed/distance compensation |

### Detection algorithms

| Algorithm | Name | Formula / Method | Notes |
|-----------|------|-----------------|-------|
| `exg` | Excess Green | 2g - r - b | Simple and fast. Woebbecke et al. 1995 |
| `exgr` | Excess Green minus Excess Red | ExG - (1.4r - g) | Better soil rejection than ExG alone |
| `maxg` | Maximum Green | 24g - 19r - 2b | Jin et al. 2021 |
| `nexg` | Normalised Excess Green | Standardised ExG using channel ratios | Less sensitive to lighting variation |
| `exhsv` | ExG + HSV combined | Normalised ExG masked by HSV thresholds | **Default.** Best balance of accuracy and robustness |
| `hsv` | HSV thresholding | Hue/Saturation/Value range filter | Fast, but sensitive to lighting changes |
| `gndvi` | Green NDVI | (NIR - green) / (NIR + green) | **Requires NIR camera.** Not for standard setups |
| `gog` | Green-on-Green | Ultralytics YOLO object detection | For in-crop weed detection. Requires a trained model (see `[GreenOnGreen]`) |
| `gog-hybrid` | AI + Colour | YOLO crop mask + ExHSV colour detection | Uses YOLO to identify crop regions, then runs ExHSV on non-crop areas. Best of both worlds for in-crop scenarios |

All algorithms except `gog`, `gog-hybrid`, and `hsv` use the `[GreenOnBrown]` thresholds. The `hsv` algorithm uses only the HSV thresholds (hue, saturation, brightness). The `gog` algorithm ignores `[GreenOnBrown]` entirely and uses `[GreenOnGreen]` settings instead. The `gog-hybrid` algorithm uses both `[GreenOnGreen]` (for YOLO crop detection) and `[GreenOnBrown]` (for ExHSV weed detection in non-crop areas).

### `[Camera]`

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `resolution_width` | `640` | 1+ (integer) | Camera capture width in pixels |
| `resolution_height` | `480` | 1+ (integer) | Camera capture height in pixels |
| `exp_compensation` | `-2` | -10 to 10 (integer) | Exposure compensation. Negative = darker (reduces sky/soil glare) |
| `crop_factor_horizontal` | `0.1` | 0.0 to 0.5 (float) | Fraction of image width to crop from each side. Removes edge distortion |
| `crop_factor_vertical` | `0.02` | 0.0 to 0.5 (float) | Fraction of image height to crop from top and bottom |

Lower resolution (e.g. 416x320 in sensitivity 1 and 3) is faster but detects fewer small weeds. 640x480 is a good balance for most setups.

### `[GreenOnBrown]`

Used by all algorithms except `gog`. The ExG thresholds are used by `exg`, `exgr`, `maxg`, `nexg`, `exhsv`, and `gndvi`. The HSV thresholds are used by `hsv` and `exhsv`.

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `exg_min` | `25` | 0--255 | Minimum Excess Green value. Pixels below this are ignored. Higher = stricter |
| `exg_max` | `200` | 0--255 | Maximum Excess Green value. Pixels above this are ignored |
| `hue_min` | `39` | 0--180 | Minimum hue (OpenCV scale). Green vegetation is roughly 35--85 |
| `hue_max` | `83` | 0--180 | Maximum hue |
| `saturation_min` | `50` | 0--255 | Minimum colour saturation. Low values include washed-out/grey pixels |
| `saturation_max` | `220` | 0--255 | Maximum colour saturation |
| `brightness_min` | `60` | 0--255 | Minimum brightness. Low values include very dark pixels (shadows) |
| `brightness_max` | `190` | 0--255 | Maximum brightness. High values include very bright pixels (glare) |
| `min_detection_area` | `10` | 0+ (integer) | Minimum contour area in pixels to count as a weed. Higher = ignore small detections |
| `invert_hue` | `False` | `True` / `False` | If True, detect pixels *outside* the hue range instead of inside. Useful for non-green targets |

**Tuning tips:** Start with the medium preset and adjust using `--show-display` or the dashboard sliders. Wider ranges (lower mins, higher maxes) catch more weeds but increase false positives. Narrower ranges are more precise but may miss weeds in variable lighting.

### `[GreenOnGreen]`

Used when `algorithm = gog` or `algorithm = gog-hybrid`. Green-on-Green detection uses [Ultralytics YOLO](https://docs.ultralytics.com/) for object detection or instance segmentation. In `gog` mode, YOLO detects weeds directly. In `gog-hybrid` mode, YOLO identifies crop regions, which are masked out before running ExHSV colour detection on the remaining (non-crop) areas -- this combines AI crop recognition with colour-based weed detection for in-crop scenarios.

**Model format:** NCNN is the recommended format for Raspberry Pi -- it runs fastest on ARM CPUs. PyTorch (.pt) models also work but are slower. To convert a model to NCNN, use `yolo export model=your_model.pt format=ncnn`.

**Model loading:** Point `model_path` at an NCNN directory (containing `.param` and `.bin` files), a `.pt` file, or a parent directory containing models. OWL searches for NCNN models first, then falls back to `.pt` files.

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `model_path` | `models` | Directory or file path | Path to YOLO model. Can be an NCNN directory, `.pt` file, or parent directory containing models |
| `confidence` | `0.5` | 0.0--1.0 (float) | Minimum detection confidence. Lower = more detections (more false positives), higher = fewer but more certain |
| `detect_classes` | *(empty)* | Comma-separated class names | Filter detections to specific classes. Empty = detect all classes the model knows |
| `actuation_mode` | `centre` | `centre`, `zone` | How detections trigger relays (see below) |
| `min_detection_pixels` | `50` | 1+ (integer) | Minimum weed pixels in a relay lane to trigger actuation. Only used in `zone` mode |
| `inference_resolution` | `320` | 160--1280 (integer) | YOLO input resolution for `gog-hybrid` mode. Lower = faster inference, higher = better crop detection. Only used in hybrid mode |
| `crop_buffer_px` | `20` | 0--50 (integer) | Dilation buffer in pixels around detected crop regions in `gog-hybrid` mode. Larger buffer = more area masked as crop (fewer false positives on crop edges). Only used in hybrid mode |

**Actuation modes:**

- **`centre`** (default) -- Uses the centre point of each detection bounding box to determine which relay lane the weed falls in. Works with both detection and segmentation models. Simple and reliable.

- **`zone`** -- Uses the segmentation mask to count weed pixels in each relay lane. A relay fires only if the pixel count exceeds `min_detection_pixels`. **Requires a segmentation model** (detection-only models have no mask data). More precise for large or irregularly shaped weeds.

### `[Controller]`

Controls the optional hardware input controller (physical switches on the OWL unit). Most users set this to `none` and use the web dashboard instead.

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `controller_type` | `none` | `none`, `ute`, `advanced` | Hardware controller type. `none` = no physical switches |

**Ute controller** (`controller_type = ute`) -- a single toggle switch:

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `switch_pin` | `37` | 1--40 (BCM pin) | GPIO pin for the toggle switch |
| `switch_purpose` | `recording` | `recording`, `detection` | What the switch controls. `recording` toggles image saving, `detection` toggles weed detection |

**Advanced controller** (`controller_type = advanced`) -- multiple switches for full field control:

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `detection_mode_pin_up` | `36` | 1--40 (BCM pin) | Switch pin for detection mode up |
| `detection_mode_pin_down` | `35` | 1--40 (BCM pin) | Switch pin for detection mode down |
| `recording_pin` | `38` | 1--40 (BCM pin) | Switch pin to toggle image recording |
| `sensitivity_pin` | `40` | 1--40 (BCM pin) | Switch pin to cycle sensitivity presets (low/high toggle) |
| `low_sensitivity_config` | `config/DAY_SENSITIVITY_1.ini` | File path | Config loaded when sensitivity switch is set to low |
| `high_sensitivity_config` | `config/DAY_SENSITIVITY_3.ini` | File path | Config loaded when sensitivity switch is set to high |

**Sensitivity presets** (used by the web dashboard for low/medium/high switching):

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `low_sensitivity_config` | `config/DAY_SENSITIVITY_1.ini` | File path | Preset loaded when dashboard selects low sensitivity |
| `medium_sensitivity_config` | `config/DAY_SENSITIVITY_2.ini` | File path | Preset loaded when dashboard selects medium sensitivity |
| `high_sensitivity_config` | `config/DAY_SENSITIVITY_3.ini` | File path | Preset loaded when dashboard selects high sensitivity |

The Advanced controller hardware switch is a two-position toggle (low/high only). The web dashboard supports all three levels (low/medium/high) via MQTT.

### `[Visualisation]`

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `image_loop_time` | `5` | Seconds (integer) | Time each image is displayed when processing a directory of images with `--show-display` |

### `[DataCollection]`

| Key | Default | Range / Valid values | Description |
|-----|---------|---------------------|-------------|
| `image_sample_enable` | `False` | `True` / `False` | Enable automatic image saving for dataset collection |
| `sample_method` | `whole` | `bbox`, `square`, `whole` | How images are saved. `whole` = full frame, `bbox` = cropped to detection bounding box, `square` = square crop around detection |
| `sample_frequency` | `30` | 1+ (integer) | Save an image every N frames. 30 = roughly once per second at 30fps |
| `save_directory` | `/media/owl/SanDisk` | Directory path | Where to save collected images. Typically a USB drive |
| `detection_enable` | `True` | `True` / `False` | Enable weed detection and relay actuation. Set to `False` for data-collection-only mode |
| `log_fps` | `False` | `True` / `False` | Log frames-per-second to the console |
| `camera_name` | `cam1` | Text string | Identifier for this camera in saved data. Useful for multi-camera rigs |

### `[Relays]`

Maps relay IDs to GPIO board pin numbers. The number of entries here must be at least `relay_num` from `[System]`.

| Key | Default | Range | Description |
|-----|---------|-------|-------------|
| `0` | `13` | 1--40 (board pin) | GPIO pin for relay 0 (leftmost spray zone) |
| `1` | `15` | 1--40 (board pin) | GPIO pin for relay 1 |
| `2` | `16` | 1--40 (board pin) | GPIO pin for relay 2 |
| `3` | `18` | 1--40 (board pin) | GPIO pin for relay 3 (rightmost spray zone) |

Add more entries (e.g. `4 = 22`) if you have more than 4 relays, and update `relay_num` to match.

---

## Infrastructure Config (CONTROLLER.ini)

`CONTROLLER.ini` controls how OWL connects to dashboards, networks, and GPS. It is **not included in the repository** -- it gets created automatically when you run the setup script (`owl_setup.sh`).

If `CONTROLLER.ini` does not exist, OWL runs in basic detection-only mode with no MQTT, no web dashboard, and no remote control. This is the default for a fresh install.

See `CONTROLLER_TEMPLATE.ini` for a fully documented reference with explanations of every setting. To create one manually:

```bash
cp config/CONTROLLER_TEMPLATE.ini config/CONTROLLER.ini
# Then edit CONTROLLER.ini with your settings
```

### Key sections in CONTROLLER.ini

| Section | What it controls |
|---------|-----------------|
| `[MQTT]` | Enable/disable MQTT, broker address, device ID |
| `[Network]` | Operation mode (`standalone` or `networked`), IP addresses |
| `[WebDashboard]` | Flask dashboard port |
| `[GPS]` | GPS source, serial settings, networked GPS server config |

## How the two files work together

OWL reads both files on startup. The detection preset is read first, then `CONTROLLER.ini` is layered on top. If the same key exists in both files, `CONTROLLER.ini` wins (this is how Python's ConfigParser merge works). This means old custom presets that still have `[MQTT]` or `[Network]` sections from before the split will still work -- `CONTROLLER.ini` values take precedence.

## Operation Modes

OWL supports three ways of running, depending on your setup:

### No controller (default)

Out of the box, with no setup script run and no `CONTROLLER.ini`, OWL just does detection. No dashboard, no MQTT, no remote control. It reads images from the camera, runs the detection algorithm, and triggers relays. This is the simplest setup -- just wire it up, power it on, and it works.

**When to use:** Single OWL unit where you just need it to spray weeds. No touchscreen, no remote monitoring needed. Configure detection thresholds using `--show-display` on a monitor, save to a preset file, and let it run.

### Standalone mode

A single OWL with its own WiFi hotspot and web dashboard. The OWL creates a WiFi network you can connect to from a phone, tablet, or in-cab touchscreen. The dashboard lets you start/stop detection, adjust thresholds with live camera preview, save presets, and monitor system health -- all from a browser.

Everything runs on one Pi: the detection loop, the MQTT broker, the web server, and the dashboard.

**When to use:** Single OWL unit where you want a touchscreen or remote control. Typical setup: OWL Pi with a 7" touchscreen mounted in the cab, connected to the OWL's own hotspot.

**Setup:** Run `owl_setup.sh`, select "yes" to dashboard, then select "Standalone" mode. The setup script creates the hotspot, installs the MQTT broker, configures Nginx, and writes `CONTROLLER.ini` with `mode = standalone`.

### Networked mode

Multiple OWLs connected to a shared WiFi network, all reporting to a central controller. The central controller runs on a separate Pi (typically in the cab with a touchscreen) and shows all connected OWLs in one dashboard. You can monitor, control, and configure every OWL from a single screen.

Each OWL connects to the network as a client with a static IP and publishes its state to the central MQTT broker. The central controller discovers OWLs automatically -- no manual registration needed.

**When to use:** Multi-boom or multi-unit setups. For example, a 36m spray rig with 3 OWLs each covering 12m, all visible and controllable from one in-cab display.

**Setup requires two scripts:**

1. **On the central controller Pi:** Run `controller/networked/in-cab_controller_setup.sh`. This installs the MQTT broker, the networked dashboard, configures WiFi, and optionally sets up kiosk mode for a touchscreen.

2. **On each OWL Pi:** Run `owl_setup.sh`, select "yes" to dashboard, then select "Networked" mode. Enter the central controller's IP when prompted. The setup script configures WiFi as a client, writes `CONTROLLER.ini` with `mode = networked` and the broker IP pointing to the controller.

## GPS Setup

OWL supports GPS tracking via a Teltonika RUTX14 (or similar) industrial router that pushes NMEA sentences over TCP. GPS data is used for:

- Live speed, heading, and satellite display on the GPS tab
- Session statistics: distance travelled, time active, area covered (hectares)
- GeoJSON track recording saved to disk for each detection session

### How it works

The **central controller** (networked mode) runs a TCP server that listens for NMEA connections. The Teltonika router connects to this server and pushes standard NMEA sentences (`$GPRMC`, `$GPGGA`, `$GPVTG`, `$GPGSV`). The GPS Manager parses these, updates the live GPS state, and feeds the session tracker and track recorder.

```
Teltonika RUTX14          Central Controller (Pi)           Browser
    GPS antenna               GPSManager                    GPS Tab
        |                         |                            |
        +--- NMEA over TCP ------>| port 8500                  |
                                  |  parse NMEA                |
                                  |  update GPSState           |
                                  |  update SessionStats       |
                                  |  write GeoJSON track       |
                                  |                            |
                                  +--- /api/gps (JSON) ------->|
                                                               | speed, heading
                                                               | satellites, area
```

Sessions auto-start when any OWL begins detection and auto-stop when all OWLs stop.

### Step 1: Configure CONTROLLER.ini on the central controller

Edit `config/CONTROLLER.ini` on the Pi running the networked controller:

```ini
[GPS]
# Enable the GPS TCP server
enable = True

# TCP port to listen on (must match Teltonika config)
nmea_port = 8500

# Spray boom width in metres (for area calculation)
boom_width = 12.0

# Directory for GeoJSON track files
track_save_directory = tracks
```

The `source`, `port`, and `baudrate` keys are only used by owl.py for serial GPS modules -- they are ignored by the networked controller.

### Step 2: Configure the Teltonika RUTX14

On the Teltonika router's web interface (typically `192.168.1.1`):

1. **Services > GPS > General**: Enable GPS
2. **Services > GPS > NMEA Forwarding**:
   - Enable NMEA forwarding
   - Protocol: **TCP**
   - Hostname/IP: the central controller's IP address (e.g. `192.168.1.2`)
   - Port: **8500** (must match `nmea_port` in CONTROLLER.ini)
   - NMEA sentences: enable at minimum **GGA** and **RMC**. Also enable **VTG** (speed) and **GSV** (satellites) for full data.

### Step 3: Firewall

The setup script (`controller/shared/setup.sh`) automatically opens port 8500 in UFW:

```bash
sudo ufw allow 8500/tcp
```

If you didn't use the setup script, add this rule manually.

### Step 4: Verify

1. Restart the networked controller: `sudo systemctl restart owl-controller.service` (or run `python controller/networked/networked.py` manually)
2. Check the logs for: `GPS Manager started on port 8500` and `GPS client connected from ...`
3. Open the GPS tab in the browser -- it should show satellite count, speed, and heading once the Teltonika gets a GPS fix

### Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| GPS tab shows "DISCONNECTED" | GPSManager not started | Check `enable = True` in `[GPS]` section of CONTROLLER.ini |
| GPSManager started but no client connects | Teltonika can't reach the controller | Verify network connectivity: `ping <controller-ip>` from Teltonika. Check UFW: `sudo ufw status` should show 8500/tcp ALLOW |
| Client connects but no fix data | Teltonika GPS has no satellite lock | Check the Teltonika GPS status page. Move to a location with sky visibility. First fix can take several minutes |
| Speed shows 0 but position is valid | VTG sentence not enabled on Teltonika | Enable VTG in Teltonika NMEA forwarding settings |
| Track files are empty | Session never started | Detection must be enabled on at least one OWL. Sessions auto-start/stop with detection |

### GPS config reference

| Key | Used by | Default | Description |
|-----|---------|---------|-------------|
| `source` | owl.py | `none` | GPS source for individual OWL (`none`/`serial`/`tcp`) |
| `port` | owl.py | `/dev/ttyUSB0` | Serial port (when `source = serial`) |
| `baudrate` | owl.py | `9600` | Serial baud rate (when `source = serial`) |
| `enable` | networked controller | `False` | Start GPS TCP server on the central controller |
| `nmea_port` | networked controller | `8500` | TCP port for NMEA connections |
| `boom_width` | networked controller | `12.0` | Boom width in metres for area calculation |
| `track_save_directory` | networked controller | `tracks` | Directory for GeoJSON track files |

## Custom presets saved from the dashboard

When you save a new preset from the dashboard (standalone or networked), it creates a timestamped INI file in this directory (e.g. `config_20260207_153022.ini`). These contain only detection parameters. You can set any saved preset as the active boot config from the dashboard.
