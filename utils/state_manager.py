#!/usr/bin/env python3
"""
Shared state module for OWL
"""

import logging
from multiprocessing import Value, Queue


class SharedState:
    def __init__(self):
        self.detection_enable = Value('b', False)
        self.image_sample_enable = Value('b', False)
        self.sensitivity_state = Value('b', False)  # False = high sensitivity, True = low sensitivity
        self.frame_queue = Queue(maxsize=5)
        self.owl_running = Value('b', False)
        self.last_frame_time = Value('d', 0.0)

        # GPS data
        self.gps_latitude = Value('d', 0.0)
        self.gps_longitude = Value('d', 0.0)
        self.gps_accuracy = Value('d', 0.0)
        self.gps_timestamp = Value('d', 0.0)
        self.gps_available = Value('b', False)

        # Config values - will be populated from config file
        self.config = None
        self.config_path = None

        # For debugging - store the ID of each Value object
        self.state_ids = {
            'detection_enable': id(self.detection_enable),
            'image_sample_enable': id(self.image_sample_enable),
            'sensitivity_state': id(self.sensitivity_state),
            'gps_latitude': id(self.gps_latitude),
            'gps_longitude': id(self.gps_longitude),
        }

    def log_state_ids(self, logger):
        """Log the IDs of all shared state objects for debugging"""
        logger.info("=== Shared State Object IDs ===")
        for name, obj_id in self.state_ids.items():
            logger.info(f"{name}: {obj_id}")
        logger.info("===============================")

    def get_gps_data(self):
        """Get current GPS data as a dictionary"""
        if not self.gps_available.value:
            return None

        return {
            'latitude': self.gps_latitude.value,
            'longitude': self.gps_longitude.value,
            'accuracy': self.gps_accuracy.value,
            'timestamp': self.gps_timestamp.value
        }

    def update_gps_data(self, lat, lon, accuracy, timestamp):
        """Update GPS data from dashboard"""
        with self.gps_latitude.get_lock():
            self.gps_latitude.value = lat
        with self.gps_longitude.get_lock():
            self.gps_longitude.value = lon
        with self.gps_accuracy.get_lock():
            self.gps_accuracy.value = accuracy
        with self.gps_timestamp.get_lock():
            self.gps_timestamp.value = timestamp
        with self.gps_available.get_lock():
            self.gps_available.value = True


# Create a global shared state instance
shared_state = SharedState()

# Log creation for debugging
logger = logging.getLogger(__name__)
logger.info("=== SHARED STATE MODULE LOADED ===")
shared_state.log_state_ids(logger)