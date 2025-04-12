import tkinter as tk
from tkinter import ttk
import paho.mqtt.client as mqtt
import json
import logging
import time
import serial
import threading
import argparse
import sys

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MQTT_Interface")

# MQTT setup
BROKER = "localhost"
PORT = 1883
TOPIC = "commands/can"

# Global MQTT client
mqtt_client = mqtt.Client("MQTT_Combined_Client")

def send_mqtt(command, **kwargs):
    """Send MQTT message."""
    msg = {"command": command}
    msg.update(kwargs)
    payload = json.dumps(msg)
    mqtt_client.publish(TOPIC, payload)
    logger.info("Published: %s", payload)

# --- Section Relay Serial Thread (USB) ---
def section_control_thread(serial_port='/dev/ttyUSB0', baudrate=38400):
    try:
        ser = serial.Serial(serial_port, baudrate, timeout=1)
    except Exception as e:
        logger.error(f"Could not open serial port: {e}")
        return

    while True:
        try:
            if ser.in_waiting >= 14:
                header = ser.read(2)
                if header == b'\x80\x81':
                    ser.read(1)
                    pgn = ord(ser.read(1))
                    length = ord(ser.read(1))

                    if pgn == 239:
                        payload = ser.read(length)
                        if len(payload) >= 9:
                            relay_lo = payload[6]
                            relay_hi = payload[7]

                            relay_states = []
                            for i in range(8):
                                relay_states.append((relay_lo >> i) & 1)
                            for i in range(8):
                                relay_states.append((relay_hi >> i) & 1)

                            send_mqtt("relay_states", states=relay_states)

            time.sleep(0.2)
        except Exception as e:
            logger.error(f"Error in serial loop: {e}")
            break

# --- GUI Class ---
class MQTTInterface:
    def __init__(self, master, mqtt_client, slave_ids):
        self.master = master
        self.mqtt_client = mqtt_client
        self.slave_ids = slave_ids
        
        master.title("MQTT Command Interface")

        global_frame = ttk.LabelFrame(master, text="Global Commands")
        global_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        
        ttk.Button(global_frame, text="All Nozzles On", command=self.all_nozzles_on).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(global_frame, text="All Nozzles Off", command=self.all_nozzles_off).grid(row=0, column=1, padx=5, pady=5)
        
        ttk.Button(global_frame, text="Recording On", command=self.recording_on).grid(row=1, column=0, padx=5, pady=5)
        ttk.Button(global_frame, text="Recording Off", command=self.recording_off).grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(global_frame, text="Sensitivity").grid(row=2, column=0, padx=5, pady=5)
        self.sensitivity_slider = ttk.Scale(global_frame, from_=1, to=10, orient="horizontal")
        self.sensitivity_slider.set(5)
        self.sensitivity_slider.grid(row=2, column=1, padx=5, pady=5)
        ttk.Button(global_frame, text="Set Sensitivity", command=self.set_sensitivity).grid(row=2, column=2, padx=5, pady=5)

        ttk.Label(global_frame, text="Files").grid(row=3, column=0, padx=5, pady=5)
        self.files_slider = ttk.Scale(global_frame, from_=1, to=10, orient="horizontal")
        self.files_slider.set(5)
        self.files_slider.grid(row=3, column=1, padx=5, pady=5)
        ttk.Button(global_frame, text="Set Files", command=self.set_files).grid(row=3, column=2, padx=5, pady=5)
        
        spot_frame = ttk.LabelFrame(master, text="Spot Spray Commands")
        spot_frame.grid(row=1, column=0, padx=10, pady=10, sticky="ew")

        ttk.Label(spot_frame, text="Slave ID:").grid(row=0, column=0, padx=5, pady=5)
        self.slave_combo = ttk.Combobox(spot_frame, values=self.slave_ids, state="readonly")
        self.slave_combo.current(0)
        self.slave_combo.grid(row=0, column=1, padx=5, pady=5)

        ttk.Button(spot_frame, text="Spot Spray On", command=self.spot_spray_on).grid(row=1, column=0, padx=5, pady=5)
        ttk.Button(spot_frame, text="Spot Spray Off", command=self.spot_spray_off).grid(row=1, column=1, padx=5, pady=5)

    def publish_command(self, command, **kwargs):
        msg = {"command": command}
        msg.update(kwargs)
        payload = json.dumps(msg)
        self.mqtt_client.publish("commands/can", payload, qos=1, retain=False)
        logger.info("Published: %s", payload)

    def all_nozzles_on(self): self.publish_command("all_nozzles", state="on")
    def all_nozzles_off(self): self.publish_command("all_nozzles", state="off")
    def recording_on(self): self.publish_command("recording", state="on")
    def recording_off(self): self.publish_command("recording", state="off")
    def set_sensitivity(self): self.publish_command("sensitivity", value=self.sensitivity_slider.get())
    def set_files(self): self.publish_command("files", value=self.files_slider.get())
    def spot_spray_on(self): self.publish_command("spot_spray", slave=self.slave_combo.get(), state="on")
    def spot_spray_off(self): self.publish_command("spot_spray", slave=self.slave_combo.get(), state="off")

# --- Main ---
def main():
    parser = argparse.ArgumentParser(description="MQTT Interface for AgOpenGPS and GUI control")
    parser.add_argument("--mode", choices=["gui", "serial"], default="gui", help="Select mode: gui or serial")
    parser.add_argument("--port", default="/dev/ttyUSB0", help="Serial port for AgOpenGPS")
    args = parser.parse_args()

    mqtt_client.connect(BROKER, PORT, 60)
    mqtt_client.loop_start()

    if args.mode == "gui":
        root = tk.Tk()
        app = MQTTInterface(root, mqtt_client, ["0x201", "0x202", "0x203", "0x204"])
        root.mainloop()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    elif args.mode == "serial":
        thread = threading.Thread(target=section_control_thread, args=(args.port,), daemon=True)
        thread.start()
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            print("Exiting...")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

if __name__ == "__main__":
    main()
