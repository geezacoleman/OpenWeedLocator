# First-boot setup — on-Pi bench test

Scripted smoke run for the phone-app setup flow. Software-level behaviour is
covered by `tests/integration/test_firstboot_integration.py`; this checklist
verifies the parts only real hardware can: the radio, mDNS, systemd, and the
Android app itself.

**Kit:** OWL Pi (standalone image), Android phone with the owl-app APK,
a WiFi router you control (for the join + rig tests), and for Part 4 an
in-cab controller Pi.

Tick every box. Any failure: grab `journalctl -u owl-firstboot -b` before
touching anything.

---

## Part 1 — Arm + boot into setup mode

- [ ] Provision the unit: `bash owl_setup.sh --ship` (fresh image; type `SHIP`
      at the prompt, or add `--yes`), or on an
      existing install `sudo bash controller/setup/install_firstboot.sh --arm`
- [ ] **CM5 carriers only:** on a fresh flash the script reports it wrote the
      camera config and warns a reboot is needed (CMs have no camera
      autodetect). After the reboot: `rpicam-hello --list-cameras` lists
      `imx296`, and `/boot/firmware/config.txt` has `camera_auto_detect=0`
      plus the `dtoverlay=imx296` block under `[all]` exactly once (re-run
      `sudo bash controller/setup/install_firstboot.sh --camera-config` →
      "already present", no duplicate lines)
- [ ] On a Pi 4/5 (non-CM) the same command **skips** with a warning and
      leaves config.txt untouched
- [ ] If the camera is missing:
      `curl http://10.42.0.1:8088/setup/api/camera/diagnostics` distinguishes
      `overlay_missing` (config.txt problem) from `probe_failed` (-121 I2C
      NACK: unpowered / wrong CSI port / hardware)
- [ ] Arming with **no hotspot profile** refuses with a clear message and
      changes nothing (`nmcli con delete OWL-XXXX` first to simulate; restore
      after)
- [ ] Reboot. Within ~60 s the `OWL-XXXX` hotspot broadcasts
- [ ] `sudo systemctl status owl-firstboot` → active, no restart loop
- [ ] Join the hotspot from a laptop (password `owl-setup`):
      `curl http://10.42.0.1:8088/setup/api/info` → JSON with camera + hotspot
- [ ] owl.py is running but **detection is off**: flip the hardware detection
      switch (if fitted) ON — relays must stay silent while the flag exists

## Part 2 — Phone wizard, standalone path

- [ ] Open the app → "Set up an OWL" → it auto-joins `OWL-XXXX` (expect the
      Android connection dialog) and shows the camera check
- [ ] Camera feed is live; cover the lens and confirm the image responds
- [ ] Choose Standalone → set a permanent password → done screen shows the
      QR; scan it with a second phone and confirm it joins the hotspot with
      the NEW password
- [ ] `curl http://10.42.0.1:8088/` now refuses — the setup service exited
- [ ] Reboot: hotspot uses the new password, owl-firstboot does NOT start,
      detection behaves normally (switch works)
- [ ] Dashboard → Config → "Reset network setup" → reboot → unit is back in
      setup mode with password `owl-setup`

## Part 3 — Phone wizard, join-WiFi path (+ failure rollback)

- [ ] Re-arm (Part 2 last step). Run the wizard, choose your test router,
      enter a **wrong password** first
- [ ] Within ~60 s the phone reports the failure and the `OWL-XXXX` hotspot
      is back (auto-revert) — reconnect and confirm the error mentions the
      password
- [ ] Retry with the right password → wizard verifies and finishes
- [ ] If your router blocks mDNS: confirm the wizard offers the manual-IP
      entry and it works
- [ ] From a laptop on the router: `curl http://<owl-ip>:8088/...` fails
      (service gone) but `https://<owl-hostname>.local/video_feed` serves

## Part 4 — Rig flow (controller + OWL + router)

- [ ] Controller Pi on the rig network; app → "Connect to a controller" →
      discovers it (or manual IP) and saves the rig
- [ ] "Add an OWL" on a freshly-armed unit → wizard assigns `owl-N`, the OWL
      joins the rig network on its static IP, and the controller dashboard
      shows it with its friendly name
- [ ] `config/fleet.json` on the controller has the device as `registered`
      (auto-confirm via heartbeat)
- [ ] Wrong rig-WiFi password: OWL reverts to its hotspot, the reservation
      survives on the controller, and a retry completes without a new slot
- [ ] Power the OWL off: dashboard shows it as an **Offline** card (not gone)
- [ ] Rename + remove the OWL from the phone (token path) and from the
      controller kiosk (no token needed)
- [ ] Abandon test: start "Add an OWL", fail the join, quit the wizard, use
      the app's hotspot-restore/cancel path → on the OWL,
      `config/CONTROLLER.ini` is back to standalone and the hostname is the
      pre-join one (`hostnamectl`)

## Part 5 — Rough handling

- [ ] Pull power mid-wizard (during "Switching networks") → on reboot the
      unit comes back in a usable state (hotspot or joined, never dark)
- [ ] Kill the phone app mid-join and reopen → the saved-OWL/home flow can
      reach the unit again
- [ ] Leave the wizard idle 35+ min on the rig reserve step, then finish →
      the OWL still registers (tombstone revive) and no second unit was
      given the same owl-N (reserve one on a second phone meanwhile)

## Part 6 — R1 (v3.9.5) quiet journal + honest gauges

- [ ] No USB drive, app polling the control screen for 10 min:
      `journalctl -u owl -u owl-dashboard --since -10m` contains no lsusb
      warnings, no sudo/LED spam, no repeated storage warnings, and no
      ANSI "Raspberry Pi Version Warning" blocks
- [ ] On a unit without passwordless sudo (or a sealed CM5): exactly one
      "LED signalling disabled" line at boot, nothing per blink afterwards
- [ ] Background/reopen the app 5x on the control screen: gauges update
      smoothly, no value flicker, the camera stream resumes live (not a
      frozen frame)
- [ ] Unplug the camera → single error signal path, journal quiet
      afterwards; replug + restart clean
- [ ] Toggle recording with no USB drive: one warning in the journal on the
      first attempt (DEBUG thereafter), honest snap-back in app + dashboard
      each attempt
- [ ] Insert a USB drive after a failed attempt, toggle recording again:
      recording starts, the latched LED error clears, no restart needed
- [ ] Dashboard slider "min weed size": set a sub-1% value, save, reload →
      the value survives (never becomes 0)

## Part 7 — R1 SSID collision honesty

- [ ] Two units saved in the app, both on the same SSID (legacy default),
      only unit B powered: unit A's entry shows "Wrong OWL / Another OWL is
      using this network name", unit B's entry shows Online
- [ ] Tapping unit A's entry warns which unit answered and does NOT open
      the control screen
- [ ] Fresh `controller/shared/setup.sh` run: offered hotspot default is
      `OWL-<last 4 of serial>`, not `OWL-1`
- [ ] `install_firstboot.sh --arm` on a unit with SSID `OWL-1`: renames to
      the serial-derived SSID and says so; custom SSIDs are left alone
- [ ] Legacy app entry (saved before serials): still shows Online against
      matching firmware and silently adopts the serial (visible in a later
      mismatch test)

## Part 8 — R2 (v3.10.0) eMMC recording + app Downloads tab

- [ ] Fresh OWL 3.0 config (`storage_location = internal`): recording lands
      in `/home/owl/owl_images/YYYYMMDD/session_*`
- [ ] `storage_location = auto` on a bench Pi: records to USB when a drive
      is mounted; pull the drive + reboot → records internally
- [ ] `fallocate -l NG /home/owl/filler` to breach the 4 GB floor →
      recording snaps off within 15 s, app toast + phone buzz, dashboard
      notification; delete the filler → recording re-enableable WITHOUT a
      restart
- [ ] Record a ~2 GB session → download to the phone over the hotspot
      end-to-end: confirm sheet shows size + sane ETA, progress bar moves,
      the ZIP lands in the phone's Downloads, opens, EXIF intact
- [ ] Cancel a download mid-pull → no partial file in the phone's Downloads
- [ ] Background the app mid-download → download stops (link drops); the
      pre-download "keep the app open" warning was shown
- [ ] While a session is recording: its row shows "Recording now" and
      delete is refused ("Stop recording first"), from phone AND via curl
      DELETE (409)
- [ ] Delete a finished session from the phone → space is freed, storage
      bar updates within one poll
- [ ] Power-cut mid-recording → unit boots clean, session intact minus the
      queued tail
- [ ] `df -h` before/after a long session: free space never below the
      floor; `ls /tmp` shows no leftover zips; journal quiet throughout
- [ ] `journalctl -u owl -u owl-dashboard` during a full-floor event: one
      storage warning per transition, not per cycle

## Part 9 — R3 (v3.11.0, contract 2) device settings + Config tab

Privileges + token:

- [ ] Re-run `controller/shared/setup.sh` on a deployed unit: sudoers files
      `95-owl-device-settings` + updated `97-owl-service-control` present,
      `/usr/local/sbin/owl-set-user-password` root-owned 0755,
      `sudo -n nmcli --version` works as the owl user, `sudo -n reboot`
      permitted (check `sudo -l`)
- [ ] Provision a fresh unit via the app: `device_token` appears in
      CONTROLLER.ini [Security] AND in the app's saved-device record
- [ ] Legacy check: delete the [Security] section, restart the dashboard →
      all settings routes work headerless; restore the token → requests
      without X-Device-Token get 403, the app (which stores the token)
      still works
- [ ] Token-guarded delete: on a token-minted unit, curl DELETE a session
      without the header → 403; the app's Downloads delete still works.
      On a no-token legacy unit, headerless curl DELETE still works

Network settings (standalone unit):

- [ ] From the app device-settings screen: scan while on the hotspot →
      "hotspot blinks" warning, phone drops ~15 s, auto-rejoins, network
      list appears
- [ ] Hotspot → home Wi-Fi in-place change: 202, phone prompted to join the
      same Wi-Fi, app rediscovers the OWL at `https://<host>.local`, saved
      record flips to wifi mode
- [ ] Wrong home-Wi-Fi password: unit reverts to the hotspot by itself,
      app shows "wrong password" (auth) from the switch result
- [ ] While on home Wi-Fi, change to a second Wi-Fi: revert-on-failure goes
      back to the FIRST Wi-Fi (not the hotspot)
- [ ] Rig-joined (networked-mode) OWL: join route returns 409 "manage from
      the rig controller"
- [ ] Hotspot password change while on the hotspot: 202, AP re-keys ~5 s
      later, app rejoins with the new PSK
- [ ] Hotspot password change while on client Wi-Fi: synchronous success,
      client connection NOT dropped; new PSK works after the next
      hotspot fallback

Passwords + power:

- [ ] Change the Pi user password from the app → SSH in with the new
      password; journal contains no trace of the password value
- [ ] Shutdown from the app with owl.service running → clean MQTT-path
      shutdown; with owl.service stopped → direct fallback still powers off
- [ ] Reboot from the app → unit comes back, dashboard reachable
- [ ] Rig screen "Shut down rig" → all OWLs notified, then the controller
      powers off

Camera white balance (Phase 3.5):

- [ ] Untouched config → `awb_mode = auto` (v3.14 default; the old daylight
      lock rendered Arducam IMX296 red). Feed colour matches `rpicam-hello`
      in the same spot; a profile saved without the WB keys still shows
      the WB fields in the kiosk editor after load
- [ ] Kiosk Advanced Settings (standalone): changing `awb_mode`,
      `awb_red_gain`, `awb_blue_gain` or `exp_compensation` applies to the
      feed within ~1 s WITHOUT pressing Save (v3.14 hot-apply, same route as
      the phone app); the field's unsaved marker clears once applied.
      Lowering `awb_red_gain` in manual visibly reduces the red cast live
- [ ] Networked Config tab Advanced Settings: same four keys apply to ALL
      OWLs on change (live only, same target as Apply/Save); Save to
      Device persists. "Calibrate white balance" calibrates every OWL
      (each samples its own camera); readout follows the editor's OWL;
      after completion the WB fields are NOT marked unsaved and a following
      Apply must not overwrite other OWLs' gains
- [ ] Networked, 2+ OWLs with different crops: load owl-1 in Advanced
      Settings, change `rotation`, Apply -> owl-2's lanes/crop unchanged
      (check its feed overlay); toast lists nothing skipped. Edit
      `crop_left` in the editor, Apply -> toast "Not sent to all OWLs
      (per-OWL settings): Camera.crop_left"; Push to device sends it to
      owl-1 only and a following Apply does not re-send it
- [ ] Networked Config tab: Calibrate white balance -> every OWL's readout
      goes running -> complete with its OWN gains (no 404 toast)
- [ ] Kiosk (standalone) Config tab: type 50 into awb_red_gain -> feed
      applies 8.0, INI carries 8.0, `systemctl restart owl.service` comes
      back up (validator passes). awb_mode typed as garbage in the app ->
      rejected toast, INI unchanged
- [ ] Profile switch on the kiosk, then Calibrate (or move a slider), then
      reboot -> the change survives (active_config.txt was repointed to the
      autosave; dashboard shows "<profile> - unsaved changes")
- [ ] Config tab → "Calibrate white balance" with a grey card / bare soil in
      view → readout shows gains + colour temperature, `awb_mode` flips to
      manual with those gains, `config_autosave.ini` carries them, image
      stays neutral when green foliage fills the frame
- [ ] manual → daylight → auto: colour re-adapts (AwbEnable re-enabled)
- [ ] Change `exp_compensation` live → brightness changes (was a silent
      no-op before restart)
- [ ] `systemctl restart owl.service` → tuned WB persists (autosave)
- [ ] USB-webcam rig: WB keys → one warning logged, no crash
- [ ] Hand-edit `awb_red_gain = 0.05` → config load fails with a clear
      validation message

App Config tab:

- [ ] Config tab: dual-knob rails + fine-tune buttons mirror the kiosk;
      slider changes visibly move the detection overlay on the live feed
- [ ] Profile sheet: switch profile, reset to default, save-as-new →
      Modified pill clears, new profile listed
- [ ] Camera card: WB dropdown, manual reveals gain steppers, tuning
      against the live feed works over the hotspot
- [ ] Advanced section: change resolution → restart-required badge →
      "Restart detection" button restarts owl.service (token-guarded)
- [ ] Appbar power icon (all three tabs): bottom sheet Shut down / Restart
      device / Cancel; sheet acts as the confirm
- [ ] Old firmware (contract 1) + new app: Config tab hidden/disabled with
      the "update OWL firmware" hint, Dashboard + Downloads still work

## Part 10 — v3.11.1 bench-bug fixes (ownership, power, recording, video)

CONTROLLER.ini ownership (the root cause of the 2026-08-15 bench failures —
a root-owned file silently disabled MQTT and the token guard):

- [ ] Fresh `--ship` provision + app finish: `ls -l ~/owl/config/CONTROLLER.ini`
      shows `owl:owl` and mode 640 (firstboot now chowns after every write)
- [ ] `mosquitto_sub -v -t 'owl/#' -W 8` shows LIVE owl/state traffic
      (not just a stale retained owl/status)
- [ ] Sabotage check: `sudo chown root:root ~/owl/config/CONTROLLER.ini &&
      sudo chmod 600 ...` then restart both services → owl.service AND
      owl-dash journals each log a prominent "CONTROLLER.ini exists but
      could not be read" error (silent skip is fixed). Restore ownership.

Power honesty:

- [ ] App power sheet "Restart device" → Pi actually reboots; pill goes
      Restarting… → back to Running; stream recovers without switching tabs
- [ ] "Shut down" → Pi powers off (MQTT path); repeat with owl.service
      stopped → direct fallback powers off
- [ ] Stop mosquitto (`sudo systemctl stop mosquitto`) → reboot from the
      app still reboots via the direct backstop; journal shows the MQTT
      failure logged, not swallowed

Token recovery:

- [ ] Delete the OWL from the app, re-add via "Connect to an OWL" →
      power/settings actions work again WITHOUT re-provisioning (the app
      silently re-earns the token with the hotspot password; the journal
      logs "Device token re-issued")
- [ ] Wrong password 5x via curl on /api/settings/token → 429 lockout

Recording to device storage:

- [ ] No USB drive, default config (`storage_location = auto`): record from
      the app → recording STARTS to internal storage, one toast "No USB
      drive. Recording to device storage.", toggle stays on
- [ ] Session metadata entered in the app appears in the new session's
      session_metadata.json (no "save_directory not available" warning)
- [ ] Plug a USB drive in, stop + start recording → new session lands on
      the USB drive
- [ ] `storage_location = usb` (strict): no drive → toggle snaps back WITH
      the "No USB drive found" toast (reason published, not silent)
- [ ] Internal free space below min_free_gb → refusal with "Storage full"

Video + rotation:

- [ ] "Restart detection" from the app config tab → pill shows Restarting…,
      feed shows offline overlay, then recovers by itself (~25 s)
- [ ] Config mini feed letterboxes the FULL frame (no top/bottom crop)
- [ ] IMX296 GS unit: image is upright with no config (rotation auto);
      `rotation = 180` flips it; HQ/CM3 unit stays flipped by default
- [ ] Delete config_autosave.ini + restart → dashboard self-heals (re-seeds
      from GENERAL_CONFIG.ini), WB card still works, saves work

Power-cut durability (2026-08-15 field find — an unsynced power-off after
--ship un-armed the unit AND zero-filled the /etc/hosts tail, breaking sudo):

- [ ] `--ship` → wait for SHIP-READY → immediately PULL POWER (no shutdown) →
      on next boot the flag still exists, owl-firstboot starts, hostname
      resolves (`sudo true` prints no resolve warning)
- [ ] `sudo /usr/local/sbin/owl-firstboot-arm` → pull power → flag survives

## Part 11 — v3.16.0 image metadata (issue #206)

Issue #206: recorded images had a blank "Date Taken", because the old sampler
wrote a GPS-only block and skipped metadata entirely without a fix. Prep:
`cd ~/owl && git log -1 --oneline` shows the v3.16.0 commit, `workon owl`,
`python -c "import piexif; print(piexif.VERSION)"`. Run `timedatectl` and write
down **Time zone** and **System clock synchronized** — every check below is read
against those two values.

- [ ] **T1 no GPS, no network, default UTC** (the reporter's worst case).
      Unplug GPS and network, recording on, low `sample_frequency`.
      `exiftool -DateTimeOriginal -OffsetTimeOriginal -ModifyDate <newest jpg>`
      → DateTimeOriginal present and non-blank, offset `+00:00`, no GPS block.
      This is the exact case that used to be empty.
- [ ] **T2 timezone set, still no GPS.** `sudo timedatectl set-timezone
      Pacific/Auckland`, restart, record. DateTimeOriginal reads local wall
      clock, offset `+12:00`/`+13:00`, **and DateTimeOriginal minus the offset
      equals the UTC instant in the filename prefix** (filename and tag come
      from the same capture stamp, so this is the strongest check available).
- [ ] **T3 Windows view** — copy a JPEG to a Windows machine, Properties →
      Details → "Date taken" is populated and matches T2. exiftool agreeing is
      necessary but not sufficient; the complaint was about consumer tooling.
- [ ] **T4 GPS fix present.** Reconnect GPS, wait for a fix.
      `exiftool -GPSDateStamp -GPSTimeStamp -GPSLatitude -GPSLongitude
      -DateTimeOriginal` → GPS date/time present and equal to the UTC instant,
      lat/lon within ~0.2 m of the known bench position, date tag still there.
- [ ] **T5 clock deliberately wrong.** `sudo systemctl stop systemd-timesyncd;
      sudo date -s '2019-01-01 00:00:00'`, record one frame with GPS connected
      → DateTimeOriginal present but 2019, **and GPSDateStamp is the real
      date**. Proves the satellite path is independent and that the tag is
      never silently dropped. Restore: restart timesyncd, reboot.
- [ ] **T6 all three sample modes.** `sample_method` = `whole`, `bbox`,
      `square` in turn. Every image carries DateTimeOriginal; every crop also
      carries `roi_x/roi_y/roi_width/roi_height` and `source_width/height` in
      its ImageDescription, and the ROI sits inside the source frame.
      `locations.jsonl` has one entry per frame listing N filenames.
- [ ] **T7 richness.** On one image:
      `exiftool -ImageDescription -UserComment -Make -Model -Software <jpg>`
      → ImageDescription carries device_id, algorithm, session_id,
      session_start, detections, the detection thresholds, and any session
      metadata typed on the dashboard; UserComment carries the raw camera
      metadata; Make/Model/Software populated.
- [ ] **T8 app path.** Reset to `Etc/UTC`, connect the phone app, confirm
      `POST /api/time/sync` returns `timezone_set: true`, record, confirm the
      offset tag follows.

## Part 12 — v3.16.0 detection and spray path

Nothing in Parts 1-11 covers the thing the machine actually does. **Run every
failsafe check with the nozzles on water or disconnected, never on chemical.**

Failsafes (do these first):

- [ ] **Service restart mid-spray.** Trigger a continuous detection so a nozzle
      is held on, then `sudo systemctl restart owl.service`. The nozzle must
      de-energise. Before v3.16 SIGTERM was unhandled and it stayed on.
- [ ] **Kill mid-spray.** Repeat with `sudo systemctl kill -s SIGKILL
      owl.service` and watch the relay LED. Record what happens — there is no
      software failsafe on SIGKILL, release depends on the kernel dropping the
      pin.
- [ ] **Camera unplugged mid-run.** Journal shows "No frame from camera -
      retrying", then a loud error after ~0.5 s. Before v3.16 a single dropped
      frame ended the job on one quiet log line.
- [ ] **Camera brief glitch.** Reseat the connector quickly → journal shows
      "Camera recovered after N dropped frame(s)" and the run continues.
- [ ] **Config typo clamp.** Set `actuation_duration = 15` by hand, restart →
      journal logs "[CONFIG] actuation clamped to safety bounds" and the spray
      is 5 s, not 15.

Lane mapping and timing:

- [ ] **Lane check.** Move a weed target across the frame left to right. Each
      relay fires in order, one at a time, with no gaps or overlaps. Repeat
      with an asymmetric crop (`crop_left=0.05, crop_right=0.20`) — lane
      boundaries must move with the crop.
- [ ] **Band check.** Place a target above `actuation_top` and below
      `actuation_bottom` → no relay fires in either case.
- [ ] **Capture-to-switch latency.** Film the target and the relay LED with a
      phone at 120 fps or higher; count frames between the target entering the
      band and the LED lighting. Record the number — it is the one figure that
      decides whether chemical lands on the weed, and nothing measures it
      today. The window anchor stays after inference (the v3.16 move to frame
      capture was reverted: it shortened sprays). v3.17 takes the processing
      time off the delay instead and reports it as "Processing N ms".
- [ ] **Processing time.** With detection running, the dashboard shows
      "Processing N ms". With `delay > 0`, the phone 120 fps method should show
      the nozzle opening N ms earlier than before v3.17.
- [ ] **No stats stall.** With `log_fps = True`, the loop time no longer spikes
      every ~3 s (stats moved off the detection loop).
- [ ] **Clock step.** While spraying, `sudo date -s '+1 hour'`: no nozzle stays
      on (relay windows run on the monotonic clock).
- [ ] **Loop time.** Set `log_fps = True`, run 5 minutes, record the reported
      FPS and average loop time at the working resolution.

Speed-adaptive timing (networked controller):

- [ ] **GPS dropout.** Drive at a steady speed, then disconnect the receiver.
      Within 30 s the dashboard must stop using the last speed and fall back to
      the configured duration/delay; the GPS group greys out. Before v3.16 the
      last speed ever seen was reused indefinitely, so stopping the tractor
      left the boom timing for the speed it was doing when the fix died.
- [ ] **Speed sweep.** 4, 8, 12 km/h with a known target: spray lands on the
      target at every speed.
- [ ] **Controller loss.** Stop the controller mid-run → OWL keeps its last
      broadcast timing (known limitation, no expiry yet). Confirm and record.

Offline replay (no hardware, run before and after any detection change):

- [ ] `python tools/replay_eval.py baseline owl_data/<session> -o eval/<name>.json`
      on a known session, then `compare` after the change. Detection counts and
      relay choices must not move unless you intended them to.

## Part 13 — nRF9151 GPS discovery (production CM5 carrier)

Goal: find the nRF9151's port, baud rate and firmware, so `NRF_GPS_*` in
`owl_setup.sh` and the OWL GPS source can be filled in. Stop owl.service first.

Findings so far (2026-09-13, owl-1):

- Only `/dev/ttyAMA10` (CM5 debug UART, boot console + getty) and `/dev/ttyS0`
  (built-in Bluetooth) exist. Neither is the nRF9151.
- No USB bridge in `lsusb`, no ModemManager, `owl` already in `dialout`.
- No `dtoverlay=uart*` in config.txt, so no header UART is enabled.
- nRF9151 runs Nordic Serial LTE Modem (confirmed by Guy): AT URCs, no NMEA.
- No UART (uart0/2/3/4-pi5, with and without ctsrts) answered `AT`.
- Pull-flip test: GPIO 8, 9, 10, 11 are held LOW under both pull-up and
  pull-down; every other tested GPIO (4-7, 12-15, 17-19, 25-26) floats.
  That block is exactly `uart3-pi5,ctsrts` (TX 8, RX 9, CTS 10, RTS 11 ->
  /dev/ttyAMA3), i.e. a 4-wire flow-controlled link. All four lines low
  (including the nRF's TX and RTS outputs) points at the nRF side being
  unpowered, held in reset or not enabled. Not yet tested: GPIO 2, 3, 16,
  20-24, 27 (16 read low against a pull-up earlier).
- Boot overlay `dtoverlay=uart3-pi5,ctsrts` (config.txt, rebooted): ttyAMA3
  exists, pins muxed (8 TXD3 hi, 11 RTS3 hi) but RXD3 (9) and CTS3 (10) still
  read LOW with the UART pull-ups on, and `AT` gets no reply. An nRF in reset
  but with its I/O rail powered would float those pins and the pull-up would
  win, so the nRF's I/O supply (or a buffer in between) is most likely off.
  Remove the overlay until nRF power is confirmed: the Pi TX/RTS driving 3.3 V
  into an unpowered chip back-feeds it through its protection diodes.
- Remaining pins: GPIO 2 and 3 held HIGH both ways (I2C1 bus with board
  pull-ups, so the carrier has I2C parts), GPIO 16 held LOW both ways (prime
  candidate: pulled-down enable of the nRF supply), GPIO 21 held HIGH both
  ways, GPIO 20 floats. Note OWL itself uses GPIO 16 (Ute switch, BOARD36),
  20 (GPS LED, BOARD38) and 21 (status LED, BOARD40) - a production-carrier
  config must not drive whichever of these the board repurposes.
- GPIO 16 driven HIGH (op dh) for 10+ s: GPIO 9/10 stayed LOW and uart3 `AT`
  still silent, so GPIO 16 is not the nRF supply enable. OWL services do not
  claim GPIO 16/20/21 with controller_type = none (relays 22/23/24/27 only).
- `i2cdetect -y 1` (GPIO 2/3): no devices answer despite board pull-ups.
- Conclusion: software-side search exhausted. Next is hardware: meter the
  nRF9151 VDD / VDD_GPIO / ENABLE, or ask the board designer what powers it.
- Correction after full `pinctrl` + forum research: GPIO 21 reads HIGH against
  the Pi pull-down. SLM's CONFIG_SLM_POWER_PIN (pull-up, active-low pulse =
  wake/power-off) or INDICATE pin (idles high) would read exactly like that,
  and both need the nRF powered - so it may be powered but in SLM idle with
  its UART off. Test: 200 ms low pulse on GPIO 21, re-read GPIO 9, AT probe on
  ttyAMA3; if silent, pulse again (the pulse toggles). GPIO 21 is also OWL's
  default status_led_pin (BOARD40) - must be `none` on this carrier.
- regulator_summary: only CM5-internal rails (3v3, 5v0, SD, WL, cam regs); no
  kernel-controlled modem rail. dmesg: no GPIO hogs or custom overlays at
  boot. So unlike the IMX296, no boot-config regulator/overlay powers the nRF.
- GPIO 21 pulsed low 200 ms twice (uart3 overlay loaded): GPIO 9/10 stayed LOW,
  `AT` silent both times. GPIO 21 is not the SLM power/wake pin.
- Status: every host-side path tested. RXD3/CTS3 held low with no response is
  either an unpowered/disabled nRF (or level shifter), or board pull-downs on
  a sleeping nRF with a wake pin not on GPIO 0-27. Only a meter, SWD access or
  the board designer can separate those. Remove the uart3 overlay from
  config.txt until then.
- SCHEMATIC (CM5 carrier, U4B), supersedes the guesses above:
  - GPIO 26 = `5G_ENABLE` (modem power enable). Never driven in any test above.
  - GPIO 8 = UART3_TX -> nRF, GPIO 9 = UART3_RX <- nRF. TX/RX ONLY, no RTS/CTS:
    use `dtoverlay=uart3-pi5` WITHOUT `ctsrts`, and `stty -crtscts`.
  - GPIO 10 = PI_OUTPUT_17 (switched light), GPIO 11 = PI_OUTPUT_6. The ctsrts
    tests drove GPIO 11 (RTS3), i.e. an output channel. Never use ctsrts here.
  - GPIO 2,3,4,5,6,7,10,11,12,13,17,18,22,23,24,25,27 = PI_OUTPUT_1..17. GPIO 2/3
    are outputs 13/14, not I2C: do not enable i2c_arm on this board.
  - GPIO 14/15 = bootloader UART (the TX/G/RX header). GPIO 21 = STATUS_LEDS.
    GPIO 16, 19, 20 not connected. GPIO_VREF = 3.3 V.
- Next test: uart3-pi5 (no ctsrts), listen on ttyAMA3, drive GPIO 26 high,
  expect SLM `Ready`, then `AT` -> OK with flow control off.
- nRF9151 sheet (U12): nRF TX = P0.18 (pin 79, via R41 33R) -> UART_LTE-RPI,
  nRF RX = P0.19 (pin 80) <- UART_RPI-LTE. No RTS/CTS (designer note: "check
  whether we should connect RTS/CTS etc or just TX/RX is ok"). The SLM build
  must put its host UART on P0.18/P0.19 with hw-flow-control removed; stock DK
  builds use other pins and would stay silent.
  - GPS pin (42) and AUX (37) are no-connect; only ANT (35) goes to the antenna
    network. COEX0-2 pulled down 100k, MAGPIO unconnected. Check GNSS RF path.
  - Tag-Connect J1: VCC pad 1 is no-connect, so pad 1 can't be used to meter
    VDD_GPIO, and the DK may need target voltage supplied separately.
  - Physical SIM slot fitted, MFF2 eSIM depopulated.
- Power sheet ("NB-IoT modem"): GPIO 26 5G_ENABLE -> 5G_ENABLE_FILTERED drives
  the ON pins of two TPS22975 load switches from +3V3: U9 -> 5G_VDD_GPIO (nRF
  VDD_GPIO, ~11.5 ms turn-on) and U10 -> 3V3_SWITCHED -> ferrite -> VDD_FILTERED
  (nRF VDD, ~2.6 ms); nRF ENABLE = VDD_FILTERED via 220R. GPIO 26 low = nRF
  fully unpowered, which explains every low reading above. Fix: GPIO 26 high
  (config.txt `gpio=26=op,dh` at boot).
- [ ] Run `bash tools/nrf9151_probe.sh` (copy to the Pi first). It fixes a
      ctsrts overlay line, power-cycles the modem on GPIO 26 while capturing
      `Ready`, checks `AT` (tries other baud rates), attempts a GPS fix
      (`GPS_WAIT=600` to wait longer), prints a verdict and offers to write
      `dtoverlay=uart3-pi5` + `gpio=26=op,dh` to config.txt. Record the output.
- 2026-09-13 probe on owl-1: WORKS. GPIO 9 lo -> hi when GPIO 26 went high;
  `AT` silent at 115200, `OK` at **460800**. `AT+CGMR` mfw_nrf91x1_2.0.2,
  `AT#XSLMVER` 2.9.1 (NCS 2.9.0). %XSYSTEMMODE already 0,0,1,0 (persisted),
  CFUN=31 and `AT#XGPS=1,0,1` OK. Boot config lines written by the probe.
  Fix/no-fix not captured yet (nRF GPS pin 42 no-connect still open).
- [ ] Fresh unit: `bash owl_setup.sh --ship-gps` -> SHIP-READY; re-run and
      `grep -c 'dtoverlay=uart3-pi5$\|gpio=26=op,dh' /boot/firmware/config.txt` = 2.
      After reboot: `groups owl` has dialout, ModemManager inactive, `[GPS]` is
      nrf91 / /dev/ttyAMA3 / 460800, `journalctl -u owl -f` shows `nRF91 GPS started`.
- [ ] Negative: remove `gpio=26=op,dh`, reboot, `--ship-gps` -> NOT SHIP-READY
      naming the nRF9151, dev cleanup skipped.
- [ ] With sky view: app/dashboard line shows `OWL GPS timing` and a speed;
      saved images carry GPS EXIF with HPositioningError. Pull the antenna:
      timing falls back to controller/fixed within a few seconds.
- [ ] Protected pins (nrf91 on): hand-edit a relay to 37 -> owl.service runs,
      journal says `Relay N on BOARD 37 disabled`, modem stays up (GPIO 26 hi).
      Dashboard section edit / networked push of relay 37 -> refused with a
      GPIO26 message. With `source = none`, relay 37 is accepted.

Next steps:

- [ ] Schematic: which CM5 GPIOs carry nRF9151 UART TX/RX (and RTS/CTS), and
      is there a power/enable/reset GPIO for the nRF? Record them here.
- [ ] Pin state before changing anything: `pinctrl get 0-27`
- [ ] Enable the matching Pi 5/CM5 UART overlay at runtime (no reboot). GPIO
      pairs: `uart0-pi5` 14/15 -> ttyAMA0, `uart1-pi5` 0/1, `uart2-pi5` 4/5,
      `uart3-pi5` 8/9, `uart4-pi5` 12/13. Start with the schematic's pair, or
      `uart0-pi5` if unknown:
      `sudo dtoverlay uart0-pi5 && ls -l /dev/ttyAMA*`
      If the node does not appear, add `dtoverlay=uart0-pi5` under `[all]` in
      `/boot/firmware/config.txt` and reboot.
- [ ] Passive listen, 115200 then 9600:
      `sudo stty -F /dev/ttyAMA0 115200 raw -echo; sudo timeout 5 cat /dev/ttyAMA0 | head -20`
- [ ] AT probe: `sudo picocom -b 115200 --omap crcrlf --echo /dev/ttyAMA0`, type
      `AT`. If there is no reply, retry with `--flow h`. Then `AT+CGMR`,
      `AT#XSLMVER`, `AT#XSMVER`, `AT%XSYSTEMMODE?`, `AT+CFUN?`.
- [ ] GNSS test, antenna outside: `AT+CFUN=0`, `AT%XSYSTEMMODE=0,0,1,0`,
      `AT+CFUN=31`, then `AT#XGPS=1,0,1` (or `AT#XGNSS=1,0,1`). Wait up to 90 s
      and record whether `#XGPS:`/`#XGNSSPOS:` lines or `$G...` NMEA arrive,
      and the time to first fix. Stop with `AT#XGPS=0` and `AT+CFUN=0`.

