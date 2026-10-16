"""utils/session_metadata.py: the data-collection pieces shared by the
networked collect screen, the standalone dashboard and (over the same API)
owl-app."""

import configparser
import json

import pytest

from utils.config_manager import write_ini_section
from utils.directory_manager import collect_session_files
from utils.session_metadata import (COLLECT_CAPTURE_DEFAULTS, OWL_CAPTURE_DEFAULTS, LAST_PICK_KEYS,
                                    capture_settings_from, coerce_capture_value, ini_value,
                                    last_pick_values, load_last_pick, merge_session_file,
                                    read_session_file, crop_slug, presets_from_config,
                                    add_custom_crop, remove_custom_crop, CUSTOM_CROPS_KEY,
                                    CUSTOM_CROPS_MAX, lighting_from, clean_session_metadata)


@pytest.mark.unit
class TestCaptureSettings:

    @pytest.mark.parametrize('key,raw,expected', [
        ('sample_mode', 'Distance', 'distance'), ('record_mode', 'video', 'video'),
        ('shutter_cap', 'on', True), ('shutter_cap', 'False', False),
        ('max_gain', '', 'auto'), ('max_gain', '16', 16.0),
        ('jpeg_quality', '90.0', 90), ('video_max_s', '5', 5), ('sample_interval_s', '0.2', 0.2)])
    def test_coerce(self, key, raw, expected):
        assert coerce_capture_value(key, raw) == expected

    @pytest.mark.parametrize('key,raw', [('sample_distance_m', '0.05'), ('jpeg_quality', '101'),
                                         ('max_gain', '0.5'), ('sample_mode', 'gps'),
                                         ('save_directory', '/tmp')])
    def test_coerce_rejects(self, key, raw):
        with pytest.raises(ValueError):
            coerce_capture_value(key, raw)

    def test_blank_and_invalid_values_fall_back(self):
        bad = []
        values = {'sample_mode': 'time', 'jpeg_quality': 'lots', 'smear_limit_mm': ''}
        out = capture_settings_from(values.get, OWL_CAPTURE_DEFAULTS,
                                    on_invalid=lambda k, raw: bad.append(k))
        assert out['sample_mode'] == 'time'
        assert out['jpeg_quality'] == 95 and out['smear_limit_mm'] == 2.0
        assert bad == ['jpeg_quality']

    def test_owl_defaults_differ_only_in_sample_mode(self):
        diff = {k for k in COLLECT_CAPTURE_DEFAULTS if COLLECT_CAPTURE_DEFAULTS[k] != OWL_CAPTURE_DEFAULTS[k]}
        assert diff == {'sample_mode'} and OWL_CAPTURE_DEFAULTS['sample_mode'] == 'frames'

    def test_ini_value(self):
        assert ini_value(True) == 'true' and ini_value(1.5) == '1.5' and ini_value('auto') == 'auto'


@pytest.mark.unit
class TestLastPick:

    def test_round_trip_through_a_hand_edited_ini(self, tmp_path):
        path = tmp_path / 'CONTROLLER.ini'
        path.write_text('# kept\n[Collect]\n# my crops\ncrops = wheat\n', encoding='utf-8')
        write_ini_section(path, 'Collect', last_pick_values(
            {'crop': 'faba_beans', 'weather': 'dew', 'notes': 'not saved'}))
        text = path.read_text()
        assert '# kept' in text and '# my crops' in text and 'notes' not in text
        cfg = configparser.ConfigParser()
        cfg.read(path)
        assert load_last_pick(cfg) == {'crop': 'faba_beans', 'weather': 'dew'}

    def test_unsafe_values_are_blanked(self):
        values = last_pick_values({'crop': '50% wheat', 'weather': 'x' * 61})
        assert values['last_crop'] == '' and values['last_weather'] == ''
        assert set(values) == {'last_' + k for k in LAST_PICK_KEYS}

    def test_no_section(self):
        assert load_last_pick(configparser.ConfigParser()) == {}

    def test_write_creates_missing_file(self, tmp_path):
        path = tmp_path / 'new.ini'
        write_ini_section(path, 'Collect', {'language': 'da'})
        assert '[Collect]' in path.read_text() and 'language = da' in path.read_text()


@pytest.mark.unit
class TestSessionFile:

    def test_read_missing_and_non_dict(self, tmp_path):
        assert read_session_file(str(tmp_path)) == {}
        (tmp_path / 'session_metadata.json').write_text('[1, 2]')
        assert read_session_file(str(tmp_path)) == {}

    def test_merge_leaves_no_temp_files(self, tmp_path):
        merge_session_file(str(tmp_path), {'crop': 'rye'})
        assert [p.name for p in tmp_path.iterdir()] == ['session_metadata.json']
        assert json.loads((tmp_path / 'session_metadata.json').read_text()) == {'crop': 'rye'}


@pytest.mark.unit
def test_date_level_zip_carries_session_metadata(tmp_path):
    session = tmp_path / '20261011' / 'session_191454'
    session.mkdir(parents=True)
    (session / 'img.jpg').write_bytes(b'x')
    (session / 'session_metadata.json').write_text('{}')
    names = {name for name, _ in collect_session_files(str(tmp_path), '20261011')}
    assert any(n.endswith('session_191454/session_metadata.json') for n in names)
    names = {name for name, _ in collect_session_files(str(tmp_path), '20261011/session_191454')}
    assert any(n.endswith('session_metadata.json') for n in names)


# --- Crops a farmer adds, and the light/soil rows (field round 2026-10-15) ---

def _collect(text=''):
    cfg = configparser.ConfigParser()
    cfg.read_string('[Collect]\n' + text)
    return cfg


@pytest.mark.unit
class TestCustomCrops:

    @pytest.mark.parametrize('name,slug', [
        ('Pak choi', 'pak_choi'), ('  Red  Cabbage ', 'red_cabbage'), ('Hvidkål', 'hvidkal'),
        ('Rødbede', 'roedbede'), ('kale/cavolo', 'kale_cavolo'), ('!!!', ''), ('', ''),
        ('x' * 60, 'x' * 40)])
    def test_slug(self, name, slug):
        assert crop_slug(name) == slug

    def test_custom_crops_come_first_and_leave_their_group(self):
        presets = presets_from_config(_collect('custom_crops = Pak choi, cabbage, , pak_choi\n'))
        assert presets['custom_crops'] == ('pak_choi', 'cabbage')
        assert presets['crops'][:2] == ('pak_choi', 'cabbage')
        assert presets['crops'].count('cabbage') == 1
        groups = {g['key']: g['crops'] for g in presets['crop_groups']}
        assert set(groups) == {'arable', 'vegetables'}
        assert 'cabbage' not in groups['vegetables']

    def test_cabbage_is_built_in(self):
        assert 'cabbage' in presets_from_config(configparser.ConfigParser())['crops']

    def test_crops_override_replaces_the_groups(self):
        presets = presets_from_config(_collect('crops = wheat, swede\n'))
        assert presets['crop_groups'] == [{'key': 'all', 'crops': ['wheat', 'swede']}]

    def test_add_returns_the_new_list(self):
        cfg = _collect('custom_crops = swede\n')
        assert add_custom_crop(cfg, 'Pak choi') == ('pak_choi', 'swede, pak_choi')

    def test_add_existing_or_built_in_saves_nothing(self):
        cfg = _collect('custom_crops = swede\n')
        assert add_custom_crop(cfg, 'Swede') == ('swede', None)
        assert add_custom_crop(cfg, 'Cabbage') == ('cabbage', None)

    def test_add_rejects_empty_and_full_lists(self):
        with pytest.raises(ValueError):
            add_custom_crop(_collect(), '  ')
        full = _collect('custom_crops = ' + ', '.join(f'crop{i}' for i in range(CUSTOM_CROPS_MAX)) + '\n')
        with pytest.raises(ValueError):
            add_custom_crop(full, 'one more')

    def test_remove(self):
        cfg = _collect('custom_crops = swede, pak_choi\n')
        assert remove_custom_crop(cfg, 'swede') == 'pak_choi'
        assert remove_custom_crop(cfg, 'wheat') is None

    def test_saved_list_round_trips_through_the_ini(self, tmp_path):
        path = tmp_path / 'CONTROLLER.ini'
        path.write_text('[Collect]\n# my crops\nlanguage = da\n', encoding='utf-8')
        cfg = configparser.ConfigParser()
        cfg.read(path)
        _, value = add_custom_crop(cfg, 'Pak choi')
        write_ini_section(path, 'Collect', {CUSTOM_CROPS_KEY: value})
        text = path.read_text(encoding='utf-8')
        assert '# my crops' in text and 'language = da' in text
        cfg = configparser.ConfigParser()
        cfg.read(path)
        assert presets_from_config(cfg)['custom_crops'] == ('pak_choi',)


@pytest.mark.unit
class TestLightAndSoil:

    @pytest.mark.parametrize('source,sky,lighting', [
        ('natural', 'sunny', 'natural_sun'), ('natural', 'partly_cloudy', 'natural_partial'),
        ('natural', 'overcast', 'natural_overcast'), ('natural', 'night', ''),
        ('artificial', 'sunny', 'artificial'), ('mixed', '', 'mixed'), ('', 'sunny', '')])
    def test_lighting_derived(self, source, sky, lighting):
        assert lighting_from(source, sky) == lighting

    def test_clean_fills_lighting_from_light_source(self):
        md = clean_session_metadata({'light_source': 'artificial', 'sky': 'sunny',
                                     'lighting': 'natural_sun'})
        assert md['lighting'] == 'artificial'

    def test_clean_keeps_old_lighting_from_older_recorders(self):
        assert clean_session_metadata({'lighting': 'natural_overcast'})['lighting'] == 'natural_overcast'

    def test_soil_fields_kept(self):
        md = clean_session_metadata({'soil_cover': 'none', 'soil_moisture': 'wet',
                                     'recent_water': 'irrigation', 'bogus': 'x'})
        assert (md['soil_cover'], md['soil_moisture'], md['recent_water']) == ('none', 'wet', 'irrigation')
        assert 'bogus' not in md

    def test_new_rows_are_remembered(self):
        for key in ('light_source', 'sky', 'soil_cover', 'soil_moisture', 'recent_water'):
            assert key in LAST_PICK_KEYS

    def test_soil_covers_match_noktura(self):
        # Noktura SURFACE_COVERS (apps/web/src/lib/validations/dataset.ts)
        assert set(presets_from_config(configparser.ConfigParser())['soil_covers']) == {
            'none', 'stubble', 'cereal_stubble', 'broadleaf_stubble', 'living_crop',
            'pasture', 'gravel', 'mixed'}
