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

    document.getElementById('config-library-selector')?.addEventListener('change', updateLibraryCaption);

    loadConfigLibrary();
}

/**
 * Show the highlighted library config's name, notes and save date under the selector.
 */
function updateLibraryCaption() {
    const sel = document.getElementById('config-library-selector');
    const cap = document.getElementById('config-library-caption');
    if (!sel || !cap) return;
    const cfg = configLibraryList.find(c => c.name === sel.value);
    if (!cfg || cfg.is_default || !sel.value || sel.value === '__reset_defaults__') {
        cap.textContent = '';
        return;
    }
    const parts = [];
    if (cfg.display_name) parts.push(cfg.display_name);
    if (cfg.created) parts.push('saved ' + cfg.created.slice(0, 10));
    if (cfg.notes) parts.push('— ' + cfg.notes);
    cap.textContent = parts.join('  ');
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
        // Show the friendly [Meta] name for the active file if we have it.
        var pretty = prettyConfigName(configName);
        badge.textContent = '★ On reboot: ' + pretty;
    }
}

/**
 * Map a config filename to its friendly [Meta] display name (falls back to a
 * formatted timestamp or the raw name).
 */
function prettyConfigName(name) {
    if (!name) return '--';
    var cfg = configLibraryList.find(function(c) { return c.name === name; });
    if (cfg && cfg.display_name) return cfg.display_name;
    return formatConfigLabel(name);
}

/**
 * Format a config filename into a readable label: prefer [Meta] display_name,
 * else strip a trailing _YYYYMMDD_HHMMSS timestamp into a date.
 */
function escapeConfigLabel(s) {
    return String(s).replace(/[&<>"]/g, function(c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
}

function formatConfigLabel(name, displayName) {
    if (displayName) {
        var safe = escapeConfigLabel(displayName);
        var m = name.match(/_(\d{4})(\d{2})(\d{2})_\d{6}\.ini$/);
        if (m) return safe + ' (' + m[3] + '/' + m[2] + ')';
        return safe;
    }
    var match = name.match(/^(.*)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})\.ini$/);
    if (match) {
        var base = match[1].replace(/^config$/, '').replace(/[-_]+/g, ' ').trim();
        var date = match[4] + '/' + match[3];
        return (base ? base + ' ' : '') + '(' + date + ')';
    }
    return name.replace(/\.ini$/, '');
}

/**
 * Set the selected library config as the active boot config on all OWLs.
 */
async function setDefaultConfig() {
    const sel = document.getElementById('config-library-selector');
    const configName = sel ? sel.value : '';
    if (!configName || configName === '__reset_defaults__') {
        showToast('Select a config first', 'warning');
        return;
    }
    try {
        const res = await apiRequest('/api/config/all/set-active', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: 'config/' + configName })
        });
        const data = await res.json();
        if (data && data.success !== false) {
            updateActiveBadge(configName);
            showToast('Set as default on reboot', 'success');
        } else {
            showToast('Failed: ' + (data.error || 'unknown'), 'error');
        }
    } catch (err) {
        showToast('Error setting default: ' + err.message, 'error');
    }
}

/**
 * Show/hide the restart-required notice based on any OWL reporting restart_required
 * in its state (set when resolution/relay_num change live).
 */
function updateRestartNotice() {
    const notice = document.getElementById('config-restart-notice');
    const textEl = document.getElementById('config-restart-text');
    if (!notice) return;

    const keys = new Set();
    for (const id in owlsData) {
        const rr = owlsData[id] && owlsData[id].restart_required;
        if (rr) String(rr).split(',').forEach(k => { if (k) keys.add(k.trim()); });
    }

    if (keys.size > 0) {
        notice.classList.remove('hidden');
        if (textEl) textEl.textContent = 'Restart needed to apply: ' + Array.from(keys).join(', ');
    } else {
        notice.classList.add('hidden');
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

    var html = '<option value="">Library&hellip;</option>';
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
            var label = formatConfigLabel(c.name, c.display_name);
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

