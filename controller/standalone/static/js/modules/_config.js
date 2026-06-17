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
    const result = await showSaveConfigModal();
    if (!result) return;
    try {
        const response = await fetch('/api/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: currentConfig, name: result.name, notes: result.notes,
                                   set_active: result.setActive, overwrite_filename: result.overwrite })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error);
        showNotification('Success', 'Saved as ' + (result.name || data.filename), 'success');
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

function configLabel(cfg) {
    if (cfg.is_default) return cfg.name + ' (default)';
    if (cfg.display_name) {
        const m = cfg.name.match(/_(\d{4})(\d{2})(\d{2})_\d{6}\.ini$/);
        return m ? cfg.display_name + ' (' + m[3] + '/' + m[2] + ')' : cfg.display_name;
    }
    return cfg.name;
}

function renderConfigSelector() {
    const selector = document.getElementById('configSelector');
    if (!selector) return;
    selector.innerHTML = availableConfigs.map(cfg => {
        const isActive = cfg.path === activeConfigPath;
        const label = configLabel(cfg).replace(/</g, '&lt;');
        return '<option value="' + cfg.path + '"' + (isActive ? ' selected' : '') + '>' + label + '</option>';
    }).join('');
    selector.onchange = (e) => {
        updateConfigCaption();
        if (e.target.value !== activeConfigPath) switchConfig(e.target.value);
    };
    updateConfigCaption();
}

function updateConfigCaption() {
    const selector = document.getElementById('configSelector');
    const cap = document.getElementById('configCaption');
    if (!selector || !cap) return;
    const cfg = availableConfigs.find(c => c.path === selector.value);
    if (!cfg || cfg.is_default) { cap.textContent = ''; return; }
    const parts = [];
    if (cfg.display_name) parts.push(cfg.display_name);
    if (cfg.created) parts.push('saved ' + cfg.created.slice(0, 10));
    if (cfg.notes) parts.push('— ' + cfg.notes);
    cap.textContent = parts.join('  ');
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

function showSaveConfigModal() {
    return new Promise(resolve => {
        // Offer "Update <name>" when the active config is a non-default custom file.
        const activeCfg = availableConfigs.find(c => c.path === activeConfigPath);
        const canUpdate = activeCfg && !activeCfg.is_default;
        const updName = canUpdate ? (activeCfg.display_name || activeCfg.name) : '';
        const modeHtml = canUpdate
            ? '<div class="form-group">' +
              '<label class="radio-line"><input type="radio" name="saveMode" value="update" id="saveModeUpdate" checked> Update &ldquo;' +
              updName.replace(/</g, '&lt;') + '&rdquo;</label>' +
              '<label class="radio-line"><input type="radio" name="saveMode" value="new" id="saveModeNew"> Save as new</label>' +
              '</div>'
            : '';
        const overlay = document.createElement('div');
        overlay.className = 'config-modal-overlay';
        overlay.innerHTML =
            '<div class="config-modal"><h3>Save configuration</h3>' +
            '<div class="config-modal-form">' +
            modeHtml +
            '<div class="form-group"><label>Name this setup</label>' +
            '<input type="text" id="saveConfigName" data-numpad class="form-input" placeholder="e.g. High sensitivity - wheat" autocomplete="off"></div>' +
            '<div class="form-group"><label>Notes (optional)</label>' +
            '<input type="text" id="saveConfigNotes" data-numpad class="form-input" placeholder="e.g. dewy mornings" autocomplete="off"></div>' +
            '<div class="form-group checkbox-group"><label><input type="checkbox" id="setActiveOnSave" checked> Make active on reboot</label></div>' +
            '<p class="config-modal-hint">A date is added automatically so saves never overwrite each other.</p>' +
            '</div>' +
            '<div class="config-modal-actions"><button class="btn-secondary" id="modalCancel">Cancel</button><button class="btn-success" id="modalSave">Save</button></div></div>';
        document.body.appendChild(overlay);
        const nameInput = overlay.querySelector('#saveConfigName');
        const notesInput = overlay.querySelector('#saveConfigNotes');
        if (canUpdate) {
            nameInput.value = activeCfg.display_name || '';
            notesInput.value = activeCfg.notes || '';
        }
        nameInput.focus();
        const done = (val) => { document.body.removeChild(overlay); resolve(val); };
        overlay.querySelector('#modalCancel').onclick = () => done(null);
        overlay.querySelector('#modalSave').onclick = () => {
            const isUpdate = canUpdate && overlay.querySelector('#saveModeUpdate').checked;
            done({
                name: nameInput.value.trim(),
                notes: notesInput.value.trim(),
                setActive: overlay.querySelector('#setActiveOnSave').checked,
                overwrite: isUpdate ? activeCfg.name : null
            });
        };
        overlay.onclick = (e) => { if (e.target === overlay) done(null); };
        nameInput.onkeypress = (e) => { if (e.key === 'Enter') overlay.querySelector('#modalSave').click(); };
    });
}

// ============================================
// VISUAL GEOMETRY EDITOR (uses shared GeometryEditor)
// ============================================

let standaloneGeometryActive = false;

function geoFracStandalone(primary, fallback, dflt) {
    var n = parseFloat(primary);
    if (!isNaN(n)) return n;
    n = parseFloat(fallback);
    if (!isNaN(n)) return n;
    return dflt;
}

async function toggleStandaloneGeometry() {
    if (typeof GeometryEditor === 'undefined') return;

    if (standaloneGeometryActive) {
        GeometryEditor.close();
        return;
    }

    // Make sure the feed is visible behind the overlay.
    document.getElementById('previewContainer')?.classList.remove('hidden');
    var img = document.getElementById('stream-img');
    if (img && !img.getAttribute('src')) img.setAttribute('src', '/video_feed');

    var values = {
        crop_left: 0.02, crop_right: 0.02, crop_top: 0.02, crop_bottom: 0.02,
        actuation_top: 0.0, actuation_bottom: 1.0
    };
    var relayNum = 4;
    try {
        var res = await fetch('/api/config');
        var data = await res.json();
        if (data.success && data.config) {
            var cam = data.config.Camera || {};
            var sys = data.config.System || {};
            values = {
                crop_left: geoFracStandalone(cam.crop_left, cam.crop_factor_horizontal, 0.02),
                crop_right: geoFracStandalone(cam.crop_right, cam.crop_factor_horizontal, 0.02),
                crop_top: geoFracStandalone(cam.crop_top, cam.crop_factor_vertical, 0.02),
                crop_bottom: geoFracStandalone(cam.crop_bottom, cam.crop_factor_vertical, 0.02),
                actuation_top: geoFracStandalone(sys.actuation_top, null, 0.0),
                actuation_bottom: geoFracStandalone(sys.actuation_bottom, null, 1.0)
            };
            relayNum = parseInt(sys.relay_num, 10) || 4;
        }
    } catch (e) {
        showNotification('Info', 'Using defaults — could not read geometry', 'info');
    }

    var btn = document.getElementById('geometryBtn');

    function setPreviewMode(mode) {
        fetch('/api/preview-mode', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode })
        });
    }

    // Show the full uncropped frame behind the overlay while editing.
    setPreviewMode('full');

    GeometryEditor.open({
        host: document.getElementById('stream-frame'),
        img: img,
        panel: document.getElementById('standalone-geometry-panel'),
        values: values,
        relayNum: relayNum,
        allowAll: false,
        onCommit: function (v, info) {
            fetch('/api/geometry', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    params: {
                        crop_left: String(v.crop_left), crop_right: String(v.crop_right),
                        crop_top: String(v.crop_top), crop_bottom: String(v.crop_bottom),
                        actuation_top: String(v.actuation_top), actuation_bottom: String(v.actuation_bottom)
                    },
                    persist: info && info.persist
                })
            });
            if (info && info.persist) showNotification('Success', 'Geometry saved', 'success');
        },
        onClose: function () {
            standaloneGeometryActive = false;
            setPreviewMode('cropped');
            if (btn) { btn.textContent = 'Adjust geometry'; btn.classList.remove('active'); }
        }
    });

    standaloneGeometryActive = true;
    if (btn) { btn.textContent = 'Close geometry'; btn.classList.add('active'); }
    showNotification('Tip', 'Drag the edges and band on the feed. Done saves; Cancel reverts.', 'info');
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
