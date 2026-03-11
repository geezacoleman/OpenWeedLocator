// ============================================
// OWL Central Controller - Main Action Buttons
// Detection/recording toggles, OWL restart
// ============================================

function toggleMainDetection() {
    const btn = document.getElementById('main-detection-btn');
    const nozzleBtn = document.getElementById('main-nozzles-btn');

    if (btn.classList.contains('off')) {
        // Currently stopping, so start
        btn.classList.remove('off');
        btn.textContent = 'Start Detection';
        globalDetectionEnabled = false;
        sendCommand('all', 'toggle_detection', false);
        showToast('Detection stopped on all OWLs', 'info');
    } else {
        // Starting detection — turn off nozzles if active
        if (globalNozzlesActive && nozzleBtn) {
            nozzleBtn.classList.remove('active');
            nozzleBtn.textContent = 'All Nozzles';
            globalNozzlesActive = false;
            sendCommand('all', 'toggle_all_nozzles', false);
        }
        btn.classList.add('off');
        btn.textContent = 'Stop Detection';
        globalDetectionEnabled = true;
        sendCommand('all', 'toggle_detection', true);
        showToast('Detection started on all OWLs', 'success');
    }
}

function toggleMainRecording() {
    const btn = document.getElementById('main-recording-btn');

    if (btn.classList.contains('active')) {
        // Currently on, so turn off
        btn.classList.remove('active');
        btn.textContent = 'Start Recording';
        globalRecordingEnabled = false;
        sendCommand('all', 'toggle_recording', false);
        showToast('Recording stopped on all OWLs', 'info');
    } else {
        // Check if any connected OWL has resolution below max
        var lowResOwl = _findLowResolutionOWL();
        if (lowResOwl && typeof showResolutionWarningModal === 'function') {
            showResolutionWarningModal(lowResOwl.w, lowResOwl.h,
                function onAccept() {
                    // Change resolution on all OWLs, save (copy-on-write), then restart
                    sendCommand('all', 'set_config_section', {
                        section: 'Camera',
                        params: {resolution_width: String(OWL_MAX_RES_WIDTH), resolution_height: String(OWL_MAX_RES_HEIGHT)}
                    })
                    .then(function() { return sendCommand('all', 'save_config', {}); })
                    .then(function() {
                        for (var id in owlsData) {
                            if (owlsData[id] && owlsData[id].connected) {
                                restartOWL(id);
                            }
                        }
                        showToast('Resolution changed — restarting all OWLs. Start recording when they are back online.', 'info');
                    });
                },
                function onContinue() {
                    _doToggleRecordingOn(btn);
                }
            );
            return;
        }

        _doToggleRecordingOn(btn);
    }
}

function _doToggleRecordingOn(btn) {
    btn.classList.add('active');
    btn.textContent = 'Stop Recording';
    globalRecordingEnabled = true;
    sendCommand('all', 'toggle_recording', true);
    showToast('Recording started on all OWLs', 'success');
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
            if (isResolutionBelowMax(w, h)) {
                return {w: w, h: h};
            }
        }
    }
    return null;
}

// ============================================
// ALL NOZZLES TOGGLE
// ============================================

function toggleAllNozzles() {
    const btn = document.getElementById('main-nozzles-btn');
    const detBtn = document.getElementById('main-detection-btn');

    if (btn.classList.contains('active')) {
        // Turn off
        btn.classList.remove('active');
        btn.textContent = 'All Nozzles';
        globalNozzlesActive = false;
        sendCommand('all', 'toggle_all_nozzles', false);
        showToast('All nozzles OFF', 'info');
    } else {
        // Turn on — also disable detection
        btn.classList.add('active');
        btn.textContent = 'Nozzles ON';
        globalNozzlesActive = true;
        sendCommand('all', 'toggle_all_nozzles', true);
        // Visual: show detection as off
        if (detBtn) {
            detBtn.classList.remove('off');
            detBtn.textContent = 'Start Detection';
        }
        globalDetectionEnabled = false;
        showToast('All nozzles ON — detection disabled', 'warning');
    }
}

// ============================================
// TRACKING TOGGLE
// ============================================

function toggleTracking() {
    const btn = document.getElementById('main-tracking-btn');
    if (!btn) return;

    if (btn.classList.contains('active')) {
        btn.classList.remove('active');
        btn.textContent = 'Tracking';
        globalTrackingEnabled = false;
        sendCommand('all', 'set_tracking', false);
        showToast('Tracking disabled', 'info');
    } else {
        btn.classList.add('active');
        btn.textContent = 'Tracking ON';
        globalTrackingEnabled = true;
        sendCommand('all', 'set_tracking', true);
        showToast('Tracking enabled', 'success');
    }
}

// ============================================
// OWL RESTART
// ============================================

async function restartOWL(deviceId) {
    // btn may be null when called programmatically (e.g. resolution change)
    const btn = (typeof event !== 'undefined' && event && event.currentTarget)
        ? event.currentTarget : null;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Restarting...';
    }

    try {
        const res = await fetch('/api/owl/' + deviceId + '/restart', { method: 'POST' });
        const data = await res.json();

        if (data.success) {
            showToast('Restart command sent to ' + deviceId, 'success');
        } else {
            showToast('Restart failed: ' + (data.error || 'Unknown'), 'error');
        }
    } catch (err) {
        showToast('Error restarting ' + deviceId, 'error');
    }

    // Re-enable after 10 seconds (OWL needs time to restart)
    if (btn) {
        setTimeout(() => {
            btn.disabled = false;
            btn.textContent = 'Restart';
        }, 10000);
    }
}

// ============================================
// PIPELINE MODE SELECTOR
// ============================================

// Remember GoB algorithm when switching modes
let lastGoBAlgorithm = 'exhsv';
let pendingMode = null;

function setPipelineMode(mode) {
    var btn = document.querySelector('.mode-btn[data-mode="' + mode + '"]');
    if (!btn || btn.classList.contains('disabled') || btn.classList.contains('loading')) return;

    // Map mode to algorithm value
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

    // Show loading state
    pendingMode = mode;
    btn.classList.add('loading');

    // Send command to all OWLs
    sendCommand('all', 'set_algorithm', algorithm);
}

function updatePipelineModeUI(algorithm) {
    var mode;
    if (algorithm === 'gog') {
        mode = 'gog';
    } else if (algorithm === 'gog-hybrid') {
        mode = 'hybrid';
    } else {
        mode = 'gob';
        // Remember the GoB algorithm for switching back
        if (algorithm) lastGoBAlgorithm = algorithm;
    }

    // Update button states
    document.querySelectorAll('.mode-btn').forEach(function(btn) {
        btn.classList.remove('active', 'loading');
        if (btn.dataset.mode === mode) {
            btn.classList.add('active');
        }
    });

    pendingMode = null;

    // Update slider visibility
    if (typeof updateSliderVisibility === 'function') {
        updateSliderVisibility(algorithm);
    }
}

// ============================================
// SYSTEM SHUTDOWN
// ============================================

var _shutdownTimer = null;

function initiateShutdown() {
    var overlay = document.getElementById('shutdown-overlay');
    var confirmBtn = document.getElementById('btn-confirm-shutdown');
    var countdownEl = document.getElementById('shutdown-countdown');
    if (!overlay) return;

    overlay.classList.add('visible');
    confirmBtn.disabled = true;

    var remaining = 3;
    countdownEl.textContent = remaining;

    _shutdownTimer = setInterval(function() {
        remaining--;
        countdownEl.textContent = remaining;
        if (remaining <= 0) {
            clearInterval(_shutdownTimer);
            _shutdownTimer = null;
            confirmBtn.disabled = false;
            confirmBtn.textContent = 'Confirm';
        }
    }, 1000);
}

function executeShutdown() {
    var confirmBtn = document.getElementById('btn-confirm-shutdown');
    var powerBtn = document.getElementById('power-btn');
    if (confirmBtn) {
        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Shutting down...';
    }
    if (powerBtn) powerBtn.disabled = true;

    fetch('/api/system/shutdown', { method: 'POST' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success) {
                showToast('Shutting down all OWLs and controller...', 'info');
            } else {
                showToast('Shutdown failed: ' + (data.error || 'Unknown'), 'error');
                cancelShutdown();
            }
        })
        .catch(function() {
            showToast('Shutdown request sent', 'info');
        });
}

function cancelShutdown() {
    var overlay = document.getElementById('shutdown-overlay');
    var confirmBtn = document.getElementById('btn-confirm-shutdown');
    var countdownEl = document.getElementById('shutdown-countdown');

    if (_shutdownTimer) {
        clearInterval(_shutdownTimer);
        _shutdownTimer = null;
    }

    if (overlay) overlay.classList.remove('visible');
    if (confirmBtn) {
        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Confirm (3)';
    }
    if (countdownEl) countdownEl.textContent = '3';
}

// ============================================
// FIX SCREEN
// ============================================

function fixScreen() {
    if (!confirm('Reinstall touchscreen firmware?\n\nThis may take up to 2 minutes. A reboot will be needed after.')) {
        return;
    }

    var btn = document.getElementById('btn-fix-screen');
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Fixing...';
    }

    fetch('/api/system/fix-screen', { method: 'POST' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success && data.needs_reboot) {
                showToast('Firmware reinstalled. Reboot needed.', 'success');
                if (btn) {
                    btn.textContent = 'Reboot Now';
                    btn.disabled = false;
                    btn.onclick = function() {
                        btn.disabled = true;
                        btn.textContent = 'Rebooting...';
                        fetch('/api/system/reboot', { method: 'POST' })
                            .then(function() {
                                showToast('Rebooting controller...', 'info');
                            })
                            .catch(function() {
                                showToast('Reboot request sent', 'info');
                            });
                    };
                }
            } else {
                showToast('Fix screen failed: ' + (data.error || 'Unknown'), 'error');
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = 'Fix Screen';
                }
            }
        })
        .catch(function(err) {
            showToast('Error: ' + err.message, 'error');
            if (btn) {
                btn.disabled = false;
                btn.textContent = 'Fix Screen';
            }
        });
}

// ============================================
// MODE AVAILABILITY
// ============================================

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
