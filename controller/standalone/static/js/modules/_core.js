/* ==========================================================================
   OWL Dashboard - Core Module
   Global state, API helper, utilities
   ========================================================================== */

/* --------------------------------------------------------------------------
   Global State
   -------------------------------------------------------------------------- */
let isGpsEnabled = true;
let gpsWatchId = null;
let gpsData = null;
let zoomLevel = 1;
let updateInterval = null;
let pendingRequests = {};
let hardwareControllerActive = false;
let controllerType = 'none';

/* --------------------------------------------------------------------------
   Constants
   -------------------------------------------------------------------------- */
const SYSTEM_UPDATE_INTERVAL = 2000;
const zoomStep = 0.2;
const maxZoom = 3;
const minZoom = 1;

/* --------------------------------------------------------------------------
   API Request Helper
   Handles timeouts, abort controllers, and cache-busting
   -------------------------------------------------------------------------- */
function apiRequest(url, options = {}, timeout = 10000) {
    if (pendingRequests[url]) {
        pendingRequests[url].abort();
    }

    const controller = new AbortController();
    pendingRequests[url] = controller;

    const timeoutId = setTimeout(() => controller.abort(), timeout);

    const separator = url.includes('?') ? '&' : '?';
    const cacheBustUrl = url + separator + 't=' + Date.now();

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

/* --------------------------------------------------------------------------
   Utility Functions
   -------------------------------------------------------------------------- */
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatTime(seconds) {
    if (!seconds || seconds < 0) return '--:--';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function formatLabel(key) {
    return key
        .replace(/_/g, ' ')
        .replace(/([A-Z])/g, ' $1')
        .replace(/^./, str => str.toUpperCase())
        .trim();
}
