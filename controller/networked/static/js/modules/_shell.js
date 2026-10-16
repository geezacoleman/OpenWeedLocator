// ============================================
// OWL Central Controller - redesigned shell (2026-10-12)
// "The standalone in horizontal, with more OWLs down one side": the OWL
// rail, Dashboard | Data | Map | Settings, the AI models page per OWL,
// camera, data collection, test nozzles and maintenance. Built from
// /shared/js/owl_ui.js; fed every poll by updateDashboard() -> Shell.update.
// The rail selection is the scope every control acts on (currentScope()).
// The live view is a pop-up (Live view on Dashboard), not a fixed panel.
// ============================================

let _scope = 'all';
function currentScope() { return _scope; }

const Shell = (function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const UI = () => window.OwlUI;
    let nav = null;
    let settings = null;
    const seen = {};           // device id -> last state seen (offline cards stay)
    let lastData = {};
    let collectState = null;
    let collectTimer = null;
    let feedTimer = null;
    let feedBusy = false;
    let feedRetryAt = 0;       // back off 5 s after a failed snapshot
    let blocked = null;        // {mode, html} after a refused or blocked mode tap
    let aiPicked = {};         // device id -> class list being sent
    let fleetUpdate = null;
    let updateTimer = null;
    let camValues = null;
    let evStep = null;
    let dcForm = null;
    let livePop = null;
    let liveId = null;          // OWL picked in the live view pop-up (null = the selection)
    let nozzleTester = null;
    let trackPersistStep = null;
    let trackBufferStep = null;

    // ---------- init ----------
    function init() {
        UI().fillIcons(document);
        nav = UI().initNav($('mainNav'), onView);
        settings = UI().initSettings($('view-settings'), onPanel);
        $('power-btn').addEventListener('click', () => initiateShutdown());
        $('detectSwitch').addEventListener('click', toggleMainDetection);
        $('recordSwitch').addEventListener('click', toggleMainRecording);
        $('nozzleSwitch').addEventListener('click', toggleAllNozzles);
        $('trackingSwitch').addEventListener('click', toggleTracking);
        document.querySelectorAll('#pipeline-mode-selector .mode-btn').forEach((b) =>
            b.addEventListener('click', () => pickMode(b.dataset.mode)));
        document.querySelectorAll('#sensitivity-seg [data-level]').forEach((b) =>
            b.addEventListener('click', () => setSensitivityLevel(b.dataset.level)));
        livePop = UI().livePop($('livePop'), { onOpen: startFeed, onClose: () => { stopFeed(); liveId = null; } });
        $('liveOpen').addEventListener('click', () => livePop.open());
        $('feedFull').addEventListener('click', () => { const id = feedOwl(); if (id) openVideoFeed(id); });
        $('feedFrame').addEventListener('click', () => { const id = feedOwl(); if (id) grabFrame(id); });
        $('sessionsRefresh').addEventListener('click', () => { fetch('/api/collect/sessions/refresh', { method: 'POST' }).catch(() => {}); setTimeout(pollCollect, 1500); });
        $('camGeometry').addEventListener('click', () => { settings.open('colour', true); setTimeout(() => toggleGeometryEditor(), 100); });
        $('rebootController').addEventListener('click', rebootController);
        $('fleetUpdateBtn').addEventListener('click', fleetUpdateStart);
        initCamera();
        initNozzles();
        initTracking();
        initCapture();
        pollFleetUpdate();
    }

    function isView(name) { const v = $('view-' + name); return !!(v && v.classList.contains('is-on')); }

    function onView(name) {
        livePop.close();
        if (name === 'map') { startGPSPolling(); if (typeof window.gpsMapShow === 'function') window.gpsMapShow(); }
        else stopGPSPolling();
        if (name === 'data') { pollCollect(); collectTimer = collectTimer || setInterval(pollCollect, 3000); }
        else if (collectTimer) { clearInterval(collectTimer); collectTimer = null; }
        if (name === 'settings') onPanel((settings.current() || {}).panel || 'colour');
        else stopConfigPreview();
    }

    function onPanel(name) {
        if (name === 'colour') startConfigPreview(); else stopConfigPreview();
        if (name === 'camera') loadCamera();
        if (name === 'ai') renderAIPanel();
        if (name === 'nozzles') nozzleTester.render();
        if (name === 'maint') { renderMaint(); pollFleetUpdate(); }
    }

    function openSettings(panel) {
        nav.show('settings');
        settings.open(panel, true);
    }

    // ---------- scope (the OWL rail) ----------
    function owlName(id) {
        const o = owlsData[id] || seen[id] || {};
        return o.friendly_name || id;
    }
    function setScope(id) {
        _scope = id;
        blocked = null;
        // Preview, editor and Advanced follow the selected OWL
        const prev = $('config-preview-device');
        if (id !== 'all' && prev && [...prev.options].some((o) => o.value === id)) { prev.value = id; pollConfigFeed(); }
        const ed = $('config-editor-device');
        if (id !== 'all' && ed && [...ed.options].some((o) => o.value === id)) ed.value = id;
        lastSliderSendTime = 0;
        updateDashboard();
        render();
        liveId = null;
        if (livePop.isOpen()) { stopFeed(); startFeed(); }
        if (isView('settings') && (settings.current() || {}).panel === 'ai') renderAIPanel();
    }
    // The OWL the controls show: the selected one, or the first connected
    function scopeOwl() {
        if (_scope !== 'all') return owlsData[_scope] && owlsData[_scope].connected ? owlsData[_scope] : null;
        const id = firstConnected();
        return id ? owlsData[id] : null;
    }
    function firstConnected() {
        return Object.keys(owlsData).sort().find((id) => owlsData[id] && owlsData[id].connected) || null;
    }
    function feedOwl() {
        if (liveId && owlsData[liveId] && owlsData[liveId].connected) return liveId;
        if (_scope !== 'all') return owlsData[_scope] && owlsData[_scope].connected ? _scope : null;
        return firstConnected();
    }

    function railHTML() {
        const U = UI();
        const now = Date.now() / 1000;
        const ids = Object.keys(seen).sort();
        const online = ids.filter((id) => owlsData[id] && owlsData[id].connected);
        const spraying = online.filter((id) => owlsData[id].detection_enable).length;
        let html = '<button type="button" class="ou-owl" data-owl="all" aria-selected="' + (_scope === 'all') + '"><div class="nm"><span>All OWLs</span></div><div class="st">' +
            U.esc(!ids.length ? 'No OWLs yet' : spraying + ' of ' + ids.length + ' spraying') + '</div></button>';
        ids.forEach((id) => {
            const live = owlsData[id] && owlsData[id].connected;
            const o = live ? owlsData[id] : (seen[id] || {});
            let pill;
            let st;
            if (!live) {
                pill = '<span class="ou-pill off">Off</span>';
                const ago = o._seenAt ? now - o._seenAt : (o.last_seen ? now - o.last_seen : null);
                st = U.esc(U.relTime(ago));
            } else {
                if (o.fault) pill = '<span class="ou-pill warn">Fault</span>';
                else if (o.detection_mode === 2) pill = '<span class="ou-pill ok"><span class="ou-dot"></span>All on</span>';
                else if (o.detection_enable) pill = '<span class="ou-pill ok"><span class="ou-dot"></span>On</span>';
                else if (o.image_sample_enable) pill = '<span class="ou-pill rec"><span class="ou-dot"></span>Rec</span>';
                else pill = '<span class="ou-pill off">Ready</span>';
                const cap = o.capture || {};
                st = U.joinSep([typeof cap.saved_count === 'number' ? cap.saved_count + ' photos' : null,
                    typeof o.speed_kmh === 'number' ? o.speed_kmh.toFixed(1) + ' km/h' : null]) || U.esc('Online');
            }
            html += '<button type="button" class="ou-owl' + (live ? '' : ' off') + '" data-owl="' + U.esc(id) + '" aria-selected="' + (_scope === id) +
                '"><div class="nm"><span>' + U.esc(owlName(id)) + '</span>' + pill + '</div><div class="st">' + st + '</div></button>';
        });
        return html;
    }

    function renderRails() {
        const html = railHTML();
        document.querySelectorAll('[data-rail]').forEach((rail) => {
            if (rail.dataset.html === html) return;
            rail.dataset.html = html;
            rail.innerHTML = html;
            rail.querySelectorAll('[data-owl]').forEach((b) => b.addEventListener('click', () => setScope(b.dataset.owl)));
        });
    }

    // ---------- live view (pop-up) ----------
    // A snapshot every second while the pop-up is open (as the old config
    // feed did): far lighter than an MJPEG stream through the controller.
    function startFeed() {
        if (feedTimer) return;
        $('fieldFeedEmpty').hidden = false;
        $('fieldFeedEmpty').textContent = 'Loading the camera picture';
        feedRetryAt = 0;
        feedTimer = setInterval(nextFrame, 1000);
        nextFrame();
    }
    function stopFeed() {
        clearInterval(feedTimer);
        feedTimer = null;
        feedBusy = false;
        $('fieldFeed').removeAttribute('src');
        $('fieldFeed').hidden = true;
    }
    // One button per online OWL in the pop-up head (two or more online)
    function renderLivePick(id) {
        const online = Object.keys(owlsData).sort().filter((k) => owlsData[k] && owlsData[k].connected);
        const html = online.length < 2 ? '' : online.map((k) =>
            '<button type="button" data-live="' + UI().esc(k) + '" class="' + (k === id ? 'on' : '') + '">' + UI().esc(owlName(k)) + '</button>').join('');
        const box = $('livePick');
        box.hidden = !html;
        if (box.dataset.html === html) return;
        box.dataset.html = html;
        box.innerHTML = html;
        box.querySelectorAll('[data-live]').forEach((b) => b.addEventListener('click', () => {
            liveId = b.dataset.live;
            feedRetryAt = 0;
            nextFrame();
        }));
    }
    function nextFrame() {
        const img = $('fieldFeed');
        const id = feedOwl();
        $('feedLabel').textContent = id ? owlName(id) : 'Live view';
        renderLivePick(id);
        if (!id) {
            img.hidden = true;
            $('fieldFeedEmpty').hidden = false;
            $('fieldFeedEmpty').textContent = _scope !== 'all' ? owlName(_scope) + ' is offline'
                : (Object.keys(owlsData).length ? 'No OWL connected' : 'Waiting for OWLs');
            return;
        }
        if (feedBusy || document.hidden || Date.now() < feedRetryAt) return;
        feedBusy = true;
        const probe = new Image();
        probe.onload = () => { img.src = probe.src; img.hidden = false; $('fieldFeedEmpty').hidden = true; feedBusy = false; };
        probe.onerror = () => { feedBusy = false; feedRetryAt = Date.now() + 5000; if (img.hidden) { $('fieldFeedEmpty').hidden = false; $('fieldFeedEmpty').textContent = 'No picture from ' + owlName(id); } };
        probe.src = '/api/snapshot/' + encodeURIComponent(id) + '?t=' + Date.now();
    }

    // ---------- detect with ----------
    function blockReason() {
        const scoped = scopedOwls();
        const noAI = scoped.filter((o) => !UI().canRunAI(o.ai));
        if (noAI.length) return { kind: 'no_ai', owls: noAI.map((o) => o.friendly_name || o._id) };
        const noModel = scoped.filter((o) => UI().aiReported(o.ai) && o.model_available === false);
        if (noModel.length) return { kind: 'no_model', owls: noModel.map((o) => o.friendly_name || o._id) };
        return null;
    }
    function pickMode(mode) {
        const reason = (mode === 'gog' || mode === 'hybrid') ? blockReason() : null;
        if (reason) {
            const who = reason.owls.length === scopedOwls().length ? (_scope === 'all' ? 'these OWLs' : 'this OWL') : reason.owls.join(', ');
            blocked = {
                mode: mode,
                html: reason.kind === 'no_ai'
                    ? '<b>AI isn\'t installed on ' + UI().esc(who) + '.</b> Colour detection stays on. <button type="button" class="lnk" data-go="ai">Install AI ›</button>'
                    : '<b>No AI model on ' + UI().esc(who) + ' yet.</b> Colour detection stays on. <button type="button" class="lnk" data-go="ai">AI models ›</button>',
            };
            renderDetectNotice();
            return;
        }
        blocked = null;
        renderDetectNotice();
        setPipelineMode(mode);
    }
    function modeRefused(mode, message) {
        blocked = { mode: mode, html: '<b>Not switched.</b> ' + UI().esc(message) };
        renderDetectNotice();
    }
    function renderDetectNotice() {
        const reason = blockReason();
        if (typeof updateModeAvailability === 'function') updateModeAvailability(!reason);
        document.querySelectorAll('#pipeline-mode-selector .mode-btn').forEach((b) =>
            b.classList.toggle('picked', !!blocked && b.dataset.mode === blocked.mode));
        const box = $('detectNotice');
        const html = blocked ? UI().notice('warn', blocked.html) : '';
        if (box.dataset.html === html) return;
        box.dataset.html = html;
        box.innerHTML = html;
        const go = box.querySelector('[data-go]');
        if (go) go.addEventListener('click', () => { blocked = null; renderDetectNotice(); openSettings('ai'); });
    }

    // ---------- per-poll update ----------
    function update(data) {
        lastData = data || {};
        const now = Date.now() / 1000;
        Object.keys(owlsData).forEach((id) => {
            const o = owlsData[id];
            if (o && o.connected) seen[id] = Object.assign({}, o, { _seenAt: now });
            else if (o && !seen[id]) seen[id] = Object.assign({}, o, { _seenAt: o.last_seen || null });
        });
        render();
    }

    function render() {
        const U = UI();
        renderRails();
        const online = Object.keys(owlsData).filter((id) => owlsData[id] && owlsData[id].connected);
        const scoped = scopedOwls();
        const one = scopeOwl();
        const scopeLabel = _scope === 'all' ? 'All OWLs' : owlName(_scope);
        document.querySelectorAll('[data-scope-label]').forEach((el) => { el.textContent = scopeLabel; });

        // State pill
        let state = ['idle', 'Ready'];
        if (!mqttConnected) state = ['bad', 'No broker'];
        else if (!online.length) state = ['idle', 'No OWLs'];
        else if (online.some((id) => owlsData[id].fault)) state = ['bad', 'Fault'];
        else if (online.some((id) => owlsData[id].detection_enable)) state = ['', 'Spraying'];
        else if (online.some((id) => owlsData[id].image_sample_enable)) state = ['rec', 'Recording'];
        $('stateDot').className = 'ou-dot ' + state[0];
        $('stateText').textContent = state[1];
        $('topSub').textContent = online.length ? online.length + (online.length === 1 ? ' OWL online' : ' OWLs online') : '';

        // Spraying
        const mode = one ? U.algoMode(one.algorithm) : 'gob';
        const spraying = globalDetectionEnabled && !globalNozzlesActive;
        U.setSwitch($('detectSwitch'), spraying, { go: spraying });
        $('sprayHint').innerHTML = globalNozzlesActive ? U.esc('Off while blanket spray is on')
            : U.joinSep([scopeLabel, spraying && one ? U.MODE_LABELS[mode] : (spraying ? null : 'Off')]);

        // Recording (fleet-wide run)
        U.setSwitch($('recordSwitch'), globalRecordingEnabled, { rec: true });
        renderRecordHint();
        U.setSwitch($('nozzleSwitch'), globalNozzlesActive, { go: globalNozzlesActive });
        $('blanketHint').textContent = globalNozzlesActive ? 'Every nozzle is spraying' : 'Every nozzle on, detection off';
        U.setSwitch($('trackingSwitch'), globalTrackingEnabled);
        const liveOwl = feedOwl();
        $('liveHint').textContent = liveOwl ? 'Camera picture from ' + owlName(liveOwl) : 'No OWL connected';

        renderNotices(scoped);
        renderDetectNotice();
        renderOffline(scoped);
        renderTracking(one);

        // Stat cells: speed and frame rate always; the slowest OWL sets the frame rate
        const faults = scoped.filter((o) => o.fault).length;
        let health;
        if (!scoped.length) health = { label: 'Status', value: _scope === 'all' ? 'No OWLs' : 'Offline', cls: 'warn' };
        else if (faults) health = { label: 'Status', value: faults === 1 ? 'Fault' : faults + ' faults', cls: 'bad' };
        else health = { label: 'Status', value: 'Healthy', cls: 'ok' };
        const frees = scoped.map((o) => o.storage_free_mb).filter((v) => typeof v === 'number');
        const free = frees.length ? U.fmtGB(Math.min.apply(null, frees)) : null;
        const speeds = scoped.map((o) => o.speed_kmh).filter((v) => typeof v === 'number');
        const speed = speeds.length ? (speeds.reduce((a, b) => a + b, 0) / speeds.length).toFixed(1) + ' km/h' : null;
        const loops = scoped.map((o) => o.avg_loop_time_ms).filter((v) => typeof v === 'number' && v > 0);
        const fps = loops.length ? Math.round(1000 / Math.max.apply(null, loops)) + ' fps' : null;
        U.statCells($('fieldStats'), [health,
            { label: 'Speed', value: speed || (scoped.length ? 'No GPS' : null) },
            { label: 'Frame rate', value: fps },
            { label: 'Free', value: free }]);
        if (isView('settings') && (settings.current() || {}).panel === 'nozzles') nozzleTester.render();

        // Side list badges: problems show on the row itself
        const needAI = online.filter((id) => U.aiReported(owlsData[id].ai) && !U.canRunAI(owlsData[id].ai)).length;
        U.setBadge($('view-settings'), 'ai', needAI ? (needAI === online.length ? 'Not installed' : needAI + ' need AI') : null);

        if (isView('settings')) {
            const p = (settings.current() || {}).panel;
            if (p === 'ai') renderAIPanel();
            if (p === 'maint') renderMaint();
        }
        if (isView('data')) renderSessions();
        renderCameraNotice();
    }

    function renderRecordHint() {
        const U = UI();
        const recOwls = Object.keys(owlsData).filter((id) => owlsData[id] && owlsData[id].connected && owlsData[id].image_sample_enable)
            .map((id) => owlsData[id]);
        const hint = $('recordHint');
        if (!globalRecordingEnabled || !recOwls.length) {
            hint.textContent = _scope === 'all' ? 'Off' : 'Off, records on all OWLs';
            $('sessionChips').innerHTML = '';
            return;
        }
        const paused = recOwls.find((o) => (o.capture || {}).paused_slow);
        if (paused) {
            hint.textContent = 'Paused below ' + paused.capture.min_speed_kmh + ' km/h';
        } else {
            const n = recOwls.reduce((sum, o) => sum + (((o.capture || {}).saved_count) || 0), 0);
            hint.textContent = n + (n === 1 ? ' photo' : ' photos') + (recOwls.length > 1 ? ' on ' + recOwls.length + ' OWLs' : '');
        }
        const md = recOwls.map((o) => o.session_metadata).find((m) => m && m.crop);
        const chips = md && window.CollectForm ? (CollectForm.metadataChips(md, _collectConfig.presets) || []) : [];
        $('sessionChips').innerHTML = U.labsHTML(chips.slice(0, 3));
    }

    const FAULT_ACTIONS = { nozzles_off: 'Nozzles are off.', blanket: 'Blanket spraying while spraying is on.', spot_spray: 'Spot spraying continues.' };
    function renderNotices(scoped) {
        const U = UI();
        const out = [];
        const many = _scope === 'all';
        const who = (o) => many ? (o.friendly_name || o._id) + ': ' : '';
        if (_scope !== 'all' && !(owlsData[_scope] && owlsData[_scope].connected)) {
            const s = seen[_scope] || {};
            out.push(U.notice('warn', '<b>' + U.esc(owlName(_scope)) + ' is offline.</b> ' + U.esc(U.relTime(s._seenAt ? Date.now() / 1000 - s._seenAt : null)) + '.'));
        }
        scoped.forEach((o) => {
            if (o.fault) out.push(U.notice('bad', '<b>' + U.esc(who(o)) + 'Fault: ' + U.esc(o.fault.message) + '.</b> ' + U.esc(FAULT_ACTIONS[o.fault.action] || '')));
            if (o.algorithm_error) out.push(U.notice('bad', '<b>' + U.esc(who(o)) + 'Detection could not start.</b> ' + U.esc(o.algorithm_error)));
            if (o.speed_warning) out.push(U.notice('warn', '<b>' + U.esc(who(o)) + 'Slow down.</b> ' + U.esc(o.speed_warning)));
            if (o.recording_blocked_reason && !o.image_sample_enable) {
                out.push(U.notice('warn', '<b>' + U.esc(who(o)) + 'Cannot record:</b> ' + U.esc(String(o.recording_blocked_reason).replace(/_/g, ' '))));
            }
            if (o.controller_type && o.controller_type !== 'none') {
                out.push(U.notice('info', '<b>' + U.esc(who(o)) + 'Cab switch fitted (' + U.esc(o.controller_type) + ').</b> Use the standalone dashboard on that OWL.'));
            }
        });
        if (scoped.some((o) => o.restart_required)) {
            out.push(U.notice('info', '<b>Restart needed.</b> New settings apply after the OWLs restart. <button type="button" class="lnk" data-act="restart">Restart OWLs ›</button>'));
        }
        // Different AI models matter once an OWL sprays with AI
        const mismatch = mismatchHTML();
        if (mismatch && Object.keys(owlsData).some((id) => owlsData[id] && owlsData[id].connected &&
                ['gog', 'gog-hybrid'].indexOf(owlsData[id].algorithm) !== -1)) out.unshift(mismatch);
        const html = out.slice(0, 3).join('');
        const box = $('fieldNotices');
        if (box.dataset.html !== html) {
            box.dataset.html = html;
            box.innerHTML = html;
            const r = box.querySelector('[data-act="restart"]');
            if (r) r.addEventListener('click', () => { sendCommand(currentScope(), 'restart_service'); showToast('Restarting', 'info'); });
            wireUseModel(box);
        }
        const ai = $('aiNotices');
        if (ai.dataset.html !== mismatch) {
            ai.dataset.html = mismatch;
            ai.innerHTML = mismatch;
            wireUseModel(ai);
        }
    }

    // No OWL in the selection: the settings that act on OWLs are greyed
    // with the reason (never hidden). With OWLs, updateSliderVisibility
    // (_config_tab.js) decides per detection mode.
    function renderOffline(scoped) {
        if (scoped.length) {
            document.querySelectorAll('#colourSteppers [data-pstep]').forEach((el) => UI().stepOff(el, false));
            return;
        }
        const why = _scope === 'all' ? 'No OWL connected' : owlName(_scope) + ' is offline';
        document.querySelectorAll('[data-pstep]').forEach((el) => UI().stepOff(el, true, why));
        const html = UI().notice('info', '<b>' + UI().esc(why) + '.</b> The values show the defaults until an OWL connects.');
        const box = $('colourNotices');
        if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
    }

    // ---------- tracking (AI models) ----------
    function initTracking() {
        document.querySelectorAll('#track-stability-selector [data-stability]').forEach((b) => b.addEventListener('click', () => {
            const level = b.dataset.stability;
            setTrackParams(UI().TRACK_PRESETS[level], 'Track stability ' + b.textContent.toLowerCase());
        }));
        trackPersistStep = UI().stepper($('trackPersist'), {
            value: null, min: 0, max: 15, step: 1, label: 'Spray a lost weed for (frames)', settleMs: 400,
            onChange: (v) => setTrackParams({ detection_persist_frames: String(v) }),
        });
        trackBufferStep = UI().stepper($('trackBuffer'), {
            value: null, min: 10, max: 150, step: 10, label: 'Remember a lost track for (frames)', settleMs: 400,
            onChange: (v) => setTrackParams({ track_buffer: String(v) }),
        });
    }
    function renderTracking(one) {
        const p = (one && one.track_params) || {};
        const has = Object.keys(p).length > 0;
        const on = !!one && globalTrackingEnabled;
        const preset = has ? UI().trackPreset(p) : null;
        document.querySelectorAll('#track-stability-selector [data-stability]').forEach((b) => {
            b.classList.toggle('on', b.dataset.stability === preset);
            b.disabled = !on;
        });
        $('trackCustom').textContent = has && !preset ? '(custom)' : '';
        trackPersistStep.set(typeof p.detection_persist_frames === 'number' ? p.detection_persist_frames : null);
        trackBufferStep.set(typeof p.track_buffer === 'number' ? p.track_buffer : null);
        const why = !one ? 'No OWL connected' : !has ? 'Update this OWL to change these' : !on ? 'Turn Track weeds on to change these' : '';
        trackPersistStep.disable(!!why);
        trackBufferStep.disable(!!why);
        $('trackWhy').textContent = why;
        $('trackWhy').hidden = !why;
    }

    // ---------- one AI model on every OWL ----------
    // The usual set-up is the same model on every OWL; a difference is
    // allowed but always shown in red, with a one-tap fix.
    function mismatchHTML() {
        const online = Object.keys(owlsData).sort().filter((id) => owlsData[id] && owlsData[id].connected);
        const mm = UI().modelMismatch(online.map((id) => ({
            name: owlName(id), current_model: owlsData[id].current_model, available_models: owlsData[id].available_models,
        })));
        if (!mm) return '';
        const who = Object.keys(mm.byModel).map((m) => mm.byModel[m].join(', ') + ': ' + (m || 'no model')).join('. ');
        const fix = mm.common.length ? ' <button type="button" class="lnk" data-use-model="' + UI().esc(mm.common[0]) + '">Use ' + UI().esc(mm.common[0]) + ' on all ›</button>'
            : ' Send one model to every OWL from the Model library.';
        return UI().notice('bad', '<b>The OWLs run different AI models.</b> ' + UI().esc(who) + '.' + fix);
    }
    function wireUseModel(box) {
        box.querySelectorAll('[data-use-model]').forEach((b) => b.addEventListener('click', async () => {
            const name = b.dataset.useModel;
            const ok = await UI().confirm({ title: 'Use ' + name + ' on every OWL?', body: 'Each OWL loads it now. Spraying carries on while it loads.', ok: 'Use on all' });
            if (!ok) return;
            sendCommand('all', 'set_model', name);
            showToast('Loading ' + name + ' on every OWL', 'info');
        }));
    }

    // ---------- AI models ----------
    function renderAIPanel() {
        const U = UI();
        const el = $('aiModels');
        if (_scope !== 'all') {
            const o = owlsData[_scope];
            if (!o || !o.connected) {
                el.innerHTML = U.notice('warn', '<b>' + U.esc(owlName(_scope)) + ' is offline.</b> Its models show when it is back.');
                return;
            }
            const id = _scope;
            const picked = aiPicked[id] && Date.now() - aiPicked[id].at < 6000 ? aiPicked[id].list : null;
            const upd = ((fleetUpdate || {}).devices || {})[id];
            U.renderAIModels(el, o, {
                setModel: (name) => { sendCommand(id, 'set_model', name); showToast('Loading ' + name + ' on ' + owlName(id), 'info'); },
                toggleClass: (name, all) => {
                    const list = U.toggleClass(picked || o.detect_classes || [], name, all);
                    aiPicked[id] = { list: list, at: Date.now() };
                    renderAIPanel();
                    clearTimeout(aiPicked[id].timer);
                    aiPicked[id].timer = setTimeout(() => sendCommand(id, 'set_detect_classes', list), 600);
                },
                installAI: () => installAI([id]),
            }, { pickedClasses: picked, update: upd ? upd.software_update : null,
                 noModelsHint: 'Upload models on the Model library page (Maintenance), then send them here.' });
            return;
        }
        const ids = Object.keys(seen).sort();
        let html = '<div class="ou-list">';
        if (!ids.length) html += '<div class="ou-row"><div class="main"><span class="name">No OWLs yet</span><span class="meta">OWLs show here when they connect.</span></div></div>';
        ids.forEach((id) => {
            const live = owlsData[id] && owlsData[id].connected;
            const o = live ? owlsData[id] : seen[id];
            const b = U.aiRunBadge(o.ai);
            html += '<button type="button" class="ou-row" data-owl="' + U.esc(id) + '"><div class="main"><span class="name">' + U.esc(owlName(id)) +
                '</span><span class="meta">' + U.esc(live ? (o.current_model || 'No model loaded') : 'Offline') + '</span></div><span class="ou-run ' + b.cls + '">' + U.esc(b.text) + '</span></button>';
        });
        html += '</div>';
        const need = ids.filter((id) => owlsData[id] && owlsData[id].connected && U.aiReported(owlsData[id].ai) && !U.canRunAI(owlsData[id].ai));
        html += '<p class="ou-hint" style="margin-top:8px">Tap an OWL to choose its model and what it sprays. The same model on every OWL is the usual set-up.</p>' +
            '<div class="ou-actions" style="margin-top:10px"><a class="ou-btn navy slim" href="/models">Send a model to all OWLs</a>' +
            (need.length ? '<button type="button" class="ou-btn ghost slim" id="installNeeded">Install AI on ' + need.length + (need.length === 1 ? ' OWL' : ' OWLs') + '</button>' : '') +
            '<span class="ou-hint">Models are uploaded on the Model library page.</span></div>';
        if (el.dataset.html === html) return;
        el.dataset.html = html;
        el.innerHTML = html;
        el.querySelectorAll('[data-owl]').forEach((b) => b.addEventListener('click', () => setScope(b.dataset.owl)));
        const inst = $('installNeeded');
        if (inst) inst.addEventListener('click', () => installAI(need));
    }

    async function installAI(ids) {
        const ok = await UI().confirm({
            title: ids.length === 1 ? 'Install AI on ' + owlName(ids[0]) + '?' : 'Install AI on ' + ids.length + ' OWLs?',
            body: 'Each OWL downloads about 2 GB and takes 20 minutes or more, one after the other. Spraying keeps working on colour detection.',
            ok: 'Install AI',
        });
        if (!ok) return;
        fetch('/api/ai/install', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device_ids: ids }),
        }).then((r) => r.json()).then((data) => {
            if (!data.success) throw new Error(data.error || 'Could not start the install');
            showToast('Installing AI', 'info');
            pollFleetUpdate();
        }).catch((e) => showToast(e.message, 'error'));
    }

    // ---------- maintenance ----------
    function renderMaint() {
        const U = UI();
        const ids = Object.keys(seen).sort();
        let html = '';
        if (!ids.length) html = '<div class="ou-row"><div class="main"><span class="name">No OWLs yet</span></div></div>';
        ids.forEach((id) => {
            const live = owlsData[id] && owlsData[id].connected;
            const o = live ? owlsData[id] : seen[id];
            const meta = live ? U.joinSep([o.version ? 'v' + o.version : null, o.git_branch && o.git_branch !== 'unknown' ? o.git_branch : null,
                typeof o.cpu_temp === 'number' && o.cpu_temp > 0 ? Math.round(o.cpu_temp) + ' °C' : null,
                typeof o.cpu_percent === 'number' ? 'Load ' + Math.round(o.cpu_percent) + '%' : null,
                typeof o.memory_percent === 'number' ? 'Memory ' + Math.round(o.memory_percent) + '%' : null]) : U.esc('Offline');
            html += '<div class="ou-row"><div class="main"><span class="name">' + U.esc(owlName(id)) + '</span><span class="meta">' + meta +
                '</span></div>' + (live ? '<button type="button" class="ou-btn ghost slim" data-restart="' + U.esc(id) + '">Restart</button>' : '') + '</div>';
        });
        const box = $('maintOwls');
        if (box.dataset.html !== html) {
            box.dataset.html = html;
            box.innerHTML = html;
            box.querySelectorAll('[data-restart]').forEach((b) => b.addEventListener('click', async () => {
                if (await UI().confirm({ title: 'Restart ' + owlName(b.dataset.restart) + '?', body: 'It stops spraying for about 20 seconds.', ok: 'Restart' })) {
                    restartOWL(b.dataset.restart);
                }
            }));
        }
        const fu = (fleetUpdate || {}).fleet_update || {};
        const running = !!fu.active;
        $('fleetUpdateBtn').disabled = running;
        $('fleetUpdateMeta').textContent = running ? 'Updating ' + (fu.current ? owlName(fu.current) : 'the OWLs') + '. Each OWL restarts when done.'
            : fu.finished_at && fu.error ? 'Last update stopped: ' + fu.error
            : fu.finished_at ? 'Last update finished'
            : (_scope === 'all' ? 'All OWLs, one at a time. Needs internet.' : owlName(_scope) + ' only. Needs internet.');
    }

    function pollFleetUpdate() {
        clearTimeout(updateTimer);
        fetch('/api/update/status').then((r) => r.json()).then((data) => {
            fleetUpdate = data;
            if (isView('settings')) renderMaint();
            const fu = data.fleet_update || {};
            const any = Object.values(data.devices || {}).some((d) => {
                const s = (d.software_update || {}).status;
                return s && ['idle', 'complete', 'rolled_back', 'error'].indexOf(s) === -1;
            });
            if (any || fu.active) updateTimer = setTimeout(pollFleetUpdate, 4000);
        }).catch(() => {});
    }

    async function fleetUpdateStart() {
        const ids = _scope === 'all' ? Object.keys(owlsData).filter((id) => owlsData[id] && owlsData[id].connected) : [_scope];
        const branches = Array.from(new Set(ids.map((id) => (owlsData[id] || {}).git_branch).filter((b) => b && b !== 'unknown' && b !== 'HEAD')));
        if (!ids.length) { showToast('No OWLs connected', 'warning'); return; }
        if (branches.length > 1) { showToast('These OWLs run different branches. Update them one at a time.', 'warning'); return; }
        const ok = await UI().confirm({
            title: ids.length === 1 ? 'Update ' + owlName(ids[0]) + '?' : 'Update ' + ids.length + ' OWLs?',
            body: 'Needs internet on the OWLs. They update one at a time and restart. A failed update rolls back.', ok: 'Update',
        });
        if (!ok) return;
        fetch('/api/update', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_ids: _scope === 'all' ? 'all' : ids, ref: branches[0] || 'main' }),
        }).then((r) => r.json()).then((data) => {
            if (!data.success) throw new Error(data.error || 'Could not start the update');
            showToast('Updating', 'info');
            pollFleetUpdate();
        }).catch((e) => showToast(e.message, 'error'));
    }

    async function rebootController() {
        const ok = await UI().confirm({ title: 'Restart this controller?', body: 'The OWLs keep running. The screen is back in about a minute.', ok: 'Restart' });
        if (!ok) return;
        fetch('/api/system/reboot', { method: 'POST' }).then(() => showToast('Restarting the controller', 'info')).catch(() => showToast('Restart sent', 'info'));
    }

    // ---------- camera (/api/collect/camera, all OWLs) ----------
    function initCamera() {
        $('camAwb').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
            if (b.dataset.value === 'manual') { calibrate(); return; }
            setCamera({ awb_mode: b.dataset.value });
        }));
        $('camCalibrate').addEventListener('click', calibrate);
        $('camRes').innerHTML = UI().RESOLUTIONS.map((r) => '<button type="button" data-value="' + r + '">' + r.replace('x', '×') + '</button>').join('');
        $('camRes').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
            const [w, h] = b.dataset.value.split('x');
            setCamera({ resolution_width: w, resolution_height: h }, true);
        }));
        $('camRot').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => setCamera({ rotation: b.dataset.value }, true)));
        evStep = UI().stepper($('camEv'), {
            value: null, min: -10, max: 10, step: 1, label: 'Brightness', settleMs: 400,
            format: (v) => (v > 0 ? '+' + v : v < 0 ? '−' + Math.abs(v) : '0'),
            onChange: (v) => setCamera({ exp_compensation: String(v) }),
        });
    }
    function loadCamera() {
        fetch('/api/collect/camera').then((r) => r.json()).then((data) => {
            camValues = data.camera || null;
            renderCamera();
            if (!camValues) setTimeout(() => { if ((settings.current() || {}).panel === 'camera' && isView('settings')) loadCamera(); }, 2000);
        }).catch(() => {});
    }
    // The values come from a connected OWL's settings: with none connected
    // nothing can show as selected, so the page says so and greys out.
    function renderCamera() {
        const c = camValues || {};
        const none = !camValues;
        const awb = String(c.awb_mode || '').toLowerCase();
        $('camAwb').querySelectorAll('button').forEach((b) => { b.classList.toggle('on', b.dataset.value === awb); b.disabled = none; });
        evStep.set(c.exp_compensation === undefined || c.exp_compensation === null || c.exp_compensation === '' ? null : parseInt(c.exp_compensation, 10) || 0);
        evStep.disable(none);
        const res = UI().resolutionKey(c.resolution_width, c.resolution_height);
        $('camRes').querySelectorAll('button').forEach((b) => { b.classList.toggle('on', b.dataset.value === res); b.disabled = none; });
        $('camResNow').textContent = res && UI().RESOLUTIONS.indexOf(res) === -1 ? 'Now ' + res.replace('x', '×') : '';
        const rot = String(c.rotation || '').toLowerCase();
        $('camRot').querySelectorAll('button').forEach((b) => { b.classList.toggle('on', b.dataset.value === rot); b.disabled = none; });
        $('camCalibrate').disabled = none;
        const online = Object.keys(owlsData).some((id) => owlsData[id] && owlsData[id].connected);
        const html = !none ? '' : UI().notice('info', online
            ? '<b>Waiting for the OWLs to send their camera settings.</b>'
            : '<b>No OWL connected.</b> The camera settings show here when an OWL connects.');
        const box = $('cameraState');
        if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
    }
    function calibrate() {
        fetch('/api/collect/camera', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'calibrate_awb' }) })
            .then((r) => r.json()).then((d) => showToast(d.success ? 'Calibrating white balance' : (d.error || 'Could not calibrate'), d.success ? 'info' : 'error'))
            .catch(() => showToast('Could not reach the OWLs', 'error'));
    }
    async function setCamera(params, restart) {
        if (restart) {
            const ok = await UI().confirm({ title: 'Change on all OWLs?', body: 'This applies after the OWLs restart, about 20 seconds.', ok: 'Change' });
            if (!ok) return;
        }
        camValues = Object.assign(camValues || {}, params);
        renderCamera();
        fetch('/api/collect/camera', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ params: params }) })
            .then((r) => r.json()).then((d) => {
                if (!d.success) throw new Error(d.error || 'Could not save');
                showToast(restart ? 'Saved. Restart the OWLs to apply it.' : 'Saved', 'success');
            }).catch((e) => { showToast(e.message, 'error'); loadCamera(); });
    }
    function renderCameraNotice() {
        const need = Object.keys(owlsData).some((id) => owlsData[id] && owlsData[id].connected && owlsData[id].restart_required);
        const html = need ? UI().notice('info', '<b>Restart needed.</b> Size and rotation change after the OWLs restart. <button type="button" class="lnk" data-act="restart">Restart OWLs ›</button>') : '';
        const box = $('cameraNotices');
        if (box.dataset.html === html) return;
        box.dataset.html = html;
        box.innerHTML = html;
        const r = box.querySelector('[data-act="restart"]');
        if (r) r.addEventListener('click', () => { sendCommand('all', 'restart_service'); showToast('Restarting OWLs', 'info'); });
    }

    // ---------- data collection (/api/collect/settings) ----------
    function initCapture() {
        if (!window.CollectForm) return;
        dcForm = CollectForm.mountCapture($('dcCaptureForm'), {
            sampleModes: ['distance', 'time'], gain: true, steppers: true,
            onChange: (key, value) => {
                const capture = {};
                capture[key] = value;
                fetch('/api/collect/settings', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ capture: capture }),
                }).then((r) => r.json()).then((d) => {
                    if (!d.success) throw new Error(d.error || 'Could not save');
                    _collectConfig.capture = d.capture;
                    dcForm.render(d.capture);
                    showToast('Saved on all OWLs', 'success');
                }).catch((e) => { showToast(e.message, 'error'); dcForm.render(_collectConfig.capture); });
            },
        });
    }
    function onCollectConfig(data) {
        if (dcForm) dcForm.render(data.capture);
        if (window.CollectI18n) CollectI18n.apply($('panel-collection'));
    }

    // ---------- test nozzles ----------
    function initNozzles() {
        let restore = [];
        UI().hold($('holdSpray'), {
            maxMs: 10000,
            start: () => {
                restore = scopedOwls().filter((o) => o.detection_enable && o.detection_mode !== 2).map((o) => o._id);
                sendCommand(currentScope(), 'toggle_all_nozzles', true);
            },
            stop: () => {
                sendCommand(currentScope(), 'toggle_all_nozzles', false).then(() => {
                    restore.forEach((id) => sendCommand(id, 'toggle_detection', true));
                });
            },
        });
        // One nozzle at a time, or each in turn (the OWL stops each test itself)
        nozzleTester = UI().nozzleTest($('nozzleTest'), {
            owls: () => scopedOwls().sort((a, b) => a._id.localeCompare(b._id)).map((o) => ({
                id: o._id, name: o.friendly_name || o._id,
                relays: typeof o.relay_num === 'number' && o.relay_num > 0 ? o.relay_num : null,
                busy: !!o.detection_enable || o.detection_mode === 2,
            })),
            send: (id, relays, ms) => sendCommand(id, 'test_nozzles', { relays: relays, duration_ms: ms }),
            stopSpraying: (ids) => ids.forEach((id) => {
                const o = owlsData[id] || {};
                if (o.detection_mode === 2) sendCommand(id, 'toggle_all_nozzles', false);
                else if (o.detection_enable) sendCommand(id, 'toggle_detection', false);
            }),
        });
    }

    // ---------- data: sessions on the OWLs ----------
    function pollCollect() {
        fetch('/api/collect/state').then((r) => r.json()).then((data) => { collectState = data; renderSessions(); }).catch(() => {});
    }
    function sessionStatus(owl, s) {
        const req = ((collectState || {}).uploads || []).find((u) => u.local_id === owl.id && u.session_id === s.session_id);
        const tr = owl.data_transfer || {};
        if (tr.session_date === s.session_id && tr.status) {
            if (tr.status === 'complete') return ['ok', 'Uploaded'];
            if (tr.status === 'error') return ['bad', 'Upload failed'];
            if (tr.status !== 'idle') return ['c', 'Uploading ' + Math.max(0, Math.min(100, Math.round(Number(tr.progress) || 0))) + '%'];
        }
        if (req) {
            if (req.status === 'rejected') return ['bad', 'Not accepted'];
            if (req.status === 'accepted' || req.status === 'queued') return ['c', 'Queued in Noktura'];
            return ['c', 'Requested'];
        }
        return ['', 'On OWL'];
    }
    function renderSessions() {
        const U = UI();
        const st = collectState;
        $('dataScope').textContent = _scope === 'all' ? 'All OWLs' : owlName(_scope);
        const notes = [];
        if (st && !(st.cloud || {}).configured) notes.push(U.notice('info', '<b>Noktura is not connected.</b> Recordings stay on the OWLs until you copy them to a laptop.'));
        const nh = notes.join('');
        if ($('dataNotices').dataset.html !== nh) { $('dataNotices').dataset.html = nh; $('dataNotices').innerHTML = nh; }
        const list = $('sessionsList');
        if (!st) return;
        const rows = [];
        (st.owls || []).forEach((owl) => {
            if (_scope !== 'all' && owl.id !== _scope) return;
            (owl.data_sessions || []).forEach((s) => rows.push([owl, s]));
        });
        rows.sort((a, b) => String(b[1].session_id).localeCompare(String(a[1].session_id)));
        let html;
        if (!rows.length) {
            html = '<p class="ou-hint">No recordings on ' + U.esc(_scope === 'all' ? 'the OWLs' : owlName(_scope)) + ' yet. Tap Refresh after recording.</p>';
        } else {
            html = rows.slice(0, 60).map(([owl, s]) => {
                const status = sessionStatus(owl, s);
                const canAct = status[0] === '' || status[0] === 'bad' || status[0] === 'ok';
                const what = [s.image_count !== undefined ? s.image_count + ' photos' : null, typeof s.total_size === 'number' ? U.fmtBytes(s.total_size) : null];
                return '<div class="ou-sess"><div class="ou-sess-row"><span class="ou-thumb">' + U.ICONS.rec + '</span><div style="flex:1;min-width:0"><div class="when">' +
                    U.esc(U.sessionWhen(String(s.date || ''), String(s.time || ''))) + '</div><div class="what">' + U.joinSep([owlName(owl.id)].concat(what)) +
                    '</div></div><span class="ou-run ' + (status[0] || 'c') + '">' + U.esc(status[1]) + '</span></div>' +
                    (canAct ? '<div class="ou-sess-actions"><button type="button" class="ou-btn slim" data-up="' + U.esc(owl.id + '|' + s.session_id) + '">' + U.ICONS.upload +
                        'Upload to Noktura</button><button type="button" class="ou-iconbtn" aria-label="Delete" data-del="' + U.esc(owl.id + '|' + s.session_id) + '">' + U.ICONS.bin + '</button></div>' : '') + '</div>';
            }).join('');
        }
        if (list.dataset.html === html) return;
        list.dataset.html = html;
        list.innerHTML = html;
        list.querySelectorAll('[data-up]').forEach((b) => b.addEventListener('click', () => sessionAction('upload', b.dataset.up)));
        list.querySelectorAll('[data-del]').forEach((b) => b.addEventListener('click', () => sessionAction('delete', b.dataset.del)));
    }
    async function sessionAction(kind, key) {
        const i = key.indexOf('|');
        const item = { owl: key.slice(0, i), session_id: key.slice(i + 1) };
        if (kind === 'delete') {
            const ok = await UI().confirm({ title: 'Delete this recording?', body: 'The photos are removed from ' + owlName(item.owl) + '. Upload or copy them first if you need them.', ok: 'Delete', okClass: 'stop' });
            if (!ok) return;
        } else if (!((collectState || {}).cloud || {}).configured) {
            showToast('Connect to Noktura first (Settings, Noktura)', 'warning');
            return;
        }
        fetch(kind === 'delete' ? '/api/collect/sessions/delete' : '/api/collect/upload', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items: [item] }),
        }).then((r) => r.json()).then((d) => {
            if (!d.success) throw new Error(d.error || 'It did not go through');
            if (kind === 'delete' && (d.skipped || []).length) showToast('Not deleted: ' + d.skipped[0].reason.replace(/_/g, ' '), 'warning');
            else if (kind === 'upload' && !(d.requested || []).length) showToast('Already requested. Wait for Noktura to answer.', 'warning');
            else showToast(kind === 'delete' ? 'Deleted' : 'Upload requested', 'success');
            setTimeout(pollCollect, 800);
        }).catch((e) => showToast(e.message, 'error'));
    }

    return {
        init: init, update: update, render: render, scopeOwl: scopeOwl, openSettings: openSettings,
        modeRefused: modeRefused, onCollectConfig: onCollectConfig, setScope: setScope,
    };
})();
