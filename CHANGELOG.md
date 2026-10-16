# Changelog

Notable changes to the OWL software (owl.py, the standalone dashboard and the networked controller). Newest first. Versions follow `version.py`. History before this file is in the git log.

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
