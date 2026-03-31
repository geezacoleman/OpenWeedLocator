/* ==========================================================================
   Data Downloads — Local session listing, ZIP download, delete
   Page: /downloads (separate from kiosk dashboard)
   Standalone version: direct filesystem access, no MQTT round-trips
   ========================================================================== */

(function () {
    'use strict';

    var sessionsList = document.getElementById('sessions-list');
    var refreshBtn = document.getElementById('refresh-btn');
    var storageFill = document.getElementById('storage-fill');
    var storageText = document.getElementById('storage-text');
    var storageHeader = document.getElementById('storage-header');

    // -----------------------------------------------------------------------
    // Sessions
    // -----------------------------------------------------------------------

    function loadSessions() {
        sessionsList.innerHTML = '<div class="list-empty">Scanning...</div>';

        fetch('/api/downloads/sessions')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                renderSessions(data.sessions || []);
                if (data.storage) {
                    renderStorage(data.storage);
                }
            })
            .catch(function () {
                sessionsList.innerHTML = '<div class="list-empty">Error loading sessions</div>';
            });
    }

    function renderSessions(sessions) {
        if (sessions.length === 0) {
            sessionsList.innerHTML = '<div class="list-empty">No recording sessions found</div>';
            return;
        }

        var html = '';
        sessions.forEach(function (s) {
            var sizeStr = formatBytes(s.total_size || 0);
            var dateStr = formatDate(s.date);

            html += '<div class="session-block" data-date="' + escapeAttr(s.date) + '">'
                + '<div class="session-item" onclick="togglePreview(\'' + escapeAttr(s.date) + '\')">'
                + '  <div class="session-info">'
                + '    <div class="session-date">' + escapeHtml(dateStr) + '</div>'
                + '    <div class="session-meta">'
                + '      <span>' + s.image_count + ' images</span>'
                + '      <span>' + sizeStr + '</span>'
                + '      <span class="preview-hint">tap to preview</span>'
                + '    </div>'
                + '  </div>'
                + '  <div class="session-actions">'
                + '    <a class="btn-dl success" href="/api/downloads/session/' + escapeAttr(s.date) + '" onclick="event.stopPropagation()">Download ZIP</a>'
                + '    <button class="btn-dl danger" onclick="event.stopPropagation(); deleteSession(\'' + escapeAttr(s.date) + '\')">Delete</button>'
                + '  </div>'
                + '</div>'
                + '<div class="session-preview" id="preview-' + escapeAttr(s.date) + '" style="display:none;"></div>'
                + '</div>';
        });

        sessionsList.innerHTML = html;
    }

    // -----------------------------------------------------------------------
    // Image preview
    // -----------------------------------------------------------------------

    function togglePreview(date) {
        var previewEl = document.getElementById('preview-' + date);
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

        previewEl.innerHTML = '<div class="list-empty">Loading images...</div>';
        previewEl.style.display = '';

        fetch('/api/downloads/session/' + encodeURIComponent(date) + '/files')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var files = data.files || [];
                if (files.length === 0) {
                    previewEl.innerHTML = '<div class="list-empty">No images</div>';
                    return;
                }

                var html = '<div class="thumb-grid">';
                files.forEach(function (f) {
                    var src = '/api/downloads/file/' + encodeURIComponent(date) + '/' + encodeURIComponent(f.filename);
                    html += '<a class="thumb-link" href="' + src + '" target="_blank">'
                        + '<img class="thumb-img" src="' + src + '" loading="lazy" alt="' + escapeAttr(f.filename) + '">'
                        + '<span class="thumb-name">' + escapeHtml(f.filename) + '</span>'
                        + '</a>';
                });
                html += '</div>';

                previewEl.innerHTML = html;
                previewEl.dataset.loaded = '1';
            })
            .catch(function () {
                previewEl.innerHTML = '<div class="list-empty">Error loading images</div>';
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
    }

    // -----------------------------------------------------------------------
    // Delete
    // -----------------------------------------------------------------------

    function deleteSession(sessionDate) {
        if (!confirm('Delete session "' + sessionDate + '"? This removes the images permanently.')) return;

        fetch('/api/downloads/session/' + encodeURIComponent(sessionDate), { method: 'DELETE' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    showToast('Session deleted: ' + sessionDate, 'success');
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

    function formatDate(dateStr) {
        // Convert YYYYMMDD to YYYY-MM-DD
        if (dateStr && dateStr.length === 8) {
            return dateStr.slice(0, 4) + '-' + dateStr.slice(4, 6) + '-' + dateStr.slice(6, 8);
        }
        return dateStr;
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

    window.deleteSession = deleteSession;
    window.togglePreview = togglePreview;

    document.addEventListener('DOMContentLoaded', function () {
        loadSessions();

        if (refreshBtn) {
            refreshBtn.addEventListener('click', loadSessions);
        }
    });
})();
