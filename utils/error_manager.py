import subprocess
import logging

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

    def log_error(self, logger=None):
        """Log the error with its details."""
        logger = logger or logging.getLogger(__name__)
        logger.error(f"{self.__class__.__name__} ({self.error_id}): {str(self)}")
        logger.debug(f"Error details: {self.details}")
        logger.debug(f"Timestamp: {self.timestamp}")

### HARDWARE RELATED ERRORS ###
@dataclass
class ProcessInfo:
    pid: int
    command: str

class USBError(OWLError):
    """Base class for USB-related errors."""
    pass

class USBMountError(USBError):
    """Raised when there are issues mounting a USB device."""

    def __init__(self, device: str = None, message: str = None):
        details = {'device': device} if device else {}

        if not message:
            message = (
                    self.format_error_header("USB Mount Error") +
                    self.format_section(
                        "Problem",
                        f"Failed to mount USB device: {self.colorize(str(device), 'WHITE', bold=True)}"
                    )
            )

        super().__init__(message, details)


class USBWriteError(USBError):
    """Raised when there are issues writing to a USB device."""

    def __init__(self, device: str = None, message: str = None):
        details = {'device': device} if device else {}

        if not message:
            message = (
                    self.format_error_header("USB Write Error") +
                    self.format_section(
                        "Problem",
                        f"Failed to write to USB device: {self.colorize(str(device), 'WHITE', bold=True)}"
                    )
            )

        super().__init__(message, details)


class NoWritableUSBError(USBError):
    """Raised when no writable USB devices are found."""

    def __init__(self):
        message = (
                self.format_error_header("No Writable USB Devices") +
                self.format_section(
                    "Problem",
                    "No writable USB devices were found"
                ) +
                self.format_section(
                    "Solutions",
                    "1. Check if USB device is properly connected\n"
                    "2. Verify USB device is not write-protected\n"
                    "3. Ensure USB device is properly formatted"
                )
        )
        super().__init__(message)


### PROCESS RELATED ERRORS ###
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
                   and parts[0].isdigit()  # Verify it's a valid number first
            ]
        except subprocess.CalledProcessError:
            return []

    def __init__(self, message: Optional[str] = None):
        processes = self.get_owl_processes()
        details = {'running_processes': [vars(p) for p in processes]}

        if not message:
            process_list = "\n".join(
                f"    {self.colorize(f'PID: {proc.pid}', 'WHITE', bold=True)} - Command: {proc.command}"
                for proc in processes
            ) or "    No OWL processes found in PS output."

            message = (
                    self.format_error_header("OWL Process Already Running") +
                    self.format_section(
                        "Status",
                        "It looks like owl.py is already running. To continue, you need to stop the existing instance."
                    ) +
                    self.format_section("Running OWL processes", process_list) +
                    self.format_section(
                        "Commands to stop processes",
                        f"    {self.colorize('kill <PID>', 'WHITE', bold=True)}  - Graceful termination\n"
                        f"    {self.colorize('kill -9 <PID>', 'WHITE', bold=True)} - Force termination (use with caution)"
                    ) +
                    self.format_section(
                        "Important Notes",
                        "- Double-check the PID before stopping it!\n"
                        "- If no processes are listed but error persists, check GPIO outputs\n"
                        "- Ensure all GPIO resources are properly released"
                    )
            )

        super().__init__(message, details)


class OWLControllerError(OWLError):
    """Base class for controller-related errors."""
    pass


class ControllerTypeError(OWLControllerError):
    """Raised when an invalid controller type is specified."""

    def __init__(self, invalid_type: str, config_file: str = "config.ini"):
        details = {
            'invalid_type': invalid_type,
            'valid_types': ['None', 'Ute', 'Advanced'],
            'config_file': config_file
        }

        message = (
                self.format_error_header("Invalid Controller Type") +
                self.format_section(
                    "Problem",
                    f"Controller type '{self.colorize(invalid_type, 'WHITE', bold=True)}' is not valid"
                ) +
                self.format_section(
                    "Valid Types",
                    "• None - No physical controller\n"
                    "• Ute - Single switch controller\n"
                    "• Advanced - Multi-switch controller"
                ) +
                self.format_section(
                    "Fix",
                    f"Edit {self.colorize(config_file, 'WHITE', bold=True)} and set correct controller_type"
                    f"in the config file. Remember '' are not needed."
                )
        )
        super().__init__(message, details)


class ControllerPinError(OWLControllerError):
    """Raised when there are issues with controller GPIO pins."""

    def __init__(self, pin_name: str, pin_number: int = None, reason: str = None):
        details = {
            'pin_name': pin_name,
            'pin_number': pin_number,
            'reason': reason
        }

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
        super().__init__(message, details)


class ControllerConfigError(OWLControllerError):
    """Raised when there are issues with controller configuration."""

    def __init__(self, config_key: str, section: str = "Controller"):
        details = {
            'config_key': config_key,
            'section': section
        }

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
        super().__init__(message, details)


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
