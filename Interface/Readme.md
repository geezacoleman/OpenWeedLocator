# MQTT Command Interface & Slave Controller System

This project provides a centralized MQTT command interface (publisher) and a slave controller (subscriber) that communicate via an MQTT broker. The command interface is built with Tkinter and acts as a graphical user interface (similar to a Node‑RED dashboard), while slave devices receive and process commands intended for them based on their unique slave IDs.

## Overview

- **MQTT Command Interface (Publisher):**  
  A Python/Tkinter GUI application that lets you send commands such as "All Nozzles On/Off", "Recording On/Off", adjust "Sensitivity" and "Files" via sliders, and send "Spot Spray" commands to a selectable slave.

- **Slave Controller (Subscriber):**  
  A Python script that runs on a slave device (e.g., a Raspberry Pi) and listens for MQTT commands on a common topic. The slave processes only messages that include its unique slave identifier.

## Components

- **Publisher Script:**  
  - **File:** `mqtt_interface.py`  
  - Provides a GUI interface to send MQTT messages to the topic `"commands/can"`.
  - Commands include global commands (all nozzles, recording, sensitivity, files) and spot spray commands with a selectable slave ID.

- **Slave Script:**  
  - **File:** `slave_controller.py`  
  - Subscribes to the MQTT topic (`"commands/can"`) and processes messages intended for its own slave ID.
  - Updates internal state based on commands such as recording, sensitivity, and detection mode.
  - Uses dummy placeholder classes (`DummyOwl` and `DummyStatusIndicator`) which should be replaced with your actual implementations.

## Requirements

- **MQTT Broker:**  
  An MQTT broker (e.g., [Mosquitto](https://mosquitto.org/)) running on your network. The default configuration assumes the broker is on `localhost` (port `1883`).

- **Python 3:**  
  Ensure you have Python 3 installed.

- **Python Dependencies:**
  - `paho-mqtt`
  - `tkinter` (usually included with Python on most platforms)
  - Standard libraries: `json`, `logging`, `configparser`, and `multiprocessing`

To install the MQTT dependency, run:

```bash
pip install paho-mqtt
```

## Setup Instructions

### 1. Start an MQTT Broker

Make sure you have an MQTT broker running (for example, Mosquitto on `localhost:1883`).

### 2. Run the Command Interface Script

1. Verify that `mqtt_interface.py` contains the correct MQTT broker address and port.
2. Edit the list of slave IDs in the script if necessary.
3. Run the script with:

   ```bash
   python mqtt_interface.py
   ```

4. A Tkinter GUI window will open, allowing you to send commands.

### 3. Run the Slave Script

1. Edit the `SLAVE_ID` variable in `slave_controller.py` to match the unique identifier for the slave (e.g., `"0x201"`).
2. Update configuration file paths if necessary.
3. Replace the dummy classes (`DummyOwl` and `DummyStatusIndicator`) with your actual implementations.
4. Run the script with:

   ```bash
   python slave_controller.py
   ```

5. The slave script will subscribe to the MQTT topic `"commands/can"` and process only the commands that include its slave ID.

## How It Works

- **Publisher Script:**  
  When a command is issued (for example, pressing "Recording On"), the publisher creates a JSON message (e.g., `{ "command": "recording", "state": "on" }`) and publishes it to the MQTT topic `"commands/can"`.

- **Slave Script:**  
  The slave script listens on the MQTT topic and processes only the messages whose `"slave"` field matches its unique `SLAVE_ID`. It then updates its internal state accordingly (e.g., starting/stopping recording or adjusting sensitivity).

- **Multi-Slave Support:**  
  To support multiple slaves, run the `slave_controller.py` script on each device with a unique `SLAVE_ID`.

## Extending the System

### Adding More Commands

- Update both the publisher and slave scripts with additional command types and corresponding JSON payloads as needed.

### Adding More Slaves

- Run the `slave_controller.py` script on additional devices, each with a unique `SLAVE_ID`.

## Troubleshooting

### MQTT Connection Issues

- Verify that your MQTT broker is running and that the broker's address and port are correctly configured in both scripts.

### GUI Not Responding

- Check for errors in the console output. The Tkinter main loop should keep the GUI responsive.

### No Commands Received at Slave

- Ensure that the JSON payload sent by the publisher includes the correct `"slave"` field matching the slave’s `SLAVE_ID`.
- Check the logs for any errors.

## License and Credits

This sample code is provided as-is and you are free to use and modify it as needed.

Happy coding!
