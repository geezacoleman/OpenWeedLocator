# Node-RED Multi-Slave Wireless CAN Command System

This project demonstrates a multi-slave command system where a global control dashboard sends both global and slave-specific commands over MQTT. These commands are then received by individual Node‑RED flows—one for each slave device (0x201, 0x202, 0x203, and 0x204). The commands can be used to trigger hardware actions such as controlling CAN bus interfaces, GPIO outputs, or other peripherals.

## Overview

The system consists of two main parts:

1. **Global/Control Flow (Dashboard):**
   - Provides dashboard controls (buttons, switches, sliders) to issue commands.
   - **Global commands include:**
     - **All Nozzles On/Off:**  
       Example payload: `{ "command": "all_nozzles", "state": "on" }`
     - **Recording On/Off:**  
       Example payload: `{ "command": "recording", "state": "off" }`
     - **Sensitivity (Slider 1–10):**  
       Example payload: `{ "command": "sensitivity", "value": 7 }`
     - **Files (Slider 1–10):**  
       Example payload: `{ "command": "files", "value": 3 }`
   - **Slave-specific commands include:**
     - **Spot Spray Enable (per slave):**  
       Example payload for slave 0x201:  
       `{ "command": "spot_spray", "slave": "0x201", "state": "on" }`

   All dashboard commands are formatted as JSON and published to the MQTT topic `commands/can`.

2. **Slave Flows:**
   - There are four separate Node‑RED flows (one per slave: 0x201, 0x202, 0x203, and 0x204).
   - Each slave flow subscribes to the MQTT topic `commands/can` and processes incoming JSON commands.
   - Global commands (e.g. `all_nozzles`, `recording`, `sensitivity`, `files`) are applied to every slave.
   - Slave-specific commands (e.g. `spot_spray`) are only processed if the message’s `slave` field matches the slave’s address.
   - Processed commands can be forwarded to a hardware interface (for example, a SocketCAN-out node) or used to control local hardware directly.

## Flows Included

- **Global/Control Flow (Dashboard):**  
  Contains dashboard nodes (buttons, switches, sliders) and function nodes that format commands before publishing them via MQTT.  
  _File: `flows-global.json`_

- **Slave 0x201 Flow:**  
  Subscribes to `commands/can`, filters, and processes commands for slave address 0x201.  
  _File: `flows-slave-0x201.json`_

- **Slave 0x202 Flow:**  
  Processes commands for slave address 0x202.  
  _File: `flows-slave-0x202.json`_

- **Slave 0x203 Flow:**  
  Processes commands for slave address 0x203.  
  _File: `flows-slave-0x203.json`_

- **Slave 0x204 Flow:**  
  Processes commands for slave address 0x204.  
  _File: `flows-slave-0x204.json`_

## Setup and Installation

### Prerequisites

- **Node-RED:**  
  Install Node-RED (for example, on a Raspberry Pi or any other device).  
  [Installation Instructions](https://nodered.org/docs/getting-started/installation)

- **Node-RED Dashboard:**  
  Install the Node-RED dashboard nodes via the palette manager or by running:
  ```bash
  npm install node-red-dashboard
  ```

- **MQTT Nodes:**  
  Ensure the MQTT nodes are installed (they are usually included with Node-RED).

- **MQTT Broker:**  
  Run an MQTT broker (for example, Mosquitto) that is accessible from your Node-RED device. The flows assume the broker is at `localhost:1883`.

### Importing the Flows

1. Open the Node-RED editor.
2. From the menu, choose **Import > Clipboard**.
3. Copy and paste the JSON export for each flow:
   - Global/Control Flow
   - Slave 0x201 Flow
   - Slave 0x202 Flow
   - Slave 0x203 Flow
   - Slave 0x204 Flow
4. Deploy the flows.
5. Adjust MQTT broker settings if necessary (double-click the MQTT nodes and modify the `broker` field).

### Running the System

- **Global Dashboard:**  
  Open the Node-RED Dashboard URL (usually `http://<your-node-red-IP>:1880/ui`) in your browser. Use the controls to send commands.
- **Slave Devices:**  
  Each slave flow subscribes to the MQTT topic `commands/can` and processes only the commands relevant to that slave. Verify message processing via the Node-RED debug sidebar.

## Directory Structure

```
/node-red-project
  ├── flows-global.json      # Global control flow export
  ├── flows-slave-0x201.json   # Slave 0x201 flow export
  ├── flows-slave-0x202.json   # Slave 0x202 flow export
  ├── flows-slave-0x203.json   # Slave 0x203 flow export
  ├── flows-slave-0x204.json   # Slave 0x204 flow export
  └── README.md
```

## Troubleshooting

- **MQTT Connection:**  
  Ensure your MQTT broker is running and reachable. Use an MQTT client (e.g., MQTT Explorer) to verify that messages are published to and received on `commands/can`.

- **Message Processing:**  
  Check the Node-RED debug sidebar for log messages from the function nodes. This will help verify that messages are correctly filtered and formatted per slave.

- **Hardware Integration:**  
  If integrating with actual CAN hardware (e.g., via SocketCAN), verify that your CAN interface is correctly configured, and modify the flows to forward messages to a hardware node instead of debug nodes.

## Customization

- **Adjusting Topics & Payloads:**  
  Modify the function nodes if you need a different JSON structure or wish to include additional parameters.

- **Hardware Control:**  
  Replace debug nodes in the slave flows with hardware interface nodes (e.g., `socketcan-out` or `rpi-gpio out`) to control physical devices.

- **Deployment:**  
  You can deploy the global/control flow on one Node-RED instance and deploy each slave flow on separate devices (such as Raspberry Pis) as required.

## References

- [Node-RED Documentation](https://nodered.org/docs/)
- [Node-RED Dashboard GitHub](https://github.com/node-red/node-red-dashboard)
- [MQTT Documentation](https://mqtt.org/)

## Conclusion

This project provides a flexible method to issue global and slave-specific commands over a wireless network using Node-RED and MQTT. The flows can be easily adapted for integration with CAN bus systems or direct hardware control. For additional information or further customization, please refer to the Node-RED documentation or community forums.
