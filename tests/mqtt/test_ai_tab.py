"""
Tests for AI tab: model selection, class filtering, MQTT command handlers.
"""

from unittest.mock import MagicMock



# ---------------------------------------------------------------------------
# set_detect_classes / set_model before an OWL is attached
# (with an OWL: test_mqtt_command_handlers.py TestSetDetectClasses, TestSetModelCommand)
# ---------------------------------------------------------------------------

class TestNoOwlAttached:
    def test_detect_classes_are_kept_in_state(self, mqtt_publisher):
        mqtt_publisher.owl_instance = None
        mqtt_publisher._handle_command({'action': 'set_detect_classes', 'value': ['wheat']})
        assert mqtt_publisher.state['detect_classes'] == ['wheat']

    def test_a_model_switch_is_not_reported_as_done(self, mqtt_publisher):
        """Nothing can load it yet: the dashboard must not show it as current."""
        mqtt_publisher.owl_instance = None
        before = mqtt_publisher.state['current_model']
        mqtt_publisher._handle_command({'action': 'set_model', 'value': 'model.pt'})
        assert mqtt_publisher.state['current_model'] == before


# ---------------------------------------------------------------------------
# _list_available_models helper
# ---------------------------------------------------------------------------

class TestListAvailableModels:
    """Tests for the _list_available_models helper."""

    def test_finds_pt_files(self, mqtt_publisher, owl_home):
        """Should find .pt files in the models directory."""
        (owl_home / 'models' / 'yolo8n.pt').touch()
        (owl_home / 'models' / 'yolo11s.pt').touch()
        assert sorted(mqtt_publisher._list_available_models()) == ['yolo11s.pt', 'yolo8n.pt']

    def test_finds_ncnn_dirs(self, mqtt_publisher, owl_home):
        """Should find NCNN model subdirectories (contain .param files)."""
        ncnn_dir = owl_home / 'models' / 'yolo8n_ncnn'
        ncnn_dir.mkdir()
        (ncnn_dir / 'model.param').touch()
        (ncnn_dir / 'model.bin').touch()
        assert mqtt_publisher._list_available_models() == ['yolo8n_ncnn']

    def test_empty_models_dir(self, mqtt_publisher, owl_home):
        """Should return empty list when no models found."""
        assert mqtt_publisher._list_available_models() == []

    def test_no_models_dir(self, mqtt_publisher, owl_home):
        """Should return empty list when models/ doesn't exist."""
        (owl_home / 'models').rmdir()
        assert mqtt_publisher._list_available_models() == []


# ---------------------------------------------------------------------------
# State sync includes new AI fields
# ---------------------------------------------------------------------------

class TestStateSyncAIFields:
    """Verify _sync_parameters_to_state populates AI tab fields."""

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
