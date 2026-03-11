/* ==========================================================================
   OWL Central Controller - Full Config Editor
   Uses shared CONFIG_FIELD_DEFS, createConfigSection, createConfigField
   from /shared/js/config.js

   Networked-specific logic: load from device via MQTT, config library
   management, active config badge, device selector.
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

    // Update target badge with connected count
    var connectedCount = Object.keys(owlsData).filter(id =>
        owlsData[id] && owlsData[id].connected === true
    ).length;
    var badgeEl = document.getElementById('config-target-badge');
    if (badgeEl) {
        badgeEl.textContent = connectedCount > 0
            ? 'All OWLs (' + connectedCount + ' connected)'
            : 'All OWLs';
    }

    // Populate preview device selector
    var previewSel = document.getElementById('config-preview-device');
    if (previewSel) {
        var prevValue = previewSel.value;
        var previewHtml = '';

        for (const id of Object.keys(owlsData)) {
            if (owlsData[id] && owlsData[id].connected === true) {
                previewHtml += '<option value="' + id + '">' + id + '</option>';
            }
        }

        if (!previewHtml) {
            previewHtml = '<option value="" disabled>No OWLs connected</option>';
        }

        previewSel.innerHTML = previewHtml;

        // Restore previous selection if still valid
        if ([...previewSel.options].some(o => o.value === prevValue)) {
            previewSel.value = prevValue;
        }
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
}

function updateActiveBadge(configName) {
    const badge = document.getElementById('config-active-badge');
    if (badge) {
        badge.textContent = 'Active: ' + configName;
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

    var defaults = configLibraryList.filter(c => c.is_default);
    var custom = configLibraryList.filter(c => !c.is_default);

    var html = '<option value="">Presets...</option>';
    html += '<option value="__reset_defaults__">Reset to Defaults</option>';

    if (defaults.length > 0) {
        html += '<optgroup label="Defaults">';
        defaults.forEach(c => {
            html += '<option value="' + c.name + '">' + c.name + '</option>';
        });
        html += '</optgroup>';
    }

    if (custom.length > 0) {
        html += '<optgroup label="Custom">';
        custom.forEach(c => {
            // Format timestamp filenames for readability
            var label = c.name;
            var match = label.match(/^config_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.ini$/);
            if (match) {
                label = match[1] + '-' + match[2] + '-' + match[3] + ' ' +
                        match[4] + ':' + match[5] + ':' + match[6];
            }
            html += '<option value="' + c.name + '">' + label + '</option>';
        });
        html += '</optgroup>';
    }

    sel.innerHTML = html;
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

