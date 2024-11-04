from pathlib import Path
from configparser import ConfigParser, Error as ConfigParserError

import utils.error_manager as errors

class ConfigValidator:
    """Validates OWL configuration files"""

    # Define required sections and their required keys
    REQUIRED_CONFIG = {
        'System': {
            'required_keys': {'algorithm', 'relay_num', 'actuation_duration', 'delay'},
            'optional_keys': {'input_file_or_directory'}
        },
        'Controller': {
            'required_keys': {'controller_type'},
            'optional_keys': {
                'detection_mode_pin_up', 'detection_mode_pin_down',
                'recording_pin', 'sensitivity_pin', 'low_sensitivity_config',
                'high_sensitivity_config', 'switch_purpose', 'switch_pin'
            }
        },
        'Camera': {
            'required_keys': {'resolution_width', 'resolution_height'},
            'optional_keys': {'exp_compensation'}
        },
        'GreenOnBrown': {
            'required_keys': {
                'exgMin', 'exgMax', 'hueMin', 'hueMax',
                'saturationMin', 'saturationMax', 'brightnessMin', 'brightnessMax',
                'min_detection_area'
            },
            'optional_keys': {'invert_hue'}
        },
        'DataCollection': {
            'required_keys': {'sample_images', 'sample_method', 'save_directory'},
            'optional_keys': {'sample_frequency', 'disable_detection', 'log_fps', 'camera_name'}
        },
        'Relays': {
            'required_keys': {'0', '1', '2', '3'},
            'optional_keys': set()
        }
    }

    @classmethod
    def load_and_validate_config(cls, config_path: Path) -> ConfigParser:
        """
        Load and validate configuration file.

        Args:
            config_path: Path to configuration file

        Returns:
            ConfigParser: Validated configuration object

        Raises:
            ConfigFileError: If file cannot be loaded
            ConfigSectionError: If required sections are missing
            ConfigKeyError: If required keys are missing
        """
        config = ConfigParser()

        # Check file exists and can be read
        if not config_path.exists():
            raise errors.ConfigFileError(config_path, "File does not exist")

        try:
            files_read = config.read(config_path)
            if not files_read:
                raise errors.ConfigFileError(config_path, "File could not be read")
        except ConfigParserError as e:
            raise errors.ConfigFileError(config_path, f"Parse error: {str(e)}")

        # Check required sections
        missing_sections = set(cls.REQUIRED_CONFIG.keys()) - set(config.sections())
        if missing_sections:
            raise errors.ConfigSectionError(missing_sections, config_path)

        # Check required keys in each section
        for section, requirements in cls.REQUIRED_CONFIG.items():
            if section in config:
                missing_keys = requirements['required_keys'] - set(config[section].keys())
                if missing_keys:
                    raise errors.ConfigKeyError(section, missing_keys, config_path)

                # Optionally: check for unknown keys
                unknown_keys = set(config[section].keys()) - (
                        requirements['required_keys'] | requirements['optional_keys']
                )
                if unknown_keys:
                    logging.warning(
                        f"Unknown keys in section [{section}]: {', '.join(unknown_keys)}"
                    )

        return config
