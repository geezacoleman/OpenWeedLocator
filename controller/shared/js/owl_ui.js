// ============================================
// OWL UI - shared behaviour of the redesigned standalone dashboard and
// networked controller (styles: /shared/css/owl_ui.css).
// Navigation, settings side list, switches, stat cells, sheets, hold to
// spray, the device-token retry and the AI models page renderer. Pure
// helpers: each dashboard passes in its own API calls (one OWL vs a fleet).
// Copy rules: sentence case, short labels, never ' · ' between items.
// ============================================

(function () {
    'use strict';

    const SVG = (body, extra) => '<svg viewBox="0 0 24 24" aria-hidden="true"' + (extra || '') + '>' + body + '</svg>';
    const STROKE = ' fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"';

    const ICONS = {
        power: SVG('<path d="M12 3.5v8"/><path d="M6.6 7a7.5 7.5 0 1 0 10.8 0"/>', ' fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"'),
        back: SVG('<path d="M15 4L7 12l8 8"/>', ' fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"'),
        chev: SVG('<path d="M9 5l7 7-7 7"/>', ' fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"'),
        warn: SVG('<path d="M12 3.5L2.8 19.5h18.4Z"/><path d="M12 10v4.2"/><circle cx="12" cy="17" r=".6" fill="currentColor"/>', STROKE),
        info: SVG('<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5"/><circle cx="12" cy="8" r=".6" fill="currentColor"/>', STROKE),
        field: SVG('<path d="M4.6 17a8 8 0 1 1 14.8 0"/><path d="M12 13.5 L15.4 9.6"/><circle cx="12" cy="13.5" r="1.6" fill="currentColor" stroke="none"/>', STROKE),
        data: SVG('<path d="M3.5 7.2c0-1 .8-1.7 1.7-1.7h3.9l2 2.1h7.7c1 0 1.7.8 1.7 1.7v7.5c0 1-.8 1.7-1.7 1.7H5.2c-1 0-1.7-.8-1.7-1.7Z"/><path d="M12 10.4v5"/><path d="M9.7 13.2L12 15.5l2.3-2.3"/>', STROKE),
        map: SVG('<path d="M12 20.5s-6.2-5.4-6.2-10.6a6.2 6.2 0 0 1 12.4 0c0 5.2-6.2 10.6-6.2 10.6Z"/><circle cx="12" cy="9.9" r="2.3"/>', STROKE),
        settings: SVG('<path d="M6 4.5v15M12 4.5v15M18 4.5v15"/><rect x="3.9" y="12.4" width="4.2" height="3.4" rx="1.1" fill="currentColor"/><rect x="9.9" y="6.6" width="4.2" height="3.4" rx="1.1" fill="currentColor"/><rect x="15.9" y="10.2" width="4.2" height="3.4" rx="1.1" fill="currentColor"/>', STROKE),
        leaf: SVG('<path d="M5 19c0-8 5-13.5 14-14-.5 9-6 14-14 14Z"/><path d="M5 19l7-7"/>', STROKE),
        ai: SVG('<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9.5 3v3M14.5 3v3M9.5 18v3M14.5 18v3M3 9.5h3M3 14.5h3M18 9.5h3M18 14.5h3"/>', STROKE),
        cam: SVG('<path d="M4 8h3l1.6-2.2h6.8L17 8h3v11H4Z"/><circle cx="12" cy="13.2" r="3.4"/>', STROKE),
        rec: SVG('<rect x="3.5" y="5" width="17" height="14" rx="3"/><circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none"/>', STROKE),
        file: SVG('<path d="M6 3.5h8l4 4V20.5H6Z"/><path d="M14 3.5v4h4"/>', STROKE),
        nozzle: SVG('<path d="M9 4h6v5l-3 3-3-3Z"/><path d="M8 16l-2 4M12 15v5M16 16l2 4"/>', STROKE),
        spray: SVG('<path d="M4 12h3l2-6 3 12 2-6h6"/>', STROKE),
        tool: SVG('<path d="M14.5 6.5a4 4 0 0 0 5 5L13 18a2.1 2.1 0 1 1-3-3l6.5-6.5Z"/><path d="M4 20l5-5"/>', STROKE),
        cloud: SVG('<path d="M7 18.5h10.5a4 4 0 0 0 .4-8 5.5 5.5 0 0 0-10.6-1.3A4.7 4.7 0 0 0 7 18.5Z"/>', STROKE),
        code: SVG('<path d="M8.5 7L3.5 12l5 5M15.5 7l5 5-5 5"/>', STROKE),
        full: SVG('<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>', STROKE),
        down: SVG('<path d="M12 4v11"/><path d="M7.5 11 12 15.5 16.5 11"/><path d="M5 19.5h14"/>', STROKE),
        crop: SVG('<path d="M6 3v13a2 2 0 0 0 2 2h13"/><path d="M3 6h13a2 2 0 0 1 2 2v13"/>', STROKE),
        tag: SVG('<path d="M3.5 12.2V4.5h7.7l9.3 9.3-7.7 7.7Z"/><circle cx="8" cy="9" r="1.4"/>', STROKE),
        bin: SVG('<path d="M4.5 7h15"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 13h9l1-13"/>', STROKE),
        upload: SVG('<path d="M12 15.5v-11"/><path d="M7.5 9 12 4.5 16.5 9"/><path d="M5 19.5h14"/>', STROKE),
    };

    const esc = (s) => String(s === undefined || s === null ? '' : s).replace(/[&<>"']/g,
        (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const SEP = '<i class="ou-sep"></i>';

    function joinSep(parts) {
        return parts.filter((p) => p !== null && p !== undefined && p !== '').map(esc).join(SEP);
    }

    function toast(message, type) {
        if (typeof showNotification === 'function') {
            showNotification(type === 'error' ? 'Error' : 'OWL', message, type || 'info', 3500);
        } else if (typeof showToast === 'function') {
            showToast(message, type || 'info');
        }
    }

    function safeStore(key, value) {
        try {
            if (value === undefined) return window.sessionStorage.getItem(key);
            if (value === null) window.sessionStorage.removeItem(key);
            else window.sessionStorage.setItem(key, value);
        } catch (e) { /* private window or blocked storage */ }
        return null;
    }

    // ---------- views (top bar nav) ----------
    // buttons: [data-view] inside nav; views: #view-<name>.ou-view
    function initNav(nav, onChange) {
        const buttons = Array.from(nav.querySelectorAll('[data-view]'));
        function show(name) {
            buttons.forEach((b) => b.setAttribute('aria-selected', String(b.dataset.view === name)));
            document.querySelectorAll('.ou-view').forEach((v) => v.classList.toggle('is-on', v.id === 'view-' + name));
            if (onChange) onChange(name);
        }
        buttons.forEach((b) => b.addEventListener('click', () => show(b.dataset.view)));
        return { show: show, current: () => (buttons.find((b) => b.getAttribute('aria-selected') === 'true') || {}).dataset };
    }

    // ---------- settings: side list + panel ----------
    // Wide: the list stays, the panel beside it changes. Narrow: the list is
    // a page of rows; a row opens its panel with a Back link.
    function initSettings(root, onOpen) {
        const side = root.querySelector('.ou-side');
        const buttons = Array.from(side.querySelectorAll('[data-panel]'));
        const narrow = () => window.matchMedia('(max-width: 839px)').matches;
        buttons.forEach((b) => {
            if (!b.querySelector('.chev')) b.insertAdjacentHTML('beforeend', '<span class="chev">' + ICONS.chev.replace('<svg', '<svg width="18" height="18"') + '</span>');
        });
        root.querySelectorAll('.ou-panel').forEach((panel) => {
            if (panel.querySelector('.ou-back')) return;
            const back = document.createElement('button');
            back.type = 'button';
            back.className = 'ou-back';
            back.innerHTML = ICONS.back + 'Settings';
            back.addEventListener('click', () => root.classList.remove('has-page'));
            panel.insertBefore(back, panel.firstChild);
        });
        function open(name, fromUser) {
            buttons.forEach((b) => b.setAttribute('aria-selected', String(b.dataset.panel === name)));
            root.querySelectorAll('.ou-panel').forEach((p) => p.classList.toggle('is-on', p.id === 'panel-' + name));
            if (fromUser && narrow()) {
                root.classList.add('has-page');
                window.scrollTo(0, 0);
            }
            if (onOpen) onOpen(name);
        }
        buttons.forEach((b) => b.addEventListener('click', () => open(b.dataset.panel, true)));
        return { open: open, current: () => (buttons.find((b) => b.getAttribute('aria-selected') === 'true') || {}).dataset };
    }

    function setBadge(root, panel, text, cls) {
        const btn = root.querySelector('.ou-side [data-panel="' + panel + '"]');
        if (!btn) return;
        let small = btn.querySelector('small');
        if (!text) { if (small) small.remove(); return; }
        if (!small) {
            small = document.createElement('small');
            btn.insertBefore(small, btn.querySelector('.chev'));
        }
        small.className = cls || '';
        small.textContent = text;
    }

    // ---------- switches, segmented, stat cells ----------
    function setSwitch(card, on, opts) {
        const o = opts || {};
        const sw = card.querySelector('.ou-sw');
        if (sw) {
            sw.classList.toggle('on', !!on);
            sw.classList.toggle('rec', !!o.rec);
        }
        card.setAttribute('aria-pressed', String(!!on));
        if (o.go !== undefined) card.classList.toggle('is-go', !!o.go);
    }

    function statCells(el, cells) {
        el.innerHTML = cells.map((c) => '<div class="' + esc(c.cls || '') + '"><span>' + esc(c.label) +
            '</span><b>' + esc(c.value === null || c.value === undefined || c.value === '' ? '--' : c.value) + '</b></div>').join('');
    }

    function labsHTML(chips, cls) {
        return (chips || []).map((c) => '<span class="ou-lab ' + (cls || '') + '">' + esc(c) + '</span>').join('');
    }

    function notice(kind, html) {
        const icon = kind === 'info' ? ICONS.info : ICONS.warn;
        return '<div class="ou-notice ' + (kind === 'warn' ? '' : kind) + '">' + icon + '<div>' + html + '</div></div>';
    }

    // ---------- formatting ----------
    function fmtGB(mb) {
        if (typeof mb !== 'number' || isNaN(mb)) return null;
        const gb = mb / 1024;
        return (gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)) + ' GB';
    }
    function fmtBytes(bytes) {
        if (typeof bytes !== 'number') return '';
        if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GB';
        if (bytes >= 1048576) return Math.round(bytes / 1048576) + ' MB';
        return Math.max(1, Math.round(bytes / 1024)) + ' KB';
    }
    function relTime(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds) || seconds < 0) return 'Not seen yet';
        if (seconds < 60) return 'Seen just now';
        if (seconds < 3600) return 'Seen ' + Math.round(seconds / 60) + ' min ago';
        if (seconds < 86400) return 'Seen ' + Math.round(seconds / 3600) + ' h ago';
        return 'Seen ' + Math.round(seconds / 86400) + ' days ago';
    }
    function sessionWhen(date, time) {
        // date YYYYMMDD, time HHMMSS (may be empty)
        if (!date || date.length !== 8) return date || '';
        const d = new Date(+date.slice(0, 4), +date.slice(4, 6) - 1, +date.slice(6, 8));
        const today = new Date();
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        const same = d.toDateString() === today.toDateString();
        const day = same ? 'Today' : d.getDate() + ' ' + months[d.getMonth()];
        return time && time.length >= 4 ? day + ', ' + time.slice(0, 2) + ':' + time.slice(2, 4) : day;
    }

    // ---------- detection modes ----------
    const MODE_LABELS = { gob: 'Colour detection', gog: 'AI detection', hybrid: 'Hybrid detection', lut: 'Painted detection' };
    function algoMode(algorithm) {
        if (algorithm === 'gog') return 'gog';
        if (algorithm === 'gog-hybrid') return 'hybrid';
        if (algorithm === 'lut') return 'lut';
        return 'gob';
    }

    // ---------- AI capability (utils/ai_capability.py) ----------
    // Only Hailo-8 is supported. A Hailo-8L or 10H on a DIY build is shown
    // as found but not supported; the AI then runs on the CPU (if installed).
    const CHIP_NAMES = { hailo8: 'Hailo-8', hailo8l: 'Hailo-8L', hailo10h: 'Hailo-10H' };

    function canRunAI(ai) {
        if (!ai || typeof ai !== 'object' || !('accelerator' in ai)) return true;
        return ai.accelerator !== null && ai.accelerator !== undefined;
    }
    function aiReported(ai) { return !!(ai && typeof ai === 'object' && 'accelerator' in ai); }
    function isHailo(ai) { return !!(ai && ai.accelerator === 'hailo8'); }

    // Where this OWL's AI runs, as a badge: {text, cls}
    function aiRunBadge(ai) {
        if (!aiReported(ai)) return { text: 'Checking', cls: 'c' };
        if (isHailo(ai)) return { text: 'Hailo', cls: 'h' };
        if (ai.accelerator === 'cpu') return { text: 'CPU', cls: 'c' };
        return { text: 'Needs AI', cls: 'w' };
    }

    // The capability card at the top of AI models: {icon, title, sub, notice}
    function aiSummary(ai) {
        if (!aiReported(ai)) {
            return { icon: 'grey', title: 'Checking AI', sub: 'Waiting for the OWL to report' };
        }
        const hailo = ai.hailo || {};
        const cpu = !!(ai.cpu && ai.cpu.installed);
        if (isHailo(ai)) return { icon: '', title: 'Hailo-8', sub: 'AI accelerator ready', state: 'hailo' };
        if (hailo.present) {
            const name = CHIP_NAMES[hailo.chip];
            if (hailo.chip && hailo.chip !== 'hailo8') {
                return { icon: 'amber', title: name + ' found', sub: 'Not supported',
                         notice: ['warn', '<b>' + esc(name) + ' found, not supported.</b> ' +
                             (cpu ? 'AI runs on the CPU.' : 'AI isn\'t installed, so detection stays on colour.')],
                         state: cpu ? 'cpu' : 'none' };
            }
            return { icon: 'amber', title: name ? name + ' found' : 'Hailo card found', sub: 'Not set up yet',
                     notice: ['bad', '<b>The Hailo card isn\'t ready.</b> ' +
                         (cpu ? 'AI runs on the CPU until it is set up.' : 'Detection stays on colour until it is set up.')],
                     setup: true, state: cpu ? 'cpu' : 'none' };
        }
        if (cpu) {
            return { icon: 'grey', title: 'CPU only', sub: 'No AI accelerator fitted', state: 'cpu',
                     notice: ['info', '<b>Slower than an AI accelerator.</b> Fine for testing. Drive slower when spraying with AI.'] };
        }
        return { icon: 'amber', title: 'AI isn\'t installed', sub: 'This OWL was set up without AI', state: 'none' };
    }

    // Model rows for one OWL. state = system_stats or one OWL's state.
    // Each row: radio (current model), name, meta, badge.
    function modelRows(state) {
        const s = state || {};
        const models = (s.available_models || []).slice();
        const current = s.current_model || '';
        const dl = s.model_download || {};
        const downloading = dl.status === 'downloading' ? dl.model_name : null;
        if (downloading && models.indexOf(downloading) === -1) models.push(downloading);
        const ai = s.ai;
        const badge = aiRunBadge(ai);
        return models.map((name) => {
            const row = { name: name, current: name === current, meta: '', badge: badge, progress: null };
            if (name === downloading) {
                // progress comes from the OWL's state: force it to a 0-100 number
                const pct = Math.round(Math.max(0, Math.min(100, Number(dl.progress) || 0)));
                row.meta = 'Downloading';
                row.badge = { text: pct + '%', cls: 'c' };
                row.progress = pct;
            } else if (dl.status === 'error' && dl.model_name === name) {
                row.meta = dl.error ? 'File check failed: ' + dl.error : 'File check failed';
                row.badge = { text: 'Failed', cls: 'bad' };
            } else if (name === current) {
                row.meta = 'In use';
            } else {
                row.meta = /\.pt$/i.test(name) ? 'PyTorch model' : 'NCNN model';
            }
            if (!canRunAI(ai)) row.badge = { text: 'Needs AI', cls: 'w' };
            return row;
        });
    }

    // Renders the whole AI models page for ONE OWL into `el`.
    // handlers: {setModel(name), toggleClass(name, on), installAI(), dirty()}
    function renderAIModels(el, state, handlers, opts) {
        const s = state || {};
        const o = opts || {};
        const h = handlers || {};
        const sum = aiSummary(s.ai);
        const aiOn = s.algorithm === 'gog' || s.algorithm === 'gog-hybrid';
        const loop = typeof s.avg_loop_time_ms === 'number' && s.avg_loop_time_ms > 0 ? s.avg_loop_time_ms : null;
        const cells = [
            { label: 'Speed', value: aiOn && loop ? Math.round(1000 / loop) + ' fps' : null },
            { label: 'Per frame', value: aiOn && loop ? Math.round(loop) + ' ms' : null },
            { label: sum.state === 'hailo' ? 'Temp' : 'CPU', value: typeof s.cpu_temp === 'number' && s.cpu_temp > 0 ? Math.round(s.cpu_temp) + ' °C' : null },
        ];
        if (s.model_storage && typeof s.model_storage.used_mb === 'number' && typeof s.model_storage.budget_mb === 'number') {
            cells.push({ label: 'Space used', value: fmtGB(s.model_storage.used_mb) + ' of ' + fmtGB(s.model_storage.budget_mb) });
        }
        const capCells = cells.map((c) => '<div><span>' + esc(c.label) + '</span><b>' + esc(c.value || '--') + '</b></div>').join('');
        const showCells = sum.state !== 'none';

        let left = '<div class="ou-card"><div class="ou-cap-head"><span class="ou-chip-ic ' + sum.icon + '">' + ICONS.ai +
            '</span><div style="flex:1;min-width:0"><div class="t1">' + esc(sum.title) + '</div><div class="t2">' + esc(sum.sub) +
            '</div></div></div>' + (showCells ? '<div class="ou-stats flat">' + capCells + '</div>' : '') + '</div>';
        if (sum.notice) left += notice(sum.notice[0], sum.notice[1]);
        if (sum.setup) {
            left += '<button class="ou-btn navy slim" type="button" disabled>Set up Hailo</button>' +
                '<p class="ou-hint">Hailo set-up from the dashboard comes in a later update.</p>';
        }
        // Install AI: only when the OWL said it has no AI at all
        const update = s.software_update || o.update || {};
        if (aiReported(s.ai) && !canRunAI(s.ai)) {
            const running = !!update.status && ['idle', 'complete', 'rolled_back', 'error'].indexOf(update.status) === -1;
            if (running) {
                left += '<div class="ou-card" style="padding:14px 16px;display:flex;flex-direction:column;gap:8px">' +
                    '<b>Installing AI</b><div class="ou-meter"><i style="width:40%"></i></div>' +
                    '<span class="ou-hint">This takes 20 minutes or more. The OWL restarts when it is done.</span></div>';
            } else if (update.status === 'rolled_back' || update.status === 'error') {
                left += notice('bad', '<b>Install AI did not finish.</b> ' + esc(update.error || 'The OWL went back to the previous version.')) +
                    (h.installAI ? '<button class="ou-btn navy slim" type="button" data-ai="install">Try again</button>' : '');
            } else if (h.installAI) {
                left += '<button class="ou-btn navy slim" type="button" data-ai="install">Install AI</button>' +
                    '<p class="ou-hint">Uses about 2 GB of data. Use Wi-Fi or a phone hotspot, not the router SIM. Spraying keeps working on colour detection.</p>';
            }
        }

        // Classes
        const classes = s.model_classes || {};
        const names = Object.keys(classes).map((k) => classes[k]);
        const picked = o.pickedClasses || s.detect_classes || [];
        let cls = '<span class="ou-list-head">Spray on</span><div class="ou-list"><div class="ou-classes">';
        if (!names.length) {
            cls += '<span class="ou-hint">Load a model to see what it can find.</span>';
        } else {
            names.forEach((n) => {
                const on = !picked.length || picked.indexOf(n) !== -1;
                cls += '<button type="button" class="ou-cls' + (on ? ' on' : '') + '" data-cls="' + esc(n) + '"><i></i>' + esc(n) + '</button>';
            });
        }
        cls += '</div></div>';
        if (names.length) cls += '<p class="ou-hint">' + (picked.length ? picked.length + ' of ' + names.length + ' classes sprayed.' : 'All classes are sprayed.') + '</p>';

        // Models list
        const rows = modelRows(s);
        let list = '<span class="ou-list-head">On this OWL</span><div class="ou-list">';
        if (!rows.length) {
            list += '<div class="ou-row"><div class="main"><span class="name">No models yet</span><span class="meta">' +
                esc(o.noModelsHint || 'Models sent from Noktura or copied to models/ show here.') + '</span></div></div>';
        }
        const hailoCard = !!(s.ai && s.ai.hailo && s.ai.hailo.present);
        rows.forEach((r) => {
            list += '<button type="button" class="ou-row" data-model="' + esc(r.name) + '"><span class="ou-radio' + (r.current ? ' on' : '') +
                '"></span><div class="main"><span class="name">' + esc(r.name) + '</span><span class="meta">' + esc(r.meta) +
                '</span></div><span class="ou-run ' + esc(r.badge.cls) + '">' + esc(r.badge.text) + '</span></button>';
            if (r.progress !== null) list += '<div class="ou-rowx"><div class="ou-meter"><i style="width:' + Math.max(0, Math.min(100, Number(r.progress) || 0)) + '%"></i></div></div>';
            if (r.current && hailoCard) {
                list += '<div class="ou-rowx"><span>Run on</span><div class="ou-mini"><button type="button" disabled class="' +
                    (isHailo(s.ai) ? 'on' : '') + '">Hailo</button><button type="button" disabled class="' + (isHailo(s.ai) ? '' : 'on') +
                    '">CPU</button></div></div><div class="ou-rowx"><span style="font-weight:500">Choosing per model comes in a later update.</span></div>';
            }
        });
        list += '</div>';

        el.innerHTML = '<div class="ou-two"><div class="ou-stack">' + left + cls + '</div><div class="ou-stack">' + list +
            (o.extraRight || '') + '</div></div>' + (o.extraBottom || '');

        el.querySelectorAll('[data-model]').forEach((b) => b.addEventListener('click', () => {
            if (h.setModel && !b.querySelector('.ou-radio.on')) h.setModel(b.dataset.model);
        }));
        el.querySelectorAll('[data-cls]').forEach((b) => b.addEventListener('click', () => {
            if (h.toggleClass) h.toggleClass(b.dataset.cls, names);
        }));
        const install = el.querySelector('[data-ai="install"]');
        if (install) install.addEventListener('click', () => h.installAI());
    }

    // Class selection rule: empty list = everything. Toggling from "all"
    // leaves every other class on.
    function toggleClass(picked, name, all) {
        let next = picked && picked.length ? picked.slice() : all.slice();
        const i = next.indexOf(name);
        if (i === -1) next.push(name); else next.splice(i, 1);
        if (next.length === all.length) next = [];
        return next;
    }

    // ---------- sheets ----------
    // confirm({title, body, ok, okClass, cancel, countdown}) -> Promise<bool>
    function confirmSheet(opts) {
        const o = opts || {};
        return new Promise((resolve) => {
            const scrim = document.createElement('div');
            scrim.className = 'ou-scrim';
            scrim.innerHTML = '<div class="ou-modal" role="dialog" aria-modal="true"><h3>' + esc(o.title) + '</h3>' +
                (o.body ? '<p>' + esc(o.body) + '</p>' : '') + (o.html || '') +
                '<div class="ou-modal-foot"><button type="button" class="ou-btn ghost" data-a="no">' + esc(o.cancel || 'Cancel') +
                '</button><button type="button" class="ou-btn ' + (o.okClass || 'navy') + '" data-a="yes">' + esc(o.ok || 'OK') + '</button></div></div>';
            (document.querySelector('.ou-app') || document.body).appendChild(scrim);
            const yes = scrim.querySelector('[data-a="yes"]');
            let timer = null;
            if (o.countdown) {
                let left = o.countdown;
                yes.disabled = true;
                yes.textContent = (o.ok || 'OK') + ' (' + left + ')';
                timer = setInterval(() => {
                    left -= 1;
                    if (left <= 0) { clearInterval(timer); yes.disabled = false; yes.textContent = o.ok || 'OK'; }
                    else yes.textContent = (o.ok || 'OK') + ' (' + left + ')';
                }, 1000);
            }
            function done(v) { if (timer) clearInterval(timer); scrim.remove(); resolve(v); }
            yes.addEventListener('click', () => done(true));
            scrim.querySelector('[data-a="no"]').addEventListener('click', () => done(false));
            scrim.addEventListener('click', (e) => { if (e.target === scrim) done(false); });
        });
    }

    // choices sheet: [{label, hint, value, cls}] -> Promise<value|null>
    function choiceSheet(opts) {
        const o = opts || {};
        return new Promise((resolve) => {
            const scrim = document.createElement('div');
            scrim.className = 'ou-scrim';
            scrim.innerHTML = '<div class="ou-modal" role="dialog" aria-modal="true"><h3>' + esc(o.title) + '</h3>' +
                (o.body ? '<p>' + esc(o.body) + '</p>' : '') + '<div class="ou-list">' +
                (o.choices || []).map((c, i) => '<button type="button" class="ou-row" data-i="' + i + '"><div class="main"><span class="name"' +
                    (c.danger ? ' style="color:var(--ou-bad)"' : '') + '>' + esc(c.label) + '</span>' +
                    (c.hint ? '<span class="meta">' + esc(c.hint) + '</span>' : '') + '</div>' + ICONS.chev.replace('<svg', '<svg width="18" height="18" style="color:#B4BEC9"') + '</button>').join('') +
                '</div><div class="ou-modal-foot"><button type="button" class="ou-btn ghost" data-a="no">Cancel</button></div></div>';
            (document.querySelector('.ou-app') || document.body).appendChild(scrim);
            function done(v) { scrim.remove(); resolve(v); }
            scrim.querySelectorAll('[data-i]').forEach((b) => b.addEventListener('click', () => done(o.choices[+b.dataset.i].value)));
            scrim.querySelector('[data-a="no"]').addEventListener('click', () => done(null));
            scrim.addEventListener('click', (e) => { if (e.target === scrim) done(null); });
        });
    }

    // prompt({title, body, label, password}) -> Promise<string|null>
    function promptSheet(opts) {
        const o = opts || {};
        return new Promise((resolve) => {
            const scrim = document.createElement('div');
            scrim.className = 'ou-scrim';
            scrim.innerHTML = '<div class="ou-modal" role="dialog" aria-modal="true"><h3>' + esc(o.title) + '</h3>' +
                (o.body ? '<p>' + esc(o.body) + '</p>' : '') +
                '<input class="ou-input" type="' + (o.password ? 'password' : 'text') + '" data-numpad autocomplete="off" aria-label="' + esc(o.label || o.title) + '">' +
                '<div class="ou-modal-foot"><button type="button" class="ou-btn ghost" data-a="no">Cancel</button>' +
                '<button type="button" class="ou-btn navy" data-a="yes">' + esc(o.ok || 'OK') + '</button></div></div>';
            (document.querySelector('.ou-app') || document.body).appendChild(scrim);
            const input = scrim.querySelector('input');
            input.dataset.label = o.label || o.title || '';
            function done(v) { scrim.remove(); resolve(v); }
            scrim.querySelector('[data-a="yes"]').addEventListener('click', () => done(input.value));
            scrim.querySelector('[data-a="no"]').addEventListener('click', () => done(null));
        });
    }

    // ---------- device token (standalone guarded routes) ----------
    // Shut down, reboot, Install AI, delete session and network reset need
    // the unit's device token when it has one. The kiosk browser has none:
    // on a 403 the driver types the hotspot password once and the OWL hands
    // the token back (POST /api/settings/token, the app's recovery path).
    const TOKEN_KEY = 'owl-device-token';
    async function guardedFetch(url, options) {
        const opts = Object.assign({ method: 'POST' }, options || {});
        const attempt = () => {
            const headers = Object.assign({}, opts.headers || {});
            const token = safeStore(TOKEN_KEY);
            if (token) headers['X-Device-Token'] = token;
            return fetch(url, Object.assign({}, opts, { headers: headers }));
        };
        let res = await attempt();
        if (res.status !== 403) return res;
        let body = {};
        try { body = await res.clone().json(); } catch (e) { /* not JSON */ }
        if (!/token/i.test(String(body.error || ''))) return res;
        const password = await promptSheet({
            title: 'Hotspot password', password: true, ok: 'Continue', label: 'Hotspot password',
            body: 'This OWL is protected. Type its Wi-Fi hotspot password once to continue.',
        });
        if (!password) return res;
        const tr = await fetch('/api/settings/token', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hotspot_password: password }),
        });
        const td = await tr.json().catch(() => ({}));
        if (!tr.ok || !td.device_token) {
            toast(td.error || 'Wrong hotspot password', 'error');
            return res;
        }
        safeStore(TOKEN_KEY, td.device_token);
        res = await attempt();
        return res;
    }

    // ---------- hold to spray ----------
    // Spray while held; release, leaving the button or maxMs stops it.
    function hold(btn, o) {
        let timer = null;
        let tick = null;
        let on = false;
        const maxMs = o.maxMs || 10000;
        const label = btn.querySelector('small');
        const idle = label ? label.textContent : '';
        function start(e) {
            if (e) e.preventDefault();
            if (on) return;
            on = true;
            btn.classList.add('is-on');
            o.start();
            const t0 = Date.now();
            timer = setTimeout(stop, maxMs);
            tick = setInterval(() => {
                if (label) label.textContent = 'Spraying. Stops in ' + Math.max(0, Math.ceil((maxMs - (Date.now() - t0)) / 1000)) + ' s';
            }, 250);
        }
        function stop() {
            if (!on) return;
            on = false;
            clearTimeout(timer);
            clearInterval(tick);
            btn.classList.remove('is-on');
            if (label) label.textContent = idle;
            o.stop();
        }
        btn.addEventListener('pointerdown', start);
        ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => btn.addEventListener(ev, stop));
        btn.addEventListener('keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') start(e); });
        btn.addEventListener('keyup', stop);
        window.addEventListener('blur', stop);
        document.addEventListener('visibilitychange', () => { if (document.hidden) stop(); });
        return { stop: stop };
    }

    // ---------- stepper (kstep) ----------
    function stepper(el, o) {
        el.innerHTML = '<button type="button" aria-label="Less">−</button><span></span><button type="button" aria-label="More">+</button>';
        const [minus, plus] = el.querySelectorAll('button');
        const out = el.querySelector('span');
        let value = o.value;
        function render() {
            out.innerHTML = (value === null || value === undefined ? '--' : esc(o.format ? o.format(value) : value)) +
                (o.unit ? '<small>' + esc(o.unit) + '</small>' : '');
        }
        function bump(d) {
            if (value === null || value === undefined) return;
            const next = Math.round(Math.min(o.max, Math.max(o.min, value + d * o.step)) * 1000) / 1000;
            if (next === value) return;
            value = next;
            render();
            o.onChange(value);
        }
        minus.addEventListener('click', () => bump(-1));
        plus.addEventListener('click', () => bump(1));
        render();
        return { set: (v) => { value = v; render(); }, get: () => value };
    }

    // <span data-icon="name"></span> placeholders in the templates
    function fillIcons(root) {
        (root || document).querySelectorAll('[data-icon]').forEach((el) => {
            if (!el.firstChild && ICONS[el.dataset.icon]) el.innerHTML = ICONS[el.dataset.icon];
        });
    }

    window.OwlUI = {
        fillIcons: fillIcons,
        ICONS: ICONS, esc: esc, SEP: SEP, joinSep: joinSep, toast: toast, store: safeStore,
        initNav: initNav, initSettings: initSettings, setBadge: setBadge,
        setSwitch: setSwitch, statCells: statCells, labsHTML: labsHTML, notice: notice,
        fmtGB: fmtGB, fmtBytes: fmtBytes, relTime: relTime, sessionWhen: sessionWhen,
        MODE_LABELS: MODE_LABELS, algoMode: algoMode,
        canRunAI: canRunAI, aiReported: aiReported, isHailo: isHailo, aiRunBadge: aiRunBadge, aiSummary: aiSummary,
        modelRows: modelRows, renderAIModels: renderAIModels, toggleClass: toggleClass,
        confirm: confirmSheet, choose: choiceSheet, prompt: promptSheet,
        guardedFetch: guardedFetch, hold: hold, stepper: stepper,
    };
})();
