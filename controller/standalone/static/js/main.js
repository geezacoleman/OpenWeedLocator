/* ==========================================================================
   OWL Dashboard - Main Entry Point

   Load order (all modules must be loaded before this file):
   1. _core.js       - Global state, API helper, utilities
   2. _notifications.js - Notification system (required by all modules)
   3. _controls.js   - Dashboard controls, power, detection, recording, preview
   4. _storage.js    - File browser, USB devices
   5. _stats.js      - System status polling
   6. _ai_tab.js     - AI model selection and class filtering
   7. _config_tab.js  - Config tab range sliders
   8. _config.js     - Configuration editor (INI)
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
            } else if (targetId === 'ai') {
                refreshAITab();
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
    initControlButtons();
    initStorageTab();
    initPreview();
    initGPS();
    initNotifications();
    initHardwareControllerCheck();
    initDashboardControls();
    initConfigEditor();
    initSliders();

    // Initialize on-screen numpad for kiosk
    if (typeof Numpad !== 'undefined') Numpad.init();

    // Initialize widget system
    if (typeof initWidgets === 'function') initWidgets();

    // Initialize agent tab
    if (typeof initAgent === 'function') initAgent();

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
