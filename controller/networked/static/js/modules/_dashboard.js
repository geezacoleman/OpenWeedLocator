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
 */
function syncConfigFromOWLState(owlState) {
    if (!owlState) return;

    var synced = false;
    var params = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                  'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                  'min_detection_area', 'crop_buffer_px'];

    for (var i = 0; i < params.length; i++) {
        var key = params[i];
        if (key in configParams && typeof owlState[key] !== 'undefined') {
            var newVal = Number(owlState[key]);
            if (!isNaN(newVal) && configParams[key].value !== newVal) {
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
        }
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
    const grid = document.getElementById('owls-grid');
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
                <div class="empty-state-subtext">Waiting for MQTT…</div>
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
    const fan = owl.fan_status?.rpm ?? 0;
    const cpu = owl.cpu_percent ?? 0;
    const mem = owl.memory_percent ?? 0;

    // Determine temp class
    let tempClass = 'good';
    if (temp > 70) tempClass = 'danger';
    else if (temp > 60) tempClass = 'warning';

    const disAttr = isOnline ? '' : 'disabled';

    return `
        <div class="owl-box ${onlineClass}">
            <div class="owl-box-header">
                <div class="owl-card-title">
                    <h3>${deviceId}</h3>
                </div>
                <span class="owl-status-badge ${onlineClass}">
                    <span class="badge-dot"></span>
                    ${isOnline ? 'ONLINE' : 'OFFLINE'}
                </span>
            </div>

            <div class="owl-stats">
                <div class="owl-stat-item">
                    <div class="owl-stat-label">CPU Temp</div>
                    <div class="owl-stat-value ${tempClass}">${temp.toFixed(1)}°C</div>
                </div>
                <div class="owl-stat-item">
                    <div class="owl-stat-label">Fan</div>
                    <div class="owl-stat-value">${fan} RPM</div>
                </div>
                <div class="owl-stat-item">
                    <div class="owl-stat-label">CPU</div>
                    <div class="owl-stat-value">${cpu.toFixed(0)}%</div>
                </div>
                <div class="owl-stat-item">
                    <div class="owl-stat-label">Memory</div>
                    <div class="owl-stat-value">${mem.toFixed(0)}%</div>
                </div>
            </div>

            <div class="owl-actions">
                <button class="owl-btn btn-video" onclick="openVideoFeed('${deviceId}')" ${disAttr}>
                    VIDEO
                </button>
                <button class="owl-btn btn-frame" onclick="grabFrame('${deviceId}')" ${disAttr}>
                    GRAB FRAME
                </button>
                <button class="owl-btn btn-restart" onclick="restartOWL('${deviceId}')" ${disAttr}>
                    RESTART
                </button>
            </div>
        </div>
    `;
}

