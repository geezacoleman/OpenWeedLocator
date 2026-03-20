# OWL Companion App - Scope Document

## Vision

A native Android companion app that turns OWL setup from a shell-script-on-a-Pi experience into a GoPro-style "unbox, power on, connect, go" experience. The app is the first thing a user touches after powering up their OWL kit.

---

## Architecture Overview

```
┌─────────────────┐         WiFi (Hotspot)         ┌──────────────────┐
│  Android App    │◄──────────────────────────────►│  OWL (Raspi)     │
│                 │   MQTT (1883) + REST (443)      │                  │
│  - Setup Wizard │   MJPEG stream (8001)           │  - owl.py        │
│  - Live View    │                                 │  - Flask API     │
│  - Fleet Mgmt   │         HTTPS                   │  - MQTT broker   │
│  - Boom Builder │◄────────────────────────────────►│                  │
│                 │                                 └──────────────────┘
│                 │         HTTPS
│                 │◄────────────────────────────────►┌──────────────────┐
│                 │                                 │ noktura.tech     │
│                 │                                 │  - Fleet API     │
│                 │                                 │  - Data sync     │
└─────────────────┘                                 └──────────────────┘
```

**Key insight:** The OWL already runs a WiFi hotspot (standalone mode), MQTT broker, Flask API, and MJPEG stream. The app leverages all of these — minimal firmware changes needed.

---

## Phase 1: Setup Wizard (GoPro-style onboarding)

### 1.1 Device Discovery & Pairing
- **Scan for OWL hotspots** — OWL creates WiFi AP named `OWL-{id}` (already implemented in `setup.sh`). The app scans for SSIDs matching `OWL-*` and presents them.
- **Connect to OWL WiFi** — Prompt user to join the OWL hotspot (password entry). On Android 10+, use `WifiNetworkSuggestion` API for seamless connect.
- **Verify connection** — Hit `https://10.42.0.1/api/owl/state` (existing endpoint) to confirm handshake. Display OWL version, hardware info from `version.py`'s `SystemInfo`.
- **mDNS fallback** — Resolve `owl-1.local` via NSD (Android Network Service Discovery) if IP is unknown.

### 1.2 WiFi Network Configuration
- **Show available networks** — New firmware endpoint: `GET /api/wifi/scan` returns nearby SSIDs detected by the Pi (`nmcli device wifi list`).
- **Configure home/farm WiFi** — User picks a network, enters password. New endpoint: `POST /api/wifi/connect` triggers `nmcli` connection on the Pi.
- **Static IP setup** — Optional advanced setting for networked mode. Mirrors what `setup.sh` does with `nmcli connection modify`.
- **Connectivity test** — Verify the OWL can reach the internet (for noktura.tech registration and firmware updates).

### 1.3 Device Naming & Identity
- **Name your OWL** — Friendly name (e.g., "Left Boom OWL") stored in `CONTROLLER.ini` `device_id`.
- **Set hostname** — Updates avahi/mDNS hostname so the OWL is reachable as `left-boom-owl.local`.

### 1.4 Camera Verification
- **Live preview** — Stream from `http://10.42.0.1:8001/stream` (existing MJPEG endpoint) directly in the app.
- **Focus assist** — Port the FFT focus metric from `focus_gui.py` to an overlay on the live stream. Show a focus quality bar that updates in real-time.
- **Lens check** — Guide user through cleaning/adjusting the lens with visual feedback.

### 1.5 Initial Detection Test
- **Run a test detection** — Send MQTT command to enable detection (`owl/commands` → `{"detection_enable": true}`), show detections overlaid on the live stream.
- **Sensitivity picker** — Present Low/Medium/High presets (already in `GENERAL_CONFIG.ini` `[Sensitivity_*]` sections) with visual examples.
- **"It's working!" moment** — When weeds are detected, show a satisfying animation/highlight. This is the excitement moment.

---

## Phase 2: Boom Configuration & Fleet Management

### 2.1 Boom Diagram Builder
- **Visual boom layout** — Canvas showing a top-down view of a spray boom.
- **Drag-and-drop OWL placement** — User places OWL units at positions along the boom (Position 1–N).
- **Nozzle mapping** — Each OWL position maps to relay outputs (relay_0–relay_3 in `[Relays]` config). Visual mapping: "OWL at Position 3 controls nozzles 9–12."
- **Boom width config** — Sets `boom_width` in `[GPS]` config for area coverage calculations.
- **Save/load layouts** — Persist boom configurations locally and optionally to noktura.tech.

### 2.2 Multi-OWL Fleet View
- **Auto-discovery** — In networked mode, subscribe to `owl/+/state` (existing wildcard topic) to find all OWLs on the network.
- **Fleet dashboard** — Card per OWL showing: online/offline status, CPU temp, detection count, last heartbeat.
- **Bulk configuration** — Push sensitivity presets, algorithm choices, or model files to multiple OWLs simultaneously (existing MQTT command pathway).
- **Fleet health alerts** — Push notifications when an OWL goes offline, overheats, or storage fills up (based on existing state data: `cpu_temp`, `disk_percent`).

### 2.3 GPS & Session Tracking
- **Live map view** — Plot OWL GPS positions on a map (data from `owl/{id}/gps` MQTT topic).
- **Session stats** — Distance covered, area sprayed, detection count, spray savings estimate.
- **Track history** — Stored locally, synced to noktura.tech when connected.

---

## Phase 3: Live Operations

### 3.1 Live Detection Feed
- **MJPEG stream** — Full-screen live view from port 8001 with detection bounding boxes overlaid (already rendered by `owl.py`).
- **Detection counter** — Real-time count with visual pulse effect on each detection.
- **Spray activity indicators** — Show which relays are firing (data from MQTT state: `detection_mode`).
- **Performance overlay** — FPS, loop time, CPU temp as a subtle HUD (all available in MQTT state).

### 3.2 Quick Controls
- **Detection on/off toggle** — MQTT: `owl/commands` → `{"detection_enable": true/false}`
- **Recording toggle** — MQTT: `owl/commands` → `{"image_sample_enable": true/false}`
- **Sensitivity slider** — Switch between Low/Medium/High or adjust thresholds live via MQTT.
- **Algorithm switcher** — Change between `exhsv`, `gog`, etc. (MQTT command).
- **Emergency stop** — Kill all relay actuation immediately.

### 3.3 Real-time Tuning
- **Threshold sliders** — ExG min/max, Hue, Saturation, Brightness with live preview showing mask overlay (mirrors existing web dashboard sliders).
- **Before/after split view** — Show raw frame vs detection mask side by side.
- **Save preset** — Save current thresholds as named preset to `config/` directory via MQTT/API.

---

## Phase 4: noktura.tech Cloud Integration

### 4.1 Device Registration
- **Account creation/login** — OAuth or email/password auth against noktura.tech API.
- **Register OWL** — Link device serial/ID to user's noktura.tech account.
- **Firmware updates** — Check for OWL updates, download and apply OTA (new firmware capability needed).

### 4.2 Data Sync
- **Detection image upload** — Trigger S3 upload (existing `upload_manager.py`) from the app, monitor progress.
- **Session data sync** — GPS tracks, detection logs, performance metrics pushed to noktura.tech.
- **Remote config management** — Push/pull detection presets between noktura.tech cloud and OWL devices.

### 4.3 Fleet Analytics (noktura.tech web)
- **Weed pressure maps** — Heatmaps from GPS + detection data across sessions.
- **Spray savings reports** — Estimated chemical reduction vs blanket spray.
- **Device health history** — Uptime, temperature trends, storage usage over time.

---

## Firmware Changes Required (OWL-side)

These are additions to the existing OWL Flask API and `owl.py`:

| Endpoint | Method | Purpose | Complexity |
|----------|--------|---------|------------|
| `/api/wifi/scan` | GET | List available WiFi networks | Low |
| `/api/wifi/connect` | POST | Connect to a WiFi network | Medium |
| `/api/wifi/status` | GET | Current WiFi connection status | Low |
| `/api/device/info` | GET | Hardware info, version, serial | Low |
| `/api/device/name` | POST | Set device name/hostname | Low |
| `/api/config/boom` | GET/POST | Boom layout & nozzle mapping | Medium |
| `/api/config/presets` | GET/POST/DELETE | Manage detection presets | Low (partially exists) |
| `/api/firmware/check` | GET | Check for updates | Medium |
| `/api/firmware/update` | POST | Apply OTA update | High |
| `/api/focus/metric` | GET (SSE) | Stream focus quality score | Low |

**Existing endpoints/capabilities already usable:**
- MQTT broker on port 1883 — all control commands
- MJPEG stream on port 8001 — live video
- Flask API on port 443 — state, config, system stats
- mDNS via avahi — device discovery
- S3 upload manager — data sync

---

## Android Tech Stack (Recommended)

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| Language | Kotlin | Modern Android standard |
| UI | Jetpack Compose | Declarative, fast iteration |
| Architecture | MVVM + Clean Architecture | Testable, scalable |
| Networking | Retrofit + OkHttp | REST API calls |
| MQTT | Eclipse Paho Android | Matches OWL's paho-mqtt broker |
| Video Stream | Custom MJPEG decoder (OkHttp streaming) | Low latency, no transcoding needed |
| WiFi Management | Android WifiManager / WifiNetworkSuggestion | Hotspot connection |
| Service Discovery | Android NSD (Network Service Discovery) | mDNS resolution |
| Maps | Google Maps SDK or Mapbox | GPS visualization |
| Local Storage | Room (SQLite) | Boom configs, session history |
| DI | Hilt | Dependency injection |
| Image Loading | Coil | Kotlin-first image loading |
| Push Notifications | Firebase Cloud Messaging | Fleet health alerts |

---

## App Screen Map

```
Splash → Onboarding (3 slides)
  │
  ├── Setup Wizard
  │   ├── 1. Scan for OWL hotspots
  │   ├── 2. Connect to OWL WiFi
  │   ├── 3. Camera preview + focus
  │   ├── 4. Configure home WiFi (optional)
  │   ├── 5. Name your OWL
  │   ├── 6. Run test detection → "It works!"
  │   └── 7. Register on noktura.tech (optional)
  │
  ├── Home Dashboard
  │   ├── OWL Status Card (online/offline, temp, detections)
  │   ├── Quick Actions (detect on/off, record, sensitivity)
  │   └── Live View thumbnail (tap to fullscreen)
  │
  ├── Live View (fullscreen)
  │   ├── MJPEG stream with detection overlay
  │   ├── Detection counter + relay indicators
  │   ├── Floating controls (sensitivity, algorithm)
  │   └── Performance HUD (FPS, temp)
  │
  ├── Tuning
  │   ├── Threshold sliders (ExG, Hue, Sat, Brightness)
  │   ├── Live mask preview
  │   ├── Sensitivity presets
  │   └── Save/load presets
  │
  ├── Boom Builder
  │   ├── Boom diagram canvas
  │   ├── Drag-drop OWL positioning
  │   ├── Nozzle-to-relay mapping
  │   └── Save/load boom layouts
  │
  ├── Fleet
  │   ├── All OWL cards (auto-discovered)
  │   ├── Per-OWL detail view
  │   ├── Bulk config push
  │   └── Health alerts
  │
  ├── Map & Sessions
  │   ├── GPS track view
  │   ├── Session history
  │   └── Detection heatmap
  │
  └── Settings
      ├── noktura.tech account
      ├── Data sync preferences
      ├── Firmware updates
      └── App preferences
```

---

## Development Phases & Effort Estimates

### MVP (Phase 1) — Setup Wizard + Live View
- Device discovery & WiFi pairing
- Camera preview with focus assist
- Detection test with sensitivity presets
- Live MJPEG stream with detection overlay
- Basic quick controls (on/off, sensitivity)
- ~6-8 firmware endpoints to add

### Phase 2 — Boom Builder + Fleet
- Boom diagram UI with drag-drop
- Multi-OWL discovery and management
- Bulk configuration
- Health monitoring & alerts

### Phase 3 — Cloud Integration
- noktura.tech account & registration
- Data upload monitoring
- Remote config management
- Session analytics

### Phase 4 — Polish & Advanced
- OTA firmware updates
- Weed pressure heatmaps
- Spray savings reports
- iOS port (Kotlin Multiplatform or Swift)

---

## Key Design Principles

1. **Leverage what exists** — OWL already has MQTT, Flask, MJPEG, mDNS, S3 upload. The app is primarily a native mobile frontend to existing capabilities.
2. **Offline-first** — The app must work fully with just OWL hotspot (no internet). Cloud features are opt-in.
3. **Instant gratification** — Get the user to see their first weed detection within 5 minutes of opening the box.
4. **Farm-proof UI** — Large touch targets, high contrast, works in sunlight, works with gloves. Minimal text entry.
5. **Progressive disclosure** — Setup wizard covers the basics. Advanced tuning and fleet management are there when needed.
