/* ==========================================================================
   Demo Page JS — self-contained, zero dashboard dependencies
   Polls /api/owls + /api/actuation every 5 seconds
   ========================================================================== */
(function () {
    'use strict';

    var POLL_INTERVAL = 5000;
    var grid = document.getElementById('demo-grid');
    var statOwls = document.getElementById('stat-owls');
    var statSpeed = document.getElementById('stat-speed');
    var statDetecting = document.getElementById('stat-detecting');

    // Track which cards exist so we can add/remove
    var currentCards = {};

    function formatDeviceName(id) {
        // "owl-vegowl-1" -> "VegOWL 1"
        var parts = id.replace(/^owl-/, '').split('-');
        return parts.map(function (p) { return p.charAt(0).toUpperCase() + p.slice(1); }).join(' ');
    }

    function loadSnapshot(img, deviceId) {
        fetch('/api/snapshot/' + deviceId + '?t=' + Date.now())
            .then(function (r) { return r.ok ? r.blob() : null; })
            .then(function (blob) {
                if (!blob) return;
                var prev = img.dataset.blobUrl;
                if (prev) URL.revokeObjectURL(prev);
                var url = URL.createObjectURL(blob);
                img.dataset.blobUrl = url;
                img.src = url;
            })
            .catch(function () { /* snapshot unavailable */ });
    }

    function createCameraCard(deviceId, state) {
        var card = document.createElement('div');
        card.className = 'demo-camera-card';
        card.dataset.deviceId = deviceId;

        var img = document.createElement('img');
        img.className = 'demo-camera-img';
        img.alt = deviceId;
        loadSnapshot(img, deviceId);

        var info = document.createElement('div');
        info.className = 'demo-camera-info';

        var left = document.createElement('div');
        var name = document.createElement('div');
        name.className = 'demo-camera-name';
        name.textContent = formatDeviceName(deviceId);
        var meta = document.createElement('div');
        meta.className = 'demo-camera-meta';
        meta.textContent = buildMeta(state);
        left.appendChild(name);
        left.appendChild(meta);

        var badge = document.createElement('span');
        badge.className = 'demo-badge ' + (state.detection_enable ? 'detecting' : 'idle');
        badge.textContent = state.detection_enable ? 'Detecting' : 'Idle';

        info.appendChild(left);
        info.appendChild(badge);
        card.appendChild(img);
        card.appendChild(info);

        return card;
    }

    function buildMeta(state) {
        var parts = [];
        var lt = state.avg_loop_time_ms;
        if (lt && lt > 0) parts.push(Math.round(lt) + ' ms/frame');
        var algo = state.algorithm;
        if (algo) parts.push(algo.toUpperCase());
        return parts.join(' | ') || '';
    }

    function updateCard(card, deviceId, state) {
        // Update snapshot via fetch+blob (bypasses browser cache)
        var img = card.querySelector('.demo-camera-img');
        if (img) loadSnapshot(img, deviceId);

        // Update meta text
        var meta = card.querySelector('.demo-camera-meta');
        if (meta) meta.textContent = buildMeta(state);

        // Update badge
        var badge = card.querySelector('.demo-badge');
        if (badge) {
            badge.className = 'demo-badge ' + (state.detection_enable ? 'detecting' : 'idle');
            badge.textContent = state.detection_enable ? 'Detecting' : 'Idle';
        }
    }

    function renderOwls(owls) {
        var owlIds = Object.keys(owls);

        if (owlIds.length === 0) {
            grid.innerHTML = '<div class="demo-empty">No OWL units connected</div>';
            currentCards = {};
            return;
        }

        // Remove cards for disconnected OWLs
        Object.keys(currentCards).forEach(function (id) {
            if (!owls[id]) {
                var card = currentCards[id];
                if (card && card.parentNode) card.parentNode.removeChild(card);
                delete currentCards[id];
            }
        });

        // Clear empty-state message if present
        var empty = grid.querySelector('.demo-empty');
        if (empty) empty.remove();

        // Add or update cards
        owlIds.forEach(function (id) {
            if (currentCards[id]) {
                updateCard(currentCards[id], id, owls[id]);
            } else {
                var card = createCameraCard(id, owls[id]);
                grid.appendChild(card);
                currentCards[id] = card;
            }
        });
    }

    function updateStats(owls, actuation) {
        var owlIds = Object.keys(owls);
        var detectingCount = owlIds.filter(function (id) {
            return owls[id].detection_enable;
        }).length;

        statOwls.textContent = owlIds.length;
        statDetecting.textContent = detectingCount;

        // Speed from actuation endpoint
        var speed = actuation && actuation.speed_kmh;
        if (speed != null && speed > 0) {
            statSpeed.textContent = speed.toFixed(1) + ' km/h';
        } else {
            statSpeed.textContent = '--';
        }
    }

    function poll() {
        Promise.all([
            fetch('/api/owls').then(function (r) { return r.json(); }),
            fetch('/api/actuation').then(function (r) { return r.json(); })
        ]).then(function (results) {
            var owlsData = results[0];
            var actuationData = results[1];
            var owls = owlsData.owls || {};
            renderOwls(owls);
            updateStats(owls, actuationData);
        }).catch(function (err) {
            console.warn('Demo poll error:', err);
        });
    }

    // Initial poll + interval
    poll();
    setInterval(poll, POLL_INTERVAL);
})();
