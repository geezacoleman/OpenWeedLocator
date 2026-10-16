// ============================================
// OWL Controller - shared data-collection form pieces
// The capture settings form and the "What are you recording?" modal, used
// by the networked collect screen and the standalone dashboard. Both talk
// to the same routes (/api/collect/config, /settings, /start), so this file
// holds no controller-specific code; callers pass the API calls in.
// Labels: window.CollectI18n (collect_i18n.js). Values sent are slugs.
// Styles: /shared/css/_collect.css (inside a .collect or .c-scope element).
// ============================================

(function () {
    'use strict';

    const PICK_KEYS = ['crop', 'crop_season', 'light_source', 'sky', 'weather',
                       'soil_cover', 'soil_moisture', 'recent_water', 'purpose'];
    const t = (key, values) => window.CollectI18n.t(key, values);
    const $in = (root, sel) => root.querySelector(sel);
    const esc = (s) => String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    // A choice in the middle of a sentence: "Sunny, dry"
    const lowerFirst = (s) => (s ? s.charAt(0).toLowerCase() + s.slice(1) : s);

    // The single `lighting` slug older runs and [Collect] last_lighting hold
    const LEGACY_LIGHTING = {
        natural_sun: ['natural', 'sunny'], natural_partial: ['natural', 'partly_cloudy'],
        natural_overcast: ['natural', 'overcast'], artificial: ['artificial', ''], mixed: ['mixed', ''],
    };

    // Last choices come from the controller ([Collect] last_*): the kiosk
    // browser runs incognito, so local storage is empty after every boot.
    // Nothing is chosen until the driver taps it (no invented conditions);
    // after the first run every choice comes back from last time.
    function defaultPick(last) {
        const pick = { crop: '', crop_season: '', light_source: '', sky: '', weather: '', soil_cover: '',
                       soil_moisture: '', recent_water: '', purpose: '', notes: '' };
        Object.assign(pick, last || {});
        const legacy = LEGACY_LIGHTING[pick.lighting];
        if (!pick.light_source && legacy) {
            pick.light_source = legacy[0];
            pick.sky = pick.sky || legacy[1];
        }
        delete pick.lighting;
        return pick;
    }

    function cropLabel(crop, presets) {
        if (!crop) return '';
        const known = ((presets || {}).crops || []).indexOf(crop) !== -1 || t('crop_' + crop) !== crop;
        return known ? t('crop_' + crop) : crop;
    }

    // An enclosed hood with its own lights: sky and weather don't apply
    function isIndoor(md) { return md.light_source === 'artificial'; }

    function cropText(md, presets) {
        if (!md.crop) return '';
        const crop = cropLabel(md.crop, presets);
        return md.crop_season ? t('season_' + md.crop_season) + ' ' + lowerFirst(crop) : crop;
    }

    function joinParts(parts) {
        return parts.filter(Boolean).map((p, i) => (i ? lowerFirst(p) : p)).join(', ');
    }

    function lightText(md) {
        if (!md.light_source) {
            // Runs recorded before light source and sky were separate
            return joinParts([md.lighting ? t('lighting_' + md.lighting) : '', md.weather ? t('weather_' + md.weather) : '']);
        }
        if (isIndoor(md)) return t('sum_indoor');
        const parts = [];
        if (md.light_source === 'mixed') parts.push(t('sum_mixed'));
        else if (!md.sky) parts.push(t('sum_natural'));
        if (md.sky) parts.push(t('sky_' + md.sky));
        if (md.weather) parts.push(t('weather_' + md.weather));
        return joinParts(parts);
    }

    function soilText(md) {
        const watered = md.recent_water === 'rain' || md.recent_water === 'irrigation';
        let wet = '';
        if (md.soil_moisture) wet = t('moisture_' + md.soil_moisture) + (watered ? ' ' + t('sum_after_' + md.recent_water) : '');
        else if (watered) wet = t('sum_water_' + md.recent_water);
        return joinParts([md.soil_cover ? t('cover_' + md.soil_cover) : '', wet]);
    }

    // ---------- sessions on the OWLs ----------
    // "214 photos", "1 video", "12 photos, 1 video"
    function sessionCounts(s) {
        const n = s.image_count || 0, v = s.video_count || 0;
        const parts = [];
        if (n || !v) parts.push(n === 1 ? t('one_photo') : t('n_photos', { n: n }));
        if (v) parts.push(v === 1 ? t('one_video') : t('n_videos', { n: v }));
        return parts.join(', ');
    }

    // Where a session's upload is: {cls, text, pct, sub}. cls: '' on the
    // OWL, 'req' waiting on Noktura, 'up' sending, 'ok' sent, 'bad' not
    // sent. Noktura runs one upload at a time per rig and carries on by
    // itself when the signal drops (acks queued / retrying / stored /
    // failed, controller /api/collect/state uploads, newest first); the
    // OWL reports the bytes in data_transfer.
    function uploadStatus(owl, session, uploads) {
        const req = (uploads || []).find((u) => u.local_id === owl.id && u.session_id === session.session_id);
        const tr = owl.data_transfer || {};
        const mine = tr.session_date === session.session_id && tr.status;
        const out = (cls, text, pct, sub) => ({ cls: cls, text: text, pct: pct || 0, sub: sub || '' });
        if (mine && ['idle', 'complete', 'error'].indexOf(tr.status) === -1) {
            // progress comes from the OWL's MQTT state: a number, never markup
            const pct = Math.max(0, Math.min(100, Math.round(Number(tr.progress) || 0)));
            return out('up', t('st_uploading', { p: pct }), pct, Number(tr.retries) > 0 ? t('st_carried_on') : '');
        }
        if (req) {
            if (req.status === 'stored') return out('ok', t('st_uploaded'));
            if (req.status === 'failed') return out('bad', t('st_not_sent'));
            if (req.status === 'rejected') return out('bad', t('st_rejected') + (req.reason ? ': ' + req.reason : ''));
            if (req.status === 'retrying') {
                return out('req', req.retry_in_s ? t('st_retrying_in', { s: req.retry_in_s }) : t('st_retrying'));
            }
            if (req.status === 'queued') {
                return out('req', req.position && req.queue_length
                    ? t('st_waiting', { pos: req.position, n: req.queue_length }) : t('st_waiting_plain'));
            }
            if (req.status === 'requested') return out('req', t('st_requested'));
            // accepted: the OWL has been told to send, until it reports an end
            const ended = mine && (tr.status === 'complete' || tr.status === 'error');
            if (req.status === 'accepted' && !ended) return out('req', t('st_accepted'));
        }
        if (mine && tr.status === 'complete') return out('ok', t('st_uploaded'));
        if (mine && tr.status === 'error') return out('bad', t('st_failed'));
        return out('', t('st_on_owl'));
    }

    // Short lines for a run: crop, light and weather, soil, purpose
    function metadataChips(md, presets) {
        if (!md || !md.crop) return null;
        return [cropText(md, presets), lightText(md), soilText(md),
                md.purpose ? t('purpose_' + md.purpose) : ''].filter(Boolean);
    }

    // ---------- capture settings ----------
    const MODE_LABELS = { distance: 'by_distance', time: 'by_time', frames: 'by_frames' };

    function seg(setting, options) {
        return '<div class="c-seg" data-setting="' + setting + '">' + options.map((o) =>
            '<button type="button" data-value="' + o[0] + '"' + (o[1] ? ' data-i18n="' + o[1] + '"' : '') + '>' +
            (o[2] || '') + '</button>').join('') + '</div>';
    }
    // steppers: wrap the number in − / + buttons (the standalone and
    // networked Settings panels); the input still opens the numpad.
    let steppers = false;
    function numberField(id, setting, label, step, min, max) {
        const input = '<input class="c-input" id="' + id + '" type="number" step="' + step + '" min="' + min + '" max="' + max +
            '" data-numpad data-setting="' + setting + '" />';
        return '<div class="c-field"><label class="c-label" for="' + id + '" data-i18n="' + label + '"></label>' +
            (steppers ? '<div class="c-stepper"><button type="button" data-step="-1" aria-label="Less">−</button>' + input +
                '<button type="button" data-step="1" aria-label="More">+</button></div>' : input) + '</div>';
    }

    // opts.sampleModes: which "Take images" choices to offer (networked:
    // distance/time; standalone adds frames, its own default).
    // opts.gain: include the gain limit (networked has it on the Camera pane).
    function captureFormHTML(opts) {
        const o = opts || {};
        const modes = o.sampleModes || ['distance', 'time'];
        steppers = !!o.steppers;
        let html = '';
        html += '<div class="c-field"><span class="c-label" data-i18n="record_as"></span>' +
            seg('record_mode', [['images', 'stills'], ['video', 'video']]) + '</div>';
        html += '<div class="c-field"><span class="c-label" data-i18n="spacing_mode"></span>' +
            seg('sample_mode', modes.map((m) => [m, MODE_LABELS[m]])) + '</div>';
        html += numberField('set-distance', 'sample_distance_m', 'every_m', '0.1', '0.1', '50');
        html += numberField('set-interval', 'sample_interval_s', 'every_s', '0.1', '0.2', '60');
        html += numberField('set-min-speed', 'min_speed_kmh', 'min_speed', '0.1', '0', '20');
        html += numberField('set-quality', 'jpeg_quality', 'quality', '1', '50', '100');
        html += numberField('set-video', 'video_max_s', 'video_len', '1', '5', '60');
        html += numberField('set-smear', 'smear_limit_mm', 'smear', '0.1', '0.2', '20');
        html += '<div class="c-field"><span class="c-label" data-i18n="shutter_cap"></span>' +
            seg('shutter_cap', [['true', 'on'], ['false', 'off']]) + '</div>';
        if (o.gain) {
            html += '<div class="c-field"><span class="c-label" data-i18n="gain_limit"></span>' +
                seg('max_gain', [['auto', 'auto'], ['8', '', '8×'], ['16', '', '16×']]) + '</div>';
        }
        return html;
    }

    function captureDisplayValue(key, value) {
        if (key === 'max_gain') return String(value === undefined || value === null ? 'auto' : value).replace(/\.0$/, '');
        return String(value);
    }

    // Fills container (a .c-form) and binds it. onChange(key, value) is
    // called for every edit; render(capture) redraws from saved values.
    function mountCapture(container, opts) {
        const o = opts || {};
        container.innerHTML = captureFormHTML(o);
        if (window.CollectI18n) window.CollectI18n.apply(container);
        container.querySelectorAll('.c-seg[data-setting]').forEach((s) => s.querySelectorAll('button').forEach((b) =>
            b.addEventListener('click', () => o.onChange(s.dataset.setting, b.dataset.value))));
        container.querySelectorAll('input[data-setting]').forEach((input) =>
            input.addEventListener('change', () => o.onChange(input.dataset.setting, input.value)));
        container.querySelectorAll('.c-stepper button[data-step]').forEach((b) => b.addEventListener('click', () => {
            const input = b.parentElement.querySelector('input');
            const step = parseFloat(input.step) || 1;
            const cur = parseFloat(input.value);
            if (isNaN(cur)) return;
            const next = Math.min(parseFloat(input.max), Math.max(parseFloat(input.min), cur + step * Number(b.dataset.step)));
            input.value = String(Math.round(next * 100) / 100);
            input.dispatchEvent(new Event('change'));
        }));

        function render(capture) {
            const c = capture || {};
            container.querySelectorAll('[data-setting]').forEach((el) => {
                const key = el.dataset.setting;
                if (el.classList.contains('c-seg')) {
                    el.querySelectorAll('button').forEach((b) =>
                        b.setAttribute('aria-pressed', String(captureDisplayValue(key, c[key]) === b.dataset.value)));
                } else if (c[key] !== undefined) {
                    el.value = c[key];
                }
            });
            // Only the fields the chosen modes use: distance spacing (with
            // the no-GPS time fallback), time spacing, or every Nth frame
            // (frequency lives under Advanced); clip length only for video
            const mode = c.sample_mode;
            showField('sample_distance_m', mode === 'distance');
            showField('sample_interval_s', mode === 'distance' || mode === 'time');
            showField('video_max_s', c.record_mode === 'video');
            // Only still images pause when slow; a video clip keeps running
            showField('min_speed_kmh', c.record_mode !== 'video');
        }
        function showField(setting, visible) {
            const input = container.querySelector('input[data-setting="' + setting + '"]');
            if (input) input.closest('.c-field').hidden = !visible;
        }
        return { render: render };
    }

    // ---------- record modal (design B, signed off 2026-10-16) ----------
    // Five rows on the left show what is picked (Crop, Light and weather,
    // Soil, Purpose, Notes); tapping one shows only its choices on the
    // right. Start recording is always one tap away. Only the crop grid
    // scrolls; everything fits the 7" screen (1024x600).
    const SECTIONS = ['crop', 'light', 'soil', 'purpose', 'notes'];
    const CHEVRON = '<svg class="c-rf-chev" viewBox="0 0 20 20" aria-hidden="true"><path d="M7 4l6 6-6 6"></path></svg>';
    const MODAL_HTML =
        '<div class="c-modal c-rf" role="dialog" aria-modal="true" aria-labelledby="modal-title">' +
        '<div class="c-rf-head"><h2 id="modal-title" data-i18n="m_title"></h2>' +
        '<button class="c-btn" type="button" id="modal-cancel" data-i18n="cancel"></button></div>' +
        '<div class="c-rf-body"><div class="c-rf-list">' +
        SECTIONS.map((s) => '<button type="button" class="c-rf-row" data-section="' + s + '" aria-pressed="false">' +
            '<span class="c-rf-text"><span class="c-rf-k" data-i18n="sec_' + s + '"></span><span class="c-rf-v"></span></span>' +
            CHEVRON + '</button>').join('') +
        '<button class="c-btn c-btn--go c-rf-start" type="button" id="modal-start" data-i18n="start"></button>' +
        '</div><div class="c-rf-pane" id="rf-pane"></div></div>' +
        '<input type="text" id="meta-add-crop" data-numpad hidden maxlength="40" />' +
        '</div>';

    function question(label, note, body) {
        return '<div class="c-rf-q"><div class="c-rf-qh"><span>' + esc(label) + '</span>' +
            (note ? '<small>' + esc(note) + '</small>' : '') + '</div>' + body + '</div>';
    }

    function optionButtons(key, values, chosen, prefix, disabled) {
        return values.map((v) => '<button type="button" data-pick="' + key + '" data-value="' + esc(v) + '" aria-pressed="' +
            String(!disabled && chosen === v) + '"' + (disabled ? ' disabled' : '') + '>' + esc(t(prefix + '_' + v)) + '</button>').join('');
    }

    // One row of joined choices (3 or 4 of them)
    function segQuestion(label, note, key, values, chosen, prefix, disabled) {
        return question(label, note, '<div class="c-seg c-rf-seg" role="group" aria-label="' + esc(label) + '">' +
            optionButtons(key, values, chosen, prefix, disabled) + '</div>');
    }

    // opts.parent: where the scrim goes (default document.body; must sit
    // inside a .collect / .c-scope element for the styles).
    // opts.presets(): current preset lists. opts.onStart(metadata): Start.
    // opts.onPresets(presets): new lists after a crop was added or removed
    // (POST / DELETE /api/collect/crops, the same on both controllers).
    function mountRecordModal(opts) {
        const o = opts || {};
        const scrim = document.createElement('div');
        scrim.className = 'c-scrim';
        scrim.id = 'record-modal';
        scrim.hidden = true;
        scrim.innerHTML = MODAL_HTML;
        (o.parent || document.body).appendChild(scrim);
        if (window.CollectI18n) window.CollectI18n.apply(scrim);
        const startBtn = $in(scrim, '#modal-start');
        const pane = $in(scrim, '#rf-pane');
        const addInput = $in(scrim, '#meta-add-crop');
        let pick = defaultPick();
        let section = 'crop';
        let tab = null;
        let removing = false;
        let ownPresets = null;

        function presets() { return ownPresets || (o.presets && o.presets()) || {}; }
        function seasonal(p) { return (p.seasonal_crops || []).indexOf(pick.crop) !== -1; }
        function say(msg) {
            if (typeof showToast === 'function') showToast(msg, 'error');
            else if (typeof showNotification === 'function') showNotification('Error', msg, 'error');
        }

        // Your crops first, then the built-in groups. A crop picked last
        // time that is on no list (typed before crops could be saved)
        // still shows under Your crops so it stays chosen.
        function cropGroups(p) {
            const mine = (p.custom_crops || []).slice();
            const groups = (p.crop_groups && p.crop_groups.length) ? p.crop_groups
                : [{ key: 'all', crops: (p.crops || []).filter((c) => mine.indexOf(c) === -1) }];
            const listed = mine.concat.apply(mine, groups.map((g) => g.crops));
            if (pick.crop && listed.indexOf(pick.crop) === -1) mine.unshift(pick.crop);
            return [{ key: 'mine', crops: mine }].concat(groups);
        }

        function sectionValue(name, p) {
            if (name === 'crop') return cropText({ crop: pick.crop, crop_season: seasonal(p) ? pick.crop_season : '' }, p);
            if (name === 'light') return lightText(pick);
            if (name === 'soil') return soilText(pick);
            if (name === 'purpose') return pick.purpose ? t('purpose_' + pick.purpose) : '';
            return pick.notes;
        }

        function renderList() {
            const p = presets();
            scrim.querySelectorAll('.c-rf-row').forEach((row) => {
                const name = row.dataset.section;
                const value = sectionValue(name, p);
                const v = row.querySelector('.c-rf-v');
                v.textContent = value || t(name === 'crop' ? 'choose_crop' : name === 'notes' ? 'none' : 'not_set');
                v.classList.toggle('is-unset', !value);
                row.setAttribute('aria-pressed', String(name === section));
            });
            startBtn.disabled = !pick.crop;
        }

        function cropPane(p) {
            const groups = cropGroups(p);
            if (!tab || !groups.some((g) => g.key === tab)) {
                const holding = groups.find((g) => g.crops.indexOf(pick.crop) !== -1);
                tab = holding ? holding.key : (groups[0].crops.length ? 'mine' : groups[1] ? groups[1].key : 'mine');
            }
            const custom = p.custom_crops || [];
            const shown = groups.find((g) => g.key === tab).crops;
            removing = removing && tab === 'mine' && custom.length > 0;
            let html = '<div class="c-rf-bar"><div class="c-seg c-rf-tabs" role="group" aria-label="' + esc(t('crop')) + '">' +
                groups.map((g) => '<button type="button" data-tab="' + g.key + '" aria-pressed="' + String(g.key === tab) + '">' +
                    esc(t('crops_' + g.key)) + '</button>').join('') + '</div>';
            if (tab === 'mine' && custom.length) {
                html += '<button type="button" class="c-btn c-rf-edit" data-act="removing" aria-pressed="' + removing + '">' +
                    esc(t(removing ? 'done' : 'edit_crops')) + '</button>';
            }
            html += '<button type="button" class="c-btn c-rf-add" data-act="add">' + esc(t('add_crop')) + '</button></div>';
            html += '<div class="c-rf-tiles c-rf-crops">';
            html += shown.map((c) => {
                if (removing && custom.indexOf(c) !== -1) {
                    return '<button type="button" class="c-opt c-opt--remove" data-act="remove" data-value="' + esc(c) + '">' +
                        esc(cropLabel(c, p)) + '<small>' + esc(t('tap_remove')) + '</small></button>';
                }
                return '<button type="button" class="c-opt" data-pick="crop" data-value="' + esc(c) + '" aria-pressed="' +
                    String(pick.crop === c) + '">' + esc(cropLabel(c, p)) + '</button>';
            }).join('');
            if (!shown.length) html += '<p class="c-hint c-rf-empty">' + esc(t('no_own_crops')) + '</p>';
            html += '</div>';
            const isSeasonal = seasonal(p);
            html += segQuestion(t('season'), isSeasonal ? '' : t('season_only'), 'crop_season', ['winter', 'spring'],
                pick.crop_season, 'season', !isSeasonal);
            return html;
        }

        function lightPane(p) {
            const indoor = isIndoor(pick);
            return segQuestion(t('light'), '', 'light_source', p.light_sources || [], pick.light_source, 'light') +
                segQuestion(t('sky'), indoor ? t('not_needed_light') : '', 'sky', p.skies || [], pick.sky, 'sky', indoor) +
                segQuestion(t('weather'), indoor ? t('not_needed_indoors') : '', 'weather', p.weather || [], pick.weather, 'weather', indoor);
        }

        function soilPane(p) {
            return question(t('soil_cover'), '', '<div class="c-rf-tiles" role="group" aria-label="' + esc(t('soil_cover')) + '">' +
                    (p.soil_covers || []).map((v) => '<button type="button" class="c-opt" data-pick="soil_cover" data-value="' + esc(v) +
                        '" aria-pressed="' + String(pick.soil_cover === v) + '">' + esc(t('cover_' + v)) + '</button>').join('') + '</div>') +
                segQuestion(t('soil_surface'), '', 'soil_moisture', p.soil_moistures || [], pick.soil_moisture, 'moisture') +
                segQuestion(t('watered'), t('watered_note'), 'recent_water', p.recent_water || [], pick.recent_water, 'water');
        }

        function renderPane() {
            const p = presets();
            let html = '';
            if (section === 'crop') html = cropPane(p);
            else if (section === 'light') html = lightPane(p);
            else if (section === 'soil') html = soilPane(p);
            else if (section === 'purpose') html = segQuestion(t('purpose_q'), '', 'purpose', p.purposes || [], pick.purpose, 'purpose');
            else {
                html = '<div class="c-rf-q"><label class="c-rf-qh" for="meta-notes"><span>' + esc(t('notes')) + '</span><small>' +
                    esc(t('optional')) + '</small></label><input class="c-input c-rf-notes" id="meta-notes" type="text" maxlength="200" data-numpad /></div>';
            }
            const next = SECTIONS[SECTIONS.indexOf(section) + 1];
            if (next) {
                html += '<div class="c-rf-next"><button type="button" class="c-btn" data-act="next">' +
                    esc(t('next_to', { s: t('sec_' + next) })) + '</button></div>';
            }
            pane.innerHTML = html;
            const notes = $in(pane, '#meta-notes');
            if (notes) {
                notes.value = pick.notes || '';
                notes.placeholder = t('notes_ph');
                notes.dataset.label = t('notes');
                const keep = () => { pick.notes = notes.value.trim(); renderList(); };
                notes.addEventListener('input', keep);
                notes.addEventListener('change', keep);
            }
        }

        function refresh() { renderList(); renderPane(); }

        function setPresets(next) {
            if (o.onPresets) o.onPresets(next); else ownPresets = next;
        }

        function cropRequest(url, method, body) {
            return fetch(url, { method: method, headers: { 'Content-Type': 'application/json' },
                                body: body ? JSON.stringify(body) : undefined })
                .then((r) => r.json().catch(() => ({})).then((data) => {
                    if (!r.ok || !data.success) throw new Error(data.error || t('crop_not_saved'));
                    setPresets(data.presets);
                    return data;
                }));
        }

        // Saved on the controller straight away ([Collect] custom_crops)
        function addCrop(name) {
            cropRequest('/api/collect/crops', 'POST', { name: name }).then((data) => {
                pick.crop = data.crop;
                tab = 'mine';
                removing = false;
                refresh();
            }).catch((e) => say(e.message));
        }
        function removeCrop(slug) {
            cropRequest('/api/collect/crops/' + encodeURIComponent(slug), 'DELETE').then(() => {
                if (pick.crop === slug) pick.crop = '';
                refresh();
            }).catch((e) => say(e.message));
        }

        function onPick(key, value) {
            // A second tap keeps the choice: a bump in the cab must not clear it
            pick[key] = value;
            refresh();
        }

        scrim.addEventListener('click', (e) => {
            const row = e.target.closest('.c-rf-row');
            if (row) { section = row.dataset.section; removing = false; refresh(); return; }
            const opt = e.target.closest('[data-pick]');
            if (opt && !opt.disabled) { onPick(opt.dataset.pick, opt.dataset.value); return; }
            const tabBtn = e.target.closest('[data-tab]');
            if (tabBtn) { tab = tabBtn.dataset.tab; removing = false; renderPane(); return; }
            const act = e.target.closest('[data-act]');
            if (!act) return;
            if (act.dataset.act === 'next') { section = SECTIONS[SECTIONS.indexOf(section) + 1]; refresh(); }
            else if (act.dataset.act === 'removing') { removing = !removing; renderPane(); }
            else if (act.dataset.act === 'remove') removeCrop(act.dataset.value);
            else if (act.dataset.act === 'add') {
                addInput.value = '';
                addInput.dataset.label = t('add_crop_ph');
                // The field stays hidden: the on-screen keyboard is the editor
                if (typeof Numpad !== 'undefined') Numpad.open(addInput);
                else { addInput.hidden = false; addInput.focus(); }
            }
        });
        addInput.addEventListener('change', () => {
            const name = addInput.value.trim();
            addInput.hidden = true;
            addInput.value = '';
            if (name) addCrop(name);
        });

        $in(scrim, '#modal-cancel').addEventListener('click', close);
        // open(lastPick, {startLabel, onStart}) reuses the form for other
        // jobs, e.g. adding details to a session already recorded
        let onStart = o.onStart;
        startBtn.addEventListener('click', () => {
            if (!pick.crop) { section = 'crop'; refresh(); return; }
            const p = presets();
            const indoor = isIndoor(pick);
            const handler = onStart;
            close();
            handler({
                crop: pick.crop, crop_season: seasonal(p) ? pick.crop_season : '',
                light_source: pick.light_source, sky: indoor ? '' : pick.sky, weather: indoor ? '' : pick.weather,
                soil_cover: pick.soil_cover, soil_moisture: pick.soil_moisture, recent_water: pick.recent_water,
                purpose: pick.purpose, notes: pick.notes,
            });
        });

        function open(lastPick, mode) {
            const m = mode || {};
            if (lastPick) pick = defaultPick(Object.assign({}, lastPick, { notes: m.onStart ? (lastPick.notes || '') : pick.notes }));
            onStart = m.onStart || o.onStart;
            startBtn.textContent = m.startLabel || t('start');
            section = 'crop';
            tab = null;
            removing = false;
            refresh();
            scrim.hidden = false;
        }
        function close() { scrim.hidden = true; onStart = o.onStart; }
        function setPick(last) { pick = defaultPick(last); }

        return { el: scrim, open: open, close: close, refresh: refresh, setPick: setPick };
    }

    window.CollectForm = {
        PICK_KEYS: PICK_KEYS,
        defaultPick: defaultPick,
        cropLabel: cropLabel,
        metadataChips: metadataChips,
        lightText: lightText,
        soilText: soilText,
        sessionCounts: sessionCounts,
        uploadStatus: uploadStatus,
        captureFormHTML: captureFormHTML,
        mountCapture: mountCapture,
        mountRecordModal: mountRecordModal,
    };
})();
