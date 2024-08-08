from datetime import datetime
import os


class DirectorySetup:
    def __init__(self, save_directory):
        self.save_directory = save_directory
        self.save_subdirectory = None

    def setup_directories(self):
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
        try:
            if os.path.ismount(self.save_directory):
                os.makedirs(self.save_subdirectory, exist_ok=True)
            else:
                raise PermissionError("[PERMISSION ERROR] Not a mount point")

            if self.test_file_write():
                print(f"[SUCCESS] Directory setup complete: {self.save_subdirectory}")
                return self.save_subdirectory
            else:
                raise PermissionError("Failed to write test file")

        except PermissionError:
            return self._handle_permission_error()

    def _handle_permission_error(self):
        try:
            username = os.listdir('/media/')[0]
            usb_drives = os.listdir(os.path.join('/media', username))
            for drive in usb_drives:
                try:
                    self.save_directory = os.path.join('/media', username, drive)
                    self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
                    os.makedirs(self.save_subdirectory, exist_ok=True)

                    if self.test_file_write():
                        print(f'[SUCCESS] Tried {drive}. Connected and writable.')
                        return self.save_subdirectory
                    else:
                        print(f'[ERROR] Tried {drive}. Connected but not writable.')

                except PermissionError:
                    print(f'[ERROR] Tried {drive}. Failed')

            print("[ERROR] No writable USB drives found.")
            return None

        except Exception as e:
            print(f"\n[USB ERROR] Error accessing USB drives.\nError message: {e}")
            return None

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