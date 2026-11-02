"""Save logs support bundle (utils/support_bundle.py).

The bundle is emailed by a farmer, so what must never be in it matters more
than what is: passwords, tokens, keys and GPS positions. The corpus below is
made of the real shapes these appear in in OWL logs and the journal.
"""

import io
import json
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from utils import support_bundle as sb


SECRET_LINES = [
    # sudo logs every nmcli command line (owl-dash.service journal)
    ('sudo[812]: owl : PWD=/ ; USER=root ; COMMAND=/usr/bin/nmcli con modify OWL-9390 '
     'wifi-sec.psk hunter2 more', 'hunter2'),
    ('COMMAND=/usr/bin/nmcli con add type wifi 802-11-wireless-security.psk pass word', 'pass word'),
    ('nmcli failed (dev wifi connect FarmNet password my secret ifname wlan0)', 'my secret'),
    ('802-11-wireless-security.psk:Sup3rS3cret', 'Sup3rS3cret'),
    ('X-Device-Token: abcDEF123xyz', 'abcDEF123xyz'),
    ("{'device_token': 'tok123', 'crop': 'wheat'}", 'tok123'),
    ('{"hotspot_password": "farmpass1"}', 'farmpass1'),
    ('remote_password = s3cr3tValue', 's3cr3tValue'),
    ('api_key=sk-ant-abc123', 'sk-ant-abc123'),
    ('Authorization: Bearer abc.def-ghi', 'abc.def-ghi'),
    ('jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl', 'eyJhbGciOiJIUzI1NiJ9'),
    ('From https://geeza:ghp_SECRET@github.com/x/owl', 'ghp_SECRET'),
    ('PUT https://store.example/obj?X-Amz-Signature=deadbeef&X-Amz-Date=1', 'deadbeef'),
    ('-----BEGIN PRIVATE KEY-----\nMIIEvQ\nabc\n-----END PRIVATE KEY-----', 'MIIEvQ'),
]

POSITION_LINES = [
    ("Ignoring GPS update: {'latitude': -33.786234, 'longitude': 151.02613}", '-33.786234'),
    ('{"lat": -31.5138, "lon": 150.2521, "accuracy": 4.2}', '-31.5138'),
    ('gps_best_lat=-31.51 gps_best_lon=150.25', '150.25'),
    ('$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47', '4807.038'),
    ('$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A', '01131.000'),
    ('#XGPS: -33.786234,151.026130,45.3,4.2,2.7778,210.0,"2026-09-13 06:25:48"', '151.026130'),
    ('position -33.786234, 151.026130 saved', '151.026130'),
]

BENIGN = [
    'cpu 45.3 temp 61.2',
    'OWL v3.25.6 on hardening-3.22',
    'speed 12.5 km/h accuracy 4.2 heading 210.0',
    'latency_ms=33 loop 41.2 ms',
    'Invalid or missing device token',
    'max_tokens=5',
    'gps_fix True source phone',
]


@pytest.mark.unit
class TestRedact:

    @pytest.mark.parametrize('line,secret', SECRET_LINES)
    def test_secrets_removed(self, line, secret):
        out = sb.redact(line)
        assert secret not in out
        assert '[removed]' in out or '[removed' in out

    @pytest.mark.parametrize('line,secret', POSITION_LINES)
    def test_positions_removed(self, line, secret):
        assert secret not in sb.redact(line)

    @pytest.mark.parametrize('line', BENIGN)
    def test_benign_text_untouched(self, line):
        assert sb.redact(line) == line

    def test_no_double_bracket_after_nmcli_colon_form(self):
        assert sb.redact('x.psk:abc') .endswith('[removed]')
        assert '[removed]]' not in sb.redact('802-11-wireless-security.psk:abc')

    def test_data_walk(self):
        data = {'system': {'cpu_temp': 61.2, 'latency_ms': 33},
                'owl_state': {'gps_fix': True, 'gps_latitude': -33.7, 'gps_best_lon': 151.0,
                              'gps_payload': {'latitude': -33.7, 'speed_kmh': 12.0},
                              'device_token': 'abc', 'note': 'wifi-sec.psk secret1',
                              'gps_longitude': None}}
        out = sb.redact_data(data)
        text = json.dumps(out)
        for value in ('-33.7', '151.0', 'abc', 'secret1'):
            assert value not in text
        assert out['system'] == {'cpu_temp': 61.2, 'latency_ms': 33}
        assert out['owl_state']['gps_payload']['speed_kmh'] == 12.0
        assert out['owl_state']['gps_longitude'] is None   # absent stays absent


@pytest.mark.unit
class TestControllerIni:

    def test_allowlisted_sections_only(self, tmp_path):
        ini = tmp_path / 'CONTROLLER.ini'
        ini.write_text(
            '[MQTT]\nbroker_ip = localhost\n\n'
            '[Security]\ndevice_token = abc123\n\n'
            '[Farm]\nfarm_name = Smith Farm\noperator = Jo Smith\n\n'
            '[Cloud]\nenable = True\nbroker_host = mqtt.noktura.tech\n'
            'username = owl-1\npassword_file = /home/owl/.owl/cloud/credentials\n\n'
            '[Support]\nemail = dealer@example.com\n\n'
            '[NewSecretThing]\nkey = x\n')
        text = sb.controller_ini_text(str(ini))
        assert 'broker_ip = localhost' in text
        assert 'broker_host = mqtt.noktura.tech' in text
        assert 'dealer@example.com' in text
        for gone in ('abc123', 'Smith', 'password_file', 'owl-1', 'NewSecretThing'):
            assert gone not in text

    def test_support_email(self, tmp_path):
        ini = tmp_path / 'CONTROLLER.ini'
        assert sb.support_email(str(ini)) == 'support@noktura.tech'
        ini.write_text('[Support]\nemail = dealer@example.com\n')
        assert sb.support_email(str(ini)) == 'dealer@example.com'
        ini.write_text('[Support]\nemail = not an address\n')
        assert sb.support_email(str(ini)) == 'support@noktura.tech'


@pytest.mark.unit
class TestPieces:

    def test_tail_starts_on_a_full_line(self, tmp_path):
        log = tmp_path / 'owl.jsonl'
        log.write_bytes(''.join(f'line {i}\n' for i in range(1000)).encode())
        text = sb.tail_text(str(log), 100)
        assert text.startswith('line ') and text.endswith('line 999\n')
        assert len(text) <= 100

    def test_app_report_is_capped_and_strings_only(self):
        report = {'app_version': '0.4.2', 'device': 'Android 14; SM-G991B',
                  'events': [{'time': 't', 'kind': 'error', 'text': 'x' * 900}] * 400
                  + [5, {'text': {'nested': 1}}, {'kind': 'request', 'text': 'token=abc'}]}
        text = sb.app_report_text(report)
        assert 'App version: 0.4.2' in text
        assert 'x' * 501 not in text
        assert 'abc' not in text
        assert text.count('\n') < 320
        assert sb.app_report_text('nope') is None

    def test_owl_log_files_picks_recent_dashboard_logs(self, tmp_path):
        for day in ('20261001', '20261002', '20261003', '20261004'):
            (tmp_path / f'owl_dashboard_{day}.log').write_text('x')
        (tmp_path / 'owl.jsonl').write_text('x')
        (tmp_path / 'detections.jsonl').write_text('x')
        names = [arc for arc, _, _ in sb.owl_log_files(tmp_path)]
        assert names[0] == 'logs/owl.jsonl'
        assert 'logs/owl_dashboard_20261001.log' not in names
        assert 'logs/owl_dashboard_20261004.log' in names
        assert not any('detections' in n for n in names)


def _fake_run(outputs, slow=()):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] in slow:
            raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
        key = ' '.join(argv[:2])
        return SimpleNamespace(stdout=outputs.get(key, ''), stderr='')
    run.calls = calls
    return run


def _members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name).decode() for name in zf.namelist()}


@pytest.mark.unit
class TestBuildBundle:

    def test_contents_and_redaction(self, tmp_path):
        log = tmp_path / 'owl.jsonl'
        log.write_text('{"lat": -33.78, "msg": "ok"}\nwifi-sec.psk topsecret\n')
        run = _fake_run({'journalctl --list-boots': ' -1 abc\n  0 def\n',
                         'journalctl -b': 'owl[1]: Session metadata saved: {\'latitude\': -33.7}\n',
                         'journalctl -k': 'usb 1-1: USB disconnect, device number 3\n',
                         'systemctl is-active': 'active\n'})
        filename, data = sb.build_bundle(
            label='OWL-2026-000001', units=('owl', 'owl-dash'), version_info={'version': '3.25.6'},
            stats={'gps_latitude': -33.7, 'cpu_temp': 55}, log_files=[('logs/owl.jsonl', str(log), 10**6)],
            app_report={'app_version': '0.4.2', 'events': []}, run=run, journal_access=True)
        assert filename.startswith('owl_logs_OWL-2026-000001_') and filename.endswith('.zip')
        files = _members(data)
        assert set(files) >= {'summary.txt', 'stats.json', 'logs/owl.jsonl', 'app.txt',
                              'journal/services-boot0.txt', 'journal/services-boot1.txt',
                              'journal/kernel-boot0.txt', 'journal/kernel-boot1.txt'}
        everything = '\n'.join(files.values())
        assert '-33.7' not in everything and 'topsecret' not in everything
        assert 'USB disconnect' in files['journal/kernel-boot0.txt']
        assert 'owl: active' in files['summary.txt']
        # one merged, time-ordered journal call per boot for all units
        merged = [c for c in run.calls if c[:2] == ['journalctl', '-b']]
        assert len(merged) == 2 and merged[0].count('-u') == 2

    def test_single_boot_and_unreadable_journal_are_noted(self):
        run = _fake_run({'journalctl --list-boots': '  0 def\n'})
        _, data = sb.build_bundle(label='x', units=('owl',), run=run, journal_access=True)
        files = _members(data)
        assert 'journal/services-boot1.txt' not in files
        assert 'no previous boot' in files['summary.txt']

        _, data = sb.build_bundle(label='x', units=('owl',), run=_fake_run({}), journal_access=False)
        files = _members(data)
        assert not any(name.startswith('journal/') for name in files)
        assert 'journal not readable' in files['summary.txt']

    def test_deadline_skips_late_commands(self):
        now = [0.0]

        def clock():
            return now[0]

        def run(argv, **kwargs):
            now[0] += 6   # every command eats its whole timeout
            return SimpleNamespace(stdout='', stderr='')

        _, data = sb.build_bundle(label='x', units=('owl', 'owl-dash'), run=run, clock=clock,
                                  deadline_s=20, journal_access=True)
        summary = _members(data)['summary.txt']
        assert 'skipped (time limit)' in summary

    def test_hung_command_is_a_note_not_a_crash(self):
        _, data = sb.build_bundle(label='x', units=('owl',), journal_access=False,
                                  run=_fake_run({}, slow=('systemctl', 'vcgencmd')))
        assert 'TimeoutExpired' in _members(data)['summary.txt']


@pytest.mark.unit
class TestBundleStore:

    def test_ticket_reusable_until_expiry(self):
        now = [0.0]
        store = sb.BundleStore(clock=lambda: now[0])
        ticket = store.put('a.zip', b'1')
        assert store.get(ticket) == ('a.zip', b'1')
        assert store.get(ticket) == ('a.zip', b'1')   # a dropped download may retry
        now[0] = sb.TICKET_TTL_S + 1
        assert store.get(ticket) is None

    def test_oldest_evicted(self):
        store = sb.BundleStore()
        tickets = [store.put(f'{i}.zip', b'x') for i in range(sb.TICKETS_KEPT + 1)]
        assert store.get(tickets[0]) is None
        assert store.get(tickets[-1]) is not None


@pytest.mark.unit
class TestNmcliArgsHidden:
    """Wi-Fi passwords used to reach the dashboard log on any nmcli failure."""

    def test_log_and_error_hide_the_password(self, caplog):
        from utils.network_manager import NetworkManager, NetworkManagerError

        def runner(argv, timeout=30):
            return SimpleNamespace(returncode=10, stdout='', stderr='Error: failed')

        nm = NetworkManager(runner=runner)
        with pytest.raises(NetworkManagerError):
            nm._run(['con', 'modify', 'OWL-1', 'wifi-sec.psk', 'hunter22'])
        with pytest.raises(NetworkManagerError) as err:
            nm._run(['dev', 'wifi', 'connect', 'Farm', 'password', 'hunter33'])
        assert 'hunter22' not in caplog.text and 'hunter33' not in caplog.text
        assert 'hunter33' not in str(err.value)

    def test_timeout_message_hides_the_password(self):
        from utils.network_manager import NetworkManager, NetworkManagerError

        def runner(argv, timeout=30):
            raise subprocess.TimeoutExpired(argv, timeout)

        with pytest.raises(NetworkManagerError) as err:
            NetworkManager(runner=runner)._run(['con', 'modify', 'x', 'wifi-sec.psk', 'hunter44'])
        assert 'hunter44' not in str(err.value) and '[hidden]' in str(err.value)
