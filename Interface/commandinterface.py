import tkinter as tk
from tkinter import ttk
import paho.mqtt.client as mqtt
import json
import logging

# Configure logging.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MQTT_Interface")

class MQTTInterface:
    def __init__(self, master, mqtt_client, slave_ids):
        self.master = master
        self.mqtt_client = mqtt_client
        self.slave_ids = slave_ids
        
        master.title("MQTT Command Interface")
        
        # -------------------------
        # Global Commands Frame
        # -------------------------
        global_frame = ttk.LabelFrame(master, text="Global Commands")
        global_frame.grid(row=0, column=0, padx=10, pady=10, sticky="ew")
        
        # All Nozzles On/Off
        btn_all_nozzles_on = ttk.Button(global_frame, text="All Nozzles On", command=self.all_nozzles_on)
        btn_all_nozzles_on.grid(row=0, column=0, padx=5, pady=5)
        btn_all_nozzles_off = ttk.Button(global_frame, text="All Nozzles Off", command=self.all_nozzles_off)
        btn_all_nozzles_off.grid(row=0, column=1, padx=5, pady=5)
        
        # Recording On/Off
        btn_recording_on = ttk.Button(global_frame, text="Recording On", command=self.recording_on)
        btn_recording_on.grid(row=1, column=0, padx=5, pady=5)
        btn_recording_off = ttk.Button(global_frame, text="Recording Off", command=self.recording_off)
        btn_recording_off.grid(row=1, column=1, padx=5, pady=5)
        
        # Sensitivity Slider
        sensitivity_label = ttk.Label(global_frame, text="Sensitivity")
        sensitivity_label.grid(row=2, column=0, padx=5, pady=5)
        self.sensitivity_slider = ttk.Scale(global_frame, from_=1, to=10, orient="horizontal")
        self.sensitivity_slider.set(5)  # Default value
        self.sensitivity_slider.grid(row=2, column=1, padx=5, pady=5)
        btn_set_sensitivity = ttk.Button(global_frame, text="Set Sensitivity", command=self.set_sensitivity)
        btn_set_sensitivity.grid(row=2, column=2, padx=5, pady=5)
        
        # Files Slider
        files_label = ttk.Label(global_frame, text="Files")
        files_label.grid(row=3, column=0, padx=5, pady=5)
        self.files_slider = ttk.Scale(global_frame, from_=1, to=10, orient="horizontal")
        self.files_slider.set(5)  # Default value
        self.files_slider.grid(row=3, column=1, padx=5, pady=5)
        btn_set_files = ttk.Button(global_frame, text="Set Files", command=self.set_files)
        btn_set_files.grid(row=3, column=2, padx=5, pady=5)
        
        # -------------------------
        # Spot Spray Commands Frame
        # -------------------------
        spot_frame = ttk.LabelFrame(master, text="Spot Spray Commands")
        spot_frame.grid(row=1, column=0, padx=10, pady=10, sticky="ew")
        
        # Slave ID selection (Combobox)
        slave_label = ttk.Label(spot_frame, text="Slave ID:")
        slave_label.grid(row=0, column=0, padx=5, pady=5)
        self.slave_combo = ttk.Combobox(spot_frame, values=self.slave_ids, state="readonly")
        self.slave_combo.current(0)  # Default to the first slave.
        self.slave_combo.grid(row=0, column=1, padx=5, pady=5)
        
        # Spot Spray On/Off buttons
        btn_spot_on = ttk.Button(spot_frame, text="Spot Spray On", command=self.spot_spray_on)
        btn_spot_on.grid(row=1, column=0, padx=5, pady=5)
        btn_spot_off = ttk.Button(spot_frame, text="Spot Spray Off", command=self.spot_spray_off)
        btn_spot_off.grid(row=1, column=1, padx=5, pady=5)
        
    def publish_command(self, command, **kwargs):
        """Compose and publish a JSON command message to the MQTT topic."""
        message = {"command": command}
        message.update(kwargs)
        payload = json.dumps(message)
        self.mqtt_client.publish("commands/can", payload, qos=1, retain=False)
        logger.info("Published: %s", payload)
    
    # Global command methods.
    def all_nozzles_on(self):
        self.publish_command("all_nozzles", state="on")
    
    def all_nozzles_off(self):
        self.publish_command("all_nozzles", state="off")
    
    def recording_on(self):
        self.publish_command("recording", state="on")
    
    def recording_off(self):
        self.publish_command("recording", state="off")
    
    def set_sensitivity(self):
        value = self.sensitivity_slider.get()
        self.publish_command("sensitivity", value=value)
    
    def set_files(self):
        value = self.files_slider.get()
        self.publish_command("files", value=value)
    
    # Spot spray command methods.
    def spot_spray_on(self):
        slave = self.slave_combo.get()
        self.publish_command("spot_spray", slave=slave, state="on")
    
    def spot_spray_off(self):
        slave = self.slave_combo.get()
        self.publish_command("spot_spray", slave=slave, state="off")

def run_publisher():
    # MQTT broker settings (update if necessary).
    broker = "localhost"
    port = 1883
    
    # Create and set up the MQTT client.
    client = mqtt.Client("MQTT_Interface_Client")
    client.connect(broker, port, 60)
    client.loop_start()
    
    # Define the list of available slave IDs.
    slave_ids = ["0x201", "0x202", "0x203", "0x204"]
    
    # Set up the main Tkinter window.
    root = tk.Tk()
    app = MQTTInterface(root, client, slave_ids)
    
    # Run the GUI event loop.
    root.mainloop()
    
    # Clean up: stop the MQTT loop and disconnect.
    client.loop_stop()
    client.disconnect()

if __name__ == "__main__":
    run_publisher()
