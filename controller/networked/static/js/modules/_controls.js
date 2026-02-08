// ============================================
// OWL Central Controller - Main Action Buttons
// Detection/recording toggles, OWL restart
// ============================================

function toggleMainDetection() {
    const btn = document.getElementById('main-detection-btn');

    if (btn.classList.contains('off')) {
        // Currently stopping, so start
        btn.classList.remove('off');
        btn.textContent = 'START DETECTION';
        globalDetectionEnabled = false;
        sendCommand('all', 'toggle_detection', false);
        showToast('Detection stopped on all OWLs', 'info');
    } else {
        // Currently starting, so stop
        btn.classList.add('off');
        btn.textContent = 'STOP DETECTION';
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
        btn.textContent = 'RECORDING OFF';
        globalRecordingEnabled = false;
        sendCommand('all', 'toggle_recording', false);
        showToast('Recording stopped on all OWLs', 'info');
    } else {
        // Currently off, so turn on
        btn.classList.add('active');
        btn.textContent = 'RECORDING ON';
        globalRecordingEnabled = true;
        sendCommand('all', 'toggle_recording', true);
        showToast('Recording started on all OWLs', 'success');
    }
}

// ============================================
// OWL RESTART
// ============================================

async function restartOWL(deviceId) {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.textContent = 'RESTARTING...';

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
        btn.textContent = 'RESTART';
    }, 10000);
}
