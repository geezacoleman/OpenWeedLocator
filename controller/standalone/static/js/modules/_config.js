/* ==========================================================================
   OWL Dashboard - Config Editor Module
   INI configuration editor with sections, fields, and modals
   ========================================================================== */

let originalConfig = {};
let currentConfig = {};
let configHasChanges = false;
let activeConfigPath = '';
let isDefaultConfig = true;
let availableConfigs = [];

const CONFIG_FIELD_DEFS = {
    'System': {
        'algorithm': { type: 'select', options: ['exhsv', 'exg', 'hsv', 'gog'], help: 'Detection algorithm' },
        'input_file_or_directory': { type: 'text', help: 'Leave empty for camera input' },
        'relay_num': { type: 'number', min: 1, max: 8, help: 'Number of relays (1-8)' },
        'actuation_duration': { type: 'number', step: 0.01, min: 0.01, max: 2.0, help: 'Spray duration in seconds' },
        'delay': { type: 'number', step: 0.01, min: 0, max: 5.0, help: 'Delay before actuation' }
    },
    'MQTT': {
        'enable': { type: 'boolean', help: 'Enable MQTT communication' },
        'broker_ip': { type: 'text', help: 'MQTT broker IP address' },
        'broker_port': { type: 'number', min: 1, max: 65535, help: 'MQTT broker port' },
        'device_id': { type: 'text', help: 'Device identifier' }
    },
    'Camera': {
        'resolution_width': { type: 'select', options: ['320', '640', '800', '1024', '1280', '1920'], help: 'Width' },
        'resolution_height': { type: 'select', options: ['240', '480', '600', '768', '720', '1080'], help: 'Height' },
        'exp_compensation': { type: 'number', min: -10, max: 10, help: 'Exposure compensation' }
    },
    'GreenOnBrown': {
        'exg_min': { type: 'number', min: 0, max: 255 },
        'exg_max': { type: 'number', min: 0, max: 255 },
        'hue_min': { type: 'number', min: 0, max: 179 },
        'hue_max': { type: 'number', min: 0, max: 179 },
        'saturation_min': { type: 'number', min: 0, max: 255 },
        'saturation_max': { type: 'number', min: 0, max: 255 },
        'brightness_min': { type: 'number', min: 0, max: 255 },
        'brightness_max': { type: 'number', min: 0, max: 255 },
        'min_detection_area': { type: 'number', min: 1, max: 10000 },
        'invert_hue': { type: 'boolean' }
    },
    'GreenOnGreen': {
        'model_path': { type: 'text', help: 'Path to AI model' },
        'confidence': { type: 'number', step: 0.05, min: 0.1, max: 1.0, help: 'Detection threshold' }
    },
    'DataCollection': {
        'image_sample_enable': { type: 'boolean', help: 'Enable image saving' },
        'sample_method': { type: 'select', options: ['whole', 'bbox', 'square'] },
        'sample_frequency': { type: 'number', min: 1, max: 1000 },
        'save_directory': { type: 'text', help: 'Save directory path' },
        'detection_enable': { type: 'boolean' }
    },
    'Controller': {
        'controller_type': { type: 'select', options: ['none', 'ute', 'advanced', 'networked'] }
    },
    'GPS': {
        'source': { type: 'select', options: ['none', 'dashboard', 'hat'] },
        'port': { type: 'text' },
        'baudrate': { type: 'select', options: ['4800', '9600', '19200', '38400', '57600', '115200'] }
    },
    'Relays': { _isRelaySection: true }
};

const RESTART_SECTIONS = ['MQTT', 'Network', 'WebDashboard', 'Controller'];

function initConfigEditor() {
    document.getElementById('reloadConfig')?.addEventListener('click', loadConfig);
    document.getElementById('saveConfig')?.addEventListener('click', saveConfig);
    document.getElementById('resetDefault')?.addEventListener('click', resetToDefault);
    document.querySelector('[data-tab="config"]')?.addEventListener('click', () => {
        if (Object.keys(currentConfig).length === 0) loadConfig();
    });
}

async function loadConfig() {
    const container = document.getElementById('configSections');
    container.innerHTML = '<div class="config-loading"><div class="spinner"></div><span>Loading...</span></div>';
    try {
        const response = await fetch('/api/config');
        const data = await response.json();
        if (!data.success) throw new Error(data.error);
        originalConfig = JSON.parse(JSON.stringify(data.config));
        currentConfig = JSON.parse(JSON.stringify(data.config));
        activeConfigPath = data.active_config;
        isDefaultConfig = data.is_default;
        availableConfigs = data.available_configs || [];
        document.getElementById('configFilePath').textContent = data.config_name;
        updateActiveConfigBadge();
        renderConfigSections();
        renderConfigSelector();
        updateChangeIndicators();
    } catch (error) {
        container.innerHTML = '<p style="color:red">Error: ' + error.message + '</p>';
    }
}

async function saveConfig() {
    if (!configHasChanges) { showNotification('Info', 'No changes', 'info'); return; }
    const timestamp = new Date().toISOString().slice(0,19).replace(/[-:T]/g, '');
    const result = await showSaveConfigModal('config_' + timestamp + '.ini');
    if (!result) return;
    try {
        const response = await fetch('/api/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: currentConfig, filename: result.filename, set_active: result.setActive })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error);
        showNotification('Success', 'Saved as ' + data.filename, 'success');
        await loadConfig();
    } catch (error) { showNotification('Error', error.message, 'error'); }
}

async function resetToDefault() {
    if (!await showConfigConfirmModal('Reset', 'Reset to default config?')) return;
    try {
        const response = await fetch('/api/config/reset-default', { method: 'POST' });
        const data = await response.json();
        if (!data.success) throw new Error(data.error);
        showNotification('Success', 'Reset complete', 'success');
        loadConfig();
    } catch (error) { showNotification('Error', error.message, 'error'); }
}

async function switchConfig(configPath) {
    if (configHasChanges && !await showConfigConfirmModal('Unsaved', 'Discard changes?')) {
        document.getElementById('configSelector').value = activeConfigPath;
        return;
    }
    try {
        const response = await fetch('/api/config/set-active', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: configPath })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error);
        showNotification('Success', 'Switched config', 'success');
        loadConfig();
    } catch (error) { showNotification('Error', error.message, 'error'); }
}

function updateActiveConfigBadge() {
    const badge = document.getElementById('activeConfigBadge');
    if (badge) {
        badge.textContent = isDefaultConfig ? 'Default' : 'Custom';
        badge.className = 'config-badge ' + (isDefaultConfig ? 'default' : 'custom');
    }
}

function renderConfigSelector() {
    const selector = document.getElementById('configSelector');
    if (!selector) return;
    selector.innerHTML = availableConfigs.map(cfg => {
        const isActive = cfg.path === activeConfigPath;
        const label = cfg.is_default ? cfg.name + ' (default)' : cfg.name;
        return '<option value="' + cfg.path + '"' + (isActive ? ' selected' : '') + '>' + label + '</option>';
    }).join('');
    selector.onchange = (e) => { if (e.target.value !== activeConfigPath) switchConfig(e.target.value); };
}

function renderConfigSections() {
    const container = document.getElementById('configSections');
    container.innerHTML = '';
    const order = ['System', 'Camera', 'GreenOnBrown', 'GreenOnGreen', 'DataCollection', 'Controller', 'MQTT', 'GPS', 'Relays'];
    Object.keys(currentConfig).forEach(s => { if (!order.includes(s)) order.push(s); });
    order.forEach(section => { if (currentConfig[section]) container.appendChild(createSectionElement(section, currentConfig[section])); });
}

function createSectionElement(sectionName, sectionData) {
    const section = document.createElement('div');
    section.className = 'config-section';
    const fieldDefs = CONFIG_FIELD_DEFS[sectionName] || {};
    const hasWarning = RESTART_SECTIONS.includes(sectionName);
    const header = document.createElement('div');
    header.className = 'config-section-header';
    header.innerHTML = '<h3>' + sectionName + (hasWarning ? ' <span class="section-badge warning">Restart</span>' : '') + '</h3>';
    header.addEventListener('click', () => section.classList.toggle('collapsed'));
    const body = document.createElement('div');
    body.className = 'config-section-body';
    if (fieldDefs._isRelaySection) {
        body.innerHTML = '<div class="relay-mapping"></div>';
        const rc = body.querySelector('.relay-mapping');
        Object.entries(sectionData).forEach(([key, value]) => {
            const item = document.createElement('div');
            item.className = 'relay-item';
            item.innerHTML = '<label>Relay ' + key + ':</label><input type="number" data-section="' + sectionName + '" data-key="' + key + '" value="' + value + '" min="1" max="40">';
            rc.appendChild(item);
        });
    } else {
        Object.entries(sectionData).forEach(([key, value]) => body.appendChild(createFieldElement(sectionName, key, value, fieldDefs[key])));
    }
    section.appendChild(header);
    section.appendChild(body);
    section.querySelectorAll('input, select').forEach(input => {
        input.addEventListener('change', handleFieldChange);
        input.addEventListener('input', handleFieldChange);
    });
    return section;
}

function createFieldElement(section, key, value, fieldDef) {
    const field = document.createElement('div');
    field.className = 'config-field';
    const def = fieldDef || { type: 'text' };
    const id = 'config-' + section + '-' + key;
    const strValue = String(value);
    let html = '';
    if (def.type === 'boolean' || strValue.toLowerCase() === 'true' || strValue.toLowerCase() === 'false') {
        html = '<div class="checkbox-wrapper"><input type="checkbox" id="' + id + '" data-section="' + section + '" data-key="' + key + '"' + (strValue.toLowerCase() === 'true' ? ' checked' : '') + '><label for="' + id + '">' + formatLabel(key) + '</label></div>';
    } else if (def.type === 'select' && def.options) {
        const opts = def.options.map(o => '<option value="' + o + '"' + (String(o) === strValue ? ' selected' : '') + '>' + o + '</option>').join('');
        html = '<label for="' + id + '">' + formatLabel(key) + '</label><select id="' + id + '" data-section="' + section + '" data-key="' + key + '">' + opts + '</select>';
    } else if (def.type === 'number') {
        const attrs = (def.min !== undefined ? ' min="' + def.min + '"' : '') + (def.max !== undefined ? ' max="' + def.max + '"' : '') + (def.step !== undefined ? ' step="' + def.step + '"' : '');
        html = '<label for="' + id + '">' + formatLabel(key) + '</label><input type="number" id="' + id + '" data-section="' + section + '" data-key="' + key + '" value="' + value + '"' + attrs + '>';
    } else {
        html = '<label for="' + id + '">' + formatLabel(key) + '</label><input type="text" id="' + id + '" data-section="' + section + '" data-key="' + key + '" value="' + value + '">';
    }
    field.innerHTML = html;
    if (def.help) { const h = document.createElement('span'); h.className = 'field-help'; h.textContent = def.help; field.appendChild(h); }
    return field;
}

function handleFieldChange(event) {
    const input = event.target;
    const section = input.dataset.section;
    const key = input.dataset.key;
    const value = input.type === 'checkbox' ? (input.checked ? 'True' : 'False') : input.value;
    if (!currentConfig[section]) currentConfig[section] = {};
    currentConfig[section][key] = value;
    input.classList.toggle('modified', String(originalConfig[section]?.[key]) !== String(value));
    updateChangeIndicators();
}

function updateChangeIndicators() {
    const hasChanges = JSON.stringify(currentConfig) !== JSON.stringify(originalConfig);
    configHasChanges = hasChanges;
    document.getElementById('configUnsaved')?.classList.toggle('hidden', !hasChanges);
    let restart = false;
    RESTART_SECTIONS.forEach(s => { if (JSON.stringify(currentConfig[s]) !== JSON.stringify(originalConfig[s])) restart = true; });
    document.getElementById('configWarning')?.classList.toggle('hidden', !restart);
}

function showSaveConfigModal(suggestedName) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'config-modal-overlay';
        overlay.innerHTML = '<div class="config-modal"><h3>Save Configuration</h3><div class="config-modal-form"><div class="form-group"><label>Filename:</label><input type="text" id="saveConfigName" value="' + suggestedName + '" class="form-input"></div><div class="form-group checkbox-group"><label><input type="checkbox" id="setActiveOnSave" checked> Set as active</label></div></div><div class="config-modal-actions"><button class="btn-secondary" id="modalCancel">Cancel</button><button class="btn-success" id="modalSave">Save</button></div></div>';
        document.body.appendChild(overlay);
        const input = overlay.querySelector('#saveConfigName');
        input.focus(); input.select();
        overlay.querySelector('#modalCancel').onclick = () => { document.body.removeChild(overlay); resolve(null); };
        overlay.querySelector('#modalSave').onclick = () => { document.body.removeChild(overlay); resolve({ filename: input.value.trim(), setActive: overlay.querySelector('#setActiveOnSave').checked }); };
        overlay.onclick = (e) => { if (e.target === overlay) { document.body.removeChild(overlay); resolve(null); } };
        input.onkeypress = (e) => { if (e.key === 'Enter') overlay.querySelector('#modalSave').click(); };
    });
}

function showConfigConfirmModal(title, message) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'config-modal-overlay';
        overlay.innerHTML = '<div class="config-modal"><h3>' + title + '</h3><p>' + message + '</p><div class="config-modal-actions"><button class="btn-secondary" id="modalCancel">Cancel</button><button class="btn-primary" id="modalConfirm">Confirm</button></div></div>';
        document.body.appendChild(overlay);
        overlay.querySelector('#modalCancel').onclick = () => { document.body.removeChild(overlay); resolve(false); };
        overlay.querySelector('#modalConfirm').onclick = () => { document.body.removeChild(overlay); resolve(true); };
        overlay.onclick = (e) => { if (e.target === overlay) { document.body.removeChild(overlay); resolve(false); } };
    });
}
