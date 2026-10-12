"""Farmer-entered session metadata and data-collection keys shared by the
OWL (utils/mqtt_manager.py) and both controllers.

Kept free of heavy imports so the networked controller can use it without
pulling in the OWL's MQTT manager.

Metadata is written into every image's EXIF ImageDescription and into
session_metadata.json. Noktura maps it onto existing dataset fields:
crop -> crop_type, lighting -> datasets.lighting, notes -> description, and
crop_season / weather / purpose into agcontext
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

SESSION_METADATA_FIELDS = ('field_name', 'crop', 'crop_season', 'weather', 'lighting',
                           'purpose', 'notes', 'vehicle', 'run_id')
SESSION_METADATA_MAX_LEN = 500

# Capture keys the controller may set live (MQTT set_capture_params).
# Applied live only: the controller keeps them in its own [Collect] and
# re-sends them on every reconnect, so collect settings never reach spray mode.
CAPTURE_PARAM_KEYS = ('sample_mode', 'sample_distance_m', 'sample_interval_s', 'jpeg_quality',
                      'record_mode', 'video_max_s', 'shutter_cap', 'smear_limit_mm', 'max_gain')


def clean_session_metadata(source):
    """Session metadata dict with every known field as a trimmed string."""
    source = source or {}
    return {key: str(source.get(key, '') or '').strip()[:SESSION_METADATA_MAX_LEN]
            for key in SESSION_METADATA_FIELDS}


# --- Record form presets ---------------------------------------------------
# Slugs shown as tap buttons by every recorder UI (networked collect screen,
# standalone dashboard, owl-app). Labels/translations live in
# controller/shared/js/collect_i18n.js. Each list is overridable as a
# comma-separated key in CONTROLLER.ini [Collect].
DEFAULT_CROPS = ('wheat', 'barley', 'canola', 'rye', 'oats', 'triticale', 'sugar_beet',
                 'maize', 'grass_clover', 'peas', 'faba_beans', 'potatoes', 'fallow')
SEASONAL_CROPS = ('wheat', 'barley', 'canola', 'rye', 'triticale')
DEFAULT_WEATHER = ('dry', 'dew', 'wet', 'windy')
# Noktura datasets.lighting enum values
DEFAULT_LIGHTING = ('natural_sun', 'natural_partial', 'natural_overcast')
DEFAULT_PURPOSES = ('herbicide', 'fungicide', 'insecticide', 'survey')

# Record form choices remembered as [Collect] last_<key>
LAST_PICK_KEYS = ('crop', 'crop_season', 'weather', 'lighting', 'purpose')
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
}
OWL_CAPTURE_DEFAULTS = dict(COLLECT_CAPTURE_DEFAULTS, sample_mode='frames')


def parse_list(value, default):
    items = [v.strip() for v in str(value or '').split(',') if v.strip()]
    return tuple(items) or tuple(default)


def presets_from_config(config):
    """Preset lists for the record form, from CONTROLLER.ini [Collect]."""
    get = (lambda key: config.get('Collect', key, fallback='')) if config.has_section('Collect') \
        else (lambda key: '')
    return {
        'crops': parse_list(get('crops'), DEFAULT_CROPS),
        'seasonal_crops': parse_list(get('seasonal_crops'), SEASONAL_CROPS),
        'weather': parse_list(get('weather'), DEFAULT_WEATHER),
        'lighting': parse_list(get('lighting'), DEFAULT_LIGHTING),
        'purposes': parse_list(get('purposes'), DEFAULT_PURPOSES),
    }


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
              'jpeg_quality': (50, 100), 'video_max_s': (5, 60), 'smear_limit_mm': (0.2, 20.0)}
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
