from pathlib import Path
from configparser import ConfigParser, Error as ConfigParserError
from typing import Dict, Set, Tuple

import logging
import utils.error_manager as errors

logger = logging.getLogger(__name__)

class ConfigValidator:
    """Validates OWL configuration files"""

    REQUIRED_CONFIG = {
        'System': {
            'required_keys': {'algorithm', 'relay_num', 'actuation_duration', 'delay', 'dashboard_enable'},
            'optional_keys': {'input_file_or_directory'}
        },
        'Controller': {
            # Base requirements for all controller types
            'required_keys': {'controller_type'},
            'optional_keys': {
                'detection_mode_pin_up',
                'detection_mode_pin_down',
                'recording_pin',
                'sensitivity_pin',
                'low_sensitivity_config',
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
                        'low_sensitivity_config',
                        'high_sensitivity_config'
                    },
                    'optional_keys': set()
                }
            }
        },
        'Camera': {
            'required_keys': {'resolution_width', 'resolution_height'},
            'optional_keys': {'exp_compensation'}
        },
        'GreenOnBrown': {
            'required_keys': {
                'exg_min', 'exg_max', 'hue_min', 'hue_max',
                'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
                'min_detection_area'
            },
            'optional_keys': {'invert_hue'}
        },
        'DataCollection': {
            'required_keys': {'image_sample_enable', 'sample_method', 'save_directory'},
            'optional_keys': {'sample_frequency', 'detection_enable', 'log_fps', 'camera_name'}
        },
        'Relays': {
            'required_keys': {'0', '1', '2', '3'},
            'optional_keys': set()
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
        'exp_compensation': ('float', -10, 10),
        # Detection confidence
        'confidence': ('float', 0, 1),
        # GPIO pins
        'switch_pin': ('pin', 1, 40),
        'detection_mode_pin_up': ('pin', 1, 40),
        'detection_mode_pin_down': ('pin', 1, 40),
        'recording_pin': ('pin', 1, 40),
        'sensitivity_pin': ('pin', 1, 40),
    }

    VALID_ALGORITHMS = {'exg', 'exgr', 'maxg', 'nexg', 'exhsv', 'hsv', 'gndvi', 'gog'}
    VALID_CONTROLLER_TYPES = {'none', 'ute', 'advanced'}
    VALID_SWITCH_PURPOSES = {'recording', 'sensitivity'}

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

        # For advanced controller, validate config files exist
        if controller_type == 'advanced':
            for config_key in ['low_sensitivity_config', 'high_sensitivity_config']:
                if config.has_option('Controller', config_key):
                    config_path = Path(config.get('Controller', config_key))
                    if not config_path.exists():
                        if 'Controller' not in controller_errors:
                            controller_errors['Controller'] = {}
                        controller_errors['Controller'][config_key] = f'Config file does not exist: {config_path}'

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

        if algorithm not in cls.VALID_ALGORITHMS:
            return False, {'System': {
                'algorithm': f'Invalid algorithm. Must be one of: {", ".join(sorted(cls.VALID_ALGORITHMS))}'
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

            elif val_type == 'pin':
                val = int(value)
                if min_val is not None and val < min_val:
                    return False, f"Pin must be >= {min_val}"
                if max_val is not None and val > max_val:
                    return False, f"Pin must be <= {max_val}"
                if val in used_pins:
                    return False, f"Pin {val} is already in use"
                used_pins.add(val)

        except ValueError:
            return False, f"Must be a valid {val_type}"

        return True, ""

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

        # Validate sections and values
        for section in config.sections():
            section_errors = {}
            for key, value in config[section].items():
                is_valid, error_msg = cls.validate_value(key, value, used_pins)
                if not is_valid:
                    section_errors[key] = value + f" - {error_msg}"
            if section_errors:
                validation_errors[section] = section_errors

        # Validate relay configuration
        is_valid, relay_errors, relay_warnings = cls.validate_relays(config)
        if not is_valid:
            validation_errors.update(relay_errors)

        # Log any relay warnings
        for warning in relay_warnings:
            logger.warning(warning)

        # Check required keys in each section
        for section, requirements in working_config.items():
            if section not in config.sections():
                continue  # Skip if section is missing - we've already recorded this error

            config_keys = set(config[section].keys())
            required_keys = {k.lower() for k in requirements['required_keys']}
            optional_keys = {k.lower() for k in requirements['optional_keys']}

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