import os
import subprocess
from datetime import datetime
import time

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
        # Check all devices connected via USB
        usb_devices = self.get_usb_devices()

        if not usb_devices:
            raise USBMountError("No USB drives detected.")

        # Attempt to mount the first available writable USB drive
        for device in usb_devices:
            mount_point = self.mount_usb_device(device)
            if mount_point:
                self.save_directory = mount_point
                self.save_subdirectory = os.path.join(self.save_directory, datetime.now().strftime('%Y%m%d'))
                os.makedirs(self.save_subdirectory, exist_ok=True)

                if self.test_file_write():
                    print(f'[SUCCESS] Connected to {mount_point} and it is writable.')
                    return self.save_subdirectory
                else:
                    print(f'[ERROR] {mount_point} is connected but not writable.')

        raise NoWritableUSBError("No writable USB drives found.")

    def get_usb_devices(self):
        # Use 'lsblk' to list block devices and filter USB devices
        result = subprocess.run(['lsblk', '-o', 'NAME,TRAN'], capture_output=True, text=True)
        devices = []
        for line in result.stdout.splitlines():
            if 'usb' in line:
                device_name = line.split()[0]
                devices.append(f'/dev/{device_name}')
        return devices

    def mount_usb_device(self, device):
        # Mount the USB device to /media
        mount_point = f'/media/usb_{device.split("/")[-1]}'
        try:
            if not os.path.ismount(mount_point):
                os.makedirs(mount_point, exist_ok=True)
                subprocess.run(['sudo', 'mount', device, mount_point], check=True)
                return mount_point
            else:
                return mount_point
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed to mount {device}: {e}")
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
