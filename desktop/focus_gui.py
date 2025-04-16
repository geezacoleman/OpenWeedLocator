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

        # Focus tracking
        self.focus_history = collections.deque(maxlen=250)  # Store up to 1000 focus values
        self.focus_moving_avg = collections.deque(maxlen=10)  # For 10-frame moving average
        self.best_focus = float('inf')  # Lower values are better for FFT blur detection
        self.focus_direction = 0  # 0: no change, 1: improving, -1: worsening
        self.last_avg_focus = float('inf')

        # Create layout
        self.create_widgets()

        # Start camera in a separate thread
        self.camera_thread = threading.Thread(target=self.camera_loop)
        self.camera_thread.daemon = True
        self.camera_thread.start()

        self.update_gui()

    def create_widgets(self):
        self.paned_window = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.camera_frame = ttk.Frame(self.paned_window)
        self.paned_window.add(self.camera_frame, weight=3)

        # Title and instructions
        title_label = ttk.Label(
            self.camera_frame,
            text="OWL Camera Focus Tool",
            font=("Arial", 16, "bold")
        )
        title_label.pack(pady=(0, 5))

        instructions_text = (
            "Adjust the camera lens for the HIGHEST focus value."
        )
        instructions_label = ttk.Label(
            self.camera_frame,
            text=instructions_text,
            font=("Arial", 10)
        )
        instructions_label.pack(pady=(0, 10))

        # Camera view
        self.video_frame = ttk.LabelFrame(self.camera_frame, text="Camera Feed")
        self.video_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.video_label = ttk.Label(self.video_frame)
        self.video_label.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Focus information frame (right pane)
        self.focus_frame = ttk.Frame(self.paned_window)
        self.paned_window.add(self.focus_frame, weight=2)

        # Current focus value display
        current_focus_frame = ttk.LabelFrame(self.focus_frame, text="Focus Value")
        current_focus_frame.pack(fill=tk.X, padx=5, pady=5)

        # Focus explanation
        focus_expl_frame = ttk.Frame(current_focus_frame)
        focus_expl_frame.pack(fill=tk.X, padx=5, pady=5)

        focus_value_frame = ttk.Frame(current_focus_frame)
        focus_value_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(
            focus_value_frame,
            text="Current:",
            font=("Arial", 12, "bold")
        ).pack(side=tk.LEFT)

        self.focus_value_label = ttk.Label(
            focus_value_frame,
            text="0.0",
            font=("Arial", 16)
        )
        self.focus_value_label.pack(side=tk.LEFT, padx=(5, 0))

        # Trend indicator
        self.trend_label = ttk.Label(
            focus_value_frame,
            text="",
            font=("Arial", 16, "bold")
        )
        self.trend_label.pack(side=tk.LEFT, padx=(10, 0))

        best_focus_frame = ttk.Frame(current_focus_frame)
        best_focus_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(
            best_focus_frame,
            text="Best:",
            font=("Arial", 12, "bold")
        ).pack(side=tk.LEFT)

        self.best_focus_label = ttk.Label(
            best_focus_frame,
            text="0.0",
            font=("Arial", 16)
        )
        self.best_focus_label.pack(side=tk.LEFT, padx=(5, 0))

        # Reset button
        self.reset_button = ttk.Button(
            best_focus_frame,
            text="Reset Best",
            command=self.reset_best_focus
        )
        self.reset_button.pack(side=tk.RIGHT)

        # Focus history graph
        graph_frame = ttk.LabelFrame(self.focus_frame, text="Focus History")
        graph_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.fig = plt.Figure(figsize=(5, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel('Frame')
        self.ax.set_ylabel('Focus Value (lower is better)')
        self.ax.set_title('Focus History')
        self.ax.grid(True)

        self.canvas = FigureCanvasTkAgg(self.fig, graph_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Exit button at the bottom
        button_frame = ttk.Frame(self.root)
        button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        self.exit_button = ttk.Button(
            button_frame,
            text="Exit",
            command=self.on_closing
        )
        self.exit_button.pack(side=tk.RIGHT)

    def reset_best_focus(self):
        """Reset the best focus value"""
        self.best_focus = float('inf')
        self.update_focus_display()

    def camera_loop(self):
        """Camera capture loop that runs in a separate thread"""
        try:
            self.cap = VideoStream(resolution=self.resolution,
                                   exp_compensation=self.exp_compensation)
            self.cap.start()
            time.sleep(1)  # Allow camera time to initialize

            while self.is_running:
                frame = self.cap.read()

                if frame is None:
                    time.sleep(0.1)
                    continue

                grey = cv2.cvtColor(frame.copy(), cv2.COLOR_BGR2GRAY)
                focus_val = fft_blur(grey, size=30)

                self.focus_history.append(focus_val)
                self.focus_moving_avg.append(focus_val)

                current_avg_focus = np.mean(self.focus_moving_avg)

                if len(self.focus_moving_avg) >= 5:  # Need at least 5 frames to determine trend
                    if current_avg_focus > self.last_avg_focus - 1:
                        self.focus_direction = 1
                    elif current_avg_focus < self.last_avg_focus + 1:
                        self.focus_direction = -1
                    else:
                        self.focus_direction = 0

                self.last_avg_focus = current_avg_focus

                # Update best focus
                if current_avg_focus < self.best_focus:
                    self.best_focus = current_avg_focus

                # Store frame for display
                self.frame = frame.copy()

                # Small delay to reduce CPU usage
                time.sleep(0.01)

        except Exception as e:
            print(f"Error in camera loop: {e}")
            import traceback
            traceback.print_exc()

        finally:
            self.release_camera()

    def update_gui(self):
        """Update GUI elements"""
        if not self.is_running:
            return

        if self.frame is not None:
            frame_rgb = cv2.cvtColor(self.frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb)
            win_width = self.video_frame.winfo_width() - 20
            win_height = self.video_frame.winfo_height() - 20

            if win_width > 10 and win_height > 10:
                img_ratio = self.frame.shape[1] / self.frame.shape[0]
                win_ratio = win_width / win_height

                if win_ratio > img_ratio:
                    new_height = win_height
                    new_width = int(new_height * img_ratio)
                else:
                    new_width = win_width
                    new_height = int(new_width / img_ratio)

                img = img.resize((new_width, new_height), Image.LANCZOS)

            self.photo = ImageTk.PhotoImage(image=img)
            self.video_label.configure(image=self.photo)

        self.update_focus_display()
        self.update_focus_graph()
        self.root.after(33, self.update_gui)

    def update_focus_display(self):
        """Update the focus value displays and trend indicator"""
        if self.focus_moving_avg:
            current_avg_focus = np.mean(self.focus_moving_avg)
            self.focus_value_label.configure(text=f"{current_avg_focus:.1f}")
            self.best_focus_label.configure(text=f"{self.best_focus:.1f}")
            ratio = current_avg_focus / max(self.best_focus, 1)

            if ratio <= 1.05:  # Within 5% of best focus
                self.focus_value_label.configure(foreground="green")
            elif ratio <= 1.2:  # Within 20% of best focus
                self.focus_value_label.configure(foreground="orange")
            else:
                self.focus_value_label.configure(foreground="red")

    def update_focus_graph(self):
        """Update the focus history graph"""
        if len(self.focus_history) > 1:
            self.ax.clear()

            # Plot the focus history
            x_data = list(range(len(self.focus_history)))
            y_data = list(self.focus_history)

            # Plot with a line
            self.ax.plot(x_data, y_data, 'b-', linewidth=1, alpha=0.7)

            # Plot moving average
            if len(self.focus_history) >= 10:
                # Calculate moving average
                avg_window = 10
                y_avg = []
                for i in range(len(y_data) - avg_window + 1):
                    avg = sum(y_data[i:i + avg_window]) / avg_window
                    y_avg.append(avg)

                # Plot moving average
                x_avg = list(range(avg_window - 1, len(y_data)))
                self.ax.plot(x_avg, y_avg, 'r-', linewidth=2, label='10-frame Avg')

            if self.best_focus < float('inf'):
                self.ax.axhline(y=self.best_focus, color='g', linestyle='--',
                                linewidth=1, label='Best Focus')

            self.ax.set_xlabel('Frame')
            self.ax.set_ylabel('Focus Value (lower is better)')
            self.ax.set_title('Focus History')
            self.ax.grid(True, alpha=0.3)
            self.ax.legend(loc='upper right')

            if y_data:
                max_focus = max(y_data)
                min_focus = min(y_data)
                range_focus = max_focus - min_focus

                # Add margins
                margin = max(range_focus * 0.1, 1)
                y_min = max(0, min_focus - margin)
                y_max = max_focus + margin

                self.ax.set_ylim(y_min, y_max)

            # Update canvas
            self.canvas.draw()

    def release_camera(self):
        """Release camera resources"""
        self.cap.stop()
        self.cap = None

    def show_error(self, message):
        """Show error message in GUI thread"""
        self.root.after(0, lambda: messagebox.showerror("Error", message))

    def on_closing(self):
        """Clean up resources when window is closed"""
        self.is_running = False
        self.root.after(100, self.release_camera)
        self.root.destroy()

def main():
    root = tk.Tk()
    app = OWLFocusGUI(root)

    def signal_handler(sig, frame):
        app.on_closing()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start the Tkinter event loop
    root.mainloop()


if __name__ == "__main__":
    main()