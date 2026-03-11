// ============================================
// OWL Central Controller - Main Orchestrator
// Initialization, event listeners, cleanup
//
// Load order (all scripts loaded before this):
//   1. /shared/js/api.js        - apiRequest(), abortAllRequests()
//   2. /shared/js/utils.js      - formatUptime(), formatFileSize(), etc.
//   3. /shared/js/toast.js      - showToast()
//   4. /shared/js/config.js     - createConfigSection(), CONFIG_FIELD_DEFS
//   5. modules/_core.js         - state vars, sendCommand()
//   6. modules/_dashboard.js    - updateDashboard(), loadConfigDefaults()
//   7. modules/_controls.js     - toggleMainDetection(), restartOWL()
//   8. modules/_config_tab.js   - slider controls, preview, advanced
//   9. modules/_video.js        - openVideoFeed(), downloadVideoFrame()
//  10. modules/_config_editor.js - config editor, save as modal
//  11. modules/_gps.js          - GPS tab, polling, tab switching
//  12. modules/_actuation.js    - Actuation sliders, speed-adaptive
//  13. modules/_ai_tab.js       - AI tab, model selection, class filtering
// ============================================

// ============================================
// INITIALIZATION
// ============================================

document.addEventListener('DOMContentLoaded', async () => {
    setupEventListeners();
    await loadConfigDefaults();
    updateAllSliders();
    initConfigEditor();
    checkGPSAvailability();
    initGaugeArcs();
    initActuationSliders();
    initSensitivityDial();
    startActuationPolling();
    if (typeof Numpad !== 'undefined') Numpad.init();
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

    // Config action buttons — broadcast to all OWLs
    document.getElementById('apply-all-btn')?.addEventListener('click', sendAllToDevice);
    document.getElementById('save-all-btn')?.addEventListener('click', saveToAll);
    document.getElementById('load-preset-btn')?.addEventListener('click', loadPresetToDevice);

    // Advanced Settings — send to single device
    document.getElementById('send-to-single-device-btn')?.addEventListener('click', sendToSingleDevice);

    // Delete preset from library
    document.getElementById('delete-preset-btn')?.addEventListener('click', deleteLibraryConfig);

    // Preview device selector — swap feed live
    document.getElementById('config-preview-device')?.addEventListener('change', onPreviewDeviceChanged);
}

// ============================================
// CLEANUP
// ============================================

window.addEventListener('beforeunload', () => {
    if (updateInterval) {
        clearInterval(updateInterval);
    }
    aiTabActive = false;
    stopGPSPolling();
    stopActuationPolling();
    stopConfigPreview();
});
