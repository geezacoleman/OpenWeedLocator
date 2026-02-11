/* ==========================================================================
   OWL Dashboard - AI Tab Module (Standalone)
   Model selection and class filtering for YOLO detection
   ========================================================================== */

var aiSelectedClasses = [];
var aiModelClasses = {};
var aiAvailableModels = [];
var aiCurrentModel = '';
var aiClassesDirty = false;  // true when user has toggled buttons but not yet applied
var aiLastModelKey = '';      // tracks model changes to force grid rebuild

/* --------------------------------------------------------------------------
   Refresh from system stats
   -------------------------------------------------------------------------- */

function refreshAITab() {
    // Called when AI tab is opened or from stats polling
}

function syncAITabFromStats(data) {
    if (!data) return;

    aiAvailableModels = data.available_models || [];
    aiCurrentModel = data.current_model || '';
    aiModelClasses = data.model_classes || {};
    var existingSelection = data.detect_classes || [];

    var algorithm = data.algorithm || 'exhsv';
    var isAIMode = (algorithm === 'gog' || algorithm === 'gog-hybrid');
    var hasModel = aiCurrentModel !== '';

    // Update status banner
    var banner = document.getElementById('ai-status-banner');
    var statusText = document.getElementById('ai-status-text');

    if (banner && statusText) {
        if (!data.owl_running) {
            banner.className = 'ai-status-banner disconnected';
            statusText.textContent = 'OWL Not Running';
        } else if (!isAIMode) {
            banner.className = 'ai-status-banner no-model';
            statusText.textContent = 'Switch to AI or Hybrid detection mode';
        } else if (hasModel) {
            banner.className = 'ai-status-banner connected';
            statusText.textContent = 'Model Loaded: ' + aiCurrentModel;
        } else if (aiAvailableModels.length > 0) {
            banner.className = 'ai-status-banner no-model';
            statusText.textContent = 'No model active - select one below';
        } else {
            banner.className = 'ai-status-banner no-model';
            statusText.textContent = 'No models found in models/ directory';
        }
    }

    // Populate model dropdown
    populateModelDropdown();

    // Show model info badges
    updateModelInfo();

    // Detect model change — force grid rebuild even if dirty
    var modelKey = aiCurrentModel + '|' + Object.keys(aiModelClasses).length;
    if (modelKey !== aiLastModelKey) {
        aiLastModelKey = modelKey;
        aiClassesDirty = false;  // new model = new classes, must rebuild
    }

    // Only rebuild class grid if user hasn't made local edits
    if (!aiClassesDirty) {
        renderClassGrid(existingSelection);
    }
}

/* --------------------------------------------------------------------------
   Model Dropdown
   -------------------------------------------------------------------------- */

function populateModelDropdown() {
    var select = document.getElementById('ai-model-select');
    if (!select) return;

    select.innerHTML = '';

    if (aiAvailableModels.length === 0) {
        select.innerHTML = '<option value="">No models available</option>';
        return;
    }

    for (var i = 0; i < aiAvailableModels.length; i++) {
        var opt = document.createElement('option');
        opt.value = aiAvailableModels[i];
        opt.textContent = aiAvailableModels[i];
        if (aiAvailableModels[i] === aiCurrentModel) {
            opt.selected = true;
        }
        select.appendChild(opt);
    }
}

function updateModelInfo() {
    var infoEl = document.getElementById('ai-model-info');
    if (!infoEl) return;

    if (!aiCurrentModel) {
        infoEl.innerHTML = '';
        return;
    }

    var classCount = Object.keys(aiModelClasses).length;
    var isSeg = aiCurrentModel.toLowerCase().indexOf('seg') !== -1;
    var typeLabel = isSeg ? 'Segmentation' : 'Detection';
    var typeClass = isSeg ? 'seg' : 'det';

    infoEl.innerHTML =
        '<span class="ai-model-badge ' + typeClass + '">' + typeLabel + '</span>' +
        '<span class="ai-model-badge">' + classCount + ' classes</span>';
}

/* --------------------------------------------------------------------------
   Class Grid
   -------------------------------------------------------------------------- */

function renderClassGrid(existingSelection) {
    var grid = document.getElementById('ai-classes-grid');
    if (!grid) return;

    var classIds = Object.keys(aiModelClasses);

    if (classIds.length === 0) {
        grid.innerHTML = '<span style="color:var(--text-light);">Load a model to see classes</span>';
        aiSelectedClasses = [];
        updateClassHint();
        return;
    }

    var selectedLower = (existingSelection || []).map(function(c) { return c.toLowerCase(); });

    var html = '';
    for (var i = 0; i < classIds.length; i++) {
        var className = aiModelClasses[classIds[i]];
        var isSelected = selectedLower.indexOf(className.toLowerCase()) !== -1;
        html += '<button class="ai-class-btn' + (isSelected ? ' selected' : '') + '"' +
                ' data-class="' + className + '"' +
                ' onclick="toggleClassButton(this)">' +
                className +
                '</button>';
    }

    grid.innerHTML = html;

    // Rebuild selected list
    aiSelectedClasses = [];
    for (var j = 0; j < classIds.length; j++) {
        var name = aiModelClasses[classIds[j]];
        if (selectedLower.indexOf(name.toLowerCase()) !== -1) {
            aiSelectedClasses.push(name);
        }
    }

    updateClassHint();
}

function toggleClassButton(btn) {
    var className = btn.getAttribute('data-class');

    if (btn.classList.contains('selected')) {
        btn.classList.remove('selected');
        var idx = aiSelectedClasses.indexOf(className);
        if (idx !== -1) aiSelectedClasses.splice(idx, 1);
    } else {
        btn.classList.add('selected');
        aiSelectedClasses.push(className);
    }

    aiClassesDirty = true;
    updateClassHint();
}

function updateClassHint() {
    var hint = document.getElementById('ai-classes-hint');
    if (!hint) return;

    if (aiSelectedClasses.length === 0) {
        hint.textContent = 'Select classes to detect (empty = all)';
    } else {
        hint.textContent = aiSelectedClasses.length + ' class' +
            (aiSelectedClasses.length === 1 ? '' : 'es') + ' selected';
    }
}

/* --------------------------------------------------------------------------
   Actions
   -------------------------------------------------------------------------- */

function onModelSelected() {
    var select = document.getElementById('ai-model-select');
    if (!select || !select.value) return;

    apiRequest('/api/ai/set_model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: select.value })
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                showNotification('Success', 'Switching model to ' + select.value, 'success', 3000);
            } else {
                showNotification('Error', data.error || 'Failed to set model', 'error');
            }
        })
        .catch(function(err) {
            showNotification('Error', err.message || 'Failed to set model', 'error');
        });

    clearAIClassSelection();
}

function applyAIClassSelection() {
    apiRequest('/api/ai/set_detect_classes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ classes: aiSelectedClasses })
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                if (aiSelectedClasses.length === 0) {
                    showNotification('Success', 'Detecting all classes', 'success', 3000);
                } else {
                    showNotification('Success', 'Detecting: ' + aiSelectedClasses.join(', '), 'success', 3000);
                }
            } else {
                showNotification('Error', data.error || 'Failed to set classes', 'error');
            }
        })
        .catch(function(err) {
            showNotification('Error', err.message || 'Failed to set classes', 'error');
        });
    aiClassesDirty = false;
}

function clearAIClassSelection() {
    aiSelectedClasses = [];
    aiClassesDirty = false;
    var buttons = document.querySelectorAll('.ai-class-btn.selected');
    for (var i = 0; i < buttons.length; i++) {
        buttons[i].classList.remove('selected');
    }
    updateClassHint();
}
