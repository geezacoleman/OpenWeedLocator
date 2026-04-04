/* ==========================================================================
   Model Manager — Upload, Library, Deploy
   Page: /models (separate from kiosk dashboard)
   ========================================================================== */

(function () {
    'use strict';

    // DOM refs
    const uploadZone = document.getElementById('upload-zone');
    const uploadInput = document.getElementById('upload-input');
    const progressContainer = document.getElementById('upload-progress');
    const progressFill = document.getElementById('upload-progress-fill');
    const progressText = document.getElementById('upload-progress-text');
    const modelList = document.getElementById('model-list');
    const refreshBtn = document.getElementById('refresh-models-btn');

    // Deploy modal refs
    let deployModal = null;
    let deployModelName = '';
    let deployPollingInterval = null;

    // -----------------------------------------------------------------------
    // Upload
    // -----------------------------------------------------------------------

    function initUpload() {
        if (!uploadZone || !uploadInput) return;

        uploadZone.addEventListener('dragover', function (e) {
            e.preventDefault();
            uploadZone.classList.add('drag-over');
        });

        uploadZone.addEventListener('dragleave', function () {
            uploadZone.classList.remove('drag-over');
        });

        uploadZone.addEventListener('drop', function (e) {
            e.preventDefault();
            uploadZone.classList.remove('drag-over');
            if (e.dataTransfer.files.length > 0) {
                uploadFile(e.dataTransfer.files[0]);
            }
        });

        uploadInput.addEventListener('change', function () {
            if (uploadInput.files.length > 0) {
                uploadFile(uploadInput.files[0]);
            }
        });
    }

    function uploadFile(file) {
        var ext = file.name.split('.').pop().toLowerCase();
        if (ext !== 'pt' && ext !== 'zip') {
            showToast('Only .pt and .zip files are allowed', 'error');
            return;
        }

        var formData = new FormData();
        formData.append('file', file);

        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/models/upload', true);

        // Show progress
        progressContainer.classList.add('active');
        progressFill.style.width = '0%';
        progressText.textContent = 'Uploading ' + file.name + '...';

        xhr.upload.onprogress = function (e) {
            if (e.lengthComputable) {
                var pct = Math.round(e.loaded / e.total * 100);
                progressFill.style.width = pct + '%';
                progressText.textContent = 'Uploading ' + file.name + '... ' + pct + '%';
            }
        };

        xhr.onload = function () {
            if (xhr.status === 200) {
                var result = JSON.parse(xhr.responseText);
                if (result.success) {
                    progressFill.style.width = '100%';
                    progressText.textContent = 'Upload complete: ' + result.filename;
                    showToast('Model uploaded: ' + result.filename, 'success');
                    setTimeout(function () {
                        progressContainer.classList.remove('active');
                    }, 2000);
                    refreshModelList();
                } else {
                    progressText.textContent = 'Error: ' + result.error;
                    showToast(result.error, 'error');
                }
            } else {
                var msg = 'Upload failed';
                try {
                    msg = JSON.parse(xhr.responseText).error || msg;
                } catch (e) { /* ignore */ }

                // Handle 413 (Request Entity Too Large) explicitly
                if (xhr.status === 413) {
                    msg = 'File too large. Maximum size is 200MB.';
                }

                progressText.textContent = 'Error: ' + msg;
                showToast(msg, 'error');
            }
            uploadInput.value = '';
        };

        xhr.onerror = function () {
            progressText.textContent = 'Upload failed (network error)';
            showToast('Upload failed (network error)', 'error');
            uploadInput.value = '';
        };

        xhr.send(formData);
    }

    // -----------------------------------------------------------------------
    // Model Library
    // -----------------------------------------------------------------------

    function refreshModelList() {
        fetch('/api/models')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                renderModelList(data.models || []);
            })
            .catch(function (err) {
                modelList.innerHTML = '<div class="model-list-empty">Error loading models</div>';
            });
    }

    function renderModelList(models) {
        if (!modelList) return;

        if (models.length === 0) {
            modelList.innerHTML = '<div class="model-list-empty">No models in library. Upload a .pt or .zip file above.</div>';
            return;
        }

        var html = '';
        models.forEach(function (m) {
            var sizeStr = formatBytes(m.size);
            var deployed = m.deployed_to && m.deployed_to.length > 0;
            var deployedStr = deployed
                ? '<div class="model-deployed">Deployed to: ' + m.deployed_to.join(', ') + '</div>'
                : '<div class="model-not-deployed">Not deployed</div>';

            html += '<div class="model-item">'
                + '  <div class="model-info">'
                + '    <div class="model-name">' + escapeHtml(m.name) + '</div>'
                + '    <div class="model-meta">'
                + '      <span class="model-badge ' + m.type + '">' + m.type + '</span>'
                + '      <span>' + sizeStr + '</span>'
                + '    </div>'
                + '    ' + deployedStr
                + '  </div>'
                + '  <div class="model-actions">'
                + '    <button class="btn-model deploy" onclick="openDeployModal(\'' + escapeAttr(m.name) + '\')">Deploy</button>'
                + '    <button class="btn-model danger" onclick="deleteModel(\'' + escapeAttr(m.name) + '\')">Delete</button>'
                + '  </div>'
                + '</div>';
        });

        modelList.innerHTML = html;
    }

    // -----------------------------------------------------------------------
    // Deploy Modal
    // -----------------------------------------------------------------------

    function openDeployModal(modelName) {
        deployModelName = modelName;

        // Fetch OWL list
        fetch('/api/owls')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                showDeployModal(modelName, data.owls || {});
            })
            .catch(function () {
                showToast('Failed to fetch OWL list', 'error');
            });
    }

    function showDeployModal(modelName, owls) {
        // Remove existing
        closeDeployModal();

        var owlIds = Object.keys(owls);

        var backdrop = document.createElement('div');
        backdrop.className = 'deploy-backdrop';
        backdrop.id = 'deploy-backdrop';
        backdrop.onclick = function (e) {
            if (e.target === backdrop) closeDeployModal();
        };

        var modalHtml = '<div class="deploy-modal" id="deploy-modal-inner">'
            + '<div class="deploy-modal-header">Deploy ' + escapeHtml(modelName) + '</div>'
            + '<div class="deploy-modal-body" id="deploy-modal-body">';

        if (owlIds.length === 0) {
            modalHtml += '<div class="deploy-no-owls">No OWLs connected</div>';
        } else {
            owlIds.forEach(function (id) {
                var owl = owls[id];
                var connected = owl.connected;
                var statusClass = connected ? 'online' : 'offline';
                var statusText = connected ? 'online' : 'offline';
                var checked = connected ? 'checked' : '';
                var disabled = connected ? '' : 'disabled';

                modalHtml += '<div class="deploy-owl-item">'
                    + '  <label>'
                    + '    <input type="checkbox" value="' + escapeAttr(id) + '" ' + checked + ' ' + disabled + ' />'
                    + '    ' + escapeHtml(id)
                    + '  </label>'
                    + '  <span class="deploy-owl-status ' + statusClass + '">' + statusText + '</span>'
                    + '</div>';
            });
        }

        modalHtml += '</div>'
            + '<div class="deploy-modal-footer" id="deploy-modal-footer">'
            + '  <button class="btn-model" onclick="closeDeployModal()">Cancel</button>'
            + '  <button class="btn-model deploy" id="deploy-confirm-btn" onclick="confirmDeploy()">Deploy</button>'
            + '</div>'
            + '</div>';

        backdrop.innerHTML = modalHtml;
        document.body.appendChild(backdrop);
        deployModal = backdrop;
    }

    function confirmDeploy() {
        if (!deployModal) return;

        var checkboxes = deployModal.querySelectorAll('input[type="checkbox"]:checked');
        var deviceIds = [];
        checkboxes.forEach(function (cb) { deviceIds.push(cb.value); });

        if (deviceIds.length === 0) {
            showToast('Select at least one OWL', 'error');
            return;
        }

        // Disable confirm button
        var btn = document.getElementById('deploy-confirm-btn');
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Deploying...';
        }

        fetch('/api/models/deploy', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model_name: deployModelName,
                device_ids: deviceIds
            })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('Deploy command sent to ' + data.sent_to.length + ' OWLs', 'success');
                    // Switch to progress view
                    showDeployProgress(deviceIds);
                } else {
                    showToast(data.error || 'Deploy failed', 'error');
                    if (btn) { btn.disabled = false; btn.textContent = 'Deploy'; }
                }
            })
            .catch(function () {
                showToast('Deploy request failed', 'error');
                if (btn) { btn.disabled = false; btn.textContent = 'Deploy'; }
            });
    }

    function showDeployProgress(deviceIds) {
        var body = document.getElementById('deploy-modal-body');
        var footer = document.getElementById('deploy-modal-footer');
        if (!body) return;

        var html = '';
        deviceIds.forEach(function (id) {
            html += '<div class="deploy-progress-item" data-owl-id="' + escapeAttr(id) + '">'
                + '  <span class="deploy-progress-name">' + escapeHtml(id) + '</span>'
                + '  <div class="deploy-progress-bar"><div class="deploy-progress-fill" id="dp-fill-' + escapeAttr(id) + '"></div></div>'
                + '  <span class="deploy-progress-pct" id="dp-pct-' + escapeAttr(id) + '">0%</span>'
                + '</div>';
        });
        body.innerHTML = html;

        if (footer) {
            footer.innerHTML = '<button class="btn-model" onclick="closeDeployModal()">Close</button>';
        }

        // Start polling
        deployPollingInterval = setInterval(function () {
            pollDeployProgress(deviceIds);
        }, 2000);
    }

    function pollDeployProgress(deviceIds) {
        fetch('/api/owls')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var owls = data.owls || {};
                var allDone = true;

                deviceIds.forEach(function (id) {
                    var owl = owls[id];
                    var dl = owl ? (owl.model_download || {}) : {};
                    var status = dl.status || 'idle';
                    var progress = dl.progress || 0;
                    var error = dl.error || '';

                    var fillEl = document.getElementById('dp-fill-' + id);
                    var pctEl = document.getElementById('dp-pct-' + id);

                    if (fillEl) fillEl.style.width = progress + '%';

                    if (status === 'error') {
                        if (fillEl) fillEl.classList.add('error');
                        if (pctEl) {
                            pctEl.textContent = 'Error';
                            pctEl.classList.add('error');
                        }
                    } else if (status === 'complete') {
                        if (pctEl) pctEl.textContent = '100%';
                        if (fillEl) fillEl.style.width = '100%';
                    } else {
                        if (pctEl) pctEl.textContent = progress + '%';
                        allDone = false;
                    }
                });

                if (allDone && deployPollingInterval) {
                    clearInterval(deployPollingInterval);
                    deployPollingInterval = null;
                    refreshModelList();
                }
            })
            .catch(function () {
                // Network error during poll — keep trying
            });
    }

    function closeDeployModal() {
        if (deployPollingInterval) {
            clearInterval(deployPollingInterval);
            deployPollingInterval = null;
        }
        var backdrop = document.getElementById('deploy-backdrop');
        if (backdrop) backdrop.remove();
        deployModal = null;
    }

    // -----------------------------------------------------------------------
    // Delete
    // -----------------------------------------------------------------------

    function deleteModel(name) {
        if (!confirm('Delete model "' + name + '"? This cannot be undone.')) return;

        fetch('/api/models/' + encodeURIComponent(name), { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('Model deleted: ' + name, 'success');
                    refreshModelList();
                } else {
                    showToast(data.error || 'Delete failed', 'error');
                }
            })
            .catch(function () {
                showToast('Delete request failed', 'error');
            });
    }

    // -----------------------------------------------------------------------
    // Toast (simple inline — not using shared toast since this is a standalone page)
    // -----------------------------------------------------------------------

    function showToast(message, type) {
        var toast = document.getElementById('models-toast');
        if (!toast) return;
        toast.textContent = message;
        toast.className = 'models-toast show ' + (type || '');
        clearTimeout(toast._timeout);
        toast._timeout = setTimeout(function () {
            toast.className = 'models-toast';
        }, 4000);
    }

    // -----------------------------------------------------------------------
    // Utilities
    // -----------------------------------------------------------------------

    function formatBytes(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return str.replace(/'/g, "\\'").replace(/"/g, '&quot;');
    }

    // -----------------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------------

    // Expose to global scope for onclick handlers
    window.openDeployModal = openDeployModal;
    window.closeDeployModal = closeDeployModal;
    window.confirmDeploy = confirmDeploy;
    window.deleteModel = deleteModel;

    document.addEventListener('DOMContentLoaded', function () {
        initUpload();
        refreshModelList();

        if (refreshBtn) {
            refreshBtn.addEventListener('click', refreshModelList);
        }
    });
})();
