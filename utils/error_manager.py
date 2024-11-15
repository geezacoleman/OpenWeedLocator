import subprocess
import logging
import traceback
import sys
import os

from pathlib import Path
from typing import List, Optional, Dict, Any, Set
from datetime import datetime
from dataclasses import dataclass

# Base exception class for OWL-specific errors
class OWLError(Exception):
    """Base exception class for all OWL-related errors."""

    # Color definitions available to all OWL errors
    COLORS = {
        'RED': "\033[91m",
        'GREEN': "\033[92m",
        'YELLOW': "\033[93m",
        'BLUE': "\033[94m",
        'PURPLE': "\033[95m",
        'CYAN': "\033[96m",
        'WHITE': "\033[97m",
        'RESET': "\033[0m",
        'BOLD': "\033[1m",
        'UNDERLINE': "\033[4m"
    }

    def __init__(self, message: str = None, details: Dict[str, Any] = None):
        self.details = details or {}
        self.timestamp = datetime.now()
        self.error_id = f"OWL_{self.timestamp.strftime('%Y%m%d_%H%M%S')}"
        super().__init__(message)

    @classmethod
    def colorize(cls, text: str, color: str, bold: bool = False, underline: bool = False) -> str:
        """
        Apply color and text formatting to a string.

        Args:
            text: The text to colorize
            color: Color name from COLORS dict
            bold: Whether to make the text bold
            underline: Whether to underline the text
        """
        formatting = ""
        if bold:
            formatting += cls.COLORS['BOLD']
        if underline:
            formatting += cls.COLORS['UNDERLINE']

        color_code = cls.COLORS.get(color.upper(), '')
        return f"{formatting}{color_code}{text}{cls.COLORS['RESET']}"

    def format_error_header(self, title: str) -> str:
        """Create a standardized error header."""
        return (
            f"\n{self.colorize(title, 'RED', bold=True)}\n"
            f"{self.colorize(f'Error ID: {self.error_id}', 'YELLOW')}\n"
        )

    def format_section(self, title: str, content: str) -> str:
        """Create a standardized section in the error message."""
        return (
            f"\n{self.colorize(title + ':', 'GREEN')}\n"
            f"{content}\n"
        )

### HARDWARE RELATED ERRORS ###
class CameraNotFoundError(OWLError):
    """Raised when there are issues with camera initialization or connection."""

    def __init__(self, error_type: str = None, original_error: str = None):
        # Initialize parent first
        super().__init__(
            message=None,
            details={
                'error_type': error_type,
                'original_error': original_error
            }
        )

        # Now we can build the message
        message = (
            self.format_error_header("Camera Connection Error") +
            self.format_section(
                "Problem",
                f"Failed to initialize camera: {self.colorize(error_type, 'WHITE', bold=True)}\n"
                f"Error details: {original_error}"
            ) +
            self.format_section(
                "Solutions",
                "1. Check if the camera ribbon cable is properly connected\n"
                "2. Inspect the ribbon cable for damage\n"
                "3. Verify camera module is properly seated\n"
                "4. Check if camera is enabled in raspi-config\n"
                "5. Try reconnecting the camera with the system powered off"
            ) +
            self.format_section(
                "How to get more information",
                f"• {self.colorize('vcgencmd get_camera', 'WHITE', bold=True)} - Check if camera is detected\n"
                f"• {self.colorize('libcamera-hello', 'WHITE', bold=True)} - Test camera feed\n"
                f"• {self.colorize('dmesg | grep -i camera', 'WHITE', bold=True)} - Check system logs"
            )
        )

        self.args = (message,)

@dataclass
class ProcessInfo:
    pid: int
    command: str

### Storage set up related errors ###
class StorageError(OWLError):
    """Base class for storage-related errors"""
    pass


class USBError(StorageError):
    """Base class for USB-related errors"""
    pass


class USBMountError(USBError):
    """Raised when there are issues mounting a USB device"""

    def __init__(self, device: str = None, message: str = None):
        super().__init__(
            message=None,
            details={'device': device} if device else {}
        )

        if not message:
            message = (
                    self.format_error_header("USB Mount Error") +
                    self.format_section(
                        "Problem",
                        f"Failed to mount USB device: {self.colorize(str(device), 'WHITE', bold=True)}"
                    ) +
                    self.format_section(
                        "Solutions",
                        "1. Check if USB device is properly connected\n"
                        "2. Verify device permissions\n"
                        "3. Check system mount points"
                    )
            )

        self.args = (message,)


class USBWriteError(USBError):
    """Raised when there are issues writing to a USB device"""

    def __init__(self, device: str = None, message: str = None):
        super().__init__(
            message=None,
            details={'device': device} if device else {}
        )

        if not message:
            message = (
                    self.format_error_header("USB Write Error") +
                    self.format_section(
                        "Problem",
                        f"Failed to write to USB device: {self.colorize(str(device), 'WHITE', bold=True)}"
                    ) +
                    self.format_section(
                        "Solutions",
                        "1. Check if device is write-protected\n"
                        "2. Verify available space\n"
                        "3. Check file system permissions"
                    )
            )

        self.args = (message,)


class NoWritableUSBError(USBError):
    """Raised when no writable USB devices are found"""

    def __init__(self, searched_paths: list[str] = None):
        super().__init__(
            message=None,
            details={'searched_paths': searched_paths} if searched_paths else {}
        )

        message = (
                self.format_error_header("No Writable USB Devices") +
                self.format_section(
                    "Problem",
                    "No writable USB devices were found"
                )
        )

        if searched_paths:
            message += self.format_section(
                "Searched Locations",
                "\n".join(f"• {path}" for path in searched_paths)
            )

        message += self.format_section(
            "Solutions",
            "1. Check if USB device is properly connected\n"
            "2. Verify USB device is not write-protected\n"
            "3. Ensure USB device is properly formatted\n"
            "4. Check device permissions"
        )

        self.args = (message,)


class StorageSystemError(StorageError):
    """Raised when there are platform/system compatibility issues with storage"""

    def __init__(self, platform: str = None, message: str = None):
        super().__init__(
            message=None,
            details={'platform': platform} if platform else {}
        )

        if not message:
            message = (
                    self.format_error_header("Storage System Compatibility Error") +
                    self.format_section(
                        "Problem",
                        f"Storage operation not supported on {self.colorize(platform, 'WHITE', bold=True)} platform"
                    ) +
                    self.format_section(
                        "Required",
                        "This operation requires Linux/Raspberry Pi"
                    ) +
                    self.format_section(
                        "Solutions",
                        "1. Use a supported platform, or\n"
                        "2. Specify a valid local directory path in config file\n"
                        "3. Use --save-directory flag to set local path"
                    )
            )

        self.args = (message,)

### OWL PROCESS RELATED ERRORS ###
class OWLProcessError(OWLError):
    """Base class for process-related errors."""
    pass


class OWLAlreadyRunningError(OWLProcessError):
    """Raised when OWL is already running."""

    @staticmethod
    def get_owl_processes() -> List[ProcessInfo]:
        """Get information about running OWL processes."""
        try:
            result = subprocess.check_output(['ps', '-eo', 'pid,command'], text=True).splitlines()
            return [
                ProcessInfo(pid=int(parts[0]), command=' '.join(parts[1:]))
                for line in result
                if 'owl.py' in line
                   and len(parts := line.strip().split()) >= 2
                   and parts[0].isdigit()
            ]
        except subprocess.CalledProcessError:
            return []

    def __init__(self, message: Optional[str] = None):
        processes = self.get_owl_processes()

        super().__init__(
            message=None,
            details={'running_processes': [vars(p) for p in processes]}
        )

        process_list = "\n".join(
            f"    {self.colorize(f'PID: {proc.pid}', 'WHITE', bold=True)} - Command: {proc.command}"
            for proc in processes
        ) or "    No OWL processes found in PS output."

        formatted_message = (
                self.format_error_header("OWL Process Already Running") +
                self.format_section(
                    "Status",
                    "Another instance of OWL appears to be running. The GPIO pins are in use."
                ) +
                self.format_section(
                    "Running OWL Processes",
                    process_list
                ) +
                self.format_section(
                    "Commands to Stop",
                    f"    {self.colorize('kill <PID>', 'WHITE', bold=True)}  - Graceful termination\n"
                    f"    {self.colorize('kill -9 <PID>', 'WHITE', bold=True)} - Force termination (use with caution)"
                ) +
                self.format_section(
                    "Important Notes",
                    "- Double-check the PID before stopping it!\n"
                    "- If no processes are listed but error persists, check GPIO outputs\n"
                    "- Ensure all GPIO resources are properly released\n"
                    "- Try rebooting if the issue persists"
                )
        )

        self.args = (formatted_message,)

### CONTROLLER ERRORS ###
class OWLControllerError(OWLError):
    """Base class for controller-related errors."""
    pass


class ControllerPinError(OWLControllerError):
    """Raised when there are issues with controller GPIO pins."""

    def __init__(self, pin_name: str, pin_number: int = None, reason: str = None):
        # Call super first
        super().__init__(
            message=None,
            details={
                'pin_name': pin_name,
                'pin_number': pin_number,
                'reason': reason
            }
        )

        message = (
            self.format_error_header("GPIO Pin Error") +
            self.format_section(
                "Pin Details",
                f"• Name: {self.colorize(pin_name, 'WHITE', bold=True)}\n" +
                (f"• Number: {self.colorize(f'BOARD{pin_number}', 'WHITE', bold=True)}\n" if pin_number else "") +
                (f"• Reason: {reason}\n" if reason else "")
            ) +
            self.format_section(
                "Common Fixes",
                "1. Check for pin conflicts with other processes\n"
                "2. Verify physical connections\n"
                "3. Confirm pin numbers in config"
            )
        )
        self.args = (message,)


class ControllerConfigError(OWLControllerError):
    """Raised when there are issues with controller configuration."""

    def __init__(self, config_key: str, section: str = "Controller"):
        # Call super first
        super().__init__(
            message=None,
            details={
                'config_key': config_key,
                'section': section
            }
        )

        message = (
            self.format_error_header("Controller Configuration Error") +
            self.format_section(
                "Missing Configuration",
                f"Required key '{self.colorize(config_key, 'WHITE', bold=True)}' "
                f"not found in section [{self.colorize(section, 'WHITE', bold=True)}]"
            ) +
            self.format_section(
                "Fix",
                "1. Check your config.ini file\n"
                f"2. Add the missing {config_key} setting in [{section}] section\n"
                "3. Ensure the value is appropriate for your controller type"
            )
        )
        self.args = (message,)

### CONFIGURATION ERRORS ###
class OWLConfigError(OWLError):
    """Base class for config file errors"""
    pass


class ConfigFileError(OWLConfigError):
    """Raised when there are issues with the config file itself"""
    def __init__(self, config_path: Path, reason: str = None):
        # First initialize parent
        super().__init__(
            message=None,
            details={
                'config_path': str(config_path),
                'reason': reason
            }
        )

        # Now build message using parent's methods
        message = (
            self.format_error_header("Configuration File Error") +
            self.format_section(
                "Problem",
                f"Cannot load configuration file: {self.colorize(str(config_path), 'WHITE', bold=True)}\n"
                f"Reason: {reason if reason else 'File not found or inaccessible'}"
            ) +
            self.format_section(
                "Fix",
                "1. Verify the config file exists\n"
                "2. Check file permissions\n"
                "3. Ensure the file path is correct\n"
                "4. Verify the file is not corrupted"
            )
        )
        self.args = (message,)  # Update Exception's message


class ConfigSectionError(OWLConfigError):
    """Raised when required sections are missing"""
    def __init__(self, missing_sections: Set[str], config_path: Path):
        # First initialize parent
        super().__init__(
            message=None,
            details={
                'missing_sections': list(missing_sections),
                'config_path': str(config_path)
            }
        )

        # Now build message
        message = (
            self.format_error_header("Missing Configuration Sections") +
            self.format_section(
                "Problem",
                f"Required sections missing from {self.colorize(str(config_path), 'WHITE', bold=True)}:\n" +
                "\n".join(f"• {self.colorize(section, 'WHITE', bold=True)}"
                         for section in missing_sections)
            ) +
            self.format_section(
                "Fix",
                "Add the missing sections to your config file with appropriate settings"
            )
        )
        self.args = (message,)


class ConfigKeyError(OWLConfigError):
    """Raised when required keys are missing in a section"""
    def __init__(self, section: str, missing_keys: Set[str], config_path: Path):
        super().__init__(
            message=None,
            details={
                'section': section,
                'missing_keys': list(missing_keys),
                'config_path': str(config_path)
            }
        )

        message = (
            self.format_error_header("Missing Configuration Keys") +
            self.format_section(
                "Problem",
                f"Required keys missing from section [{self.colorize(section, 'WHITE', bold=True)}]:\n" +
                "\n".join(f"• {self.colorize(key, 'WHITE', bold=True)}"
                         for key in missing_keys)
            ) +
            self.format_section(
                "Fix",
                f"Add the missing keys to the [{section}] section of your config file"
            )
        )
        self.args = (message,)


class ConfigValueError(OWLConfigError):
    """Raised when configuration values are invalid"""
    def __init__(self, section_errors: Dict[str, Dict[str, str]], config_path: Path):
        super().__init__(
            message=None,
            details={
                'section_errors': section_errors,
                'config_path': str(config_path)
            }
        )

        error_lines = []
        for section, errors in section_errors.items():
            for key, error_msg in errors.items():
                error_lines.append(
                    f"[{self.colorize(section, 'WHITE', bold=True)}] "
                    f"{self.colorize(key, 'WHITE', bold=True)} = {error_msg}"
                )

        message = (
            self.format_error_header("Invalid Configuration Values") +
            self.format_section(
                "Problem",
                "The following configuration values are invalid:\n" +
                "\n".join(f"• {line}" for line in error_lines)
            ) +
            self.format_section(
                "Fix",
                f"Correct these values in your config file to be within their expected ranges"
            )
        )
        self.args = (message,)

### ALGORITHM ERRORS ###
class AlgorithmError(OWLError):
    """Base class for algorithm-related errors"""

    ERROR_MESSAGES = {
        ModuleNotFoundError: {
            'coral': {
                'message': "Coral AI device support not installed",
                'details': "Visit: https://coral.ai/docs/accelerator/get-started/#requirements",
                'fix': "Install pycoral using: pip install pycoral"
            }
        },
        (IndexError, FileNotFoundError): {
            'models': {
                'message': "Model files not found",
                'details': "Required model files are missing from the 'models' directory",
                'fix': "Ensure model files are present in the 'models' directory"
            }
        },
        ValueError: {
            'delegate': {
                'message': "Coral AI device not recognized",
                'details': "Google Coral device connection issue",
                'fix': (
                    "1. Check device connection\n"
                    "2. Try unplugging and reconnecting the device\n"
                    "3. Restart the Raspberry Pi\n"
                    "More info: https://github.com/tensorflow/tensorflow/issues/32743"
                )
            }
        }
    }

    def __init__(self, algorithm: str, error: Exception):
        # Call the base class initializer to ensure attributes like error_id are set
        super().__init__(
            message=None,  # We'll set the detailed message below
            details={
                'algorithm': algorithm,
                'error_type': type(error).__name__,
                'original_error': str(error)
            }
        )

        self.algorithm = algorithm
        self.original_error = error
        self.error_type = type(error)

        # Find matching error configuration and format the message
        error_config = self._get_error_config(error)
        self.message = self._format_error_message(error_config)

        # Update the message in the base class
        self.args = (self.message,)

    def _get_error_config(self, error: Exception) -> dict:
        """Find the matching error configuration."""
        for error_types, configs in self.ERROR_MESSAGES.items():
            if isinstance(error, error_types):
                if isinstance(error, ValueError) and 'delegate' in str(error):
                    return configs['delegate']
                return next(iter(configs.values()))
        return {
            'message': "Unrecognized algorithm error",
            'details': str(error),
            'fix': "Check the error message and logs for more information"
        }

    def _format_error_message(self, config: dict) -> str:
        """Format the error message with the configuration."""
        return (
            self.format_error_header(f"Algorithm Error: {config['message']}") +
            self.format_section(
                "Algorithm",
                f"Failed to initialize algorithm: {self.colorize(self.algorithm, 'WHITE', bold=True)}"
            ) +
            self.format_section(
                "Details",
                f"{config['details']}\n"
                f"Original error: {self.colorize(str(self.original_error), 'WHITE')}"
            ) +
            self.format_section(
                "Fix",
                config['fix']
            )
        )

    def handle(self, owl_instance) -> None:
        """
        Handle algorithm errors with appropriate logging and actions.

        Args:
            owl_instance: The Owl instance that encountered the error.
        """
        # Use the logger from the owl_instance if available
        logger = getattr(owl_instance, 'logger', logging.getLogger(__name__))

        # Log the full error with context
        logger.error(
            self.message,
            extra={
                'algorithm': self.algorithm,
                'error_type': self.error_type.__name__,
                'error_details': self.details
            }
        )

        # Debug logging for troubleshooting with traceback
        if logger.isEnabledFor(logging.DEBUG):
            traceback_str = ''.join(traceback.format_exception(None, self.original_error, self.original_error.__traceback__))
            logger.debug(
                "Full error context",
                extra={
                    'traceback': traceback_str,
                    'error_class': f"{self.error_type.__module__}.{self.error_type.__name__}"
                }
            )

        # Stop OWL
        if hasattr(owl_instance, 'stop'):
            owl_instance.stop()
        else:
            logger.info("Exiting due to algorithm error.")
            sys.exit(1)

### Module/Software/Dependency Errors ###
class OpenCVError(OWLError):
    """Raised when there are issues with OpenCV (cv2) initialization or imports"""

    def __init__(self, error_msg: str = None):
        # Initialize with a formatted error message
        super().__init__(
            message=None,  # Pass None initially; we’ll set the final message below
            details={
                'original_error': error_msg,
                'virtual_env': os.environ.get('VIRTUAL_ENV'),
                'python_version': sys.version,
                'env_path': sys.prefix
            }
        )

        message = (
            self.format_error_header("OpenCV (cv2) Import Error") +
            self.format_section(
                "Problem",
                f"Failed to import OpenCV (cv2)\n"
                f"Error: {self.colorize(str(error_msg), 'WHITE', bold=True)}"
            ) +
            self.format_section(
                "Likely Cause",
                "You are not in the 'owl' virtual environment"
            ) +
            self.format_section(
                "Solution",
                "1. Activate the owl virtual environment:\n"
                f"   {self.colorize('workon owl', 'WHITE', bold=True)}\n\n"
                "2. If the environment doesn't exist, create it with the owl_setup.sh:\n"
                f"   {self.colorize('bash owl_setup.sh', 'WHITE', bold=True)}\n"
                "3. If opencv (cv2) is not yet installed in the environment, use owl_setup.sh:\n"
                f"   {self.colorize('bash owl_setup.sh', 'WHITE', bold=True)}\n"
                f"3. or install it manually within the {self.colorize('(owl)', 'GREEN', bold=True)} environment:\n"
                f"   {self.colorize('pip install opencv-python', 'WHITE', bold=True)}\n"
            ) +
            self.format_section(
                "Verify Environment",
                f"After activation, you should see {self.colorize('(owl)', 'GREEN', bold=True)} "
                "at the start of the command prompt.\nIf the error persists, raise an issue on the OpenWeedLocator"
                "Github page:\nhttps://github.com/geezacoleman/OpenWeedLocator/issues"
            )
        )

        self.args = (message,)

    def handle(self, owl_instance=None) -> None:
        """
        Handle OpenCV errors with appropriate logging and actions.

        Args:
            owl_instance: Optional Owl instance that encountered the error.
        """
        # Use owl_instance's logger if available, otherwise get the module logger
        if owl_instance and hasattr(owl_instance, 'logger'):
            logger = owl_instance.logger
        else:
            logger = logging.getLogger(__name__)

        # Log the error
        logger.error(
            str(self),
            extra={
                'virtual_env': os.environ.get('VIRTUAL_ENV'),
                'python_version': sys.version,
                'env_path': sys.prefix,
                'error_details': self.details
            }
        )

        # Stop the application or exit if owl_instance is not available
        if owl_instance and hasattr(owl_instance, 'stop'):
            owl_instance.stop()
        else:
            logger.info("Exiting due to OpenCV import error.")
            sys.exit(1)


class DependencyError(OWLError):
    """Raised when there are issues with Python package dependencies"""

    # Map common packages to their pip install names
    PACKAGE_MAP = {
        'imutils': 'imutils',
        'cv2': 'opencv-python',
        'multiprocessing': 'multiprocessing',
        'pathlib': 'pathlib',
        'version': 'local version.py file',  # Local file
        'SystemInfo': 'local version.py file',  # Local file
        'FPS': 'imutils'  # Part of imutils package
    }

    def __init__(self, missing_module: str, error_msg: str = None):
        self.missing_module = missing_module
        self.pip_package = self.PACKAGE_MAP.get(missing_module, missing_module)

        super().__init__(
            message=None,
            details={
                'missing_module': missing_module,
                'pip_package': self.pip_package,
                'original_error': error_msg
            }
        )

        # Build detailed error message
        if self.pip_package.startswith('local'):
            # Handle local file dependencies
            message = self._format_local_file_error()
        else:
            # Handle pip installable packages
            message = self._format_pip_package_error()

        self.args = (message,)

    def _format_pip_package_error(self) -> str:
        """Format error message for pip installable packages"""
        return (
                self.format_error_header("Python Package Dependency Error") +
                self.format_section(
                    "Problem",
                    f"Failed to import required module: {self.colorize(self.missing_module, 'WHITE', bold=True)}\n"
                    "This usually means the package is not installed in the owl virtual environment."
                ) +
                self.format_section(
                    "Quick Fix",
                    f"Install the missing package:\n"
                    f"1. Ensure you're in the owl environment:\n"
                    f"   {self.colorize('workon owl', 'WHITE', bold=True)}\n"
                    f"2. Install the package:\n"
                    f"   {self.colorize(f'pip install {self.pip_package}', 'WHITE', bold=True)}"
                ) +
                self.format_section(
                    "Complete Fix",
                    "Install all requirements:\n"
                    f"1. Activate owl environment: {self.colorize('workon owl', 'WHITE', bold=True)}\n"
                    f"2. Navigate to owl directory: {self.colorize('cd /path/to/owl', 'WHITE', bold=True)}\n"
                    f"3. Install requirements: {self.colorize('pip install -r requirements.txt', 'WHITE', bold=True)}"
                ) +
                self.format_section(
                    "Verify Installation",
                    f"Check if package is installed:\n"
                    f"{self.colorize(f'pip show {self.pip_package}', 'WHITE', bold=True)}"
                )
        )

    def _format_local_file_error(self) -> str:
        """Format error message for local file dependencies"""
        return (
                self.format_error_header("Local Module Import Error") +
                self.format_section(
                    "Problem",
                    f"Failed to import local module: {self.colorize(self.missing_module, 'WHITE', bold=True)}\n"
                    "This usually means you're not in the correct directory or the file is missing."
                ) +
                self.format_section(
                    "Solution",
                    "1. Ensure you're in the owl environment:\n"
                    f"   {self.colorize('workon owl', 'WHITE', bold=True)}\n"
                    "2. Navigate to the owl directory:\n"
                    f"   {self.colorize('cd /path/to/owl', 'WHITE', bold=True)}\n"
                    "3. Verify the file exists:\n"
                    f"   {self.colorize(f'ls {self.missing_module}.py', 'WHITE', bold=True)}"
                ) +
                self.format_section(
                    "If File is Missing",
                    "The file might have been deleted or not properly downloaded.\n"
                    "Try reinstalling OWL from the repository."
                )
        )

    def handle(self, owl_instance=None) -> None:
        """Handle dependency errors with appropriate logging and actions"""
        if owl_instance and hasattr(owl_instance, 'logger'):
            logger = owl_instance.logger
        else:
            logger = logging.getLogger(__name__)

        # Log the error
        logger.error(
            str(self),
            extra={
                'missing_module': self.missing_module,
                'pip_package': self.pip_package,
                'error_details': self.details
            }
        )

        if owl_instance and hasattr(owl_instance, 'stop'):
            owl_instance.stop()
        else:
            sys.exit(1)