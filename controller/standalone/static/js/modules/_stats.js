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
            // Cache resolution for recording warning check
            lastResWidth = data.resolution_width || 0;
            lastResHeight = data.resolution_height || 0;

            // Gradient chips
            setText('cpuChipVal', `${data.cpu_percent}%`);
            setText('memChipVal', `${data.memory_percent}%`);
            setText('tempChipVal', `${data.cpu_temp}°C`);

            // Power button reflects owl_running
            setPowerButtonState(!!data.owl_running);

            // Detection & recording big switches (and status chip)
            const detectionOn = !!(data.detection_running ?? data.detection_enable);
            const recordingOn = !!(data.recording ?? data.image_sample_enable);

            const nozzlesOn = data.detection_mode === 2;
            syncSwitch('detectSwitch', nozzlesOn ? false : detectionOn);
            syncSwitch('recordSwitch', recordingOn);
            syncSwitch('nozzleSwitch', nozzlesOn);
            syncSwitch('trackingSwitch', !!data.tracking_enabled);

            // Sync session metadata panel visibility with recording state
            if (typeof syncSessionMetadataPanel === 'function') {
                syncSessionMetadataPanel(recordingOn);
            }

            // Show/hide track stability panel based on tracking state
            var stabilityTile = document.getElementById('track-stability-tile');
            if (stabilityTile) {
                stabilityTile.style.display = data.tracking_enabled ? '' : 'none';
            }

            // OWL service status chip
            const owlChip = document.getElementById('owlStatusChip');
            const owlText = document.getElementById('owlStatusText');
            if (owlChip && owlText) {
                if (data.owl_running) {
                    owlChip.classList.add('on');
                    owlChip.classList.remove('off');
                    owlText.textContent = 'Running';
                } else {
                    owlChip.classList.remove('on');
                    owlChip.classList.add('off');
                    owlText.textContent = 'Stopped';
                }
            }

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

            // Header online/offline
            const statusDot = document.getElementById('statusDot');
            const statusText = document.getElementById('statusText');
            if (statusDot && statusText) {
                statusDot.classList.toggle('connected', !!data.owl_running);
                statusText.textContent = data.owl_running ? 'Online' : 'Offline';
            }

            // Sync AI tab if function exists
            if (typeof syncAITabFromStats === 'function') {
                syncAITabFromStats(data);
            }

            // Sync config sliders from MQTT state
            if (typeof syncSlidersFromStats === 'function') {
                syncSlidersFromStats(data);
            }

            // Update hardware lock UI based on controller status
            updateHardwareLockUI();

            // Pipeline mode selector
            if (typeof updatePipelineModeUI === 'function' && data.algorithm) {
                updatePipelineModeUI(data.algorithm);
            }
            if (typeof updateModeAvailability === 'function') {
                updateModeAvailability(!!data.model_available);
            }

            // Algorithm error banner
            if (typeof updateAlgorithmError === 'function') {
                updateAlgorithmError(data.algorithm_error);
            }
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
