import os
import platform
from datetime import datetime

class DirectoryManager:
    def __init__(self, save_directory):
        self.save_directory = save_directory
        self.save_subdirectory = None
        self.system_platform = platform.system()

    def setup_directories(self, enable_device_save=False):
        self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))

        # Attempt to create directory in the primary save location
        if self.try_create_directory(self.save_directory):
            return self.save_subdirectory

        # If enable_device_save is True, attempt to use USB drives
        if not enable_device_save:
            usb_save_directory = self.find_usb_directory()
            if usb_save_directory:
                self.save_directory = usb_save_directory
                self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
                if self.try_create_directory(self.save_directory):
                    return self.save_subdirectory

        print("[ERROR] Could not create save directory.")
        return None

    def try_create_directory(self, directory):
        try:
            if self.system_platform == 'Linux':
                if os.path.ismount(directory):
                    os.makedirs(self.save_subdirectory, exist_ok=True)
                    return True

            elif self.system_platform == 'Windows':
                if os.path.exists(directory):
                    os.makedirs(self.save_subdirectory, exist_ok=True)
                    return True

        except PermissionError:
            print(f"[ERROR] Permission denied for directory: {directory}")

        except Exception as e:
            print(f"[ERROR] Failed to create directory {directory}: {e}")

        return False

    def find_usb_directory(self):
        if self.system_platform == 'Linux':
            return self._find_usb_directory_linux()
        elif self.system_platform == 'Windows':
            print('[INFO] saving to drive specified in config file.')
            return self._find_usb_directory_windows()
        else:
            print("[ERROR] Unsupported platform.")
            return None

    def _find_usb_directory_linux(self):
        try:
            username = os.listdir('/media/')[0]
            usb_drives = os.listdir(os.path.join('/media', username))
            for drive in usb_drives:
                drive_path = os.path.join('/media', username, drive)
                if os.path.ismount(drive_path):
                    print(f"[SUCCESS] Found USB drive: {drive}")
                    return drive_path
                else:
                    print(f"[ERROR] USB drive {drive} is not mounted.")
        except Exception as e:
            print(f"[USB ERROR] Error finding USB drives: {e}")
        return None

    def _find_usb_directory_windows(self):
        try:
            print('here333')
            try:
                import win32com.client
            except Exception as e:
                print(e)

            print('here111')
            wmi = win32com.client.GetObject("winmgmts:")
            for usb in wmi.InstancesOf("Win32_LogicalDisk"):
                print(usb)
                if usb.Description == "Removable Disk":
                    print(f"[SUCCESS] Found USB drive: {usb.DeviceID}")
                    return f"{usb.DeviceID}\\"

        except Exception as e:
            print(f"[USB ERROR] Error finding USB drives: {e}")

        return None

    def test_save_file(self):
        try:
            test_file_path = os.path.join(self.save_subdirectory, "test_file.txt")
            with open(test_file_path, "w") as f:
                f.write("This is a test file.")
            os.remove(test_file_path)
            return True
        except Exception as e:
            print(f"[ERROR] Failed to save test file: {e}")
            return False
