import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Any

from utils.mqtt_manager import MQTTManager


class MQTTGui:
    """Simple GUI to interact with an MQTT broker."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("MQTT GUI")

        self.host_var = tk.StringVar(value="localhost")
        self.port_var = tk.IntVar(value=1883)
        self.topic_var = tk.StringVar(value="owl/detections")

        self.manager: MQTTManager | None = None
        self._build_widgets()

    def _build_widgets(self) -> None:
        frame = ttk.Frame(self.root, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        ttk.Label(frame, text="Host:").grid(row=0, column=0, sticky="e")
        ttk.Entry(frame, textvariable=self.host_var, width=15).grid(row=0, column=1, sticky="w")
        ttk.Label(frame, text="Port:").grid(row=0, column=2, sticky="e")
        ttk.Entry(frame, textvariable=self.port_var, width=5).grid(row=0, column=3, sticky="w")
        ttk.Label(frame, text="Topic:").grid(row=1, column=0, sticky="e")
        ttk.Entry(frame, textvariable=self.topic_var, width=20).grid(row=1, column=1, columnspan=3, sticky="w")

        connect_btn = ttk.Button(frame, text="Connect", command=self.connect)
        connect_btn.grid(row=2, column=0, pady=5)
        ttk.Button(frame, text="Disconnect", command=self.disconnect).grid(row=2, column=1, pady=5)

        self.log = scrolledtext.ScrolledText(frame, width=60, height=15, state="disabled")
        self.log.grid(row=3, column=0, columnspan=4, pady=5)

        self.msg_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.msg_var, width=40).grid(row=4, column=0, columnspan=3, sticky="ew")
        ttk.Button(frame, text="Publish", command=self.publish).grid(row=4, column=3, padx=5, pady=5)

    def connect(self) -> None:
        host = self.host_var.get()
        port = self.port_var.get()
        topic = self.topic_var.get()
        self.manager = MQTTManager(host=host, port=port, topic=topic)
        try:
            self.manager.connect()
            self.manager.subscribe(topic, self.on_message)
            self.manager.start_loop()
            self._log(f"Connected to {host}:{port} ({topic})")
        except Exception as exc:  # pragma: no cover - network errors
            self._log(f"Connection failed: {exc}")
            self.manager = None

    def disconnect(self) -> None:
        if self.manager:
            self.manager.disconnect()
            self._log("Disconnected")
            self.manager = None

    def publish(self) -> None:
        if not self.manager:
            self._log("Not connected")
            return
        payload = {"text": self.msg_var.get()}
        self.manager.publish(payload)
        self.msg_var.set("")

    def on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        payload = message.payload.decode("utf-8")
        self._log(f"{message.topic}: {payload}")

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, text + "\n")
        self.log.configure(state="disabled")
        self.log.see(tk.END)


def main() -> None:
    root = tk.Tk()
    gui = MQTTGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
