# MQTT Command Interface & Slave Controller System

This project provides a centralized MQTT command interface (publisher) and a slave controller (subscriber) that communicate via an MQTT broker. The command interface is built with Tkinter and acts as a graphical user interface (similar to a Node‑RED dashboard), while slave devices receive and process commands intended for them based on their unique slave IDs.

---

## Table of Contents

1. [MQTT Broker Setup](#mqtt-broker-setup)
   - [Installing and Running an MQTT Broker on Windows](#installing-and-running-an-mqtt-broker-on-windows)
   - [Setting Up an MQTT Broker on Raspberry Pi](#setting-up-an-mqtt-broker-on-raspberry-pi)
   - [Creating a WiFi Access Point on Raspberry Pi for the Broker](#creating-a-wifi-access-point-on-raspberry-pi-for-the-broker)
2. [Publisher (Command Interface) Setup](#publisher-command-interface-setup)
3. [Slave Device Setup](#slave-device-setup)
4. [How It Works](#how-it-works)
5. [Extending the System](#extending-the-system)
6. [Troubleshooting](#troubleshooting)
7. [License and Credits](#license-and-credits)

---

## MQTT Broker Setup

### Installing and Running an MQTT Broker on Windows

1. **Download Mosquitto Broker for Windows:**
   - Visit the [Mosquitto download page](https://mosquitto.org/download/) and download the Windows installer (choose the latest stable version).

2. **Install Mosquitto:**
   - Run the installer and follow the on-screen instructions. By default, Mosquitto will be installed in a folder such as `C:\Program Files\Mosquitto\`.

3. **Configure Mosquitto (Optional):**
   - Mosquitto comes with a default configuration file (`mosquitto.conf`). To use custom settings, open the file in a text editor.  
   - Ensure that the listener is set up on port `1883` (the default) and that no authentication is enforced if you want to keep it simple.

4. **Start the Broker:**
   - Open a Command Prompt window with administrator privileges.
   - Navigate to the Mosquitto installation directory (e.g., `cd "C:\Program Files\Mosquitto"`).
   - Start the broker by running:
     ```bash
     mosquitto.exe -v
     ```
   - The `-v` flag enables verbose logging so you can see connection messages and any errors.

5. **Test the Broker:**
   - Use an MQTT client (such as MQTT.fx, MQTT Explorer, or even the command line) to subscribe to a topic (e.g., `commands/can`) and publish a test message to verify the broker works.

### Setting Up an MQTT Broker on Raspberry Pi

1. **Install Mosquitto on Raspberry Pi:**
   - Open a terminal on your Raspberry Pi.
   - Update your package lists:
     ```bash
     sudo apt update
     ```
   - Install Mosquitto and the clients package:
     ```bash
     sudo apt install mosquitto mosquitto-clients
     ```
   - Mosquitto should start automatically after installation.

2. **Configure Mosquitto (Optional):**
   - The default configuration usually works fine. If needed, edit the configuration file (often at `/etc/mosquitto/mosquitto.conf`) to adjust settings like listener ports or authentication.

3. **Test the Broker on Raspberry Pi:**
   - In one terminal, subscribe to a topic:
     ```bash
     mosquitto_sub -h localhost -t "commands/can" -v
     ```
   - In another terminal, publish a test message:
     ```bash
     mosquitto_pub -h localhost -t "commands/can" -m "Hello from Raspberry Pi"
     ```

### Creating a WiFi Access Point on Raspberry Pi for the Broker

(Instructions skipped here for brevity - see previous responses for full configuration)

---

## Publisher (Command Interface) Setup

1. **Verify MQTT Settings:**
   - Open `mqtt_interface.py` and ensure the MQTT broker address and port (e.g., `localhost` and `1883`) are correct.
2. **Edit Slave IDs:**
   - Modify the list of slave IDs as needed.
3. **Run the Script:**
   ```bash
   python mqtt_interface.py
   ```

---

## Slave Device Setup

1. **Prepare the Slave Device:**
   - Ensure the device (for example, a Raspberry Pi or Windows computer) has Python 3 installed and the required packages (`paho-mqtt`).

2. **Configure the Slave Script:**
   - Open `slave_controller.py`.
   - Set the `SLAVE_ID` variable to a unique identifier for this slave (e.g., `"0x201"`).
   - Update configuration file paths if necessary.
   - Replace the dummy classes (`DummyOwl` and `DummyStatusIndicator`) with your actual implementations.

3. **Run the Script:**
   ```bash
   python slave_controller.py
   ```

---

## How It Works

- **Publisher Script:**  
  Sends JSON payloads to `"commands/can"`. Example:
  ```json
  { "command": "recording", "state": "on" }
  ```

- **Slave Script:**  
  Subscribes to `"commands/can"` and processes only the commands where `"slave"` matches its `SLAVE_ID`.

---

## Extending the System

- **Add Commands:**  
  Define them in both `mqtt_interface.py` and `slave_controller.py`.

- **Add More Slaves:**  
  Run `slave_controller.py` on each additional device with a unique `SLAVE_ID`.

---

## Troubleshooting

- **Broker not connecting:**  
  Confirm broker is running and reachable at the configured address and port.

- **GUI not opening:**  
  Ensure you’re using a system that supports GUI (or use VNC or SSH X-forwarding on Raspberry Pi).

- **No commands received:**  
  Verify that the `"slave"` field is present and matches the actual `SLAVE_ID`.

---

## License and Credits

This system is provided as-is. You are free to use and modify for educational or commercial projects.

Happy coding!

