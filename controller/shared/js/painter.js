/* ==========================================================================
   OWL Controllers - Weed Painter (shared)

   Full-screen modal for painting weed / background strokes on frozen camera
   frames to train a LUT detection profile. All LUT maths run server-side
   (utils/lut_manager.py — the same code the OWL detection loop uses); this
   module only captures strokes and composites the returned preview overlay.

   Designed for in-cab use: strokes are the atomic unit (one bumped finger =
   one Undo press), nothing reaches the sprayer until "Save & apply", and
   unsaved strokes survive a browser reload via sessionStorage.

   Usage (per controller):
     Painter.init({
       api: { session, sessionEnd, frame, frames, preview, save },
       framePayload: function () { return {}; },   // networked adds device_id
       onOpen: function () {},   // detection interlock + preview mode
       onClose: function () {},
       onSaved: function (meta, applied) {}
     });
     Painter.open();  Painter.close();  Painter.isOpen();
   ========================================================================== */

const Painter = (function () {
    'use strict';

    var DRAFT_KEY = 'owlPainterDraft';
    var BRUSH_DIAMETERS = [24, 48, 80];   // on-screen px (S / M / L)
    var ZOOM_MIN = 1, ZOOM_MAX = 4, ZOOM_STEP = 0.5;
    var PREVIEW_DEBOUNCE_MS = 250;

    var opts = null;
    var overlay = null;
    var els = {};
    var _isOpen = false;

    // Session state
    var sessionId = null;
    var frames = [];          // [{id, imgSrc, width, height, strokes: []}]
    var currentIdx = -1;
    var baseProfile = null;   // profile name being extended, or null
    var sensitivity = 50;
    var tool = 'weed';        // weed | background | erase | pan
    var brushIdx = 1;
    var counts = { weed: 0, background: 0, required: 1 };
    var dirty = false;

    // Interaction state
    var activePointer = null;
    var liveStroke = null;
    var panState = null;
    var zoom = 1, panX = 0, panY = 0;
    var previewSeq = 0;
    var previewTimer = null;

    // ── DOM ───────────────────────────────────────────────────────────────

    function _build() {
        if (overlay) return;
        overlay = document.createElement('div');
        overlay.className = 'painter-overlay';
        overlay.style.display = 'none';
        overlay.innerHTML =
            '<div class="painter-main">' +
            '  <div class="painter-stage">' +
            '    <div class="painter-content">' +
            '      <img class="painter-frame" draggable="false" alt="">' +
            '      <canvas class="painter-mask"></canvas>' +
            '      <canvas class="painter-strokes"></canvas>' +
            '    </div>' +
            '    <div class="painter-zoombar">' +
            '      <button class="painter-zoom-btn" data-z="out">&minus;</button>' +
            '      <span class="painter-zoom-val">1.0&times;</span>' +
            '      <button class="painter-zoom-btn" data-z="in">+</button>' +
            '      <button class="painter-zoom-btn painter-zoom-reset" data-z="reset">Reset</button>' +
            '    </div>' +
            '    <div class="painter-hint"></div>' +
            '    <div class="painter-dialog" style="display:none"></div>' +
            '  </div>' +
            '  <div class="painter-filmstrip">' +
            '    <div class="painter-thumbs"></div>' +
            '    <button class="painter-newframe">+ New frame</button>' +
            '  </div>' +
            '</div>' +
            '<div class="painter-rail">' +
            '  <div class="painter-rail-title">Weed painter</div>' +
            '  <div class="painter-tools">' +
            '    <button class="painter-tool painter-tool-weed active" data-tool="weed">Weed</button>' +
            '    <button class="painter-tool painter-tool-bg" data-tool="background">Background</button>' +
            '    <button class="painter-tool" data-tool="erase">Eraser</button>' +
            '    <button class="painter-tool" data-tool="pan">Pan</button>' +
            '  </div>' +
            '  <div class="painter-brushes">' +
            '    <span class="painter-label">Brush</span>' +
            '    <button class="painter-brush" data-brush="0"><span style="width:10px;height:10px"></span></button>' +
            '    <button class="painter-brush active" data-brush="1"><span style="width:18px;height:18px"></span></button>' +
            '    <button class="painter-brush" data-brush="2"><span style="width:28px;height:28px"></span></button>' +
            '  </div>' +
            '  <button class="painter-undo">Undo stroke</button>' +
            '  <div class="painter-sens">' +
            '    <span class="painter-label">Sensitivity <b class="painter-sens-val">50</b></span>' +
            '    <input type="range" class="painter-sens-slider" min="0" max="100" step="1" value="50">' +
            '  </div>' +
            '  <div class="painter-swatch-wrap" style="display:none">' +
            '    <span class="painter-label">Sprayed colours <b class="painter-coverage"></b></span>' +
            '    <img class="painter-swatch" alt="Colours this profile sprays" draggable="false">' +
            '  </div>' +
            '  <div class="painter-counts"></div>' +
            '  <div class="painter-rail-spacer"></div>' +
            '  <button class="painter-save" disabled>Save profile</button>' +
            '  <button class="painter-exit">Exit</button>' +
            '</div>';
        document.body.appendChild(overlay);

        els.stage = overlay.querySelector('.painter-stage');
        els.content = overlay.querySelector('.painter-content');
        els.frame = overlay.querySelector('.painter-frame');
        els.mask = overlay.querySelector('.painter-mask');
        els.strokes = overlay.querySelector('.painter-strokes');
        els.hint = overlay.querySelector('.painter-hint');
        els.dialog = overlay.querySelector('.painter-dialog');
        els.thumbs = overlay.querySelector('.painter-thumbs');
        els.newFrame = overlay.querySelector('.painter-newframe');
        els.zoomVal = overlay.querySelector('.painter-zoom-val');
        els.sensSlider = overlay.querySelector('.painter-sens-slider');
        els.sensVal = overlay.querySelector('.painter-sens-val');
        els.swatchWrap = overlay.querySelector('.painter-swatch-wrap');
        els.swatchImg = overlay.querySelector('.painter-swatch');
        els.coverage = overlay.querySelector('.painter-coverage');
        els.counts = overlay.querySelector('.painter-counts');
        els.saveBtn = overlay.querySelector('.painter-save');
        els.undoBtn = overlay.querySelector('.painter-undo');

        // Tools
        overlay.querySelectorAll('.painter-tool').forEach(function (btn) {
            btn.addEventListener('click', function () {
                tool = btn.dataset.tool;
                overlay.querySelectorAll('.painter-tool').forEach(function (b) {
                    b.classList.toggle('active', b === btn);
                });
                els.strokes.style.cursor = (tool === 'pan') ? 'grab' : 'crosshair';
            });
        });
        overlay.querySelectorAll('.painter-brush').forEach(function (btn) {
            btn.addEventListener('click', function () {
                brushIdx = parseInt(btn.dataset.brush, 10);
                overlay.querySelectorAll('.painter-brush').forEach(function (b) {
                    b.classList.toggle('active', b === btn);
                });
            });
        });

        els.undoBtn.addEventListener('click', _undo);
        els.saveBtn.addEventListener('click', _openSaveDialog);
        overlay.querySelector('.painter-exit').addEventListener('click', _requestClose);
        els.newFrame.addEventListener('click', function () { _grabFrame(false); });

        // Zoom
        overlay.querySelectorAll('.painter-zoom-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (btn.dataset.z === 'in') _setZoom(zoom + ZOOM_STEP);
                else if (btn.dataset.z === 'out') _setZoom(zoom - ZOOM_STEP);
                else { zoom = 1; panX = 0; panY = 0; _applyTransform(); }
            });
        });
        els.stage.addEventListener('wheel', function (e) {
            e.preventDefault();
            _setZoom(zoom + (e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP));
        }, { passive: false });

        // Sensitivity
        els.sensSlider.addEventListener('input', function () {
            sensitivity = parseInt(els.sensSlider.value, 10);
            els.sensVal.textContent = sensitivity;
        });
        els.sensSlider.addEventListener('change', function () {
            _saveDraft();
            _schedulePreview();
        });

        // Painting — single pointer only, pointercancel-safe
        els.strokes.addEventListener('pointerdown', _onPointerDown);
        els.strokes.addEventListener('pointermove', _onPointerMove);
        els.strokes.addEventListener('pointerup', _onPointerUp);
        els.strokes.addEventListener('pointercancel', _onPointerUp);
    }

    // ── Coordinate helpers ────────────────────────────────────────────────

    function _framePos(e) {
        // Bounding rect reflects the zoom/pan transform, so this maps screen
        // to frame pixels correctly at any zoom level.
        var rect = els.strokes.getBoundingClientRect();
        return {
            x: (e.clientX - rect.left) / rect.width * els.strokes.width,
            y: (e.clientY - rect.top) / rect.height * els.strokes.height,
            scale: els.strokes.width / rect.width
        };
    }

    function _setZoom(z) {
        zoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
        if (zoom === ZOOM_MIN) { panX = 0; panY = 0; }
        _applyTransform();
    }

    function _applyTransform() {
        // Keep the frame at least half-visible in both axes
        var sw = els.stage.clientWidth, sh = els.stage.clientHeight;
        panX = Math.min(sw / 2, Math.max(-sw * (zoom - 0.5), panX));
        panY = Math.min(sh / 2, Math.max(-sh * (zoom - 0.5), panY));
        els.content.style.transform =
            'translate(' + panX + 'px,' + panY + 'px) scale(' + zoom + ')';
        els.zoomVal.textContent = zoom.toFixed(1) + '×';
    }

    // ── Painting ──────────────────────────────────────────────────────────

    function _onPointerDown(e) {
        if (activePointer !== null) return;   // ignore palm / second finger
        if (currentIdx < 0) return;
        e.preventDefault();
        activePointer = e.pointerId;
        els.strokes.setPointerCapture(e.pointerId);

        if (tool === 'pan') {
            panState = { x: e.clientX, y: e.clientY };
            els.strokes.style.cursor = 'grabbing';
            return;
        }
        if (tool === 'erase') {
            _eraseAt(_framePos(e));
            return;
        }
        var pos = _framePos(e);
        liveStroke = {
            frame_id: frames[currentIdx].id,
            label: tool,   // 'weed' | 'background'
            radius: (BRUSH_DIAMETERS[brushIdx] / 2) * pos.scale / els.strokes.width,
            points: [[pos.x / els.strokes.width, pos.y / els.strokes.height]]
        };
        _drawStrokeSegment(liveStroke, liveStroke.points.length - 1);
    }

    function _onPointerMove(e) {
        if (e.pointerId !== activePointer) return;
        if (panState) {
            panX += e.clientX - panState.x;
            panY += e.clientY - panState.y;
            panState = { x: e.clientX, y: e.clientY };
            _applyTransform();
            return;
        }
        if (!liveStroke) return;
        var pos = _framePos(e);
        var nx = pos.x / els.strokes.width, ny = pos.y / els.strokes.height;
        var last = liveStroke.points[liveStroke.points.length - 1];
        var dx = (nx - last[0]) * els.strokes.width;
        var dy = (ny - last[1]) * els.strokes.height;
        if (dx * dx + dy * dy < 4) return;   // ~2px spacing in frame coords
        liveStroke.points.push([nx, ny]);
        _drawStrokeSegment(liveStroke, liveStroke.points.length - 1);
    }

    function _onPointerUp(e) {
        if (e.pointerId !== activePointer) return;
        activePointer = null;
        if (panState) {
            panState = null;
            els.strokes.style.cursor = 'grab';
            return;
        }
        if (!liveStroke) return;
        frames[currentIdx].strokes.push(liveStroke);
        liveStroke = null;
        dirty = true;
        _saveDraft();
        _updateRail();
        _schedulePreview();
    }

    function _strokeColour(label) {
        return label === 'weed' ? 'rgba(46,204,64,0.55)' : 'rgba(255,65,54,0.50)';
    }

    function _drawStrokeSegment(stroke, i) {
        var ctx = els.strokes.getContext('2d');
        var w = els.strokes.width, h = els.strokes.height;
        ctx.strokeStyle = ctx.fillStyle = _strokeColour(stroke.label);
        ctx.lineWidth = stroke.radius * w * 2;
        ctx.lineCap = ctx.lineJoin = 'round';
        var p = stroke.points[i];
        if (i === 0) {
            ctx.beginPath();
            ctx.arc(p[0] * w, p[1] * h, stroke.radius * w, 0, Math.PI * 2);
            ctx.fill();
        } else {
            var q = stroke.points[i - 1];
            ctx.beginPath();
            ctx.moveTo(q[0] * w, q[1] * h);
            ctx.lineTo(p[0] * w, p[1] * h);
            ctx.stroke();
        }
    }

    function _redrawStrokes() {
        var ctx = els.strokes.getContext('2d');
        ctx.clearRect(0, 0, els.strokes.width, els.strokes.height);
        if (currentIdx < 0) return;
        frames[currentIdx].strokes.forEach(function (s) {
            for (var i = 0; i < s.points.length; i++) _drawStrokeSegment(s, i);
        });
    }

    function _undo() {
        if (currentIdx < 0) return;
        var list = frames[currentIdx].strokes;
        if (!list.length) return;
        list.pop();
        dirty = true;
        _redrawStrokes();
        _saveDraft();
        _updateRail();
        _schedulePreview();
    }

    function _eraseAt(pos) {
        if (currentIdx < 0) return;
        var list = frames[currentIdx].strokes;
        var w = els.strokes.width, h = els.strokes.height;
        var slop = 12 * pos.scale;   // 12 screen-px tap tolerance
        for (var s = list.length - 1; s >= 0; s--) {
            var stroke = list[s];
            var hitDist = stroke.radius * w + slop;
            for (var i = 0; i < stroke.points.length; i++) {
                var px = stroke.points[i][0] * w, py = stroke.points[i][1] * h;
                var d2;
                if (i === 0) {
                    d2 = (pos.x - px) * (pos.x - px) + (pos.y - py) * (pos.y - py);
                } else {
                    d2 = _distToSegment2(pos.x, pos.y,
                        stroke.points[i - 1][0] * w, stroke.points[i - 1][1] * h, px, py);
                }
                if (d2 <= hitDist * hitDist) {
                    list.splice(s, 1);
                    dirty = true;
                    _redrawStrokes();
                    _saveDraft();
                    _updateRail();
                    _schedulePreview();
                    return;
                }
            }
        }
    }

    function _distToSegment2(x, y, x1, y1, x2, y2) {
        var dx = x2 - x1, dy = y2 - y1;
        var len2 = dx * dx + dy * dy;
        var t = len2 ? Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / len2)) : 0;
        var cx = x1 + t * dx, cy = y1 + t * dy;
        return (x - cx) * (x - cx) + (y - cy) * (y - cy);
    }

    // ── Frames / filmstrip ────────────────────────────────────────────────

    function _addFrame(frameData, select) {
        frames.push({
            id: frameData.frame_id,
            imgSrc: 'data:image/jpeg;base64,' + frameData.image,
            width: frameData.width,
            height: frameData.height,
            strokes: frameData.strokes || []
        });
        _renderThumbs();
        if (select !== false) _selectFrame(frames.length - 1);
    }

    function _selectFrame(idx) {
        currentIdx = idx;
        var f = frames[idx];
        els.frame.src = f.imgSrc;
        els.strokes.width = f.width;
        els.strokes.height = f.height;
        els.mask.width = f.width;
        els.mask.height = f.height;
        els.mask.getContext('2d').clearRect(0, 0, f.width, f.height);
        _redrawStrokes();
        _renderThumbs();
        _schedulePreview();
    }

    function _renderThumbs() {
        els.thumbs.innerHTML = '';
        frames.forEach(function (f, i) {
            var t = document.createElement('button');
            t.className = 'painter-thumb' + (i === currentIdx ? ' active' : '');
            var img = document.createElement('img');
            img.src = f.imgSrc;
            img.draggable = false;
            t.appendChild(img);
            var n = document.createElement('span');
            n.textContent = f.strokes.length ? f.strokes.length : '';
            t.appendChild(n);
            t.addEventListener('click', function () { _selectFrame(i); });
            els.thumbs.appendChild(t);
        });
    }

    function _grabFrame(isFirst) {
        var payload = Object.assign({ session_id: sessionId },
            opts.framePayload ? opts.framePayload() : {});
        return fetch(opts.api.frame, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) throw new Error(data.error || 'Frame grab failed');
            _addFrame(data, true);
            _saveDraft();
        }).catch(function (err) {
            _setHint('Could not grab a frame: ' + err.message, true);
            if (isFirst) _setHint('Could not grab a frame — is the OWL service running?', true);
        });
    }

    // ── Preview ───────────────────────────────────────────────────────────

    function _allStrokes() {
        var all = [];
        frames.forEach(function (f) { all = all.concat(f.strokes); });
        return all;
    }

    function _schedulePreview() {
        if (previewTimer) clearTimeout(previewTimer);
        previewTimer = setTimeout(_requestPreview, PREVIEW_DEBOUNCE_MS);
    }

    function _requestPreview() {
        if (currentIdx < 0) return;
        var seq = ++previewSeq;
        fetch(opts.api.preview, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: sessionId,
                frame_id: frames[currentIdx].id,
                strokes: _allStrokes(),
                sensitivity: sensitivity,
                base_profile: baseProfile || ''
            })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (seq !== previewSeq) return;   // stale response
            if (!data.success) { _setHint(data.error || 'Preview failed', true); return; }
            counts = data.counts || counts;
            _updateRail();

            // "Sprayed colours" swatch — the LUT made visible
            if (data.swatch) {
                els.swatchImg.src = 'data:image/png;base64,' + data.swatch;
                els.coverage.textContent = (typeof data.coverage === 'number')
                    ? data.coverage + '% of colours' : '';
                els.swatchWrap.style.display = '';
            } else {
                els.swatchWrap.style.display = 'none';
            }

            var ctx = els.mask.getContext('2d');
            if (!data.overlay) {
                ctx.clearRect(0, 0, els.mask.width, els.mask.height);
                return;
            }
            var img = new Image();
            img.onload = function () {
                if (seq !== previewSeq) return;
                ctx.clearRect(0, 0, els.mask.width, els.mask.height);
                ctx.drawImage(img, 0, 0);
            };
            img.src = 'data:image/png;base64,' + data.overlay;
        }).catch(function () { /* transient — next stroke retries */ });
    }

    function _updateRail() {
        var ready = counts.weed >= counts.required && counts.background >= counts.required;
        els.saveBtn.disabled = !ready;
        els.counts.textContent =
            'Weed: ' + counts.weed.toLocaleString() + ' px · ' +
            'Background: ' + counts.background.toLocaleString() + ' px';
        if (!_allStrokes().length) {
            _setHint('Paint over weeds with the green brush, then paint soil and stubble with the red brush.');
        } else if (counts.weed < counts.required) {
            _setHint('Paint more weeds (need ' + counts.required.toLocaleString() + ' px of each).');
        } else if (counts.background < counts.required) {
            _setHint('Now paint background — soil, stubble, shadows.');
        } else {
            _setHint('Orange shows what would be sprayed. Adjust sensitivity or keep painting.');
        }
    }

    function _setHint(msg, isError) {
        els.hint.textContent = msg;
        els.hint.classList.toggle('error', !!isError);
    }

    // ── Dialogs (in-overlay, kiosk-sized) ─────────────────────────────────

    function _showDialog(html, buttons) {
        els.dialog.innerHTML = '<div class="painter-dialog-box">' + html +
            '<div class="painter-dialog-actions"></div></div>';
        var actions = els.dialog.querySelector('.painter-dialog-actions');
        buttons.forEach(function (b) {
            var btn = document.createElement('button');
            btn.textContent = b.label;
            btn.className = 'painter-dialog-btn' + (b.primary ? ' primary' : '');
            btn.addEventListener('click', function () {
                els.dialog.style.display = 'none';
                if (b.onClick) b.onClick();
            });
            actions.appendChild(btn);
        });
        els.dialog.style.display = 'flex';
    }

    function _suggestName() {
        var d = new Date();
        function pad(n) { return (n < 10 ? '0' : '') + n; }
        return 'paddock_' + pad(d.getMonth() + 1) + pad(d.getDate()) + '_' +
            pad(d.getHours()) + pad(d.getMinutes());
    }

    function _openSaveDialog() {
        _showDialog(
            '<div class="painter-dialog-title">Save profile</div>' +
            '<input type="text" class="painter-name-input" value="' + _suggestName() + '" ' +
            'autocapitalize="off" autocomplete="off" spellcheck="false">' +
            '<div class="painter-dialog-note">Lowercase letters, numbers and underscores.</div>',
            [
                { label: 'Save & apply', primary: true, onClick: function () { _save(true); } },
                { label: 'Save only', onClick: function () { _save(false); } },
                { label: 'Cancel' }
            ]);
        var input = els.dialog.querySelector('.painter-name-input');
        input.focus();
        input.select();
    }

    function _save(applyNow) {
        var input = overlay.querySelector('.painter-name-input');
        var name = (input ? input.value : _suggestName()).trim().toLowerCase();
        if (!/^[a-z][a-z0-9_]{0,30}$/.test(name)) {
            _setHint('Invalid name — use lowercase letters, numbers, underscores.', true);
            return;
        }
        fetch(opts.api.save, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                session_id: sessionId,
                name: name,
                strokes: _allStrokes(),
                sensitivity: sensitivity,
                base_profile: baseProfile || '',
                apply: !!applyNow,
                thumbnail_frame_id: currentIdx >= 0 ? frames[currentIdx].id : ''
            })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (!data.success) { _setHint(data.error || 'Save failed', true); return; }
            dirty = false;
            _clearDraft();
            if (typeof showToast === 'function') {
                showToast(applyNow ? 'Profile "' + name + '" saved and applied'
                                   : 'Profile "' + name + '" saved', 'success');
            }
            if (opts.onSaved) opts.onSaved(data.meta, !!applyNow);
            _teardown();
        }).catch(function (err) { _setHint('Save failed: ' + err.message, true); });
    }

    function _requestClose() {
        if (dirty && _allStrokes().length) {
            _showDialog(
                '<div class="painter-dialog-title">Discard unsaved painting?</div>',
                [
                    { label: 'Keep painting', primary: true },
                    { label: 'Discard', onClick: _teardown }
                ]);
            return;
        }
        _teardown();
    }

    // ── Draft persistence (bump / reload insurance) ───────────────────────

    function _saveDraft() {
        try {
            sessionStorage.setItem(DRAFT_KEY, JSON.stringify({
                sessionId: sessionId,
                sensitivity: sensitivity,
                baseProfile: baseProfile,
                frames: frames.map(function (f) { return { id: f.id, strokes: f.strokes }; })
            }));
        } catch (e) { /* storage full — painting continues, just no recovery */ }
    }

    function _clearDraft() {
        try { sessionStorage.removeItem(DRAFT_KEY); } catch (e) { }
    }

    function _loadDraft() {
        try { return JSON.parse(sessionStorage.getItem(DRAFT_KEY)); }
        catch (e) { return null; }
    }

    function _tryResume(draft) {
        return fetch(opts.api.frames + '?session_id=' + encodeURIComponent(draft.sessionId))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success || !data.frames.length) return false;
                sessionId = draft.sessionId;
                sensitivity = draft.sensitivity || 50;
                baseProfile = draft.baseProfile || null;
                els.sensSlider.value = sensitivity;
                els.sensVal.textContent = sensitivity;
                var strokesById = {};
                (draft.frames || []).forEach(function (f) { strokesById[f.id] = f.strokes; });
                data.frames.forEach(function (f) {
                    f.strokes = strokesById[f.frame_id] || [];
                    _addFrame(f, false);
                });
                dirty = true;
                _selectFrame(frames.length - 1);
                return true;
            }).catch(function () { return false; });
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────

    function open(openOpts) {
        if (_isOpen) return;
        _build();
        _isOpen = true;
        overlay.style.display = 'flex';

        // Reset session state
        sessionId = null;
        frames = [];
        currentIdx = -1;
        dirty = false;
        zoom = 1; panX = 0; panY = 0;
        _applyTransform();
        baseProfile = (openOpts && openOpts.baseProfile) || null;
        sensitivity = (openOpts && typeof openOpts.sensitivity === 'number')
            ? openOpts.sensitivity : 50;
        els.sensSlider.value = sensitivity;
        els.sensVal.textContent = sensitivity;
        counts = { weed: 0, background: 0, required: 1 };
        els.swatchWrap.style.display = 'none';
        els.thumbs.innerHTML = '';
        els.strokes.getContext('2d').clearRect(0, 0, els.strokes.width, els.strokes.height);
        els.mask.getContext('2d').clearRect(0, 0, els.mask.width, els.mask.height);
        _updateRail();

        if (opts.onOpen) opts.onOpen();

        var draft = _loadDraft();
        var start = function () {
            fetch(opts.api.session, { method: 'POST' })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    sessionId = data.session_id;
                    return _grabFrame(true);
                })
                .catch(function (err) { _setHint('Could not start session: ' + err.message, true); });
        };

        if (draft && draft.sessionId && (draft.frames || []).some(function (f) { return f.strokes.length; })) {
            _showDialog(
                '<div class="painter-dialog-title">Unsaved painting found</div>' +
                '<div class="painter-dialog-note">Resume where you left off?</div>',
                [
                    { label: 'Resume', primary: true, onClick: function () {
                        _tryResume(draft).then(function (ok) {
                            if (!ok) { _clearDraft(); start(); }
                        });
                    } },
                    { label: 'Discard', onClick: function () { _clearDraft(); start(); } }
                ]);
        } else {
            start();
        }
    }

    function _teardown() {
        _isOpen = false;
        overlay.style.display = 'none';
        if (!dirty) _clearDraft();
        if (sessionId && !dirty) {
            fetch(opts.api.sessionEnd, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId })
            }).catch(function () { });
        }
        if (opts.onClose) opts.onClose();
    }

    function init(initOpts) {
        opts = initOpts || {};
        opts.api = opts.api || {};
    }

    return {
        init: init,
        open: open,
        close: function () { if (_isOpen) _teardown(); },
        isOpen: function () { return _isOpen; }
    };
})();
