// ============================================
// OWL Central Controller - GPS Tab
// ============================================

let gpsPollingInterval = null;
let gpsEnabled = false;
const GPS_POLL_MS = 1000;

// ============================================
// TAB SWITCHING
// ============================================

function switchToOWLTab() {
    document.getElementById('tab-owls').classList.add('active');
    document.getElementById('tab-gps').classList.remove('active');
    document.getElementById('tab-ai').classList.remove('active');
    document.getElementById('tab-config').classList.remove('active');
    document.getElementById('view-owls').style.display = '';
    document.getElementById('view-gps').style.display = 'none';
    document.getElementById('view-ai').style.display = 'none';
    document.getElementById('view-config').style.display = 'none';
    aiTabActive = false;
    stopGPSPolling();
    stopConfigPreview();
}

function switchToGPSTab() {
    document.getElementById('tab-gps').classList.add('active');
    document.getElementById('tab-owls').classList.remove('active');
    document.getElementById('tab-ai').classList.remove('active');
    document.getElementById('tab-config').classList.remove('active');
    document.getElementById('view-gps').style.display = '';
    document.getElementById('view-owls').style.display = 'none';
    document.getElementById('view-ai').style.display = 'none';
    document.getElementById('view-config').style.display = 'none';
    aiTabActive = false;
    startGPSPolling();
    stopConfigPreview();
}

function switchToConfigTab() {
    document.getElementById('tab-config').classList.add('active');
    document.getElementById('tab-owls').classList.remove('active');
    document.getElementById('tab-gps').classList.remove('active');
    document.getElementById('tab-ai').classList.remove('active');
    document.getElementById('view-config').style.display = '';
    document.getElementById('view-owls').style.display = 'none';
    document.getElementById('view-gps').style.display = 'none';
    document.getElementById('view-ai').style.display = 'none';
    aiTabActive = false;
    stopGPSPolling();
}

// ============================================
// GPS POLLING
// ============================================

function startGPSPolling() {
    if (gpsPollingInterval) return;
    pollGPS(); // Immediate first poll
    gpsPollingInterval = setInterval(pollGPS, GPS_POLL_MS);
}

function stopGPSPolling() {
    if (gpsPollingInterval) {
        clearInterval(gpsPollingInterval);
        gpsPollingInterval = null;
    }
}

async function pollGPS() {
    try {
        const res = await fetch('/api/gps');
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        updateGPSDisplay(data);
    } catch (err) {
        // Show disconnected state on error
        updateGPSConnectionStatus(false, false);
    }
}

// ============================================
// DISPLAY UPDATES
// ============================================

function updateGPSDisplay(data) {
    if (!data.connection || !data.connection.gps_enabled) {
        updateGPSConnectionStatus(false, false);
        return;
    }

    const fix = data.fix || {};
    const conn = data.connection || {};
    const session = data.session || {};

    updateGPSConnectionStatus(conn.tcp_connected, fix.fix_valid);
    updateSpeedDisplay(fix.fix_valid ? fix.speed_kmh : null);
    updateCompass(fix.fix_valid ? fix.heading : null);
    updateAccuracyBar(fix.hdop, fix.satellites);
    updateSessionStats(session);
}

function updateGPSConnectionStatus(tcpConnected, fixValid) {
    const banner = document.getElementById('gps-status-banner');
    const text = document.getElementById('gps-status-text');

    if (!banner || !text) return;

    banner.classList.remove('connected', 'searching', 'disconnected');

    if (!tcpConnected) {
        banner.classList.add('disconnected');
        text.textContent = 'GPS Disconnected';
    } else if (!fixValid) {
        banner.classList.add('searching');
        text.textContent = 'Searching for Satellites...';
    } else {
        banner.classList.add('connected');
        text.textContent = 'GPS Connected';
    }
}

function updateSpeedDisplay(speedKmh) {
    const el = document.getElementById('gps-speed');
    if (!el) return;

    if (speedKmh === null || speedKmh === undefined) {
        el.textContent = '--';
    } else {
        el.textContent = Math.round(speedKmh);
    }
}

function updateCompass(heading) {
    const arrow = document.getElementById('compass-arrow');
    const label = document.getElementById('compass-heading');

    if (!arrow || !label) return;

    if (heading === null || heading === undefined) {
        label.textContent = '--\u00B0';
        return;
    }

    arrow.style.transform = 'translate(-50%, -100%) rotate(' + heading + 'deg)';
    label.textContent = Math.round(heading) + '\u00B0';
}

function updateAccuracyBar(hdop, satellites) {
    const fill = document.getElementById('gps-accuracy-fill');
    const satsEl = document.getElementById('gps-satellites');

    if (satsEl) {
        satsEl.textContent = (satellites !== null && satellites !== undefined) ? satellites : '--';
    }

    if (!fill) return;

    if (hdop === null || hdop === undefined) {
        fill.style.width = '0%';
        fill.style.background = '#999';
        return;
    }

    // HDOP: <1 = excellent, 1-2 = good, 2-5 = moderate, >5 = poor
    var pct, color;
    if (hdop <= 1.0) {
        pct = 100;
        color = 'var(--success)';
    } else if (hdop <= 2.0) {
        pct = 80;
        color = 'var(--success)';
    } else if (hdop <= 5.0) {
        pct = 50;
        color = 'var(--warning)';
    } else {
        pct = 20;
        color = 'var(--danger)';
    }

    fill.style.width = pct + '%';
    fill.style.background = color;
}

function updateSessionStats(session) {
    var el;

    el = document.getElementById('gps-distance');
    if (el) {
        el.textContent = (session.distance_km || 0).toFixed(2) + ' km';
    }

    el = document.getElementById('gps-time');
    if (el) {
        var secs = Math.round(session.time_active_s || 0);
        var h = Math.floor(secs / 3600);
        var m = Math.floor((secs % 3600) / 60);
        var s = secs % 60;
        el.textContent = h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
    }

    el = document.getElementById('gps-area');
    if (el) {
        el.textContent = (session.area_hectares || 0).toFixed(2) + ' ha';
    }
}

// ============================================
// GPS TAB VISIBILITY CHECK
// ============================================

async function checkGPSAvailability() {
    // Always show the GPS tab — never hide it.
    // If GPS is unavailable, the panel shows a clear status message instead.
    try {
        const res = await fetch('/api/gps');
        if (!res.ok) return;
        const data = await res.json();
        if (data.connection && !data.connection.gps_enabled) {
            updateGPSConnectionStatus(false, false);
        }
    } catch (e) {
        updateGPSConnectionStatus(false, false);
    }
}
