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

// Last seen storage warning level; notifications fire on transitions only
let lastStorageWarning = null;

function watchStorageWarning(data) {
    const warning = data.storage_warning || 'ok';
    if (warning === lastStorageWarning) return;
    const previous = lastStorageWarning;
    lastStorageWarning = warning;
    if (previous === null) return;   // first poll: no transition to report

    const freeGb = (typeof data.storage_free_mb === 'number')
        ? (data.storage_free_mb / 1024).toFixed(1) : null;
    const freeText = freeGb ? ` (${freeGb} GB free)` : '';
    if (warning === 'full') {
        showNotification('Storage full',
            `Recording unavailable${freeText}. Download or delete sessions to continue`,
            'error', 10000);
    } else if (warning === 'low') {
        showNotification('Storage low',
            `Space is running out${freeText}. Download or delete sessions soon`,
            'warning', 8000);
    } else if (previous === 'full' || previous === 'low') {
        showNotification('Storage recovered', `Recording available again${freeText}`, 'success');
    }
}

// Last seen speed warning; notifications fire on transitions only (the
// warning itself shows as a notice on Field, drawn by _shell.js)
let lastSpeedWarning = null;

function watchSpeedWarning(data) {
    const warning = data.speed_warning || '';
    if (warning === lastSpeedWarning) return;
    const previous = lastSpeedWarning;
    lastSpeedWarning = warning;
    if (warning) {
        showNotification('Slow down', warning, 'warning', 8000);
    } else if (previous) {
        showNotification('Speed OK', 'Sprays are on target again', 'success');
    }
}

/**
 * Update system stats from API. The redesigned screens are drawn by
 * Shell.update (_shell.js); the older modules sync their own pieces here.
 */
function updateSystemStats() {
    apiRequest('/api/system_stats')
        .then(r => r.json())
        .then(data => {
            // Cache resolution + clamp state for recording warning check
            lastResWidth = data.resolution_width || 0;
            lastResHeight = data.resolution_height || 0;
            lastRequestedResWidth = data.requested_resolution_width || data.resolution_width || 0;
            lastRequestedResHeight = data.requested_resolution_height || data.resolution_height || 0;
            lastResolutionClamped = !!data.resolution_clamped;
            lastRpiVersion = data.rpi_version || 'unknown';
            lastAllowHighResolution = !!data.allow_high_resolution;
            if (typeof setHighResContextBadge === 'function') {
                setHighResContextBadge(lastRpiVersion);
            }

            // Sensitivity buttons (mode-aware: reads the axis matching the
            // active detection mode, highlights nothing on a custom value)
            if (typeof syncSensitivityFromStats === 'function') syncSensitivityFromStats(data);

            // Fan Auto / full speed + RPM
            const fanMode = normalizeFanMode(data.fan_status);
            document.querySelectorAll('.seg-btn[data-fan]').forEach(b => {
                b.classList.toggle('active', b.dataset.fan === fanMode);
            });
            const rpmEl = document.getElementById('fanRpmReadout');
            if (rpmEl) {
                const fan = data.fan_status || {};
                rpmEl.textContent = fan.is_rpi5
                    ? ((typeof fan.rpm === 'number') ? `${fan.rpm} rpm` : '--')
                    : 'Fan control needs a Raspberry Pi 5';
            }

            // Storage and speed warnings: notify once per state change
            watchStorageWarning(data);
            watchSpeedWarning(data);

            if (typeof updateCloudManageBlock === 'function') updateCloudManageBlock(data);
            if (typeof syncSlidersFromStats === 'function') syncSlidersFromStats(data);
            if (typeof syncLutPanelFromStats === 'function') syncLutPanelFromStats(data);

            // Hardware lock UI based on controller status
            updateHardwareLockUI();

            // Pipeline mode selector
            if (typeof updatePipelineModeUI === 'function' && data.algorithm) {
                updatePipelineModeUI(data.algorithm);
            }
            if (typeof updatePaintedChipHint === 'function') {
                updatePaintedChipHint((data.available_lut_profiles || []).length > 0);
            }
            if (typeof OWLWidget !== 'undefined') OWLWidget._broadcastState(data);

            // Live white balance readout + calibration progress (Camera page)
            if (typeof updateAwbReadout === 'function') {
                updateAwbReadout(data.camera, data.awb_calibration,
                    typeof onAwbCalibrationComplete === 'function' ? onAwbCalibrationComplete : null);
            }

            // The redesigned screens (switches, notices, stat cells, AI page)
            if (typeof Shell !== 'undefined') Shell.update(data);
        })
        .catch(err => {
            // Silent fail for stats polling
        })
        .finally(() => {
            checkForErrors();
        });
}

/**
 * Normalize sensitivity data from API
 */
function normalizeSensitivity(data) {
    if (data && typeof data.sensitivity_level === 'string') {
        return data.sensitivity_level.toLowerCase();
    }
    return 'medium';
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
