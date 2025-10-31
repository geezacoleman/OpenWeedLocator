#!/usr/bin/env python3
"""
OWL Central Controller Dashboard
Manages multiple OWL units via MQTT
"""

from flask import Flask, render_template, jsonify, request
import paho.mqtt.client as mqtt
import json
import threading
import time

app = Flask(__name__)

# Global state for all OWLs
# owls_state = { "owl_1": {"last_seen": 1678886400, "ip": "192.168.1.11", ...}, ... }
owls_state = {}
mqtt_client = None


# --- MQTT Functions ---

def on_connect(client, userdata, flags, rc):
    """Subscribes to all OWL topics when connected."""
    print(f"Connected to MQTT broker with result: {rc}")
    # Subscribe to all OWL state and status topics
    client.subscribe("owl/+/state")
    client.subscribe("owl/+/status")


def on_message(client, userdata, msg):
    """Processes incoming MQTT messages and updates the global state."""
    global owls_state
    try:
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 3:
            device_id = topic_parts[1]
            topic_type = topic_parts[2]
            data = json.loads(msg.payload.decode())

            # Ensure the device has an entry in the state
            if device_id not in owls_state:
                owls_state[device_id] = {}

            # Update the state with the new data
            owls_state[device_id].update(data)
            owls_state[device_id]['last_seen'] = time.time()

            # Add device_id to the state for convenience
            owls_state[device_id]['device_id'] = device_id

    except Exception as e:
        print(f"Error processing message on topic {msg.topic}: {e}")


def setup_mqtt():
    """Configures and starts the MQTT client."""
    global mqtt_client
    mqtt_client = mqtt.Client(client_id='owl_central_controller')
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    try:
        mqtt_client.connect('localhost', 1883, 60)
        mqtt_client.loop_start()
    except Exception as e:
        print(f"Could not connect to MQTT broker: {e}")


# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main controller dashboard."""
    return render_template('index.html')


@app.route('/api/owls')
def get_owls():
    """Returns the current state of all connected OWLs."""
    return jsonify(owls_state)


@app.route('/api/command', methods=['POST'])
def send_command():
    """Receives a command from the web UI and publishes it via MQTT."""
    data = request.json
    device_id = data.get('device_id')
    action = data.get('action')
    value = data.get('value')

    payload = {}

    # Build the MQTT payload based on the action
    if action == 'toggle_detection':
        # Note: This logic might be better on the client-side
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
    else:
        payload = {'action': action, 'value': value}

    # Determine which topic(s) to publish to
    if device_id == 'all':
        # Send command to all known devices
        for owl_id in owls_state.keys():
            topic = f"owl/{owl_id}/commands"
            mqtt_client.publish(topic, json.dumps(payload))
        print(f"Sent command '{action}' to ALL devices")
    else:
        # Send to a specific device
        topic = f"owl/{device_id}/commands"
        mqtt_client.publish(topic, json.dumps(payload))
        print(f"Sent command '{action}' to {device_id}")

    return jsonify({'status': 'sent'})


# --- Main ---

if __name__ == '__main__':
    setup_mqtt()
    # Note: debug=True is good for development, but set to False for production
    app.run(host='127.0.0.1', port=8000, debug=True)