/**
 * OWL Web Interface
 * Simplified JavaScript for monitoring, GPS, and detection/recording control
 */

let isGpsEnabled = true;
let gpsWatchId = null;
let gpsData = null;
let isRecording = false;
let recordingStartTime = null;
let zoomLevel = 1;
let updateInterval = null;
const MAX_RECORDING_TIME = 30; // seconds
const ESTIMATED_BITRATE = 2000000; // bits per second
const zoomStep = 0.2;
const maxZoom = 3;
const minZoom = 1;

document.addEventListener('DOMContentLoaded', function() {
    initTabs();
    initZoom();
    initGPS();

    // Start periodic updates
    startUpdateInterval();

    // Set up button handlers
    document.getElementById('downloadFrame')?.addEventListener('click', downloadFrame);
    document.getElementById('recordButton')?.addEventListener('click', toggleRecording);
    document.getElementById('start-detection')?.addEventListener('click', startDetection);
    document.getElementById('stop-detection')?.addEventListener('click', stopDetection);
});

/**
 * Initialize tab navigation
 */
function initTabs() {
    const tabLinks = document.querySelectorAll('.nav-tab');
    tabLinks.forEach(tab => {
        tab.addEventListener('click', function() {
            tabLinks.forEach(t => t.classList.remove('active'));
            this.classList.add('active');
            const tabContents = document.querySelectorAll('.tab-content');
            tabContents.forEach(content => content.classList.remove('active'));
            const targetId = this.getAttribute('data-tab');
            document.getElementById(targetId).classList.add('active');
        });
    });
    const firstTab = document.querySelector('.nav-tab');
    if (firstTab) firstTab.click();
}

/**
 * Initialize zoom functionality
 */
function initZoom() {
    document.querySelector('.zoom-controls button:nth-child(1)')?.addEventListener('click', zoomIn);
    document.querySelector('.zoom-controls button:nth-child(2)')?.addEventListener('click', zoomOut);
    document.querySelector('.zoom-controls button:nth-child(3)')?.addEventListener('click', resetZoom);
}

/**
 * Zoom functions
 */
function zoomIn() {
    if (zoomLevel < maxZoom) {
        zoomLevel += zoomStep;
        updateZoom();
    }
}

function zoomOut() {
    if (zoomLevel > minZoom) {
        zoomLevel -= zoomStep;
        updateZoom();
    }
}

function resetZoom() {
    zoomLevel = 1;
    updateZoom();
}

function updateZoom() {
    const img = document.querySelector('.zoom-image');
    if (img) img.style.transform = `scale(${zoomLevel})`;
}

/**
 * Download current frame
 */
function downloadFrame() {
    fetch('/api/download_frame', { method: 'POST' })
        .then(response => {
            if (!response.ok) throw new Error('Frame not available');
            return response.blob();
        })
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
        .catch(error => showNotification('Error', error.message, 'error'));
}

/**
 * Toggle recording state
 */
function toggleRecording() {
    const button = document.getElementById('recordButton');
    const statusElement = document.getElementById('recordingStatus');
    if (!button || !statusElement) return;

    if (!isRecording) {
        fetch('/api/recording/start', { method: 'POST' })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    isRecording = true;
                    recordingStartTime = Date.now();
                    button.textContent = 'Stop Recording';
                    button.classList.add('recording');
                    statusElement.style.display = 'block';
                    updateRecordingStatus();
                    if (window.recordingInterval) clearInterval(window.recordingInterval);
                    window.recordingInterval = setInterval(updateRecordingStatus, 1000);
                } else {
                    throw new Error(data.message || 'Failed to start recording');
                }
            })
            .catch(error => showNotification('Error', error.message, 'error'));
    } else {
        button.disabled = true;
        button.innerHTML = '<div class="spinner"></div>';
        fetch('/api/recording/stop', { method: 'POST' })
            .then(response => {
                if (!response.ok) throw new Error('Recording failed');
                return response.blob();
            })
            .then(blob => {
                isRecording = false;
                button.disabled = false;
                button.textContent = 'Start Recording';
                button.classList.remove('recording');
                statusElement.style.display = 'none';
                if (window.recordingInterval) {
                    clearInterval(window.recordingInterval);
                    window.recordingInterval = null;
                }
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.style.display = 'none';
                a.href = url;
                const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
                a.download = `owl_recording_${timestamp}.mp4`;
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);
                showNotification('Success', 'Recording saved and downloaded', 'success');
            })
            .catch(error => {
                isRecording = false;
                button.disabled = false;
                button.textContent = 'Start Recording';
                button.classList.remove('recording');
                statusElement.style.display = 'none';
                if (window.recordingInterval) {
                    clearInterval(window.recordingInterval);
                    window.recordingInterval = null;
                }
                showNotification('Error', error.message, 'error');
            });
    }
}

/**
 * Update recording status display
 */
function updateRecordingStatus() {
    const statusElement = document.getElementById('recordingStatus');
    if (!statusElement || !isRecording) return;
    const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
    const remaining = MAX_RECORDING_TIME - elapsed;
    const estimatedSize = Math.max(1, Math.floor((elapsed * ESTIMATED_BITRATE) / (8 * 1024 * 1024)));
    let gpsStatus = gpsData ? `GPS: ±${gpsData.accuracy.toFixed(1)}m` : '';
    statusElement.innerHTML = `Recording: ${remaining}s remaining<br>Estimated Size: ~${estimatedSize}MB${gpsStatus ? '<br>' + gpsStatus : ''}`;
    if (elapsed >= MAX_RECORDING_TIME) toggleRecording();
}

/**
 * Start/stop detection
 */
function startDetection() {
    fetch('/api/detection/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) showNotification('Success', data.message, 'success');
            else showNotification('Error', data.message, 'error');
        })
        .catch(error => showNotification('Error', error.message, 'error'));
}

function stopDetection() {
    fetch('/api/detection/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) showNotification('Success', data.message, 'success');
            else showNotification('Error', data.message, 'error');
        })
        .catch(error => showNotification('Error', error.message, 'error'));
}

/**
 * Initialize GPS functionality
 */
function initGPS() {
    const toggle = document.getElementById('gps-toggle');
    if (toggle) {
        toggle.addEventListener('change', toggleGPS);
        if (toggle.checked) startGPS();
    } else {
        startGPS();
    }
}

/**
 * GPS functions
 */
function startGPS() {
    if (!('geolocation' in navigator)) {
        updateGPSStatus(null, 'GPS not available');
        return;
    }
    const options = { enableHighAccuracy: true, timeout: 5000, maximumAge: 0 };
    gpsWatchId = navigator.geolocation.watchPosition(handleGPSSuccess, handleGPSError, options);
}

function stopGPS() {
    if (gpsWatchId !== null) {
        navigator.geolocation.clearWatch(gpsWatchId);
        gpsWatchId = null;
    }
    updateGPSStatus(null, 'GPS disabled');
}

function toggleGPS(event) {
    isGpsEnabled = event.target.checked;
    if (isGpsEnabled) startGPS();
    else stopGPS();
}

function handleGPSSuccess(position) {
    gpsData = {
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
        accuracy: position.coords.accuracy,
        timestamp: new Date().toISOString()
    };
    updateGPSStatus(gpsData.accuracy);
    fetch('/api/update_gps', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gpsData)
    });
}

function handleGPSError(error) {
    console.error('GPS Error:', error);
    updateGPSStatus(null, error.message);
}

function updateGPSStatus(accuracy, errorMessage = null) {
    const iconElement = document.querySelector('.gps-icon');
    const textElement = document.getElementById('gps-accuracy');
    if (!iconElement || !textElement) return;

    if (!isGpsEnabled) {
        iconElement.closest('.gps-status').classList.add('gps-disabled');
        textElement.textContent = 'GPS disabled';
        return;
    }

    iconElement.closest('.gps-status').classList.remove('gps-disabled');
    iconElement.classList.remove('gps-good', 'gps-medium', 'gps-poor');

    if (errorMessage) {
        iconElement.classList.add('gps-poor');
        textElement.textContent = errorMessage;
    } else if (accuracy === null) {
        iconElement.classList.add('gps-poor');
        textElement.textContent = 'Searching...';
    } else {
        if (accuracy <= 5) iconElement.classList.add('gps-good');
        else if (accuracy <= 10) iconElement.classList.add('gps-medium');
        else iconElement.classList.add('gps-poor');
        textElement.textContent = `±${accuracy.toFixed(1)}m`;
    }
}

/**
 * Start system status update interval
 */
function startUpdateInterval() {
    if (updateInterval) clearInterval(updateInterval);
    updateSystemStats();
    updateInterval = setInterval(updateSystemStats, 3000);
}

/**
 * Update system statistics
 */
function updateSystemStats() {
    fetch('/api/system_stats')
        .then(response => response.json())
        .then(data => {
            const cpuElement = document.getElementById('cpuValue');
            if (cpuElement) {
                cpuElement.textContent = `${data.cpu_percent}%`;
                cpuElement.style.color = getColorForValue(data.cpu_percent, 100);
            }

            const tempElement = document.getElementById('tempValue');
            if (tempElement) {
                tempElement.textContent = `${data.cpu_temp}°C`;
                cpuElement.style.color = getColorForValue(data.cpu_temp, 85);
            }

            const detectionElement = document.getElementById('detectionStatus');
            if (detectionElement) {
                detectionElement.textContent = data.detection_enabled ? 'Enabled' : 'Disabled';
            }

            const recordingTextElement = document.getElementById('recordingStatusText');
            if (recordingTextElement) {
                recordingTextElement.textContent = data.recording_enabled ? 'Recording' : 'Stopped';
            }

            const timestampElement = document.getElementById('statusTimestamp');
            if (timestampElement) {
                timestampElement.textContent = data.timestamp || new Date().toLocaleString();
            }
        })
        .catch(error => console.error('Error fetching system stats:', error));
}

/**
 * Get color based on percentage value
 */
function getColorForValue(value, max) {
    const normalized = value / max;
    const hue = ((1 - normalized) * 120).toFixed(0);
    return `hsl(${hue}, 70%, 50%)`;
}

/**
 * Show notification
 */
function showNotification(title, message, type = 'info') {
    const container = document.getElementById('notifications');
    if (!container) return;

    const notification = document.createElement('div');
    notification.className = `notification ${type}`;
    notification.innerHTML = `
        <div class="notification-title">${title}</div>
        <div class="notification-message">${message}</div>
        <button class="notification-close">×</button>
    `;

    container.appendChild(notification);

    const closeButton = notification.querySelector('.notification-close');
    closeButton.addEventListener('click', function() {
        notification.style.animation = 'slide-out 0.3s forwards';
        setTimeout(() => container.removeChild(notification), 300);
    });

    setTimeout(() => {
        if (notification.parentNode === container) {
            notification.style.animation = 'slide-out 0.3s forwards';
            setTimeout(() => container.removeChild(notification), 300);
        }
    }, 5000);
}

// Cleanup on unload
window.addEventListener('unload', () => {
    if (gpsWatchId !== null) navigator.geolocation.clearWatch(gpsWatchId);
    if (updateInterval) clearInterval(updateInterval);
    if (window.recordingInterval) clearInterval(window.recordingInterval);
});