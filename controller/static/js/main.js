// ============================================
// OWL Central Controller - Main JavaScript
// ============================================

let owlsData = {};
let mqttConnected = false;
let updateInterval = null;
const UPDATE_INTERVAL = 2000;

// Configuration parameters
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

let currentConfigPage = 1;
const totalConfigPages = 5;

// Global detection state
let globalDetectionEnabled = false;
let globalRecordingEnabled = false;

// ============================================
// INITIALIZATION
// ============================================

document.addEventListener('DOMContentLoaded', async () => {
    setupEventListeners();
    await loadConfigDefaults();
    updateAllSliders();
    updateDashboard();
    updateInterval = setInterval(updateDashboard, UPDATE_INTERVAL);
});

function setupEventListeners() {
    // Config drawer
    document.getElementById('open-config-btn')?.addEventListener('click', openConfig);
    document.getElementById('close-config-btn')?.addEventListener('click', closeConfig);
    document.getElementById('overlay')?.addEventListener('click', closeConfig);

    // Pagination
    document.getElementById('prev-page-btn')?.addEventListener('click', () => goToConfigPage(currentConfigPage - 1));
    document.getElementById('next-page-btn')?.addEventListener('click', () => goToConfigPage(currentConfigPage + 1));

    // Page dots
    document.querySelectorAll('.page-dot').forEach(dot => {
        dot.addEventListener('click', () => goToConfigPage(parseInt(dot.dataset.page)));
    });

    // Slider buttons
    document.querySelectorAll('.slider-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const param = btn.dataset.param;
            const delta = parseInt(btn.dataset.delta);
            adjustParameter(param, delta);
        });
    });

    // Slider click
    document.querySelectorAll('.slider-vertical').forEach(slider => {
        slider.addEventListener('click', handleSliderClick);
    });

    // Config actions
    document.getElementById('apply-config-btn')?.addEventListener('click', applyConfigToAll);
    document.getElementById('reset-config-btn')?.addEventListener('click', resetConfigDefaults);

    // Keyboard support
    window.addEventListener('keydown', (e) => {
        const panel = document.getElementById('config-panel');
        if (!panel?.classList.contains('open')) return;

        if (e.key === 'ArrowRight') goToConfigPage(currentConfigPage + 1);
        if (e.key === 'ArrowLeft') goToConfigPage(currentConfigPage - 1);
        if (e.key === 'Escape') closeConfig();
    });
}

// ============================================
// CONFIG DEFAULTS LOADING
// ============================================

async function loadConfigDefaults() {
    try {
        const res = await fetch('/api/greenonbrown/defaults');
        if (!res.ok) throw new Error('HTTP ' + res.status);

        const data = await res.json();

        for (const [key, cfg] of Object.entries(data)) {
            if (!(key in configParams)) continue;

            if (cfg && typeof cfg === 'object') {
                if (typeof cfg.value !== 'undefined') configParams[key].value = cfg.value;
                if (typeof cfg.min !== 'undefined') configParams[key].min = cfg.min;
                if (typeof cfg.max !== 'undefined') configParams[key].max = cfg.max;
            } else {
                configParams[key].value = cfg;
            }
        }

        console.log('Config defaults loaded:', configParams);
    } catch (err) {
        console.error('Error loading config defaults:', err);
        showToast('Failed to load config defaults', 'error');
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
        updateTargetSelector();
    } catch (err) {
        console.error('Dashboard update error:', err);
        mqttConnected = false;
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

    const ids = Object.keys(owlsData);

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
                    <div class="owl-stat-label">🌡️ CPU Temp</div>
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
                    📹 VIDEO
                </button>
            </div>
        </div>
    `;
}

function updateTargetSelector() {
    const sel = document.getElementById('target-owl-select');
    if (!sel) return;

    const currentValue = sel.value;
    let html = '<option value="all">All OWLs</option>';

    for (const id of Object.keys(owlsData)) {
        html += `<option value="${id}">${id}</option>`;
    }

    sel.innerHTML = html;

    // Restore selection if it still exists
    if ([...sel.options].some(o => o.value === currentValue)) {
        sel.value = currentValue;
    }
}

// ============================================
// MAIN ACTION BUTTONS
// ============================================

function toggleMainDetection() {
    const btn = document.getElementById('main-detection-btn');

    if (btn.classList.contains('off')) {
        // Currently stopping, so start
        btn.classList.remove('off');
        btn.textContent = 'START DETECTION';
        globalDetectionEnabled = false;
        sendCommand('all', 'toggle_detection', false);
        showToast('Detection stopped on all OWLs', 'info');
    } else {
        // Currently starting, so stop
        btn.classList.add('off');
        btn.textContent = 'STOP DETECTION';
        globalDetectionEnabled = true;
        sendCommand('all', 'toggle_detection', true);
        showToast('Detection started on all OWLs', 'success');
    }
}

function toggleMainRecording() {
    const btn = document.getElementById('main-recording-btn');

    if (btn.classList.contains('active')) {
        // Currently on, so turn off
        btn.classList.remove('active');
        btn.textContent = 'RECORDING OFF';
        globalRecordingEnabled = false;
        sendCommand('all', 'toggle_recording', false);
        showToast('Recording stopped on all OWLs', 'info');
    } else {
        // Currently off, so turn on
        btn.classList.add('active');
        btn.textContent = 'RECORDING ON';
        globalRecordingEnabled = true;
        sendCommand('all', 'toggle_recording', true);
        showToast('Recording started on all OWLs', 'success');
    }
}

// ============================================
// COMMAND SENDING
// ============================================

async function sendCommand(deviceId, action, value = null) {
    try {
        const payload = {
            device_id: deviceId,
            action: action
        };

        if (value !== null) {
            payload.value = value;
        }

        const res = await fetch('/api/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const result = await res.json();

        if (!result.success) {
            console.error('Command failed:', result.error);
            showToast('Command failed: ' + result.error, 'error');
        } else {
            setTimeout(updateDashboard, 400);
        }

        return result;
    } catch (err) {
        console.error('Error sending command:', err);
        showToast('Error sending command', 'error');
        return { success: false, error: err.message };
    }
}

// ============================================
// CONFIG DRAWER
// ============================================

function openConfig() {
    document.getElementById('config-panel')?.classList.add('open');
    document.getElementById('overlay')?.classList.add('active');
    goToConfigPage(1);
}

function closeConfig() {
    document.getElementById('config-panel')?.classList.remove('open');
    document.getElementById('overlay')?.classList.remove('active');
}

function goToConfigPage(n) {
    if (n < 1 || n > totalConfigPages) return;

    currentConfigPage = n;

    // Update page visibility
    document.querySelectorAll('.config-page').forEach(page => {
        page.classList.remove('active');
    });
    document.getElementById(`page-${n}`)?.classList.add('active');

    // Update dots
    document.querySelectorAll('.page-dot').forEach(dot => {
        dot.classList.toggle('active', parseInt(dot.dataset.page) === n);
    });

    // Update buttons
    document.getElementById('prev-page-btn').disabled = (n === 1);
    document.getElementById('next-page-btn').disabled = (n === totalConfigPages);

    // Update title
    const title = document.getElementById(`page-${n}`)?.dataset.title || 'Config';
    document.getElementById('config-page-title').textContent = title;
}

// ============================================
// SLIDER CONTROLS
// ============================================

function handleSliderClick(e) {
    const slider = e.currentTarget;
    const param = slider.dataset.param;
    const p = configParams[param];

    if (!p) return;

    const rect = slider.getBoundingClientRect();
    const clickY = e.clientY - rect.top;
    const pct = 100 - (clickY / rect.height) * 100;
    const val = Math.round((pct / 100) * (p.max - p.min) + p.min);

    configParams[param].value = Math.max(p.min, Math.min(p.max, val));
    updateSlider(param);
    sendConfigUpdate(param, configParams[param].value);
}

function adjustParameter(param, delta) {
    const p = configParams[param];
    if (!p) return;

    const newVal = Math.max(p.min, Math.min(p.max, p.value + delta));
    p.value = newVal;
    updateSlider(param);
    sendConfigUpdate(param, newVal);
}

function updateSlider(param) {
    const p = configParams[param];
    if (!p) return;

    const pct = ((p.value - p.min) / (p.max - p.min)) * 100;

    const valueEl = document.getElementById(`${param}-value`);
    if (valueEl) valueEl.textContent = p.value;

    const track = document.getElementById(`${param}-track`);
    if (track) track.style.height = pct + '%';

    const thumb = document.getElementById(`${param}-thumb`);
    if (thumb) thumb.style.bottom = `calc(${pct}% - 30px)`;
}

function updateAllSliders() {
    for (const key in configParams) {
        updateSlider(key);
    }
}

function sendConfigUpdate(param, value) {
    const sel = document.getElementById('target-owl-select');
    const target = sel ? sel.value : 'all';

    sendCommand(target, 'set_config', { key: param, value: value });
}

function applyConfigToAll() {
    let count = 0;
    for (const key in configParams) {
        sendConfigUpdate(key, configParams[key].value);
        count++;
    }
    showToast(`Applied ${count} settings to all OWLs`, 'success');
}

async function resetConfigDefaults() {
    await loadConfigDefaults();
    updateAllSliders();
    applyConfigToAll();
    showToast('Reset to default values', 'info');
}

// ============================================
// VIDEO MODAL
// ============================================

function openVideoFeed(deviceId) {
    const modal = document.getElementById('video-modal');
    const img = document.getElementById('video-feed-img');
    const title = document.getElementById('video-modal-title');

    if (!modal || !img || !title) return;

    title.textContent = `${deviceId} Video Feed`;
    img.src = `/api/video_feed/${deviceId}`;
    modal.style.display = 'flex';
}

function closeVideoModal() {
    const modal = document.getElementById('video-modal');
    const img = document.getElementById('video-feed-img');

    if (!modal || !img) return;

    modal.style.display = 'none';
    img.src = ''; // Stop video stream
}

// ============================================
// TOAST NOTIFICATIONS
// ============================================

function showToast(message, type = 'info') {
    const toast = document.getElementById('toast');
    if (!toast) return;

    // Remove existing classes
    toast.classList.remove('success', 'error', 'warning', 'info', 'show');

    // Set message and type
    toast.textContent = message;
    toast.classList.add(type);

    // Show toast
    setTimeout(() => toast.classList.add('show'), 10);

    // Hide after 3 seconds
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// ============================================
// UTILITY FUNCTIONS
// ============================================

function formatUptime(seconds) {
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    return `${hours}h ${mins}m`;
}

// ============================================
// CLEANUP
// ============================================

window.addEventListener('beforeunload', () => {
    if (updateInterval) {
        clearInterval(updateInterval);
    }
});