/* ==========================================================================
   OWL Dashboard (standalone) - redesigned shell (2026-10-12)
   Dashboard | Data | Settings, built from /shared/js/owl_ui.js. The live
   view is a pop-up (Live view on Dashboard); the camera stream only runs
   while it is open. The older
   modules still own their jobs (_controls: switches and modes, _config_tab:
   sliders, _config: profiles and the INI editor, _data_collection: the
   shared record form); this file draws the new screens around them and is
   fed every stats poll through Shell.update(data).
   ========================================================================== */

const Shell = (function () {
    'use strict';

    const $ = (id) => document.getElementById(id);
    const UI = () => window.OwlUI;
    let nav = null;
    let settings = null;
    let last = {};
    let streamOn = false;
    let aiPicked = null;          // local class selection while it is being sent
    let aiPickedAt = 0;
    let aiModelPendingUntil = 0;
    let blockedPick = null;       // AI or Hybrid tapped on a unit that can't run it
    let camValues = null;
    let evStep = null;
    let sessionsLoaded = false;
    let diskKnown = false;
    let livePop = null;
    let nozzleTester = null;
    let trackPersistStep = null;
    let trackBufferStep = null;

    // ---------- init ----------
    function init() {
        UI().fillIcons(document);
        nav = UI().initNav($('mainNav'), onView);
        settings = UI().initSettings($('view-settings'), onPanel);
        $('powerBtn').addEventListener('click', powerSheet);

        // Detect with: check what the OWL can run before asking it
        document.querySelectorAll('#pipeline-mode-selector .mode-btn').forEach((b) =>
            b.addEventListener('click', () => pickMode(b.dataset.mode)));

        livePop = UI().livePop($('livePop'), {
            onOpen: () => setStream(true),
            onClose: () => {
                if (typeof standaloneGeometryActive !== 'undefined' && standaloneGeometryActive) GeometryEditor.close();
                setStream(false);
            },
        });
        $('liveOpen').addEventListener('click', () => livePop.open());
        $('camGeometry').addEventListener('click', () => {
            if (typeof toggleStandaloneGeometry === 'function') toggleStandaloneGeometry();
        });
        initCamera();
        initNozzles();
        initTracking();
        initMaintenance();
        initData();
        document.addEventListener('visibilitychange', () => setStream(!document.hidden && livePop.isOpen()));
    }

    function isView(name) {
        const v = $('view-' + name);
        return !!(v && v.classList.contains('is-on'));
    }

    function onView(name) {
        livePop.close();
        if (name === 'data') loadSessions();
    }

    function onPanel(name) {
        if (name === 'profiles' && typeof loadConfig === 'function'
                && typeof currentConfig !== 'undefined' && Object.keys(currentConfig).length === 0) {
            loadConfig();
        }
        if (name === 'camera') loadCamera();
        if (name === 'maint' && typeof loadStorageData === 'function') loadStorageData();
        if (name === 'ai') renderAI(last);
        if (name === 'nozzles') nozzleTester.render();
    }

    function openSettings(panel) {
        nav.show('settings');
        settings.open(panel, true);
    }

    // ---------- live view (pop-up) ----------
    // The MJPEG stream runs only while the pop-up is open: encoding it costs
    // the OWL CPU it would otherwise spend detecting.
    function setStream(on) {
        const img = $('stream-img');
        if (!img || on === streamOn) return;
        streamOn = on;
        if (on) {
            img.onerror = () => { $('stream-status-overlay').hidden = false; img.hidden = true; };
            img.onload = () => { $('stream-status-overlay').hidden = true; img.hidden = false; };
            img.hidden = false;
            img.src = '/video_feed?t=' + Date.now();
        } else if (typeof standaloneGeometryActive === 'undefined' || !standaloneGeometryActive) {
            img.removeAttribute('src');
        }
    }

    // ---------- detect with ----------
    // Why AI and Hybrid can't run here, or '' (amber dot + notice)
    function aiBlockReason(d) {
        if (!UI().canRunAI(d.ai)) return 'no_ai';
        if (UI().aiReported(d.ai) && d.model_available === false) return 'no_model';
        return '';
    }

    function pickMode(mode) {
        if ((mode === 'gog' || mode === 'hybrid') && aiBlockReason(last)) {
            blockedPick = mode;
            renderDetectNotice();
            return;
        }
        blockedPick = null;
        renderDetectNotice();
        if (typeof setPipelineMode === 'function') setPipelineMode(mode);
    }

    function renderDetectNotice() {
        const box = $('detectNotice');
        const reason = aiBlockReason(last);
        if (typeof updateModeAvailability === 'function') updateModeAvailability(!reason);
        if (!reason) blockedPick = null;
        document.querySelectorAll('#pipeline-mode-selector .mode-btn').forEach((b) =>
            b.classList.toggle('picked', b.dataset.mode === blockedPick));
        const key = blockedPick ? blockedPick + reason : '';
        if (box.dataset.key === key) return;
        box.dataset.key = key;
        if (!key) { box.innerHTML = ''; return; }
        box.innerHTML = reason === 'no_ai'
            ? UI().notice('warn', '<b>AI isn\'t installed on this OWL.</b> Colour detection stays on. ' +
                '<button type="button" class="lnk" data-go="ai">Install AI ›</button>')
            : UI().notice('warn', '<b>No AI model on this OWL yet.</b> Colour detection stays on. ' +
                '<button type="button" class="lnk" data-go="ai">AI models ›</button>');
        box.querySelector('[data-go]').addEventListener('click', () => { blockedPick = null; renderDetectNotice(); openSettings('ai'); });
    }

    // ---------- power ----------
    async function powerSheet() {
        const running = !!last.owl_running;
        const choice = await UI().choose({
            title: 'Power',
            choices: [
                { label: 'Shut down', hint: 'Turns the OWL off safely', value: 'shutdown', danger: true },
                { label: 'Restart', hint: 'Restarts the whole OWL, about a minute', value: 'reboot' },
                running ? { label: 'Stop detection', hint: 'Stops the OWL program, the screen stays on', value: 'stop' }
                        : { label: 'Start detection', hint: 'Starts the OWL program', value: 'start' },
            ],
        });
        if (!choice) return;
        if (choice === 'stop') { stopOwl().then(updateSystemStats).catch(() => {}); return; }
        if (choice === 'start') { startOwl().then(updateSystemStats).catch(() => {}); return; }
        const shut = choice === 'shutdown';
        const ok = await UI().confirm({
            title: shut ? 'Shut down this OWL?' : 'Restart this OWL?',
            body: shut ? 'Recording stops first. Wait 30 seconds after the lights go out before you switch off the power.'
                       : 'Spraying and recording stop until it is back, about a minute.',
            ok: shut ? 'Shut down' : 'Restart', okClass: shut ? 'stop' : 'navy', countdown: shut ? 3 : 0,
        });
        if (!ok) return;
        try {
            const res = await UI().guardedFetch('/api/system/' + choice, { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.success) throw new Error(data.error || 'The OWL did not accept it');
            showNotification('Power', shut ? 'Shutting down' : 'Restarting', 'info', 8000);
        } catch (e) {
            showNotification('Power', e.message, 'error');
        }
    }

    // 0 means the board gave no reading (not a real 0 degrees)
    function temp(d) {
        return typeof d.cpu_temp === 'number' && d.cpu_temp > 0 ? Math.round(d.cpu_temp) + ' °C' : null;
    }

    // ---------- per-poll update ----------
    function update(data) {
        last = data || {};
        const d = last;
        const mode = UI().algoMode(d.algorithm);
        const detecting = !!(d.detection_running ?? d.detection_enable);
        const recording = !!(d.recording ?? d.image_sample_enable);
        const blanket = d.detection_mode === 2;

        // Top bar
        $('unitName').textContent = d.hostname ? d.hostname.toUpperCase().replace(/^OWL(\d)/, 'OWL-$1') : 'OWL';
        let cfg = (d.config_source || d.config_name || '').replace(/\.ini$/, '').replace(/_\d{8}_\d{6}$/, '');
        if (cfg && d.config_unsaved) cfg += ' (unsaved)';
        $('activeConfigName').textContent = cfg;
        $('colourProfileName').textContent = cfg;
        let state = ['idle', 'Ready'];
        if (!d.owl_running) state = ['idle', 'Stopped'];
        else if (d.fault) state = ['bad', 'Fault'];
        else if (blanket) state = ['', 'Blanket spray'];
        else if (detecting) state = ['', 'Spraying'];
        else if (recording) state = ['rec', 'Recording'];
        $('stateDot').className = 'ou-dot ' + state[0];
        $('stateText').textContent = state[1];

        // Prime switches
        UI().setSwitch($('detectSwitch'), detecting && !blanket, { go: detecting && !blanket });
        $('sprayHint').innerHTML = detecting && !blanket
            ? UI().joinSep([UI().MODE_LABELS[mode], typeof d.relay_num === 'number' ? d.relay_num + ' nozzles' : null])
            : UI().esc(blanket ? 'Off while blanket spray is on' : 'Off');
        UI().setSwitch($('recordSwitch'), recording, { rec: true });
        renderRecordHint(d, recording);
        UI().setSwitch($('nozzleSwitch'), blanket, { go: blanket });
        $('blanketHint').textContent = blanket ? 'Every nozzle is spraying' : 'Every nozzle on, detection off';
        UI().setSwitch($('trackingSwitch'), !!d.tracking_enabled);
        $('liveMode').textContent = UI().MODE_LABELS[mode];
        $('liveModeChip').textContent = UI().MODE_LABELS[mode];

        renderNotices(d);
        renderDetectNotice();
        renderTracking(d);

        // Stat cells: speed and frame rate always on the Dashboard
        const health = !d.owl_running ? ['warn', 'Stopped'] : d.fault ? ['bad', 'Fault'] : ['ok', 'Healthy'];
        const free = typeof d.storage_free_mb === 'number' ? UI().fmtGB(d.storage_free_mb) : null;
        const fps = typeof d.avg_loop_time_ms === 'number' && d.avg_loop_time_ms > 0 ? Math.round(1000 / d.avg_loop_time_ms) + ' fps' : null;
        UI().statCells($('fieldStats'), [
            { label: 'Status', value: health[1], cls: health[0] },
            { label: 'Speed', value: typeof d.speed_kmh === 'number' ? d.speed_kmh.toFixed(1) + ' km/h' : 'No GPS' },
            { label: 'Frame rate', value: fps },
            { label: 'Free', value: free },
        ]);
        UI().statCells($('timingStats'), [
            { label: 'Speed', value: typeof d.speed_kmh === 'number' ? d.speed_kmh.toFixed(1) + ' km/h' : null },
            { label: 'Timing', value: { 'device-gps': 'OWL GPS', controller: 'Controller', config: 'Fixed' }[d.actuation_source] || null },
            { label: 'Processing', value: typeof d.latency_ms === 'number' ? Math.round(d.latency_ms) + ' ms' : null },
        ]);
        const fan = d.fan_status || {};
        UI().statCells($('maintStats'), [
            { label: 'CPU load', value: typeof d.cpu_percent === 'number' ? Math.round(d.cpu_percent) + '%' : null },
            { label: 'Memory', value: typeof d.memory_percent === 'number' ? Math.round(d.memory_percent) + '%' : null },
            { label: 'CPU temp', value: temp(d) },
            { label: 'Frame', value: typeof d.avg_loop_time_ms === 'number' && d.avg_loop_time_ms > 0 ? Math.round(d.avg_loop_time_ms) + ' ms' : null },
            { label: 'Fan', value: fan.is_rpi5 && typeof fan.rpm === 'number' ? fan.rpm + ' rpm' : null },
        ]);
        $('versionText').textContent = d.version ? 'Version ' + d.version : '';

        // Side list badges: problems show on the row itself
        const aiBadge = !UI().aiReported(d.ai) ? null : UI().canRunAI(d.ai) ? null : 'Not installed';
        UI().setBadge($('view-settings'), 'ai', aiBadge);
        const cloudCls = typeof cloudStatusClass === 'function' ? cloudStatusClass(d) : 'neutral';
        const cloudText = { neutral: 'Not linked', connected: 'Connected', disconnected: 'Offline', connecting: 'Connecting' }[cloudCls];
        UI().setBadge($('view-settings'), 'noktura', cloudCls === 'disconnected' ? 'Offline' : null);
        $('cloudStatusText').textContent = cloudText || '';
        $('dcSaved').textContent = cfg ? 'Saved to ' + cfg : '';
        $('aiPanelSub').textContent = UI().aiSummary(d.ai).title;

        if (isView('settings') && settings.current() && settings.current().panel === 'ai') renderAI(d);
        if (isView('settings') && settings.current() && settings.current().panel === 'nozzles') nozzleTester.render();
        renderStorage(d);
        renderUpdate(d);
    }

    function renderRecordHint(d, recording) {
        const capture = d.capture || {};
        const hint = $('recordHint');
        const chipsBox = $('sessionChips');
        if (!recording) {
            hint.textContent = 'Tap to choose crop and conditions';
            chipsBox.innerHTML = '';
            return;
        }
        const chips = window.CollectForm && typeof _dcConfig !== 'undefined'
            ? (CollectForm.metadataChips(d.session_metadata, _dcConfig.presets) || []) : [];
        if (capture.paused_slow) {
            hint.textContent = 'Paused below ' + capture.min_speed_kmh + ' km/h';
        } else if (capture.record_mode === 'video') {
            hint.textContent = typeof capture.video_seconds === 'number' ? 'Video ' + Math.round(capture.video_seconds) + ' s' : 'Video';
        } else {
            const n = typeof capture.saved_count === 'number' ? capture.saved_count : null;
            hint.textContent = n === null ? 'Recording' : n + (n === 1 ? ' photo' : ' photos');
        }
        chipsBox.innerHTML = UI().labsHTML(chips.slice(0, 3));
    }

    const FAULT_ACTIONS = {
        nozzles_off: 'Nozzles are off.',
        blanket: 'Blanket spraying while spraying is switched on.',
        spot_spray: 'Spot spraying continues.',
    };
    const BLOCKED = {
        no_drive: 'No USB drive found. Insert a drive and press Record again.',
        storage_full: 'Storage is full. Download or delete recordings, then press Record again.',
        floor_unreachable: 'The free-space floor (min_free_gb) is higher than this disk allows. Lower it under Advanced.',
        storage_error: 'Recording storage error. Check the OWL logs.',
    };
    function renderNotices(d) {
        const out = [];
        const UIx = UI();
        if (d.fault) out.push(UIx.notice('bad', '<b>Fault: ' + UIx.esc(d.fault.message) + '.</b> ' + UIx.esc(FAULT_ACTIONS[d.fault.action] || '')));
        if (d.algorithm_error) out.push(UIx.notice('bad', '<b>Detection could not start.</b> ' + UIx.esc(d.algorithm_error)));
        if (d.speed_warning) out.push(UIx.notice('warn', '<b>Slow down.</b> ' + UIx.esc(d.speed_warning)));
        if (d.recording_blocked_reason && !d.image_sample_enable) {
            out.push(UIx.notice('warn', '<b>Cannot record.</b> ' + UIx.esc(BLOCKED[d.recording_blocked_reason] || d.recording_blocked_reason)));
        }
        if (d.algorithm_pending) out.push(UIx.notice('info', '<b>Switching detection.</b> Loading ' + UIx.esc(UIx.MODE_LABELS[UIx.algoMode(d.algorithm_pending)].toLowerCase()) + '.'));
        if (d.restart_required) {
            out.push(UIx.notice('info', '<b>Restart needed.</b> New camera settings apply after a restart. ' +
                '<button type="button" class="lnk" data-act="restart">Restart detection ›</button>'));
        }
        const html = out.join('');
        const box = $('fieldNotices');
        if (box.dataset.html !== html) {
            box.dataset.html = html;
            box.innerHTML = html;
            const r = box.querySelector('[data-act="restart"]');
            if (r) r.addEventListener('click', restartService);
        }
        const cam = $('cameraNotices');
        const camHtml = d.restart_required
            ? UIx.notice('info', '<b>Restart needed.</b> Size and rotation change after a restart. <button type="button" class="lnk" data-act="restart">Restart detection ›</button>') : '';
        if (cam.dataset.html !== camHtml) {
            cam.dataset.html = camHtml;
            cam.innerHTML = camHtml;
            const r = cam.querySelector('[data-act="restart"]');
            if (r) r.addEventListener('click', restartService);
        }
    }

    function restartService() {
        stopOwl().then(() => setTimeout(() => startOwl().then(updateSystemStats).catch(() => {}), 1500)).catch(() => {});
    }

    // ---------- AI models ----------
    function renderAI(d) {
        if (!d) return;
        const el = $('aiModels');
        if (aiPicked && Date.now() - aiPickedAt > 6000) aiPicked = null;
        const state = Object.assign({}, d);
        if (Date.now() < aiModelPendingUntil && Shell._pendingModel) state.current_model = Shell._pendingModel;
        UI().renderAIModels(el, state, {
            setModel: setModel,
            toggleClass: (name, all) => {
                aiPicked = UI().toggleClass(aiPicked || d.detect_classes || [], name, all);
                aiPickedAt = Date.now();
                renderAI(last);
                sendClasses();
            },
            installAI: installAI,
        }, { pickedClasses: aiPicked, update: d.software_update || lastUpdate });
    }

    let classTimer = null;
    function sendClasses() {
        clearTimeout(classTimer);
        classTimer = setTimeout(() => {
            apiRequest('/api/ai/set_detect_classes', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ classes: aiPicked || [] }),
            }).then((r) => r.json()).then((res) => {
                if (!res.success) throw new Error(res.error || 'Could not save');
                aiPickedAt = Date.now();
            }).catch((e) => showNotification('AI models', e.message, 'error'));
        }, 600);
    }

    function setModel(name) {
        Shell._pendingModel = name;
        aiModelPendingUntil = Date.now() + 8000;
        renderAI(last);
        apiRequest('/api/ai/set_model', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: name }),
        }).then((r) => r.json()).then((res) => {
            if (!res.success) throw new Error(res.error || 'Could not switch model');
            showNotification('AI models', 'Loading ' + name, 'info', 2500);
        }).catch((e) => { aiModelPendingUntil = 0; showNotification('AI models', e.message, 'error'); renderAI(last); });
    }

    async function installAI() {
        const ok = await UI().confirm({
            title: 'Install AI on this OWL?',
            body: 'Downloads about 2 GB and takes 20 minutes or more. Use Wi-Fi or a phone hotspot. Spraying keeps working on colour detection.',
            ok: 'Install AI',
        });
        if (!ok) return;
        try {
            const res = await UI().guardedFetch('/api/ai/install', { method: 'POST' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.success) throw new Error(data.error || 'Could not start the install');
            lastUpdate = { status: 'starting' };
            pollUpdate();
            renderAI(last);
        } catch (e) {
            showNotification('Install AI', e.message, 'error');
        }
    }

    // ---------- software update (Maintenance) ----------
    let lastUpdate = null;
    let updateTimer = null;
    function pollUpdate() {
        clearTimeout(updateTimer);
        fetch('/api/settings/update/status').then((r) => r.json()).then((data) => {
            lastUpdate = data.update || null;
            renderUpdate(last);
            if (lastUpdate && ['idle', 'complete', 'rolled_back', 'error'].indexOf(lastUpdate.status) === -1) {
                updateTimer = setTimeout(pollUpdate, 4000);
            }
        }).catch(() => {});
    }
    function renderUpdate(d) {
        const u = lastUpdate || {};
        const running = u.status && ['idle', 'complete', 'rolled_back', 'error'].indexOf(u.status) === -1;
        $('updateBtn').disabled = !!running;
        $('updateMeta').textContent = running ? 'Updating. The OWL restarts when it is done.'
            : u.status === 'complete' ? 'Updated' + (u.to ? ' to ' + u.to.slice(0, 7) : '')
            : u.status === 'rolled_back' || u.status === 'error' ? 'Did not finish: ' + (u.error || 'rolled back')
            : (d && d.git_branch ? 'Branch ' + d.git_branch + '. Needs internet.' : 'Needs internet');
    }

    // ---------- camera ----------
    function initCamera() {
        $('camAwb').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
            if (b.dataset.value === 'manual') { calibrate(); return; }
            setCamera({ awb_mode: b.dataset.value });
        }));
        $('camCalibrate').addEventListener('click', calibrate);
        $('camRes').innerHTML = UI().RESOLUTIONS.map((r) => '<button type="button" data-value="' + r + '">' + r.replace('x', '×') + '</button>').join('');
        $('camRes').querySelectorAll('button').forEach((b) => b.addEventListener('click', async () => {
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
    function calibrate() {
        if (typeof calibrateWhiteBalance === 'function') calibrateWhiteBalance();
    }
    function loadCamera() {
        fetch('/api/config').then((r) => r.json()).then((data) => {
            if (!data.success) return;
            camValues = Object.assign({}, (data.config || {}).Camera || {});
            renderCamera();
        }).catch(() => {});
    }
    function renderCamera() {
        const c = camValues || {};
        const live = (last.camera || {});
        const awb = String(live.awb_mode || c.awb_mode || 'auto').toLowerCase();
        $('camAwb').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.value === awb));
        const ev = live.exp_compensation !== undefined ? live.exp_compensation : c.exp_compensation;
        evStep.set(ev === undefined || ev === '' ? 0 : parseInt(ev, 10) || 0);
        const res = UI().resolutionKey(c.resolution_width, c.resolution_height);
        $('camRes').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.value === res));
        $('camResNow').textContent = res && UI().RESOLUTIONS.indexOf(res) === -1 ? 'Now ' + res.replace('x', '×') : '';
        const rot = String(c.rotation || 'auto').toLowerCase();
        $('camRot').querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.value === rot));
        $('cameraSub').textContent = last.resolution_width ? 'Running ' + last.resolution_width + '×' + last.resolution_height : '';
    }
    async function setCamera(params, restart) {
        if (restart) {
            const ok = await UI().confirm({ title: 'Change and restart?', body: 'This setting applies after the OWL program restarts, about 20 s.', ok: 'Change' });
            if (!ok) return;
        }
        Object.assign(camValues || (camValues = {}), params);
        renderCamera();
        apiRequest('/api/config/section', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ section: 'Camera', params: params }),
        }).then((r) => r.json()).then((res) => {
            if (!res.success) throw new Error(res.error || 'Could not save');
            showNotification('Camera', restart ? 'Saved. Restarting detection' : 'Saved', 'success', 2000);
            if (restart) restartService();
        }).catch((e) => { showNotification('Camera', e.message, 'error'); loadCamera(); });
    }

    // ---------- test nozzles ----------
    function initNozzles() {
        let wasDetecting = false;
        UI().hold($('holdSpray'), {
            maxMs: 10000,
            start: () => {
                wasDetecting = !!last.detection_enable && last.detection_mode !== 2;
                apiRequest('/api/nozzles/all-on', { method: 'POST' }).catch(() => {});
            },
            stop: () => {
                fetch('/api/nozzles/all-off', { method: 'POST' })
                    .then(() => { if (wasDetecting) return fetch('/api/detection/start', { method: 'POST' }); })
                    .then(() => updateSystemStats()).catch(() => {});
            },
        });
        // One nozzle at a time, or each in turn (the OWL stops each test itself)
        nozzleTester = UI().nozzleTest($('nozzleTest'), {
            owls: () => (last.owl_running ? [{
                id: 'owl', name: 'Nozzles',
                relays: typeof last.relay_num === 'number' && last.relay_num > 0 ? last.relay_num : null,
                busy: !!last.detection_enable || last.detection_mode === 2,
            }] : []),
            send: (id, relays, ms) => apiRequest('/api/nozzles/test', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ relays: relays, duration_ms: ms }),
            }).catch(() => {}),
            stopSpraying: () => {
                const fn = last.detection_mode === 2 ? stopAllNozzles : stopDetection;
                fn().then(() => updateSystemStats()).catch(() => {});
            },
        });
    }

    // ---------- tracking (AI models) ----------
    function initTracking() {
        document.querySelectorAll('#track-stability-selector [data-stability]').forEach((b) => b.addEventListener('click', () => {
            setTrackParams(UI().TRACK_PRESETS[b.dataset.stability], 'Track stability ' + b.textContent.toLowerCase());
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
    function renderTracking(d) {
        const p = d.track_params || {};
        const has = Object.keys(p).length > 0;
        const on = !!d.tracking_enabled;
        const preset = has ? UI().trackPreset(p) : null;
        document.querySelectorAll('#track-stability-selector [data-stability]').forEach((b) => {
            b.classList.toggle('on', b.dataset.stability === preset);
            b.disabled = !on;
        });
        $('trackCustom').textContent = has && !preset ? '(custom)' : '';
        trackPersistStep.set(typeof p.detection_persist_frames === 'number' ? p.detection_persist_frames : null);
        trackBufferStep.set(typeof p.track_buffer === 'number' ? p.track_buffer : null);
        const why = !has ? 'Update this OWL to change these' : !on ? 'Turn Track weeds on to change these' : '';
        trackPersistStep.disable(!!why);
        trackBufferStep.disable(!!why);
        $('trackWhy').textContent = why;
        $('trackWhy').hidden = !why;
    }

    // ---------- maintenance ----------
    function initMaintenance() {
        $('openMessages').addEventListener('click', () => { if (typeof showNotificationPanel === 'function') showNotificationPanel(); });
        $('restartService').addEventListener('click', async () => {
            if (await UI().confirm({ title: 'Restart detection?', body: 'Spraying stops for about 20 seconds.', ok: 'Restart' })) restartService();
        });
        $('updateBtn').addEventListener('click', async () => {
            const ok = await UI().confirm({ title: 'Update the OWL software?', body: 'Needs internet. Spraying stops while it installs and the OWL restarts. A failed update rolls back.', ok: 'Update' });
            if (!ok) return;
            try {
                const res = await UI().guardedFetch('/api/settings/update/start', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data.success) throw new Error(data.error || 'Could not start the update');
                lastUpdate = { status: 'starting' };
                renderUpdate(last);
                pollUpdate();
            } catch (e) { showNotification('Update', e.message, 'error'); }
        });
        pollUpdate();
    }

    // ---------- data ----------
    function initData() {
        $('sessionsRefresh').addEventListener('click', loadSessions);
    }

    function renderStorage(d) {
        const free = typeof d.storage_free_mb === 'number' ? d.storage_free_mb : null;
        // The recordings folder's disk (loadSessions) wins over the OWL's estimate
        if (free !== null && !diskKnown) $('storageFree').textContent = UI().fmtGB(free) + ' free';
        const where = d.recording_location || d.storage_location;
        $('storageWhere').textContent = where ? 'Recording to ' + ({ internal: 'internal storage', usb: 'USB drive', auto: 'internal storage' }[where] || where) : '';
        const warn = d.storage_warning === 'full' ? UI().notice('bad', '<b>Storage full.</b> Download or delete recordings to keep recording.')
            : d.storage_warning === 'low' ? UI().notice('warn', '<b>Storage low.</b> Download or delete recordings soon.') : '';
        if ($('dataNotices').dataset.html !== warn) { $('dataNotices').dataset.html = warn; $('dataNotices').innerHTML = warn; }
    }

    function loadSessions() {
        const list = $('sessionsList');
        fetch('/api/downloads/sessions').then((r) => r.json()).then((data) => {
            const st = data.storage;
            if (st && st.total_mb) {
                diskKnown = true;
                $('storageFree').textContent = UI().fmtGB(st.free_mb) + ' free';
                $('storageUsed').textContent = UI().fmtGB(st.used_mb) + ' of ' + UI().fmtGB(st.total_mb) + ' used';
                $('storageMeter').style.width = Math.round(st.used_mb / st.total_mb * 100) + '%';
            }
            const sessions = data.sessions || [];
            if (!sessions.length) {
                list.innerHTML = '<p class="ou-hint">' + UI().esc(data.error ? 'No recordings folder yet. Start a recording first.' : 'No recordings yet.') + '</p>';
                return;
            }
            list.innerHTML = sessions.map(sessionHTML).join('');
            list.querySelectorAll('[data-details]').forEach((b) => b.addEventListener('click', () => editDetails(b.dataset.details)));
            list.querySelectorAll('[data-delete]').forEach((b) => b.addEventListener('click', () => deleteSession(b.dataset.delete)));
            sessions.slice(0, 30).forEach(loadSessionLabels);
            sessionsLoaded = true;
        }).catch(() => { list.innerHTML = '<p class="ou-hint">Could not read the recordings.</p>'; });
    }

    function sessionHTML(s) {
        const U = UI();
        const id = U.esc(s.session_id);
        const what = [s.image_count + (s.image_count === 1 ? ' photo' : ' photos'),
            s.video_count ? s.video_count + (s.video_count === 1 ? ' video' : ' videos') : null, U.fmtBytes(s.total_size)];
        return '<div class="ou-sess" data-sess="' + id + '"><div class="ou-sess-row"><span class="ou-thumb">' + U.ICONS.rec +
            '</span><div style="flex:1;min-width:0"><div class="when">' + U.esc(U.sessionWhen(s.date, s.time)) +
            '</div><div class="what">' + U.joinSep(what) + '</div></div></div><div class="ou-labs" data-labels></div>' +
            '<div class="ou-sess-actions"><a class="ou-btn slim" href="/api/downloads/session/' + U.esc(encodeURI(s.session_id)) + '" download>' +
            U.ICONS.down + 'Download</a><button type="button" class="ou-btn ghost slim" data-details="' + id + '">' + U.ICONS.tag +
            'Details</button><button type="button" class="ou-iconbtn" aria-label="Delete" data-delete="' + id + '">' + U.ICONS.bin + '</button></div></div>';
    }

    const sessionMeta = {};
    function loadSessionLabels(s) {
        fetch('/api/downloads/session/' + encodeURI(s.session_id) + '/metadata').then((r) => r.json()).then((data) => {
            sessionMeta[s.session_id] = data.metadata || {};
            drawLabels(s.session_id);
        }).catch(() => {});
    }
    function drawLabels(id) {
        const box = document.querySelector('[data-sess="' + CSS.escape(id) + '"] [data-labels]');
        if (!box) return;
        const md = sessionMeta[id] || {};
        const chips = window.CollectForm ? CollectForm.metadataChips(md, (typeof _dcConfig !== 'undefined' ? _dcConfig.presets : {})) : null;
        box.innerHTML = chips ? UI().labsHTML(chips) : '<span class="ou-lab add" data-add>+ Add details</span>';
        const add = box.querySelector('[data-add]');
        if (add) add.addEventListener('click', () => editDetails(id));
    }

    function editDetails(id) {
        if (typeof _dcRecordModal === 'undefined' || !_dcRecordModal) return;
        _dcRecordModal.open(sessionMeta[id] || {}, {
            startLabel: 'Save details',
            onStart: (metadata) => {
                fetch('/api/downloads/session/' + encodeURI(id) + '/metadata', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ metadata: metadata }),
                }).then((r) => r.json()).then((res) => {
                    if (!res.success) throw new Error(res.error || 'Could not save');
                    sessionMeta[id] = res.metadata || metadata;
                    drawLabels(id);
                    showNotification('Data', 'Details saved', 'success', 2000);
                }).catch((e) => showNotification('Data', e.message, 'error'));
            },
        });
    }

    async function deleteSession(id) {
        const ok = await UI().confirm({ title: 'Delete this recording?', body: 'The photos are removed from the OWL. Download first if you need a copy.', ok: 'Delete', okClass: 'stop' });
        if (!ok) return;
        try {
            const res = await UI().guardedFetch('/api/downloads/session/' + encodeURI(id), { method: 'DELETE' });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.success) throw new Error(data.error || 'Could not delete');
            loadSessions();
        } catch (e) { showNotification('Data', e.message, 'error'); }
    }

    return {
        init: init, update: update, openSettings: openSettings, showView: (v) => nav.show(v), setStream: setStream,
        openLive: () => livePop.open(), _pendingModel: null,
    };
})();
