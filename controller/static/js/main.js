// OWL Central Controller (paged config, loads defaults from API)

let owlsData = {};
let mqttConnected = false;
let updateInterval = null;
const offlineCounts = {};
const MAX_OFFLINE_POLLS = 5;
const UPDATE_INTERVAL = 2000;

// Canonical parameter map (snake_case); ranges optional and will be overridden by API if provided
const configParams = {
    exg_min: {value: 0, min: 0, max: 255},
    exg_max: {value: 0, min: 0, max: 255},
    hue_min: {value: 0, min: 0, max: 179},
    hue_max: {value: 0, min: 0, max: 179},
    saturation_min: {value: 0, min: 0, max: 255},
    saturation_max: {value: 0, min: 0, max: 255},
    brightness_min: {value: 0, min: 0, max: 255},
    brightness_max: {value: 0, min: 0, max: 255},
    min_detection_area: {value: 0, min: 1, max: 1000}
};

let currentConfigPage = 1;
const totalConfigPages = 5;

document.addEventListener('DOMContentLoaded', async () => {
    setupConfigUI();
    await loadConfigDefaults();    // <-- pull initial values/ranges from backend
    updateAllSliders();            // reflect loaded defaults visually
    updateDashboard();
    updateInterval = setInterval(updateDashboard, UPDATE_INTERVAL);
});

// Load defaults from your existing endpoint (no hardcoding)
async function loadConfigDefaults() {
    try {
        const res = await fetch('/api/greenonbrown/defaults');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        // Expected either: {key:{value,min,max}} OR {key:value}. Support both.
        for (const [key, cfg] of Object.entries(data)) {
            if (!(key in configParams)) continue;
            if (cfg && typeof cfg === 'object') {
                if (typeof cfg.value !== 'undefined') configParams[key].value = cfg.value;
                if (typeof cfg.min !== 'undefined') configParams[key].min = cfg.min;
                if (typeof cfg.max !== 'undefined') configParams[key].max = cfg.max;
            } else {
                configParams[key].value = cfg; // flat number
            }
        }
    } catch (err) {
        console.error('Error loading config defaults:', err);
    }
}

// Dashboard polling
async function updateDashboard() {
    try {
        const res = await fetch('/api/owls');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        mqttConnected = !!data.mqtt_connected;
        const fresh = data.owls || {};
        owlsData = pruneAndMergeOwls(owlsData, fresh);
        updateMQTTStatus();
        updateOWLGrid();
        updateTargetSelector();
        updateLastUpdate();
    } catch (err) {
        console.error(err);
        mqttConnected = false;
        updateMQTTStatus();
    }
}

function pruneAndMergeOwls(current, fresh) {
    const result = {};
    for (const [id, owl] of Object.entries(fresh)) {
        if (owl.connected) {
            offlineCounts[id] = 0;
        } else {
            offlineCounts[id] = (offlineCounts[id] || 0) + 1;
        }
        result[id] = owl;
    }
    for (const id of Object.keys(current)) {
        if (!(id in fresh)) {
            offlineCounts[id] = (offlineCounts[id] || 0) + 1;
            if (offlineCounts[id] < MAX_OFFLINE_POLLS) {
                result[id] = {...current[id], connected: false};
            } else {
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

function updateOWLGrid() {
    const grid = document.getElementById('owls-grid');
    if (!grid) return;
    const ids = Object.keys(owlsData);
    if (ids.length === 0) {
        grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="empty-state-text">No OWLs Connected</div><div class="empty-state-subtext">Waiting for MQTT…</div></div>`;
        return;
    }
    grid.innerHTML = ids.map(id => buildOWLCard(id, owlsData[id])).join('');
}

function buildOWLCard(deviceId, owl) {
    const isOnline = !!owl.connected;
    const onlineClass = isOnline ? 'online' : 'offline';
    const temp = owl.cpu_temp ?? 0;
    const fan = owl.fan_status?.rpm ?? 0;
    const cpu = owl.cpu_percent ?? 0;
    const mem = owl.memory_percent ?? 0;
    const detectionEnabled = !!owl.detection_enable;
    const recordingEnabled = !!owl.image_sample_enable;
    let tempClass = 'good';
    if (temp > 70) tempClass = 'danger'; else if (temp > 60) tempClass = 'warning';
    const disAttr = isOnline ? '' : 'disabled';
    return `
  <div class="owl-card ${onlineClass}">
    <div class="owl-card-header">
      <div class="owl-card-title"><h3>${deviceId}</h3></div>
      <span class="owl-status-badge ${onlineClass}"><span class="badge-dot"></span>${isOnline ? 'ONLINE' : 'OFFLINE'}</span>
    </div>
    <div class="owl-stats">
      <div class="owl-stat-item"><div class="owl-stat-label">CPU Temp</div><div class="owl-stat-value ${tempClass}">${temp}°C</div></div>
      <div class="owl-stat-item"><div class="owl-stat-label">Fan</div><div class="owl-stat-value">${fan} RPM</div></div>
      <div class="owl-stat-item"><div class="owl-stat-label">CPU</div><div class="owl-stat-value">${cpu}%</div></div>
      <div class="owl-stat-item"><div class="owl-stat-label">Memory</div><div class="owl-stat-value">${mem}%</div></div>
    </div>
    <div class="owl-actions">
      <button class="owl-btn btn-video" onclick="openVideoFeed('${deviceId}')" ${disAttr}>Video</button>
      <button class="owl-btn btn-detection" onclick="toggleDetection('${deviceId}')" ${disAttr}>${detectionEnabled ? 'Detection ON' : 'Detection OFF'}</button>
      <button class="owl-btn btn-recording" onclick="toggleRecording('${deviceId}')" ${disAttr}>${recordingEnabled ? 'Recording ON' : 'Recording OFF'}</button>
    </div>
  </div>`;
}

function updateTargetSelector() {
    const sel = document.getElementById('target-owl-select');
    if (!sel) return;
    const keep = sel.value;
    let html = '<option value="all">All OWLs</option>';
    for (const id of Object.keys(owlsData)) html += `<option value="${id}">${id}</option>`;
    sel.innerHTML = html;
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
}

function updateLastUpdate() {
    const el = document.getElementById('last-update');
    if (el) {
        el.textContent = 'Last update: ' + new Date().toLocaleTimeString();
    }
}

// Commands
async function sendCommand(deviceId, action, value = null) {
    const payload = {device_id: deviceId, action, ...(value !== null ? {value} : {})};
    const res = await fetch('/api/command', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    });
    const out = await res.json();
    if (!out.success) console.error('Command failed:', out.error); else setTimeout(updateDashboard, 400);
}

async function toggleDetection(deviceId) {
    await sendCommand(deviceId, 'toggle_detection');
}

async function toggleRecording(deviceId) {
    await sendCommand(deviceId, 'toggle_recording');
}

// --- Config UI ---
function setupConfigUI() {
    const openBtn = document.getElementById('open-config-btn');
    const closeBtn = document.getElementById('close-config-btn');
    const panel = document.getElementById('config-panel');
    openBtn?.addEventListener('click', () => {
        panel?.classList.add('open');
        goToConfigPage(1);
    });
    closeBtn?.addEventListener('click', () => panel?.classList.remove('open'));

    document.getElementById('prev-page-btn')?.addEventListener('click', () => goToConfigPage(currentConfigPage - 1));
    document.getElementById('next-page-btn')?.addEventListener('click', () => goToConfigPage(currentConfigPage + 1));
    document.querySelectorAll('.page-dot').forEach(dot => dot.addEventListener('click', () => goToConfigPage(parseInt(dot.dataset.page))));

    document.querySelectorAll('.slider-btn').forEach(btn => btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const p = btn.dataset.param;
        const d = parseInt(btn.dataset.delta);
        adjustParameter(p, d);
    }));
    document.querySelectorAll('.slider-vertical').forEach(sl => sl.addEventListener('click', handleSliderClick));

    document.getElementById('apply-config-btn')?.addEventListener('click', applyConfigToAll);
    document.getElementById('reset-config-btn')?.addEventListener('click', resetConfigDefaults);

    window.addEventListener('keydown', (e) => {
        if (!panel?.classList.contains('open')) return;
        if (e.key === 'ArrowRight') goToConfigPage(currentConfigPage + 1);
        if (e.key === 'ArrowLeft') goToConfigPage(currentConfigPage - 1);
    });
}

function goToConfigPage(n) {
    if (n < 1 || n > totalConfigPages) return;
    currentConfigPage = n;
    document.querySelectorAll('.config-page').forEach(p => p.classList.remove('active'));
    document.getElementById(`page-${n}`)?.classList.add('active');
    document.querySelectorAll('.page-dot').forEach(d => {
        d.classList.toggle('active', parseInt(d.dataset.page) === n);
    });
    document.getElementById('prev-page-btn').disabled = (n === 1);
    document.getElementById('next-page-btn').disabled = (n === totalConfigPages);
    const title = document.getElementById(`page-${n}`)?.dataset.title || 'Config';
    document.getElementById('config-page-title').textContent = `Config: ${title}`;
}

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
    const v = Math.max(p.min, Math.min(p.max, p.value + delta));
    p.value = v;
    updateSlider(param);
    sendConfigUpdate(param, v);
}

function updateSlider(param) {
    const p = configParams[param];
    const pct = ((p.value - p.min) / (p.max - p.min)) * 100;
    const valueEl = document.getElementById(`${param}-value`);
    if (valueEl) valueEl.textContent = p.value;
    const track = document.getElementById(`${param}-track`);
    if (track) track.style.height = pct + '%';
    const thumb = document.getElementById(`${param}-thumb`);
    if (thumb) thumb.style.bottom = `calc(${pct}% - 35px)`;
}

function updateAllSliders() {
    for (const k in configParams) updateSlider(k);
}

function sendConfigUpdate(param, value) {
    const sel = document.getElementById('target-owl-select');
    const target = sel ? sel.value : 'all';
    fetch('/api/command', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({device_id: target, action: 'set_config', value: {key: param, value}})
    })
        .then(r => r.json()).then(j => {
        if (!j.success) console.error('Update failed', j.error);
    });
}

function applyConfigToAll() {
    for (const k in configParams) sendConfigUpdate(k, configParams[k].value);
    console.log('Configuration applied to all OWLs');
}

async function resetConfigDefaults() {
    await loadConfigDefaults();
    updateAllSliders();
    applyConfigToAll();
    console.log('Reset to backend defaults');
}

// Video modal
function openVideoFeed(deviceId) {
    const m = document.getElementById('video-modal');
    const img = document.getElementById('video-feed-img');
    const t = document.getElementById('video-modal-title');
    t.textContent = `${deviceId} Video Feed`;
    img.src = `/api/video_feed/${deviceId}`;
    m.style.display = 'flex';
    document.body.classList.add('modal-open');
}

function closeVideoModal() {
    const m = document.getElementById('video-modal');
    if (!m) return;
    m.style.display = 'none';
    document.body.classList.remove('modal-open');
}
