/* ==========================================================================
   OWL Dashboard - LUT Panel (Standalone)
   Config-tab UI for painted LUT detection profiles: profile dropdown, single
   sensitivity slider, painter launch, and the detection interlock while the
   painter is open. The panel is always visible; the manual threshold sliders
   grey out (never hidden) while the LUT algorithm is active.
   ========================================================================== */

var lutPanelState = {
    lastSendTime: 0,
    sliderActive: false,
    detectionWasOn: false,
    profilesJson: '',
    // Panel forced visible after tapping the Painted mode button while the
    // OWL still reports another algorithm (e.g. no profile applied yet) —
    // otherwise "Paint weeds" would be unreachable.
    forced: false
};

function isLutPanelForced() { return lutPanelState.forced; }

function clearLutPanelForced() {
    lutPanelState.forced = false;
    setLutPanelVisible(false);
}

function setLutPanelVisible(show) {
    var panel = document.getElementById('lut-panel');
    if (panel) panel.style.display = show ? '' : 'none';
}

/* Tapping the Painted mode button: apply the selected profile, or reveal
   the panel (on the config tab) when nothing is painted yet. */
function activateLutMode() {
    lutPanelState.forced = true;
    setLutPanelVisible(true);
    if (typeof updatePipelineModeUI === 'function') updatePipelineModeUI('lut');

    var sel = document.getElementById('lutProfileSelect');
    if (sel && sel.value) {
        lutPanelState.lastSendTime = Date.now();
        apiRequest('/api/painter/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: sel.value,
                sensitivity: lutCurrentSensitivity()
            })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) {
                showNotification('Error', data.error || 'Apply failed', 'error');
            }
        }).catch(function () { });
    } else {
        // Jump to the config tab where the panel lives
        var configTab = document.querySelector('.nav-tab[data-tab="config"]');
        if (configTab && !configTab.classList.contains('active')) configTab.click();
        showNotification('Painted detection',
            'No painted profiles yet — press Paint weeds to create one', 'info', 4000);
    }
}

function initLutPanel() {
    if (typeof Painter === 'undefined') return;

    Painter.init({
        api: {
            session: '/api/painter/session',
            sessionEnd: '/api/painter/session/end',
            frame: '/api/painter/frame',
            frames: '/api/painter/session/frames',
            preview: '/api/painter/preview',
            save: '/api/painter/save'
        },
        onOpen: lutPainterInterlockOn,
        onClose: lutPainterInterlockOff,
        onSaved: function (meta, applied) {
            lutPanelState.lastSendTime = Date.now();
            if (applied) lutPanelState.forced = true;   // until stats report lut
        }
    });

    var openBtn = document.getElementById('openPainterBtn');
    if (openBtn) {
        openBtn.addEventListener('click', function () {
            Painter.open({ sensitivity: lutCurrentSensitivity() });
        });
    }

    var extendBtn = document.getElementById('extendLutProfileBtn');
    if (extendBtn) {
        extendBtn.addEventListener('click', function () {
            var sel = document.getElementById('lutProfileSelect');
            if (!sel || !sel.value) return;
            Painter.open({
                baseProfile: sel.value,
                sensitivity: lutCurrentSensitivity()
            });
        });
    }

    var profileSel = document.getElementById('lutProfileSelect');
    if (profileSel) profileSel.addEventListener('change', updateLutSwatch);

    var applyBtn = document.getElementById('applyLutProfileBtn');
    if (applyBtn) {
        applyBtn.addEventListener('click', function () {
            var sel = document.getElementById('lutProfileSelect');
            if (!sel || !sel.value) return;
            lutPanelState.lastSendTime = Date.now();
            apiRequest('/api/painter/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: sel.value,
                    sensitivity: lutCurrentSensitivity()
                })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (data.success) {
                    showNotification('Success', 'Profile "' + sel.value + '" applied', 'success', 2000);
                } else {
                    showNotification('Error', data.error || 'Apply failed', 'error');
                }
            }).catch(function (err) {
                showNotification('Error', err.message, 'error');
            });
        });
    }

    var deleteBtn = document.getElementById('deleteLutProfileBtn');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', function () {
            var sel = document.getElementById('lutProfileSelect');
            if (!sel || !sel.value) return;
            if (!confirm('Delete profile "' + sel.value + '"?')) return;
            apiRequest('/api/painter/profiles/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: sel.value })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) {
                    showNotification('Error', data.error || 'Delete failed', 'error');
                }
            }).catch(function () { });
        });
    }
}

/* Sensitivity is a native slider group now — its value lives in configParams */
function lutCurrentSensitivity() {
    return (typeof configParams !== 'undefined' && configParams.lut_sensitivity)
        ? configParams.lut_sensitivity.value : 50;
}

/* "Sprayed colours" strip for the selected profile — refetched only when
   the (profile, sensitivity) pair actually changes, not on every poll. */
var lutSwatchKey = '';

function updateLutSwatch() {
    var img = document.getElementById('lutSwatchImg');
    if (!img) return;
    var sel = document.getElementById('lutProfileSelect');
    var name = sel ? sel.value : '';
    if (!name) {
        img.style.display = 'none';
        lutSwatchKey = '';
        return;
    }
    var sens = lutCurrentSensitivity();
    var key = name + ':' + sens;
    if (key === lutSwatchKey) return;
    lutSwatchKey = key;
    img.onerror = function () { img.style.display = 'none'; };
    img.onload = function () { img.style.display = ''; };
    img.src = '/api/painter/swatch?name=' + encodeURIComponent(name) +
        '&sensitivity=' + encodeURIComponent(sens) + '&t=' + Date.now();
}

/* Detection interlock: never fire nozzles while the operator is head-down
   painting. Stream the full frame so the whole scene is paintable. */
function lutPainterInterlockOn() {
    apiRequest('/api/system_stats')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            lutPanelState.detectionWasOn = !!data.detection_enable;
            if (lutPanelState.detectionWasOn) {
                apiRequest('/api/detection/stop', { method: 'POST' }).catch(function () { });
            }
        }).catch(function () { });
    apiRequest('/api/preview-mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'full' })
    }).catch(function () { });
}

function lutPainterInterlockOff() {
    apiRequest('/api/preview-mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'cropped' })
    }).catch(function () { });
    if (lutPanelState.detectionWasOn) {
        lutPanelState.detectionWasOn = false;
        apiRequest('/api/detection/start', { method: 'POST' }).catch(function () { });
    }
}

/* Called from the stats polling loop (2s). Guarded against overwriting
   the dropdown for 3s after any send (snap-back pattern); the sensitivity
   value itself is synced by the generic slider engine. */
function syncLutPanelFromStats(data) {
    var isLut = data.algorithm === 'lut';
    if (isLut) lutPanelState.forced = false;   // OWL reached painted mode
    setLutPanelVisible(isLut || lutPanelState.forced);

    if (Date.now() - lutPanelState.lastSendTime < 3000) return;

    var sel = document.getElementById('lutProfileSelect');
    if (sel) {
        var profiles = data.available_lut_profiles || [];
        var json = JSON.stringify({ p: profiles, a: data.lut_profile });
        if (json !== lutPanelState.profilesJson) {
            lutPanelState.profilesJson = json;
            sel.innerHTML = '';
            if (!profiles.length) {
                var opt = document.createElement('option');
                opt.value = '';
                opt.textContent = 'No painted profiles yet';
                sel.appendChild(opt);
            }
            profiles.forEach(function (p) {
                var o = document.createElement('option');
                o.value = p.name;
                o.textContent = p.name + (p.is_builtin ? ' (built-in)'
                    : (p.created ? ' (' + p.created.slice(0, 10) + ')' : ''));
                sel.appendChild(o);
            });
            if (data.lut_profile) sel.value = data.lut_profile;
        }
    }
    updateLutSwatch();
}
