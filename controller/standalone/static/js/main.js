/* ==========================================================================
   OWL Dashboard - Main Entry Point

   Load order (all modules must be loaded before this file):
   1. _core.js       - Global state, API helper, utilities
   2. _notifications.js - Notification system (required by all modules)
   3. _controls.js   - Dashboard controls, power, detection, recording, preview
   4. _storage.js    - File browser, USB devices
   5. _stats.js      - System status polling
   6. _config_tab.js - − / + steppers (Colour tuning, AI confidence, crop buffer)
   7. _lut_panel.js  - painted profiles
   8. _config.js     - profiles and the INI editor
   9. _data_collection.js - shared capture form and record modal
  10. _shell.js      - the redesigned screens (Field, Data, Settings)
  11. main.js        - This file (initialization)
   ========================================================================== */

/**
 * Main initialization on DOM ready
 */
document.addEventListener('DOMContentLoaded', function() {
    // Initialize all modules; the redesigned screens first (_shell.js)
    Shell.init();
    initControlButtons();
    initStorageTab();
    initPreview();
    initGPS();
    initNotifications();
    initHardwareControllerCheck();
    initSwitchPurposeToggle();
    initDashboardControls();
    initDataCollection();
    initConfigEditor();
    initParamSteppers();

    // Initialize painted LUT panel + weed painter
    if (typeof initLutPanel === 'function') initLutPanel();

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
