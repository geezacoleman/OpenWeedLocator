from datetime import datetime
import time
import os


class USBMountError(Exception):
    pass


class USBWriteError(Exception):
    pass


class NoWritableUSBError(Exception):
    pass


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

        raise NoWritableUSBError("Failed to set up directories after multiple attempts.")

    def _try_setup_directories(self):
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
        if not os.path.ismount(self.save_directory):
            return self._handle_mount_error()

        os.makedirs(self.save_subdirectory, exist_ok=True)
        if not self.test_file_write():
            raise USBWriteError("Failed to write test file")

        print(f"[SUCCESS] Directory setup complete: {self.save_subdirectory}")
        return self.save_subdirectory

    def _handle_mount_error(self):
        media_dir = '/media'
        for username in os.listdir(media_dir):
            user_media_dir = os.path.join(media_dir, username)
            if os.path.isdir(user_media_dir):
                for drive in os.listdir(user_media_dir):
                    drive_path = os.path.join(user_media_dir, drive)
                    if os.path.ismount(drive_path):
                        self.save_directory = drive_path
                        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
                        try:
                            os.makedirs(self.save_subdirectory, exist_ok=True)
                            if self.test_file_write():
                                print(f'[SUCCESS] Connected to {drive_path} and it is writable.')
                                return self.save_subdirectory
                            else:
                                print(f'[ERROR] {drive_path} is connected but not writable.')
                        except PermissionError:
                            print(f'[ERROR] Failed to access {drive_path}')

        raise NoWritableUSBError("No writable USB drives found.")

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