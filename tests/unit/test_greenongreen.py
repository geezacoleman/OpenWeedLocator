"""
Unit tests for GreenOnGreen (Ultralytics YOLO) weed detection.

The model is a stand-in (CI has no ultralytics): MagicMock in the older
classes at the top, the attribute-exact fakes of fake_yolo.py in the
crop-safety classes at the bottom. Everything else is production code.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

# Imports ultralytics/torch (10-20 s per process): CI only, not the pre-push hook
pytestmark = pytest.mark.slow

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def make_mock_boxes(box_mocks, track_ids=None):
    """Create a mock YOLO Boxes object that supports attribute access and iteration.

    The real YOLO ``result.boxes`` is a ``Boxes`` object, not a plain list.
    It has a ``.id`` attribute (numpy array or None) AND is iterable over
    individual box objects.  Using a plain Python list for ``result.boxes``
    prevents setting ``.id`` because lists don't allow arbitrary attributes.

    Args:
        box_mocks: list of individual box MagicMock objects (iterable items).
        track_ids: numpy array of track IDs, or None.

    Returns:
        A MagicMock that behaves like a YOLO Boxes object.
    """
    mock_boxes = MagicMock()
    mock_boxes.id = track_ids
    # Make iteration over mock_boxes yield the individual box mocks
    mock_boxes.__iter__ = MagicMock(return_value=iter(box_mocks))
    mock_boxes.__len__ = MagicMock(return_value=len(box_mocks))
    # Support indexed access (result.boxes[i]) used in show_display path
    mock_boxes.__getitem__ = MagicMock(side_effect=lambda i: box_mocks[i])
    return mock_boxes


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
    mock_box.conf = np.array([0.85])
    mock_box.cls = np.array([0])
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

    @patch('utils.greenongreen.YOLO')
    def test_no_models_raises(self, mock_yolo_cls, tmp_path):
        """Empty directory raises FileNotFoundError."""
        from utils.greenongreen import GreenOnGreen
        with pytest.raises(FileNotFoundError, match='No YOLO models found'):
            GreenOnGreen(model_path=str(tmp_path))

    @patch('utils.greenongreen.YOLO')
    def test_nonexistent_path_raises(self, mock_yolo_cls):
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
        # model box (100, 50)-(200, 150) as [x, y, w, h] and its centre
        assert boxes == [[100, 50, 100, 100]]
        assert weed_centres == [[150, 100]]

    @patch('utils.greenongreen.YOLO')
    def test_return_signature_segmentation(self, mock_yolo_cls, tmp_path):
        """Segmentation model returns (contours, boxes, centres, image)."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='segment')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        contours, boxes, weed_centres, image_out = gog.inference(image)

        assert len(contours) == 1
        assert contours[0].reshape(-1, 2).tolist() == [[100, 50], [200, 50], [200, 150], [100, 150]]

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

        # An annotated copy; the frame that gets recorded is not drawn on
        assert image_out is not image
        assert not image.any()
        assert tuple(image_out[50, 100]) == (0, 0, 255), 'untracked box outline is red'

    @patch('utils.greenongreen.YOLO')
    def test_build_mask_segmentation(self, mock_yolo_cls, tmp_path):
        """build_mask=True with segmentation model creates detection_mask."""
        (tmp_path / 'model.pt').touch()
        mock_yolo_cls.return_value = make_mock_yolo(task='segment')

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path))

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image, build_mask=True)

        assert gog.detection_mask.shape == (480, 640)
        assert gog.detection_mask.dtype == np.uint8
        assert gog.detection_mask[100, 150] == 255

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
        mock_box.conf = np.array([0.9])
        mock_box.cls = np.array([0])
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

        # Exactly the polygon is filled: this is what zone actuation counts per lane
        mask = gog.detection_mask
        expected = np.zeros((480, 640), dtype=np.uint8)
        expected[50:151, 100:201] = 255
        assert np.array_equal(mask, expected)


class TestHybridMode:
    """Test hybrid pipeline: YOLO crop mask + ExHSV weed detection."""

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

class TestTrackingMode:
    """Test tracking-related features in GreenOnGreen."""

    @patch('utils.greenongreen.YOLO')
    def test_tracking_disabled_uses_predict(self, mock_yolo_cls, tmp_path):
        """tracking_enabled=False calls model.predict (not track)."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), tracking_enabled=False)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image)

        mock_model.predict.assert_called_once()
        mock_model.track.assert_not_called()

    @patch('utils.greenongreen.YOLO')
    def test_tracking_enabled_uses_track(self, mock_yolo_cls, tmp_path):
        """tracking_enabled=True calls model.track (not predict)."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()

        # Setup track to return same as predict
        mock_result = MagicMock()
        mock_box = MagicMock()
        mock_box.xyxy = [np.array([100, 50, 200, 150])]
        mock_box.conf = np.array([0.85])
        mock_box.cls = np.array([0])
        mock_result.boxes = make_mock_boxes([mock_box], track_ids=np.array([1]))
        mock_result.masks = None
        mock_model.track.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), tracking_enabled=True)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image)

        mock_model.track.assert_called_once()
        mock_model.predict.assert_not_called()
        from utils.greenongreen import TRACKER_YAML
        kwargs = mock_model.track.call_args.kwargs
        assert kwargs['persist'] is True, 'without persist every frame starts a new tracker'
        assert kwargs['tracker'] == TRACKER_YAML and Path(TRACKER_YAML).is_file()

    @patch('utils.greenongreen.YOLO')
    def test_tracking_stores_raw_attrs(self, mock_yolo_cls, tmp_path):
        """After track(), last_track_ids/class_ids/confidences are populated."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()

        mock_result = MagicMock()
        mock_box1 = MagicMock()
        mock_box1.xyxy = [np.array([10, 20, 50, 60])]
        mock_box1.conf = np.array([0.9])
        mock_box1.cls = np.array([0])
        mock_box2 = MagicMock()
        mock_box2.xyxy = [np.array([100, 100, 200, 200])]
        mock_box2.conf = np.array([0.7])
        mock_box2.cls = np.array([1])

        mock_result.boxes = make_mock_boxes([mock_box1, mock_box2], track_ids=np.array([5, 8]))
        mock_result.masks = None
        mock_model.track.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), tracking_enabled=True)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image)

        assert gog.last_track_ids == [5, 8]
        assert gog.last_class_ids == [0, 1]
        assert gog.last_confidences == pytest.approx([0.9, 0.7])
        assert gog.last_raw_boxes == [[10, 20, 40, 40], [100, 100, 100, 100]]

    @patch('utils.greenongreen.YOLO')
    def test_tracking_no_ids_stores_empty(self, mock_yolo_cls, tmp_path):
        """When track returns no IDs (first frame), raw attrs are empty lists."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()

        mock_result = MagicMock()
        mock_box = MagicMock()
        mock_box.xyxy = [np.array([100, 50, 200, 150])]
        mock_box.conf = np.array([0.85])
        mock_box.cls = np.array([0])
        mock_result.boxes = make_mock_boxes([mock_box], track_ids=None)
        mock_result.masks = None
        mock_model.track.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), tracking_enabled=True)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        _, boxes, centres, _ = gog.inference(image)

        # Boxes and centres still populated (from xyxy), but track IDs empty
        assert len(boxes) == 1
        assert gog.last_track_ids == []
        assert gog.last_class_ids == []

    @patch('utils.greenongreen.YOLO')
    def test_tracking_no_classes_filter(self, mock_yolo_cls, tmp_path):
        """With tracking enabled, predict/track is called without classes= filter."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()

        mock_result = MagicMock()
        mock_result.boxes = make_mock_boxes([], track_ids=None)
        mock_result.masks = None
        mock_model.track.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), tracking_enabled=True,
                           detect_classes=['weed'])

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image)

        # track() should NOT get classes= kwarg (ClassSmoother handles filtering)
        call_kwargs = mock_model.track.call_args[1]
        assert 'classes' not in call_kwargs

    @patch('utils.greenongreen.YOLO')
    def test_reset_tracker_clears_state(self, mock_yolo_cls, tmp_path):
        """reset_tracker clears all tracking state."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()
        mock_yolo_cls.return_value = mock_model

        from utils.greenongreen import GreenOnGreen
        from utils.tracker import CropMaskStabilizer

        stabilizer = CropMaskStabilizer(max_age=3)
        stabilizer.update([1], [[10, 10, 20, 20]])

        gog = GreenOnGreen(model_path=str(tmp_path), tracking_enabled=True,
                           crop_stabilizer=stabilizer)
        gog.last_track_ids = [1, 2]
        gog.last_raw_boxes = [[0, 0, 10, 10], [20, 20, 30, 30]]
        gog.last_class_ids = [0, 1]
        gog.last_confidences = [0.9, 0.8]

        gog.reset_tracker()

        assert gog.last_track_ids == []
        assert gog.last_raw_boxes == []
        assert gog.last_class_ids == []
        assert gog.last_confidences == []
        assert stabilizer.active_count == 0

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_tracking_uses_track(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """Hybrid mode with tracking uses model.track instead of predict."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()

        mock_result = MagicMock()
        mock_result.boxes = make_mock_boxes([], track_ids=None)
        mock_result.masks = None
        mock_model.track.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        mock_gob = MagicMock()
        mock_gob.inference.return_value = (None, [], [], np.zeros((480, 640, 3), dtype=np.uint8))
        mock_gob_cls.return_value = mock_gob

        from utils.greenongreen import GreenOnGreen
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True,
                           tracking_enabled=True)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image)

        mock_model.track.assert_called_once()
        mock_model.predict.assert_not_called()

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_tracking_feeds_stabilizer(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """Hybrid tracking feeds crop detections to CropMaskStabilizer."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()

        # Create result with tracked crop detection
        mock_result = MagicMock()
        mock_box = MagicMock()
        mock_box.xyxy = [np.array([100, 50, 200, 150])]
        mock_box.conf = np.array([0.8])
        mock_box.cls = np.array([1])  # crop class
        mock_result.boxes = make_mock_boxes([mock_box], track_ids=np.array([3]))
        mock_result.masks = None
        mock_model.track.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        mock_gob = MagicMock()
        mock_gob.inference.return_value = (None, [], [], np.zeros((480, 640, 3), dtype=np.uint8))
        mock_gob_cls.return_value = mock_gob

        from utils.greenongreen import GreenOnGreen
        from utils.tracker import CropMaskStabilizer

        stabilizer = CropMaskStabilizer(max_age=3)
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True,
                           tracking_enabled=True, crop_stabilizer=stabilizer)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        gog.inference(image)

        # Stabilizer should have received the crop detection
        assert stabilizer.active_count == 1

    @patch('utils.greenongreen.YOLO')
    @patch('utils.greenonbrown.GreenOnBrown')
    def test_hybrid_no_track_ids_falls_back(self, mock_gob_cls, mock_yolo_cls, tmp_path):
        """When tracking enabled but no track IDs, falls back to per-frame mask."""
        (tmp_path / 'model.pt').touch()
        mock_model = make_mock_yolo()

        mock_result = MagicMock()
        mock_box = MagicMock()
        mock_box.xyxy = [np.array([100, 50, 200, 150])]
        mock_box.conf = np.array([0.8])
        mock_box.cls = np.array([1])
        mock_result.boxes = make_mock_boxes([mock_box], track_ids=None)
        mock_result.masks = None
        mock_model.track.return_value = [mock_result]
        mock_yolo_cls.return_value = mock_model

        mock_gob = MagicMock()
        mock_gob.inference.return_value = (
            None, [[300, 300, 20, 20]], [[310, 310]],
            np.zeros((480, 640, 3), dtype=np.uint8)
        )
        mock_gob_cls.return_value = mock_gob

        from utils.greenongreen import GreenOnGreen
        from utils.tracker import CropMaskStabilizer

        stabilizer = CropMaskStabilizer(max_age=3)
        gog = GreenOnGreen(model_path=str(tmp_path), hybrid_mode=True,
                           tracking_enabled=True, crop_stabilizer=stabilizer)

        image = np.zeros((480, 640, 3), dtype=np.uint8)
        _, boxes, centres, _ = gog.inference(image)

        # Should not crash; weed detection still works
        assert len(boxes) == 1  # weed at (300,300) outside crop at (100,50)


# ===========================================================================
# Crop safety. From here down the model is fake_yolo's attribute-exact
# stand-in and every assertion is on what GreenOnGreen hands to owl.py for
# actuation: the boxes and centres.
# ===========================================================================

import fake_yolo                                              # noqa: E402
from fake_yolo import CROP, WEED, LostTrack, Results, Tracker, box   # noqa: E402

SOIL = (40, 60, 90)
GREEN = (60, 180, 60)


def build(monkeypatch, tmp_path, model, **kwargs):
    """A production GreenOnGreen that loaded `model`."""
    from utils.greenongreen import GreenOnGreen
    fake_yolo.install(monkeypatch, model)
    return GreenOnGreen(model_path=fake_yolo.model_file(tmp_path), **kwargs)


def paddock(*weeds, width=416, height=320):
    """Bare soil with a 20x20 green weed centred on each (x, y)."""
    image = np.full((height, width, 3), SOIL, dtype=np.uint8)
    for cx, cy in weeds:
        cv2.rectangle(image, (cx - 10, cy - 10), (cx + 9, cy + 9), GREEN, -1)
    return image


class TestNewObjectFrameDoesNotSprayCrop:
    """With tracking on, the frame a new object first appears on comes back
    from ultralytics with NO track ids and every raw detection, crop included
    (tracking runs without a class filter). Those boxes go straight to
    actuation, so GreenOnGreen has to drop the non-target classes itself.
    Field bug: crop sprayed on the first frame of every new plant."""

    FRAME = np.zeros((240, 320, 3), dtype=np.uint8)
    ROWS = [box(10, 10, 50, 50, WEED),
            box(100, 100, 200, 200, CROP, conf=0.95),
            box(60, 10, 90, 40, WEED, conf=0.8)]

    def test_crop_is_dropped_and_the_weeds_are_kept(self, monkeypatch, tmp_path):
        model = fake_yolo.Model([Results(self.ROWS, ids=None)])
        gog = build(monkeypatch, tmp_path, model, tracking_enabled=True,
                    detect_classes=['weed'])

        _, boxes, centres, _ = gog.inference(self.FRAME)

        assert boxes == [[10, 10, 40, 40], [60, 10, 30, 30]]
        assert centres == [[30, 30], [75, 25]]
        assert gog.last_track_ids == [], 'nothing was tracked, so nothing for the smoother'

    def test_a_frame_of_only_crop_gives_no_detections(self, monkeypatch, tmp_path):
        model = fake_yolo.Model([Results([box(100, 100, 200, 200, CROP)], ids=None)])
        gog = build(monkeypatch, tmp_path, model, tracking_enabled=True,
                    detect_classes=['weed'])
        _, boxes, centres, _ = gog.inference(self.FRAME)
        assert (boxes, centres) == ([], [])

    def test_the_target_can_be_the_other_class(self, monkeypatch, tmp_path):
        model = fake_yolo.Model([Results(self.ROWS, ids=None)])
        gog = build(monkeypatch, tmp_path, model, tracking_enabled=True,
                    detect_classes=['Crop'])
        assert gog.inference(self.FRAME)[1] == [[100, 100, 100, 100]]

    def test_changing_the_target_classes_live_changes_what_is_dropped(self, monkeypatch, tmp_path):
        model = fake_yolo.Model([Results(self.ROWS, ids=None)])
        gog = build(monkeypatch, tmp_path, model, tracking_enabled=True,
                    detect_classes=['weed'])
        gog.update_detect_classes(['crop'])
        assert gog.inference(self.FRAME)[1] == [[100, 100, 100, 100]]

    def test_with_no_target_classes_everything_is_a_target(self, monkeypatch, tmp_path):
        model = fake_yolo.Model([Results(self.ROWS, ids=None)])
        gog = build(monkeypatch, tmp_path, model, tracking_enabled=True)
        assert len(gog.inference(self.FRAME)[1]) == 3

    def test_tracked_frames_pass_every_class_on_with_ids_for_the_smoother(
            self, monkeypatch, tmp_path):
        """Once ids exist owl.py filters by the smoothed class per track, so
        it needs all of them, in step with the raw boxes."""
        model = fake_yolo.Model([Results(self.ROWS, ids=[4, 5, 6])])
        gog = build(monkeypatch, tmp_path, model, tracking_enabled=True,
                    detect_classes=['weed'])

        _, boxes, _, _ = gog.inference(self.FRAME)

        assert boxes == [[10, 10, 40, 40], [100, 100, 100, 100], [60, 10, 30, 30]]
        assert gog.last_track_ids == [4, 5, 6]
        assert gog.last_class_ids == [WEED, CROP, WEED]
        assert gog.last_raw_boxes == boxes
        assert gog.last_confidences == pytest.approx([0.9, 0.95, 0.8])

    def test_without_tracking_the_model_itself_is_asked_for_the_target_classes(
            self, monkeypatch, tmp_path):
        model = fake_yolo.Model([Results([box(10, 10, 50, 50, WEED)])])
        gog = build(monkeypatch, tmp_path, model, detect_classes=['weed'])
        gog.inference(self.FRAME, confidence=0.35)
        (kind, kwargs), = model.calls
        assert kind == 'predict'
        assert kwargs['classes'] == [WEED]
        assert kwargs['conf'] == 0.35


class TestHybridCropBuffer:
    """Hybrid: the model finds the crop, colour detection finds everything
    green, and whatever is centred on crop (plus the buffer) is not sprayed.
    Colour detection here is the real GreenOnBrown on a real frame."""

    CROP_BOX = box(100, 100, 200, 200, CROP)
    INSIDE = (150, 150)        # centred on the crop
    NEAR = (215, 150)          # 15 px right of the crop box edge
    FAR = (335, 255)

    def hybrid(self, monkeypatch, tmp_path, frames=None, **kwargs):
        model = fake_yolo.Model(frames or [Results([self.CROP_BOX])])
        kwargs.setdefault('crop_buffer_px', 20)
        return build(monkeypatch, tmp_path, model, hybrid_mode=True, **kwargs), model

    def centres(self, gog, *weeds):
        _, boxes, centres, _ = gog.inference(paddock(*weeds))
        assert len(boxes) == len(centres)
        return sorted(centres)

    def test_all_three_weeds_are_seen_when_there_is_no_crop(self, monkeypatch, tmp_path):
        gog, _ = self.hybrid(monkeypatch, tmp_path, frames=[Results()])
        assert self.centres(gog, self.INSIDE, self.NEAR, self.FAR) == \
            sorted([list(self.INSIDE), list(self.NEAR), list(self.FAR)])

    def test_a_weed_on_the_crop_is_not_sprayed(self, monkeypatch, tmp_path):
        gog, _ = self.hybrid(monkeypatch, tmp_path, crop_buffer_px=0)
        assert self.centres(gog, self.INSIDE, self.FAR) == [list(self.FAR)]

    @pytest.mark.parametrize('buffer_px, near_is_sprayed', [
        (0, True), (14, True), (15, False), (20, False)])
    def test_the_buffer_reaches_exactly_as_far_as_it_says(
            self, monkeypatch, tmp_path, buffer_px, near_is_sprayed):
        gog, _ = self.hybrid(monkeypatch, tmp_path, crop_buffer_px=buffer_px)
        expected = [list(self.NEAR), list(self.FAR)] if near_is_sprayed else [list(self.FAR)]
        assert self.centres(gog, self.INSIDE, self.NEAR, self.FAR) == expected

    def test_changing_the_buffer_live_takes_effect(self, monkeypatch, tmp_path):
        gog, _ = self.hybrid(monkeypatch, tmp_path, crop_buffer_px=0)
        assert self.centres(gog, self.NEAR) == [list(self.NEAR)]
        gog.set_crop_buffer(20)
        assert self.centres(gog, self.NEAR) == []
        gog.set_crop_buffer(0)
        assert self.centres(gog, self.NEAR) == [list(self.NEAR)]

    def test_a_segmentation_model_masks_the_outline_not_the_box(self, monkeypatch, tmp_path):
        """Crop outline is the upper-left triangle of its box. A weed in the
        other half of the box, 42 px from the outline, is still a weed."""
        triangle = [(100, 100), (200, 100), (100, 200)]
        gog, _ = self.hybrid(monkeypatch, tmp_path,
                             frames=[Results([self.CROP_BOX], polygons=[triangle])])
        in_outline, in_box_only = (120, 120), (180, 180)
        assert self.centres(gog, in_outline, in_box_only) == [list(in_box_only)]

    def test_the_model_is_asked_for_crop_at_the_inference_resolution(self, monkeypatch, tmp_path):
        gog, model = self.hybrid(monkeypatch, tmp_path, detect_classes=['crop'],
                                 inference_resolution=256)
        gog.inference(paddock(self.FAR), confidence=0.4)
        (kind, kwargs), = model.calls
        assert kind == 'predict'
        assert (kwargs['classes'], kwargs['imgsz'], kwargs['conf']) == ([CROP], 256, 0.4)

    def test_colour_thresholds_reach_the_colour_detector(self, monkeypatch, tmp_path):
        gog, _ = self.hybrid(monkeypatch, tmp_path, frames=[Results()])
        frame = paddock(self.FAR)
        assert gog.inference(frame)[2] == [list(self.FAR)]
        assert gog.inference(frame, hue_min=100, hue_max=120)[2] == []
        assert gog.inference(frame, min_detection_area=5000)[2] == []

    def test_the_display_copy_is_annotated_and_the_frame_is_not(self, monkeypatch, tmp_path):
        gog, _ = self.hybrid(monkeypatch, tmp_path)
        frame = paddock(self.FAR)
        before = frame.copy()
        _, _, _, shown = gog.inference(frame, show_display=True)
        assert np.array_equal(frame, before)
        assert tuple(shown[150, 150]) != SOIL, 'crop area is tinted'
        assert tuple(shown[150, 210]) != SOIL, 'so is the buffer around it'
        assert tuple(shown[20, 20]) == SOIL


class TestHybridCropThatTheModelLost:
    """Hybrid with tracking: a crop the model misses for a few frames must
    stay masked, or the colour detector sprays it."""

    INSIDE, FAR = (150, 150), (335, 255)
    CROP_XYXY = (100, 100, 200, 200)

    def hybrid(self, monkeypatch, tmp_path, frames, tracker=None, persist=5, max_age=3):
        from utils.tracker import CropMaskStabilizer
        model = fake_yolo.Model(frames, tracker=tracker)
        gog = build(monkeypatch, tmp_path, model, hybrid_mode=True, crop_buffer_px=0,
                    tracking_enabled=True, crop_stabilizer=CropMaskStabilizer(max_age=max_age),
                    detection_persist_frames=persist)
        return gog, model

    def centres(self, gog):
        return sorted(gog.inference(paddock(self.INSIDE, self.FAR))[2])

    @pytest.mark.parametrize('persist, age, crop_still_masked', [
        (5, 5, True),      # within detection_persist_frames
        (5, 6, False),     # one frame too old
        (0, 3, True),      # persistence off: the stabilizer's max_age (3) applies
        (0, 4, False),
    ])
    def test_the_kalman_prediction_of_a_lost_crop_is_masked_for_the_persist_frames(
            self, monkeypatch, tmp_path, persist, age, crop_still_masked):
        lost = LostTrack(7, self.CROP_XYXY, CROP, end_frame=20 - age)
        gog, model = self.hybrid(monkeypatch, tmp_path, [Results()],
                                 tracker=Tracker(frame_id=20, lost=[lost]), persist=persist)

        got = self.centres(gog)

        assert model.calls[0][0] == 'track'
        expected = [list(self.FAR)] if crop_still_masked else [list(self.INSIDE), list(self.FAR)]
        assert got == expected

    def test_a_prediction_that_drifted_past_the_frame_edge_still_masks(self, monkeypatch, tmp_path):
        lost = LostTrack(7, (-60, 100, 200, 900), CROP, end_frame=19)
        gog, _ = self.hybrid(monkeypatch, tmp_path, [Results()],
                             tracker=Tracker(frame_id=20, lost=[lost]))
        assert self.centres(gog) == [list(self.FAR)]

    def test_a_tracked_crop_stays_masked_for_max_age_frames_after_it_vanishes(
            self, monkeypatch, tmp_path):
        """Frame 1: crop 3 at the weed. Frames 2+: the model only reports
        another crop (id 4) in the corner. Crop 3 is held for max_age=2
        frames, then let go."""
        here = Results([box(*self.CROP_XYXY, cls=CROP)], ids=[3])
        elsewhere = Results([box(0, 0, 30, 30, CROP)], ids=[4])
        gog, _ = self.hybrid(monkeypatch, tmp_path, [here, elsewhere], max_age=2)

        both = [list(self.INSIDE), list(self.FAR)]
        assert self.centres(gog) == [list(self.FAR)]          # seen
        assert self.centres(gog) == [list(self.FAR)]          # held, age 1
        assert self.centres(gog) == [list(self.FAR)]          # held, age 2
        assert self.centres(gog) == both                      # released

    def test_untracked_crop_on_a_new_object_frame_is_still_masked(self, monkeypatch, tmp_path):
        """No ids yet (first frame of a new crop): fall back to this frame's
        boxes rather than an empty mask."""
        gog, _ = self.hybrid(monkeypatch, tmp_path,
                             [Results([box(*self.CROP_XYXY, cls=CROP)], ids=None)])
        assert self.centres(gog) == [list(self.FAR)]

    def test_reset_forgets_held_crops_and_resets_bytetrack(self, monkeypatch, tmp_path):
        here = Results([box(*self.CROP_XYXY, cls=CROP)], ids=[3])
        tracker = Tracker()
        gog, _ = self.hybrid(monkeypatch, tmp_path, [here, Results()], tracker=tracker)
        self.centres(gog)
        gog.reset_tracker()
        assert tracker.resets == 1
        assert gog._crop_stabilizer.active_count == 0
        assert gog.last_track_ids == []


class TestTrackerSettingsReachByteTrack:
    """BYTETracker reads its thresholds from tracker.args; the tracker only
    exists after the first track() call. (The same against a real
    BYTETracker: test_bytetrack_real.py, where ultralytics is installed.)"""

    FRAME = np.zeros((240, 320, 3), dtype=np.uint8)

    def test_settings_from_the_config_are_applied_after_the_first_frame(
            self, monkeypatch, tmp_path):
        tracker = Tracker()
        gog = build(monkeypatch, tmp_path, fake_yolo.Model(tracker=tracker),
                    tracking_enabled=True,
                    tracker_params={'match_thresh': 0.93, 'track_buffer': 45})
        assert tracker.args.match_thresh == 0.8, 'nothing to apply to before the first frame'

        gog.inference(self.FRAME)

        assert tracker.args.match_thresh == 0.93
        assert tracker.args.track_buffer == 45
        assert tracker.max_time_lost == 45, 'track_buffer only acts through max_time_lost'
        assert tracker.resets == 0

    def test_a_live_change_before_the_first_frame_is_queued_not_lost(self, monkeypatch, tmp_path):
        tracker = Tracker()
        gog = build(monkeypatch, tmp_path, fake_yolo.Model(tracker=tracker),
                    tracking_enabled=True, tracker_params={'match_thresh': 0.93})
        gog.update_tracker_params_direct({'track_high_thresh': 0.33})
        gog.inference(self.FRAME)
        assert (tracker.args.match_thresh, tracker.args.track_high_thresh) == (0.93, 0.33)

    def test_a_live_change_applies_at_once_and_keeps_the_tracks(self, monkeypatch, tmp_path):
        tracker = Tracker()
        gog = build(monkeypatch, tmp_path, fake_yolo.Model(tracker=tracker), tracking_enabled=True)
        gog.inference(self.FRAME)

        gog.update_tracker_params_direct({'match_thresh': 0.91, 'track_buffer': 90})

        assert (tracker.args.match_thresh, tracker.args.track_buffer) == (0.91, 90)
        assert tracker.max_time_lost == 90
        assert tracker.resets == 0, 'a reset would drop every track and restart the ids'

    @pytest.mark.parametrize('level', ['low', 'medium', 'high'])
    def test_stability_presets(self, monkeypatch, tmp_path, level):
        from utils.greenongreen import GreenOnGreen
        tracker = Tracker(track_buffer=1, match_thresh=0.0)
        gog = build(monkeypatch, tmp_path, fake_yolo.Model(tracker=tracker), tracking_enabled=True)
        gog.inference(self.FRAME)

        gog.update_tracker_params(level)

        preset = GreenOnGreen.TRACK_STABILITY_PRESETS[level]
        assert vars(tracker.args) == preset
        assert tracker.max_time_lost == preset['track_buffer']

    def test_an_unknown_preset_changes_nothing(self, monkeypatch, tmp_path):
        tracker = Tracker()
        gog = build(monkeypatch, tmp_path, fake_yolo.Model(tracker=tracker), tracking_enabled=True)
        gog.inference(self.FRAME)
        before = dict(vars(tracker.args))
        gog.update_tracker_params('extreme')
        assert vars(tracker.args) == before
        assert gog._pending_tracker_params is None

    def test_presets_are_ordered_and_inside_the_validator_ranges(self):
        """More stability = longer buffer and more lenient matching; every
        value must be one the config validator accepts."""
        from utils.config_manager import ConfigValidator
        from utils.greenongreen import GreenOnGreen
        presets = GreenOnGreen.TRACK_STABILITY_PRESETS
        low, medium, high = presets['low'], presets['medium'], presets['high']
        assert low['track_buffer'] < medium['track_buffer'] < high['track_buffer']
        assert low['match_thresh'] < medium['match_thresh'] < high['match_thresh']
        for preset in presets.values():
            for key, value in preset.items():
                _, lowest, highest = ConfigValidator.VALUE_VALIDATORS[key]
                assert lowest <= value <= highest, key


class TestShippedTrackerSettingsAgree:
    """Contract checks on shipped files (they read files, they do not run
    the tracker). The yaml bootstraps ByteTrack for the first frame, the INI
    takes over after it, and the dashboard's preset buttons send their own
    copy of the numbers: three copies that must say the same thing."""

    KEYS = ('track_high_thresh', 'track_low_thresh', 'new_track_thresh',
            'track_buffer', 'match_thresh')

    @staticmethod
    def tracker_yaml():
        from utils.greenongreen import TRACKER_YAML
        values = {}
        for line in Path(TRACKER_YAML).read_text(encoding='utf-8').splitlines():
            line = line.split('#', 1)[0].strip()
            if ':' in line:
                key, value = line.split(':', 1)
                values[key.strip()] = value.strip()
        return values

    def test_tracker_yaml_matches_the_shipped_ini(self):
        import configparser
        ini = configparser.ConfigParser()
        ini.read(PROJECT_ROOT / 'config' / 'GENERAL_CONFIG.ini')
        yaml_values = self.tracker_yaml()
        assert yaml_values['tracker_type'] == 'bytetrack'
        assert 'fuse_score' in yaml_values, 'BYTETracker reads it; ultralytics raises without'
        for key in self.KEYS:
            assert float(yaml_values[key]) == ini.getfloat('Tracking', key), key

    def test_dashboard_preset_buttons_send_the_python_presets(self):
        import re
        from utils.greenongreen import GreenOnGreen
        text = (PROJECT_ROOT / 'controller/shared/js/owl_ui.js').read_text(encoding='utf-8')
        block = re.search(r'TRACK_PRESETS = \{(.*?)\};', text, re.DOTALL).group(1)
        for level, preset in GreenOnGreen.TRACK_STABILITY_PRESETS.items():
            fields = re.search(level + r':\s*\{([^}]*)\}', block).group(1)
            in_js = {k: float(v) for k, v in re.findall(r'(\w+):\s*([\d.]+)', fields)}
            assert in_js == {k: float(v) for k, v in preset.items()}, level
