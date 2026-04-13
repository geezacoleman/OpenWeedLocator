/* ==========================================================================
   OWL Dashboard - Controls Module
   Dashboard controls, power, detection, recording, hardware controller
   ========================================================================== */

/**
 * Initialize dashboard controls (power, detection, recording, sensitivity, fan)
 */
function initDashboardControls() {
    // Power button
    const powerBtn = document.getElementById('owlPowerBtn');
    if (powerBtn) {
        powerBtn.addEventListener('click', () => {
            const isOn = powerBtn.getAttribute('aria-pressed') === 'true';
            const fn = isOn ? stopOwl : startOwl;
            fn()
                .then(() => updateSystemStats())
                .catch(err => showNotification('Error', err.message || 'Power action failed', 'error'));
        });
    }

    // Detection
    const detectBtn = document.getElementById('detectSwitch');
    if (detectBtn) {
        detectBtn.addEventListener('click', () => {
            const isOn = detectBtn.getAttribute('aria-pressed') === 'true';
            const fn = isOn ? stopDetection : startDetection;
            fn()
                .then(() => updateSystemStats())
                .catch(err => showNotification('Error', err.message || 'Detection action failed', 'error'));
        });
    }

    // Recording
    const recordBtn = document.getElementById('recordSwitch');
    if (recordBtn) {
        recordBtn.addEventListener('click', () => {
            const isOn = recordBtn.getAttribute('aria-pressed') === 'true';
            const fn = isOn ? stopRecording : startRecording;
            fn()
                .then(() => updateSystemStats())
                .catch(err => showNotification('Error', err.message || 'Recording action failed', 'error'));
        });
    }

    // Tracking
    const trackingBtn = document.getElementById('trackingSwitch');
    if (trackingBtn) {
        trackingBtn.addEventListener('click', () => {
            const isOn = trackingBtn.getAttribute('aria-pressed') === 'true';
            const fn = isOn ? stopTracking : startTracking;
            fn()
                .then(() => updateSystemStats())
                .catch(err => showNotification('Error', err.message || 'Tracking action failed', 'error'));
        });
    }

    // All Nozzles
    const nozzleBtn = document.getElementById('nozzleSwitch');
    if (nozzleBtn) {
        nozzleBtn.addEventListener('click', () => {
            const isOn = nozzleBtn.getAttribute('aria-pressed') === 'true';
            const fn = isOn ? stopAllNozzles : startAllNozzles;
            fn()
                .then(() => updateSystemStats())
                .catch(err => showNotification('Error', err.message || 'Nozzle action failed', 'error'));
        });
    }

    // Sensitivity presets — bind static buttons, then fetch dynamic list
    bindSensitivityButtons();
    fetchSensitivityPresets();

    // Fan (Auto / 100)
    const fanBtns = document.querySelectorAll('.seg-btn[data-fan]');
    fanBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            setSegActive(fanBtns, btn);
            const mode = btn.dataset.fan;
            setFanMode(mode)
                .then(() => updateSystemStats())
                .catch(err => showNotification('Error', err.message || 'Failed to set fan mode', 'error'));
        });
    });
}

/**
 * Initialize control buttons with debouncing
 */
function initControlButtons() {
    const buttons = {
        'downloadFrame': downloadFrame,
        'start-recording': startRecording,
        'stop-recording': stopRecording,
        'start-detection': startDetection,
        'stop-detection': stopDetection
    };

    Object.entries(buttons).forEach(([id, handler]) => {
        const button = document.getElementById(id);
        if (button) {
            button.addEventListener('click', function(e) {
                e.preventDefault();

                if (this.classList.contains('disabled')) return;

                this.classList.add('disabled');
                const originalText = this.textContent;
                this.innerHTML = '<div class="spinner-small"></div>' + originalText;

                handler.call(this);

                setTimeout(() => {
                    this.innerHTML = originalText;
                    this.classList.remove('disabled');
                }, 2000);
            });
        }
    });
}

/* --------------------------------------------------------------------------
   Power Controls
   -------------------------------------------------------------------------- */

function startOwl() {
    const btn = document.getElementById('owlPowerBtn');

    if (btn) {
        btn.classList.add('booting');
        btn.classList.remove('on', 'stopping');
        btn.disabled = true;
    }

    return apiRequest('/api/owl/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'OWL service starting...', 'success');
            } else {
                throw new Error(data.error || 'Failed to start OWL service');
            }
        })
        .catch(err => {
            if (btn) {
                btn.classList.remove('booting');
                btn.disabled = false;
            }
            throw err;
        })
        .finally(() => {
            setTimeout(() => {
                if (btn) btn.disabled = false;
            }, 2000);
        });
}

function stopOwl() {
    const btn = document.getElementById('owlPowerBtn');

    if (btn) {
        btn.classList.add('stopping');
        btn.classList.remove('on', 'booting');
        btn.disabled = true;
    }

    return apiRequest('/api/owl/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'OWL service stopping...', 'success');
            } else {
                throw new Error(data.error || 'Failed to stop OWL service');
            }
        })
        .catch(err => {
            if (btn) {
                btn.classList.remove('stopping');
                btn.disabled = false;
            }
            throw err;
        })
        .finally(() => {
            setTimeout(() => {
                if (btn) btn.disabled = false;
            }, 2000);
        });
}

/* --------------------------------------------------------------------------
   Hardware lock check — controller-type-aware
   -------------------------------------------------------------------------- */

/**
 * Check if a specific control is hardware-locked for the current controller type.
 * UTE: only 'recording' is hardware-controlled.
 * Advanced: 'recording', 'detection', 'sensitivity' are hardware-controlled.
 * Returns true if the action should be blocked.
 */
function isHardwareLocked(control) {
    if (!hardwareControllerActive) return false;

    if (controllerType === 'ute') {
        // Ute has one switch — it locks only the control it manages
        return control === switchPurpose;
    } else if (controllerType === 'advanced') {
        return ['recording', 'detection', 'sensitivity'].includes(control);
    }
    return false;
}

/* --------------------------------------------------------------------------
   Detection Controls
   -------------------------------------------------------------------------- */

function startDetection() {
    if (isHardwareLocked('detection')) {
        showNotification(
            'Hardware Priority',
            `Use the detection switch on your ${controllerType.toUpperCase()} controller`,
            'warning'
        );
        return Promise.resolve();
    }

    // Turn off nozzles if active (starting detection overrides blanket mode)
    const nozzleBtn = document.getElementById('nozzleSwitch');
    if (nozzleBtn && nozzleBtn.getAttribute('aria-pressed') === 'true') {
        stopAllNozzles();
    }

    showNotification('Info', 'Starting detection...', 'info');

    return apiRequest('/api/detection/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Detection started', 'success');
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to start detection');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to start detection', 'error');
        });
}

function stopDetection() {
    if (isHardwareLocked('detection')) {
        showNotification(
            'Hardware Priority',
            `Use the detection switch on your ${controllerType.toUpperCase()} controller`,
            'warning'
        );
        return Promise.resolve();
    }

    showNotification('Info', 'Stopping detection...', 'info');

    return apiRequest('/api/detection/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Detection stopped', 'success');
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to stop detection');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to stop detection', 'error');
        });
}

/* --------------------------------------------------------------------------
   Recording Controls
   -------------------------------------------------------------------------- */

function startRecording() {
    if (isHardwareLocked('recording')) {
        showNotification(
            'Hardware Priority',
            `Use the recording switch on your ${controllerType.toUpperCase()} controller`,
            'warning'
        );
        return Promise.resolve();
    }

    // Check resolution before starting recording
    if (typeof isResolutionBelowMax === 'function' && isResolutionBelowMax(lastResWidth, lastResHeight)) {
        return new Promise(function(resolve) {
            showResolutionWarningModal(lastResWidth, lastResHeight,
                function onAccept() {
                    // Persist max resolution (copy-on-write safe) then restart OWL
                    apiRequest('/api/camera/set_max_resolution', { method: 'POST' })
                    .then(function() { return stopOwl(); })
                    .then(function() {
                        setTimeout(function() {
                            startOwl();
                            showNotification('Info',
                                'Resolution changed to ' + OWL_MAX_RES_WIDTH + 'x' + OWL_MAX_RES_HEIGHT +
                                ' — OWL restarting. Start recording when it is back online.', 'info', 8000);
                        }, 1000);
                    })
                    .catch(function(err) {
                        showNotification('Error', err.message || 'Failed to change resolution', 'error');
                    });
                    resolve();
                },
                function onContinue() {
                    _doStartRecording();
                    resolve();
                }
            );
        });
    }

    return _doStartRecording();
}

function _doStartRecording() {
    showNotification('Info', 'Starting recording...', 'info');

    return apiRequest('/api/recording/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Recording started', 'success');
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to start recording');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to start recording', 'error');
        });
}

function stopRecording() {
    if (isHardwareLocked('recording')) {
        showNotification(
            'Hardware Priority',
            `Use the recording switch on your ${controllerType.toUpperCase()} controller`,
            'warning'
        );
        return Promise.resolve();
    }

    showNotification('Info', 'Stopping recording...', 'info');

    return apiRequest('/api/recording/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Recording stopped', 'success');
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to stop recording');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to stop recording', 'error');
        });
}

/* --------------------------------------------------------------------------
   Tracking Controls
   -------------------------------------------------------------------------- */

function startTracking() {
    showNotification('Info', 'Enabling tracking...', 'info');

    return apiRequest('/api/tracking/set', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ value: true })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', 'Tracking enabled', 'success', 2000);
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to enable tracking');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to enable tracking', 'error');
        });
}

function stopTracking() {
    showNotification('Info', 'Disabling tracking...', 'info');

    return apiRequest('/api/tracking/set', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ value: false })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', 'Tracking disabled', 'success', 2000);
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to disable tracking');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to disable tracking', 'error');
        });
}

/* --------------------------------------------------------------------------
   Track Stability
   -------------------------------------------------------------------------- */

var TRACK_STABILITY_PRESETS = {
    low:    { track_high_thresh: 0.3,  track_low_thresh: 0.15, new_track_thresh: 0.3,  track_buffer: 30, match_thresh: 0.8 },
    medium: { track_high_thresh: 0.2,  track_low_thresh: 0.05, new_track_thresh: 0.2,  track_buffer: 60, match_thresh: 0.7 },
    high:   { track_high_thresh: 0.15, track_low_thresh: 0.05, new_track_thresh: 0.15, track_buffer: 90, match_thresh: 0.6 }
};

function setTrackStability(level) {
    var btns = document.querySelectorAll('#track-stability-buttons .seg-btn');
    btns.forEach(function(b) { b.classList.remove('active'); });
    var target = document.querySelector('#track-stability-buttons .seg-btn[data-stability="' + level + '"]');
    if (target) target.classList.add('active');

    var preset = TRACK_STABILITY_PRESETS[level];
    if (!preset) return;

    return apiRequest('/api/config/section', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ section: 'Tracking', params: preset })
    })
        .then(function(response) { return response.json(); })
        .then(function(data) {
            if (data.success) {
                showNotification('Success', 'Track stability: ' + level, 'success', 2000);
                updateSystemStats();
            } else {
                throw new Error(data.error || 'Failed to set track stability');
            }
        })
        .catch(function(error) {
            showNotification('Error', error.message || 'Failed to set track stability', 'error');
        });
}

/* --------------------------------------------------------------------------
   All Nozzles Controls
   -------------------------------------------------------------------------- */

function startAllNozzles() {
    showNotification('Info', 'Turning all nozzles ON...', 'info');

    return apiRequest('/api/nozzles/all-on', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Warning', 'All nozzles ON — detection disabled', 'warning');
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to turn on nozzles');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to turn on nozzles', 'error');
        });
}

function stopAllNozzles() {
    showNotification('Info', 'Turning all nozzles OFF...', 'info');

    return apiRequest('/api/nozzles/all-off', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', 'All nozzles OFF', 'success');
                updateSystemStats();
            } else {
                throw new Error(data.message || 'Failed to turn off nozzles');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to turn off nozzles', 'error');
        });
}

/* --------------------------------------------------------------------------
   Sensitivity & Fan Controls
   -------------------------------------------------------------------------- */

function setSegActive(nodeList, activeBtn) {
    nodeList.forEach(b => b.classList.toggle('active', b === activeBtn));
}

function bindSensitivityButtons() {
    const container = document.getElementById('sensitivity-buttons');
    if (!container) return;
    container.querySelectorAll('.seg-btn[data-sens]').forEach(btn => {
        btn.addEventListener('click', () => {
            const allBtns = container.querySelectorAll('.seg-btn[data-sens]');
            setSegActive(allBtns, btn);
            setSensitivity(btn.dataset.sens);
        });
    });
}

function fetchSensitivityPresets() {
    apiRequest('/api/sensitivity/presets')
        .then(r => r.json())
        .then(data => {
            if (!data.success) return;
            renderSensitivityButtons(data.presets || [], data.active || 'medium');
        })
        .catch(() => {}); // Silently fall back to static buttons
}

function renderSensitivityButtons(presets, active) {
    const container = document.getElementById('sensitivity-buttons');
    if (!container) return;
    if (!presets.length) return; // Keep static buttons

    container.innerHTML = '';
    // Builtin labels
    const builtinLabels = { low: 'Low', medium: 'Med', high: 'High' };

    // Builtins first, then custom
    const builtins = presets.filter(p => p.is_builtin);
    const custom = presets.filter(p => !p.is_builtin);
    const ordered = [...builtins, ...custom];

    // Adjust segmented class for button count
    container.className = 'segmented';
    if (ordered.length === 2) container.classList.add('segmented--two');
    else if (ordered.length === 3) container.classList.add('segmented--three');

    ordered.forEach(preset => {
        const btn = document.createElement('button');
        btn.className = 'seg-btn';
        if (preset.name === active) btn.classList.add('active');
        btn.dataset.sens = preset.name;
        btn.textContent = builtinLabels[preset.name] || preset.name;
        btn.addEventListener('click', () => {
            const allBtns = container.querySelectorAll('.seg-btn[data-sens]');
            setSegActive(allBtns, btn);
            setSensitivity(preset.name);
        });
        container.appendChild(btn);
    });
}

function setSensitivity(level) {
    return apiRequest('/api/sensitivity/set', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ level: level })
    })
        .then(r => r.json())
        .then(d => {
            if (!d.success) throw new Error(d.error || 'Failed to set sensitivity');
            showNotification('Success', d.message || 'Sensitivity set', 'success', 2000);
            updateSystemStats();
        })
        .catch(err => {
            showNotification('Error', err.message || 'Failed to set sensitivity', 'error');
            throw err;
        });
}

function setFanMode(mode) {
    return apiRequest('/api/fan/set', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ mode: mode })
    })
        .then(r => r.json())
        .then(d => {
            if (!d.success) throw new Error(d.error || 'Failed to set fan mode');
            showNotification('Success', d.message || 'Fan mode toggled', 'success', 2000);
            updateSystemStats();
        })
        .catch(err => {
            showNotification('Error', err.message || 'Failed to set fan mode', 'error');
            throw err;
        });
}

/* --------------------------------------------------------------------------
   Frame Download
   -------------------------------------------------------------------------- */

function downloadFrame() {
    showNotification('Info', 'Downloading current frame...', 'info');

    const streamImg = document.getElementById('stream-img');
    if (!streamImg || !streamImg.src) {
        showNotification('Error', 'No video stream available', 'error');
        return;
    }

    apiRequest('/api/download_frame', { method: 'POST' })
        .then(response => response.blob())
        .then(blob => {
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
            a.download = `owl_frame_${timestamp}.jpg`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
            showNotification('Success', 'Frame downloaded successfully', 'success');
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to download frame', 'error');
        });
}

/* --------------------------------------------------------------------------
   Hardware Controller
   -------------------------------------------------------------------------- */

function initHardwareControllerCheck() {
    checkHardwareControllerStatus();
    setInterval(checkHardwareControllerStatus, 30000);
}

function checkHardwareControllerStatus() {
    apiRequest('/api/controller_config')
        .then(response => response.json())
        .then(data => {
            const wasActive = hardwareControllerActive;
            hardwareControllerActive = data.hardware_active;
            controllerType = data.controller_type;
            switchPurpose = data.switch_purpose || 'recording';

            updateHardwareLockUI();

            if (wasActive !== hardwareControllerActive) {
                if (hardwareControllerActive) {
                    showNotification(
                        'Hardware Controller',
                        `${controllerType.toUpperCase()} controller active - use physical switches for control`,
                        'info',
                        8000
                    );
                } else {
                    showNotification(
                        'Web Control',
                        'Hardware controller not active - web controls available',
                        'info',
                        5000
                    );
                }
            }
        })
        .catch(error => {
            hardwareControllerActive = false;
            controllerType = 'none';
            switchPurpose = 'recording';
            updateHardwareLockUI();
        });
}

function updateHardwareLockUI() {
    // Clear all locks first
    document.querySelectorAll('.hardware-locked').forEach(el => {
        el.classList.remove('hardware-locked');
    });
    document.querySelectorAll('.lock-icon').forEach(el => el.remove());

    // Hardware notice
    const notice = document.querySelector('.hardware-notice');
    if (notice) {
        notice.style.display = hardwareControllerActive ? 'flex' : 'none';
    }

    // Show/hide switch purpose toggle (only for Ute)
    var switchPurposeIndicator = document.getElementById('switchPurposeIndicator');
    if (switchPurposeIndicator) {
        switchPurposeIndicator.style.display = (controllerType === 'ute') ? 'inline-flex' : 'none';
        syncSwitchPurposeToggle();
    }

    if (!hardwareControllerActive) return;

    // Controller-type-selective locking:
    // UTE: lock only the switch matching switch_purpose (recording OR detection)
    // Advanced: recording, detection, sensitivity
    // Fan, tracking, track stability, nozzles: NEVER locked
    if (controllerType === 'ute') {
        lockSwitch(switchPurpose === 'detection' ? 'detectSwitch' : 'recordSwitch');
    } else if (controllerType === 'advanced') {
        lockSwitch('recordSwitch');
        lockSwitch('detectSwitch');
        lockSegmented('sensitivity-buttons');
    }
}

function lockSwitch(switchId) {
    const sw = document.getElementById(switchId);
    if (!sw) return;
    sw.classList.add('hardware-locked');
    const stateSpan = sw.querySelector('.switch-state');
    if (stateSpan && !stateSpan.querySelector('.lock-icon')) {
        addLockIcon(stateSpan);
    }
}

function lockSegmented(segId) {
    const seg = document.getElementById(segId);
    if (!seg) return;
    seg.classList.add('hardware-locked');
    const tile = seg.closest('.control-tile');
    if (tile) tile.classList.add('hardware-locked');
}

function addLockIcon(element) {
    const lockSpan = document.createElement('span');
    lockSpan.className = 'lock-icon';
    lockSpan.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24"><path d="M12,17A2,2 0 0,0 14,15C14,13.89 13.1,13 12,13A2,2 0 0,0 10,15A2,2 0 0,0 12,17M18,8A2,2 0 0,1 20,10V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V10C4,8.89 4.9,8 6,8H7V6A5,5 0 0,1 12,1A5,5 0 0,1 17,6V8H18M12,3A3,3 0 0,0 9,6V8H15V6A3,3 0 0,0 12,3Z"/></svg>`;
    element.insertBefore(lockSpan, element.firstChild);
}

function removeLockIcon(element) {
    const lockIcon = element.querySelector('.lock-icon');
    if (lockIcon) lockIcon.remove();
}

/* --------------------------------------------------------------------------
   Switch Purpose Toggle (Ute controller: recording vs detection)
   -------------------------------------------------------------------------- */

function initSwitchPurposeToggle() {
    var toggle = document.getElementById('switchPurposeToggle');
    if (toggle) {
        toggle.addEventListener('change', function() {
            var newPurpose = toggle.checked ? 'detection' : 'recording';
            setSwitchPurpose(newPurpose);
        });
    }
}

function setSwitchPurpose(purpose) {
    apiRequest('/api/controller/switch_purpose', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ purpose: purpose })
    })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                switchPurpose = purpose;
                showNotification('Switch Purpose', d.message || 'Restart OWL to apply', 'info', 6000);
                updateHardwareLockUI();
            } else {
                throw new Error(d.error || 'Failed to set switch purpose');
            }
        })
        .catch(function(err) {
            showNotification('Error', err.message || 'Failed to set switch purpose', 'error');
            // Revert toggle
            syncSwitchPurposeToggle();
        });
}

function syncSwitchPurposeToggle() {
    var toggle = document.getElementById('switchPurposeToggle');
    var label = document.getElementById('switchPurposeText');
    if (toggle) {
        toggle.checked = (switchPurpose === 'detection');
    }
    if (label) {
        label.textContent = switchPurpose === 'detection' ? 'Detection' : 'Recording';
    }
}

/* --------------------------------------------------------------------------
   Pipeline Mode Selector
   -------------------------------------------------------------------------- */

let lastGoBAlgorithm = 'exhsv';
let pendingMode = null;
let pendingModeTimestamp = 0;

function setPipelineMode(mode) {
    var btn = document.querySelector('.mode-btn[data-mode="' + mode + '"]');
    if (!btn || btn.classList.contains('disabled') || btn.classList.contains('loading')) return;

    var algorithm;
    if (mode === 'gob') {
        algorithm = lastGoBAlgorithm;
    } else if (mode === 'gog') {
        algorithm = 'gog';
    } else if (mode === 'hybrid') {
        algorithm = 'gog-hybrid';
    } else {
        return;
    }

    pendingMode = mode;
    pendingModeTimestamp = Date.now();
    btn.classList.add('loading');

    apiRequest('/api/algorithm/set', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ algorithm: algorithm })
    })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', 'Algorithm set to ' + algorithm, 'success', 2000);
            } else {
                showNotification('Error', data.error || 'Failed to set algorithm', 'error');
                btn.classList.remove('loading');
                pendingMode = null;
            }
        })
        .catch(err => {
            showNotification('Error', err.message || 'Failed to set algorithm', 'error');
            btn.classList.remove('loading');
            pendingMode = null;
        });
}

function updatePipelineModeUI(algorithm) {
    // Don't overwrite UI while mode change is in-flight (max 10s cooldown)
    if (pendingMode && (Date.now() - pendingModeTimestamp < 10000)) return;
    pendingMode = null;

    var mode;
    if (algorithm === 'gog') {
        mode = 'gog';
    } else if (algorithm === 'gog-hybrid') {
        mode = 'hybrid';
    } else {
        mode = 'gob';
        if (algorithm) lastGoBAlgorithm = algorithm;
    }

    document.querySelectorAll('.mode-btn').forEach(function(btn) {
        btn.classList.remove('active', 'loading');
        if (btn.dataset.mode === mode) {
            btn.classList.add('active');
        }
    });
}

function updateModeAvailability(modelAvailable) {
    var gogBtn = document.querySelector('.mode-btn[data-mode="gog"]');
    var hybridBtn = document.querySelector('.mode-btn[data-mode="hybrid"]');

    if (gogBtn) {
        if (modelAvailable) {
            gogBtn.classList.remove('disabled');
        } else {
            gogBtn.classList.add('disabled');
        }
    }
    if (hybridBtn) {
        if (modelAvailable) {
            hybridBtn.classList.remove('disabled');
        } else {
            hybridBtn.classList.add('disabled');
        }
    }
}

/* --------------------------------------------------------------------------
   Algorithm Error State
   -------------------------------------------------------------------------- */

let lastAlgorithmError = null;

function updateAlgorithmError(error) {
    // Only act on state changes
    if (error === lastAlgorithmError) return;
    lastAlgorithmError = error;

    var banner = document.getElementById('algorithmErrorBanner');
    if (error) {
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'algorithmErrorBanner';
            banner.className = 'algorithm-error-banner';
            var modeRow = document.querySelector('.pipeline-mode');
            if (modeRow) modeRow.parentNode.insertBefore(banner, modeRow.nextSibling);
        }
        banner.textContent = 'Detection unavailable: ' + error + '. Switch algorithm to recover.';
        banner.style.display = '';
    } else {
        if (banner) banner.style.display = 'none';
    }
}

/* --------------------------------------------------------------------------
   Preview (Dashboard inline stream)
   -------------------------------------------------------------------------- */

let previewActive = false;

function initPreview() {
    const toggleBtn = document.getElementById('previewToggle');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', togglePreview);
    }

    const fullscreenBtn = document.getElementById('fullscreenBtn');
    if (fullscreenBtn) {
        fullscreenBtn.addEventListener('click', toggleFullscreen);
    }

    const downloadBtn = document.getElementById('downloadFrame');
    if (downloadBtn) {
        downloadBtn.addEventListener('click', downloadFrame);
    }
}

function togglePreview() {
    const container = document.getElementById('previewContainer');
    const btn = document.getElementById('previewToggle');
    const img = document.getElementById('stream-img');
    if (!container) return;

    previewActive = !previewActive;
    container.classList.toggle('hidden', !previewActive);
    if (btn) btn.classList.toggle('active', previewActive);

    if (previewActive && img) {
        img.src = '/video_feed?t=' + Date.now();
        img.onerror = function() {
            var overlay = document.getElementById('stream-status-overlay');
            if (overlay) overlay.classList.remove('hidden');
            img.style.display = 'none';
        };
        img.onload = function() {
            var overlay = document.getElementById('stream-status-overlay');
            if (overlay) overlay.classList.add('hidden');
            img.style.display = 'block';
        };
    }
}

function toggleFullscreen() {
    const container = document.getElementById('previewContainer');
    if (!container) return;

    if (!document.fullscreenElement) {
        container.requestFullscreen().catch(function(err) {
            console.error('Error entering fullscreen:', err);
        });
    } else {
        document.exitFullscreen();
    }
}

/* --------------------------------------------------------------------------
   GPS Functions (browser geolocation for image tagging)
   -------------------------------------------------------------------------- */

function initGPS() {
    const gpsToggle = document.getElementById('gpsToggle');
    if (gpsToggle) {
        gpsToggle.addEventListener('change', toggleGPS);
        if (gpsToggle.checked) {
            startGPS();
        }
    }
}

function toggleGPS() {
    const gpsToggle = document.getElementById('gpsToggle');
    if (gpsToggle && gpsToggle.checked) {
        startGPS();
    } else {
        stopGPS();
    }
}

function startGPS() {
    if (!navigator.geolocation) {
        showNotification('GPS Error', 'Geolocation not supported', 'error');
        return;
    }

    isGpsEnabled = true;

    gpsWatchId = navigator.geolocation.watchPosition(
        handleGPSSuccess,
        handleGPSError,
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 }
    );

    updateGPSIndicator();
}

function stopGPS() {
    isGpsEnabled = false;

    if (gpsWatchId !== null) {
        navigator.geolocation.clearWatch(gpsWatchId);
        gpsWatchId = null;
    }

    gpsData = null;
    updateGPSIndicator();
}

function handleGPSSuccess(position) {
    gpsData = {
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
        accuracy: position.coords.accuracy,
        speed: position.coords.speed,
        heading: position.coords.heading,
        timestamp: position.timestamp
    };

    updateGPSIndicator();

    // Send to server for image tagging
    apiRequest('/api/update_gps', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gpsData)
    }).catch(function() {});
}

function handleGPSError(error) {
    updateGPSIndicator();
}

function updateGPSIndicator() {
    var indicator = document.getElementById('gpsIndicator');
    var statusText = document.getElementById('gpsStatusText');
    if (!indicator) return;

    if (!isGpsEnabled || !gpsData) {
        indicator.classList.remove('gps-active', 'gps-good', 'gps-medium', 'gps-poor');
        indicator.classList.add('gps-off');
        if (statusText) statusText.textContent = isGpsEnabled ? 'Acquiring...' : 'GPS Off';
        return;
    }

    indicator.classList.remove('gps-off');
    indicator.classList.add('gps-active');

    // Accuracy class
    indicator.classList.remove('gps-good', 'gps-medium', 'gps-poor');
    if (gpsData.accuracy <= 10) {
        indicator.classList.add('gps-good');
    } else if (gpsData.accuracy <= 30) {
        indicator.classList.add('gps-medium');
    } else {
        indicator.classList.add('gps-poor');
    }

    if (statusText) {
        statusText.textContent = '\u00B1' + Math.round(gpsData.accuracy) + 'm';
    }
}

/* --------------------------------------------------------------------------
   Session Metadata
   -------------------------------------------------------------------------- */

let _sessionMetadataLocked = false;
let _lastRecordingState = false;

function initSessionMetadata() {
    const toggle = document.getElementById('sessionInfoToggle');
    if (toggle) {
        toggle.addEventListener('click', function () {
            const body = document.getElementById('sessionInfoBody');
            const chevron = document.getElementById('sessionChevron');
            if (body) {
                const visible = body.style.display !== 'none';
                body.style.display = visible ? 'none' : '';
                if (chevron) chevron.style.transform = visible ? '' : 'rotate(180deg)';
            }
        });
    }
}

function syncSessionMetadataPanel(isRecording) {
    // Auto-expand when recording starts
    if (isRecording && !_lastRecordingState) {
        const body = document.getElementById('sessionInfoBody');
        const chevron = document.getElementById('sessionChevron');
        if (body) {
            body.style.display = '';
            if (chevron) chevron.style.transform = 'rotate(180deg)';
        }
        loadSessionMetadata();
    }
    _lastRecordingState = isRecording;
}

function loadSessionMetadata() {
    fetch('/api/session/metadata')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            var fn = document.getElementById('metaFieldName');
            var crop = document.getElementById('metaCrop');
            var weather = document.getElementById('metaWeather');
            var vehicle = document.getElementById('metaVehicle');
            if (fn) fn.value = data.field_name || '';
            if (crop) crop.value = data.crop || '';
            if (weather) weather.value = data.weather || '';
            if (vehicle) vehicle.value = data.vehicle || '';
        })
        .catch(function () {});
}

function saveSessionMetadata() {
    var data = {
        field_name: (document.getElementById('metaFieldName') || {}).value || '',
        crop: (document.getElementById('metaCrop') || {}).value || '',
        weather: (document.getElementById('metaWeather') || {}).value || '',
        vehicle: (document.getElementById('metaVehicle') || {}).value || '',
    };

    fetch('/api/session/metadata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    })
    .then(function (r) { return r.json(); })
    .then(function (result) {
        if (result.success !== false) {
            _lockMetadataForm();
            showNotification('Success', 'Session info saved', 'success');
        } else {
            showNotification('Error', result.error || 'Save failed', 'error');
        }
    })
    .catch(function () {
        showNotification('Error', 'Failed to save session info', 'error');
    });
}

function editSessionMetadata() {
    _unlockMetadataForm();
}

function _lockMetadataForm() {
    _sessionMetadataLocked = true;
    document.querySelectorAll('.session-field input').forEach(function (el) {
        el.readOnly = true;
        el.classList.add('locked');
    });
    var saveBtn = document.getElementById('sessionSaveBtn');
    var editBtn = document.getElementById('sessionEditBtn');
    if (saveBtn) saveBtn.style.display = 'none';
    if (editBtn) editBtn.style.display = '';
}

function _unlockMetadataForm() {
    _sessionMetadataLocked = false;
    document.querySelectorAll('.session-field input').forEach(function (el) {
        el.readOnly = false;
        el.classList.remove('locked');
    });
    var saveBtn = document.getElementById('sessionSaveBtn');
    var editBtn = document.getElementById('sessionEditBtn');
    if (saveBtn) saveBtn.style.display = '';
    if (editBtn) editBtn.style.display = 'none';
}
