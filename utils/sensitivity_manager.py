"""
Sensitivity preset manager for OWL.

Replaces the old three-file sensitivity system with embedded [Sensitivity_*]
sections inside GENERAL_CONFIG.ini.  Hardware settings live once in the main
sections and are NEVER touched by a preset switch — only the 8 GreenOnBrown
detection thresholds + min weed size (% of frame) change.
"""

import logging
import os
import time
import tempfile
import configparser
from pathlib import Path

logger = logging.getLogger(__name__)

# Protected config files that must never be written to directly (GEOMETRY.ini is
# device-resident mount config, written only by the geometry editor)
PROTECTED_CONFIGS = frozenset({'GENERAL_CONFIG.ini', 'CONTROLLER.ini', 'GEOMETRY.ini'})


class SensitivityManager:
    """Load, apply, save, and delete sensitivity presets."""

    SENSITIVITY_KEYS = frozenset({
        'exg_min', 'exg_max',
        'hue_min', 'hue_max',
        'saturation_min', 'saturation_max',
        'brightness_min', 'brightness_max',
    })

    # Canonical min weed size key: % of the detection frame (float).
    PERCENT_KEY = 'min_detection_area_percent'
    # Legacy px key — accepted when loading old [Sensitivity_*] sections and
    # converted to the percent key at apply time; never written to new presets.
    LEGACY_PX_KEY = 'min_detection_area'

    # Hardcoded fallbacks — used when config has no [Sensitivity_*] sections.
    # Percent values mirror the historical px values at the default 416x320 frame.
    BUILTIN_PRESETS = {
        'low': {
            'exg_min': 25, 'exg_max': 200,
            'hue_min': 41, 'hue_max': 80,
            'saturation_min': 52, 'saturation_max': 218,
            'brightness_min': 62, 'brightness_max': 188,
            'min_detection_area_percent': 0.015,
        },
        'medium': {
            'exg_min': 25, 'exg_max': 200,
            'hue_min': 39, 'hue_max': 83,
            'saturation_min': 50, 'saturation_max': 220,
            'brightness_min': 60, 'brightness_max': 190,
            'min_detection_area_percent': 0.0075,
        },
        'high': {
            'exg_min': 22, 'exg_max': 210,
            'hue_min': 35, 'hue_max': 85,
            'saturation_min': 40, 'saturation_max': 225,
            'brightness_min': 50, 'brightness_max': 200,
            'min_detection_area_percent': 0.004,
        },
    }

    BUILTIN_NAMES = frozenset(BUILTIN_PRESETS.keys())

    def __init__(self, config, config_path):
        """
        Parameters
        ----------
        config : configparser.ConfigParser
            The already-loaded config (from owl.py or standalone).
        config_path : str or Path
            Filesystem path to the INI file (for persisting changes).
        """
        self.config = config
        self.config_path = str(config_path)
        self._presets = {}  # name -> {key: int_value, ...}
        self._load_presets()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_active_preset(self):
        """Return the name of the currently active preset (default 'medium')."""
        if self.config.has_section('Sensitivity'):
            return self.config.get('Sensitivity', 'active', fallback='medium').lower()
        return 'medium'

    def list_presets(self):
        """Return list of dicts: [{name, values, is_builtin}, ...]."""
        result = []
        for name, values in self._presets.items():
            result.append({
                'name': name,
                'values': dict(values),
                'is_builtin': name in self.BUILTIN_NAMES,
            })
        return result

    def get_preset_values(self, name):
        """Return dict of threshold values for *name*, or None."""
        name = name.lower()
        return dict(self._presets[name]) if name in self._presets else None

    def apply_preset(self, name, owl_instance):
        """Copy preset values to *owl_instance* attributes.

        Returns True on success, False if preset not found.
        """
        name = name.lower()
        values = self.get_preset_values(name)
        if values is None:
            logger.error(f"Unknown sensitivity preset: {name}")
            return False

        # Legacy preset sections carry only the px key — convert it so the
        # min-area component still takes effect (owl.py uses the percent key)
        if self.PERCENT_KEY not in values and self.LEGACY_PX_KEY in values:
            try:
                res = getattr(owl_instance, 'resolution', None) or (416, 320)
                area = res[0] * res[1]
                values[self.PERCENT_KEY] = max(0.0005, min(
                    5.0, values[self.LEGACY_PX_KEY] / area * 100))
            except (TypeError, IndexError, ZeroDivisionError):
                pass

        for key, val in values.items():
            setattr(owl_instance, key, val)

        # Queue trackbar updates for main thread (cv2 HighGUI not thread-safe)
        if getattr(owl_instance, 'show_display', False):
            trackbar_map = {
                'exg_min': 'ExG-Min', 'exg_max': 'ExG-Max',
                'hue_min': 'Hue-Min', 'hue_max': 'Hue-Max',
                'saturation_min': 'Sat-Min', 'saturation_max': 'Sat-Max',
                'brightness_min': 'Bright-Min', 'brightness_max': 'Bright-Max',
            }
            pending = getattr(owl_instance, '_pending_trackbar_updates', None)
            if pending is not None:
                for key, trackbar_name in trackbar_map.items():
                    if key in values:
                        pending[trackbar_name] = values[key]

        # Update the active marker in config (runtime only until persist)
        if not self.config.has_section('Sensitivity'):
            self.config.add_section('Sensitivity')
        self.config.set('Sensitivity', 'active', name)

        logger.info(f"Applied sensitivity preset: {name}")
        return True

    def save_custom_preset(self, name, values=None, owl_instance=None):
        """Save current slider values as a custom preset.

        Either pass *values* (dict) directly, or pass *owl_instance* to
        read the threshold keys from its attributes.

        Returns True on success.
        """
        name = name.lower().strip()
        if not name:
            logger.error("Cannot save preset with empty name")
            return False

        if values is None and owl_instance is not None:
            values = {k: getattr(owl_instance, k) for k in self.SENSITIVITY_KEYS}
            percent = getattr(owl_instance, self.PERCENT_KEY, None)
            if percent is not None:
                values[self.PERCENT_KEY] = percent
        if values is None:
            logger.error("No values provided for preset save")
            return False

        # Ensure the 8 threshold keys are present
        missing = self.SENSITIVITY_KEYS - set(values.keys())
        if missing:
            logger.error(f"Missing keys for preset: {missing}")
            return False

        # Store int thresholds + the canonical float percent key. A legacy px
        # value is converted rather than written (new presets are percent-only).
        preset_values = {k: int(values[k]) for k in self.SENSITIVITY_KEYS}
        if self.PERCENT_KEY in values:
            preset_values[self.PERCENT_KEY] = float(values[self.PERCENT_KEY])
        elif self.LEGACY_PX_KEY in values:
            res = getattr(owl_instance, 'resolution', None) or (416, 320)
            try:
                area = res[0] * res[1]
            except (TypeError, IndexError):
                area = 416 * 320
            preset_values[self.PERCENT_KEY] = max(0.0005, min(
                5.0, float(values[self.LEGACY_PX_KEY]) / area * 100))
        self._presets[name] = preset_values

        # Write to config
        section = self._section_name(name)
        if not self.config.has_section(section):
            self.config.add_section(section)
        for k, v in preset_values.items():
            self.config.set(section, k, str(v))

        self.persist()
        logger.info(f"Saved custom preset: {name}")
        return True

    def delete_custom_preset(self, name):
        """Delete a custom preset. Builtins cannot be deleted.

        Returns True on success, False on failure.
        """
        name = name.lower().strip()
        if name in self.BUILTIN_NAMES:
            logger.error(f"Cannot delete builtin preset: {name}")
            return False

        if name not in self._presets:
            logger.error(f"Preset not found: {name}")
            return False

        del self._presets[name]
        section = self._section_name(name)
        if self.config.has_section(section):
            self.config.remove_section(section)

        # If we deleted the active preset, fall back to medium
        if self.get_active_preset() == name:
            self.config.set('Sensitivity', 'active', 'medium')

        self.persist()
        logger.info(f"Deleted custom preset: {name}")
        return True

    def persist(self):
        """Write config to disk safely.

        If the config path is a protected default (GENERAL_CONFIG.ini),
        creates a timestamped copy and updates active_config.txt so
        neither owl.py nor the dashboard pollute the template.
        """
        try:
            target_path = self.config_path
            basename = os.path.basename(target_path)

            from utils.config_manager import AUTOSAVE_CONFIG
            if basename != AUTOSAVE_CONFIG:
                # Frozen-preset model: templates AND named presets stay
                # untouched — live changes divert to the single autosave
                # working file, overwritten in place
                config_dir = os.path.dirname(target_path)
                new_name = AUTOSAVE_CONFIG
                target_path = os.path.join(config_dir, new_name)
                logger.info(
                    f"Unsaved change to {basename} — writing to {new_name}"
                )

                # Record which file the working copy derives from
                if not self.config.has_section('Meta'):
                    self.config.add_section('Meta')
                self.config.set('Meta', 'source', basename)

                # Update active_config.txt so owl.py uses the new file
                pointer_path = os.path.join(config_dir, 'active_config.txt')
                try:
                    with open(pointer_path, 'w') as f:
                        f.write(f'config/{new_name}')
                except Exception as e:
                    logger.error(f"Failed to update active_config.txt: {e}")

                # Future writes go to the new file
                self.config_path = target_path

            dir_name = os.path.dirname(target_path)
            fd, tmp_path = tempfile.mkstemp(
                suffix='.ini', prefix='.owl_cfg_', dir=dir_name or '.'
            )
            with os.fdopen(fd, 'w') as f:
                self.config.write(f)
            # Atomic rename (works on POSIX; on Windows replaces dest)
            os.replace(tmp_path, target_path)
            logger.info(f"Config persisted to {target_path}")
        except Exception as e:
            logger.error(f"Failed to persist config: {e}")
            # Clean up temp file if rename failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_presets(self):
        """Populate self._presets from config sections + builtin fallbacks."""
        # Start with builtins
        for name, values in self.BUILTIN_PRESETS.items():
            self._presets[name] = dict(values)

        # Override / add from config [Sensitivity_*] sections
        for section in self.config.sections():
            if not section.startswith('Sensitivity_'):
                continue
            name = section[len('Sensitivity_'):].lower()
            try:
                values = {}
                for key in self.SENSITIVITY_KEYS:
                    values[key] = self.config.getint(section, key)
                # Canonical percent key; legacy sections carry the px key
                # instead (converted to percent at apply time)
                if self.config.has_option(section, self.PERCENT_KEY):
                    values[self.PERCENT_KEY] = self.config.getfloat(
                        section, self.PERCENT_KEY)
                elif self.config.has_option(section, self.LEGACY_PX_KEY):
                    values[self.LEGACY_PX_KEY] = self.config.getint(
                        section, self.LEGACY_PX_KEY)
                self._presets[name] = values
            except (configparser.NoOptionError, ValueError) as e:
                logger.warning(f"Skipping malformed preset [{section}]: {e}")

    @staticmethod
    def _section_name(preset_name):
        """Convert preset name to INI section: 'low' -> 'Sensitivity_Low'."""
        return f'Sensitivity_{preset_name.capitalize()}'
