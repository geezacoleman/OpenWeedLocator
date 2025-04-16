#!/usr/bin/env python3
import sys
import cv2
import time
import signal
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import threading
from PIL import Image, ImageTk
import collections
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from utils.video_manager import VideoStream
from utils.algorithms import fft_blur

class OWLFocusGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("OWL Camera Focus Tool")
        self.root.geometry("1000x700")
        self.root.configure(bg='#f0f0f0')
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.is_running = True
        self.frame = None
        self.resolution = (640, 480)
        self.exp_compensation = -2

        # Focus tracking: Persist best focus value and corresponding frame indefinitely.
        self.focus_history = collections.deque(maxlen=250)
        self.focus_moving_avg = collections.deque(maxlen=10)
        self.best_focus = float('-inf')
        self.best_frame = None
        self.last_avg_focus = float('-inf')

        self.create_widgets()
        self.camera_thread = threading.Thread(target=self.camera_loop, daemon=True)
        self.camera_thread.start()
        self.update_gui()

    def create_widgets(self):
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Video Feed Section: Fixed size using a container frame.
        video_frame = ttk.LabelFrame(main_frame, text="Camera Feed")
        video_frame.pack(pady=(0,10))
        self.video_width = 640
        self.video_height = 480
        self.video_container = tk.Frame(video_frame, width=self.video_width, height=self.video_height)
        self.video_container.pack(padx=5, pady=5)
        self.video_container.pack_propagate(False)
        self.video_label = tk.Label(self.video_container)
        self.video_label.pack(fill=tk.BOTH, expand=True)

        # Middle Section: Create a grid with three equal columns.
        mid_frame = ttk.Frame(main_frame)
        mid_frame.pack(fill=tk.X, pady=(0,10))
        mid_frame.columnconfigure(0, weight=1)
        mid_frame.columnconfigure(1, weight=1)
        mid_frame.columnconfigure(2, weight=1)

        # Current focus value.
        current_frame = ttk.Frame(mid_frame)
        current_frame.grid(row=0, column=0, padx=10)
        ttk.Label(current_frame, text="Current:", font=("Arial", 12, "bold")).pack(side=tk.LEFT)
        self.focus_value_container = tk.Frame(current_frame, bg="#f0f0f0", padx=5, pady=2)
        self.focus_value_container.pack(side=tk.LEFT, padx=(5,0))
        # Use a tk.Label (not ttk) to allow background updates.
        self.focus_value_label = tk.Label(self.focus_value_container, text="0.0", font=("Arial", 16), bg="#f0f0f0")
        self.focus_value_label.pack()

        # Best focus value.
        best_frame = ttk.Frame(mid_frame)
        best_frame.grid(row=0, column=1, padx=10)
        ttk.Label(best_frame, text="Best:", font=("Arial", 12, "bold")).pack(side=tk.LEFT)
        self.best_focus_label = ttk.Label(best_frame, text="0.0", font=("Arial", 16))
        self.best_focus_label.pack(side=tk.LEFT, padx=(5,0))

        # Buttons: Reset Best and Display Best.
        button_frame = ttk.Frame(mid_frame)
        button_frame.grid(row=0, column=2, padx=10)
        self.reset_button = ttk.Button(button_frame, text="Reset Best", command=self.reset_best_focus)
        self.reset_button.pack(side=tk.LEFT, padx=5)
        self.display_button = ttk.Button(button_frame, text="Display Best", command=self.display_best)
        self.display_button.pack(side=tk.LEFT, padx=5)

        # Graph Section: Focus History.
        # Force the graph frame to have a minimum height.
        graph_frame = ttk.LabelFrame(main_frame, text="Focus History")
        graph_frame.pack(fill=tk.BOTH, expand=True)
        graph_frame.config(height=500)
        graph_frame.pack_propagate(True)
        self.fig = plt.Figure(figsize=(5, 2), dpi=100)
        # Set the figure background to match the window.
        self.fig.patch.set_facecolor('#f0f0f0')
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor('#f0f0f0')
        self.ax.set_xlabel('Frame')
        self.ax.set_ylabel('Focus')
        self.ax.grid(True)
        self.canvas = FigureCanvasTkAgg(self.fig, graph_frame)
        self.canvas.draw()
        # Set the canvas background.
        self.canvas.get_tk_widget().configure(background='#f0f0f0')
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Exit Button at the bottom.
        exit_frame = ttk.Frame(main_frame)
        exit_frame.pack(fill=tk.X, pady=(5,0))
        self.exit_button = ttk.Button(exit_frame, text="Exit", command=self.on_closing)
        self.exit_button.pack(side=tk.RIGHT)

    def reset_best_focus(self):
        self.best_focus = float('-inf')
        self.best_frame = None
        self.update_focus_display()

    def display_best(self):
        if self.best_frame is None:
            messagebox.showinfo("No Best Frame", "No best frame recorded yet.")
            return
        win = tk.Toplevel(self.root)
        win.title("Best Focus Frame")
        frame_rgb = cv2.cvtColor(self.best_frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)
        photo = ImageTk.PhotoImage(image=img)
        lbl = ttk.Label(win, image=photo)
        lbl.image = photo  # Keep a reference to avoid garbage collection.
        lbl.pack()

    def camera_loop(self):
        try:
            self.cap = VideoStream(resolution=self.resolution, exp_compensation=self.exp_compensation)
            self.cap.start()
            time.sleep(1)
            while self.is_running:
                frame = self.cap.read()
                if frame is None:
                    time.sleep(0.1)
                    continue
                grey = cv2.cvtColor(frame.copy(), cv2.COLOR_BGR2GRAY)
                focus_val = fft_blur(grey, size=60)
                self.focus_history.append(focus_val)
                self.focus_moving_avg.append(focus_val)
                current_avg_focus = np.mean(self.focus_moving_avg)
                if current_avg_focus > self.best_focus:
                    self.best_focus = current_avg_focus
                    self.best_frame = frame.copy()
                self.last_avg_focus = current_avg_focus
                self.frame = frame.copy()
                time.sleep(0.01)
        except Exception as e:
            print(f"Error in camera loop: {e}")
        finally:
            self.release_camera()

    def update_gui(self):
        if not self.is_running:
            return
        if self.frame is not None:
            frame_rgb = cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            # Resize frame to fixed video container dimensions.
            img = img.resize((self.video_width, self.video_height), Image.LANCZOS)
            self.photo = ImageTk.PhotoImage(image=img)
            self.video_label.configure(image=self.photo)
        self.update_focus_display()
        self.update_focus_graph()
        self.root.after(33, self.update_gui)

    def update_focus_display(self):
        if self.focus_moving_avg:
            current_avg_focus = np.mean(self.focus_moving_avg)
            self.focus_value_label.configure(text=f"{current_avg_focus:.1f}")
            self.best_focus_label.configure(text=f"{self.best_focus:.1f}")
            if current_avg_focus >= self.best_focus:
                new_bg = "#90EE90"  # Green when current equals/exceeds best.
            elif current_avg_focus < self.last_avg_focus:
                new_bg = "#FFB6C1"  # Red when focus is deteriorating.
            else:
                new_bg = "#f0f0f0"
            self.focus_value_container.configure(bg=new_bg)
            self.focus_value_label.configure(bg=new_bg)

    def update_focus_graph(self):
        if len(self.focus_history) > 1:
            self.ax.clear()
            x_data = list(range(len(self.focus_history)))
            y_data = list(self.focus_history)
            self.ax.plot(x_data, y_data, 'b-', linewidth=1, alpha=0.7)
            if len(self.focus_history) >= 10:
                window_size = 10
                y_avg = np.convolve(y_data, np.ones(window_size) / window_size, mode='valid')
                x_avg = list(range(window_size - 1, len(y_data)))
                self.ax.plot(x_avg, y_avg, 'r-', linewidth=2, label='10-frame Avg')
            if self.best_focus > float('-inf'):
                self.ax.axhline(y=self.best_focus, color='g', linestyle='--', linewidth=1, label='Best Focus')
            self.ax.set_xlabel('Frame')
            self.ax.set_ylabel('Focus')
            self.ax.grid(True, alpha=0.3)
            self.ax.legend(loc='upper right')
            if y_data:
                max_focus = max(y_data)
                min_focus = min(y_data)
                range_focus = max_focus - min_focus
                margin = max(range_focus * 0.1, 1)
                y_min = max(0, min_focus - margin)
                y_max = max_focus + margin
                self.ax.set_ylim(y_min, y_max)
            self.canvas.draw()

    def release_camera(self):
        if hasattr(self, 'cap') and self.cap is not None:
            try:
                self.cap.stop()
            except Exception:
                pass
            self.cap = None

    def on_closing(self):
        self.is_running = False
        self.root.after(100, self.release_camera)
        self.focus_history.clear()
        self.focus_moving_avg.clear()
        self.root.destroy()

def main():
    root = tk.Tk()
    app = OWLFocusGUI(root)
    def signal_handler(sig, frame):
        app.on_closing()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    root.mainloop()

if __name__ == "__main__":
    main()
