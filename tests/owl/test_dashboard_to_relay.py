"""
A spray button on the standalone dashboard, followed all the way to the relay.

    POST /api/...  ->  OWLDashboard route  ->  DashMQTTSubscriber publishes
    JSON on a topic  ->  the Owl's OWLMQTTPublisher._on_message  ->  handler
    ->  RelayControl.relay_on / relay_off

Both ends are real: a real OWLDashboard (standalone_test_client) and a real
Owl (rig). The only stand-in is the broker: what the dashboard's paho client
is asked to publish is handed, topic and bytes, to the Owl's own on-message
callback. Nothing here names an MQTT action, so a route that sends the wrong
command (the 2026-11-16 audit made "all off" send blanket and "stop" send
start, and every test passed) fails at the pin.
"""

import time
from types import SimpleNamespace

import pytest

from harness import PROMPT_S

ALL = {0, 1, 2, 3}            # relay_num = 4 in GENERAL_CONFIG.ini


@pytest.fixture
def cab(rig, standalone_test_client, tmp_path):
    """cab(**rig_kwargs) -> (rig, post). post(path, json) presses a dashboard
    button and returns the Flask response."""
    client, dashboard, _ = standalone_test_client

    def _build(firstboot=False, **kwargs):
        if firstboot:
            # the path owl_factory points OWL_FIRSTBOOT_FLAG at
            (tmp_path / 'no-firstboot.flag').write_text('armed')
        r = rig(dash='standalone', **kwargs)
        subscriber = dashboard.mqtt_client
        subscriber.connected = True

        def deliver(topic, payload, *args, **kwargs):
            r.owl.dash._on_message(None, None, SimpleNamespace(
                topic=topic, payload=payload.encode()))
            r.owl._update_state_once()

        subscriber.client.publish = deliver
        return r, lambda path, json=None: client.post(path, json=json)

    return _build


def no_on_since(r, moment):
    late = [(round(at - moment, 3), relay) for at, relay, on in r.drives if on and at > moment]
    assert not late, f'relays switched on after they should have stopped: {late}'


def test_start_spraying_sprays_weeds_and_stop_closes_an_open_nozzle(cab):
    r, post = cab(spray=False)
    assert r.show_card(settle=0.6).fired == [], 'sprayed before Start was pressed'

    assert post('/api/detection/start').status_code == 200
    showing = r.show_card(x=0.62)
    assert showing.fired == [2]
    assert showing.on < PROMPT_S

    r.cam.card = True                                  # a weed under the boom
    r.wait_for(lambda: r.relays_on(), 'the weed to switch a relay', timeout=3.0)
    assert post('/api/detection/stop').status_code == 200
    r.wait_all_off(timeout=PROMPT_S)
    stopped = time.monotonic()
    time.sleep(0.8)                                    # the weed is still there
    no_on_since(r, stopped)


def test_all_on_switches_every_relay_and_all_off_closes_them(cab):
    r, post = cab(spray=False)
    assert post('/api/nozzles/all-on').status_code == 200
    r.wait_all_on(timeout=PROMPT_S)
    time.sleep(0.5)
    assert r.relays_on() == ALL, 'blanket did not hold'

    assert post('/api/nozzles/all-off').status_code == 200
    r.wait_all_off(timeout=PROMPT_S)
    asked = time.monotonic()
    r.show_card(settle=0.6)                            # off means off, not spot
    no_on_since(r, asked)


def test_hold_sprays_while_held_stops_on_release_and_lets_go_by_itself(cab):
    r, post = cab(spray=False)

    # held: the page renews about every second
    for _ in range(3):
        assert post('/api/nozzles/hold', json={'ms': 1500}).status_code == 200
        r.wait_all_on(timeout=PROMPT_S)
        time.sleep(0.7)
        assert r.relays_on() == ALL, 'the nozzles dropped out under a held button'
    assert post('/api/nozzles/hold', json={'ms': 0}).status_code == 200
    r.wait_all_off(timeout=PROMPT_S)

    # the phone walks away mid-hold, having asked for a minute
    assert post('/api/nozzles/hold', json={'ms': 60000}).status_code == 200
    r.wait_all_on(timeout=PROMPT_S)
    started = time.monotonic()
    r.wait_all_off(timeout=2.5)
    held = time.monotonic() - started
    assert 1.3 <= held <= 2.0, f'held for {held:.2f} s with nothing renewing a 1.5 s lease'


def test_nozzle_test_opens_the_named_nozzle_for_the_time_asked(cab):
    r, post = cab(spray=False)
    assert post('/api/nozzles/test', json={'relays': [1], 'duration_ms': 400}).status_code == 200
    r.wait_for(lambda: r.relays_on() == {1}, 'relay 1 on', timeout=PROMPT_S)
    started = time.monotonic()
    r.wait_all_off(timeout=1.5)
    lasted = time.monotonic() - started
    assert 0.3 <= lasted <= 0.4 + PROMPT_S, f'nozzle test of 0.4 s lasted {lasted:.2f} s'
    assert {relay for _, relay, on in r.drives if on} == {1}

    # an empty list from the page stops a running test
    post('/api/nozzles/test', json={'relays': [0, 3], 'duration_ms': 5000})
    r.wait_for(lambda: r.relays_on() == {0, 3}, 'relays 0 and 3 on', timeout=PROMPT_S)
    assert post('/api/nozzles/test', json={'relays': []}).status_code == 200
    r.wait_all_off(timeout=PROMPT_S)


def test_no_button_opens_a_nozzle_while_first_boot_setup_is_running(cab):
    r, post = cab(firstboot=True, spray=False)
    assert r.owl._firstboot_active is True
    for path, body in [('/api/detection/start', None),
                       ('/api/nozzles/all-on', None),
                       ('/api/nozzles/hold', {'ms': 1500}),
                       ('/api/nozzles/test', {'relays': [0, 1], 'duration_ms': 1000})]:
        assert post(path, json=body).status_code == 200    # sent; the OWL refuses
        time.sleep(0.3)
    r.show_card(settle=0.6)
    assert not [d for d in r.drives if d[2]], 'a relay was switched on during first-boot setup'
    assert r.owl.dash.state['spray']['reason'] == 'firstboot'



def test_finishing_setup_releases_the_hold_and_nothing_asked_during_setup_replays(
        cab, tmp_path, monkeypatch):
    """The setup service's own teardown (FirstBootState._teardown) removes
    the flag while the Owl is running. The Owl looks again at most every 5 s,
    never per frame, and what was pressed during setup must not start by
    itself afterwards."""
    from pathlib import Path
    from unittest.mock import MagicMock
    from controller.setup.firstboot_state import FirstBootState

    r, post = cab(firstboot=True, spray=False)
    flag = tmp_path / 'no-firstboot.flag'

    looked = []
    real_exists = Path.exists

    def exists(self, *args, **kwargs):
        if self == flag:
            looked.append(time.monotonic())
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'exists', exists)

    post('/api/nozzles/all-on')                        # refused: setup is running
    time.sleep(1.5)                                    # ~45 frames
    assert len(looked) <= 1, f'the flag was checked {len(looked)} times in 1.5 s'
    assert not r.relays_on()

    setup = FirstBootState(MagicMock(), flag_path=flag, state_file=tmp_path / 'state.json',
                           timer_factory=MagicMock(), system_runner=MagicMock(),
                           exit_fn=MagicMock(), controller_ini=tmp_path / 'CONTROLLER.ini')
    setup._teardown()
    finished = time.monotonic()
    assert not real_exists(flag)

    r.wait_for(lambda: r.owl._firstboot_active is False, 'the Owl to see setup finish', timeout=6.0)
    assert time.monotonic() - finished <= 5.5
    time.sleep(0.5)
    assert not [d for d in r.drives if d[2]], 'the blanket asked for during setup started by itself'
    assert r.owl.dash.state['spray']['reason'] == 'setup_finished'

    assert post('/api/detection/start').status_code == 200
    showing = r.show_card(x=0.38)
    assert showing.fired == [1]
    assert showing.on < PROMPT_S
