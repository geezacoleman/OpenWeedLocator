"""The deployment matrix, on a real Owl: controller x dashboard x recording.

v3.17-v3.21 saved no images on any unit with controller_type = none and a
dashboard (and on Ute units with a dashboard): every save raised, the error
was caught and logged, and nothing checked that files appeared. Each case
here turns recording on the way that deployment does (switch Value and/or
dashboard state) and asserts the outcome the farmer cares about: JPEGs on
disk, a capture event for the dashboard, and no ERROR in the log (owl_factory
fails the test on one).

Faked: the camera (FakeCamera/scripted frames), the broker (port 1, so the
dashboard publisher runs offline) and the switch-panel process (a thread, so
Ute/Advanced also run on Windows). Everything else is the real code.
"""

import threading
import time

import cv2
import numpy as np
import pytest

import owl as owl_module
from harness import make_frame, run_hoot

DASH = {
    'none': [],
    'standalone': [('MQTT', 'enable', 'True'), ('MQTT', 'broker_ip', '127.0.0.1'),
                   ('MQTT', 'broker_port', '1'), ('MQTT', 'device_id', 'test-owl')],
}
DASH['networked'] = DASH['standalone'] + [('Network', 'mode', 'networked')]


class _ThreadProcess(threading.Thread):
    """Stands in for multiprocessing.Process for the switch-panel controller:
    under 'spawn' (Windows) the real one pickles the whole Owl and hangs."""

    def __init__(self, target):
        super().__init__(target=target, daemon=True)

    def terminate(self):
        pass  # controller.stop() sets its stop flag; run() then returns


@pytest.fixture(autouse=True)
def controller_in_thread(monkeypatch):
    monkeypatch.setattr(owl_module, 'Process', _ThreadProcess)


def build(owl_factory, config_factory, input_dir, controller, dash, extra=()):
    config = config_factory(overrides=[
        ('Controller', 'controller_type', controller),
        ('DataCollection', 'detection_enable', 'True'),
        # No switch and no dashboard: the config is the only way to record
        ('DataCollection', 'image_sample_enable',
         str(controller == 'none' and dash == 'none')),
        ('DataCollection', 'sample_frequency', '1'),
        *DASH[dash], *extra])
    return owl_factory(config, input_file_or_directory=str(input_dir))


class Switch:
    """A switch panel toggle. Off-Pi the controllers have none (they read as
    off), and hoot() re-reads the panel at start."""

    def __init__(self, pressed):
        self.is_pressed = pressed


def switch_on(instance, detection=True, recording=True):
    """What the operator does: flip the panel switches and press the
    dashboard buttons. Each mode's state sync reads the source it uses."""
    controller = instance.controller
    if instance.controller_type == 'ute':
        controller.switch = Switch(recording if controller.switch_purpose == 'recording'
                                   else detection)
        controller.update_state()
    elif instance.controller_type == 'advanced':
        controller.recording_switch = Switch(recording)
        controller.detection_mode_switch_down = Switch(detection)  # spot spray
        controller.detection_mode_switch_up = Switch(False)
        controller.update_state()
    if instance.dash:
        with instance.dash.state_lock:
            instance.dash.state['detection_enable'] = detection
            instance.dash.state['image_sample_enable'] = recording
    instance._update_state_once()


def saved_images(tmp_path):
    return sorted((tmp_path / 'owl_images').rglob('*.jpg'))


MATRIX = [('none', 'none'), ('none', 'standalone'), ('none', 'networked'),
          ('ute', 'none'), ('ute', 'standalone'),
          ('advanced', 'none'), ('advanced', 'standalone')]


@pytest.mark.parametrize('controller,dash', MATRIX)
def test_recording_saves_images(owl_factory, config_factory, input_dir, tmp_path,
                                controller, dash):
    instance = build(owl_factory, config_factory, input_dir, controller, dash)
    captures = []
    if instance.dash:
        real_publish = instance.dash.publish_capture
        instance.dash.publish_capture = lambda rec: (captures.append(1), real_publish(rec))

    switch_on(instance)
    fired = run_hoot(instance, frames=4)

    # A Ute with no dashboard has one switch, set to recording here, so
    # nothing can turn detection on (decided behaviour, see below).
    if (controller, dash) != ('ute', 'none'):
        assert fired, 'detection on: the green blob must fire a relay'
    images = saved_images(tmp_path)
    assert images, f'{controller} + {dash}: recording on but no image was saved'
    if instance.dash:
        assert captures, 'the dashboard gallery/map never heard about a capture'


@pytest.mark.parametrize('controller,dash', [('none', 'standalone'), ('ute', 'none')])
def test_boot_does_not_spray_until_switched_on(owl_factory, config_factory, input_dir,
                                               controller, dash):
    """Decided 2026-10-10 (keep): with a dashboard, or a switch panel and no
    dashboard, a unit never sprays at power-up whatever the config says."""
    instance = build(owl_factory, config_factory, input_dir, controller, dash)
    instance._update_state_once()
    assert run_hoot(instance, frames=3) == []


def test_firstboot_flag_holds_detection_off(owl_factory, config_factory, input_dir,
                                            tmp_path, monkeypatch):
    flag = tmp_path / 'owl-firstboot.flag'
    flag.write_text('armed')
    monkeypatch.setenv('OWL_FIRSTBOOT_FLAG', str(flag))
    instance = build(owl_factory, config_factory, input_dir, 'none', 'standalone')

    switch_on(instance, recording=False)
    assert instance._detection_enable is False
    assert run_hoot(instance, frames=3) == []


def test_bbox_crops_are_the_detected_weed(owl_factory, config_factory, input_dir, tmp_path):
    """Boxes are in cropped-frame pixels; crops used to be cut from the full
    frame at those coordinates, i.e. shifted off the weed."""
    instance = build(owl_factory, config_factory, input_dir, 'none', 'none',
                     extra=[('DataCollection', 'sample_method', 'bbox')])
    switch_on(instance)
    run_hoot(instance, frames=2)

    images = saved_images(tmp_path)
    assert images
    crop = cv2.imread(str(images[0]))
    # make_frame's blob is (60, 180, 60) BGR on (40, 60, 90) soil: the crop
    # must be centred on it (an offset crop has the blob against one edge)
    ys, xs = np.nonzero(crop[:, :, 1] > 150)
    h, w = crop.shape[:2]
    assert len(xs) > 0.3 * h * w, 'the saved crop is mostly not the weed'
    assert abs(xs.mean() - w / 2) < 0.1 * w and abs(ys.mean() - h / 2) < 0.1 * h,         'the saved crop is not centred on the weed'


def test_preset_load_does_not_replace_shared_switch_values(owl_factory, config_factory,
                                                           input_dir):
    """Loading a preset sends [DataCollection]; the catch-all used to set
    owl.image_sample_enable (a shared Value) to the string 'False', and the
    next state-sync cycle then raised forever."""
    instance = build(owl_factory, config_factory, input_dir, 'ute', 'standalone')
    save_dir = instance.save_directory

    instance.dash._handle_set_config_section('DataCollection', {
        'image_sample_enable': 'False', 'detection_enable': 'True',
        'save_directory': 'somewhere_else'})

    assert hasattr(instance.image_sample_enable, 'get_lock')
    assert hasattr(instance.detection_enable, 'get_lock')
    assert instance.save_directory == save_dir
    instance._update_state_once()  # must not raise


# --------------------------------------------------------------------------
# Spraying faults and [System] on_error
# --------------------------------------------------------------------------

class ScriptedCamera:
    """A live camera (not --input): None means a dropped frame. Raises
    KeyboardInterrupt when the script runs out, which ends hoot()."""

    def __init__(self, script):
        self.script = list(script)
        self.frame = make_frame()

    def read_with_metadata(self):
        if not self.script:
            raise KeyboardInterrupt
        return (self.frame.copy() if self.script.pop(0) else None), None

    def stop(self):
        pass


def spy_relays(instance):
    calls = []
    relay = instance.relay_controller.relay
    for name in ('all_on', 'all_off'):
        real = getattr(relay, name)
        setattr(relay, name, lambda *a, _n=name, _r=real, **k: (calls.append(_n), _r(*a, **k)))
    return calls


@pytest.mark.allow_errors
def test_camera_lost_on_error_off_stops_spraying(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir, 'none', 'standalone')
    switch_on(instance, recording=False)
    instance.input_file_or_directory = None
    instance.cam = ScriptedCamera([False] * 20)
    calls = spy_relays(instance)

    with pytest.raises(SystemExit):
        instance.hoot()

    assert instance._fault_state['code'] == 'camera_lost'
    assert instance._fault_state['action'] == 'nozzles_off'
    assert 'all_on' not in calls and 'all_off' in calls
    assert instance.dash.state['fault']['code'] == 'camera_lost'


@pytest.mark.allow_errors
def test_camera_lost_on_error_on_blanket_sprays_then_recovers(owl_factory, config_factory,
                                                              input_dir):
    instance = build(owl_factory, config_factory, input_dir, 'none', 'standalone',
                     extra=[('System', 'on_error', 'on')])
    switch_on(instance, recording=False)
    instance.input_file_or_directory = None
    # 11 dropped frames raise the fault, then the camera comes back
    instance.cam = ScriptedCamera([False] * 11 + [True] * 2)
    calls = spy_relays(instance)

    try:
        instance.hoot()
    except SystemExit:
        pass

    assert 'all_on' in calls, 'on_error = on must blanket spray while the camera is gone'
    assert instance._fault_state is None, 'the fault must clear when frames return'
    assert calls.index('all_off', calls.index('all_on')) > 0, 'blanket must end on recovery'


@pytest.mark.allow_errors
def test_state_sync_survives_errors_and_raises_a_fault(owl_factory, config_factory,
                                                       input_dir, monkeypatch):
    """The state-sync thread used to die on the first exception, silently:
    the dashboard could no longer turn spraying off."""
    instance = build(owl_factory, config_factory, input_dir, 'none', 'standalone')
    monkeypatch.setattr(instance, '_STATE_FAILURES_BEFORE_FAULT', 3)
    real = instance._get_best_gps_data

    def broken():
        raise ValueError('bad GPS payload')

    monkeypatch.setattr(instance, '_get_best_gps_data', broken)
    deadline = time.time() + 3
    while instance._fault_state is None and time.time() < deadline:
        time.sleep(0.05)
    assert instance._fault_state['code'] == 'control_lost'
    assert instance._detection_enable is False  # on_error = off

    monkeypatch.setattr(instance, '_get_best_gps_data', real)
    deadline = time.time() + 3
    while instance._fault_state is not None and time.time() < deadline:
        time.sleep(0.05)
    assert instance._fault_state is None, 'the thread kept running and recovered'
