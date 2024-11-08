import logging
import json
import queue
import sys

from pathlib import Path
from queue import Queue
from threading import Thread, Event
from typing import Dict, Any
from time import time
from logging.handlers import RotatingFileHandler


class JSONFormatter(logging.Formatter):
    """Formats log records as JSON strings"""

    def format(self, record: logging.LogRecord) -> str:
        message = {
            'timestamp': self.formatTime(record, self.datefmt),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName
        }

        # Add any extra context passed with the log
        if hasattr(record, 'detection_data'):
            message['detection_data'] = record.detection_data

        return json.dumps(message)


class ConsoleFormatter(logging.Formatter):
    """Human-readable formatter for console output"""

    def format(self, record: logging.LogRecord) -> str:
        return f"{self.formatTime(record, self.datefmt)} - {record.levelname} - [{record.name}] - {record.getMessage()}"


class LogManager:
    """Centralized logging management for OWL"""
    _instance = None
    _initialized = False

    BACKUP_COUNT = 100
    MAX_BYTES = 10 * 1024 * 1024  # 10MB per file

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self.detection_queue = Queue(maxsize=1000)
        self.stop_event = Event()
        self.batch_size = 100
        self.flush_interval = 1.0  # seconds
        self.last_flush = time()

        # Define instance-wide loggers
        self.logger = logging.getLogger("LogManager")
        self.detection_logger = logging.getLogger("detection")

        # Start the detection processing thread
        self.worker = Thread(target=self._process_detection_queue, daemon=True)
        self.worker.start()

    @classmethod
    def setup(cls, log_dir: Path, log_level: str = 'INFO') -> None:
        """Initialize the logging system"""
        instance = cls()

        log_dir.mkdir(exist_ok=True)

        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        root_logger.handlers = []

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(ConsoleFormatter(
            fmt='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'))
        root_logger.addHandler(console_handler)

        main_handler = RotatingFileHandler(
            filename=log_dir / 'owl.jsonl',
            maxBytes=cls.MAX_BYTES,  # 10MB
            backupCount=cls.BACKUP_COUNT)

        main_handler.setFormatter(JSONFormatter())
        root_logger.addHandler(main_handler)

        detection_handler = RotatingFileHandler(
            filename=log_dir / 'detections.jsonl',
            maxBytes=cls.MAX_BYTES,  # 10MB
            backupCount=cls.BACKUP_COUNT)

        detection_handler.setFormatter(JSONFormatter())

        # Configure detection logger
        detection_logger = logging.getLogger('detection')
        detection_logger.handlers = [detection_handler]
        detection_logger.propagate = False  # Don't propagate to root logger

        # Update the instance-level loggers
        instance.logger = root_logger
        instance.detection_logger = detection_logger

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """Get a logger instance for a module"""
        return logging.getLogger(name)

    def log_detection(self, frame_id: int, detections: Dict[str, Any]) -> None:
        """Queue a detection event for logging"""
        self.detection_queue.put({
            'timestamp': time(),
            'frame_id': frame_id,
            'detections': detections
        })

    def _process_detection_queue(self) -> None:
        """Background worker to process detection events"""
        batch = []

        while not self.stop_event.is_set():
            try:
                event = self.detection_queue.get(timeout=0.1)
                batch.append(event)

                # Flush if batch is full or interval exceeded
                if len(batch) >= self.batch_size or time() - self.last_flush >= self.flush_interval:
                    self._flush_detection_batch(batch)
                    batch.clear()
                    self.last_flush = time()

            except queue.Empty:
                pass  # No event, continue loop

            except Exception as e:
                self.logger.error(f"Error in processing detection queue: {e}", exc_info=True)

        if batch:
            self._flush_detection_batch(batch)

    def _flush_detection_batch(self, batch: list) -> None:
        """Write batch of detection events to log"""
        if batch:
            self.detection_logger.info(
                f"Processed batch of {len(batch)} detections",
                extra={'detection_data': batch}
            )

    def stop(self) -> None:
        """Stop the background worker"""
        self.stop_event.set()
        self.worker.join()
