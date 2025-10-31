#!/usr/bin/env python3
"""
OWL Central Controller Dashboard
Manages multiple OWL units via MQTT
"""

from flask import Flask, render_template, jsonify, request, render_template_string
import paho.mqtt.client as mqtt
import json
import threading
import time
import os
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Global state for all OWLs
owls_state = {}
mqtt_client = None
mqtt_connected = False


# --- MQTT Functions ---

def on_connect(client, userdata, flags, rc):
    """Subscribes to all OWL topics when connected."""
    global mqtt_connected
    if rc == 0:
        logger.info(f"Connected to MQTT broker successfully")
        mqtt_connected = True
        # Subscribe to all OWL state and status topics
        client.subscribe("owl/+/state")
        client.subscribe("owl/+/status")
        client.subscribe("owl/+/detection")
        client.subscribe("owl/+/config")
        logger.info("Subscribed to OWL topics")
    else:
        logger.error(f"Failed to connect to MQTT broker with result code: {rc}")
        mqtt_connected = False


def on_disconnect(client, userdata, rc):
    """Handle disconnection from MQTT broker."""
    global mqtt_connected
    mqtt_connected = False
    if rc != 0:
        logger.warning(f"Unexpected MQTT disconnection. Attempting to reconnect...")


def on_message(client, userdata, msg):
    """Processes incoming MQTT messages and updates the global state."""
    global owls_state
    try:
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 3:
            device_id = topic_parts[1]
            topic_type = topic_parts[2]

            # Try to decode the payload
            try:
                data = json.loads(msg.payload.decode())
            except json.JSONDecodeError:
                # If not JSON, store as string
                data = {topic_type: msg.payload.decode()}

            # Ensure the device has an entry in the state
            if device_id not in owls_state:
                owls_state[device_id] = {
                    'device_id': device_id,
                    'first_seen': time.time()
                }

            # Update the state based on topic type
            if topic_type == 'state':
                owls_state[device_id].update(data)
            elif topic_type == 'status':
                owls_state[device_id]['status'] = data
            elif topic_type == 'detection':
                owls_state[device_id]['detection'] = data
            elif topic_type == 'config':
                owls_state[device_id]['config'] = data
            else:
                owls_state[device_id][topic_type] = data

            owls_state[device_id]['last_seen'] = time.time()

            logger.debug(f"Updated state for {device_id}: {topic_type}")

    except Exception as e:
        logger.error(f"Error processing message on topic {msg.topic}: {e}")


def setup_mqtt():
    """Configures and starts the MQTT client."""
    global mqtt_client

    mqtt_client = mqtt.Client(client_id='owl_central_controller')
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message

    try:
        # Try connecting to localhost first
        mqtt_client.connect('localhost', 1883, 60)
        mqtt_client.loop_start()
        logger.info("MQTT client started and connecting to localhost:1883")
    except Exception as e:
        logger.error(f"Could not connect to MQTT broker: {e}")
        # Try again with 127.0.0.1
        try:
            mqtt_client.connect('127.0.0.1', 1883, 60)
            mqtt_client.loop_start()
            logger.info("MQTT client started and connecting to 127.0.0.1:1883")
        except Exception as e2:
            logger.error(f"Could not connect to MQTT broker on 127.0.0.1: {e2}")


# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main controller dashboard."""
    # Check if templates directory exists
    template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    template_file = os.path.join(template_dir, 'index.html')

    if not os.path.exists(template_file):
        logger.error(f"File {template_file} not found")

    return render_template('index.html')


@app.route('/api/owls')
def get_owls():
    """Returns the current state of all connected OWLs."""
    # Add connection status
    response = {
        'owls': owls_state,
        'mqtt_connected': mqtt_connected,
        'controller_time': time.time()
    }
    return jsonify(response)


@app.route('/api/health')
def health_check():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'mqtt_connected': mqtt_connected,
        'owl_count': len(owls_state),
        'uptime': time.time()
    })


@app.route('/api/command', methods=['POST'])
def send_command():
    """Receives a command from the web UI and publishes it via MQTT."""
    if not mqtt_connected:
        return jsonify({'status': 'error', 'message': 'MQTT not connected'}), 503

    try:
        data = request.json
        device_id = data.get('device_id')
        action = data.get('action')
        value = data.get('value')

        if not device_id or not action:
            return jsonify({'status': 'error', 'message': 'Missing device_id or action'}), 400

        payload = {}

        # Build the MQTT payload based on the action
        if action == 'toggle_detection':
            current = owls_state.get(device_id, {}).get('detection_enable', False)
            payload = {'action': 'set_detection_enable', 'value': not current}
        elif action == 'set_sensitivity':
            payload = {'action': 'set_sensitivity_level', 'level': value}
        elif action == 'set_greenonbrown_config':
            # Value is expected to be a dict: {'key': 'hue_min', 'value': 50}
            payload = {
                'action': 'set_config_value',
                'section': 'GreenOnBrown',
                'key': value.get('key'),
                'value': value.get('value')
            }
        elif action == 'reboot':
            payload = {'action': 'reboot'}
        elif action == 'restart_service':
            payload = {'action': 'restart_service'}
        else:
            payload = {'action': action, 'value': value}

        # Determine which topic(s) to publish to
        if device_id == 'all':
            # Send command to all known devices
            for owl_id in owls_state.keys():
                topic = f"owl/{owl_id}/commands"
                mqtt_client.publish(topic, json.dumps(payload))
            logger.info(f"Sent command '{action}' to ALL devices")
        else:
            # Send to a specific device
            topic = f"owl/{device_id}/commands"
            mqtt_client.publish(topic, json.dumps(payload))
            logger.info(f"Sent command '{action}' to {device_id}")

        return jsonify({'status': 'sent', 'command': action, 'device': device_id})

    except Exception as e:
        logger.error(f"Error sending command: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/mqtt/status')
def mqtt_status():
    """Returns MQTT connection status."""
    return jsonify({
        'connected': mqtt_connected,
        'client_id': 'owl_central_controller' if mqtt_client else None,
        'broker': 'localhost:1883'
    })


if __name__ == '__main__':
    setup_mqtt()
    # Note: When run by gunicorn, this block won't execute
    # For testing/development only
    app.run(host='127.0.0.1', port=8000, debug=False)