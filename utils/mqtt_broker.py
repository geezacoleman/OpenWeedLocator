import asyncio
import argparse
import logging
import signal
from hbmqtt.broker import Broker


DEFAULT_CONFIG = {
    "listeners": {
        "default": {
            "type": "tcp",
            "bind": "0.0.0.0:1883",
        }
    },
    "sys_interval": 10,
    "auth": {
        "allow-anonymous": True
    },
}


async def start_broker(host: str, port: int) -> Broker:
    """Create and start an MQTT broker."""
    config = DEFAULT_CONFIG.copy()
    config["listeners"]["default"]["bind"] = f"{host}:{port}"
    broker = Broker(config)
    await broker.start()
    return broker


async def main() -> None:
    """Run a simple MQTT broker for local testing."""
    parser = argparse.ArgumentParser(description="Run a minimal MQTT broker")
    parser.add_argument("--host", default="0.0.0.0", help="Broker listen host")
    parser.add_argument("--port", type=int, default=1883, help="Broker port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    broker = await start_broker(args.host, args.port)
    logging.info("Broker running at %s:%s", args.host, args.port)

    shutdown_event = asyncio.Event()

    def _signal_handler(*_: int) -> None:
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _signal_handler)

    await shutdown_event.wait()
    await broker.shutdown()
    logging.info("Broker shutdown")


if __name__ == "__main__":
    asyncio.run(main())
