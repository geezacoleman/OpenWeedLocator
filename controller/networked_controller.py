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
from datetime import datetime

app = Flask(__name__)

# Global state for all OWLs
owls_state = {}
mqtt_client = None

def on_connect(client, userdata, flags, rc):
    print(f"Connected to MQTT broker with result: {rc}")
    # Subscribe to all OWL topics
    client.subscribe("owl/+/state")
    client.subscribe("owl/+/status")

def on_message(client, userdata, msg):
    try:
        topic_parts = msg.topic.split('/')
        if len(topic_parts) >= 3:
            device_id = topic_parts[1]
            data = json.loads(msg.payload.decode())
            data['last_seen'] = time.time()
            owls_state[device_id] = data
    except Exception as e:
        print(f"Error processing message: {e}")

@app.route('/')
def index():
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>OWL Central Controller</title>
        <meta http-equiv="refresh" content="5">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #1a1a1a; color: #fff; }
            h1 { color: #4CAF50; }
            .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
            .owl-card { background: #2a2a2a; border-radius: 8px; padding: 15px; border: 2px solid #444; }
            .owl-card.active { border-color: #4CAF50; }
            .owl-card h3 { margin-top: 0; color: #4CAF50; }
            .status { padding: 5px 10px; border-radius: 4px; display: inline-block; }
            .status.online { background: #4CAF50; }
            .status.offline { background: #f44336; }
            .controls { margin-top: 10px; }
            button { background: #4CAF50; color: white; border: none; padding: 8px 16px;
                    border-radius: 4px; cursor: pointer; margin: 2px; }
            button:hover { background: #45a049; }
        </style>
    </head>
    <body>
        <h1>OWL Central Controller</h1>
        <p>Connected OWLs: <span id="owl-count">0</span></p>
        <div class="grid" id="owls-grid"></div>

        <script>
            function updateStatus() {
                fetch('/api/owls')
                    .then(response => response.json())
                    .then(data => {
                        const grid = document.getElementById('owls-grid');
                        const count = document.getElementById('owl-count');
                        grid.innerHTML = '';
                        count.textContent = Object.keys(data).length;

                        for (const [id, owl] of Object.entries(data)) {
                            const card = document.createElement('div');
                            card.className = 'owl-card ' + (owl.owl_running ? 'active' : '');

                            const isOnline = (Date.now()/1000 - owl.last_seen) < 10;

                            card.innerHTML = `
                                <h3>OWL: ${id}</h3>
                                <p>Status: <span class="status ${isOnline ? 'online' : 'offline'}">
                                    ${isOnline ? 'Online' : 'Offline'}</span></p>
                                <p>Detection: ${owl.detection_enable ? 'Enabled' : 'Disabled'}</p>
                                <p>Sensitivity: ${owl.sensitivity_level || 'Unknown'}</p>
                                <div class="controls">
                                    <button onclick="sendCommand('${id}', 'toggle_detection')">
                                        Toggle Detection</button>
                                    <button onclick="sendCommand('${id}', 'set_sensitivity', 'high')">
                                        High Sensitivity</button>
                                    <button onclick="sendCommand('${id}', 'set_sensitivity', 'low')">
                                        Low Sensitivity</button>
                                </div>
                            `;
                            grid.appendChild(card);
                        }
                    });
            }

            function sendCommand(deviceId, action, value) {
                fetch('/api/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({device_id: deviceId, action: action, value: value})
                });
            }

            updateStatus();
            setInterval(updateStatus, 2000);
        </script>
    </body>
    </html>
    '''

@app.route('/api/owls')
def get_owls():
    return jsonify(owls_state)

@app.route('/api/command', methods=['POST'])
def send_command():
    data = request.json
    device_id = data.get('device_id')
    action = data.get('action')
    value = data.get('value')

    topic = f"owl/{device_id}/commands"

    if action == 'toggle_detection':
        current = owls_state.get(device_id, {}).get('detection_enable', False)
        payload = {'action': 'set_detection_enable', 'value': not current}
    elif action == 'set_sensitivity':
        payload = {'action': 'set_sensitivity_level', 'level': value}
    else:
        payload = {'action': action, 'value': value}

    mqtt_client.publish(topic, json.dumps(payload))
    return jsonify({'status': 'sent'})

def setup_mqtt():
    global mqtt_client
    mqtt_client = mqtt.Client(client_id='owl_central_controller')
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    mqtt_client.connect('localhost', 1883, 60)
    mqtt_client.loop_start()

if __name__ == '__main__':
    setup_mqtt()
    app.run(host='0.0.0.0', port=8000, debug=False)
