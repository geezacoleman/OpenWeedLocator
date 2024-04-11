import logging
from logging.handlers import QueueHandler, QueueListener
from queue import Queue
import os
from datetime import datetime, timezone
from time import strftime

class AsyncDBLogger:
    def __init__(self, db_path):
        self.db_path = db_path
        self.setup_logger()

    def setup_logger(self):
        # Create loggers for detections and system messages
        self.detection_logger = logging.getLogger("detection")
        self.system_logger = logging.getLogger("system")

        # Set the logging level
        self.detection_logger.setLevel(logging.INFO)
        self.system_logger.setLevel(logging.INFO)

        # Create queues and queue handlers for asynchronous logging
        detection_queue = Queue(-1)  # No limit on queue size
        system_queue = Queue(-1)

        detection_queue_handler = QueueHandler(detection_queue)
        system_queue_handler = QueueHandler(system_queue)

        # Add queue handlers to loggers
        self.detection_logger.addHandler(detection_queue_handler)
        self.system_logger.addHandler(system_queue_handler)

        # Define log file paths
        detection_log_path = os.path.join(self.db_path, "detections.log")
        system_log_path = os.path.join(self.db_path, "system.log")

        # Create file handlers
        detection_file_handler = logging.FileHandler(detection_log_path)
        system_file_handler = logging.FileHandler(system_log_path)

        # Optional: define and set a formatter if you want a specific log format
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        detection_file_handler.setFormatter(formatter)
        system_file_handler.setFormatter(formatter)

        # Create and start QueueListeners for each logger
        self.detection_listener = QueueListener(detection_queue, detection_file_handler)
        self.system_listener = QueueListener(system_queue, system_file_handler)
        self.detection_listener.start()
        self.system_listener.start()

    def log_detection(self, message):
        self.detection_logger.info(message)

    def log_system(self, message):
        self.system_logger.info(message)

    def stop(self):
        # Stop the QueueListeners to flush the queues and ensure all logs are written
        self.detection_listener.stop()
        self.system_listener.stop()



class Logger:
    def __init__(self, db_name, saveDir):
        self.db_name = strftime("%Y%m%d-%H%M%S_") + db_name
        self.saveDir = saveDir
        if not os.path.exists(self.saveDir):
            os.makedirs(self.saveDir)

        self.db_path = os.path.join(self.saveDir, self.db_name)
        self.dbLogger = AsyncDBLogger(self.db_path)

    def log_line(self, line, verbose=False):
        timestamp = datetime.now(timezone.utc)
        formatted_line = f"{timestamp} {line}\n"
        if verbose:
            print(formatted_line)
        self.dbLogger.log(timestamp, line)

    def log_line_video(self, line, verbose):
        self.log_line(line, verbose=False)
        self.videoLine = f"{datetime.now(timezone.utc)} {line}\n"
        if verbose:
            print(line)
        # Add code here if you need to handle video logs differently

    def new_video_logfile(self, name):
        self.videoLog = name
        self.log_line_video(f'NEW VIDEO LOG CREATED {name}', verbose=True)

    def stop_logging(self):
        self.dbLogger.stop()