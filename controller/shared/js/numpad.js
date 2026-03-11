/**
 * On-screen numpad + keyboard for kiosk input (no physical keyboard).
 *
 * Number inputs  → 3x4 numpad grid (digits + dot)
 * Text inputs    → QWERTY keyboard + number row + special chars
 *
 * Physical keyboard works alongside when overlay is open (desktop use).
 *
 * Auto-attaches to <input type="number"> and <input type="text"> inside
 * .config-section-body.  Call `Numpad.init()` once on DOMContentLoaded.
 *
 * Usage from other modules:
 *   Numpad.open(inputEl)   — programmatic open
 *   Numpad.close()         — dismiss without applying
 */

const Numpad = (() => {
    let overlay = null;
    let panel = null;
    let display = null;
    let label = null;
    let dotBtn = null;
    let numpadGrid = null;
    let keyboardGrid = null;
    let activeInput = null;
    let value = '';
    let _isOpen = false;

    // ── Build DOM ──────────────────────────────────────────────

    function _build() {
        overlay = document.createElement('div');
        overlay.className = 'numpad-overlay';
        overlay.style.display = 'none';

        overlay.innerHTML =
            '<div class="numpad-panel">' +
                '<div class="numpad-header">' +
                    '<div class="numpad-label"></div>' +
                    '<div class="numpad-display">0</div>' +
                '</div>' +
                // ── Numpad grid (number inputs) ──
                '<div class="numpad-grid">' +
                    '<button class="numpad-btn" data-val="7">7</button>' +
                    '<button class="numpad-btn" data-val="8">8</button>' +
                    '<button class="numpad-btn" data-val="9">9</button>' +
                    '<button class="numpad-btn" data-val="4">4</button>' +
                    '<button class="numpad-btn" data-val="5">5</button>' +
                    '<button class="numpad-btn" data-val="6">6</button>' +
                    '<button class="numpad-btn" data-val="1">1</button>' +
                    '<button class="numpad-btn" data-val="2">2</button>' +
                    '<button class="numpad-btn" data-val="3">3</button>' +
                    '<button class="numpad-btn numpad-btn--dot" data-val=".">.</button>' +
                    '<button class="numpad-btn" data-val="0">0</button>' +
                    '<button class="numpad-btn numpad-btn--backspace" data-action="backspace">&larr;</button>' +
                '</div>' +
                // ── Keyboard grid (text inputs) ──
                '<div class="keyboard-grid">' +
                    _buildKeyboardRows() +
                '</div>' +
                // ── Shared actions ──
                '<div class="numpad-actions">' +
                    '<button class="numpad-cancel">Cancel</button>' +
                    '<button class="numpad-ok">OK</button>' +
                '</div>' +
            '</div>';

        document.body.appendChild(overlay);

        panel = overlay.querySelector('.numpad-panel');
        display = overlay.querySelector('.numpad-display');
        label = overlay.querySelector('.numpad-label');
        dotBtn = overlay.querySelector('.numpad-btn--dot');
        numpadGrid = overlay.querySelector('.numpad-grid');
        keyboardGrid = overlay.querySelector('.keyboard-grid');

        // Event delegation
        numpadGrid.addEventListener('click', _onBtnClick);
        keyboardGrid.addEventListener('click', _onBtnClick);
        overlay.querySelector('.numpad-cancel').addEventListener('click', close);
        overlay.querySelector('.numpad-ok').addEventListener('click', _onOk);

        // Backdrop dismiss
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) close();
        });
    }

    function _buildKeyboardRows() {
        var rows = [
            { keys: '1234567890'.split(''), cls: '' },
            { keys: 'qwertyuiop'.split(''), cls: '' },
            { keys: 'asdfghjkl'.split(''), cls: 'keyboard-row--offset' },
            { keys: 'zxcvbnm'.split(''), cls: 'keyboard-row--offset2',
              extra: '<button class="kb-btn kb-btn--wide" data-action="backspace">&larr;</button>' },
            { special: true }
        ];

        var html = '';
        for (var r = 0; r < rows.length; r++) {
            var row = rows[r];
            if (row.special) {
                html += '<div class="keyboard-row">' +
                    '<button class="kb-btn" data-val=".">.</button>' +
                    '<button class="kb-btn" data-val="/">/</button>' +
                    '<button class="kb-btn" data-val="_">_</button>' +
                    '<button class="kb-btn" data-val="-">-</button>' +
                    '<button class="kb-btn" data-val=",">,</button>' +
                    '<button class="kb-btn kb-btn--space" data-val=" ">space</button>' +
                    '<button class="kb-btn kb-btn--wide" data-action="clear">CLR</button>' +
                    '</div>';
            } else {
                html += '<div class="keyboard-row ' + (row.cls || '') + '">';
                for (var k = 0; k < row.keys.length; k++) {
                    html += '<button class="kb-btn" data-val="' + row.keys[k] + '">' + row.keys[k] + '</button>';
                }
                if (row.extra) html += row.extra;
                html += '</div>';
            }
        }
        return html;
    }

    // ── Input handling ─────────────────────────────────────────

    function _onBtnClick(e) {
        var btn = e.target.closest('.numpad-btn, .kb-btn');
        if (!btn) return;

        var action = btn.dataset.action;
        if (action === 'backspace') {
            value = value.slice(0, -1);
        } else if (action === 'clear') {
            value = '';
        } else {
            var ch = btn.dataset.val;
            if (ch === undefined) return;
            // Numpad mode: prevent double dots
            if (ch === '.' && value.includes('.') && activeInput && activeInput.type === 'number') return;
            // Prevent leading zeros for numbers (except "0.")
            if (activeInput && activeInput.type === 'number' && value === '0' && ch !== '.') {
                value = ch;
            } else {
                value += ch;
            }
        }
        _updateDisplay();
    }

    function _onKeyDown(e) {
        if (!_isOpen) return;

        if (e.key === 'Enter') { e.preventDefault(); _onOk(); return; }
        if (e.key === 'Escape') { e.preventDefault(); close(); return; }
        if (e.key === 'Backspace') {
            e.preventDefault();
            value = value.slice(0, -1);
            _updateDisplay();
            return;
        }
        // Ignore modifier combos (Ctrl+C, Alt+Tab, etc.)
        if (e.ctrlKey || e.altKey || e.metaKey) return;

        // Single printable character
        if (e.key.length === 1) {
            if (activeInput && activeInput.type === 'number') {
                // Number mode: only digits and dot
                if (!/[0-9.]/.test(e.key)) return;
                if (e.key === '.' && value.includes('.')) return;
                if (value === '0' && e.key !== '.') { value = e.key; }
                else { value += e.key; }
            } else {
                value += e.key;
            }
            e.preventDefault();
            _updateDisplay();
        }
    }

    function _updateDisplay() {
        if (!display) return;
        if (activeInput && activeInput.type !== 'number') {
            // Text mode: show value as-is, show placeholder when empty
            display.textContent = value || '';
            display.classList.toggle('numpad-display--empty', !value);
        } else {
            display.textContent = value || '0';
            display.classList.remove('numpad-display--empty');
        }
    }

    function _onOk() {
        if (!activeInput) { close(); return; }

        if (activeInput.type === 'number') {
            var num = parseFloat(value || '0');
            if (isNaN(num)) num = 0;

            // Clamp to min/max
            var min = activeInput.hasAttribute('min') ? parseFloat(activeInput.min) : null;
            var max = activeInput.hasAttribute('max') ? parseFloat(activeInput.max) : null;
            if (min !== null && num < min) num = min;
            if (max !== null && num > max) num = max;

            // Round for integer steps
            var step = activeInput.hasAttribute('step') ? parseFloat(activeInput.step) : null;
            if (step && step === Math.floor(step)) {
                num = Math.round(num);
            }

            activeInput.value = num;
        } else {
            activeInput.value = value;
        }

        activeInput.dispatchEvent(new Event('change', { bubbles: true }));
        activeInput.dispatchEvent(new Event('input', { bubbles: true }));
        close();
    }

    // ── Open / Close ───────────────────────────────────────────

    function open(inputEl) {
        if (_isOpen) return;
        if (!overlay) _build();

        _isOpen = true;
        activeInput = inputEl;
        value = inputEl.value || '';

        var isNumber = inputEl.type === 'number';

        // Switch mode
        numpadGrid.style.display = isNumber ? 'grid' : 'none';
        keyboardGrid.style.display = isNumber ? 'none' : 'flex';
        panel.classList.toggle('numpad-panel--keyboard', !isNumber);

        // Show/hide dot for integer-only number fields
        if (isNumber) {
            dotBtn.classList.toggle('numpad-btn--hidden', inputEl.step === '1');
        }

        // Label
        var labelEl = document.querySelector('label[for="' + inputEl.id + '"]');
        var labelText = inputEl.dataset.label
            || (labelEl ? labelEl.textContent.trim() : '')
            || inputEl.placeholder
            || '';
        label.textContent = labelText;

        _updateDisplay();
        overlay.style.display = 'flex';

        // Blur input to prevent direct typing going into the field
        inputEl.blur();

        // Listen for physical keyboard
        document.addEventListener('keydown', _onKeyDown);
    }

    function close() {
        if (overlay) overlay.style.display = 'none';
        document.removeEventListener('keydown', _onKeyDown);
        _isOpen = false;
        activeInput = null;
        value = '';
    }

    // ── Init ───────────────────────────────────────────────────

    function init() {
        document.addEventListener('focusin', function(e) {
            if (_isOpen) return;
            var el = e.target;
            if (el.tagName !== 'INPUT') return;
            if (el.type !== 'number' && el.type !== 'text') return;
            // Only for config editor inputs and relay mappings
            if (!el.closest('.config-section-body') && !el.dataset.numpad) return;

            open(el);
        });
    }

    return { init: init, open: open, close: close };
})();
