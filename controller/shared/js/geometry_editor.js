/* ==========================================================================
   OWL Controllers - Visual Geometry Editor (shared)

   Drag-on-the-feed editor for the per-edge crop rectangle and the actuation
   band. All maths run client-side in fractions of the image box, so it is
   resolution-independent and adds ZERO cost to the OWL detection loop — the
   backend only receives the final fractions on drag-end.

   Host integration:
     GeometryEditor.open({
       host:     <element, position:relative, contains the <img>>,
       img:      <the live-feed <img> element>,
       panel:    <element to render the Done/Cancel + nudge controls into>,
       values:   { crop_left, crop_right, crop_top, crop_bottom,
                   actuation_top, actuation_bottom },   // fractions
       relayNum: <int>,
       onCommit: function(values) { ... push to device ... },
       onClose:  function() { ... }
     });
     GeometryEditor.close();
     GeometryEditor.isOpen();
   ========================================================================== */

const GeometryEditor = (function () {
    'use strict';

    var MIN_SPAN = 0.05;   // min kept width/height (fraction)
    var MIN_BAND = 0.02;   // min actuation band height (fraction)
    var MAX_INSET = 0.49;

    var state = null;      // { crop_left, crop_right, crop_top, crop_bottom, actuation_top, actuation_bottom }
    var initialValues = null;  // snapshot taken on open, for Cancel revert
    var opts = null;
    var overlay = null;
    var dragEdge = null;

    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

    function isOpen() { return overlay !== null; }

    // ── Build the overlay DOM once per open ──────────────────────────────
    function _buildOverlay() {
        overlay = document.createElement('div');
        overlay.className = 'geo-overlay';
        overlay.innerHTML =
            '<div class="geo-dim geo-dim-top"></div>' +
            '<div class="geo-dim geo-dim-bottom"></div>' +
            '<div class="geo-dim geo-dim-left"></div>' +
            '<div class="geo-dim geo-dim-right"></div>' +
            '<div class="geo-crop-rect"></div>' +
            '<div class="geo-band"></div>' +
            '<div class="geo-lanes"></div>' +
            '<div class="geo-handle geo-h-left" data-edge="left"></div>' +
            '<div class="geo-handle geo-h-right" data-edge="right"></div>' +
            '<div class="geo-handle geo-h-top" data-edge="top"></div>' +
            '<div class="geo-handle geo-h-bottom" data-edge="bottom"></div>' +
            '<div class="geo-handle geo-handle-band geo-h-atop" data-edge="atop"></div>' +
            '<div class="geo-handle geo-handle-band geo-h-abottom" data-edge="abottom"></div>';
        opts.host.appendChild(overlay);

        overlay.querySelectorAll('.geo-handle').forEach(function (h) {
            h.addEventListener('pointerdown', _startDrag);
        });
    }

    // ── Render all element positions from state (% of overlay box) ───────
    function render() {
        if (!overlay) return;
        var L = state.crop_left, R = state.crop_right, T = state.crop_top, B = state.crop_bottom;
        var cw = 1 - L - R;          // cropped width fraction
        var ch = 1 - T - B;          // cropped height fraction
        var pc = function (f) { return (f * 100) + '%'; };

        // Dim panels (cropped-away area)
        _css('.geo-dim-top', { left: '0', top: '0', width: '100%', height: pc(T) });
        _css('.geo-dim-bottom', { left: '0', bottom: '0', width: '100%', height: pc(B) });
        _css('.geo-dim-left', { left: '0', top: pc(T), width: pc(L), height: pc(ch) });
        _css('.geo-dim-right', { right: '0', top: pc(T), width: pc(R), height: pc(ch) });

        // Crop rectangle
        _css('.geo-crop-rect', { left: pc(L), top: pc(T), width: pc(cw), height: pc(ch) });

        // Actuation band (band fractions are relative to cropped height)
        var bandTop = T + state.actuation_top * ch;
        var bandBot = T + state.actuation_bottom * ch;
        _css('.geo-band', { left: pc(L), width: pc(cw), top: pc(bandTop), height: pc(bandBot - bandTop) });

        // Lane lines (interior dividers across the cropped width)
        var lanes = overlay.querySelector('.geo-lanes');
        var html = '';
        for (var i = 1; i < (opts.relayNum || 1); i++) {
            var x = L + (i / opts.relayNum) * cw;
            html += '<div class="geo-lane" style="left:' + pc(x) + ';top:' + pc(T) + ';height:' + pc(ch) + '"></div>';
        }
        lanes.innerHTML = html;

        // Edge handles
        _css('.geo-h-left', { left: pc(L), top: pc(T + ch / 2) });
        _css('.geo-h-right', { left: pc(1 - R), top: pc(T + ch / 2) });
        _css('.geo-h-top', { left: pc(L + cw / 2), top: pc(T) });
        _css('.geo-h-bottom', { left: pc(L + cw / 2), top: pc(1 - B) });
        _css('.geo-h-atop', { left: pc(L + cw / 2), top: pc(bandTop) });
        _css('.geo-h-abottom', { left: pc(L + cw / 2), top: pc(bandBot) });

        _renderNudge();
    }

    function _css(sel, styles) {
        var el = overlay.querySelector(sel);
        if (!el) return;
        for (var k in styles) {
            // reset opposing anchors so left/right don't fight
            if (k === 'left') el.style.right = '';
            if (k === 'right') el.style.left = '';
            if (k === 'top') el.style.bottom = '';
            if (k === 'bottom') el.style.top = '';
            el.style[k] = styles[k];
        }
    }

    // ── Drag handling ────────────────────────────────────────────────────
    function _startDrag(e) {
        e.preventDefault();
        e.stopPropagation();
        dragEdge = e.currentTarget.dataset.edge;
        e.currentTarget.setPointerCapture(e.pointerId);
        e.currentTarget.addEventListener('pointermove', _onDrag);
        e.currentTarget.addEventListener('pointerup', _endDrag);
        e.currentTarget.addEventListener('pointercancel', _endDrag);
        e.currentTarget.classList.add('dragging');
    }

    function _onDrag(e) {
        if (!dragEdge) return;
        var rect = overlay.getBoundingClientRect();
        var fx = clamp((e.clientX - rect.left) / rect.width, 0, 1);
        var fy = clamp((e.clientY - rect.top) / rect.height, 0, 1);
        _applyEdge(dragEdge, fx, fy);
        render();
    }

    function _applyEdge(edge, fx, fy) {
        var ch = 1 - state.crop_top - state.crop_bottom;
        switch (edge) {
            case 'left':
                state.crop_left = clamp(fx, 0, Math.min(MAX_INSET, 1 - state.crop_right - MIN_SPAN));
                break;
            case 'right':
                state.crop_right = clamp(1 - fx, 0, Math.min(MAX_INSET, 1 - state.crop_left - MIN_SPAN));
                break;
            case 'top':
                state.crop_top = clamp(fy, 0, Math.min(MAX_INSET, 1 - state.crop_bottom - MIN_SPAN));
                break;
            case 'bottom':
                state.crop_bottom = clamp(1 - fy, 0, Math.min(MAX_INSET, 1 - state.crop_top - MIN_SPAN));
                break;
            case 'atop': {
                var aT = (fy - state.crop_top) / ch;
                state.actuation_top = clamp(aT, 0, state.actuation_bottom - MIN_BAND);
                break;
            }
            case 'abottom': {
                var aB = (fy - state.crop_top) / ch;
                state.actuation_bottom = clamp(aB, state.actuation_top + MIN_BAND, 1);
                break;
            }
        }
    }

    function _endDrag(e) {
        if (!dragEdge) return;
        var h = e.currentTarget;
        h.removeEventListener('pointermove', _onDrag);
        h.removeEventListener('pointerup', _endDrag);
        h.removeEventListener('pointercancel', _endDrag);
        h.classList.remove('dragging');
        dragEdge = null;
        _commit(false);
    }

    // ── Nudge controls (cab-precision) ───────────────────────────────────
    var NUDGE_ROWS = [
        { key: 'crop_left', label: 'Left' },
        { key: 'crop_right', label: 'Right' },
        { key: 'crop_top', label: 'Top' },
        { key: 'crop_bottom', label: 'Bottom' },
        { key: 'actuation_top', label: 'Band top' },
        { key: 'actuation_bottom', label: 'Band bottom' }
    ];

    function _buildPanel() {
        if (!opts.panel) return;
        var rows = NUDGE_ROWS.map(function (r) {
            return '<div class="geo-nudge-row" data-key="' + r.key + '">' +
                '<span class="geo-nudge-label">' + r.label + '</span>' +
                '<button class="geo-nudge-btn" data-delta="-0.01">&minus;</button>' +
                '<span class="geo-nudge-val"></span>' +
                '<button class="geo-nudge-btn" data-delta="0.01">+</button>' +
                '</div>';
        }).join('');
        var allRow = opts.allowAll
            ? '<label class="geo-apply-all"><input type="checkbox" id="geo-apply-all"> Apply to all OWLs</label>'
            : '';
        opts.panel.innerHTML =
            '<div class="geo-panel-title">Adjust crop &amp; actuation band</div>' +
            '<div class="geo-nudge-grid">' + rows + '</div>' +
            allRow +
            '<div class="geo-panel-actions">' +
            '<button class="geo-btn geo-btn-secondary" id="geo-cancel">Cancel</button>' +
            '<button class="geo-btn geo-btn-primary" id="geo-done">Done (save)</button>' +
            '</div>';

        opts.panel.querySelectorAll('.geo-nudge-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var key = btn.closest('.geo-nudge-row').dataset.key;
                _nudge(key, parseFloat(btn.dataset.delta));
            });
        });
        opts.panel.querySelector('#geo-cancel').addEventListener('click', cancel);
        opts.panel.querySelector('#geo-done').addEventListener('click', done);
    }

    function _nudge(key, delta) {
        var v = state[key] + delta;
        // Re-use edge clamps by mapping the key to an edge drag.
        if (key === 'crop_left') state.crop_left = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_right - MIN_SPAN));
        else if (key === 'crop_right') state.crop_right = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_left - MIN_SPAN));
        else if (key === 'crop_top') state.crop_top = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_bottom - MIN_SPAN));
        else if (key === 'crop_bottom') state.crop_bottom = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_top - MIN_SPAN));
        else if (key === 'actuation_top') state.actuation_top = clamp(v, 0, state.actuation_bottom - MIN_BAND);
        else if (key === 'actuation_bottom') state.actuation_bottom = clamp(v, state.actuation_top + MIN_BAND, 1);
        render();
        _commit(false);
    }

    function _renderNudge() {
        if (!opts.panel) return;
        NUDGE_ROWS.forEach(function (r) {
            var row = opts.panel.querySelector('.geo-nudge-row[data-key="' + r.key + '"] .geo-nudge-val');
            if (row) row.textContent = state[r.key].toFixed(2);
        });
    }

    // ── Commit / lifecycle ───────────────────────────────────────────────
    function _currentValues() {
        return {
            crop_left: round2(state.crop_left),
            crop_right: round2(state.crop_right),
            crop_top: round2(state.crop_top),
            crop_bottom: round2(state.crop_bottom),
            actuation_top: round2(state.actuation_top),
            actuation_bottom: round2(state.actuation_bottom)
        };
    }

    function _applyToAll() {
        var cb = opts && opts.panel && opts.panel.querySelector('#geo-apply-all');
        return !!(cb && cb.checked);
    }

    // persist=false → live preview only; persist=true → write to GEOMETRY.ini.
    function _commit(persist) {
        if (opts && typeof opts.onCommit === 'function') {
            opts.onCommit(_currentValues(), { persist: !!persist, applyToAll: _applyToAll() });
        }
    }

    function round2(v) { return Math.round(v * 100) / 100; }

    function open(o) {
        if (overlay) close();
        opts = o;
        var v = o.values || {};
        state = {
            crop_left: numOr(v.crop_left, 0.02),
            crop_right: numOr(v.crop_right, 0.02),
            crop_top: numOr(v.crop_top, 0.02),
            crop_bottom: numOr(v.crop_bottom, 0.02),
            actuation_top: numOr(v.actuation_top, 0.0),
            actuation_bottom: numOr(v.actuation_bottom, 1.0)
        };
        initialValues = Object.assign({}, state);   // snapshot for Cancel
        _buildOverlay();
        _buildPanel();
        render();
        if (o.host) {
            o.host.classList.add('geo-editing');
            // Mark the container (column / preview box) so its CSS can switch to a
            // fit-the-screen layout while editing.
            if (o.host.parentElement) o.host.parentElement.classList.add('geo-host-active');
        }
    }

    function numOr(v, d) {
        var n = parseFloat(v);
        return isNaN(n) ? d : n;
    }

    function _teardown() {
        if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
        if (opts && opts.host) {
            opts.host.classList.remove('geo-editing');
            if (opts.host.parentElement) opts.host.parentElement.classList.remove('geo-host-active');
        }
        if (opts && opts.panel) opts.panel.innerHTML = '';
        overlay = null;
        dragEdge = null;
    }

    function close() {
        var cb = opts && opts.onClose;
        _teardown();
        state = null;
        initialValues = null;
        opts = null;
        if (typeof cb === 'function') cb();
    }

    // Done = persist the current geometry to GEOMETRY.ini, then close.
    function done() {
        _commit(true);
        close();
    }

    // Cancel = revert to the snapshot taken on open (pushes the original values
    // back live so the device no longer reflects the abandoned drag), then close.
    function cancel() {
        if (state && initialValues) {
            state = Object.assign({}, initialValues);
            render();
            _commit(false);
        }
        close();
    }

    return { open: open, close: close, done: done, cancel: cancel, isOpen: isOpen, render: render };
})();
