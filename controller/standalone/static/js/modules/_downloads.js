/* ==========================================================================
   Data Downloads — Local session listing, ZIP download, delete
   Page: /downloads (separate from kiosk dashboard)
   Standalone version: direct filesystem access, no MQTT round-trips
   ========================================================================== */

(function () {
    'use strict';

    var sessionsList = document.getElementById('sessions-list');
    var summaryEl = document.getElementById('sessions-summary');
    var refreshBtn = document.getElementById('refresh-btn');
    var storageFill = document.getElementById('storage-fill');
    var storageText = document.getElementById('storage-text');
    var storageHeader = document.getElementById('storage-header');
    var storageWarning = document.getElementById('storage-warning');

    // -----------------------------------------------------------------------
    // Sessions
    // -----------------------------------------------------------------------

    function loadSessions() {
        sessionsList.textContent = '';
        var scanning = document.createElement('div');
        scanning.className = 'list-empty';
        scanning.textContent = 'Scanning...';
        sessionsList.appendChild(scanning);

        fetch('/api/downloads/sessions')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error) {
                    sessionsList.textContent = '';
                    var errDiv = document.createElement('div');
                    errDiv.className = 'list-empty';
                    errDiv.textContent = data.error;
                    sessionsList.appendChild(errDiv);
                } else {
                    renderSessions(data.sessions || []);
                }
                if (data.storage) {
                    renderStorage(data.storage);
                }
            })
            .catch(function () {
                sessionsList.textContent = '';
                var err = document.createElement('div');
                err.className = 'list-empty';
                err.textContent = 'Error loading sessions';
                sessionsList.appendChild(err);
            });
    }

    function renderSessions(sessions) {
        sessionsList.textContent = '';

        // Summary
        if (summaryEl) {
            if (sessions.length === 0) {
                summaryEl.textContent = '';
            } else {
                var totalImages = 0;
                var totalSize = 0;
                sessions.forEach(function (s) {
                    totalImages += s.image_count || 0;
                    totalSize += s.total_size || 0;
                });
                summaryEl.textContent = sessions.length + ' session' + (sessions.length !== 1 ? 's' : '')
                    + ', ' + totalImages + ' images, ' + formatBytes(totalSize);
            }
        }

        if (sessions.length === 0) {
            var empty = document.createElement('div');
            empty.className = 'list-empty';
            empty.textContent = 'No recording sessions found';
            sessionsList.appendChild(empty);
            return;
        }

        sessions.forEach(function (s) {
            var sizeStr = formatBytes(s.total_size || 0);
            var dateDisplay = formatSessionDate(s.date, s.time);
            var sid = s.session_id;
            var domId = sid.replace(/\//g, '-');

            var block = document.createElement('div');
            block.className = 'session-block';

            // Session row (clickable for preview)
            var item = document.createElement('div');
            item.className = 'session-item';
            item.addEventListener('click', (function (id) {
                return function () { togglePreview(id); };
            })(sid));

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
            var hint = document.createElement('span');
            hint.className = 'preview-hint';
            hint.textContent = 'tap to preview';
            meta.appendChild(hint);
            info.appendChild(meta);
            item.appendChild(info);

            var actions = document.createElement('div');
            actions.className = 'session-actions';

            var dlLink = document.createElement('a');
            dlLink.className = 'btn-dl success';
            dlLink.href = '/api/downloads/session/' + encodeURI(sid);
            dlLink.textContent = 'Download ZIP';
            dlLink.addEventListener('click', function (e) { e.stopPropagation(); });
            actions.appendChild(dlLink);

            var delBtn = document.createElement('button');
            delBtn.className = 'btn-dl danger';
            delBtn.textContent = 'Delete';
            delBtn.addEventListener('click', (function (id) {
                return function (e) { e.stopPropagation(); deleteSession(id); };
            })(sid));
            actions.appendChild(delBtn);

            item.appendChild(actions);
            block.appendChild(item);

            // Preview container
            var preview = document.createElement('div');
            preview.className = 'session-preview';
            preview.id = 'preview-' + domId;
            preview.style.display = 'none';
            block.appendChild(preview);

            sessionsList.appendChild(block);
        });
    }

    // -----------------------------------------------------------------------
    // Image preview
    // -----------------------------------------------------------------------

    function togglePreview(sessionId) {
        var domId = sessionId.replace(/\//g, '-');
        var previewEl = document.getElementById('preview-' + domId);
        if (!previewEl) return;

        if (previewEl.style.display !== 'none') {
            previewEl.style.display = 'none';
            return;
        }

        // Already loaded?
        if (previewEl.dataset.loaded) {
            previewEl.style.display = '';
            return;
        }

        previewEl.textContent = '';
        var loading = document.createElement('div');
        loading.className = 'list-empty';
        loading.textContent = 'Loading images...';
        previewEl.appendChild(loading);
        previewEl.style.display = '';

        fetch('/api/downloads/session/' + encodeURI(sessionId) + '/files')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var files = data.files || [];
                previewEl.textContent = '';

                if (files.length === 0) {
                    var empty = document.createElement('div');
                    empty.className = 'list-empty';
                    empty.textContent = 'No images';
                    previewEl.appendChild(empty);
                    return;
                }

                var grid = document.createElement('div');
                grid.className = 'thumb-grid';

                files.forEach(function (f) {
                    var src = '/api/downloads/file/' + encodeURI(sessionId) + '/' + encodeURIComponent(f.filename);
                    var link = document.createElement('a');
                    link.className = 'thumb-link';
                    link.href = src;
                    link.target = '_blank';

                    var img = document.createElement('img');
                    img.className = 'thumb-img';
                    img.src = src;
                    img.loading = 'lazy';
                    img.alt = f.filename;
                    link.appendChild(img);

                    var name = document.createElement('span');
                    name.className = 'thumb-name';
                    name.textContent = f.filename;
                    link.appendChild(name);

                    grid.appendChild(link);
                });

                previewEl.appendChild(grid);
                previewEl.dataset.loaded = '1';
            })
            .catch(function () {
                previewEl.textContent = '';
                var err = document.createElement('div');
                err.className = 'list-empty';
                err.textContent = 'Error loading images';
                previewEl.appendChild(err);
            });
    }

    function renderStorage(storage) {
        if (!storage) return;

        var usedGB = (storage.used_mb / 1024).toFixed(1);
        var totalGB = (storage.total_mb / 1024).toFixed(1);
        var freeGB = (storage.free_mb / 1024).toFixed(1);
        var percent = storage.total_mb > 0
            ? Math.round((storage.used_mb / storage.total_mb) * 100)
            : 0;

        if (storageText) {
            storageText.textContent = usedGB + ' GB used / ' + totalGB + ' GB total (' + freeGB + ' GB free)';
        }

        if (storageFill) {
            storageFill.style.width = percent + '%';
            storageFill.className = 'storage-bar-fill';
            if (percent >= 90) {
                storageFill.classList.add('critical');
            } else if (percent >= 75) {
                storageFill.classList.add('warning');
            }
        }

        if (storageHeader) {
            storageHeader.textContent = 'Storage (' + percent + '% used)';
        }

        if (storageWarning) {
            if (percent >= 90) {
                storageWarning.textContent = 'Storage nearly full - delete old sessions to free space';
                storageWarning.className = 'storage-warning critical';
            } else if (percent >= 75) {
                storageWarning.textContent = 'Storage running low';
                storageWarning.className = 'storage-warning warning';
            } else {
                storageWarning.textContent = '';
                storageWarning.className = 'storage-warning';
            }
        }
    }

    // -----------------------------------------------------------------------
    // Delete
    // -----------------------------------------------------------------------

    function deleteSession(sessionId) {
        if (!confirm('Delete session "' + sessionId + '"? This removes the images permanently.')) return;

        fetch('/api/downloads/session/' + encodeURI(sessionId), { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('Session deleted', 'success');
                    loadSessions();
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

    // -----------------------------------------------------------------------
    // Init
    // -----------------------------------------------------------------------

    window.deleteSession = deleteSession;
    window.togglePreview = togglePreview;

    document.addEventListener('DOMContentLoaded', function () {
        loadSessions();

        if (refreshBtn) {
            refreshBtn.addEventListener('click', loadSessions);
        }
    });
})();
