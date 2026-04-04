// ============================================
// OWL Central Controller - Dashboard
// Dashboard polling loop, OWL grid rendering
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
                  'min_detection_area', 'crop_buffer_px', 'confidence'];

    for (var i = 0; i < params.length; i++) {
        var key = params[i];
        if (key in configParams && typeof owlState[key] !== 'undefined') {
            var newVal = Number(owlState[key]);
            if (isNaN(newVal)) continue;

            // Confidence comes from OWL as float 0.0-1.0, convert to pct 0-100
            if (key === 'confidence') {
                newVal = Math.round(newVal * 100);
            }

            if (configParams[key].value !== newVal) {
                configParams[key].value = newVal;
                synced = true;
            }
        }
    }

    if (synced && typeof updateAllSliders === 'function') {
        updateAllSliders();
    }
}

// ============================================
// CONFIG MISMATCH DETECTION
// ============================================

/**
 * Compare 9 GreenOnBrown keys across all connected OWLs.
 * Shows/hides the config-mismatch-badge in the config tab toolbar.
 */
function checkConfigMismatch() {
    var badge = document.getElementById('config-mismatch-badge');
    if (!badge) return;

    var keys = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                'min_detection_area'];

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
        showToast(`Loaded ${loadedCount} config defaults`, 'success');
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
        updateOWLGrid();
        // Update config editor device selector if it exists
        if (typeof updateConfigEditorDevices === 'function') {
            updateConfigEditorDevices();
        }

        // Sync pipeline mode from first connected OWL
        var firstOwl = null;
        for (var id in owlsData) {
            if (owlsData[id] && owlsData[id].connected) {
                firstOwl = owlsData[id];
                break;
            }
        }
        if (firstOwl) {
            if (typeof updatePipelineModeUI === 'function' && firstOwl.algorithm) {
                updatePipelineModeUI(firstOwl.algorithm);
            }
            if (typeof updateModeAvailability === 'function') {
                updateModeAvailability(!!firstOwl.model_available);
            }
            // Sync slider values from OWL state so dashboard matches device
            syncConfigFromOWLState(firstOwl);
            // Check for config mismatch across OWLs
            checkConfigMismatch();
            // Sync sensitivity dial from OWL state
            if (typeof updateSensitivityDial === 'function' && firstOwl.sensitivity_level) {
                updateSensitivityDial(firstOwl.sensitivity_level);
            }
            // Sync nozzle button state from OWL
            const nozzleBtn = document.getElementById('main-nozzles-btn');
            if (nozzleBtn) {
                const nozzlesOn = firstOwl.detection_mode === 2;
                nozzleBtn.classList.toggle('active', nozzlesOn);
                nozzleBtn.textContent = nozzlesOn ? 'Nozzles ON' : 'All Nozzles';
                globalNozzlesActive = nozzlesOn;
            }
            // Sync tracking button state from OWL
            const trackingBtn = document.getElementById('main-tracking-btn');
            if (trackingBtn) {
                const trackingOn = !!firstOwl.tracking_enabled;
                globalTrackingEnabled = trackingOn;
                trackingBtn.classList.toggle('active', trackingOn);
                trackingBtn.textContent = trackingOn ? 'Tracking ON' : 'Tracking';
            }

            // Show/hide track stability panel based on tracking state
            const stabilityPanel = document.getElementById('track-stability-panel');
            if (stabilityPanel) {
                stabilityPanel.style.display = globalTrackingEnabled ? '' : 'none';
            }
        }
        // Broadcast to widget state listeners
        if (typeof OWLWidget !== 'undefined' && firstOwl) {
            OWLWidget._broadcastState(firstOwl);
        }

        // Sync AI tab if it's active
        if (typeof syncAITabFromDashboard === 'function') syncAITabFromDashboard();
    } catch (err) {
        console.error('Dashboard update error:', err);
        mqttConnected = false;
        owlsData = {}; // Clear all OWLs on error
        updateMQTTStatus();
    }
}

function updateMQTTStatus() {
    const dot = document.getElementById('mqtt-status-dot');
    const txt = document.getElementById('mqtt-status-text');

    if (!dot || !txt) return;

    if (mqttConnected) {
        dot.classList.add('connected');
        txt.textContent = 'MQTT Connected';
    } else {
        dot.classList.remove('connected');
        txt.textContent = 'MQTT Disconnected';
    }
}

function updateOWLGrid() {
    const grid = document.getElementById('owls-column');
    if (!grid) return;

    // Filter to only show OWLs that have been seen recently (connected=true)
    const ids = Object.keys(owlsData).filter(id => {
        const owl = owlsData[id];
        return owl && owl.connected === true;
    });

    if (ids.length === 0) {
        grid.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-text">No OWLs Connected</div>
                <div class="empty-state-subtext">Waiting for MQTT...</div>
            </div>
        `;
        return;
    }

    grid.innerHTML = ids.map(id => buildOWLCard(id, owlsData[id])).join('');
}

function buildOWLCard(deviceId, owl) {
    const isOnline = !!owl.connected;
    const onlineClass = isOnline ? 'online' : 'offline';

    // Get stats
    const temp = owl.cpu_temp ?? 0;
    const cpu = owl.cpu_percent ?? 0;
    const mem = owl.memory_percent ?? 0;
    const loopMs = owl.avg_loop_time_ms ?? 0;

    // Determine temp class
    let tempClass = '';
    if (temp > 70) tempClass = ' style="color:#c0392b"';
    else if (temp > 60) tempClass = ' style="color:#d4820a"';

    const disAttr = isOnline ? '' : 'disabled';

    return `
        <div class="owl-card-compact ${onlineClass}">
            <div class="owl-card-compact-header">
                <h4>${deviceId}</h4>
                <span class="owl-status-badge ${onlineClass}">
                    <span class="badge-dot"></span>
                    ${isOnline ? 'Online' : 'Offline'}
                </span>
            </div>
            <div class="owl-compact-stats">
                <div class="owl-compact-stat"><strong${tempClass}>${temp.toFixed(0)}°C</strong> CPU</div>
                <div class="owl-compact-stat"><strong>${cpu.toFixed(0)}%</strong> Load</div>
                <div class="owl-compact-stat"><strong>${mem.toFixed(0)}%</strong> Mem</div>
                <div class="owl-compact-stat"><strong>${loopMs > 0 ? loopMs.toFixed(0) + 'ms' : '--'}</strong> Loop</div>
            </div>
            <div class="owl-compact-actions">
                <button class="owl-compact-btn btn-video" onclick="openVideoFeed('${deviceId}')" ${disAttr}>Video</button>
                <button class="owl-compact-btn btn-frame" onclick="grabFrame('${deviceId}')" ${disAttr}>Frame</button>
                <button class="owl-compact-btn btn-restart" onclick="restartOWL('${deviceId}')" ${disAttr}>Restart</button>
            </div>
        </div>
    `;
}

