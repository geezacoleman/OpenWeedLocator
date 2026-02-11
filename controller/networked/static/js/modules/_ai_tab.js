// ============================================
// OWL Central Controller - AI Tab
// Model selection and class filtering
// ============================================

var aiTabActive = false;
var aiSelectedClasses = [];
var aiModelClasses = {};
var aiAvailableModels = [];
var aiCurrentModel = '';
var aiClassesDirty = false;  // true when user has toggled buttons but not yet applied
var aiLastModelKey = '';      // tracks model changes to force grid rebuild

// ============================================
// TAB SWITCHING
// ============================================

function switchToAITab() {
    document.getElementById('tab-ai').classList.add('active');
    document.getElementById('tab-owls').classList.remove('active');
    document.getElementById('tab-gps').classList.remove('active');
    document.getElementById('tab-config').classList.remove('active');
    document.getElementById('view-ai').style.display = '';
    document.getElementById('view-owls').style.display = 'none';
    document.getElementById('view-gps').style.display = 'none';
    document.getElementById('view-config').style.display = 'none';
    aiTabActive = true;
    stopGPSPolling();
    stopConfigPreview();
    refreshAITab();
}

// ============================================
// REFRESH FROM OWL DATA
// ============================================

function refreshAITab() {
    var firstOwl = null;
    for (var id in owlsData) {
        if (owlsData[id] && owlsData[id].connected) {
            firstOwl = owlsData[id];
            break;
        }
    }

    var banner = document.getElementById('ai-status-banner');
    var statusText = document.getElementById('ai-status-text');

    if (!firstOwl) {
        banner.className = 'ai-status-banner disconnected';
        statusText.textContent = 'No OWLs Connected';
        // Clear stale data from previous connection
        aiAvailableModels = [];
        aiCurrentModel = '';
        aiModelClasses = {};
        aiSelectedClasses = [];
        aiClassesDirty = false;
        aiLastModelKey = '';
        document.getElementById('ai-model-select').innerHTML = '<option value="">No models available</option>';
        document.getElementById('ai-model-info').innerHTML = '';
        document.getElementById('ai-classes-grid').innerHTML =
            '<span style="color:var(--text-light);">Connect an OWL to see classes</span>';
        updateClassHint();
        return;
    }

    // Extract AI data from OWL state
    aiAvailableModels = firstOwl.available_models || [];
    aiCurrentModel = firstOwl.current_model || '';
    aiModelClasses = firstOwl.model_classes || {};
    var existingSelection = firstOwl.detect_classes || [];

    var algorithm = firstOwl.algorithm || 'exhsv';
    var isAIMode = (algorithm === 'gog' || algorithm === 'gog-hybrid');
    var hasModel = aiCurrentModel !== '';

    if (!isAIMode) {
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

// ============================================
// CLASS GRID
// ============================================

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

    // Normalise existing selection to lowercase for matching
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

    // Rebuild selected list from existing selection
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

// ============================================
// ACTIONS
// ============================================

function onModelSelected() {
    var select = document.getElementById('ai-model-select');
    if (!select || !select.value) return;

    sendCommand('all', 'set_model', select.value);
    showToast('Switching model to ' + select.value + '...', 'info');

    // Clear class selection — new model may have different classes
    clearAIClassSelection();
}

function applyAIClassSelection() {
    sendCommand('all', 'set_detect_classes', aiSelectedClasses);
    aiClassesDirty = false;

    if (aiSelectedClasses.length === 0) {
        showToast('Detecting all classes', 'success');
    } else {
        showToast('Detecting: ' + aiSelectedClasses.join(', '), 'success');
    }
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

// ============================================
// DASHBOARD SYNC
// ============================================

function syncAITabFromDashboard() {
    if (!aiTabActive) return;
    refreshAITab();
}
