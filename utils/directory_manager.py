from datetime import datetime
import utils.error_manager as errors
import json
import platform
import re
import shutil
import time
import os
import zipfile

from utils.log_manager import LogManager


GB = 1024 ** 3

# Shared constants for session scanning
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')
_DATE_PATTERN = re.compile(r'^\d{8}$')
_SESSION_PATTERN = re.compile(r'^session_\d{6}$')


def _is_session_metadata_file(filename):
    """Location sidecar / GPS track files that ride along with a session's
    images (included in ZIP downloads, excluded from image listings)."""
    return (filename == 'locations.jsonl'
            or (filename.startswith('track_') and filename.endswith('.geojson')))


def scan_sessions(save_dir):
    """Scan save_dir for recording sessions.

    Supports two directory structures:
    - New: save_dir/YYYYMMDD/session_HHMMSS/ (per-recording sessions)
    - Legacy: save_dir/YYYYMMDD/ (flat, all images in date dir)

    Returns list of session dicts sorted newest-first, each with:
        session_id, date, time, image_count, total_size
    """
    if not save_dir or not os.path.isdir(save_dir):
        return []

    sessions = []

    for date_entry in sorted(os.listdir(save_dir), reverse=True):
        date_path = os.path.join(save_dir, date_entry)
        if not os.path.isdir(date_path) or not _DATE_PATTERN.match(date_entry):
            continue

        # Check for session subdirectories (new structure)
        subdirs = [d for d in os.listdir(date_path)
                   if os.path.isdir(os.path.join(date_path, d)) and _SESSION_PATTERN.match(d)]

        if subdirs:
            for sess in sorted(subdirs, reverse=True):
                sess_path = os.path.join(date_path, sess)
                image_files = [f for f in os.listdir(sess_path)
                               if f.lower().endswith(IMAGE_EXTENSIONS)]
                image_size = sum(
                    os.path.getsize(os.path.join(sess_path, f))
                    for f in image_files
                )
                sessions.append({
                    'session_id': f"{date_entry}/{sess}",
                    'date': date_entry,
                    'time': sess.replace('session_', ''),
                    'image_count': len(image_files),
                    'image_size': image_size,
                    'total_size': image_size,
                })
        else:
            # Legacy structure: images directly in YYYYMMDD dir
            image_files = [f for f in os.listdir(date_path)
                           if f.lower().endswith(IMAGE_EXTENSIONS)]
            if image_files:
                image_size = sum(
                    os.path.getsize(os.path.join(date_path, f))
                    for f in image_files
                )
                sessions.append({
                    'session_id': date_entry,
                    'date': date_entry,
                    'time': '',
                    'image_count': len(image_files),
                    'image_size': image_size,
                    'total_size': image_size,
                })

    return sessions


def collect_session_files(save_dir, session_id):
    """Collect all image files for a session, ready for zipping.

    session_id can be:
    - "YYYYMMDD" — all images under that date (legacy flat + all session subdirs)
    - "YYYYMMDD/session_HHMMSS" — specific session only

    Returns list of (archive_name, full_path) tuples.
    """
    if not save_dir or not session_id:
        return []

    # Validate format
    if not re.match(r'^\d{8}(/session_\d{6})?$', session_id):
        return []

    target = os.path.join(save_dir, session_id)
    if not os.path.isdir(target):
        return []

    files = []

    def _wanted(name):
        return name.lower().endswith(IMAGE_EXTENSIONS) or _is_session_metadata_file(name)

    if '/' in session_id:
        # Specific session: YYYYMMDD/session_HHMMSS
        for f in os.listdir(target):
            fp = os.path.join(target, f)
            if os.path.isfile(fp) and _wanted(f):
                files.append((f'images/{f}', fp))
    else:
        # Date-level: collect from session subdirs AND flat files
        for entry in os.listdir(target):
            entry_path = os.path.join(target, entry)
            if os.path.isdir(entry_path) and _SESSION_PATTERN.match(entry):
                # Session subdir — include subdir name in archive path
                for f in os.listdir(entry_path):
                    fp = os.path.join(entry_path, f)
                    if os.path.isfile(fp) and _wanted(f):
                        files.append((f'images/{entry}/{f}', fp))
            elif os.path.isfile(entry_path) and entry.lower().endswith(IMAGE_EXTENSIONS):
                # Legacy flat file
                files.append((f'images/{entry}', entry_path))

    return files


def select_preview_images(save_dir, session_id, count):
    """Pick up to `count` evenly-spaced image paths from a session.

    First and last frames are always included. For a single session subdir the
    archive names carry ISO timestamps, so sorting them is chronological. For a
    whole-date selection that mixes legacy flat images with session subdirs the
    ordering is by archive path (flat frames sort before session_* frames), which
    is stable but only approximately chronological — acceptable for evenly-spaced
    preview thumbnails. Returns a list of absolute paths (may be shorter than
    `count` for small sessions).
    """
    if count <= 0:
        return []

    files = collect_session_files(save_dir, session_id)
    # Images only — collect_session_files also carries sidecar/track files
    paths = [fp for _, fp in sorted(files)
             if fp.lower().endswith(IMAGE_EXTENSIONS)]

    if len(paths) <= count:
        return paths
    if count == 1:
        return [paths[0]]

    n = len(paths)
    indices = [round(i * (n - 1) / (count - 1)) for i in range(count)]
    return [paths[i] for i in indices]


class _ZipStreamBuffer:
    """Write-only sink for zipfile that a generator drains in chunks.

    Deliberately has NO tell()/seek(): zipfile then treats the output as
    unseekable and writes data-descriptor members instead of seeking back
    to patch headers (impossible on drained bytes). Peak memory is one
    archive member, never the whole ZIP — multi-GB session ZIPs must not
    touch /tmp (tmpfs on Trixie) or RAM in full.
    """

    def __init__(self):
        self._chunks = []

    def write(self, data):
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def drain(self):
        chunks, self._chunks = self._chunks, []
        return b''.join(chunks)


def stream_zip(file_pairs):
    """Yield the bytes of a ZIP_STORED archive of (arcname, path) pairs.

    Stored, not deflated: the payload is JPEGs (already compressed), so this
    stays pure I/O and the client can estimate progress from the raw sizes.
    Files that vanish mid-stream (unlikely — deleting the recording session
    is refused) are skipped rather than corrupting the archive.
    """
    buffer = _ZipStreamBuffer()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED, allowZip64=True) as archive:
        for arcname, path in file_pairs:
            try:
                archive.write(path, arcname)
            except OSError:
                continue
            data = buffer.drain()
            if data:
                yield data
    tail = buffer.drain()
    if tail:
        yield tail


def _downsample_keep_ends(items, target):
    """Even-stride downsample that always keeps the first and last items."""
    if len(items) <= target:
        return list(items)
    if target < 2:
        return list(items[:target])
    step = (len(items) - 1) / (target - 1)
    return [items[round(i * step)] for i in range(target)]


def read_session_locations(session_path, max_features=2000):
    """Parse a session's locations.jsonl sidecar into GeoJSON Point features.

    Returns (features, total, truncated), or None when the session has no
    sidecar at all (caller falls back to an EXIF scan). Malformed or
    coordinate-less lines — including a torn final line from a mid-write
    power cut — are skipped, never fatal.
    """
    path = os.path.join(session_path, 'locations.jsonl')
    if not os.path.isfile(path):
        return None

    features = []
    try:
        with open(path, 'r') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    lat = float(entry['lat'])
                    lon = float(entry['lon'])
                except (ValueError, KeyError, TypeError):
                    continue
                props = {k: entry[k]
                         for k in ('ts', 'frame_id', 'files', 'accuracy',
                                   'speed_kmh', 'heading', 'source')
                         if k in entry}
                features.append({
                    'type': 'Feature',
                    'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                    'properties': props,
                })
    except OSError:
        return None

    total = len(features)
    truncated = total > max_features
    if truncated:
        features = _downsample_keep_ends(features, max_features)
    return features, total, truncated


def _exif_dms_to_decimal(rationals, ref):
    """EXIF GPS rationals + hemisphere ref -> decimal degrees, or None.

    Inverse of image_sampler._decimal_to_dms; tolerates 2- or 3-part DMS.
    """
    try:
        degrees = rationals[0][0] / rationals[0][1]
        minutes = rationals[1][0] / rationals[1][1]
        seconds = 0.0
        if len(rationals) > 2 and rationals[2][1]:
            seconds = rationals[2][0] / rationals[2][1]
        value = degrees + minutes / 60.0 + seconds / 3600.0
        if isinstance(ref, bytes):
            ref = ref.decode('ascii', 'ignore')
        if ref in ('S', 'W'):
            value = -value
        return value
    except (TypeError, ZeroDivisionError, IndexError, KeyError):
        return None


def scan_exif_locations(session_path, max_files=500):
    """Legacy fallback: pull GPS points from a session's JPEG EXIF.

    Prefers whole-frame files (``*_frame_N.jpg`` without ``_n_``); a
    crops-only session (bbox/square modes) is deduplicated to one file per
    frame by the prefix before ``_n_``. Capped at max_files because each
    file costs a piexif.load on the Pi. Returns (features, total, truncated);
    `total` counts candidate frames, features only those with GPS.
    """
    import piexif

    try:
        entries = [f for f in os.listdir(session_path)
                   if f.lower().endswith(('.jpg', '.jpeg'))]
    except OSError:
        return [], 0, False

    frames = sorted(f for f in entries if '_frame_' in f and '_n_' not in f)
    if not frames:
        by_frame = {}
        for f in sorted(entries):
            by_frame.setdefault(f.split('_n_')[0], f)
        frames = sorted(by_frame.values())

    total = len(frames)
    truncated = total > max_files
    frames = frames[:max_files]

    features = []
    for fname in frames:
        try:
            exif = piexif.load(os.path.join(session_path, fname))
        except Exception:
            continue
        gps = exif.get('GPS') or {}
        lat = _exif_dms_to_decimal(gps.get(piexif.GPSIFD.GPSLatitude),
                                   gps.get(piexif.GPSIFD.GPSLatitudeRef))
        lon = _exif_dms_to_decimal(gps.get(piexif.GPSIFD.GPSLongitude),
                                   gps.get(piexif.GPSIFD.GPSLongitudeRef))
        if lat is None or lon is None:
            continue
        props = {'files': [fname]}
        dto = (exif.get('Exif') or {}).get(piexif.ExifIFD.DateTimeOriginal)
        if dto:
            if isinstance(dto, bytes):
                dto = dto.decode('ascii', 'ignore')
            # 'YYYY:MM:DD HH:MM:SS' -> ISO, LOCAL time (no Z — EXIF
            # DateTimeOriginal is local; sidecar ts is UTC with Z)
            props['ts'] = dto.replace(':', '-', 2).replace(' ', 'T')
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
            'properties': props,
        })
    return features, total, truncated


def read_session_tracks(session_path):
    """Return the LineString feature(s) from a session's track_*.geojson.

    Written by TrackRecorder (utils/gps_manager.py) — same format as the
    networked controller's tracks/. Unreadable files are skipped.
    """
    features = []
    try:
        names = sorted(os.listdir(session_path))
    except OSError:
        return features
    for fname in names:
        if not (fname.startswith('track_') and fname.endswith('.geojson')):
            continue
        try:
            with open(os.path.join(session_path, fname), 'r') as fh:
                data = json.load(fh)
            for feat in data.get('features', []):
                if (feat.get('geometry') or {}).get('type') == 'LineString':
                    features.append(feat)
        except (OSError, ValueError):
            continue
    return features


class DirectorySetup:
    def __init__(self, save_directory, storage_location='auto',
                 internal_save_directory=None, min_free_gb=0):
        self.logger = LogManager.get_logger(__name__)
        self.save_directory = save_directory
        self.save_subdirectory = None
        # usb: /media mount required (legacy behaviour)
        # internal: eMMC/SD path, no mount gate (sealed OWL 3.0 units)
        # auto (default): USB when one is mounted, else internal
        # Empty/whitespace values resolve to auto, matching owl.py's fallback —
        # a blank key in an INI must never silently disable the internal path.
        self.storage_location = ((storage_location or '').strip() or 'auto').lower()
        self.internal_save_directory = internal_save_directory
        self.min_free_gb = min_free_gb
        # What setup actually landed on ('usb' or 'internal') — auto mode
        # resolves at runtime; the storage watchdog keys its rule off this.
        self.resolved_location = 'usb'

    def setup_directories(self, max_retries=5, retry_delay=2):
        for attempt in range(max_retries):
            try:
                return self._try_setup_directories()
            except (errors.USBMountError, errors.USBWriteError, errors.NoWritableUSBError) as e:
                if attempt < max_retries - 1:
                    self.logger.info(f"[INFO] Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    self.logger.info(f"[INFO] Attempt {attempt + 1} failed: {str(e)}.")

        raise errors.NoWritableUSBError()

    def _try_setup_directories(self):
        if self.storage_location == 'internal':
            return self._setup_internal()

        try:
            return self._try_setup_usb()
        except (errors.USBMountError, errors.USBWriteError, errors.NoWritableUSBError):
            if self.storage_location == 'auto' and self.internal_save_directory:
                self.logger.info("No writable USB drive; using internal storage (storage_location=auto)")
                return self._setup_internal()
            raise

    def _try_setup_usb(self):
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
        if not os.path.ismount(self.save_directory):
            return self._handle_mount_error()

        os.makedirs(self.save_subdirectory, exist_ok=True)
        if not self.test_file_write():
            raise errors.USBWriteError("Failed to write test file")

        self.logger.info(f"[SUCCESS] Directory setup complete: {self.save_subdirectory}")
        return self.save_directory, self.save_subdirectory

    def _setup_internal(self):
        """Internal (eMMC/SD) recording path: no mount gate, just create and
        write-test the configured directory. Recording is refused below the
        free-space floor here; the status indicator watchdog enforces the
        same floor while a session runs."""
        if not self.internal_save_directory:
            raise errors.StorageSystemError(
                message="storage_location is internal but internal_save_directory is not set")

        self.resolved_location = 'internal'
        self.save_directory = self.internal_save_directory
        self.save_subdirectory = os.path.join(
            self.save_directory, datetime.now().strftime('%Y%m%d'))

        try:
            os.makedirs(self.save_subdirectory, exist_ok=True)
        except OSError as e:
            raise errors.StorageSystemError(
                message=f"Cannot create internal storage directory {self.save_subdirectory}: {e}") from e

        if not self.test_file_write():
            raise errors.StorageSystemError(
                message=f"Internal storage directory {self.save_subdirectory} is not writable")

        if self.min_free_gb:
            free = shutil.disk_usage(self.save_directory).free
            floor = self.min_free_gb * GB
            if free < floor:
                # Distinguish "delete sessions and it works" from "this floor
                # can never be met on this disk" — telling an operator to
                # delete sessions that don't exist reads as a hard ban.
                reclaimable = sum(s['total_size']
                                  for s in scan_sessions(self.save_directory))
                if free + reclaimable < floor:
                    raise errors.StorageSystemError(
                        message=(f"free-space floor unreachable on this disk: floor is "
                                 f"{self.min_free_gb} GB but at most "
                                 f"{(free + reclaimable) / GB:.1f} GB can be freed "
                                 f"({free / GB:.1f} GB free + {reclaimable / GB:.1f} GB in sessions). "
                                 f"Lower min_free_gb in the config to record on this device."))
                raise errors.StorageSystemError(
                    message=(f"Internal storage below the free-space floor: "
                             f"{free / GB:.1f} GB free, floor is {self.min_free_gb} GB. "
                             f"Delete or download sessions to record."))

        self.logger.info(f"[SUCCESS] Internal storage ready: {self.save_subdirectory}")
        return self.save_directory, self.save_subdirectory

    def _handle_mount_error(self):
        """
        Handle USB mount errors on Raspberry Pi systems.
        Searches /media directory for mounted, writable USB drives.
        On non-Linux platforms (Windows/Mac), falls back to a local directory for testing.
        """
        if platform.system() != 'Linux':
            return self._setup_local_fallback()

        media_dir = '/media'
        try:
            mounted_drives = self._find_mounted_drives(media_dir)
        except OSError as e:
            raise errors.USBMountError(device=media_dir) from e

        for drive_path in mounted_drives:
            if self._try_setup_drive(drive_path):
                return self.save_directory, self.save_subdirectory

        raise errors.NoWritableUSBError(searched_paths=[media_dir])

    def _find_mounted_drives(self, media_dir: str) -> list[str]:
        """Find all mounted drives in the media directory."""
        mounted_drives = []

        try:
            for username in os.listdir(media_dir):
                user_media_dir = os.path.join(media_dir, username)
                if not os.path.isdir(user_media_dir):
                    continue

                for drive in os.listdir(user_media_dir):
                    drive_path = os.path.join(user_media_dir, drive)
                    if os.path.ismount(drive_path):
                        mounted_drives.append(drive_path)
        except OSError as e:
            self.logger.error(f"Error accessing media directory: {e}", exc_info=True)

        return mounted_drives

    def _try_setup_drive(self, drive_path: str) -> bool:
        """
        Try to setup a specific drive for writing.

        Returns:
            bool: True if drive is writable and setup successful
        """
        self.save_directory = drive_path
        self.save_subdirectory = os.path.join(
            self.save_directory,
            datetime.now().strftime('%Y%m%d')
        )

        try:
            os.makedirs(self.save_subdirectory, exist_ok=True)
            if self.test_file_write():
                self.logger.info(f'Connected to {drive_path} and it is writable.')
                return True
            self.logger.error(f'{drive_path} is connected but not writable.')
        except PermissionError:
            self.logger.error(f'Failed to access {drive_path}', exc_info=True)

        return False

    def _setup_local_fallback(self):
        """Fall back to a local directory for testing on non-Linux platforms."""
        self.save_directory = os.path.join(os.getcwd(), 'owl_data')
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
        os.makedirs(self.save_subdirectory, exist_ok=True)

        if not self.test_file_write():
            raise errors.USBWriteError(device=self.save_directory)

        self.logger.info(f"[TEST MODE] Non-Linux platform detected. Saving to local directory: {self.save_subdirectory}")
        return self.save_directory, self.save_subdirectory

    def test_file_write(self):
        test_file_path = os.path.join(self.save_subdirectory, 'test_write.txt')
        try:
            with open(test_file_path, 'w') as f:
                f.write('Test write successful')
            os.remove(test_file_path)
            return True
        except Exception as e:
            self.logger.error(f"[ERROR] Failed to write test file: {e}", exc_info=True)
            return False