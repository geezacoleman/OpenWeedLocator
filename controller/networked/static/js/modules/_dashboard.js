// ============================================
// OWL Central Controller - Dashboard
// Dashboard polling loop (the screens are drawn by _shell.js)
// ============================================

// ============================================
// CONFIG SYNC FROM OWL STATE
// ============================================

/**
 * Sync slider configParams from the OWL's published state.
 * Called on each dashboard poll so sliders always match the device.
 * Skips sync for 5s after last slider send to prevent snap-back.
 */
function syncConfigFromOWLState(owlState) {
    if (!owlState) return;

    // Snap-back guard: skip sync if user recently sent slider values
    if (typeof lastSliderSendTime !== 'undefined' && (Date.now() - lastSliderSendTime) < 5000) {
        return;
    }

    var synced = false;
    var params = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                  'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                  'min_detection_area', 'min_detection_area_percent',
                  'crop_buffer_px', 'confidence', 'lut_sensitivity'];

    for (var i = 0; i < params.length; i++) {
        var key = params[i];
        if (key in configParams && typeof owlState[key] !== 'undefined') {
            var newVal = Number(owlState[key]);
            if (isNaN(newVal)) continue;

            // Confidence comes from OWL as float 0.0-1.0, convert to pct 0-100
            if (key === 'confidence') {
                newVal = Math.round(newVal * 100);
            }

            // Min weed size %: when unset (0 = legacy px mode) seed the
            // slider from the px value converted against the frame area
            if (key === 'min_detection_area_percent') {
                var mp = configParams[key];
                if (newVal <= 0) {
                    var area = (Number(owlState.resolution_width) || 640)
                             * (Number(owlState.resolution_height) || 480);
                    newVal = (Number(owlState.min_detection_area) || 10) / area * 100;
                }
                newVal = roundParamValue(mp, Math.max(mp.min, Math.min(mp.max, newVal)));
            }

            if (configParams[key].value !== newVal) {
                configParams[key].value = newVal;
                synced = true;
            }
        }
    }

    if (synced && typeof updateAllSliders === 'function') {
        updateAllSliders();
        if (typeof updateLutSwatch === 'function') updateLutSwatch();
    }
}

// ============================================
// CONFIG MISMATCH DETECTION
// ============================================

/**
 * Compare the GreenOnBrown threshold keys across all connected OWLs.
 * Shows/hides the config-mismatch-badge in the config tab toolbar.
 */
function checkConfigMismatch() {
    var badge = document.getElementById('config-mismatch-badge');
    if (!badge) return;

    var keys = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                'min_detection_area_percent'];

    var connectedOwls = [];
    for (var id in owlsData) {
        if (owlsData[id] && owlsData[id].connected) {
            connectedOwls.push(owlsData[id]);
        }
    }

    if (connectedOwls.length < 2) {
        badge.classList.add('hidden');
        return;
    }

    var mismatch = false;
    var ref = connectedOwls[0];
    for (var i = 1; i < connectedOwls.length; i++) {
        for (var k = 0; k < keys.length; k++) {
            if (String(connectedOwls[i][keys[k]]) !== String(ref[keys[k]])) {
                mismatch = true;
                break;
            }
        }
        if (mismatch) break;
    }

    badge.classList.toggle('hidden', !mismatch);
}

// ============================================
// CONFIG DEFAULTS LOADING
// ============================================

async function loadConfigDefaults() {
    try {
        console.log('Loading config defaults from API...');
        const res = await fetch('/api/greenonbrown/defaults');
        if (!res.ok) throw new Error('HTTP ' + res.status);

        const data = await res.json();
        console.log('API defaults received:', data);

        let loadedCount = 0;
        for (const [key, cfg] of Object.entries(data)) {
            if (!(key in configParams)) {
                console.warn(`Unknown parameter from API: ${key}`);
                continue;
            }

            if (cfg && typeof cfg === 'object') {
                if (typeof cfg.value !== 'undefined') configParams[key].value = cfg.value;
                if (typeof cfg.min !== 'undefined') configParams[key].min = cfg.min;
                if (typeof cfg.max !== 'undefined') configParams[key].max = cfg.max;
                loadedCount++;
            } else {
                // Handle flat number format
                configParams[key].value = cfg;
                loadedCount++;
            }
        }

        console.log(`Loaded ${loadedCount} config parameters from API`);
    } catch (err) {
        console.error('Failed to load config defaults:', err);
        showToast('Warning: Using fallback config values', 'warning');

        // Set reasonable fallback values if API completely fails
        configParams.exg_min.value = 25;
        configParams.exg_max.value = 200;
        configParams.hue_min.value = 39;
        configParams.hue_max.value = 83;
        configParams.saturation_min.value = 50;
        configParams.saturation_max.value = 220;
        configParams.brightness_min.value = 60;
        configParams.brightness_max.value = 190;
        configParams.min_detection_area.value = 10;
    }
}

// ============================================
// DASHBOARD UPDATE
// ============================================

async function updateDashboard() {
    try {
        const res = await fetch('/api/owls');
        if (!res.ok) throw new Error('HTTP ' + res.status);

        const data = await res.json();
        mqttConnected = !!data.mqtt_connected;

        owlsData = data.owls || {};

        updateMQTTStatus();
        updateCloudStatus(data);
        // Config editor device selector + preview selector
        if (typeof updateConfigEditorDevices === 'function') updateConfigEditorDevices();
        // White balance readout + calibration progress for the editor's device
        if (typeof updateAwbReadout === 'function' && typeof getConfigEditorTarget === 'function') {
            const editorTarget = getConfigEditorTarget();
            const editorOwl = owlsData[editorTarget];
            if (editorOwl) {
                updateAwbReadout(editorOwl.camera, editorOwl.awb_calibration,
                    typeof onDeviceAwbCalibrationComplete === 'function' ? onDeviceAwbCalibrationComplete : null,
                    editorTarget);
            }
        }
        // Show/hide the restart-required notice based on OWL state
        if (typeof updateRestartNotice === 'function') updateRestartNotice();

        // The OWL the controls follow: the selected one, or the first
        // connected OWL when All OWLs is selected
        const firstOwl = (typeof Shell !== 'undefined') ? Shell.scopeOwl() : null;
        if (firstOwl) {
            if (typeof updatePipelineModeUI === 'function' && firstOwl.algorithm) {
                updatePipelineModeUI(firstOwl.algorithm);
            }
            if (typeof updatePaintedChipHint === 'function') {
                updatePaintedChipHint((firstOwl.available_lut_profiles || []).length > 0);
            }
            if (typeof syncLutPanelFromOwl === 'function') syncLutPanelFromOwl(firstOwl);
            if (typeof setHighResContextBadge === 'function' && firstOwl.rpi_version) {
                setHighResContextBadge(firstOwl.rpi_version);
            }
            // Sync slider values from OWL state so the controls match the device
            syncConfigFromOWLState(firstOwl);
            checkConfigMismatch();
            // Sensitivity (mode-aware: reads the axis matching the active mode)
            if (typeof syncSensitivityFromOwl === 'function') syncSensitivityFromOwl(firstOwl);
            if (!mainPressRecent()) {
                const scoped = scopedOwls();
                globalNozzlesActive = scoped.some((o) => o.detection_mode === 2);
                globalDetectionEnabled = scoped.some((o) => o.detection_enable) && !globalNozzlesActive;
                globalTrackingEnabled = !!firstOwl.tracking_enabled;
            }
            globalRecordingEnabled = Object.values(owlsData).some((o) => o && o.connected && o.image_sample_enable);
        }
        // Broadcast to widget state listeners
        if (typeof OWLWidget !== 'undefined' && firstOwl) OWLWidget._broadcastState(firstOwl);

        // The redesigned screens: rail, switches, notices, stat cells
        if (typeof Shell !== 'undefined') Shell.update(data);
    } catch (err) {
        console.error('Dashboard update error:', err);
        mqttConnected = false;
        owlsData = {}; // Clear all OWLs on error
        updateMQTTStatus();
        if (typeof Shell !== 'undefined') Shell.update({ owls: {} });
    }
}

function updateMQTTStatus() {
    const txt = document.getElementById('mqtt-status-text');
    if (txt) txt.textContent = mqttConnected ? 'Broker connected' : 'Broker not connected';
}

// Cloud (Noktura) link: fleet-level, independent of per-OWL online state.
function updateCloudStatus(data) {
    renderCloudStatus(
        document.getElementById('cloud-status-dot'),
        document.getElementById('cloud-status-text'),
        data
    );
    if (typeof updateCloudManageBlock === 'function') updateCloudManageBlock(data);
}
