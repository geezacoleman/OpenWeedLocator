/**
 * OWL Web Interface - Optimized Version
 * Improved JavaScript for monitoring, GPS, and detection/recording control
 */

let isGpsEnabled = true;
let gpsWatchId = null;
let gpsData = null;
let isRecording = false;
let recordingStartTime = null;
let zoomLevel = 1;
let updateInterval = null;
let pendingRequests = {};
const MAX_RECORDING_TIME = 30; // seconds
const ESTIMATED_BITRATE = 2000000; // bits per second
const SYSTEM_UPDATE_INTERVAL = 5000; // Reduced polling frequency
const zoomStep = 0.2;
const maxZoom = 3;
const minZoom = 1;

document.addEventListener('DOMContentLoaded', function() {
    initTabs();
    initZoom();
    initGPS();
    initControlButtons();

    // Start periodic updates
    startUpdateInterval();
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
 * Initialize control buttons with debouncing
 */
function initControlButtons() {
    const buttons = {
        'downloadFrame': downloadFrame,
        'recordButton': toggleRecording,
        'start-detection': startDetection,
        'stop-detection': stopDetection
    };

    Object.entries(buttons).forEach(([id, handler]) => {
        const button = document.getElementById(id);
        if (button) {
            button.addEventListener('click', function(e) {
                e.preventDefault();

                // Prevent multiple clicks
                if (this.classList.contains('disabled')) return;

                // Visual feedback
                this.classList.add('disabled');
                const originalText = this.textContent;
                this.innerHTML = '<div class="spinner-small"></div>';

                // Execute handler with debouncing
                handler.call(this);

                // Reset button after 2 seconds regardless of response
                setTimeout(() => {
                    if (id !== 'recordButton' || !isRecording) {
                        this.innerHTML = originalText;
                    }
                    this.classList.remove('disabled');
                }, 2000);
            });
        }
    });
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
 * API request with timeout and abort controller
 */
function apiRequest(url, options = {}, timeout = 10000) {
    // Cancel any pending request to the same endpoint
    if (pendingRequests[url]) {
        pendingRequests[url].abort();
    }

    // Create new abort controller
    const controller = new AbortController();
    pendingRequests[url] = controller;

    // Set up timeout
    const timeoutId = setTimeout(() => controller.abort(), timeout);

    // Merge options with signal
    const fetchOptions = {
        ...options,
        signal: controller.signal,
        cache: 'no-store', // Prevent caching
        headers: {
            ...options.headers,
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache'
        }
    };

    // Make request
    return fetch(url, fetchOptions)
        .then(response => {
            clearTimeout(timeoutId);
            delete pendingRequests[url];
            if (!response.ok) throw new Error(`Request failed: ${response.status}`);
            return response;
        })
        .catch(error => {
            clearTimeout(timeoutId);
            delete pendingRequests[url];
            if (error.name === 'AbortError') {
                throw new Error('Request timed out');
            }
            throw error;
        });
}

/**
 * Download current frame with optimized handling
 */
function downloadFrame() {
    showNotification('Info', 'Downloading current frame...', 'info');

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
            console.error('Download frame error:', error);
        });
}

/**
 * Toggle recording state with optimized handling
 */
function toggleRecording() {
    const button = document.getElementById('recordButton');
    const statusElement = document.getElementById('recordingStatus');
    if (!button || !statusElement) return;

    if (!isRecording) {
        showNotification('Info', 'Starting recording...', 'info');

        apiRequest('/api/recording/start', { method: 'POST' })
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
            .catch(error => {
                showNotification('Error', error.message || 'Failed to start recording', 'error');
                console.error('Recording start error:', error);
            });
    } else {
        button.disabled = true;
        button.innerHTML = '<div class="spinner"></div>';
        showNotification('Info', 'Stopping recording and downloading...', 'info');

        apiRequest('/api/recording/stop', { method: 'POST' }, 30000) // Longer timeout for video processing
            .then(response => response.blob())
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

                // Download the video file
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
                showNotification('Error', error.message || 'Failed to save recording', 'error');
                console.error('Recording stop error:', error);
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
 * Start/stop detection with optimized handling
 */
function startDetection() {
    showNotification('Info', 'Starting detection...', 'info');

    apiRequest('/api/detection/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Detection started', 'success');
                // Update UI immediately without waiting for next poll
                const detectionElement = document.getElementById('detectionStatus');
                if (detectionElement) detectionElement.textContent = 'Enabled';
            } else {
                throw new Error(data.message || 'Failed to start detection');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to start detection', 'error');
            console.error('Detection start error:', error);
        });
}

function stopDetection() {
    showNotification('Info', 'Stopping detection...', 'info');

    apiRequest('/api/detection/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Detection stopped', 'success');
                // Update UI immediately without waiting for next poll
                const detectionElement = document.getElementById('detectionStatus');
                if (detectionElement) detectionElement.textContent = 'Disabled';
            } else {
                throw new Error(data.message || 'Failed to stop detection');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to stop detection', 'error');
            console.error('Detection stop error:', error);
        });
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
 * GPS functions with optimized handling
 */
function startGPS() {
    if (!('geolocation' in navigator)) {
        updateGPSStatus(null, 'GPS not available');
        return;
    }

    const options = {
        enableHighAccuracy: true,
        timeout: 5000,
        maximumAge: 10000  // Accept locations up to 10 seconds old to reduce power consumption
    };

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

    // Only send GPS updates to server if it changed significantly
    if (!window.lastGpsSent ||
        Math.abs(window.lastGpsSent.latitude - gpsData.latitude) > 0.0001 ||
        Math.abs(window.lastGpsSent.longitude - gpsData.longitude) > 0.0001 ||
        Math.abs(window.lastGpsSent.accuracy - gpsData.accuracy) > 1) {

        window.lastGpsSent = {...gpsData};

        apiRequest('/api/update_gps', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(gpsData)
        }).catch(error => console.error('GPS update error:', error));
    }
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
    updateInterval = setInterval(updateSystemStats, SYSTEM_UPDATE_INTERVAL);
}

/**
 * Update system statistics with optimized handling
 */
function updateSystemStats() {
    apiRequest('/api/system_stats')
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
                tempElement.style.color = getColorForValue(data.cpu_temp, 85);
            }

            const detectionElement = document.getElementById('detectionStatus');
            if (detectionElement) {
                detectionElement.textContent = data.detection_enable ? 'Enabled' : 'Disabled';
            }

            const recordingTextElement = document.getElementById('recordingStatusText');
            if (recordingTextElement) {
                recordingTextElement.textContent = data.recording_enable ? 'Recording' : 'Stopped';
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
 * Show notification with improved handling
 */
function showNotification(title, message, type = 'info', duration = 5000) {
    const container = document.getElementById('notifications');
    if (!container) return;

    // Limit number of notifications
    const existingNotifications = container.querySelectorAll('.notification');
    if (existingNotifications.length > 5) {
        container.removeChild(existingNotifications[0]);
    }

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
        setTimeout(() => {
            if (notification.parentNode === container) {
                container.removeChild(notification);
            }
        }, 300);
    });

    setTimeout(() => {
        if (notification.parentNode === container) {
            notification.style.animation = 'slide-out 0.3s forwards';
            setTimeout(() => {
                if (notification.parentNode === container) {
                    container.removeChild(notification);
                }
            }, 300);
        }
    }, duration);
}

// Add CSS for the spinner
const style = document.createElement('style');
style.textContent = `
.spinner-small {
    width: 12px;
    height: 12px;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-radius: 50%;
    border-top-color: #fff;
    display: inline-block;
    animation: spin 1s ease-in-out infinite;
    margin-right: 5px;
    vertical-align: middle;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

.disabled {
    opacity: 0.7;
    cursor: not-allowed;
}
`;
document.head.appendChild(style);

// Cleanup on unload
window.addEventListener('unload', () => {
    if (gpsWatchId !== null) navigator.geolocation.clearWatch(gpsWatchId);
    if (updateInterval) clearInterval(updateInterval);
    if (window.recordingInterval) clearInterval(window.recordingInterval);

    // Abort any pending requests
    Object.values(pendingRequests).forEach(controller => {
        try { controller.abort(); } catch (e) {}
    });
});