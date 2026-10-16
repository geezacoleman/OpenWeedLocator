// ============================================
// OWL Central Controller - main controls
// Spraying, recording, nozzles, tracking, detection mode, restart, power.
// Every control acts on the rail selection: currentScope() is 'all' or one
// OWL's id (_shell.js). Recording stays fleet-wide: one run, one run id,
// started and stopped through /api/collect/* like the Collect screen.
// ============================================

function scopeTarget() {
    return (typeof currentScope === 'function') ? currentScope() : 'all';
}

function scopeName() {
    const t = scopeTarget();
    if (t === 'all') return 'all OWLs';
    const owl = owlsData[t] || {};
    return owl.friendly_name || t;
}

// Connected OWLs the current selection covers
function scopedOwls() {
    const t = scopeTarget();
    return Object.keys(owlsData).filter((id) => owlsData[id] && owlsData[id].connected &&
        (t === 'all' || id === t)).map((id) => Object.assign({ _id: id }, owlsData[id]));
}

// Switches follow what the OWLs report, not only what was last pressed: an
// OWL that refused (no drive), faulted or rebooted otherwise still showed
// as running. Skipped briefly after a press while the OWLs catch up.
let _mainPressAt = 0;
function mainPressRecent() { return Date.now() - _mainPressAt < 3000; }

function toggleMainDetection() {
    _mainPressAt = Date.now();
    const target = scopeTarget();
    if (globalDetectionEnabled) {
        globalDetectionEnabled = false;
        sendCommand(target, 'toggle_detection', false);
        showToast('Spraying off on ' + scopeName(), 'info');
    } else {
        // Starting detection turns blanket spraying off first
        if (globalNozzlesActive) {
            globalNozzlesActive = false;
            sendCommand(target, 'toggle_all_nozzles', false);
        }
        globalDetectionEnabled = true;
        sendCommand(target, 'toggle_detection', true);
        showToast('Spraying on ' + scopeName(), 'success');
    }
    if (typeof Shell !== 'undefined') Shell.render();
}

// ============================================
// RECORDING: the shared "What are you recording?" form (collect_form.js),
// started with /api/collect/start like the Collect screen and standalone.
// ============================================

let _recordModal = null;
let _collectConfig = { presets: {}, capture: {}, last_pick: null };

function initRecordModal() {
    if (!window.CollectForm || !window.CollectI18n) return;
    const scope = document.createElement('div');
    scope.className = 'c-scope';
    document.body.appendChild(scope);
    _recordModal = CollectForm.mountRecordModal({
        parent: scope,
        presets: () => _collectConfig.presets,
        onStart: startCollectRecording,
        onPresets: (presets) => { _collectConfig.presets = presets; },
    });
    fetch('/api/collect/config').then((r) => r.json()).then((data) => {
        _collectConfig = data;
        CollectI18n.setLang(data.language || 'en');
        CollectI18n.apply(scope);
        _recordModal.setPick(data.last_pick);
        _recordModal.refresh();
        if (typeof Shell !== 'undefined') Shell.onCollectConfig(data);
    }).catch(() => CollectI18n.apply(scope));
}

function openRecordModal() {
    if (_recordModal) _recordModal.open();
    else startCollectRecording({});
}

function startCollectRecording(metadata) {
    _mainPressAt = Date.now();
    fetch('/api/collect/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ metadata: metadata }),
    }).then((r) => r.json().then((data) => ({ ok: r.ok, data: data }))).then(({ ok, data }) => {
        if (!ok || !data.success) throw new Error(data.error || 'Could not start recording');
        globalRecordingEnabled = true;
        _collectConfig.last_pick = metadata;
        showToast('Recording on all OWLs', 'success');
        setTimeout(updateDashboard, 400);
    }).catch((e) => showToast(e.message, 'error'));
}

async function toggleMainRecording() {
    if (globalRecordingEnabled) {
        const ok = await OwlUI.confirm({ title: 'Stop recording on all OWLs?', ok: 'Stop', okClass: 'stop', cancel: 'Keep recording' });
        if (!ok) return;
        _mainPressAt = Date.now();
        fetch('/api/collect/stop', { method: 'POST' }).then((r) => r.json()).then((data) => {
            if (!data.success) throw new Error(data.error || 'Could not stop recording');
            globalRecordingEnabled = false;
            showToast('Recording stopped', 'info');
            setTimeout(updateDashboard, 400);
        }).catch((e) => showToast(e.message, 'error'));
        return;
    }

    // Any connected OWL silently clamping its configured resolution (Pi 3/4
    // safety limit): offer the override + restart for those OWLs only.
    var clampedOwls = _findClampedOWLs();
    if (clampedOwls.length > 0 && typeof showHighResWarningModal === 'function') {
        var first = clampedOwls[0];
        showHighResWarningModal(
            first.rpi, first.requestedW, first.requestedH, first.actualW, first.actualH,
            function onCancel() { /* recording stays off */ },
            function onOverride() {
                var ids = clampedOwls.map(function(o) { return o.deviceId; });
                var pending = ids.map(function(id) {
                    return sendCommand(id, 'set_config_section', {
                        section: 'Camera', params: {allow_high_resolution: 'True'}
                    })
                        .then(function() { return sendCommand(id, 'save_config', {}); })
                        .then(function() { return restartOWL(id); });
                });
                Promise.all(pending).then(function() {
                    showToast('High-resolution override saved on ' + ids.join(', ') +
                        '. Restarting. Start recording when they are back online.', 'info');
                });
            }
        );
        return;
    }

    var lowResOwl = _findLowResolutionOWL();
    if (lowResOwl && typeof showResolutionWarningModal === 'function') {
        showResolutionWarningModal(lowResOwl.w, lowResOwl.h,
            function onAccept() {
                sendCommand('all', 'set_config_section', {
                    section: 'Camera',
                    params: {resolution_width: String(OWL_MAX_RES_WIDTH), resolution_height: String(OWL_MAX_RES_HEIGHT)}
                })
                    .then(function() { return sendCommand('all', 'save_config', {}); })
                    .then(function() {
                        for (var id in owlsData) {
                            if (owlsData[id] && owlsData[id].connected) restartOWL(id);
                        }
                        showToast('Resolution changed. Restarting all OWLs. Start recording when they are back online.', 'info');
                    });
            },
            function onContinue() { openRecordModal(); }
        );
        return;
    }

    openRecordModal();
}

/**
 * Find all connected OWLs whose configured resolution is being silently
 * clamped at startup (Pi 3/4 safety limit). Returns an array of
 * {rpi, requestedW, requestedH, actualW, actualH, deviceId}.
 */
function _findClampedOWLs() {
    if (typeof isClampActive !== 'function') return [];
    var result = [];
    for (var id in owlsData) {
        var owl = owlsData[id];
        if (!owl || !owl.connected) continue;
        if (isClampActive(owl)) {
            result.push({
                rpi: owl.rpi_version || 'unknown',
                requestedW: owl.requested_resolution_width || owl.resolution_width || 0,
                requestedH: owl.requested_resolution_height || owl.resolution_height || 0,
                actualW: owl.resolution_width || 0,
                actualH: owl.resolution_height || 0,
                deviceId: id
            });
        }
    }
    return result;
}

/**
 * Find the first connected OWL with resolution below maximum.
 * Returns {w, h} or null if all are at max (or resolution unknown).
 */
function _findLowResolutionOWL() {
    if (typeof isResolutionBelowMax !== 'function') return null;
    for (var id in owlsData) {
        var owl = owlsData[id];
        if (owl && owl.connected) {
            var w = owl.resolution_width || 0;
            var h = owl.resolution_height || 0;
            if (isResolutionBelowMax(w, h)) return {w: w, h: h};
        }
    }
    return null;
}

// ============================================
// ALL NOZZLES (blanket spray) AND HOLD TO SPRAY
// ============================================

function toggleAllNozzles() {
    _mainPressAt = Date.now();
    const target = scopeTarget();
    if (globalNozzlesActive) {
        globalNozzlesActive = false;
        sendCommand(target, 'toggle_all_nozzles', false);
        showToast('Blanket spray off', 'info');
    } else {
        globalNozzlesActive = true;
        globalDetectionEnabled = false;
        sendCommand(target, 'toggle_all_nozzles', true);
        showToast('Blanket spray on ' + scopeName() + ', detection off', 'warning');
    }
    if (typeof Shell !== 'undefined') Shell.render();
}

// ============================================
// TRACKING
// ============================================

function toggleTracking() {
    const target = scopeTarget();
    if (globalTrackingEnabled) {
        globalTrackingEnabled = false;
        sendCommand(target, 'set_tracking', false);
        showToast('Tracking off', 'info');
    } else {
        globalTrackingEnabled = true;
        sendCommand(target, 'set_tracking', true);
        showToast('Tracking on', 'success');
    }
    if (typeof Shell !== 'undefined') Shell.render();
}

// Tracking values (stability preset, or one key) on the selected OWLs.
// Applied live; Save as profile keeps them.
function setTrackParams(params, label) {
    sendCommand(scopeTarget(), 'set_config_section', { section: 'Tracking', params: params });
    if (label) showToast(label, 'info');
}

// ============================================
// OWL RESTART
// ============================================

async function restartOWL(deviceId) {
    try {
        const res = await fetch('/api/owl/' + deviceId + '/restart', { method: 'POST' });
        const data = await res.json();
        if (data.success) showToast('Restarting ' + deviceId, 'success');
        else showToast('Restart failed: ' + (data.error || 'unknown'), 'error');
    } catch (err) {
        showToast('Could not reach ' + deviceId, 'error');
    }
}

// ============================================
// DETECTION MODE (Detect with)
// ============================================

// Remember GoB algorithm when switching modes
let lastGoBAlgorithm = 'exhsv';
let pendingMode = null;
let pendingModeAt = 0;
// Active mode: the sensitivity control reads this to know which axis it drives
let currentPipelineMode = 'gob';

// Same contract as the standalone: /api/algorithm/set, 400 + a plain reason
// when an OWL can't run it. Blocked AI is caught before asking (_shell.js).
function setPipelineMode(mode) {
    var btn = document.querySelector('.mode-btn[data-mode="' + mode + '"]');
    if (!btn || btn.classList.contains('loading')) return;

    if (mode === 'lut') {
        // Painted mode activates via profile apply (a profile must exist);
        // with none saved, reveal the panel so Paint weeds is reachable.
        if (btn.classList.contains('no-profiles')) {
            showToast('No painted profiles yet. Use Paint weeds to create one', 'info');
            if (typeof Shell !== 'undefined') Shell.openSettings('colour');
        }
        if (typeof activateLutMode === 'function') activateLutMode();
        return;
    }
    if (typeof clearLutPanelForced === 'function') clearLutPanelForced();

    var algorithm = mode === 'gob' ? lastGoBAlgorithm : mode === 'gog' ? 'gog' : mode === 'hybrid' ? 'gog-hybrid' : null;
    if (!algorithm) return;

    pendingMode = mode;
    pendingModeAt = Date.now();
    btn.classList.add('loading');
    fetch('/api/algorithm/set', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ algorithm: algorithm, device_id: scopeTarget() }),
    }).then((r) => r.json().then((data) => ({ ok: r.ok, data: data }))).then(({ ok, data }) => {
        if (!ok || !data.success) throw new Error(data.error || 'The OWLs did not switch');
        setTimeout(updateDashboard, 400);
    }).catch((e) => {
        pendingMode = null;
        btn.classList.remove('loading');
        if (typeof Shell !== 'undefined') Shell.modeRefused(mode, e.message);
        else showToast(e.message, 'error');
    });
}

function updatePipelineModeUI(algorithm) {
    // Don't overwrite the buttons while a switch is loading (10 s at most)
    if (pendingMode && Date.now() - pendingModeAt < 10000) return;
    var mode;
    if (algorithm === 'gog') mode = 'gog';
    else if (algorithm === 'gog-hybrid') mode = 'hybrid';
    else if (algorithm === 'lut') mode = 'lut';   // never remember 'lut' as a GoB algorithm
    else {
        mode = 'gob';
        if (algorithm) lastGoBAlgorithm = algorithm;
    }

    // A locally-forced Painted panel (no profiles applied yet) keeps its
    // button lit even though the fleet still reports a GoB algorithm.
    if (mode === 'gob' && typeof isLutPanelForced === 'function' && isLutPanelForced()) mode = 'lut';

    // Pipeline buttons only, not the stability .mode-btn
    document.querySelectorAll('.mode-btn[data-mode]').forEach(function(btn) {
        btn.classList.remove('active', 'loading');
        if (btn.dataset.mode === mode) btn.classList.add('active');
    });

    pendingMode = null;
    currentPipelineMode = mode;
    if (typeof updateSensitivityModeCaption === 'function') updateSensitivityModeCaption();
    if (typeof updateSliderVisibility === 'function') updateSliderVisibility(mode === 'lut' ? 'lut' : algorithm);
}

// ============================================
// POWER: all OWLs and this controller
// ============================================

async function initiateShutdown() {
    const recording = Object.values(owlsData).some((o) => o && o.connected && o.image_sample_enable);
    const ok = await OwlUI.confirm({
        title: 'Turn off all OWLs and this screen?',
        body: (recording ? 'Recording stops first. Photos already saved are kept. ' : '') +
            'Wait 30 seconds after the screen goes dark before you switch off the power.',
        ok: 'Turn off', okClass: 'stop', countdown: 3,
    });
    if (ok) executeShutdown();
}

function executeShutdown() {
    var powerBtn = document.getElementById('power-btn');
    if (powerBtn) powerBtn.disabled = true;
    fetch('/api/system/shutdown', { method: 'POST' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success) showToast('Turning off all OWLs and this screen', 'info');
            else {
                showToast('Could not turn off: ' + (data.error || 'unknown'), 'error');
                if (powerBtn) powerBtn.disabled = false;
            }
        })
        .catch(function() { showToast('Turn off sent', 'info'); });
}

// ============================================
// FIX SCREEN (Maintenance)
// ============================================

async function fixScreen() {
    const ok = await OwlUI.confirm({
        title: 'Reinstall the touchscreen firmware?',
        body: 'This can take up to 2 minutes. The controller needs a restart after it.',
        ok: 'Reinstall',
    });
    if (!ok) return;
    const row = document.getElementById('btn-fix-screen');
    const meta = row ? row.querySelector('.meta') : null;
    if (row) row.disabled = true;
    if (meta) meta.textContent = 'Reinstalling';
    fetch('/api/system/fix-screen', { method: 'POST' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success && data.needs_reboot) {
                showToast('Firmware reinstalled. Restart the controller.', 'success');
                if (meta) meta.textContent = 'Done. Restart the controller to finish.';
            } else {
                showToast('Fix screen failed: ' + (data.error || 'unknown'), 'error');
                if (meta) meta.textContent = 'Reinstalls the touchscreen firmware';
            }
        })
        .catch(function(err) {
            showToast('Error: ' + err.message, 'error');
            if (meta) meta.textContent = 'Reinstalls the touchscreen firmware';
        })
        .finally(function() { if (row) row.disabled = false; });
}

// ============================================
// MODE AVAILABILITY
// ============================================

// AI and Hybrid get an amber dot (not a disabled grey) when an OWL in the
// selection can't run them; tapping one then explains why (_shell.js).
function updateModeAvailability(canRun) {
    // querySelectorAll: every copy of the mode selector stays in sync
    document.querySelectorAll(
        '.mode-btn[data-mode="gog"], .mode-btn[data-mode="hybrid"]'
    ).forEach(function(btn) {
        btn.classList.toggle('blocked', !canRun);
        btn.title = canRun ? '' : 'AI can not run on every OWL yet';
    });
}

function updatePaintedChipHint(hasProfiles) {
    document.querySelectorAll('.mode-btn[data-mode="lut"]').forEach(function(btn) {
        btn.classList.toggle('no-profiles', !hasProfiles);
        btn.title = hasProfiles ? '' : 'No painted profiles yet. Opens the paint panel';
    });
}
