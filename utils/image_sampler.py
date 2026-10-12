import cv2
import json
import math
import os
import time
import numpy as np
from collections import deque
from datetime import datetime, timezone
import threading
from multiprocessing import Event, Process, Queue, Value
from multiprocessing.queues import Empty
from utils.algorithms import fft_blur
from utils.log_manager import LogManager
from io import BytesIO
from PIL import Image

try:
    import piexif
    import piexif.helper
except ImportError as e:
    from utils.error_manager import DependencyError
    raise DependencyError('piexif', str(e))

logger = LogManager.get_logger(__name__)

# Per-field cap for the free-form JSON EXIF tags (UserComment, ImageDescription).
# Keeps total EXIF well under the JPEG APP1 ~64 KB limit so the structured tags
# (DateTimeOriginal, GPS, exposure) always fit and the save never fails on metadata.
_MAX_EXIF_JSON = 8000

# Keys that must survive a trim: the settings and provenance that make a frame
# reproducible. Everything else is free text the operator typed.
_ESSENTIAL_CONTEXT_KEYS = (
    'device_id', 'algorithm', 'model', 'session_id', 'session_start',
    'detections', 'source_width', 'source_height',
    'roi_x', 'roi_y', 'roi_width', 'roi_height',
    'exg_min', 'exg_max', 'hue_min', 'hue_max',
    'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
    'min_detection_area_percent', 'sensitivity_level',
    'lut_profile', 'lut_sensitivity', 'confidence', 'invert_hue',
)


def _fit_json(payload, keep=(), cap=_MAX_EXIF_JSON):
    """Serialise a dict to JSON, dropping the least important keys until it fits.

    Overflow used to drop the whole tag, which threw away the detection settings
    along with whatever caused the overflow. Biggest non-essential values go
    first, so one oversized field cannot cost twenty small ones, and keys in
    `keep` are only sacrificed once nothing else is left.
    """
    payload = dict(payload)
    text = json.dumps(payload, default=str)
    if len(text) <= cap:
        return text

    droppable = sorted((k for k in payload if k not in keep),
                       key=lambda k: len(str(payload[k])), reverse=True)
    droppable += [k for k in payload if k in keep]
    for key in droppable:
        payload.pop(key, None)
        payload['metadata_truncated'] = True
        text = json.dumps(payload, default=str)
        if len(text) <= cap:
            return text
    return None

# Sensor model prefix -> real manufacturer. libcamera reports only the sensor model
# (e.g. 'imx296', 'ar0234'); the model-number prefix encodes the maker, so we can fill
# EXIF Make with a TRUE value derived from data we already pull — and omit it (never
# invent one) for an unrecognised prefix. Prefix conventions:
#   Sony        IMX
#   OmniVision  OV (legacy), OG (newer, e.g. og02b10), OX (automotive)
#   onsemi      AR (e.g. ar0234; formerly Aptina), MT9 (legacy Aptina/Micron)
#   GalaxyCore  GC
#   Samsung     S5K
#   SK Hynix    Hi
# Sorted longest-prefix-first so a more specific prefix always wins.
_SENSOR_MAKE = sorted((
    ('imx', 'Sony'),
    ('ov', 'OmniVision'),
    ('og', 'OmniVision'),
    ('ox', 'OmniVision'),
    ('ar', 'onsemi'),
    ('mt9', 'onsemi'),
    ('gc', 'GalaxyCore'),
    ('s5k', 'Samsung'),
    ('hi', 'SK Hynix'),
), key=lambda kv: -len(kv[0]))


def _sensor_make(model):
    """Return the manufacturer for a sensor model string, or None if unrecognised."""
    m = model.lower()
    return next((make for prefix, make in _SENSOR_MAKE if m.startswith(prefix)), None)


def _decimal_to_dms(value):
    """Decimal degrees -> EXIF rationals as degrees + decimal minutes.

    Matches NMEA's native DDMM.MMMM precision (~0.2 m); rounding instead of
    truncating avoids the up-to-0.3 m southward/westward bias of the old int() code.
    """
    value = abs(value)
    degrees = int(value)
    minutes = round((value - degrees) * 60 * 10000)
    if minutes >= 60 * 10000:  # rounding carried into the next degree
        degrees += 1
        minutes -= 60 * 10000
    return [(degrees, 1), (minutes, 10000), (0, 1)]


def _nmea_time_to_rationals(time_utc):
    """NMEA 'hhmmss[.sss]' -> EXIF GPSTimeStamp rationals, or None if malformed."""
    try:
        hours = int(time_utc[0:2])
        minutes = int(time_utc[2:4])
        seconds = float(time_utc[4:])
        if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 61):
            return None
        return [(hours, 1), (minutes, 1), (round(seconds * 1000), 1000)]
    except (ValueError, IndexError, TypeError):
        return None


def _nmea_date_to_stamp(date):
    """NMEA 'ddmmyy' -> EXIF 'YYYY:MM:DD', or None if malformed."""
    try:
        day = int(date[0:2])
        month = int(date[2:4])
        year = 2000 + int(date[4:6])
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return None
        return f"{year:04d}:{month:02d}:{day:02d}"
    except (ValueError, IndexError, TypeError):
        return None


def build_exif_bytes(gps_data=None, camera_metadata=None, context=None, capture_time=None):
    """Build EXIF bytes for a saved image from whatever sources are available.

    Fail-safe by design: every tag is written only when its source value is
    actually present — absent data means the tag is omitted entirely, never
    substituted with a default or invented value.

    :param gps_data: dict with latitude/longitude and optional hdop, accuracy,
        altitude, speed_kmh, heading, satellites, utc_time, utc_date (NMEA strings)
    :param camera_metadata: picamera2 per-frame metadata dict (ExposureTime µs,
        AnalogueGain, DigitalGain, Lux, ...) or None on webcams
    :param context: dict with camera_model, owl_version, device_id, algorithm,
        model, and farmer session metadata (field_name, crop, weather, vehicle)
    :param capture_time: tz-aware datetime of frame capture
    :return: piexif-encoded bytes, or None when there is nothing to embed
    """
    try:
        zeroth, exif_ifd, gps_ifd = {}, {}, {}

        if capture_time is not None:
            local_time = capture_time.astimezone()
            stamp = local_time.strftime('%Y:%m:%d %H:%M:%S')
            zeroth[piexif.ImageIFD.DateTime] = stamp
            exif_ifd[piexif.ExifIFD.DateTimeOriginal] = stamp
            exif_ifd[piexif.ExifIFD.DateTimeDigitized] = stamp
            exif_ifd[piexif.ExifIFD.SubSecTimeOriginal] = f"{capture_time.microsecond // 1000:03d}"
            offset = local_time.strftime('%z')  # e.g. '+1000'
            if offset and hasattr(piexif.ExifIFD, 'OffsetTimeOriginal'):  # EXIF 2.31, older piexif lacks it
                exif_ifd[piexif.ExifIFD.OffsetTimeOriginal] = f"{offset[:3]}:{offset[3:]}"

        context = {k: v for k, v in (context or {}).items() if v not in (None, '')}
        camera_model = context.pop('camera_model', None)
        if camera_model:
            model = str(camera_model)
            make = _sensor_make(model)
            if make:  # omit when the sensor maker is unknown — never invent it
                zeroth[piexif.ImageIFD.Make] = make
            zeroth[piexif.ImageIFD.Model] = model
        owl_version = context.pop('owl_version', None)
        if owl_version:
            zeroth[piexif.ImageIFD.Software] = f"OpenWeedLocator {owl_version}"
        if context:  # device_id, algorithm, detection settings, ROI, session metadata
            desc = _fit_json(context, keep=_ESSENTIAL_CONTEXT_KEYS)
            if desc:
                zeroth[piexif.ImageIFD.ImageDescription] = desc
            else:
                logger.warning('EXIF ImageDescription could not be trimmed to fit; omitted')

        if camera_metadata:
            exposure_us = camera_metadata.get('ExposureTime')
            if exposure_us:
                exif_ifd[piexif.ExifIFD.ExposureTime] = (int(exposure_us), 1_000_000)
            analogue_gain = camera_metadata.get('AnalogueGain')
            if analogue_gain:
                # libcamera convention: ISO equivalent = total gain x 100
                total_gain = analogue_gain * (camera_metadata.get('DigitalGain') or 1.0)
                exif_ifd[piexif.ExifIFD.ISOSpeedRatings] = round(total_gain * 100)
            lens_position = camera_metadata.get('LensPosition')
            if lens_position:  # dioptres -> metres
                exif_ifd[piexif.ExifIFD.SubjectDistance] = (round(100 / lens_position), 100)
            # Complete raw metadata for scientific traceability (authoritative record)
            meta_json = _fit_json(camera_metadata)
            if meta_json:
                exif_ifd[piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(meta_json)
            else:
                logger.warning('EXIF UserComment could not be trimmed to fit; omitted')

        if gps_data and gps_data.get('latitude') is not None and gps_data.get('longitude') is not None:
            lat = float(gps_data['latitude'])
            lon = float(gps_data['longitude'])
            gps_ifd[piexif.GPSIFD.GPSVersionID] = (2, 3, 0, 0)
            gps_ifd[piexif.GPSIFD.GPSLatitudeRef] = 'N' if lat >= 0 else 'S'
            gps_ifd[piexif.GPSIFD.GPSLatitude] = _decimal_to_dms(lat)
            gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = 'E' if lon >= 0 else 'W'
            gps_ifd[piexif.GPSIFD.GPSLongitude] = _decimal_to_dms(lon)
            gps_ifd[piexif.GPSIFD.GPSMapDatum] = 'WGS-84'

            if gps_data.get('altitude') is not None:
                altitude = float(gps_data['altitude'])
                gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 1 if altitude < 0 else 0
                gps_ifd[piexif.GPSIFD.GPSAltitude] = (round(abs(altitude) * 100), 100)
            if gps_data.get('hdop') is not None:
                gps_ifd[piexif.GPSIFD.GPSDOP] = (round(float(gps_data['hdop']) * 100), 100)
            elif gps_data.get('accuracy') is not None and hasattr(piexif.GPSIFD, 'GPSHPositioningError'):
                # browser geolocation reports accuracy in metres, not HDOP
                gps_ifd[piexif.GPSIFD.GPSHPositioningError] = (round(float(gps_data['accuracy']) * 100), 100)
            if gps_data.get('speed_kmh') is not None:
                gps_ifd[piexif.GPSIFD.GPSSpeedRef] = 'K'
                gps_ifd[piexif.GPSIFD.GPSSpeed] = (round(float(gps_data['speed_kmh']) * 100), 100)
            if gps_data.get('heading') is not None:
                gps_ifd[piexif.GPSIFD.GPSImgDirectionRef] = 'T'
                gps_ifd[piexif.GPSIFD.GPSImgDirection] = (round(float(gps_data['heading']) * 100), 100)
            if gps_data.get('satellites') is not None:
                gps_ifd[piexif.GPSIFD.GPSSatellites] = str(gps_data['satellites'])

            # NMEA UTC is GPS ground truth — correct even when the Pi clock is wrong
            gps_time = _nmea_time_to_rationals(gps_data.get('utc_time')) if gps_data.get('utc_time') else None
            gps_date = _nmea_date_to_stamp(gps_data.get('utc_date')) if gps_data.get('utc_date') else None
            if gps_time and gps_date:  # only meaningful as a pair
                gps_ifd[piexif.GPSIFD.GPSTimeStamp] = gps_time
                gps_ifd[piexif.GPSIFD.GPSDateStamp] = gps_date

        if not (zeroth or exif_ifd or gps_ifd):
            return None
        return piexif.dump({'0th': zeroth, 'Exif': exif_ifd, 'GPS': gps_ifd})

    except Exception as e:
        logger.error(f"Failed to build EXIF metadata, image will be saved without it: {e}")
        return None


def encode_jpeg(pil_image, exif_bytes=None, quality=95):
    """Encode a PIL Image to JPEG bytes, embedding EXIF when provided.

    Fail-safe: if the EXIF is rejected at save time (e.g. it exceeds the JPEG APP1
    ~64 KB limit), the image is still written WITHOUT EXIF rather than lost — the
    pixel data always reaches disk.
    """
    save_kwargs = dict(format='JPEG', quality=quality, subsampling=0, optimize=True, progressive=True)
    if exif_bytes:
        try:
            buf = BytesIO()
            pil_image.save(buf, exif=exif_bytes, **save_kwargs)
            return buf.getvalue()
        except Exception as e:
            logger.error(f"Failed to embed EXIF, saving image without it: {e}")
    buf = BytesIO()
    pil_image.save(buf, **save_kwargs)
    return buf.getvalue()


SAMPLE_MODES = ('frames', 'distance', 'time')
# Below this speed a fresh GPS fix means the rig is parked: distance mode
# saves nothing rather than hundreds of copies of the same patch of ground.
STATIONARY_KMH = 0.5


def _finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {value}")
    return value


class CaptureScheduler:
    """Decides which frames the image sampler saves.

    frames:   every `frequency`th frame (the original behaviour)
    distance: every `distance_m` travelled, integrated from GPS speed x time.
              With no fresh speed it falls back to every `interval_s`; with a
              fresh speed below STATIONARY_KMH it saves nothing.
    time:     every `interval_s`

    In every mode a fresh GPS speed below `min_speed_kmh` saves nothing
    (0 = never pause). Without a fresh speed images keep coming.

    Pure (no camera, no cv2) so the spacing rules are unit-testable.
    """

    def __init__(self, mode='frames', frequency=30, distance_m=1.0, interval_s=2.0,
                 min_speed_kmh=0.0):
        self.configure(mode=mode, frequency=frequency, distance_m=distance_m,
                       interval_s=interval_s, min_speed_kmh=min_speed_kmh)
        self.reset()

    def configure(self, mode=None, frequency=None, distance_m=None, interval_s=None,
                  min_speed_kmh=None):
        if mode is not None:
            self.mode = mode if mode in SAMPLE_MODES else 'frames'
        if frequency is not None:
            self.frequency = max(1, int(frequency))
        # Same bounds as the config validator; inf/nan rejected outright.
        if distance_m is not None:
            self.distance_m = min(50.0, max(0.05, _finite(distance_m)))
        if interval_s is not None:
            self.interval_s = min(60.0, max(0.1, _finite(interval_s)))
        if min_speed_kmh is not None:
            self.min_speed_kmh = min(20.0, max(0.0, _finite(min_speed_kmh)))

    def too_slow(self, speed_kmh):
        """True when a fresh GPS speed is under the pause limit."""
        return speed_kmh is not None and speed_kmh < self.min_speed_kmh

    def reset(self):
        """Start of a session: the first eligible frame is saved straight away."""
        self._travelled = None
        self._last_tick = None
        self._last_capture = None

    def should_capture(self, frame_count, now=None, speed_kmh=None, frequency=None):
        """True when this frame should be saved. `speed_kmh` is None when
        there is no fresh GPS speed. `frequency` overrides the frame interval
        (the live sample_frequency, which the dashboards still set directly)."""
        if self.too_slow(speed_kmh):
            return False
        if self.mode == 'frames':
            return frame_count % max(1, int(frequency or self.frequency)) == 0

        now = time.monotonic() if now is None else now
        if self.mode == 'distance' and speed_kmh is not None:
            dt = 0.0 if self._last_tick is None else min(max(now - self._last_tick, 0.0), 1.0)
            self._last_tick = now
            if speed_kmh < STATIONARY_KMH:
                return False
            if self._travelled is None:
                self._travelled = 0.0
                self._last_capture = now
                return True
            self._travelled += speed_kmh / 3.6 * dt
            if self._travelled >= self.distance_m:
                # Reset rather than subtract: after a stall the backlog would
                # otherwise come out as a burst of near-identical frames.
                self._travelled = 0.0
                self._last_capture = now
                return True
            return False

        # time mode, or distance mode without a fresh speed
        self._last_tick = None
        if self._last_capture is None or now - self._last_capture >= self.interval_s:
            self._last_capture = now
            return True
        return False


def image_quality(frame, width=320):
    """Cheap per-image quality metrics on a small grey copy (a few ms).

    sharpness  - fft_blur score; scene dependent, so it is only meaningful
                 against the same session's baseline
    brightness - mean 0-255
    clipped    - % of pixels above 250
    dark       - % of pixels below 10
    """
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    total = float(gray.size)
    with np.errstate(divide='ignore'):
        sharpness = float(fft_blur(gray, size=min(30, min(gray.shape) // 4)))
    if not np.isfinite(sharpness):  # a flat frame (lens cap) reconstructs to log(0)
        sharpness = 0.0
    return {
        'sharpness': round(sharpness, 2),
        'brightness': round(float(gray.mean()), 1),
        'clipped': round(100.0 * np.count_nonzero(gray > 250) / total, 2),
        'dark': round(100.0 * np.count_nonzero(gray < 10) / total, 2),
    }


class CaptureQC:
    """Rolling image-quality summary for one session.

    The baseline is the median of the session's first `baseline_n` images, so
    "softer than at the start" works whatever the scene's absolute score is.
    """

    def __init__(self, window=10, baseline_n=10):
        self._recent = deque(maxlen=window)
        self._baseline_n = baseline_n
        self._baseline_samples = []
        self.baseline_sharpness = None
        self.last = None

    def add(self, metrics):
        self.last = metrics
        self._recent.append(metrics)
        if self.baseline_sharpness is None:
            self._baseline_samples.append(metrics['sharpness'])
            if len(self._baseline_samples) >= self._baseline_n:
                self.baseline_sharpness = round(float(np.median(self._baseline_samples)), 2)

    def summary(self):
        if not self._recent:
            return None
        recent = list(self._recent)
        median = {k: round(float(np.median([m[k] for m in recent])), 2)
                  for k in ('sharpness', 'brightness', 'clipped', 'dark')}
        # Last 3 images, oldest first — the controller warns on 3 in a row.
        return {'last': self.last, 'median': median, 'recent_sharpness': [m['sharpness'] for m in recent[-3:]],
                'baseline_sharpness': self.baseline_sharpness, 'count': len(recent)}


def make_thumbnail(frame, width=480, quality=80):
    """Small JPEG of a frame for the controller's gallery, or None."""
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


class ImageRecorder:
    # Class default so recorders built without __init__ (tests, pickled
    # worker copies) still save at the long-standing quality.
    jpeg_quality = 95
    # The gallery shows one thumbnail every couple of seconds, so encoding
    # one per saved image would only cost main-loop time.
    THUMB_INTERVAL_S = 2.0
    _thumb_at = float('-inf')

    def __init__(self, save_directory, mode='whole', max_queue=200, new_process_threshold=90, max_processes=4,
                 jpeg_quality=95):
        self.save_directory = save_directory
        self.mode = mode
        self.jpeg_quality = jpeg_quality
        self.queue = Queue(maxsize=max_queue)
        self.new_process_threshold = new_process_threshold
        self.max_processes = max_processes
        self.processes = []
        self.running = True
        # Shared with the workers: they hold a fork-time copy of `running`,
        # so a stop has to reach them through an Event.
        self._stop_event = Event()
        self.failed = Value('i', 0)
        self._failed_seen = 0
        self._queue_full_logged_at = float('-inf')
        self.logger = LogManager.get_logger(__name__)

        # Main-process view of what was last queued for saving: the gallery
        # thumbnail and the quality summary. Worker processes never touch these.
        self.saved_count = 0
        self.last_thumbnail = None
        self.last_saved = None
        self.qc = CaptureQC()

        self.start_new_process()

    def start_new_process(self):
        if len(self.processes) < self.max_processes:
            p = Process(target=self.save_images)
            p.start()
            self.processes.append(p)
            self.logger.info(f"[INFO] Started new process, total processes: {len(self.processes)}")
        else:
            self.logger.warning("[INFO] Maximum number of processes reached.")

    def save_images(self):
        while True:
            try:
                frame, frame_id, boxes, centres, gps_data, camera_metadata, context, capture_time, quality = \
                    self.queue.get(timeout=0.5)
            except Empty:
                if self._stop_event.is_set():
                    break
                continue
            except KeyboardInterrupt:
                self.logger.info("[INFO] KeyboardInterrupt received in save_images. Exiting.")
                break

            # Workers hold the recorder as it was at fork time, so a live
            # jpeg_quality change only reaches them with each frame.
            self.jpeg_quality = quality
            # One bad frame (drive pulled, I/O error, empty crop) used to kill
            # the worker, and saving then stopped with nobody told. Count it
            # for the main process (add_frame reports it) and carry on.
            try:
                self.process_frame(frame, frame_id, boxes, centres, gps_data,
                                   camera_metadata, context, capture_time)
            except Exception as e:
                logger.error(f"Image {frame_id} not saved: {e}", exc_info=True)
                with self.failed.get_lock():
                    self.failed.value += 1

    def process_frame(self, frame, frame_id, boxes, centres, gps_data,
                      camera_metadata=None, context=None, capture_time=None):
        if capture_time is None:
            capture_time = datetime.now(timezone.utc)
        timestamp = capture_time.strftime('%Y-%m-%dT%H%M%S.%f')[:-3] + 'Z'

        source_height, source_width = frame.shape[:2]
        base_context = dict(context or {})
        base_context['source_width'] = source_width
        base_context['source_height'] = source_height

        def exif_for(x, y, width, height):
            """EXIF for one saved region, carrying where it came from.

            A bbox or square crop is otherwise untraceable: nothing in the file
            says which part of which frame it is, so it cannot be re-labelled
            against the original or checked for context.
            """
            region = dict(base_context, roi_x=x, roi_y=y,
                          roi_width=width, roi_height=height)
            return build_exif_bytes(gps_data=gps_data,
                                    camera_metadata=camera_metadata,
                                    context=region,
                                    capture_time=capture_time)

        if self.mode == 'whole':
            files = self.save_frame(frame, frame_id, timestamp, exif_for)
        elif self.mode == 'bbox':
            files = self.save_bboxes(frame, frame_id, boxes, timestamp, exif_for)
        elif self.mode == 'square':
            files = self.save_squares(frame, frame_id, centres, timestamp, exif_for)
        else:
            files = []

        if files and gps_data and gps_data.get('latitude') is not None \
                and gps_data.get('longitude') is not None:
            self._append_location(frame_id, gps_data, capture_time, files)

    def _append_location(self, frame_id, gps_data, capture_time, files):
        """Append one line to the per-session location sidecar
        (locations.jsonl); all of a frame's crops share the one entry.
        The Map tab and GeoJSON route read this instead of re-scanning EXIF.

        Only called when a GPS fix was present (fail-safe — no line beats a
        fake one). Workers are separate processes, so there is no shared
        lock: each entry is a single short write() to an O_APPEND handle,
        which POSIX keeps atomic per line. Readers skip malformed/torn
        lines. A sidecar failure must never fail the image save.
        """
        try:
            entry = {
                # Full ISO-8601 UTC (with colons) — distinct from the filename stamp
                'ts': capture_time.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
                'frame_id': frame_id,
                'lat': float(gps_data['latitude']),
                'lon': float(gps_data['longitude']),
            }
            for key in ('accuracy', 'altitude', 'speed_kmh', 'heading', 'source'):
                if gps_data.get(key) is not None:
                    entry[key] = gps_data[key]
            entry['files'] = files
            path = os.path.join(self.save_directory, 'locations.jsonl')
            with open(path, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            # Module logger: worker processes may hold a bare recorder
            logger.warning(f"Failed to append location sidecar entry: {e}")

    def save_frame(self, frame, frame_id, timestamp, exif_for):
        height, width = frame.shape[:2]
        filename = f"{timestamp}_frame_{frame_id}.jpg"
        filepath = os.path.join(self.save_directory, filename)
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        image_bytes = encode_jpeg(image, exif_for(0, 0, width, height), quality=self.jpeg_quality)
        with open(filepath, 'wb') as f:
            f.write(image_bytes)
        return [filename]

    def save_bboxes(self, frame, frame_id, boxes, timestamp, exif_for):
        filenames = []
        for contour_id, box in enumerate(boxes):
            startX, startY, width, height = box
            cropped_image = frame[startY:startY+height, startX:startX+width]
            filename = f"{timestamp}_frame_{frame_id}_n_{str(contour_id)}.jpg"
            filepath = os.path.join(self.save_directory, filename)
            image = Image.fromarray(cv2.cvtColor(cropped_image, cv2.COLOR_BGR2RGB))
            image_bytes = encode_jpeg(image, exif_for(startX, startY, width, height), quality=self.jpeg_quality)
            with open(filepath, 'wb') as f:
                f.write(image_bytes)
            filenames.append(filename)
        return filenames

    def save_squares(self, frame, frame_id, centres, timestamp, exif_for):
        filenames = []
        side_length = min(200, frame.shape[0])
        halfLength = side_length // 2
        for contour_id, centre in enumerate(centres):
            # Clamp the origin so the square always fits, then derive the end
            # from it. The old code adjusted startX/startY on overflow but left
            # endX/endY at their pre-clamp values, so the slice ran past the
            # frame edge and numpy silently returned a differently-sized crop.
            startX = min(max(centre[0] - np.random.randint(10, halfLength), 0),
                         max(frame.shape[1] - side_length, 0))
            startY = min(max(centre[1] - np.random.randint(10, halfLength), 0),
                         max(frame.shape[0] - side_length, 0))
            endX = startX + side_length
            endY = startY + side_length
            square_image = frame[startY:endY, startX:endX]
            filename = f"{timestamp}_frame_{frame_id}_n_{str(contour_id)}.jpg"
            filepath = os.path.join(self.save_directory, filename)
            image = Image.fromarray(cv2.cvtColor(square_image, cv2.COLOR_BGR2RGB))
            image_bytes = encode_jpeg(image, exif_for(startX, startY,
                                                      endX - startX, endY - startY),
                                      quality=self.jpeg_quality)
            with open(filepath, 'wb') as f:
                f.write(image_bytes)
            filenames.append(filename)
        return filenames

    def add_frame(self, frame, frame_id, boxes, centres, gps_data=None,
                  camera_metadata=None, context=None):
        # Stamp capture time here, not at dequeue — frames can sit in the queue
        capture_time = datetime.now(timezone.utc)
        if not self.queue.full():
            self.queue.put((frame, frame_id, boxes, centres, gps_data,
                            camera_metadata, context, capture_time, self.jpeg_quality))
            now = time.monotonic()
            thumbnail = now - self._thumb_at >= self.THUMB_INTERVAL_S
            if thumbnail:
                self._thumb_at = now
            self.note_preview(frame, gps_data, capture_time, saved=True, thumbnail=thumbnail)
        else:
            now = time.monotonic()
            if now - self._queue_full_logged_at >= 10:
                self._queue_full_logged_at = now
                self.logger.warning("Image queue full - frames are being skipped")

        failed = self.failed.value
        if failed != self._failed_seen:
            self._failed_seen = failed
            self.logger.error(f"Image saving failed for {failed} image(s) this session "
                              f"- check the recording drive")

        if self.queue.qsize() > self.new_process_threshold and len(self.processes) < self.max_processes:
            self.start_new_process()

    def note_preview(self, frame, gps_data=None, capture_time=None, saved=False, thumbnail=True):
        """Refresh the quality summary, and the gallery thumbnail when
        `thumbnail` is set, from a frame.

        Called for every queued image, and by video mode for frames that are
        recorded into a clip rather than saved as files. Auxiliary: a failure
        here must never cost the image.
        """
        try:
            self.qc.add(image_quality(frame))
            if thumbnail:
                self.last_thumbnail = make_thumbnail(frame)
            if saved:
                self.saved_count += 1
            capture_time = capture_time or datetime.now(timezone.utc)
            info = {'count': self.saved_count,
                    'ts': capture_time.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'}
            if gps_data and gps_data.get('latitude') is not None and gps_data.get('longitude') is not None:
                info['lat'] = float(gps_data['latitude'])
                info['lon'] = float(gps_data['longitude'])
            self.last_saved = info
        except Exception as e:
            self.logger.warning(f"Preview/quality update failed (image unaffected): {e}")

    def stop(self, wait=True):
        """Stop the workers once they have saved what is queued.

        wait=False returns at once and finishes in the background, for calls
        from the detection loop: joining the workers there cost >= 1 s of
        spraying per recording start, and the queued frames were thrown away.
        """
        self.running = False
        self._stop_event.set()
        if wait:
            self._join_workers()
        else:
            threading.Thread(target=self._join_workers, daemon=True).start()

    def _join_workers(self):
        # Generous: the workers are saving what was queued, and a frame lost
        # at shutdown is worse than a few seconds (systemd allows 90 s).
        for p in self.processes:
            try:
                p.join(timeout=10)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=0.5)
            except Exception as e:
                self.logger.error(f"Failed to stop process: {e}")

        self.processes = []
        self.queue.close()
        self.logger.info("[INFO] ImageRecorder stopped.")

    def terminate(self):
        """Force terminate all image recording processes."""
        self.running = False
        for p in self.processes:
            if p.is_alive():
                try:
                    p.terminate()
                    p.join(timeout=0.5)
                except Exception as e:
                    self.logger.error(f"Failed to terminate process: {e}")

        self.processes.clear()
        self.queue.close()
        self.queue.join_thread()
        self.logger.info("[INFO] All recording processes terminated forcefully.")
