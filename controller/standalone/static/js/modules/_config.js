/* ==========================================================================
   OWL Dashboard - Config Editor Module (Standalone)
   Uses shared CONFIG_FIELD_DEFS, createConfigSection, createConfigField
   from /shared/js/config.js
   ========================================================================== */

let originalConfig = {};
let currentConfig = {};
let configHasChanges = false;
let activeConfigPath = '';
let isDefaultConfig = true;
let availableConfigs = [];

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
        // Clear slider cooldown so stats polling can push new values immediately
        lastSliderSendTime = 0;
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
    const order = getOrderedSections(currentConfig);
    order.forEach(section => {
        if (currentConfig[section]) {
            container.appendChild(createConfigSection(section, currentConfig[section], handleFieldChange));
        }
    });
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
