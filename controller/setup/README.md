# OWL first-boot setup

Appliance-style first boot: a factory-fresh (or factory-reset) OWL broadcasts
its `OWL-XXXX` hotspot with a fixed setup password, and the OWL phone app
(private `owl-app` repo) walks the user from unboxing to a working unit.

This directory is **not** the standalone controller — it is a small,
self-terminating Flask API (`setup_app.py`, port 8088) that only exists while
the first-boot flag is present.

## Lifecycle

```
sudo bash install_firstboot.sh --arm          (image prep / factory reset)
        ↓ reboot
owl-firstboot.service starts (ConditionPathExists=/boot/firmware/owl-firstboot.flag)
        ↓
startup(): hotspot down → WiFi scan (cached) → hotspot up
        ↓
phone app joins OWL-XXXX (fixed password "owl-setup"), drives /setup/api/*
        ↓
either  finish(standalone): user sets a permanent hotspot password
or      wifi/join → OWL switches to the customer's network (auto-reverts to
        hotspot if the join fails) → finish(wifi)
        ↓
teardown: flag removed, avahi entry + ufw rule removed, service exits
          and can never restart until re-armed
```

## API

All endpoints are JSON under `/setup/api/`; errors are
`{"success": false, "error": "..."}`. See the `API.md` contract in the
owl-app repo for full request/response shapes.

**Contract versioning:** `info` (and the fleet controller descriptor, and
the standalone dashboard's `system_stats`) carry `contract_version`
(`APP_CONTRACT_VERSION` in `version.py`). The phone app checks it on first
contact and blocks with an update message on a mismatch instead of failing
mysteriously mid-wizard. Bump it ONLY for breaking changes — additive
fields don't bump; old servers without the field are treated as version 1.

| Endpoint | Method | Purpose |
|---|---|---|
| `/setup/api/info` | GET | device id, version, `contract_version`, hotspot, camera check, owl.service state |
| `/setup/api/camera/frame` | GET | single JPEG (proxy of owl.py :8001) |
| `/setup/api/camera/stream` | GET | MJPEG stream proxy |
| `/setup/api/camera/diagnostics` | GET | sensor-level triage when no frames arrive: `status` is `ok` \| `overlay_missing` (dtoverlay not in config.txt) \| `probe_failed` (kernel saw the sensor but it didn't come up; `-121` in `detail` = I2C NACK) \| `no_rpicam` |
| `/setup/api/wifi/scan` | GET | cached scan; `?rescan=true` cycles the AP (drops clients) |
| `/setup/api/wifi/join` | POST | `{ssid, password?}` (password omitted for open networks) → 202, switch happens 5 s later |
| `/setup/api/wifi/result` | GET | join outcome (persisted across reboots); includes `warning` for non-fatal problems (`owl_restart_failed`, `hotspot_unavailable`) |
| `/setup/api/status` | GET | state machine + network mode (polled pre/post switch) |
| `/setup/api/hotspot/restore` | POST | abandon client attempt, re-raise the AP |
| `/setup/api/controller/join` | POST | join a rig: `{ssid, password, device_id, static_ip, gateway, subnet_prefix, broker_ip, broker_port, dns?}` (identity assigned by the controller's `/api/fleet/reserve`; IPs and int ranges validated) → 202, switch 5 s later |
| `/setup/api/finish` | POST | `{mode: standalone\|wifi\|controller, new_password?}` → teardown |

## Controller join (networked rigs)

The join sequence: set hostname (`owl-N`) → rewrite `CONTROLLER.ini`
(`[MQTT] broker_ip/device_id`, `[Network] mode/static_ip` — all other
sections preserved) → join the rig WiFi with the assigned **static IP** →
restart `owl.service` so it heartbeats to the controller (which
auto-confirms the reservation; a failed restart is surfaced as
`warning: owl_restart_failed` rather than silently claiming success). On a
failed radio switch the OWL reverts to its hotspot, but hostname/config are
**deliberately kept** — they describe the target state, the reservation is
still held, and a retry re-runs only the switch. An **abandoned** join is
different: `POST /setup/api/hotspot/restore` rolls the identity back
(pre-join `CONTROLLER.ini` backup + hostname), so a cancelled wizard never
leaves the OWL pointed at a rig it never joined.
Verification polls `http://<static_ip>:8088/setup/api/wifi/result`.

## If the setup service can't start

`owl-firstboot.service` carries `OnFailure=owl-firstboot-fallback.service`:
after 3 failed starts inside 60 s, systemd launches `fallback_server.py`
(system python, stdlib only — the venv may be the broken part) on the same
port. It answers every request with 503 JSON containing the crashed unit's
journal tail, so the phone app surfaces the real error instead of
"Failed to fetch". The fallback is condition-gated on the flag like the
main unit and is removed by `--disarm`.

## Why the scan is cached

The Pi's radio cannot scan while it is an access point. `startup()` scans once
before raising the AP (no phone is connected yet, so nothing drops).
`?rescan=true` knowingly cycles the AP; the app warns the user and reconnects.

## Shipping / demo-image prep

One command on a fresh image:

```
bash owl_setup.sh --ship
```

Non-interactive: base install, standalone setup with hotspot `OWL-XXXX`
(suffix from the Pi serial) and the fixed setup password, then arms
first-boot.

**CM5 camera:** Compute Modules have **no camera autodetection** — the stock
`camera_auto_detect=1` silently does nothing, so a fresh flash never loads
the sensor overlay (no error, no I2C bus, no camera). Both `owl_setup.sh`
(before its camera checks) and `install_firstboot.sh --arm` write the fix to
`/boot/firmware/config.txt` under `[all]`: `camera_auto_detect=0`,
`dtoverlay=imx296` on **both** CSI ports (same sensor on both is safe; two
*different* sensors collide at I2C 0x1a), and the `i2c_csi_dsi` dtparams so
`i2cdetect` works in the field. Idempotent; skipped with a warning on
non-CM boards (Pi 4/5 autodetect works and must not be fought). Standalone
entry: `sudo bash install_firstboot.sh --camera-config` (exit 2 = changed,
reboot needed). The sensor name is `CAMERA_OVERLAY` at the top of
`install_firstboot.sh` — a sensor swap must also update `CAMERA_SENSOR` in
`firstboot_state.py` (a unit test cross-checks them). Rule of thumb: after
any re-flash, verify config.txt state before debugging hardware. As its final step it runs `dev/clean.sh --yes` detached, which
strips dev residue — **every WiFi profile except the OWL hotspot** (a dev
phone-hotspot password must never ship on a unit), shell history, SSH keys,
logs, cached credentials. If you are SSH'd in over a personal network the
session drops at that point by design; give it a minute
(`/var/log/owl-ship-clean.txt`), then focus the camera, power off, box it.

Manual route (existing installs): run `controller/shared/setup.sh` in
**standalone** mode (keep the `OWL-` SSID prefix — the phone app's
auto-connect matches `OWL-*` only) and answer yes to its arm prompt, or run
`sudo bash controller/setup/install_firstboot.sh --arm` yourself. Arming
refuses if no hotspot profile exists — that ordering is what guarantees a
phone can always reach an armed unit.

Bench verification: work through `BENCH_TEST.md` in this directory.

## Factory reset / re-entering setup

- From the standalone dashboard: Config tab → "Reset network setup", then reboot.
- Over SSH: `sudo bash controller/setup/install_firstboot.sh --arm && sudo reboot`
- From any PC with the SD card: create an empty file `owl-firstboot.flag` on
  the boot (FAT) partition — arming survives because the systemd unit stays
  installed after finish; only the flag is consumed.

## During first boot

`owl.py` still runs (it feeds the camera preview) but detection is **locked
off for as long as the flag exists** — the state thread re-asserts it, so a
flipped-on hardware detection switch or a dashboard toggle cannot click
relays on the bench. Normal control returns within seconds of setup
finishing (the flag is re-checked at a slow cadence off the hot path; when
the flag was absent at boot, nothing is ever checked).

## Security posture (deliberate, revisit before mass rollout)

The setup API has no authentication: the only barrier is the hotspot PSK,
which is fixed and public (`owl-setup`). That is acceptable for a
setup-window-only surface on demo units — anyone in radio range during the
minutes of setup could interfere, nothing more. Before this becomes the
out-of-box experience for every shipped unit (or gains cloud/Noktura
integration) it needs a real story: per-unit setup secrets (QR on the
chassis) or a pairing confirmation on the device.

## Tests

`pytest tests/test_setup_app_routes.py tests/test_network_manager.py
tests/integration/test_firstboot_integration.py -v` — the first two are
unit-level (mocked nmcli, manual timers); the integration suite runs the
real Flask app over a real socket with real timer handoffs against a
stateful fake nmcli. All platform-independent.
