"""AI on a unit that cannot run it, on a real Owl.

Field bug, owl-1 (2026-10-12): installed with owl_setup.sh --factory, so no
ultralytics. Choosing AI saved algorithm=gog, the detector failed to load,
and after a restart the OWL ran with no detection at all.

That got an exhsv fallback in v3.25. Reversed 2026-11-20 (Guy, docs/HAILO.md
§4): colour detection only finds green, so in a crop it sprays the crop. AI
that can't run is now a spraying fault under on_error, loud everywhere:
- gog/gog-hybrid that fails to load at startup -> ai_unavailable, no detector;
- AI that stops mid-field -> retry (a Hailo), the model's CPU build, else ai_lost;
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


def build(owl_factory, config_factory, input_dir, algorithm, on_error='off'):
    config = config_factory(overrides=[
        ('System', 'algorithm', algorithm),
        ('System', 'on_error', on_error),
        ('DataCollection', 'detection_enable', 'True'),
        *DASH])
    return owl_factory(config, input_file_or_directory=str(input_dir)), config


def a_model():
    """A .pt in the OWL home's models/: the load then fails on ultralytics,
    not on an empty folder."""
    from utils.paths import home_dir
    models_dir = home_dir('models')
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / 'weeds.pt').write_bytes(b'pt')


def detection_on(instance):
    # The dashboard's Start button: a command the OWL decides on
    instance.dash._handle_command({'action': 'set_detection_enable', 'value': True})
    instance._update_state_once()


@pytest.mark.allow_errors
@pytest.mark.parametrize('algorithm', ['gog', 'gog-hybrid'])
def test_ai_that_cannot_start_is_a_fault_never_colour_detection(
        owl_factory, config_factory, input_dir, no_ultralytics, algorithm):
    a_model()
    instance, config_path = build(owl_factory, config_factory, input_dir, algorithm)
    detection_on(instance)

    fired = run_hoot(instance, frames=4)

    assert not fired, 'no colour detection standing in for AI: it would spray the crop'
    assert instance.running_algorithm is None
    assert instance._fault_state['code'] == 'ai_unavailable'
    assert instance._fault_state['action'] == 'nozzles_off'
    assert instance._ai_status == 'fault'
    instance.dash._refresh_ai_state()
    state = instance.dash.state
    assert 'ultralytics' in state['algorithm_error']
    assert state['ai']['status'] == 'fault' and 'AI could not start' in state['ai']['status_reason']
    # The file is not rewritten: once AI is installed the unit runs it again
    assert f'algorithm = {algorithm}' in config_path.read_text()


@pytest.mark.allow_errors
def test_ai_that_cannot_start_follows_on_error_on(owl_factory, config_factory, input_dir,
                                                  no_ultralytics):
    instance, _ = build(owl_factory, config_factory, input_dir, 'gog', on_error='on')
    detection_on(instance)
    run_hoot(instance, frames=2)
    assert instance._fault_state['code'] == 'ai_unavailable'
    assert instance._fault_state['action'] == 'blanket', 'detection cannot run: blanket, as for a lost camera'


@pytest.mark.allow_errors
def test_switching_to_colour_on_purpose_clears_the_ai_fault(owl_factory, config_factory,
                                                            input_dir, no_ultralytics):
    instance, _ = build(owl_factory, config_factory, input_dir, 'gog')
    instance.dash._handle_command({'action': 'set_algorithm', 'value': 'exhsv'})
    run_hoot(instance, frames=3)
    assert instance.running_algorithm == 'exhsv'
    assert 'ai_unavailable' not in instance._faults
    assert instance._ai_status == 'ok'


@pytest.mark.allow_errors
def test_live_switch_to_ai_that_fails_keeps_detection_and_config(
        owl_factory, config_factory, input_dir, no_ultralytics):
    a_model()
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
    assert {'cpu', 'hailo', 'accelerator'} <= set(ai)
    assert set(ai['hailo']) == {'present', 'chip', 'runtime', 'hailort'}
    assert isinstance(ai['cpu']['installed'], bool)


# ---------------------------------------------------------------------------
# Mid-field: the Hailo stops (docs/HAILO.md §4)
# ---------------------------------------------------------------------------

class FlakyModel:
    """A YOLO stand-in whose predict() raises like a Hailo that went away."""

    task = 'detect'
    names = {0: 'weed'}
    predictor = None

    def __init__(self, path, task=None, fail=False):
        self.path, self.fail = str(path), fail

    def predict(self, **_kw):
        if self.fail:
            raise RuntimeError('HAILO_FW_CONTROL failed with 19')
        return []

    track = predict


@pytest.fixture
def models(owl_home, monkeypatch):
    """models/ with a Hailo build and a CPU build of one model. Loading a
    folder named *_hailo_model gives a model that raises on inference; any
    reload of it fails (the card is gone); the .pt works."""
    from utils import model_catalog
    models_dir = owl_home / 'models'
    models_dir.mkdir(exist_ok=True)
    hailo = models_dir / 'weeds_hailo_model'
    hailo.mkdir()
    (hailo / 'model.hef').write_text('Architecture HEF was compiled for: HAILO8L\n')
    (hailo / 'metadata.yaml').write_text('task: detect\nnames:\n  0: weed\n')
    loads = []

    def fake_yolo(path, task=None):
        loads.append(str(path))
        if '_hailo_model' in str(path):
            if len([p for p in loads if '_hailo_model' in p]) > 1:
                raise RuntimeError('Hailo device not found')
            return FlakyModel(path, fail=True)
        return FlakyModel(path)

    monkeypatch.setattr(gog_module, 'YOLO', fake_yolo)
    real = model_catalog.hef_info
    monkeypatch.setattr(model_catalog, 'hef_info', lambda p, runner=None: real(
        p, runner or (lambda argv, **kw: type('R', (), {'stdout': open(argv[-1]).read(), 'stderr': ''})())))
    monkeypatch.setattr('owl.HAILO_RETRY_S', 0)
    return models_dir, loads


def hailo_unit(instance):
    instance._ai_capability = {'cpu': {'installed': True}, 'accelerator': 'hailo8l',
                               'hailo': {'present': True, 'chip': 'hailo8l', 'runtime': True, 'hailort': '4.23.0'}}
    instance.dash.state['ai'] = instance._ai_capability


@pytest.mark.allow_errors
def test_a_hailo_that_stops_runs_the_cpu_build_with_a_warning(
        owl_factory, config_factory, input_dir, models):
    models_dir, loads = models
    (models_dir / 'weeds.pt').write_bytes(b'pt')
    instance, _ = build(owl_factory, config_factory, input_dir, 'gog')
    hailo_unit(instance)
    detection_on(instance)

    run_hoot(instance, frames=4)

    assert loads[0].endswith('weeds_hailo_model'), 'the Hailo build starts first'
    assert loads.count(loads[0]) == 1 + 3, 'loaded once, then re-opened 3 times'
    assert loads[-1].endswith('weeds.pt')
    assert instance._ai_status == 'degraded'
    assert instance._faults == {}, 'running on the CPU is a warning, not a fault'
    instance.dash._refresh_ai_state()
    assert instance.dash.state['ai']['status'] == 'degraded'
    assert 'on the CPU' in instance.dash.state['ai']['status_reason']


@pytest.mark.allow_errors
def test_a_hailo_that_stops_with_no_cpu_build_is_the_ai_lost_fault(
        owl_factory, config_factory, input_dir, models):
    instance, _ = build(owl_factory, config_factory, input_dir, 'gog')
    hailo_unit(instance)
    detection_on(instance)

    run_hoot(instance, frames=4)

    assert instance._fault_state['code'] == 'ai_lost'
    assert instance._fault_state['action'] == 'nozzles_off'
    assert instance._ai_status == 'fault'
    assert instance.running_algorithm == 'gog', 'still the chosen mode: Try the Hailo again works from here'


@pytest.mark.allow_errors
def test_run_on_hailo_never_falls_back_to_the_cpu(owl_factory, config_factory, input_dir, models):
    from utils import model_catalog
    models_dir, loads = models
    (models_dir / 'weeds.pt').write_bytes(b'pt')
    model_catalog.write_sidecar(models_dir, 'weeds', run_on='hailo')
    instance, _ = build(owl_factory, config_factory, input_dir, 'gog')
    hailo_unit(instance)
    detection_on(instance)

    run_hoot(instance, frames=4)

    assert not any(p.endswith('weeds.pt') for p in loads)
    assert instance._fault_state['code'] == 'ai_lost'
