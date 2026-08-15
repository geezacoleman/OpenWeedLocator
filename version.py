import logging
from dataclasses import dataclass
from pathlib import Path
import platform
import sys
import subprocess
from typing import Optional


@dataclass
class Version:
    major: int = 3
    minor: int = 9
    patch: int = 4
    tag: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}" + (f"-{self.tag}" if self.tag else "")

VERSION = Version()

# App <-> OWL REST contract version, checked by the OWL phone app on first
# contact (setup API info, fleet controller descriptor, dashboard stats).
# Bump ONLY for breaking changes: a field the app relies on is renamed,
# removed, or changes meaning; a status code the app branches on changes.
# Purely additive fields do NOT bump it. A missing field on old servers is
# treated by the app as version 1.
APP_CONTRACT_VERSION = 1

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
    def get_git_info(cwd: Optional[Path] = None) -> Optional[dict]:
        # Default to the repo this file lives in, not the process CWD —
        # services may start with an unrelated working directory.
        if cwd is None:
            cwd = Path(__file__).parent
        try:
            commit = subprocess.check_output(
                ['git', 'rev-parse', '--short', 'HEAD'],
                cwd=cwd, stderr=subprocess.DEVNULL).decode('ascii').strip()
            branch = subprocess.check_output(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=cwd, stderr=subprocess.DEVNULL).decode('ascii').strip()
            return {'commit': commit, 'branch': branch}
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
            SystemInfo.logger.warning("Git information could not be retrieved: %s", e)
            return None
