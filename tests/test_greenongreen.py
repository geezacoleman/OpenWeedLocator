"""
Unit tests for GreenOnGreen (Ultralytics YOLO) weed detection.

Mocks the YOLO model to avoid model file dependency in CI.
Run: pytest tests/test_greenongreen.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import cv2
import numpy as np
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def make_mock_yolo(task='detect', names=None):
    """Create a mock YOLO model with configurable task and class names."""
    mock_model = MagicMock()
    mock_model.task = task
    mock_model.names = names or {0: 'weed', 1: 'crop'}

    # Set up predict to return mock results
    mock_result = MagicMock()

    # Mock boxes
    mock_box = MagicMock()
    mock_box.xyxy = [np.array([100, 50, 200, 150])]
    mock_box.conf = [np.array([0.85])]
    mock_box.cls = [np.array([0])]
    mock_result.boxes = [mock_box]

    # Mock masks (None for detection, populated for segmentation)
    if task == 'segment':
        mock_mask_xy = np.array([[100, 50], [200, 50], [200, 150], [100, 150]], dtype=np.float32)
        mock_result.masks = MagicMock()
        mock_result.masks.xy = [mock_mask_xy]
    else:
        mock_result.masks = None

    mock_model.predict.return_value = [mock_result]
    return mock_model


class TestModelDiscovery:
    """Test _load_model finds models in various directory structures."""

    @patch('utils.greenongreen.YOLO')
    def test_load_ncnn_model_dir(self, mock_yolo_cls, tmp_path):
        """NCNN model directory (has .param file) loads directly."""
        # Create fake NCNN model dir
        (tmp_path / 'model.param').touch()
        (tmp_path / 'model.bin').touch()

        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        mock_yolo_cls.assert_called_once_with(str(tmp_path), task=None)

    @patch('utils.greenongreen.YOLO')
    def test_load_ncnn_subdir(self, mock_yolo_cls, tmp_path):
        """Parent dir with NCNN subdirectory finds the subdir."""
        ncnn_dir = tmp_path / 'yolo11n_ncnn_model'
        ncnn_dir.mkdir()
        (ncnn_dir / 'model.param').touch()
        (ncnn_dir / 'model.bin').touch()

        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        mock_yolo_cls.assert_called_once_with(str(ncnn_dir), task=None)

    @patch('utils.greenongreen.YOLO')
    def test_load_pt_file_in_dir(self, mock_yolo_cls, tmp_path):
        """Parent dir with .pt file loads the .pt file."""
        pt_file = tmp_path / 'best.pt'
        pt_file.touch()

        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        mock_yolo_cls.assert_called_once_with(str(pt_file), task=None)

    @patch('utils.greenongreen.YOLO')
    def test_load_exact_pt_path(self, mock_yolo_cls, tmp_path):
        """Exact .pt file path loads directly."""
        pt_file = tmp_path / 'my_model.pt'
        pt_file.touch()

        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(pt_file))

        mock_yolo_cls.assert_called_once_with(str(pt_file), task=None)

    @patch('utils.greenongreen.YOLO')
    def test_load_seg_model_infers_task(self, mock_yolo_cls, tmp_path):
        """Model with '-seg' in name passes task='segment' to YOLO."""
        pt_file = tmp_path / 'yolo11n-seg_best.pt'
        pt_file.touch()

        mock_yolo_cls.return_value = make_mock_yolo(task='segment')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(pt_file))

        mock_yolo_cls.assert_called_once_with(str(pt_file), task='segment')

    @patch('utils.greenongreen.YOLO')
    def test_ncnn_preferred_over_pt(self, mock_yolo_cls, tmp_path):
        """NCNN subdirectory is preferred over .pt file in same parent dir."""
        ncnn_dir = tmp_path / 'model_ncnn'
        ncnn_dir.mkdir()
        (ncnn_dir / 'model.param').touch()
        (tmp_path / 'model.pt').touch()

        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        # Should have loaded the NCNN dir, not the .pt
        mock_yolo_cls.assert_called_once_with(str(ncnn_dir), task=None)

    def test_no_models_raises(self, tmp_path):
        """Empty directory raises FileNotFoundError."""
        from utils.greenongreen import GreenOnGreen
        with pytest.raises(FileNotFoundError, match='No YOLO models found'):
            GreenOnGreen(model_path=str(tmp_path))

    def test_nonexistent_path_raises(self):
        """Non-existent path raises FileNotFoundError."""
        from utils.greenongreen import GreenOnGreen
        with pytest.raises(FileNotFoundError, match='does not exist'):
            GreenOnGreen(model_path='/nonexistent/path/to/model')


class TestClassResolution:
    """Test _resolve_classes maps names to IDs correctly."""

    @patch('utils.greenongreen.YOLO')
    def test_resolve_valid_classes(self, mock_yolo_cls, tmp_path):
        """Valid class names resolve to model IDs."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo(names={0: 'weed', 1: 'crop', 2: 'grass'})
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), detect_classes=['weed', 'grass'])

        assert gog._detect_class_ids == [0, 2]

    @patch('utils.greenongreen.YOLO')
    def test_resolve_case_insensitive(self, mock_yolo_cls, tmp_path):
        """Class name matching is case-insensitive."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo(names={0: 'Weed', 1: 'Crop'})
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), detect_classes=['WEED'])

        assert gog._detect_class_ids == [0]

    @patch('utils.greenongreen.YOLO')
    def test_resolve_invalid_class_warns(self, mock_yolo_cls, tmp_path):
        """Invalid class names are skipped with a warning."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo(names={0: 'weed', 1: 'crop'})
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), detect_classes=['nonexistent'])

        # All invalid -> returns None (detect all)
        assert gog._detect_class_ids is None

    @patch('utils.greenongreen.YOLO')
    def test_resolve_empty_list(self, mock_yolo_cls, tmp_path):
        """Empty class list means detect all."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), detect_classes=None)

        assert gog._detect_class_ids is None

    @patch('utils.greenongreen.YOLO')
    def test_class_names_property(self, mock_yolo_cls, tmp_path):
        """class_names property returns model.names dict."""
        (tmp_path / 'model.pt').touch()
        names = {0: 'weed', 1: 'crop', 2: 'grass'}
        mock_yolo_cls.return_value = make_mock_yolo(names=names)

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        assert gog.class_names == names


class TestInference:
    """Test inference method return signature and display modes."""

    @patch('utils.greenongreen.YOLO')
    def test_return_signature_detection(self, mock_yolo_cls, tmp_path):
        """Detection model returns (None, boxes, centres, image)."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        contours, boxes, weed_centres, image_out = gog.inference(image)

        assert contours is None
        assert isinstance(boxes, list)
        assert isinstance(weed_centres, list)
        assert len(boxes) == 1
        assert len(weed_centres) == 1
        # Verify box format [x, y, w, h]
        assert len(boxes[0]) == 4
        # Verify centre format [cx, cy]
        assert len(weed_centres[0]) == 2

    @patch('utils.greenongreen.YOLO')
    def test_return_signature_segmentation(self, mock_yolo_cls, tmp_path):
        """Segmentation model returns (contours, boxes, centres, image)."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='segment')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        contours, boxes, weed_centres, image_out = gog.inference(image)

        assert contours is not None
        assert isinstance(contours, list)
        assert len(contours) == 1

    @patch('utils.greenongreen.YOLO')
    def test_show_display_false_returns_original(self, mock_yolo_cls, tmp_path):
        """show_display=False returns the original image (not a copy)."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        _, _, _, image_out = gog.inference(image, show_display=False)

        # Same object, not a copy
        assert image_out is image

    @patch('utils.greenongreen.YOLO')
    def test_show_display_true_returns_copy(self, mock_yolo_cls, tmp_path):
        """show_display=True returns an annotated copy."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        _, _, _, image_out = gog.inference(image, show_display=True)

        # Different object (copy)
        assert image_out is not image

    @patch('utils.greenongreen.YOLO')
    def test_build_mask_segmentation(self, mock_yolo_cls, tmp_path):
        """build_mask=True with segmentation model creates detection_mask."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='segment')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image, build_mask=True)

        assert gog.detection_mask is not None
        assert gog.detection_mask.shape == (480, 640)
        assert gog.detection_mask.dtype == np.uint8

    @patch('utils.greenongreen.YOLO')
    def test_build_mask_false_no_mask(self, mock_yolo_cls, tmp_path):
        """build_mask=False skips mask construction."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='segment')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image, build_mask=False)

        assert gog.detection_mask is None

    @patch('utils.greenongreen.YOLO')
    def test_build_mask_detection_model(self, mock_yolo_cls, tmp_path):
        """build_mask=True with detection model leaves mask as None."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image, build_mask=True)

        assert gog.detection_mask is None

    @patch('utils.greenongreen.YOLO')
    def test_no_detections_returns_empty(self, mock_yolo_cls, tmp_path):
        """No detections returns empty lists."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo(task='detect')
        # Override predict to return empty result
        mock_result = MagicMock()
        mock_result.boxes = []
        mock_result.masks = None
        mock_model.predict.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        contours, boxes, weed_centres, _ = gog.inference(image)

        assert contours is None
        assert boxes == []
        assert weed_centres == []


class TestZoneActuation:
    """Test zone actuation mask logic."""

    @patch('utils.greenongreen.YOLO')
    def test_zone_mask_covers_correct_area(self, mock_yolo_cls, tmp_path):
        """Detection mask has pixels in the correct region."""
        (tmp_path / 'model.pt').touch()

        # Create mock with specific mask coordinates
        mock_model = make_mock_yolo(task='segment')
        mock_result = MagicMock()
        mock_box = MagicMock()
        mock_box.xyxy = [np.array([100, 50, 200, 150])]
        mock_box.conf = [np.array([0.9])]
        mock_box.cls = [np.array([0])]
        mock_result.boxes = [mock_box]

        # Mask polygon covering x=100-200, y=50-150
        mask_xy = np.array([[100, 50], [200, 50], [200, 150], [100, 150]], dtype=np.float32)
        mock_result.masks = MagicMock()
        mock_result.masks.xy = [mask_xy]
        mock_model.predict.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image, build_mask=True)

        # Check that pixels exist in the mask region
        mask = gog.detection_mask
        assert np.count_nonzero(mask[:, 100:200]) > 0
        # Check that pixels outside are zero
        assert np.count_nonzero(mask[:, 0:50]) == 0
        assert np.count_nonzero(mask[:, 300:640]) == 0


class TestHybridMode:
    """Test hybrid pipeline: YOLO crop mask + ExHSV weed detection."""

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_init_creates_gob(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """hybrid_mode=True creates internal GreenOnBrown."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True)

        assert gog.hybrid_mode is True
        assert gog._gob is not None
        mock_gob_cls.assert_called_once_with(algorithm='exhsv')

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_returns_standard_tuple(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """Hybrid inference returns 4-element tuple (contours, boxes, centres, image)."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        # Mock GreenOnBrown.inference to return a weed detection
        mock_gob = MagicMock()
        mock_gob.inference.return_value = (
            None,                                   # contours
            [[300, 200, 40, 40]],                   # boxes (outside crop zone)
            [[320, 220]],                           # centres
            np.zeros((480, 640, 3), dtype=np.uint8) # image
        )
        mock_gob_cls.return_value = mock_gob

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        result = gog.inference(image)

        assert len(result) == 4
        contours, boxes, weed_centres, image_out = result

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_filters_crop_detections(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """Weeds whose centre falls in crop mask area are removed."""
        (tmp_path / 'model.pt').touch()

        # YOLO detects crop at (100,50)-(200,150)
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        # GoB finds two weeds: one IN crop zone, one OUTSIDE
        mock_gob = MagicMock()
        mock_gob.inference.return_value = (
            None,
            [[150, 100, 20, 20], [400, 300, 20, 20]],  # boxes
            [[160, 110], [410, 310]],                     # centres (first in crop, second outside)
            np.zeros((480, 640, 3), dtype=np.uint8)
        )
        mock_gob_cls.return_value = mock_gob

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True, crop_buffer_px=0)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        _, boxes, centres, _ = gog.inference(image)

        # Only the weed outside the crop zone should remain
        assert len(boxes) == 1
        assert centres[0] == [410, 310]

    @patch('utils.greenongreen.YOLO')
    def test_dilate_kernel_precomputed(self, mock_yolo_cls, tmp_path):
        """Kernel shape = (2*px+1, 2*px+1)."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True, crop_buffer_px=15)

        assert gog._dilate_kernel is not None
        assert gog._dilate_kernel.shape == (31, 31)

    @patch('utils.greenongreen.YOLO')
    def test_zero_buffer_no_kernel(self, mock_yolo_cls, tmp_path):
        """buffer=0 -> kernel is None."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True, crop_buffer_px=0)

        assert gog._dilate_kernel is None

    @patch('utils.greenongreen.YOLO')
    def test_set_crop_buffer_rebuilds_kernel(self, mock_yolo_cls, tmp_path):
        """set_crop_buffer only rebuilds when value changes."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True, crop_buffer_px=10)

        old_kernel = gog._dilate_kernel
        gog.set_crop_buffer(10)  # Same value, no rebuild
        assert gog._dilate_kernel is old_kernel

        gog.set_crop_buffer(25)  # Different value, rebuild
        assert gog._dilate_kernel is not old_kernel
        assert gog._dilate_kernel.shape == (51, 51)

    @patch('utils.greenongreen.YOLO')
    def test_hybrid_executor_created(self, mock_yolo_cls, tmp_path):
        """hybrid_mode=True creates thread pool executor; False does not."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo()

        from utils.greenongreen import GreenOnGreen

        gog_hybrid = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True)
        assert gog_hybrid._executor is not None

        gog_normal = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=False)
        assert gog_normal._executor is None

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_exhsv_receives_full_image(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """ExHSV receives the original full image, not a masked copy."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        mock_gob = MagicMock()
        mock_gob.inference.return_value = (
            None, [], [], np.zeros((480, 640, 3), dtype=np.uint8)
        )
        mock_gob_cls.return_value = mock_gob

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True)

        image = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        gog.inference(image)

        # Verify GreenOnBrown.inference received the original image (not masked)
        call_args = mock_gob.inference.call_args
        received_image = call_args[0][0]
        np.testing.assert_array_equal(received_image, image)

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_show_display_overlay(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """show_display in hybrid mode returns annotated copy."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='detect')

        mock_gob = MagicMock()
        mock_gob.inference.return_value = (
            None, [], [], np.zeros((480, 640, 3), dtype=np.uint8)
        )
        mock_gob_cls.return_value = mock_gob

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        _, _, _, image_out = gog.inference(image, show_display=True)

        # Returns a copy, not original
        assert image_out is not image


class TestConfigIntegration:
    """Test that updated config files are valid."""

    def test_config_files_have_new_greenongreen_keys(self):
        """All DAY_SENSITIVITY configs have the new GreenOnGreen keys."""
        import configparser
        config_dir = PROJECT_ROOT / 'config'

        for ini_name in ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini']:
            config = configparser.ConfigParser()
            config.read(config_dir / ini_name)

            assert config.has_section('GreenOnGreen'), f'{ini_name} missing GreenOnGreen section'
            assert config.has_option('GreenOnGreen', 'model_path'), f'{ini_name} missing model_path'
            assert config.has_option('GreenOnGreen', 'confidence'), f'{ini_name} missing confidence'
            assert config.has_option('GreenOnGreen', 'detect_classes'), f'{ini_name} missing detect_classes'
            assert config.has_option('GreenOnGreen', 'actuation_mode'), f'{ini_name} missing actuation_mode'
            assert config.has_option('GreenOnGreen', 'min_detection_pixels'), f'{ini_name} missing min_detection_pixels'

            # Verify old key is removed
            assert not config.has_option('GreenOnGreen', 'class_filter_id'), \
                f'{ini_name} still has obsolete class_filter_id'

    def test_actuation_mode_values_valid(self):
        """actuation_mode values are valid in all configs."""
        import configparser
        config_dir = PROJECT_ROOT / 'config'

        for ini_name in ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini']:
            config = configparser.ConfigParser()
            config.read(config_dir / ini_name)

            mode = config.get('GreenOnGreen', 'actuation_mode')
            assert mode in ('centre', 'zone'), f'{ini_name} has invalid actuation_mode: {mode}'
