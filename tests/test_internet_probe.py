"""Unit tests for utils/internet_probe.py (default route + ping)."""

from unittest.mock import MagicMock, patch

import pytest

from utils import internet_probe


def _runner(route_stdout='default via 192.168.43.1 dev wlan0\n',
            route_rc=0, ping_rc=0):
    def run(argv, **kwargs):
        if argv[1:2] == ['route']:
            return MagicMock(returncode=route_rc, stdout=route_stdout)
        return MagicMock(returncode=ping_rc, stdout='')
    return MagicMock(side_effect=run)


@pytest.mark.unit
class TestProbeInternet:
    @pytest.fixture(autouse=True)
    def binaries_exist(self):
        with patch('utils.internet_probe._first_existing',
                   side_effect=lambda candidates: candidates[0]):
            yield

    def test_reachable_when_route_and_ping(self):
        result = internet_probe.probe_internet(runner=_runner())
        assert result['reachable'] is True
        assert result['has_route'] is True
        assert result['can_ping'] is True
        assert result['default_dev'] == 'wlan0'
        assert result['error'] is None

    def test_no_default_route_skips_ping(self):
        runner = _runner(route_stdout='10.42.0.0/24 dev wlan0 scope link\n')
        result = internet_probe.probe_internet(runner=runner)
        assert result['reachable'] is False
        assert result['has_route'] is False
        assert runner.call_count == 1   # no ping attempted
        assert result['error'] == 'No default route'

    def test_route_without_ping(self):
        result = internet_probe.probe_internet(runner=_runner(ping_rc=1))
        assert result['has_route'] is True
        assert result['can_ping'] is False
        assert result['reachable'] is False
        assert result['error'] == 'No internet connectivity'

    def test_timeout_is_reported_not_raised(self):
        runner = MagicMock(side_effect=internet_probe.subprocess.TimeoutExpired('ip', 2))
        result = internet_probe.probe_internet(runner=runner)
        assert result['reachable'] is False
        assert result['error'] == 'Network check timed out'

    def test_ping_bounded_by_short_wait(self):
        runner = _runner()
        internet_probe.probe_internet(runner=runner)
        ping_call = runner.call_args_list[1]
        assert ping_call[0][0][1:5] == ['-c', '1', '-W', '2']
        assert ping_call[1]['timeout'] == internet_probe.PING_TIMEOUT_S
