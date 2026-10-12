/* ==========================================================================
   Data collection (standalone)
   Mounts the shared collect form (/shared/js/collect_form.js): the capture
   settings card on the Config tab and the "What are you recording?" form
   that Record opens. Same /api/collect/* routes as the networked collect
   screen and owl-app. Settings save to the active config and apply live.
   ========================================================================== */

let _dcConfig = { presets: {}, capture: {}, language: 'en' };
let _dcCapture = null;
let _dcRecordModal = null;

function initDataCollection() {
    if (!window.CollectForm || !window.CollectI18n) return;

    // The modal needs the collect tokens: wrap it in a .c-scope element
    const scope = document.createElement('div');
    scope.className = 'c-scope';
    document.body.appendChild(scope);
    _dcRecordModal = CollectForm.mountRecordModal({
        parent: scope,
        presets: () => _dcConfig.presets,
        onStart: startCollectRecording,
    });

    const form = document.getElementById('dcCaptureForm');
    if (form) {
        _dcCapture = CollectForm.mountCapture(form, {
            sampleModes: ['frames', 'distance', 'time'],
            gain: true,
            onChange: saveCaptureSetting,
        });
    }

    apiRequest('/api/collect/config')
        .then(r => r.json())
        .then(data => {
            _dcConfig = data;
            CollectI18n.setLang(data.language || 'en');
            CollectI18n.apply(document.getElementById('data-collection-card') || document);
            CollectI18n.apply(scope);
            _dcRecordModal.setPick(data.last_pick);
            _dcRecordModal.refresh();
            if (_dcCapture) _dcCapture.render(data.capture);
        })
        .catch(() => {
            CollectI18n.apply(scope);
            if (form) CollectI18n.apply(form);
        });
}

function saveCaptureSetting(key, value) {
    const capture = {};
    capture[key] = value;
    // fetch, not apiRequest: apiRequest aborts the previous call to the same
    // URL, which would drop a save when two settings change quickly
    fetch('/api/collect/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ capture: capture }),
    })
        .then(r => r.json().then(data => ({ ok: r.ok, data: data })))
        .then(({ ok, data }) => {
            if (!ok || !data.success) throw new Error(data.error || 'Could not save');
            _dcConfig.capture = data.capture;
            _dcCapture.render(data.capture);
            showNotification('Saved', data.live === false
                ? 'Saved. It applies when the OWL is running.' : 'Saved', 'success');
        })
        .catch(err => {
            showNotification('Error', err.message || 'Could not save', 'error');
            _dcCapture.render(_dcConfig.capture);
        });
}

function openRecordForm() {
    if (_dcRecordModal) {
        _dcRecordModal.open();
        return;
    }
    // Shared form missing (scripts failed to load): record without labels
    startCollectRecording({});
}

function startCollectRecording(metadata) {
    showNotification('Info', 'Starting recording...', 'info');
    fetch('/api/collect/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ metadata: metadata }),
    })
        .then(r => r.json().then(data => ({ ok: r.ok, data: data })))
        .then(({ ok, data }) => {
            if (!ok || !data.success) throw new Error(data.error || CollectI18n.t('dc_failed'));
            _dcConfig.last_pick = metadata;
            onRecordingStarted();
        })
        .catch(err => showNotification('Error', err.message || 'Could not start recording', 'error'));
}

function renderSessionChips(isRecording, metadata) {
    const box = document.getElementById('sessionChips');
    if (!box || !window.CollectForm) return;
    const chips = isRecording ? CollectForm.metadataChips(metadata, _dcConfig.presets) : null;
    box.innerHTML = '';
    (chips || []).forEach((text) => {
        const chip = document.createElement('span');
        chip.className = 'c-chip';
        chip.textContent = text;
        box.appendChild(chip);
    });
}
