import argparse
import logging
from typing import Any

from utils.mqtt_manager import MQTTManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def on_message(client: Any, userdata: Any, message: Any) -> None:
    """Print incoming MQTT messages."""
    payload = message.payload.decode("utf-8")
    print(f"\n[MSG] {message.topic}: {payload}")


def main() -> None:
    """Interactive command line interface for an MQTT broker."""
    parser = argparse.ArgumentParser(description="Simple MQTT broker interface")
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument(
        "--topic",
        default="owl/detections",
        help="Topic to subscribe and publish to",
    )

    args = parser.parse_args()

    manager = MQTTManager(host=args.host, port=args.port, topic=args.topic)
    manager.connect()
    manager.subscribe(args.topic, on_message)
    manager.start_loop()

    print(
        f"Connected to MQTT broker at {args.host}:{args.port} on topic '{args.topic}'"
    )
    print("Type messages to publish or 'exit' to quit.")

    try:
        while True:
            try:
                text = input(" > ")
            except EOFError:
                break
            if text.lower() in {"exit", "quit"}:
                break
            manager.publish({"text": text})
    finally:
        manager.disconnect()
        print("Disconnected from broker.")


if __name__ == "__main__":
    main()
