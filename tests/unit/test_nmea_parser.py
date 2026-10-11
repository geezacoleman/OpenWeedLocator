"""Tests for NMEA parser functions in utils/gps_manager.py."""

import pytest
from utils.gps_manager import (
    validate_checksum,
    nmea_to_decimal,
    parse_gprmc,
    parse_gpgga,
    parse_gpvtg,
    parse_gpgsv,
    parse_sentence,
    KNOTS_TO_KMH,
)


# ---------------------------------------------------------------------------
# Checksum validation
# ---------------------------------------------------------------------------

class TestValidateChecksum:
    def test_valid_rmc(self):
        assert validate_checksum('$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A')

    def test_valid_gga(self):
        assert validate_checksum('$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F')

    def test_invalid_checksum(self):
        assert not validate_checksum('$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*FF')

    def test_missing_dollar(self):
        assert not validate_checksum('GPRMC,123519,A*00')

    def test_missing_asterisk(self):
        assert not validate_checksum('$GPRMC,123519,A')

    def test_bad_hex(self):
        assert not validate_checksum('$GPRMC*ZZ')

    def test_empty_string(self):
        assert not validate_checksum('')


# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------

class TestNmeaToDecimal:
    def test_north_latitude(self):
        # 48 degrees 07.038 minutes N = 48.1173
        result = nmea_to_decimal('4807.038', 'N')
        assert result == pytest.approx(48.1173, abs=0.001)

    def test_south_latitude(self):
        result = nmea_to_decimal('3348.500', 'S')
        assert result < 0
        assert result == pytest.approx(-33.808333, abs=0.001)

    def test_east_longitude(self):
        result = nmea_to_decimal('01131.000', 'E')
        assert result == pytest.approx(11.516667, abs=0.001)

    def test_west_longitude(self):
        result = nmea_to_decimal('14952.500', 'W')
        assert result < 0
        assert result == pytest.approx(-149.875, abs=0.001)

    def test_empty_raw(self):
        assert nmea_to_decimal('', 'N') is None

    def test_none_hemisphere(self):
        assert nmea_to_decimal('4807.038', None) is None

    def test_invalid_value(self):
        assert nmea_to_decimal('not_a_number', 'N') is None


# ---------------------------------------------------------------------------
# RMC parsing
# ---------------------------------------------------------------------------

class TestParseGprmc:
    VALID_RMC = '$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A'

    def test_parse_valid_rmc(self):
        result = parse_gprmc(self.VALID_RMC)
        assert result is not None
        assert result['type'] == 'RMC'
        assert result['status'] == 'A'
        assert result['lat'] == pytest.approx(48.1173, abs=0.001)
        assert result['lon'] == pytest.approx(11.5167, abs=0.001)
        assert result['speed_knots'] == pytest.approx(22.4)
        assert result['heading'] == pytest.approx(84.4)
        assert result['time_utc'] == '123519'
        assert result['date'] == '230394'

    def test_void_status(self):
        void_rmc = '$GPRMC,123519,V,,,,,,,230394,,,*24'
        result = parse_gprmc(void_rmc)
        assert result is not None
        assert result['status'] == 'V'
        assert result['lat'] is None
        assert result['lon'] is None

    def test_too_few_fields(self):
        assert parse_gprmc('$GPRMC,123519,A*00') is None


# ---------------------------------------------------------------------------
# GGA parsing
# ---------------------------------------------------------------------------

class TestParseGpgga:
    VALID_GGA = '$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F'

    def test_parse_valid_gga(self):
        result = parse_gpgga(self.VALID_GGA)
        assert result is not None
        assert result['type'] == 'GGA'
        assert result['lat'] == pytest.approx(48.1173, abs=0.001)
        assert result['lon'] == pytest.approx(11.5167, abs=0.001)
        assert result['fix_quality'] == 1
        assert result['satellites'] == 8
        assert result['hdop'] == pytest.approx(0.9)
        assert result['altitude'] == pytest.approx(545.4)

    def test_no_fix(self):
        no_fix = '$GPGGA,123519,,,,,0,00,,,,,,,*66'
        result = parse_gpgga(no_fix)
        assert result is not None
        assert result['fix_quality'] == 0
        assert result['lat'] is None


# ---------------------------------------------------------------------------
# VTG parsing
# ---------------------------------------------------------------------------

class TestParseGpvtg:
    VALID_VTG = '$GPVTG,054.7,T,034.4,M,005.5,N,010.2,K*48'

    def test_parse_valid_vtg(self):
        result = parse_gpvtg(self.VALID_VTG)
        assert result is not None
        assert result['type'] == 'VTG'
        assert result['heading_true'] == pytest.approx(54.7)
        assert result['speed_knots'] == pytest.approx(5.5)
        assert result['speed_kmh'] == pytest.approx(10.2)


# ---------------------------------------------------------------------------
# GSV parsing
# ---------------------------------------------------------------------------

class TestParseGpgsv:
    VALID_GSV = '$GPGSV,3,1,11,03,03,111,00,04,15,270,00,06,01,010,00,13,06,292,00*74'

    def test_parse_valid_gsv(self):
        result = parse_gpgsv(self.VALID_GSV)
        assert result is not None
        assert result['type'] == 'GSV'
        assert result['satellites_in_view'] == 11


# ---------------------------------------------------------------------------
# Universal sentence router
# ---------------------------------------------------------------------------

class TestParseSentence:
    def test_routes_gprmc(self):
        result = parse_sentence('$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*6A')
        assert result is not None
        assert result['type'] == 'RMC'

    def test_routes_gnrmc(self):
        # $GN prefix (multi-constellation)
        sentence = '$GNRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*74'
        result = parse_sentence(sentence)
        assert result is not None
        assert result['type'] == 'RMC'

    def test_invalid_checksum_rejected(self):
        result = parse_sentence('$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W*FF')
        assert result is None

    def test_unknown_sentence_type(self):
        # $GPXXX is not a known type — but needs valid checksum
        result = parse_sentence('$GPXXX,some,data*00')
        # Will fail checksum
        assert result is None

    def test_empty_line(self):
        assert parse_sentence('') is None

    def test_non_nmea(self):
        assert parse_sentence('random garbage text') is None

    def test_teltonika_imei_prefix(self):
        # Teltonika routers prepend IMEI to NMEA: '6003197898_$GPGGA,...'
        sentence = '1234567890_$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F'
        result = parse_sentence(sentence)
        assert result is not None
        assert result['type'] == 'GGA'
        assert result['satellites'] == 8
        assert result['lat'] == pytest.approx(48.1173, abs=0.001)
