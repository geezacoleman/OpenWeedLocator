// ============================================
// OWL Central Controller - Config Tab
// Range sliders, drag handling, fine-tune, preview
// ============================================

// ============================================
// SLIDER INITIALIZATION
// ============================================

let dragState = null;
let lastSliderSendTime = 0;  // snap-back guard: track when sliders last sent

function initSliders() {
    // Knob drag via pointer events
    document.querySelectorAll('.slider-knob').forEach(function(knob) {
        knob.addEventListener('pointerdown', startKnobDrag);
    });

    // Track click to jump nearest knob
    document.querySelectorAll('.slider-track').forEach(function(track) {
        track.addEventListener('pointerdown', handleTrackClick);
    });

    // Fine-tune buttons
    document.querySelectorAll('.fine-btn').forEach(function(btn) {
        btn.addEventListener('click', handleFineTune);
    });
}

// ============================================
// KNOB DRAGGING
// ============================================

function startKnobDrag(e) {
    e.preventDefault();
    e.stopPropagation();
    var knob = e.currentTarget;
    var param = knob.dataset.param;
    var rail = knob.closest('.slider-rail');

    // Mark as active knob in this group
    var group = knob.closest('.config-slider-group');
    group.querySelectorAll('.slider-knob').forEach(function(k) { k.classList.remove('active'); });
    knob.classList.add('active');

    knob.setPointerCapture(e.pointerId);

    dragState = {
        knob: knob,
        param: param,
        rail: rail,
        railRect: rail.getBoundingClientRect()
    };

    knob.addEventListener('pointermove', onKnobDrag);
    knob.addEventListener('pointerup', endKnobDrag);
    knob.addEventListener('pointercancel', endKnobDrag);
}

function onKnobDrag(e) {
    if (!dragState) return;

    var param = dragState.param;
    var railRect = dragState.railRect;
    var p = configParams[param];
    if (!p) return;

    var x = e.clientX - railRect.left;
    var pct = Math.max(0, Math.min(100, (x / railRect.width) * 100));
    var val = Math.round((pct / 100) * (p.max - p.min) + p.min);

    // Constrain range slider pairs
    val = constrainRangeValue(dragState.knob, param, val);

    configParams[param].value = val;
    updateSlider(param);
}

function endKnobDrag(e) {
    if (!dragState) return;

    var knob = dragState.knob;
    var param = dragState.param;

    knob.removeEventListener('pointermove', onKnobDrag);
    knob.removeEventListener('pointerup', endKnobDrag);
    knob.removeEventListener('pointercancel', endKnobDrag);

    // Send final value
    sendConfigUpdate(param, configParams[param].value);
    dragState = null;
}

// ============================================
// TRACK CLICK (tap to jump)
// ============================================

function handleTrackClick(e) {
    var track = e.currentTarget;
    var rail = track.closest('.slider-rail');
    var slider = rail.closest('.range-slider, .single-slider');
    var group = slider.closest('.config-slider-group');

    var railRect = rail.getBoundingClientRect();
    var x = e.clientX - railRect.left;
    var pct = Math.max(0, Math.min(100, (x / railRect.width) * 100));

    if (slider.classList.contains('single-slider')) {
        var param = slider.dataset.param;
        var p = configParams[param];
        if (!p) return;

        var val = Math.round((pct / 100) * (p.max - p.min) + p.min);
        configParams[param].value = Math.max(p.min, Math.min(p.max, val));
        updateSlider(param);
        sendConfigUpdate(param, configParams[param].value);
    } else {
        // Range slider — move nearest knob
        var minParam = slider.dataset.paramMin;
        var maxParam = slider.dataset.paramMax;
        var pMin = configParams[minParam];
        var pMax = configParams[maxParam];
        if (!pMin || !pMax) return;

        var minPct = ((pMin.value - pMin.min) / (pMin.max - pMin.min)) * 100;
        var maxPct = ((pMax.value - pMax.min) / (pMax.max - pMax.min)) * 100;

        var targetParam = (Math.abs(pct - minPct) <= Math.abs(pct - maxPct)) ? minParam : maxParam;
        var tp = configParams[targetParam];
        var newVal = Math.round((pct / 100) * (tp.max - tp.min) + tp.min);

        // Constrain
        var knob = document.getElementById(targetParam + '-knob');
        newVal = constrainRangeValue(knob, targetParam, newVal);

        configParams[targetParam].value = newVal;
        updateSlider(targetParam);
        sendConfigUpdate(targetParam, newVal);

        // Activate this knob
        group.querySelectorAll('.slider-knob').forEach(function(k) { k.classList.remove('active'); });
        if (knob) knob.classList.add('active');
    }
}

// ============================================
// FINE-TUNE BUTTONS
// ============================================

function handleFineTune(e) {
    var btn = e.currentTarget;
    var delta = parseInt(btn.dataset.delta);
    var group = btn.closest('.config-slider-group');

    // Find the active knob in this group
    var activeKnob = group.querySelector('.slider-knob.active');
    if (!activeKnob) {
        activeKnob = group.querySelector('.slider-knob');
        if (activeKnob) activeKnob.classList.add('active');
    }
    if (!activeKnob) return;

    adjustParameter(activeKnob.dataset.param, delta);
}

// ============================================
// CONSTRAINT HELPER
// ============================================

function constrainRangeValue(knob, param, val) {
    var slider = knob.closest('.range-slider');
    if (!slider) return Math.max(configParams[param].min, Math.min(configParams[param].max, val));

    var minParam = slider.dataset.paramMin;
    var maxParam = slider.dataset.paramMax;

    if (param === minParam && val > configParams[maxParam].value) {
        val = configParams[maxParam].value;
    }
    if (param === maxParam && val < configParams[minParam].value) {
        val = configParams[minParam].value;
    }

    return Math.max(configParams[param].min, Math.min(configParams[param].max, val));
}

// ============================================
// SLIDER UPDATES
// ============================================

function adjustParameter(param, delta) {
    var p = configParams[param];
    if (!p) return;

    var newVal = Math.max(p.min, Math.min(p.max, p.value + delta));

    // Constrain range slider pairs
    var knob = document.getElementById(param + '-knob');
    if (knob) {
        newVal = constrainRangeValue(knob, param, newVal);
    }

    p.value = newVal;
    updateSlider(param);
    sendConfigUpdate(param, newVal);
}

function updateSlider(param) {
    var p = configParams[param];
    if (!p) return;

    var pct = ((p.value - p.min) / (p.max - p.min)) * 100;

    // Update value display
    var valueEl = document.getElementById(param + '-value');
    if (valueEl) valueEl.textContent = p.value;

    // Position knob
    var knob = document.getElementById(param + '-knob');
    if (knob) knob.style.left = pct + '%';

    // Update fill
    if (!knob) return;
    var rail = knob.closest('.slider-rail');
    if (!rail) return;
    var fill = rail.querySelector('.slider-fill');
    if (!fill) return;

    var slider = rail.closest('.range-slider, .single-slider');
    if (slider && slider.classList.contains('range-slider')) {
        var minParam = slider.dataset.paramMin;
        var maxParam = slider.dataset.paramMax;
        var pMin = configParams[minParam];
        var pMax = configParams[maxParam];
        if (pMin && pMax) {
            var minPct = ((pMin.value - pMin.min) / (pMin.max - pMin.min)) * 100;
            var maxPct = ((pMax.value - pMax.min) / (pMax.max - pMax.min)) * 100;
            fill.style.left = minPct + '%';
            fill.style.width = Math.max(0, maxPct - minPct) + '%';
        }
    } else {
        fill.style.left = '0';
        fill.style.width = pct + '%';
    }
}

function updateAllSliders() {
    for (var key in configParams) {
        updateSlider(key);
    }
}

// ============================================
// CONFIG COMMANDS
// ============================================

function sendConfigUpdate(param, value) {
    var target = 'all';

    lastSliderSendTime = Date.now();

    // Route each param to its correct config section via set_config_section
    // This updates BOTH the live instance AND ConfigParser on the OWL,
    // so Save to Device captures slider values correctly.
    if (param === 'crop_buffer_px') {
        sendCommand(target, 'set_config_section', {
            section: 'GreenOnGreen',
            params: { crop_buffer_px: String(value) }
        });
    } else if (param === 'confidence') {
        // Convert percentage (0-100) to float (0.0-1.0) for OWL
        sendCommand(target, 'set_config_section', {
            section: 'GreenOnGreen',
            params: { confidence: String(value / 100) }
        });
    } else {
        // All GoB params go to GreenOnBrown section
        var params = {};
        params[param] = String(value);
        sendCommand(target, 'set_config_section', {
            section: 'GreenOnBrown',
            params: params
        });
    }
}

/**
 * Send all slider + editor values to device(s) via set_config_section.
 * Live-updates the device — does NOT persist to disk.
 */
function sendAllToDevice() {
    var target = 'all';

    lastSliderSendTime = Date.now();

    // Send slider params as whole sections
    var gobParams = {};
    var gobKeys = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                   'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                   'min_detection_area'];
    for (var i = 0; i < gobKeys.length; i++) {
        var k = gobKeys[i];
        if (configParams[k]) gobParams[k] = String(configParams[k].value);
    }

    var gogParams = {};
    if (configParams.confidence) gogParams.confidence = String(configParams.confidence.value / 100);
    if (configParams.crop_buffer_px) gogParams.crop_buffer_px = String(configParams.crop_buffer_px.value);

    sendCommand(target, 'set_config_section', { section: 'GreenOnBrown', params: gobParams });
    if (Object.keys(gogParams).length > 0) {
        sendCommand(target, 'set_config_section', { section: 'GreenOnGreen', params: gogParams });
    }

    // Also send any editor changes if loaded
    if (configEditorHasChanges && typeof deviceConfig !== 'undefined') {
        for (var section in deviceConfig) {
            if (JSON.stringify(deviceConfig[section]) !== JSON.stringify(originalDeviceConfig[section])) {
                sendCommand(target, 'set_config_section', {
                    section: section,
                    params: deviceConfig[section]
                });
            }
        }
        originalDeviceConfig = JSON.parse(JSON.stringify(deviceConfig));
        configEditorHasChanges = false;
        if (typeof updateConfigEditorChangeState === 'function') updateConfigEditorChangeState();
    }

    showToast('Testing on all OWLs — not saved', 'info');
}

/**
 * Open the Save dialog. Sliders have already applied live, so this only
 * captures a name/notes and persists.
 */
function openSaveModal() {
    var modal = document.getElementById('config-save-modal');
    if (!modal) return;

    // Offer "Update <name>" when a non-default library config is selected.
    var sel = document.getElementById('config-library-selector');
    var selName = sel ? sel.value : '';
    var cfg = (typeof configLibraryList !== 'undefined')
        ? configLibraryList.find(function (c) { return c.name === selName; }) : null;
    var modeGroup = document.getElementById('config-save-mode-group');
    var canUpdate = cfg && !cfg.is_default && selName && selName !== '__reset_defaults__';
    if (modeGroup) {
        modeGroup.style.display = canUpdate ? '' : 'none';
        if (canUpdate) {
            document.getElementById('config-save-target').textContent =
                cfg.display_name || cfg.name;
            document.getElementById('config-save-mode-update').checked = true;
            modeGroup.dataset.filename = cfg.name;
        } else {
            delete modeGroup.dataset.filename;
        }
    }

    var nameInput = document.getElementById('config-save-name');
    var notesInput = document.getElementById('config-save-notes');
    if (nameInput) nameInput.value = canUpdate ? (cfg.display_name || '') : suggestConfigName();
    if (notesInput) notesInput.value = canUpdate ? (cfg.notes || '') : '';

    modal.classList.remove('hidden');
}

function closeSaveModal() {
    var modal = document.getElementById('config-save-modal');
    if (modal) modal.classList.add('hidden');
}

/**
 * Suggest an editable default name based on the active config / algorithm.
 */
function suggestConfigName() {
    var algo = 'config';
    try {
        var first = (typeof getFirstConnectedOwl === 'function') ? getFirstConnectedOwl() : null;
        if (first && owlsData[first] && owlsData[first].algorithm) algo = owlsData[first].algorithm;
    } catch (e) { /* ignore */ }
    return algo + ' setup';
}

/**
 * Confirm the Save dialog: apply live, save a named copy to the library, save to
 * each OWL's disk (carrying the name), and optionally set it active on reboot.
 */
async function confirmSaveToAll() {
    var target = 'all';
    var name = (document.getElementById('config-save-name') || {}).value || '';
    var notes = (document.getElementById('config-save-notes') || {}).value || '';
    var setActive = !!(document.getElementById('config-save-active') || {}).checked;
    name = name.trim();

    // Update-in-place vs save-as-new.
    var modeGroup = document.getElementById('config-save-mode-group');
    var isUpdate = modeGroup && modeGroup.style.display !== 'none'
        && document.getElementById('config-save-mode-update')
        && document.getElementById('config-save-mode-update').checked;
    var overwriteFilename = isUpdate && modeGroup ? modeGroup.dataset.filename : null;

    // Apply current settings live first.
    sendAllToDevice();

    try {
        // Save a named copy to the controller library (if editor data is loaded).
        var savedFilename = null;
        if (typeof deviceConfig !== 'undefined' && Object.keys(deviceConfig).length > 0) {
            var libRes = await apiRequest('/api/config/library', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ config: deviceConfig, name: name, notes: notes,
                                       overwrite_filename: overwriteFilename })
            });
            var libData = await libRes.json();
            if (libData && libData.filename) savedFilename = libData.filename;
        }

        // Tell all OWLs to save their current config to disk, carrying the name.
        await apiRequest('/api/config/' + target + '/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: savedFilename, name: name, notes: notes })
        });

        // Optionally set as the active boot config on all OWLs.
        if (setActive && savedFilename) {
            await apiRequest('/api/config/' + target + '/set-active', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ config: 'config/' + savedFilename })
            });
            updateActiveBadge(name || savedFilename);
        }

        closeSaveModal();
        var nameInput = document.getElementById('config-save-name');
        if (nameInput) nameInput.value = '';
        if (typeof loadConfigLibrary === 'function') await loadConfigLibrary();
        showToast(setActive ? 'Saved and set active on reboot' : 'Saved to library', 'success');

    } catch (err) {
        showToast('Error saving: ' + err.message, 'error');
    }
}

/**
 * Load selected preset and push it to the device.
 */
async function loadPresetToDevice() {
    var libSel = document.getElementById('config-library-selector');
    var presetName = libSel ? libSel.value : '';
    if (!presetName) {
        showToast('Select a preset first', 'warning');
        return;
    }

    // Check for "Reset to Defaults" pseudo-entry
    if (presetName === '__reset_defaults__') {
        await loadConfigDefaults();
        updateAllSliders();
        sendAllToDevice();
        showToast('Reset to default values', 'info');
        return;
    }

    try {
        // Broadcast: load preset data and send sections to all OWLs
        var res = await apiRequest('/api/presets/' + presetName);
        var data = await res.json();
        if (!data.success) throw new Error(data.error || 'Failed to load preset');

        for (var section in data.config) {
            sendCommand('all', 'set_config_section', {
                section: section,
                params: data.config[section]
            });
        }

        // Also load into editor if available
        var presetRes = await apiRequest('/api/presets/' + presetName);
        var presetData = await presetRes.json();
        if (presetData.success) {
            deviceConfig = JSON.parse(JSON.stringify(presetData.config));
            originalDeviceConfig = JSON.parse(JSON.stringify(presetData.config));
            configEditorHasChanges = false;
            if (typeof renderDeviceConfigSections === 'function') renderDeviceConfigSections();
            if (typeof updateConfigEditorChangeState === 'function') updateConfigEditorChangeState();

            // Sync slider values from preset
            syncSlidersFromConfig(presetData.config);
        }

        lastSliderSendTime = Date.now();
        showToast('Applied ' + presetName + ' to all OWLs (not saved)', 'success');

    } catch (err) {
        showToast('Error loading preset: ' + err.message, 'error');
    }
}

/**
 * Sync slider configParams from a loaded config object.
 */
function syncSlidersFromConfig(config) {
    var gob = config.GreenOnBrown || {};
    var gog = config.GreenOnGreen || {};

    var gobKeys = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                   'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                   'min_detection_area'];
    for (var i = 0; i < gobKeys.length; i++) {
        var k = gobKeys[i];
        if (k in gob && k in configParams) {
            configParams[k].value = Number(gob[k]);
        }
    }

    if ('confidence' in gog && 'confidence' in configParams) {
        configParams.confidence.value = Math.round(Number(gog.confidence) * 100);
    }
    if ('crop_buffer_px' in gog && 'crop_buffer_px' in configParams) {
        configParams.crop_buffer_px.value = Number(gog.crop_buffer_px);
    }

    updateAllSliders();
}

// ============================================
// LIVE PREVIEW TOGGLE
// ============================================

let configPreviewActive = false;

function getFirstConnectedOwl() {
    for (var id in owlsData) {
        if (owlsData[id] && owlsData[id].connected) return id;
    }
    return null;
}

function getSelectedPreviewDevice() {
    var sel = document.getElementById('config-preview-device');
    return (sel && sel.value) ? sel.value : getFirstConnectedOwl();
}

function toggleConfigPreview() {
    var split = document.getElementById('config-split');
    var img = document.getElementById('config-preview-img');
    var btn = document.getElementById('config-preview-btn');

    if (!split || !img) return;

    configPreviewActive = !configPreviewActive;

    var label = document.getElementById('config-preview-label');

    if (configPreviewActive) {
        split.classList.add('preview-active');
        if (btn) {
            btn.textContent = 'Hide Preview';
            btn.classList.add('active');
        }
        var deviceId = getSelectedPreviewDevice();
        if (deviceId) {
            img.src = '/api/video_feed/' + deviceId;
            if (label) label.textContent = deviceId;
        }
    } else {
        split.classList.remove('preview-active');
        if (btn) {
            btn.textContent = 'Preview';
            btn.classList.remove('active');
        }
        img.src = '';
        if (label) label.textContent = '';
    }
}

function updateConfigPreviewDevice() {
    if (!configPreviewActive) return;

    var img = document.getElementById('config-preview-img');
    var label = document.getElementById('config-preview-label');
    if (!img) return;

    var deviceId = getSelectedPreviewDevice();
    img.src = deviceId ? '/api/video_feed/' + deviceId : '';
    if (label) label.textContent = deviceId || '';
}

function onPreviewDeviceChanged() {
    if (!configPreviewActive) return;

    var img = document.getElementById('config-preview-img');
    var label = document.getElementById('config-preview-label');
    if (!img) return;

    var deviceId = getSelectedPreviewDevice();
    if (deviceId) {
        img.src = '/api/video_feed/' + deviceId;
        if (label) label.textContent = deviceId;
    } else {
        img.src = '';
        if (label) label.textContent = '';
    }
}

function stopConfigPreview() {
    if (geometryEditorActive) {
        geoCloseEditor();
    }
    if (!configPreviewActive) return;

    configPreviewActive = false;
    var split = document.getElementById('config-split');
    var img = document.getElementById('config-preview-img');
    var btn = document.getElementById('config-preview-btn');

    if (split) split.classList.remove('preview-active');
    if (img) img.src = '';
    if (btn) {
        btn.textContent = 'Preview';
        btn.classList.remove('active');
    }
}

// ============================================
// VISUAL GEOMETRY EDITOR (up to 2 feeds side by side)
// ============================================

let geometryEditorActive = false;
let geoInstances = [];   // [{ slot, deviceId, editor }]
let geoActive = null;    // active editor instance
let geoControls = null;  // shared controls panel handle
let geoMode = 2;         // 1 or 2 feeds

function geoFrac(primary, fallback, dflt) {
    var n = parseFloat(primary);
    if (!isNaN(n)) return n;
    n = parseFloat(fallback);
    if (!isNaN(n)) return n;
    return dflt;
}

function geoConnectedOwls() {
    var ids = [];
    for (var id in owlsData) { if (owlsData[id] && owlsData[id].connected) ids.push(id); }
    return ids;
}

function geoSetPreviewMode(deviceId, mode) {
    apiRequest('/api/preview-mode/' + deviceId, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: mode })
    });
}

function geoPostGeometry(target, v, persist) {
    apiRequest('/api/geometry/' + target, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            params: {
                crop_left: String(v.crop_left), crop_right: String(v.crop_right),
                crop_top: String(v.crop_top), crop_bottom: String(v.crop_bottom),
                actuation_top: String(v.actuation_top), actuation_bottom: String(v.actuation_bottom)
            }, persist: !!persist
        })
    });
}

async function geoFetchSeed(deviceId) {
    var seed = {
        values: { crop_left: 0.02, crop_right: 0.02, crop_top: 0.02, crop_bottom: 0.02,
                  actuation_top: 0.0, actuation_bottom: 1.0 },
        relayNum: 4
    };
    try {
        var res = await apiRequest('/api/config/' + deviceId, {}, 5000);
        var data = await res.json();
        if (data.success && data.config) {
            var cam = data.config.Camera || {}, sys = data.config.System || {};
            seed.values = {
                crop_left: geoFrac(cam.crop_left, cam.crop_factor_horizontal, 0.02),
                crop_right: geoFrac(cam.crop_right, cam.crop_factor_horizontal, 0.02),
                crop_top: geoFrac(cam.crop_top, cam.crop_factor_vertical, 0.02),
                crop_bottom: geoFrac(cam.crop_bottom, cam.crop_factor_vertical, 0.02),
                actuation_top: geoFrac(sys.actuation_top, null, 0.0),
                actuation_bottom: geoFrac(sys.actuation_bottom, null, 1.0)
            };
            seed.relayNum = parseInt(sys.relay_num, 10) || 4;
        }
    } catch (e) { /* defaults */ }
    return seed;
}

function geoSetActive(editor) {
    geoActive = editor;
    geoInstances.forEach(function (g) { g.editor.setActive(g.editor === editor); });
    if (geoControls) geoControls.refresh();
}

function geoDestroyInstances() {
    geoInstances.forEach(function (g) {
        geoSetPreviewMode(g.deviceId, 'cropped');
        g.editor.destroy();
    });
    geoInstances = [];
    geoActive = null;
}

// (Re)build the feed slots from current picker selections + mode.
async function geoBuildSlots() {
    geoDestroyInstances();

    var owls = geoConnectedOwls();
    var slotCount = (geoMode === 2 && owls.length >= 2) ? 2 : 1;

    var feedsEl = document.getElementById('geo-feeds');
    if (feedsEl) {
        feedsEl.classList.toggle('geo-2up', slotCount === 2);
        feedsEl.classList.toggle('geo-1up', slotCount === 1);
    }

    for (var s = 0; s < 2; s++) {
        var slotEl = document.querySelector('.geo-slot[data-slot="' + s + '"]');
        if (!slotEl) continue;
        slotEl.style.display = (s < slotCount) ? '' : 'none';
        if (s >= slotCount) continue;

        var pick = slotEl.querySelector('.geo-slot-pick');
        var prev = pick.value;
        pick.innerHTML = owls.map(function (id) {
            return '<option value="' + id + '">' + id + '</option>';
        }).join('');
        var deviceId = (owls.indexOf(prev) >= 0) ? prev : (owls[s] || owls[0]);
        if (deviceId) pick.value = deviceId;
        pick.onchange = function () { geoBuildSlots(); };
        if (!deviceId) continue;

        var img = document.getElementById('geo-img-' + s);
        var frame = document.getElementById('geo-frame-' + s);
        geoSetPreviewMode(deviceId, 'full');
        img.src = '/api/video_feed/' + deviceId + '?t=' + Date.now();

        var seed = await geoFetchSeed(deviceId);
        (function (devId, slotIdx) {
            var editor = GeometryEditor.create({
                host: frame, img: img, values: seed.values, relayNum: seed.relayNum,
                deviceId: devId, label: devId,
                onCommit: function (v, info) { geoPostGeometry(devId, v, info && info.persist); },
                onActivate: function (ed) { geoSetActive(ed); },
                onChange: function () { if (geoControls) geoControls.refresh(); }
            });
            geoInstances.push({ slot: slotIdx, deviceId: devId, editor: editor });
        })(deviceId, s);
    }

    if (geoInstances.length) geoSetActive(geoInstances[0].editor);
}

function geoCloseEditor() {
    geoDestroyInstances();
    if (geoControls) { geoControls.destroy(); geoControls = null; }

    var split = document.getElementById('config-split');
    if (split) split.classList.remove('geo-mode');
    var multi = document.getElementById('geo-multi');
    if (multi) multi.style.display = 'none';
    var frame = document.getElementById('config-preview-frame');
    var label = document.getElementById('config-preview-label');
    if (frame) frame.style.display = '';
    if (label) label.style.display = '';
    // Restore the bits hidden during geometry.
    ['.config-action-bar', '.config-mode-bar', '#config-preview-device',
     '#config-preview-btn'].forEach(function (sel) {
        var el = document.querySelector(sel);
        if (el) el.style.display = '';
    });
    var feedToggle = document.getElementById('geo-feed-toggle');
    if (feedToggle) feedToggle.style.display = 'none';

    geometryEditorActive = false;
    var btn = document.getElementById('config-geometry-btn');
    if (btn) { btn.textContent = 'Adjust geometry'; btn.classList.remove('active'); }
}

async function toggleGeometryEditor() {
    if (typeof GeometryEditor === 'undefined') return;
    if (geometryEditorActive) { geoCloseEditor(); return; }

    var owls = geoConnectedOwls();
    if (!owls.length) { showToast('No OWLs connected', 'warning'); return; }

    geometryEditorActive = true;
    geoMode = (owls.length >= 2) ? 2 : 1;

    // Switch the layout into geometry mode (hide sliders + single preview).
    var split = document.getElementById('config-split');
    if (split) split.classList.add('geo-mode');
    var frame = document.getElementById('config-preview-frame');
    var label = document.getElementById('config-preview-label');
    if (frame) frame.style.display = 'none';
    if (label) label.style.display = 'none';
    var multi = document.getElementById('geo-multi');
    if (multi) multi.style.display = 'flex';
    // Hide everything not relevant to geometry to reclaim space.
    [['.config-action-bar', null], ['.config-mode-bar', null],
     ['#config-preview-device', null], ['#config-preview-btn', null]].forEach(function (pair) {
        var el = document.querySelector(pair[0]);
        if (el) el.style.display = 'none';
    });
    // 1-up / 2-up toggle lives in the toolbar so it's always reachable.
    var feedToggle = document.getElementById('geo-feed-toggle');
    if (feedToggle) feedToggle.style.display = '';
    document.querySelectorAll('#geo-feed-toggle .geo-updown-btn').forEach(function (b) {
        b.classList.toggle('active', parseInt(b.dataset.up, 10) === geoMode);
        b.onclick = function () {
            geoMode = parseInt(b.dataset.up, 10);
            document.querySelectorAll('#geo-feed-toggle .geo-updown-btn').forEach(function (x) {
                x.classList.toggle('active', x === b);
            });
            geoBuildSlots();
        };
    });

    // Shared controls panel (acts on the active feed).
    geoControls = GeometryEditor.mountControls(document.getElementById('config-geometry-panel'), {
        allowAll: true,
        getActive: function () { return geoActive; },
        onCancel: function () {
            geoInstances.forEach(function (g) { g.editor.revert(); });
            geoCloseEditor();
        },
        onDone: function (applyAll) {
            if (applyAll && geoActive) {
                var v = geoActive.getValues();
                geoPostGeometry('all', v, true);
                geoInstances.forEach(function (g) { if (g.editor !== geoActive) g.editor.setValues(v); });
                showToast('Geometry saved to all OWLs', 'success');
            } else {
                geoInstances.forEach(function (g) { g.editor.commitPersist(); });
                showToast('Geometry saved', 'success');
            }
            geoCloseEditor();
        }
    });

    var btn = document.getElementById('config-geometry-btn');
    if (btn) { btn.textContent = 'Close geometry'; btn.classList.add('active'); }
    showToast('Drag the edges/band on each feed. Tap a feed to make it active. Done saves; Cancel reverts.', 'info');

    await geoBuildSlots();
}

// ============================================
// ADVANCED SETTINGS TOGGLE
// ============================================

function toggleAdvancedSettings() {
    var toggle = document.getElementById('config-advanced-toggle');
    var body = document.getElementById('config-advanced-body');

    if (!toggle || !body) return;

    var isOpen = toggle.classList.toggle('open');
    body.classList.toggle('open', isOpen);
}

// ============================================
// SEND TO SINGLE DEVICE (Advanced Settings)
// ============================================

/**
 * Send all slider + editor values to a single device selected in Advanced Settings.
 */
function sendToSingleDevice() {
    var sel = document.getElementById('config-editor-device');
    var target = sel ? sel.value : null;

    if (!target) {
        showToast('Select a device first', 'warning');
        return;
    }

    lastSliderSendTime = Date.now();

    // Send slider params as whole sections
    var gobParams = {};
    var gobKeys = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                   'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                   'min_detection_area'];
    for (var i = 0; i < gobKeys.length; i++) {
        var k = gobKeys[i];
        if (configParams[k]) gobParams[k] = String(configParams[k].value);
    }

    var gogParams = {};
    if (configParams.confidence) gogParams.confidence = String(configParams.confidence.value / 100);
    if (configParams.crop_buffer_px) gogParams.crop_buffer_px = String(configParams.crop_buffer_px.value);

    sendCommand(target, 'set_config_section', { section: 'GreenOnBrown', params: gobParams });
    if (Object.keys(gogParams).length > 0) {
        sendCommand(target, 'set_config_section', { section: 'GreenOnGreen', params: gogParams });
    }

    // Also send any editor changes if loaded
    if (configEditorHasChanges && typeof deviceConfig !== 'undefined') {
        for (var section in deviceConfig) {
            if (JSON.stringify(deviceConfig[section]) !== JSON.stringify(originalDeviceConfig[section])) {
                sendCommand(target, 'set_config_section', {
                    section: section,
                    params: deviceConfig[section]
                });
            }
        }
    }

    showToast('Settings sent to ' + target, 'success');
}

// ============================================
// SLIDER VISIBILITY BY MODE
// ============================================

function updateSliderVisibility(algorithm) {
    var gobSliders = document.querySelectorAll('.config-slider-group:not(#crop-buffer-slider-group):not(#confidence-slider-group)');
    var bufferSlider = document.getElementById('crop-buffer-slider-group');
    var confidenceSlider = document.getElementById('confidence-slider-group');

    if (algorithm === 'gog') {
        // Pure AI: hide GoB sliders, show confidence only
        gobSliders.forEach(function(el) { el.style.display = 'none'; });
        if (bufferSlider) bufferSlider.style.display = 'none';
        if (confidenceSlider) confidenceSlider.style.display = '';
    } else if (algorithm === 'gog-hybrid') {
        // Hybrid: show GoB sliders + buffer + confidence
        gobSliders.forEach(function(el) { el.style.display = ''; });
        if (bufferSlider) bufferSlider.style.display = '';
        if (confidenceSlider) confidenceSlider.style.display = '';
    } else {
        // Colour: show GoB sliders, hide buffer and confidence
        gobSliders.forEach(function(el) { el.style.display = ''; });
        if (bufferSlider) bufferSlider.style.display = 'none';
        if (confidenceSlider) confidenceSlider.style.display = 'none';
    }
}
