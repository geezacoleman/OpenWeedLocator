from time import strftime
from datetime import datetime, timezone
import os

# this class logs everything that happens - detections, coordinates (if supplied) and nozzle
# will also log errors and framerates
class Logger:
    def __init__(self, name, saveDir):
        self.name = strftime("%Y%m%d-%H%M%S_") + name
        self.saveDir = saveDir
        if not os.path.exists(self.saveDir):
            os.makedirs(self.saveDir)

        self.savePath = os.path.join(self.saveDir, self.name)
        self.logList = []

    def log_line(self, line, verbose=False):
        self.line = str(datetime.now(timezone.utc)) + " " + line + "\n"
        if verbose:
            print(line)
        with open(self.savePath, 'a+') as file:
            file.write(self.line)
            self.logList.append(self.line)

    def log_line_video(self, line, verbose):
        self.log_line(line, verbose=False)
        self.videoLine = str(datetime.now(timezone.utc)) + " " + line + "\n"
        if verbose:
            print(line)

        with open(self.videoLog, 'a+') as file:
            file.write(self.videoLine)

    def new_video_logfile(self, name):
        self.videoLog = name
        self.log_line_video('NEW VIDEO LOG CREATED {}'.format(name), verbose=True)
