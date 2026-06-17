/* ==========================================================================
   OWL Controllers - Visual Geometry Editor (shared)

   Drag-on-the-feed editor for the per-edge crop rectangle and the actuation
   band. All maths run client-side in fractions of the image box, so it is
   resolution-independent and adds ZERO cost to the OWL detection loop — the
   backend only receives the final fractions on commit.

   Multi-instance: GeometryEditor.create(opts) returns one editor bound to a
   feed (host + img). Several can run at once (networked 2-up). A single shared
   controls panel (nudge ± + Done/Cancel) is rendered with mountControls() and
   acts on whichever instance is "active".

   Backward-compatible singleton API (used by standalone, single feed):
     GeometryEditor.open({host, img, panel, values, relayNum, allowAll,
                          onCommit, onClose});
     GeometryEditor.close(); GeometryEditor.isOpen();

   Per-instance:
     var ed = GeometryEditor.create({host, img, values, relayNum, label,
                                     onCommit(values,{persist}), onActivate(ed),
                                     onChange(ed)});
     ed.getValues(); ed.setActive(bool); ed.nudge(key,delta);
     ed.revert(); ed.commitPersist(); ed.destroy();
   ========================================================================== */

const GeometryEditor = (function () {
    'use strict';

    var MIN_SPAN = 0.05;   // min kept width/height (fraction)
    var MIN_BAND = 0.02;   // min actuation band height (fraction)
    var MAX_INSET = 0.49;
    var ZOOM_MIN = 1, ZOOM_MAX = 4, ZOOM_STEP = 0.25;

    var NUDGE_ROWS = [
        { key: 'crop_left', label: 'Left' },
        { key: 'crop_right', label: 'Right' },
        { key: 'crop_top', label: 'Top' },
        { key: 'crop_bottom', label: 'Bottom' },
        { key: 'actuation_top', label: 'Band top' },
        { key: 'actuation_bottom', label: 'Band bottom' }
    ];

    function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
    function round2(v) { return Math.round(v * 100) / 100; }
    function numOr(v, d) { var n = parseFloat(v); return isNaN(n) ? d : n; }

    // ── A single editor instance bound to one feed ───────────────────────
    function create(o) {
        var state = {
            crop_left: numOr((o.values || {}).crop_left, 0.02),
            crop_right: numOr((o.values || {}).crop_right, 0.02),
            crop_top: numOr((o.values || {}).crop_top, 0.02),
            crop_bottom: numOr((o.values || {}).crop_bottom, 0.02),
            actuation_top: numOr((o.values || {}).actuation_top, 0.0),
            actuation_bottom: numOr((o.values || {}).actuation_bottom, 1.0)
        };
        var snapshot = Object.assign({}, state);
        var relayNum = o.relayNum || 1;

        var zoom = 1, panX = 0, panY = 0;
        var content, overlay, zoombar, dragEdge = null, panning = null;
        var inst = {};

        // Build: wrap the img + overlay in a transformable content layer inside
        // the host, which clips (overflow hidden) for zoom/pan.
        function build() {
            o.host.classList.add('geo-editing');
            o.host.style.overflow = 'hidden';   // clip the zoomed/panned content
            if (o.host.parentElement) o.host.parentElement.classList.add('geo-host-active');

            content = document.createElement('div');
            content.className = 'geo-content';
            o.host.insertBefore(content, o.img);
            content.appendChild(o.img);

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
            content.appendChild(overlay);

            overlay.querySelectorAll('.geo-handle').forEach(function (h) {
                h.addEventListener('pointerdown', _startEdgeDrag);
            });
            // Background drag pans; pointer-down anywhere activates this feed.
            overlay.addEventListener('pointerdown', _startPan);

            // Zoom toolbar + optional label
            zoombar = document.createElement('div');
            zoombar.className = 'geo-zoombar';
            zoombar.innerHTML =
                (o.label ? '<span class="geo-zoom-label">' + escapeText(o.label) + '</span>' : '') +
                '<button class="geo-zoom-btn" data-z="out">&minus;</button>' +
                '<span class="geo-zoom-val">1.0&times;</span>' +
                '<button class="geo-zoom-btn" data-z="in">+</button>' +
                '<button class="geo-zoom-btn geo-zoom-reset" data-z="reset">Reset</button>';
            o.host.appendChild(zoombar);
            zoombar.querySelectorAll('.geo-zoom-btn').forEach(function (b) {
                b.addEventListener('click', function (e) {
                    e.stopPropagation();
                    if (b.dataset.z === 'in') _setZoom(zoom + ZOOM_STEP);
                    else if (b.dataset.z === 'out') _setZoom(zoom - ZOOM_STEP);
                    else { zoom = 1; panX = 0; panY = 0; _applyTransform(); }
                });
            });
            o.host.addEventListener('wheel', _onWheel, { passive: false });

            render();
            _applyTransform();
        }

        function escapeText(s) {
            return String(s).replace(/[&<>"]/g, function (c) {
                return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
            });
        }

        // ── Zoom / pan ────────────────────────────────────────────────────
        function _setZoom(z) {
            zoom = clamp(z, ZOOM_MIN, ZOOM_MAX);
            if (zoom === 1) { panX = 0; panY = 0; }
            _applyTransform();
        }
        function _applyTransform() {
            if (!content) return;
            content.style.transformOrigin = '0 0';
            content.style.transform = 'translate(' + panX + 'px,' + panY + 'px) scale(' + zoom + ')';
            var val = zoombar && zoombar.querySelector('.geo-zoom-val');
            if (val) val.textContent = zoom.toFixed(1) + '×';
            // Keep handles a roughly constant size regardless of zoom.
            if (overlay) {
                overlay.querySelectorAll('.geo-handle').forEach(function (h) {
                    h.style.transform = 'translate(-50%,-50%) scale(' + (1 / zoom) + ')';
                });
            }
        }
        function _onWheel(e) {
            e.preventDefault();
            _setZoom(zoom + (e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP));
        }
        function _startPan(e) {
            if (e.target.classList.contains('geo-handle')) return;  // handle drag, not pan
            activate();
            if (zoom <= 1) return;   // nothing to pan at 1x
            panning = { x: e.clientX, y: e.clientY, px: panX, py: panY };
            overlay.setPointerCapture(e.pointerId);
            overlay.addEventListener('pointermove', _onPan);
            overlay.addEventListener('pointerup', _endPan);
            overlay.addEventListener('pointercancel', _endPan);
        }
        function _onPan(e) {
            if (!panning) return;
            panX = panning.px + (e.clientX - panning.x);
            panY = panning.py + (e.clientY - panning.y);
            _applyTransform();
        }
        function _endPan(e) {
            panning = null;
            overlay.removeEventListener('pointermove', _onPan);
            overlay.removeEventListener('pointerup', _endPan);
            overlay.removeEventListener('pointercancel', _endPan);
        }

        // ── Render ──────────────────────────────────────────────────────────
        function pc(f) { return (f * 100) + '%'; }
        function render() {
            if (!overlay) return;
            var L = state.crop_left, R = state.crop_right, T = state.crop_top, B = state.crop_bottom;
            var cw = 1 - L - R, ch = 1 - T - B;
            _css('.geo-dim-top', { left: '0', top: '0', width: '100%', height: pc(T) });
            _css('.geo-dim-bottom', { left: '0', bottom: '0', width: '100%', height: pc(B) });
            _css('.geo-dim-left', { left: '0', top: pc(T), width: pc(L), height: pc(ch) });
            _css('.geo-dim-right', { right: '0', top: pc(T), width: pc(R), height: pc(ch) });
            _css('.geo-crop-rect', { left: pc(L), top: pc(T), width: pc(cw), height: pc(ch) });
            var bandTop = T + state.actuation_top * ch;
            var bandBot = T + state.actuation_bottom * ch;
            _css('.geo-band', { left: pc(L), width: pc(cw), top: pc(bandTop), height: pc(bandBot - bandTop) });

            var lanes = overlay.querySelector('.geo-lanes');
            var html = '';
            for (var i = 1; i < relayNum; i++) {
                var x = L + (i / relayNum) * cw;
                html += '<div class="geo-lane" style="left:' + pc(x) + ';top:' + pc(T) + ';height:' + pc(ch) + '"></div>';
            }
            lanes.innerHTML = html;

            _css('.geo-h-left', { left: pc(L), top: pc(T + ch / 2) });
            _css('.geo-h-right', { left: pc(1 - R), top: pc(T + ch / 2) });
            _css('.geo-h-top', { left: pc(L + cw / 2), top: pc(T) });
            _css('.geo-h-bottom', { left: pc(L + cw / 2), top: pc(1 - B) });
            _css('.geo-h-atop', { left: pc(L + cw / 2), top: pc(bandTop) });
            _css('.geo-h-abottom', { left: pc(L + cw / 2), top: pc(bandBot) });
        }
        function _css(sel, styles) {
            var el = overlay.querySelector(sel);
            if (!el) return;
            for (var k in styles) {
                if (k === 'left') el.style.right = '';
                if (k === 'right') el.style.left = '';
                if (k === 'top') el.style.bottom = '';
                if (k === 'bottom') el.style.top = '';
                el.style[k] = styles[k];
            }
        }

        // ── Edge dragging ─────────────────────────────────────────────────
        function _startEdgeDrag(e) {
            e.preventDefault();
            e.stopPropagation();
            activate();
            dragEdge = e.currentTarget.dataset.edge;
            e.currentTarget.setPointerCapture(e.pointerId);
            e.currentTarget.addEventListener('pointermove', _onEdgeDrag);
            e.currentTarget.addEventListener('pointerup', _endEdgeDrag);
            e.currentTarget.addEventListener('pointercancel', _endEdgeDrag);
            e.currentTarget.classList.add('dragging');
        }
        function _onEdgeDrag(e) {
            if (!dragEdge) return;
            // overlay rect already reflects the zoom/pan transform, so the
            // fraction maths stay correct at any magnification.
            var rect = overlay.getBoundingClientRect();
            var fx = clamp((e.clientX - rect.left) / rect.width, 0, 1);
            var fy = clamp((e.clientY - rect.top) / rect.height, 0, 1);
            _applyEdge(dragEdge, fx, fy);
            render();
            _changed();
        }
        function _applyEdge(edge, fx, fy) {
            var ch = 1 - state.crop_top - state.crop_bottom;
            switch (edge) {
                case 'left': state.crop_left = clamp(fx, 0, Math.min(MAX_INSET, 1 - state.crop_right - MIN_SPAN)); break;
                case 'right': state.crop_right = clamp(1 - fx, 0, Math.min(MAX_INSET, 1 - state.crop_left - MIN_SPAN)); break;
                case 'top': state.crop_top = clamp(fy, 0, Math.min(MAX_INSET, 1 - state.crop_bottom - MIN_SPAN)); break;
                case 'bottom': state.crop_bottom = clamp(1 - fy, 0, Math.min(MAX_INSET, 1 - state.crop_top - MIN_SPAN)); break;
                case 'atop': state.actuation_top = clamp((fy - state.crop_top) / ch, 0, state.actuation_bottom - MIN_BAND); break;
                case 'abottom': state.actuation_bottom = clamp((fy - state.crop_top) / ch, state.actuation_top + MIN_BAND, 1); break;
            }
        }
        function _endEdgeDrag(e) {
            if (!dragEdge) return;
            var h = e.currentTarget;
            h.removeEventListener('pointermove', _onEdgeDrag);
            h.removeEventListener('pointerup', _endEdgeDrag);
            h.removeEventListener('pointercancel', _endEdgeDrag);
            h.classList.remove('dragging');
            dragEdge = null;
            _commit(false);
        }

        function values() {
            return {
                crop_left: round2(state.crop_left), crop_right: round2(state.crop_right),
                crop_top: round2(state.crop_top), crop_bottom: round2(state.crop_bottom),
                actuation_top: round2(state.actuation_top), actuation_bottom: round2(state.actuation_bottom)
            };
        }
        function _commit(persist) {
            if (typeof o.onCommit === 'function') o.onCommit(values(), { persist: !!persist });
        }
        function _changed() {
            if (typeof o.onChange === 'function') o.onChange(inst);
        }
        function activate() {
            if (typeof o.onActivate === 'function') o.onActivate(inst);
        }

        // ── Public instance API ───────────────────────────────────────────
        inst.host = o.host;
        inst.deviceId = o.deviceId || null;
        inst.getValues = values;
        inst.getDisplayValues = values;
        inst.setActive = function (on) {
            if (o.host) o.host.classList.toggle('geo-active-feed', !!on);
        };
        inst.setValues = function (v) {
            state.crop_left = numOr(v.crop_left, state.crop_left);
            state.crop_right = numOr(v.crop_right, state.crop_right);
            state.crop_top = numOr(v.crop_top, state.crop_top);
            state.crop_bottom = numOr(v.crop_bottom, state.crop_bottom);
            state.actuation_top = numOr(v.actuation_top, state.actuation_top);
            state.actuation_bottom = numOr(v.actuation_bottom, state.actuation_bottom);
            render();
        };
        inst.nudge = function (key, delta) {
            var v = state[key] + delta;
            if (key === 'crop_left') state.crop_left = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_right - MIN_SPAN));
            else if (key === 'crop_right') state.crop_right = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_left - MIN_SPAN));
            else if (key === 'crop_top') state.crop_top = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_bottom - MIN_SPAN));
            else if (key === 'crop_bottom') state.crop_bottom = clamp(v, 0, Math.min(MAX_INSET, 1 - state.crop_top - MIN_SPAN));
            else if (key === 'actuation_top') state.actuation_top = clamp(v, 0, state.actuation_bottom - MIN_BAND);
            else if (key === 'actuation_bottom') state.actuation_bottom = clamp(v, state.actuation_top + MIN_BAND, 1);
            render();
            _commit(false);
            _changed();
        };
        inst.revert = function () {
            state = Object.assign({}, snapshot);
            render();
            _commit(false);
        };
        inst.commitPersist = function () { _commit(true); };
        inst.destroy = function () {
            o.host.removeEventListener('wheel', _onWheel);
            if (content && content.parentNode) {
                // put the img back where it was, then drop the wrappers
                o.host.insertBefore(o.img, content);
                content.parentNode.removeChild(content);
            }
            if (zoombar && zoombar.parentNode) zoombar.parentNode.removeChild(zoombar);
            o.host.classList.remove('geo-editing', 'geo-active-feed');
            o.host.style.overflow = '';
            o.img.style.transform = '';
            if (o.host.parentElement) o.host.parentElement.classList.remove('geo-host-active');
            overlay = content = zoombar = null;
        };

        build();
        return inst;
    }

    // ── Shared controls panel (acts on the active instance) ──────────────
    function mountControls(panelEl, ctx) {
        if (!panelEl) return { refresh: function () {}, destroy: function () {} };
        var rows = NUDGE_ROWS.map(function (r) {
            return '<div class="geo-nudge-row" data-key="' + r.key + '">' +
                '<span class="geo-nudge-label">' + r.label + '</span>' +
                '<button class="geo-nudge-btn" data-delta="-0.01">&minus;</button>' +
                '<span class="geo-nudge-val">--</span>' +
                '<button class="geo-nudge-btn" data-delta="0.01">+</button>' +
                '</div>';
        }).join('');
        var allRow = ctx.allowAll
            ? '<label class="geo-apply-all"><input type="checkbox" id="geo-apply-all"> Apply to all OWLs</label>'
            : '';
        // Starts minimised (just the buttons) so it doesn't cover the feed;
        // "Fine-tune" expands the nudge grid.
        panelEl.classList.add('geo-panel-pop', 'geo-min');
        panelEl.innerHTML =
            '<div class="geo-panel-head">' +
            '<button class="geo-min-btn" id="geo-min" title="Show/hide fine-tune">&#9874; Fine-tune</button>' +
            '<div class="geo-panel-actions">' +
            '<button class="geo-btn geo-btn-secondary" id="geo-cancel">Cancel</button>' +
            '<button class="geo-btn geo-btn-primary" id="geo-done">Done</button>' +
            '</div></div>' +
            '<div class="geo-panel-body">' +
            '<div class="geo-nudge-grid">' + rows + '</div>' +
            allRow +
            '</div>';

        var minBtn = panelEl.querySelector('#geo-min');
        minBtn.addEventListener('click', function () {
            panelEl.classList.toggle('geo-min');
        });

        function api() { return { applyToAll: function () { var c = panelEl.querySelector('#geo-apply-all'); return !!(c && c.checked); } }; }
        function refresh() {
            var a = ctx.getActive && ctx.getActive();
            var v = a ? a.getDisplayValues() : null;
            NUDGE_ROWS.forEach(function (r) {
                var el = panelEl.querySelector('.geo-nudge-row[data-key="' + r.key + '"] .geo-nudge-val');
                if (el) el.textContent = v ? v[r.key].toFixed(2) : '--';
            });
        }
        panelEl.querySelectorAll('.geo-nudge-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var a = ctx.getActive && ctx.getActive();
                if (!a) return;
                a.nudge(btn.closest('.geo-nudge-row').dataset.key, parseFloat(btn.dataset.delta));
                refresh();
            });
        });
        panelEl.querySelector('#geo-cancel').addEventListener('click', function () { ctx.onCancel && ctx.onCancel(); });
        panelEl.querySelector('#geo-done').addEventListener('click', function () {
            ctx.onDone && ctx.onDone(api().applyToAll());
        });
        refresh();
        return { refresh: refresh, destroy: function () { panelEl.innerHTML = ''; } };
    }

    // ── Backward-compatible singleton (standalone single feed) ───────────
    var _single = null, _singlePanel = null, _singleOpts = null;

    function open(o) {
        if (_single) close();
        _singleOpts = o;
        _single = create({
            host: o.host, img: o.img, values: o.values, relayNum: o.relayNum,
            deviceId: o.deviceId, label: o.label,
            onCommit: o.onCommit,
            onActivate: function () {},
            onChange: function () { if (_singlePanel) _singlePanel.refresh(); }
        });
        _single.setActive(true);
        _singlePanel = mountControls(o.panel, {
            allowAll: o.allowAll,
            getActive: function () { return _single; },
            onCancel: function () { _single && _single.revert(); close(); },
            onDone: function () { _single && _single.commitPersist(); close(); }
        });
    }

    function close() {
        var cb = _singleOpts && _singleOpts.onClose;
        if (_singlePanel) { _singlePanel.destroy(); _singlePanel = null; }
        if (_single) { _single.destroy(); _single = null; }
        _singleOpts = null;
        if (typeof cb === 'function') cb();
    }

    function done() { if (_single) { _single.commitPersist(); } close(); }
    function cancel() { if (_single) { _single.revert(); } close(); }
    function isOpen() { return _single !== null; }

    return {
        create: create, mountControls: mountControls,
        open: open, close: close, done: done, cancel: cancel, isOpen: isOpen
    };
})();
