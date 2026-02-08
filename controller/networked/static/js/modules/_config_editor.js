/* ==========================================================================
   OWL Central Controller - Full Config Editor
   Uses shared CONFIG_FIELD_DEFS, createConfigSection, createConfigField
   from /shared/js/config.js

   Networked-specific logic: load from device via MQTT, push changes,
   config library (Save As + load + delete), active config management.

   Mirrors standalone _config.js patterns: active_config.txt pointer,
   timestamp filenames, protected defaults, set-active checkbox.
   ========================================================================== */

let deviceConfig = {};
let originalDeviceConfig = {};
let configEditorDevice = null;
let configEditorHasChanges = false;
let configLibraryList = [];

/**
 * Get the currently selected target device from the config editor device selector
 */
function getConfigEditorTarget() {
    const sel = document.getElementById('config-editor-device');
    return sel ? sel.value : null;
}

/**
 * Initialize the config editor page
 */
function initConfigEditor() {
    document.getElementById('load-device-config-btn')?.addEventListener('click', () => {
        const deviceId = getConfigEditorTarget();
        if (deviceId) loadDeviceConfig(deviceId);
    });

    document.getElementById('push-config-btn')?.addEventListener('click', pushConfigChanges);
    document.getElementById('save-as-config-btn')?.addEventListener('click', openSaveAsModal);
    document.getElementById('load-library-config-btn')?.addEventListener('click', loadLibraryConfigIntoEditor);
    document.getElementById('delete-library-config-btn')?.addEventListener('click', deleteLibraryConfig);

    loadConfigLibrary();
}

/**
 * Update the config editor device selector with current connected OWLs
 */
function updateConfigEditorDevices() {
    const sel = document.getElementById('config-editor-device');
    if (!sel) return;

    const currentValue = sel.value;
    let html = '';

    for (const id of Object.keys(owlsData)) {
        if (owlsData[id] && owlsData[id].connected === true) {
            html += '<option value="' + id + '">' + id + '</option>';
        }
    }

    if (!html) {
        html = '<option value="" disabled>No OWLs connected</option>';
    }

    sel.innerHTML = html;

    if ([...sel.options].some(o => o.value === currentValue)) {
        sel.value = currentValue;
    }
}

// ============================================
// LOAD DEVICE CONFIG
// ============================================

async function loadDeviceConfig(deviceId) {
    const container = document.getElementById('config-editor-sections');
    if (!container) return;

    container.innerHTML = '<div class="config-loading"><div class="spinner"></div><span>Loading config from ' + deviceId + '...</span></div>';
    configEditorDevice = deviceId;

    try {
        const res = await apiRequest('/api/config/' + deviceId, {}, 5000);
        const data = await res.json();

        if (!data.success) throw new Error(data.error || 'Failed to load config');

        originalDeviceConfig = JSON.parse(JSON.stringify(data.config));
        deviceConfig = JSON.parse(JSON.stringify(data.config));
        configEditorHasChanges = false;

        updateActiveBadge(data.config_name || 'Unknown');
        renderDeviceConfigSections();
        updateConfigEditorChangeState();
        showToast('Config loaded from ' + deviceId, 'success');

    } catch (err) {
        container.innerHTML = '<div class="config-loading"><span style="color:#e74c3c">Error: ' + err.message + '</span></div>';
        showToast('Failed to load config: ' + err.message, 'error');
    }
}

function renderDeviceConfigSections() {
    const container = document.getElementById('config-editor-sections');
    if (!container) return;

    container.innerHTML = '';
    const order = getOrderedSections(deviceConfig);

    order.forEach(section => {
        if (deviceConfig[section]) {
            container.appendChild(createConfigSection(section, deviceConfig[section], handleConfigEditorFieldChange));
        }
    });
}

function handleConfigEditorFieldChange(event) {
    const input = event.target;
    const section = input.dataset.section;
    const key = input.dataset.key;
    const value = input.type === 'checkbox' ? (input.checked ? 'True' : 'False') : input.value;

    if (!deviceConfig[section]) deviceConfig[section] = {};
    deviceConfig[section][key] = value;

    input.classList.toggle('modified', String(originalDeviceConfig[section]?.[key]) !== String(value));
    updateConfigEditorChangeState();
}

function updateConfigEditorChangeState() {
    configEditorHasChanges = JSON.stringify(deviceConfig) !== JSON.stringify(originalDeviceConfig);

    const unsavedEl = document.getElementById('config-editor-unsaved');
    if (unsavedEl) unsavedEl.classList.toggle('hidden', !configEditorHasChanges);

    const pushBtn = document.getElementById('push-config-btn');
    if (pushBtn) pushBtn.disabled = !configEditorHasChanges;
}

function updateActiveBadge(configName) {
    const badge = document.getElementById('config-active-badge');
    if (badge) {
        badge.textContent = 'Active: ' + configName;
    }
}

// ============================================
// PUSH TO DEVICE
// ============================================

async function pushConfigChanges() {
    if (!configEditorDevice || !configEditorHasChanges) return;

    const deviceId = configEditorDevice;
    let sectionsPushed = 0;

    try {
        for (const section of Object.keys(deviceConfig)) {
            if (JSON.stringify(deviceConfig[section]) !== JSON.stringify(originalDeviceConfig[section])) {
                await apiRequest('/api/config/' + deviceId, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ section: section, params: deviceConfig[section] })
                });
                sectionsPushed++;
            }
        }

        originalDeviceConfig = JSON.parse(JSON.stringify(deviceConfig));
        configEditorHasChanges = false;
        updateConfigEditorChangeState();

        document.querySelectorAll('#config-editor-sections .modified').forEach(el => {
            el.classList.remove('modified');
        });

        showToast('Pushed ' + sectionsPushed + ' section(s) to ' + deviceId, 'success');

    } catch (err) {
        showToast('Error pushing config: ' + err.message, 'error');
    }
}

// ============================================
// CONFIG LIBRARY
// ============================================

async function loadConfigLibrary() {
    try {
        const res = await apiRequest('/api/config/library');
        const data = await res.json();

        if (data.success) {
            configLibraryList = data.configs || [];
            renderConfigLibrarySelector();
        }
    } catch (err) {
        console.error('Failed to load config library:', err);
    }
}

function renderConfigLibrarySelector() {
    const sel = document.getElementById('config-library-selector');
    if (!sel) return;

    sel.innerHTML = '<option value="">Config library...</option>' +
        configLibraryList.map(c =>
            '<option value="' + c.name + '">' + c.name +
            (c.is_default ? ' (default)' : '') + '</option>'
        ).join('');
}

async function loadLibraryConfigIntoEditor() {
    const sel = document.getElementById('config-library-selector');
    const configName = sel ? sel.value : '';
    if (!configName) {
        showToast('Select a config first', 'warning');
        return;
    }

    try {
        const res = await apiRequest('/api/presets/' + configName);
        const data = await res.json();

        if (!data.success) throw new Error(data.error || 'Failed to load config');

        deviceConfig = JSON.parse(JSON.stringify(data.config));
        configEditorHasChanges = JSON.stringify(deviceConfig) !== JSON.stringify(originalDeviceConfig);

        renderDeviceConfigSections();
        updateConfigEditorChangeState();
        showToast('Loaded: ' + configName, 'success');

    } catch (err) {
        showToast('Error loading config: ' + err.message, 'error');
    }
}

async function deleteLibraryConfig() {
    const sel = document.getElementById('config-library-selector');
    const configName = sel ? sel.value : '';
    if (!configName) {
        showToast('Select a config first', 'warning');
        return;
    }

    // Check if it's a protected default
    var cfg = configLibraryList.find(function(c) { return c.name === configName; });
    if (cfg && cfg.is_default) {
        showToast('Cannot delete default configs', 'error');
        return;
    }

    try {
        const res = await apiRequest('/api/config/library/' + configName, { method: 'DELETE' });
        const data = await res.json();

        if (data.success) {
            showToast('Deleted: ' + configName, 'success');
            await loadConfigLibrary();
        } else {
            showToast('Delete failed: ' + (data.error || 'Unknown'), 'error');
        }
    } catch (err) {
        showToast('Error deleting config: ' + err.message, 'error');
    }
}

// ============================================
// SAVE AS MODAL (mirrors standalone showSaveConfigModal)
// ============================================

function openSaveAsModal() {
    if (Object.keys(deviceConfig).length === 0) {
        showToast('Load a config first', 'warning');
        return;
    }

    // Pre-fill with timestamp filename (same pattern as standalone)
    var ts = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '');
    var input = document.getElementById('save-as-filename');
    if (input) {
        input.value = 'config_' + ts + '.ini';
    }

    document.getElementById('save-as-modal').style.display = 'flex';

    // Focus and select filename
    if (input) {
        input.focus();
        input.select();
    }
}

function closeSaveAsModal() {
    document.getElementById('save-as-modal').style.display = 'none';
}

async function confirmSaveAs() {
    var filenameInput = document.getElementById('save-as-filename');
    var filename = filenameInput ? filenameInput.value.trim() : '';
    if (!filename) {
        showToast('Enter a filename', 'warning');
        return;
    }

    if (!filename.endsWith('.ini')) {
        filename += '.ini';
    }

    var setActive = document.getElementById('save-as-set-active')?.checked || false;
    var deviceId = configEditorDevice;

    closeSaveAsModal();

    try {
        // Step 1: Save locally to controller's config library
        var libRes = await apiRequest('/api/config/library', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: deviceConfig, filename: filename })
        });
        var libData = await libRes.json();

        if (!libData.success) {
            showToast('Save failed: ' + (libData.error || 'Unknown'), 'error');
            return;
        }

        // Step 2: Save on OWL device via MQTT (if device is selected)
        if (deviceId) {
            await apiRequest('/api/config/' + deviceId + '/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename: filename })
            });

            // Step 3: Optionally set as active boot config on device
            if (setActive) {
                await apiRequest('/api/config/' + deviceId + '/set-active', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: 'config/' + filename })
                });
            }
        }

        showToast('Saved as ' + filename + (setActive ? ' (set as active)' : ''), 'success');

        // Refresh the library selector
        await loadConfigLibrary();

        // Update badge if set active
        if (setActive) {
            updateActiveBadge(filename);
        }

    } catch (err) {
        showToast('Error saving config: ' + err.message, 'error');
    }
}
