// ============================================
// OWL Central Controller - Main Orchestrator
// Initialization, event listeners, cleanup
//
// Load order (all scripts loaded before this):
//   1. /shared/js/api.js, utils.js, toast.js, config.js, numpad.js, ...
//   2. /shared/js/collect_i18n.js + collect_form.js - the shared record form
//   3. /shared/js/owl_ui.js     - the redesign's shared components
//   4. modules/_core.js         - state vars, sendCommand()
//   5. modules/_dashboard.js    - updateDashboard() poll, loadConfigDefaults()
//   6. modules/_controls.js     - spraying, recording, modes, power
//   7. modules/_config_tab.js   - sliders, preview, geometry, profiles
//   8. modules/_lut_panel.js    - painted profiles
//   9. modules/_video.js        - video modal, frame grab
//  10. modules/_config_editor.js - per-OWL INI editor, library
//  11. modules/_gps.js + _gps_map.js - Map
//  12. modules/_actuation.js    - Spraying (speed-adaptive), sensitivity
//  13. modules/_shell.js        - the redesigned screens and the OWL rail
// ============================================

document.addEventListener('DOMContentLoaded', async () => {
    Shell.init();
    setupEventListeners();
    initRecordModal();
    await loadConfigDefaults();
    updateAllSliders();
    initConfigEditor();
    checkGPSAvailability();
    initGaugeArcs();
    initActuationSliders();
    initSensitivityDial();
    startActuationPolling();
    if (typeof Numpad !== 'undefined') Numpad.init();
    if (typeof initLutPanel === 'function') initLutPanel();
    if (typeof initWidgets === 'function') initWidgets();
    if (typeof initAgent === 'function') initAgent();
    updateDashboard();
    updateInterval = setInterval(updateDashboard, UPDATE_INTERVAL);
});

/**
 * Initialize the static background arcs for all SVG gauges.
 */
function initGaugeArcs() {
    var bgIds = ['speed-gauge-bg', 'loop-gauge-bg', 'duration-gauge-bg'];
    for (var i = 0; i < bgIds.length; i++) {
        var el = document.getElementById(bgIds[i]);
        if (el) {
            el.setAttribute('d', describeArc(70, 80, 55, -90, 90));
        }
    }
}

function setupEventListeners() {
    // Range/single sliders (knob drag, track click, fine-tune)
    initSliders();

    // Colour tuning / Profiles actions (scoped by the OWL rail)
    document.getElementById('apply-all-btn')?.addEventListener('click', sendAllToDevice);
    document.getElementById('save-all-btn')?.addEventListener('click', openSaveModal);
    document.getElementById('profilesSave')?.addEventListener('click', openSaveModal);
    document.getElementById('load-preset-btn')?.addEventListener('click', loadPresetToDevice);
    document.getElementById('set-default-btn')?.addEventListener('click', setDefaultConfig);

    // Save dialog
    document.getElementById('config-save-cancel')?.addEventListener('click', closeSaveModal);
    document.getElementById('config-save-confirm')?.addEventListener('click', confirmSaveToAll);

    // Restart-required notice
    document.getElementById('config-restart-btn')?.addEventListener('click', () => {
        sendCommand('all', 'restart_service');
        showToast('Restarting OWLs', 'info');
    });

    // Advanced: send to one OWL
    document.getElementById('send-to-single-device-btn')?.addEventListener('click', sendToSingleDevice);

    // Delete preset from library
    document.getElementById('delete-preset-btn')?.addEventListener('click', deleteLibraryConfig);

    // Preview device selector: swap feed live
    document.getElementById('config-preview-device')?.addEventListener('change', onPreviewDeviceChanged);
}

// ============================================
// CLEANUP
// ============================================

window.addEventListener('beforeunload', () => {
    if (updateInterval) {
        clearInterval(updateInterval);
    }
    stopGPSPolling();
    stopActuationPolling();
    stopConfigPreview();
});
