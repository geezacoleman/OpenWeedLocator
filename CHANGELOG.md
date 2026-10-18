# Changelog

Notable changes to the OWL software (owl.py, the standalone dashboard and the networked controller). Newest first. Versions follow `version.py`. History before this file is in the git log.

## [Unreleased] 3.25.5: dashboard file and config paths confined (branch hardening-3.22)

### Security
- **The standalone USB download no longer serves files outside `/media`.** `/api/download_file`, `/api/browse_files` and `/api/navigate_directory` checked only that the path *started* with `/media`, so `/media/../home/owl/owl/config/CONTROLLER.ini` returned the device token and the Noktura cloud password to anyone on the OWL's network. Paths are now resolved (`..` and symlinks) before the check (`usb_path`).
- **Config names can only name detection profiles.** New `config_profile_path()` in `utils/config_manager.py` (same shape as `model_path_in`): a flat `.ini` in `config/`, never `CONTROLLER.ini`, `GEOMETRY.ini` or a `*_TEMPLATE.ini`. Used by `/api/config/delete` (which passed `config/../owl.py` to `os.remove`), `/api/config/set-active` (which accepted `CONTROLLER.ini`, after which `GET /api/config` served the device token), the save route's `overwrite_filename`, the MQTT `set_active_config` handler, and the active-config pointer reads in the dashboard and `owl.py` (a pointer naming anything else falls back to `GENERAL_CONFIG.ini` with a warning).

### Changed
- `CONTROLLER.ini` no longer appears in the dashboard's config list. It was listed as a "(default)" profile; selecting it loaded controller settings as the detection config.

## [Unreleased] 3.25.4: Install AI fits a 2 GB / 16 GB CM5 (branch hardening-3.22)

### Fixed
- **Install AI no longer fails with "No space left on device".** On 64-bit ARM the PyPI torch wheel depends on the full NVIDIA CUDA stack (about 3 to 4 GB to download, 6 GB+ installed), and pip unpacked it into `/tmp`, which Trixie mounts as a RAM-backed tmpfs (1 GB on a 2 GB CM5). `requirements-gog.txt` now pins `torch==2.14.1+cpu` and `torchvision==0.29.1+cpu` from the PyTorch CPU index (the `+cpu` pin cannot match the PyPI CUDA build), so about 324 MB of wheels instead of 4 GB. `owl_update.sh` runs every pip call with `TMPDIR` on disk and `--no-cache-dir`, deletes that temp folder on exit (rollback included), checks free space on the repo, venv and temp filesystems, and fails the install (so it rolls back) if a CUDA torch build ends up in the venv. The routine "Green-on-Green detected" re-install on AI units goes through the same path.
- **Install AI keeps the OWL's OpenCV and numpy.** ultralytics asks for `opencv-python`, which pip does not know `opencv-contrib-python` already provides, so it put the newest OpenCV (5.0) over `cv2`. Both are now held at the installed versions with a pip constraint.

## [Unreleased] 3.25.3: no live position in the cloud feed (branch hardening-3.22)

### Changed
- **The OWL no longer sends its GPS position to Noktura.** `gps_latitude`, `gps_longitude` and `gps_available` are out of `CLOUD_STATE_FIELDS`, so no live location leaves the farm. Noktura already strips them on arrival and keeps no live position (Noktura BUG-126, devices v3 Phase 1). A session's location still reaches Noktura inside the uploaded zip. Local GPS (owl.py, the dashboard, the phone app's REST state) is unchanged.

## [Unreleased] 3.25.2: field round 2026-10-15 (branch hardening-3.22)

From a farmer's run on a networked rig (2 OWLs, Teltonika 5G at 2 bars). Plan: `~/.claude/plans/groovy-knitting-stardust.md`. Needs the matching Noktura release (branch `fix/owl-field-2026-10-15`, migration 121) before any OWL records with the new session names.

### Fixed
- **Uploads to Noktura failing with `[Errno 104] Connection reset by peer`.**
  - **Cause:** every session under 4 GiB went up as one PUT with no retry. Two OWLs uploaded at once over one weak uplink, so any dropped connection lost the whole upload, and a retry started again from the beginning.
  - **Now:** the OWL reports `data_transfer.caps = ["resume"]` and resumes a multipart upload from the parts already in storage (`upload.completed_parts`, checked with `expect_md5`; `zip_changed` if the session changed).
  - **Retries:** each part gets 5 attempts (2/4/8/16 s apart, 60 s socket timeout); small single PUTs get 3. `data_transfer.retries` counts them.
  - **On error:** `bytes_sent` is kept, so it shows how far the upload got.
  - **Same bytes on a re-zip:** zips list their files in a fixed order, so zipping an unchanged session again gives an identical file.
  - **Noktura side:** multipart for everything above 16 MiB, one upload at a time per rig, automatic resume.
- **Two OWLs on one rig gave their sessions the same name.**
  - **New name:** sessions are `YYYYMMDD/sHHMMSS_<owl>_<serial4>`, e.g. `20261015/s164025_owl-1_a3f9` (the OWL's start time, its name, and the last 4 hex digits of its Pi serial).
  - **Old names:** `session_HHMMSS` folders keep working everywhere.
  - **One pattern:** `utils/directory_manager.SESSION_ID_RE` is now used everywhere a session id is checked, and sessions list newest first by start time.
- **Each OWL booted on a different AI model.**
  - **The model is saved:** a model switch is written to `[GreenOnGreen] model_path` once the model has loaded. A failed switch keeps the old model.
  - **Fixed order:** a `models` folder is picked from in name order.
  - **Startup pushes survive:** a model or algorithm the controller sends while the OWL is still starting is no longer wiped.
  - **Controller:** it remembers the choice in `CONTROLLER.ini [Fleet] model` (every OWL) and `model_<owl id>` (one OWL that differs). It only re-sends a model to an OWL running a different one. Choosing a model for one OWL no longer changes the fleet choice.

### Added
- **Pause / Resume recording** (MQTT `set_recording_paused`, `POST /api/collect/pause` and `/resume` on both controllers).
  - **Same session:** the session stays open and nothing is saved while paused. A video clip ends at a pause and a new clip starts on resume.
  - **Track and warnings:** GPS track points are skipped, `capture.paused` is reported, and the controller raises no "not saving" warnings for a paused OWL.
  - **Rejoining OWL:** an OWL that rejoins mid-pause stays paused.
  - **LAN only:** it is not a cloud command.
- **More crops, and your own crops.**
  - **Lists:** Arable (16) and Vegetables (14: cabbage, cauliflower, broccoli, kale, Brussels sprouts, lettuce, spinach, onion, leek, carrot, parsnip, beetroot, celery, pumpkin).
  - **Add and remove:** `POST /api/collect/crops {name}` adds a crop and `DELETE /api/collect/crops/<slug>` removes one. They are saved straight away as `[Collect] custom_crops` and listed first.
  - **Presets:** they gain `custom_crops` and `crop_groups`, while `crops` stays the flat list.
- **Light, sky and soil on the record form.**
  - **New fields:** `light_source` (natural/artificial/mixed), `sky` (sunny/partly_cloudy/overcast/night), `soil_cover` (Noktura surface covers), `soil_moisture` (dry/moist/wet) and `recent_water` (none/rain/irrigation).
  - **Old field still written:** `lighting` is derived from `light_source` + `sky`, so older Noktura builds keep working.
- **`video_count` in the cloud session manifest.**
- **Controller upload acks:**
  - **Queue place:** `queued` acks carry `position`/`queue_length`.
  - **New statuses:** `retrying`, `stored` and `failed`.

### Changed (record form and screens, 2026-10-16)
- **New record form** (design B, signed off by Guy 2026-10-16; `controller/shared/js/collect_form.js`, used by the Collect screen and both dashboards).
  - **Layout:** five rows on the left show what is chosen (Crop, Light and weather, Soil, Purpose, Notes). Tapping one shows only its choices on the right. Start recording is always on screen, and only the crop grid scrolls. It fits 1024x600.
  - **Crops:** tabs for Your crops, Arable and Vegetables, plus **+ Add crop** (saved straight away through `POST /api/collect/crops`) and **Edit my crops** (`DELETE`).
  - **Nothing invented:** a first run starts with nothing chosen, and an unchosen field is left out of the metadata. Earlier versions pre-filled weather, light and purpose. Last time's choices come back; a `lighting`-only last pick is read as light source + sky.
  - **Indoors:** artificial light greys out sky and weather, and they are sent empty.
  - **Taps:** a second tap on a chosen answer keeps it, so a bump in the cab cannot clear it.
  - **Labels:** English and Danish for every new crop and condition.
- **Pause / Resume buttons:**
  - **Collect screen:** beside the record button. A paused run shows "Paused" and an amber dot per OWL.
  - **Dashboards:** beside Recording on both. The button is greyed until something records.
  - **Controller state:** `/api/collect/state` now has `paused` (every recording OWL paused) and a per-OWL `paused`.
- **Session lists show video:** "214 photos", "1 video" or "12 photos, 1 video" (`CollectForm.sessionCounts`) on the Collect screen and both dashboards' Data tabs.
- **Upload states:** one shared `CollectForm.uploadStatus` for the Collect screen and the networked Data tab. The states are Waiting (2 of 5), Starting, Sending 40%, Signal dropped carrying on (in N s), Sent and Not sent. While sending, a line under it says "Signal dropped, carried on" after a retry.
- **Video limit:** "Video stopped at 60 s. Tap Start recording for another clip" on the Collect screen and both dashboards.

## [Unreleased] (on 3.25.1, branch hardening-3.22)

Dashboard UX round from Guy's field review, 2026-10-14. Both the standalone dashboard and the networked controller.

### Added
- **Test nozzles, one at a time:** a numbered button per nozzle (hold to spray it) and Run sequence, which fires each nozzle for 1 s in turn, OWL by OWL on the controller. The buttons are greyed while spraying or blanket spraying is on, with "Turn spraying off".
- **New OWL MQTT command `test_nozzles`** `{value: {relays: [0-based], duration_ms}}`; an empty list stops a test.
  - **Self-timeout:** the OWL switches the relays off itself after at most 10 s.
  - **Refused while spraying:** it is refused while spot or blanket spraying, or during first-boot setup.
  - **Ended by spraying:** spraying or blanket spray switching on ends a running test.
  - **Not from the cloud:** it is not in the cloud allowlist, so Noktura cannot fire nozzles.
- **New standalone route `POST /api/nozzles/test`:** returns 400 on bad input and 503 when the command is not delivered.
- **New OWL state fields:**
  - `relay_num` (nozzle count)
  - `nozzle_test` (relays a test has on)
  - `track_params` (tracking values in use)

  Standalone `/api/system_stats` passes them through.
- **Live view pop-up:** a Live view button on the Dashboard opens the camera picture over the page.
  - **Networked:** one button per online OWL.
  - **Standalone:** the camera stream only runs while the pop-up is open, and the crop and spray zone is edited inside it.
- **More on the Dashboard:** Blanket spray (moved back from Test nozzles), AI confidence, crop buffer, and Speed and Frame rate stat cells. On the controller, Frame rate is the slowest OWL's.
- **Tracking controls (AI models):**
  - **Stability:** a Low/Medium/High preset that shows "(custom)" when no preset matches.
  - **Spray a lost weed for (frames):** `detection_persist_frames`, 0 to 15.
  - **Remember a lost track for (frames):** `track_buffer`, 10 to 150.
- **Different AI models on different OWLs (controller):** a red warning on AI models, and on the Dashboard once any OWL sprays with AI, with "Use <model> on all".
- **Camera image sizes:** every size from 320×240 to 1456×1088, with the current size highlighted. 728×544 is added to the settings file editor's list.
- **No OWL connected (controller):** the Camera and Colour tuning pages say so and grey their controls.

### Changed
- **First tab:** "Field" is now "Dashboard". The view id stays `field`.
- **Sliders are now − value + steppers:** every slider (Colour tuning, AI confidence, crop buffer, spray length) is now a stepper.
  - **Hold:** holding − or + repeats and speeds up.
  - **Tap:** tapping the number opens the kiosk numpad.
  - **Send:** the value is sent once the taps stop.
  - **Ranges:** colour ranges are From/To pairs, and From never passes To.
- **Greyed, not hidden:** AI confidence and crop buffer are greyed with a short reason in modes that don't use them ("AI and Hybrid only", "Hybrid only").
- **Advanced merged into Profiles:** the settings file editor now sits under Profiles, and the Assistant has its own Settings row.
- **Spraying page (controller):** keeps only spray length. Crop buffer and AI confidence moved to the Dashboard and AI models.
- **One source for shared code:** the tracking presets and the per-mode Colour tuning layout now live once, in `controller/shared/js/owl_ui.js`.

### Fixed
- **Track stability hidden after reload:** it disappeared after a page reload even with tracking on.
- **Broken-image icon (controller):** Colour tuning showed one, over overlapping text, with no OWL connected.
- **Camera page showing nothing selected:** an OWL on a size other than the two offered lit neither button.
- **Nozzle hint (standalone):** "N nozzles" never showed because system_stats did not carry `relay_num`.

### Removed
- **Slider code:** knob-drag, track-tap and fine-tune slider code in both dashboards, the controller's actuation sliders, and the unused `computeAvgLoopTimeFromOWLs`.
