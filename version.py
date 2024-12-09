import logging
from dataclasses import dataclass
import platform
import sys
import subprocess
from typing import Optional


@dataclass
class Version:
    major: int = 2
    minor: int = 2
    patch: int = 0
    tag: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}" + (f"-{self.tag}" if self.tag else "")

VERSION = Version()

class SystemInfo:
    logger = logging.getLogger("SystemInfo")

    @staticmethod
    def get_os_info() -> dict:
        return {
            'system': platform.system(),
            'release': platform.release(),
            'version': platform.version(),
            'machine': platform.machine(),
            'processor': platform.processor()
        }

    @staticmethod
    def get_python_info() -> dict:
        return {
            'version': sys.version,
            'implementation': platform.python_implementation(),
            'compiler': platform.python_compiler()
        }

    @staticmethod
    def get_rpi_info() -> Optional[str]:
        try:
            with open('/proc/device-tree/model', 'r') as f:
                return f.read().strip('\x00')
        except FileNotFoundError:
            SystemInfo.logger.warning("Raspberry Pi information not found.")
            return None

    @staticmethod
    def get_git_info() -> Optional[dict]:
        try:
            # Check if git is available first
            if subprocess.call(['which', 'git'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
                raise FileNotFoundError("Git not available")

            commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode('ascii').strip()
            branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).decode('ascii').strip()
            return {'commit': commit, 'branch': branch}
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            SystemInfo.logger.warning("Git information could not be retrieved: %s", e)
            return None
