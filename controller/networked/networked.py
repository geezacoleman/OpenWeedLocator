#!/usr/bin/env python3
"""
OWL Central Controller - Ghost OWL Fix
Key improvements:
1. Reduced TTL from 15s to 8s
2. More aggressive offline marking (5s timeout)
3. Last Will & Testament support
4. Better state cleanup
"""

import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from flask import Flask, render_template, jsonify, request, Response, stream_with_context, send_from_directory
import urllib3
import paho.mqtt.client as mqtt
import json
import threading
import time
import logging
import configparser
from datetime import datetime
from pathlib import Path
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

SHARED_DIR = os.path.join(os.path.dirname(__file__), '..', 'shared')

class CentralController:
    """Central controller for managing multiple OWLs via MQTT"""

    def __init__(self, config_file=None):
        if config_file is None:
            config_file = Path(__file__).parent.parent.parent / 'config' / 'CONTROLLER.ini'
        self.config = self._load_config(config_file)

        # MQTT Configuration from config file
        self.broker_host = self.config.get('MQTT', 'broker_ip', fallback='localhost')
        self.broker_port = self.config.getint('MQTT', 'broker_port', fallback=1883)
        self.client_id = self.config.get('MQTT', 'client_id', fallback='owl_central_controller')

        # State management
        self.owls_state = {}
        self.mqtt_connected = False
        self.mqtt_lock = threading.Lock()

        # REDUCED TIMEOUT: Mark offline after 5 seconds instead of 10
        self.offline_timeout = 5.0  # seconds

        # MQTT client
        self.mqtt_client = None

        # GPS Manager (optional)
        self.gps_manager = None
        if self.config.getboolean('GPS', 'enable', fallback=False):
            try:
                from utils.gps_manager import GPSManager
                self.gps_manager = GPSManager(
                    port=self.config.getint('GPS', 'nmea_port', fallback=8500),
                    boom_width=self.config.getfloat('GPS', 'boom_width', fallback=12.0),
                    track_dir=self.config.get('GPS', 'track_save_directory', fallback='tracks')
                )
                logger.info("GPS Manager initialized")
            except Exception as e:
                logger.error(f"Failed to initialize GPS Manager: {e}")

        logger.info(f"Central Controller initialized (broker: {self.broker_host}:{self.broker_port})")

    def _load_config(self, config_file):
        """Load configuration from file"""
        config_path = Path(config_file)
        config = configparser.ConfigParser()

        if config_path.exists():
            config.read(config_path)
            logger.info(f"Config loaded from {config_path}")
        else:
            logger.warning(f"Config file not found at {config_path}, using defaults")
            config.add_section('MQTT')
            config.set('MQTT', 'broker_ip', 'localhost')
            config.set('MQTT', 'broker_port', '1883')
            config.set('MQTT', 'client_id', 'owl_central_controller')

        return config

    def setup_mqtt(self):
        """Initialize and start MQTT client"""
        logger.info(f"Setting up MQTT (ID: {self.client_id})")

        self.mqtt_client = mqtt.Client(client_id=self.client_id)
        self.mqtt_client.on_connect = self._on_connect
        self.mqtt_client.on_disconnect = self._on_disconnect
        self.mqtt_client.on_message = self._on_message

        # Set reconnect behavior
        self.mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

        try:
            logger.info(f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}")
            self.mqtt_client.connect(self.broker_host, self.broker_port, 60)
            self.mqtt_client.loop_start()
            logger.info("MQTT client loop started")
        except Exception as e:
            logger.error(f"Failed to connect to MQTT broker: {e}")
            self.mqtt_client = None

    def _on_connect(self, client, userdata, flags, rc):
        """MQTT connection callback"""
        if rc == 0:
            logger.info("Connected to MQTT broker")
            self.mqtt_connected = True

            # Subscribe to all OWL topics
            topics = [
                ("owl/+/state", 0),
                ("owl/+/status", 0),
                ("owl/+/detection", 0),
                ("owl/+/config", 0),
                ("owl/+/system", 0),
            ]
            client.subscribe(topics)
            logger.info(f"Subscribed to {len(topics)} topic patterns")

            logger.info("Clearing potential ghost OWL retained messages...")

        else:
            logger.error(f"Failed to connect to MQTT broker (code: {rc})")
            self.mqtt_connected = False

    def _on_disconnect(self, client, userdata, rc):
        """MQTT disconnection callback"""
        self.mqtt_connected = False
        if rc != 0:
            logger.warning(f"Unexpected MQTT disconnection (code: {rc})")
        else:
            logger.info("Disconnected from MQTT broker")

    def _on_message(self, client, userdata, msg):
        """Process incoming MQTT messages"""
        try:
            topic_parts = msg.topic.split('/')
            if len(topic_parts) < 3:
                return

            device_id = topic_parts[1]
            topic_type = topic_parts[2]

            # Decode payload
            try:
                payload = json.loads(msg.payload.decode())
            except json.JSONDecodeError:
                return

            current_time = time.time()

            # Update state
            with self.mqtt_lock:
                if device_id not in self.owls_state:
                    self.owls_state[device_id] = {
                        'device_id': device_id,
                        'first_seen': current_time,
                        'last_seen': current_time,
                        'connected': True
                    }
                    logger.info(f"New OWL discovered: {device_id}")

                # Merge payload into state
                if topic_type == 'state':
                    self.owls_state[device_id].update(payload)
                elif topic_type == 'status':
                    self.owls_state[device_id]['status'] = payload
                elif topic_type == 'detection':
                    self.owls_state[device_id]['detection'] = payload
                elif topic_type == 'config':
                    self.owls_state[device_id]['config'] = payload
                elif topic_type == 'system':
                    self.owls_state[device_id]['system'] = payload
                else:
                    self.owls_state[device_id][topic_type] = payload

                self.owls_state[device_id]['last_seen'] = current_time
                self.owls_state[device_id]['connected'] = True

            logger.debug(f"Updated {device_id} ({topic_type})")

        except Exception as e:
            logger.error(f"Error processing message on {msg.topic}: {e}")

    def check_connections(self):
        """Background thread to check for offline OWLs - runs every 1 second"""
        while True:
            try:
                current_time = time.time()

                with self.mqtt_lock:
                    for device_id, state in list(self.owls_state.items()):
                        last_seen = state.get('last_seen', 0)
                        time_since = current_time - last_seen

                        if time_since > self.offline_timeout:
                            if state.get('connected', False):
                                state['connected'] = False
                                logger.warning(f"{device_id} marked offline ({time_since:.1f}s since last seen)")

                        if time_since > (self.offline_timeout * 2):
                            logger.info(f"Removing stale OWL: {device_id} ({time_since:.1f}s since last seen)")
                            del self.owls_state[device_id]

                self._update_session_state()
                time.sleep(2)

            except Exception as e:
                logger.error(f"Error in connection checker: {e}")
                time.sleep(5)

    def _update_session_state(self):
        """Auto-start/stop GPS session when any OWL detection state changes."""
        if not self.gps_manager:
            return
        any_detecting = any(
            s.get('detection_enable', False)
            for s in self.owls_state.values()
            if s.get('connected', False)
        )
        if any_detecting and not self.gps_manager.session_active:
            self.gps_manager.start_session()
        elif not any_detecting and self.gps_manager.session_active:
            self.gps_manager.stop_session()

    def get_owls(self):
        """Get current state of all OWLs"""
        with self.mqtt_lock:
            owls_copy = json.loads(json.dumps(self.owls_state, default=str))

        # Add computed fields
        for device_id, state in owls_copy.items():
            last_seen = state.get('last_seen', 0)
            state['seconds_since_update'] = time.time() - last_seen
            state['last_seen_formatted'] = datetime.fromtimestamp(last_seen).strftime(
                '%H:%M:%S') if last_seen else 'Never'

        return {
            'owls': owls_copy,
            'mqtt_connected': self.mqtt_connected,
            'controller_time': time.time(),
            'owl_count': len(owls_copy)
        }

    def get_recent_owls(self, ttl=8):
        """Get only recently active OWLs"""
        now = time.time()
        recent = {}

        with self.mqtt_lock:
            for oid, state in list(self.owls_state.items()):
                last_seen = state.get("last_seen", 0)
                time_since = now - last_seen

                if time_since <= ttl:
                    recent[oid] = dict(state)
                else:
                    logger.debug(f"Dropping stale OWL {oid} from get_recent_owls (TTL exceeded)")

        return recent

    def request_device_config(self, device_id, timeout=3.0):
        """Send get_config command and poll for the response in owls_state"""
        if not self.mqtt_connected or not self.mqtt_client:
            return None

        # Clear any stale config data
        with self.mqtt_lock:
            if device_id in self.owls_state:
                self.owls_state[device_id].pop('config', None)

        # Send the get_config command
        topic = f"owl/{device_id}/commands"
        payload = json.dumps({'action': 'get_config'})
        self.mqtt_client.publish(topic, payload)

        # Poll for the config response
        start = time.time()
        while time.time() - start < timeout:
            with self.mqtt_lock:
                state = self.owls_state.get(device_id, {})
                config_data = state.get('config')
                if config_data and isinstance(config_data, dict) and 'config' in config_data:
                    return config_data
            time.sleep(0.1)

        logger.warning(f"Timeout waiting for config from {device_id}")
        return None

    def list_local_presets(self):
        """List INI config files in the config/ directory"""
        config_dir = Path(__file__).parent.parent.parent / 'config'
        presets = []
        if config_dir.exists():
            for ini_file in sorted(config_dir.glob('*.ini')):
                presets.append({
                    'name': ini_file.stem,
                    'filename': ini_file.name,
                    'path': str(ini_file),
                    'is_default': ini_file.name.startswith('DAY_SENSITIVITY_')
                })
        return presets

    def read_preset(self, filename):
        """Read a preset INI file and return as dict"""
        config_dir = Path(__file__).parent.parent.parent / 'config'
        config_path = config_dir / filename

        if not config_path.exists():
            return None

        # Security: only allow files in the config directory
        if not config_path.resolve().parent == config_dir.resolve():
            return None

        config = configparser.ConfigParser()
        config.read(config_path)

        config_dict = {}
        for section in config.sections():
            config_dict[section] = dict(config[section])

        return {
            'config': config_dict,
            'config_name': filename,
            'config_path': str(config_path)
        }

    def send_command(self, device_id, action, value=None):
        """Send command to OWL(s)"""
        if not self.mqtt_connected or not self.mqtt_client:
            return {'success': False, 'error': 'MQTT not connected'}

        # Build payload
        payload = {'action': action}

        if action == 'toggle_detection':
            if device_id != 'all':
                current = self.owls_state.get(device_id, {}).get('detection_enable', False)
                payload = {'action': 'set_detection_enable', 'value': not current}
            else:
                payload = {'action': 'set_detection_enable', 'value': value}

        elif action == 'toggle_recording':
            if device_id != 'all':
                current = self.owls_state.get(device_id, {}).get('image_sample_enable', False)
                payload = {'action': 'set_image_sample_enable', 'value': not current}
            else:
                payload = {'action': 'set_image_sample_enable', 'value': value}

        elif action == 'set_sensitivity':
            payload = {'action': 'set_sensitivity_level', 'level': value}

        elif action == 'set_config':
            payload = {
                'action': 'set_greenonbrown_param',
                'param': value.get('key'),
                'value': value.get('value')
            }

        elif action == 'set_config_section':
            payload = {
                'action': 'set_config_section',
                'section': value.get('section'),
                'params': value.get('params')
            }

        elif action == 'save_config':
            payload = {
                'action': 'save_config',
                'filename': value.get('filename') if value else None
            }

        elif action == 'set_active_config':
            payload = {
                'action': 'set_active_config',
                'config': value
            }

        elif action == 'restart_service':
            payload = {'action': 'restart_service'}

        else:
            payload = {'action': action, 'value': value}

        # Publish
        if device_id == 'all':
            sent_to = []
            with self.mqtt_lock:
                for owl_id, state in self.owls_state.items():
                    if state.get('connected', False):
                        topic = f"owl/{owl_id}/commands"
                        result = self.mqtt_client.publish(topic, json.dumps(payload))
                        if result.rc == mqtt.MQTT_ERR_SUCCESS:
                            sent_to.append(owl_id)

            logger.info(f"Broadcast '{action}' to {len(sent_to)} OWLs")
            return {'success': True, 'message': f"Sent to {len(sent_to)} OWLs", 'targets': sent_to}
        else:
            topic = f"owl/{device_id}/commands"
            result = self.mqtt_client.publish(topic, json.dumps(payload))

            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                logger.info(f"Sent '{action}' to {device_id}")
                return {'success': True, 'message': f"Sent to {device_id}"}
            else:
                logger.error(f"Failed to publish to {device_id} (code: {result.rc})")
                return {'success': False, 'error': f"Publish failed (code: {result.rc})"}


# Create Flask app
app = Flask(__name__,
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'),
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
)

# Initialize controller
controller = CentralController()

@app.route('/shared/<path:filename>')
def shared_static(filename):
    return send_from_directory(SHARED_DIR, filename)

# Flask routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route("/api/owls")
def api_owls():
    return jsonify({
        "mqtt_connected": controller.mqtt_connected,
        "owls": controller.get_recent_owls(ttl=8),  # Reduced TTL
    })


@app.route('/api/health')
def health_check():
    return jsonify({
        'status': 'healthy',
        'mqtt_connected': controller.mqtt_connected,
        'owl_count': len(controller.owls_state),
        'uptime': time.time()
    })


@app.route('/api/command', methods=['POST'])
def send_command():
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        device_id = data.get('device_id')
        action = data.get('action')
        value = data.get('value')

        if not device_id or not action:
            return jsonify({'success': False, 'error': 'Missing device_id or action'}), 400

        result = controller.send_command(device_id, action, value)

        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 500

    except Exception as e:
        logger.error(f"Error sending command: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mqtt/status')
def mqtt_status():
    return jsonify({
        'connected': controller.mqtt_connected,
        'client_id': controller.client_id,
        'broker': f"{controller.broker_host}:{controller.broker_port}"
    })


@app.route('/api/greenonbrown/defaults')
def get_greenonbrown_defaults():
    defaults = {
        'exg_min': {'value': 25, 'min': 0, 'max': 255, 'step': 1, 'label': 'ExG Min'},
        'exg_max': {'value': 200, 'min': 0, 'max': 255, 'step': 1, 'label': 'ExG Max'},
        'hue_min': {'value': 39, 'min': 0, 'max': 179, 'step': 1, 'label': 'Hue Min'},
        'hue_max': {'value': 83, 'min': 0, 'max': 179, 'step': 1, 'label': 'Hue Max'},
        'saturation_min': {'value': 50, 'min': 0, 'max': 255, 'step': 1, 'label': 'Saturation Min'},
        'saturation_max': {'value': 220, 'min': 0, 'max': 255, 'step': 1, 'label': 'Saturation Max'},
        'brightness_min': {'value': 60, 'min': 0, 'max': 255, 'step': 1, 'label': 'Brightness Min'},
        'brightness_max': {'value': 190, 'min': 0, 'max': 255, 'step': 1, 'label': 'Brightness Max'},
        'min_detection_area': {'value': 10, 'min': 1, 'max': 1000, 'step': 1, 'label': 'Min Detection Area'}
    }
    return jsonify(defaults)


@app.route('/api/config/<device_id>', methods=['GET'])
def get_device_config(device_id):
    """Fetch full config from a device via MQTT"""
    try:
        config_data = controller.request_device_config(device_id, timeout=3.0)
        if config_data:
            return jsonify({'success': True, **config_data})
        else:
            return jsonify({'success': False, 'error': f'Timeout waiting for config from {device_id}'}), 504
    except Exception as e:
        logger.error(f"Error getting config for {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/<device_id>', methods=['POST'])
def push_device_config(device_id):
    """Push section changes to a device"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        section = data.get('section')
        params = data.get('params', {})

        if not section or not params:
            return jsonify({'success': False, 'error': 'Missing section or params'}), 400

        result = controller.send_command(device_id, 'set_config_section', {
            'section': section,
            'params': params
        })
        return jsonify(result)

    except Exception as e:
        logger.error(f"Error pushing config to {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/<device_id>/save', methods=['POST'])
def save_device_config(device_id):
    """Tell a device to save its config to disk"""
    try:
        data = request.json or {}
        filename = data.get('filename')
        result = controller.send_command(device_id, 'save_config', {'filename': filename})
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error saving config on {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/presets')
def list_presets():
    """List local preset INI files"""
    presets = controller.list_local_presets()
    return jsonify({'success': True, 'presets': presets})


@app.route('/api/presets/<name>')
def get_preset(name):
    """Read a preset as JSON"""
    # Ensure filename ends with .ini
    if not name.endswith('.ini'):
        name = name + '.ini'

    data = controller.read_preset(name)
    if data:
        return jsonify({'success': True, **data})
    else:
        return jsonify({'success': False, 'error': f'Preset not found: {name}'}), 404


@app.route('/api/presets/push/<device_id>', methods=['POST'])
def push_preset_to_device(device_id):
    """Push a preset config to a device section by section"""
    try:
        data = request.json
        if not data:
            return jsonify({'success': False, 'error': 'No data'}), 400

        preset_name = data.get('preset')
        if not preset_name:
            return jsonify({'success': False, 'error': 'Missing preset name'}), 400

        if not preset_name.endswith('.ini'):
            preset_name = preset_name + '.ini'

        preset_data = controller.read_preset(preset_name)
        if not preset_data:
            return jsonify({'success': False, 'error': f'Preset not found: {preset_name}'}), 404

        # Push each section to the device
        sections_pushed = 0
        for section, params in preset_data['config'].items():
            controller.send_command(device_id, 'set_config_section', {
                'section': section,
                'params': params
            })
            sections_pushed += 1

        return jsonify({
            'success': True,
            'message': f'Pushed {sections_pushed} sections from {preset_name} to {device_id}'
        })

    except Exception as e:
        logger.error(f"Error pushing preset to {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/library', methods=['GET'])
def list_config_library():
    """List all config files (presets + custom saved) — mirrors standalone /api/config/list"""
    try:
        config_dir = Path(__file__).parent.parent.parent / 'config'
        configs = []
        protected = ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini', 'CONTROLLER.ini']

        if config_dir.exists():
            for f in sorted(config_dir.glob('*.ini')):
                stat = f.stat()
                configs.append({
                    'name': f.name,
                    'path': f'config/{f.name}',
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'is_default': f.name in protected
                })

        # Sort: defaults first, then by modified descending
        configs.sort(key=lambda x: (not x['is_default'], x['name']))

        return jsonify({'success': True, 'configs': configs})
    except Exception as e:
        logger.error(f"Error listing config library: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/library', methods=['POST'])
def save_to_config_library():
    """Save config to controller's local filesystem — mirrors standalone POST /api/config"""
    try:
        data = request.json
        if not data or 'config' not in data:
            return jsonify({'success': False, 'error': 'No config data provided'}), 400

        # Generate filename with timestamp (same as standalone)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        suggested_name = data.get('filename', f'config_{timestamp}.ini')

        if not suggested_name.endswith('.ini'):
            suggested_name += '.ini'

        # Sanitize filename (same as standalone)
        safe_name = "".join(c for c in suggested_name if c.isalnum() or c in ('_', '-', '.')).strip()
        if not safe_name:
            safe_name = f'config_{timestamp}.ini'

        config_dir = Path(__file__).parent.parent.parent / 'config'
        new_config_path = config_dir / safe_name

        # Don't allow overwriting protected configs (same as standalone)
        protected = ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini', 'CONTROLLER.ini']
        if safe_name in protected:
            return jsonify({
                'success': False,
                'error': f'Cannot overwrite default config "{safe_name}". Choose a different name.'
            }), 400

        # Build and write config (same as standalone)
        config = configparser.ConfigParser()
        config.optionxform = str

        for section, options in data['config'].items():
            if not config.has_section(section):
                config.add_section(section)
            for key, value in options.items():
                config.set(section, key, str(value))

        with open(new_config_path, 'w') as f:
            config.write(f)

        logger.info(f"Config saved to library: {new_config_path}")

        return jsonify({
            'success': True,
            'message': f'Saved as {safe_name}',
            'filename': safe_name,
            'relative_path': f'config/{safe_name}'
        })

    except Exception as e:
        logger.error(f"Error saving to config library: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/library/<name>', methods=['DELETE'])
def delete_from_config_library(name):
    """Delete a custom config from library — mirrors standalone /api/config/delete"""
    try:
        protected = ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini', 'CONTROLLER.ini']
        if name in protected:
            return jsonify({'success': False, 'error': 'Cannot delete default configs'}), 400

        config_dir = Path(__file__).parent.parent.parent / 'config'
        config_path = config_dir / name

        # Security: only allow files in config directory
        if not config_path.resolve().parent == config_dir.resolve():
            return jsonify({'success': False, 'error': 'Invalid path'}), 400

        if not config_path.exists():
            return jsonify({'success': False, 'error': 'Config not found'}), 404

        config_path.unlink()
        logger.info(f"Deleted config from library: {name}")

        return jsonify({'success': True, 'message': f'Deleted {name}'})
    except Exception as e:
        logger.error(f"Error deleting config: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/config/<device_id>/set-active', methods=['POST'])
def set_device_active_config(device_id):
    """Tell an OWL to set its active_config.txt — uses existing set_active_config MQTT handler"""
    try:
        data = request.json or {}
        config_path = data.get('config')
        if not config_path:
            return jsonify({'success': False, 'error': 'No config path specified'}), 400

        result = controller.send_command(device_id, 'set_active_config', config_path)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error setting active config on {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/owl/<device_id>/restart', methods=['POST'])
def restart_owl(device_id):
    """Send restart_service command to an OWL"""
    try:
        result = controller.send_command(device_id, 'restart_service')
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error restarting {device_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/snapshot/<device_id>')
def snapshot_proxy(device_id):
    """Proxy single JPEG frame from OWL device (95% quality)."""
    device_id = device_id.replace('_', '-')
    snapshot_url = f"http://{device_id}.local:8001/latest_frame.jpg"
    try:
        r = requests.get(snapshot_url, timeout=5, verify=False)
        if r.status_code != 200:
            logger.error(f"Failed to get snapshot from {device_id} (Status: {r.status_code})")
            return f"Error: Could not get snapshot from {device_id}.", 502
        return Response(r.content, mimetype='image/jpeg')
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error getting snapshot from {device_id}: {e}")
        return f"Error: {device_id} is offline or unreachable.", 502
    except Exception as e:
        logger.error(f"Error getting snapshot from {device_id}: {e}")
        return f"Error: An unknown error occurred.", 500


@app.route('/api/video_feed/<device_id>')
def video_feed_proxy(device_id):
    """Proxies the MJPEG stream from an OWL, ignoring SSL errors."""

    device_id = device_id.replace('_', '-')
    video_url = f"https://{device_id}.local/video_feed"
    logger.info(f"Proxying video feed for {device_id} from {video_url}")

    try:
        r = requests.get(video_url, stream=True, verify=False, timeout=5)

        if r.status_code != 200:
            logger.error(f"Failed to get stream from {video_url} (Status: {r.status_code})")
            return f"Error: Could not connect to {device_id}.", 502

        ct = r.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame")

        return Response(
            stream_with_context(r.iter_content(chunk_size=1024)),
            content_type=ct,
            status=r.status_code,
            direct_passthrough=True,
        )

    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection error proxying {video_url}: {e}")
        return f"Error: {device_id} is offline or unreachable.", 502
    except Exception as e:
        logger.error(f"Generic error proxying {video_url}: {e}")
        return "Error: An unknown error occurred.", 500


# ---------------------------------------------------------------------------
# GPS API routes
# ---------------------------------------------------------------------------

@app.route('/api/gps')
def api_gps():
    """Current GPS fix, connection status, and session stats."""
    if not controller.gps_manager:
        return jsonify({'connection': {'gps_enabled': False}}), 200
    return jsonify(controller.gps_manager.get_state())


@app.route('/api/gps/tracks')
def api_gps_tracks():
    """List saved GeoJSON track files."""
    if not controller.gps_manager:
        return jsonify({'tracks': []}), 200

    track_dir = controller.gps_manager.track_dir
    tracks = []
    if os.path.isdir(track_dir):
        for f in sorted(os.listdir(track_dir), reverse=True):
            if f.endswith('.geojson'):
                filepath = os.path.join(track_dir, f)
                tracks.append({
                    'filename': f,
                    'size_bytes': os.path.getsize(filepath),
                    'modified': os.path.getmtime(filepath),
                })
    return jsonify({'tracks': tracks})


@app.route('/api/gps/tracks/<filename>')
def api_gps_track_download(filename):
    """Download a saved GeoJSON track file."""
    if not controller.gps_manager:
        return jsonify({'error': 'GPS not enabled'}), 404

    track_dir = os.path.abspath(controller.gps_manager.track_dir)

    # Security: only serve files from the track directory
    filepath = os.path.abspath(os.path.join(track_dir, filename))
    if not filepath.startswith(track_dir):
        return jsonify({'error': 'Invalid path'}), 400

    if not os.path.isfile(filepath):
        return jsonify({'error': 'Track not found'}), 404

    return send_from_directory(track_dir, filename, mimetype='application/geo+json')


@app.route('/api/gps/config', methods=['POST'])
def api_gps_config():
    """Update GPS config at runtime (e.g. boom_width)."""
    if not controller.gps_manager:
        return jsonify({'success': False, 'error': 'GPS not enabled'}), 400

    data = request.json or {}
    if 'boom_width' in data:
        try:
            bw = float(data['boom_width'])
            controller.gps_manager.session.boom_width_m = bw
            return jsonify({'success': True, 'boom_width_m': bw})
        except (ValueError, TypeError):
            return jsonify({'success': False, 'error': 'Invalid boom_width'}), 400

    return jsonify({'success': False, 'error': 'No valid config parameters'}), 400


# Initialize on startup
def init_app():
    logger.info("OWL Central Controller Starting")
    controller.setup_mqtt()

    # Start GPS manager if configured
    if controller.gps_manager:
        controller.gps_manager.start()

    # Start connection checker
    checker_thread = threading.Thread(target=controller.check_connections, daemon=True)
    checker_thread.start()


init_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)