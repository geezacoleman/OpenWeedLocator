// ============================================
// OWL Central Controller - Actuation Module
// Speed gauges, spray length, coverage warning
// ============================================

let actuationPollInterval = null;
const ACTUATION_POLL_MS = 1000;

// Spray length (cm of ground per spray): only used with a GPS speed, sent
// through /api/actuation/config. AI confidence and crop buffer live on the
// Dashboard (configParams).
let sprayLengthStep = null;

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
    // Shown as frames per second: the OWL's camera read blocks per frame,
    // so 1000 / avg loop time is true detection throughput
    var valueEl = document.getElementById('loop-gauge-value');
    var fillEl = document.getElementById('loop-gauge-fill');

    if (!valueEl || !fillEl) return;

    // A YOLO model on a Pi legitimately runs ~8-15 fps — hold AI/Hybrid mode
    // to an honest bar so the gauge doesn't cry wolf all season.
    var aiMode = dialMode() === 'gog' || dialMode() === 'hybrid';
    var maxFps = aiMode ? 20 : 40;
    var goodFps = aiMode ? 8 : 20;
    var warnFps = aiMode ? 4 : 10;

    var fps = avgMs > 0 ? 1000 / avgMs : 0;
    var fraction = Math.min(fps / maxFps, 1);

    valueEl.textContent = fps > 0 ? fps.toFixed(0) : '--';
    updateGaugeArc(fillEl, fraction);

    fillEl.classList.remove('loop-good', 'loop-warn', 'loop-danger', 'loop-idle');
    if (fps <= 0) fillEl.classList.add('loop-idle');   // no data — neutral, not "good"
    else if (fps >= goodFps) fillEl.classList.add('loop-good');
    else if (fps >= warnFps) fillEl.classList.add('loop-warn');
    else fillEl.classList.add('loop-danger');
}

function updateDurationGauge(durationSec) {
    var valueEl = document.getElementById('duration-gauge-value');
    var fillEl = document.getElementById('duration-gauge-fill');
    if (!valueEl || !fillEl) return;

    var ms = durationSec * 1000;
    var maxMs = 500;
    var fraction = Math.min(ms / maxMs, 1);

    valueEl.textContent = ms > 0 ? ms.toFixed(0) : '--';
    updateGaugeArc(fillEl, fraction);

    fillEl.classList.remove('duration-normal', 'duration-medium', 'duration-long');
    if (ms > 300) fillEl.classList.add('duration-long');
    else if (ms > 150) fillEl.classList.add('duration-medium');
    else fillEl.classList.add('duration-normal');
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
        updateDurationGauge(data.actuation_duration || 0);

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

        // Spray length: greyed (never hidden) without a GPS speed
        if (sprayLengthStep) {
            if (typeof data.actuation_length_cm === 'number') sprayLengthStep.set(data.actuation_length_cm);
            OwlUI.stepOff(document.getElementById('sprayLength'), (data.gps_status || 'no_gps') === 'no_gps',
                'Used when there is a GPS speed. Without one the OWLs use their fixed spray time.');
        }

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
// SPRAY LENGTH (Settings, Spraying)
// ============================================

function initSprayLength() {
    var el = document.getElementById('sprayLength');
    if (!el) return;
    sprayLengthStep = OwlUI.stepper(el, {
        value: null, min: 2, max: 50, step: 1, unit: 'cm', label: 'Spray length (cm)', settleMs: 400,
        onChange: function (val) {
            fetch('/api/actuation/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ actuation_length_cm: val })
            }).catch(function (err) {
                console.error('Failed to send actuation config:', err);
            });
        },
    });
}

// ============================================
// SENSITIVITY DIAL (3-segment SVG arc)
// ============================================

var sensitivityLevels = ['low', 'medium', 'high'];
var sensitivityColors = {
    low: '#3498db',
    medium: '#27ae60',
    high: '#e67e22',
    custom: '#7f8c8d'
};
var sensitivityLabels = {
    low: 'Low',
    medium: 'Medium',
    high: 'High',
    custom: 'Custom'
};
// Needle angle for each level (degrees, 0 = straight up)
var sensitivityNeedleAngles = { low: -61, medium: 0, high: 61 };
var currentSensitivity = 'medium';

// The dial drives whichever detection mode is active: Colour -> GoB preset,
// Painted -> lut_sensitivity, AI -> model confidence (lower = sprays more),
// Hybrid -> GoB preset + confidence together. When the underlying value was
// fine-tuned on the config tab and matches no level, the dial shows Custom.
var lutSensitivityLevels = { low: 30, medium: 50, high: 70 };
var LUT_LEVEL_TOLERANCE = 2;
var confidenceLevels = { low: 0.65, medium: 0.50, high: 0.35 };
var CONFIDENCE_TOLERANCE = 0.02;
var sensitivityModeCaptions = {
    gob: 'Sensitivity',
    lut: 'Painted sensitivity',
    gog: 'AI sensitivity',
    hybrid: 'Colour + AI sensitivity'
};

function dialMode() {
    return (typeof currentPipelineMode !== 'undefined') ? currentPipelineMode : 'gob';
}

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
    currentSensitivity = level;
    updateSensitivityDial(level);

    var mode = dialMode();

    if (mode === 'gob' || mode === 'hybrid') {
        // GoB preset to the selected OWL(s)
        sendCommand(typeof scopeTarget === 'function' ? scopeTarget() : 'all', 'set_sensitivity', level);
    }
    if (mode === 'lut') {
        // Same path as the config-tab slider so both stay in step
        var lutVal = lutSensitivityLevels[level];
        if (typeof configParams !== 'undefined' && configParams.lut_sensitivity) {
            configParams.lut_sensitivity.value = lutVal;
            if (typeof updateSlider === 'function') updateSlider('lut_sensitivity');
        }
        if (typeof sendConfigUpdate === 'function') sendConfigUpdate('lut_sensitivity', lutVal);
    }
    if (mode === 'gog' || mode === 'hybrid') {
        var confPct = confidenceLevels[level] * 100;
        if (typeof configParams !== 'undefined' && configParams.confidence) {
            configParams.confidence.value = confPct;
            if (typeof updateSlider === 'function') updateSlider('confidence');
        }
        if (typeof sendConfigUpdate === 'function') sendConfigUpdate('confidence', confPct);
    }

    // Snap-back guard: don't let the next heartbeats undo the click
    if (typeof lastSliderSendTime !== 'undefined') lastSliderSendTime = Date.now();
}

/* Map heartbeat state to the level the dial should display for the active
   mode; 'custom' when the value matches no level. In Colour mode the raw
   preset name passes through so custom presets show by name, dimmed. */
function sensitivityLevelFromState(mode, owl) {
    if (mode === 'lut') {
        var v = parseFloat(owl.lut_sensitivity);
        if (isNaN(v)) return null;
        for (var L in lutSensitivityLevels) {
            if (Math.abs(v - lutSensitivityLevels[L]) <= LUT_LEVEL_TOLERANCE) return L;
        }
        return 'custom';
    }
    if (mode === 'gog' || mode === 'hybrid') {
        var c = parseFloat(owl.confidence);
        if (isNaN(c)) return null;
        var match = null;
        for (var K in confidenceLevels) {
            if (Math.abs(c - confidenceLevels[K]) <= CONFIDENCE_TOLERANCE) { match = K; break; }
        }
        if (mode === 'gog') return match || 'custom';
        // Hybrid: the GoB preset and confidence must agree on a level
        var preset = (owl.sensitivity_level || '').toLowerCase();
        return (match && preset === match) ? match : 'custom';
    }
    return owl.sensitivity_level || null;
}

function syncSensitivityFromOwl(owl) {
    updateSensitivityModeCaption();
    if (!owl) return;
    if (typeof lastSliderSendTime !== 'undefined'
            && (Date.now() - lastSliderSendTime) < 5000) return;
    var level = sensitivityLevelFromState(dialMode(), owl);
    if (level) updateSensitivityDial(level);
}

function updateSensitivityModeCaption() {
    var el = document.getElementById('sensitivity-mode-caption');
    if (el) el.textContent = sensitivityModeCaptions[dialMode()] || 'Sensitivity';
}

function updateSensitivityDial(level) {
    if (!level) return;

    currentSensitivity = level;

    // Field: the Low / Medium / High segmented control (no match = Custom)
    document.querySelectorAll('#sensitivity-seg [data-level]').forEach(function(b) {
        b.classList.toggle('active', b.dataset.level === level);
    });

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
        label.textContent = sensitivityLabels[level] || level.charAt(0).toUpperCase() + level.slice(1);
        label.style.color = sensitivityColors[level] || '#555';
    }
}
