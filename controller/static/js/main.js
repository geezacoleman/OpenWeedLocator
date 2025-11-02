// OWL Central Controller JavaScript

let owlsData = {};
let mqttConnected = false;
let updateInterval = null;
let configDefaults = {};

// NEW: track offline counts per device
const offlineCounts = {};
const MAX_OFFLINE_POLLS = 5;   // after 5 polls (~10s) hide
const UPDATE_INTERVAL = 2000;

// init
document.addEventListener('DOMContentLoaded', function() {
    console.log('OWL Central Controller initializing...');

    // NEW: side panel toggles
    const openBtn = document.getElementById('open-config-btn');
    const closeBtn = document.getElementById('close-config-btn');
    const panel = document.getElementById('config-panel');

    if (openBtn && panel) {
        openBtn.addEventListener('click', () => panel.classList.add('open'));
    }
    if (closeBtn && panel) {
        closeBtn.addEventListener('click', () => panel.classList.remove('open'));
    }

    loadConfigDefaults();
    startPolling();

    const targetSelect = document.getElementById('target-owl-select');
    if (targetSelect) {
        targetSelect.addEventListener('change', updateConfigSliders);
    }
});

// no tabs anymore
function startPolling() {
    updateDashboard();
    updateInterval = setInterval(updateDashboard, UPDATE_INTERVAL);
}

async function updateDashboard() {
    try {
        const response = await fetch('/api/owls');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();

        const freshOwls = data.owls || {};
        mqttConnected = data.mqtt_connected || false;

        // prune / merge
        owlsData = pruneAndMergeOwls(owlsData, freshOwls);

        updateMQTTStatus();
        updateOWLCount();
        updateOWLGrid();
        updateTargetSelector();
        updateLastUpdate();

    } catch (err) {
        console.error('Error updating dashboard:', err);
        mqttConnected = false;
        updateMQTTStatus();
    }
}

/**
 * Merge newly received OWLs and drop ones that have been offline
 * or missing for MAX_OFFLINE_POLLS.
 */
function pruneAndMergeOwls(current, fresh) {
    const now = Date.now() / 1000;
    const result = {};

    // first, loop over fresh owls
    for (const [id, owl] of Object.entries(fresh)) {
        // reset offline count if online
        if (owl.connected) {
            offlineCounts[id] = 0;
        } else {
            // offline but still publishing
            offlineCounts[id] = (offlineCounts[id] || 0) + 1;
        }

        result[id] = owl;
    }

    // then, look at owls we had before but didn't get now
    for (const id of Object.keys(current)) {
        if (!(id in fresh)) {
            // missing in this poll
            offlineCounts[id] = (offlineCounts[id] || 0) + 1;

            if (offlineCounts[id] < MAX_OFFLINE_POLLS) {
                // keep showing briefly
                result[id] = current[id];
                // also force connected=false for styling
                result[id].connected = false;
            } else {
                // drop completely
                delete offlineCounts[id];
            }
        }
    }

    return result;
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

function updateOWLCount() {
    const el = document.getElementById('owl-count');
    if (!el) return;
    el.textContent = Object.keys(owlsData).length;
}

function updateOWLGrid() {
    const grid = document.getElementById('owls-grid');
    if (!grid) return;

    const ids = Object.keys(owlsData);
    if (ids.length === 0) {
        grid.innerHTML = `
            <div class="empty-state" style="grid-column: 1 / -1;">
                <div class="empty-state-text">No OWLs Connected</div>
                <div class="empty-state-subtext">Waiting for MQTT updates…</div>
            </div>
        `;
        return;
    }

    let html = '';
    for (const [id, owl] of Object.entries(owlsData)) {
        html += buildOWLCard(id, owl);
    }
    grid.innerHTML = html;
}

function buildOWLCard(deviceId, owl) {
    const isOnline = !!owl.connected;
    const onlineClass = isOnline ? 'online' : 'offline';

    const temp = owl.cpu_temp ?? 0;
    const fanRpm = owl.fan_status?.rpm ?? 0;
    const cpuPercent = owl.cpu_percent ?? 0;
    const memPercent = owl.memory_percent ?? 0;

    const detectionEnabled = owl.detection_enable || false;
    const recordingEnabled = owl.image_sample_enable || false;

    let tempClass = 'good';
    if (temp > 70) tempClass = 'danger';
    else if (temp > 60) tempClass = 'warning';

    const detectionBtnClass = detectionEnabled ? 'btn-detection active' : 'btn-detection inactive';
    const recordingBtnClass = recordingEnabled ? 'btn-recording active' : 'btn-recording inactive';
    const disabledAttr = isOnline ? '' : 'disabled';

    return `
        <div class="owl-card ${onlineClass}">
            <div class="owl-card-header">
                <div class="owl-card-title">
                    <h3>${deviceId}</h3>
                    <span class="owl-status-badge ${onlineClass}">
                        <span class="badge-dot"></span>
                        ${isOnline ? 'ONLINE' : 'OFFLINE'}
                    </span>
                </div>
            </div>
            <div class="owl-stats">
                <div class="owl-stat-item">
                    <div class="owl-stat-label">CPU Temp</div>
                    <div class="owl-stat-value ${tempClass}">${temp}°C</div>
                </div>
                <div class="owl-stat-item">
                    <div class="owl-stat-label">Fan</div>
                    <div class="owl-stat-value">${fanRpm} RPM</div>
                </div>
                <div class="owl-stat-item">
                    <div class="owl-stat-label">CPU</div>
                    <div class="owl-stat-value">${cpuPercent}%</div>
                </div>
                <div class="owl-stat-item">
                    <div class="owl-stat-label">Memory</div>
                    <div class="owl-stat-value">${memPercent}%</div>
                </div>
            </div>
            <div class="owl-actions">
                <button class="owl-btn btn-video" onclick="openVideoFeed('${deviceId}')" ${disabledAttr}>
                    Video
                </button>
                <button class="owl-btn ${detectionBtnClass}" onclick="toggleDetection('${deviceId}')" ${disabledAttr}>
                    ${detectionEnabled ? 'Detection ON' : 'Detection OFF'}
                </button>
                <button class="owl-btn ${recordingBtnClass}" onclick="toggleRecording('${deviceId}')" ${disabledAttr}>
                    ${recordingEnabled ? 'Recording ON' : 'Recording OFF'}
                </button>
            </div>
        </div>
    `;
}


// Update target OWL selector dropdown
function updateTargetSelector() {
    const select = document.getElementById('target-owl-select');
    if (!select) return;

    const currentValue = select.value;

    // Build options
    let optionsHTML = '<option value="all">All OWLs</option>';

    for (const deviceId of Object.keys(owlsData)) {
        optionsHTML += `<option value="${deviceId}">${deviceId}</option>`;
    }

    select.innerHTML = optionsHTML;

    // Restore selection if it still exists
    if (currentValue && Array.from(select.options).some(opt => opt.value === currentValue)) {
        select.value = currentValue;
    }
}

// Update last update timestamp
function updateLastUpdate() {
    const lastUpdateElement = document.getElementById('last-update');
    if (lastUpdateElement) {
        const now = new Date();
        lastUpdateElement.textContent = `Last update: ${now.toLocaleTimeString()}`;
    }
}

// Load default GreenOnBrown config values
async function loadConfigDefaults() {
    try {
        const response = await fetch('/api/greenonbrown/defaults');
        const data = await response.json();
        configDefaults = data;

        // convert {key: {label, value, ...}} --> {key: value}
        const flat = {};
        for (const [key, cfg] of Object.entries(configDefaults)) {
            flat[key] = cfg.value;
        }

        // use the better renderer
        renderGreenOnBrownControls(flat);

    } catch (error) {
        console.error('Error loading config defaults:', error);
    }
}


// Build configuration sliders
function buildConfigSliders() {
    const container = document.getElementById('greenonbrown-controls');
    if (!container) return;

    let html = '';

    for (const [key, config] of Object.entries(configDefaults)) {
        html += `
            <div class="slider-group">
                <div class="slider-label">
                    <span class="slider-label-text">${config.label}</span>
                    <span class="slider-value" id="slider-value-${key}">${config.value}</span>
                </div>
                <input 
                    type="range" 
                    class="slider-input" 
                    id="slider-${key}"
                    min="${config.min}" 
                    max="${config.max}" 
                    step="${config.step}" 
                    value="${config.value}"
                    data-key="${key}"
                    onchange="sendConfigValue('${key}', this.value)"
                    oninput="updateSliderValue('${key}', this.value)"
                >
            </div>
        `;
    }

    container.innerHTML = html;
}

function renderGreenOnBrownControls(configObj) {
    const container = document.getElementById('greenonbrown-controls');
    if (!container) return;

    container.innerHTML = '';

    // field -> [label, min, max]
    const fields = {
        brightness_max: ["Brightness Max", 0, 255],
        brightness_min: ["Brightness Min", 0, 255],
        exg_max: ["ExG Max", 0, 255],
        exg_min: ["ExG Min", 0, 255],
        hue_max: ["Hue Max", 0, 179],
        hue_min: ["Hue Min", 0, 179],
        min_detection_area: ["Min Detection Area", 0, 5000],
        saturation_max: ["Saturation Max", 0, 255],
        saturation_min: ["Saturation Min", 0, 255],
    };

    Object.entries(fields).forEach(([key, [label, min, max]]) => {
        const val = configObj && key in configObj ? configObj[key] : Math.floor((min + max) / 2);

        const group = document.createElement('div');
        group.className = 'control-group';
        group.innerHTML = `
            <div class="control-label-row">
                <label for="${key}">${label}</label>
                <span class="control-value" id="${key}-value">${val}</span>
            </div>
            <div class="control-slider-row">
                <span class="minmax">${min}</span>
                <input type="range"
                    id="${key}"
                    name="${key}"
                    min="${min}"
                    max="${max}"
                    value="${val}"
                    data-key="${key}">
                <span class="minmax">${max}</span>
            </div>
        `;
        container.appendChild(group);
    });

    // hook up change events
    container.querySelectorAll('input[type="range"]').forEach((input) => {
        input.addEventListener('input', onConfigSliderInput);
        input.addEventListener('change', onConfigSliderChange);
    });
}

function onConfigSliderInput(e) {
    const key = e.target.dataset.key;
    const val = e.target.value;
    const span = document.getElementById(`${key}-value`);
    if (span) span.textContent = val;
}

function onConfigSliderChange(e) {
    const key = e.target.dataset.key;
    const val = Number(e.target.value);

    const targetSelect = document.getElementById('target-owl-select');
    const target = targetSelect ? targetSelect.value : 'all';

    // send to backend
    sendConfigUpdate(target, { [key]: val });
}


// Update slider value display (while dragging)
function updateSliderValue(key, value) {
    const valueDisplay = document.getElementById(`slider-value-${key}`);
    if (valueDisplay) {
        valueDisplay.textContent = value;
    }
}

// Update config sliders based on selected OWL
function updateConfigSliders() {
    const targetSelect = document.getElementById('target-owl-select');
    const selectedOwl = targetSelect.value;

    if (selectedOwl === 'all') {
        // Reset to defaults
        for (const [key, config] of Object.entries(configDefaults)) {
            const slider = document.getElementById(`slider-${key}`);
            if (slider) {
                slider.value = config.value;
                updateSliderValue(key, config.value);
            }
        }
    } else {
        // Load values from selected OWL if available
        const owl = owlsData[selectedOwl];
        if (owl && owl.config) {
            for (const [key, value] of Object.entries(owl.config)) {
                const slider = document.getElementById(`slider-${key}`);
                if (slider) {
                    slider.value = value;
                    updateSliderValue(key, value);
                }
            }
        }
    }
}

// Send configuration value to OWL(s)
async function sendConfigValue(key, value) {
    const targetSelect = document.getElementById('target-owl-select');
    const target = targetSelect.value;

    const payload = {
        device_id: target,
        action: 'set_config',
        value: {
            section: 'GreenOnBrown',
            key: key,
            value: parseInt(value)
        }
    };

    try {
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        const result = await response.json();

        if (result.success) {
            console.log(`Config ${key} set to ${value} for ${target}`);
        } else {
            console.error('Failed to send config:', result.error);
            showNotification(`Failed to update ${key}`, 'error');
        }

    } catch (error) {
        console.error('Error sending config:', error);
        showNotification('Network error', 'error');
    }
}

// Toggle detection for an OWL
async function toggleDetection(deviceId) {
    await sendCommand(deviceId, 'toggle_detection');
}

// Toggle recording for an OWL
async function toggleRecording(deviceId) {
    await sendCommand(deviceId, 'toggle_recording');
}

// Send command to OWL via MQTT
async function sendCommand(deviceId, action, value = null) {
    const payload = {
        device_id: deviceId,
        action: action
    };

    if (value !== null) {
        payload.value = value;
    }

    try {
        const response = await fetch('/api/command', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        const result = await response.json();

        if (result.success) {
            console.log(`Command ${action} sent to ${deviceId}`);
            // Force immediate update
            setTimeout(updateDashboard, 500);
        } else {
            console.error('Command failed:', result.error);
            showNotification(`Failed to ${action}`, 'error');
        }

    } catch (error) {
        console.error('Error sending command:', error);
        showNotification('Network error', 'error');
    }
}
// Open video feed modal
function openVideoFeed(deviceId) {
    const modal = document.getElementById('video-modal');
    const title = document.getElementById('video-modal-title');
    const img = document.getElementById('video-feed-img');

    // Set title
    title.textContent = `${deviceId} - Video Feed`;

    // Set video feed URL - to our new PROXY endpoint
    const videoUrl = `/api/video_feed/${deviceId}`; //
    console.log(`Loading video feed from proxy: ${videoUrl}`);

    // Show modal first
    modal.style.display = 'block';

    // Then set image source
    img.src = videoUrl;

    // We can simplify the error handler now
    img.onload = function() {
        console.log('Video proxy feed loaded successfully');
    };

    img.onerror = function() {
        console.error(`Failed to load video proxy feed from ${videoUrl}`);
        img.alt = 'Video feed unavailable. Check controller logs and if OWL is online.';
        // We set src to blank to stop retry loops
        img.src = '';
    };
}

// Close video feed modal
function closeVideoModal() {
    const modal = document.getElementById('video-modal');
    const img = document.getElementById('video-feed-img');

    // Hide modal
    modal.style.display = 'none';

    // Clear image source to stop loading
    img.src = '';
}

// Close modal when clicking outside
document.addEventListener('click', function(event) {
    const modal = document.getElementById('video-modal');
    if (event.target === modal) {
        closeVideoModal();
    }
});

// Show notification (simple toast)
function showNotification(message, type = 'info') {
    // Simple console notification for now
    // Could be enhanced with actual toast notifications
    console.log(`[${type.toUpperCase()}] ${message}`);
}

// Cleanup on page unload
window.addEventListener('beforeunload', function() {
    if (updateInterval) {
        clearInterval(updateInterval);
    }
});

console.log('OWL Central Controller JavaScript loaded');