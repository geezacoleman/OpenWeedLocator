/* ==========================================================================
   OWL Controllers - Shared Toast Notifications
   Simple toast/snackbar notification helper
   
   Usage: Include after utils.js
   
   Note: This provides a simple showToast() function that works with
   the shared _toast.css styles. For the full notification panel system,
   use the standalone dashboard's _notifications.js module.
   ========================================================================== */

/**
 * Default toast duration in milliseconds
 * @type {number}
 */
const DEFAULT_TOAST_DURATION = 3000;

/**
 * Currently active toast timeout ID
 * @type {number|null}
 */
let currentToastTimeout = null;

/**
 * Show a toast notification
 * 
 * This function supports two patterns:
 * 1. Simple toast element with id="toast" (networked controller style)
 * 2. Quick toast element with id="quickToast" (standalone dashboard style)
 * 
 * @param {string} message - Message to display
 * @param {string} [type='info'] - Type: 'success', 'error', 'warning', 'info'
 * @param {number} [duration=3000] - Duration in milliseconds
 * 
 * @example
 * showToast('Settings saved', 'success');
 * showToast('Connection failed', 'error', 5000);
 */
function showToast(message, type = 'info', duration = DEFAULT_TOAST_DURATION) {
    // Try quick toast first (standalone dashboard)
    let toast = document.getElementById('quickToast');
    
    if (toast) {
        // Quick toast style (pill, centered at bottom)
        toast.textContent = message;
        toast.className = `quick-toast ${type}`;
        toast.classList.remove('hidden');

        // Clear any existing timeout
        if (currentToastTimeout) {
            clearTimeout(currentToastTimeout);
        }

        // Auto-hide
        currentToastTimeout = setTimeout(() => {
            toast.classList.add('hidden');
        }, duration);
        
        return;
    }
    
    // Fall back to standard toast (networked controller)
    toast = document.getElementById('toast');
    
    if (toast) {
        // Standard toast style (centered, slides up)
        toast.textContent = message;
        toast.classList.remove('success', 'error', 'warning', 'info', 'show');
        toast.classList.add(type);

        // Trigger reflow to restart animation
        toast.offsetHeight;

        // Show toast
        setTimeout(() => toast.classList.add('show'), 10);

        // Clear any existing timeout
        if (currentToastTimeout) {
            clearTimeout(currentToastTimeout);
        }

        // Auto-hide
        currentToastTimeout = setTimeout(() => {
            toast.classList.remove('show');
        }, duration);
        
        return;
    }

    // No toast element found - log to console as fallback
    const logMethod = type === 'error' ? 'error' : type === 'warning' ? 'warn' : 'log';
    console[logMethod](`[Toast ${type}] ${message}`);
}

/**
 * Hide any visible toast immediately
 */
function hideToast() {
    if (currentToastTimeout) {
        clearTimeout(currentToastTimeout);
        currentToastTimeout = null;
    }

    const quickToast = document.getElementById('quickToast');
    if (quickToast) {
        quickToast.classList.add('hidden');
    }

    const toast = document.getElementById('toast');
    if (toast) {
        toast.classList.remove('show');
    }
}

/**
 * Convenience methods for specific toast types
 */
const Toast = {
    success: (message, duration) => showToast(message, 'success', duration),
    error: (message, duration) => showToast(message, 'error', duration),
    warning: (message, duration) => showToast(message, 'warning', duration),
    info: (message, duration) => showToast(message, 'info', duration),
    hide: hideToast
};
