/* ==========================================================================
   OWL Dashboard - Stream Module
   Video stream, zoom controls, GPS tracking
   ========================================================================== */

/**
 * Initialize video stream
 */
function initVideoStream() {
    const streamImg = document.getElementById('stream-img');
    if (!streamImg) return;

    setupVideoStream();
    streamImg.addEventListener('load', handleStreamLoad);
    streamImg.addEventListener('error', handleStreamError);
}

/**
 * Setup video stream with cache busting
 */
function setupVideoStream() {
    const streamImg = document.getElementById('stream-img');
    if (!streamImg) return;

    const timestamp = new Date().getTime();
    streamImg.src = `/video_feed?t=${timestamp}`;
}

/**
 * Handle successful stream load
 */
function handleStreamLoad() {
    updateStreamStatus('Connected', 'success');
}

/**
 * Handle stream errors with retry logic
 */
function handleStreamError(error) {
    updateStreamStatus('Connection failed - retrying...', 'error');

    setTimeout(() => {
        setupVideoStream();
    }, 3000);
}

/**
 * Update stream connection status
 */
function updateStreamStatus(message, type) {
    if (type === 'error') {
        showNotification('Stream Error', message, 'warning', 3000);
    }
}

/**
 * Refresh video stream manually
 */
function refreshVideoStream() {
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

/* --------------------------------------------------------------------------
   Zoom Functions
   -------------------------------------------------------------------------- */

/**
 * Initialize zoom functionality
 */
function initZoom() {
    document.getElementById('zoomIn')?.addEventListener('click', zoomIn);
    document.getElementById('zoomOut')?.addEventListener('click', zoomOut);
    document.getElementById('zoomReset')?.addEventListener('click', resetZoom);
}

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

/* --------------------------------------------------------------------------
   GPS Functions
   -------------------------------------------------------------------------- */

/**
 * Initialize GPS functionality
 */
function initGPS() {
    const gpsToggle = document.getElementById('gpsToggle');
    if (gpsToggle) {
        gpsToggle.addEventListener('change', toggleGPS);
        if (gpsToggle.checked) {
            startGPS();
        }
    }
}

/**
 * Toggle GPS tracking
 */
function toggleGPS() {
    const gpsToggle = document.getElementById('gpsToggle');
    if (gpsToggle && gpsToggle.checked) {
        startGPS();
    } else {
        stopGPS();
    }
}

/**
 * Start GPS tracking
 */
function startGPS() {
    if (!navigator.geolocation) {
        showNotification('GPS Error', 'Geolocation not supported', 'error');
        return;
    }

    isGpsEnabled = true;

    gpsWatchId = navigator.geolocation.watchPosition(
        handleGPSSuccess,
        handleGPSError,
        {
            enableHighAccuracy: true,
            timeout: 10000,
            maximumAge: 5000
        }
    );

    updateGPSStatus();
}

/**
 * Stop GPS tracking
 */
function stopGPS() {
    isGpsEnabled = false;

    if (gpsWatchId !== null) {
        navigator.geolocation.clearWatch(gpsWatchId);
        gpsWatchId = null;
    }

    gpsData = null;
    updateGPSStatus();
}

/**
 * Handle GPS position update
 */
function handleGPSSuccess(position) {
    gpsData = {
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
        accuracy: position.coords.accuracy,
        speed: position.coords.speed,
        heading: position.coords.heading,
        timestamp: position.timestamp
    };

    updateGPSStatus();

    // Send to server
    apiRequest('/api/update_gps', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(gpsData)
    }).catch(err => {
        // Silent fail for GPS updates
    });
}

/**
 * Handle GPS error
 */
function handleGPSError(error) {
    let message = 'GPS error';
    switch (error.code) {
        case error.PERMISSION_DENIED:
            message = 'GPS permission denied';
            break;
        case error.POSITION_UNAVAILABLE:
            message = 'GPS position unavailable';
            break;
        case error.TIMEOUT:
            message = 'GPS timeout';
            break;
    }

    const gpsStatus = document.querySelector('.gps-status');
    if (gpsStatus) {
        gpsStatus.classList.add('gps-disabled');
    }
}

/**
 * Update GPS status display
 */
function updateGPSStatus() {
    const gpsStatus = document.querySelector('.gps-status');
    const gpsIcon = document.querySelector('.gps-icon');
    const gpsAccuracy = document.getElementById('gps-accuracy');

    if (!gpsStatus) return;

    if (!isGpsEnabled || !gpsData) {
        gpsStatus.classList.add('gps-disabled');
        if (gpsIcon) {
            gpsIcon.classList.remove('gps-good', 'gps-medium', 'gps-poor');
        }
        if (gpsAccuracy) {
            gpsAccuracy.textContent = isGpsEnabled ? 'Acquiring...' : 'Off';
        }
        return;
    }

    gpsStatus.classList.remove('gps-disabled');

    // Update accuracy display
    if (gpsAccuracy) {
        gpsAccuracy.textContent = `±${Math.round(gpsData.accuracy)}m`;
    }

    // Update icon color based on accuracy
    if (gpsIcon) {
        gpsIcon.classList.remove('gps-good', 'gps-medium', 'gps-poor');
        if (gpsData.accuracy <= 10) {
            gpsIcon.classList.add('gps-good');
        } else if (gpsData.accuracy <= 30) {
            gpsIcon.classList.add('gps-medium');
        } else {
            gpsIcon.classList.add('gps-poor');
        }
    }
}
