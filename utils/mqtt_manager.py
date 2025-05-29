import json
import logging
from typing import Dict, Any

try:
    import paho.mqtt.client as mqtt
except ImportError as e:
    raise ImportError(
        "paho-mqtt is required for MQTT support. Please install the package."
    ) from e


class MQTTManager:
    """Simple wrapper around the paho-mqtt client."""

    def __init__(self, host: str = "localhost", port: int = 1883, topic: str = "owl/detections"):
        self.logger = logging.getLogger(__name__)
        self.host = host
        self.port = port
        self.topic = topic
        self.client = mqtt.Client()
        self._loop_started = False

    def connect(self) -> None:
        """Connect to the MQTT broker."""
        self.logger.info(f"Connecting to MQTT broker at {self.host}:{self.port}")
        self.client.connect(self.host, self.port)

    def start_loop(self) -> None:
        """Start the MQTT network loop in a background thread."""
        if not self._loop_started:
            self.client.loop_start()
            self._loop_started = True

    def stop_loop(self) -> None:
        """Stop the background MQTT network loop."""
        if self._loop_started:
            self.client.loop_stop()
            self._loop_started = False

    def subscribe(self, topic: str, callback) -> None:
        """Subscribe to a topic with the provided callback."""
        self.logger.info(f"Subscribing to MQTT topic: {topic}")
        self.client.subscribe(topic)
        self.client.on_message = callback

    def publish(self, payload: Dict[str, Any]) -> None:
        """Publish a JSON payload to the configured topic."""
        try:
            message = json.dumps(payload)
            self.client.publish(self.topic, message)
        except Exception as exc:  # pragma: no cover - network errors only
            self.logger.error(f"MQTT publish failed: {exc}")

    def disconnect(self) -> None:
        """Disconnect from the broker."""
        self.stop_loop()
        self.client.disconnect()

