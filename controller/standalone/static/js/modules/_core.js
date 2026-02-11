/* ==========================================================================
   OWL Dashboard - Core Module
   Global state and constants.
   API helper (apiRequest, pendingRequests) provided by shared/js/api.js.
   Utility helpers (formatFileSize, formatLabel) provided by shared/js/utils.js.
   ========================================================================== */

/* --------------------------------------------------------------------------
   Global State
   -------------------------------------------------------------------------- */
let isGpsEnabled = true;
let gpsWatchId = null;
let gpsData = null;
let zoomLevel = 1;
let updateInterval = null;
let hardwareControllerActive = false;
let controllerType = 'none';

/* --------------------------------------------------------------------------
   Constants
   -------------------------------------------------------------------------- */
const SYSTEM_UPDATE_INTERVAL = 2000;
const zoomStep = 0.2;
const maxZoom = 3;
const minZoom = 1;
