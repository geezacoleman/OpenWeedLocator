"""EXIF embedding tests for utils/image_sampler.py.

Round-trips piexif.load() over JPEGs produced by build_exif_bytes() +
encode_jpeg(). The contract under test: every tag is written only when its
source value is present — absent data means the tag is OMITTED, never
substituted with a default or invented value.
"""

import json
from datetime import datetime, timezone

import numpy as np
import piexif
import piexif.helper
import pytest
from PIL import Image

from utils.image_sampler import (
    build_exif_bytes,
    encode_jpeg,
    _decimal_to_dms,
    _nmea_time_to_rationals,
    _nmea_date_to_stamp,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

FULL_GPS = {
    'latitude': -33.785391,
    'longitude': 151.123456,
    'accuracy': 0.9,
    'hdop': 0.9,
    'altitude': 45.3,
    'speed_kmh': 8.4,
    'heading': 271.5,
    'satellites': 12,
    'utc_time': '015230.50',
    'utc_date': '110626',
    'timestamp': 1750000000.0,
}

FULL_CAMERA = {
    'ExposureTime': 1250,       # µs
    'AnalogueGain': 2.5,
    'DigitalGain': 1.2,
    'Lux': 5230.4,
    'ColourTemperature': 5600,
}

FULL_CONTEXT = {
    'device_id': 'owl-1',
    'algorithm': 'exhsv',
    'model': None,              # not a GoG run — must be omitted from EXIF
    'owl_version': '3.0.0',
    'camera_model': 'imx296',
    'field_name': 'North paddock',
    'crop': 'wheat',
    'weather': 'sunny',
    'vehicle': '',              # empty — must be omitted from EXIF
}

CAPTURE_TIME = datetime(2026, 6, 11, 1, 52, 30, 500000, tzinfo=timezone.utc)


def _image():
    return Image.new('RGB', (32, 24), (40, 90, 30))


def _load(exif_bytes):
    """Encode a JPEG with the given EXIF and load the tags back."""
    return piexif.load(encode_jpeg(_image(), exif_bytes))


def _rat(value):
    return value[0] / value[1]


def _dms_to_decimal(dms):
    return _rat(dms[0]) + _rat(dms[1]) / 60 + _rat(dms[2]) / 3600


# ---------------------------------------------------------------------------
# Round-trip: everything present
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRoundTrip:

    def test_gps_coordinates_recovered(self):
        exif = _load(build_exif_bytes(gps_data=FULL_GPS))
        gps = exif['GPS']
        lat = _dms_to_decimal(gps[piexif.GPSIFD.GPSLatitude])
        lon = _dms_to_decimal(gps[piexif.GPSIFD.GPSLongitude])
        assert gps[piexif.GPSIFD.GPSLatitudeRef] == b'S'
        assert gps[piexif.GPSIFD.GPSLongitudeRef] == b'E'
        assert lat == pytest.approx(abs(FULL_GPS['latitude']), abs=2e-6)
        assert lon == pytest.approx(abs(FULL_GPS['longitude']), abs=2e-6)

    def test_datetime_tags(self):
        exif = _load(build_exif_bytes(capture_time=CAPTURE_TIME))
        local_stamp = CAPTURE_TIME.astimezone().strftime('%Y:%m:%d %H:%M:%S').encode()
        assert exif['0th'][piexif.ImageIFD.DateTime] == local_stamp
        assert exif['Exif'][piexif.ExifIFD.DateTimeOriginal] == local_stamp
        assert exif['Exif'][piexif.ExifIFD.DateTimeDigitized] == local_stamp
        assert exif['Exif'][piexif.ExifIFD.SubSecTimeOriginal] == b'500'

    def test_exposure_and_iso(self):
        exif = _load(build_exif_bytes(camera_metadata=FULL_CAMERA))
        exposure = exif['Exif'][piexif.ExifIFD.ExposureTime]
        assert _rat(exposure) == pytest.approx(1250 / 1_000_000)
        # ISO = AnalogueGain * DigitalGain * 100
        assert exif['Exif'][piexif.ExifIFD.ISOSpeedRatings] == 300

    def test_make_model_software(self):
        exif = _load(build_exif_bytes(context=FULL_CONTEXT))
        assert exif['0th'][piexif.ImageIFD.Make] == b'Raspberry Pi'
        assert exif['0th'][piexif.ImageIFD.Model] == b'imx296'
        assert exif['0th'][piexif.ImageIFD.Software] == b'OpenWeedLocator 3.0.0'

    def test_image_description_json_omits_empty_values(self):
        exif = _load(build_exif_bytes(context=FULL_CONTEXT))
        description = json.loads(exif['0th'][piexif.ImageIFD.ImageDescription])
        assert description['device_id'] == 'owl-1'
        assert description['algorithm'] == 'exhsv'
        assert description['field_name'] == 'North paddock'
        assert description['crop'] == 'wheat'
        # None and empty-string values must not be embedded
        assert 'model' not in description
        assert 'vehicle' not in description
        # camera_model/owl_version are mapped to dedicated tags, not duplicated here
        assert 'camera_model' not in description
        assert 'owl_version' not in description

    def test_user_comment_holds_full_camera_metadata(self):
        exif = _load(build_exif_bytes(camera_metadata=FULL_CAMERA))
        comment = piexif.helper.UserComment.load(exif['Exif'][piexif.ExifIFD.UserComment])
        metadata = json.loads(comment)
        assert metadata['Lux'] == pytest.approx(5230.4)
        assert metadata['ColourTemperature'] == 5600
        assert metadata['ExposureTime'] == 1250

    def test_gps_date_and_time_stamps(self):
        exif = _load(build_exif_bytes(gps_data=FULL_GPS))
        gps = exif['GPS']
        assert gps[piexif.GPSIFD.GPSDateStamp] == b'2026:06:11'
        hours, minutes, seconds = gps[piexif.GPSIFD.GPSTimeStamp]
        assert _rat(hours) == 1
        assert _rat(minutes) == 52
        assert _rat(seconds) == pytest.approx(30.5)

    def test_altitude_and_negative_ref(self):
        exif = _load(build_exif_bytes(gps_data=FULL_GPS))
        gps = exif['GPS']
        assert gps[piexif.GPSIFD.GPSAltitudeRef] == 0
        assert _rat(gps[piexif.GPSIFD.GPSAltitude]) == pytest.approx(45.3)

        below_sea = dict(FULL_GPS, altitude=-12.5)
        gps = _load(build_exif_bytes(gps_data=below_sea))['GPS']
        assert gps[piexif.GPSIFD.GPSAltitudeRef] == 1
        assert _rat(gps[piexif.GPSIFD.GPSAltitude]) == pytest.approx(12.5)

    def test_speed_heading_satellites(self):
        gps = _load(build_exif_bytes(gps_data=FULL_GPS))['GPS']
        assert gps[piexif.GPSIFD.GPSSpeedRef] == b'K'
        assert _rat(gps[piexif.GPSIFD.GPSSpeed]) == pytest.approx(8.4)
        assert gps[piexif.GPSIFD.GPSImgDirectionRef] == b'T'
        assert _rat(gps[piexif.GPSIFD.GPSImgDirection]) == pytest.approx(271.5)
        assert gps[piexif.GPSIFD.GPSSatellites] == b'12'

    def test_hdop_maps_to_dop_not_positioning_error(self):
        gps = _load(build_exif_bytes(gps_data=FULL_GPS))['GPS']
        assert _rat(gps[piexif.GPSIFD.GPSDOP]) == pytest.approx(0.9)
        assert piexif.GPSIFD.GPSHPositioningError not in gps

    def test_browser_accuracy_maps_to_positioning_error(self):
        browser_gps = {'latitude': -33.7, 'longitude': 151.1, 'accuracy': 12.0,
                       'timestamp': 1750000000.0}
        gps = _load(build_exif_bytes(gps_data=browser_gps))['GPS']
        assert _rat(gps[piexif.GPSIFD.GPSHPositioningError]) == pytest.approx(12.0)
        assert piexif.GPSIFD.GPSDOP not in gps


# ---------------------------------------------------------------------------
# Fail-safe: absent sources mean absent tags — never defaults
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestFailSafeOmission:

    def test_all_none_returns_none(self):
        assert build_exif_bytes() is None
        assert build_exif_bytes(gps_data=None, camera_metadata=None,
                                context=None, capture_time=None) is None

    def test_encode_jpeg_without_exif_is_valid(self):
        jpeg_bytes = encode_jpeg(_image(), exif_bytes=None)
        image = Image.open(__import__('io').BytesIO(jpeg_bytes))
        assert image.size == (32, 24)
        exif = piexif.load(jpeg_bytes)
        assert not exif['GPS'] and not exif['Exif'] and not exif['0th']

    def test_gps_without_coordinates_omits_gps_ifd(self):
        exif = _load(build_exif_bytes(gps_data={'speed_kmh': 5.0},
                                      capture_time=CAPTURE_TIME))
        assert not exif['GPS']

    def test_minimal_gps_omits_optional_tags(self):
        gps = _load(build_exif_bytes(gps_data={'latitude': -33.7, 'longitude': 151.1}))['GPS']
        assert piexif.GPSIFD.GPSLatitude in gps
        for tag in (piexif.GPSIFD.GPSAltitude, piexif.GPSIFD.GPSDOP,
                    piexif.GPSIFD.GPSHPositioningError, piexif.GPSIFD.GPSSpeed,
                    piexif.GPSIFD.GPSImgDirection, piexif.GPSIFD.GPSSatellites,
                    piexif.GPSIFD.GPSTimeStamp, piexif.GPSIFD.GPSDateStamp):
            assert tag not in gps

    def test_unpaired_utc_time_omitted(self):
        """GPS time without a date (or vice versa) is meaningless — omit both."""
        gps_data = dict(FULL_GPS, utc_date=None)
        gps = _load(build_exif_bytes(gps_data=gps_data))['GPS']
        assert piexif.GPSIFD.GPSTimeStamp not in gps
        assert piexif.GPSIFD.GPSDateStamp not in gps

    def test_no_capture_time_omits_datetime(self):
        exif = _load(build_exif_bytes(gps_data=FULL_GPS))
        assert piexif.ImageIFD.DateTime not in exif['0th']
        assert piexif.ExifIFD.DateTimeOriginal not in exif['Exif']

    def test_no_camera_metadata_omits_exposure_tags(self):
        exif = _load(build_exif_bytes(capture_time=CAPTURE_TIME))
        assert piexif.ExifIFD.ExposureTime not in exif['Exif']
        assert piexif.ExifIFD.ISOSpeedRatings not in exif['Exif']
        assert piexif.ExifIFD.UserComment not in exif['Exif']

    def test_partial_camera_metadata(self):
        """ExposureTime without gain: only the exposure tag is written."""
        exif = _load(build_exif_bytes(camera_metadata={'ExposureTime': 2000}))
        assert piexif.ExifIFD.ExposureTime in exif['Exif']
        assert piexif.ExifIFD.ISOSpeedRatings not in exif['Exif']

    def test_empty_context_omits_description_and_make(self):
        exif = _load(build_exif_bytes(context={'device_id': None, 'vehicle': ''},
                                      capture_time=CAPTURE_TIME))
        assert piexif.ImageIFD.ImageDescription not in exif['0th']
        assert piexif.ImageIFD.Make not in exif['0th']
        assert piexif.ImageIFD.Software not in exif['0th']

    def test_malformed_nmea_helpers_return_none(self):
        assert _nmea_time_to_rationals('garbage') is None
        assert _nmea_time_to_rationals('') is None
        assert _nmea_time_to_rationals('991299.0') is None
        assert _nmea_date_to_stamp('garbage') is None
        assert _nmea_date_to_stamp('') is None
        assert _nmea_date_to_stamp('991399') is None


# ---------------------------------------------------------------------------
# DMS conversion precision (regression for int() truncation, ~0.3 m bias)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDMSPrecision:

    @pytest.mark.parametrize('value', [-33.785391, 151.123456, 0.000001, 89.999999])
    def test_round_trip_within_20cm(self, value):
        dms = _decimal_to_dms(value)
        recovered = _dms_to_decimal(dms)
        # 2e-6 degrees of latitude ≈ 0.22 m
        assert recovered == pytest.approx(abs(value), abs=2e-6)

    def test_rounding_carry_into_degrees(self):
        """A value whose minutes round up to exactly 60 must carry cleanly."""
        dms = _decimal_to_dms(33.99999999)
        assert dms == [(34, 1), (0, 10000), (0, 1)]

    def test_rationals_are_integers(self):
        for numerator, denominator in _decimal_to_dms(-33.785391):
            assert isinstance(numerator, int)
            assert isinstance(denominator, int)


# ---------------------------------------------------------------------------
# ImageRecorder save path writes EXIF to disk
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestImageRecorderSavePath:

    def _bare_recorder(self, tmp_path, mode='whole'):
        """ImageRecorder without spawning worker processes."""
        from utils.image_sampler import ImageRecorder
        recorder = ImageRecorder.__new__(ImageRecorder)
        recorder.save_directory = str(tmp_path)
        recorder.mode = mode
        return recorder

    def test_process_frame_writes_jpeg_with_full_exif(self, tmp_path):
        recorder = self._bare_recorder(tmp_path)
        frame = np.zeros((24, 32, 3), dtype=np.uint8)
        recorder.process_frame(frame, 1, None, None, FULL_GPS,
                               FULL_CAMERA, FULL_CONTEXT, CAPTURE_TIME)

        files = list(tmp_path.glob('*.jpg'))
        assert len(files) == 1
        exif = piexif.load(str(files[0]))
        assert piexif.GPSIFD.GPSLatitude in exif['GPS']
        assert piexif.ExifIFD.DateTimeOriginal in exif['Exif']
        assert piexif.ExifIFD.ExposureTime in exif['Exif']
        assert exif['0th'][piexif.ImageIFD.Model] == b'imx296'

    def test_process_frame_without_metadata_still_saves(self, tmp_path):
        """Webcam / no-GPS path: image must save cleanly with no EXIF."""
        recorder = self._bare_recorder(tmp_path)
        frame = np.zeros((24, 32, 3), dtype=np.uint8)
        recorder.process_frame(frame, 2, None, None, None, None, None, None)

        files = list(tmp_path.glob('*.jpg'))
        assert len(files) == 1
        exif = piexif.load(str(files[0]))
        assert not exif['GPS']
