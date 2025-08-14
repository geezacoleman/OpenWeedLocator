import logging
import json
import queue
import sys

from datetime import datetime
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
        detection_logger.propagate = False

        # Update the instance-level loggers
        instance.logger = root_logger
        instance.detection_logger = detection_logger

    @classmethod
    def add_mqtt_handler(cls, mqtt_client, mqtt_error_topic):
        """
        Dynamically adds an MQTT handler to the root logger after it has been set up.
        """
        if not mqtt_client or not mqtt_error_topic:
            logging.getLogger().warning("MQTT client or topic not provided; skipping MQTT handler setup.")
            return

        try:
            root_logger = logging.getLogger()

            if any(isinstance(h, MQTTLogHandler) for h in root_logger.handlers):
                root_logger.info("MQTTLogHandler already attached. Skipping.")
                return

            mqtt_handler = MQTTLogHandler(client=mqtt_client, topic=mqtt_error_topic)
            mqtt_handler.setLevel(logging.ERROR)
            mqtt_handler.setFormatter(logging.Formatter('%(message)s'))
            root_logger.addHandler(mqtt_handler)
            root_logger.info(
                "MQTT logging handler successfully added. Critical errors will now be sent to the dashboard.")
        except Exception as e:
            logging.getLogger().error(f"Failed to add MQTT logging handler: {e}")

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


class MQTTLogHandler(logging.Handler):
    """
    A logging handler that publishes log records to an MQTT topic.
    """

    def __init__(self, client, topic, qos=1, retain=False):
        super().__init__()
        self.client = client
        self.topic = topic
        self.qos = qos
        self.retain = retain
        self._publishing = False

    def emit(self, record):
        """
        Formats and publishes a log record to the MQTT topic.
        """
        if self._publishing:
            return

        try:
            self._publishing = True

            if record.levelno < logging.ERROR:
                return
            payload = {
                "level": record.levelname,
                "message": self.format(record),
                "timestamp": datetime.fromtimestamp(record.created).isoformat()
            }

            self.client.publish(self.topic, json.dumps(payload), self.qos, self.retain)

        except Exception:
            self.handleError(record)
        finally:
            self._publishing = False