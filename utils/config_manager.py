from pathlib import Path
from configparser import ConfigParser, Error as ConfigParserError
from typing import Dict, Set, Tuple

import os
import re
import logging
import tempfile
from datetime import datetime
import utils.error_manager as errors

logger = logging.getLogger(__name__)

GREENONBROWN_PARAMS = frozenset({
    'exg_min', 'exg_max', 'hue_min', 'hue_max',
    'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
    'min_detection_area', 'min_detection_area_percent', 'invert_hue',
    'lut_sensitivity',
})

# Per-unit MOUNT geometry. Lives in GEOMETRY.ini (device-resident), NOT in named
# detection configs. Single source of truth for both the recompute trigger and the
# geometry-strip on named saves.
GEOMETRY_SECTION_KEYS = {
    'Camera': {'crop_left', 'crop_right', 'crop_top', 'crop_bottom'},
    'System': {'actuation_top', 'actuation_bottom'},
}
GEOMETRY_KEYS = frozenset(k for keys in GEOMETRY_SECTION_KEYS.values() for k in keys)
GEOMETRY_FILE = 'GEOMETRY.ini'

# Live (unsaved) changes made while a protected template is active land in
# this single working file, overwritten in place — never a new timestamped
# file per slider move. Explicit "Save As" still creates named presets.
AUTOSAVE_CONFIG = 'config_autosave.ini'


def strip_geometry_keys(config_dict):
    """Return a copy of a {section: {key: value}} config dict with mount geometry
    keys removed, so named detection configs never carry geometry (it is
    device-resident in GEOMETRY.ini). Sections left empty are dropped."""
    out = {}
    for section, opts in config_dict.items():
        geom = GEOMETRY_SECTION_KEYS.get(section, set())
        if geom:
            kept = {k: v for k, v in opts.items() if k not in geom}
        else:
            kept = dict(opts)
        if kept:
            out[section] = kept
    return out


# config_unsaved_state cache: {(autosave_path, source_path): (a_mtime, s_mtime, result)}
_UNSAVED_STATE_CACHE = {}


def _config_signature(path):
    """Normalized {(section, key): value} signature of an .ini for content
    comparison. Excludes [Meta], geometry keys, and the legacy min_detection_area
    px key; numeric values compare as floats ('0' == '0.0'), everything else
    case-insensitively ('True' == 'true')."""
    cp = ConfigParser()
    cp.optionxform = str
    cp.read(path)
    sig = {}
    for section in cp.sections():
        if section == 'Meta':
            continue
        geom = GEOMETRY_SECTION_KEYS.get(section, set())
        for key in cp.options(section):
            if key in geom or key == 'min_detection_area':
                continue
            value = cp.get(section, key, raw=True).strip()
            try:
                norm = repr(float(value))
            except ValueError:
                norm = value.lower()
            sig[(section, key.lower())] = norm
    return sig


def config_unsaved_state(active_path):
    """Return (unsaved, source) for the active config file.

    'Unsaved' means the autosave working file's content materially differs
    from its [Meta] source profile — NOT merely "running from the autosave
    file" (any live slider nudge repoints the active config at the autosave
    file permanently, which used to make every boot read as unsaved).

    - Named file active -> (False, '')
    - Autosave with no/missing [Meta] source -> (True, source or '') (fail-noisy)
    - Autosave matching its source -> (False, source)
    - Autosave differing from its source -> (True, source)

    Results are cached on both files' mtimes, so per-second state publishes
    cost two os.stat calls, not two INI parses.
    """
    active_path = str(active_path)
    if os.path.basename(active_path) != AUTOSAVE_CONFIG:
        return False, ''

    source = parse_config_meta(active_path).get('source', '')
    if not source:
        return True, ''
    source_path = os.path.join(os.path.dirname(active_path), os.path.basename(source))
    if not os.path.isfile(source_path):
        return True, source

    try:
        key = (active_path, source_path)
        mtimes = (os.stat(active_path).st_mtime_ns, os.stat(source_path).st_mtime_ns)
        cached = _UNSAVED_STATE_CACHE.get(key)
        if cached and cached[0] == mtimes:
            return cached[1]
        result = (_config_signature(active_path) != _config_signature(source_path),
                  source)
        _UNSAVED_STATE_CACHE[key] = (mtimes, result)
        return result
    except (OSError, ConfigParserError):
        return True, source


def strip_legacy_min_area(config_dict):
    """Return a copy of a {section: {key: value}} config dict with the legacy
    min_detection_area px key removed from any section whose canonical
    min_detection_area_percent key carries a real value. Saved files are
    percent-only; the px key stays accepted on read for legacy configs."""
    out = {}
    for section, opts in config_dict.items():
        kept = dict(opts)
        try:
            pct = float(kept.get('min_detection_area_percent', 0) or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct > 0:
            kept.pop('min_detection_area', None)
        out[section] = kept
    return out


def build_config_filename(display_name, fallback_filename, timestamp):
    """Build a safe filename for a saved config.

    - A human display_name becomes '<safe-name>_<timestamp>.ini' (timestamp kept
      for uniqueness so named saves never overwrite each other).
    - Otherwise an explicit fallback_filename is honoured as-is (sanitised) for
      backward compatibility with programmatic callers.
    - Otherwise a plain 'config_<timestamp>.ini'.
    """
    if display_name:
        base = re.sub(r'[^a-z0-9]+', '-', display_name.lower()).strip('-')
        if base:
            return f'{base}_{timestamp}.ini'
    if fallback_filename:
        fn = fallback_filename if fallback_filename.endswith('.ini') else fallback_filename + '.ini'
        safe = re.sub(r'[^A-Za-z0-9_.-]+', '', fn)
        if safe and safe != '.ini':
            return safe
    return f'config_{timestamp}.ini'


def parse_config_meta(path):
    """Read the [Meta] section from a config .ini.

    Returns {display_name, notes, created} with absent keys omitted (fail-safe —
    never invents values). Returns {} if the section/file is missing or unreadable.
    """
    try:
        cp = ConfigParser()
        cp.optionxform = str
        cp.read(path)
        if not cp.has_section('Meta'):
            return {}
        meta = {}
        for k in ('display_name', 'notes', 'created', 'source'):
            if cp.has_option('Meta', k):
                meta[k] = cp.get('Meta', k)
        return meta
    except Exception:
        return {}


def seed_autosave(config_dir, source_path):
    """Copy a just-loaded config into the autosave working file.

    Named presets and templates are frozen (Word-doc model): the running
    state lives in AUTOSAVE_CONFIG, which is re-seeded from the source
    whenever a config is loaded and then overwritten in place by live
    changes. [Meta] source records which file the working copy derives
    from. No-op when the source IS the autosave file or doesn't exist.
    Returns the autosave path.
    """
    autosave_path = os.path.join(str(config_dir), AUTOSAVE_CONFIG)
    source_path = str(source_path)
    basename = os.path.basename(source_path)
    if basename == AUTOSAVE_CONFIG or not os.path.isfile(source_path):
        return autosave_path
    cp = ConfigParser()
    cp.optionxform = str
    cp.read(source_path)
    if not cp.has_section('Meta'):
        cp.add_section('Meta')
    cp.set('Meta', 'source', basename)
    atomic_write_config(autosave_path, cp.write)
    return autosave_path


def stamp_config_meta(config, display_name, notes, existing_path=None):
    """Stamp a fresh [Meta] section onto a ConfigParser from the provided name/notes.

    Fixes the DuplicateSectionError round-trip bug: callers must first remove any
    incoming [Meta] from the config dict (it is re-stamped authoritatively here).
    On an update (existing_path points at the file being overwritten), a missing
    name/notes is preserved from the existing file and the original 'created'
    timestamp is kept, so an update never blanks a config's friendly name. No-op
    when there is nothing to record.
    """
    existing = parse_config_meta(existing_path) if existing_path else {}
    name = (display_name or existing.get('display_name', '')).strip()
    note = (notes or existing.get('notes', '')).strip()
    if not (name or note):
        return
    if not config.has_section('Meta'):
        config.add_section('Meta')
    if name:
        config.set('Meta', 'display_name', name)
    if note:
        config.set('Meta', 'notes', note)
    config.set('Meta', 'created',
               existing.get('created') or datetime.now().isoformat(timespec='seconds'))


def atomic_write_config(path, write_fn):
    """Atomically write a config file so a power loss can never leave a truncated
    .ini (tractors lose power mid-write). Writes to a temp file in the SAME
    directory — so os.replace stays on one filesystem and is atomic — then renames
    it over the target. On any failure the temp file is removed and the error
    re-raised, leaving the original untouched.

    :param path: destination .ini path
    :param write_fn: callable(file_obj) that writes the config to the given handle
    """
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(suffix='.ini', prefix='.owl_cfg_', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            write_fn(f)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class ConfigValidator:
    """Validates OWL configuration files"""

    # Infrastructure sections that must exist in CONTROLLER.ini
    CONTROLLER_INI_SECTIONS = {'MQTT', 'WebDashboard', 'Network', 'GPS', 'Actuation'}

    REQUIRED_CONFIG = {
        'System': {
            'required_keys': {'algorithm', 'relay_num'},
            'optional_keys': {'input_file_or_directory', 'actuation_duration', 'delay', 'actuation_zone',
                              'actuation_top', 'actuation_bottom'}
        },
        'Controller': {
            # Base requirements for all controller types
            'required_keys': {'controller_type'},
            'optional_keys': {
                'status_led_pin',
                'gps_led_pin',
                'detection_mode_pin_up',
                'detection_mode_pin_down',
                'recording_pin',
                'sensitivity_pin',
                'low_sensitivity_config',
                'medium_sensitivity_config',
                'high_sensitivity_config',
                'switch_purpose',
                'switch_pin'
            },
            # Type-specific requirements
            'type_specific': {
                'none': {
                    'required_keys': set(),
                    'optional_keys': set()
                },
                'ute': {
                    'required_keys': {'switch_pin', 'switch_purpose'},
                    'optional_keys': set()
                },
                'advanced': {
                    'required_keys': {
                        'detection_mode_pin_up',
                        'detection_mode_pin_down',
                        'recording_pin',
                        'sensitivity_pin',
                    },
                    'optional_keys': {
                        'low_sensitivity_config',
                        'high_sensitivity_config',
                    }
                }
            }
        },
        'Camera': {
            'required_keys': {'resolution_width', 'resolution_height'},
            # awb_mode / rotation are string keys (enum-validated) — optional_keys only
            'optional_keys': {'exp_compensation', 'crop_factor_horizontal', 'crop_factor_vertical', 'camera_type',
                              'allow_high_resolution',
                              'crop_left', 'crop_right', 'crop_top', 'crop_bottom',
                              'awb_mode', 'awb_red_gain', 'awb_blue_gain', 'rotation'}
        },
        'GreenOnGreen': {
            'required_keys': {'model_path', 'confidence'},
            'optional_keys': {'detect_classes', 'actuation_mode', 'min_detection_pixels',
                            'inference_resolution', 'crop_buffer_px'}
        },
        'GreenOnBrown': {
            'required_keys': {
                'exg_min', 'exg_max', 'hue_min', 'hue_max',
                'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max'
            },
            # min_detection_area (px) is legacy — migrated to the canonical
            # min_detection_area_percent at load and stripped on save
            'optional_keys': {'invert_hue', 'lut_profile', 'lut_sensitivity',
                              'min_detection_area', 'min_detection_area_percent'}
        },
        'DataCollection': {
            'required_keys': {'image_sample_enable', 'sample_method', 'save_directory'},
            # storage_location: usb | internal | auto (auto: USB if mounted,
            # else internal). internal_save_directory + min_free_gb drive the
            # eMMC recording mode on sealed OWL 3.0 units; image_quota_gb is
            # the display allowance shown in the UI, not enforced.
            'optional_keys': {'sample_frequency', 'detection_enable', 'log_fps', 'camera_name',
                              'storage_location', 'internal_save_directory',
                              'min_free_gb', 'image_quota_gb'}
        },
        'Relays': {
            'required_keys': set(),
            'optional_keys': {str(i) for i in range(16)}
        }
    }

    VALUE_VALIDATORS = {
        # 8-bit values (0-255)
        'exg_min': ('int', 0, 255),
        'exg_max': ('int', 0, 255),
        'saturation_min': ('int', 0, 255),
        'saturation_max': ('int', 0, 255),
        'brightness_min': ('int', 0, 255),
        'brightness_max': ('int', 0, 255),
        # Hue values (0-180)
        'hue_min': ('int', 0, 180),
        'hue_max': ('int', 0, 180),
        # Resolution
        'resolution_width': ('int', 1, None),
        'resolution_height': ('int', 1, None),
        # Camera settings
        'exp_compensation': ('int', -10, 10),
        # Manual white-balance gains (used when awb_mode = manual).
        # Min 0.1, not 0.0 — libcamera treats ColourGains=(0,0) as "let AWB choose".
        'awb_red_gain': ('float', 0.1, 8.0),
        'awb_blue_gain': ('float', 0.1, 8.0),
        # Detection confidence
        'confidence': ('float', 0, 1),
        # GreenOnGreen
        'min_detection_pixels': ('int', 1, None),
        'inference_resolution': ('int', 160, 1280),
        'crop_buffer_px': ('int', 0, 50),
        'actuation_zone': ('int', 1, 100),
        # Painted LUT detection (lut_profile is a string key — optional_keys only)
        'lut_sensitivity': ('int', 0, 100),
        # Internal (eMMC) storage mode: hard free-space floor + display quota
        # (storage_location/internal_save_directory are string keys — optional_keys only)
        'min_free_gb': ('int', 1, None),
        'image_quota_gb': ('int', 1, None),
        # Min weed size as % of the detection (cropped) frame area.
        # 0 disables it (legacy min_detection_area px value applies instead).
        'min_detection_area_percent': ('float', 0, 5),
        # Per-edge crop fractions (inset from each edge, 0.0-0.49)
        'crop_left': ('float', 0, 0.49),
        'crop_right': ('float', 0, 0.49),
        'crop_top': ('float', 0, 0.49),
        'crop_bottom': ('float', 0, 0.49),
        # Actuation band (fractions of cropped height; 0.0 = top of crop, 1.0 = bottom)
        'actuation_top': ('float', 0, 1.0),
        'actuation_bottom': ('float', 0, 1.0),
        # GPIO pins
        'switch_pin': ('pin', 1, 40),
        'detection_mode_pin_up': ('pin', 1, 40),
        'detection_mode_pin_down': ('pin', 1, 40),
        'recording_pin': ('pin', 1, 40),
        'sensitivity_pin': ('pin', 1, 40),
        # Tracking (ByteTrack params)
        'track_high_thresh': ('float', 0.01, 0.5),
        'track_low_thresh': ('float', 0.01, 0.3),
        'new_track_thresh': ('float', 0.01, 0.5),
        'track_buffer': ('int', 10, 150),
        'match_thresh': ('float', 0.1, 0.95),
        'track_class_window': ('int', 1, 20),
        'track_crop_persist': ('int', 1, 10),
        'detection_persist_frames': ('int', 0, 15),
        # Network ports ([MQTT] and [Cloud])
        'broker_port': ('int', 1, 65535),
        # Boolean fields
        'image_sample_enable': ('bool', None, None),
        'detection_enable': ('bool', None, None),
        'log_fps': ('bool', None, None),
        'invert_hue': ('bool', None, None),
        'tracking_enabled': ('bool', None, None),
    }

    VALID_ALGORITHMS = {'exg', 'exgr', 'maxg', 'nexg', 'exhsv', 'hsv', 'gndvi', 'lut', 'gog', 'gog-hybrid'}

    @classmethod
    def get_valid_algorithms(cls):
        """Return builtin algorithms plus any custom ones on disk."""
        valid = set(cls.VALID_ALGORITHMS)
        try:
            from custom_algorithms import list_algorithms
            for algo in list_algorithms():
                valid.add(algo['name'])
        except Exception:
            pass
        return valid
    VALID_CONTROLLER_TYPES = {'none', 'ute', 'advanced'}
    VALID_SWITCH_PURPOSES = {'recording', 'detection'}
    VALID_ACTUATION_MODES = {'centre', 'zone'}
    VALID_CAMERA_TYPES = {'rpi', 'usb', 'auto'}
    VALID_AWB_MODES = {'auto', 'daylight', 'cloudy', 'tungsten', 'fluorescent', 'indoor', 'manual'}
    VALID_ROTATIONS = {'auto', '0', '180'}
    VALID_SAMPLE_METHODS = {'bbox', 'square', 'whole'}
    VALID_STORAGE_LOCATIONS = {'usb', 'internal', 'auto'}
    VALID_BOOLEANS = {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}

    # Valid Raspberry Pi GPIO pins (BOARD numbering)
    # Excludes: power pins (1, 2, 4, 17), ground pins (6, 9, 14, 20, 25, 30, 34, 39),
    # and I2C EEPROM reserved pins (27, 28)
    VALID_GPIO_PINS = {
        3, 5, 7, 8, 10, 11, 12, 13, 15, 16, 18, 19,
        21, 22, 23, 24, 26, 29, 31, 32, 33, 35, 36, 37, 38, 40
    }

    # Human-readable descriptions for invalid pins
    RESERVED_PIN_DESCRIPTIONS = {
        1: '3.3V Power', 2: '5V Power', 4: '5V Power', 17: '3.3V Power',
        6: 'Ground', 9: 'Ground', 14: 'Ground', 20: 'Ground',
        25: 'Ground', 30: 'Ground', 34: 'Ground', 39: 'Ground',
        27: 'I2C EEPROM (ID_SD)', 28: 'I2C EEPROM (ID_SC)'
    }

    # Sensitivity preset section keys
    SENSITIVITY_SECTION_KEYS = {
        'exg_min', 'exg_max', 'hue_min', 'hue_max',
        'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
    }
    # Optional preset keys — min_detection_area_percent is canonical (new
    # sections), min_detection_area px is legacy (old sections, migrated on save)
    SENSITIVITY_SECTION_OPTIONAL_KEYS = {
        'min_detection_area', 'min_detection_area_percent',
    }

    # Optional top-level sections (not in REQUIRED_CONFIG)
    OPTIONAL_SECTIONS = {
        'Visualisation': {
            'optional_keys': {'image_loop_time'}
        },
        'Sensitivity': {
            'optional_keys': {'active'}
        },
        'Tracking': {
            'optional_keys': {'tracking_enabled', 'track_high_thresh', 'track_low_thresh',
                              'new_track_thresh', 'track_buffer', 'match_thresh',
                              'track_class_window', 'track_crop_persist',
                              'detection_persist_frames'}
        },
        # Cloud bridge (Noktura) — written by owl_cloud_provision.sh.
        # Optional: un-provisioned devices have no [Cloud] section at all.
        'Cloud': {
            'optional_keys': {'enable', 'broker_host', 'broker_port', 'device_id',
                              'ca_cert', 'username', 'password_file', 'portal_url'}
        },
        # Human-readable config metadata written when a named config is saved.
        # Ignored by the detection pipeline; present so the controller can show
        # a friendly name/notes. Listed here so it never triggers a startup warning.
        'Meta': {
            'optional_keys': {'display_name', 'notes', 'created'}
        },
    }

    # to check for valid ranges
    THRESHOLD_PAIRS = [
        ('exg_min', 'exg_max'),
        ('hue_min', 'hue_max'),
        ('saturation_min', 'saturation_max'),
        ('brightness_min', 'brightness_max')
    ]

    @classmethod
    def validate_controller(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Validate controller configuration."""
        controller_errors: Dict[str, Dict[str, str]] = {}  # Type hint for errors dictionary
        controller_type = config.get('Controller', 'controller_type', fallback='').lower()

        # Validate controller type
        if not controller_type:
            return False, {'Controller': {'controller_type': 'Controller type must be specified'}}

        if controller_type not in cls.VALID_CONTROLLER_TYPES:
            return False, {'Controller': {
                'controller_type': f'Invalid controller type. Must be one of: {", ".join(sorted(cls.VALID_CONTROLLER_TYPES))}'
            }}

        # For UTE controller, validate switch_purpose
        if controller_type == 'ute' and config.has_option('Controller', 'switch_purpose'):
            switch_purpose = config.get('Controller', 'switch_purpose').lower()
            if switch_purpose not in cls.VALID_SWITCH_PURPOSES:
                if 'Controller' not in controller_errors:
                    controller_errors['Controller'] = {}
                controller_errors['Controller'][
                    'switch_purpose'] = f'Must be one of: {", ".join(sorted(cls.VALID_SWITCH_PURPOSES))}'

        # Hardware controllers (ute/advanced) should not be used with networked mode
        if controller_type in ('ute', 'advanced'):
            network_mode = config.get('Network', 'mode', fallback='').strip("'\" ").lower()
            if network_mode == 'networked':
                if 'Controller' not in controller_errors:
                    controller_errors['Controller'] = {}
                controller_errors['Controller']['controller_type'] = (
                    f'Hardware controllers ({controller_type}) cannot be used with networked mode. '
                    f'Use the standalone dashboard instead.'
                )

        return not bool(controller_errors), controller_errors

    @classmethod
    def get_controller_requirements(cls, controller_type: str) -> Tuple[set, set]:
        """Get combined base and type-specific requirements for a controller."""
        base_required = cls.REQUIRED_CONFIG['Controller']['required_keys']
        base_optional = cls.REQUIRED_CONFIG['Controller']['optional_keys']

        type_config = cls.REQUIRED_CONFIG['Controller']['type_specific'].get(
            controller_type,
            {'required_keys': set(), 'optional_keys': set()}
        )

        return (
            base_required | type_config['required_keys'],
            base_optional | type_config['optional_keys']
        )

    @classmethod
    def validate_algorithm(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Validate algorithm selection."""
        algorithm = config.get('System', 'algorithm', fallback='').lower()
        if not algorithm:
            return False, {'System': {'algorithm': 'Algorithm must be specified'}}

        valid = cls.get_valid_algorithms()
        if algorithm not in valid:
            return False, {'System': {
                'algorithm': f'Invalid algorithm. Must be one of: {", ".join(sorted(valid))}'
            }}

        return True, {}

    @classmethod
    def validate_thresholds(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """
        Validate threshold relationships and detection ranges.
        Returns (is_valid, errors)
        """
        ACCEPTABLE_RANGE = 5
        threshold_errors = {}
        section_errors = {}

        # Validate min < max for all threshold pairs
        for min_key, max_key in cls.THRESHOLD_PAIRS:
            try:
                min_val = config.getint('GreenOnBrown', min_key)
                max_val = config.getint('GreenOnBrown', max_key)

                if min_val >= max_val:
                    section_errors[f"{min_key}_{max_key}"] = (
                        f"{min_key} ({min_val}) must be less than {max_key} ({max_val})"
                    )
            except (ValueError, ConfigParserError):
                # Skip if values aren't valid integers - this will be caught by value validation
                continue

        # Validate detection ranges overlap
        algorithm = config.get('System', 'algorithm', fallback='').lower()

        # For HSV-based algorithms, check HSV ranges make sense together
        if algorithm in {'hsv', 'exhsv'}:
            try:
                hue_range = range(config.getint('GreenOnBrown', 'hue_min'),
                                  config.getint('GreenOnBrown', 'hue_max'))
                sat_range = range(config.getint('GreenOnBrown', 'saturation_min'),
                                  config.getint('GreenOnBrown', 'saturation_max'))
                val_range = range(config.getint('GreenOnBrown', 'brightness_min'),
                                  config.getint('GreenOnBrown', 'brightness_max'))

                # Check if ranges are too restrictive
                if len(hue_range) < ACCEPTABLE_RANGE:
                    section_errors['hue_range'] = 'Hue range is too narrow for reliable detection'
                if len(sat_range) < ACCEPTABLE_RANGE:
                    section_errors['saturation_range'] = 'Saturation range is too narrow for reliable detection'
                if len(val_range) < ACCEPTABLE_RANGE:
                    section_errors['brightness_range'] = 'Brightness range is too narrow for reliable detection'

            except (ValueError, ConfigParserError):
                # Skip if values aren't valid integers - this will be caught by value validation
                pass

        # For EXG-based algorithms, check EXG range
        if algorithm in {'exg', 'exgr', 'maxg', 'nexg', 'exhsv'}:
            try:
                exg_range = range(config.getint('GreenOnBrown', 'exg_min'),
                                  config.getint('GreenOnBrown', 'exg_max'))

                if len(exg_range) < ACCEPTABLE_RANGE:
                    section_errors['exg_range'] = 'ExG range is too narrow for reliable detection'

            except (ValueError, ConfigParserError):
                pass

        if section_errors:
            threshold_errors['GreenOnBrown'] = section_errors

        return not bool(threshold_errors), threshold_errors

    @classmethod
    def validate_value(cls, key: str, value: str, used_pins: Set[int]) -> Tuple[bool, str]:
        """Validate a single config value."""
        if key not in cls.VALUE_VALIDATORS:
            return True, ""

        val_type, min_val, max_val = cls.VALUE_VALIDATORS[key]

        try:
            if val_type == 'int':
                val = int(value)
                if min_val is not None and val < min_val:
                    return False, f"Value must be >= {min_val}"
                if max_val is not None and val > max_val:
                    return False, f"Value must be <= {max_val}"

            elif val_type == 'float':
                val = float(value)
                if min_val is not None and val < min_val:
                    return False, f"Value must be >= {min_val}"
                if max_val is not None and val > max_val:
                    return False, f"Value must be <= {max_val}"

            elif val_type == 'bool':
                if value.lower() not in cls.VALID_BOOLEANS:
                    return False, f"Must be a boolean value (true/false, yes/no, 1/0, on/off)"

            elif val_type == 'pin':
                val = int(value)
                if min_val is not None and val < min_val:
                    return False, f"Pin must be >= {min_val}"
                if max_val is not None and val > max_val:
                    return False, f"Pin must be <= {max_val}"
                if val not in cls.VALID_GPIO_PINS:
                    desc = cls.RESERVED_PIN_DESCRIPTIONS.get(val, 'Reserved/Invalid')
                    return False, f"Pin {val} is not a valid GPIO pin ({desc})"
                if val in used_pins:
                    return False, f"Pin {val} is already in use"
                used_pins.add(val)

        except ValueError:
            return False, f"Must be a valid {val_type}"

        return True, ""

    @classmethod
    def validate_camera_type(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Validate camera type selection."""
        if not config.has_option('Camera', 'camera_type'):
            return True, {}  # Optional field, skip if not present

        camera_type = config.get('Camera', 'camera_type', fallback='').lower()

        if camera_type not in cls.VALID_CAMERA_TYPES:
            return False, {'Camera': {
                'camera_type': f'Invalid camera type. Must be one of: {", ".join(sorted(cls.VALID_CAMERA_TYPES))}'
            }}

        return True, {}

    @classmethod
    def validate_awb_mode(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Validate white balance mode selection."""
        if not config.has_option('Camera', 'awb_mode'):
            return True, {}  # Optional field, skip if not present

        awb_mode = config.get('Camera', 'awb_mode', fallback='').lower()

        if awb_mode not in cls.VALID_AWB_MODES:
            return False, {'Camera': {
                'awb_mode': f'Invalid white balance mode. Must be one of: {", ".join(sorted(cls.VALID_AWB_MODES))}'
            }}

        return True, {}

    @classmethod
    def validate_rotation(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Validate camera rotation selection."""
        if not config.has_option('Camera', 'rotation'):
            return True, {}  # Optional field, skip if not present

        rotation = config.get('Camera', 'rotation', fallback='').strip().lower()

        if rotation not in cls.VALID_ROTATIONS:
            return False, {'Camera': {
                'rotation': f'Invalid rotation. Must be one of: {", ".join(sorted(cls.VALID_ROTATIONS))}'
            }}

        return True, {}

    @classmethod
    def validate_sample_method(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Validate sample method selection."""
        if not config.has_option('DataCollection', 'sample_method'):
            return True, {}  # Will be caught by required key validation

        sample_method = config.get('DataCollection', 'sample_method', fallback='').lower()

        if sample_method not in cls.VALID_SAMPLE_METHODS:
            return False, {'DataCollection': {
                'sample_method': f'Invalid sample method. Must be one of: {", ".join(sorted(cls.VALID_SAMPLE_METHODS))}'
            }}

        return True, {}

    @classmethod
    def validate_storage_location(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Validate storage location selection (usb | internal | auto)."""
        if not config.has_option('DataCollection', 'storage_location'):
            return True, {}  # Optional key; absent means auto

        storage_location = config.get('DataCollection', 'storage_location', fallback='').strip().lower()
        if not storage_location:
            return True, {}  # Blank value treated exactly like a missing key (auto)

        if storage_location not in cls.VALID_STORAGE_LOCATIONS:
            return False, {'DataCollection': {
                'storage_location': f'Invalid storage location. Must be one of: {", ".join(sorted(cls.VALID_STORAGE_LOCATIONS))}'
            }}

        return True, {}

    @classmethod
    def validate_relay_pin_conflicts(cls, config: ConfigParser, used_pins: Set[int]) -> Tuple[bool, Dict[str, Dict[str, str]]]:
        """Check relay pins don't conflict with controller pins and are valid GPIO pins."""
        if not config.has_section('Relays'):
            return True, {}

        relay_errors = {}
        relay_pins = set()

        for key, value in config['Relays'].items():
            try:
                pin_val = int(value)
            except ValueError:
                continue  # validate_relays() handles format errors

            if pin_val not in cls.VALID_GPIO_PINS:
                desc = cls.RESERVED_PIN_DESCRIPTIONS.get(pin_val, 'Reserved/Invalid')
                relay_errors[key] = f"Pin {pin_val} is not a valid GPIO pin ({desc})"
            elif pin_val in relay_pins:
                relay_errors[key] = f"Pin {pin_val} is already assigned to another relay"
            elif pin_val in used_pins:
                relay_errors[key] = f"Pin {pin_val} conflicts with a controller pin"
            else:
                relay_pins.add(pin_val)

        if relay_errors:
            return False, {'Relays': relay_errors}

        return True, {}

    @classmethod
    def validate_relays(cls, config: ConfigParser) -> Tuple[bool, Dict[str, Dict[str, str]], list[str]]:
        """
        Validate relay configuration between System.relay_num and Relays section.
        Returns:
            Tuple containing:
            - bool: whether validation passed
            - Dict[str, Dict[str, str]]: nested dictionary of section -> {key: error_message}
            - list[str]: list of warning messages
        """
        try:
            relay_num = config.getint('System', 'relay_num')
            if relay_num < 0:
                return False, {'System': {'relay_num': 'Must be a non-negative integer'}}, []
        except ValueError:
            return False, {'System': {'relay_num': 'Must be a valid integer'}}, []

        # Get available relays (keys should be '0', '1', etc.)
        available_relays = set(config['Relays'].keys())

        # Validate relay keys are proper integers
        try:
            for relay in available_relays:
                _ = int(relay)
        except ValueError:
            return False, {'Relays': {'format': 'Relay keys must be integers (0, 1, 2, etc.)'}}, []

        configured_relays = {str(i) for i in range(relay_num)}

        # Check if requesting more relays than configured
        if relay_num > len(available_relays):
            return False, {
                'System': {
                    'relay_num': f'Requests {relay_num} relays but only {len(available_relays)} are configured in [Relays] section'
                }
            }, []

        # If requesting fewer relays than configured, generate warning about unused relays
        warnings = []
        if relay_num < len(available_relays):
            unused_relays = available_relays - configured_relays
            warnings.append(
                f"Only using {relay_num} relays but {len(available_relays)} are configured. "
                f"Unused relays: {', '.join(sorted(unused_relays))}"
            )

        # Validate that required relay numbers exist
        missing_relays = configured_relays - available_relays
        if missing_relays:
            return False, {
                'Relays': {
                    'missing': f'Missing configurations for relays: {", ".join(sorted(missing_relays))}'
                }
            }, []

        return True, {}, warnings

    @classmethod
    def validate_sensitivity_sections(cls, config: ConfigParser) -> Dict[str, Dict[str, str]]:
        """Validate [Sensitivity_*] preset sections have the required 9 keys with valid values."""
        errors = {}
        for section in config.sections():
            if not section.startswith('Sensitivity_'):
                continue
            section_errors = {}
            config_keys = set(config[section].keys())
            allowed_keys = cls.SENSITIVITY_SECTION_KEYS | cls.SENSITIVITY_SECTION_OPTIONAL_KEYS
            missing = cls.SENSITIVITY_SECTION_KEYS - config_keys
            if missing:
                section_errors['missing_keys'] = f"Missing required keys: {', '.join(sorted(missing))}"
            extra = config_keys - allowed_keys
            if extra:
                section_errors['extra_keys'] = f"Unexpected keys: {', '.join(sorted(extra))}"
            # Validate values are in range
            for key in allowed_keys & config_keys:
                value = config.get(section, key)
                used_pins = set()  # not relevant for sensitivity keys
                is_valid, error_msg = cls.validate_value(key, value, used_pins)
                if not is_valid:
                    section_errors[key] = value + f" - {error_msg}"
            if section_errors:
                errors[section] = section_errors
        return errors

    @classmethod
    def validate_controller_ini(cls, config_path: Path) -> None:
        """Verify CONTROLLER.ini exists alongside the detection config and has required sections."""
        controller_ini = config_path.parent / 'CONTROLLER.ini'
        if not controller_ini.exists():
            logger.warning(f"CONTROLLER.ini not found at {controller_ini} - infrastructure defaults will be used")
            return

        ctrl_config = ConfigParser()
        try:
            ctrl_config.read(controller_ini)
        except ConfigParserError as e:
            logger.warning(f"CONTROLLER.ini parse error: {e}")
            return

        missing = cls.CONTROLLER_INI_SECTIONS - set(ctrl_config.sections())
        if missing:
            logger.warning(
                f"CONTROLLER.ini missing sections: {', '.join(sorted(missing))}. "
                f"Infrastructure defaults will be used."
            )

    @classmethod
    def load_and_validate_config(cls, config_path: Path) -> ConfigParser:
        """Load and validate configuration file."""
        config = ConfigParser()
        used_pins = set()
        validation_errors = {}

        # File existence and parsing must still raise immediately
        # as we can't continue without a valid file
        if not config_path.exists():
            raise errors.ConfigFileError(config_path, "File does not exist")

        try:
            files_read = config.read(config_path)
            if not files_read:
                raise errors.ConfigFileError(config_path, "File could not be read")
        except ConfigParserError as e:
            raise errors.ConfigFileError(config_path, f"Parse error: {str(e)}")

        # Verify CONTROLLER.ini exists and has infrastructure sections
        cls.validate_controller_ini(config_path)

        # Create working copy of config requirements
        working_config = dict(cls.REQUIRED_CONFIG)

        # Validate controller specific rules
        is_valid, controller_errors = cls.validate_controller(config)
        if not is_valid:
            validation_errors.update(controller_errors)

        # Update controller requirements based on type
        controller_type = config.get('Controller', 'controller_type', fallback='').lower()
        required_keys, optional_keys = cls.get_controller_requirements(controller_type)
        working_config['Controller'] = {
            'required_keys': required_keys,
            'optional_keys': optional_keys
        }

        # Validate algorithm
        is_valid, algorithm_errors = cls.validate_algorithm(config)
        if not is_valid:
            validation_errors.update(algorithm_errors)

        # Validate camera type
        is_valid, camera_errors = cls.validate_camera_type(config)
        if not is_valid:
            validation_errors.update(camera_errors)

        # Validate white balance mode
        is_valid, awb_errors = cls.validate_awb_mode(config)
        if not is_valid:
            validation_errors.update(awb_errors)

        # Validate camera rotation
        is_valid, rotation_errors = cls.validate_rotation(config)
        if not is_valid:
            validation_errors.update(rotation_errors)

        # Validate sample method
        is_valid, sample_errors = cls.validate_sample_method(config)
        if not is_valid:
            validation_errors.update(sample_errors)

        # Validate storage location
        is_valid, storage_errors = cls.validate_storage_location(config)
        if not is_valid:
            validation_errors.update(storage_errors)

        # Validate actuation_mode if present
        if config.has_option('GreenOnGreen', 'actuation_mode'):
            act_mode = config.get('GreenOnGreen', 'actuation_mode').strip().lower()
            if act_mode and act_mode not in cls.VALID_ACTUATION_MODES:
                if 'GreenOnGreen' not in validation_errors:
                    validation_errors['GreenOnGreen'] = {}
                validation_errors['GreenOnGreen']['actuation_mode'] = (
                    f'Invalid actuation mode. Must be one of: {", ".join(sorted(cls.VALID_ACTUATION_MODES))}'
                )

        # Threshold validation
        is_valid, threshold_errors = cls.validate_thresholds(config)
        if not is_valid:
            validation_errors.update(threshold_errors)

        # Check required sections
        missing_sections = set(working_config.keys()) - set(config.sections())
        if missing_sections:
            validation_errors['missing_sections'] = {
                'sections': f"Missing required sections: {', '.join(missing_sections)}"
            }

        # Determine which controller pin keys are inactive (belong to a different controller type)
        # so their pin values don't cause false "already in use" conflicts
        controller_type = config.get('Controller', 'controller_type', fallback='none').strip("'\" ").lower()
        _UTE_PINS = {'switch_pin'}
        _ADVANCED_PINS = {'detection_mode_pin_up', 'detection_mode_pin_down', 'recording_pin', 'sensitivity_pin'}
        if controller_type == 'ute':
            inactive_pin_keys = _ADVANCED_PINS
        elif controller_type == 'advanced':
            inactive_pin_keys = _UTE_PINS
        else:
            inactive_pin_keys = _UTE_PINS | _ADVANCED_PINS

        # Validate sections and values
        for section in config.sections():
            # Skip Sensitivity_* preset sections — validated separately below
            if section.startswith('Sensitivity_'):
                continue
            section_errors = {}
            for key, value in config[section].items():
                # Skip pin conflict checks for controller pins that aren't active
                if section == 'Controller' and key in inactive_pin_keys:
                    continue
                is_valid, error_msg = cls.validate_value(key, value, used_pins)
                if not is_valid:
                    section_errors[key] = value + f" - {error_msg}"
            if section_errors:
                validation_errors[section] = section_errors

        # Validate relay pins against controller pins and GPIO validity
        is_valid, relay_pin_errors = cls.validate_relay_pin_conflicts(config, used_pins)
        if not is_valid:
            for section, errs in relay_pin_errors.items():
                if section in validation_errors:
                    validation_errors[section].update(errs)
                else:
                    validation_errors[section] = errs

        # Validate Sensitivity_* preset sections
        sensitivity_errors = cls.validate_sensitivity_sections(config)
        if sensitivity_errors:
            validation_errors.update(sensitivity_errors)

        # Validate relay configuration
        is_valid, relay_errors, relay_warnings = cls.validate_relays(config)
        if not is_valid:
            validation_errors.update(relay_errors)

        # Log any relay warnings
        for warning in relay_warnings:
            logger.warning(warning)

        # Check required keys in each section
        # Merge REQUIRED_CONFIG with OPTIONAL_SECTIONS for key checking
        all_section_defs = dict(working_config)
        for sec_name, sec_def in cls.OPTIONAL_SECTIONS.items():
            if sec_name not in all_section_defs:
                all_section_defs[sec_name] = {
                    'required_keys': set(),
                    'optional_keys': sec_def.get('optional_keys', set()),
                }

        for section in config.sections():
            # Skip Sensitivity_* — validated separately
            if section.startswith('Sensitivity_'):
                continue

            if section not in all_section_defs:
                # Skip sections not defined in our schema (e.g. MQTT, GPS — from CONTROLLER.ini)
                continue

            requirements = all_section_defs[section]
            config_keys = set(config[section].keys())
            required_keys = {k.lower() for k in requirements['required_keys']}
            optional_keys = {k.lower() for k in requirements.get('optional_keys', set())}

            missing_keys = required_keys - config_keys
            if missing_keys:
                if section not in validation_errors:
                    validation_errors[section] = {}
                validation_errors[section].update({
                    k: "Required key missing" for k in missing_keys
                })

            unknown_keys = config_keys - (required_keys | optional_keys)
            if unknown_keys:
                logger.warning(
                    f"Unknown keys in section [{section}]: {', '.join(unknown_keys)}"
                )

        # Raise all validation errors at once
        if validation_errors:
            raise errors.ConfigValueError(validation_errors, config_path)

        logger.info(f"Successfully loaded and validated config: {config_path}")
        return config