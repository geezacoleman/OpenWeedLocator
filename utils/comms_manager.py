#!/usr/bin/env python3
import json
import threading
import time
import uuid
import logging
import socket
import queue
from typing import Dict, Any, Optional, Callable, List, Set

import utils.error_manager as errors
from utils.log_manager import LogManager

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


class MQTTError(errors.OWLError):
    """Base class for MQTT-related errors."""
    pass


class MQTTConnectionError(MQTTError):
    """Raised when there are issues connecting to the MQTT broker."""

    def __init__(self, broker: str = None, port: int = None, reason: str = None):
        super().__init__(
            message=None,
            details={
                'broker': broker,
                'port': port,
                'reason': reason
            }
        )

        message = (
                self.format_error_header("MQTT Connection Error") +
                self.format_section(
                    "Problem",
                    f"Failed to connect to MQTT broker: {self.colorize(f'{broker}:{port}', 'WHITE', bold=True)}\n"
                    f"Reason: {reason if reason else 'Unknown connection issue'}"
                ) +
                self.format_section(
                    "Fix",
                    "1. Verify the broker address and port are correct\n"
                    "2. Check network connectivity\n"
                    "3. Ensure the MQTT broker is running\n"
                    "4. Verify firewall rules allow MQTT traffic"
                )
        )
        self.args = (message,)


class MQTTConfigError(MQTTError):
    """Raised when there are issues with MQTT configuration."""

    def __init__(self, config_key: str = None, section: str = "MQTT"):
        super().__init__(
            message=None,
            details={
                'config_key': config_key,
                'section': section
            }
        )

        message = (
                self.format_error_header("MQTT Configuration Error") +
                self.format_section(
                    "Problem",
                    f"Invalid or missing MQTT configuration: {self.colorize(config_key, 'WHITE', bold=True)} "
                    f"in section [{self.colorize(section, 'WHITE', bold=True)}]"
                ) +
                self.format_section(
                    "Fix",
                    "1. Check your config.ini file\n"
                    f"2. Add or correct the {config_key} setting in [{section}] section\n"
                    "3. Ensure the broker details are correct"
                )
        )
        self.args = (message,)


class MQTTPublishError(MQTTError):
    """Raised when there are issues publishing messages to MQTT."""

    def __init__(self, topic: str = None, reason: str = None):
        super().__init__(
            message=None,
            details={
                'topic': topic,
                'reason': reason
            }
        )

        message = (
                self.format_error_header("MQTT Publish Error") +
                self.format_section(
                    "Problem",
                    f"Failed to publish message to topic: {self.colorize(topic, 'WHITE', bold=True)}\n"
                    f"Reason: {reason if reason else 'Unknown publish error'}"
                ) +
                self.format_section(
                    "Fix",
                    "1. Verify connection to the broker\n"
                    "2. Check if topic structure is valid\n"
                    "3. Ensure message size is within limits\n"
                    "4. Check broker permissions for publishing"
                )
        )
        self.args = (message,)


class MQTTManager:
    """
    Manages MQTT communication for OWL devices.

    Handles subscribing to command topics and publishing status updates,
    error reports, and detection events in a non-blocking manner.
    """

    # Default MQTT settings
    DEFAULT_PORT = 1883
    DEFAULT_KEEPALIVE = 60
    DEFAULT_QOS = 0
    RECONNECT_DELAY_MIN = 1
    RECONNECT_DELAY_MAX = 120
    MAX_QUEUE_SIZE = 1000

    def __init__(self,
                 broker_host: str,
                 broker_port: int = DEFAULT_PORT,
                 device_id: str = None,
                 username: str = None,
                 password: str = None,
                 owl_instance=None):
        """
        Initialize the MQTT manager with broker details and device ID.

        Args:
            broker_host: MQTT broker hostname or IP
            broker_port: MQTT broker port (default: 1883)
            device_id: Unique identifier for this OWL device (default: generated)
            username: MQTT broker username (optional)
            password: MQTT broker password (optional)
            owl_instance: Reference to the OWL instance (optional)
        """
        if mqtt is None:
            raise ImportError("paho-mqtt is not installed. Install with 'pip install paho-mqtt'")

        self.logger = LogManager.get_logger(__name__)
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.device_id = device_id or self._generate_device_id()
        self.username = username
        self.password = password
        self.owl = owl_instance

        # Connection state
        self.connected = False
        self.connecting = False
        self.stopping = False
        self.reconnect_delay = self.RECONNECT_DELAY_MIN

        # Message queues for outgoing messages
        self.status_queue = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self.error_queue = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self.detection_queue = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)

        # Thread control
        self.connect_thread = None
        self.status_thread = None
        self.error_thread = None
        self.detection_thread = None

        # Topic structure
        self.base_topic = f"owl/device/{self.device_id}"

        # Topic definitions
        self.topics = {
            # Command topics (subscribe)
            'command': {
                'settings': f"{self.base_topic}/command/settings",
                'model': f"{self.base_topic}/command/model",
                'location': f"{self.base_topic}/command/location",
            },
            # Status topics (publish)
            'telemetry': {
                'status': f"{self.base_topic}/status",
                'error': f"{self.base_topic}/error",
                'detection': f"{self.base_topic}/detection",
                'heartbeat': f"{self.base_topic}/heartbeat",
            }
        }

        # Callbacks for command handling
        self.command_handlers = {}

        # Initialize MQTT client
        self.client = mqtt.Client(client_id=f"owl-{self.device_id}")
        if username and password:
            self.client.username_pw_set(username, password)

        # Set up callbacks
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.on_publish = self._on_publish

        # Set up LWT (Last Will and Testament)
        self.client.will_set(
            f"{self.base_topic}/status",
            payload=json.dumps({"status": "offline", "device_id": self.device_id}),
            qos=1,
            retain=True
        )

        self.logger.info(f"MQTT Manager initialized for device {self.device_id}")

    def start(self):
        """Start the MQTT manager and connect to the broker."""
        if self.stopping:
            return

        if not self.connecting and not self.connected:
            self.stopping = False
            self.connecting = True

            # Start connection thread to avoid blocking
            self.connect_thread = threading.Thread(
                target=self._connect_thread,
                name="MQTTConnectThread"
            )
            self.connect_thread.daemon = True
            self.connect_thread.start()

            # Start publisher threads
            self._start_publisher_threads()

            self.logger.info(f"MQTT Manager starting, connecting to {self.broker_host}:{self.broker_port}")
        else:
            self.logger.info("MQTT Manager already started or connecting")

    def stop(self):
        """Stop the MQTT manager and disconnect from the broker."""
        self.logger.info("Stopping MQTT Manager")
        self.stopping = True

        # Publish offline status if connected
        if self.connected:
            try:
                self.client.publish(
                    f"{self.base_topic}/status",
                    payload=json.dumps({"status": "offline", "device_id": self.device_id}),
                    qos=1,
                    retain=True
                )
            except Exception as e:
                self.logger.warning(f"Failed to publish offline status: {e}")

        # Disconnect client
        try:
            self.client.disconnect()
        except Exception as e:
            self.logger.warning(f"Error disconnecting from MQTT broker: {e}")

        # Clear queues
        self._clear_queue(self.status_queue)
        self._clear_queue(self.error_queue)
        self._clear_queue(self.detection_queue)

        # Wait for threads to end
        threads = [self.connect_thread, self.status_thread,
                   self.error_thread, self.detection_thread]

        for thread in threads:
            if thread and thread.is_alive():
                thread.join(timeout=1.0)

        self.logger.info("MQTT Manager stopped")

    def _clear_queue(self, q):
        """Clear a queue without blocking."""
        try:
            while True:
                q.get_nowait()
                q.task_done()
        except queue.Empty:
            pass

    def _connect_thread(self):
        """Connection thread to handle broker connection and reconnection."""
        while not self.stopping:
            try:
                self.logger.info(f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}")
                self.client.connect(
                    self.broker_host,
                    self.broker_port,
                    keepalive=self.DEFAULT_KEEPALIVE
                )
                self.client.loop_start()
                break  # Exit loop if connection successful

            except (socket.error, OSError) as e:
                self.logger.warning(f"Failed to connect to MQTT broker: {e}")
                if not self.stopping:
                    self.logger.info(f"Retrying connection in {self.reconnect_delay} seconds")
                    time.sleep(self.reconnect_delay)
                    # Exponential backoff with maximum delay
                    self.reconnect_delay = min(self.reconnect_delay * 2, self.RECONNECT_DELAY_MAX)

        self.connecting = False

    def _start_publisher_threads(self):
        """Start threads for publishing different types of messages."""
        # Status publisher thread
        self.status_thread = threading.Thread(
            target=self._publisher_thread,
            args=(self.status_queue, self.topics['telemetry']['status'], 1),
            name="MQTTStatusThread"
        )
        self.status_thread.daemon = True
        self.status_thread.start()

        # Error publisher thread
        self.error_thread = threading.Thread(
            target=self._publisher_thread,
            args=(self.error_queue, self.topics['telemetry']['error'], 1),
            name="MQTTErrorThread"
        )
        self.error_thread.daemon = True
        self.error_thread.start()

        # Detection publisher thread
        self.detection_thread = threading.Thread(
            target=self._detection_publisher_thread,
            name="MQTTDetectionThread"
        )
        self.detection_thread.daemon = True
        self.detection_thread.start()

    def _publisher_thread(self, queue_obj, topic, qos=0):
        """Generic publisher thread for sending messages from a queue."""
        while not self.stopping:
            try:
                message = queue_obj.get(timeout=1.0)

                if self.connected:
                    try:
                        payload = json.dumps(message)
                        result = self.client.publish(topic, payload=payload, qos=qos)
                        result.wait_for_publish(timeout=2.0)

                        if not result.is_published():
                            self.logger.warning(f"Failed to publish message to {topic}")
                            # Could requeue message here if needed

                    except Exception as e:
                        self.logger.error(f"Error publishing to {topic}: {e}")
                else:
                    # If disconnected, put the message back in the queue
                    try:
                        queue_obj.put(message, block=False)
                    except queue.Full:
                        self.logger.warning(f"Queue full, dropping message for {topic}")

                queue_obj.task_done()

            except queue.Empty:
                pass  # No messages in queue
            except Exception as e:
                self.logger.error(f"Error in publisher thread for {topic}: {e}")

    def _detection_publisher_thread(self):
        """Special thread for handling detection messages with batching."""
        batch = []
        batch_size = 10  # Maximum batch size
        last_publish_time = time.time()
        max_wait_time = 0.5  # Maximum time to wait before publishing a batch

        detection_topic = self.topics['telemetry']['detection']

        while not self.stopping:
            try:
                # Get message or timeout
                try:
                    message = self.detection_queue.get(timeout=0.1)
                    batch.append(message)
                    self.detection_queue.task_done()
                except queue.Empty:
                    pass  # No messages

                current_time = time.time()
                time_elapsed = current_time - last_publish_time

                # Publish if batch is full or max wait time elapsed
                if (len(batch) >= batch_size or
                        (len(batch) > 0 and time_elapsed >= max_wait_time)):

                    if self.connected:
                        try:
                            payload = json.dumps({
                                "detections": batch,
                                "timestamp": current_time,
                                "device_id": self.device_id,
                                "count": len(batch)
                            })

                            result = self.client.publish(
                                detection_topic,
                                payload=payload,
                                qos=self.DEFAULT_QOS
                            )
                            result.wait_for_publish(timeout=1.0)

                        except Exception as e:
                            self.logger.error(f"Error publishing detection batch: {e}")

                    # Reset batch and time
                    batch = []
                    last_publish_time = current_time

            except Exception as e:
                self.logger.error(f"Error in detection publisher thread: {e}")
                time.sleep(1.0)  # Avoid tight loop in case of repeated errors

    def _on_connect(self, client, userdata, flags, rc):
        """Callback for when the client connects to the broker."""
        if rc == 0:
            self.connected = True
            self.reconnect_delay = self.RECONNECT_DELAY_MIN  # Reset backoff
            self.logger.info("Connected to MQTT broker")

            # Subscribe to command topics
            for category, topics in self.topics['command'].items():
                if isinstance(topics, dict):
                    for name, topic in topics.items():
                        client.subscribe(topic, qos=1)
                else:
                    client.subscribe(topics, qos=1)

            # Publish online status
            online_status = {
                "status": "online",
                "device_id": self.device_id,
                "timestamp": time.time(),
                "ip_address": self._get_ip_address()
            }

            self.client.publish(
                self.topics['telemetry']['status'],
                payload=json.dumps(online_status),
                qos=1,
                retain=True
            )

            # Start heartbeat
            threading.Thread(
                target=self._heartbeat_thread,
                daemon=True,
                name="MQTTHeartbeatThread"
            ).start()

        else:
            error_messages = {
                1: "Connection refused - incorrect protocol version",
                2: "Connection refused - invalid client identifier",
                3: "Connection refused - server unavailable",
                4: "Connection refused - bad username or password",
                5: "Connection refused - not authorised"
            }

            error_msg = error_messages.get(rc, f"Unknown error code: {rc}")
            self.logger.error(f"Failed to connect to MQTT broker: {error_msg}")
            self.connected = False

    def _on_disconnect(self, client, userdata, rc):
        """Callback for when the client disconnects from the broker."""
        self.connected = False
        if rc != 0 and not self.stopping:
            self.logger.warning(f"Unexpected disconnection from MQTT broker, rc={rc}")
            # Reconnect will be handled by loop_forever in connect thread
        else:
            self.logger.info("Disconnected from MQTT broker")

    def _on_message(self, client, userdata, msg):
        """Callback for when a message is received from the broker."""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')
            self.logger.debug(f"Received message on topic {topic}: {payload}")

            # Parse JSON payload
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                self.logger.warning(f"Received invalid JSON on topic {topic}")
                return

            # Handle command based on topic
            for category, topics in self.topics['command'].items():
                if isinstance(topics, dict):
                    for cmd_type, cmd_topic in topics.items():
                        if topic == cmd_topic and cmd_type in self.command_handlers:
                            try:
                                self.command_handlers[cmd_type](data)
                            except Exception as e:
                                self.logger.error(f"Error handling {cmd_type} command: {e}")
                elif topic == topics and category in self.command_handlers:
                    try:
                        self.command_handlers[category](data)
                    except Exception as e:
                        self.logger.error(f"Error handling {category} command: {e}")

        except Exception as e:
            self.logger.error(f"Error processing MQTT message: {e}")

    def _on_publish(self, client, userdata, mid):
        """Callback for when a message is published."""
        self.logger.debug(f"Message {mid} published successfully")

    def _get_ip_address(self) -> str:
        """Get the device's IP address."""
        try:
            # Create a socket to determine the outbound IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            # Fallback if we can't determine the IP
            return socket.gethostname()

    def _generate_device_id(self) -> str:
        """Generate a unique device ID based on MAC address or random UUID."""
        try:
            # Try to use MAC address for stability
            mac = uuid.getnode()
            if (mac >> 40) % 2 == 0:  # Check if MAC is unique and not locally administered
                return f"{mac:012x}"
        except Exception:
            pass

        return str(uuid.uuid4())[:8]

    def _heartbeat_thread(self):
        """Thread to send periodic heartbeat messages."""
        while self.connected and not self.stopping:
            try:
                heartbeat = {
                    "device_id": self.device_id,
                    "timestamp": time.time()
                }

                if self.owl:
                    if hasattr(self.owl, 'disable_detection'):
                        heartbeat["detection_enabled"] = not self.owl.disable_detection
                    if hasattr(self.owl, 'sample_images'):
                        heartbeat["recording_enabled"] = self.owl.sample_images

                self.client.publish(
                    self.topics['telemetry']['heartbeat'],
                    payload=json.dumps(heartbeat),
                    qos=0
                )

            except Exception as e:
                self.logger.warning(f"Error sending heartbeat: {e}")

            time.sleep(30)  # Send heartbeat every 30 seconds

    def register_command_handler(self, command_type: str, callback: Callable[[Dict], None]):
        """
        Register a callback function for a command type.

        Args:
            command_type: Type of command ('settings', 'model', etc.)
            callback: Function that will be called with the command data
        """
        self.command_handlers[command_type] = callback
        self.logger.info(f"Registered handler for '{command_type}' commands")

    def publish_status(self, status_data: Dict[str, Any]):
        """
        Publish a status update.

        Args:
            status_data: Dictionary with status information
        """
        if not status_data.get('device_id'):
            status_data['device_id'] = self.device_id

        if not status_data.get('timestamp'):
            status_data['timestamp'] = time.time()

        try:
            self.status_queue.put(status_data, block=False)
        except queue.Full:
            self.logger.warning("Status queue full, dropping status update")

    def publish_error(self, error_data: Dict[str, Any]):
        """
        Publish an error message.

        Args:
            error_data: Dictionary with error information
        """
        if not error_data.get('device_id'):
            error_data['device_id'] = self.device_id

        if not error_data.get('timestamp'):
            error_data['timestamp'] = time.time()

        try:
            self.error_queue.put(error_data, block=False)
        except queue.Full:
            self.logger.warning("Error queue full, dropping error message")

    def publish_detection(self, detection_data: Dict[str, Any]):
        """
        Publish a detection event.

        Args:
            detection_data: Dictionary with detection information
        """
        if not detection_data.get('device_id'):
            detection_data['device_id'] = self.device_id

        if not detection_data.get('timestamp'):
            detection_data['timestamp'] = time.time()

        try:
            self.detection_queue.put(detection_data, block=False)
        except queue.Full:
            self.logger.warning("Detection queue full, dropping detection event")

    def add_custom_topic(self, category: str, topic_name: str, topic_path: str = None):
        """
        Add a custom topic to the topic structure.

        Args:
            category: Topic category ('command' or 'telemetry')
            topic_name: Name for the new topic
            topic_path: Full topic path (default: auto-generated from base topic)
        """
        if category not in self.topics:
            self.topics[category] = {}

        if isinstance(self.topics[category], str):
            current_topic = self.topics[category]
            self.topics[category] = {'default': current_topic}

        if not topic_path:
            topic_path = f"{self.base_topic}/{category}/{topic_name}"

        self.topics[category][topic_name] = topic_path

        # If we're adding a command topic and we're connected, subscribe to it
        if category == 'command' and self.connected:
            self.client.subscribe(topic_path, qos=1)

        self.logger.info(f"Added custom topic '{topic_name}' to category '{category}': {topic_path}")
        return topic_path

    # Add to MQTTManager class in comms_manager.py

    def setup_command_topics(self):
        """
        Set up command topics for OWL control using state variables.
        These topics allow remote control of detection and recording functions.
        """
        # Core control commands
        self.add_custom_topic('command', 'detection', f"{self.base_topic}/command/detection")
        self.add_custom_topic('command', 'recording', f"{self.base_topic}/command/recording")
        self.add_custom_topic('command', 'system', f"{self.base_topic}/command/system")

        # Register handlers
        self.register_command_handler('detection', self._handle_detection_command)
        self.register_command_handler('recording', self._handle_recording_command)
        self.register_command_handler('system', self._handle_system_command)

        self.logger.info("OWL control command topics initialized")

    def _handle_detection_command(self, data):
        """
        Handle detection control command by changing the detection_state.

        Args:
            data: Command data with action
        """
        if not self.owl:
            self.logger.warning("Cannot control detection - no OWL instance available")
            return

        if not hasattr(self.owl, 'detection_state'):
            self.logger.warning("Cannot control detection - detection_state not found")
            return

        action = data.get('action')

        if action == 'enable':
            with self.owl.detection_state.get_lock():
                self.owl.detection_state.value = True
                if hasattr(self.owl, 'disable_detection'):
                    self.owl.disable_detection = False
            self.logger.info("Detection enabled via MQTT")
            self.publish_status({"detection": "enabled"})

        elif action == 'disable':
            with self.owl.detection_state.get_lock():
                self.owl.detection_state.value = False
                if hasattr(self.owl, 'disable_detection'):
                    self.owl.disable_detection = True
            self.logger.info("Detection disabled via MQTT")
            self.publish_status({"detection": "disabled"})

        else:
            self.logger.warning(f"Unknown detection action: {action}")

    def _handle_recording_command(self, data):
        """
        Handle recording control command by changing the sample_state.

        Args:
            data: Command data with action
        """
        if not self.owl:
            self.logger.warning("Cannot control recording - no OWL instance available")
            return

        if not hasattr(self.owl, 'sample_state'):
            self.logger.warning("Cannot control recording - sample_state not found")
            return

        action = data.get('action')

        if action == 'enable':
            with self.owl.sample_state.get_lock():
                self.owl.sample_state.value = True
                if hasattr(self.owl, 'sample_images'):
                    self.owl.sample_images = True
                    # Notify status indicator if available
                    if hasattr(self.owl, 'status_indicator'):
                        self.owl.status_indicator.enable_image_recording()
            self.logger.info("Recording enabled via MQTT")
            self.publish_status({"recording": "enabled"})

        elif action == 'disable':
            with self.owl.sample_state.get_lock():
                self.owl.sample_state.value = False
                if hasattr(self.owl, 'sample_images'):
                    self.owl.sample_images = False
                    # Notify status indicator if available
                    if hasattr(self.owl, 'status_indicator'):
                        self.owl.status_indicator.disable_image_recording()
            self.logger.info("Recording disabled via MQTT")
            self.publish_status({"recording": "disabled"})

        else:
            self.logger.warning(f"Unknown recording action: {action}")

    def _handle_system_command(self, data):
        """
        Handle system-wide commands like stop.

        Args:
            data: Command data with action
        """
        if not self.owl:
            self.logger.warning("Cannot control system - no OWL instance available")
            return

        action = data.get('action')

        if action == 'stop':
            if hasattr(self.owl, 'stop_flag'):
                with self.owl.stop_flag.get_lock():
                    self.owl.stop_flag.value = True
                self.logger.info("OWL stop requested via MQTT")
                self.publish_status({"system": "stopping"})
            else:
                # Fallback to direct method call
                self.logger.info("OWL stop requested via direct method call")
                self.owl.stop()

        elif action == 'restart':
            # First send status update
            self.logger.info("OWL restart requested via MQTT")
            self.publish_status({"system": "restarting"})

            # Request a reboot
            self.request_reboot(reason="Restart requested via MQTT")

        else:
            self.logger.warning(f"Unknown system action: {action}")

    def get_status(self):
        """
        Get the current status of detection and recording.
        """
        status = {
            "device_id": self.device_id,
            "timestamp": time.time()
        }

        # Add detection state if available
        if self.owl and hasattr(self.owl, 'detection_state'):
            status["detection"] = "enabled" if self.owl.detection_state.value else "disabled"

        # Add recording state if available
        if self.owl and hasattr(self.owl, 'sample_state'):
            status["recording"] = "enabled" if self.owl.sample_state.value else "disabled"

        return status

    def publish_current_status(self):
        """
        Publish the current status of the OWL device.
        """
        status = self.get_status()
        self.publish_status(status)
        self.logger.debug("Published current status")

    @classmethod
    def from_config(cls, config, owl_instance=None):
        """
        Create an MQTTManager instance from a ConfigParser object.

        Args:
            config: ConfigParser with MQTT section
            owl_instance: Reference to the OWL instance

        Returns:
            MQTTManager instance

        Raises:
            MQTTConfigError: If required configuration is missing
        """
        if 'MQTT' not in config:
            raise MQTTConfigError(config_key='section', section='MQTT')

        # Required parameters
        try:
            broker_host = config.get('MQTT', 'broker_host')
        except Exception:
            raise MQTTConfigError(config_key='broker_host')

        # Optional parameters
        try:
            broker_port = config.getint('MQTT', 'broker_port')
        except Exception:
            broker_port = cls.DEFAULT_PORT

        try:
            device_id = config.get('MQTT', 'device_id')
        except Exception:
            device_id = None

        try:
            username = config.get('MQTT', 'username')
            password = config.get('MQTT', 'password')
        except Exception:
            username = None
            password = None

        return cls(
            broker_host=broker_host,
            broker_port=broker_port,
            device_id=device_id,
            username=username,
            password=password,
            owl_instance=owl_instance
        )


class SystemMonitor:
    """
    Monitors system metrics like CPU usage, temperature, and memory usage.
    Publishes these metrics via MQTT.
    """

    def __init__(self, mqtt_manager, interval=60):
        """
        Initialize system monitor with MQTT manager.

        Args:
            mqtt_manager: MQTTManager instance for publishing metrics
            interval: Monitoring interval in seconds (default: 60)
        """
        self.logger = LogManager.get_logger(__name__)
        self.mqtt_manager = mqtt_manager
        self.interval = interval
        self.stopping = False
        self.monitor_thread = None

        # Add telemetry topic if not exists
        if 'system' not in self.mqtt_manager.topics['telemetry']:
            self.mqtt_manager.add_custom_topic('telemetry', 'system',
                                               f"{self.mqtt_manager.base_topic}/system")

    def start(self):
        """Start the system monitoring thread."""
        if self.monitor_thread is None or not self.monitor_thread.is_alive():
            self.stopping = False
            self.monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="SystemMonitorThread"
            )
            self.monitor_thread.daemon = True
            self.monitor_thread.start()
            self.logger.info(f"System monitoring started, interval: {self.interval}s")

    def stop(self):
        """Stop the system monitoring thread."""
        self.stopping = True
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=2.0)
        self.logger.info("System monitoring stopped")

    def _monitor_loop(self):
        """Main monitoring loop that collects and publishes metrics."""
        while not self.stopping:
            try:
                # Collect system metrics
                metrics = self._collect_metrics()

                # Publish metrics
                if metrics and self.mqtt_manager.connected:
                    self.mqtt_manager.client.publish(
                        self.mqtt_manager.topics['telemetry']['system'],
                        payload=json.dumps(metrics),
                        qos=0  # Use QoS 0 for telemetry to reduce overhead
                    )

            except Exception as e:
                self.logger.error(f"Error in system monitoring: {e}")

            # Sleep until next collection interval
            for _ in range(int(self.interval / 0.5)):
                if self.stopping:
                    break
                time.sleep(0.5)

    def _collect_metrics(self):
        """
        Collect system metrics including CPU, memory, temperature.

        Returns:
            dict: Dictionary containing system metrics
        """
        metrics = {
            'device_id': self.mqtt_manager.device_id,
            'timestamp': time.time()
        }

        # Get CPU usage
        try:
            metrics['cpu_usage'] = self._get_cpu_usage()
        except Exception as e:
            self.logger.warning(f"Failed to get CPU usage: {e}")

        # Get memory usage
        try:
            metrics['memory'] = self._get_memory_usage()
        except Exception as e:
            self.logger.warning(f"Failed to get memory usage: {e}")

        # Get temperature (Raspberry Pi specific)
        try:
            metrics['temperature'] = self._get_temperature()
        except Exception as e:
            self.logger.warning(f"Failed to get temperature: {e}")

        # Get disk usage
        try:
            metrics['disk'] = self._get_disk_usage()
        except Exception as e:
            self.logger.warning(f"Failed to get disk usage: {e}")

        return metrics

    def _get_cpu_usage(self):
        """Get CPU usage percentage."""
        import psutil
        return psutil.cpu_percent(interval=1)

    def _get_memory_usage(self):
        """Get memory usage statistics."""
        import psutil
        memory = psutil.virtual_memory()
        return {
            'total_mb': memory.total / (1024 * 1024),
            'available_mb': memory.available / (1024 * 1024),
            'used_percent': memory.percent
        }

    def _get_temperature(self):
        """Get CPU temperature (Raspberry Pi specific)."""
        try:
            # Try to read from thermal zone
            with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                temp = float(f.read()) / 1000.0
                return temp
        except (FileNotFoundError, PermissionError):
            # Try using vcgencmd (Raspberry Pi specific)
            try:
                import subprocess
                output = subprocess.check_output(
                    ['vcgencmd', 'measure_temp'],
                    universal_newlines=True
                )
                temp = float(output.replace('temp=', '').replace('\'C', ''))
                return temp
            except (subprocess.SubprocessError, ValueError, FileNotFoundError):
                raise ValueError("Could not determine system temperature")

    def _get_disk_usage(self):
        """Get disk usage for the root filesystem."""
        import psutil
        usage = psutil.disk_usage('/')
        return {
            'total_gb': usage.total / (1024 * 1024 * 1024),
            'used_gb': usage.used / (1024 * 1024 * 1024),
            'free_gb': usage.free / (1024 * 1024 * 1024),
            'used_percent': usage.percent
        }


# Add this to the MQTTManager class
def create_system_monitor(self, interval=60):
    """
    Create and start a system monitor that publishes metrics via MQTT.

    Args:
        interval: Monitoring interval in seconds

    Returns:
        SystemMonitor: The created system monitor instance
    """
    monitor = SystemMonitor(self, interval)
    monitor.start()
    return monitor