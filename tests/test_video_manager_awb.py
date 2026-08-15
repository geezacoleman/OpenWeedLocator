"""White-balance control translation in utils/video_manager.py (R3).

Windows-runnable: libcamera is monkeypatched with a stub, so
build_awb_controls and the PiCamera2Stream live-update merge are tested
without any camera stack.
"""

from unittest.mock import MagicMock

import pytest

import utils.video_manager as vm
from utils.video_manager import build_awb_controls, PiCamera2Stream, VideoStream


class _FakeEnum:
    Auto = 'enum-auto'
    Daylight = 'enum-daylight'
    Cloudy = 'enum-cloudy'
    Tungsten = 'enum-tungsten'
    Fluorescent = 'enum-fluorescent'
    Indoor = 'enum-indoor'


@pytest.fixture
def fake_libcamera(monkeypatch):
    lib = MagicMock()
    lib.controls.AwbModeEnum = _FakeEnum
    monkeypatch.setattr(vm, 'libcamera', lib)
    return lib


@pytest.mark.unit
class TestBuildAwbControls:
    @pytest.mark.parametrize('mode,enum_value', [
        ('auto', _FakeEnum.Auto),
        ('daylight', _FakeEnum.Daylight),
        ('cloudy', _FakeEnum.Cloudy),
        ('tungsten', _FakeEnum.Tungsten),
        ('fluorescent', _FakeEnum.Fluorescent),
        ('indoor', _FakeEnum.Indoor),
    ])
    def test_presets_enable_awb_with_right_enum(self, fake_libcamera, mode, enum_value):
        controls = build_awb_controls(awb_mode=mode)
        assert controls['AwbEnable'] is True, (
            "AwbEnable must be sent explicitly for every preset so switching "
            "back FROM manual re-enables auto white balance."
        )
        assert controls['AwbMode'] == enum_value
        assert 'ColourGains' not in controls

    def test_preset_mode_is_case_and_whitespace_tolerant(self, fake_libcamera):
        controls = build_awb_controls(awb_mode=' Cloudy ')
        assert controls['AwbMode'] == _FakeEnum.Cloudy

    def test_manual_disables_awb_and_sets_gains(self, fake_libcamera):
        controls = build_awb_controls(awb_mode='manual',
                                      awb_red_gain='1.5', awb_blue_gain=3)
        assert controls['AwbEnable'] is False
        assert controls['ColourGains'] == (1.5, 3.0)
        assert 'AwbMode' not in controls

    def test_exposure_included_when_given(self, fake_libcamera):
        controls = build_awb_controls(awb_mode='daylight', exp_compensation=-2)
        assert controls['ExposureValue'] == -2

    def test_exposure_omitted_when_none(self, fake_libcamera):
        controls = build_awb_controls(awb_mode='daylight')
        assert 'ExposureValue' not in controls

    def test_unknown_mode_falls_back_to_daylight(self, fake_libcamera):
        controls = build_awb_controls(awb_mode='banana')
        assert controls['AwbEnable'] is True
        assert controls['AwbMode'] == _FakeEnum.Daylight

    def test_returns_empty_without_libcamera(self, monkeypatch):
        monkeypatch.setattr(vm, 'libcamera', None)
        assert build_awb_controls(awb_mode='manual') == {}


@pytest.mark.unit
class TestSetCameraControlsMerge:
    """Partial live updates merge into the stream-held state so a red-gain-
    only change keeps the current mode."""

    def _stream(self):
        stream = PiCamera2Stream.__new__(PiCamera2Stream)
        stream.logger = MagicMock()
        stream.camera = MagicMock()
        stream._awb_state = {'awb_mode': 'daylight', 'awb_red_gain': 2.0,
                             'awb_blue_gain': 2.0, 'exp_compensation': -2}
        return stream

    def test_partial_update_merges(self, fake_libcamera):
        stream = self._stream()
        stream._awb_state['awb_mode'] = 'manual'
        assert stream.set_camera_controls({'awb_red_gain': 4.0}) is True
        controls = stream.camera.set_controls.call_args[0][0]
        assert controls['AwbEnable'] is False
        assert controls['ColourGains'] == (4.0, 2.0)
        assert stream._awb_state['awb_red_gain'] == 4.0

    def test_mode_switch_back_to_preset_reenables_awb(self, fake_libcamera):
        stream = self._stream()
        stream._awb_state['awb_mode'] = 'manual'
        stream.set_camera_controls({'awb_mode': 'auto'})
        controls = stream.camera.set_controls.call_args[0][0]
        assert controls['AwbEnable'] is True
        assert controls['AwbMode'] == _FakeEnum.Auto

    def test_exp_compensation_live(self, fake_libcamera):
        stream = self._stream()
        stream.set_camera_controls({'exp_compensation': 3})
        controls = stream.camera.set_controls.call_args[0][0]
        assert controls['ExposureValue'] == 3

    def test_returns_false_without_libcamera(self, monkeypatch):
        monkeypatch.setattr(vm, 'libcamera', None)
        stream = self._stream()
        assert stream.set_camera_controls({'awb_red_gain': 4.0}) is False
        stream.camera.set_controls.assert_not_called()


@pytest.mark.unit
class TestVideoStreamDelegation:
    def test_delegates_to_backend(self, fake_libcamera):
        vs = VideoStream.__new__(VideoStream)
        vs.logger = MagicMock()
        vs.stream = MagicMock()
        vs.stream.set_camera_controls.return_value = True
        assert vs.set_camera_controls({'awb_mode': 'auto'}) is True
        vs.stream.set_camera_controls.assert_called_once_with({'awb_mode': 'auto'})

    def test_returns_false_when_backend_lacks_method(self):
        vs = VideoStream.__new__(VideoStream)
        vs.logger = MagicMock()
        vs.stream = object()  # e.g. WebcamStream — no set_camera_controls
        assert vs.set_camera_controls({'awb_mode': 'auto'}) is False


@pytest.mark.unit
class TestAwbModeValidator:
    def test_accepts_all_seven_case_insensitive(self):
        import configparser
        from utils.config_manager import ConfigValidator
        for mode in ('auto', 'DAYLIGHT', 'Cloudy', 'tungsten', 'fluorescent',
                     'indoor', 'Manual'):
            config = configparser.ConfigParser()
            config.add_section('Camera')
            config.set('Camera', 'awb_mode', mode)
            ok, errors = ConfigValidator.validate_awb_mode(config)
            assert ok, f"'{mode}' should be valid: {errors}"

    def test_rejects_garbage(self):
        import configparser
        from utils.config_manager import ConfigValidator
        config = configparser.ConfigParser()
        config.add_section('Camera')
        config.set('Camera', 'awb_mode', 'sunset')
        ok, errors = ConfigValidator.validate_awb_mode(config)
        assert not ok
        assert 'awb_mode' in errors['Camera']

    def test_absent_key_skipped(self):
        import configparser
        from utils.config_manager import ConfigValidator
        config = configparser.ConfigParser()
        config.add_section('Camera')
        ok, errors = ConfigValidator.validate_awb_mode(config)
        assert ok and errors == {}

    def test_bad_mode_fails_full_config_load(self, tmp_path):
        from pathlib import Path
        from utils.config_manager import ConfigValidator
        source = Path(__file__).parent.parent / 'config' / 'GENERAL_CONFIG.ini'
        import configparser
        config = configparser.ConfigParser()
        config.read(source)
        config.set('Camera', 'awb_mode', 'sunset')
        bad = tmp_path / 'bad_awb.ini'
        with open(bad, 'w') as handle:
            config.write(handle)
        with pytest.raises(Exception) as excinfo:
            ConfigValidator.load_and_validate_config(bad)
        assert 'awb_mode' in str(excinfo.value)
