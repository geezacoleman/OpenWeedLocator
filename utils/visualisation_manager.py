import time
import numpy as np
import warnings

class BasicTerminal:
    def __init__(self):
        self.width = 80
        self.height = 24
        self.class_name = "BasicTerminal"

    @staticmethod
    def move_x(x):
        return f"\033[{x}G"

    @property
    def normal(self):
        return "\033[0m"

    def on_color_rgb(self, r, g, b):
        return f"\033[48;2;{r};{g};{b}m"

    def __str__(self):
        return f"<{self.class_name} object with width={self.width} and height={self.height}>"

try:
    from blessed import Terminal

except ModuleNotFoundError:
    warnings.warn("[WARNING] blessed library not found. Using basic terminal functionality. "
                  "\nNote, please run 'pip install blessed' and check OWL installation to fix.")
    Terminal = BasicTerminal


class RelayVis:
    def __init__(self, relays=4):
        self.term = Terminal()
        self.relays = relays
        self.width = self.term.width
        self.height = self.term.height
        self.box_width = 10
        self.active_color = [50, 255, 50]
        self.inactive_color = [100, 100, 100]

        self.status_list = [False for i in range(relays)]
        self.x_positions = [(relay * self.box_width + relay * 2) for relay in range(relays)]

    def setup(self):
        for id, pos in enumerate(self.x_positions):
            print(self.term.move_x(pos), f'Nozzle {id + 1}', end=' ')
        print('\r')
        for i, x_pos in enumerate(self.x_positions):
            r, g, b = self.inactive_color
            box_str = self.term.on_color_rgb(r, g, b) + " " * (self.box_width) + self.term.normal
            print(self.term.move_x(x_pos) + f"{box_str}", end='', flush=True)

    def update(self, relay=1, status=True):
        self.status_list[relay] = status

        if self.status_list[relay]:
            r, g, b = self.active_color
            box_str = self.term.on_color_rgb(r, g, b) + " " * (self.box_width) + self.term.normal
            print(self.term.move_x(self.x_positions[relay]) + f"{box_str}", end="", flush=True)
        else:
            r, g, b = self.inactive_color
            box_str = self.term.on_color_rgb(r, g, b) + " " * (self.box_width) + self.term.normal
            print(self.term.move_x(self.x_positions[relay]) + f"{box_str}", end="", flush=True)

    def close(self):
        print("\n", end='\n')

if __name__ == "__main__":
    box_drawer = RelayVis(relays=4)

    for i in range(0, 100):
        relay = np.random.randint(0, 4)
        status = bool(np.random.randint(0, 2))
        box_drawer.update(relay=relay, status=status)
        time.sleep(0.01)
