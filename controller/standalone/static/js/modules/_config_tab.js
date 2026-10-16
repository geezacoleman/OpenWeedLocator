/* ==========================================================================
   OWL Dashboard - detection settings (Standalone)
   − / + steppers for Colour tuning, AI confidence and crop buffer, bound
   to configParams (owl_ui.js paramSteppers), sent through the standalone API
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

// ── Log-scale aware stepper maths (linear when p.scale is undefined) ──

function roundParamValue(p, val) {
    if (p.decimals) {
        var f = Math.pow(10, p.decimals);
        return Math.round(val * f) / f;
    }
    return Math.round(val);
}

function sliderStepValue(p, val, delta) {
    // One step: additive for linear params, multiplicative for log params
    var next = (p.scale === 'log') ? val * Math.pow(1.12, delta) : val + delta;
    return roundParamValue(p, Math.max(p.min, Math.min(p.max, next)));
}

function sliderDisplayValue(p) {
    var text = p.decimals ? Number(p.value).toFixed(p.decimals) : p.value;
    return p.unit ? text + p.unit : String(text);
}

let slidersInitialised = false;
let lastSliderSendTime = 0;

// ============================================
// INITIALIZATION
// ============================================

// Binds every [data-pstep] stepper (Dashboard and Settings) to configParams.
// Holding − / + repeats; the value is sent once the taps stop.
function initParamSteppers() {
    OwlUI.paramSteppers(document, {
        params: configParams,
        stepValue: sliderStepValue,
        render: updateSlider,
        commit: sendSliderUpdate,
        touched: function () { lastSliderSendTime = Date.now(); },
    });
    slidersInitialised = true;
    updateAllSliders();
}

// Name kept from the slider days: every caller redraws a value with it
function updateSlider(param) {
    var p = configParams[param];
    if (p) OwlUI.showParam(param, sliderDisplayValue(p));
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
    if (!slidersInitialised) return;
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
// SETTINGS BY MODE (shared with the networked controller)
// ============================================

function updateSliderVisibility(algorithm) {
    OwlUI.showModeSettings(algorithm);
}
