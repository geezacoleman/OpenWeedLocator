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
        updateOWLGrid();
        // Update config editor device selector if it exists
        if (typeof updateConfigEditorDevices === 'function') {
            updateConfigEditorDevices();
        }
        // Show/hide the restart-required notice based on OWL state
        if (typeof updateRestartNotice === 'function') {
            updateRestartNotice();
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
            if (typeof updatePaintedChipHint === 'function') {
                updatePaintedChipHint((firstOwl.available_lut_profiles || []).length > 0);
            }
            if (typeof syncLutPanelFromOwl === 'function') {
                syncLutPanelFromOwl(firstOwl);
            }
            if (typeof setHighResContextBadge === 'function' && firstOwl.rpi_version) {
                setHighResContextBadge(firstOwl.rpi_version);
            }
            // Sync slider values from OWL state so dashboard matches device
            syncConfigFromOWLState(firstOwl);
            // Check for config mismatch across OWLs
            checkConfigMismatch();
            // Sync sensitivity dial from OWL state (mode-aware: reads the
            // axis matching the active mode, shows Custom on no match)
            if (typeof syncSensitivityFromOwl === 'function') {
                syncSensitivityFromOwl(firstOwl);
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

// Cloud (Noktura) link — fleet-level, independent of per-OWL online state.
function updateCloudStatus(data) {
    renderCloudStatus(
        document.getElementById('cloud-status-dot'),
        document.getElementById('cloud-status-text'),
        data
    );
    if (typeof updateCloudManageBlock === 'function') {
        updateCloudManageBlock(data);
    }
}

function updateOWLGrid() {
    const grid = document.getElementById('owls-column');
    if (!grid) return;

    // Show OWLs seen recently (connected=true) plus fleet-registered units
    // (registered=true) — a registered OWL that goes quiet shows as an
    // Offline card instead of vanishing.
    const ids = Object.keys(owlsData).filter(id => {
        const owl = owlsData[id];
        return owl && (owl.connected === true || owl.registered === true);
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

function buildMiniDial(opts) {
    // Automotive micro-dial: fixed grey band with painted amber/red zones,
    // needle at the value. Baked as a static SVG string because the card
    // list is rebuilt via innerHTML every update cycle.
    // opts: {value (null = idle), min, max, amber: [a,b], red: [a,b],
    //        display, label, endLabels: [lo,hi]}
    const span = opts.max - opts.min;
    const frac = v => Math.max(0, Math.min(1, (v - opts.min) / span));
    const arc = (f0, f1, cls) =>
        `<path class="${cls}" d="${describeArc(70, 80, 55, -90 + f0 * 180, -90 + f1 * 180)}"></path>`;

    const hasValue = opts.value !== null && opts.value !== undefined;
    const angle = -90 + (hasValue ? frac(opts.value) : 0) * 180;

    let zones = arc(0, 1, 'mini-dial-track');
    if (opts.amber) zones += arc(frac(opts.amber[0]), frac(opts.amber[1]), 'mini-dial-amber');
    if (opts.red) zones += arc(frac(opts.red[0]), frac(opts.red[1]), 'mini-dial-red');

    const within = z => hasValue && z && opts.value >= z[0] && opts.value <= z[1];
    const stateClass = !hasValue ? ' idle'
        : within(opts.red) ? ' in-red'
        : within(opts.amber) ? ' in-amber' : '';

    const ends = opts.endLabels
        ? `<text x="6" y="76" class="mini-dial-end">${opts.endLabels[0]}</text>` +
          `<text x="134" y="76" class="mini-dial-end" text-anchor="end">${opts.endLabels[1]}</text>`
        : '';

    return `
        <div class="owl-mini-dial${stateClass}">
            <svg viewBox="0 0 140 90">
                ${zones}
                ${ends}
                <g transform="rotate(${angle} 70 80)">
                    <line class="mini-dial-needle" x1="70" y1="80" x2="70" y2="34"></line>
                    <circle class="mini-dial-hub" cx="70" cy="80" r="8"></circle>
                </g>
            </svg>
            <div class="mini-dial-value">${opts.display}</div>
            <div class="mini-dial-label">${opts.label}</div>
        </div>`;
}

function buildOWLCard(deviceId, owl) {
    const isOnline = !!owl.connected;
    const onlineClass = isOnline ? 'online' : 'offline';

    // Get stats
    const temp = owl.cpu_temp ?? 0;
    const cpu = owl.cpu_percent ?? 0;
    const mem = owl.memory_percent ?? 0;
    const loopMs = owl.avg_loop_time_ms ?? 0;
    const fps = loopMs > 0 ? 1000 / loopMs : null;  // null while not detecting
    // AI inference legitimately runs ~8-15 fps on a Pi — judge it on its own scale
    const aiAlgo = owl.algorithm === 'gog' || owl.algorithm === 'gog-hybrid';
    const fpsDial = aiAlgo
        ? {max: 20, red: [0, 4], amber: [4, 8]}
        : {max: 40, red: [0, 10], amber: [10, 20]};

    const disAttr = isOnline ? '' : 'disabled';
    const ctrlType = owl.controller_type || 'none';
    const hwWarning = (ctrlType !== 'none')
        ? '<div class="device-warning-badge">Hardware controller (' + ctrlType + '). Use standalone dashboard</div>'
        : '';

    // Active config "Running: <name>" (prefer the friendly [Meta] name).
    // The autosave working file displays as its source preset; the unsaved
    // marker appears only when its content actually differs from that source.
    let cfgName = owl.config_source || owl.config_name || '';
    if (cfgName && typeof prettyConfigName === 'function') cfgName = prettyConfigName(cfgName);
    if (cfgName && owl.config_unsaved) cfgName += ' - unsaved changes';
    const esc = (typeof escapeConfigLabel === 'function') ? escapeConfigLabel : (s) => s;
    const cfgLine = (isOnline && cfgName)
        ? '<div class="owl-compact-config" title="Config loaded on this OWL">Running: ' + esc(cfgName) + '</div>'
        : '';

    // friendly_name is user-typed via the fleet API — must be escaped.
    // deviceId comes from MQTT topic names (anonymous broker), so it is
    // just as untrusted.
    const displayName = esc(owl.friendly_name || deviceId);
    const nameTitle = owl.assigned_ip
        ? ` title="${esc(deviceId)} - ${esc(owl.assigned_ip)}"` : '';

    return `
        <div class="owl-card-compact ${onlineClass}">
            <div class="owl-card-compact-header">
                <h4${nameTitle}>${displayName}</h4>
                <span class="owl-status-badge ${onlineClass}">
                    <span class="badge-dot"></span>
                    ${isOnline ? 'Online' : 'Offline'}
                </span>
                ${hwWarning}
            </div>
            ${cfgLine}
            <div class="owl-mini-dials">
                ${buildMiniDial({value: isOnline ? temp : null, min: 30, max: 90,
                                 amber: [60, 70], red: [70, 90], endLabels: ['C', 'H'],
                                 display: isOnline ? temp.toFixed(0) + '°C' : '--', label: 'CPU'})}
                ${buildMiniDial({value: isOnline ? cpu : null, min: 0, max: 100,
                                 amber: [80, 90], red: [90, 100],
                                 display: isOnline ? cpu.toFixed(0) + '%' : '--', label: 'Load'})}
                ${buildMiniDial({value: isOnline ? mem : null, min: 0, max: 100,
                                 amber: [80, 90], red: [90, 100],
                                 display: isOnline ? mem.toFixed(0) + '%' : '--', label: 'Mem'})}
                ${buildMiniDial({value: isOnline ? fps : null, min: 0, max: fpsDial.max,
                                 red: fpsDial.red, amber: fpsDial.amber,
                                 display: (isOnline && fps) ? fps.toFixed(0) : '--', label: 'FPS'})}
            </div>
            <div class="owl-compact-actions">
                <button class="owl-compact-btn btn-video" onclick="openVideoFeed('${deviceId}')" ${disAttr}>Video</button>
                <button class="owl-compact-btn btn-frame" onclick="grabFrame('${deviceId}')" ${disAttr}>Frame</button>
                <button class="owl-compact-btn btn-restart" onclick="restartOWL('${deviceId}')" ${disAttr}>Restart</button>
            </div>
        </div>
    `;
}

