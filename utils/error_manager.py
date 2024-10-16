import subprocess

class USBMountError(Exception):
    pass


class USBWriteError(Exception):
    pass


class NoWritableUSBError(Exception):
    pass


class OWLAlreadyRunningError(Exception):
    """Raised when OWL is already running."""

    def __init__(self, message=None):
        RED = "\033[91m"
        GREEN = "\033[92m"
        YELLOW = "\033[93m"
        RESET = "\033[0m"
        BOLD = "\033[1m"

        try:
            result = subprocess.check_output(['pgrep', '-f', 'owl.py']).decode('utf-8').strip()
            pids = result if result else "No OWL process found."
        except subprocess.CalledProcessError:
            pids = "No OWL process found."

        if message is None:
            message = (
                f"\n{RED}{BOLD}!!!  OWL Process Already Running  !!!{RESET}\n"
                f"{YELLOW}It looks like owl.py is already running. To continue, you need to stop the existing instance.{RESET}\n\n"
                f"{GREEN}OWL process(es):{RESET}\n"
                f"    {BOLD}{pids}{RESET}\n\n"
                f"{GREEN}To stop the process(es), use the following command for each PID above:{RESET}\n"
                f"    {BOLD}kill <PID>{RESET}\n\n"
                f"{RED}IMPORTANT: Be sure to double-check the PID before stopping it!{RESET}\n"
                f"{RED}NOTE: If no PIDs are listed above, check GPIO outputs are not in use or have been closed correctly!{RESET}\n"
            )
        super().__init__(message)

