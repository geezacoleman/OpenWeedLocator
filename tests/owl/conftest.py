"""Fixtures for tests that build a real Owl (tests/owl/).

owl_factory fails a test that logs an ERROR from any logger while it runs.
The image-saving bug of v3.17-v3.21 was an error logged on every frame and
swallowed; this is the check that would have caught it. A test that expects
an error opts out with @pytest.mark.allow_errors.
"""

import configparser
import logging
import os
from pathlib import Path

import pytest

import harness
from harness import BASE_CONFIG


@pytest.fixture
def isolate_repo_configs(monkeypatch):
    """Ignore any CONTROLLER.ini/GEOMETRY.ini the developer has locally.

    owl.py reads both from config/ on top of the chosen config file, so a
    real unit's files would otherwise decide whether these tests see a
    dashboard.
    """
    real_read = configparser.RawConfigParser.read

    def read(self, filenames, encoding=None):
        if isinstance(filenames, (str, os.PathLike)):
            filenames = [filenames]
        keep = [f for f in filenames
                if Path(f).name not in ('CONTROLLER.ini', 'GEOMETRY.ini')]
        return real_read(self, keep, encoding)

    monkeypatch.setattr(configparser.RawConfigParser, 'read', read)


@pytest.fixture
def input_dir(tmp_path):
    """A directory of images, as passed to `python owl.py --input ...`."""
    import cv2
    from harness import make_frame

    media = tmp_path / 'media'
    media.mkdir()
    for i in range(2):
        cv2.imwrite(str(media / f'frame{i}.png'), make_frame())
    return media


@pytest.fixture
def config_factory(tmp_path):
    """Build a config file from GENERAL_CONFIG.ini with overrides applied."""

    def _factory(name='TEST_CONFIG.ini', overrides=None, drop=()):
        config = configparser.ConfigParser()
        config.read(BASE_CONFIG)

        # Keep tests fast and off the real recording paths
        config.set('Camera', 'resolution_width', '416')
        config.set('Camera', 'resolution_height', '320')
        config.set('DataCollection', 'storage_location', 'internal')
        config.set('DataCollection', 'internal_save_directory',
                   str(tmp_path / 'owl_images'))
        config.set('DataCollection', 'save_directory', str(tmp_path / 'owl_images'))
        config.set('DataCollection', 'min_free_gb', '1')

        for section, key, value in (overrides or []):
            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, value)

        for section, key in drop:
            config.remove_option(section, key)

        path = tmp_path / name
        with open(path, 'w') as f:
            config.write(f)
        return path

    return _factory


class _ErrorCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def owl_factory(isolate_repo_configs, monkeypatch, tmp_path, request):
    """Build real Owl instances and guarantee they are torn down.

    Owl.stop() ends in sys.exit(0), which is right for the application but
    would kill the test run, so SystemExit is swallowed here and in
    harness.run_hoot().
    """
    from owl import Owl

    built = []
    errors = _ErrorCollector()
    # Never touch the operator's first-boot flag path from a test
    monkeypatch.setenv('OWL_FIRSTBOOT_FLAG', str(tmp_path / 'no-firstboot.flag'))

    def _factory(config_path, **kwargs):
        instance = Owl(config_file=str(config_path), **kwargs)
        # After construction: LogManager.setup() inside Owl() replaces the
        # root logger's handlers, which would drop this one.
        root = logging.getLogger()
        if errors not in root.handlers:
            root.addHandler(errors)
        built.append(instance)
        return instance

    yield _factory

    logged = [f'{r.name}: {r.getMessage()}' for r in errors.records]
    logging.getLogger().removeHandler(errors)
    for instance in built:
        try:
            instance.stop()
        except SystemExit:
            pass
        except Exception:
            pass
    if logged and not request.node.get_closest_marker('allow_errors'):
        pytest.fail('ERROR logged while the OWL ran:\n  ' + '\n  '.join(logged))


@pytest.fixture
def rig(owl_factory, config_factory, input_dir, monkeypatch):
    """Build a running Owl whose relays are watched (harness.Rig).

    rig(controller='none'|'ute'|'advanced', dash='none'|'standalone'|'networked',
        gps=False, speed_kmh=0.0, fix=True, overrides=[(section, key, value)],
        spray=True, lag_s=0.0)

    gps=True gives the unit its own serial GPS receiver (harness.GpsReceiver)
    reading speed_kmh. spray=True turns spot spraying on the way that
    deployment does (config, switch or dashboard command).
    """
    import owl as owl_module

    monkeypatch.setattr(owl_module, 'Process', harness.ThreadProcess)
    rigs = []

    def _build(controller='none', dash='none', gps=False, speed_kmh=0.0, fix=True,
               overrides=(), spray=True, lag_s=0.0):
        receiver = harness.GpsReceiver(speed_kmh=speed_kmh, fix=fix) if gps else None
        if gps:
            from types import SimpleNamespace
            monkeypatch.setattr(owl_module, 'serial', SimpleNamespace(
                Serial=lambda **kwargs: receiver, SerialException=OSError))
        config = config_factory(overrides=[
            ('Controller', 'controller_type', controller),
            ('Controller', 'switch_purpose', 'detection'),
            # A bare unit sprays from its config; with a switch or a
            # dashboard, spray_on() below asks the way the operator does
            ('DataCollection', 'detection_enable', str(spray)),
            ('GPS', 'source', 'serial' if gps else 'none'),
            ('GPS', 'port', '/dev/fake-gps'),
            *harness.DASH[dash], *overrides])
        instance = owl_factory(config, input_file_or_directory=str(input_dir))
        built = harness.Rig(instance, harness.CardCamera(lag_s=lag_s), receiver)
        rigs.append(built)
        if gps:
            assert instance._gps_state is not None, 'the serial GPS reader did not start'
        if spray:
            built.spray_on() if (controller != 'none' or dash != 'none') else \
                built.wait_for(lambda: instance.relay_controller.mode == 'spot', 'spot spraying')
        return built

    yield _build
    for built in rigs:
        built.stop()
        if built.gps:
            built.gps.close()
