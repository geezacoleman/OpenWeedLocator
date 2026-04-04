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
import sys
import configparser
from pathlib import Path
import socket

from collections import deque
from utils.config_manager import GREENONBROWN_PARAMS
from utils.directory_manager import scan_sessions, collect_session_files

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

    def __init__(self, broker_host='localhost', broker_port=1883, client_id='owl_main', device_id=None, network_mode=None, static_ip=None):
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
            'static_ip': static_ip or '',
            # Algorithm state
            'algorithm': 'exhsv',
            'model_available': False,
            'crop_buffer_px': 20,
            'inference_resolution': 320,
            # GreenOnGreen parameters
            'confidence': 0.5,
            # AI tab: model + class info
            'current_model': '',
            'available_models': [],
            'model_classes': {},
            'detect_classes': [],
            # GreenOnBrown parameters (will be populated on first update)
            'exg_min': None,
            'exg_max': None,
            'hue_min': None,
            'hue_max': None,
            'saturation_min': None,
            'saturation_max': None,
            'brightness_min': None,
            'brightness_max': None,
            # Actuation state
            'avg_loop_time_ms': 0.0,
            'actuation_duration': 0.15,
            'delay': 0.0,
            'actuation_source': 'config',
            # Camera resolution
            'resolution_width': 0,
            'resolution_height': 0,
            # Model download state
            'model_download': {
                'status': 'idle',
                'model_name': '',
                'progress': 0,
                'error': ''
            },
            # Storage
            'save_directory': '',
            # Session metadata (filled by farmer via dashboard)
            'session_metadata': {
                'field_name': '',
                'crop': '',
                'weather': '',
                'vehicle': '',
            },
            # Data sessions and transfer state
            'data_sessions': [],
            'data_transfer': {
                'status': 'idle',
                'session_date': '',
                'progress': 0,
                'bytes_sent': 0,
                'bytes_total': 0,
                'error': ''
            }
        }

        # Thread safety
        self.state_lock = threading.RLock()
        self.config_lock = threading.Lock()

        # Config file path (set via set_owl_instance or directly)
        self.config_file = None

        # OWL instance reference
        self.owl_instance = None
        self.sensitivity_manager = None

        self.last_sensitivity_level = 'medium'
        self.monitoring_thread = None
        self.heartbeat_thread = None

        # MQTT client
        self.client = mqtt.Client(client_id=self.client_id)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        # Enable paho's built-in auto-reconnect after initial connection is lost
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        # Last Will & Testament — broker publishes this if OWL disconnects ungracefully
        lwt_payload = json.dumps({
            'device_id': self.device_id,
            'owl_running': False,
            'connected': False,
            'timestamp': time.time()
        })
        self.client.will_set(self.topics['status'], lwt_payload, qos=1, retain=True)

        # Connection state
        self.connected = False
        self.running = False
        self.connection_attempts = 0
        self.max_connection_attempts = 5
        self._reconnect_thread = None

    def set_owl_instance(self, owl_instance):
        """Set reference to owl instance and sensitivity manager"""
        self.owl_instance = owl_instance
        self.sensitivity_manager = getattr(owl_instance, 'sensitivity_manager', None)

        # Capture the config file path from the owl instance
        if hasattr(owl_instance, 'config_path'):
            self.config_file = owl_instance.config_path

        self.logger.info("OWL instance configured")
        if self.sensitivity_manager:
            active = self.sensitivity_manager.get_active_preset()
            self.logger.info(f"  Active sensitivity preset: {active}")

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

            self.state['confidence'] = getattr(
                self.owl_instance, '_gog_confidence', 0.5)

            # Algorithm state
            self.state['algorithm'] = self.owl_instance.config.get(
                'System', 'algorithm', fallback='exhsv')
            self.state['crop_buffer_px'] = getattr(
                self.owl_instance, 'crop_buffer_px', 20)
            self.state['inference_resolution'] = getattr(
                self.owl_instance, 'inference_resolution', 320)

            # Save directory (runtime-resolved — may differ from config if USB path changed)
            self.state['save_directory'] = getattr(self.owl_instance, 'save_directory', '')

            # Camera resolution
            res = getattr(self.owl_instance, 'resolution', (0, 0))
            self.state['resolution_width'] = res[0]
            self.state['resolution_height'] = res[1]

            # Check model availability (any NCNN dirs or .pt files in models/)
            self.state['model_available'] = self._check_model_available()

            # AI tab: available models, current model, class names
            self.state['available_models'] = self._list_available_models()
            self.state['detect_classes'] = getattr(self.owl_instance, '_detect_classes_list', [])
            gog = getattr(self.owl_instance, '_gog_detector', None)
            # Use pending model name if OWL hasn't processed the swap yet,
            # so the dashboard dropdown doesn't snap back to the old model.
            pending_model = getattr(self.owl_instance, '_pending_model', None)
            if pending_model is not None:
                pass  # keep current_model as set by the set_model handler
            elif gog and hasattr(gog, 'model'):
                self.state['current_model'] = getattr(gog, '_model_filename', '')
                self.state['model_classes'] = {str(k): v for k, v in gog.model.names.items()}
            else:
                self.state['current_model'] = ''
                self.state['model_classes'] = {}

            # Tracking state
            self.state['tracking_enabled'] = getattr(
                self.owl_instance, 'tracking_enabled', False)

            # Sensitivity presets
            if self.sensitivity_manager:
                self.state['sensitivity_level'] = self.sensitivity_manager.get_active_preset()
                self.state['sensitivity_presets'] = self.sensitivity_manager.list_presets()

    def start(self):
        """Start the MQTT IPC server.

        Starts heartbeat and monitoring threads immediately (they guard on
        self.connected). If the broker is unreachable, a background reconnect
        thread retries with exponential backoff instead of giving up.
        """
        self.running = True

        with self.state_lock:
            self.state['owl_running'] = True

        # Start threads first — they already guard on self.connected
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

        self.monitoring_thread = threading.Thread(target=self._monitor_states, daemon=True)
        self.monitoring_thread.start()

        # Try to connect to broker
        try:
            self.logger.info(f"Attempting to connect to MQTT broker at {self.broker_host}:{self.broker_port}")
            self.client.connect(self.broker_host, self.broker_port, 60)
            self.client.loop_start()

            # Publish initial state
            self._publish_state()

            self.logger.info(f"MQTT IPC Server started successfully")
            self.logger.info(f"Device ID: {self.device_id}")
            self.logger.info(f"Publishing to topics: {list(self.topics.values())}")

        except Exception as e:
            if self.networked_mode:
                self.logger.warning(f"Could not connect to network broker at {self.broker_host}:{self.broker_port}: {e}")
                self.logger.warning("OWL will continue to operate locally — reconnecting in background")
            else:
                self.logger.warning(f"Could not connect to local MQTT broker: {e}")
                self.logger.warning("Dashboard features disabled — reconnecting in background")

            # Start background reconnect instead of giving up
            self._reconnect_thread = threading.Thread(target=self._background_reconnect, daemon=True)
            self._reconnect_thread.start()

    def _background_reconnect(self):
        """Retry broker connection with exponential backoff (2s → 30s cap)."""
        delay = 2
        max_delay = 30

        while self.running and not self.connected:
            self.logger.info(f"MQTT reconnect: trying {self.broker_host}:{self.broker_port} in {delay}s...")
            time.sleep(delay)

            if not self.running:
                break

            try:
                self.client.connect(self.broker_host, self.broker_port, 60)
                self.client.loop_start()
                # _on_connect callback sets self.connected = True
                self.logger.info("MQTT reconnect: connection attempt sent, waiting for callback")
                break
            except Exception as e:
                self.logger.warning(f"MQTT reconnect failed: {e}")
                delay = min(delay * 2, max_delay)

    def stop(self):
        """Stop the MQTT server"""
        self.running = False

        # Mark OWL as stopped
        with self.state_lock:
            self.state['owl_running'] = False

        if self.connected:
            try:
                self.client.publish(self.topics['status'], json.dumps({
                    'device_id': self.device_id,
                    'owl_running': False,
                    'timestamp': time.time()
                }), retain=True)

                # Also update main state
                self._publish_state()
            except Exception:
                pass  # Best-effort during shutdown

        try:
            self.client.loop_stop()
        except Exception:
            pass  # Client may never have started the loop

        try:
            self.client.disconnect()
        except Exception:
            pass  # Client may never have connected

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

            # Publish full state immediately so controller sees everything
            # (don't wait for next 2s heartbeat cycle)
            self._publish_state()

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
            self.logger.error(f"Error processing MQTT message on topic {msg.topic}: {e}", exc_info=True)
            print(f"[OWL MQTT ERROR] {msg.topic}: {e}", file=sys.stderr)

    def _handle_command(self, command):
        """Handle control commands from dashboard or central controller"""
        action = command.get('action')

        self.logger.info(f"Received command: {action}")

        with self.state_lock:
            if action == 'set_detection_enable':
                self.state['detection_enable'] = bool(command.get('value', False))
                self.logger.info(f"Detection enable set to: {self.state['detection_enable']}")

            elif action == 'set_image_sample_enable':
                was_recording = self.state['image_sample_enable']
                self.state['image_sample_enable'] = bool(command.get('value', False))
                self.logger.info(f"Image sample enable set to: {self.state['image_sample_enable']}")

                # Auto-save session metadata when recording stops
                if was_recording and not self.state['image_sample_enable']:
                    self._auto_save_session_metadata()

            elif action == 'set_sensitivity_level':
                level = command.get('level', '').lower()

                if self.sensitivity_manager:
                    valid_names = {p['name'] for p in self.sensitivity_manager.list_presets()}
                    if level not in valid_names:
                        self.logger.error(f"Invalid sensitivity preset: {level}")
                        return
                else:
                    if level not in ('low', 'medium', 'high'):
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
                try:
                    from utils.config_manager import ConfigValidator
                    valid = ConfigValidator.get_valid_algorithms()
                except Exception:
                    valid = {'exg', 'exgr', 'maxg', 'nexg', 'exhsv', 'hsv', 'gndvi', 'gog', 'gog-hybrid'}
                if value in valid:
                    self.state['algorithm'] = value
                    if self.owl_instance:
                        self.owl_instance._pending_algorithm = value
                        # Update config so heartbeat reads new value immediately
                        if hasattr(self.owl_instance, 'config'):
                            self.owl_instance.config.set('System', 'algorithm', value)
                    self.logger.info(f"Algorithm set to: {value}")
                else:
                    self.logger.error(f"Invalid algorithm: {value}")

            elif action == 'install_algorithm':
                algo_name = command.get('name', '')
                algo_code = command.get('code', '')
                if algo_name and algo_code:
                    try:
                        from custom_algorithms import validate_algorithm_code, save_algorithm
                        ok, err = validate_algorithm_code(algo_code)
                        if ok:
                            result = save_algorithm(algo_name, algo_code,
                                                    command.get('description', ''))
                            if result.get('success'):
                                self.logger.info(f"Custom algorithm installed: {algo_name}")
                            else:
                                self.logger.error(f"Custom algorithm save failed: {result.get('error')}")
                        else:
                            self.logger.error(f"Custom algorithm validation failed: {err}")
                    except Exception as e:
                        self.logger.error(f"Custom algorithm install error: {e}")

            elif action == 'set_greenongreen_param':
                key = command.get('key')
                value = command.get('value')
                if key and value is not None:
                    self._update_greenongreen_param(key, value)

            elif action == 'set_config':
                key = command.get('key')
                value = command.get('value')
                if key is not None and value is not None:
                    self._update_greenonbrown_param(key, value)

            elif action == 'set_crop_buffer':
                try:
                    value = max(0, min(50, int(command.get('value', 20))))
                    self.state['crop_buffer_px'] = value
                    if self.owl_instance:
                        self.owl_instance.crop_buffer_px = value
                    self.logger.info(f"Crop buffer set to: {value}px")
                except (ValueError, TypeError) as e:
                    self.logger.error(f"Invalid crop_buffer value: {e}")

            elif action == 'set_detect_classes':
                raw = command.get('value', [])
                if isinstance(raw, str):
                    class_list = [c.strip() for c in raw.split(',') if c.strip()]
                elif isinstance(raw, list):
                    class_list = [str(c).strip() for c in raw if str(c).strip()]
                else:
                    class_list = []
                if self.owl_instance:
                    self.owl_instance._pending_detect_classes = class_list
                self.state['detect_classes'] = class_list
                self.logger.info(f"detect_classes queued: {class_list}")
                self._publish_state()

            elif action == 'set_model':
                model_name = command.get('value', '')
                if model_name and self.owl_instance:
                    # Resolve to models/ directory path so GreenOnGreen can find it
                    model_path = os.path.join('models', str(model_name))
                    self.owl_instance._pending_model = model_path
                    # Immediately report new model so dashboard doesn't snap back
                    self.state['current_model'] = str(model_name)
                    self.logger.info(f"Model switch queued: {model_path}")
                    self._publish_state()

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

            elif action == 'set_detection_mode':
                mode = int(command.get('value', 1))
                valid_modes = {0, 1, 2}
                if mode not in valid_modes:
                    self.logger.error(f"Invalid detection mode: {mode}")
                    return
                self.state['detection_mode'] = mode
                if mode == 2:  # Blanket — all nozzles on
                    self.state['detection_enable'] = False
                    if self.owl_instance and hasattr(self.owl_instance, 'relay_controller'):
                        self.owl_instance.relay_controller.relay.all_on()
                    self.logger.info("Blanket spray: all nozzles ON, detection disabled")
                elif mode == 0:  # Spot spray — detection on
                    self.state['detection_enable'] = True
                    if self.owl_instance and hasattr(self.owl_instance, 'relay_controller'):
                        self.owl_instance.relay_controller.relay.all_off()
                    self.logger.info("Spot spray: detection enabled, nozzles auto")
                else:  # Off
                    self.state['detection_enable'] = False
                    if self.owl_instance and hasattr(self.owl_instance, 'relay_controller'):
                        self.owl_instance.relay_controller.relay.all_off()
                    self.logger.info("Off: detection disabled, all nozzles OFF")

            elif action == 'set_actuation_params':
                self._handle_set_actuation_params(command)

            elif action == 'download_model':
                url = command.get('url')
                filename = command.get('filename')
                sha256 = command.get('sha256', '')
                is_archive = command.get('is_archive', False)
                if url and filename:
                    threading.Thread(
                        target=self._download_model,
                        args=(url, filename, sha256, is_archive),
                        daemon=True
                    ).start()
                else:
                    self.logger.error("download_model missing url or filename")

            elif action == 'save_sensitivity_preset':
                name = command.get('name', '').strip()
                if name and self.sensitivity_manager and self.owl_instance:
                    success = self.sensitivity_manager.save_custom_preset(
                        name, owl_instance=self.owl_instance)
                    if success:
                        self.logger.info(f"Saved sensitivity preset: {name}")
                    else:
                        self.logger.error(f"Failed to save preset: {name}")
                    self._sync_parameters_to_state()

            elif action == 'delete_sensitivity_preset':
                name = command.get('name', '').strip()
                if name and self.sensitivity_manager:
                    success = self.sensitivity_manager.delete_custom_preset(name)
                    if success:
                        self.logger.info(f"Deleted sensitivity preset: {name}")
                    else:
                        self.logger.error(f"Failed to delete preset: {name}")
                    self._sync_parameters_to_state()

            elif action == 'list_sensitivity_presets':
                # Just sync — presets are published as part of state
                self._sync_parameters_to_state()

            elif action == 'set_tracking':
                raw = command.get('value', False)
                value = raw if isinstance(raw, bool) else str(raw).lower() == 'true'
                self.state['tracking_enabled'] = value
                if self.owl_instance:
                    self.owl_instance.tracking_enabled = value
                    # Create or clear smoother/stabilizer
                    if value and not getattr(self.owl_instance, '_class_smoother', None):
                        from utils.tracker import ClassSmoother, CropMaskStabilizer
                        window = getattr(self.owl_instance, '_track_class_window', 5)
                        persist = getattr(self.owl_instance, '_track_crop_persist', 3)
                        self.owl_instance._class_smoother = ClassSmoother(window=window)
                        self.owl_instance._crop_stabilizer = CropMaskStabilizer(max_age=persist)
                    elif not value:
                        if getattr(self.owl_instance, '_class_smoother', None):
                            self.owl_instance._class_smoother.reset()
                        if getattr(self.owl_instance, '_crop_stabilizer', None):
                            self.owl_instance._crop_stabilizer.reset()
                    # Update detector's tracking_enabled flag
                    gog = getattr(self.owl_instance, '_gog_detector', None)
                    if gog and hasattr(gog, 'tracking_enabled'):
                        gog.tracking_enabled = value
                        if not value:
                            gog.reset_tracker()
                        elif value and self.owl_instance._crop_stabilizer:
                            gog._crop_stabilizer = self.owl_instance._crop_stabilizer
                self.logger.info(f"Tracking {'enabled' if value else 'disabled'}")
                self._publish_state()

            elif action == 'set_session_metadata':
                metadata = {
                    'field_name': str(command.get('field_name', '')).strip(),
                    'crop': str(command.get('crop', '')).strip(),
                    'weather': str(command.get('weather', '')).strip(),
                    'vehicle': str(command.get('vehicle', '')).strip(),
                }
                self.state['session_metadata'] = metadata
                self._write_session_metadata(metadata)
                self.logger.info(f"Session metadata saved: {metadata}")

            elif action == 'list_data_sessions':
                threading.Thread(
                    target=self._list_data_sessions,
                    daemon=True
                ).start()

            elif action == 'transfer_session':
                # session_id (YYYYMMDD/session_HHMMSS) preferred; session_date (YYYYMMDD) for legacy
                session_date = command.get('session_id') or command.get('session_date', '')
                data_types = command.get('data_types', ['images', 'logs', 'tracks', 'config'])
                upload_url = command.get('upload_url', '')
                if session_date and upload_url:
                    threading.Thread(
                        target=self._upload_session,
                        args=(session_date, data_types, upload_url),
                        daemon=True
                    ).start()
                else:
                    self.logger.error("transfer_session missing session_date or upload_url")

            elif action == 'delete_session':
                session_date = command.get('session_id') or command.get('session_date', '')
                data_types = command.get('data_types', ['images'])
                if session_date:
                    threading.Thread(
                        target=self._delete_session,
                        args=(session_date, data_types),
                        daemon=True
                    ).start()
                else:
                    self.logger.error("delete_session missing session_date")

            elif action == 'reboot':
                self.logger.warning("Reboot command received - this requires system privileges")
                # Future implementation

            elif action == 'restart_service':
                self.logger.warning("Service restart command received")
                self._handle_restart_service()

            elif action == 'shutdown':
                self.logger.warning("Shutdown command received")
                self._handle_shutdown()

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
            gob_changed = False
            for key, value in params.items():
                # Route threshold keys through _update_greenonbrown_param
                # regardless of section name (defense-in-depth)
                if key in GREENONBROWN_PARAMS:
                    self._update_greenonbrown_param(key, value)
                    gob_changed = True
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

                # Also update the config object for persistence (non-GoB keys only;
                # _update_greenonbrown_param handles GoB config internally)
                if key not in GREENONBROWN_PARAMS:
                    with self.config_lock:
                        if hasattr(self.owl_instance, 'config'):
                            if not self.owl_instance.config.has_section(section):
                                self.owl_instance.config.add_section(section)
                            self.owl_instance.config.set(section, key, str(value))

            self.logger.info(f"Applied {len(params)} params to [{section}]")

            # Push ByteTrack params to live tracker when Tracking section changes
            if section == 'Tracking':
                # Route tracking_enabled through set_tracking handler (creates
                # ClassSmoother/CropMaskStabilizer and sets gog.tracking_enabled)
                if 'tracking_enabled' in params:
                    self._handle_command({
                        'action': 'set_tracking',
                        'value': params['tracking_enabled']
                    })

                tracker_keys = {'track_high_thresh', 'track_low_thresh',
                                'new_track_thresh', 'track_buffer', 'match_thresh'}
                if tracker_keys & set(params.keys()):
                    gog = getattr(self.owl_instance, '_gog_detector', None)
                    if gog and hasattr(gog, 'update_tracker_params_direct'):
                        tracker_params = {}
                        for k in tracker_keys:
                            val = getattr(self.owl_instance, k, None)
                            if val is not None:
                                tracker_params[k] = val
                        gog.update_tracker_params_direct(tracker_params)

                if 'detection_persist_frames' in params:
                    gog = getattr(self.owl_instance, '_gog_detector', None)
                    if gog:
                        gog.detection_persist_frames = getattr(
                            self.owl_instance, 'detection_persist_frames', 0)

            # Ensure dashboard sees the changes immediately
            self._sync_parameters_to_state()
            self._publish_state()

            # Auto-persist GreenOnBrown threshold changes (copy-on-write safe)
            if gob_changed:
                self._handle_save_config()

        except Exception as e:
            self.logger.error(f"Error setting config section [{section}]: {e}")

    def _handle_save_config(self, filename=None):
        """Write current config to disk.

        Copy-on-write: if the resolved path is a protected default
        (GENERAL_CONFIG.ini, CONTROLLER.ini), creates a timestamped copy
        and updates active_config.txt so the default is never overwritten.
        """
        try:
            if not hasattr(self.owl_instance, 'config'):
                self.logger.error("Cannot save config - OWL has no config object")
                return

            config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config')
            protected = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']

            if filename:
                # Save to a new file in the config directory
                save_path = os.path.join(config_dir, filename)

                # Safety: don't overwrite default presets
                basename = os.path.basename(save_path)
                if basename in protected:
                    self.logger.error(f"Cannot overwrite default preset: {basename}")
                    return
            else:
                save_path = self._resolve_config_path()
                if not save_path:
                    self.logger.error("Cannot save config - no config file path")
                    return

                # Copy-on-write: protected defaults get a timestamped copy
                basename = os.path.basename(save_path)
                if basename in protected:
                    from datetime import datetime
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    new_name = f'config_{timestamp}.ini'
                    save_path = os.path.join(config_dir, new_name)
                    self.logger.info(
                        f"Protected default {basename} — saving as {new_name}")

                    # Update active_config.txt to point to the new file
                    self._handle_set_active_config(f'config/{new_name}')

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

    def _handle_shutdown(self):
        """Shut down the system (called via MQTT from central controller).

        Resolves shutdown binary path to match the sudoers entry created
        by controller/shared/setup.sh (command -v shutdown).
        """
        try:
            import shutil
            import subprocess
            shutdown_bin = shutil.which('shutdown') or '/usr/sbin/shutdown'
            self.logger.warning("Shutting down system...")
            subprocess.Popen(
                ['sudo', shutdown_bin, 'now'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            self.logger.error(f"Error shutting down: {e}")

    def _handle_set_actuation_params(self, command):
        """Handle actuation parameter updates from central controller"""
        MIN_DURATION = 0.01
        MAX_DURATION = 5.0

        try:
            duration = float(command.get('actuation_duration', 0))
            delay = float(command.get('delay', 0))
            source = command.get('source', 'controller')

            # Clamp to safety bounds
            duration = max(MIN_DURATION, min(MAX_DURATION, duration))
            delay = max(0.0, min(MAX_DURATION, delay))

            if self.owl_instance:
                self.owl_instance.actuation_duration = duration
                self.owl_instance.delay = delay

            prev_duration = self.state.get('actuation_duration')
            prev_delay = self.state.get('delay')

            self.state['actuation_duration'] = duration
            self.state['delay'] = delay
            self.state['actuation_source'] = source

            if duration != prev_duration or delay != prev_delay:
                self.logger.info(f"Actuation params updated: duration={duration:.4f}s, delay={delay:.4f}s, source={source}")

        except (ValueError, TypeError) as e:
            self.logger.error(f"Invalid actuation params: {e}")

    def _download_model(self, url, filename, expected_sha256, is_archive):
        """Download a model file from the controller. Runs in background thread."""
        import urllib.request
        import ssl
        import hashlib
        import tempfile

        models_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
        os.makedirs(models_dir, exist_ok=True)

        tmp_path = None
        try:
            # Update state: downloading
            with self.state_lock:
                self.state['model_download'] = {
                    'status': 'downloading',
                    'model_name': filename,
                    'progress': 0,
                    'error': ''
                }
            self._publish_state()

            # SSL context for self-signed certs
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            req = urllib.request.Request(url)
            response = urllib.request.urlopen(req, context=ssl_ctx)
            total_size = int(response.headers.get('Content-Length', 0))

            # Download to temp file
            tmp_fd, tmp_path = tempfile.mkstemp(dir=models_dir, suffix='.tmp')
            downloaded = 0
            last_progress_update = 0

            with os.fdopen(tmp_fd, 'wb') as f:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    # Update progress every 100KB
                    if downloaded - last_progress_update >= 102400:
                        last_progress_update = downloaded
                        progress = int((downloaded / total_size * 100)) if total_size > 0 else 0
                        with self.state_lock:
                            self.state['model_download']['progress'] = progress
                        self._publish_state()

            # Place the file, then verify SHA256
            if is_archive:
                # Extract zip to a directory
                import zipfile
                dir_name = filename.rsplit('.', 1)[0] if '.' in filename else filename
                extract_dir = os.path.join(models_dir, dir_name)
                if os.path.isdir(extract_dir):
                    import shutil
                    shutil.rmtree(extract_dir)
                os.makedirs(extract_dir)

                with zipfile.ZipFile(tmp_path, 'r') as zf:
                    zf.extractall(extract_dir)

                os.unlink(tmp_path)
                tmp_path = None

                # Verify SHA256 of extracted directory contents (matches
                # how the controller computed it at upload time)
                if expected_sha256:
                    h = hashlib.sha256()
                    for fp in sorted(Path(extract_dir).rglob('*')):
                        if fp.is_file():
                            with open(fp, 'rb') as hf:
                                while True:
                                    chunk = hf.read(65536)
                                    if not chunk:
                                        break
                                    h.update(chunk)
                    actual_sha256 = h.hexdigest()
                    if actual_sha256 != expected_sha256:
                        import shutil
                        shutil.rmtree(extract_dir)
                        raise ValueError(
                            f'SHA256 mismatch: expected {expected_sha256[:12]}..., '
                            f'got {actual_sha256[:12]}...'
                        )
                self.logger.info(f"Model extracted to {extract_dir}")
            else:
                # Verify SHA256 of the file directly
                if expected_sha256:
                    h = hashlib.sha256()
                    with open(tmp_path, 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            h.update(chunk)
                    actual_sha256 = h.hexdigest()
                    if actual_sha256 != expected_sha256:
                        raise ValueError(
                            f'SHA256 mismatch: expected {expected_sha256[:12]}..., '
                            f'got {actual_sha256[:12]}...'
                        )

                # Atomic rename
                final_path = os.path.join(models_dir, filename)
                if os.path.exists(final_path):
                    os.unlink(final_path)
                os.rename(tmp_path, final_path)
                tmp_path = None
                self.logger.info(f"Model saved to {final_path}")

            # Update state: complete
            with self.state_lock:
                self.state['model_download'] = {
                    'status': 'complete',
                    'model_name': filename,
                    'progress': 100,
                    'error': ''
                }
                # Refresh available models list
                self.state['available_models'] = self._list_available_models()
            self._publish_state()

        except Exception as e:
            self.logger.error(f"Model download failed: {e}")
            # Cleanup temp file
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            with self.state_lock:
                self.state['model_download'] = {
                    'status': 'error',
                    'model_name': filename,
                    'progress': 0,
                    'error': str(e)
                }
            self._publish_state()

    def _write_session_metadata(self, metadata):
        """Write session_metadata.json to the active session directory.

        Uses the ImageRecorder's save_directory (the active session subfolder)
        if available, otherwise falls back to the most recent date directory.
        """
        # Prefer the active session dir from ImageRecorder
        session_path = None
        if self.owl_instance:
            recorder = getattr(self.owl_instance, 'image_recorder', None)
            if recorder:
                rec_dir = getattr(recorder, 'save_directory', None)
                if rec_dir and os.path.isdir(rec_dir):
                    session_path = rec_dir

        # Fallback: find most recent YYYYMMDD dir (or session subdir within it)
        if not session_path:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None
            if not save_dir or not os.path.isdir(save_dir):
                self.logger.warning("Cannot write session metadata: save_directory not available")
                return

            import re
            date_pattern = re.compile(r'^\d{8}$')
            session_pattern = re.compile(r'^session_\d{6}$')

            for date_dir in sorted(os.listdir(save_dir), reverse=True):
                date_path = os.path.join(save_dir, date_dir)
                if not os.path.isdir(date_path) or not date_pattern.match(date_dir):
                    continue
                # Check for session subdirs
                subdirs = sorted([d for d in os.listdir(date_path)
                                  if os.path.isdir(os.path.join(date_path, d)) and session_pattern.match(d)],
                                 reverse=True)
                if subdirs:
                    session_path = os.path.join(date_path, subdirs[0])
                else:
                    session_path = date_path
                break

        if not session_path:
            self.logger.warning("Cannot write session metadata: no session directories found")
            return

        metadata_path = os.path.join(session_path, 'session_metadata.json')

        import json
        from datetime import datetime
        payload = dict(metadata)
        payload['recorded_at'] = datetime.now().isoformat(timespec='seconds')
        payload['owl_id'] = self.state.get('device_id', '')

        try:
            with open(metadata_path, 'w') as f:
                json.dump(payload, f, indent=2)
            self.logger.info(f"Session metadata written to {metadata_path}")
        except OSError as e:
            self.logger.error(f"Failed to write session metadata: {e}")

    def _auto_save_session_metadata(self):
        """Auto-save session metadata when recording stops (if any fields are non-empty)."""
        with self.state_lock:
            metadata = self.state.get('session_metadata', {})

        # Only save if at least one field has content
        if any(v.strip() for v in metadata.values() if isinstance(v, str)):
            self._write_session_metadata(metadata)

    def _list_data_sessions(self):
        """Scan save_directory for recording sessions. Publish to state."""
        try:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None
            sessions = scan_sessions(save_dir)

            with self.state_lock:
                self.state['data_sessions'] = sessions
            self._publish_state()

        except Exception as e:
            self.logger.error(f"Error listing data sessions: {e}")
            with self.state_lock:
                self.state['data_sessions'] = []
            self._publish_state()

    def _upload_session(self, session_date, data_types, upload_url):
        """ZIP and upload a data session to the controller. Runs in background thread.

        session_date can be "YYYYMMDD" (all sessions under that date) or
        "YYYYMMDD/session_HHMMSS" (specific session).
        """
        import re
        import urllib.request
        import ssl
        import tempfile
        import zipfile

        # Validate format: date or date/session
        if not re.match(r'^\d{8}(/session_\d{6})?$', session_date):
            self.logger.error(f"Invalid session identifier: {session_date}")
            with self.state_lock:
                self.state['data_transfer'] = {
                    'status': 'error', 'session_date': session_date,
                    'progress': 0, 'bytes_sent': 0, 'bytes_total': 0,
                    'error': 'Invalid session identifier'
                }
            self._publish_state()
            return

        # Guard: one transfer at a time
        with self.state_lock:
            if self.state['data_transfer']['status'] not in ('idle', 'complete', 'error'):
                self.logger.warning("Transfer already in progress")
                return
            self.state['data_transfer'] = {
                'status': 'scanning', 'session_date': session_date,
                'progress': 0, 'bytes_sent': 0, 'bytes_total': 0, 'error': ''
            }
        self._publish_state()

        tmp_path = None
        try:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None

            # Collect files to zip using shared scanner
            files_to_zip = []
            if 'images' in data_types and save_dir:
                files_to_zip = collect_session_files(save_dir, session_date)

            if not files_to_zip:
                with self.state_lock:
                    self.state['data_transfer'] = {
                        'status': 'error', 'session_date': session_date,
                        'progress': 0, 'bytes_sent': 0, 'bytes_total': 0,
                        'error': 'No files found for this session'
                    }
                self._publish_state()
                return

            # Create temp ZIP (ZIP_STORED — JPEGs already compressed)
            with self.state_lock:
                self.state['data_transfer']['status'] = 'zipping'
            self._publish_state()

            tmp_fd, tmp_path = tempfile.mkstemp(suffix='.zip')
            with os.fdopen(tmp_fd, 'wb') as tmp_f:
                with zipfile.ZipFile(tmp_f, 'w', zipfile.ZIP_STORED) as zf:
                    for i, (arc_name, full_path) in enumerate(files_to_zip):
                        zf.write(full_path, arc_name)
                        # Progress during zipping: 0-50%
                        progress = int((i + 1) / len(files_to_zip) * 50)
                        with self.state_lock:
                            self.state['data_transfer']['progress'] = progress
                        if (i + 1) % 10 == 0:
                            self._publish_state()

            zip_size = os.path.getsize(tmp_path)

            # Upload via HTTP POST
            with self.state_lock:
                self.state['data_transfer']['status'] = 'uploading'
                self.state['data_transfer']['bytes_total'] = zip_size
            self._publish_state()

            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            # Stream the file in chunks — never load entire ZIP into memory
            # Use a wrapper that tracks bytes read for progress reporting
            upload_state = self.state['data_transfer']

            class ProgressReader:
                """File wrapper that publishes upload progress via MQTT."""
                def __init__(inner_self, fh, total, publisher):
                    inner_self.fh = fh
                    inner_self.total = total
                    inner_self.sent = 0
                    inner_self.publisher = publisher
                    inner_self.last_publish = 0

                def read(inner_self, size=-1):
                    chunk = inner_self.fh.read(size)
                    inner_self.sent += len(chunk)
                    # Progress 50-100% during upload (0-50% was zipping)
                    if inner_self.total > 0:
                        pct = 50 + int(inner_self.sent / inner_self.total * 50)
                    else:
                        pct = 100
                    with inner_self.publisher.state_lock:
                        inner_self.publisher.state['data_transfer']['progress'] = pct
                        inner_self.publisher.state['data_transfer']['bytes_sent'] = inner_self.sent
                    # Publish every 100KB
                    if inner_self.sent - inner_self.last_publish >= 102400:
                        inner_self.last_publish = inner_self.sent
                        inner_self.publisher._publish_state()
                    return chunk

                def __len__(inner_self):
                    return inner_self.total

            with open(tmp_path, 'rb') as f:
                progress_fh = ProgressReader(f, zip_size, self)
                req = urllib.request.Request(
                    upload_url,
                    data=progress_fh,
                    method='POST',
                    headers={
                        'Content-Type': 'application/octet-stream',
                        'X-OWL-Device-ID': self.device_id,
                        'X-OWL-Session-Date': session_date,
                        'Content-Length': str(zip_size),
                    }
                )

                response = urllib.request.urlopen(req, context=ssl_ctx, timeout=300)
            resp_code = response.getcode()

            if resp_code in (200, 201):
                with self.state_lock:
                    self.state['data_transfer'] = {
                        'status': 'complete', 'session_date': session_date,
                        'progress': 100, 'bytes_sent': zip_size,
                        'bytes_total': zip_size, 'error': ''
                    }
                self._publish_state()
                self.logger.info(f"Session {session_date} uploaded successfully ({zip_size} bytes)")
            else:
                raise Exception(f"Upload failed with status {resp_code}")

        except Exception as e:
            self.logger.error(f"Session upload failed: {e}")
            with self.state_lock:
                self.state['data_transfer'] = {
                    'status': 'error', 'session_date': session_date,
                    'progress': 0, 'bytes_sent': 0, 'bytes_total': 0,
                    'error': str(e)
                }
            self._publish_state()

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _delete_session(self, session_date, data_types):
        """Delete a data session directory from the OWL. Runs in background thread.

        session_date can be "YYYYMMDD" or "YYYYMMDD/session_HHMMSS".
        """
        import re
        import shutil

        if not re.match(r'^\d{8}(/session_\d{6})?$', session_date):
            self.logger.error(f"Invalid session identifier for deletion: {session_date}")
            return

        try:
            save_dir = getattr(self.owl_instance, 'save_directory', None) if self.owl_instance else None
            if not save_dir:
                self.logger.error("No save_directory configured")
                return

            if 'images' in data_types:
                target = os.path.join(save_dir, session_date)
                real_target = os.path.realpath(target)
                real_save = os.path.realpath(save_dir)

                # Path traversal check
                if not real_target.startswith(real_save + os.sep):
                    self.logger.error(f"Path traversal rejected: {target}")
                    return

                if os.path.isdir(target):
                    shutil.rmtree(target)
                    self.logger.info(f"Deleted session directory: {target}")
                else:
                    self.logger.warning(f"Session directory not found: {target}")

            # Refresh sessions list
            self._list_data_sessions()

        except Exception as e:
            self.logger.error(f"Error deleting session {session_date}: {e}")

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
        """Apply a sensitivity preset via SensitivityManager."""
        if self.owl_instance is None:
            self.logger.warning("Cannot apply preset - OWL instance not set")
            return

        if self.sensitivity_manager:
            success = self.sensitivity_manager.apply_preset(preset, self.owl_instance)
            if success:
                self._sync_parameters_to_state()
            else:
                self.logger.error(f"Failed to apply preset: {preset}")
        else:
            self.logger.warning("No SensitivityManager available")

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
            'min_detection_area', 'invert_hue'
        ]

        if param_name not in valid_params:
            self.logger.error(f"Invalid parameter name: {param_name}")
            return

        try:
            # Boolean params
            if param_name == 'invert_hue':
                param_value = str(param_value).lower() in ('true', '1', 'yes')
            else:
                # Convert to int
                param_value = int(param_value)

            # Update the Owl instance attribute directly
            setattr(self.owl_instance, param_name, param_value)

            # Queue trackbar update for main thread (cv2 HighGUI is not thread-safe)
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
                    self.owl_instance._pending_trackbar_updates[trackbar_name] = param_value

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

                with self.state_lock:
                    self.state['confidence'] = param_value
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

    def _list_available_models(self):
        """List available YOLO models (.pt files and NCNN subdirs) in models/ directory."""
        models = []
        try:
            models_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models')
            if not os.path.isdir(models_dir):
                return models
            for item in sorted(os.listdir(models_dir)):
                item_path = os.path.join(models_dir, item)
                if item.endswith('.pt'):
                    models.append(item)
                elif os.path.isdir(item_path):
                    for f in os.listdir(item_path):
                        if f.endswith('.param'):
                            models.append(item)
                            break
        except Exception:
            pass
        return models

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
                    self._refresh_ai_state()
                    with self.state_lock:
                        self._publish_state()
                time.sleep(heartbeat_interval)
            except Exception as e:
                self.logger.error(f"Error in heartbeat loop: {e}")
                time.sleep(heartbeat_interval)

    def _refresh_ai_state(self):
        """Refresh AI tab state from live OWL detector. Called every heartbeat."""
        if self.owl_instance is None:
            return
        with self.state_lock:
            # Refresh algorithm from live config (handles runtime changes)
            self.state['algorithm'] = self.owl_instance.config.get(
                'System', 'algorithm', fallback=self.state.get('algorithm', 'exhsv'))

            gog = getattr(self.owl_instance, '_gog_detector', None)
            # Use pending model name if OWL hasn't processed the swap yet,
            # so the dashboard dropdown doesn't snap back to the old model.
            pending_model = getattr(self.owl_instance, '_pending_model', None)
            if pending_model is not None:
                # Model swap queued — keep current_model as set by the handler
                pass
            elif gog and hasattr(gog, 'model'):
                self.state['current_model'] = getattr(gog, '_model_filename', '')
                self.state['model_classes'] = {str(k): v for k, v in gog.model.names.items()}
            else:
                self.state['current_model'] = ''
                self.state['model_classes'] = {}
            # Use pending classes if OWL hasn't processed them yet,
            # otherwise use the active detect_classes_list
            pending = getattr(self.owl_instance, '_pending_detect_classes', None)
            if pending is not None:
                self.state['detect_classes'] = pending
            else:
                self.state['detect_classes'] = getattr(self.owl_instance, '_detect_classes_list', [])
            self.state['available_models'] = self._list_available_models()

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
        """Set image sampling state (for owl.py internal use and hardware controllers)"""
        with self.state_lock:
            was_recording = self.state['image_sample_enable']
            self.state['image_sample_enable'] = bool(value)
            self.state['last_update'] = time.time()

        # Auto-save session metadata when recording stops (hardware switch path)
        if was_recording and not bool(value):
            self._auto_save_session_metadata()

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
            self.state['avg_loop_time_ms'] = stats_dict.get('avg_loop_time_ms', 0.0)
            self.state['actuation_duration'] = stats_dict.get('actuation_duration', self.state.get('actuation_duration', 0.15))
            self.state['delay'] = stats_dict.get('delay', self.state.get('delay', 0.0))
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
            self.logger.warning(f"Unexpected MQTT disconnection (rc={rc})")

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

        # ANY valid message from OWL proves it's alive — update heartbeat
        # (matches networked controller's last_seen pattern)
        with self.state_lock:
            self.last_heartbeat = time.time()

        # Dispatch based on topic
        if topic == self.topics['state']:
            with self.state_lock:
                self.current_state = data
            self.logger.debug(f"State update received ({len(data)} fields)")

        elif topic == self.topics['status']:
            self.logger.info(f"OWL status: {data}")
            # Update owl_running from status messages (these are confirmed arriving)
            with self.state_lock:
                if 'owl_running' in data:
                    self.current_state['owl_running'] = data['owl_running']
                if 'connected' in data:
                    self.current_state['connected'] = data['connected']

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
        """Set sensitivity level (accepts builtin and custom preset names)"""
        level = level.lower()
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

    def set_detection_mode(self, mode):
        """Set detection mode: 0=spot spray, 1=off, 2=blanket"""
        return self._send_command('set_detection_mode', value=int(mode))

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