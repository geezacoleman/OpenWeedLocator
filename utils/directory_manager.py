from datetime import datetime
import utils.error_manager as errors
import platform
import re
import time
import os

from utils.log_manager import LogManager


# Shared constants for session scanning
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')
_DATE_PATTERN = re.compile(r'^\d{8}$')
_SESSION_PATTERN = re.compile(r'^session_\d{6}$')


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

    if '/' in session_id:
        # Specific session: YYYYMMDD/session_HHMMSS
        for f in os.listdir(target):
            fp = os.path.join(target, f)
            if os.path.isfile(fp) and f.lower().endswith(IMAGE_EXTENSIONS):
                files.append((f'images/{f}', fp))
    else:
        # Date-level: collect from session subdirs AND flat files
        for entry in os.listdir(target):
            entry_path = os.path.join(target, entry)
            if os.path.isdir(entry_path) and _SESSION_PATTERN.match(entry):
                # Session subdir — include subdir name in archive path
                for f in os.listdir(entry_path):
                    fp = os.path.join(entry_path, f)
                    if os.path.isfile(fp) and f.lower().endswith(IMAGE_EXTENSIONS):
                        files.append((f'images/{entry}/{f}', fp))
            elif os.path.isfile(entry_path) and entry.lower().endswith(IMAGE_EXTENSIONS):
                # Legacy flat file
                files.append((f'images/{entry}', entry_path))

    return files


class DirectorySetup:
    def _is_raspberry_pi(self):
        try:
            with open('/proc/device-tree/model', 'r') as f:
                return 'raspberry pi' in f.read().lower()
        except Exception:
            return False

    def __init__(self, save_directory):
        self.logger = LogManager.get_logger(__name__)
        self.save_directory = save_directory
        self.save_subdirectory = None

    def setup_directories(self, max_retries=5, retry_delay=2):
        for attempt in range(max_retries):
            try:
                return self._try_setup_directories()
            except (errors.USBMountError, errors.USBWriteError, errors.NoWritableUSBError) as e:
                self.logger.info(f"[INFO] Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)

        raise errors.NoWritableUSBError()

    def _try_setup_directories(self):
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))

        if os.path.ismount(self.save_directory):
            os.makedirs(self.save_subdirectory, exist_ok=True)
            if not self.test_file_write():
                raise errors.USBWriteError("Failed to write test file")
            self.logger.info(f"[SUCCESS] Directory setup complete: {self.save_subdirectory}")
            return self.save_directory, self.save_subdirectory

        # Non-Raspberry Linux boards (e.g., Orange Pi) may use local storage instead of /media USB mounts.
        # Keep strict USB-mount behavior on Raspberry Pi, but allow local writable directories elsewhere.
        if platform.system() == 'Linux' and not self._is_raspberry_pi():
            os.makedirs(self.save_subdirectory, exist_ok=True)
            if not self.test_file_write():
                raise errors.USBWriteError("Failed to write test file")
            self.logger.info(f"[SUCCESS] Local directory setup complete: {self.save_subdirectory}")
            return self.save_directory, self.save_subdirectory

        return self._handle_mount_error()

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