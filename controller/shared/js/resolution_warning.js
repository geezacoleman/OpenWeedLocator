/* ==========================================================================
   OWL Controllers - Resolution Warning Modal
   Warns when recording at less than maximum camera resolution
   ========================================================================== */

/**
 * Maximum camera resolution for the OWL platform.
 * Images recorded below this are less useful for model training.
 */
var OWL_MAX_RES_WIDTH = 1456;
var OWL_MAX_RES_HEIGHT = 1088;

/**
 * Show a warning modal when the user tries to start recording
 * at a resolution lower than the camera maximum.
 *
 * Uses existing .config-modal-overlay + .config-modal CSS classes.
 *
 * @param {number} currentWidth  - Current resolution width
 * @param {number} currentHeight - Current resolution height
 * @param {function} onAccept    - Called when user chooses to change resolution
 * @param {function} onContinue  - Called when user chooses to continue at current resolution
 */
function showResolutionWarningModal(currentWidth, currentHeight, onAccept, onContinue) {
    // Build overlay
    var overlay = document.createElement('div');
    overlay.className = 'config-modal-overlay';

    // Build modal
    var modal = document.createElement('div');
    modal.className = 'config-modal';
    modal.style.maxWidth = '480px';

    modal.innerHTML =
        '<h3>Low resolution for recording</h3>' +
        '<p>Current resolution is ' + currentWidth + 'x' + currentHeight + '. ' +
        'For best image quality, we recommend recording at ' +
        OWL_MAX_RES_WIDTH + 'x' + OWL_MAX_RES_HEIGHT + '. ' +
        'This requires restarting the OWL(s).</p>' +
        '<div class="config-modal-actions" style="flex-direction:column;gap:0.5rem;">' +
            '<button class="config-action-btn primary res-warn-accept" ' +
                'style="width:100%;padding:0.75rem 1rem;font-size:1rem;">' +
                'Change to ' + OWL_MAX_RES_WIDTH + 'x' + OWL_MAX_RES_HEIGHT + ' and restart' +
            '</button>' +
            '<button class="config-action-btn secondary res-warn-continue" ' +
                'style="width:100%;padding:0.75rem 1rem;font-size:1rem;">' +
                'Continue at ' + currentWidth + 'x' + currentHeight +
            '</button>' +
        '</div>';

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    function dismiss() {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }

    // Overlay click dismisses (no action taken)
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) dismiss();
    });

    // Accept button
    modal.querySelector('.res-warn-accept').addEventListener('click', function() {
        dismiss();
        if (typeof onAccept === 'function') onAccept();
    });

    // Continue button
    modal.querySelector('.res-warn-continue').addEventListener('click', function() {
        dismiss();
        if (typeof onContinue === 'function') onContinue();
    });
}

/**
 * Check whether the given resolution is below maximum.
 * @param {number} width
 * @param {number} height
 * @returns {boolean} true if resolution is below max
 */
function isResolutionBelowMax(width, height) {
    return (width > 0 && height > 0) &&
           (width < OWL_MAX_RES_WIDTH || height < OWL_MAX_RES_HEIGHT);
}
