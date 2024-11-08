from datetime import datetime
import utils.error_manager as errors
import platform
import time
import os

from utils.log_manager import LogManager


class DirectorySetup:
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
        if not os.path.ismount(self.save_directory):
            return self._handle_mount_error()

        os.makedirs(self.save_subdirectory, exist_ok=True)
        if not self.test_file_write():
            raise errors.USBWriteError("Failed to write test file")

        self.logger.info(f"[SUCCESS] Directory setup complete: {self.save_subdirectory}")
        return self.save_directory, self.save_subdirectory

    def _handle_mount_error(self):
        """
        Handle USB mount errors on Raspberry Pi systems.
        Searches /media directory for mounted, writable USB drives.
        """
        if platform.system() != 'Linux':
            raise errors.StorageSystemError(platform=platform.system())

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