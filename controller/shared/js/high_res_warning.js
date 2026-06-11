/* ==========================================================================
   OWL Controllers - High-Resolution Warning Modal
   Warns when recording at a resolution above the Pi 3/4 safety clamp
   (832x640). The OWL clamps to 640x480 on those Pis unless the user
   explicitly enables [Camera] allow_high_resolution.
   ========================================================================== */

/**
 * Pixel count above which Pi 3/4 OWLs apply the safety clamp.
 * Mirrors the threshold in owl.py (832 * 640 = 532480).
 */
var OWL_HIGH_RES_PIXEL_THRESHOLD = 832 * 640;

/**
 * Pi versions that apply the safety clamp.
 */
var OWL_CLAMPED_PI_VERSIONS = ['rpi-3', 'rpi-4'];

/**
 * Pure-logic check: returns true when the given resolution would trigger the
 * Pi 3/4 safety clamp. Used by tests and by the offline-judgement path; the
 * runtime check should prefer the explicit resolution_clamped state field
 * published by the OWL, since that knows definitively whether the clamp fired.
 *
 * @param {string} rpiVersion  - 'rpi-3' / 'rpi-4' / 'rpi-5' / 'unknown'
 * @param {number} width       - resolution to evaluate
 * @param {number} height
 * @param {boolean} allowHighRes - current value of [Camera] allow_high_resolution
 */
function isHighResAboveClamp(rpiVersion, width, height, allowHighRes) {
    if (allowHighRes) return false;
    if (OWL_CLAMPED_PI_VERSIONS.indexOf(rpiVersion) === -1) return false;
    if (!(width > 0 && height > 0)) return false;
    return (width * height) > OWL_HIGH_RES_PIXEL_THRESHOLD;
}

/**
 * Runtime check: does this OWL state indicate the resolution was silently
 * clamped at startup? The OWL is authoritative here — it sets
 * resolution_clamped=True when it actually applied the clamp.
 *
 * @param {object} state - OWL heartbeat state (owl.py publishes via mqtt_manager)
 */
function isClampActive(state) {
    if (!state) return false;
    if (state.allow_high_resolution) return false;
    return !!state.resolution_clamped;
}

function _hrwPrettyPi(rpiVersion) {
    if (rpiVersion === 'rpi-3') return 'Pi 3';
    if (rpiVersion === 'rpi-4') return 'Pi 4';
    if (rpiVersion === 'rpi-5') return 'Pi 5';
    return String(rpiVersion || 'unknown');
}

function _hrwMakeButton(label, className, extraStyle) {
    var btn = document.createElement('button');
    btn.className = 'config-action-btn ' + className;
    btn.textContent = label;
    btn.style.width = '100%';
    btn.style.padding = '0.75rem 1rem';
    btn.style.fontSize = '1rem';
    if (extraStyle) {
        for (var key in extraStyle) {
            if (Object.prototype.hasOwnProperty.call(extraStyle, key)) {
                btn.style[key] = extraStyle[key];
            }
        }
    }
    return btn;
}

/**
 * Show a warning modal when an OWL is silently clamping the requested
 * resolution at startup. Explains the mismatch and offers a single override
 * action that persists the flag and restarts the OWL.
 *
 * Uses existing .config-modal-overlay + .config-modal CSS classes.
 *
 * @param {string}   rpiVersion    - 'rpi-3' / 'rpi-4'
 * @param {number}   requestedWidth   - Resolution from config (what the user wants)
 * @param {number}   requestedHeight
 * @param {number}   actualWidth      - Resolution the OWL is actually running (post-clamp)
 * @param {number}   actualHeight
 * @param {function} onCancel      - Called when user cancels (no recording started)
 * @param {function} onOverride    - Called when user accepts the override.
 *                                   Receives no args; expected to persist the
 *                                   flag, trigger restart, and surface its own
 *                                   follow-up toast.
 */
function showHighResWarningModal(rpiVersion, requestedWidth, requestedHeight, actualWidth, actualHeight, onCancel, onOverride) {
    var prettyPi = _hrwPrettyPi(rpiVersion);

    var overlay = document.createElement('div');
    overlay.className = 'config-modal-overlay';

    var modal = document.createElement('div');
    modal.className = 'config-modal';
    modal.style.maxWidth = '520px';

    var title = document.createElement('h3');
    title.textContent = 'Resolution clamped on ' + prettyPi;
    modal.appendChild(title);

    var detail = document.createElement('p');
    detail.appendChild(document.createTextNode('You configured '));
    var reqStrong = document.createElement('strong');
    reqStrong.textContent = requestedWidth + 'x' + requestedHeight;
    detail.appendChild(reqStrong);
    detail.appendChild(document.createTextNode(', but the OWL is running at '));
    var actualStrong = document.createElement('strong');
    actualStrong.textContent = actualWidth + 'x' + actualHeight;
    detail.appendChild(actualStrong);
    detail.appendChild(document.createTextNode(
        ' because the Pi 3/4 safety clamp is active. Override to apply your configured ' +
        'resolution — the OWL will restart.'
    ));
    modal.appendChild(detail);

    var notice = document.createElement('p');
    notice.style.color = '#856404';
    notice.style.background = '#fff3cd';
    notice.style.border = '1px solid #ffc107';
    notice.style.borderRadius = '6px';
    notice.style.padding = '0.6rem 0.8rem';
    notice.style.fontSize = '0.9rem';
    notice.textContent =
        'High-res capture on older Pis can cause camera/GPU memory issues. ' +
        'Only override if you have verified your hardware handles this resolution.';
    modal.appendChild(notice);

    var actions = document.createElement('div');
    actions.className = 'config-modal-actions';
    actions.style.flexDirection = 'column';
    actions.style.gap = '0.5rem';

    var cancelBtn = _hrwMakeButton('Cancel', 'primary high-res-cancel', {
        background: '#f4f5f7',
        color: '#333',
        border: '1px solid #c0c4cc'
    });
    var overrideBtn = _hrwMakeButton('Override and restart OWL', 'secondary high-res-override', {
        background: '#e67e22',
        color: '#fff',
        border: '1px solid #d35400'
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(overrideBtn);
    modal.appendChild(actions);

    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    function dismiss() {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }

    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) {
            dismiss();
            if (typeof onCancel === 'function') onCancel();
        }
    });

    cancelBtn.addEventListener('click', function() {
        dismiss();
        if (typeof onCancel === 'function') onCancel();
    });

    overrideBtn.addEventListener('click', function() {
        dismiss();
        if (typeof onOverride === 'function') onOverride();
    });
}
