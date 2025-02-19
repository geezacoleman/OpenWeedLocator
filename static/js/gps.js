let gpsWatchId = null;
let isGpsEnabled = true;

function initGPS() {
    const toggle = document.getElementById('gps-toggle');
    toggle.addEventListener('change', toggleGPS);

    // Initialize GPS if enabled
    if (toggle.checked) {
        startGPS();
    }
}

function startGPS() {
    if (!('geolocation' in navigator)) {
        updateGPSStatus(null, 'GPS not available');
        return;
    }

    const options = {
        enableHighAccuracy: true,
        timeout: 5000,
        maximumAge: 0
    };

    gpsWatchId = navigator.geolocation.watchPosition(
        handleGPSSuccess,
        handleGPSError,
        options
    );
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
    if (isGpsEnabled) {
        startGPS();
    } else {
        stopGPS();
    }
}

function handleGPSSuccess(position) {
    const gpsData = {
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
        accuracy: position.coords.accuracy,
        timestamp: new Date().toISOString()
    };

    updateGPSStatus(gpsData.accuracy);

    // Send to server
    fetch('/update_gps', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
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

    if (!isGpsEnabled) {
        iconElement.closest('.gps-status').classList.add('gps-disabled');
        textElement.textContent = 'GPS disabled';
        return;
    }

    iconElement.closest('.gps-status').classList.remove('gps-disabled');

    if (errorMessage) {
        iconElement.classList.remove('gps-good', 'gps-medium', 'gps-poor');
        iconElement.classList.add('gps-poor');
        textElement.textContent = errorMessage;
        return;
    }

    if (accuracy === null) {
        iconElement.classList.remove('gps-good', 'gps-medium', 'gps-poor');
        iconElement.classList.add('gps-poor');
        textElement.textContent = 'Searching...';
        return;
    }

    // Update icon based on accuracy
    iconElement.classList.remove('gps-good', 'gps-medium', 'gps-poor');
    if (accuracy <= 5) {
        iconElement.classList.add('gps-good');
    } else if (accuracy <= 10) {
        iconElement.classList.add('gps-medium');
    } else {
        iconElement.classList.add('gps-poor');
    }

    textElement.textContent = `±${accuracy.toFixed(1)}m`;
}

// Clean up on page unload
window.addEventListener('unload', () => {
    if (gpsWatchId !== null) {
        navigator.geolocation.clearWatch(gpsWatchId);
    }
});

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', initGPS);