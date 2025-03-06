#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OWL Web Server
--------------
Web server for the OpenWeedLocator (OWL) providing a WiFi-accessible interface
for monitoring and control, integrated with owl.py.
"""

import os
import sys
import time
import logging
import queue
from pathlib import Path
from datetime import datetime
from multiprocessing import Value

try:
    from flask import Flask, Response, render_template, request, jsonify, send_from_directory
except ImportError as e:
    print(f"Error: Flask not installed. Please run 'pip install flask'")
    sys.exit(1)

try:
    import cv2
    import numpy as np
except ImportError as e:
    print(f"Error: OpenCV or NumPy not installed. Please run 'pip install opencv-python numpy'")
    sys.exit(1)


class OWLWebServer:
    def __init__(self, port=5000, owl_home="~/owl", detection_enable=None, recording_enable=None):
        """Initialize the OWL Web Server.

        Args:
            port: The port to run the web server on (default: 5000)
            owl_home: Base directory for OWL (default: ~/owl)
            detection_enable: Shared Value for detection state (from owl.py)
            recording_enable: Shared Value for recording state (from owl.py)
        """
        self.port = port
        OWL_HOME = os.path.expanduser(owl_home)
        self.logs_path = os.path.join(OWL_HOME, "logs")

        # Shared state from owl.py
        self.detection_enable = detection_enable or Value('b', True)  # Default true if standalone
        self.recording_enable = recording_enable or Value('b', False)  # Default false if standalone

        # Initialize logging
        self._setup_logging()

        # Create Flask app
        self.app = Flask(__name__,
                         static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'),
                         template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

        # Video frame handling
        self.frame_queue = queue.Queue(maxsize=5)
        self.latest_frame = None
        self.recording = False
        self.frame_buffer = []
        self.target_fps = 15
        self.last_frame_push_time = 0

        # GPS data
        self.gps_data = None

        # System monitoring
        self.last_cpu_check = 0
        self.cached_cpu_usage = 0
        self.last_temp_check = 0
        self.cached_temp = 0
        self.CPU_CHECK_INTERVAL = 2
        self.TEMP_CHECK_INTERVAL = 5

        # Set up routes
        self._setup_routes()

        # Create logs directory
        os.makedirs(self.logs_path, exist_ok=True)

    def _setup_logging(self):
        log_file = os.path.join(self.logs_path, f"owl_web_{datetime.now().strftime('%Y%m%d')}.log")
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger("OWL.WebServer")
        self.logger.info("OWL Web Server starting up")

    def _setup_routes(self):
        @self.app.route('/')
        def index():
            return render_template('index.html')

        @self.app.route('/video_feed')
        def video_feed():
            return Response(self._generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

        @self.app.route('/api/system_stats')
        def system_stats():
            stats = self._get_system_stats()
            stats['detection_enable'] = bool(self.detection_enable.value)
            stats['recording_enable'] = bool(self.recording_enable.value)
            return jsonify(stats)

        @self.app.route('/api/update_gps', methods=['POST'])
        def update_gps():
            try:
                data = request.json
                self.gps_data = {
                    'latitude': data['latitude'],
                    'longitude': data['longitude'],
                    'accuracy': data['accuracy'],
                    'timestamp': data['timestamp']
                }
                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Failed to update GPS data: {e}")
                return jsonify({'success': False, 'message': str(e)})

        @self.app.route('/api/detection/start', methods=['POST'])
        def start_detection():
            with self.detection_enable.get_lock():
                self.detection_enable.value = True
            self.logger.info("Detection enabled via web interface")
            return jsonify({'success': True, 'message': 'Detection enabled'})

        @self.app.route('/api/detection/stop', methods=['POST'])
        def stop_detection():
            with self.detection_enable.get_lock():
                self.detection_enable.value = False
            self.logger.info("Detection disabled via web interface")
            return jsonify({'success': True, 'message': 'Detection disabled'})

        @self.app.route('/api/recording/start', methods=['POST'])
        def start_recording():
            with self.recording_enable.get_lock():
                self.recording_enable.value = True
            self.frame_buffer = []
            self.recording = True
            self.logger.info("Recording started via web interface")
            return jsonify({'success': True, 'message': 'Recording started'})

        @self.app.route('/api/recording/stop', methods=['POST'])
        def stop_recording():
            try:
                with self.recording_enable.get_lock():
                    self.recording_enable.value = False
                if self.recording and self.frame_buffer:
                    self.recording = False
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    temp_file = os.path.join(self.logs_path, f"temp_video_{timestamp}.mp4")
                    height, width = self.frame_buffer[0].shape[:2]
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    out = cv2.VideoWriter(temp_file, fourcc, 30.0, (width, height))
                    for frame in self.frame_buffer:
                        out.write(frame)
                    out.release()
                    with open(temp_file, 'rb') as f:
                        video_data = f.read()
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                    self.frame_buffer = []
                    self.logger.info("Recording stopped and saved via web interface")
                    return Response(
                        video_data,
                        mimetype='video/mp4',
                        headers={'Content-Disposition': f'attachment; filename=owl_recording_{timestamp}.mp4'}
                    )
                return jsonify({'error': 'No recording in progress'}), 400
            except Exception as e:
                self.logger.error(f"Failed to stop recording: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/download_frame', methods=['POST'])
        def download_frame():
            try:
                frame = self._get_current_frame()
                if frame is None:
                    return jsonify({'error': 'No frame available'}), 404
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                success, buffer = cv2.imencode('.jpg', frame)
                if not success:
                    return jsonify({'error': 'Failed to encode image'}), 500
                filename = f'owl_frame_{timestamp}'
                if self.gps_data:
                    filename += f"_lat{self.gps_data['latitude']:.6f}_lon{self.gps_data['longitude']:.6f}"
                filename += '.jpg'
                return Response(
                    buffer.tobytes(),
                    mimetype='image/jpeg',
                    headers={'Content-Disposition': f'attachment; filename={filename}'}
                )
            except Exception as e:
                self.logger.error(f"Failed to download frame: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/static/<path:filename>')
        def static_files(filename):
            return send_from_directory(self.app.static_folder, filename)

    def _generate_frames(self):
        while True:
            try:
                frame = None
                try:
                    if not self.frame_queue.empty():
                        frame = self.frame_queue.get_nowait()
                        self.latest_frame = frame
                    elif self.latest_frame is not None:
                        frame = self.latest_frame.copy()
                except queue.Empty:
                    pass

                if frame is None:
                    frame = self._generate_placeholder_frame()

                h, w = frame.shape[:2]
                if w > 800:
                    scale = 800 / w
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
                frame = self._add_frame_info(frame)
                success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if not success:
                    continue
                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

                time.sleep(1 / 20)
            except Exception as e:
                self.logger.error(f"Error generating frame: {e}")
                time.sleep(0.5)

    def _generate_placeholder_frame(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        cv2.putText(frame, "OWL Camera Offline", (120, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        return frame

    def _add_frame_info(self, frame):
        height, width = frame.shape[:2]
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cv2.putText(frame, timestamp, (10, height - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        if self.gps_data:
            gps_text = f"GPS: {self.gps_data['latitude']:.6f}, {self.gps_data['longitude']:.6f}"
            cv2.putText(frame, gps_text, (10, height - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return frame

    def _get_current_frame(self):
        try:
            try:
                if not self.frame_queue.empty():
                    return self.frame_queue.get_nowait()
                elif self.latest_frame is not None:
                    return self.latest_frame.copy()
            except queue.Empty:
                pass
        except Exception as e:
            self.logger.error(f"Error getting frame: {e}")
        return None

    def update_frame(self, frame):
        if frame is None or not isinstance(frame, np.ndarray):
            return
        current_time = time.time()
        if current_time - self.last_frame_push_time < (1.0 / self.target_fps):
            return
        self.last_frame_push_time = current_time
        try:
            if self.recording:
                self.frame_buffer.append(frame.copy())
            while not self.frame_queue.empty():
                try:
                    self.frame_queue.get_nowait()
                except queue.Empty:
                    break
            self.frame_queue.put(frame)
            self.latest_frame = frame
        except Exception as e:
            self.logger.error(f"Failed to update frame: {e}")

    def _get_system_stats(self):
        try:
            import psutil
            cpu_percent = psutil.cpu_percent(interval=None)
            cpu_temp = 0
            if os.path.exists('/sys/class/thermal/thermal_zone0/temp'):
                with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
                    cpu_temp = float(f.read().strip()) / 1000
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            return {
                'cpu_percent': round(cpu_percent, 1),
                'cpu_temp': round(cpu_temp, 1),
                'memory_percent': memory.percent,
                'memory_used': round(memory.used / (1024 ** 3), 1),
                'memory_total': round(memory.total / (1024 ** 3), 1),
                'disk_percent': disk.percent,
                'disk_used': round(disk.used / (1024 ** 3), 1),
                'disk_total': round(disk.total / (1024 ** 3), 1),
                'detection_enable': bool(self.detection_enable.value),
                'recording_enable': bool(self.recording_enable.value),
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
                'detection_enable': bool(self.detection_enable.value),
                'recording_enable': bool(self.recording_enable.value),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

    def run(self, debug=False):
        try:
            self.logger.info(f"Starting OWL Web Server on port {self.port}")
            self.app.run(host='0.0.0.0', port=self.port, debug=debug, threaded=True)
        except Exception as e:
            self.logger.error(f"Failed to start web server: {e}")
            raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='OWL Web Server')
    parser.add_argument('--port', type=int, default=5000, help='Port to run the web server on')
    parser.add_argument('--owl-home', type=str, default='~/owl', help='Base directory for OWL')
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    args = parser.parse_args()

    server = OWLWebServer(port=args.port, owl_home=args.owl_home)
    server.run(debug=args.debug)