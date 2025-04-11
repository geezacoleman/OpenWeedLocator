import time
import json
import logging
import configparser
from multiprocessing import Value
import paho.mqtt.client as mqtt

# Set up logging.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

###############################################################################
# Dummy Interfaces (Replace these with your actual Owl and StatusIndicator implementations need help setting up)
###############################################################################
class DummyOwl:
    def __init__(self):
        self.sample_images = False
        self.disable_detection = True
        self.show_display = False
        self.exg_min = 0
        self.exg_max = 0
        self.hue_min = 0
        self.hue_max = 0
        self.saturation_min = 0
        self.saturation_max = 0
        self.brightness_min = 0
        self.brightness_max = 0
        self.relay_controller = type("RelayController", (), {
            "relay": type("Relay", (), {
                "all_on": lambda: logger.info("Relays all on"),
                "all_off": lambda: logger.info("Relays all off")
            })()
        })()
        self.window_name = "DummyWindow"

class DummyStatusIndicator:
    def start_storage_indicator(self): pass
    def enable_weed_detection(self): logger.info("Weed detection enabled")
    def disable_weed_detection(self): logger.info("Weed detection disabled")
    def enable_image_recording(self): logger.info("Image recording enabled")
    def disable_image_recording(self): logger.info("Image recording disabled")
    def generic_notification(self): logger.info("Generic notification")

###############################################################################
# Advanced Controller for a Slave
###############################################################################
class SimpleAdvancedController:
    """
    This controller updates advanced settings using MQTT commands.
    It runs on a slave device with a unique slave_id.
    """
    def __init__(self, slave_id, low_config, high_config, owl, status, stop_flag):
        self.slave_id = slave_id  # Unique identifier for this slave (e.g. "0x201")
        self.stop_flag = stop_flag

        # Internal shared states.
        self.recording_state = Value('b', False)
        self.sensitivity_state = Value('b', False)
        self.detection_mode_state = Value('i', 1)  # "1" means Off

        self.owl = owl
        self.status_indicator = status

        # Read sensitivity configuration from INI files.
        self.low_sensitivity_settings = self._read_config(low_config)
        self.high_sensitivity_settings = self._read_config(high_config)

    def _read_config(self, config_file):
        config = configparser.ConfigParser()
        config.read(config_file)
        return {
            'exg_min': config.getint('GreenOnBrown', 'exg_min'),
            'exg_max': config.getint('GreenOnBrown', 'exg_max'),
            'hue_min': config.getint('GreenOnBrown', 'hue_min'),
            'hue_max': config.getint('GreenOnBrown', 'hue_max'),
            'saturation_min': config.getint('GreenOnBrown', 'saturation_min'),
            'saturation_max': config.getint('GreenOnBrown', 'saturation_max'),
            'brightness_min': config.getint('GreenOnBrown', 'brightness_min'),
            'brightness_max': config.getint('GreenOnBrown', 'brightness_max')
        }

    def update_recording(self, state_str):
        """Update recording state based on a command ('on' or 'off')."""
        is_active = state_str.lower() == 'on'
        with self.recording_state.get_lock():
            self.recording_state.value = is_active
        if is_active:
            self.status_indicator.enable_image_recording()
            self.owl.sample_images = True
        else:
            self.status_indicator.disable_image_recording()
            self.owl.sample_images = False
        logger.info(f"Slave {self.slave_id}: recording set to {is_active}")

    def update_sensitivity(self, value):
        """Update sensitivity settings based on a numeric value."""
        try:
            sensitivity_value = float(value)
        except ValueError:
            logger.error("Slave %s: Invalid sensitivity value", self.slave_id)
            return

        with self.sensitivity_state.get_lock():
            self.sensitivity_state.value = sensitivity_value > 0
        settings = self.low_sensitivity_settings if self.sensitivity_state.value else self.high_sensitivity_settings

        # Update Owl instance settings.
        self.owl.exg_min = settings['exg_min']
        self.owl.exg_max = settings['exg_max']
        self.owl.hue_min = settings['hue_min']
        self.owl.hue_max = settings['hue_max']
        self.owl.saturation_min = settings['saturation_min']
        self.owl.saturation_max = settings['saturation_max']
        self.owl.brightness_min = settings['brightness_min']
        self.owl.brightness_max = settings['brightness_max']
        logger.info(f"Slave {self.slave_id}: sensitivity set to {sensitivity_value}")

    def update_detection_mode(self, mode):
        """Update detection mode:
           0 = Detection on, 1 = Off, 2 = All solenoids on.
        """
        try:
            mode = int(mode)
        except ValueError:
            logger.error("Slave %s: Invalid detection mode value", self.slave_id)
            return

        with self.detection_mode_state.get_lock():
            self.detection_mode_state.value = mode

        if mode == 0:
            self.status_indicator.enable_weed_detection()
            self.owl.disable_detection = False
        elif mode == 2:
            self.status_indicator.disable_weed_detection()
            self.owl.relay_controller.relay.all_on()
            self.owl.disable_detection = True
        else:
            self.status_indicator.disable_weed_detection()
            self.owl.relay_controller.relay.all_off()
            self.owl.disable_detection = True
        logger.info(f"Slave {self.slave_id}: detection mode set to {mode}")

    def run(self):
        try:
            while not self.stop_flag.value:
                time.sleep(0.1)
        except KeyboardInterrupt:
            logger.info(f"Slave {self.slave_id}: KeyboardInterrupt received, exiting run loop.")
            self.stop()

    def stop(self):
        with self.stop_flag.get_lock():
            self.stop_flag.value = True

###############################################################################
# Slave MQTT Client: Runs on each slave to listen for commands addressed to it.
###############################################################################
class SlaveMqttSubscriber:
    """
    This MQTT subscriber runs on the slave. It subscribes to a topic and processes
    only those messages that are intended for this slave, based on the "slave" field.
    """
    def __init__(self, broker, port, topic, slave_id, controller):
        self.slave_id = slave_id  # For example, "0x201" 
        self.topic = topic
        self.controller = controller  # Instance of SimpleAdvancedController for this slave
        self.client = mqtt.Client("Slave_MQTT_Client_" + slave_id)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message

        self.client.connect(broker, port, 60)
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, rc):
        logger.info(f"Slave {self.slave_id}: Connected to MQTT broker with code {rc}")
        client.subscribe(self.topic)

    def on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            msg_slave = payload.get("slave")
            # Process the message only if it is intended for this slave.
            if msg_slave != self.slave_id:
                return

            logger.info(f"Slave {self.slave_id}: Received message: {payload}")
            command = payload.get("command")
            if command == "recording":
                state = payload.get("state")
                if state is not None:
                    self.controller.update_recording(state)
            elif command in ["sensitivity", "files"]:
                value = payload.get("value")
                if value is not None:
                    self.controller.update_sensitivity(value)
            elif command == "detection_mode":
                mode = payload.get("mode")
                if mode is not None:
                    self.controller.update_detection_mode(mode)
            else:
                logger.warning(f"Slave {self.slave_id}: Unknown command received")
        except Exception as e:
            logger.error(f"Slave {self.slave_id}: Error processing MQTT message", exc_info=True)

###############################################################################
# Main: Set Up This Slave Device
###############################################################################
if __name__ == "__main__":
    # This slave's unique identifier.
    SLAVE_ID = "0x201"  # Change as needed for each slave.

    # Create a stop flag for orderly shutdown.
    stop_flag = Value('b', False)

    # Create your Owl and StatusIndicator instances.
    owl = DummyOwl()
    status = DummyStatusIndicator()

    # Create the Advanced Controller instance for this slave.
    controller = SimpleAdvancedController(
        slave_id=SLAVE_ID,
        low_config="low_config.ini",   # Replace with your configuration file paths.
        high_config="high_config.ini",
        owl=owl,
        status=status,
        stop_flag=stop_flag
    )
    logger.info(f"Slave {SLAVE_ID} controller initialized.")

    # Set up the MQTT subscriber on this slave.
    subscriber = SlaveMqttSubscriber(
        broker="localhost",    # Replace with your MQTT broker address.
        port=1883,
        topic="commands/can",
        slave_id=SLAVE_ID,
        controller=controller
    )

    # Run the controller loop until interrupted.
    try:
        while not stop_flag.value:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info(f"Slave {SLAVE_ID}: KeyboardInterrupt received; exiting.")
    finally:
        controller.stop()
        subscriber.client.loop_stop()
        subscriber.client.disconnect()
