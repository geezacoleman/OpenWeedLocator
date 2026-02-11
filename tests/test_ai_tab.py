"""
Tests for AI tab: model selection, class filtering, MQTT command handlers.
"""

import os
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# set_detect_classes command
# ---------------------------------------------------------------------------

class TestSetDetectClasses:
    """Tests for the set_detect_classes MQTT command handler."""

    def test_empty_list_clears_classes(self, mqtt_publisher, mock_owl):
        """Sending an empty list should queue an empty list on the owl."""
        command = {'action': 'set_detect_classes', 'value': []}
        mqtt_publisher._handle_command(command)

        assert mock_owl._pending_detect_classes == []
        assert mqtt_publisher.state['detect_classes'] == []

    def test_name_list_sets_classes(self, mqtt_publisher, mock_owl):
        """Sending a list of class names should queue them on the owl."""
        command = {'action': 'set_detect_classes', 'value': ['wheat', 'ryegrass']}
        mqtt_publisher._handle_command(command)

        assert mock_owl._pending_detect_classes == ['wheat', 'ryegrass']
        assert mqtt_publisher.state['detect_classes'] == ['wheat', 'ryegrass']

    def test_comma_string_parsed(self, mqtt_publisher, mock_owl):
        """Sending a comma-separated string should be split into a list."""
        command = {'action': 'set_detect_classes', 'value': 'wheat, ryegrass, barley'}
        mqtt_publisher._handle_command(command)

        assert mock_owl._pending_detect_classes == ['wheat', 'ryegrass', 'barley']

    def test_no_owl_instance(self, mqtt_publisher):
        """Should not crash when owl_instance is None."""
        mqtt_publisher.owl_instance = None
        command = {'action': 'set_detect_classes', 'value': ['wheat']}
        mqtt_publisher._handle_command(command)
        assert mqtt_publisher.state['detect_classes'] == ['wheat']


# ---------------------------------------------------------------------------
# set_model command
# ---------------------------------------------------------------------------

class TestSetModel:
    """Tests for the set_model MQTT command handler."""

    def test_sets_pending_model(self, mqtt_publisher, mock_owl):
        """Should set _pending_model on the owl instance with models/ path."""
        command = {'action': 'set_model', 'value': 'yolo26n-seg.pt'}
        mqtt_publisher._handle_command(command)

        assert mock_owl._pending_model == os.path.join('models', 'yolo26n-seg.pt')

    def test_empty_model_ignored(self, mqtt_publisher, mock_owl):
        """Empty model name should not set pending."""
        command = {'action': 'set_model', 'value': ''}
        mqtt_publisher._handle_command(command)

        assert mock_owl._pending_model is None

    def test_no_owl_instance(self, mqtt_publisher):
        """Should not crash when owl_instance is None."""
        mqtt_publisher.owl_instance = None
        command = {'action': 'set_model', 'value': 'model.pt'}
        mqtt_publisher._handle_command(command)  # Should not raise


# ---------------------------------------------------------------------------
# _list_available_models helper
# ---------------------------------------------------------------------------

class TestListAvailableModels:
    """Tests for the _list_available_models helper."""

    def test_finds_pt_files(self, mqtt_publisher, tmp_path):
        """Should find .pt files in the models directory."""
        models_dir = tmp_path / 'models'
        models_dir.mkdir()
        (models_dir / 'yolo8n.pt').touch()
        (models_dir / 'yolo11s.pt').touch()

        with patch('utils.mqtt_manager.os.path.dirname') as mock_dir:
            # Make the models dir resolve to our tmp path
            mock_dir.return_value = str(tmp_path)
            result = mqtt_publisher._list_available_models()

        assert 'yolo8n.pt' in result
        assert 'yolo11s.pt' in result

    def test_finds_ncnn_dirs(self, mqtt_publisher, tmp_path):
        """Should find NCNN model subdirectories (contain .param files)."""
        models_dir = tmp_path / 'models'
        models_dir.mkdir()
        ncnn_dir = models_dir / 'yolo8n_ncnn'
        ncnn_dir.mkdir()
        (ncnn_dir / 'model.param').touch()
        (ncnn_dir / 'model.bin').touch()

        with patch('utils.mqtt_manager.os.path.dirname') as mock_dir:
            mock_dir.return_value = str(tmp_path)
            result = mqtt_publisher._list_available_models()

        assert 'yolo8n_ncnn' in result

    def test_empty_models_dir(self, mqtt_publisher, tmp_path):
        """Should return empty list when no models found."""
        models_dir = tmp_path / 'models'
        models_dir.mkdir()

        with patch('utils.mqtt_manager.os.path.dirname') as mock_dir:
            mock_dir.return_value = str(tmp_path)
            result = mqtt_publisher._list_available_models()

        assert result == []

    def test_no_models_dir(self, mqtt_publisher, tmp_path):
        """Should return empty list when models/ doesn't exist."""
        with patch('utils.mqtt_manager.os.path.dirname') as mock_dir:
            mock_dir.return_value = str(tmp_path)
            result = mqtt_publisher._list_available_models()

        assert result == []


# ---------------------------------------------------------------------------
# State sync includes new AI fields
# ---------------------------------------------------------------------------

class TestStateSyncAIFields:
    """Verify _sync_parameters_to_state populates AI tab fields."""

    def test_state_has_ai_fields(self, mqtt_publisher):
        """After set_owl_instance, state should contain AI tab fields."""
        assert 'current_model' in mqtt_publisher.state
        assert 'available_models' in mqtt_publisher.state
        assert 'model_classes' in mqtt_publisher.state
        assert 'detect_classes' in mqtt_publisher.state

    def test_no_gog_detector_yields_empty(self, mqtt_publisher, mock_owl):
        """Without a GoG detector, model info should be empty."""
        mock_owl._gog_detector = None
        mqtt_publisher._sync_parameters_to_state()

        assert mqtt_publisher.state['current_model'] == ''
        assert mqtt_publisher.state['model_classes'] == {}

    def test_with_gog_detector(self, mqtt_publisher, mock_owl):
        """With a GoG detector, model info should be populated."""
        mock_gog = MagicMock()
        mock_gog._model_filename = 'test_model.pt'
        mock_gog.model.names = {0: 'wheat', 1: 'ryegrass'}
        mock_owl._gog_detector = mock_gog

        mqtt_publisher._sync_parameters_to_state()

        assert mqtt_publisher.state['current_model'] == 'test_model.pt'
        assert mqtt_publisher.state['model_classes'] == {'0': 'wheat', '1': 'ryegrass'}


class TestRefreshAIState:
    """Verify _refresh_ai_state updates state from live detector (heartbeat path)."""

    def test_refresh_picks_up_detector_created_after_init(self, mqtt_publisher, mock_owl):
        """Detector created in hoot() after set_owl_instance should be found by refresh."""
        # After init, no detector — model_classes empty
        assert mqtt_publisher.state['model_classes'] == {}

        # Simulate hoot() creating a detector
        mock_gog = MagicMock()
        mock_gog._model_filename = 'yolo26n-seg.pt'
        mock_gog.model.names = {0: 'crop', 1: 'weed', 2: 'grass'}
        mock_owl._gog_detector = mock_gog
        mock_owl._detect_classes_list = ['weed']

        # Heartbeat calls _refresh_ai_state
        mqtt_publisher._refresh_ai_state()

        assert mqtt_publisher.state['current_model'] == 'yolo26n-seg.pt'
        assert mqtt_publisher.state['model_classes'] == {'0': 'crop', '1': 'weed', '2': 'grass'}
        assert mqtt_publisher.state['detect_classes'] == ['weed']

    def test_refresh_clears_when_detector_removed(self, mqtt_publisher, mock_owl):
        """If detector is removed (algorithm switch to colour), state should clear."""
        # Set up a detector first
        mock_gog = MagicMock()
        mock_gog._model_filename = 'model.pt'
        mock_gog.model.names = {0: 'weed'}
        mock_owl._gog_detector = mock_gog
        mqtt_publisher._refresh_ai_state()
        assert mqtt_publisher.state['current_model'] == 'model.pt'

        # Simulate switch to colour mode — detector cleared
        mock_owl._gog_detector = None
        mqtt_publisher._refresh_ai_state()
        assert mqtt_publisher.state['current_model'] == ''
        assert mqtt_publisher.state['model_classes'] == {}


# ---------------------------------------------------------------------------
# GreenOnGreen.update_detect_classes
# ---------------------------------------------------------------------------

class TestGreenOnGreenUpdateClasses:
    """Test the update_detect_classes hot-update method."""

    def test_update_resolves_classes(self):
        """update_detect_classes should re-resolve class names to IDs."""
        from utils.greenongreen import GreenOnGreen

        # Create a minimal mock that has a model with names
        gog = GreenOnGreen.__new__(GreenOnGreen)
        mock_model = MagicMock()
        mock_model.names = {0: 'wheat', 1: 'ryegrass', 2: 'barley'}
        gog.model = mock_model

        gog.update_detect_classes(['ryegrass', 'barley'])
        assert gog._detect_class_ids == [1, 2]

    def test_update_with_none_clears(self):
        """Passing None should clear the filter (detect all)."""
        from utils.greenongreen import GreenOnGreen

        gog = GreenOnGreen.__new__(GreenOnGreen)
        mock_model = MagicMock()
        mock_model.names = {0: 'wheat', 1: 'ryegrass'}
        gog.model = mock_model

        gog.update_detect_classes(None)
        assert gog._detect_class_ids is None

    def test_update_with_empty_list_clears(self):
        """Passing empty list should clear the filter (detect all)."""
        from utils.greenongreen import GreenOnGreen

        gog = GreenOnGreen.__new__(GreenOnGreen)
        mock_model = MagicMock()
        mock_model.names = {0: 'wheat', 1: 'ryegrass'}
        gog.model = mock_model

        gog.update_detect_classes([])
        assert gog._detect_class_ids is None
