/* ==========================================================================
   Hardware Controller Logic (Shared)

   Pure logic for determining which controls are hardware-locked based on
   controller type and switch purpose. No DOM, no globals, no fetch calls.

   Used by: standalone dashboard, networked dashboard
   ========================================================================== */

/**
 * Check if a specific control is hardware-locked for the given controller config.
 *
 * @param {string} controlName - 'recording', 'detection', or 'sensitivity'
 * @param {string} controllerType - 'none', 'ute', or 'advanced'
 * @param {string} switchPurpose - 'recording' or 'detection' (Ute only)
 * @returns {boolean} true if the control is managed by the physical hardware
 */
function isControlHardwareLocked(controlName, controllerType, switchPurpose) {
    if (!controllerType || controllerType === 'none') return false;

    if (controllerType === 'ute') {
        // Ute has one switch — it locks only the control it manages
        return controlName === switchPurpose;
    }

    if (controllerType === 'advanced') {
        return controlName === 'recording' ||
               controlName === 'detection' ||
               controlName === 'sensitivity';
    }

    return false;
}

/**
 * Create an inline lock icon HTML string.
 * @returns {string} HTML for a lock icon span
 */
function createLockIconHTML() {
    return '<span class="lock-icon"><svg width="12" height="12" viewBox="0 0 24 24">' +
        '<path d="M12,17A2,2 0 0,0 14,15C14,13.89 13.1,13 12,13A2,2 0 0,0 10,15A2,2 0 0,0 12,17' +
        'M18,8A2,2 0 0,1 20,10V20A2,2 0 0,1 18,22H6A2,2 0 0,1 4,20V10C4,8.89 4.9,8 6,8H7V6A5,5 0 0,1 12,1' +
        'A5,5 0 0,1 17,6V8H18M12,3A3,3 0 0,0 9,6V8H15V6A3,3 0 0,0 12,3Z"/></svg></span>';
}
