"""
Widget manager for OWL dashboard.

Discovers, validates, renders and manages user-installable widgets that
extend the standalone dashboard.  Each widget lives in its own subdirectory
under ``widgets/`` and contains a ``widget.json`` manifest.

Builtin widget types (range_slider, toggle, button, etc.) are rendered
server-side using Python template strings that match existing dashboard
HTML patterns.  Custom widgets supply their own ``template.html``,
``script.js`` and ``style.css``.
"""

import json
import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Regex for filesystem-safe widget IDs: lowercase alphanumeric, hyphens, underscores
_SAFE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


class WidgetManager:
    """Discover, validate, render, install and remove dashboard widgets."""

    VALID_TYPES = frozenset({
        'range_slider', 'dual_range_slider', 'toggle', 'button',
        'select', 'stat_display', 'preset_button', 'custom',
    })

    VALID_SLOTS = frozenset({
        'dashboard_controls', 'dashboard_footer',
        'config_before_advanced', 'ai_after_classes', 'widgets_tab',
    })

    def __init__(self, widgets_dir):
        self.widgets_dir = Path(widgets_dir)
        self.widgets_dir.mkdir(parents=True, exist_ok=True)
        self._cache = {}  # id -> widget_json dict

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self):
        """Discover all widgets in *widgets_dir*.

        Each widget is a subdirectory containing ``widget.json``.
        Returns a list of widget dicts.  Logs warnings for malformed
        widgets but never raises.
        """
        self._cache.clear()
        widgets = []

        if not self.widgets_dir.exists():
            return widgets

        for entry in sorted(self.widgets_dir.iterdir()):
            if not entry.is_dir():
                continue

            manifest = entry / 'widget.json'
            if not manifest.exists():
                logger.warning("Widget directory %s has no widget.json — skipped", entry.name)
                continue

            try:
                with open(manifest, 'r', encoding='utf-8') as f:
                    spec = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Malformed widget.json in %s: %s", entry.name, exc)
                continue

            valid, errors = self.validate(spec)
            if not valid:
                logger.warning("Invalid widget %s: %s", entry.name, '; '.join(errors))
                continue

            self._cache[spec['id']] = spec
            widgets.append(spec)

        return widgets

    def get(self, widget_id):
        """Return widget dict for *widget_id*, or ``None``."""
        if widget_id in self._cache:
            return self._cache[widget_id]

        # Try loading from disk if cache is cold
        widget_dir = self.widgets_dir / widget_id
        manifest = widget_dir / 'widget.json'
        if not manifest.exists():
            return None

        try:
            with open(manifest, 'r', encoding='utf-8') as f:
                spec = json.load(f)
            valid, _ = self.validate(spec)
            if valid:
                self._cache[spec['id']] = spec
                return spec
        except (json.JSONDecodeError, OSError):
            pass

        return None

    def validate(self, spec):
        """Validate a widget spec dict.

        Returns ``(True, [])`` on success or ``(False, [error_strings])``
        on failure.
        """
        errors = []

        if not isinstance(spec, dict):
            return False, ['spec must be a dict']

        # Required fields
        widget_id = spec.get('id')
        if not widget_id or not isinstance(widget_id, str):
            errors.append('missing or invalid "id"')
        elif not _SAFE_ID_RE.match(widget_id):
            errors.append(
                f'id "{widget_id}" contains unsafe characters — '
                'use lowercase alphanumeric, hyphens and underscores only'
            )

        if not spec.get('name') or not isinstance(spec.get('name'), str):
            errors.append('missing or invalid "name"')

        wtype = spec.get('type')
        if not wtype or wtype not in self.VALID_TYPES:
            errors.append(f'invalid type "{wtype}" — must be one of {sorted(self.VALID_TYPES)}')

        slot = spec.get('slot')
        if not slot or slot not in self.VALID_SLOTS:
            errors.append(f'invalid slot "{slot}" — must be one of {sorted(self.VALID_SLOTS)}')

        return (len(errors) == 0), errors

    def render(self, widget_id):
        """Return an HTML string for *widget_id*, or ``None``.

        Builtin types are rendered from Python templates.  Custom widgets
        read ``template.html`` from the widget folder.
        """
        spec = self.get(widget_id)
        if spec is None:
            return None

        wtype = spec.get('type')
        cfg = spec.get('builtin_config', {})
        # Fall back to top-level spec fields (agent-created widgets use flat keys)
        if not cfg:
            action_params = spec.get('action_params', {})
            cfg = {
                'label': spec.get('label', spec.get('name', '')),
                'section': spec.get('config_section',
                            action_params.get('section', '')),
                'param': spec.get('config_key',
                           action_params.get('param', '')),
                'min': spec.get('min', 0),
                'max': spec.get('max', 255),
                'step': spec.get('step', 1),
                'unit': spec.get('unit', ''),
                'options': spec.get('options', []),
                'on_label': spec.get('on_label', 'ON'),
                'off_label': spec.get('off_label', 'OFF'),
                'stat_key': spec.get('stat_key', ''),
                'preset_name': spec.get('preset_name', ''),
                'mqtt_command': spec.get('mqtt_command', {}),
                'variant': spec.get('variant', 'primary'),
            }

        inner = ''
        if wtype == 'custom':
            inner = self._render_custom(widget_id)
        else:
            renderer = self._RENDERERS.get(wtype)
            if renderer is not None:
                inner = renderer(self, cfg, spec)
            else:
                inner = f'<!-- unsupported widget type: {wtype} -->'

        return (
            f'<div class="widget-container" '
            f'data-widget-id="{_esc(widget_id)}" '
            f'data-widget-type="{_esc(wtype)}">\n'
            f'{inner}\n'
            f'</div>'
        )

    def install(self, widget_id, spec, files=None):
        """Create a widget folder with ``widget.json`` and optional files.

        *files* is an optional dict of ``{filename: content_string}`` for
        custom widgets (e.g. template.html, script.js, style.css).

        Returns ``(True, None)`` on success or ``(False, error_string)``.
        """
        if not widget_id or not _SAFE_ID_RE.match(widget_id):
            return False, f'Invalid widget id: {widget_id}'

        widget_dir = self.widgets_dir / widget_id
        if widget_dir.exists():
            return False, f'Widget "{widget_id}" already exists'

        valid, errors = self.validate(spec)
        if not valid:
            return False, '; '.join(errors)

        try:
            widget_dir.mkdir(parents=True, exist_ok=True)

            manifest = widget_dir / 'widget.json'
            with open(manifest, 'w', encoding='utf-8') as f:
                json.dump(spec, f, indent=2)

            if files:
                for filename, content in files.items():
                    safe = _safe_filename(filename)
                    if safe is None:
                        continue
                    filepath = widget_dir / safe
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(content)

            self._cache[widget_id] = spec
            logger.info("Installed widget: %s", widget_id)
            return True, None

        except Exception as exc:
            # Clean up partial install
            if widget_dir.exists():
                shutil.rmtree(widget_dir, ignore_errors=True)
            logger.error("Failed to install widget %s: %s", widget_id, exc)
            return False, str(exc)

    def remove(self, widget_id):
        """Delete a widget folder.

        Returns ``(True, None)`` on success or ``(False, error_string)``.
        """
        widget_dir = self.widgets_dir / widget_id
        if not widget_dir.exists():
            return False, f'Widget "{widget_id}" not found'

        try:
            shutil.rmtree(widget_dir)
            self._cache.pop(widget_id, None)
            logger.info("Removed widget: %s", widget_id)
            return True, None
        except Exception as exc:
            logger.error("Failed to remove widget %s: %s", widget_id, exc)
            return False, str(exc)

    def update(self, widget_id, updates):
        """Merge *updates* dict into an existing widget's ``widget.json``.

        Returns ``(True, None)`` on success or ``(False, error_string)``.
        """
        widget_dir = self.widgets_dir / widget_id
        manifest = widget_dir / 'widget.json'
        if not manifest.exists():
            return False, f'Widget "{widget_id}" not found'

        try:
            with open(manifest, 'r', encoding='utf-8') as f:
                spec = json.load(f)

            spec.update(updates)

            valid, errors = self.validate(spec)
            if not valid:
                return False, '; '.join(errors)

            with open(manifest, 'w', encoding='utf-8') as f:
                json.dump(spec, f, indent=2)

            self._cache[widget_id] = spec
            logger.info("Updated widget: %s", widget_id)
            return True, None

        except Exception as exc:
            logger.error("Failed to update widget %s: %s", widget_id, exc)
            return False, str(exc)

    # ------------------------------------------------------------------
    # Builtin renderers
    # ------------------------------------------------------------------

    def _render_range_slider(self, cfg, spec):
        label = _esc(cfg.get('label', spec.get('name', '')))
        section = _esc(cfg.get('section', ''))
        param = _esc(cfg.get('param', ''))
        mn = cfg.get('min', 0)
        mx = cfg.get('max', 255)
        step = cfg.get('step', 1)
        unit = _esc(cfg.get('unit', ''))
        widget_id = _esc(spec.get('id', ''))

        # Match the actuation-slider-group layout used in the dashboard
        return (
            f'<div class="widget-slider-group">\n'
            f'  <div class="widget-slider-header">\n'
            f'    <span class="widget-slider-label">{label}</span>\n'
            f'    <button class="widget-delete-btn" data-widget-id="{widget_id}" '
            f'title="Remove widget">&times;</button>\n'
            f'  </div>\n'
            f'  <div class="widget-slider-row">\n'
            f'    <input type="range" min="{mn}" max="{mx}" step="{step}" '
            f'value="0" class="widget-range" '
            f'data-section="{section}" data-param="{param}">\n'
            f'    <span class="widget-value">0</span>\n'
            f'    <span class="widget-unit">{unit}</span>\n'
            f'  </div>\n'
            f'</div>'
        )

    def _render_dual_range_slider(self, cfg, spec):
        label = _esc(cfg.get('label', spec.get('name', '')))
        section = _esc(cfg.get('section', ''))
        param = cfg.get('param', '')
        mn = cfg.get('min', 0)
        mx = cfg.get('max', 255)
        step = cfg.get('step', 1)
        unit = _esc(cfg.get('unit', ''))

        # Derive min/max param names from action_params if param is empty
        if not param:
            action_params = spec.get('action_params', {})
            param_min = _esc(action_params.get('key_low', ''))
            param_max = _esc(action_params.get('key_high', ''))
        else:
            param_min = _esc(f"{param}_min")
            param_max = _esc(f"{param}_max")

        return (
            f'<div class="widget-field">\n'
            f'  <label class="widget-label">{label}</label>\n'
            f'  <div class="widget-slider-row">\n'
            f'    <input type="range" min="{mn}" max="{mx}" step="{step}" '
            f'value="{mn}" class="widget-range widget-range-min" '
            f'data-section="{section}" data-param="{param_min}">\n'
            f'    <span class="widget-value widget-value-min">{mn}</span>\n'
            f'  </div>\n'
            f'  <div class="widget-slider-row">\n'
            f'    <input type="range" min="{mn}" max="{mx}" step="{step}" '
            f'value="{mx}" class="widget-range widget-range-max" '
            f'data-section="{section}" data-param="{param_max}">\n'
            f'    <span class="widget-value widget-value-max">{mx}</span>\n'
            f'    <span class="widget-unit">{unit}</span>\n'
            f'  </div>\n'
            f'</div>'
        )

    def _render_toggle(self, cfg, spec):
        label = _esc(cfg.get('label', spec.get('name', '')))
        section = _esc(cfg.get('section', ''))
        param = _esc(cfg.get('param', ''))
        on_label = _esc(cfg.get('on_label', 'ON'))
        off_label = _esc(cfg.get('off_label', 'OFF'))

        return (
            f'<div class="widget-field">\n'
            f'  <label class="widget-label">{label}</label>\n'
            f'  <div class="widget-toggle-row">\n'
            f'    <button class="widget-toggle" data-section="{section}" '
            f'data-param="{param}" '
            f'data-on-label="{on_label}" data-off-label="{off_label}">'
            f'{off_label}</button>\n'
            f'  </div>\n'
            f'</div>'
        )

    def _render_button(self, cfg, spec):
        label = _esc(cfg.get('label', spec.get('name', '')))
        variant = _esc(cfg.get('variant', 'primary'))
        command = cfg.get('mqtt_command', '{}')
        # Ensure command is valid JSON string for the data attribute
        if isinstance(command, dict):
            command = json.dumps(command)
        command_esc = _esc(command)

        return (
            f'<button class="action-button btn-{variant} widget-button" '
            f"data-command='{command_esc}'>{label}</button>"
        )

    def _render_select(self, cfg, spec):
        label = _esc(cfg.get('label', spec.get('name', '')))
        section = _esc(cfg.get('section', ''))
        param = _esc(cfg.get('param', ''))
        options = cfg.get('options', [])
        action = _esc(spec.get('action', ''))
        action_attr = f' data-action="{action}"' if action else ''

        parts = []
        for opt in options:
            if isinstance(opt, dict):
                val = _esc(opt.get('value', ''))
                lbl = _esc(opt.get('label', val))
            else:
                val = _esc(opt)
                lbl = val
            parts.append(f'    <option value="{val}">{lbl}</option>')
        option_html = '\n'.join(parts)

        return (
            f'<div class="widget-field">\n'
            f'  <label class="widget-label">{label}</label>\n'
            f'  <select class="widget-select" data-section="{section}" '
            f'data-param="{param}"{action_attr}>\n'
            f'{option_html}\n'
            f'  </select>\n'
            f'</div>'
        )

    def _render_stat_display(self, cfg, spec):
        label = _esc(cfg.get('label', spec.get('name', '')))
        stat_key = _esc(cfg.get('stat_key', ''))
        fmt = _esc(cfg.get('format', '--'))
        unit = _esc(cfg.get('unit', ''))

        return (
            f'<div class="widget-stat">\n'
            f'  <span class="widget-stat-label">{label}</span>\n'
            f'  <span class="widget-stat-value" data-stat-key="{stat_key}">{fmt}</span>\n'
            f'  <span class="widget-stat-unit">{unit}</span>\n'
            f'</div>'
        )

    def _render_preset_button(self, cfg, spec):
        label = _esc(cfg.get('label', cfg.get('preset_name', spec.get('name', ''))))
        preset_name = _esc(cfg.get('preset_name', ''))
        values = cfg.get('values', {})
        values_json = _esc(json.dumps(values))

        return (
            f'<button class="action-button btn-primary widget-preset-button" '
            f'data-preset-name="{preset_name}" '
            f"data-preset-values='{values_json}'>"
            f'{label}</button>'
        )

    def _render_custom(self, widget_id):
        """Read template.html from the widget folder."""
        template_path = self.widgets_dir / widget_id / 'template.html'
        if not template_path.exists():
            return '<!-- custom widget: no template.html found -->'

        try:
            return template_path.read_text(encoding='utf-8')
        except OSError as exc:
            logger.warning("Could not read template.html for %s: %s", widget_id, exc)
            return f'<!-- error reading template for {_esc(widget_id)} -->'

    # Dispatch table for builtin types
    _RENDERERS = {
        'range_slider': _render_range_slider,
        'dual_range_slider': _render_dual_range_slider,
        'toggle': _render_toggle,
        'button': _render_button,
        'select': _render_select,
        'stat_display': _render_stat_display,
        'preset_button': _render_preset_button,
    }


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _esc(value):
    """Escape a string for safe inclusion in HTML attributes."""
    if not isinstance(value, str):
        value = str(value)
    return (
        value
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&#x27;')
    )


def _safe_filename(name):
    """Return *name* if it is a safe relative filename, else ``None``."""
    if not name or not isinstance(name, str):
        return None
    # Reject path traversal
    if '..' in name or '/' in name or '\\' in name:
        return None
    # Only allow alphanumeric, hyphens, underscores, dots
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', name):
        return None
    return name
