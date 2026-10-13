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
    // Shared config.js renders the "Calibrate white balance" row in the
    // Camera section and calls this hook; progress comes back via
    // /api/system_stats (awb_calibration) -> updateAwbReadout in _stats.js.
    window.OWL_AWB_CALIBRATE = calibrateWhiteBalance;
    document.getElementById('reloadConfig')?.addEventListener('click', loadConfig);
    document.getElementById('saveConfig')?.addEventListener('click', saveConfig);
    document.getElementById('resetDefault')?.addEventListener('click', resetToDefault);
    document.getElementById('resetNetworkSetup')?.addEventListener('click', resetNetworkSetup);
    // Profiles and Advanced load the file when first opened (_shell.js)
}

async function calibrateWhiteBalance() {
    try {
        const res = await apiRequest('/api/camera/calibrate-awb', { method: 'POST' });
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'Calibration did not start');
        showNotification('White balance', data.message || 'Calibrating white balance', 'info', 4000);
    } catch (err) {
        showNotification('White balance', 'Could not start calibration: ' + err.message, 'error');
        if (typeof updateAwbReadout === 'function') updateAwbReadout(null, null);
    }
}

function onAwbCalibrationComplete() {
    // The OWL persisted the locked gains to the active config; reload the
    // editor from disk unless the user has unsaved edits in progress.
    if (!configHasChanges && Object.keys(currentConfig).length > 0) loadConfig();
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
        // The autosave working file displays as its source preset; the unsaved
        // marker appears only when its content actually differs from that source
        document.getElementById('configFilePath').textContent =
            (data.config_source || data.config_name || '')
            + (data.config_unsaved ? ' - unsaved changes' : '');
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

/**
 * Fold live slider values into currentConfig before a save. Sliders apply
 * straight to the OWL via /api/config/param and never touch currentConfig,
 * so a save serialized from currentConfig would otherwise write the values
 * from when the page last loaded. Returns true if anything changed.
 */
function syncCurrentConfigFromSliders() {
    if (typeof configParams === 'undefined' || !Object.keys(currentConfig).length) return false;
    let changed = false;

    currentConfig.GreenOnBrown = currentConfig.GreenOnBrown || {};
    const gob = currentConfig.GreenOnBrown;
    const gobKeys = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                     'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                     'min_detection_area_percent'];
    gobKeys.forEach(k => {
        if (configParams[k] && String(gob[k]) !== String(configParams[k].value)) {
            gob[k] = String(configParams[k].value);
            changed = true;
        }
    });
    // Optional params: only fold when the config already carries the key —
    // never invent keys this device's config doesn't have.
    ['lut_sensitivity'].forEach(k => {
        if (k in gob && configParams[k] && String(gob[k]) !== String(configParams[k].value)) {
            gob[k] = String(configParams[k].value);
            changed = true;
        }
    });

    if (currentConfig.GreenOnGreen) {
        const gog = currentConfig.GreenOnGreen;
        if (configParams.confidence && String(gog.confidence) !== String(configParams.confidence.value / 100)) {
            gog.confidence = String(configParams.confidence.value / 100);
            changed = true;
        }
        if (configParams.crop_buffer_px && String(gog.crop_buffer_px) !== String(configParams.crop_buffer_px.value)) {
            gog.crop_buffer_px = String(configParams.crop_buffer_px.value);
            changed = true;
        }
    }
    return changed;
}

async function saveConfig() {
    // Slider moves count as saveable changes even though they bypass the editor.
    const sliderChanges = syncCurrentConfigFromSliders();
    if (!configHasChanges && !sliderChanges) { showNotification('Info', 'No changes', 'info'); return; }
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

async function resetNetworkSetup() {
    if (!await showConfigConfirmModal('Reset network setup',
            'The OWL will re-enter first-boot setup mode on its next reboot. ' +
            'Its hotspot password resets to the setup default. Continue?')) return;
    try {
        const response = await fetch('/api/network/reset-setup', { method: 'POST' });
        const data = await response.json();
        if (!data.success) throw new Error(data.error);
        showNotification('Success', data.message, 'success');
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
    if (cfg.notes) parts.push('- ' + cfg.notes);
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
    const fromCalibration = !!(event.detail && event.detail.fromCalibration);
    if (fromCalibration) {
        // The OWL applied and saved these itself: they are the new baseline,
        // not an unsaved edit, and must not be re-sent.
        if (!originalConfig[section]) originalConfig[section] = {};
        originalConfig[section][key] = value;
    }
    input.classList.toggle('modified', String(originalConfig[section]?.[key]) !== String(value));
    updateChangeIndicators();
    if (!fromCalibration && section === 'Camera' && KIOSK_CAMERA_LIVE_KEYS.includes(key) &&
            String(value).trim() !== '') {
        hotApplyCameraKey(key, value);
    }
}

// [Camera] keys the OWL applies to the running camera without a restart
// (mirrors CAMERA_LIVE_KEYS in utils/mqtt_manager.py). White balance and
// exposure are tuned against the live feed, so the kiosk editor pushes
// them on change like the phone app does; Save still handles the rest.
const KIOSK_CAMERA_LIVE_KEYS = ['awb_mode', 'awb_red_gain', 'awb_blue_gain', 'exp_compensation'];
let _cameraLiveTimer = null;
let _cameraLivePending = {};

function hotApplyCameraKey(key, value) {
    _cameraLivePending[key] = value;
    clearTimeout(_cameraLiveTimer);
    _cameraLiveTimer = setTimeout(async () => {
        const params = _cameraLivePending;
        _cameraLivePending = {};
        try {
            // Query string only distinguishes this from the geometry
            // editor's use of the same route: apiRequest aborts an in-flight
            // request to the same URL, and the two must not cancel each other.
            const res = await apiRequest('/api/config/section?source=camera-live', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ section: 'Camera', params })
            });
            const data = await res.json();
            if (!data.success) throw new Error(data.error || 'apply failed');
            // The route persists to the active config as well, so these
            // keys are no longer "unsaved" in the editor. A key the OWL
            // rejected stays marked and is reported.
            const rejected = data.rejected || {};
            if (!originalConfig.Camera) originalConfig.Camera = {};
            Object.entries(params).forEach(([k, v]) => {
                if (rejected[k]) return;
                originalConfig.Camera[k] = v;
                document.getElementById('config-Camera-' + k)?.classList.remove('modified');
            });
            updateChangeIndicators();
            Object.values(rejected).forEach(msg => showNotification('Camera', msg, 'error'));
        } catch (err) {
            if (err.name === 'AbortError') return; // superseded by a newer change
            showNotification('Camera', 'Live apply failed: ' + err.message, 'error');
        }
    }, 300);
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
        // No auto-focus: focusin opens the on-screen keyboard (data-numpad),
        // and it shouldn't pop up unrequested — the user taps the field to edit.
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

// Safety net: if the page is closed/reloaded while the geometry editor is open (so
// onClose never runs), revert the OWL preview to cropped via beacon (survives unload).
window.addEventListener('pagehide', function () {
    if (standaloneGeometryActive && navigator.sendBeacon) {
        navigator.sendBeacon('/api/preview-mode',
            new Blob([JSON.stringify({ mode: 'cropped' })], { type: 'application/json' }));
    }
});

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

    // The editor works on the Field live view: show it and keep it running.
    if (typeof Shell !== 'undefined') { Shell.showView('field'); Shell.setStream(true); }
    var img = document.getElementById('stream-img');
    if (img && !img.getAttribute('src')) img.setAttribute('src', '/video_feed');
    document.getElementById('view-field')?.classList.add('geo-on');

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
        showNotification('Info', 'Using defaults: could not read geometry', 'info');
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
            document.getElementById('view-field')?.classList.remove('geo-on');
            if (btn) btn.classList.remove('is-on');
        }
    });

    standaloneGeometryActive = true;
    if (btn) btn.classList.add('is-on');
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
