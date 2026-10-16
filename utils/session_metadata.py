"""Farmer-entered session metadata and data-collection keys shared by the
OWL (utils/mqtt_manager.py) and both controllers.

Kept free of heavy imports so the networked controller can use it without
pulling in the OWL's MQTT manager.

Metadata is written into every image's EXIF ImageDescription and into
session_metadata.json. Noktura maps it onto existing dataset fields:
crop -> crop_type, lighting -> datasets.lighting, soil_cover ->
datasets.surface_cover, notes -> description, and crop_season / weather /
purpose / light_source / sky / soil_moisture / recent_water into agcontext
(Noktura docs/owl/NOKTURA_INTEGRATION_SPEC.md). Values are slugs
(e.g. crop 'wheat', lighting 'natural_sun'), never translated labels.

Also the one home for what every recorder UI shares (networked collect
screen, standalone dashboard, owl-app via the same /api/collect/* routes):
record-form presets, capture-setting validation, last choices and the
per-session session_metadata.json.
"""

import json
import os
import re
import tempfile
import unicodedata

SESSION_METADATA_FIELDS = ('field_name', 'crop', 'crop_season', 'weather', 'lighting',
                           'light_source', 'sky', 'soil_cover', 'soil_moisture', 'recent_water',
                           'purpose', 'notes', 'vehicle', 'run_id')
SESSION_METADATA_MAX_LEN = 500

# Capture keys the controller may set live (MQTT set_capture_params).
# Applied live only: the controller keeps them in its own [Collect] and
# re-sends them on every reconnect, so collect settings never reach spray mode.
CAPTURE_PARAM_KEYS = ('sample_mode', 'sample_distance_m', 'sample_interval_s', 'jpeg_quality',
                      'record_mode', 'video_max_s', 'shutter_cap', 'smear_limit_mm', 'max_gain',
                      'min_speed_kmh')


def clean_session_metadata(source):
    """Session metadata dict with every known field as a trimmed string.
    When the light is given as light_source + sky, the older single
    `lighting` slug is filled in from them too."""
    source = source or {}
    cleaned = {key: str(source.get(key, '') or '').strip()[:SESSION_METADATA_MAX_LEN]
               for key in SESSION_METADATA_FIELDS}
    if cleaned['light_source']:
        cleaned['lighting'] = lighting_from(cleaned['light_source'], cleaned['sky'])
    return cleaned


def lighting_from(light_source, sky):
    """The Noktura datasets.lighting slug for a light source + sky, or ''.
    Older Noktura builds and EXIF readers only know `lighting`."""
    if light_source in ('artificial', 'mixed'):
        return light_source
    if light_source == 'natural':
        return {'sunny': 'natural_sun', 'partly_cloudy': 'natural_partial',
                'overcast': 'natural_overcast'}.get(sky, '')
    return ''


# --- Record form presets ---------------------------------------------------
# Slugs shown as tap buttons by every recorder UI (networked collect screen,
# standalone dashboard, owl-app). Labels/translations live in
# controller/shared/js/collect_i18n.js. Each list is overridable as a
# comma-separated key in CONTROLLER.ini [Collect].
ARABLE_CROPS = ('wheat', 'barley', 'oats', 'rye', 'triticale', 'maize', 'canola', 'sugar_beet',
                'potatoes', 'peas', 'faba_beans', 'lupins', 'linseed', 'sunflower',
                'grass_clover', 'fallow')
VEGETABLE_CROPS = ('cabbage', 'cauliflower', 'broccoli', 'kale', 'brussels_sprouts', 'lettuce',
                   'spinach', 'onion', 'leek', 'carrot', 'parsnip', 'beetroot', 'celery', 'pumpkin')
DEFAULT_CROPS = ARABLE_CROPS + VEGETABLE_CROPS
SEASONAL_CROPS = ('wheat', 'barley', 'canola', 'rye', 'triticale')
DEFAULT_WEATHER = ('dry', 'dew', 'wet', 'windy')
# Noktura datasets.lighting enum values (derived from light_source + sky)
DEFAULT_LIGHTING = ('natural_sun', 'natural_partial', 'natural_overcast', 'artificial', 'mixed')
# artificial = an enclosed hood with its own lights
DEFAULT_LIGHT_SOURCES = ('natural', 'artificial', 'mixed')
DEFAULT_SKIES = ('sunny', 'partly_cloudy', 'overcast', 'night')
# Noktura SURFACE_COVERS (datasets.surface_cover); none = bare soil
DEFAULT_SOIL_COVERS = ('none', 'stubble', 'cereal_stubble', 'broadleaf_stubble', 'living_crop',
                       'pasture', 'gravel', 'mixed')
DEFAULT_SOIL_MOISTURES = ('dry', 'moist', 'wet')
# Why the soil is wet: irrigation on a dry day is not a bad reading
DEFAULT_RECENT_WATER = ('none', 'rain', 'irrigation')
DEFAULT_PURPOSES = ('herbicide', 'fungicide', 'insecticide', 'survey')

# Crops a farmer added on the screen: [Collect] custom_crops (comma list of
# slugs), shown first. Hand-editable like the rest of [Collect].
CUSTOM_CROPS_KEY = 'custom_crops'
CUSTOM_CROPS_MAX = 40
CROP_SLUG_MAX_LEN = 40

# Record form choices remembered as [Collect] last_<key>
LAST_PICK_KEYS = ('crop', 'crop_season', 'weather', 'lighting', 'light_source', 'sky',
                  'soil_cover', 'soil_moisture', 'recent_water', 'purpose')
COLLECT_LANGUAGES = ('en', 'da')

# Capture settings a recorder UI pushes live (set_capture_params). The
# networked collect screen starts from these; a standalone OWL starts from
# its own [DataCollection] (OWL_CAPTURE_DEFAULTS, the same fallbacks owl.py
# uses when a key is blank).
COLLECT_CAPTURE_DEFAULTS = {
    'sample_mode': 'distance',
    'sample_distance_m': 1.0,
    'sample_interval_s': 2.0,
    'jpeg_quality': 95,
    'record_mode': 'images',
    'video_max_s': 60,
    # Off until tools/shutter_cap_probe.py has passed on the rig's camera.
    'shutter_cap': False,
    'smear_limit_mm': 2.0,
    'max_gain': 'auto',
    # No still images below this GPS speed (0 = never pause). Without a
    # fresh GPS speed images keep coming.
    'min_speed_kmh': 1.0,
}
OWL_CAPTURE_DEFAULTS = dict(COLLECT_CAPTURE_DEFAULTS, sample_mode='frames')


def parse_list(value, default):
    items = [v.strip() for v in str(value or '').split(',') if v.strip()]
    return tuple(items) or tuple(default)


def presets_from_config(config):
    """Preset lists for the record form, from CONTROLLER.ini [Collect].

    crops is the flat list (custom crops first), what older recorder UIs
    read; crop_groups splits it for the tabbed crop picker."""
    get = (lambda key: config.get('Collect', key, fallback='')) if config.has_section('Collect')         else (lambda key: '')
    custom = custom_crops(config)
    override = parse_list(get('crops'), ())
    if override:
        groups = [{'key': 'all', 'crops': [c for c in override if c not in custom]}]
    else:
        groups = [{'key': 'arable', 'crops': [c for c in ARABLE_CROPS if c not in custom]},
                  {'key': 'vegetables', 'crops': [c for c in VEGETABLE_CROPS if c not in custom]}]
    return {
        'crops': tuple(custom) + tuple(c for g in groups for c in g['crops']),
        'custom_crops': tuple(custom),
        'crop_groups': groups,
        'seasonal_crops': parse_list(get('seasonal_crops'), SEASONAL_CROPS),
        'weather': parse_list(get('weather'), DEFAULT_WEATHER),
        'lighting': parse_list(get('lighting'), DEFAULT_LIGHTING),
        'light_sources': parse_list(get('light_sources'), DEFAULT_LIGHT_SOURCES),
        'skies': parse_list(get('skies'), DEFAULT_SKIES),
        'soil_covers': parse_list(get('soil_covers'), DEFAULT_SOIL_COVERS),
        'soil_moistures': parse_list(get('soil_moistures'), DEFAULT_SOIL_MOISTURES),
        'recent_water': parse_list(get('recent_water'), DEFAULT_RECENT_WATER),
        'purposes': parse_list(get('purposes'), DEFAULT_PURPOSES),
    }


# --- Crops a farmer adds on the screen ------------------------------------
_CROP_FOLD = str.maketrans({'æ': 'ae', 'ø': 'oe', 'ß': 'ss', 'œ': 'oe'})


def crop_slug(name):
    """'Pak choi' -> 'pak_choi'; '' when nothing usable is left. Accents are
    dropped (å -> a) so the slug stays a plain INI/EXIF/Noktura value."""
    text = unicodedata.normalize('NFKD', str(name or '').lower().translate(_CROP_FOLD))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]+', '_', text).strip('_')[:CROP_SLUG_MAX_LEN].strip('_')


def custom_crops(config):
    """[Collect] custom_crops as a list of valid slugs, in order, no repeats."""
    raw = config.get('Collect', CUSTOM_CROPS_KEY, fallback='') if config.has_section('Collect') else ''
    out = []
    for item in raw.split(','):
        slug = crop_slug(item)
        if slug and slug not in out:
            out.append(slug)
    return out[:CUSTOM_CROPS_MAX]


def add_custom_crop(config, name):
    """(slug, new custom_crops value or None). None = nothing to save: the
    crop is already on the list (built in or added before). Raises
    ValueError for a name with no letters or digits, or a full list."""
    slug = crop_slug(name)
    if not slug:
        raise ValueError('Type a crop name')
    current = custom_crops(config)
    if slug in current or slug in presets_from_config(config)['crops']:
        return slug, None
    if len(current) >= CUSTOM_CROPS_MAX:
        raise ValueError(f'You already have {CUSTOM_CROPS_MAX} crops of your own; remove one first')
    return slug, ', '.join(current + [slug])


def remove_custom_crop(config, slug):
    """New custom_crops value without slug, or None if it was not there."""
    current = custom_crops(config)
    if slug not in current:
        return None
    return ', '.join(c for c in current if c != slug)


def coerce_capture_value(key, value):
    """Validate one capture setting; raises ValueError."""
    text = str(value).strip().lower()
    if key == 'sample_mode':
        if text not in ('frames', 'distance', 'time'):
            raise ValueError('sample_mode must be frames, distance or time')
        return text
    if key == 'record_mode':
        if text not in ('images', 'video'):
            raise ValueError('record_mode must be images or video')
        return text
    if key == 'shutter_cap':
        if text in ('true', '1', 'yes', 'on'):
            return True
        if text in ('false', '0', 'no', 'off'):
            return False
        raise ValueError('shutter_cap must be true or false')
    if key == 'max_gain':
        if text in ('auto', ''):
            return 'auto'
        gain = float(text)
        if not 1.0 <= gain <= 64.0:
            raise ValueError('max_gain must be auto or 1-64')
        return gain
    limits = {'sample_distance_m': (0.1, 50.0), 'sample_interval_s': (0.2, 60.0),
              'jpeg_quality': (50, 100), 'video_max_s': (5, 60), 'smear_limit_mm': (0.2, 20.0),
              'min_speed_kmh': (0.0, 20.0)}
    if key not in limits:
        raise ValueError(f'{key} is not a capture setting')
    lo, hi = limits[key]
    number = float(text)
    if not lo <= number <= hi:
        raise ValueError(f'{key} must be between {lo} and {hi}')
    return int(number) if key in ('jpeg_quality', 'video_max_s') else number


def capture_settings_from(lookup, defaults, on_invalid=None):
    """All capture settings: lookup(key) -> raw text ('' when unset), each
    value validated, falling back to defaults[key] when blank or invalid."""
    out = dict(defaults)
    for key in defaults:
        raw = str(lookup(key) or '').strip()
        if not raw:
            continue
        try:
            out[key] = coerce_capture_value(key, raw)
        except ValueError:
            if on_invalid:
                on_invalid(key, raw)
    return out


def ini_value(value):
    """How a capture value is written to an INI file."""
    return str(value).lower() if isinstance(value, bool) else str(value)


def load_last_pick(config):
    """Last record-form choices from [Collect] last_*; blanks left out."""
    last = {key: config.get('Collect', 'last_' + key, fallback='').strip()
            for key in LAST_PICK_KEYS} if config.has_section('Collect') else {}
    return {k: v for k, v in last.items() if v}


def last_pick_values(metadata):
    """[Collect] last_* keys for these choices. Only plain values are kept:
    CONTROLLER.ini is hand-edited and read with % interpolation."""
    values = {}
    for key in LAST_PICK_KEYS:
        value = str(metadata.get(key, '') or '')
        values['last_' + key] = value if re.fullmatch(r'[\w .,/()-]{0,60}', value) else ''
    return values


# --- Per-session file ------------------------------------------------------
SESSION_METADATA_FILE = 'session_metadata.json'
# Written by the OWL when the session starts; never edited afterwards
SESSION_FILE_FIXED_KEYS = ('run_id', 'recorded_at', 'owl_id')


def read_session_file(session_dir):
    """session_metadata.json of one session as a dict ({} when missing or
    unreadable)."""
    path = os.path.join(session_dir, SESSION_METADATA_FILE)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_session_file(session_dir, updates):
    """Merge farmer-entered fields into a session's session_metadata.json
    (atomic rewrite) and return the cleaned metadata. Fields not in
    updates, and the fixed keys, keep their values."""
    data = read_session_file(session_dir)
    for key, value in (updates or {}).items():
        if key in SESSION_METADATA_FIELDS and key not in SESSION_FILE_FIXED_KEYS:
            data[key] = str(value or '').strip()[:SESSION_METADATA_MAX_LEN]
    path = os.path.join(session_dir, SESSION_METADATA_FILE)
    fd, tmp_path = tempfile.mkstemp(suffix='.json', prefix='.meta_', dir=session_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return clean_session_metadata(data)
