/* ==========================================================================
   OWL Dashboard - Main Entry Point

   Load order (all modules must be loaded before this file):
   1. _core.js       - Global state, API helper, utilities
   2. _notifications.js - Notification system (required by all modules)
   3. _stream.js     - Video stream, zoom, GPS
   4. _controls.js   - Dashboard controls, power, detection, recording
   5. _storage.js    - File browser, USB devices
   6. _stats.js      - System status polling
   7. _upload.js     - S3 upload system
   8. _config.js     - Configuration editor
   9. main.js        - This file (initialization)
   ========================================================================== */

/**
 * Initialize tab navigation
 */
function initTabs() {
    const tabLinks = document.querySelectorAll('.nav-tab');
    tabLinks.forEach(tab => {
        tab.addEventListener('click', function(e) {
            if (this.classList.contains('disabled')) {
                e.preventDefault();
                e.stopPropagation();
                return;
            }

            tabLinks.forEach(t => t.classList.remove('active'));
            this.classList.add('active');

            const tabContents = document.querySelectorAll('.tab-content');
            tabContents.forEach(content => content.classList.remove('active'));

            const targetId = this.getAttribute('data-tab');
            document.getElementById(targetId).classList.add('active');

            // Reload data when tabs are clicked
            if (targetId === 'storage') {
                document.getElementById('usbDevices').innerHTML = '<p>Scanning for USB devices...</p>';
                document.getElementById('fileList').innerHTML = '<p>Click Refresh to browse recordings</p>';
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
 * Main initialization on DOM ready
 */
document.addEventListener('DOMContentLoaded', function() {
    // Initialize all modules
    initTabs();
    initZoom();
    initGPS();
    initControlButtons();
    initStorageTab();
    initVideoStream();
    initUploadTab();
    initNotifications();
    initHardwareControllerCheck();
    initDashboardControls();
    initConfigEditor();

    // Fullscreen button
    const fullscreenBtn = document.getElementById('fullscreenBtn');
    if (fullscreenBtn) {
        fullscreenBtn.addEventListener('click', toggleFullscreen);
    }

    // Start system stats polling
    startUpdateInterval();
});

/**
 * Cleanup on page unload
 */
window.addEventListener('unload', () => {
    if (gpsWatchId !== null) {
        navigator.geolocation.clearWatch(gpsWatchId);
    }
    if (updateInterval) {
        clearInterval(updateInterval);
    }

    // Abort any pending requests
    Object.values(pendingRequests).forEach(controller => {
        try { controller.abort(); } catch (e) {}
    });
});