"""Field round 2026-10-15 on a real Owl.

- Each OWL on a rig booted on a different AI model: set_model only swapped
  live, the config said model_path = models (first entry of an unsorted
  folder listing), and the controller's re-push on reconnect was wiped while
  Owl.__init__ was still running. Now the model is saved once it loads and
  pushes made during startup survive.
- Pause: the driver pauses at the headland; the session stays open and
  nothing is saved until Resume.
- Session folders carry the OWL's id and serial (s164025_owl-1_a3f9).
"""

import re
from unittest.mock import MagicMock

import pytest

import owl as owl_module
import utils.mqtt_manager as mqtt_module
from harness import run_hoot

DASH = [('MQTT', 'enable', 'True'), ('MQTT', 'broker_ip', '127.0.0.1'),
        ('MQTT', 'broker_port', '1'), ('MQTT', 'device_id', 'test-owl')]


def build(owl_factory, config_factory, input_dir, extra=()):
    config = config_factory(overrides=[
        ('System', 'algorithm', 'exhsv'),
        ('DataCollection', 'sample_frequency', '1'),
        *DASH, *extra])
    instance = owl_factory(config, input_file_or_directory=str(input_dir))
    # _handle_save_config writes into the repo's config/ folder: never from a test
    instance.dash._handle_save_config = MagicMock()
    return instance


def record(instance, paused):
    with instance.dash.state_lock:
        instance.dash.state['image_sample_enable'] = True
        instance.dash.state['recording_paused'] = paused
    instance._update_state_once()


def test_model_switch_is_saved_once_it_loads(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    instance._pending_model = 'models/weeds-v3'

    run_hoot(instance, frames=2)

    assert instance._model_path == 'models/weeds-v3'
    assert instance.config.get('GreenOnGreen', 'model_path') == 'models/weeds-v3'
    instance.dash._handle_save_config.assert_called()


@pytest.mark.allow_errors
def test_failed_model_switch_keeps_the_old_model(owl_factory, config_factory, input_dir,
                                                 monkeypatch):
    instance = build(owl_factory, config_factory, input_dir)
    before = instance.config.get('GreenOnGreen', 'model_path')
    real = owl_module.GreenOnBrown
    calls = []

    def detector(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:  # the rebuild for the new model
            raise RuntimeError('model will not load')
        return real(*args, **kwargs)

    monkeypatch.setattr(owl_module, 'GreenOnBrown', detector)
    instance._pending_model = 'models/broken'

    run_hoot(instance, frames=2)

    assert instance._model_path == before
    assert instance.config.get('GreenOnGreen', 'model_path') == before
    instance.dash._handle_save_config.assert_not_called()
    assert 'will not load' in instance.dash.state['algorithm_error']


def test_controller_push_during_startup_survives(owl_factory, config_factory, input_dir,
                                                monkeypatch):
    """The controller answers the OWL's first state with set_model at once,
    while Owl.__init__ is still setting up the camera."""
    real_start = mqtt_module.OWLMQTTPublisher.start

    def start_and_get_pushed(self):
        real_start(self)
        self.owl_instance._pending_model = 'models/fleet-choice'

    monkeypatch.setattr(mqtt_module.OWLMQTTPublisher, 'start', start_and_get_pushed)
    instance = build(owl_factory, config_factory, input_dir)

    assert instance._pending_model == 'models/fleet-choice'


def _sessions(tmp_path):
    return [p for p in (tmp_path / 'owl_images').rglob('*') if p.is_dir() and p.name.startswith('s')]


def test_paused_recording_keeps_the_session_open_and_saves_nothing(
        owl_factory, config_factory, input_dir, tmp_path):
    """Resume needs no new session: recording never went off, so the loop
    sees no start edge and keeps writing into the same folder (the unpaused
    path is test_mode_matrix.test_recording_saves_images)."""
    instance = build(owl_factory, config_factory, input_dir)
    record(instance, paused=True)

    run_hoot(instance, frames=3)

    sessions = _sessions(tmp_path)
    assert len(sessions) == 1, 'pausing must not stop the session'
    assert not list(sessions[0].glob('*.jpg'))
    assert instance.capture_state()['paused'] is True
    assert instance.capture_state()['paused_slow'] is False


def test_session_folder_carries_owl_id_and_serial(owl_factory, config_factory, input_dir, tmp_path):
    instance = build(owl_factory, config_factory, input_dir)
    record(instance, paused=False)

    run_hoot(instance, frames=2)

    (session,) = _sessions(tmp_path)
    assert re.fullmatch(r's\d{6}_test-owl_[0-9a-f]{4}', session.name)
    assert list(session.glob('*.jpg'))
