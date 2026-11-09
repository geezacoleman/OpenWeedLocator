"""One spray owner (v3.27), on a real Owl.

Every input (dashboard / app / controller request, the Ute or Advanced
switch, first-boot, faults, link loss) goes through utils/spray_mode.resolve
and owl._apply_spray, and RelayController.set_mode is the only thing that
switches the nozzles. These are the decided behaviours (2026-11-07):

- the hardware switch OFF is a hard kill, including a dashboard blanket;
- a request never comes back by itself after an inhibit (first-boot, an
  on_error = off fault, a link-loss blanket stop), and the reason is shown;
- a Ute switch held ON at boot sprays;
- link loss (networked only) never stops spot, stops blanket only under
  on_error = off;
- hold to spray is an OWL-side lease that works with the switch off.
"""

import time

import pytest

import owl as owl_module
import utils.input_manager as input_manager
from harness import run_hoot

DASH = [('MQTT', 'enable', 'True'), ('MQTT', 'broker_ip', '127.0.0.1'),
        ('MQTT', 'broker_port', '1'), ('MQTT', 'device_id', 'test-owl')]


class _ThreadProcess:
    """multiprocessing.Process stand-in: the switch-panel process only
    sleeps, and under 'spawn' (Windows) the real one pickles the Owl."""

    def __init__(self, target, daemon=True):
        self.target = target

    def start(self):
        pass

    def is_alive(self):
        return False

    def terminate(self):
        pass


@pytest.fixture(autouse=True)
def controller_in_thread(monkeypatch):
    monkeypatch.setattr(owl_module, 'Process', _ThreadProcess)


class Switch:
    def __init__(self, pressed):
        self.is_pressed = pressed


def build(owl_factory, config_factory, input_dir, controller='none', dash=True,
          extra=()):
    config = config_factory(overrides=[
        ('Controller', 'controller_type', controller),
        ('Controller', 'switch_purpose', 'detection'),
        *(DASH if dash else []), *extra])
    return owl_factory(config, input_file_or_directory=str(input_dir))


def press(instance, action, value):
    instance.dash._handle_command({'action': action, 'value': value})


def ute_switch(instance, on):
    instance.controller.switch = Switch(on)
    instance.controller.update_state()


def mode(instance):
    return instance.relay_controller.mode


# --------------------------------------------------------------------------
# Hardware switch vs dashboard
# --------------------------------------------------------------------------

def test_ute_switch_off_kills_a_dashboard_blanket(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir, controller='ute')
    ute_switch(instance, True)
    assert mode(instance) == 'spot', 'switch ON = spot'

    press(instance, 'set_detection_mode', 2)
    assert mode(instance) == 'blanket', 'the dashboard can choose blanket while the switch is ON'

    ute_switch(instance, False)
    assert mode(instance) == 'off', 'switch OFF must stop a dashboard blanket'
    assert instance._spray_request == 'off', 'switch OFF clears the request'
    instance._update_state_once()
    assert instance.dash.state['detection_mode'] == 1
    assert instance.dash.state['spray']['reason'] == 'switch_off'

    press(instance, 'set_detection_mode', 2)
    assert mode(instance) == 'off', 'no dashboard request while the switch is OFF'

    ute_switch(instance, True)
    assert mode(instance) == 'spot', 'switching back ON is spot, not the old blanket'


def test_tests_and_hold_still_work_with_the_switch_off(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir, controller='ute')
    ute_switch(instance, False)
    assert instance.hold_spray(1500)['ok'] is True
    assert instance.relay_controller.test_relays == list(range(len(instance.relay_controller.relay_condition_dict)))
    assert instance.hold_spray(0)['ok'] is True
    assert instance.relay_controller.test_relays == []
    assert instance.test_nozzles([1], 0.5)['relays'] == [1]
    instance.test_nozzles([], 0)


def test_hold_has_no_maximum_on_time(owl_factory, config_factory, input_dir):
    """Held for a minute, it sprays for a minute: only the lease ends a hold.
    A timed nozzle test of every relay is replaced by the hold, not renewed
    under its cap."""
    instance = build(owl_factory, config_factory, input_dir)
    rc = instance.relay_controller
    everything = list(range(len(rc.relay_condition_dict)))
    instance.test_nozzles(everything, 5)
    assert rc.test_capped is True
    assert instance.hold_spray(1500)['ok'] is True
    assert rc.test_capped is False, 'a hold must not inherit a cap'
    assert instance.hold_spray(1500)['ok'] is True   # a renew
    assert rc.test_capped is False and rc.test_relays == everything
    instance.hold_spray(0)


def test_ute_switch_on_at_boot_sprays(owl_factory, config_factory, input_dir, monkeypatch):
    monkeypatch.setattr(input_manager, 'testing', False)
    monkeypatch.setattr(input_manager, 'Button',
                        lambda pin, bounce_time=1.0: Switch(True), raising=False)
    instance = build(owl_factory, config_factory, input_dir, controller='ute', dash=False)
    assert instance._spray_request == 'spot'
    assert mode(instance) == 'spot'
    assert run_hoot(instance, frames=3), 'a Ute switched ON at power-up sprays'


def test_ute_switch_on_during_first_boot_never_sprays(owl_factory, config_factory, input_dir,
                                                      monkeypatch, tmp_path):
    flag = tmp_path / 'owl-firstboot.flag'
    flag.write_text('armed')
    monkeypatch.setenv('OWL_FIRSTBOOT_FLAG', str(flag))
    monkeypatch.setattr(input_manager, 'testing', False)
    monkeypatch.setattr(input_manager, 'Button',
                        lambda pin, bounce_time=1.0: Switch(True), raising=False)
    instance = build(owl_factory, config_factory, input_dir, controller='ute', dash=False)
    assert mode(instance) == 'off'
    assert instance._spray_request == 'off'
    assert run_hoot(instance, frames=3) == []


def test_advanced_switch_positions(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir, controller='advanced')
    controller = instance.controller
    controller.detection_mode_switch_up = Switch(True)    # blanket
    controller.detection_mode_switch_down = Switch(False)
    controller.update_detection_mode_state()
    assert mode(instance) == 'blanket'
    controller.detection_mode_switch_up = Switch(False)   # centre: off
    controller.update_detection_mode_state()
    assert mode(instance) == 'off'
    # hoot() re-reads the panel at start: it must not re-energise anything
    controller.update_state()
    assert mode(instance) == 'off'


# --------------------------------------------------------------------------
# First-boot: refused, not replayed
# --------------------------------------------------------------------------

def test_first_boot_request_is_refused_not_replayed(owl_factory, config_factory, input_dir,
                                                    monkeypatch, tmp_path):
    flag = tmp_path / 'owl-firstboot.flag'
    flag.write_text('armed')
    monkeypatch.setenv('OWL_FIRSTBOOT_FLAG', str(flag))
    instance = build(owl_factory, config_factory, input_dir)

    press(instance, 'set_detection_mode', 2)
    assert mode(instance) == 'off'
    assert instance.dash.state['detection_mode'] == 1, 'the refusal is what the dashboard sees'
    assert instance.dash.state['spray']['reason'] == 'firstboot'
    assert instance.hold_spray(1500)['ok'] is False

    flag.unlink()
    instance._firstboot_last_check = 0.0
    instance._update_state_once()
    assert instance._firstboot_active is False
    assert mode(instance) == 'off', 'the blanket asked for during setup must not start now'
    assert instance.dash.state['spray']['reason'] == 'setup_finished'

    press(instance, 'set_detection_mode', 2)
    assert mode(instance) == 'blanket'
    assert instance.dash.state['spray']['reason'] is None


# --------------------------------------------------------------------------
# Faults
# --------------------------------------------------------------------------

@pytest.mark.allow_errors
def test_on_error_off_fault_clears_the_request(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_enable', True)
    assert mode(instance) == 'spot'

    instance._fault('control_lost', 'test', detection_possible=True)
    assert mode(instance) == 'off'
    instance._clear_fault('control_lost')
    assert mode(instance) == 'off', 'spraying must not resume by itself'
    instance._update_state_once()
    spray = instance.dash.state['spray']
    assert spray['reason'] == 'fault:off:control_lost'
    assert spray['reason_text'] == 'Control fault'


@pytest.mark.allow_errors
def test_two_faults_are_both_tracked(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir, extra=[('System', 'on_error', 'on')])
    press(instance, 'set_detection_enable', True)
    instance._fault('relay_stuck', 'stuck', detection_possible=True)
    instance._fault('camera_lost', 'gone', detection_possible=False)
    assert mode(instance) == 'blanket'
    instance._clear_fault('camera_lost')
    assert mode(instance) == 'spot', 'on_error = on: spot again once frames return'
    assert instance._fault_state['code'] == 'relay_stuck', 'the other fault is still there'
    instance._update_state_once()
    assert [f['code'] for f in instance.dash.state['faults']] == ['relay_stuck']


@pytest.mark.allow_errors
def test_stuck_relay_is_raised_by_the_sync_tick(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    instance._relay_stuck('Relay 0 is stuck on: gpio busy')
    assert 'relay_stuck' not in instance._faults, 'the relay thread only records it'
    instance._update_state_once()
    assert 'relay_stuck' in instance._faults


@pytest.mark.allow_errors
def test_sync_stall_raises_control_lost_from_the_main_loop(owl_factory, config_factory,
                                                           input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_enable', True)
    instance._sync_thread_started = True
    instance._last_sync_at = time.monotonic() - owl_module.SYNC_STALL_S - 1
    instance._check_sync_watchdog()
    assert 'control_lost' in instance._faults
    assert mode(instance) == 'off'
    instance._last_sync_at = time.monotonic()
    instance._check_sync_watchdog()
    assert 'control_lost' not in instance._faults


@pytest.mark.allow_errors
def test_hung_main_loop_raises_control_lost_from_the_sync_thread(owl_factory, config_factory,
                                                                 input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    instance._last_loop_at = time.monotonic() - owl_module.LOOP_STALL_S - 1
    instance._check_loop_watchdog()
    assert instance._faults['control_lost']['detection_possible'] is False


# --------------------------------------------------------------------------
# Link loss (networked)
# --------------------------------------------------------------------------

def networked(owl_factory, config_factory, input_dir, on_error='off'):
    return build(owl_factory, config_factory, input_dir,
                 extra=[('Network', 'mode', 'networked'), ('System', 'on_error', on_error)])


def lose_link(instance):
    instance.dash.connected = True
    instance._controller_seen_at = time.monotonic() - owl_module.LINK_LOST_S - 1
    instance._check_link()


@pytest.mark.allow_errors
def test_link_loss_stops_blanket_under_on_error_off(owl_factory, config_factory, input_dir):
    instance = networked(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_mode', 2)
    lose_link(instance)
    assert 'link_lost' in instance._faults
    assert mode(instance) == 'off'
    instance.controller_seen()
    instance._check_link()
    assert 'link_lost' not in instance._faults
    assert mode(instance) == 'off', 'the blanket does not come back by itself'


@pytest.mark.allow_errors
@pytest.mark.parametrize('on_error, request_, expected', [
    ('off', 1, 'off'), ('off', 0, 'spot'), ('on', 0, 'spot'), ('on', 2, 'blanket')])
def test_link_loss_keeps_spot_and_on_error_on_blanket(owl_factory, config_factory, input_dir,
                                                      on_error, request_, expected):
    instance = networked(owl_factory, config_factory, input_dir, on_error=on_error)
    press(instance, 'set_detection_mode', request_)
    lose_link(instance)
    assert 'link_lost' in instance._faults, 'link loss is always reported'
    assert mode(instance) == expected


def test_standalone_has_no_link_loss(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_mode', 2)
    instance._controller_seen_at = time.monotonic() - 60
    instance._check_link()
    assert mode(instance) == 'blanket', 'the phone leaving must change nothing'


@pytest.mark.allow_errors  # the bad payload is logged; the offline broker is a lost link
def test_controller_message_is_stamped_before_handling(owl_factory, config_factory, input_dir):
    instance = networked(owl_factory, config_factory, input_dir)

    class Msg:
        topic = instance.dash.topics['commands']
        payload = b'not json'

    instance._controller_seen_at = None
    instance.dash._on_message(None, None, Msg())
    assert instance._controller_seen_at is not None


# --------------------------------------------------------------------------
# Camera, shutdown, power commands
# --------------------------------------------------------------------------

class DeadCamera:
    def __init__(self):
        self.calls = 0

    def read_with_metadata(self):
        self.calls += 1
        if self.calls > 400:
            raise KeyboardInterrupt
        return None, None

    def stop(self):
        pass


@pytest.mark.allow_errors
def test_camera_lost_under_on_error_off_retries_then_exits_non_zero(owl_factory, config_factory,
                                                                     input_dir, monkeypatch):
    instance = build(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_enable', True)
    instance.input_file_or_directory = None
    instance.cam = DeadCamera()
    monkeypatch.setattr(owl_module, 'CAMERA_REOPEN_S', 0.0)
    reopens = []
    monkeypatch.setattr(instance, '_reopen_camera', lambda: reopens.append(1) or False)

    with pytest.raises(SystemExit) as exit_info:
        instance.hoot()
    assert exit_info.value.code == 1, 'systemd must see a failure and restart the OWL'
    assert len(reopens) == owl_module.CAMERA_REOPEN_ATTEMPTS
    assert mode(instance) == 'off'


def test_stop_turns_the_nozzles_off_before_anything_slow(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_mode', 2)
    order = []
    real_set_mode = instance.relay_controller.set_mode
    instance.relay_controller.set_mode = lambda m: (order.append(('mode', m)), real_set_mode(m))[1]

    class SlowRecorder:
        def stop(self):
            order.append(('recorder', None))

    instance.image_recorder = SlowRecorder()
    with pytest.raises(SystemExit):
        instance.stop()
    assert order[0] == ('mode', 'off'), f'nozzles went off after: {order}'


def test_reboot_command_turns_spraying_off_first(owl_factory, config_factory, input_dir,
                                                 monkeypatch):
    instance = build(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_mode', 2)
    ran = []
    monkeypatch.setattr(instance.dash, '_handle_reboot', lambda: ran.append(mode(instance)))
    instance.dash._handle_command({'action': 'reboot'})
    assert instance.dash.io_flush()
    assert ran == ['off']


def test_spray_state_is_published(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir)
    press(instance, 'set_detection_mode', 0)
    spray = instance.dash.state['spray']
    assert spray['request'] == 'spot' and spray['effective'] == 'spot'
    assert instance.dash.state['detection_enable'] is True
    assert instance.dash.state['faults'] == []


@pytest.mark.allow_errors
def test_ute_led_says_switch_off_and_on_after_a_stop(owl_factory, config_factory, input_dir):
    """No screen in the cab: a switch left ON after spraying was stopped
    flashes code 9 until it is switched off and on again (faults first)."""
    instance = build(owl_factory, config_factory, input_dir, controller='ute')
    ute_switch(instance, True)
    assert mode(instance) == 'spot'

    instance._fault('control_lost', 'test', detection_possible=True)
    assert instance.status_indicator.error_code == 4, 'the fault flashes first'
    instance._clear_fault('control_lost')
    assert mode(instance) == 'off'
    assert instance.status_indicator.error_code == 9, 'switch is ON but spraying will not resume'

    ute_switch(instance, False)
    assert instance.status_indicator.error_code is None
    ute_switch(instance, True)
    assert mode(instance) == 'spot'
    assert instance.status_indicator.error_code is None


def test_no_hint_without_a_stop(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir, controller='ute')
    ute_switch(instance, True)
    press(instance, 'set_detection_enable', False)   # dashboard off: a choice, not a stop
    assert instance.status_indicator.error_code is None


@pytest.mark.allow_errors  # the second half raises the fault on purpose
def test_loading_a_model_is_not_a_hung_loop(owl_factory, config_factory, input_dir):
    """A model hot-swap can take many seconds on a Pi; under on_error = off a
    false control_lost would stop spraying until someone presses Start."""
    instance = build(owl_factory, config_factory, input_dir)
    instance._last_loop_at = time.monotonic() - owl_module.LOOP_STALL_S - 5
    instance._loop_busy = 'loading gog'
    instance._check_loop_watchdog()
    assert 'control_lost' not in instance._faults
    instance._loop_busy = None
    instance._check_loop_watchdog()
    assert 'control_lost' in instance._faults
    instance._clear_fault('control_lost')


def test_frame_age_is_published(owl_factory, config_factory, input_dir):
    instance = build(owl_factory, config_factory, input_dir)

    class Cam:
        last_frame_at = time.monotonic() - 2.5

    instance.cam = Cam()
    age = instance.get_system_stats()['last_frame_age_s']
    assert 2.0 <= age <= 4.0
