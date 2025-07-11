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
    initStorageTab();
    initVideoStream();
    initUploadTab();
    initNotifications();

    const fullscreenBtn = document.getElementById('fullscreenBtn');
    if (fullscreenBtn) {
        fullscreenBtn.addEventListener('click', toggleFullscreen);
    }

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

            // ALWAYS reload data when tabs are clicked (force refresh)
            if (targetId === 'storage') {
                // Clear any cached data first
                document.getElementById('usbDevices').innerHTML = '<p>Scanning for USB devices...</p>';
                document.getElementById('fileList').innerHTML = '<p>Click Refresh to browse recordings</p>';
                // Force reload
                loadStorageData();
            } else if (targetId === 'upload') {
                initUploadTab();
            }
        });
    });

    const firstTab = document.querySelector('.nav-tab');
    if (firstTab) firstTab.click();
}

/**
 * Initialize storage tab functionality
 */
function initStorageTab() {
    const refreshButton = document.getElementById('refreshFiles');
    const downloadLogsButton = document.getElementById('downloadLogs');

    if (refreshButton) {
        refreshButton.addEventListener('click', loadStorageData);
    }

    if (downloadLogsButton) {
        downloadLogsButton.addEventListener('click', downloadLogs);
    }

    // Add breadcrumb container if it doesn't exist
    const fileList = document.getElementById('fileList');
    if (fileList && !document.getElementById('breadcrumbs')) {
        const breadcrumbDiv = document.createElement('div');
        breadcrumbDiv.id = 'breadcrumbs';
        fileList.parentNode.insertBefore(breadcrumbDiv, fileList);
    }
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
        'stop-detection': stopDetection,
        'toggle-sensitivity': toggleSensitivity,
        'refreshStreamBtn': refreshVideoStream
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
                this.innerHTML = '<div class="spinner-small"></div>' + originalText;

                // Execute handler with debouncing
                handler.call(this);

                // Reset button after 2 seconds regardless of response
                setTimeout(() => {
                    this.innerHTML = originalText;
                    this.classList.remove('disabled');
                }, 2000);
            });
        }
    });
}

/**
 * Initialize video stream
 */
function initVideoStream() {
    const streamImg = document.getElementById('stream-img');
    if (!streamImg) {
        console.error('Stream image element not found');
        return;
    }

    // Set up the video stream
    setupVideoStream();

    // Handle image load/error events
    streamImg.addEventListener('load', handleStreamLoad);
    streamImg.addEventListener('error', handleStreamError);

    console.log('Video stream initialized');
}

/**
 * Initialize zoom functionality
 */
function initZoom() {
    document.getElementById('zoomIn')?.addEventListener('click', zoomIn);
    document.getElementById('zoomOut')?.addEventListener('click', zoomOut);
    document.getElementById('zoomReset')?.addEventListener('click', resetZoom);
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
 * Setup video stream with cache busting
 */
function setupVideoStream() {
    const streamImg = document.getElementById('stream-img');
    if (!streamImg) return;

    // Add timestamp to prevent caching issues
    const timestamp = new Date().getTime();
    streamImg.src = `/video_feed?t=${timestamp}`;

    console.log('Video stream source set');
}


/**
 * Handle successful stream load
 */
function handleStreamLoad() {
    console.log('Video stream loaded successfully');
    updateStreamStatus('Connected', 'success');
}

/**
 * Handle stream errors with retry logic
 */
function handleStreamError(error) {
    console.error('Video stream error:', error);
    updateStreamStatus('Connection failed - retrying...', 'error');

    // Retry after 3 seconds
    setTimeout(() => {
        console.log('Retrying video stream...');
        setupVideoStream();
    }, 3000);
}

/**
 * Update stream connection status (optional visual feedback)
 */
function updateStreamStatus(message, type) {
    // You can add a status indicator if you want
    console.log(`Stream status: ${message} (${type})`);

    // Optional: Show notification for errors
    if (type === 'error') {
        showNotification('Stream Error', message, 'warning', 3000);
    }
}

/**
 * Refresh video stream manually
 */
function refreshVideoStream() {
    console.log('Manually refreshing video stream...');
    setupVideoStream();
    showNotification('Info', 'Video stream refreshed', 'info', 2000);
}

/**
 * Handle fullscreen for video
 */
function toggleFullscreen() {
    const streamContainer = document.querySelector('.stream-container');
    if (!streamContainer) return;

    if (!document.fullscreenElement) {
        streamContainer.requestFullscreen().catch(err => {
            console.error('Error entering fullscreen:', err);
        });
    } else {
        document.exitFullscreen();
    }
}

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

    // Add cache-busting for multiple clients
    const separator = url.includes('?') ? '&' : '?';
    const cacheBustUrl = url + separator + 't=' + Date.now();

    // Merge options with signal and cache-busting headers
    const fetchOptions = {
        ...options,
        signal: controller.signal,
        cache: 'no-store', // Prevent caching
        headers: {
            ...options.headers,
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        }
    };

    // Make request
    return fetch(cacheBustUrl, fetchOptions)
        .then(response => {
            clearTimeout(timeoutId);
            delete pendingRequests[url];

            if (!response.ok) {
                throw new Error(`Request failed: ${response.status} ${response.statusText}`);
            }
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
            console.error('Download frame error:', error);
        });
}

/**
 * Start recording
 */
function startRecording() {
    showNotification('Info', 'Starting recording...', 'info');

    apiRequest('/api/recording/start', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Recording started', 'success');
                updateSystemStats(); // Refresh UI
            } else {
                throw new Error(data.message || 'Failed to start recording');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to start recording', 'error');
            console.error('Recording start error:', error);
        });
}

/**
 * Stop recording
 */
function stopRecording() {
    showNotification('Info', 'Stopping recording...', 'info');

    apiRequest('/api/recording/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Recording stopped', 'success');
                updateSystemStats(); // Refresh UI
            } else {
                throw new Error(data.message || 'Failed to stop recording');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to stop recording', 'error');
            console.error('Recording stop error:', error);
        });
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
                updateSystemStats(); // Refresh UI immediately
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
                updateSystemStats(); // Refresh UI immediately
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
 * Toggle sensitivity
 */
function toggleSensitivity() {
    showNotification('Info', 'Toggling sensitivity...', 'info');

    apiRequest('/api/sensitivity/toggle', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Success', data.message || 'Sensitivity toggled', 'success');
                updateSystemStats(); // Refresh UI immediately
            } else {
                throw new Error(data.message || 'Failed to toggle sensitivity');
            }
        })
        .catch(error => {
            showNotification('Error', error.message || 'Failed to toggle sensitivity', 'error');
            console.error('Sensitivity toggle error:', error);
        });
}
let currentDirectory = '/media';

/**
 * Load storage data (USB devices and files)
 */
function loadStorageData() {
    // Clear cache and force fresh data
    const usbContainer = document.getElementById('usbDevices');
    if (usbContainer) {
        usbContainer.innerHTML = '<p>Scanning for USB devices...</p>';
    }

    // Add cache-busting parameter
    const timestamp = new Date().getTime();
    apiRequest(`/api/usb_storage?t=${timestamp}`)
        .then(response => response.json())
        .then(data => {
            updateUSBDevices(data);
            if (data && data.length > 0) {
                currentDirectory = data[0].mount_point;
                loadDirectoryContents(currentDirectory);
            } else {
                loadDirectoryContents('/media');
            }
        })
        .catch(error => {
            console.error('Error loading USB storage:', error);
            if (usbContainer) {
                usbContainer.innerHTML = '<p style="color: red;">Error loading USB devices. <button onclick="loadStorageData()">Retry</button></p>';
            }
        });
}

/**
 * Load contents of a specific directory
 */
function loadDirectoryContents(directory) {
    currentDirectory = directory;

    apiRequest('/api/browse_files', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: directory })
    })
        .then(response => response.json())
        .then(data => {
            updateFileBrowser(data.files || [], directory);
            updateBreadcrumbs(directory);
        })
        .catch(error => {
            console.error('Error loading directory contents:', error);
            const fileContainer = document.getElementById('fileList');
            if (fileContainer) {
                fileContainer.innerHTML = '<p style="color: red;">Error loading directory contents</p>';
            }
        });
}

/**
 * Update USB devices display
 */
function updateUSBDevices(devices) {
    const container = document.getElementById('usbDevices');
    if (!container) return;

    if (!devices || devices.length === 0) {
        container.innerHTML = '<p>No USB storage devices detected</p>';
        return;
    }

    let html = '';
    devices.forEach(device => {
        html += `
            <div class="usb-device">
                <h4>${device.device}</h4>
                <div class="device-info">
                    <span><strong>Size:</strong> ${device.size}</span>
                    <span><strong>Used:</strong> ${device.used}</span>
                    <span><strong>Available:</strong> ${device.available}</span>
                    <span><strong>Mount:</strong> ${device.mount_point}</span>
                </div>
            </div>
        `;
    });

    container.innerHTML = html;

    // Update save directory info
    const saveDirectoryElement = document.getElementById('saveDirectory');
    const availableSpaceElement = document.getElementById('availableSpace');

    if (devices.length > 0) {
        const primaryDevice = devices[0];
        if (saveDirectoryElement) {
            saveDirectoryElement.textContent = primaryDevice.mount_point;
        }
        if (availableSpaceElement) {
            availableSpaceElement.textContent = primaryDevice.available;
        }
    }
}


/**
 * Update breadcrumb navigation
 */
function updateBreadcrumbs(directory) {
    const breadcrumbContainer = document.getElementById('breadcrumbs');
    if (!breadcrumbContainer) return;

    let html = '<nav class="breadcrumbs">';

    // Split path and create breadcrumbs
    if (directory === '/media') {
        html += '<span class="breadcrumb-item active">USB Storage</span>';
    } else {
        html += '<a href="#" onclick="navigateToDirectory(\'/media\')" class="breadcrumb-item">USB Storage</a>';

        const relativePath = directory.replace('/media/', '');
        const parts = relativePath.split('/');
        let currentPath = '/media';

        for (let i = 0; i < parts.length; i++) {
            currentPath += '/' + parts[i];
            if (i === parts.length - 1) {
                html += ` > <span class="breadcrumb-item active">${parts[i]}</span>`;
            } else {
                html += ` > <a href="#" onclick="navigateToDirectory('${currentPath}')" class="breadcrumb-item">${parts[i]}</a>`;
            }
        }
    }

    html += '</nav>';
    breadcrumbContainer.innerHTML = html;
}

/**
 * Navigate to a specific directory
 */
function navigateToDirectory(directory) {
    loadDirectoryContents(directory);
}

/**
 * Update file browser display with directory support
 */
function updateFileBrowser(files, directory) {
    const container = document.getElementById('fileList');
    if (!container) return;

    if (!files || files.length === 0) {
        container.innerHTML = '<p>No files found in this directory.</p>';
        return;
    }

    let html = '<div class="file-browser">';

    // Add current directory info
    html += `<div class="directory-info">
        <strong>Current Directory:</strong> ${directory}
        <span class="item-count">(${files.length} items)</span>
    </div>`;

    files.forEach(file => {
        const isDirectory = file.is_directory;
        const icon = isDirectory ? '📁' : '📄';
        const sizeDisplay = isDirectory ? 'Directory' : file.size_formatted || formatFileSize(file.size);
        const isParent = file.is_parent || false;

        html += `
            <div class="file-item ${isDirectory ? 'directory' : 'file'}" ${isParent ? 'data-parent="true"' : ''}>
                <div class="file-info">
                    <span class="file-icon">${icon}</span>
                    <div class="file-details">
                        <strong class="file-name">${file.name}</strong><br>
                        <small class="file-meta">
                            Size: ${sizeDisplay}
                            ${file.modified ? ` | Modified: ${file.modified}` : ''}
                        </small>
                    </div>
                </div>
                <div class="file-actions">
                    ${isDirectory ? 
                        `<button onclick="navigateToDirectory('${file.path}')" class="btn-primary">Open</button>` :
                        `<button onclick="downloadFile('${file.path}')" class="btn-secondary">Download</button>`
                    }
                </div>
            </div>
        `;
    });

    html += '</div>';
    container.innerHTML = html;

    // Update total recordings count (only count non-directory files)
    const fileCount = files.filter(f => !f.is_directory && !f.is_parent).length;
    const totalRecordingsElement = document.getElementById('totalRecordings');
    if (totalRecordingsElement) {
        totalRecordingsElement.textContent = fileCount;
    }
}

/**
 * Format file size in human readable format
 */
function formatFileSize(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

/**
 * Download a specific file
 */
function downloadFile(filePath) {
    // Check if it's actually a file
    if (!filePath || filePath.endsWith('/')) {
        showNotification('Error', 'Invalid file path', 'error');
        return;
    }

    showNotification('Info', 'Starting download...', 'info');

    // Create a download link
    const link = document.createElement('a');
    link.href = `/api/download_file?path=${encodeURIComponent(filePath)}`;
    link.download = filePath.split('/').pop();
    link.style.display = 'none';

    // Add to DOM, click, and remove
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Success', `Download started: ${link.download}`, 'success');
}

/**
 * Download logs
 */
function downloadLogs() {
    showNotification('Info', 'Downloading logs...', 'info');

    const link = document.createElement('a');
    link.href = '/api/download_logs';
    link.download = `owl_logs_${new Date().toISOString().split('T')[0]}.zip`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    showNotification('Success', 'Log download started', 'success');
}

/**
 * Initialize GPS functionality
 */
function initGPS() {
    const toggle = document.getElementById('gps-toggle');
    if (toggle) {
        toggle.addEventListener('change', toggleGPS);
        if (toggle.checked) startGPS();
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

function updateSystemStats() {
    apiRequest('/api/system_stats')
        .then(response => response.json())
        .then(data => {
            // CPU Usage
            const cpuElement = document.getElementById('cpuUsage');
            if (cpuElement) {
                cpuElement.textContent = `${data.cpu_percent}%`;
                cpuElement.style.color = getColorForValue(data.cpu_percent, 100);
            }

            // CPU Temperature
            const tempElement = document.getElementById('cpuTemp');
            if (tempElement) {
                tempElement.textContent = `${data.cpu_temp}°C`;
                tempElement.style.color = getColorForValue(data.cpu_temp, 85);
            }

            // Memory Usage
            const memoryElement = document.getElementById('memoryUsage');
            if (memoryElement) {
                memoryElement.textContent = `${data.memory_percent}%`;
                memoryElement.style.color = getColorForValue(data.memory_percent, 100);
            }

            // Disk Usage
            const diskElement = document.getElementById('diskUsage');
            if (diskElement) {
                diskElement.textContent = `${data.disk_percent}%`;
                diskElement.style.color = getColorForValue(data.disk_percent, 100);
            }

            // Update detection status boxes with colors
            updateDetectionStatusBoxes(data);

            // Update OWL status indicator in header
            const statusDot = document.getElementById('statusDot');
            const statusText = document.getElementById('statusText');
            if (statusDot && statusText) {
                if (data.owl_running) {
                    statusDot.classList.add('connected');
                    statusText.textContent = 'OWL Running';
                } else {
                    statusDot.classList.remove('connected');
                    statusText.textContent = 'OWL Offline';
                }
            }

            // Update control status displays
            const controlDetectionStatus = document.getElementById('controlDetectionStatus');
            if (controlDetectionStatus) {
                controlDetectionStatus.textContent = data.detection_enable ? 'Enabled' : 'Disabled';
            }

            const controlRecordingStatus = document.getElementById('controlRecordingStatus');
            if (controlRecordingStatus) {
                controlRecordingStatus.textContent = data.image_sample_enable ? 'Recording' : 'Stopped';
            }

            const controlSensitivityStatus = document.getElementById('controlSensitivityStatus');
            if (controlSensitivityStatus) {
                controlSensitivityStatus.textContent = data.sensitivity_state ? 'Low' : 'High';
            }

            const timestampElement = document.getElementById('statusTimestamp');
            if (timestampElement) {
                timestampElement.textContent = data.timestamp || new Date().toLocaleString();
            }
        })
        .catch(error => console.error('Error fetching system stats:', error));
}

/**
 * Update the colored detection status boxes
 */
function updateDetectionStatusBoxes(data) {
    // Detection status
    const detectionBox = document.getElementById('detectionStatusBox');
    const detectionText = document.getElementById('detectionText');
    if (detectionBox && detectionText) {
        detectionText.textContent = data.detection_enable ? 'Enabled' : 'Disabled';
        detectionBox.className = 'detection-item ' + (data.detection_enable ? 'status-enabled' : 'status-disabled');
    }

    // Recording status
    const recordingBox = document.getElementById('recordingStatusBox');
    const recordingText = document.getElementById('recordingText');
    if (recordingBox && recordingText) {
        recordingText.textContent = data.image_sample_enable ? 'Recording' : 'Stopped';
        recordingBox.className = 'detection-item ' + (data.image_sample_enable ? 'status-active' : 'status-disabled');
    }

    // Sensitivity status
    const sensitivityBox = document.getElementById('sensitivityStatusBox');
    const sensitivityText = document.getElementById('sensitivityText');
    if (sensitivityBox && sensitivityText) {
        sensitivityText.textContent = data.sensitivity_state ? 'Low' : 'High';
        sensitivityBox.className = 'detection-item ' + (data.sensitivity_state ? 'status-low' : 'status-high');
    }
}

/**
 * Get color based on percentage value
 */
function getColorForValue(value, max) {
    const normalized = value / max;
    const hue = ((1 - normalized) * 120).toFixed(0);
    return `hsl(${hue}, 70%, 50%)`;
}

let notificationStore = [];
let notificationId = 0;

/**
 * Initialize notification system
 */
function initNotifications() {
    const bell = document.getElementById('notificationBell');
    const popup = document.getElementById('notificationPopup');
    const closeBtn = document.getElementById('closeNotificationPopup');
    const clearBtn = document.getElementById('clearAllNotifications');

    if (bell) {
        bell.addEventListener('click', toggleNotificationPopup);
    }

    if (closeBtn) {
        closeBtn.addEventListener('click', hideNotificationPopup);
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', clearAllNotifications);
    }
    if (popup) {
        popup.addEventListener('click', function(e) {
            if (e.target === popup) {
                hideNotificationPopup();
            }
        });
    }
}

/**
 * notification system with mobile-friendly popup
 */
function showNotification(title, message, type = 'info', duration = 5000, showToast = true) {
    const notification = {
        id: ++notificationId,
        title,
        message,
        type,
        timestamp: new Date(),
        time: new Date().toLocaleTimeString()
    };

    notificationStore.unshift(notification); // Add to beginning

    // Limit stored notifications
    if (notificationStore.length > 50) {
        notificationStore = notificationStore.slice(0, 50);
    }

    // Update notification count
    updateNotificationCount();

    updateNotificationPopup();

    if (showToast && (type === 'success' || type === 'error')) {
        showQuickToast(message, type, 2000);
    }

    // Auto-clear success/info notifications after duration
    if (type === 'success' || type === 'info') {
        setTimeout(() => {
            removeNotification(notification.id);
        }, duration);
    }

    // Shake bell for new notifications
    const bell = document.getElementById('notificationBell');
    if (bell) {
        bell.classList.add('has-notifications');
        setTimeout(() => bell.classList.remove('has-notifications'), 500);
    }
}

/**
 * Show quick toast for immediate feedback
 */
function showQuickToast(message, type = 'info', duration = 2000) {
    const toast = document.getElementById('quickToast');
    if (!toast) return;

    toast.textContent = message;
    toast.className = `quick-toast ${type}`;
    toast.classList.remove('hidden');

    setTimeout(() => {
        toast.classList.add('hidden');
    }, duration);
}

/**
 * Toggle notification popup
 */
function toggleNotificationPopup() {
    const popup = document.getElementById('notificationPopup');
    if (!popup) return;

    if (popup.classList.contains('hidden')) {
        showNotificationPopup();
    } else {
        hideNotificationPopup();
    }
}

/**
 * Show notification popup
 */
function showNotificationPopup() {
    const popup = document.getElementById('notificationPopup');
    if (!popup) return;

    popup.classList.remove('hidden');
    updateNotificationPopup();

    // Reset notification count visual (user has seen them)
    setTimeout(() => {
        const countEl = document.getElementById('notificationCount');
        if (countEl && !countEl.classList.contains('hidden')) {
            countEl.style.animation = 'none';
        }
    }, 1000);
}

/**
 * Hide notification popup
 */
function hideNotificationPopup() {
    const popup = document.getElementById('notificationPopup');
    if (!popup) return;

    popup.classList.add('hidden');
}

/**
 * Update notification count badge
 */
function updateNotificationCount() {
    const countEl = document.getElementById('notificationCount');
    if (!countEl) return;

    const count = notificationStore.length;

    if (count > 0) {
        countEl.textContent = count > 99 ? '99+' : count;
        countEl.classList.remove('hidden');
    } else {
        countEl.classList.add('hidden');
    }
}

/**
 * Update notification popup content
 */
function updateNotificationPopup() {
    const listEl = document.getElementById('notificationList');
    if (!listEl) return;

    if (notificationStore.length === 0) {
        listEl.innerHTML = '<p class="no-notifications">No notifications</p>';
        return;
    }

    let html = '';
    notificationStore.forEach(notification => {
        const iconSymbol = {
            'success': '✓',
            'error': '✕',
            'warning': '⚠',
            'info': 'i'
        }[notification.type] || 'i';

        html += `
            <div class="notification-item" data-id="${notification.id}">
                <div class="notification-icon ${notification.type}">
                    ${iconSymbol}
                </div>
                <div class="notification-content">
                    <div class="notification-title">${notification.title}</div>
                    <div class="notification-message">${notification.message}</div>
                    <div class="notification-time">${notification.time}</div>
                </div>
            </div>
        `;
    });

    listEl.innerHTML = html;
}

/**
 * Remove specific notification
 */
function removeNotification(id) {
    notificationStore = notificationStore.filter(n => n.id !== id);
    updateNotificationCount();
    updateNotificationPopup();
}

/**
 * Clear all notifications
 */
function clearAllNotifications() {
    notificationStore = [];
    updateNotificationCount();
    updateNotificationPopup();

    showQuickToast('All notifications cleared', 'info', 1500);
}

let uploadProgressInterval = null;
let selectedUploadDirectory = null;
let uploadInProgress = false;

/**
 * Initialize upload tab functionality
 */
function initUploadTab() {
    // Connection check
    const checkConnectionBtn = document.getElementById('checkConnection');
    if (checkConnectionBtn) {
        checkConnectionBtn.addEventListener('click', checkEthernetConnection);
    }

    // Credentials testing
    const testCredentialsBtn = document.getElementById('testCredentials');
    if (testCredentialsBtn) {
        testCredentialsBtn.addEventListener('click', testS3Credentials);
    }

    // Directory browsing
    const browseDirectoriesBtn = document.getElementById('browseDirectories');
    if (browseDirectoriesBtn) {
        browseDirectoriesBtn.addEventListener('click', showDirectoryBrowser);
    }

    const scanDirectoryBtn = document.getElementById('scanDirectory');
    if (scanDirectoryBtn) {
        scanDirectoryBtn.addEventListener('click', scanSelectedDirectory);
    }

    // Upload controls
    const startUploadBtn = document.getElementById('startUpload');
    if (startUploadBtn) {
        startUploadBtn.addEventListener('click', startUploadProcess);
    }

    const stopUploadBtn = document.getElementById('stopUpload');
    if (stopUploadBtn) {
        stopUploadBtn.addEventListener('click', stopUploadProcess);
    }
     // Key file management
    const findKeyFilesBtn = document.getElementById('findKeyFiles');
    if (findKeyFilesBtn) {
        findKeyFilesBtn.addEventListener('click', findKeyFilesOnUSB);
    }
    // Auto-fill Hetzner endpoint when region is selected
    const regionSelect = document.getElementById('region');
    const endpointInput = document.getElementById('endpointUrl');
    if (regionSelect && endpointInput) {
    regionSelect.addEventListener('change', function() {
        if (this.value === 'fsn1') {
            endpointInput.value = 'https://fsn1.your-objectstorage.com';
        } else if (this.value === 'hel1') {
            endpointInput.value = 'https://hel1.your-objectstorage.com';
        } else if (this.value === 'nbg1') {
            endpointInput.value = 'https://nbg1.your-objectstorage.com';
        } else {
            endpointInput.value = '';
        }
    });
}

    // Set today's date as default
    const dateInput = document.getElementById('metadataDate');
    if (dateInput) {
        dateInput.value = new Date().toISOString().split('T')[0];
    }
}

/**
 * Find credential files on USB drives
 */
function findKeyFilesOnUSB() {
    const button = document.getElementById('findKeyFiles');
    const listElement = document.getElementById('keyFilesList');

    button.disabled = true;
    button.textContent = 'Searching...';
    listElement.classList.add('hidden');

    apiRequest('/api/upload/find_key_files')
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                displayKeyFiles(data.key_files);
                if (data.key_files.length === 0) {
                    showNotification('Info', 'No credential files found on USB drives', 'info');
                } else {
                    showNotification('Success', `Found ${data.key_files.length} credential file(s)`, 'success');
                }
            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to search for key files: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Find Key Files on USB';
        });
}

/**
 * Display found key files
 */
function displayKeyFiles(keyFiles) {
    const listElement = document.getElementById('keyFilesList');

    if (!keyFiles || keyFiles.length === 0) {
        listElement.innerHTML = '<p>No credential files found. Make sure your USB drive has a "secret_key" folder with credential files.</p>';
        listElement.classList.remove('hidden');
        return;
    }

    let html = '<div class="key-files-container">';
    html += '<h5>Found Credential Files:</h5>';

    keyFiles.forEach(file => {
        html += `
            <div class="key-file-item">
                <div class="key-file-info">
                    <div class="key-file-name">${file.name}</div>
                    <div class="key-file-meta">
                        USB: ${file.usb_device} | Size: ${formatFileSize(file.size)} | Modified: ${file.modified}
                    </div>
                </div>
                <div class="key-file-actions">
                    <button onclick="loadCredentialsFromFile('${file.path}')" class="btn-primary">
                        Load Credentials
                    </button>
                </div>
            </div>
        `;
    });

    html += '</div>';
    listElement.innerHTML = html;
    listElement.classList.remove('hidden');
}


/**
 * Load credentials from selected file
 */
function loadCredentialsFromFile(filePath) {
    showNotification('Info', 'Loading credentials...', 'info');

    apiRequest('/api/upload/load_credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: filePath })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                const creds = data.credentials;

                // Fill in the form fields
                document.getElementById('accessKey').value = creds.access_key || '';
                document.getElementById('secretKey').value = creds.secret_key || '';
                document.getElementById('bucketName').value = creds.bucket_name || '';
                document.getElementById('region').value = creds.region || 'us-east-1';

                // Set endpoint URL if provided
                if (creds.endpoint_url) {
                    document.getElementById('endpointUrl').value = creds.endpoint_url;
                } else {
                    // Auto-set Hetzner endpoints
                    const regionSelect = document.getElementById('region');
                    if (regionSelect) {
                        regionSelect.dispatchEvent(new Event('change'));
                    }
                }

                showNotification('Success', 'Credentials loaded successfully', 'success');

                // Enable test credentials button
                const testBtn = document.getElementById('testCredentials');
                if (testBtn) {
                    testBtn.disabled = false;
                }

            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to load credentials: ' + error.message, 'error');
        });
}

/**
 * Create metadata file before upload
 */
function createMetadataFile(uploadDirectory) {
    const metadata = {
        name: document.getElementById('metadataName').value.trim(),
        date: document.getElementById('metadataDate').value,
        location: document.getElementById('metadataLocation').value.trim(),
        field: document.getElementById('metadataField').value.trim(),
        weather: document.getElementById('metadataWeather').value.trim(),
        crop: document.getElementById('metadataCrop').value.trim(),
        expected_weeds: document.getElementById('metadataWeeds').value.trim(),
        notes: document.getElementById('metadataNotes').value.trim()
    };

    return apiRequest('/api/upload/create_metadata', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            metadata: metadata,
            upload_directory: uploadDirectory
        })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                console.log('Metadata file created:', data.metadata_file);
                return data;
            } else {
                throw new Error(data.error);
            }
        });
}

/**
 * Enhanced start upload with metadata
 */
function startUploadProcess() {
    // Validate all requirements
    const accessKey = document.getElementById('accessKey').value.trim();
    const secretKey = document.getElementById('secretKey').value.trim();
    const bucketName = document.getElementById('bucketName').value.trim();
    const region = document.getElementById('region').value;
    const s3Prefix = document.getElementById('s3Prefix').value.trim();
    const endpointUrl = document.getElementById('endpointUrl').value.trim() || null;

    if (!accessKey || !secretKey || !bucketName || !selectedUploadDirectory) {
        showNotification('Error', 'Please complete all required fields and select a directory', 'error');
        return;
    }

    // Create metadata file first
    createMetadataFile(selectedUploadDirectory)
        .then(() => {
            showNotification('Info', 'Metadata file created, starting upload...', 'info');

            const uploadData = {
                directory_path: selectedUploadDirectory,
                access_key: accessKey,
                secret_key: secretKey,
                bucket_name: bucketName,
                s3_prefix: s3Prefix,
                region: region,
                endpoint_url: endpointUrl,
                max_workers: 4
            };

            // Start upload
            return apiRequest('/api/upload/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(uploadData)
            });
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                uploadInProgress = true;
                showUploadProgress();
                startUploadProgressMonitoring();

                // Update UI
                document.getElementById('startUpload').disabled = true;
                document.getElementById('stopUpload').disabled = false;

                showNotification('Success', 'Upload started successfully with metadata', 'success');
            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to start upload: ' + error.message, 'error');
        });
}

/**
 * Check ethernet connection
 */
function checkEthernetConnection() {
    const button = document.getElementById('checkConnection');
    const statusElement = document.getElementById('connectionStatus');
    const textElement = document.getElementById('connectionText');
    const indicatorElement = document.getElementById('connectionIndicator');

    // Update UI to show checking state
    button.disabled = true;
    button.textContent = 'Checking...';
    statusElement.className = 'connection-status checking';
    indicatorElement.textContent = '⏳';
    textElement.textContent = 'Checking connection...';

    apiRequest('/api/upload/check_ethernet', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.connected) {
                statusElement.className = 'connection-status connected';
                indicatorElement.textContent = '✅';
                textElement.textContent = 'Ethernet connected';
                showNotification('Success', 'Ethernet connection verified', 'success');
            } else {
                statusElement.className = 'connection-status disconnected';
                indicatorElement.textContent = '❌';
                textElement.textContent = data.error || 'No ethernet connection';
                showNotification('Warning', data.error || 'No ethernet connection', 'warning');
            }
        })
        .catch(error => {
            statusElement.className = 'connection-status disconnected';
            indicatorElement.textContent = '❌';
            textElement.textContent = 'Connection check failed';
            showNotification('Error', 'Failed to check connection: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Check Ethernet Connection';
        });
}

/**
 * Test S3 credentials
 */
function testS3Credentials() {
    const button = document.getElementById('testCredentials');
    const statusElement = document.getElementById('credentialsStatus');

    // Get form values
    const accessKey = document.getElementById('accessKey').value.trim();
    const secretKey = document.getElementById('secretKey').value.trim();
    const bucketName = document.getElementById('bucketName').value.trim();
    const region = document.getElementById('region').value;
    const endpointUrl = document.getElementById('endpointUrl').value.trim() || null;

    // Validate required fields
    if (!accessKey || !secretKey || !bucketName) {
        statusElement.className = 'credentials-status invalid';
        statusElement.textContent = 'Please fill in all required fields';
        return;
    }

    // Update UI
    button.disabled = true;
    button.textContent = 'Testing...';
    statusElement.className = 'credentials-status testing';
    statusElement.textContent = 'Testing credentials...';

    const requestData = {
        access_key: accessKey,
        secret_key: secretKey,
        bucket_name: bucketName,
        region: region,
        endpoint_url: endpointUrl
    };

    apiRequest('/api/upload/test_credentials', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestData)
    })
        .then(response => response.json())
        .then(data => {
            if (data.valid) {
                statusElement.className = 'credentials-status valid';
                statusElement.textContent = '✅ Credentials valid - bucket accessible';
                showNotification('Success', 'S3 credentials verified successfully', 'success');

                // Enable directory browsing
                document.getElementById('browseDirectories').disabled = false;
            } else {
                statusElement.className = 'credentials-status invalid';
                statusElement.textContent = '❌ ' + data.error;
                showNotification('Error', 'Credential test failed: ' + data.error, 'error');
            }
        })
        .catch(error => {
            statusElement.className = 'credentials-status invalid';
            statusElement.textContent = '❌ Test failed: ' + error.message;
            showNotification('Error', 'Failed to test credentials: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Test Credentials';
        });
}

/**
 * Show directory browser
 */
function showDirectoryBrowser() {
    const browserElement = document.getElementById('directoryBrowser');
    browserElement.classList.remove('hidden');
    loadUploadDirectories('/media');
}

/**
 * Load directories for upload selection
 */
function loadUploadDirectories(directory) {
    const contentsElement = document.getElementById('directoryContents');
    contentsElement.innerHTML = '<p>Loading directories...</p>';

    apiRequest('/api/upload/browse_directories', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory: directory })
    })
        .then(response => response.json())
        .then(data => {
            updateDirectoryBrowser(data.directories || [], directory);
            updateUploadBreadcrumbs(directory);
        })
        .catch(error => {
            console.error('Error loading directories:', error);
            contentsElement.innerHTML = '<p style="color: red;">Error loading directories</p>';
        });
}

/**
 * Update directory browser display
 */
function updateDirectoryBrowser(directories, currentPath) {
    const contentsElement = document.getElementById('directoryContents');

    if (!directories || directories.length === 0) {
        contentsElement.innerHTML = '<p>No directories found.</p>';
        return;
    }

    let html = '<div class="directories-list">';

    directories.forEach(dir => {
        const isParent = dir.is_parent || false;
        const iconClass = isParent ? 'parent' : '';

        html += `
            <div class="directory-item ${iconClass}" onclick="selectUploadDirectory('${dir.path}', '${dir.name}', ${isParent})">
                <span class="directory-icon">${isParent ? '⬅️' : '📁'}</span>
                <div class="directory-info">
                    <div class="directory-name">${dir.name}</div>
                    ${dir.modified ? `<div class="directory-meta">Modified: ${dir.modified}</div>` : ''}
                </div>
                <div class="directory-actions">
                    ${!isParent ? '<button class="btn-primary" onclick="event.stopPropagation(); chooseUploadDirectory(\'' + dir.path + '\')">Select</button>' : ''}
                </div>
            </div>
        `;
    });

    html += '</div>';
    contentsElement.innerHTML = html;
}

/**
 * Navigate to directory (for browsing)
 */
function selectUploadDirectory(path, name, isParent) {
    if (isParent) {
        loadUploadDirectories(path);
    } else {
        // Just navigate into the directory
        loadUploadDirectories(path);
    }
}

/**
 * Choose directory for upload
 */
function chooseUploadDirectory(path) {
    selectedUploadDirectory = path;
    document.getElementById('selectedDirectory').textContent = path;
    document.getElementById('scanDirectory').disabled = false;

    // Hide browser and show selection
    document.getElementById('directoryBrowser').classList.add('hidden');
    showNotification('Info', `Selected directory: ${path}`, 'info');
}

/**
 * Update breadcrumbs for upload directory browser
 */
function updateUploadBreadcrumbs(directory) {
    const breadcrumbContainer = document.getElementById('directoryBreadcrumbs');
    if (!breadcrumbContainer) return;

    let html = '<nav class="breadcrumbs">';

    if (directory === '/media') {
        html += '<span class="breadcrumb-item active">USB Storage</span>';
    } else {
        html += '<a href="#" onclick="loadUploadDirectories(\'/media\')" class="breadcrumb-item">USB Storage</a>';

        const relativePath = directory.replace('/media/', '');
        const parts = relativePath.split('/');
        let currentPath = '/media';

        for (let i = 0; i < parts.length; i++) {
            currentPath += '/' + parts[i];
            if (i === parts.length - 1) {
                html += ` > <span class="breadcrumb-item active">${parts[i]}</span>`;
            } else {
                html += ` > <a href="#" onclick="loadUploadDirectories('${currentPath}')" class="breadcrumb-item">${parts[i]}</a>`;
            }
        }
    }

    html += '</nav>';
    breadcrumbContainer.innerHTML = html;
}

/**
 * Scan selected directory
 */
function scanSelectedDirectory() {
    if (!selectedUploadDirectory) {
        showNotification('Warning', 'Please select a directory first', 'warning');
        return;
    }

    const button = document.getElementById('scanDirectory');
    const resultsElement = document.getElementById('scanResults');

    button.disabled = true;
    button.textContent = 'Scanning...';
    resultsElement.classList.add('hidden');

    apiRequest('/api/upload/scan_directory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ directory_path: selectedUploadDirectory })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Update scan results
                document.getElementById('scanFileCount').textContent = data.file_count.toLocaleString();
                document.getElementById('scanTotalSize').textContent = data.total_size_formatted;

                resultsElement.classList.remove('hidden');

                // Enable upload if files found
                if (data.file_count > 0) {
                    document.getElementById('startUpload').disabled = false;
                    showNotification('Success', `Found ${data.file_count} files (${data.total_size_formatted})`, 'success');
                } else {
                    showNotification('Info', 'No files found in selected directory', 'info');
                }
            } else {
                throw new Error(data.error);
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to scan directory: ' + error.message, 'error');
        })
        .finally(() => {
            button.disabled = false;
            button.textContent = 'Scan Selected Directory';
        });
}

/**
 * Stop upload process
 */
function stopUploadProcess() {
    apiRequest('/api/upload/stop', { method: 'POST' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                showNotification('Info', 'Upload stopped', 'info');
            }
        })
        .catch(error => {
            showNotification('Error', 'Failed to stop upload: ' + error.message, 'error');
        });
}

/**
 * Show upload progress UI
 */
function showUploadProgress() {
    document.getElementById('uploadProgress').classList.remove('hidden');
    document.getElementById('uploadResults').classList.add('hidden');
}

/**
 * Start monitoring upload progress
 */
function startUploadProgressMonitoring() {
    if (uploadProgressInterval) {
        clearInterval(uploadProgressInterval);
    }

    uploadProgressInterval = setInterval(() => {
        if (!uploadInProgress) return;

        apiRequest('/api/upload/progress')
            .then(response => response.json())
            .then(data => {
                updateUploadProgress(data);

                // Check if upload is complete
                if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
                    uploadInProgress = false;
                    clearInterval(uploadProgressInterval);
                    showUploadComplete(data);
                }
            })
            .catch(error => {
                console.error('Error getting upload progress:', error);
            });
    }, 1000); // Update every second
}

/**
 * Update upload progress display
 */
function updateUploadProgress(data) {
    // Update status
    document.getElementById('uploadStatus').textContent = data.status.charAt(0).toUpperCase() + data.status.slice(1);

    // Update progress bar
    const progressPercent = Math.round(data.progress_percent || 0);
    document.getElementById('overallProgressBar').style.width = progressPercent + '%';
    document.getElementById('overallProgressText').textContent = progressPercent + '%';

    // Update file progress
    document.getElementById('fileProgressText').textContent = `${data.completed_files} / ${data.total_files}`;

    // Update stats
    document.getElementById('uploadSpeed').textContent = `${data.speed_mbps.toFixed(1)} MB/s`;
    document.getElementById('uploadETA').textContent = formatTime(data.eta_seconds);
    document.getElementById('currentFile').textContent = data.current_file || '--';
}

/**
 * Show upload complete results
 */
function showUploadComplete(data) {
    // Hide progress, show results
    document.getElementById('uploadProgress').classList.add('hidden');
    document.getElementById('uploadResults').classList.remove('hidden');

    // Update results
    const successCount = data.completed_files - data.failed_files;
    document.getElementById('successCount').textContent = successCount;
    document.getElementById('failedCount').textContent = data.failed_files;
    document.getElementById('totalTime').textContent = formatTime(data.elapsed_seconds);

    // Update UI buttons
    document.getElementById('startUpload').disabled = false;
    document.getElementById('stopUpload').disabled = true;

    // Show notification
    if (data.status === 'completed') {
        showNotification('Success', `Upload completed! ${successCount} files uploaded successfully`, 'success', 10000);
    } else if (data.status === 'failed') {
        showNotification('Error', `Upload failed: ${data.error_message}`, 'error', 10000);
    } else if (data.status === 'cancelled') {
        showNotification('Info', 'Upload was cancelled', 'info');
    } else {
        showNotification('Warning', `Upload completed with ${data.failed_files} failures`, 'warning', 8000);
    }
}

/**
 * Format time in seconds to human readable format
 */
function formatTime(seconds) {
    if (!seconds || seconds <= 0) return '--';

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);

    if (hours > 0) {
        return `${hours}h ${minutes}m ${secs}s`;
    } else if (minutes > 0) {
        return `${minutes}m ${secs}s`;
    } else {
        return `${secs}s`;
    }
}


// Add CSS for the spinner and USB devices
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

.usb-device {
    background: #f8f9fa;
    padding: 1rem;
    border-radius: 8px;
    margin-bottom: 1rem;
    border: 1px solid #e9ecef;
}

.usb-device h4 {
    margin: 0 0 0.5rem 0;
    color: var(--primary);
}

.device-info {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.5rem;
    font-size: 0.9rem;
}

.file-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem;
    border-bottom: 1px solid #e9ecef;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.2s;
}

.file-item:hover {
    background-color: #f8f9fa;
}

.file-info {
    flex: 1;
}

.file-item button {
    margin-left: 1rem;
    padding: 0.25rem 0.75rem;
    font-size: 0.8rem;
}
`;
document.head.appendChild(style);

// Add CSS for better file browser styling
const fileBrowserStyle = document.createElement('style');
fileBrowserStyle.textContent = `
.file-browser {
    margin-top: 1rem;
}

.directory-info {
    padding: 0.5rem;
    background: #f8f9fa;
    border-radius: 4px;
    margin-bottom: 1rem;
    font-size: 0.9rem;
}

.item-count {
    color: #666;
    margin-left: 0.5rem;
}

.breadcrumbs {
    margin-bottom: 1rem;
    padding: 0.5rem;
    background: #e9ecef;
    border-radius: 4px;
    font-size: 0.9rem;
}

.breadcrumb-item {
    color: #007bff;
    text-decoration: none;
    cursor: pointer;
}

.breadcrumb-item:hover {
    text-decoration: underline;
}

.breadcrumb-item.active {
    color: #6c757d;
    font-weight: bold;
}

.file-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.75rem;
    border-bottom: 1px solid #e9ecef;
    border-radius: 4px;
    transition: all 0.2s;
}

.file-item:hover {
    background-color: #f8f9fa;
}

.file-item.directory {
    background-color: #f0f8ff;
}

.file-item[data-parent="true"] {
    background-color: #fff3cd;
    border: 1px solid #ffeaa7;
}

.file-info {
    display: flex;
    align-items: center;
    flex: 1;
}

.file-icon {
    font-size: 1.5rem;
    margin-right: 0.75rem;
}

.file-details {
    flex: 1;
}

.file-name {
    font-size: 1rem;
    color: #333;
}

.file-meta {
    color: #666;
    font-size: 0.8rem;
}

.file-actions {
    margin-left: 1rem;
}

.file-actions button {
    padding: 0.25rem 0.75rem;
    font-size: 0.8rem;
    min-width: 80px;
}
`;

// Add the CSS to the document
if (!document.getElementById('file-browser-styles')) {
    fileBrowserStyle.id = 'file-browser-styles';
    document.head.appendChild(fileBrowserStyle);
}

// Cleanup on unload
window.addEventListener('unload', () => {
    if (gpsWatchId !== null) navigator.geolocation.clearWatch(gpsWatchId);
    if (updateInterval) clearInterval(updateInterval);

    // Abort any pending requests
    Object.values(pendingRequests).forEach(controller => {
        try { controller.abort(); } catch (e) {}
    });
});