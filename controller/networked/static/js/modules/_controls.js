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
        // Currently off, so turn on
        btn.classList.add('active');
        btn.textContent = 'Stop Recording';
        globalRecordingEnabled = true;
        sendCommand('all', 'toggle_recording', true);
        showToast('Recording started on all OWLs', 'success');
    }
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
// OWL RESTART
// ============================================

async function restartOWL(deviceId) {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.textContent = 'Restarting...';

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
    setTimeout(() => {
        btn.disabled = false;
        btn.textContent = 'Restart';
    }, 10000);
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
