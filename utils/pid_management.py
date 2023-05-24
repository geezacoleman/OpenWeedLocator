import signal
import psutil
import os

def send_sigint(pid):
    try:
        os.kill(pid, signal.SIGINT)

    except OSError as e:
        print(f'[ERROR] Error occurred during termination of previous `owl.py` instances {e}.\n '
              f'We recommend running `ps -C owl.py` followed by `sudo kill {pid}')


def oldest_pid():
    processes = []
    for proc in psutil.process_iter(['pid', 'name', 'create_time']):
        if proc.name() == 'owl.py':
            processes.append(proc)

        if len(processes) > 1: # i.e. there are more than two owl.py running
            processes.sort(key=lambda proc: proc.create_time())
            oldest_owl_py = processes[0] # finds the oldest owl.py
            return oldest_owl_py.pid

        return None