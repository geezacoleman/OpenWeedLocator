import argparse
import logging
from typing import Any

from utils.mqtt_manager import MQTTManager

MAX_NODES = 40

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    """Interactive UI to control multiple OWL nodes via MQTT."""
    parser = argparse.ArgumentParser(description="MQTT UI for multiple OWL nodes")
    parser.add_argument("--host", default="localhost", help="MQTT broker host")
    parser.add_argument("--port", type=int, default=1883, help="MQTT broker port")
    parser.add_argument("--nodes", type=int, default=1, help="Number of OWL nodes (1-40)")
    args = parser.parse_args()

    if not 1 <= args.nodes <= MAX_NODES:
        parser.error(f"--nodes must be between 1 and {MAX_NODES}")

    manager = MQTTManager(host=args.host, port=args.port)
    try:
        manager.connect()
    except Exception as exc:  # pragma: no cover - network access only
        logger.error("Failed to connect to MQTT broker: %s", exc)
        return

    manager.start_loop()

    print(f"Connected to MQTT broker at {args.host}:{args.port}")
    print("Available commands:\n"
          "  start <id>     - start detections on node\n"
          "  stop <id>      - stop detections on node\n"
          "  shutdown <id>  - shutdown node\n"
          "  all_on         - turn all relays on\n"
          "  all_off        - turn all relays off\n"
          "  exit           - quit UI")

    try:
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                break

            if text in {"exit", "quit"}:
                break
            if text == "all_on":
                for node in range(1, args.nodes + 1):
                    manager.publish_to_topic(f"owl/{node}/control", {"command": "all_on"})
                continue
            if text == "all_off":
                for node in range(1, args.nodes + 1):
                    manager.publish_to_topic(f"owl/{node}/control", {"command": "all_off"})
                continue

            parts = text.split()
            if len(parts) == 2 and parts[0] in {"start", "stop", "shutdown"}:
                try:
                    node = int(parts[1])
                except ValueError:
                    print("Invalid node id")
                    continue
                if not 1 <= node <= args.nodes:
                    print(f"Node id must be between 1 and {args.nodes}")
                    continue
                manager.publish_to_topic(f"owl/{node}/control", {"command": parts[0]})
            else:
                print("Unknown command")
    finally:
        manager.disconnect()
        print("Disconnected from broker")


if __name__ == "__main__":
    main()
