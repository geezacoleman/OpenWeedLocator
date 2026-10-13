"""AI on a unit that cannot run it, on a real Owl.

Field bug, owl-1 (2026-10-12): installed with owl_setup.sh --factory, so no
ultralytics. Choosing AI saved algorithm=gog, the detector failed to load,
and after a restart the OWL ran with no detection at all. Now:
- a gog/gog-hybrid that fails to load at startup falls back to exhsv (as lut
  already did) with algorithm_error set;
- a live switch only writes [System] algorithm once the detector loaded;
- the state reports the detector actually running.
"""

import pytest

import utils.greenongreen as gog_module
from harness import run_hoot

DASH = [('MQTT', 'enable', 'True'), ('MQTT', 'broker_ip', '127.0.0.1'),
        ('MQTT', 'broker_port', '1'), ('MQTT', 'device_id', 'test-owl')]


@pytest.fixture
def no_ultralytics(monkeypatch):
    """What a factory unit has: GreenOnGreen raises 'ultralytics is required'."""
    monkeypatch.setattr(gog_module, 'YOLO', None)


def build(owl_factory, config_factory, input_dir, algorithm):
    config = config_factory(overrides=[
        ('System', 'algorithm', algorithm),
        ('DataCollection', 'detection_enable', 'True'),
        *DASH])
    return owl_factory(config, input_file_or_directory=str(input_dir)), config


def detection_on(instance):
    with instance.dash.state_lock:
        instance.dash.state['detection_enable'] = True
    instance._update_state_once()


@pytest.mark.allow_errors
@pytest.mark.parametrize('algorithm', ['gog', 'gog-hybrid'])
def test_ai_without_ultralytics_falls_back_to_exhsv_at_startup(
        owl_factory, config_factory, input_dir, no_ultralytics, algorithm):
    instance, config_path = build(owl_factory, config_factory, input_dir, algorithm)
    detection_on(instance)

    fired = run_hoot(instance, frames=4)

    assert fired, 'the unit must still detect weeds with colour detection'
    assert instance.running_algorithm == 'exhsv'
    instance.dash._refresh_ai_state()
    state = instance.dash.state
    assert state['algorithm'] == 'exhsv', 'state must report what runs, not the config'
    assert 'ultralytics' in state['algorithm_error']
    # The file is not rewritten: once AI is installed the unit runs it again
    assert f'algorithm = {algorithm}' in config_path.read_text()


@pytest.mark.allow_errors
def test_live_switch_to_ai_that_fails_keeps_detection_and_config(
        owl_factory, config_factory, input_dir, no_ultralytics):
    instance, config_path = build(owl_factory, config_factory, input_dir, 'exhsv')
    detection_on(instance)
    instance.dash._handle_command({'action': 'set_algorithm', 'value': 'gog'})
    assert instance.dash.state['algorithm_pending'] == 'gog'

    fired = run_hoot(instance, frames=4)

    assert fired
    assert instance.running_algorithm == 'exhsv'
    # Not persisted as gog, in memory or on disk
    assert instance.config.get('System', 'algorithm') == 'exhsv'
    assert 'algorithm = exhsv' in config_path.read_text()
    instance.dash._refresh_ai_state()
    state = instance.dash.state
    assert state['algorithm'] == 'exhsv'
    assert state['algorithm_pending'] is None
    assert 'ultralytics' in state['algorithm_error']


def test_live_switch_that_loads_updates_config_and_state(
        owl_factory, config_factory, input_dir):
    instance, _ = build(owl_factory, config_factory, input_dir, 'exhsv')
    instance.dash._handle_command({'action': 'set_algorithm', 'value': 'hsv'})
    # Requested is not running yet, and the config waits for the load
    assert instance.config.get('System', 'algorithm') == 'exhsv'

    run_hoot(instance, frames=3)

    assert instance.running_algorithm == 'hsv'
    assert instance.config.get('System', 'algorithm') == 'hsv'
    instance.dash._refresh_ai_state()
    assert instance.dash.state['algorithm'] == 'hsv'
    assert instance.dash.state['algorithm_pending'] is None
    assert instance.dash.state['algorithm_error'] is None


def test_state_carries_the_ai_capability_block(owl_factory, config_factory, input_dir):
    instance, _ = build(owl_factory, config_factory, input_dir, 'exhsv')
    ai = instance.dash.state['ai']
    assert set(ai) == {'cpu', 'hailo', 'accelerator'}
    assert set(ai['hailo']) == {'present', 'chip', 'runtime'}
    assert isinstance(ai['cpu']['installed'], bool)
