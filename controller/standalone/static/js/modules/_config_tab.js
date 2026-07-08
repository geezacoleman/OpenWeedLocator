/* ==========================================================================
   OWL Dashboard - Config Tab Sliders (Standalone)
   Range sliders, drag handling, fine-tune buttons
   Ported from networked _config_tab.js, adapted for standalone MQTT
   ========================================================================== */

// Slider parameter state (synced from system stats polling)
const configParams = {
    exg_min: { value: 25, min: 0, max: 255 },
    exg_max: { value: 200, min: 0, max: 255 },
    hue_min: { value: 39, min: 0, max: 179 },
    hue_max: { value: 83, min: 0, max: 179 },
    saturation_min: { value: 50, min: 0, max: 255 },
    saturation_max: { value: 220, min: 0, max: 255 },
    brightness_min: { value: 60, min: 0, max: 255 },
    brightness_max: { value: 190, min: 0, max: 255 },
    min_detection_area: { value: 10, min: 1, max: 1000 },
    // Min weed size as % of the detection frame — log scale because useful
    // values span ~3 orders of magnitude (0.0005% ≈ 1px … 2% ≈ huge patch)
    min_detection_area_percent: { value: 0.003, min: 0.0005, max: 2, scale: 'log', decimals: 4, unit: '%' },
    lut_sensitivity: { value: 50, min: 0, max: 100 },
    crop_buffer_px: { value: 20, min: 0, max: 50 },
    confidence: { value: 50, min: 5, max: 100 }
};

// ── Log-scale aware slider maths (linear when p.scale is undefined) ──

function roundParamValue(p, val) {
    if (p.decimals) {
        var f = Math.pow(10, p.decimals);
        return Math.round(val * f) / f;
    }
    return Math.round(val);
}

function sliderPctToValue(p, pct) {
    var val = (p.scale === 'log')
        ? p.min * Math.pow(p.max / p.min, pct / 100)
        : (pct / 100) * (p.max - p.min) + p.min;
    return roundParamValue(p, Math.max(p.min, Math.min(p.max, val)));
}

function sliderValueToPct(p, val) {
    var v = Math.max(p.min, Math.min(p.max, val));
    return (p.scale === 'log')
        ? Math.log(v / p.min) / Math.log(p.max / p.min) * 100
        : ((v - p.min) / (p.max - p.min)) * 100;
}

function sliderStepValue(p, val, delta) {
    // Fine-tune: additive for linear params, multiplicative for log params
    var next = (p.scale === 'log') ? val * Math.pow(1.12, delta) : val + delta;
    return roundParamValue(p, Math.max(p.min, Math.min(p.max, next)));
}

function sliderDisplayValue(p) {
    var text = p.decimals ? Number(p.value).toFixed(p.decimals) : p.value;
    return p.unit ? text + p.unit : String(text);
}

let sliderDragState = null;
let slidersInitialised = false;
let lastSliderSendTime = 0;

// ============================================
// INITIALIZATION
// ============================================

function initSliders() {
    // Knob drag via pointer events
    document.querySelectorAll('.config-slider-group .slider-knob').forEach(function(knob) {
        knob.addEventListener('pointerdown', startKnobDrag);
    });

    // Track click to jump nearest knob
    document.querySelectorAll('.config-slider-group .slider-track').forEach(function(track) {
        track.addEventListener('pointerdown', handleTrackClick);
    });

    // Fine-tune buttons
    document.querySelectorAll('.config-slider-group .fine-btn').forEach(function(btn) {
        btn.addEventListener('click', handleFineTune);
    });

    slidersInitialised = true;
    updateAllSliders();
}

// ============================================
// KNOB DRAGGING
// ============================================

function startKnobDrag(e) {
    e.preventDefault();
    e.stopPropagation();
    var knob = e.currentTarget;
    var param = knob.dataset.param;
    var rail = knob.closest('.slider-rail');

    // Mark as active knob in this group
    var group = knob.closest('.config-slider-group');
    group.querySelectorAll('.slider-knob').forEach(function(k) { k.classList.remove('active'); });
    knob.classList.add('active');

    knob.setPointerCapture(e.pointerId);

    sliderDragState = {
        knob: knob,
        param: param,
        rail: rail,
        railRect: rail.getBoundingClientRect()
    };

    knob.addEventListener('pointermove', onKnobDrag);
    knob.addEventListener('pointerup', endKnobDrag);
    knob.addEventListener('pointercancel', endKnobDrag);
}

function onKnobDrag(e) {
    if (!sliderDragState) return;

    var param = sliderDragState.param;
    var railRect = sliderDragState.railRect;
    var p = configParams[param];
    if (!p) return;

    var x = e.clientX - railRect.left;
    var pct = Math.max(0, Math.min(100, (x / railRect.width) * 100));
    var val = sliderPctToValue(p, pct);

    val = constrainRangeValue(sliderDragState.knob, param, val);

    configParams[param].value = val;
    updateSlider(param);
}

function endKnobDrag(e) {
    if (!sliderDragState) return;

    var knob = sliderDragState.knob;
    var param = sliderDragState.param;

    knob.removeEventListener('pointermove', onKnobDrag);
    knob.removeEventListener('pointerup', endKnobDrag);
    knob.removeEventListener('pointercancel', endKnobDrag);

    // Send final value via standalone API
    sendSliderUpdate(param, configParams[param].value);
    sliderDragState = null;
}

// ============================================
// TRACK CLICK (tap to jump)
// ============================================

function handleTrackClick(e) {
    var track = e.currentTarget;
    var rail = track.closest('.slider-rail');
    var slider = rail.closest('.range-slider, .single-slider');
    var group = slider.closest('.config-slider-group');

    var railRect = rail.getBoundingClientRect();
    var x = e.clientX - railRect.left;
    var pct = Math.max(0, Math.min(100, (x / railRect.width) * 100));

    if (slider.classList.contains('single-slider')) {
        var param = slider.dataset.param;
        var p = configParams[param];
        if (!p) return;

        configParams[param].value = sliderPctToValue(p, pct);
        updateSlider(param);
        sendSliderUpdate(param, configParams[param].value);
    } else {
        var minParam = slider.dataset.paramMin;
        var maxParam = slider.dataset.paramMax;
        var pMin = configParams[minParam];
        var pMax = configParams[maxParam];
        if (!pMin || !pMax) return;

        var minPct = sliderValueToPct(pMin, pMin.value);
        var maxPct = sliderValueToPct(pMax, pMax.value);

        var targetParam = (Math.abs(pct - minPct) <= Math.abs(pct - maxPct)) ? minParam : maxParam;
        var tp = configParams[targetParam];
        var newVal = sliderPctToValue(tp, pct);

        var knob = document.getElementById(targetParam + '-knob');
        newVal = constrainRangeValue(knob, targetParam, newVal);

        configParams[targetParam].value = newVal;
        updateSlider(targetParam);
        sendSliderUpdate(targetParam, newVal);

        group.querySelectorAll('.slider-knob').forEach(function(k) { k.classList.remove('active'); });
        if (knob) knob.classList.add('active');
    }
}

// ============================================
// FINE-TUNE BUTTONS
// ============================================

function handleFineTune(e) {
    var btn = e.currentTarget;
    var delta = parseInt(btn.dataset.delta);
    var group = btn.closest('.config-slider-group');

    var activeKnob = group.querySelector('.slider-knob.active');
    if (!activeKnob) {
        activeKnob = group.querySelector('.slider-knob');
        if (activeKnob) activeKnob.classList.add('active');
    }
    if (!activeKnob) return;

    var param = activeKnob.dataset.param;
    var p = configParams[param];
    if (!p) return;

    var newVal = sliderStepValue(p, p.value, delta);
    newVal = constrainRangeValue(activeKnob, param, newVal);

    p.value = newVal;
    updateSlider(param);
    sendSliderUpdate(param, newVal);
}

// ============================================
// CONSTRAINT HELPER
// ============================================

function constrainRangeValue(knob, param, val) {
    var slider = knob ? knob.closest('.range-slider') : null;
    if (!slider) return Math.max(configParams[param].min, Math.min(configParams[param].max, val));

    var minParam = slider.dataset.paramMin;
    var maxParam = slider.dataset.paramMax;

    if (param === minParam && val > configParams[maxParam].value) {
        val = configParams[maxParam].value;
    }
    if (param === maxParam && val < configParams[minParam].value) {
        val = configParams[minParam].value;
    }

    return Math.max(configParams[param].min, Math.min(configParams[param].max, val));
}

// ============================================
// SLIDER VISUAL UPDATES
// ============================================

function updateSlider(param) {
    var p = configParams[param];
    if (!p) return;

    var pct = sliderValueToPct(p, p.value);

    // Update ALL value displays with IDs ending in param-value
    // (supports duplicate sliders across dashboard + config tabs)
    document.querySelectorAll('[id$="' + param + '-value"]').forEach(function(el) {
        el.textContent = sliderDisplayValue(p);
    });

    // Update ALL knobs + fills for this param
    document.querySelectorAll('.slider-knob[data-param="' + param + '"]').forEach(function(knob) {
        knob.style.left = pct + '%';

        var rail = knob.closest('.slider-rail');
        if (!rail) return;
        var fill = rail.querySelector('.slider-fill');
        if (!fill) return;

        var slider = rail.closest('.range-slider, .single-slider');
        if (slider && slider.classList.contains('range-slider')) {
            var minParam = slider.dataset.paramMin;
            var maxParam = slider.dataset.paramMax;
            var pMin = configParams[minParam];
            var pMax = configParams[maxParam];
            if (pMin && pMax) {
                var minPct = sliderValueToPct(pMin, pMin.value);
                var maxPct = sliderValueToPct(pMax, pMax.value);
                fill.style.left = minPct + '%';
                fill.style.width = Math.max(0, maxPct - minPct) + '%';
            }
        } else {
            fill.style.left = '0';
            fill.style.width = pct + '%';
        }
    });
}

function updateAllSliders() {
    for (var key in configParams) {
        updateSlider(key);
    }
}

// ============================================
// SEND COMMANDS VIA STANDALONE API
// ============================================

function sendSliderUpdate(param, value) {
    lastSliderSendTime = Date.now();
    if (param === 'lut_sensitivity' && typeof updateLutSwatch === 'function') {
        updateLutSwatch();
    }
    if (param === 'crop_buffer_px') {
        apiRequest('/api/config/crop_buffer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: value })
        }).catch(function() {});
    } else if (param === 'confidence') {
        apiRequest('/api/config/confidence', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: value / 100 })
        }).catch(function() {});
    } else {
        apiRequest('/api/config/param', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ param: param, value: value })
        }).catch(function() {});
    }
}

// ============================================
// SYNC SLIDERS FROM STATS POLLING
// ============================================

function syncSlidersFromStats(data) {
    if (!slidersInitialised || sliderDragState) return;
    // Don't overwrite values that were just sent (5s cooldown)
    if (Date.now() - lastSliderSendTime < 5000) return;

    var changed = false;
    var fields = ['exg_min', 'exg_max', 'hue_min', 'hue_max',
                  'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                  'min_detection_area', 'crop_buffer_px', 'lut_sensitivity'];

    for (var i = 0; i < fields.length; i++) {
        var key = fields[i];
        if (data[key] !== undefined && data[key] !== null && configParams[key]) {
            var val = parseInt(data[key]);
            if (!isNaN(val) && val !== configParams[key].value) {
                configParams[key].value = val;
                changed = true;
            }
        }
    }

    // Min weed size % is a float; when unset (0 = legacy px mode) seed the
    // slider from the px value converted against the frame area
    var mp = configParams.min_detection_area_percent;
    if (mp && typeof data.min_detection_area_percent === 'number') {
        var pctVal = data.min_detection_area_percent;
        if (pctVal <= 0) {
            var area = (data.resolution_width || 640) * (data.resolution_height || 480);
            pctVal = (data.min_detection_area || 10) / area * 100;
        }
        pctVal = roundParamValue(mp, Math.max(mp.min, Math.min(mp.max, pctVal)));
        if (pctVal !== mp.value) {
            mp.value = pctVal;
            changed = true;
        }
    }

    // Confidence is stored as float 0-1 in MQTT, displayed as 0-100
    if (data.confidence !== undefined && data.confidence !== null && configParams.confidence) {
        var confVal = Math.round(parseFloat(data.confidence) * 100);
        if (!isNaN(confVal) && confVal !== configParams.confidence.value) {
            configParams.confidence.value = confVal;
            changed = true;
        }
    }

    if (changed) {
        updateAllSliders();
        if (typeof updateLutSwatch === 'function') updateLutSwatch();
    }

    // Update slider visibility based on algorithm (a locally-forced Painted
    // panel shows the LUT layout even while the OWL still reports GoB)
    if (data.algorithm) {
        var forced = (typeof isLutPanelForced === 'function' && isLutPanelForced());
        updateSliderVisibility(forced ? 'lut' : data.algorithm);
    }
}

// ============================================
// SLIDER VISIBILITY BY MODE
// ============================================

function updateSliderVisibility(algorithm) {
    var gobSliders = document.querySelectorAll('.config-slider-group:not(#crop-buffer-slider-group):not(#confidence-slider-group):not(#lut-sensitivity-slider-group)');
    var bufferSlider = document.getElementById('crop-buffer-slider-group');
    var confidenceSlider = document.getElementById('confidence-slider-group');
    var dashConfidence = document.getElementById('dash-confidence-group');
    var lutSlider = document.getElementById('lut-sensitivity-slider-group');
    if (lutSlider) lutSlider.style.display = (algorithm === 'lut') ? '' : 'none';

    if (algorithm === 'lut') {
        // Painted: sensitivity + min weed size only — the LUT profile
        // replaces the colour thresholds
        var minSizeGroup = document.getElementById('min-weed-size-slider-group');
        gobSliders.forEach(function(el) {
            el.style.display = (el === minSizeGroup) ? '' : 'none';
        });
        if (bufferSlider) bufferSlider.style.display = 'none';
        if (confidenceSlider) confidenceSlider.style.display = 'none';
        if (dashConfidence) dashConfidence.style.display = 'none';
        return;
    }

    if (algorithm === 'gog') {
        gobSliders.forEach(function(el) { el.style.display = 'none'; });
        if (bufferSlider) bufferSlider.style.display = 'none';
        if (confidenceSlider) confidenceSlider.style.display = '';
        if (dashConfidence) dashConfidence.style.display = '';
    } else if (algorithm === 'gog-hybrid') {
        gobSliders.forEach(function(el) { el.style.display = ''; });
        if (bufferSlider) bufferSlider.style.display = '';
        if (confidenceSlider) confidenceSlider.style.display = '';
        if (dashConfidence) dashConfidence.style.display = '';
    } else {
        gobSliders.forEach(function(el) { el.style.display = ''; });
        if (bufferSlider) bufferSlider.style.display = 'none';
        if (confidenceSlider) confidenceSlider.style.display = 'none';
        if (dashConfidence) dashConfidence.style.display = 'none';
    }
}

// ============================================
// ADVANCED SETTINGS TOGGLE
// ============================================

function toggleAdvancedSettings() {
    var toggle = document.getElementById('config-advanced-toggle');
    var body = document.getElementById('config-advanced-body');

    if (!toggle || !body) return;

    var isOpen = toggle.classList.toggle('open');
    body.classList.toggle('open', isOpen);
}
