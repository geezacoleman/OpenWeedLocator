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

function renderGreenOnBrownControls(configObj) {
    const container = document.getElementById('greenonbrown-controls');
    if (!container) return;

    container.innerHTML = '';

    // 9 controls, fixed order for 3x3
    const fields = [
        ["brightness_max", "Brightness Max", 0, 255],
        ["brightness_min", "Brightness Min", 0, 255],
        ["exg_max",        "ExG Max",        0, 255],
        ["exg_min",        "ExG Min",        0, 255],
        ["hue_max",        "Hue Max",        0, 179],
        ["hue_min",        "Hue Min",        0, 179],
        ["min_detection_area", "Min Det. Area", 0, 5000],
        ["saturation_max", "Saturation Max", 0, 255],
        ["saturation_min", "Saturation Min", 0, 255],
    ];

    for (const [key, label, min, max] of fields) {
        // value from device/defaults
        const val = (configObj && key in configObj) ? configObj[key] : Math.floor((min + max) / 2);

        const group = document.createElement('div');
        group.className = 'control-group';
        group.innerHTML = `
            <div class="control-label-row">
                <label for="${key}">${label}</label>
                <span class="control-value" id="${key}-value">${val}</span>
            </div>
            <div class="control-slider-row">
                <span class="minmax">${min}</span>
                <input
                    type="range"
                    id="${key}"
                    name="${key}"
                    min="${min}"
                    max="${max}"
                    value="${val}"
                    data-key="${key}"
                >
                <span class="minmax">${max}</span>
            </div>
        `;
        container.appendChild(group);
    }

    // hook events
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

// ============================================
// CONFIG PAGINATION SYSTEM
// Add this to your existing main.js
// ============================================

// Config state
let currentConfigPage = 1;
const totalConfigPages = 4;

// Parameter definitions with min/max ranges
const configParams = {
    exg_min: { value: 25, min: 0, max: 255 },
    exg_max: { value: 200, min: 0, max: 255 },
    hue_min: { value: 39, min: 0, max: 179 },
    hue_max: { value: 83, min: 0, max: 179 },
    saturation_min: { value: 50, min: 0, max: 255 },
    saturation_max: { value: 220, min: 0, max: 255 },
    brightness_min: { value: 60, min: 0, max: 255 },
    brightness_max: { value: 190, min: 0, max: 255 },
    min_detection_area: { value: 10, min: 1, max: 1000 }
};

// Initialize config system
function initConfigSystem() {
    console.log('[CONFIG] Initializing pagination system...');

    // Setup button event listeners
    setupConfigButtons();

    // Setup slider interactions
    setupSliders();

    // Initialize all sliders with default values
    updateAllSliders();

    console.log('[CONFIG] Pagination system ready');
}

// Setup config button listeners
function setupConfigButtons() {
    // Open config button
    const openBtn = document.getElementById('open-config-btn');
    if (openBtn) {
        openBtn.addEventListener('click', openConfigPanel);
    }

    // Close config button
    const closeBtn = document.getElementById('close-config-btn');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeConfigPanel);
    }

    // Page navigation buttons
    const prevBtn = document.getElementById('prev-page-btn');
    const nextBtn = document.getElementById('next-page-btn');

    if (prevBtn) {
        prevBtn.addEventListener('click', () => goToConfigPage(currentConfigPage - 1));
    }

    if (nextBtn) {
        nextBtn.addEventListener('click', () => goToConfigPage(currentConfigPage + 1));
    }

    // Page dots
    document.querySelectorAll('.page-dot').forEach(dot => {
        dot.addEventListener('click', () => {
            const page = parseInt(dot.dataset.page);
            goToConfigPage(page);
        });
    });

    // Slider adjustment buttons
    document.querySelectorAll('.slider-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const param = btn.dataset.param;
            const delta = parseInt(btn.dataset.delta);
            adjustParameter(param, delta);
        });
    });

    // Action buttons
    const applyBtn = document.getElementById('apply-config-btn');
    if (applyBtn) {
        applyBtn.addEventListener('click', applyConfigToAll);
    }

    const resetBtn = document.getElementById('reset-config-btn');
    if (resetBtn) {
        resetBtn.addEventListener('click', resetConfigDefaults);
    }
}

// Setup slider interactions
function setupSliders() {
    document.querySelectorAll('.slider-vertical').forEach(slider => {
        slider.addEventListener('click', handleSliderClick);
    });
}

// Open config panel
function openConfigPanel() {
    console.log('[CONFIG] Opening panel');
    const panel = document.getElementById('config-panel');
    if (panel) {
        panel.classList.add('open');
        // Reset to first page when opening
        goToConfigPage(1);
    }
}

// Close config panel
function closeConfigPanel() {
    console.log('[CONFIG] Closing panel');
    const panel = document.getElementById('config-panel');
    if (panel) {
        panel.classList.remove('open');
    }
}

// Navigate to specific config page
function goToConfigPage(pageNum) {
    if (pageNum < 1 || pageNum > totalConfigPages) return;

    console.log(`[CONFIG] Navigating to page ${pageNum}`);

    // Update current page
    currentConfigPage = pageNum;

    // Hide all pages
    document.querySelectorAll('.config-page').forEach(page => {
        page.classList.remove('active');
    });

    // Show target page
    const targetPage = document.getElementById(`page-${pageNum}`);
    if (targetPage) {
        targetPage.classList.add('active');
    }

    // Update page dots
    document.querySelectorAll('.page-dot').forEach(dot => {
        dot.classList.remove('active');
        if (parseInt(dot.dataset.page) === pageNum) {
            dot.classList.add('active');
        }
    });

    // Update navigation buttons
    const prevBtn = document.getElementById('prev-page-btn');
    const nextBtn = document.getElementById('next-page-btn');

    if (prevBtn) {
        prevBtn.disabled = (pageNum === 1);
    }

    if (nextBtn) {
        nextBtn.disabled = (pageNum === totalConfigPages);
    }

    // Update page title
    const titles = ['', 'ExG (Excess Green)', 'Hue', 'Saturation', 'Brightness & Min Area'];
    const titleEl = document.getElementById('config-page-title');
    if (titleEl && titles[pageNum]) {
        titleEl.textContent = `Config: ${titles[pageNum]}`;
    }
}

// Handle slider click
function handleSliderClick(event) {
    const slider = event.currentTarget;
    const param = slider.dataset.param;
    const min = parseInt(slider.dataset.min);
    const max = parseInt(slider.dataset.max);

    // Calculate click position
    const rect = slider.getBoundingClientRect();
    const clickY = event.clientY - rect.top;
    const percentage = 100 - ((clickY / rect.height) * 100);

    // Convert to value
    const range = max - min;
    const value = Math.round((percentage / 100) * range + min);

    // Clamp to bounds
    const clampedValue = Math.max(min, Math.min(max, value));

    console.log(`[CONFIG] Slider click: ${param} = ${clampedValue}`);

    // Update parameter
    if (configParams[param]) {
        configParams[param].value = clampedValue;
        updateSlider(param);
        sendConfigUpdate(param, clampedValue);
    }
}

// Adjust parameter value
function adjustParameter(param, delta) {
    if (!configParams[param]) return;

    const p = configParams[param];
    const newValue = p.value + delta;
    const clampedValue = Math.max(p.min, Math.min(p.max, newValue));

    console.log(`[CONFIG] Adjust: ${param} ${delta > 0 ? '+' : ''}${delta} = ${clampedValue}`);

    p.value = clampedValue;
    updateSlider(param);
    sendConfigUpdate(param, clampedValue);
}

// Update slider visual
function updateSlider(param) {
    const p = configParams[param];
    const percentage = ((p.value - p.min) / (p.max - p.min)) * 100;

    // Update value display
    const valueEl = document.getElementById(`${param.replace('_', '-')}-value`);
    if (valueEl) {
        valueEl.textContent = p.value;
    }

    // Update track height
    const trackEl = document.getElementById(`${param.replace('_', '-')}-track`);
    if (trackEl) {
        trackEl.style.height = `${percentage}%`;
    }

    // Update thumb position
    const thumbEl = document.getElementById(`${param.replace('_', '-')}-thumb`);
    if (thumbEl) {
        thumbEl.style.bottom = `calc(${percentage}% - 35px)`;
    }
}

// Update all sliders to current values
function updateAllSliders() {
    console.log('[CONFIG] Updating all sliders...');
    for (const param in configParams) {
        updateSlider(param);
    }
}

// Send config update to backend
function sendConfigUpdate(param, value) {
    const targetSelect = document.getElementById('target-owl-select');
    const target = targetSelect ? targetSelect.value : 'all';

    console.log(`[CONFIG] Sending update: ${param} = ${value} to ${target}`);

    fetch('/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            device_id: target,
            action: 'set_config',
            value: {
                key: param,
                value: value
            }
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            console.log(`[CONFIG] Update successful: ${param}`);
        } else {
            console.error(`[CONFIG] Update failed: ${data.error}`);
        }
    })
    .catch(error => {
        console.error(`[CONFIG] Error sending update:`, error);
    });
}

// Apply config to all OWLs
function applyConfigToAll() {
    console.log('[CONFIG] Applying to all OWLs...');

    // Send all parameter values
    for (const param in configParams) {
        sendConfigUpdate(param, configParams[param].value);
    }

    // Show feedback (optional)
    showNotification('Configuration applied to all OWLs', 'success');
}

// Reset to default values
function resetConfigDefaults() {
    console.log('[CONFIG] Resetting to defaults...');

    // Reset values
    configParams.exg_min.value = 25;
    configParams.exg_max.value = 200;
    configParams.hue_min.value = 39;
    configParams.hue_max.value = 83;
    configParams.saturation_min.value = 50;
    configParams.saturation_max.value = 220;
    configParams.brightness_min.value = 60;
    configParams.brightness_max.value = 190;
    configParams.min_detection_area.value = 10;

    // Update all sliders
    updateAllSliders();

    // Send to backend
    applyConfigToAll();

    // Show feedback
    showNotification('Reset to default values', 'info');
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
    console.log('[CONFIG] DOM ready, initializing config system...');
    initConfigSystem();
});

// Export functions for use in main code if needed
window.configSystem = {
    openPanel: openConfigPanel,
    closePanel: closeConfigPanel,
    goToPage: goToConfigPage,
    updateParam: adjustParameter,
    applyAll: applyConfigToAll,
    reset: resetConfigDefaults
};

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