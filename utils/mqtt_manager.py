#!/usr/bin/env python3
"""
Complete MQTT IPC System for OWL Dashboard Communication
Supports both local (standalone) and networked (central controller) modes
"""

import json
import time
import threading
import logging
import os
import cv2
import configparser
import socket

from collections import deque

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Install paho-mqtt: pip install paho-mqtt")
    exit(1)


class OWLMQTTPublisher:
    """
    MQTT Publisher for owl.py - publishes status and receives commans
    Supports both standalone (localhost) and networked (central controller) modes
    """

    def __init__(self, broker_host='localhost', broker_port=1883, client_id='owl_main', device_id=None, network_mode=None):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self.logger = logging.getLogger(__name__)

        # Determine device ID
        if device_id is None or device_id == 'auto':
            # Use hostname as default device ID
            device_id = socket.gethostname()
            self.logger.info(f"Auto-detected device_id: {device_id}")

        self.device_id = device_id

        # Determine if we're in networked mode from config, fall back to broker IP heuristic
        if network_mode is not None:
            self.networked_mode = (network_mode.lower() == 'networked')
        else:
            self.networked_mode = (broker_host.lower() not in ['localhost', '127.0.0.1'])

        if self.networked_mode:
            self.logger.info(f"Running in NETWORKED mode - connecting to broker at {broker_host}:{broker_port}")
        else:
            self.logger.info(f"Running in STANDALONE mode - using local broker")

        # MQTT topics - now include device_id for networked mode
        if self.networked_mode:
            # Networked mode: device-specific topics
            self.topics = {
                'commands': f'owl/{device_id}/commands',
                'state': f'owl/{device_id}/state',
                'status': f'owl/{device_id}/status',
                'detection': f'owl/{device_id}/detection',
                'config': f'owl/{device_id}/config',
                'indicators': f'owl/{device_id}/indicators',
                'errors': f'owl/{device_id}/errors',
                'gps': f'owl/{device_id}/gps'
            }
        else:
            # Standalone mode: simple topics for backward compatibility
            self.topics = {
                'commands': 'owl/commands',
                'state': 'owl/state',
                'status': 'owl/status',
                'detection': 'owl/detection',
                'config': 'owl/config',
                'indicators': 'owl/indicators',
                'errors': 'owl/errors',
                'gps': 'owl/gps'
            }

        # Current state
        self.state = {
            'device_id': device_id,
            'detection_enable': False,
            'image_sample_enable': False,
            'sensitivity_level': 'medium',
            'detection_mode': 1,  # 0=spot spray, 1=off, 2=blanket
            'owl_running': False,
            'stream_active': False,
            # System statistics
            'cpu_percent': 0,
            'cpu_temp': 0,
            'memory_percent': 0,
            'memory_used': 0,
            'memory_total': 0,
            'disk_percent': 0,
            'disk_used': 0,
            'disk_total': 0,
            'fan_status': {'is_rpi5': False, 'mode': 'unavailable', 'rpm': 0},
            # GPS
            'gps_latitude': 0.0,
            'gps_longitude': 0.0,
            'gps_accuracy': 0.0,
            'gps_timestamp': 0.0,
            'gps_available': False,
            'last_update': time.time(),
            'networked_mode': self.networked_mode,
            'broker_host': broker_host,
            # Algorithm state
            'algorithm': 'exhsv',
            'model_available': False,
            'crop_buffer_px': 20,
            'inference_resolution': 320,
            # GreenOnBrown parameters (will be populated on first update)
            'exg_min': None,
            'exg_max': None,
            'hue_min': None,
            'hue_max': None,
            'saturation_min': None,
            'saturation_max': None,
            'brightness_min': None,
            'brightness_max': None
        }

        # Thread safety
        self.state_lock = threading.RLock()
        self.config_lock = threading.Lock()

        # Config file path (set via set_owl_instance or directly)
        self.config_file = None

        # OWL instance reference
        self.owl_instance = None
        self.low_sensitivity_config = None
        self.medium_sensitivity_config = None
        self.high_sensitivity_config = None

        self.last_sensitivity_level = 'medium'
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
        self.connection_attempts = 0
        self.max_connection_attempts = 5

    def set_owl_instance(self, owl_instance, low_config, medium_config, high_config):
        """Set reference to owl instance and sensitivity config files"""
        self.owl_instance = owl_instance
        self.low_sensitivity_config = low_config
        self.medium_sensitivity_config = medium_config
        self.high_sensitivity_config = high_config

        # Capture the config file path from the owl instance
        if hasattr(owl_instance, 'config_path'):
            self.config_file = owl_instance.config_path

        self.logger.info(f"OWL instance configured with sensitivity configs:")
        self.logger.info(f"  Low: {low_config}")
        self.logger.info(f"  Medium: {medium_config}")
        self.logger.info(f"  High: {high_config}")

        # Initialize state with current OWL parameters
        self._sync_parameters_to_state()

    def _sync_parameters_to_state(self):
        """Sync current OWL parameters to MQTT state"""
        if self.owl_instance is None:
            return

        with self.state_lock:
            self.state['exg_min'] = self.owl_instance.exg_min
            self.state['exg_max'] = self.owl_instance.exg_max
            self.state['hue_min'] = self.owl_instance.hue_min
            self.state['hue_max'] = self.owl_instance.hue_max
            self.state['saturation_min'] = self.owl_instance.saturation_min
            self.state['saturation_max'] = self.owl_instance.saturation_max
            self.state['brightness_min'] = self.owl_instance.brightness_min
            self.state['brightness_max'] = self.owl_instance.brightness_max
            self.state['min_detection_area'] = getattr(
                self.owl_instance, 'min_detection_area', 10)

            # Algorithm state
            self.state['algorithm'] = self.owl_instance.config.get(
                'System', 'algorithm', fallback='exhsv')
            self.state['crop_buffer_px'] = getattr(
                self.owl_instance, 'crop_buffer_px', 20)
            self.state['inference_resolution'] = getattr(
                self.owl_instance, 'inference_resolution', 320)

            # Check model availability (any NCNN dirs or .pt files in models/)
            self.state['model_available'] = self._check_model_available()

    def start(self):
        """Start the MQTT IPC server"""
        try:
            self.running = True

            # Try to connect to broker
            self.logger.info(f"Attempting to connect to MQTT broker at {self.broker_host}:{self.broker_port}")
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

            self.logger.info(f"MQTT IPC Server started successfully")
            self.logger.info(f"Device ID: {self.device_id}")
            self.logger.info(f"Publishing to topics: {list(self.topics.values())}")

        except Exception as e:
            self.logger.error(f"Failed to start MQTT server: {e}")

            if self.networked_mode:
                self.logger.warning(f"Could not connect to network broker at {self.broker_host}:{self.broker_port}")
                self.logger.warning("OWL will continue to operate locally without remote control")
            else:
                self.logger.error("Could not connect to local MQTT broker - dashboard features disabled")

            # Don't raise - allow OWL to continue operating
            self.running = False

    def stop(self):
        """Stop the MQTT server"""
        self.running = False

        # Mark OWL as stopped
        with self.state_lock:
            self.state['owl_running'] = False

        if self.connected:
            self.client.publish(self.topics['status'], json.dumps({
                'device_id': self.device_id,
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
            self.connection_attempts = 0
            self.logger.info(f"Connected to MQTT broker at {self.broker_host}:{self.broker_port}")

            # Subscribe to command topics
            client.subscribe(self.topics['commands'])
            client.subscribe(self.topics['gps'])

            self.logger.info(f"Subscribed to: {self.topics['commands']}, {self.topics['gps']}")

            # Publish connection status
            client.publish(self.topics['status'], json.dumps({
                'device_id': self.device_id,
                'owl_running': True,
                'connected': True,
                'timestamp': time.time()
            }), retain=True)

        else:
            self.connected = False
            self.connection_attempts += 1
            self.logger.error(f"Failed to connect to MQTT broker: rc={rc}")

            if self.connection_attempts >= self.max_connection_attempts:
                self.logger.error(f"Max connection attempts ({self.max_connection_attempts}) reached")

    def _on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection"""
        self.connected = False
        if rc != 0:
            self.logger.warning(f"Unexpected MQTT disconnection (rc={rc})")
            if self.networked_mode:
                self.logger.warning("Lost connection to central controller - OWL continues operating locally")

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
            self.logger.error(f"Error processing MQTT message on topic {msg.topic}: {e}")

    def _handle_command(self, command):
        """Handle control commands from dashboard or central controller"""
        action = command.get('action')

        self.logger.info(f"Received command: {action}")

        with self.state_lock:
            if action == 'set_detection_enable':
                self.state['detection_enable'] = bool(command.get('value', False))
                self.logger.info(f"Detection enable set to: {self.state['detection_enable']}")

            elif action == 'set_image_sample_enable':
                self.state['image_sample_enable'] = bool(command.get('value', False))
                self.logger.info(f"Image sample enable set to: {self.state['image_sample_enable']}")

            elif action == 'set_sensitivity_level':
                # Legacy command - maps to preset system
                level = command.get('level', '').lower()
                valid_levels = ['low', 'medium', 'high']

                if level not in valid_levels:
                    self.logger.error(f"Invalid sensitivity level: {level}")
                    return

                self.state['sensitivity_level'] = level
                self.logger.info(f"Sensitivity level set to: {level}")
                self._apply_sensitivity_preset(level)

            elif action == 'set_greenonbrown_param':
                # Individual parameter update
                param_name = command.get('param')
                param_value = command.get('value')

                if param_name and param_value is not None:
                    self.logger.info(f"Updating GreenOnBrown parameter: {param_name} = {param_value}")
                    self._update_greenonbrown_param(param_name, param_value)

            elif action == 'set_algorithm':
                value = command.get('value', '').lower()
                valid = {'exg', 'exgr', 'maxg', 'nexg', 'exhsv', 'hsv', 'gndvi', 'gog', 'gog-hybrid'}
                if value in valid:
                    self.state['algorithm'] = value
                    if self.owl_instance:
                        self.owl_instance._pending_algorithm = value
                    self.logger.info(f"Algorithm set to: {value}")
                else:
                    self.logger.error(f"Invalid algorithm: {value}")

            elif action == 'set_crop_buffer':
                try:
                    value = max(0, min(50, int(command.get('value', 20))))
                    self.state['crop_buffer_px'] = value
                    if self.owl_instance:
                        self.owl_instance.crop_buffer_px = value
                    self.logger.info(f"Crop buffer set to: {value}px")
                except (ValueError, TypeError) as e:
                    self.logger.error(f"Invalid crop_buffer value: {e}")

            elif action == 'get_config':
                self._handle_get_config()

            elif action == 'set_config_section':
                section = command.get('section')
                params = command.get('params', {})
                if section and params:
                    self._handle_set_config_section(section, params)

            elif action == 'save_config':
                filename = command.get('filename')
                self._handle_save_config(filename)

            elif action == 'set_active_config':
                config_path = command.get('config')
                if config_path:
                    self._handle_set_active_config(config_path)

            elif action == 'reboot':
                self.logger.warning("Reboot command received - this requires system privileges")
                # Future implementation

            elif action == 'restart_service':
                self.logger.warning("Service restart command received")
                self._handle_restart_service()

            # Update timestamp and publish new state
            self.state['last_update'] = time.time()
            self._publish_state()

    def _handle_get_config(self):
        """Read current config from disk and publish as JSON to config topic"""
        try:
            config_path = self._resolve_config_path()
            if not config_path:
                self.logger.error("Cannot get config - no config file path available")
                return

            with self.config_lock:
                config = configparser.ConfigParser()
                config.read(config_path)

            # Convert to dict
            config_dict = {}
            for section in config.sections():
                config_dict[section] = dict(config[section])

            payload = {
                'config': config_dict,
                'config_path': str(config_path),
                'config_name': os.path.basename(config_path),
                'device_id': self.device_id,
                'timestamp': time.time()
            }

            self.client.publish(self.topics['config'], json.dumps(payload), retain=False)
            self.logger.info(f"Published config from {config_path} ({len(config_dict)} sections)")

        except Exception as e:
            self.logger.error(f"Error handling get_config: {e}")

    def _handle_set_config_section(self, section, params):
        """Update multiple parameters in a section on the live OWL instance"""
        if self.owl_instance is None:
            self.logger.warning("Cannot set config section - OWL instance not set")
            return

        try:
            for key, value in params.items():
                # Update GreenOnBrown params on the live instance
                if section == 'GreenOnBrown':
                    self._update_greenonbrown_param(key, value)
                elif section == 'GreenOnGreen':
                    self._update_greenongreen_param(key, value)
                elif section == 'System' and key == 'algorithm':
                    # Route algorithm changes through set_algorithm handler
                    self._handle_command({'action': 'set_algorithm', 'value': value})
                elif hasattr(self.owl_instance, key):
                    # Type-convert to match existing attribute type (INI values are strings)
                    current = getattr(self.owl_instance, key)
                    try:
                        if isinstance(current, bool):
                            typed = str(value).lower() in ('true', '1', 'yes')
                        elif isinstance(current, int):
                            typed = int(float(value))
                        elif isinstance(current, float):
                            typed = float(value)
                        else:
                            typed = value
                        setattr(self.owl_instance, key, typed)
                        self.logger.info(f"Set {section}.{key} = {typed} on live instance")
                    except (ValueError, TypeError) as e:
                        self.logger.warning(f"Cannot convert {section}.{key}={value}: {e}")

                # Also update the config object for persistence
                with self.config_lock:
                    if hasattr(self.owl_instance, 'config'):
                        if not self.owl_instance.config.has_section(section):
                            self.owl_instance.config.add_section(section)
                        self.owl_instance.config.set(section, key, str(value))

            self.logger.info(f"Applied {len(params)} params to [{section}]")

        except Exception as e:
            self.logger.error(f"Error setting config section [{section}]: {e}")

    def _handle_save_config(self, filename=None):
        """Write current config to disk"""
        try:
            if not hasattr(self.owl_instance, 'config'):
                self.logger.error("Cannot save config - OWL has no config object")
                return

            if filename:
                # Save to a new file in the config directory
                config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
                save_path = os.path.join(config_dir, filename)

                # Safety: don't overwrite default presets
                basename = os.path.basename(save_path)
                if basename.startswith('DAY_SENSITIVITY_'):
                    self.logger.error(f"Cannot overwrite default preset: {basename}")
                    return
            else:
                save_path = self._resolve_config_path()
                if not save_path:
                    self.logger.error("Cannot save config - no config file path")
                    return

            with self.config_lock:
                with open(save_path, 'w') as f:
                    self.owl_instance.config.write(f)

            self.logger.info(f"Config saved to {save_path}")

        except Exception as e:
            self.logger.error(f"Error saving config: {e}")

    def _handle_set_active_config(self, config_path):
        """Write config path to active_config.txt"""
        try:
            config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
            active_path = os.path.join(config_dir, 'active_config.txt')

            with self.config_lock:
                with open(active_path, 'w') as f:
                    f.write(config_path.strip() + '\n')

            self.logger.info(f"Active config set to: {config_path}")

        except Exception as e:
            self.logger.error(f"Error setting active config: {e}")

    def _handle_restart_service(self):
        """Restart the owl.service via systemctl (same mechanism as standalone)"""
        try:
            import subprocess
            self.logger.warning("Restarting owl.service...")
            subprocess.Popen(
                ['sudo', 'systemctl', 'restart', 'owl.service'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.logger.error(f"Error restarting service: {e}")

    def _resolve_config_path(self):
        """Resolve the current config file path"""
        if self.config_file:
            return self.config_file

        if self.owl_instance and hasattr(self.owl_instance, 'config_path'):
            return self.owl_instance.config_path

        # Fallback: try active_config.txt
        config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
        active_path = os.path.join(config_dir, 'active_config.txt')
        if os.path.exists(active_path):
            with open(active_path, 'r') as f:
                return f.read().strip()

        return None

    def _apply_sensitivity_preset(self, preset):
        """
        Apply a sensitivity preset by loading config and updating Owl attributes.
        This follows the same pattern as AdvancedController.update_sensitivity_settings()
        """
        if self.owl_instance is None:
            self.logger.warning("Cannot apply preset - OWL instance not set")
            return

        # Map preset names to config files
        config_map = {
            'low': self.low_sensitivity_config,
            'medium': self.medium_sensitivity_config,
            'high': self.high_sensitivity_config
        }

        config_file = config_map.get(preset)
        if not config_file:
            self.logger.error(f"Unknown preset: {preset}")
            return

        try:
            # Load the config file
            config = configparser.ConfigParser()
            config.read(config_file)

            # Update Owl instance attributes DIRECTLY (same as AdvancedController does)
            self.owl_instance.exg_min = config.getint('GreenOnBrown', 'exg_min')
            self.owl_instance.exg_max = config.getint('GreenOnBrown', 'exg_max')
            self.owl_instance.hue_min = config.getint('GreenOnBrown', 'hue_min')
            self.owl_instance.hue_max = config.getint('GreenOnBrown', 'hue_max')
            self.owl_instance.saturation_min = config.getint('GreenOnBrown', 'saturation_min')
            self.owl_instance.saturation_max = config.getint('GreenOnBrown', 'saturation_max')
            self.owl_instance.brightness_min = config.getint('GreenOnBrown', 'brightness_min')
            self.owl_instance.brightness_max = config.getint('GreenOnBrown', 'brightness_max')

            # Update trackbars if display is active (same as AdvancedController)
            if self.owl_instance.show_display:
                cv2.setTrackbarPos("ExG-Min", self.owl_instance.window_name, self.owl_instance.exg_min)
                cv2.setTrackbarPos("ExG-Max", self.owl_instance.window_name, self.owl_instance.exg_max)
                cv2.setTrackbarPos("Hue-Min", self.owl_instance.window_name, self.owl_instance.hue_min)
                cv2.setTrackbarPos("Hue-Max", self.owl_instance.window_name, self.owl_instance.hue_max)
                cv2.setTrackbarPos("Sat-Min", self.owl_instance.window_name, self.owl_instance.saturation_min)
                cv2.setTrackbarPos("Sat-Max", self.owl_instance.window_name, self.owl_instance.saturation_max)
                cv2.setTrackbarPos("Bright-Min", self.owl_instance.window_name, self.owl_instance.brightness_min)
                cv2.setTrackbarPos("Bright-Max", self.owl_instance.window_name, self.owl_instance.brightness_max)

            self.logger.info(f"Applied {preset} sensitivity preset from {config_file}")

            # Sync updated parameters to state
            self._sync_parameters_to_state()

        except Exception as e:
            self.logger.error(f"Error applying preset {preset}: {e}")

    def _update_greenonbrown_param(self, param_name, param_value):
        """
        Update a single GreenOnBrown parameter in real-time.
        This allows fine-grained control from the GUI.
        """
        if self.owl_instance is None:
            self.logger.warning("Cannot update parameter - OWL instance not set")
            return

        # Validate parameter name
        valid_params = [
            'exg_min', 'exg_max', 'hue_min', 'hue_max',
            'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max',
            'min_detection_area'
        ]

        if param_name not in valid_params:
            self.logger.error(f"Invalid parameter name: {param_name}")
            return

        try:
            # Convert to int
            param_value = int(param_value)

            # Update the Owl instance attribute directly
            setattr(self.owl_instance, param_name, param_value)

            # Update trackbar if display is active
            if self.owl_instance.show_display:
                trackbar_map = {
                    'exg_min': 'ExG-Min',
                    'exg_max': 'ExG-Max',
                    'hue_min': 'Hue-Min',
                    'hue_max': 'Hue-Max',
                    'saturation_min': 'Sat-Min',
                    'saturation_max': 'Sat-Max',
                    'brightness_min': 'Bright-Min',
                    'brightness_max': 'Bright-Max'
                }
                trackbar_name = trackbar_map.get(param_name)
                if trackbar_name:
                    cv2.setTrackbarPos(trackbar_name, self.owl_instance.window_name, param_value)

            self.logger.info(f"Updated {param_name} = {param_value}")

            # Also update the config object so changes can be persisted if needed
            if hasattr(self.owl_instance, 'config'):
                self.owl_instance.config.set('GreenOnBrown', param_name, str(param_value))

            # Update state to reflect the change
            with self.state_lock:
                self.state[param_name] = param_value

        except Exception as e:
            self.logger.error(f"Error updating {param_name}: {e}")

    def _update_greenongreen_param(self, param_name, param_value):
        """Update a GreenOnGreen parameter in real-time. Only confidence can be hot-updated."""
        if self.owl_instance is None:
            self.logger.warning("Cannot update parameter - OWL instance not set")
            return

        if param_name == 'confidence':
            try:
                param_value = float(param_value)
                if hasattr(self.owl_instance, '_gog_confidence'):
                    self.owl_instance._gog_confidence = param_value
                    self.logger.info(f"Updated GreenOnGreen confidence = {param_value}")

                    if hasattr(self.owl_instance, 'config'):
                        self.owl_instance.config.set('GreenOnGreen', 'confidence', str(param_value))
                else:
                    self.logger.info(f"GreenOnGreen not active, storing config only")
                    if hasattr(self.owl_instance, 'config'):
                        self.owl_instance.config.set('GreenOnGreen', 'confidence', str(param_value))
            except Exception as e:
                self.logger.error(f"Error updating GreenOnGreen confidence: {e}")
        else:
            # Other params (model_path, detect_classes, actuation_mode) require restart
            self.logger.info(f"GreenOnGreen.{param_name} updated in config (restart required)")

    def _check_model_available(self):
        """Check if any YOLO model is available in the models/ directory."""
        try:
            models_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
            if not os.path.isdir(models_dir):
                return False
            # Check for NCNN subdirs (have .param files) or .pt files
            for item in os.listdir(models_dir):
                item_path = os.path.join(models_dir, item)
                if item.endswith('.pt'):
                    return True
                if os.path.isdir(item_path):
                    for f in os.listdir(item_path):
                        if f.endswith('.param'):
                            return True
            return False
        except Exception:
            return False

    def _handle_gps_update(self, gps_data):
        """Handle GPS updates from dashboard or central controller"""
        with self.state_lock:
            self.state.update({
                'gps_latitude': float(gps_data.get('latitude', 0.0)),
                'gps_longitude': float(gps_data.get('longitude', 0.0)),
                'gps_accuracy': float(gps_data.get('accuracy', 0.0)),
                'gps_timestamp': float(gps_data.get('timestamp', time.time())),
                'gps_available': True,
                'last_update': time.time()
            })

    def _monitor_states(self):
        """Monitor for state changes that need to trigger actions"""
        while self.running:
            try:
                time.sleep(0.5)

            except Exception as e:
                self.logger.error(f"Error in monitor_states: {e}")
                time.sleep(1)

    def _heartbeat_loop(self):
        """Publish periodic heartbeat to show OWL is alive"""
        heartbeat_interval = 2.0  # seconds

        while self.running:
            try:
                if self.connected:
                    with self.state_lock:
                        self._publish_state()
                time.sleep(heartbeat_interval)
            except Exception as e:
                self.logger.error(f"Error in heartbeat loop: {e}")
                time.sleep(heartbeat_interval)

    def _publish_state(self):
        """Publish current state to MQTT"""
        if self.connected:
            try:
                with self.state_lock:
                    state_copy = self.state.copy()

                self.client.publish(self.topics['state'], json.dumps(state_copy), retain=False)
            except Exception as e:
                self.logger.error(f"Error publishing state: {e}")

    # State update methods for owl.py to call
    def set_detection_enable(self, value):
        """Set detection enable state (for owl.py internal use)"""
        with self.state_lock:
            self.state['detection_enable'] = bool(value)
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_detection_mode(self, mode):
        """Set detection mode: 0=spot spray, 1=off, 2=blanket (for controller use)"""
        with self.state_lock:
            self.state['detection_mode'] = int(mode)
            # Also update detection_enable for backwards compatibility
            self.state['detection_enable'] = (mode == 0)
            self.state['last_update'] = time.time()
        self._publish_state()

    def set_image_sample_enable(self, value):
        """Set image sampling state (for owl.py internal use)"""
        with self.state_lock:
            self.state['image_sample_enable'] = bool(value)
            self.state['last_update'] = time.time()
        self._publish_state()

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

    def get_detection_enable(self):
        with self.state_lock:
            return self.state['detection_enable']

    def get_image_sample_enable(self):
        with self.state_lock:
            return self.state['image_sample_enable']

    def get_sensitivity_level(self):
        with self.state_lock:
            return self.state['sensitivity_level']

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

    def set_sensitivity_level(self, value):
        """Set sensitivity level (for owl.py internal use)"""
        with self.state_lock:
            old_value = self.state.get('sensitivity_level')
            if old_value == value:
                return

            self.state['sensitivity_level'] = value
            self.state['last_update'] = time.time()

        self._publish_state()
        self._apply_sensitivity_preset(value)
        self.logger.info(f"Sensitivity level changed from {old_value} to {value}")

    def update_system_stats(self, stats_dict):
        """
        Update system statistics from owl.py

        Args:
            stats_dict: Dictionary containing system stats from get_system_stats()
        """
        with self.state_lock:
            # Update all system stats
            self.state['cpu_percent'] = stats_dict.get('cpu_percent', 0)
            self.state['cpu_temp'] = stats_dict.get('cpu_temp', 0)
            self.state['memory_percent'] = stats_dict.get('memory_percent', 0)
            self.state['memory_used'] = stats_dict.get('memory_used', 0)
            self.state['memory_total'] = stats_dict.get('memory_total', 0)
            self.state['disk_percent'] = stats_dict.get('disk_percent', 0)
            self.state['disk_used'] = stats_dict.get('disk_used', 0)
            self.state['disk_total'] = stats_dict.get('disk_total', 0)
            self.state['fan_status'] = stats_dict.get('fan_status', {'is_rpi5': False, 'mode': 'unavailable', 'rpm': 0})
            self.state['owl_running'] = stats_dict.get('owl_running', True)  # owl.py is running if calling this
            self.state['last_update'] = time.time()

class DashMQTTSubscriber:
    """
    MQTT Subscriber for the control interfaces - subscribes to topics and sends commands
    For networked mode, use the central controller instead
    """

    def __init__(self, broker_host='localhost', broker_port=1883, client_id='owl_dashboard', device_id=None):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.client_id = client_id
        self.logger = logging.getLogger(__name__)

        # Determine if this is for networked or standalone mode
        self.networked_mode = (broker_host.lower() not in ['localhost', '127.0.0.1'])

        if self.networked_mode:
            self.logger.warning(
                "MQTTClient is designed for standalone mode. For networked mode, use the central controller.")
            # In networked mode, we need to know which device to control
            if device_id is None:
                self.logger.error("device_id required for networked mode")
                device_id = 'unknown'

        # Device ID (for networked mode)
        self.device_id = device_id or socket.gethostname()

        # MQTT topics
        if self.networked_mode and device_id:
            self.topics = {
                'commands': f'owl/{device_id}/commands',
                'state': f'owl/{device_id}/state',
                'status': f'owl/{device_id}/status',
                'detection': f'owl/{device_id}/detection',
                'config': f'owl/{device_id}/config',
                'indicators': f'owl/{device_id}/indicators',
                'errors': f'owl/{device_id}/errors',
                'gps': f'owl/{device_id}/gps'
            }
        else:
            # Standalone mode: simple topics
            self.topics = {
                'commands': 'owl/commands',
                'state': 'owl/state',
                'status': 'owl/status',
                'detection': 'owl/detection',
                'config': 'owl/config',
                'indicators': 'owl/indicators',
                'errors': 'owl/errors',
                'gps': 'owl/gps'
            }

        # Error log
        self.error_log = deque(maxlen=20)

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
            client.subscribe(self.topics['detection'])
            client.subscribe(self.topics['config'])
            client.subscribe(self.topics['indicators'])
            client.subscribe(self.topics['errors'])

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

        # Dispatch based on topic
        if topic == self.topics['state']:
            with self.state_lock:
                self.current_state = data
                self.last_heartbeat = time.time()

        elif topic == self.topics['status']:
            self.logger.info(f"OWL status: {data}")

        elif topic == self.topics['indicators']:
            self._handle_indicator(data)

        elif topic == self.topics['errors']:
            self._handle_error(data)

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

    def _handle_error(self, error_data):
        """Handles incoming error messages from owl.py"""
        try:
            self.logger.warning(f"Received error from owl.py: {error_data.get('message')}")
            with self.state_lock:
                self.error_log.append(error_data)
        except Exception as e:
            self.logger.error(f"Error while processing error message: {e}")

    def get_and_clear_errors(self):
        """Atomically retrieves and clears the current error log."""
        with self.state_lock:
            errors_to_send = list(self.error_log)
            self.error_log.clear()
        return errors_to_send

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

    def get_sensitivity_level(self):
        """Get current sensitivity level as string"""
        with self.state_lock:
            return self.current_state.get('sensitivity_level', 'medium')

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

    def set_sensitivity_level(self, level):
        """Set sensitivity level"""
        valid_levels = ['low', 'medium', 'high']
        level = level.lower()

        if level not in valid_levels:
            return {'success': False, 'error': f'Invalid sensitivity level. Valid options: {valid_levels}'}

        return self._send_command('set_sensitivity_level', level=level)

    def set_greenonbrown_param(self, param_name, param_value):
        """Update a single GreenOnBrown parameter"""
        valid_params = [
            'exg_min', 'exg_max', 'hue_min', 'hue_max',
            'saturation_min', 'saturation_max', 'brightness_min', 'brightness_max'
        ]

        if param_name not in valid_params:
            return {'success': False, 'error': f'Invalid parameter name. Valid options: {valid_params}'}

        return self._send_command('set_greenonbrown_param', param=param_name, value=param_value)

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