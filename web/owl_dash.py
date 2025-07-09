#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OWL Dashboard Server
-------------------
Standalone web dashboard for the OpenWeedLocator (OWL) providing a WiFi-accessible interface
for monitoring and control. Designed to work with or without the main owl.py process.
"""

import os
import sys
import time
import threading
import logging
import subprocess
import configparser
from pathlib import Path
from datetime import datetime
from multiprocessing import Value, Queue

try:
    from flask import Flask, Response, render_template, request, jsonify, send_from_directory, send_file
    import psutil
    import cv2
    import numpy as np
except ImportError as e:
    print(f"Error: Required modules not installed. Error: {e}")
    sys.exit(1)


# Global shared state - will be initialized by owl.py or standalone
class SharedState:
    def __init__(self):
        self.detection_enable = Value('b', False)
        self.image_sample_enable = Value('b', False)
        self.sensitivity_state = Value('b', False)  # False = high sensitivity, True = low sensitivity
        self.frame_queue = Queue(maxsize=5)
        self.owl_running = Value('b', False)
        self.last_frame_time = Value('d', 0.0)

        # Config values - will be populated from config file
        self.config = None
        self.config_path = None


# Global shared state instance
shared_state = SharedState()


class OWLDashboard:
    def __init__(self, config_file='../config/DAY_SENSITIVITY_2.ini'):
        self.config_file = config_file
        self.load_config()
        self.setup_logging()

        # Create Flask app
        self.app = Flask(__name__,
                         static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'),
                         template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

        # Video frame handling
        self.latest_frame = None
        self.frame_lock = threading.Lock()

        # Start background tasks
        self.start_background_tasks()

        # Set up routes
        self.setup_routes()

        self.logger.info("OWL Dashboard initialized")

    def load_config(self):
        """Load configuration from config file"""
        config_path = Path(self.config_file)
        if not config_path.exists():
            # Try relative to script directory
            config_path = Path(__file__).parent / self.config_file

        self.config = configparser.ConfigParser()
        if config_path.exists():
            self.config.read(config_path)
            shared_state.config = self.config
            shared_state.config_path = config_path
            print(f"Config loaded from {config_path}")
        else:
            print(f"Warning: Config file not found at {config_path}")
            # Create minimal config sections
            self.config.add_section('GreenOnBrown')
            self.config.set('GreenOnBrown', 'exg_min', '25')
            self.config.set('GreenOnBrown', 'exg_max', '200')
            self.config.set('GreenOnBrown', 'hue_min', '39')
            self.config.set('GreenOnBrown', 'hue_max', '83')
            self.config.set('GreenOnBrown', 'saturation_min', '50')
            self.config.set('GreenOnBrown', 'saturation_max', '220')
            self.config.set('GreenOnBrown', 'brightness_min', '60')
            self.config.set('GreenOnBrown', 'brightness_max', '190')

            self.config.add_section('Dashboard')
            self.config.set('Dashboard', 'dashboard_enable', 'True')
            self.config.set('Dashboard', 'dashboard_port', '8000')
            self.config.set('Dashboard', 'gps_source', 'none')

    def setup_logging(self):
        """Setup logging"""
        log_dir = Path('../logs')
        log_dir.mkdir(exist_ok=True)

        log_file = log_dir / f"owl_dashboard_{datetime.now().strftime('%Y%m%d')}.log"
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger("OWL.Dashboard")

    def start_background_tasks(self):
        """Start background tasks"""
        # Frame processing thread
        threading.Thread(target=self.process_frames, daemon=True).start()

        # Status monitoring thread
        threading.Thread(target=self.monitor_status, daemon=True).start()

    def process_frames(self):
        """Process frames from shared queue"""
        while True:
            try:
                if not shared_state.frame_queue.empty():
                    frame = shared_state.frame_queue.get_nowait()
                    with self.frame_lock:
                        self.latest_frame = frame
                        shared_state.last_frame_time.value = time.time()
                time.sleep(0.033)  # ~30 FPS
            except Exception as e:
                self.logger.error(f"Error processing frames: {e}")
                time.sleep(0.1)

    def monitor_status(self):
        """Monitor system status"""
        while True:
            try:
                # Check if owl.py is running
                owl_running = False
                try:
                    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                        if proc.info['cmdline'] and any('owl.py' in cmd for cmd in proc.info['cmdline']):
                            owl_running = True
                            break
                except:
                    pass

                with shared_state.owl_running.get_lock():
                    shared_state.owl_running.value = owl_running

                time.sleep(2)
            except Exception as e:
                self.logger.error(f"Error monitoring status: {e}")
                time.sleep(5)

    def get_usb_storage_info(self):
        """Get USB storage device information"""
        try:
            usb_devices = []
            # Get mounted drives
            result = subprocess.run(['df', '-h'], capture_output=True, text=True)
            if result.returncode == 0:
                lines = result.stdout.split('\n')
                for line in lines[1:]:  # Skip header
                    if '/media/' in line or '/mnt/' in line:
                        parts = line.split()
                        if len(parts) >= 6:
                            device = parts[0]
                            size = parts[1]
                            used = parts[2]
                            available = parts[3]
                            mount_point = parts[5]
                            usb_devices.append({
                                'device': device,
                                'size': size,
                                'used': used,
                                'available': available,
                                'mount_point': mount_point
                            })

            return usb_devices
        except Exception as e:
            self.logger.error(f"Error getting USB storage info: {e}")
            return []

    def setup_routes(self):
        """Setup Flask routes"""

        @self.app.route('/')
        def index():
            return render_template('index.html')

        @self.app.route('/video_feed')
        def video_feed():
            return Response(self.generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

        @self.app.route('/api/system_stats')
        def system_stats():
            return jsonify(self.get_system_stats())

        @self.app.route('/api/detection/start', methods=['POST'])
        def start_detection():
            try:
                with shared_state.detection_enable.get_lock():
                    shared_state.detection_enable.value = True
                self.logger.info("Detection enabled via dashboard")
                return jsonify({'success': True, 'message': 'Detection enabled'})
            except Exception as e:
                self.logger.error(f"Error starting detection: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/detection/stop', methods=['POST'])
        def stop_detection():
            try:
                with shared_state.detection_enable.get_lock():
                    shared_state.detection_enable.value = False
                self.logger.info("Detection disabled via dashboard")
                return jsonify({'success': True, 'message': 'Detection disabled'})
            except Exception as e:
                self.logger.error(f"Error stopping detection: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/recording/start', methods=['POST'])
        def start_recording():
            try:
                with shared_state.image_sample_enable.get_lock():
                    shared_state.image_sample_enable.value = True
                self.logger.info("Recording enabled via dashboard")
                return jsonify({'success': True, 'message': 'Recording enabled'})
            except Exception as e:
                self.logger.error(f"Error starting recording: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/recording/stop', methods=['POST'])
        def stop_recording():
            try:
                with shared_state.image_sample_enable.get_lock():
                    shared_state.image_sample_enable.value = False
                self.logger.info("Recording disabled via dashboard")
                return jsonify({'success': True, 'message': 'Recording disabled'})
            except Exception as e:
                self.logger.error(f"Error stopping recording: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/sensitivity/toggle', methods=['POST'])
        def toggle_sensitivity():
            try:
                with shared_state.sensitivity_state.get_lock():
                    shared_state.sensitivity_state.value = not shared_state.sensitivity_state.value

                sensitivity_name = "Low" if shared_state.sensitivity_state.value else "High"
                self.logger.info(f"Sensitivity changed to {sensitivity_name} via dashboard")
                return jsonify({'success': True, 'message': f'Sensitivity set to {sensitivity_name}'})
            except Exception as e:
                self.logger.error(f"Error toggling sensitivity: {e}")
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/download_frame', methods=['POST'])
        def download_frame():
            try:
                with self.frame_lock:
                    if self.latest_frame is None:
                        return jsonify({'error': 'No frame available'}), 404
                    frame = self.latest_frame.copy()

                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                success, buffer = cv2.imencode('.jpg', frame)
                if not success:
                    return jsonify({'error': 'Failed to encode image'}), 500

                filename = f'owl_frame_{timestamp}.jpg'
                return Response(
                    buffer.tobytes(),
                    mimetype='image/jpeg',
                    headers={'Content-Disposition': f'attachment; filename={filename}'}
                )
            except Exception as e:
                self.logger.error(f"Error downloading frame: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/static/<path:filename>')
        def static_files(filename):
            return send_from_directory(self.app.static_folder, filename)

        @self.app.route('/api/usb_storage')
        def usb_storage():
            return jsonify(self.get_usb_storage_info())

        @self.app.route('/api/browse_files', methods=['POST'])
        def browse_files():
            try:
                data = request.get_json()
                directory = data.get('directory', '/media')

                files = []
                if os.path.exists(directory):
                    for item in os.listdir(directory):
                        item_path = os.path.join(directory, item)
                        if os.path.isfile(item_path):
                            stat = os.stat(item_path)
                            files.append({
                                'name': item,
                                'size': stat.st_size,
                                'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                                'path': item_path
                            })

                return jsonify({'files': files})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/download_file')
        def download_file():
            try:
                file_path = request.args.get('path')
                if not file_path or not os.path.exists(file_path):
                    return jsonify({'error': 'File not found'}), 404

                return send_file(file_path, as_attachment=True)
            except Exception as e:
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/download_logs')
        def download_logs():
            try:
                import zipfile
                import tempfile
                import os

                # Create a temporary zip file
                temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')

                with zipfile.ZipFile(temp_zip.name, 'w') as zip_file:
                    log_dir = Path('../logs')
                    if log_dir.exists():
                        for log_file in log_dir.glob('*.log*'):
                            if log_file.is_file():
                                zip_file.write(log_file, log_file.name)

                    # Also include any jsonl files (OWL log format)
                    for jsonl_file in log_dir.glob('*.jsonl'):
                        if jsonl_file.is_file():
                            zip_file.write(jsonl_file, jsonl_file.name)

                filename = f'owl_logs_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'

                return send_file(
                    temp_zip.name,
                    as_attachment=True,
                    download_name=filename,
                    mimetype='application/zip'
                )

            except Exception as e:
                self.logger.error(f"Error creating log archive: {e}")
                return jsonify({'error': str(e)}), 500

    def generate_frames(self):
        """Generate video frames for streaming"""
        while True:
            try:
                with self.frame_lock:
                    if self.latest_frame is not None:
                        frame = self.latest_frame.copy()
                    else:
                        frame = self.generate_placeholder_frame()

                # Resize frame for web streaming
                h, w = frame.shape[:2]
                if w > 800:
                    scale = 800 / w
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)))

                # Add overlay information
                frame = self.add_frame_overlay(frame)

                # Encode frame
                success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not success:
                    continue

                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

                time.sleep(1 / 20)  # 20 FPS
            except Exception as e:
                self.logger.error(f"Error generating frame: {e}")
                time.sleep(0.5)

    def generate_placeholder_frame(self):
        """Generate placeholder frame when OWL is not running"""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        # Add OWL logo area (placeholder)
        cv2.rectangle(frame, (270, 190), (370, 290), (60, 60, 60), -1)
        cv2.putText(frame, "OWL", (295, 245), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

        # Add status text
        cv2.putText(frame, "OWL Not Running", (220, 320), cv2.FONT_HERSHEY_SIMPLEX, 1, (100, 100, 255), 2)
        cv2.putText(frame, "Dashboard Active", (230, 360), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1)

        return frame

    def add_frame_overlay(self, frame):
        """Add overlay information to frame"""
        height, width = frame.shape[:2]

        # Timestamp
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cv2.putText(frame, timestamp, (10, height - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        # System status
        detection_status = "ON" if shared_state.detection_enable.value else "OFF"
        recording_status = "ON" if shared_state.image_sample_enable.value else "OFF"
        sensitivity_status = "LOW" if shared_state.sensitivity_state.value else "HIGH"

        cv2.putText(frame, f"Detection: {detection_status}", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0) if shared_state.detection_enable.value else (0, 0, 255), 1)
        cv2.putText(frame, f"Recording: {recording_status}", (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 0) if shared_state.image_sample_enable.value else (0, 0, 255), 1)
        cv2.putText(frame, f"Sensitivity: {sensitivity_status}", (10, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1)

        return frame

    def get_system_stats(self):
        """Get system statistics"""
        try:
            # CPU and memory stats
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()

            # CPU temperature
            cpu_temp = 0
            try:
                result = subprocess.run(['/usr/bin/vcgencmd', 'measure_temp'],
                                        capture_output=True, text=True)
                if result.returncode == 0:
                    cpu_temp = float(result.stdout.replace('temp=', '').replace("'C\n", ''))
            except:
                pass

            # USB devices
            usb_devices = []
            try:
                result = subprocess.run(['lsusb'], capture_output=True, text=True)
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if 'Camera' in line or 'Webcam' in line:
                            usb_devices.append(line.strip())
            except:
                pass

            # Disk usage
            disk = psutil.disk_usage('/')

            return {
                'cpu_percent': round(cpu_percent, 1),
                'cpu_temp': round(cpu_temp, 1),
                'memory_percent': round(memory.percent, 1),
                'memory_used': round(memory.used / (1024 ** 3), 1),
                'memory_total': round(memory.total / (1024 ** 3), 1),
                'disk_percent': round(disk.percent, 1),
                'disk_used': round(disk.used / (1024 ** 3), 1),
                'disk_total': round(disk.total / (1024 ** 3), 1),
                'detection_enable': bool(shared_state.detection_enable.value),
                'image_sample_enable': bool(shared_state.image_sample_enable.value),
                'sensitivity_state': bool(shared_state.sensitivity_state.value),
                'owl_running': bool(shared_state.owl_running.value),
                'usb_devices': usb_devices,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
        except Exception as e:
            self.logger.error(f"Error getting system stats: {e}")
            return {
                'cpu_percent': 0,
                'cpu_temp': 0,
                'memory_percent': 0,
                'memory_used': 0,
                'memory_total': 0,
                'disk_percent': 0,
                'disk_used': 0,
                'disk_total': 0,
                'detection_enable': bool(shared_state.detection_enable.value),
                'image_sample_enable': bool(shared_state.image_sample_enable.value),
                'sensitivity_state': bool(shared_state.sensitivity_state.value),
                'owl_running': bool(shared_state.owl_running.value),
                'usb_devices': [],
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }


def get_shared_state():
    """Get the shared state for external access (from owl.py)"""
    return shared_state


# Create the dashboard instance
dashboard = OWLDashboard()
app = dashboard.app

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='OWL Dashboard Server')
    parser.add_argument('--port', type=int, default=8000, help='Port to run the server on')
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    parser.add_argument('--config', type=str, default='../config/DAY_SENSITIVITY_2.ini', help='Config file path')

    args = parser.parse_args()

    app.run(host='0.0.0.0', port=args.port, debug=args.debug)