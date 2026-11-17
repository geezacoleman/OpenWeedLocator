"""Tests for agent.widget_manager.WidgetManager."""

import html as html_lib
import json
import re

import pytest

from agent.widget_manager import WidgetManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_spec(widget_id='test-widget', **overrides):
    """Return a minimal valid widget spec dict."""
    spec = {
        'id': widget_id,
        'name': 'Test Widget',
        'type': 'range_slider',
        'slot': 'dashboard_controls',
        'builtin_config': {
            'param': 'exg_min',
            'section': 'GreenOnBrown',
            'min': 0,
            'max': 255,
            'step': 1,
            'label': 'ExG Min',
            'unit': '',
        },
    }
    spec.update(overrides)
    return spec


def _write_widget(widgets_dir, spec, files=None):
    """Write a widget.json (and optional extra files) to disk."""
    widget_dir = widgets_dir / spec['id']
    widget_dir.mkdir(parents=True, exist_ok=True)
    with open(widget_dir / 'widget.json', 'w') as f:
        json.dump(spec, f)
    if files:
        for name, content in files.items():
            with open(widget_dir / name, 'w') as f:
                f.write(content)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

class TestScan:
    def test_discovers_valid_widgets(self, tmp_path):
        spec = _minimal_spec('my-slider')
        _write_widget(tmp_path, spec)

        mgr = WidgetManager(tmp_path)
        result = mgr.scan()

        assert len(result) == 1
        assert result[0]['id'] == 'my-slider'

    def test_discovers_multiple_widgets(self, tmp_path):
        for i in range(3):
            _write_widget(tmp_path, _minimal_spec(f'widget-{i}'))

        mgr = WidgetManager(tmp_path)
        result = mgr.scan()
        assert len(result) == 3

    def test_skips_directory_without_manifest(self, tmp_path):
        (tmp_path / 'no-manifest').mkdir()

        mgr = WidgetManager(tmp_path)
        result = mgr.scan()
        assert result == []

    def test_skips_malformed_json(self, tmp_path):
        widget_dir = tmp_path / 'bad-json'
        widget_dir.mkdir()
        (widget_dir / 'widget.json').write_text('NOT VALID JSON')

        mgr = WidgetManager(tmp_path)
        result = mgr.scan()
        assert result == []

    def test_skips_invalid_spec(self, tmp_path):
        """A widget.json with missing required fields is skipped."""
        widget_dir = tmp_path / 'missing-fields'
        widget_dir.mkdir()
        with open(widget_dir / 'widget.json', 'w') as f:
            json.dump({'id': 'missing-fields'}, f)  # missing name, type, slot

        mgr = WidgetManager(tmp_path)
        result = mgr.scan()
        assert result == []

    def test_ignores_files_at_top_level(self, tmp_path):
        """Non-directory entries in widgets_dir are ignored."""
        (tmp_path / 'readme.txt').write_text('hello')
        mgr = WidgetManager(tmp_path)
        result = mgr.scan()
        assert result == []

    def test_empty_directory(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        result = mgr.scan()
        assert result == []


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid_spec(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        valid, errors = mgr.validate(_minimal_spec())
        assert valid is True
        assert errors == []

    def test_missing_id(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec()
        del spec['id']
        valid, errors = mgr.validate(spec)
        assert valid is False
        assert any('id' in e for e in errors)

    def test_missing_name(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec()
        del spec['name']
        valid, errors = mgr.validate(spec)
        assert valid is False
        assert any('name' in e for e in errors)

    def test_invalid_type(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(type='not_a_type')
        valid, errors = mgr.validate(spec)
        assert valid is False
        assert any('type' in e for e in errors)

    def test_invalid_slot(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(slot='nowhere')
        valid, errors = mgr.validate(spec)
        assert valid is False
        assert any('slot' in e for e in errors)

    def test_non_dict_spec(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        valid, errors = mgr.validate('not a dict')
        assert valid is False
        assert any('dict' in e for e in errors)

    def test_unsafe_id_path_traversal(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(id='../etc/passwd')
        valid, errors = mgr.validate(spec)
        assert valid is False
        assert any('unsafe' in e for e in errors)

    def test_unsafe_id_slashes(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(id='foo/bar')
        valid, errors = mgr.validate(spec)
        assert valid is False

    def test_unsafe_id_uppercase(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(id='FooBar')
        valid, errors = mgr.validate(spec)
        assert valid is False

    def test_all_valid_types_accepted(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        for wtype in WidgetManager.VALID_TYPES:
            spec = _minimal_spec(type=wtype)
            valid, _ = mgr.validate(spec)
            assert valid is True, f'type {wtype} should be valid'

    def test_all_valid_slots_accepted(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        for slot in WidgetManager.VALID_SLOTS:
            spec = _minimal_spec(slot=slot)
            valid, _ = mgr.validate(spec)
            assert valid is True, f'slot {slot} should be valid'


# ---------------------------------------------------------------------------
# render — builtin types
# ---------------------------------------------------------------------------

class TestRender:
    def test_range_slider_html(self, tmp_path):
        spec = _minimal_spec('rs-widget', type='range_slider', builtin_config={
            'param': 'exg_min', 'section': 'GreenOnBrown',
            'min': 0, 'max': 255, 'step': 1,
            'label': 'ExG Min', 'unit': '',
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('rs-widget')
        assert html is not None
        assert 'data-widget-id="rs-widget"' in html
        assert 'data-widget-type="range_slider"' in html
        assert 'type="range"' in html
        assert 'data-param="exg_min"' in html
        assert 'widget-slider-label' in html

    def test_dual_range_slider_html(self, tmp_path):
        spec = _minimal_spec('drs-widget', type='dual_range_slider', builtin_config={
            'param': 'exg', 'section': 'GreenOnBrown',
            'min': 0, 'max': 255, 'step': 1,
            'label': 'ExG Threshold', 'unit': '',
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('drs-widget')
        assert html is not None
        assert 'data-param="exg_min"' in html
        assert 'data-param="exg_max"' in html
        assert 'widget-range-min' in html
        assert 'widget-range-max' in html

    def test_toggle_html(self, tmp_path):
        spec = _minimal_spec('tog-widget', type='toggle', builtin_config={
            'param': 'tracking_enabled', 'section': 'Tracking',
            'label': 'Tracking', 'on_label': 'ON', 'off_label': 'OFF',
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('tog-widget')
        assert 'widget-toggle' in html
        assert 'data-on-label="ON"' in html
        assert 'data-off-label="OFF"' in html

    def test_button_html(self, tmp_path):
        spec = _minimal_spec('btn-widget', type='button', builtin_config={
            'label': 'Restart', 'variant': 'danger',
            'mqtt_command': '{"action": "restart"}',
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('btn-widget')
        assert html == (
            '<div class="widget-container" data-widget-id="btn-widget" data-widget-type="button">\n'
            '<button class="action-button btn-danger widget-button" '
            "data-command='{&quot;action&quot;: &quot;restart&quot;}'>Restart</button>\n"
            '</div>')

    def test_select_html(self, tmp_path):
        spec = _minimal_spec('sel-widget', type='select', builtin_config={
            'param': 'algorithm', 'section': 'System',
            'label': 'Algorithm', 'options': ['exg', 'hsv', 'exhsv'],
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('sel-widget')
        assert 'widget-select' in html
        assert '<option' in html
        assert 'exhsv' in html

    def test_select_dict_options(self, tmp_path):
        """Select widget with dict-format options renders label/value correctly."""
        spec = _minimal_spec('algo-sel', type='select', builtin_config={
            'param': 'algorithm', 'section': 'System',
            'label': 'Algorithm',
            'options': [
                {'label': 'Excess Green (exg)', 'value': 'exg'},
                {'label': 'Orange HSV', 'value': 'orange_hsv'},
            ],
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('algo-sel')
        assert 'value="exg"' in html
        assert 'Excess Green (exg)' in html
        assert 'value="orange_hsv"' in html
        assert 'Orange HSV' in html

    def test_stat_display_html(self, tmp_path):
        spec = _minimal_spec('stat-widget', type='stat_display', builtin_config={
            'label': 'CPU', 'stat_key': 'cpu_percent', 'format': '--', 'unit': '%',
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('stat-widget')
        assert 'widget-stat' in html
        assert 'data-stat-key="cpu_percent"' in html

    def test_preset_button_html(self, tmp_path):
        spec = _minimal_spec('preset-widget', type='preset_button', builtin_config={
            'preset_name': 'night_mode',
            'label': 'Night Mode',
            'values': {'exg_min': 10, 'exg_max': 200},
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('preset-widget')
        assert 'widget-preset-button' in html
        assert 'data-preset-name="night_mode"' in html
        assert 'Night Mode' in html

    def test_custom_reads_template_html(self, tmp_path):
        spec = _minimal_spec('custom-widget', type='custom')
        _write_widget(tmp_path, spec, files={
            'template.html': '<p>Custom content</p>',
        })
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('custom-widget')
        assert '<p>Custom content</p>' in html
        assert 'data-widget-type="custom"' in html

    def test_custom_missing_template(self, tmp_path):
        spec = _minimal_spec('custom-no-tpl', type='custom')
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('custom-no-tpl')
        assert html is not None
        assert 'no template.html' in html

    def test_render_unknown_widget_returns_none(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        assert mgr.render('nonexistent') is None


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------

class TestInstall:
    def test_creates_directory_and_manifest(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('new-widget')
        ok, err = mgr.install('new-widget', spec)

        assert ok is True
        assert err is None
        assert (tmp_path / 'new-widget' / 'widget.json').exists()

        with open(tmp_path / 'new-widget' / 'widget.json') as f:
            saved = json.load(f)
        assert saved['id'] == 'new-widget'

    def test_creates_additional_files(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('custom-w', type='custom')
        files = {
            'template.html': '<div>Hello</div>',
            'script.js': 'console.log("hi");',
            'style.css': '.foo { color: red; }',
        }
        ok, err = mgr.install('custom-w', spec, files=files)

        assert ok is True
        assert (tmp_path / 'custom-w' / 'template.html').exists()
        assert (tmp_path / 'custom-w' / 'script.js').exists()
        assert (tmp_path / 'custom-w' / 'style.css').exists()

    def test_duplicate_id_fails(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('dup-widget')
        mgr.install('dup-widget', spec)

        ok, err = mgr.install('dup-widget', spec)
        assert ok is False
        assert 'already exists' in err

    def test_invalid_spec_fails(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = {'id': 'bad', 'name': 'Bad'}  # missing type, slot
        ok, err = mgr.install('bad', spec)

        assert ok is False
        assert not (tmp_path / 'bad').exists()

    def test_unsafe_id_rejected(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(id='../escape')
        ok, err = mgr.install('../escape', spec)

        assert ok is False
        assert 'Invalid' in err

    def test_files_with_path_traversal_skipped(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('safe-w', type='custom')
        files = {
            '../evil.js': 'alert("pwned")',
            'template.html': '<div>OK</div>',
        }
        ok, err = mgr.install('safe-w', spec, files=files)

        assert ok is True
        assert not (tmp_path / 'evil.js').exists()
        assert (tmp_path / 'safe-w' / 'template.html').exists()


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------

class TestRemove:
    def test_deletes_widget_directory(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('to-delete')
        mgr.install('to-delete', spec)
        assert (tmp_path / 'to-delete').exists()

        ok, err = mgr.remove('to-delete')
        assert ok is True
        assert not (tmp_path / 'to-delete').exists()

    def test_removes_from_cache(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('cached')
        mgr.install('cached', spec)
        assert mgr.get('cached') is not None

        mgr.remove('cached')
        assert mgr.get('cached') is None

    def test_nonexistent_widget_fails_gracefully(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        ok, err = mgr.remove('does-not-exist')

        assert ok is False
        assert 'not found' in err


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_merges_updates(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('up-widget')
        mgr.install('up-widget', spec)

        ok, err = mgr.update('up-widget', {'description': 'Updated desc'})
        assert ok is True

        updated = mgr.get('up-widget')
        assert updated['description'] == 'Updated desc'
        assert updated['name'] == 'Test Widget'  # unchanged

    def test_persists_to_disk(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('disk-update')
        mgr.install('disk-update', spec)
        mgr.update('disk-update', {'version': '2.0.0'})

        # Read fresh from disk
        with open(tmp_path / 'disk-update' / 'widget.json') as f:
            saved = json.load(f)
        assert saved['version'] == '2.0.0'

    def test_nonexistent_widget_fails_gracefully(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        ok, err = mgr.update('ghost', {'name': 'New Name'})

        assert ok is False
        assert 'not found' in err

    def test_update_that_breaks_validation_fails(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec('break-me')
        mgr.install('break-me', spec)

        ok, err = mgr.update('break-me', {'type': 'invalid_type'})
        assert ok is False
        assert 'type' in err


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------

class TestGet:
    def test_returns_widget_by_id(self, tmp_path):
        spec = _minimal_spec('find-me')
        _write_widget(tmp_path, spec)

        mgr = WidgetManager(tmp_path)
        mgr.scan()

        result = mgr.get('find-me')
        assert result is not None
        assert result['id'] == 'find-me'

    def test_returns_none_for_missing(self, tmp_path):
        mgr = WidgetManager(tmp_path)
        assert mgr.get('nonexistent') is None

    def test_loads_from_disk_without_scan(self, tmp_path):
        """get() should work even if scan() was not called."""
        spec = _minimal_spec('lazy-load')
        _write_widget(tmp_path, spec)

        mgr = WidgetManager(tmp_path)
        result = mgr.get('lazy-load')
        assert result is not None
        assert result['id'] == 'lazy-load'

    def test_returns_none_for_invalid_on_disk(self, tmp_path):
        """A widget.json on disk that fails validation returns None."""
        widget_dir = tmp_path / 'invalid-spec'
        widget_dir.mkdir()
        with open(widget_dir / 'widget.json', 'w') as f:
            json.dump({'id': 'invalid-spec'}, f)  # missing required fields

        mgr = WidgetManager(tmp_path)
        assert mgr.get('invalid-spec') is None


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------

class TestErrorIsolation:
    def test_widgets_dir_created_if_missing(self, tmp_path):
        new_dir = tmp_path / 'nonexistent' / 'widgets'
        mgr = WidgetManager(new_dir)
        assert new_dir.exists()
        assert mgr.scan() == []


# ---------------------------------------------------------------------------
# Filesystem-safe ID validation
# ---------------------------------------------------------------------------

class TestIdSafety:
    @pytest.mark.parametrize('bad_id', [
        '../etc/passwd',
        'foo/bar',
        'foo\\bar',
        '.hidden',
        'UPPERCASE',
        'has spaces',
        'has@special',
        '',
    ])
    def test_rejects_unsafe_ids(self, tmp_path, bad_id):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(id=bad_id)
        valid, errors = mgr.validate(spec)
        assert valid is False

    @pytest.mark.parametrize('good_id', [
        'my-widget',
        'widget123',
        'a',
        'test-widget-v2',
        'foo_bar',
    ])
    def test_accepts_safe_ids(self, tmp_path, good_id):
        mgr = WidgetManager(tmp_path)
        spec = _minimal_spec(id=good_id)
        valid, errors = mgr.validate(spec)
        assert valid is True


# ---------------------------------------------------------------------------
# Fallback cfg bridge — agent-created specs (BUGs 9, 10, 11)
# ---------------------------------------------------------------------------

class TestFallbackCfgBridge:
    """Test that agent-created specs (no builtin_config) render correctly."""

    def test_select_options_from_top_level(self, tmp_path):
        """BUG 9: options at spec top-level are bridged to fallback cfg."""
        spec = {
            'id': 'algo-select',
            'name': 'Algorithm Selector',
            'type': 'select',
            'slot': 'dashboard_controls',
            'options': ['exg', 'hsv', 'exhsv'],
            'config_section': 'System',
            'config_key': 'algorithm',
        }
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('algo-select')
        assert '<option value="exg">exg</option>' in html
        assert '<option value="hsv">hsv</option>' in html
        assert '<option value="exhsv">exhsv</option>' in html

    def test_dual_range_action_params_key_low_high(self, tmp_path):
        """BUG 10: action_params.key_low/key_high are used when param is empty."""
        spec = {
            'id': 'hue-range',
            'name': 'Hue Range',
            'type': 'dual_range_slider',
            'slot': 'dashboard_controls',
            'action_params': {
                'key_low': 'hue_min',
                'key_high': 'hue_max',
                'section': 'GreenOnBrown',
            },
            'min': 0,
            'max': 179,
            'step': 1,
        }
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('hue-range')
        assert 'data-param="hue_min"' in html
        assert 'data-param="hue_max"' in html

    def test_select_action_attribute_rendered(self, tmp_path):
        """BUG 11: spec 'action' field rendered as data-action on <select>."""
        spec = {
            'id': 'algo-action-sel',
            'name': 'Algorithm',
            'type': 'select',
            'slot': 'dashboard_controls',
            'action': 'set_algorithm',
            'options': ['exg', 'hsv'],
            'config_key': 'algorithm',
        }
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('algo-action-sel')
        assert 'data-action="set_algorithm"' in html

    def test_select_no_action_no_attribute(self, tmp_path):
        """Select without action should not have data-action attribute."""
        spec = _minimal_spec('plain-sel', type='select', builtin_config={
            'param': 'algo', 'section': 'System',
            'label': 'Algo', 'options': ['a', 'b'],
        })
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('plain-sel')
        assert 'data-action' not in html

    def test_fallback_bridges_toggle_labels(self, tmp_path):
        """Toggle on_label/off_label from top-level spec are bridged."""
        spec = {
            'id': 'track-toggle',
            'name': 'Tracking',
            'type': 'toggle',
            'slot': 'dashboard_controls',
            'on_label': 'Enabled',
            'off_label': 'Disabled',
            'config_key': 'tracking_enabled',
        }
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('track-toggle')
        assert 'data-on-label="Enabled"' in html
        assert 'data-off-label="Disabled"' in html

    def test_fallback_bridges_variant(self, tmp_path):
        """Button variant from top-level spec is bridged."""
        spec = {
            'id': 'danger-btn',
            'name': 'Emergency Stop',
            'type': 'button',
            'slot': 'dashboard_controls',
            'variant': 'danger',
            'mqtt_command': {'action': 'stop'},
        }
        _write_widget(tmp_path, spec)
        mgr = WidgetManager(tmp_path)
        mgr.scan()

        html = mgr.render('danger-btn')
        assert 'btn-danger' in html


# ---------------------------------------------------------------------------
# What a widget spec can put into the dashboard page
# ---------------------------------------------------------------------------
# The agent writes these specs (create_widget), so every text field is
# untrusted. The rendered HTML is inserted into the dashboard as it is.

HOSTILE = '"><script>alert(1)</script><i x=\''


def _attributes(html):
    """Every attribute name in the rendered HTML."""
    return set(re.findall(r'\s([a-zA-Z-]+)=["\']', html))


class TestEscaping:
    @pytest.mark.parametrize('wtype, allowed', [
        ('range_slider', {'class', 'data-widget-id', 'data-widget-type', 'title', 'type', 'min', 'max',
                          'step', 'value', 'data-section', 'data-param'}),
        ('dual_range_slider', {'class', 'data-widget-id', 'data-widget-type', 'type', 'min', 'max',
                               'step', 'value', 'data-section', 'data-param'}),
        ('toggle', {'class', 'data-widget-id', 'data-widget-type', 'data-section', 'data-param',
                    'data-on-label', 'data-off-label'}),
        ('button', {'class', 'data-widget-id', 'data-widget-type', 'data-command'}),
        ('select', {'class', 'data-widget-id', 'data-widget-type', 'data-section', 'data-param',
                    'data-action', 'value'}),
        ('stat_display', {'class', 'data-widget-id', 'data-widget-type', 'data-stat-key'}),
        ('preset_button', {'class', 'data-widget-id', 'data-widget-type', 'data-preset-name',
                           'data-preset-values'}),
    ])
    def test_text_fields_cannot_add_markup_or_attributes(self, tmp_path, wtype, allowed):
        spec = {'id': 'w', 'name': HOSTILE, 'type': wtype, 'slot': 'widgets_tab',
                'action': HOSTILE,
                'builtin_config': {
                    'label': HOSTILE, 'section': HOSTILE, 'param': HOSTILE, 'unit': HOSTILE,
                    'on_label': HOSTILE, 'off_label': HOSTILE, 'variant': HOSTILE,
                    'stat_key': HOSTILE, 'format': HOSTILE, 'preset_name': HOSTILE,
                    'mqtt_command': {'action': HOSTILE}, 'values': {HOSTILE: HOSTILE},
                    'options': [HOSTILE, {'value': HOSTILE, 'label': HOSTILE}]}}
        mgr = WidgetManager(tmp_path)
        assert mgr.install('w', spec) == (True, None)

        html = mgr.render('w')

        assert '<script' not in html and '<i ' not in html
        assert _attributes(html) <= allowed, 'the spec added an attribute of its own'

    def test_a_button_command_survives_the_attribute_exactly(self, tmp_path):
        """data-command is single-quoted: a quote in the command must not end it."""
        command = {'action': 'set_sensitivity', 'value': "farmer's \"dusty\" <preset> & co"}
        spec = {'id': 'b', 'name': 'B', 'type': 'button', 'slot': 'widgets_tab', 'mqtt_command': command}
        mgr = WidgetManager(tmp_path)
        mgr.install('b', spec)

        html = mgr.render('b')

        [attribute] = re.findall(r"data-command='([^']*)'", html)
        assert json.loads(html_lib.unescape(attribute)) == command

    @pytest.mark.xfail(strict=True, reason=(
        "agent/widget_manager.py _render_range_slider and _render_dual_range_slider put min, max "
        "and step into the HTML without _esc(): a spec with min = '0\" onfocus=\"...' adds an "
        "attribute (script) to the dashboard page."))
    @pytest.mark.parametrize('wtype', ['range_slider', 'dual_range_slider'])
    @pytest.mark.parametrize('field', ['min', 'max', 'step'])
    def test_slider_numbers_cannot_add_attributes(self, tmp_path, wtype, field):
        config = {'label': 'L', 'section': 'GreenOnBrown', 'param': 'exg_min', 'min': 0, 'max': 255, 'step': 1}
        config[field] = '0" onfocus="alert(1)" autofocus="'
        spec = {'id': 's', 'name': 'S', 'type': wtype, 'slot': 'widgets_tab', 'builtin_config': config}
        mgr = WidgetManager(tmp_path)
        mgr.install('s', spec)
        assert 'onfocus' not in _attributes(mgr.render('s'))

    def test_the_agents_tool_cannot_supply_a_custom_widgets_html_or_script(self, tmp_path):
        """A custom widget's template.html and script.js are served as they
        are, so they must only ever come from files put on the unit by hand:
        create_widget passes the spec alone."""
        from agent.tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.discover()
        mgr = WidgetManager(tmp_path)
        spec = {'id': 'c', 'name': 'C', 'type': 'custom', 'slot': 'widgets_tab',
                'files': {'template.html': '<script>alert(1)</script>', 'script.js': 'alert(1)'},
                'template': '<script>alert(1)</script>'}
        result = registry.call('create_widget', {'spec': spec, 'files': spec['files']},
                               {'widget_manager': mgr})
        assert result == {'success': True, 'widget_id': 'c'}
        assert sorted(p.name for p in (tmp_path / 'c').iterdir()) == ['widget.json']
        assert '<script' not in mgr.render('c')
