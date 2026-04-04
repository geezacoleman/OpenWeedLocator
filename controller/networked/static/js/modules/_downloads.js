/* ==========================================================================
   Data Downloads — Scan, Transfer, Download, Delete
   Page: /downloads (separate from kiosk dashboard)
   ========================================================================== */

(function () {
    'use strict';

    // DOM refs
    var owlSelect = document.getElementById('owl-select');
    var scanBtn = document.getElementById('scan-btn');
    var sessionsList = document.getElementById('sessions-list');
    var filesList = document.getElementById('files-list');
    var filesHeader = document.getElementById('files-header');
    var refreshFilesBtn = document.getElementById('refresh-files-btn');

    var transferPolling = null;
    var selectedDeviceId = '';

    // -----------------------------------------------------------------------
    // OWL selector
    // -----------------------------------------------------------------------

    function loadOwls() {
        fetch('/api/owls')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var owls = data.owls || {};
                var ids = Object.keys(owls);

                owlSelect.innerHTML = '';
                if (ids.length === 0) {
                    owlSelect.innerHTML = '<option value="">No OWLs connected</option>';
                    scanBtn.disabled = true;
                    return;
                }

                owlSelect.innerHTML = '<option value="">Select an OWL...</option>';
                ids.forEach(function (id) {
                    var owl = owls[id];
                    var status = owl.connected ? 'online' : 'offline';
                    var opt = document.createElement('option');
                    opt.value = id;
                    opt.textContent = id + ' (' + status + ')';
                    opt.disabled = !owl.connected;
                    owlSelect.appendChild(opt);
                });
            })
            .catch(function () {
                owlSelect.innerHTML = '<option value="">Error loading OWLs</option>';
            });
    }

    // -----------------------------------------------------------------------
    // Sessions
    // -----------------------------------------------------------------------

    function scanSessions(deviceId) {
        if (!deviceId) return;
        selectedDeviceId = deviceId;

        sessionsList.innerHTML = '<div class="list-empty">Scanning...</div>';
        scanBtn.disabled = true;

        fetch('/api/downloads/sessions/' + encodeURIComponent(deviceId))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                // Wait a moment for the MQTT scan to complete, then re-fetch
                setTimeout(function () {
                    fetch('/api/downloads/sessions/' + encodeURIComponent(deviceId))
                        .then(function (r) { return r.json(); })
                        .then(function (data2) {
                            renderSessions(data2.sessions || []);
                            scanBtn.disabled = false;
                        });
                }, 2000);
            })
            .catch(function () {
                sessionsList.innerHTML = '<div class="list-empty">Error scanning sessions</div>';
                scanBtn.disabled = false;
            });
    }

    function formatSessionDate(dateStr, timeStr) {
        if (!dateStr || dateStr.length !== 8) return dateStr || '';

        var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        var y = dateStr.slice(0, 4);
        var m = parseInt(dateStr.slice(4, 6), 10);
        var d = parseInt(dateStr.slice(6, 8), 10);
        var result = d + ' ' + months[m - 1] + ' ' + y;

        if (timeStr && timeStr.length >= 4) {
            result += ', ' + timeStr.slice(0, 2) + ':' + timeStr.slice(2, 4);
        }
        return result;
    }

    function renderSessions(sessions) {
        if (sessions.length === 0) {
            sessionsList.textContent = '';
            var emptyDiv = document.createElement('div');
            emptyDiv.className = 'list-empty';
            emptyDiv.textContent = 'No data sessions found on this OWL';
            sessionsList.appendChild(emptyDiv);
            return;
        }

        // Build DOM elements safely — all user-facing strings go through textContent
        sessionsList.textContent = '';
        sessions.forEach(function (s) {
            var sizeStr = formatBytes(s.total_size || 0);
            var sid = s.session_id || s.date;
            var dateDisplay = formatSessionDate(s.date, s.time);

            var item = document.createElement('div');
            item.className = 'session-item';

            var info = document.createElement('div');
            info.className = 'session-info';

            var dateDiv = document.createElement('div');
            dateDiv.className = 'session-date';
            dateDiv.textContent = dateDisplay;
            info.appendChild(dateDiv);

            var meta = document.createElement('div');
            meta.className = 'session-meta';
            var imgSpan = document.createElement('span');
            imgSpan.textContent = s.image_count + ' images';
            meta.appendChild(imgSpan);
            var sizeSpan = document.createElement('span');
            sizeSpan.textContent = sizeStr;
            meta.appendChild(sizeSpan);
            info.appendChild(meta);

            item.appendChild(info);

            var actions = document.createElement('div');
            actions.className = 'session-actions';

            var transferBtn = document.createElement('button');
            transferBtn.className = 'btn-dl primary';
            transferBtn.textContent = 'Transfer';
            transferBtn.addEventListener('click', (function (id) {
                return function () { requestTransfer(id); };
            })(sid));
            actions.appendChild(transferBtn);

            var deleteBtn = document.createElement('button');
            deleteBtn.className = 'btn-dl danger';
            deleteBtn.textContent = 'Delete from OWL';
            deleteBtn.addEventListener('click', (function (id) {
                return function () { deleteRemote(id); };
            })(sid));
            actions.appendChild(deleteBtn);

            item.appendChild(actions);
            sessionsList.appendChild(item);
        });
    }

    // -----------------------------------------------------------------------
    // Transfer
    // -----------------------------------------------------------------------

    function requestTransfer(sessionId) {
        if (!selectedDeviceId) {
            showToast('No OWL selected', 'error');
            return;
        }

        fetch('/api/downloads/request', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                device_id: selectedDeviceId,
                session_id: sessionId,
            })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('Transfer started', 'success');
                    startTransferPolling(sessionDate);
                } else {
                    showToast(data.error || 'Transfer request failed', 'error');
                }
            })
            .catch(function () {
                showToast('Transfer request failed (network error)', 'error');
            });
    }

    function startTransferPolling(sessionDate) {
        stopTransferPolling();

        // Show progress bar in sessions list
        var progressHtml = '<div class="transfer-progress">'
            + '  <div class="transfer-progress-label">Transferring ' + escapeHtml(sessionDate) + '...</div>'
            + '  <div class="transfer-progress-track">'
            + '    <div class="transfer-progress-fill" id="transfer-fill"></div>'
            + '  </div>'
            + '  <div class="transfer-progress-text" id="transfer-text">Starting...</div>'
            + '</div>';

        // Prepend progress to sessions list
        sessionsList.insertAdjacentHTML('afterbegin', progressHtml);

        transferPolling = setInterval(function () {
            pollTransferProgress();
        }, 2000);
    }

    function stopTransferPolling() {
        if (transferPolling) {
            clearInterval(transferPolling);
            transferPolling = null;
        }
    }

    function pollTransferProgress() {
        if (!selectedDeviceId) return;

        fetch('/api/downloads/status/' + encodeURIComponent(selectedDeviceId))
            .then(function (r) { return r.json(); })
            .then(function (transfer) {
                var fillEl = document.getElementById('transfer-fill');
                var textEl = document.getElementById('transfer-text');
                if (!fillEl || !textEl) return;

                var status = transfer.status || 'idle';
                var progress = transfer.progress || 0;

                fillEl.style.width = progress + '%';

                if (status === 'scanning') {
                    textEl.textContent = 'Scanning files...';
                } else if (status === 'zipping') {
                    textEl.textContent = 'Creating ZIP... ' + progress + '%';
                } else if (status === 'uploading') {
                    textEl.textContent = 'Uploading... ' + progress + '%';
                } else if (status === 'complete') {
                    fillEl.style.width = '100%';
                    textEl.textContent = 'Transfer complete';
                    stopTransferPolling();
                    showToast('Transfer complete', 'success');
                    // Refresh files list
                    setTimeout(loadDownloadedFiles, 500);
                } else if (status === 'error') {
                    fillEl.classList.add('error');
                    textEl.textContent = 'Error: ' + (transfer.error || 'Unknown');
                    stopTransferPolling();
                    showToast('Transfer failed: ' + (transfer.error || 'Unknown'), 'error');
                }
            })
            .catch(function () {
                // Network error during poll — keep trying
            });
    }

    // -----------------------------------------------------------------------
    // Downloaded files
    // -----------------------------------------------------------------------

    function loadDownloadedFiles() {
        fetch('/api/downloads/files')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                renderFiles(data.files || []);
                renderStorageInfo(data.storage);
            })
            .catch(function () {
                filesList.innerHTML = '<div class="list-empty">Error loading files</div>';
            });
    }

    function renderFiles(files) {
        if (files.length === 0) {
            filesList.innerHTML = '<div class="list-empty">No files downloaded yet</div>';
            return;
        }

        var html = '';
        files.forEach(function (f) {
            var sizeStr = formatBytes(f.size);
            html += '<div class="file-item">'
                + '  <div class="file-info">'
                + '    <div class="file-name">' + escapeHtml(f.filename) + '</div>'
                + '    <div class="file-meta">' + sizeStr + '</div>'
                + '  </div>'
                + '  <div class="file-actions">'
                + '    <a class="btn-dl success" href="/api/downloads/file/' + encodeURIComponent(f.filename) + '">Download</a>'
                + '    <button class="btn-dl danger" onclick="removeFile(\'' + escapeAttr(f.filename) + '\')">Remove</button>'
                + '  </div>'
                + '</div>';
        });

        filesList.innerHTML = html;
    }

    function renderStorageInfo(storage) {
        if (!storage || !filesHeader) return;

        var usedStr = storage.used_mb < 1 ? '0' : Math.round(storage.used_mb);
        var maxStr = storage.max_mb >= 1000 ? (storage.max_mb / 1000).toFixed(0) + ' GB' : storage.max_mb + ' MB';
        filesHeader.textContent = 'Downloaded files (' + usedStr + ' MB / ' + maxStr + ' used)';
    }

    function removeFile(filename) {
        if (!confirm('Remove "' + filename + '" from controller? This cannot be undone.')) return;

        fetch('/api/downloads/file/' + encodeURIComponent(filename), { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('File removed: ' + filename, 'success');
                    loadDownloadedFiles();
                } else {
                    showToast(data.error || 'Remove failed', 'error');
                }
            })
            .catch(function () {
                showToast('Remove request failed', 'error');
            });
    }

    // -----------------------------------------------------------------------
    // Delete from OWL
    // -----------------------------------------------------------------------

    function deleteRemote(sessionId) {
        if (!selectedDeviceId) {
            showToast('No OWL selected', 'error');
            return;
        }

        if (!confirm('Delete session "' + sessionId + '" from OWL? This removes the images permanently.')) return;

        fetch('/api/downloads/delete-remote', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                device_id: selectedDeviceId,
                session_id: sessionId,
            })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('Delete command sent to OWL', 'success');
                    // Re-scan after a short delay
                    setTimeout(function () { scanSessions(selectedDeviceId); }, 3000);
                } else {
                    showToast(data.error || 'Delete failed', 'error');
                }
            })
            .catch(function () {
                showToast('Delete request failed', 'error');
            });
    }

    // -----------------------------------------------------------------------
    // Toast
    // -----------------------------------------------------------------------

    function showToast(message, type) {
        var toast = document.getElementById('downloads-toast');
        if (!toast) return;
        toast.textContent = message;
        toast.className = 'downloads-toast show ' + (type || '');
        clearTimeout(toast._timeout);
        toast._timeout = setTimeout(function () {
            toast.className = 'downloads-toast';
        }, 4000);
    }

    // -----------------------------------------------------------------------
    // Utilities
    // -----------------------------------------------------------------------

    function formatBytes(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
        return (bytes / 1073741824).toFixed(1) + ' GB';
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
    window.requestTransfer = requestTransfer;
    window.deleteRemote = deleteRemote;
    window.removeFile = removeFile;

    document.addEventListener('DOMContentLoaded', function () {
        loadOwls();
        loadDownloadedFiles();

        owlSelect.addEventListener('change', function () {
            scanBtn.disabled = !owlSelect.value;
            selectedDeviceId = owlSelect.value;
            if (!owlSelect.value) {
                sessionsList.innerHTML = '<div class="list-empty">Select an OWL and scan for sessions</div>';
            }
        });

        scanBtn.addEventListener('click', function () {
            scanSessions(owlSelect.value);
        });

        if (refreshFilesBtn) {
            refreshFilesBtn.addEventListener('click', loadDownloadedFiles);
        }
    });
})();
