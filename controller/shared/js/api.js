/* ==========================================================================
   OWL Controllers - Shared API Helper
   Fetch wrapper with timeout, abort controller, and cache-busting
   
   Usage: Include this script before app-specific scripts
   ========================================================================== */

/**
 * Pending requests tracker for abort functionality
 * @type {Object.<string, AbortController>}
 */
const pendingRequests = {};

/**
 * Default timeout for API requests (milliseconds)
 * @type {number}
 */
const DEFAULT_API_TIMEOUT = 10000;

/**
 * Make an API request with timeout, abort controller, and cache-busting
 * 
 * Features:
 * - Automatic timeout with configurable duration
 * - Aborts previous request to same URL (prevents race conditions)
 * - Cache-busting query parameter
 * - No-cache headers
 * 
 * @param {string} url - The URL to fetch
 * @param {RequestInit} [options={}] - Fetch options (method, headers, body, etc.)
 * @param {number} [timeout=10000] - Timeout in milliseconds
 * @returns {Promise<Response>} - Fetch Response object
 * @throws {Error} - Throws on timeout, network error, or non-OK response
 * 
 * @example
 * // GET request
 * apiRequest('/api/system_stats')
 *   .then(r => r.json())
 *   .then(data => console.log(data));
 * 
 * @example
 * // POST request
 * apiRequest('/api/detection/start', { method: 'POST' })
 *   .then(r => r.json())
 *   .then(data => console.log(data));
 * 
 * @example
 * // POST with JSON body
 * apiRequest('/api/config', {
 *   method: 'POST',
 *   headers: { 'Content-Type': 'application/json' },
 *   body: JSON.stringify({ key: 'value' })
 * });
 */
function apiRequest(url, options = {}, timeout = DEFAULT_API_TIMEOUT) {
    // Abort any pending request to the same URL
    if (pendingRequests[url]) {
        pendingRequests[url].abort();
    }

    // Create new abort controller for this request
    const controller = new AbortController();
    pendingRequests[url] = controller;

    // Set up timeout
    const timeoutId = setTimeout(() => controller.abort(), timeout);

    // Add cache-busting query parameter
    const separator = url.includes('?') ? '&' : '?';
    const cacheBustUrl = url + separator + '_t=' + Date.now();

    // Merge options with defaults
    const fetchOptions = {
        ...options,
        signal: controller.signal,
        cache: 'no-store',
        headers: {
            ...options.headers,
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        }
    };

    return fetch(cacheBustUrl, fetchOptions)
        .then(response => {
            clearTimeout(timeoutId);
            delete pendingRequests[url];

            if (!response.ok) {
                throw new Error(`Request failed: ${response.status} ${response.statusText}`);
            }
            return response;
        })
        .catch(error => {
            clearTimeout(timeoutId);
            delete pendingRequests[url];

            if (error.name === 'AbortError') {
                throw new Error('Request timed out');
            }
            throw error;
        });
}

/**
 * Abort all pending requests
 * Useful for cleanup on page unload
 */
function abortAllRequests() {
    Object.values(pendingRequests).forEach(controller => {
        try {
            controller.abort();
        } catch (e) {
            // Ignore abort errors
        }
    });
}

/**
 * Check if a request is pending for a given URL
 * @param {string} url - The URL to check
 * @returns {boolean}
 */
function isRequestPending(url) {
    return !!pendingRequests[url];
}

// Cleanup on page unload
if (typeof window !== 'undefined') {
    window.addEventListener('unload', abortAllRequests);
    window.addEventListener('beforeunload', abortAllRequests);
}
