# Web Controllers

OWL has two web-based dashboards for monitoring and controlling detection in the field. Which one you use depends on whether you have a single OWL or multiple OWLs on one rig.

## Do I need a controller?

No. OWL works perfectly fine without any web controller. By default (fresh install, no setup script run), OWL just runs detection -- camera in, relays out. If all you need is a box that detects weeds and triggers solenoids, you don't need any of this.

The web controllers are for when you want to:
- Start/stop detection from a touchscreen or phone
- Adjust detection thresholds with a live camera preview
- Monitor system health (CPU, memory, temperature)
- Save and load detection presets
- See GPS data and track coverage
- Control multiple OWLs from one screen

## The two controllers

### Standalone (`controller/standalone/`)

For a **single OWL unit** with its own WiFi hotspot.

The OWL Pi creates a WiFi network (e.g. "OWL-1") and runs everything locally -- detection, MQTT broker, web server, and dashboard. Connect to the hotspot from any device and open the dashboard in a browser. Typical setup is a 7" touchscreen mounted in the cab.

**What you get:**
- Live video feed with detection overlay
- Start/stop detection and image recording
- Sensitivity presets (low/medium/high)
- Threshold adjustment with sliders
- System health monitoring (CPU, temp, disk, memory)
- Config file management (save, load, delete presets)

**How to set up:**
```bash
# Run the main setup script
bash owl_setup.sh

# When prompted "Do you want to add a web dashboard?" -> yes
# When prompted for mode -> select "Standalone"
```

This installs the MQTT broker, configures the WiFi hotspot, sets up Nginx with SSL, creates the systemd service, and writes `CONTROLLER.ini` with `mode = standalone`.

**Access:** `https://owl-1.local/` or `https://10.42.0.1/` (after connecting to the OWL's hotspot)

### Networked (`controller/networked/`)

For **multiple OWL units** on a shared WiFi network, managed from one central screen.

A separate Pi acts as the central controller -- it runs the MQTT broker and the networked dashboard. Each OWL connects to the same WiFi network and publishes its status over MQTT. The controller discovers OWLs automatically and shows them all in one interface.

**What you get:**
- All connected OWLs visible in one dashboard
- Per-OWL status cards (online/offline, detection state, system health)
- Per-OWL controls (start/stop detection, restart service)
- Live video preview from any selected OWL
- Config editor with range sliders and live preview
- Push configs to individual OWLs or broadcast to all
- Config library (save, load, delete presets on the controller)
- GPS tracking with session stats, distance, area covered

**How to set up -- two steps:**

**Step 1: Set up the central controller Pi**
```bash
# On the controller Pi (the one with the touchscreen in the cab)
sudo bash controller/networked/in-cab_controller_setup.sh
```
This installs the MQTT broker, the networked dashboard, configures WiFi with a static IP, sets up Nginx, and optionally enables kiosk mode for the touchscreen.

**Step 2: Set up each OWL Pi**
```bash
# On each OWL Pi
bash owl_setup.sh

# When prompted "Do you want to add a web dashboard?" -> yes
# When prompted for mode -> select "Networked"
# Enter the central controller's IP when prompted
```
This configures WiFi as a client with a static IP, writes `CONTROLLER.ini` with `mode = networked` and the broker IP pointing to the controller.

**Access:** `https://owl-controller.local/` or `https://<controller-ip>/` (from any device on the same network)

## How MQTT connects everything

All communication between `owl.py` (the detection loop) and the dashboards happens over MQTT -- a lightweight messaging protocol designed for IoT devices.

### Standalone mode

Everything is on one Pi. The MQTT broker, owl.py, and the dashboard all talk over `localhost`. Topics are flat:

```
owl/state       -- OWL publishes its status here (heartbeat every 2s)
owl/commands    -- Dashboard sends control commands here
owl/config      -- Config data responses
```

### Networked mode

The MQTT broker runs on the central controller. Each OWL connects to it remotely and uses device-specific topics so the controller can tell them apart:

```
owl/owl-1/state       -- OWL 1's status
owl/owl-2/state       -- OWL 2's status
owl/owl-1/commands    -- Commands for OWL 1
owl/owl-2/commands    -- Commands for OWL 2
```

The controller subscribes to `owl/+/state` (wildcard) to discover all OWLs automatically. When an OWL stops sending heartbeats for 5 seconds, the controller marks it as offline.

## Directory structure

```
controller/
    standalone/             -- Single-unit dashboard
        standalone.py           Flask app
        templates/              HTML templates
        static/                 CSS and JS (per-controller)

    networked/              -- Multi-unit central controller
        networked.py            Flask app
        in-cab_controller_setup.sh  Setup script for the controller Pi
        templates/              HTML templates
        static/                 CSS and JS (per-controller)

    shared/                 -- Assets used by both controllers
        setup.sh                Setup script for OWL Pi dashboards
        css/                    Shared CSS modules (variables, buttons, etc.)
        js/                     Shared JS modules (API client, utilities, toast)
        images/                 Logos
```

Both controllers import shared CSS and JS from `controller/shared/` for consistent styling and behaviour. Controller-specific assets (layout, tabs, config editor) live in each controller's own `static/` directory.

## Troubleshooting

**Dashboard shows "MQTT Connected" but no OWLs appear (networked mode)**

The OWL is likely publishing to standalone topics (`owl/state`) instead of networked topics (`owl/{device_id}/state`). Check that `CONTROLLER.ini` on the OWL Pi has `mode = networked` under `[Network]`. If it says `standalone`, the OWL's MQTT messages won't match what the networked controller is listening for.

**Can't access the dashboard**

- Check the service is running: `systemctl status owl-dash` (standalone) or `systemctl status owl-controller` (networked)
- Check Nginx: `systemctl status nginx`
- Check MQTT: `systemctl status mosquitto`
- Test MQTT manually: `mosquitto_sub -h localhost -t "owl/#"` (you should see heartbeat messages)

**OWL shows as offline on the controller**

OWLs are marked offline after 5 seconds without a heartbeat. Check:
- Is owl.py running on the OWL? `systemctl status owl.service`
- Can the OWL reach the controller? `ping <controller-ip>` from the OWL
- Is MQTT working? `mosquitto_pub -h <controller-ip> -t "test" -m "hello"` from the OWL
