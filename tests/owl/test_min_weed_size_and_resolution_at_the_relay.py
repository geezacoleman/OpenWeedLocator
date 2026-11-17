"""Two things a config file decides, on a real Owl.

Minimum weed size. `min_detection_area_percent` (percent of the detection
frame) is the setting; `min_detection_area` (px) is the legacy key old
profiles and old clients still send. Both must end up deciding what is
sprayed, asserted at the relay. Before this file the px-to-percent
migration at boot and the live px conversion were covered by string matches
on owl.py and mqtt_manager.py.

The Pi 3/4 resolution clamp. Above 832x640 those boards are run at 640x480
unless [Camera] allow_high_resolution is set, and the dashboard is told so.
Was covered by `'not allow_high_resolution' in owl_source`.

Card sizes: with exhsv a card is measured with the ring the adaptive
threshold adds around it (about 12 px a side), so on the 416x320 test frame
a 30 px card measures about 2.0 % of the frame and an 80 px card about 8 %.
"""

import pytest

import harness
import owl as owl_module
from harness import PROMPT_S

FRAME_PX = 416 * 320
SMALL, BIG = 30, 80


@pytest.fixture
def card_size(monkeypatch):
    """Lets a test choose the size of the card the camera shows.
    (harness.CardCamera has no size setting; see the report for the request.)"""
    size = {'px': BIG}
    real = harness.card_frame

    def sized(*args, **kwargs):
        kwargs['size'] = size['px']
        return real(*args[:4], **kwargs)

    monkeypatch.setattr(harness, 'card_frame', sized)
    return size


def sprayed(r, card_size, px):
    card_size['px'] = px
    showing = r.show_card(seconds=0.4, settle=2.0)
    if showing.fired:
        assert 0 <= showing.on < PROMPT_S
    return bool(showing.fired)


def detection_frame_px(instance):
    """Area of the frame the detector sees (the camera frame less the crop margins)."""
    return instance.cropped_width * instance.cropped_height


def test_min_weed_size_decides_what_is_sprayed_and_follows_live_changes(rig, card_size):
    r = rig(dash='standalone')
    assert r.owl.min_detection_area_percent == 0.0075, 'the shipped medium preset'
    assert sprayed(r, card_size, SMALL), 'under the shipped minimum a small weed is sprayed'

    # The dashboard raises it to 4 % of the frame
    r.command('set_config_section', section='GreenOnBrown',
              params={'min_detection_area_percent': 4.0})
    assert not sprayed(r, card_size, SMALL), 'a weed of 2 % of the frame is under a 4 % minimum'
    assert sprayed(r, card_size, BIG), 'a weed of 8 % is over it'

    # ...and back down
    r.command('set_config_section', section='GreenOnBrown',
              params={'min_detection_area_percent': 0.5})
    assert sprayed(r, card_size, SMALL)

    # An old client sends the legacy px key: 5000 px^2 of the detection frame
    r.command('set_config_section', section='GreenOnBrown', params={'min_detection_area': 5000})
    percent = r.owl.min_detection_area_percent
    assert percent / 100 * detection_frame_px(r.owl) == pytest.approx(5000, abs=1)
    assert r.owl.config.get('GreenOnBrown', 'min_detection_area_percent') == f'{percent:.4f}'
    assert not sprayed(r, card_size, SMALL), 'a px minimum from a legacy client was ignored'
    assert sprayed(r, card_size, BIG)


# A profile from before the percent key existed. Sections that can carry the
# minimum: [GreenOnBrown] and the active [Sensitivity_*] preset (applied last).
PRESETS = ('Sensitivity_Low', 'Sensitivity_Medium', 'Sensitivity_High')
NO_PERCENT_KEYS = [(section, 'min_detection_area_percent')
                   for section in ('GreenOnBrown',) + PRESETS]


def legacy_owl(owl_factory, config_factory, input_dir, px_in, px, width=416, height=320):
    """An Owl booted from a profile whose only minimum is `px`, in the
    [GreenOnBrown] section or in the active preset."""
    section = 'GreenOnBrown' if px_in == 'GreenOnBrown' else 'Sensitivity_Medium'
    config = config_factory(
        overrides=[(section, 'min_detection_area', str(px)),
                   ('Camera', 'resolution_width', str(width)),
                   ('Camera', 'resolution_height', str(height)),
                   ('Controller', 'controller_type', 'none'),
                   ('DataCollection', 'detection_enable', 'True')],
        drop=NO_PERCENT_KEYS)
    return owl_factory(config, input_file_or_directory=str(input_dir))


@pytest.mark.parametrize('px_in', ['GreenOnBrown', 'active preset'])
def test_a_legacy_px_minimum_in_the_profile_is_honoured_from_boot(
        owl_factory, config_factory, input_dir, card_size, px_in):
    instance = legacy_owl(owl_factory, config_factory, input_dir, px_in, 5000)
    r = harness.Rig(instance, harness.CardCamera())
    try:
        r.wait_for(lambda: instance.relay_controller.mode == 'spot', 'spot spraying')

        # 5000 px^2 of the camera frame; the crop margins take up to 8 % off
        minimum_px = instance.min_detection_area_percent / 100 * detection_frame_px(instance)
        assert 4600 <= minimum_px <= 5000
        if px_in == 'GreenOnBrown':
            assert float(instance.config.get('GreenOnBrown', 'min_detection_area_percent')) == \
                pytest.approx(instance.min_detection_area_percent, abs=1e-4), \
                'the migrated value must be what the next save writes'
        assert not sprayed(r, card_size, SMALL)
        assert sprayed(r, card_size, BIG)
    finally:
        r.stop()


def test_a_legacy_preset_minimum_means_the_same_px_at_any_resolution(
        owl_factory, config_factory, input_dir):
    instance = legacy_owl(owl_factory, config_factory, input_dir, 'active preset', 2000,
                          width=832, height=640)
    minimum_px = instance.min_detection_area_percent / 100 * detection_frame_px(instance)
    assert 1800 <= minimum_px <= 2000


def test_a_legacy_minimum_survives_the_pi4_resolution_clamp(
        owl_factory, config_factory, input_dir, monkeypatch):
    monkeypatch.setattr(owl_module, 'get_rpi_version', lambda: 'rpi-4')
    instance = legacy_owl(owl_factory, config_factory, input_dir, 'GreenOnBrown', 2000,
                          width=1456, height=1088)
    assert tuple(instance.resolution) == (640, 480)
    minimum_px = instance.min_detection_area_percent / 100 * detection_frame_px(instance)
    assert 1800 <= minimum_px <= 2000


def test_the_migrated_minimum_stays_inside_the_range_the_validator_accepts(
        owl_factory, config_factory, input_dir):
    """0.0005-5 %: a profile with an absurd px value must still produce a
    config that loads next boot."""
    from utils.config_manager import ConfigValidator
    low, high = ConfigValidator.VALUE_VALIDATORS['min_detection_area_percent'][1:]
    for px, expected in ((1000000, 5.0), (0, 0.0005)):
        instance = legacy_owl(owl_factory, config_factory, input_dir, 'GreenOnBrown', px)
        assert instance.min_detection_area_percent == expected
        assert float(instance.config.get('GreenOnBrown', 'min_detection_area_percent')) == expected
        assert low <= expected <= high


# --------------------------------------------------------------------------
# Resolution clamp
# --------------------------------------------------------------------------

DASH = harness.DASH['standalone']


@pytest.mark.parametrize('board, requested, allow, runs_at', [
    ('rpi-4', (1456, 1088), False, (640, 480)),      # the shipped default on a Pi 4
    ('rpi-3', (1280, 720), False, (640, 480)),
    ('rpi-4', (832, 640), False, (832, 640)),        # exactly the limit is allowed
    ('rpi-4', (832, 656), False, (640, 480)),        # just over it
    ('rpi-4', (1456, 1088), True, (1456, 1088)),     # the operator opted in
    ('rpi-5', (1456, 1088), False, (1456, 1088)),    # a Pi 5 is never clamped
    ('non-rpi', (1456, 1088), False, (1456, 1088)),
])
def test_pi_3_and_4_run_high_resolutions_at_640x480_unless_opted_in(
        owl_factory, config_factory, input_dir, monkeypatch, board, requested, allow, runs_at):
    monkeypatch.setattr(owl_module, 'get_rpi_version', lambda: board)
    config = config_factory(overrides=[
        ('Camera', 'resolution_width', str(requested[0])),
        ('Camera', 'resolution_height', str(requested[1])),
        ('Camera', 'allow_high_resolution', str(allow)), *DASH])

    instance = owl_factory(config, input_file_or_directory=str(input_dir))

    clamped = runs_at != requested
    assert tuple(instance.resolution) == runs_at
    assert tuple(instance.requested_resolution) == requested
    assert instance.resolution_clamped is clamped

    # What the dashboard needs to explain it to the operator
    instance._update_state_once()
    state = instance.dash.state
    assert (state['resolution_width'], state['resolution_height']) == runs_at
    assert (state['requested_resolution_width'], state['requested_resolution_height']) == requested
    assert state['resolution_clamped'] is clamped
    assert state['allow_high_resolution'] is allow
    assert state['rpi_version'] == board
