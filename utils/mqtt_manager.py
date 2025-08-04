#!/usr/bin/env python3
"""
Complete MQTT IPC System for OWL Dashboard Communication
Replaces DashboardController functionality with proper inter-process communication
"""

import json
import time
import threading
import logging
import base64
import cv2
import numpy as np
import configparser
from typing import Optional, Callable

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Install paho-mqtt: pip install paho-mqtt")
    exit(1)


class MQTTServer:
    """
    MQTT IPC Server for owl.py
    Replaces DashboardController - manages state and handles dashboard commands
    """

    def __init__(self, broker_host='localhost', broker_port=1883, client_id='owl_main'):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self.logger = logging.getLogger(__name__)

        # MQTT topics
        self.topics = {
            'commands': 'owl/commands',  # Dashboard sends commands here
            'state': 'owl/state',  # Owl publishes state here
            'gps': 'owl/gps',  # Dashboard sends GPS here
            'status': 'owl/status',  # System status
            'indicators': 'owl/indicators'  # Weed detection, image write indicators
        }

        # Current state (replaces multiprocessing.Value objects)
        self.state = {
            'detection_enable': False,
            'image_sample_enable': False,
            'sensitivity_state': False,  # False = High, True = Low
            'owl_running': False,  # Will be set to True when server starts
            'stream_active': False,
            'gps_latitude': 0.0,
            'gps_longitude': 0.0,
            'gps_accuracy': 0.0,
            'gps_timestamp': 0.0,
            'gps_available': False,
            'last_update': time.time()
        }

        # Thread safety
        self.state_lock = threading.RLock()

        # OWL instance reference for sensitivity switching
        self.owl_instance = None
        self.low_sensitivity_config = None
        self.high_sensitivity_config = None

        self.last_sensitivity_state = False
        self.monitoring_thread = None
        self.heartbeat_thread = None

        # MQTT client
        self.client = mqtt.Client(client_id=self.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        # Connection state
        self.connected = False
        self.running = False

    def set_owl_instance(self, owl_instance, low_config, high_config):
        """Set reference to owl instance for sensitivity switching"""
        self.owl_instance = owl_instance
        self.low_sensitivity_config = low_config
        self.high_sensitivity_config = high_config
        self.logger.info(f"OWL instance configured with sensitivity configs: {low_config}, {high_config}")

    def start(self):
        """Start the MQTT IPC server"""
        try:
            self.running = True
            self.client.connect(self.broker_host, self.broker_port, 60)
            self.client.loop_start()

            with self.state_lock:
                self.state['owl_running'] = True

            self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self.heartbeat_thread.start()

            self.monitoring_thread = threading.Thread(target=self._monitor_states, daemon=True)
            self.monitoring_thread.start()

            # Publish initial state
            self._publish_state()

            self.logger.info(f"MQTT IPC Server started (broker: {self.broker_host}:{self.broker_port})")

        except Exception as e:
            self.logger.error(f"Failed to start MQTT server: {e}")
            raise

    def stop(self):
        """Stop the MQTT server"""
        self.running = False

        # Mark OWL as stopped
        with self.state_lock:
            self.state['owl_running'] = False

        if self.connected:
            self.client.publish(self.topics['status'], json.dumps({
                'owl_running': False,
                'timestamp': time.time()
            }), retain=True)

            # Also update main state
            self._publish_state()

        self.client.loop_stop()
        self.client.disconnect()
        self.logger.info("MQTT IPC Server stopped")

    def _on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection"""
        if rc == 0:
            self.connected = True
            self.logger.info("Connected to MQTT broker")

            # Subscribe to command topics
            client.subscribe(self.topics['commands'])
            client.subscribe(self.topics['gps'])

            client.publish(self.topics['status'], json.dumps({
                'owl_running': True,
                'timestamp': time.time()
            }), retain=True)

        else:
            self.logger.error(f"Failed to connect to MQTT broker: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection"""
        self.connected = False
        if rc != 0:
            self.logger.warning("Unexpected MQTT disconnection")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode())

            if topic == self.topics['commands']:
                self._handle_command(payload)
            elif topic == self.topics['gps']:
                self._handle_gps_update(payload)

        except Exception as e:
            self.logger.error(f"Error processing MQTT message: {e}")

    def _handle_command(self, command):
        """Handle control commands from dashboard"""
        action = command.get('action')

        with self.state_lock:
            if action == 'set_detection_enable':
                self.state['detection_enable'] = bool(command.get('value', False))
                self.logger.info(f"Detection enable set to: {self.state['detection_enable']}")

            elif action == 'set_image_sample_enable':
                self.state['image_sample_enable'] = bool(command.get('value', False))
                self.logger.info(f"Image sample enable set to: {self.state['image_sample_enable']}")

            elif action == 'toggle_sensitivity':
                self.state['sensitivity_state'] = not self.state['sensitivity_state']
                sensitivity_name = "Low" if self.state['sensitivity_state'] else "High"
                self.logger.info(f"Sensitivity toggled to: {sensitivity_name}")

                # Apply sensitivity change immediately
                self._apply_sensitivity_config_change(self.state['sensitivity_state'])

            # Update timestamp and publish new state
            self.state['last_update'] = time.time()
            self._publish_state()

    def _handle_gps_update(self, gps_data):
        """Handle GPS updates from dashboard"""
        with self.state_lock:
            self.state.update({
                'gps_latitude': float(gps_data.get('latitude', 0.0)),
                'gps_longitude': float(gps_data.get('longitude', 0.0)),
                'gps_accuracy': float(gps_data.get('accuracy', 0.0)),
                'gps_timestamp': float(gps_data.get('timestamp', time.time())),
                'gps_available': True,
                'last_update': time.time()
            })
        # self.logger.info(f"GPS updated: lat={self.state['gps_latitude']}, lon={self.state['gps_longitude']}")

    def _monitor_states(self):
        while self.running:
            try:
                with self.state_lock:
                    current_sensitivity = self.state['sensitivity_state']

                # Check for sensitivity changes
                if current_sensitivity != self.last_sensitivity_state:
                    self._apply_sensitivity_config_change(current_sensitivity)
                    self.last_sensitivity_state = current_sensitivity

                time.sleep(0.01)

            except Exception as e:
                self.logger.error(f"Error in MQTT monitoring: {e}")
                time.sleep(1)

    def _heartbeat_loop(self):
        while self.running:
            try:
                # Publish state every 2 seconds as heartbeat
                self._publish_state()
                time.sleep(2.0)
            except Exception as e:
                self.logger.error(f"Heartbeat error: {e}")
                time.sleep(5.0)

    def _apply_sensitivity_config_change(self, is_low_sensitivity):
        if not self.owl_instance or not self.low_sensitivity_config or not self.high_sensitivity_config:
            return

        try:
            config_file = self.low_sensitivity_config if is_low_sensitivity else self.high_sensitivity_config

            config = configparser.ConfigParser()
            config.read(config_file)

            # Update owl instance settings (exactly like DashboardController does)
            self.owl_instance.exg_min = config.getint('GreenOnBrown', 'exg_min')
            self.owl_instance.exg_max = config.getint('GreenOnBrown', 'exg_max')
            self.owl_instance.hue_min = config.getint('GreenOnBrown', 'hue_min')
            self.owl_instance.hue_max = config.getint('GreenOnBrown', 'hue_max')
            self.owl_instance.saturation_min = config.getint('GreenOnBrown', 'saturation_min')
            self.owl_instance.saturation_max = config.getint('GreenOnBrown', 'saturation_max')
            self.owl_instance.brightness_min = config.getint('GreenOnBrown', 'brightness_min')
            self.owl_instance.brightness_max = config.getint('GreenOnBrown', 'brightness_max')

            # Update trackbars if show_display is True (like DashboardController does)
            if self.owl_instance.show_display:
                cv2.setTrackbarPos("ExG-Min", self.owl_instance.window_name, self.owl_instance.exg_min)
                cv2.setTrackbarPos("ExG-Max", self.owl_instance.window_name, self.owl_instance.exg_max)
                cv2.setTrackbarPos("Hue-Min", self.owl_instance.window_name, self.owl_instance.hue_min)
                cv2.setTrackbarPos("Hue-Max", self.owl_instance.window_name, self.owl_instance.hue_max)
                cv2.setTrackbarPos("Sat-Min", self.owl_instance.window_name, self.owl_instance.saturation_min)
                cv2.setTrackbarPos("Sat-Max", self.owl_instance.window_name, self.owl_instance.saturation_max)
                cv2.setTrackbarPos("Bright-Min", self.owl_instance.window_name, self.owl_instance.brightness_min)
                cv2.setTrackbarPos("Bright-Max", self.owl_instance.window_name, self.owl_instance.brightness_max)

        except Exception as e:
            self.logger.error(f"Error applying sensitivity config: {e}")

    def _publish_state(self):
        """Publish current state to MQTT"""
        if self.connected:
            with self.state_lock:
                state_msg = json.dumps(self.state)
                self.client.publish(self.topics['state'], state_msg, retain=True)

    def weed_detect_indicator(self):
        """Send weed detection indicator to dashboard (replaces DashboardController method)"""
        if self.connected:
            indicator_msg = json.dumps({
                'type': 'weed_detected',
                'timestamp': time.time()
            })
            self.client.publish(self.topics['indicators'], indicator_msg, qos=0)

    def image_write_indicator(self):
        """Send image write indicator to dashboard (replaces DashboardController method)"""
        if self.connected:
            indicator_msg = json.dumps({
                'type': 'image_written',
                'timestamp': time.time()
            })
            self.client.publish(self.topics['indicators'], indicator_msg, qos=0)

    def drive_full_indicator(self):
        """Send drive full indicator to dashboard"""
        if self.connected:
            indicator_msg = json.dumps({
                'type': 'drive_full',
                'timestamp': time.time(),
                'message': 'Storage drive is full - recording disabled'
            })
            self.client.publish(self.topics['indicators'], indicator_msg, qos=0)

    # Public methods for owl.py to use (replaces multiprocessing.Value access)
    def get_detection_enable(self):
        with self.state_lock:
            return self.state['detection_enable']

    def get_image_sample_enable(self):
        with self.state_lock:
            return self.state['image_sample_enable']

    def get_sensitivity_state(self):
        with self.state_lock:
            return self.state['sensitivity_state']

    def get_gps_data(self):
        with self.state_lock:
            if not self.state['gps_available']:
                return None
            return {
                'latitude': self.state['gps_latitude'],
                'longitude': self.state['gps_longitude'],
                'accuracy': self.state['gps_accuracy'],
                'timestamp': self.state['gps_timestamp']
            }

    def set_stream_status(self, is_active: bool):
        """Allows the main Owl instance to report the video stream status."""
        with self.state_lock:
            if self.state.get('stream_active') != is_active:
                self.state['stream_active'] = is_active
                self.state['last_update'] = time.time()
                self._publish_state()

    def set_image_sample_enable(self, value):
        """Set image sampling state (for owl.py internal use)"""
        with self.state_lock:
            self.state['image_sample_enable'] = bool(value)
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_detection_enable(self, value):
        """Set detection state (for owl.py internal use)"""
        with self.state_lock:
            self.state['detection_enable'] = bool(value)
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_sensitivity_state(self, value):
        """Set sensitivity state (for owl.py internal use)"""
        with self.state_lock:
            self.state['sensitivity_state'] = bool(value)
            self.state['last_update'] = time.time()
        self._publish_state()

        self._apply_sensitivity_config_change(value)

class MQTTClient:
    """
    MQTT IPC Client for owl_dash.py
    Sends commands to owl.py and receives state
    """

    def __init__(self, broker_host='localhost', broker_port=1883, client_id='owl_dashboard'):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self.logger = logging.getLogger(__name__)

        # MQTT topics (same as server)
        self.topics = {
            'commands': 'owl/commands',
            'state': 'owl/state',
            'gps': 'owl/gps',
            'status': 'owl/status',
            'indicators': 'owl/indicators'
        }

        # Current state cache
        self.current_state = {}
        self.state_lock = threading.RLock()

        # Indicators
        self.last_weed_detect = 0
        self.last_image_write = 0

        self.last_heartbeat = 0
        self.heartbeat_timeout = 5.0


        # MQTT client
        self.client = mqtt.Client(client_id=self.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        self.connected = False

    def start(self):
        """Start the MQTT IPC client"""
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            self.logger.info(f"MQTT IPC Client started (broker: {self.broker_host}:{self.broker_port})")
        except Exception as e:
            self.logger.error(f"Failed to start MQTT client: {e}")
            raise

    def stop(self):
        """Stop the MQTT IPC client"""
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection"""
        if rc == 0:
            self.connected = True
            self.logger.info("Connected to MQTT broker")

            # Subscribe to all relevant topics
            client.subscribe(self.topics['state'])
            client.subscribe(self.topics['status'])
            client.subscribe(self.topics['indicators'])

        else:
            self.logger.error(f"Failed to connect to MQTT broker: {rc}")

    def _on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection"""
        self.connected = False
        if rc != 0:
            self.logger.warning("Unexpected MQTT disconnection")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages"""
        topic = msg.topic
        raw = msg.payload.decode(errors='ignore')
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.logger.error(
                f"Malformed JSON on topic '{topic}': "
                f"{raw[:60]!r}… ({e})"
            )
            return

        # Now dispatch cleanly
        if topic == self.topics['state']:
            with self.state_lock:
                self.current_state = data
                self.last_heartbeat = time.time()

        elif topic == self.topics['status']:
            self.logger.info(f"OWL status: {data}")

        elif topic == self.topics['indicators']:
            self._handle_indicator(data)

    def _handle_indicator(self, indicator_data):
        """Handle indicator messages (weed detection, image write)"""
        try:
            indicator_type = indicator_data.get('type')
            timestamp = indicator_data.get('timestamp', time.time())

            if indicator_type == 'weed_detected':
                self.last_weed_detect = timestamp
            elif indicator_type == 'image_written':
                self.last_image_write = timestamp

        except Exception as e:
            self.logger.error(f"Error handling indicator: {e}")

    def get_state(self):
        """Get current state"""
        with self.state_lock:
            current_time = time.time()
            if current_time - self.last_heartbeat > self.heartbeat_timeout:
                self.current_state['owl_running'] = False

            return self.current_state.copy()

    def get_weed_detect_indicator(self):
        """Check if weed was recently detected (for UI indicators)"""
        return (time.time() - self.last_weed_detect) < 1.0  # 1 second indicator

    def get_image_write_indicator(self):
        """Check if image was recently written (for UI indicators)"""
        return (time.time() - self.last_image_write) < 1.0  # 1 second indicator

    def _send_command(self, action, **kwargs):
        """Send command to OWL"""
        if not self.connected:
            return {'success': False, 'error': 'Not connected to MQTT broker'}

        command = {'action': action, **kwargs}
        try:
            self.client.publish(self.topics['commands'], json.dumps(command))
            return {'success': True, 'message': f'Command {action} sent'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def set_detection_enable(self, value):
        """Enable/disable detection"""
        return self._send_command('set_detection_enable', value=value)

    def set_image_sample_enable(self, value):
        """Enable/disable image sampling"""
        return self._send_command('set_image_sample_enable', value=value)

    def toggle_sensitivity(self):
        """Toggle sensitivity state"""
        return self._send_command('toggle_sensitivity')

    def update_gps(self, lat, lon, accuracy, timestamp=None):
        """Update GPS data"""
        if timestamp is None:
            timestamp = time.time()

        gps_data = {
            'latitude': lat,
            'longitude': lon,
            'accuracy': accuracy,
            'timestamp': timestamp
        }

        try:
            self.client.publish(self.topics['gps'], json.dumps(gps_data))
            return {'success': True, 'message': 'GPS data sent'}
        except Exception as e:
            return {'success': False, 'error': str(e)}