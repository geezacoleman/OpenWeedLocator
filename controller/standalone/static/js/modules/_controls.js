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

    // Sensitivity (Low / High)
    const sensBtns = document.querySelectorAll('.seg-btn[data-sens]');
    sensBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            setSegActive(sensBtns, btn);
            const level = btn.dataset.sens.toLowerCase();
            setSensitivity(level)
                .then(() => updateSystemStats())
                .catch(err => showNotification('Error', err.message || 'Failed to set sensitivity', 'error'));
        });
    });

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
   Detection Controls
   -------------------------------------------------------------------------- */

function startDetection() {
    if (hardwareControllerActive) {
        showNotification(
            'Hardware Priority',
            `Use the detection switch on your ${controllerType.toUpperCase()} controller`,
            'warning'
        );
        return Promise.resolve();
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
    if (hardwareControllerActive) {
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
    if (hardwareControllerActive) {
        showNotification(
            'Hardware Priority',
            `Use the recording switch on your ${controllerType.toUpperCase()} controller`,
            'warning'
        );
        return Promise.resolve();
    }

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
    if (hardwareControllerActive) {
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
   Sensitivity & Fan Controls
   -------------------------------------------------------------------------- */

function setSegActive(nodeList, activeBtn) {
    nodeList.forEach(b => b.classList.toggle('active', b === activeBtn));
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
            showNotification('Success', d.message || 'Sensitivity toggled', 'success', 2000);
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
            updateHardwareLockUI();
        });
}

function updateHardwareLockUI() {
    // Update OWL switches
    const switches = document.querySelectorAll('.owl-switch');
    switches.forEach(sw => {
        if (hardwareControllerActive) {
            sw.classList.add('hardware-locked');
            const stateSpan = sw.querySelector('.switch-state');
            if (stateSpan && !stateSpan.querySelector('.lock-icon')) {
                addLockIcon(stateSpan);
            }
        } else {
            sw.classList.remove('hardware-locked');
            const stateSpan = sw.querySelector('.switch-state');
            if (stateSpan) {
                removeLockIcon(stateSpan);
            }
        }
    });

    // Update segmented controls
    const segmentedControls = document.querySelectorAll('.segmented');
    segmentedControls.forEach(seg => {
        if (hardwareControllerActive) {
            seg.classList.add('hardware-locked');
            const tile = seg.closest('.control-tile');
            if (tile) tile.classList.add('hardware-locked');
        } else {
            seg.classList.remove('hardware-locked');
            const tile = seg.closest('.control-tile');
            if (tile) tile.classList.remove('hardware-locked');
        }
    });

    // Update hardware notice
    const notice = document.querySelector('.hardware-notice');
    if (notice) {
        notice.style.display = hardwareControllerActive ? 'flex' : 'none';
    }
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
