// ============================================
// OWL Central Controller - Config Tab
// Range sliders, drag handling, fine-tune, preview
// ============================================

// ============================================
// SLIDER INITIALIZATION
// ============================================

let dragState = null;

function initSliders() {
    // Knob drag via pointer events
    document.querySelectorAll('.slider-knob').forEach(function(knob) {
        knob.addEventListener('pointerdown', startKnobDrag);
    });

    // Track click to jump nearest knob
    document.querySelectorAll('.slider-track').forEach(function(track) {
        track.addEventListener('pointerdown', handleTrackClick);
    });

    // Fine-tune buttons
    document.querySelectorAll('.fine-btn').forEach(function(btn) {
        btn.addEventListener('click', handleFineTune);
    });
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

    dragState = {
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
    if (!dragState) return;

    var param = dragState.param;
    var railRect = dragState.railRect;
    var p = configParams[param];
    if (!p) return;

    var x = e.clientX - railRect.left;
    var pct = Math.max(0, Math.min(100, (x / railRect.width) * 100));
    var val = Math.round((pct / 100) * (p.max - p.min) + p.min);

    // Constrain range slider pairs
    val = constrainRangeValue(dragState.knob, param, val);

    configParams[param].value = val;
    updateSlider(param);
}

function endKnobDrag(e) {
    if (!dragState) return;

    var knob = dragState.knob;
    var param = dragState.param;

    knob.removeEventListener('pointermove', onKnobDrag);
    knob.removeEventListener('pointerup', endKnobDrag);
    knob.removeEventListener('pointercancel', endKnobDrag);

    // Send final value
    sendConfigUpdate(param, configParams[param].value);
    dragState = null;
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

        var val = Math.round((pct / 100) * (p.max - p.min) + p.min);
        configParams[param].value = Math.max(p.min, Math.min(p.max, val));
        updateSlider(param);
        sendConfigUpdate(param, configParams[param].value);
    } else {
        // Range slider — move nearest knob
        var minParam = slider.dataset.paramMin;
        var maxParam = slider.dataset.paramMax;
        var pMin = configParams[minParam];
        var pMax = configParams[maxParam];
        if (!pMin || !pMax) return;

        var minPct = ((pMin.value - pMin.min) / (pMin.max - pMin.min)) * 100;
        var maxPct = ((pMax.value - pMax.min) / (pMax.max - pMax.min)) * 100;

        var targetParam = (Math.abs(pct - minPct) <= Math.abs(pct - maxPct)) ? minParam : maxParam;
        var tp = configParams[targetParam];
        var newVal = Math.round((pct / 100) * (tp.max - tp.min) + tp.min);

        // Constrain
        var knob = document.getElementById(targetParam + '-knob');
        newVal = constrainRangeValue(knob, targetParam, newVal);

        configParams[targetParam].value = newVal;
        updateSlider(targetParam);
        sendConfigUpdate(targetParam, newVal);

        // Activate this knob
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

    // Find the active knob in this group
    var activeKnob = group.querySelector('.slider-knob.active');
    if (!activeKnob) {
        activeKnob = group.querySelector('.slider-knob');
        if (activeKnob) activeKnob.classList.add('active');
    }
    if (!activeKnob) return;

    adjustParameter(activeKnob.dataset.param, delta);
}

// ============================================
// CONSTRAINT HELPER
// ============================================

function constrainRangeValue(knob, param, val) {
    var slider = knob.closest('.range-slider');
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
// SLIDER UPDATES
// ============================================

function adjustParameter(param, delta) {
    var p = configParams[param];
    if (!p) return;

    var newVal = Math.max(p.min, Math.min(p.max, p.value + delta));

    // Constrain range slider pairs
    var knob = document.getElementById(param + '-knob');
    if (knob) {
        newVal = constrainRangeValue(knob, param, newVal);
    }

    p.value = newVal;
    updateSlider(param);
    sendConfigUpdate(param, newVal);
}

function updateSlider(param) {
    var p = configParams[param];
    if (!p) return;

    var pct = ((p.value - p.min) / (p.max - p.min)) * 100;

    // Update value display
    var valueEl = document.getElementById(param + '-value');
    if (valueEl) valueEl.textContent = p.value;

    // Position knob
    var knob = document.getElementById(param + '-knob');
    if (knob) knob.style.left = pct + '%';

    // Update fill
    if (!knob) return;
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
            var minPct = ((pMin.value - pMin.min) / (pMin.max - pMin.min)) * 100;
            var maxPct = ((pMax.value - pMax.min) / (pMax.max - pMax.min)) * 100;
            fill.style.left = minPct + '%';
            fill.style.width = Math.max(0, maxPct - minPct) + '%';
        }
    } else {
        fill.style.left = '0';
        fill.style.width = pct + '%';
    }
}

function updateAllSliders() {
    for (var key in configParams) {
        updateSlider(key);
    }
}

// ============================================
// CONFIG COMMANDS
// ============================================

function sendConfigUpdate(param, value) {
    var sel = document.getElementById('config-editor-device');
    var target = sel ? sel.value : 'all';

    // Crop buffer uses dedicated MQTT command (not set_greenonbrown_param)
    if (param === 'crop_buffer_px') {
        sendCommand(target, 'set_crop_buffer', value);
    } else if (param === 'confidence') {
        // Convert percentage (0-100) to float (0.0-1.0) for OWL
        sendCommand(target, 'set_greenongreen_param', { key: 'confidence', value: value / 100 });
    } else {
        sendCommand(target, 'set_config', { key: param, value: value });
    }
}

function applyConfigToAll() {
    var count = 0;
    for (var key in configParams) {
        sendConfigUpdate(key, configParams[key].value);
        count++;
    }
    showToast('Applied ' + count + ' settings', 'success');
}

async function resetConfigDefaults() {
    await loadConfigDefaults();
    updateAllSliders();
    applyConfigToAll();
    showToast('Reset to default values', 'info');
}

// ============================================
// LIVE PREVIEW TOGGLE
// ============================================

let configPreviewActive = false;

function toggleConfigPreview() {
    var split = document.getElementById('config-split');
    var img = document.getElementById('config-preview-img');
    var btn = document.getElementById('config-preview-btn');
    var deviceSel = document.getElementById('config-editor-device');

    if (!split || !img) return;

    configPreviewActive = !configPreviewActive;

    if (configPreviewActive) {
        split.classList.add('preview-active');
        if (btn) {
            btn.textContent = 'HIDE PREVIEW';
            btn.classList.add('active');
        }
        var deviceId = deviceSel ? deviceSel.value : null;
        if (deviceId) {
            img.src = '/api/video_feed/' + deviceId;
        }
    } else {
        split.classList.remove('preview-active');
        if (btn) {
            btn.textContent = 'PREVIEW';
            btn.classList.remove('active');
        }
        img.src = '';
    }
}

function updateConfigPreviewDevice() {
    if (!configPreviewActive) return;

    var img = document.getElementById('config-preview-img');
    var deviceSel = document.getElementById('config-editor-device');
    if (!img || !deviceSel) return;

    var deviceId = deviceSel.value;
    img.src = deviceId ? '/api/video_feed/' + deviceId : '';
}

function stopConfigPreview() {
    if (!configPreviewActive) return;

    configPreviewActive = false;
    var split = document.getElementById('config-split');
    var img = document.getElementById('config-preview-img');
    var btn = document.getElementById('config-preview-btn');

    if (split) split.classList.remove('preview-active');
    if (img) img.src = '';
    if (btn) {
        btn.textContent = 'PREVIEW';
        btn.classList.remove('active');
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

// ============================================
// SLIDER VISIBILITY BY MODE
// ============================================

function updateSliderVisibility(algorithm) {
    var gobSliders = document.querySelectorAll('.config-slider-group:not(#crop-buffer-slider-group):not(#confidence-slider-group)');
    var bufferSlider = document.getElementById('crop-buffer-slider-group');
    var confidenceSlider = document.getElementById('confidence-slider-group');

    if (algorithm === 'gog') {
        // Pure AI: hide GoB sliders, show confidence only
        gobSliders.forEach(function(el) { el.style.display = 'none'; });
        if (bufferSlider) bufferSlider.style.display = 'none';
        if (confidenceSlider) confidenceSlider.style.display = '';
    } else if (algorithm === 'gog-hybrid') {
        // Hybrid: show GoB sliders + buffer + confidence
        gobSliders.forEach(function(el) { el.style.display = ''; });
        if (bufferSlider) bufferSlider.style.display = '';
        if (confidenceSlider) confidenceSlider.style.display = '';
    } else {
        // Colour: show GoB sliders, hide buffer and confidence
        gobSliders.forEach(function(el) { el.style.display = ''; });
        if (bufferSlider) bufferSlider.style.display = 'none';
        if (confidenceSlider) confidenceSlider.style.display = 'none';
    }
}
