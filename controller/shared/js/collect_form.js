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

    const PICK_KEYS = ['crop', 'crop_season', 'weather', 'lighting', 'purpose'];
    const t = (key, values) => window.CollectI18n.t(key, values);
    const $in = (root, sel) => root.querySelector(sel);

    // Last choices come from the controller ([Collect] last_*): the kiosk
    // browser runs incognito, so local storage is empty after every boot.
    // No crop is chosen until the driver taps one.
    function defaultPick(last) {
        return Object.assign({ crop: '', crop_season: 'winter', weather: 'dry',
                               lighting: 'natural_sun', purpose: 'herbicide', notes: '' }, last || {});
    }

    function cropLabel(crop, presets) {
        if (!crop) return '';
        const known = ((presets || {}).crops || []).indexOf(crop) !== -1 || t('crop_' + crop) !== crop;
        return known ? t('crop_' + crop) : crop;
    }

    function metadataChips(md, presets) {
        if (!md || !md.crop) return null;
        const chips = [];
        let crop = cropLabel(md.crop, presets);
        if (md.crop_season) crop = t('season_' + md.crop_season) + ' ' + crop.toLowerCase();
        chips.push(crop);
        if (md.weather) chips.push(t('weather_' + md.weather));
        if (md.lighting) chips.push(t('lighting_' + md.lighting));
        if (md.purpose) chips.push(t('purpose_' + md.purpose));
        return chips;
    }

    // ---------- capture settings ----------
    const MODE_LABELS = { distance: 'by_distance', time: 'by_time', frames: 'by_frames' };

    function seg(setting, options) {
        return '<div class="c-seg" data-setting="' + setting + '">' + options.map((o) =>
            '<button type="button" data-value="' + o[0] + '"' + (o[1] ? ' data-i18n="' + o[1] + '"' : '') + '>' +
            (o[2] || '') + '</button>').join('') + '</div>';
    }
    function numberField(id, setting, label, step, min, max) {
        return '<div class="c-field"><label class="c-label" for="' + id + '" data-i18n="' + label + '"></label>' +
            '<input class="c-input" id="' + id + '" type="number" step="' + step + '" min="' + min + '" max="' + max +
            '" data-numpad data-setting="' + setting + '" /></div>';
    }

    // opts.sampleModes: which "Take images" choices to offer (networked:
    // distance/time; standalone adds frames, its own default).
    // opts.gain: include the gain limit (networked has it on the Camera pane).
    function captureFormHTML(opts) {
        const o = opts || {};
        const modes = o.sampleModes || ['distance', 'time'];
        let html = '';
        html += '<div class="c-field"><span class="c-label" data-i18n="record_as"></span>' +
            seg('record_mode', [['images', 'stills'], ['video', 'video']]) + '</div>';
        html += '<div class="c-field"><span class="c-label" data-i18n="spacing_mode"></span>' +
            seg('sample_mode', modes.map((m) => [m, MODE_LABELS[m]])) + '</div>';
        html += numberField('set-distance', 'sample_distance_m', 'every_m', '0.1', '0.1', '50');
        html += numberField('set-interval', 'sample_interval_s', 'every_s', '0.1', '0.2', '60');
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
        }
        function showField(setting, visible) {
            const input = container.querySelector('input[data-setting="' + setting + '"]');
            if (input) input.closest('.c-field').hidden = !visible;
        }
        return { render: render };
    }

    // ---------- record modal ----------
    const MODAL_HTML =
        '<div class="c-modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">' +
        '<h2 id="modal-title" data-i18n="m_title"></h2>' +
        '<div class="c-mrow"><span data-i18n="crop"></span><div class="c-opts" data-pick="crop"></div></div>' +
        '<div class="c-mrow" id="season-row"><span data-i18n="season"></span><div class="c-opts" data-pick="crop_season"></div></div>' +
        '<div class="c-mrow"><span data-i18n="weather"></span><div class="c-opts" data-pick="weather"></div></div>' +
        '<div class="c-mrow"><span data-i18n="lighting"></span><div class="c-opts" data-pick="lighting"></div></div>' +
        '<div class="c-mrow"><span data-i18n="purpose"></span><div class="c-opts" data-pick="purpose"></div></div>' +
        '<div class="c-mrow"><label for="meta-notes" data-i18n="notes"></label>' +
        '<input class="c-input c-input--wide" id="meta-notes" type="text" maxlength="200" data-numpad data-i18n-ph="notes_ph" /></div>' +
        '<input type="text" id="meta-other-crop" data-numpad hidden maxlength="60" />' +
        '<div class="c-mfoot"><button class="c-btn" type="button" id="modal-cancel" data-i18n="cancel"></button>' +
        '<button class="c-btn c-btn--go" type="button" id="modal-start" data-i18n="start"></button></div>' +
        '</div>';

    // opts.parent: where the scrim goes (default document.body; must sit
    // inside a .collect / .c-scope element for the styles).
    // opts.presets(): current preset lists. opts.onStart(metadata): Start.
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
        const otherInput = $in(scrim, '#meta-other-crop');
        let pick = defaultPick();

        function presets() { return (o.presets && o.presets()) || {}; }

        function refresh() {
            const p = presets();
            const lists = {
                crop: (p.crops || []).concat(['__other']),
                crop_season: ['winter', 'spring'],
                weather: p.weather || [],
                lighting: p.lighting || [],
                purpose: p.purposes || [],
            };
            PICK_KEYS.forEach((key) => {
                const box = $in(scrim, '[data-pick="' + key + '"]');
                box.innerHTML = '';
                lists[key].forEach((value) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'c-opt';
                    const isOtherCustom = key === 'crop' && value === '__other' && pick.crop && lists.crop.indexOf(pick.crop) === -1;
                    if (value === '__other') b.textContent = isOtherCustom ? t('other') + ': ' + pick.crop : t('other') + '…';
                    else b.textContent = t((key === 'crop_season' ? 'season' : key) + '_' + value);
                    b.setAttribute('aria-pressed', String(pick[key] === value || isOtherCustom));
                    b.addEventListener('click', () => onPick(key, value));
                    box.appendChild(b);
                });
            });
            const seasonal = (p.seasonal_crops || []).indexOf(pick.crop) !== -1;
            $in(scrim, '#season-row').classList.toggle('is-hidden', !seasonal);
            startBtn.disabled = !pick.crop;
        }

        function onPick(key, value) {
            if (key === 'crop' && value === '__other') {
                otherInput.value = (presets().crops || []).indexOf(pick.crop) === -1 ? pick.crop : '';
                otherInput.dataset.label = t('other_ph');
                // The field stays hidden: the on-screen keyboard is the editor
                if (typeof Numpad !== 'undefined') Numpad.open(otherInput);
                else { otherInput.hidden = false; otherInput.focus(); }
                return;
            }
            pick[key] = value;
            refresh();
        }

        otherInput.addEventListener('change', () => {
            const value = otherInput.value.trim();
            otherInput.hidden = true;
            if (value) { pick.crop = value.toLowerCase(); refresh(); }
        });
        $in(scrim, '#modal-cancel').addEventListener('click', close);
        startBtn.addEventListener('click', () => {
            if (!pick.crop) return;
            pick.notes = $in(scrim, '#meta-notes').value.trim();
            const seasonal = (presets().seasonal_crops || []).indexOf(pick.crop) !== -1;
            close();
            o.onStart({
                crop: pick.crop, crop_season: seasonal ? pick.crop_season : '',
                weather: pick.weather, lighting: pick.lighting, purpose: pick.purpose, notes: pick.notes,
            });
        });

        function open(lastPick) {
            if (lastPick) pick = defaultPick(Object.assign({}, lastPick, { notes: pick.notes }));
            refresh();
            $in(scrim, '#meta-notes').value = pick.notes || '';
            scrim.hidden = false;
        }
        function close() { scrim.hidden = true; }
        function setPick(last) { pick = defaultPick(last); }

        return { el: scrim, open: open, close: close, refresh: refresh, setPick: setPick };
    }

    window.CollectForm = {
        PICK_KEYS: PICK_KEYS,
        defaultPick: defaultPick,
        cropLabel: cropLabel,
        metadataChips: metadataChips,
        captureFormHTML: captureFormHTML,
        mountCapture: mountCapture,
        mountRecordModal: mountRecordModal,
    };
})();
