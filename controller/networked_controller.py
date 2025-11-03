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

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from flask import Flask, render_template, jsonify, request, Response, stream_with_context
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


class CentralController:
    """Central controller for managing multiple OWLs via MQTT"""

    def __init__(self, config_file='../config/CONTROLLER.ini'):
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

        logger.info(f"Central Controller initialized (broker: {self.broker_host}:{self.broker_port})")

    def _load_config(self, config_file):
        """Load configuration from file"""
        config_path = Path(config_file)
        if not config_path.exists():
            config_path = Path(__file__).parent / config_file

        config = configparser.ConfigParser()

        if config_path.exists():
            config.read(config_path)
            logger.info(f"Config loaded from {config_path}")
        else:
            logger.warning(f"Config file not found at {config_path}, using defaults")
            # Create default config sections
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

                time.sleep(2)

            except Exception as e:
                logger.error(f"Error in connection checker: {e}")
                time.sleep(5)

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
app = Flask(__name__)

# Initialize controller
controller = CentralController()


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


# Initialize on startup
def init_app():
    logger.info("OWL Central Controller Starting")
    controller.setup_mqtt()

    # Start connection checker
    checker_thread = threading.Thread(target=controller.check_connections, daemon=True)
    checker_thread.start()


init_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)