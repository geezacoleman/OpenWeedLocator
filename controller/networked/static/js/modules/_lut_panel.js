/* ==========================================================================
   OWL Central Controller - LUT Panel (Networked)
   Config-tab UI for painted LUT detection profiles. Mirrors the standalone
   _lut_panel.js but fleet-wide: frames come from the selected preview
   device, apply deploys the profile to all connected OWLs, and the panel
   syncs from owlsData (the /api/owls poll).
   ========================================================================== */

var lutPanelState = {
    lastSendTime: 0,
    sliderActive: false,
    detectingIds: [],
    paintDeviceId: null,
    profilesJson: '',
    // Panel forced visible after tapping the Painted mode button while the
    // fleet still reports another algorithm (e.g. no profile applied yet) —
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

/* Tapping the Painted mode button: apply the selected profile fleet-wide,
   or just reveal the panel when nothing is painted yet. */
function activateLutMode() {
    lutPanelState.forced = true;
    setLutPanelVisible(true);
    if (typeof updatePipelineModeUI === 'function') updatePipelineModeUI('lut');

    var sel = document.getElementById('lutProfileSelect');
    if (sel && sel.value) {
        lutPanelState.lastSendTime = Date.now();
        fetch('/api/painter/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: sel.value,
                sensitivity: lutCurrentSensitivity()
            })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) showToast(data.error || 'Apply failed', 'error');
        }).catch(function () { });
    } else {
        showToast('No painted profiles yet — press Paint weeds to create one', 'info');
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
        framePayload: function () {
            return { device_id: getSelectedPreviewDevice() || '' };
        },
        onOpen: lutPainterInterlockOn,
        onClose: lutPainterInterlockOff,
        onSaved: function (meta, applied) {
            lutPanelState.lastSendTime = Date.now();
            if (applied) lutPanelState.forced = true;   // until state reports lut
            refreshLutProfiles();
        }
    });

    var openBtn = document.getElementById('openPainterBtn');
    if (openBtn) {
        openBtn.addEventListener('click', function () {
            if (!getSelectedPreviewDevice()) {
                showToast('No OWLs connected', 'error');
                return;
            }
            Painter.open({ sensitivity: lutCurrentSensitivity() });
        });
    }

    var extendBtn = document.getElementById('extendLutProfileBtn');
    if (extendBtn) {
        extendBtn.addEventListener('click', function () {
            var sel = document.getElementById('lutProfileSelect');
            if (!sel || !sel.value) return;
            if (!getSelectedPreviewDevice()) {
                showToast('No OWLs connected', 'error');
                return;
            }
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
            fetch('/api/painter/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: sel.value,
                    sensitivity: lutCurrentSensitivity()
                })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (data.success) {
                    showToast('Profile "' + sel.value + '" deployed to ' +
                        (data.sent_to || []).length + ' OWLs', 'success');
                } else {
                    showToast(data.error || 'Deploy failed', 'error');
                }
            }).catch(function (err) { showToast(err.message, 'error'); });
        });
    }

    var deleteBtn = document.getElementById('deleteLutProfileBtn');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', function () {
            var sel = document.getElementById('lutProfileSelect');
            if (!sel || !sel.value) return;
            if (!confirm('Delete profile "' + sel.value + '" from the library and all OWLs?')) return;
            fetch('/api/painter/profiles/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: sel.value })
            }).then(function (r) { return r.json(); }).then(function (data) {
                if (!data.success) showToast(data.error || 'Delete failed', 'error');
                refreshLutProfiles();
            }).catch(function () { });
        });
    }

    refreshLutProfiles();
}

/* Sensitivity is a native slider group now — its value lives in configParams */
function lutCurrentSensitivity() {
    return (typeof configParams !== 'undefined' && configParams.lut_sensitivity)
        ? configParams.lut_sensitivity.value : 50;
}

/* "Sprayed colours" strip for the selected profile — refetched only when
   the (profile, sensitivity) pair actually changes, not on every poll. */
var lutSwatchKey = '';
var lutSwatchObjectUrl = null;

function setLutCoverage(pct) {
    var fill = document.getElementById('lutCoverageFill');
    var text = document.getElementById('lutCoverageText');
    if (!fill || !text) return;
    if (pct === null || isNaN(pct)) {
        fill.style.height = '0';
        text.textContent = '--';
        return;
    }
    // Typical profiles cover 2-10% of colour space — scale against a 25%
    // cap so slider motion is visible; the text carries the exact value
    var h = Math.min(100, (pct / 25) * 100);
    if (pct > 0 && h < 6) h = 6;
    fill.style.height = h + '%';
    text.textContent = pct.toFixed(1) + '% of colours';
}

function updateLutSwatch() {
    var img = document.getElementById('lutSwatchImg');
    if (!img) return;
    var sel = document.getElementById('lutProfileSelect');
    var name = sel ? sel.value : '';
    if (!name) {
        img.style.display = 'none';
        setLutCoverage(null);
        lutSwatchKey = '';
        return;
    }
    var sens = lutCurrentSensitivity();
    var key = name + ':' + sens;
    if (key === lutSwatchKey) return;
    lutSwatchKey = key;
    fetch('/api/painter/swatch?name=' + encodeURIComponent(name) +
          '&sensitivity=' + encodeURIComponent(sens) + '&t=' + Date.now())
        .then(function (r) {
            if (!r.ok) throw new Error('swatch ' + r.status);
            var coverage = parseFloat(r.headers.get('X-Coverage'));
            return r.blob().then(function (blob) {
                if (lutSwatchObjectUrl) URL.revokeObjectURL(lutSwatchObjectUrl);
                lutSwatchObjectUrl = URL.createObjectURL(blob);
                img.src = lutSwatchObjectUrl;
                img.style.display = '';
                setLutCoverage(coverage);
            });
        })
        .catch(function () {
            img.style.display = 'none';
            setLutCoverage(null);
        });
}

function refreshLutProfiles() {
    fetch('/api/painter/profiles')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (!data.success) return;
            renderLutProfileOptions(data.profiles || [], data.active || '');
        }).catch(function () { });
}

function renderLutProfileOptions(profiles, active) {
    var sel = document.getElementById('lutProfileSelect');
    if (!sel) return;
    var json = JSON.stringify({ p: profiles.map(function (p) { return p.name; }), a: active });
    if (json === lutPanelState.profilesJson) return;
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
    if (active) sel.value = active;
    updateLutSwatch();
}

/* Detection interlock: pause the whole fleet while painting, restore the
   units that were detecting on exit. Stream the paint device full-frame. */
function lutPainterInterlockOn() {
    lutPanelState.detectingIds = [];
    for (var id in owlsData) {
        if (owlsData[id] && owlsData[id].connected && owlsData[id].detection_enable) {
            lutPanelState.detectingIds.push(id);
        }
    }
    if (lutPanelState.detectingIds.length) {
        sendCommand('all', 'toggle_detection', false);
    }
    lutPanelState.paintDeviceId = getSelectedPreviewDevice();
    if (lutPanelState.paintDeviceId) {
        fetch('/api/preview-mode/' + lutPanelState.paintDeviceId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'full' })
        }).catch(function () { });
    }
}

function lutPainterInterlockOff() {
    if (lutPanelState.paintDeviceId) {
        fetch('/api/preview-mode/' + lutPanelState.paintDeviceId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'cropped' })
        }).catch(function () { });
        lutPanelState.paintDeviceId = null;
    }
    if (lutPanelState.detectingIds.length) {
        // toggle_detection per device flips the (now off) state back on
        lutPanelState.detectingIds.forEach(function (id) {
            sendCommand(id, 'toggle_detection');
        });
        lutPanelState.detectingIds = [];
    }
    refreshLutProfiles();
}

/* Called from the /api/owls poll with the first connected OWL's state. */
function syncLutPanelFromOwl(owl) {
    var isLut = !!(owl && owl.algorithm === 'lut');
    if (isLut) lutPanelState.forced = false;   // fleet reached painted mode
    setLutPanelVisible(isLut || lutPanelState.forced);

    if (Date.now() - lutPanelState.lastSendTime < 3000) return;

    if (owl && owl.available_lut_profiles) {
        renderLutProfileOptions(owl.available_lut_profiles, owl.lut_profile || '');
    }
}
