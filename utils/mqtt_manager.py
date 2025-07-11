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
            'frames': 'owl/frames',  # Owl publishes frames here
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

        self.last_frame_time = 0
        self.frame_interval = 0.1

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

            sensitivity_name = "Low" if is_low_sensitivity else "High"
            self.logger.info(f"Applied {sensitivity_name} sensitivity config from: {config_file}")

        except Exception as e:
            self.logger.error(f"Error applying sensitivity config: {e}")

    def _publish_state(self):
        """Publish current state to MQTT"""
        if self.connected:
            with self.state_lock:
                state_msg = json.dumps(self.state)
                self.client.publish(self.topics['state'], state_msg, retain=True)

    def update_frame(self, frame):
        """
        Throttle frame publishing to 10 FPS max and encode the given OpenCV frame as JPEG
        """
        # Throttle frames to 10 FPS
        current_time = time.time()
        if current_time - self.last_frame_time < self.frame_interval:
            return True  # Skip this frame to maintain 10 FPS

        self.last_frame_time = current_time

        if not self.connected:
            return False

        try:
            # Integer-only parameters—True is not allowed here
            params = [
                cv2.IMWRITE_JPEG_QUALITY, 70,
                cv2.IMWRITE_JPEG_OPTIMIZE, 1
            ]
            success, buffer = cv2.imencode('.jpg', frame, params)
            if not success:
                self.logger.error("Failed to JPEG-encode frame")
                return False

            img_data = buffer.tobytes()

            # Resize if too large (MQTT message size limits)
            if len(img_data) > 512 * 1024:  # 512KB limit
                h, w = frame.shape[:2]
                scale = 640 / w if w > 640 else 1
                small = cv2.resize(frame, (int(w * scale), int(h * scale)))
                success, buffer = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 60])
                img_data = buffer.tobytes()
                if not success:
                    self.logger.error("Failed to encode downscaled frame")
                    return False

            # Encode as base64 for JSON
            img_b64 = base64.b64encode(img_data).decode('utf-8')
            frame_msg = json.dumps({
                'image': img_b64,
                'timestamp': time.time(),
                'encoding': 'jpeg_base64'
            })

            rc, _ = self.client.publish(self.topics['frames'], frame_msg, qos=0)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                self.logger.error(f"MQTT publish error rc={rc}")
                return False

            return True

        except Exception as e:
            self.logger.error(f"Error publishing frame: {e}")
            return False

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

        # Apply the sensitivity config change immediately
        self._apply_sensitivity_config_change(value)

class MQTTClient:
    """
    MQTT IPC Client for owl_dash.py
    Sends commands to owl.py and receives state/frames
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
            'frames': 'owl/frames',
            'gps': 'owl/gps',
            'status': 'owl/status',
            'indicators': 'owl/indicators'
        }

        # Current state cache
        self.current_state = {}
        self.latest_frame = None
        self.state_lock = threading.RLock()
        self.last_frame_time = 0
        self.frame_timeout = 5.0

        # Indicators
        self.last_weed_detect = 0
        self.last_image_write = 0

        # Callbacks
        self.frame_callback: Optional[Callable] = None

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
            client.subscribe(self.topics['frames'])
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

        elif topic == self.topics['frames']:
            self._handle_frame(data)

        elif topic == self.topics['status']:
            self.logger.info(f"OWL status: {data}")

        elif topic == self.topics['indicators']:
            self._handle_indicator(data)

    def _handle_frame(self, frame_data):
        """Handle incoming video frame"""
        try:
            img_b64 = frame_data.get('image')
            if img_b64:
                # Decode base64
                img_data = base64.b64decode(img_b64)

                # Decode JPEG
                img_array = np.frombuffer(img_data, dtype=np.uint8)
                frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

                if frame is not None:
                    self.latest_frame = frame
                    self.last_frame_time = time.time()  # Update frame timestamp

                    # Call frame callback if set
                    if self.frame_callback:
                        self.frame_callback(frame)

        except Exception as e:
            self.logger.error(f"Error decoding frame: {e}")

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

    def get_latest_frame(self):
        """Get the latest frame, return None if too old"""
        if time.time() - self.last_frame_time > self.frame_timeout:
            return None  # Force placeholder
        return self.latest_frame

    def get_state(self):
        """Get current state"""
        with self.state_lock:
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