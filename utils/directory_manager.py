from datetime import datetime
from utils.error_manager import USBMountError, NoWritableUSBError, USBWriteError
import platform
import logging
import time
import os


class DirectorySetup:
    def __init__(self, save_directory):
        self.save_directory = save_directory
        self.save_subdirectory = None

    def setup_directories(self, max_retries=5, retry_delay=2):
        for attempt in range(max_retries):
            try:
                return self._try_setup_directories()
            except (USBMountError, USBWriteError, NoWritableUSBError) as e:
                print(f"[INFO] Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)

        raise NoWritableUSBError()

    def _try_setup_directories(self):
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
        if not os.path.ismount(self.save_directory):
            return self._handle_mount_error()

        os.makedirs(self.save_subdirectory, exist_ok=True)
        if not self.test_file_write():
            raise USBWriteError("Failed to write test file")

        print(f"[SUCCESS] Directory setup complete: {self.save_subdirectory}")
        return self.save_directory, self.save_subdirectory

    def _handle_mount_error(self):
        """
        Handle USB mount errors on Raspberry Pi systems.
        Searches /media directory for mounted, writable USB drives.

        Returns:
            Tuple[str, str]: (save_directory, save_subdirectory) if found

        Raises:
            NoWritableUSBError: If no writable USB drive is found
            RuntimeError: If not running on Linux/Raspberry Pi
        """
        if platform.system() != 'Linux':
            raise RuntimeError(
                "USB directory handling is only supported on Linux/Raspberry Pi. "
                "If testing on another system, specify a valid directory path."
            )

        media_dir = '/media'
        mounted_drives = self._find_mounted_drives(media_dir)

        for drive_path in mounted_drives:
            if self._try_setup_drive(drive_path):
                return self.save_directory, self.save_subdirectory

        raise NoWritableUSBError("No writable USB drives found.")

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
            logging.error(f"Error accessing media directory: {e}")

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
                logging.info(f'Connected to {drive_path} and it is writable.')
                return True
            logging.error(f'{drive_path} is connected but not writable.')
        except PermissionError:
            logging.error(f'Failed to access {drive_path}')

        return False

    def test_file_write(self):
        test_file_path = os.path.join(self.save_subdirectory, 'test_write.txt')
        try:
            with open(test_file_path, 'w') as f:
                f.write('Test write successful')
            os.remove(test_file_path)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to write test file: {e}")
            return False