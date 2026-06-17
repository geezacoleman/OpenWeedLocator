/* ==========================================================================
   OWL Controllers - Shared Utility Functions
   Common formatting, DOM helpers, and utility functions
   
   Usage: Include this script after api.js
   ========================================================================== */

/* -------------------------------------------------------------------------
   Number & Size Formatting
   ------------------------------------------------------------------------- */

/**
 * Format bytes to human-readable file size
 * @param {number} bytes - Size in bytes
 * @param {number} [decimals=2] - Number of decimal places
 * @returns {string} - Formatted string (e.g., "1.5 MB")
 * 
 * @example
 * formatFileSize(1536) // "1.5 KB"
 * formatFileSize(1048576) // "1 MB"
 */
function formatFileSize(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    if (!bytes || isNaN(bytes)) return '--';
    
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    
    return parseFloat((bytes / Math.pow(k, i)).toFixed(decimals)) + ' ' + sizes[i];
}

/**
 * Format seconds to MM:SS or HH:MM:SS
 * @param {number} seconds - Duration in seconds
 * @param {boolean} [includeHours=false] - Always include hours
 * @returns {string} - Formatted time string
 * 
 * @example
 * formatTime(90) // "1:30"
 * formatTime(3661) // "1:01:01"
 */
function formatTime(seconds) {
    if (!seconds || seconds < 0 || isNaN(seconds)) return '--:--';
    
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    if (hours > 0) {
        return `${hours}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    }
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

/**
 * Format seconds to human-readable uptime (Xh Xm)
 * @param {number} seconds - Duration in seconds
 * @returns {string} - Formatted string (e.g., "2h 30m")
 */
function formatUptime(seconds) {
    if (!seconds || seconds < 0 || isNaN(seconds)) return '--';
    
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    
    if (hours > 0) {
        return `${hours}h ${mins}m`;
    }
    return `${mins}m`;
}

/**
 * Format a config/API key to human-readable label
 * @param {string} key - Snake_case or camelCase key
 * @returns {string} - Formatted label
 * 
 * @example
 * formatLabel('exg_min') // "Exg Min"
 * formatLabel('cpuPercent') // "Cpu Percent"
 */
function formatLabel(key) {
    if (!key) return '';
    
    return key
        .replace(/_/g, ' ')           // snake_case to spaces
        .replace(/([A-Z])/g, ' $1')   // camelCase to spaces
        .replace(/^./, str => str.toUpperCase())  // Capitalize first
        .trim();
}

/**
 * Format number with thousands separator
 * @param {number} num - Number to format
 * @returns {string} - Formatted number
 */
function formatNumber(num) {
    if (num === null || num === undefined || isNaN(num)) return '--';
    return num.toLocaleString();
}

/**
 * Format percentage value
 * @param {number} value - Value (0-100 or 0-1)
 * @param {number} [decimals=1] - Decimal places
 * @returns {string} - Formatted percentage
 */
function formatPercent(value, decimals = 1) {
    if (value === null || value === undefined || isNaN(value)) return '--';
    
    // Assume values > 1 are already percentages
    const percent = value > 1 ? value : value * 100;
    return percent.toFixed(decimals) + '%';
}

/* -------------------------------------------------------------------------
   Date & Time Formatting
   ------------------------------------------------------------------------- */

/**
 * Format date to ISO date string (YYYY-MM-DD)
 * @param {Date|string|number} date - Date to format
 * @returns {string}
 */
function formatDate(date) {
    const d = new Date(date);
    if (isNaN(d.getTime())) return '--';
    return d.toISOString().split('T')[0];
}

/**
 * Format date to locale time string (HH:MM:SS)
 * @param {Date|string|number} date - Date to format
 * @returns {string}
 */
function formatTimeOfDay(date) {
    const d = new Date(date);
    if (isNaN(d.getTime())) return '--';
    return d.toLocaleTimeString();
}

/**
 * Format date to full timestamp for filenames
 * @param {Date} [date=new Date()] - Date to format
 * @returns {string} - e.g., "20250101_143022"
 */
function formatTimestamp(date = new Date()) {
    return date.toISOString()
        .replace(/[-:T]/g, '')
        .slice(0, 15)
        .replace(/(\d{8})(\d{6})/, '$1_$2');
}

/* -------------------------------------------------------------------------
   DOM Helpers
   ------------------------------------------------------------------------- */

/**
 * Safely set text content of an element
 * @param {string} id - Element ID
 * @param {string} text - Text to set
 */
function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

/**
 * Safely set innerHTML of an element
 * @param {string} id - Element ID
 * @param {string} html - HTML to set
 */
function setHTML(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
}

/**
 * Toggle a class on an element
 * @param {string} id - Element ID
 * @param {string} className - Class to toggle
 * @param {boolean} [force] - Force add/remove
 */
function toggleClass(id, className, force) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle(className, force);
}

/**
 * Show an element (remove 'hidden' class)
 * @param {string|Element} el - Element or ID
 */
function showElement(el) {
    const element = typeof el === 'string' ? document.getElementById(el) : el;
    if (element) element.classList.remove('hidden');
}

/**
 * Hide an element (add 'hidden' class)
 * @param {string|Element} el - Element or ID
 */
function hideElement(el) {
    const element = typeof el === 'string' ? document.getElementById(el) : el;
    if (element) element.classList.add('hidden');
}

/**
 * Get element by ID with null safety
 * @param {string} id - Element ID
 * @returns {HTMLElement|null}
 */
function getEl(id) {
    return document.getElementById(id);
}

/* -------------------------------------------------------------------------
   Debounce & Throttle
   ------------------------------------------------------------------------- */

/**
 * Debounce a function
 * @param {Function} func - Function to debounce
 * @param {number} wait - Wait time in ms
 * @returns {Function}
 */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/**
 * Throttle a function
 * @param {Function} func - Function to throttle
 * @param {number} limit - Minimum time between calls in ms
 * @returns {Function}
 */
function throttle(func, limit) {
    let inThrottle;
    return function executedFunction(...args) {
        if (!inThrottle) {
            func(...args);
            inThrottle = true;
            setTimeout(() => inThrottle = false, limit);
        }
    };
}

/* -------------------------------------------------------------------------
   Validation & Checks
   ------------------------------------------------------------------------- */

/**
 * Check if value is a valid number
 * @param {*} value - Value to check
 * @returns {boolean}
 */
function isValidNumber(value) {
    return typeof value === 'number' && !isNaN(value) && isFinite(value);
}

/**
 * Clamp a number between min and max
 * @param {number} value - Value to clamp
 * @param {number} min - Minimum
 * @param {number} max - Maximum
 * @returns {number}
 */
function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
}

/**
 * Check if string is empty or whitespace only
 * @param {string} str - String to check
 * @returns {boolean}
 */
function isEmpty(str) {
    return !str || str.trim().length === 0;
}

/* -------------------------------------------------------------------------
   Cloud (Noktura) connectivity — shared by both controllers
   ------------------------------------------------------------------------- */

/**
 * Build the Noktura portal management URL for a device, or '' if unavailable.
 * @param {string} portalUrl - [Cloud] portal_url base (no trailing slash)
 * @param {string} slug - cloud device_id
 * @returns {string}
 */
function cloudPortalLink(portalUrl, slug) {
    if (isEmpty(portalUrl) || isEmpty(slug)) return '';
    return `${portalUrl.replace(/\/+$/, '')}/d/${encodeURIComponent(slug)}`;
}

/**
 * Derive the cloud link status-dot class from an API payload.
 * @returns {'neutral'|'connected'|'disconnected'|'connecting'}
 */
function cloudStatusClass(data) {
    if (!data || !data.cloud_enabled) return 'neutral';
    if (data.cloud_connected === true) return 'connected';
    if (data.cloud_connected === false) return 'disconnected';
    return 'connecting';  // enabled but bridge state not yet observed
}

/**
 * Render the header cloud-status chip from an API stats/owls payload.
 * Three states: not linked (grey), connecting (amber, enabled but unseen),
 * disconnected (red), connected (green).
 * @param {HTMLElement} dotEl - the status dot span
 * @param {HTMLElement} textEl - the status text span
 * @param {object} data - payload with cloud_enabled/cloud_connected/cloud_device_id
 */
function renderCloudStatus(dotEl, textEl, data) {
    if (!dotEl || !textEl) return;
    const cls = cloudStatusClass(data);
    dotEl.classList.remove('connected', 'disconnected', 'connecting', 'neutral');
    dotEl.classList.add(cls);

    const slug = (data && data.cloud_device_id) || '';
    if (cls === 'neutral') {
        textEl.textContent = 'Cloud: not linked';
    } else if (cls === 'connected') {
        textEl.textContent = slug ? `Cloud: connected as ${slug}` : 'Cloud: connected';
    } else if (cls === 'disconnected') {
        textEl.textContent = 'Cloud: disconnected';
    } else {
        textEl.textContent = 'Cloud: connecting…';
    }
}

// Remember the last URL we drew a QR for, so the 2s poll doesn't redraw it.
let _cloudQrUrl = null;

/**
 * Populate the Config-tab "Noktura cloud" manage card (status + portal link +
 * QR). No-op when the card isn't on the page. Renders the QR only when the
 * portal URL changes. Requires the vendored `qrcode` global for the QR.
 * @param {object} data - payload with cloud_* fields
 */
function updateCloudManageBlock(data) {
    const block = document.getElementById('cloud-manage-block');
    if (!block) return;

    const cls = cloudStatusClass(data);
    const slug = (data && data.cloud_device_id) || '';
    const url = cloudPortalLink(data && data.cloud_portal_url, slug);

    const dot = document.getElementById('cloud-manage-dot');
    if (dot) {
        dot.classList.remove('connected', 'disconnected', 'connecting', 'neutral');
        dot.classList.add(cls);
    }

    const statusEl = document.getElementById('cloud-manage-status');
    if (statusEl) {
        if (cls === 'neutral') {
            statusEl.textContent = 'This device is not linked to Noktura. Run owl_cloud_provision.sh with the credentials issued at registration to connect.';
        } else if (cls === 'connected') {
            statusEl.textContent = `Connected to Noktura as ${slug || 'this device'}.`;
        } else if (cls === 'disconnected') {
            statusEl.textContent = 'Configured, but the cloud link is currently down (cellular or broker).';
        } else {
            statusEl.textContent = 'Configured — connecting to Noktura…';
        }
    }

    const linkEl = document.getElementById('cloud-manage-link');
    const qrEl = document.getElementById('cloud-manage-qr');
    if (url) {
        if (linkEl) {
            linkEl.href = url;
            linkEl.textContent = url;
            linkEl.classList.remove('hidden');
        }
        if (qrEl && _cloudQrUrl !== url) {
            _cloudQrUrl = url;
            try {
                const qr = qrcode(0, 'M');
                qr.addData(url);
                qr.make();
                qrEl.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
                qrEl.classList.remove('hidden');
            } catch (e) {
                qrEl.classList.add('hidden');
            }
        }
    } else {
        if (linkEl) { linkEl.classList.add('hidden'); linkEl.removeAttribute('href'); }
        if (qrEl) { qrEl.classList.add('hidden'); qrEl.innerHTML = ''; }
        _cloudQrUrl = null;
    }
}
