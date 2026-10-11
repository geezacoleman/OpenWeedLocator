"""Tests for custom algorithm management (create, validate, load, deploy, delete)."""

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from custom_algorithms import (
    CUSTOM_ALGO_DIR,
    validate_algorithm_code,
    save_algorithm,
    load_custom_algorithm,
    discover_custom_algorithms,
    delete_algorithm,
    list_algorithms,
    get_algorithm_code,
    _validate_name,
)


# ---------------------------------------------------------------------------
# Fixtures — use a temp directory so tests don't pollute the real dir
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def temp_algo_dir(tmp_path, monkeypatch):
    """Redirect CUSTOM_ALGO_DIR to a temp directory for test isolation."""
    import custom_algorithms
    monkeypatch.setattr(custom_algorithms, 'CUSTOM_ALGO_DIR', tmp_path)
    return tmp_path


VALID_ALGO_CODE = """\
import numpy as np
import cv2

def bright_green(image):
    blue, green, red = cv2.split(image)
    return (2.0 * green.astype(np.float32) - red.astype(np.float32) - blue.astype(np.float32)).clip(0, 255).astype('uint8')
"""

VALID_BINARY_ALGO = """\
import numpy as np
import cv2

def green_mask(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (30, 40, 40), (80, 255, 255))
    return mask, True
"""


# ---------------------------------------------------------------------------
# AST validation
# ---------------------------------------------------------------------------

class TestValidateCode:
    def test_valid_algorithm(self):
        ok, err = validate_algorithm_code(VALID_ALGO_CODE)
        assert ok is True
        assert err == ''

    def test_valid_binary_algorithm(self):
        ok, err = validate_algorithm_code(VALID_BINARY_ALGO)
        assert ok is True

    def test_rejects_empty_code(self):
        ok, err = validate_algorithm_code('')
        assert ok is False
        assert 'empty' in err.lower()

    def test_rejects_import_os(self):
        code = 'import os\ndef algo(image): return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'os' in err

    def test_rejects_import_subprocess(self):
        code = 'import subprocess\ndef algo(image): return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'subprocess' in err

    def test_rejects_from_os_import(self):
        code = 'from os import path\ndef algo(image): return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'os' in err

    def test_rejects_exec(self):
        code = 'import numpy as np\ndef algo(image):\n    exec("pass")\n    return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'exec' in err

    def test_rejects_eval(self):
        code = 'import numpy as np\ndef algo(image):\n    return eval("image[:,:,1]")'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'eval' in err

    def test_rejects_open(self):
        code = 'import numpy as np\ndef algo(image):\n    open("/etc/passwd")\n    return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'open' in err

    def test_rejects_dunder_import(self):
        code = 'import numpy as np\ndef algo(image):\n    __import__("os")\n    return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert '__import__' in err

    def test_rejects_no_image_param(self):
        code = 'import numpy as np\ndef algo(data): return data'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'image' in err.lower()

    def test_rejects_syntax_error(self):
        code = 'def algo(image):\n    return [[[['
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'syntax' in err.lower()

    def test_allows_numpy_import(self):
        code = 'import numpy as np\ndef algo(image): return np.zeros((100,100), dtype=np.uint8)'
        ok, err = validate_algorithm_code(code)
        assert ok is True

    def test_allows_cv2_import(self):
        code = 'import cv2\ndef algo(image): return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)'
        ok, err = validate_algorithm_code(code)
        assert ok is True

    def test_allows_math_import(self):
        code = 'import math\nimport numpy as np\ndef algo(image): return np.full(image.shape[:2], int(math.pi), dtype=np.uint8)'
        ok, err = validate_algorithm_code(code)
        assert ok is True

    def test_rejects_import_shutil(self):
        code = 'import shutil\ndef algo(image): return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'shutil' in err

    def test_rejects_import_sys(self):
        code = 'import sys\ndef algo(image): return image[:,:,1]'
        ok, err = validate_algorithm_code(code)
        assert ok is False
        assert 'sys' in err


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

class TestNameValidation:
    def test_valid_name(self):
        ok, err = _validate_name('bright_green')
        assert ok is True

    def test_valid_single_char(self):
        ok, err = _validate_name('a')
        assert ok is True

    def test_rejects_empty(self):
        ok, err = _validate_name('')
        assert ok is False

    def test_rejects_path_traversal(self):
        ok, err = _validate_name('../evil')
        assert ok is False

    def test_rejects_uppercase(self):
        ok, err = _validate_name('BrightGreen')
        assert ok is False

    def test_rejects_special_chars(self):
        ok, err = _validate_name('algo-name')
        assert ok is False

    def test_rejects_too_long(self):
        ok, err = _validate_name('a' * 32)
        assert ok is False

    def test_rejects_starts_with_number(self):
        ok, err = _validate_name('1algo')
        assert ok is False

    def test_rejects_dunder_init(self):
        ok, err = _validate_name('__init__')
        assert ok is False

    def test_allows_underscores(self):
        ok, err = _validate_name('my_custom_algo')
        assert ok is True

    def test_allows_numbers(self):
        ok, err = _validate_name('algo2')
        assert ok is True


# ---------------------------------------------------------------------------
# Save / load / discover round-trip
# ---------------------------------------------------------------------------

class TestSaveLoadDiscover:
    def test_save_and_load(self, temp_algo_dir):
        result = save_algorithm('bright_green', VALID_ALGO_CODE, 'A bright green detector')
        assert result['success'] is True
        assert result['name'] == 'bright_green'

        func = load_custom_algorithm('bright_green')
        assert func is not None
        assert callable(func)

    def test_save_rejects_bad_code(self, temp_algo_dir):
        result = save_algorithm('bad', 'import os\ndef algo(image): pass')
        assert result['success'] is False
        assert 'validation' in result['error'].lower()

    def test_save_rejects_bad_name(self, temp_algo_dir):
        result = save_algorithm('../evil', VALID_ALGO_CODE)
        assert result['success'] is False
        assert 'name' in result['error'].lower()

    def test_discover_finds_saved(self, temp_algo_dir):
        save_algorithm('algo_a', VALID_ALGO_CODE)
        save_algorithm('algo_b', VALID_ALGO_CODE)

        algos = discover_custom_algorithms()
        assert 'algo_a' in algos
        assert 'algo_b' in algos
        assert callable(algos['algo_a'])

    def test_discover_skips_init(self, temp_algo_dir):
        algos = discover_custom_algorithms()
        assert '__init__' not in algos

    def test_load_nonexistent_returns_none(self, temp_algo_dir):
        func = load_custom_algorithm('nonexistent')
        assert func is None

    def test_list_returns_metadata(self, temp_algo_dir):
        save_algorithm('test_algo', VALID_ALGO_CODE, 'Test description')
        algos = list_algorithms()
        assert len(algos) == 1
        assert algos[0]['name'] == 'test_algo'
        assert 'Test description' in algos[0]['description']
        assert 'modified' in algos[0]

    def test_get_algorithm_code(self, temp_algo_dir):
        save_algorithm('myalgo', VALID_ALGO_CODE)
        code = get_algorithm_code('myalgo')
        assert code is not None
        assert 'bright_green' in code  # function name from VALID_ALGO_CODE

    def test_get_algorithm_code_nonexistent(self, temp_algo_dir):
        code = get_algorithm_code('nonexistent')
        assert code is None

    def test_save_adds_docstring(self, temp_algo_dir):
        code = 'import numpy as np\ndef algo(image): return image[:,:,1]'
        save_algorithm('doctest', code, 'My description')
        saved = get_algorithm_code('doctest')
        assert saved.startswith('"""My description"""')


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_existing(self, temp_algo_dir):
        save_algorithm('to_delete', VALID_ALGO_CODE)
        result = delete_algorithm('to_delete')
        assert result['success'] is True
        assert not (temp_algo_dir / 'to_delete.py').exists()

    def test_delete_nonexistent(self, temp_algo_dir):
        result = delete_algorithm('nonexistent')
        assert result['success'] is False
        assert 'not found' in result['error'].lower()

    def test_delete_bad_name(self, temp_algo_dir):
        result = delete_algorithm('../evil')
        assert result['success'] is False


# ---------------------------------------------------------------------------
# Algorithm execution on synthetic image
# ---------------------------------------------------------------------------

class TestAlgorithmExecution:
    def test_custom_algo_runs(self, temp_algo_dir):
        """Custom algorithm produces correct shape output on real image."""
        import numpy as np
        save_algorithm('test_exec', VALID_ALGO_CODE)
        func = load_custom_algorithm('test_exec')
        assert func is not None

        image = np.full((480, 640, 3), (60, 80, 120), dtype=np.uint8)
        output = func(image)
        assert isinstance(output, np.ndarray)
        assert output.shape == (480, 640)
        assert output.dtype == np.uint8

    def test_binary_algo_returns_tuple(self, temp_algo_dir):
        """Binary algorithm returns (image, True) tuple."""
        import numpy as np
        save_algorithm('test_binary', VALID_BINARY_ALGO)
        func = load_custom_algorithm('test_binary')
        assert func is not None

        image = np.full((480, 640, 3), (60, 80, 120), dtype=np.uint8)
        result = func(image)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[1] is True
        assert result[0].shape == (480, 640)


# ---------------------------------------------------------------------------
# GreenOnBrown integration
# ---------------------------------------------------------------------------

class TestGreenOnBrownIntegration:
    def test_discovers_custom_algos(self, temp_algo_dir):
        """GreenOnBrown.__init__ picks up custom algorithms."""
        save_algorithm('custom_exg', VALID_ALGO_CODE)
        from utils.greenonbrown import GreenOnBrown
        gob = GreenOnBrown()
        assert 'custom_exg' in gob.algorithms
        assert callable(gob.algorithms['custom_exg'])

    def test_builtin_algos_preserved(self, temp_algo_dir):
        """Custom algo discovery doesn't break builtins."""
        from utils.greenonbrown import GreenOnBrown
        gob = GreenOnBrown()
        assert 'exg' in gob.algorithms
        assert 'exhsv' in gob.algorithms


# ---------------------------------------------------------------------------
# ConfigValidator integration
# ---------------------------------------------------------------------------

class TestConfigValidatorIntegration:
    def test_get_valid_algorithms_includes_custom(self, temp_algo_dir):
        """ConfigValidator.get_valid_algorithms() includes custom names."""
        save_algorithm('my_custom', VALID_ALGO_CODE)
        from utils.config_manager import ConfigValidator
        valid = ConfigValidator.get_valid_algorithms()
        assert 'my_custom' in valid
        assert 'exg' in valid  # builtins still present
        assert 'exhsv' in valid


# ---------------------------------------------------------------------------
# GreenOnBrown.inference() with custom tuple-returning algorithms
# ---------------------------------------------------------------------------

class TestInferenceTupleReturn:
    """Regression: custom algo returning (mask, True) must not crash inference()."""

    def test_binary_algo_through_inference(self, temp_algo_dir):
        """GreenOnBrown.inference() handles (image, True) return from custom algo."""
        import numpy as np
        save_algorithm('orange_hsv', VALID_BINARY_ALGO)
        from utils.greenonbrown import GreenOnBrown

        gob = GreenOnBrown()
        assert 'orange_hsv' in gob.algorithms

        image = np.full((480, 640, 3), (60, 80, 120), dtype=np.uint8)
        # This would crash with "inhomogeneous shape" before the fix
        cnts, boxes, centres, out = gob.inference(
            image, algorithm='orange_hsv', show_display=False
        )
        assert isinstance(boxes, list)
        assert isinstance(centres, list)

    def test_grayscale_algo_through_inference(self, temp_algo_dir):
        """GreenOnBrown.inference() handles plain ndarray return from custom algo."""
        import numpy as np
        save_algorithm('custom_exg', VALID_ALGO_CODE)
        from utils.greenonbrown import GreenOnBrown

        gob = GreenOnBrown()
        image = np.full((480, 640, 3), (60, 80, 120), dtype=np.uint8)
        cnts, boxes, centres, out = gob.inference(
            image, algorithm='custom_exg', show_display=False
        )
        assert isinstance(boxes, list)
        assert isinstance(centres, list)


# ---------------------------------------------------------------------------
# Params dict passing — custom algorithms can receive threshold values
# ---------------------------------------------------------------------------

PARAMS_AWARE_ALGO = """\
import numpy as np
import cv2

def threshold_aware(image, params):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue_min = params.get('hue_min', 30)
    hue_max = params.get('hue_max', 90)
    sat_min = params.get('saturation_min', 30)
    sat_max = params.get('saturation_max', 255)
    bri_min = params.get('brightness_min', 5)
    bri_max = params.get('brightness_max', 200)
    mask = cv2.inRange(hsv,
        np.array([hue_min, sat_min, bri_min], dtype=np.uint8),
        np.array([hue_max, sat_max, bri_max], dtype=np.uint8))
    return mask, True
"""


class TestParamsPassthrough:
    """Verify custom algorithms receive the params dict from inference()."""

    def test_params_aware_algo_receives_thresholds(self, temp_algo_dir):
        """Algorithm with (image, params) signature gets the params dict."""
        import numpy as np
        save_algorithm('threshold_aware', PARAMS_AWARE_ALGO)
        from utils.greenonbrown import GreenOnBrown

        gob = GreenOnBrown()
        image = np.full((480, 640, 3), (60, 80, 120), dtype=np.uint8)
        # Should not raise — params dict is passed
        cnts, boxes, centres, out = gob.inference(
            image, algorithm='threshold_aware', show_display=False,
            hue_min=10, hue_max=80,
        )
        assert isinstance(boxes, list)

    def test_old_algo_without_params_still_works(self, temp_algo_dir):
        """Algorithm with only (image) signature still works via fallback."""
        import numpy as np
        save_algorithm('old_style', VALID_ALGO_CODE)
        from utils.greenonbrown import GreenOnBrown

        gob = GreenOnBrown()
        image = np.full((480, 640, 3), (60, 80, 120), dtype=np.uint8)
        # Should not raise — falls back to func(image)
        cnts, boxes, centres, out = gob.inference(
            image, algorithm='old_style', show_display=False,
        )
        assert isinstance(boxes, list)

    def test_params_values_match_inference_args(self, temp_algo_dir):
        """Verify the params dict contains the actual threshold values passed to inference."""
        import numpy as np
        # Algorithm that stores params for inspection
        code = """\
import numpy as np

_last_params = {}

def spy_algo(image, params):
    global _last_params
    _last_params = dict(params)
    return np.zeros(image.shape[:2], dtype=np.uint8)
"""
        save_algorithm('spy_algo', code)
        from utils.greenonbrown import GreenOnBrown

        gob = GreenOnBrown()
        image = np.full((100, 100, 3), 128, dtype=np.uint8)
        gob.inference(
            image, algorithm='spy_algo', show_display=False,
            exg_min=42, exg_max=200, hue_min=15, hue_max=75,
            saturation_min=50, saturation_max=220,
            brightness_min=10, brightness_max=180,
        )

        # Get the params that were passed
        mod = load_custom_algorithm.__module__
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'spy', str(temp_algo_dir / 'spy_algo.py'))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        # The _last_params won't be accessible from the reloaded module,
        # so verify via the algorithm dict directly
        func = gob.algorithms.get('spy_algo')
        assert func is not None
        # Run it directly to capture params
        captured = {}
        def capture_algo(image, params):
            captured.update(params)
            return np.zeros(image.shape[:2], dtype=np.uint8)
        gob.algorithms['spy_algo'] = capture_algo
        gob.inference(
            image, algorithm='spy_algo', show_display=False,
            exg_min=42, exg_max=200, hue_min=15, hue_max=75,
        )
        assert captured['exg_min'] == 42
        assert captured['exg_max'] == 200
        assert captured['hue_min'] == 15
        assert captured['hue_max'] == 75
