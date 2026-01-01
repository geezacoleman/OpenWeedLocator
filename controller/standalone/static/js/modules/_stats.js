/* ==========================================================================
   OWL Dashboard - System Stats Module
   System status polling and UI updates
   ========================================================================== */

/**
 * Start system status update interval
 */
function startUpdateInterval() {
    if (updateInterval) clearInterval(updateInterval);
    updateSystemStats();
    updateInterval = setInterval(updateSystemStats, SYSTEM_UPDATE_INTERVAL);
}

/**
 * Update system stats from API
 */
function updateSystemStats() {
    apiRequest('/api/system_stats')
        .then(r => r.json())
        .then(data => {
            // Gradient chips
            setText('cpuChipVal', `${data.cpu_percent}%`);
            setText('memChipVal', `${data.memory_percent}%`);
            setText('tempChipVal', `${data.cpu_temp}°C`);

            // Power button reflects owl_running
            setPowerButtonState(!!data.owl_running);

            // Detection & recording big switches (and status chip)
            const detectionOn = !!(data.detection_running ?? data.detection_enable);
            const recordingOn = !!(data.recording ?? data.image_sample_enable);

            syncSwitch('detectSwitch', detectionOn);
            syncSwitch('recordSwitch', recordingOn);

            // Handle detection_mode for status chip (0=Spot Spray, 1=Off, 2=Blanket)
            const detectionMode = data.detection_mode;
            updateDetectionModeDisplay(detectionMode, detectionOn);

            // Sensitivity Low/High
            const sensLabel = normalizeSensitivity(data);
            document.querySelectorAll('.seg-btn[data-sens]').forEach(b => {
                b.classList.toggle('active', b.dataset.sens === sensLabel);
            });

            // Fan Auto/100 + RPM
            const fanMode = normalizeFanMode(data.fan_status);
            document.querySelectorAll('.seg-btn[data-fan]').forEach(b => {
                b.classList.toggle('active', b.dataset.fan === fanMode);
            });
            const rpmEl = document.getElementById('fanRpmReadout');
            if (rpmEl) {
                const rpm = data?.fan_status?.rpm;
                rpmEl.textContent = (typeof rpm === 'number') ? `${rpm} rpm` : '—';
            }

            // Header online/offline & stream overlay
            const statusDot = document.getElementById('statusDot');
            const statusText = document.getElementById('statusText');
            if (statusDot && statusText) {
                statusDot.classList.toggle('connected', !!data.owl_running);
                statusText.textContent = data.owl_running ? 'Online' : 'Offline';
            }
            const streamOverlay = document.getElementById('stream-status-overlay');
            const streamImg = document.getElementById('stream-img');
            if (streamOverlay && streamImg) {
                const on = !!data.stream_active;
                streamOverlay.classList.toggle('hidden', on);
                streamImg.style.display = on ? 'block' : 'none';
            }

            // Update hardware lock UI based on controller status
            updateHardwareLockUI();
        })
        .catch(err => {
            // Silent fail for stats polling
        })
        .finally(() => {
            checkForErrors();
        });

    // Helper functions
    function setText(id, text) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function syncSwitch(id, on) {
        const btn = document.getElementById(id);
        if (!btn) return;
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        const state = btn.querySelector('.switch-state');
        if (state) state.textContent = on ? 'ON' : 'OFF';
    }

    function setPowerButtonState(on) {
        const btn = document.getElementById('owlPowerBtn');
        if (!btn) return;

        btn.classList.remove('booting', 'stopping');
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.classList.toggle('on', on);
    }
}

/**
 * Update detection mode display (Spot Spray / Off / Blanket)
 */
function updateDetectionModeDisplay(mode, detectionOn) {
    const chip = document.getElementById('sprayerStatusChip');
    if (!chip) return;

    const txt = document.getElementById('sprayerStatusText');

    // detection_mode: 0 = Spot Spray, 1 = Off, 2 = Blanket
    if (mode === 0) {
        chip.classList.add('on');
        chip.classList.remove('blanket', 'off');
        if (txt) txt.textContent = 'Spot Spray';
    } else if (mode === 2) {
        chip.classList.add('on', 'blanket');
        chip.classList.remove('off');
        if (txt) txt.textContent = 'Blanket';
    } else {
        chip.classList.remove('blanket');
        if (detectionOn) {
            chip.classList.add('on');
            chip.classList.remove('off');
            if (txt) txt.textContent = 'Running';
        } else {
            chip.classList.remove('on');
            chip.classList.add('off');
            if (txt) txt.textContent = 'Off';
        }
    }
}

/**
 * Normalize sensitivity data from API
 */
function normalizeSensitivity(data) {
    if (data && typeof data.sensitivity_level === 'string') {
        return data.sensitivity_level.toLowerCase() === 'high' ? 'High' : 'Low';
    }
    return 'High';
}

/**
 * Normalize fan mode from status object
 */
function normalizeFanMode(status) {
    if (!status || !status.mode) return 'auto';
    const m = String(status.mode).toLowerCase();
    if (m.includes('100')) return '100';
    if (m.includes('auto')) return 'auto';
    return (m === '1' || m === '100' || m === '1.0') ? '100' : 'auto';
}

/**
 * Polls the backend for errors from owl.py and displays them as notifications
 */
function checkForErrors() {
    apiRequest('/api/get_errors')
        .then(response => response.json())
        .then(errors => {
            if (errors && errors.length > 0) {
                errors.forEach(error => {
                    const title = `OWL Error: ${error.level || 'ERROR'}`;
                    const message = error.message || 'An unknown error occurred.';
                    showNotification(title, message, 'error', 0);
                });
            }
        })
        .catch(error => {
            // Silent fail for error polling
        });
}
