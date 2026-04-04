"""
Integration test: GPS speed -> dynamic actuation timing.

Spoofs NMEA sentences at various speeds and verifies the full pipeline:
  Teltonika NMEA (TCP) -> GPSManager -> SpeedAverager -> ActuationCalculator
    -> MQTT broadcast -> OWL _handle_set_actuation_params -> owl.actuation_duration

Tests the full range of spray distances (1-50cm) and field speeds (1-25 km/h),
including boundary analysis where MIN_DURATION (0.01s) and MAX_DURATION (5.0s)
clamps kick in.
"""

import socket
import time
import pytest

from utils.gps_manager import GPSManager, GPSState, parse_sentence, KNOTS_TO_KMH
from controller.networked.networked import SpeedAverager, ActuationCalculator


# ---------------------------------------------------------------------------
# Helpers -- build valid NMEA sentences with correct checksums
# ---------------------------------------------------------------------------

def _nmea_checksum(body):
    """Compute XOR checksum for an NMEA sentence body (between $ and *)."""
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f'{cs:02X}'


def make_gprmc(speed_knots, lat='3351.8900', lat_h='S', lon='15110.5300', lon_h='E',
               status='A', heading='045.0', time_utc='120000.00', date='200226'):
    """Build a valid $GPRMC sentence with correct checksum."""
    body = f'GPRMC,{time_utc},{status},{lat},{lat_h},{lon},{lon_h},{speed_knots:.1f},{heading},{date},,A'
    return f'${body}*{_nmea_checksum(body)}\r\n'


def make_gpvtg(speed_kmh, heading='045.0'):
    """Build a valid $GPVTG sentence with correct checksum."""
    speed_knots = speed_kmh / KNOTS_TO_KMH
    body = f'GPVTG,{heading},T,,M,{speed_knots:.1f},N,{speed_kmh:.1f},K,A'
    return f'${body}*{_nmea_checksum(body)}\r\n'


def make_gpgga(lat='3351.8900', lat_h='S', lon='15110.5300', lon_h='E',
               fix_quality=1, satellites=8, hdop=1.2, altitude=120.0):
    """Build a valid $GPGGA sentence with correct checksum."""
    body = (f'GPGGA,120000.00,{lat},{lat_h},{lon},{lon_h},'
            f'{fix_quality},{satellites:02d},{hdop:.1f},{altitude:.1f},M,0.0,M,,')
    return f'${body}*{_nmea_checksum(body)}\r\n'


def kmh_to_knots(speed_kmh):
    return speed_kmh / KNOTS_TO_KMH


# ---------------------------------------------------------------------------
# Test 1: NMEA parsing produces correct speed values
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNMEASpeedParsing:
    """Verify that spoofed NMEA sentences parse to correct speed values."""

    @pytest.mark.parametrize('speed_kmh', [1.0, 2.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0])
    def test_rmc_speed_roundtrip(self, speed_kmh):
        """RMC sentence -> parsed speed matches input."""
        knots = kmh_to_knots(speed_kmh)
        sentence = make_gprmc(knots)
        parsed = parse_sentence(sentence)
        assert parsed is not None, f'Failed to parse: {sentence!r}'
        assert parsed['type'] == 'RMC'
        assert parsed['speed_knots'] == pytest.approx(knots, abs=0.1)
        reconstructed_kmh = parsed['speed_knots'] * KNOTS_TO_KMH
        assert reconstructed_kmh == pytest.approx(speed_kmh, abs=0.2)

    @pytest.mark.parametrize('speed_kmh', [1.0, 2.0, 10.0, 25.0])
    def test_vtg_speed_roundtrip(self, speed_kmh):
        """VTG sentence -> parsed speed matches input."""
        sentence = make_gpvtg(speed_kmh)
        parsed = parse_sentence(sentence)
        assert parsed is not None
        assert parsed['type'] == 'VTG'
        assert parsed['speed_kmh'] == pytest.approx(speed_kmh, abs=0.1)

    def test_gga_provides_fix(self):
        """GGA sentence -> fix_quality > 0 means valid fix."""
        sentence = make_gpgga()
        parsed = parse_sentence(sentence)
        assert parsed is not None
        assert parsed['type'] == 'GGA'
        assert parsed['fix_quality'] == 1
        assert parsed['lat'] is not None
        assert parsed['lon'] is not None


# ---------------------------------------------------------------------------
# Test 2: GPSState correctly accumulates speed from NMEA
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGPSStateSpeedUpdate:
    """Verify GPSState.speed_kmh updates from both RMC and VTG sentences."""

    def test_rmc_updates_speed(self):
        state = GPSState()
        rmc = parse_sentence(make_gprmc(kmh_to_knots(12.0)))
        assert rmc is not None, 'RMC parse failed'
        state.update_from_rmc(rmc)
        snap = state.get_dict()
        assert snap['speed_kmh'] == pytest.approx(12.0, abs=0.3)

    def test_vtg_updates_speed(self):
        state = GPSState()
        vtg = parse_sentence(make_gpvtg(18.5))
        state.update_from_vtg(vtg)
        snap = state.get_dict()
        assert snap['speed_kmh'] == pytest.approx(18.5, abs=0.1)

    def test_gga_then_rmc_gives_valid_fix(self):
        state = GPSState()
        gga = parse_sentence(make_gpgga())
        assert gga is not None, 'GGA parse failed'
        state.update_from_gga(gga)
        rmc = parse_sentence(make_gprmc(kmh_to_knots(10.0), status='A'))
        assert rmc is not None, 'RMC parse failed'
        state.update_from_rmc(rmc)
        snap = state.get_dict()
        assert snap['fix_valid'] is True
        assert snap['speed_kmh'] == pytest.approx(10.0, abs=0.3)

    @pytest.mark.parametrize('speed_kmh', [1.0, 5.0, 15.0])
    def test_speed_reaches_gps_state(self, speed_kmh):
        """Full chain: make NMEA -> parse -> GPSState -> get_dict speed."""
        state = GPSState()
        gga = parse_sentence(make_gpgga())
        state.update_from_gga(gga)
        rmc = parse_sentence(make_gprmc(kmh_to_knots(speed_kmh)))
        state.update_from_rmc(rmc)
        snap = state.get_dict()
        assert snap['fix_valid'] is True
        assert snap['speed_kmh'] == pytest.approx(speed_kmh, abs=0.5)


# ---------------------------------------------------------------------------
# Test 3: ActuationCalculator -- exhaustive speed/distance matrix
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestActuationMath:
    """Verify actuation duration & delay across the full range of spray
    distances (1-50cm) and field speeds (1-25 km/h), including where
    safety clamps activate."""

    SPEEDS = [1.0, 2.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0]  # km/h
    DISTANCES = [1, 2, 3, 5, 10, 15, 20, 30, 50]  # cm (API range: 2-50)

    def _expected_duration(self, length_cm, speed_kmh):
        speed_m_s = speed_kmh / 3.6
        return (length_cm / 100.0) / speed_m_s

    def _expected_delay(self, offset_cm, speed_kmh):
        speed_m_s = speed_kmh / 3.6
        return (offset_cm / 100.0) / speed_m_s

    @pytest.mark.parametrize('speed_kmh', SPEEDS)
    def test_1cm_spray_distance(self, speed_kmh):
        """1cm spray distance: verify duration changes with speed."""
        calc = ActuationCalculator(actuation_length_cm=1.0, offset_cm=0.0)
        result = calc.compute(speed_kmh)

        expected = self._expected_duration(1.0, speed_kmh)
        expected = max(0.01, min(5.0, expected))

        assert result['source'] == 'gps'
        assert result['actuation_duration'] == pytest.approx(expected, abs=0.0001)

    @pytest.mark.parametrize('speed_kmh', SPEEDS)
    def test_2cm_spray_distance(self, speed_kmh):
        """2cm spray distance: verify duration changes with speed."""
        calc = ActuationCalculator(actuation_length_cm=2.0, offset_cm=0.0)
        result = calc.compute(speed_kmh)

        expected = self._expected_duration(2.0, speed_kmh)
        expected = max(0.01, min(5.0, expected))

        assert result['source'] == 'gps'
        assert result['actuation_duration'] == pytest.approx(expected, abs=0.0001)

    @pytest.mark.parametrize('distance_cm', DISTANCES)
    @pytest.mark.parametrize('speed_kmh', SPEEDS)
    def test_full_distance_speed_matrix(self, distance_cm, speed_kmh):
        """Every distance x speed combination produces correct clamped result."""
        calc = ActuationCalculator(actuation_length_cm=distance_cm, offset_cm=0.0)
        result = calc.compute(speed_kmh)

        raw = self._expected_duration(distance_cm, speed_kmh)
        expected = max(0.01, min(5.0, raw))

        assert result['source'] == 'gps'
        assert result['actuation_duration'] == pytest.approx(expected, abs=0.0001)

    @pytest.mark.parametrize('speed_kmh', SPEEDS)
    def test_with_30cm_offset(self, speed_kmh):
        """10cm spray + 30cm nozzle offset: verify both duration and delay."""
        calc = ActuationCalculator(actuation_length_cm=10.0, offset_cm=30.0)
        result = calc.compute(speed_kmh)

        exp_dur = max(0.01, min(5.0, self._expected_duration(10.0, speed_kmh)))
        exp_del = max(0.0, min(5.0, self._expected_delay(30.0, speed_kmh)))

        assert result['actuation_duration'] == pytest.approx(exp_dur, abs=0.0001)
        assert result['delay'] == pytest.approx(exp_del, abs=0.0001)

    def test_duration_decreases_with_speed(self):
        """Core property: faster speed -> shorter duration.

        Uses 50cm distance so no speed in range hits the 10ms clamp.
        Max safe speed for 50cm = 50*3.6 = 180 km/h.
        """
        calc = ActuationCalculator(actuation_length_cm=50.0, offset_cm=0.0)
        durations = [calc.compute(s)['actuation_duration'] for s in self.SPEEDS]
        for i in range(1, len(durations)):
            assert durations[i] < durations[i - 1], (
                f"Duration should decrease: {self.SPEEDS[i-1]}km/h={durations[i-1]:.4f}s "
                f"> {self.SPEEDS[i]}km/h={durations[i]:.4f}s"
            )

    def test_delay_decreases_with_speed(self):
        """Core property: faster speed -> shorter delay (fixed offset)."""
        calc = ActuationCalculator(actuation_length_cm=50.0, offset_cm=50.0)
        delays = [calc.compute(s)['delay'] for s in self.SPEEDS]
        for i in range(1, len(delays)):
            assert delays[i] < delays[i - 1]

    def test_double_distance_doubles_duration(self):
        """2x distance = 2x duration at same speed (when not clamped)."""
        for speed in [1.0, 2.0, 3.0]:
            # Use distances and speeds where neither is clamped
            calc_5 = ActuationCalculator(actuation_length_cm=5.0, offset_cm=0.0)
            calc_10 = ActuationCalculator(actuation_length_cm=10.0, offset_cm=0.0)
            d5 = calc_5.compute(speed)['actuation_duration']
            d10 = calc_10.compute(speed)['actuation_duration']
            assert d10 == pytest.approx(d5 * 2, abs=0.0001), (
                f'At {speed} km/h: 10cm={d10:.4f}s should be 2x 5cm={d5:.4f}s'
            )


# ---------------------------------------------------------------------------
# Test 3b: Boundary analysis -- where do clamps activate?
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestActuationBoundaries:
    """Find exactly where the MIN_DURATION (0.01s / 10ms) and MAX_DURATION
    (5.0s) safety clamps kick in for each spray distance."""

    ALL_DISTANCES = [1, 2, 3, 5, 10, 15, 20, 30, 40, 50]  # cm
    ALL_SPEEDS = [0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 12.0,
                  15.0, 20.0, 25.0, 30.0]  # km/h

    def test_min_duration_clamp_locations(self):
        """Identify speed/distance combos where 10ms floor is hit.

        These are the cases where the relay literally can't fire fast
        enough -- the solenoid needs at least 10ms to physically open.
        Any combo that hits this clamp means you're driving too fast
        for that spray distance.
        """
        clamped = []

        for dist in self.ALL_DISTANCES:
            calc = ActuationCalculator(actuation_length_cm=dist, offset_cm=0.0)
            for speed in self.ALL_SPEEDS:
                if speed < 0.5:
                    continue
                result = calc.compute(speed)
                raw = (dist / 100.0) / (speed / 3.6)
                if raw < 0.01:  # Would be clamped
                    clamped.append((dist, speed, raw * 1000, result['actuation_duration'] * 1000))

        print('\n  === MIN_DURATION (10ms) clamp analysis ===')
        print('  These combos hit the 10ms floor -- spray too small for the speed.')
        print(f'  {"Dist (cm)":>10}  {"Speed (km/h)":>13}  {"Raw (ms)":>10}  {"Clamped (ms)":>13}')
        print(f'  {"-"*10}  {"-"*13}  {"-"*10}  {"-"*13}')
        for dist, speed, raw_ms, clamped_ms in clamped:
            print(f'  {dist:10d}  {speed:13.1f}  {raw_ms:10.2f}  {clamped_ms:13.1f}')

        if not clamped:
            print('  (none -- all combos within safe range)')

        # Verify all clamped values are exactly 10ms
        for dist, speed, raw_ms, clamped_ms in clamped:
            assert clamped_ms == pytest.approx(10.0, abs=0.01)

    def test_max_duration_clamp_locations(self):
        """Identify speed/distance combos where 5s ceiling is hit.

        These are extremely slow speeds where relay would stay open
        too long, wasting spray and potentially causing damage.
        """
        clamped = []

        for dist in self.ALL_DISTANCES:
            calc = ActuationCalculator(actuation_length_cm=dist, offset_cm=0.0)
            for speed in self.ALL_SPEEDS:
                if speed < 0.5:
                    continue
                result = calc.compute(speed)
                raw = (dist / 100.0) / (speed / 3.6)
                if raw > 5.0:  # Would be clamped
                    clamped.append((dist, speed, raw * 1000, result['actuation_duration'] * 1000))

        print('\n  === MAX_DURATION (5000ms) clamp analysis ===')
        print('  These combos hit the 5s ceiling -- too slow for this spray distance.')
        print(f'  {"Dist (cm)":>10}  {"Speed (km/h)":>13}  {"Raw (ms)":>10}  {"Clamped (ms)":>13}')
        print(f'  {"-"*10}  {"-"*13}  {"-"*10}  {"-"*13}')
        for dist, speed, raw_ms, clamped_ms in clamped:
            print(f'  {dist:10d}  {speed:13.1f}  {raw_ms:10.1f}  {clamped_ms:13.1f}')

        if not clamped:
            print('  (none -- all combos within safe range)')

        for dist, speed, raw_ms, clamped_ms in clamped:
            assert clamped_ms == pytest.approx(5000.0, abs=0.1)

    def test_safe_operating_envelope(self):
        """For each distance, find the max safe speed (before 10ms clamp).

        This is the critical output for field testing: tells the farmer
        the maximum speed they can drive for each spray distance setting.
        """
        print('\n  === Safe operating envelope ===')
        print("  Maximum speed before 10ms clamp (relay can't switch faster)")
        print(f'  {"Dist (cm)":>10}  {"Max speed (km/h)":>17}  {"Duration at max (ms)":>21}')
        print(f'  {"-"*10}  {"-"*17}  {"-"*21}')

        for dist in self.ALL_DISTANCES:
            # Solve: (dist/100) / (speed/3.6) = 0.01
            # speed = (dist/100) / 0.01 * 3.6 = dist * 3.6
            max_safe_speed = dist * 3.6  # km/h

            calc = ActuationCalculator(actuation_length_cm=dist, offset_cm=0.0)
            result = calc.compute(max_safe_speed)

            print(f'  {dist:10d}  {max_safe_speed:17.1f}  '
                  f'{result["actuation_duration"]*1000:21.1f}')

            # At max_safe_speed, duration should be exactly 10ms
            assert result['actuation_duration'] == pytest.approx(0.01, abs=0.0001)

            # Just below max speed: should be > 10ms (not clamped)
            result_below = calc.compute(max_safe_speed * 0.9)
            assert result_below['actuation_duration'] > 0.01

    def test_typical_field_scenarios(self):
        """Real-world scenarios: typical tractor speeds + spray widths.

        Common setups:
        - Broadacre: 10-20cm at 10-20 km/h
        - Horticultural: 2-5cm at 3-8 km/h
        - Fallow: 5-15cm at 8-15 km/h
        """
        scenarios = [
            # (name, distance_cm, offset_cm, speed_range)
            ('Broadacre low', 10, 30, [8, 10, 12, 15, 18, 20]),
            ('Broadacre high', 20, 30, [10, 15, 20, 25]),
            ('Horticultural', 3, 15, [1, 3, 5, 8]),
            ('Fallow spray', 10, 20, [8, 10, 12, 15]),
            ('Precision micro (1cm)', 1, 10, [1, 2, 3, 3.6]),  # 3.6 = exact clamp point
            ('Precision micro (2cm)', 2, 10, [1, 2, 3, 5, 7.2]),  # 7.2 = exact clamp point
        ]

        print('\n  === Typical field scenarios ===')

        for name, dist, offset, speeds in scenarios:
            calc = ActuationCalculator(actuation_length_cm=dist, offset_cm=offset)
            print(f'\n  --- {name}: {dist}cm spray, {offset}cm offset ---')
            print(f'  {"Speed":>8}  {"Duration":>10}  {"Delay":>10}  {"Status":>12}')
            print(f'  {"-"*8}  {"-"*10}  {"-"*10}  {"-"*12}')

            for speed in speeds:
                r = calc.compute(speed)
                raw_dur = (dist / 100.0) / (speed / 3.6)
                if raw_dur < 0.01:
                    status = 'CLAMPED-MIN'
                elif raw_dur > 5.0:
                    status = 'CLAMPED-MAX'
                else:
                    status = 'OK'

                print(f'  {speed:7.1f}h  {r["actuation_duration"]*1000:9.1f}ms  '
                      f'{r["delay"]*1000:9.1f}ms  {status:>12}')

                # Verify source is GPS (not config fallback)
                assert r['source'] == 'gps'

    def test_min_duration_is_physically_reasonable(self):
        """10ms minimum is reasonable for solenoid response time.

        Typical agricultural solenoid valves have 5-15ms response times.
        The 10ms floor ensures the valve has time to fully open.
        """
        assert ActuationCalculator.MIN_DURATION == 0.01  # 10ms
        assert ActuationCalculator.MAX_DURATION == 5.0    # 5s
        assert ActuationCalculator.MIN_SPEED == 0.5       # 0.5 km/h


# ---------------------------------------------------------------------------
# Test 4: SpeedAverager -> ActuationCalculator end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSpeedToActuationPipeline:
    """Simulate the _actuation_broadcast_loop data flow without threads."""

    def test_gps_speed_feeds_actuation(self):
        """GPS speed sample -> SpeedAverager -> ActuationCalculator -> params."""
        sa = SpeedAverager(window_seconds=5.0)
        calc = ActuationCalculator(actuation_length_cm=10.0, offset_cm=30.0)

        # Simulate 3 GPS samples at ~10 km/h
        for speed in [9.8, 10.0, 10.2]:
            avg = sa.add_sample(speed)

        assert avg is not None
        result = calc.compute(avg)
        assert result['source'] == 'gps'
        assert result['actuation_duration'] > 0
        assert result['delay'] > 0

    def test_speed_change_updates_actuation(self):
        """Increasing speed should decrease actuation duration.

        Uses 50cm distance to stay well above the 10ms clamp.
        """
        sa = SpeedAverager(window_seconds=60.0)  # Long window so all samples count
        calc = ActuationCalculator(actuation_length_cm=50.0, offset_cm=0.0)

        results = []
        for speed in [1.0, 5.0, 10.0, 15.0, 20.0, 25.0]:
            avg = sa.add_sample(speed)
            result = calc.compute(avg)
            results.append((speed, avg, result['actuation_duration']))

        # Each successive result should have lower duration (avg speed increases)
        for i in range(1, len(results)):
            assert results[i][2] < results[i - 1][2], (
                f"Duration should decrease as avg speed increases: "
                f"sample={results[i][0]}, avg={results[i][1]:.1f}, "
                f"dur={results[i][2]:.4f}"
            )

    def test_gps_dropout_uses_fallback(self):
        """When GPS drops out, SpeedAverager provides fallback speed."""
        sa = SpeedAverager(window_seconds=0.1)
        calc = ActuationCalculator(
            actuation_length_cm=10.0, offset_cm=0.0,
            fallback_duration=0.15, fallback_delay=0.0
        )

        # Feed some samples then let window expire
        sa.add_sample(10.0)
        sa.add_sample(10.0)
        time.sleep(0.15)

        # Window is empty
        assert sa.get_average() is None

        # Fallback should still be available
        fallback = sa.get_fallback_speed()
        assert fallback is not None
        assert fallback == pytest.approx(10.0)

        # Using fallback should give GPS-derived duration (not config fallback)
        result = calc.compute(fallback)
        assert result['source'] == 'gps'
        assert result['actuation_duration'] < 0.15  # Less than config fallback

    def test_no_gps_uses_config_fallback(self):
        """Without any GPS data, falls back to config values."""
        sa = SpeedAverager()
        calc = ActuationCalculator(
            actuation_length_cm=10.0, offset_cm=0.0,
            fallback_duration=0.20, fallback_delay=0.05
        )

        # No samples added
        avg = sa.get_average()
        fallback = sa.get_fallback_speed()
        assert avg is None
        assert fallback is None

        result = calc.compute(None)
        assert result['source'] == 'config'
        assert result['actuation_duration'] == 0.20
        assert result['delay'] == 0.05


# ---------------------------------------------------------------------------
# Test 5: Full TCP -> GPSManager -> SpeedAverager -> ActuationCalculator
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestGPSTCPToActuation:
    """Spoof NMEA over TCP to GPSManager, verify speed reaches actuation.

    These are genuine integration tests: real TCP sockets, real NMEA parsing,
    real GPSState updates, real SpeedAverager averaging, real ActuationCalculator
    math. Only MQTT publish is mocked (tested separately in TestOWLActuationHandler).
    """

    def _send_nmea_to_port(self, port, sentences, pause=0.05):
        """Connect to GPSManager's TCP port and send NMEA sentences."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(('127.0.0.1', port))
        for sentence in sentences:
            sock.sendall(sentence.encode('ascii'))
            time.sleep(pause)
        sock.close()

    def test_spoofed_gps_updates_speed(self):
        """Send NMEA at known speed over TCP -> GPSManager -> verify speed."""
        port = 18501
        gps = GPSManager(port=port, boom_width=12.0)
        gps.start()
        time.sleep(0.3)

        try:
            speed_kmh = 12.0
            knots = kmh_to_knots(speed_kmh)

            sentences = [
                make_gpgga(),
                make_gprmc(knots),
                make_gprmc(knots),
            ]

            self._send_nmea_to_port(port, sentences, pause=0.1)
            time.sleep(0.3)

            state = gps.state.get_dict()
            assert state['speed_kmh'] is not None, 'Speed not updated from NMEA'
            assert state['speed_kmh'] == pytest.approx(speed_kmh, abs=0.5)
            assert state['fix_valid'] is True

        finally:
            gps.stop()

    def test_speed_change_reflected_in_gps_state(self):
        """Send NMEA at two different speeds, verify GPSState updates."""
        port = 18502
        gps = GPSManager(port=port, boom_width=12.0)
        gps.start()
        time.sleep(0.3)

        try:
            # First batch: 8 km/h
            sentences = [
                make_gpgga(),
                make_gprmc(kmh_to_knots(8.0)),
            ]
            self._send_nmea_to_port(port, sentences, pause=0.1)
            time.sleep(0.3)

            state1 = gps.state.get_dict()
            assert state1['speed_kmh'] == pytest.approx(8.0, abs=0.5)

            # Second batch: 20 km/h
            sentences = [
                make_gprmc(kmh_to_knots(20.0)),
            ]
            self._send_nmea_to_port(port, sentences, pause=0.1)
            time.sleep(0.3)

            state2 = gps.state.get_dict()
            assert state2['speed_kmh'] == pytest.approx(20.0, abs=0.5)
            assert state2['speed_kmh'] > state1['speed_kmh']

        finally:
            gps.stop()

    def test_full_pipeline_gps_to_actuation_params(self):
        """Full integration: TCP NMEA -> GPSManager -> SpeedAverager
        -> ActuationCalculator -> verify actuation timing changes with speed.

        Uses 20cm distance to stay well above the 10ms clamp at all test speeds.
        This is THE test for the field test requirement.
        """
        port = 18503
        gps = GPSManager(port=port, boom_width=12.0)
        gps.start()
        time.sleep(0.3)

        sa = SpeedAverager(window_seconds=5.0)
        calc = ActuationCalculator(actuation_length_cm=20.0, offset_cm=30.0)

        try:
            test_speeds = [1.0, 5.0, 10.0, 15.0, 20.0]
            results = []

            for speed_kmh in test_speeds:
                knots = kmh_to_knots(speed_kmh)
                sentences = [
                    make_gpgga(),
                    make_gprmc(knots),
                ]
                self._send_nmea_to_port(port, sentences, pause=0.05)
                time.sleep(0.2)

                # Read speed from GPS manager (mirrors _actuation_broadcast_loop)
                gps_state = gps.get_state()
                fix = gps_state.get('fix', {})
                raw_speed = fix.get('speed_kmh')
                assert raw_speed is not None, f'No speed from GPS at {speed_kmh} km/h'

                avg = sa.add_sample(raw_speed)
                r = calc.compute(avg)
                results.append(r)

                print(f'\n  GPS feed {speed_kmh:5.1f} km/h -> parsed {raw_speed:.1f} km/h, '
                      f'avg {avg:.1f} km/h -> '
                      f'duration={r["actuation_duration"]*1000:.1f}ms, '
                      f'delay={r["delay"]*1000:.1f}ms')

            # Verify all results are GPS-sourced
            for r in results:
                assert r['source'] == 'gps'

            # Verify durations decrease as speed increases
            for i in range(1, len(results)):
                assert results[i]['actuation_duration'] < results[i - 1]['actuation_duration'], (
                    f'Duration should decrease at higher speed. '
                    f'Got {results[i-1]["actuation_duration"]:.4f}s -> '
                    f'{results[i]["actuation_duration"]:.4f}s'
                )

            # Verify delays also decrease
            for i in range(1, len(results)):
                assert results[i]['delay'] < results[i - 1]['delay']

        finally:
            gps.stop()

    @pytest.mark.parametrize('speed_kmh', [1.0, 5.0, 10.0, 15.0, 20.0])
    def test_tcp_speed_matches_direct_calculation(self, speed_kmh):
        """TCP-parsed speed produces same actuation as direct calculation.

        Verifies no data corruption through the TCP -> parse -> state chain.
        At low speeds (1 km/h = 0.54 knots), NMEA's 1-decimal-place rounding
        introduces ~10% quantization error. This is a real Teltonika limitation,
        not a code bug. Tolerance is set to 15% to accommodate.
        """
        port = 18504 + int(speed_kmh)  # Unique port per parametrize
        gps = GPSManager(port=port, boom_width=12.0)
        gps.start()
        time.sleep(0.3)

        calc = ActuationCalculator(actuation_length_cm=10.0, offset_cm=20.0)

        try:
            sentences = [
                make_gpgga(),
                make_gprmc(kmh_to_knots(speed_kmh)),
            ]
            self._send_nmea_to_port(port, sentences, pause=0.05)
            time.sleep(0.3)

            # Get speed from GPS pipeline
            gps_speed = gps.state.get_dict()['speed_kmh']
            assert gps_speed is not None
            tcp_result = calc.compute(gps_speed)

            # Direct calculation at same speed
            direct_result = calc.compute(speed_kmh)

            # Use 15% relative tolerance to accommodate NMEA rounding
            # (0.54 knots rounds to 0.5 -> ~7% speed error at 1 km/h)
            assert tcp_result['actuation_duration'] == pytest.approx(
                direct_result['actuation_duration'], rel=0.15)
            assert tcp_result['delay'] == pytest.approx(
                direct_result['delay'], rel=0.15)

        finally:
            gps.stop()


# ---------------------------------------------------------------------------
# Test 6: MQTT handler on OWL side receives and applies params
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestOWLActuationHandler:
    """Verify the OWL-side MQTT handler correctly applies actuation params.

    This tests the final link: ActuationCalculator output -> MQTT command
    -> OWL mqtt_manager._handle_set_actuation_params -> owl.actuation_duration.
    """

    def test_handler_updates_owl_attrs(self, mqtt_publisher, mock_owl):
        """set_actuation_params command updates owl instance attributes."""
        calc = ActuationCalculator(actuation_length_cm=10.0, offset_cm=30.0)
        result = calc.compute(10.0)

        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': result['actuation_duration'],
            'delay': result['delay'],
            'source': result['source']
        })

        assert mock_owl.actuation_duration == pytest.approx(result['actuation_duration'])
        assert mock_owl.delay == pytest.approx(result['delay'])

    def test_handler_state_reflects_source(self, mqtt_publisher, mock_owl):
        """State dict shows 'gps' source after GPS-derived update."""
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': 0.05,
            'delay': 0.1,
            'source': 'gps'
        })
        assert mqtt_publisher.state['actuation_source'] == 'gps'
        assert mqtt_publisher.state['actuation_duration'] == pytest.approx(0.05)
        assert mqtt_publisher.state['delay'] == pytest.approx(0.1)

    def test_changing_speed_changes_owl_params(self, mqtt_publisher, mock_owl):
        """Simulate speed change: 5 km/h -> 10 km/h at 20cm, verify OWL updates.

        Uses 20cm so neither speed hits the 10ms clamp (max safe = 72 km/h).
        """
        calc = ActuationCalculator(actuation_length_cm=20.0, offset_cm=0.0)

        r1 = calc.compute(5.0)
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': r1['actuation_duration'],
            'delay': r1['delay'],
            'source': 'gps'
        })
        dur_at_5 = mock_owl.actuation_duration

        r2 = calc.compute(10.0)
        mqtt_publisher._handle_command({
            'action': 'set_actuation_params',
            'actuation_duration': r2['actuation_duration'],
            'delay': r2['delay'],
            'source': 'gps'
        })
        dur_at_10 = mock_owl.actuation_duration

        assert dur_at_10 < dur_at_5, (
            f'Duration should decrease at higher speed: '
            f'5km/h={dur_at_5:.4f}s, 10km/h={dur_at_10:.4f}s'
        )

    @pytest.mark.parametrize('distance_cm', [5, 10, 20, 50])
    def test_full_chain_per_distance(self, mqtt_publisher, mock_owl, distance_cm):
        """For each spray distance: GPS speed -> calculator -> MQTT handler
        -> verify owl.actuation_duration is set correctly.

        Tests 3 speeds per distance, all below the clamp point.
        """
        calc = ActuationCalculator(actuation_length_cm=distance_cm, offset_cm=20.0)

        # Use speeds well below the clamp point for this distance
        max_safe = distance_cm * 3.6
        test_speeds = [max_safe * 0.2, max_safe * 0.5, max_safe * 0.8]

        prev_duration = None
        for speed in test_speeds:
            result = calc.compute(speed)
            mqtt_publisher._handle_command({
                'action': 'set_actuation_params',
                'actuation_duration': result['actuation_duration'],
                'delay': result['delay'],
                'source': 'gps'
            })

            # Verify OWL instance was updated
            assert mock_owl.actuation_duration == pytest.approx(result['actuation_duration'])
            assert mock_owl.delay == pytest.approx(result['delay'])

            # Verify duration decreases with speed
            if prev_duration is not None:
                assert mock_owl.actuation_duration < prev_duration
            prev_duration = mock_owl.actuation_duration


# ---------------------------------------------------------------------------
# Test 7: Summary table -- print the field reference card
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFieldReferenceTable:
    """Print reference tables for field testing."""

    def test_print_reference_table(self):
        """Generate the reference card the farmer can take to the field."""
        speeds = [1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0]

        for length_cm in [1.0, 2.0, 5.0, 10.0, 20.0, 50.0]:
            calc = ActuationCalculator(
                actuation_length_cm=length_cm,
                offset_cm=30.0,
                fallback_duration=0.15,
                fallback_delay=0.0
            )

            max_safe = length_cm * 3.6
            print(f'\n  === {length_cm}cm spray, 30cm offset (max safe: {max_safe:.0f} km/h) ===')
            print(f'  {"Speed (km/h)":>14}  {"Duration (ms)":>14}  {"Delay (ms)":>12}  {"Status":>10}')
            print(f'  {"-"*14}  {"-"*14}  {"-"*12}  {"-"*10}')

            for speed in speeds:
                r = calc.compute(speed)
                raw = (length_cm / 100.0) / (speed / 3.6)
                if raw < 0.01:
                    status = 'CLAMPED'
                else:
                    status = 'OK'

                print(f'  {speed:14.1f}  {r["actuation_duration"]*1000:14.1f}  '
                      f'{r["delay"]*1000:12.1f}  {status:>10}')

            # Also show fallback
            r = calc.compute(None)
            print(f'  {"(no GPS)":>14}  {r["actuation_duration"]*1000:14.1f}  '
                  f'{r["delay"]*1000:12.1f}  {"FALLBACK":>10}')

        assert True
