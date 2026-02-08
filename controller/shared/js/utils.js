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
