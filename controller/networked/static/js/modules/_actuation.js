// ============================================
// OWL Central Controller - Actuation Module
// Speed gauges, actuation sliders, coverage warning
// ============================================

let actuationPollInterval = null;
const ACTUATION_POLL_MS = 1000;

// Actuation slider state
// actuation_length_cm: sent via /api/actuation/config (GPS-dependent)
// crop_buffer_px, confidence: sent via sendConfigUpdate (synced from configParams)
let actuationParams = {
    actuation_length_cm: { value: 10, min: 2, max: 50 },
    crop_buffer_px: { value: 20, min: 0, max: 50 },
    confidence: { value: 50, min: 5, max: 100 }
};

// Unit labels for each slider
var actuationUnits = {
    actuation_length_cm: ' cm',
    crop_buffer_px: ' px',
    confidence: '%'
};

// ============================================
// SVG ARC GAUGE HELPERS
// ============================================

/**
 * Generate an SVG arc path for a semicircular gauge.
 * cx, cy: centre of the arc
 * r: radius
 * startAngle, endAngle: in degrees (0 = 12 o'clock, going clockwise)
 * We use a 180-degree arc from -180 to 0 (left to right semicircle).
 */
function describeArc(cx, cy, r, startDeg, endDeg) {
    var startRad = (startDeg - 90) * Math.PI / 180;
    var endRad = (endDeg - 90) * Math.PI / 180;
    var x1 = cx + r * Math.cos(startRad);
    var y1 = cy + r * Math.sin(startRad);
    var x2 = cx + r * Math.cos(endRad);
    var y2 = cy + r * Math.sin(endRad);
    var largeArc = (endDeg - startDeg > 180) ? 1 : 0;
    return 'M ' + x1 + ' ' + y1 + ' A ' + r + ' ' + r + ' 0 ' + largeArc + ' 1 ' + x2 + ' ' + y2;
}

function updateGaugeArc(fillEl, fraction) {
    // Arc goes from 180deg to 0deg (left to right semicircle)
    // fraction 0..1 maps to sweep
    var sweep = Math.max(0, Math.min(1, fraction)) * 180;
    var path = describeArc(70, 80, 55, -90, -90 + sweep);
    fillEl.setAttribute('d', path);
}

// ============================================
// GAUGE UPDATES
// ============================================

function updateSpeedGauge(speedKmh, gpsStatus) {
    var valueEl = document.getElementById('speed-gauge-value');
    var fillEl = document.getElementById('speed-gauge-fill');
    var badgeEl = document.getElementById('speed-gps-badge');

    if (!valueEl || !fillEl) return;

    var maxSpeed = 30;
    var fraction = Math.min(speedKmh / maxSpeed, 1);

    valueEl.textContent = speedKmh.toFixed(1);
    updateGaugeArc(fillEl, fraction);

    // Update fill colour class
    fillEl.classList.remove('stale', 'no-gps');
    if (gpsStatus === 'stale') fillEl.classList.add('stale');
    else if (gpsStatus === 'no_gps') fillEl.classList.add('no-gps');

    // GPS badge
    if (badgeEl) {
        badgeEl.className = 'gps-badge';
        if (gpsStatus === 'active') {
            badgeEl.classList.add('active');
            badgeEl.textContent = 'GPS Active';
        } else if (gpsStatus === 'stale') {
            badgeEl.classList.add('stale');
            badgeEl.textContent = 'GPS Stale';
        } else {
            badgeEl.classList.add('no-gps');
            badgeEl.textContent = 'No GPS';
        }
    }
}

function updateLoopTimeGauge(avgMs) {
    var valueEl = document.getElementById('loop-gauge-value');
    var fillEl = document.getElementById('loop-gauge-fill');

    if (!valueEl || !fillEl) return;

    var maxMs = 200;
    var fraction = Math.min(avgMs / maxMs, 1);

    valueEl.textContent = avgMs > 0 ? avgMs.toFixed(0) : '--';
    updateGaugeArc(fillEl, fraction);

    // Colour thresholds
    fillEl.classList.remove('loop-good', 'loop-warn', 'loop-danger');
    if (avgMs > 100) fillEl.classList.add('loop-danger');
    else if (avgMs > 50) fillEl.classList.add('loop-warn');
    else fillEl.classList.add('loop-good');
}

// ============================================
// ACTUATION POLLING
// ============================================

async function pollActuation() {
    try {
        var res = await fetch('/api/actuation');
        if (!res.ok) return;

        var data = await res.json();

        // Update gauges
        updateSpeedGauge(data.speed_kmh || 0, data.gps_status || 'no_gps');
        updateLoopTimeGauge(data.avg_loop_time_ms || 0);

        // Update computed values
        var durEl = document.getElementById('actuation-duration-value');
        var delayEl = document.getElementById('actuation-delay-value');
        var srcEl = document.getElementById('actuation-source-value');

        if (durEl) durEl.textContent = (data.actuation_duration * 1000).toFixed(0) + 'ms';
        if (delayEl) delayEl.textContent = (data.delay * 1000).toFixed(0) + 'ms';
        if (srcEl) {
            var src = data.source || 'config';
            srcEl.textContent = src.charAt(0).toUpperCase() + src.slice(1);
            srcEl.className = 'actuation-source ' + (data.source || 'config');
        }

        // Sync actuation_length_cm from actuation API
        if (typeof data.actuation_length_cm !== 'undefined') {
            actuationParams.actuation_length_cm.value = data.actuation_length_cm;
        }

        // Sync crop_buffer_px and confidence from configParams (kept in sync by dashboard)
        actuationParams.crop_buffer_px.value = configParams.crop_buffer_px.value;
        actuationParams.confidence.value = configParams.confidence.value;

        updateActuationSliders();

        // GPS disable state — only affects actuation length slider
        var gpsGroup = document.querySelector('.actuation-gps-group');
        if (gpsGroup) {
            if ((data.gps_status || 'no_gps') === 'no_gps') {
                gpsGroup.classList.add('no-gps');
            } else {
                gpsGroup.classList.remove('no-gps');
            }
        }

        // AI confidence disable state — greyed when not AI mode
        updateConfidenceSliderState();

        // Coverage warning
        var warnEl = document.getElementById('coverage-warning');
        if (warnEl) {
            if (!data.coverage_ok && data.coverage_message) {
                warnEl.textContent = data.coverage_message;
                warnEl.classList.add('visible');
            } else {
                warnEl.classList.remove('visible');
            }
        }
    } catch (err) {
        // Silently fail — will retry next poll
    }
}

function startActuationPolling() {
    if (actuationPollInterval) return;
    pollActuation(); // Immediate first poll
    actuationPollInterval = setInterval(pollActuation, ACTUATION_POLL_MS);
}

function stopActuationPolling() {
    if (actuationPollInterval) {
        clearInterval(actuationPollInterval);
        actuationPollInterval = null;
    }
}

// ============================================
// CONFIDENCE SLIDER STATE
// ============================================

function updateConfidenceSliderState() {
    var confGroup = document.getElementById('confidence-act-group');
    if (!confGroup) return;

    var activeBtn = document.querySelector('.mode-btn.active');
    var mode = activeBtn ? activeBtn.dataset.mode : 'gob';

    if (mode === 'gog' || mode === 'hybrid') {
        confGroup.classList.remove('disabled');
    } else {
        confGroup.classList.add('disabled');
    }
}

// ============================================
// ACTUATION SLIDERS (pointer-capture drag)
// ============================================

function initActuationSliders() {
    var sliders = document.querySelectorAll('.actuation-slider-wrap .single-slider');
    sliders.forEach(function(slider) {
        var knob = slider.querySelector('.slider-knob');
        if (!knob) return;

        var param = knob.getAttribute('data-param');
        if (!param || !(param in actuationParams)) return;

        // Pointer-capture drag
        knob.addEventListener('pointerdown', function(e) {
            e.preventDefault();
            knob.setPointerCapture(e.pointerId);

            function onMove(ev) {
                var rail = slider.querySelector('.slider-rail');
                if (!rail) return;
                var rect = rail.getBoundingClientRect();
                var pct = (ev.clientX - rect.left) / rect.width;
                pct = Math.max(0, Math.min(1, pct));

                var cfg = actuationParams[param];
                var val = Math.round(cfg.min + pct * (cfg.max - cfg.min));
                cfg.value = val;
                positionActuationKnob(knob, param);
                updateActuationValueDisplay(param);
            }

            function onUp() {
                knob.removeEventListener('pointermove', onMove);
                knob.removeEventListener('pointerup', onUp);
                knob.removeEventListener('pointercancel', onUp);
                sendActuationParam(param);
            }

            knob.addEventListener('pointermove', onMove);
            knob.addEventListener('pointerup', onUp);
            knob.addEventListener('pointercancel', onUp);
        });

        // Tap-to-jump on track
        var rail = slider.querySelector('.slider-rail');
        if (rail) {
            rail.addEventListener('pointerdown', function(e) {
                if (e.target.classList.contains('slider-knob')) return;
                var rect = rail.getBoundingClientRect();
                var pct = (e.clientX - rect.left) / rect.width;
                pct = Math.max(0, Math.min(1, pct));

                var cfg = actuationParams[param];
                cfg.value = Math.round(cfg.min + pct * (cfg.max - cfg.min));
                positionActuationKnob(knob, param);
                updateActuationValueDisplay(param);
                sendActuationParam(param);
            });
        }
    });
}

function positionActuationKnob(knob, param) {
    var cfg = actuationParams[param];
    if (!cfg) return;
    var pct = (cfg.value - cfg.min) / (cfg.max - cfg.min);
    knob.style.left = (pct * 100) + '%';

    // Update fill bar
    var fill = knob.closest('.single-slider')?.querySelector('.slider-fill');
    if (fill) {
        fill.style.width = (pct * 100) + '%';
    }
}

function updateActuationValueDisplay(param) {
    var el = document.getElementById(param + '-act-value');
    if (el && actuationParams[param]) {
        el.textContent = actuationParams[param].value + (actuationUnits[param] || '');
    }
}

function updateActuationSliders() {
    for (var param in actuationParams) {
        var knob = document.getElementById(param + '-act-knob');
        if (knob) positionActuationKnob(knob, param);
        updateActuationValueDisplay(param);
    }
}

// ============================================
// SEND PARAM UPDATES (per-param routing)
// ============================================

function sendActuationParam(param) {
    var val = actuationParams[param].value;

    if (param === 'actuation_length_cm') {
        // Actuation geometry — POST to actuation API
        fetch('/api/actuation/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ actuation_length_cm: val })
        }).catch(function(err) {
            console.error('Failed to send actuation config:', err);
        });
    } else if (param === 'crop_buffer_px') {
        // Keep configParams in sync, send via MQTT
        configParams.crop_buffer_px.value = val;
        sendCommand('all', 'set_crop_buffer', val);
        // Update config tab slider if visible
        if (typeof updateSlider === 'function') updateSlider(param);
    } else if (param === 'confidence') {
        // Keep configParams in sync, send via MQTT (convert % to 0.0-1.0)
        configParams.confidence.value = val;
        sendCommand('all', 'set_greenongreen_param', { key: 'confidence', value: val / 100 });
        // Update config tab slider if visible
        if (typeof updateSlider === 'function') updateSlider(param);
    }
}

// ============================================
// SENSITIVITY DIAL (3-segment SVG arc)
// ============================================

var sensitivityLevels = ['low', 'medium', 'high'];
var sensitivityColors = {
    low: '#3498db',
    medium: '#27ae60',
    high: '#e67e22'
};
var sensitivityLabels = {
    low: 'Low',
    medium: 'Medium',
    high: 'High'
};
// Needle angle for each level (degrees, 0 = straight up)
var sensitivityNeedleAngles = { low: -61, medium: 0, high: 61 };
var currentSensitivity = 'medium';

function initSensitivityDial() {
    var svg = document.getElementById('sensitivity-svg');
    if (!svg) return;

    var svgNS = 'http://www.w3.org/2000/svg';

    // Clear existing content
    svg.innerHTML = '';

    // Draw 3 arc segments with gaps
    // Full arc from -90 to 90 degrees (left to right semicircle)
    var segments = [
        { level: 'low', start: -90, end: -32 },
        { level: 'medium', start: -29, end: 29 },
        { level: 'high', start: 32, end: 90 }
    ];

    // Background track (full arc, light gray)
    var bgPath = document.createElementNS(svgNS, 'path');
    bgPath.setAttribute('d', describeArc(70, 80, 55, -90, 90));
    bgPath.setAttribute('fill', 'none');
    bgPath.setAttribute('stroke', '#e8ecf0');
    bgPath.setAttribute('stroke-width', '16');
    bgPath.setAttribute('stroke-linecap', 'round');
    svg.appendChild(bgPath);

    segments.forEach(function(seg) {
        // Invisible wide hit area for touch
        var hitArea = document.createElementNS(svgNS, 'path');
        hitArea.setAttribute('d', describeArc(70, 80, 55, seg.start, seg.end));
        hitArea.setAttribute('fill', 'none');
        hitArea.setAttribute('stroke', 'transparent');
        hitArea.setAttribute('stroke-width', '36');
        hitArea.style.cursor = 'pointer';
        hitArea.addEventListener('click', function() {
            setSensitivityLevel(seg.level);
        });
        svg.appendChild(hitArea);

        // Visible coloured segment
        var path = document.createElementNS(svgNS, 'path');
        path.setAttribute('d', describeArc(70, 80, 55, seg.start, seg.end));
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', sensitivityColors[seg.level]);
        path.setAttribute('stroke-width', '16');
        path.setAttribute('stroke-linecap', 'round');
        path.setAttribute('class', 'sensitivity-segment');
        path.setAttribute('data-level', seg.level);
        path.style.opacity = '0.25';
        path.style.pointerEvents = 'none';
        svg.appendChild(path);
    });

    // Needle group (line + arrowhead, rotates around center)
    var needleG = document.createElementNS(svgNS, 'g');
    needleG.setAttribute('id', 'sensitivity-needle');

    var shaft = document.createElementNS(svgNS, 'line');
    shaft.setAttribute('x1', '70');
    shaft.setAttribute('y1', '80');
    shaft.setAttribute('x2', '70');
    shaft.setAttribute('y2', '36');
    shaft.setAttribute('stroke', '#2c3e50');
    shaft.setAttribute('stroke-width', '2.5');
    shaft.setAttribute('stroke-linecap', 'round');
    needleG.appendChild(shaft);

    var tip = document.createElementNS(svgNS, 'polygon');
    tip.setAttribute('points', '70,28 66.5,37 73.5,37');
    tip.setAttribute('fill', '#2c3e50');
    needleG.appendChild(tip);

    svg.appendChild(needleG);

    // Center cap (covers needle base)
    var cap = document.createElementNS(svgNS, 'circle');
    cap.setAttribute('cx', '70');
    cap.setAttribute('cy', '80');
    cap.setAttribute('r', '5');
    cap.setAttribute('fill', '#2c3e50');
    svg.appendChild(cap);

    updateSensitivityDial(currentSensitivity);
}

function setSensitivityLevel(level) {
    if (sensitivityLevels.indexOf(level) === -1) return;

    currentSensitivity = level;
    updateSensitivityDial(level);

    // Broadcast to all OWLs
    sendCommand('all', 'set_sensitivity', level);
}

function updateSensitivityDial(level) {
    if (!level || sensitivityLevels.indexOf(level) === -1) return;

    currentSensitivity = level;

    // Highlight active segment, dim others
    var segments = document.querySelectorAll('.sensitivity-segment');
    segments.forEach(function(seg) {
        var segLevel = seg.getAttribute('data-level');
        if (segLevel === level) {
            seg.style.opacity = '1';
            seg.setAttribute('stroke-width', '18');
        } else {
            seg.style.opacity = '0.25';
            seg.setAttribute('stroke-width', '16');
        }
    });

    // Rotate needle
    var needleGroup = document.getElementById('sensitivity-needle');
    if (needleGroup) {
        var angle = sensitivityNeedleAngles[level] || 0;
        needleGroup.setAttribute('transform', 'rotate(' + angle + ' 70 80)');
    }

    // Update label
    var label = document.getElementById('sensitivity-level-text');
    if (label) {
        label.textContent = sensitivityLabels[level] || 'Medium';
        label.style.color = sensitivityColors[level] || sensitivityColors.medium;
    }
}

// ============================================
// AVERAGE LOOP TIME FROM OWL STATES
// (called from dashboard update for gauge when no actuation API)
// ============================================

function computeAvgLoopTimeFromOWLs() {
    var total = 0;
    var count = 0;
    for (var id in owlsData) {
        var lt = owlsData[id]?.avg_loop_time_ms;
        if (lt && lt > 0) {
            total += lt;
            count++;
        }
    }
    return count > 0 ? total / count : 0;
}
