// ============================================
// OWL Controller - Data collection screen (layout A)
// Polls /api/collect/state once a second; everything on screen is drawn
// from that, so a reload or a second browser always shows the same thing.
// Routes: controller/networked/networked.py (/api/collect/*).
// The capture form and record modal are shared with the standalone
// dashboard: /shared/js/collect_form.js.
// ============================================

(function () {
    'use strict';

    const I = window.CollectI18n;
    const F = window.CollectForm;
    const t = I.t;
    const STATE_MS = 1000;
    const POINTS_MS = 4000;
    const OWL_COLOURS = ['#1f6fd1', '#d1611f', '#2e8b57', '#8e44ad', '#b8860b', '#c0392b'];
    const MAX_WARNINGS = 3;

    let config = { presets: {}, capture: {}, language: 'en' };
    let state = null;
    let starting = false;
    let recordModal = null;
    let captureForm = null;
    let ticked = {};
    let thumbSeq = {};
    let map = null, mapLayer = null, mapFitted = false;
    let pointsNext = 0, pointsRun = null, pointCount = 0;
    const markers = [];
    let cameraValues = null;
    let liveOwl = null;

    // ---------- helpers ----------
    const $ = (id) => document.getElementById(id);
    const esc = (s) => String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    function api(url, body) {
        const opts = body === undefined ? {} : {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        };
        return fetch(url, opts).then((r) => r.json().catch(() => ({})).then((data) => {
            if (!r.ok) {
                const err = new Error(data.error || ('HTTP ' + r.status));
                err.data = data;
                err.status = r.status;
                throw err;
            }
            return data;
        }));
    }

    function toast(msg, type) {
        if (typeof showToast === 'function') showToast(msg, type || 'info');
    }

    function owlColour(id) {
        const ids = (state && state.owls ? state.owls.map((o) => o.id) : []);
        const i = Math.max(0, ids.indexOf(id));
        return OWL_COLOURS[i % OWL_COLOURS.length];
    }

    function storageGet(key, fallback) {
        try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch (e) { return fallback; }
    }
    function storageSet(key, value) {
        try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* kiosk incognito */ }
    }
    function shutterText(us) {
        if (!us) return '-';
        return '1/' + Math.round(1000000 / us);
    }
    function mb(bytes) { return bytes ? Math.round(bytes / 1048576) : 0; }
    function freeText(freeMb) {
        if (freeMb === null || freeMb === undefined) return '-';
        return freeMb >= 1024 ? Math.round(freeMb / 1024) + ' GB' : freeMb + ' MB';
    }
    function fmtElapsed(startedAt) {
        if (!startedAt) return '';
        const s = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
        const m = Math.floor(s / 60);
        return String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    }
    // ---------- language ----------
    function setLanguage(lang, persist) {
        I.setLang(lang);
        I.apply();
        document.querySelectorAll('.c-lang button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.lang === lang)));
        $('ev-down').setAttribute('aria-label', t('darker'));
        $('ev-up').setAttribute('aria-label', t('brighter'));
        if (typeof Numpad !== 'undefined' && Numpad.setLocale) Numpad.setLocale(lang, { cancel: t('cancel'), ok: t('ok') });
        storageSet('collect.lang', lang);
        if (persist) api('/api/collect/settings', { language: lang }).catch(() => {});
        if (recordModal) recordModal.refresh();
        render();
    }

    // ---------- tabs ----------
    function showView(name) {
        document.querySelectorAll('.c-tab').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.view === name)));
        document.querySelectorAll('.c-view').forEach((v) => v.classList.toggle('is-on', v.id === 'view-' + name));
        if (name === 'map') initMap();
        if (name === 'settings') onSettingsShown();
    }
    function showPane(name) {
        document.querySelectorAll('.c-setnav button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.pane === name)));
        document.querySelectorAll('.c-pane').forEach((p) => p.classList.toggle('is-on', p.id === 'pane-' + name));
        if (name === 'camera') loadCamera();
        if (name === 'sessions') refreshSessions();
    }
    function onSettingsShown() {
        const current = document.querySelector('.c-setnav button[aria-selected="true"]');
        if (current) showPane(current.dataset.pane);
    }

    // ---------- record view ----------
    function render() {
        if (!state) return;
        renderRecordButton();
        renderOwlRows();
        renderChips();
        renderWarnings();
        renderGallery();
        renderCloudPill();
        renderMapSide();
        renderSessions();
        renderCloudPane();
        renderCameraLive();
    }

    function renderRecordButton() {
        if (!state) return;
        const btn = $('record-btn');
        const connected = state.owls.filter((o) => o.connected);
        btn.classList.toggle('is-recording', state.recording);
        btn.disabled = !state.recording && (connected.length === 0 || starting);
        $('record-label').textContent = state.recording ? t('stop') : t('start');
        let sub = t('ready');
        if (starting) sub = t('starting');
        else if (state.recording) sub = fmtElapsed(state.run.started_at) || '';
        else if (connected.length === 0) sub = t('no_owls');
        $('record-sub').textContent = sub;
    }

    function renderOwlRows() {
        const rows = state.owls.map((o) => {
            const colour = owlColour(o.id);
            if (!o.connected) {
                return '<div class="c-owl-row c-owl-row--off"><span class="c-dot c-dot--bad"></span>' +
                    '<span class="c-owl-name" style="color:' + colour + '">' + esc(o.id) + '</span>' +
                    '<span>' + esc(t('offline')) + '</span><span></span><span></span><span></span><span></span></div>';
            }
            const dot = o.recording ? 'c-dot--ok' : '';
            const images = o.record_mode === 'video' && o.video_seconds
                ? t('video_clip', { s: Math.round(o.video_seconds) }) : (o.saved_count || 0);
            return '<div class="c-owl-row"><span class="c-dot ' + dot + '"></span>' +
                '<span class="c-owl-name" style="color:' + colour + '">' + esc(o.id) + '</span>' +
                cell(t('images'), images) +
                cell(t('speed'), o.speed_kmh === null || o.speed_kmh === undefined ? '-' : o.speed_kmh.toFixed(1) + ' km/h') +
                cell(t('shutter'), shutterText(o.exposure_time_us) + (o.analogue_gain ? ' · ' + o.analogue_gain.toFixed(1) + '×' : '')) +
                cell(t('free'), freeText(o.storage_free_mb)) +
                '<button class="c-btn c-btn--small c-live-btn" type="button" data-live="' + esc(o.id) + '">' +
                esc(t('live')) + '</button></div>';
        });
        $('owl-rows').innerHTML = rows.join('') || '<div class="c-hint">' + esc(t('no_owls')) + '</div>';
    }
    function cell(label, value) {
        return '<span><span class="c-k">' + esc(label) + '</span><span class="c-num">' + esc(value) + '</span></span>';
    }

    function renderChips() {
        const chips = state.recording ? F.metadataChips(state.run.metadata, config.presets) : null;
        $('meta-chips').innerHTML = chips
            ? chips.map((c) => '<span class="c-chip">' + esc(c) + '</span>').join('')
            : '<span class="c-chip c-chip--muted">' + esc(t('meta_hint')) + '</span>';
    }

    function warningText(w) {
        const v = { owl: w.owl || '' };
        if (w.code === 'blur_risk') {
            v.mm = w.smear_mm; v.kmh = w.target_kmh;
            return t(w.capped ? 'w_blur_risk_capped' : 'w_blur_risk', v);
        }
        if (w.code === 'storage_low') v.free = freeText(w.free_mb);
        if (w.code === 'recording_blocked') v.reason = String(w.reason || '').replace(/_/g, ' ');
        if (w.code === 'video_limit') v.s = w.seconds;
        if (w.code === 'not_saving') v.s = w.seconds;
        if (w.code === 'paused_slow') v.kmh = w.kmh;
        if (w.code === 'fault') v.message = w.message || '';
        return t('w_' + w.code, v);
    }
    function renderWarnings() {
        const list = state.warnings || [];
        if (!list.length) {
            $('warnings').innerHTML = '<div class="c-ok-line"><span class="c-dot c-dot--ok"></span>' + esc(t('all_ok')) + '</div>';
            return;
        }
        let html = list.slice(0, MAX_WARNINGS).map((w) =>
            '<div class="c-warn' + (w.severity === 'bad' ? ' c-warn--bad' : '') + '"><span>' + esc(warningText(w)) + '</span></div>').join('');
        if (list.length > MAX_WARNINGS) html += '<div class="c-more">' + esc(t('more_warnings', { n: list.length - MAX_WARNINGS })) + '</div>';
        $('warnings').innerHTML = html;
    }

    // Gallery: two OWLs show latest + previous each; more OWLs show latest only.
    function gallerySlots() {
        const owls = state.owls.map((o) => o.id);
        if (owls.length <= 2) {
            const slots = [];
            owls.forEach((id) => slots.push([id, 0]));
            owls.forEach((id) => slots.push([id, 1]));
            return slots.slice(0, 4);
        }
        return owls.slice(0, 4).map((id) => [id, 0]);
    }
    function renderGallery() {
        const gallery = $('gallery');
        const slots = gallerySlots();
        const thumbs = state.thumbs || {};
        const key = slots.map((s) => s.join(':')).join('|');
        if (gallery.dataset.key !== key) {
            gallery.dataset.key = key;
            gallery.innerHTML = slots.map(([id, idx]) =>
                '<div class="c-card c-shot" data-owl="' + esc(id) + '" data-idx="' + idx + '">' +
                '<span class="c-shot-tag" style="background:' + owlColour(id) + '">' + esc(id) + '</span>' +
                '<div class="c-shot-empty"></div><img alt="" hidden />' +
                '<div class="c-shot-cap"><b></b><span class="c-num"></span></div></div>').join('');
            thumbSeq = {};
        }
        gallery.querySelectorAll('.c-shot').forEach((el) => {
            const id = el.dataset.owl, idx = Number(el.dataset.idx);
            const seqs = thumbs[id] || [];
            const seq = seqs[idx];
            const img = el.querySelector('img'), empty = el.querySelector('.c-shot-empty');
            el.querySelector('.c-shot-cap b').textContent = t(idx === 0 ? 'latest' : 'previous');
            const owl = state.owls.find((o) => o.id === id);
            el.querySelector('.c-shot-cap .c-num').textContent = owl && idx === 0 && owl.saved_count ? '#' + owl.saved_count : '';
            if (seq === undefined) {
                img.hidden = true; empty.hidden = false;
                empty.textContent = idx === 0 ? t('waiting_image') : '';
                return;
            }
            const slotKey = id + ':' + idx;
            if (thumbSeq[slotKey] !== seq) {
                thumbSeq[slotKey] = seq;
                img.src = '/api/collect/thumb/' + encodeURIComponent(id) + '/' + idx + '?s=' + seq;
            }
            img.hidden = false; empty.hidden = true;
        });
    }

    function renderCloudPill() {
        const c = state.cloud;
        let dot = '', text = t('pill_not_set');
        if (c.configured) {
            if (c.uploading_pct !== null && c.uploading_pct !== undefined) { dot = 'c-dot--up'; text = t('pill_uploading', { p: c.uploading_pct }); }
            else if (c.connected === true) { dot = 'c-dot--ok'; text = t('pill_connected'); }
            else if (c.connected === false) { dot = 'c-dot--bad'; text = t('pill_offline'); }
            else { dot = 'c-dot--warn'; text = t('cloud_checking'); }
        }
        $('cloud-dot').className = 'c-dot ' + dot;
        $('cloud-text').textContent = text;
    }

    // ---------- record / stop ----------
    function onRecordPress() {
        if (!state) return;
        if (state.recording) { $('stop-modal').hidden = false; return; }
        recordModal.open();
    }

    function startRecording(metadata) {
        starting = true;
        renderRecordButton();
        api('/api/collect/start', { metadata: metadata })
            .catch((e) => toast(e.message, 'error'))
            .finally(() => { starting = false; poll(); });
    }

    function stopRecording() {
        $('stop-modal').hidden = true;
        api('/api/collect/stop', {}).catch((e) => toast(e.message, 'error')).finally(poll);
    }

    // ---------- turn off ----------
    function onPowerPress() {
        $('power-recording').hidden = !(state && state.recording);
        $('power-yes').disabled = false;
        $('power-modal').hidden = false;
    }

    // Stop the run first so the controller closes it and the OWLs finish
    // their last image, then shut down every OWL and this controller
    function powerOff() {
        $('power-yes').disabled = true;
        const stopped = state && state.recording
            ? api('/api/collect/stop', {}).catch(() => {}) : Promise.resolve();
        stopped.then(() => api('/api/system/shutdown', {})).then(() => {
            $('power-modal').hidden = true;
            $('power-done').hidden = false;
            $('power-btn').disabled = true;
        }).catch((e) => {
            $('power-yes').disabled = false;
            toast(e.message, 'error');
        });
    }

    // ---------- map ----------
    function initMap() {
        if (map || typeof L === 'undefined') { if (map) setTimeout(() => map.invalidateSize(), 50); return; }
        map = L.map('collect-map', { zoomControl: true, center: [56, 10], zoom: 6, preferCanvas: true });
        map.zoomControl.setPosition('bottomright');
        // Plain grid underneath so the map never looks broken without tiles
        const Grid = L.GridLayer.extend({
            createTile: function () {
                const size = this.getTileSize();
                const c = document.createElement('canvas');
                c.width = size.x; c.height = size.y;
                const g = c.getContext('2d');
                g.fillStyle = '#e8ede4'; g.fillRect(0, 0, size.x, size.y);
                g.strokeStyle = 'rgba(80, 100, 80, 0.2)'; g.strokeRect(0, 0, size.x, size.y);
                return c;
            },
        });
        new Grid().addTo(map);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
        }).addTo(map);
        mapLayer = L.layerGroup().addTo(map);
        markers.forEach((m) => m.addTo(mapLayer));
        setTimeout(() => {
            map.invalidateSize();
            // Points may have arrived before the tab was first opened
            if (markers.length && !mapFitted) { centreMap(); mapFitted = true; }
        }, 50);
    }

    function pollPoints() {
        api('/api/collect/points?after=' + pointsNext).then((data) => {
            if (data.run_id !== pointsRun) {
                // New run: start the map afresh
                pointsRun = data.run_id;
                markers.length = 0;
                pointCount = 0;
                if (mapLayer) mapLayer.clearLayers();
                mapFitted = false;
                if (pointsNext !== 0) { pointsNext = 0; return pollPoints(); }
            }
            (data.points || []).forEach((p) => {
                const m = L.circleMarker([p.lat, p.lon], {
                    radius: 4, weight: 0, fillOpacity: 0.9, fillColor: owlColour(p.owl),
                });
                markers.push(m);
                if (mapLayer) m.addTo(mapLayer);
            });
            pointCount += (data.points || []).length;
            pointsNext = data.next;
            if (map && markers.length && !mapFitted) { centreMap(); mapFitted = true; }
            renderMapSide();
        }).catch(() => {});
    }

    function centreMap() {
        if (!map || !markers.length) return;
        const bounds = L.latLngBounds(markers.map((m) => m.getLatLng()));
        map.fitBounds(bounds.pad(0.15), { maxZoom: 18 });
    }

    function renderMapSide() {
        $('map-count').textContent = String(pointCount);
        $('map-empty').hidden = pointCount > 0;
        const c = config.capture || {};
        $('map-spacing').textContent = c.sample_mode === 'time' ? (c.sample_interval_s + ' s')
            : (c.sample_distance_m !== undefined ? c.sample_distance_m + ' m' : '-');
        if (!state) return;
        $('map-legend').innerHTML = state.owls.map((o) =>
            '<div><span class="c-swatch" style="background:' + owlColour(o.id) + '"></span>' + esc(o.id) + '</div>').join('');
    }

    // ---------- sessions and upload ----------
    function refreshSessions() {
        api('/api/collect/sessions/refresh', {}).catch(() => {});
    }

    function sessionStatus(owl, session) {
        const req = (state.uploads || []).find((u) => u.local_id === owl.id && u.session_id === session.session_id);
        const tr = owl.data_transfer || {};
        if (tr.session_date === session.session_id && tr.status) {
            if (tr.status === 'complete') return ['ok', t('st_uploaded')];
            if (tr.status === 'error') return ['bad', t('st_failed')];
            if (tr.status !== 'idle') {
                // progress comes from the OWL's MQTT state: a number, never markup
                const pct = Math.max(0, Math.min(100, Math.round(Number(tr.progress) || 0)));
                return ['up', t('st_uploading', { p: pct }), pct];
            }
        }
        if (req) {
            if (req.status === 'rejected') return ['bad', t('st_rejected') + (req.reason ? ': ' + req.reason : '')];
            if (req.status === 'accepted' || req.status === 'queued') return ['req', t('st_accepted')];
            return ['req', t('st_requested')];
        }
        return ['', t('st_on_owl')];
    }

    // capture.session_id is the folder name (session_HHMMSS); session lists
    // use date/folder. The newest match is the one being written.
    function isRecordingInto(owl, session) {
        if (!owl.recording || !owl.session_id) return false;
        const current = (owl.data_sessions || []).map((x) => String(x.session_id))
            .filter((id) => id === owl.session_id || id.endsWith('/' + owl.session_id))
            .sort().pop();
        return current === String(session.session_id);
    }

    function renderSessions() {
        const body = $('session-rows');
        const rows = [];
        state.owls.forEach((owl) => {
            (owl.data_sessions || []).slice().sort((a, b) => String(b.session_id).localeCompare(String(a.session_id)))
                .forEach((s) => rows.push([owl, s]));
        });
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="6" class="c-hint">' + esc(t('no_sessions')) + '</td></tr>';
        } else {
            body.innerHTML = rows.map(([owl, s]) => {
                const key = owl.id + '|' + s.session_id;
                const st = sessionStatus(owl, s);
                // Not the session this OWL is still recording into, uploading
                // or waiting on Noktura for. Uploaded ones can be ticked to delete.
                const canPick = (st[0] === '' || st[0] === 'bad' || st[0] === 'ok') && !isRecordingInto(owl, s);
                if (!canPick) delete ticked[key];
                const date = String(s.date || '');
                const when = (date.length === 8 ? date.slice(6, 8) + '-' + date.slice(4, 6) + ' ' : '') + (s.time || '');
                return '<tr><td>' + (canPick ? '<button type="button" class="c-tick" data-key="' + esc(key) + '" aria-pressed="' +
                    String(!!ticked[key]) + '" aria-label="' + esc(s.session_id) + '">' + (ticked[key] ? '&#10003;' : '') + '</button>' : '') + '</td>' +
                    '<td><b style="color:' + owlColour(owl.id) + '">' + esc(owl.id) + '</b></td>' +
                    '<td class="c-num">' + esc(when || s.session_id) + '</td>' +
                    '<td class="c-num">' + esc(s.image_count || 0) + '</td>' +
                    '<td class="c-num">' + mb(s.total_size) + ' MB</td>' +
                    '<td><span class="c-state' + (st[0] ? ' c-state--' + st[0] : '') + '">' + esc(st[1]) + '</span>' +
                    (st[0] === 'up' ? '<div class="c-bar"><i style="width:' + st[2] + '%"></i></div>' : '') + '</td></tr>';
            }).join('');
        }
        const chosen = Object.keys(ticked).filter((k) => ticked[k]);
        const bytes = chosen.reduce((sum, key) => {
            const [owlId, sid] = key.split('|');
            const owl = state.owls.find((o) => o.id === owlId);
            const s = owl && (owl.data_sessions || []).find((x) => x.session_id === sid);
            return sum + (s ? s.total_size || 0 : 0);
        }, 0);
        $('upload-btn').disabled = !chosen.length;
        $('delete-btn').disabled = !chosen.length;
        $('upload-summary').textContent = chosen.length ? t('selected', { n: chosen.length, mb: mb(bytes) }) : t('nothing_selected');
    }

    function onUpload() {
        if (!state.cloud.configured) { toast(t('cloud_needed'), 'warning'); return; }
        api('/api/collect/upload', { items: chosenItems() }).then((data) => {
            ticked = {};
            if (!(data.requested || []).length) toast(t('upload_already'), 'warning');
            poll();
        }).catch((e) => toast(e.message, 'error'));
    }

    function chosenItems() {
        return Object.keys(ticked).filter((k) => ticked[k]).map((k) => {
            const [owl, session_id] = k.split('|');
            return { owl: owl, session_id: session_id };
        });
    }
    function onDeletePress() {
        const n = chosenItems().length;
        if (!n) return;
        $('delete-title').textContent = t('delete_title', { n: n });
        $('delete-modal').hidden = false;
    }
    function deleteSessions() {
        $('delete-modal').hidden = true;
        api('/api/collect/sessions/delete', { items: chosenItems() }).then((data) => {
            ticked = {};
            const done = (data.deleted || []).length, skipped = (data.skipped || []).length;
            if (done) toast(t('deleted', { n: done }), 'success');
            if (skipped) toast(t('delete_skipped', { n: skipped }), 'warning');
            poll();
        }).catch((e) => toast(e.message, 'error'));
    }

    // ---------- capture settings ----------
    function saveCapture(key, value) {
        const capture = {}; capture[key] = value;
        api('/api/collect/settings', { capture: capture }).then((data) => {
            config.capture = data.capture;
            captureForm.render(config.capture);
            renderMapSide();
            toast(t('saved'), 'success');
        }).catch((e) => { toast(e.message, 'error'); captureForm.render(config.capture); });
    }

    // ---------- camera ----------
    function loadCamera(retry) {
        api('/api/collect/camera').then((data) => {
            if (!data.camera) {
                $('camera-waiting').hidden = false;
                if (!retry) setTimeout(() => loadCamera(true), 1500);
                return;
            }
            $('camera-waiting').hidden = true;
            cameraValues = data.camera;
            renderCameraForm();
        }).catch(() => {});
    }

    function renderCameraForm() {
        const cam = cameraValues || {};
        const values = {
            resolution: cam.resolution_width && cam.resolution_height ? cam.resolution_width + 'x' + cam.resolution_height : '',
            awb_mode: (cam.awb_mode || 'auto').toLowerCase(),
            rotation: (cam.rotation || 'auto').toLowerCase(),
            max_gain: String(config.capture.max_gain === undefined ? 'auto' : config.capture.max_gain).replace(/\.0$/, ''),
        };
        document.querySelectorAll('#pane-camera [data-camera]').forEach((seg) => {
            seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-pressed', String(values[seg.dataset.camera] === b.dataset.value)));
        });
        $('ev-value').textContent = cam.exp_compensation === undefined || cam.exp_compensation === null ? '-' : String(cam.exp_compensation);
    }

    function setCamera(params) {
        api('/api/collect/camera', { params: params }).then((data) => {
            cameraValues = Object.assign({}, cameraValues || {}, params);
            if ('max_gain' in params) config.capture.max_gain = params.max_gain;
            renderCameraForm();
            toast(t((data.restart_required || []).length ? 'saved_restart' : 'saved'), 'success');
            poll();
        }).catch((e) => toast(e.message, 'error'));
    }

    function stepBrightness(delta) {
        const current = Number((cameraValues || {}).exp_compensation || 0);
        // Same range the config editors offer
        const next = Math.max(-4, Math.min(4, current + delta));
        if (next !== current) setCamera({ exp_compensation: next });
    }

    function renderCameraLive() {
        const live = state.owls.filter((o) => o.connected).map((o) =>
            '<div><span class="c-k">' + esc(o.id) + '</span><span class="c-num">' +
            esc(shutterText(o.exposure_time_us)) + ' s · ' + (o.analogue_gain ? o.analogue_gain.toFixed(1) + '×' : '-') +
            (o.colour_temperature ? ' · ' + Math.round(o.colour_temperature) + ' K' : '') + '</span></div>');
        $('camera-live').innerHTML = live.join('');
        $('restart-row').hidden = !state.owls.some((o) => o.connected && o.restart_required);
    }

    // ---------- Live view ----------
    // One OWL's MJPEG stream through the controller (/api/video_feed/<id>).
    // Each open stream holds one of gunicorn's threads, so closing or
    // switching always drops the old src first.
    function openLive(id) {
        if (!id || id === liveOwl) return;
        liveOwl = id;
        const img = $('live-img');
        img.removeAttribute('src');
        img.hidden = false;
        $('live-error').hidden = true;
        $('live-title').textContent = t('live_title', { owl: id });
        renderLiveOwls();
        img.src = '/api/video_feed/' + encodeURIComponent(id) + '?t=' + Date.now();
        $('live-modal').hidden = false;
    }
    function closeLive() {
        if (!liveOwl) return;
        liveOwl = null;
        $('live-img').removeAttribute('src');
        $('live-modal').hidden = true;
    }
    function renderLiveOwls() {
        const ids = state ? state.owls.filter((o) => o.connected).map((o) => o.id) : [];
        const seg = $('live-owls');
        seg.hidden = ids.length < 2;
        seg.innerHTML = ids.map((id) => '<button type="button" data-live="' + esc(id) + '" aria-pressed="' +
            (id === liveOwl) + '">' + esc(id) + '</button>').join('');
    }
    function onLiveError() {
        if (!liveOwl) return;
        $('live-img').hidden = true;
        $('live-error').textContent = t('live_error', { owl: liveOwl });
        $('live-error').hidden = false;
    }
    // pointerdown, not click: the status rows are redrawn every second, and a
    // redraw between press and release would swallow the tap.
    function bindLiveButtons(container) {
        const open = (e) => {
            const b = e.target.closest('[data-live]');
            if (b) openLive(b.dataset.live);
        };
        container.addEventListener('pointerdown', open);
        container.addEventListener('click', open);
    }

    // ---------- Noktura ----------
    function renderCloudPane() {
        const c = state.cloud;
        let status = t('cloud_not_set');
        if (c.configured) status = c.connected === true ? t('cloud_connected') : c.connected === false ? t('cloud_offline') : t('cloud_checking');
        $('cloud-status').textContent = status;
        $('cloud-site').textContent = c.site || '-';
        $('cloud-owls').textContent = state.owls.map((o) =>
            o.id + ' ' + (o.internet_ok === true ? t('ok_short') : o.internet_ok === false ? t('no_route') : '-')).join(' · ') || '-';
        $('replace-row').hidden = !c.configured;
        // Shown as text: a link would open a window the kiosk cannot close
        const link = $('portal-link');
        link.hidden = !c.portal_url;
        link.textContent = c.portal_url ? t('cloud_portal', { url: c.portal_url }) : '';
    }

    function onConnect() {
        const command = $('paste-command').value;
        $('connect-btn').disabled = true;
        api('/api/collect/cloud/provision', { command: command, replace: $('replace-check').checked })
            .then((data) => {
                $('paste-command').value = '';
                toast(t('connected_ok', { site: data.device_id }), 'success');
                poll();
            })
            .catch((e) => toast(e.message, 'error'))
            .finally(() => { $('connect-btn').disabled = false; });
    }

    // ---------- polling ----------
    function poll() {
        return api('/api/collect/state').then((data) => {
            state = data;
            render();
        }).catch(() => {});
    }

    function bind() {
        document.querySelectorAll('.c-tab').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));
        document.querySelectorAll('.c-setnav button').forEach((b) => b.addEventListener('click', () => showPane(b.dataset.pane)));
        document.querySelectorAll('.c-lang button').forEach((b) => b.addEventListener('click', () => setLanguage(b.dataset.lang, true)));
        $('cloud-pill').addEventListener('click', () => { showView('settings'); showPane('sessions'); });
        $('record-btn').addEventListener('click', onRecordPress);
        $('stop-no').addEventListener('click', () => { $('stop-modal').hidden = true; });
        $('stop-yes').addEventListener('click', stopRecording);
        $('power-btn').addEventListener('click', onPowerPress);
        $('power-no').addEventListener('click', () => { $('power-modal').hidden = true; });
        $('power-yes').addEventListener('click', powerOff);
        $('map-centre').addEventListener('click', centreMap);
        $('sessions-refresh').addEventListener('click', refreshSessions);
        $('upload-btn').addEventListener('click', onUpload);
        $('delete-btn').addEventListener('click', onDeletePress);
        $('delete-no').addEventListener('click', () => { $('delete-modal').hidden = true; });
        $('delete-yes').addEventListener('click', deleteSessions);
        $('session-rows').addEventListener('click', (e) => {
            const b = e.target.closest('.c-tick');
            if (!b) return;
            ticked[b.dataset.key] = !ticked[b.dataset.key];
            renderSessions();
        });
        document.querySelectorAll('#pane-camera [data-camera]').forEach((seg) => seg.querySelectorAll('button').forEach((b) =>
            b.addEventListener('click', () => {
                const key = seg.dataset.camera, value = b.dataset.value;
                if (key === 'resolution') {
                    const [w, h] = value.split('x');
                    setCamera({ resolution_width: w, resolution_height: h });
                } else {
                    const params = {}; params[key] = value;
                    setCamera(params);
                }
            })));
        $('ev-down').addEventListener('click', () => stepBrightness(-1));
        $('ev-up').addEventListener('click', () => stepBrightness(1));
        $('calibrate-btn').addEventListener('click', () => api('/api/collect/camera', { action: 'calibrate_awb' })
            .then(() => toast(t('saved'), 'success')).catch((e) => toast(e.message, 'error')));
        $('restart-btn').addEventListener('click', () => api('/api/command', { device_id: 'all', action: 'restart_service' })
            .catch((e) => toast(e.message, 'error')));
        $('connect-btn').addEventListener('click', onConnect);
        bindLiveButtons($('owl-rows'));
        bindLiveButtons($('live-owls'));
        $('live-close').addEventListener('click', closeLive);
        $('live-img').addEventListener('error', onLiveError);
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeLive(); });
        document.addEventListener('visibilitychange', () => { if (document.hidden) closeLive(); });
    }

    function init() {
        recordModal = F.mountRecordModal({ presets: () => config.presets, onStart: startRecording });
        captureForm = F.mountCapture($('capture-form'), { onChange: saveCapture });
        if (typeof Numpad !== 'undefined') Numpad.init();
        bind();
        api('/api/collect/config').then((data) => {
            config = data;
            recordModal.setPick(data.last_pick);
            captureForm.render(config.capture);
            setLanguage(storageGet('collect.lang', data.language || 'en'), false);
        }).catch(() => setLanguage(storageGet('collect.lang', 'en'), false));
        poll();
        setInterval(poll, STATE_MS);
        setInterval(pollPoints, POINTS_MS);
        pollPoints();
        // Keep the elapsed timer ticking between polls
        setInterval(renderRecordButton, 1000);
    }

    document.addEventListener('DOMContentLoaded', init);
})();
