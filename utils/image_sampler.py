import cv2
import json
import os
import numpy as np
from datetime import datetime, timezone
from multiprocessing import Process, Queue
from multiprocessing.queues import Empty
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
        if context:  # device_id, algorithm, model, session metadata
            desc = json.dumps(context)
            if len(desc) <= _MAX_EXIF_JSON:
                zeroth[piexif.ImageIFD.ImageDescription] = desc
            else:
                logger.warning(f"EXIF ImageDescription too large ({len(desc)} bytes); omitted")

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
            meta_json = json.dumps(camera_metadata, default=str)
            if len(meta_json) <= _MAX_EXIF_JSON:
                exif_ifd[piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(meta_json)
            else:
                logger.warning(f"EXIF UserComment too large ({len(meta_json)} bytes); omitted")

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


class ImageRecorder:
    def __init__(self, save_directory, mode='whole', max_queue=200, new_process_threshold=90, max_processes=4):
        self.save_directory = save_directory
        self.mode = mode
        self.queue = Queue(maxsize=max_queue)
        self.new_process_threshold = new_process_threshold
        self.max_processes = max_processes
        self.processes = []
        self.running = True
        self.logger = LogManager.get_logger(__name__)

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
        while self.running or not self.queue.empty():
            try:
                frame, frame_id, boxes, centres, gps_data, camera_metadata, context, capture_time = \
                    self.queue.get(timeout=3)
            except Empty:
                if not self.running:
                    break
                continue
            except KeyboardInterrupt:
                self.logger.info("[INFO] KeyboardInterrupt received in save_images. Exiting.")
                break

            self.process_frame(frame, frame_id, boxes, centres, gps_data,
                               camera_metadata, context, capture_time)

    def process_frame(self, frame, frame_id, boxes, centres, gps_data,
                      camera_metadata=None, context=None, capture_time=None):
        if capture_time is None:
            capture_time = datetime.now(timezone.utc)
        timestamp = capture_time.strftime('%Y-%m-%dT%H%M%S.%f')[:-3] + 'Z'
        exif_bytes = build_exif_bytes(gps_data=gps_data,
                                      camera_metadata=camera_metadata,
                                      context=context,
                                      capture_time=capture_time)
        if self.mode == 'whole':
            files = self.save_frame(frame, frame_id, timestamp, exif_bytes)
        elif self.mode == 'bbox':
            files = self.save_bboxes(frame, frame_id, boxes, timestamp, exif_bytes)
        elif self.mode == 'square':
            files = self.save_squares(frame, frame_id, centres, timestamp, exif_bytes)
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

    def save_frame(self, frame, frame_id, timestamp, exif_bytes):
        filename = f"{timestamp}_frame_{frame_id}.jpg"
        filepath = os.path.join(self.save_directory, filename)
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        image_bytes = encode_jpeg(image, exif_bytes)
        with open(filepath, 'wb') as f:
            f.write(image_bytes)
        return [filename]

    def save_bboxes(self, frame, frame_id, boxes, timestamp, exif_bytes):
        filenames = []
        for contour_id, box in enumerate(boxes):
            startX, startY, width, height = box
            cropped_image = frame[startY:startY+height, startX:startX+width]
            filename = f"{timestamp}_frame_{frame_id}_n_{str(contour_id)}.jpg"
            filepath = os.path.join(self.save_directory, filename)
            image = Image.fromarray(cv2.cvtColor(cropped_image, cv2.COLOR_BGR2RGB))
            image_bytes = encode_jpeg(image, exif_bytes)
            with open(filepath, 'wb') as f:
                f.write(image_bytes)
            filenames.append(filename)
        return filenames

    def save_squares(self, frame, frame_id, centres, timestamp, exif_bytes):
        filenames = []
        side_length = min(200, frame.shape[0])
        halfLength = side_length // 2
        for contour_id, centre in enumerate(centres):
            startX = max(centre[0] - np.random.randint(10, halfLength), 0)
            startY = max(centre[1] - np.random.randint(10, halfLength), 0)
            endX = startX + side_length
            endY = startY + side_length
            if endX > frame.shape[1]:
                startX = frame.shape[1] - side_length
            if endY > frame.shape[0]:
                startY = frame.shape[0] - side_length
            square_image = frame[startY:endY, startX:endX]
            filename = f"{timestamp}_frame_{frame_id}_n_{str(contour_id)}.jpg"
            filepath = os.path.join(self.save_directory, filename)
            image = Image.fromarray(cv2.cvtColor(square_image, cv2.COLOR_BGR2RGB))
            image_bytes = encode_jpeg(image, exif_bytes)
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
                            camera_metadata, context, capture_time))
        else:
            self.logger.info("[INFO] Queue is full, spinning up new process. Frame skipped.")

        if self.queue.qsize() > self.new_process_threshold and len(self.processes) < self.max_processes:
            self.start_new_process()

    def stop(self):
        """Stop image recording processes and clean up resources."""
        self.running = False

        try:
            while not self.queue.empty():
                self.queue.get_nowait()
        except Exception as e:
            self.logger.warning(f"Failed to clear queue: {e}")

        self.queue.close()
        self.queue.join_thread()

        for p in self.processes:
            try:
                p.join(timeout=1)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=0.5)
            except Exception as e:
                self.logger.error(f"Failed to stop process: {e}")

        self.processes.clear()
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
