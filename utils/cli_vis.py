import time
from blessed import Terminal
import numpy as np

class NozzleVis:
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
            print(self.term.move_x(pos), f'Nozzle {id}', end=' ')
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
    box_drawer = NozzleVis(relays=4)
    #
    for i in range(0, 100):
        relay = np.random.randint(0, 4)
        status = bool(np.random.randint(0, 2))
        box_drawer.update(relay=relay, status=status)
        time.sleep(0.01)


    # sys.exit()
    # from blessed import Terminal
    #
    # term = Terminal()
    #
    # with term.fullscreen():
    #     with term.cbreak():
    #         # Set the position and size of the box
    #         x_position = 10
    #         y_position = 5
    #         box_width = 10
    #         box_height = 10
    #
    #         # Draw the box
    #         # term.move_xy(x_position, y_position)
    #         box_str = term.on_color_rgb(0, 255, 0) + " " * box_width + term.normal
    #
    #         print(f"{box_str}\n", end="")

        # Wait for a key press before exiting
        # term.inkey()