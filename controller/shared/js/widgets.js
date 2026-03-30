/* ==========================================================================
   Widget Loader and Runtime API

   Discovers installed widgets from the backend, injects their HTML into
   matching slot containers, and wires up builtin interactions (sliders,
   toggles, buttons, selects, stat displays, preset buttons).

   Custom widgets get their own sandboxed CSS and JS loaded dynamically.

   Load order: this module should be loaded BEFORE main.js so that
   initWidgets() is available in DOMContentLoaded.
   ========================================================================== */

/**
 * Public API exposed to custom widgets via window.OWLWidget.
 * Builtin widgets use the same helpers internally.
 */
const OWLWidget = {
    /** Return the latest cached system state (from stats polling). */
    getState() {
        return typeof lastSystemStats !== 'undefined' ? lastSystemStats : {};
    },

    /** Fetch a single config value from the backend. */
    async getConfig(section, key) {
        try {
            const resp = await fetch('/api/config');
            if (!resp.ok) return null;
            const data = await resp.json();
            if (data.success && data.config && data.config[section]) {
                return data.config[section][key] || null;
            }
        } catch (e) {
            console.error('OWLWidget.getConfig error:', e);
        }
        return null;
    },

    /** Send an MQTT command via the backend. */
    async sendCommand(action, params) {
        try {
            const body = Object.assign({ action: action }, params || {});
            const resp = await fetch('/api/config/param', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            return await resp.json();
        } catch (e) {
            console.error('OWLWidget.sendCommand error:', e);
            return { success: false, error: String(e) };
        }
    },

    /** Set a single config parameter via the backend. */
    async setParam(section, key, value) {
        try {
            var body = { param: key, value: value };
            if (section) body.section = section;
            const resp = await fetch('/api/config/param', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            return await resp.json();
        } catch (e) {
            console.error('OWLWidget.setParam error:', e);
            return { success: false, error: String(e) };
        }
    },

    /** Register a callback for state updates (fired on each stats poll). */
    _stateListeners: [],
    onStateUpdate(callback) {
        if (typeof callback === 'function') {
            OWLWidget._stateListeners.push(callback);
        }
    },

    /** Show a toast notification. */
    notify(title, msg, level) {
        if (typeof showQuickToast === 'function') {
            showQuickToast(msg || title, level || 'info');
        } else {
            console.log('[OWLWidget]', title, msg);
        }
    },

    /** Internal: broadcast state to registered listeners. */
    _broadcastState(state) {
        for (const cb of OWLWidget._stateListeners) {
            try { cb(state); } catch (e) { console.error('Widget state listener error:', e); }
        }
    },
};

window.OWLWidget = OWLWidget;


/**
 * Initialize the widget system.
 * Fetches the widget list from the backend and loads each one into its slot.
 */
async function initWidgets() {
    try {
        const resp = await fetch('/api/widgets');
        if (!resp.ok) return;
        const widgets = await resp.json();

        for (const widget of widgets) {
            // Skip already-loaded widgets (safe to re-call after agent creates one)
            if (document.querySelector('.widget-container[data-widget-id="' + widget.id + '"]')) {
                continue;
            }
            try {
                await loadWidget(widget);
            } catch (err) {
                console.error('Widget ' + widget.id + ' failed:', err);
                showWidgetError(widget);
            }
        }
    } catch (err) {
        console.error('Widget init failed:', err);
    }
}


/**
 * Load a single widget: fetch its rendered HTML, insert it into the
 * matching slot, and wire up interactions.
 */
async function loadWidget(widget) {
    var slot = document.querySelector('[data-widget-slot="' + widget.slot + '"]');
    if (!slot) return;

    var resp = await fetch('/api/widgets/' + encodeURIComponent(widget.id) + '/template');
    if (!resp.ok) throw new Error('Template fetch failed: ' + resp.status);
    var html = await resp.text();

    // The HTML already includes the widget-container wrapper from the backend
    var temp = document.createElement('div');
    temp.innerHTML = html;
    var container = temp.firstElementChild;
    if (!container) return;

    slot.appendChild(container);

    // Load custom CSS/JS for custom widgets
    if (widget.type === 'custom') {
        await loadWidgetStyle(widget.id);
        await loadWidgetScript(widget.id);
    }

    // Wire up builtin interactions
    wireBuiltinWidget(container, widget);
}


/**
 * Wire up event listeners for builtin widget types.
 */
function wireBuiltinWidget(container, widget) {
    // Delete buttons
    var deleteBtns = container.querySelectorAll('.widget-delete-btn');
    deleteBtns.forEach(function(btn) {
        btn.addEventListener('click', function() {
            var widgetId = btn.dataset.widgetId;
            if (!widgetId) return;
            fetch('/api/widgets/' + encodeURIComponent(widgetId), { method: 'DELETE' })
                .then(function(resp) {
                    if (resp.ok) {
                        // Remove from DOM
                        var el = container.closest('.widget-container') || container;
                        el.remove();
                    }
                })
                .catch(function(e) { console.error('Widget delete error:', e); });
        });
    });

    // Range sliders
    var sliders = container.querySelectorAll('.widget-range');
    sliders.forEach(function(slider) {
        var valueEl = slider.parentElement.querySelector('.widget-value');
        // Sync display on input
        slider.addEventListener('input', function() {
            if (valueEl) valueEl.textContent = slider.value;
        });
        // Send on change (after drag ends)
        slider.addEventListener('change', function() {
            var param = slider.dataset.param;
            if (param) {
                OWLWidget.setParam(slider.dataset.section || '', param, Number(slider.value));
            }
        });
    });

    // Toggles
    var toggles = container.querySelectorAll('.widget-toggle');
    toggles.forEach(function(toggle) {
        toggle.addEventListener('click', function() {
            var isActive = toggle.classList.toggle('active');
            var onLabel = toggle.dataset.onLabel || 'ON';
            var offLabel = toggle.dataset.offLabel || 'OFF';
            toggle.textContent = isActive ? onLabel : offLabel;

            var param = toggle.dataset.param;
            if (param) {
                OWLWidget.setParam(toggle.dataset.section || '', param, isActive ? 'true' : 'false');
            }
        });
    });

    // Buttons (MQTT command)
    var buttons = container.querySelectorAll('.widget-button');
    buttons.forEach(function(btn) {
        btn.addEventListener('click', function() {
            var cmdStr = btn.dataset.command;
            if (!cmdStr) return;
            try {
                var cmd = JSON.parse(cmdStr);
                var action = cmd.action || 'unknown';
                delete cmd.action;
                OWLWidget.sendCommand(action, cmd);
            } catch (e) {
                console.error('Widget button command parse error:', e);
            }
        });
    });

    // Selects
    var selects = container.querySelectorAll('.widget-select');
    selects.forEach(function(sel) {
        sel.addEventListener('change', function() {
            var action = sel.dataset.action;
            if (action) {
                OWLWidget.sendCommand(action, { value: sel.value });
            } else {
                var param = sel.dataset.param;
                if (param) {
                    OWLWidget.setParam(sel.dataset.section || '', param, sel.value);
                }
            }
        });
    });

    // Preset buttons
    var presetBtns = container.querySelectorAll('.widget-preset-button');
    presetBtns.forEach(function(btn) {
        btn.addEventListener('click', function() {
            var presetName = btn.dataset.presetName;
            if (presetName) {
                fetch('/api/sensitivity/set', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ level: presetName }),
                }).catch(function(e) { console.error('Preset apply error:', e); });
            }
        });
    });

    // Stat displays are updated via the state broadcast
    var statEls = container.querySelectorAll('.widget-stat-value[data-stat-key]');
    if (statEls.length > 0) {
        OWLWidget.onStateUpdate(function(state) {
            statEls.forEach(function(el) {
                var key = el.dataset.statKey;
                if (key && state[key] !== undefined) {
                    el.textContent = state[key];
                }
            });
        });
    }
}


/**
 * Load a custom widget's scoped CSS.
 */
async function loadWidgetStyle(widgetId) {
    try {
        var resp = await fetch('/api/widgets/' + encodeURIComponent(widgetId) + '/style');
        if (!resp.ok) return;
        var css = await resp.text();
        if (!css.trim()) return;

        var style = document.createElement('style');
        style.dataset.widgetId = widgetId;
        style.textContent = css;
        document.head.appendChild(style);
    } catch (e) {
        console.error('Widget style load error:', e);
    }
}


/**
 * Load a custom widget's IIFE-wrapped script.
 */
async function loadWidgetScript(widgetId) {
    return new Promise(function(resolve) {
        var script = document.createElement('script');
        script.src = '/api/widgets/' + encodeURIComponent(widgetId) + '/script';
        script.dataset.widgetId = widgetId;
        script.onload = resolve;
        script.onerror = function() {
            console.error('Widget script load error:', widgetId);
            resolve();
        };
        document.body.appendChild(script);
    });
}


/**
 * Show an error placeholder when a widget fails to load.
 */
function showWidgetError(widget) {
    var slot = document.querySelector('[data-widget-slot="' + widget.slot + '"]');
    if (!slot) return;

    var el = document.createElement('div');
    el.className = 'widget-container widget-error';
    el.dataset.widgetId = widget.id;
    el.textContent = 'Widget error: ' + (widget.name || widget.id);
    slot.appendChild(el);
}
