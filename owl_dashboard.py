import logging
import queue
import psutil
import time
import tempfile
import os
import piexif
import json
from PIL import Image
from io import BytesIO
from datetime import datetime
from utils.output_manager import get_platform_config
import utils.error_manager as errors

# Try importing required packages
try:
    from flask import Flask, Response, render_template, jsonify, request, send_from_directory
except ImportError as e:
    raise errors.DependencyError('flask', str(e))

try:
    import cv2
except ImportError as e:
    raise errors.OpenCVError(str(e))

try:
    import numpy as np
except ImportError as e:
    raise errors.DependencyError('numpy', str(e))

testing, lgpioERROR = get_platform_config()

# Import GPIO components only if needed
if not testing:
    from gpiozero import CPUTemperature


class OWLDashboard:
    def __init__(self, port: int = 5000, target_fps=30):
        # Initialize Flask with template and static folders
        self.app = Flask(__name__,
                         template_folder='templates',
                         static_folder='static')

        self.port = port
        self.frame_queue = queue.Queue(maxsize=1)
        self.latest_frame = None
        self.last_frame_time = None
        self.recording = False
        self.frame_buffer = []
        self.target_fps = target_fps
        self.last_frame_push_time = 0

        self.gps_data = None

        # System monitoring
        self.last_cpu_check = 0
        self.cached_cpu_usage = 0
        self.last_temp_check = 0
        self.cached_temp = 0
        self.last_connection_check = time.time()
        self.connection_retry_count = 0
        self.MAX_RETRIES = 3
        self.connection_status = "Connected"
        self.CPU_CHECK_INTERVAL = 2
        self.TEMP_CHECK_INTERVAL = 5
        self.MAX_RECORDING_TIME = 30  # seconds
        self.recording_start_time = None

        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger("OWL.Dashboard")

        try:
            self._setup_routes()
        except Exception as e:
            raise errors.StreamInitError(f"Failed to setup routes: {str(e)}")

        self.logger.info("Dashboard initialized successfully")

    def _add_gps_to_image(self, image_data: bytes) -> bytes:
        """Add GPS data to image EXIF if available."""
        if not self.gps_data:
            return image_data

        try:
            # Convert lat/lon to EXIF format
            lat = float(self.gps_data['latitude'])
            lon = float(self.gps_data['longitude'])

            lat_deg = int(lat)
            lat_min = int((lat - lat_deg) * 60)
            lat_sec = int((((lat - lat_deg) * 60) - lat_min) * 60 * 100)

            lon_deg = int(lon)
            lon_min = int((lon - lon_deg) * 60)
            lon_sec = int((((lon - lon_deg) * 60) - lon_min) * 60 * 100)

            exif_dict = {
                "GPS": {
                    piexif.GPSIFD.GPSLatitudeRef: 'N' if lat >= 0 else 'S',
                    piexif.GPSIFD.GPSLatitude: [(lat_deg, 1), (lat_min, 1), (lat_sec, 100)],
                    piexif.GPSIFD.GPSLongitudeRef: 'E' if lon >= 0 else 'W',
                    piexif.GPSIFD.GPSLongitude: [(lon_deg, 1), (lon_min, 1), (lon_sec, 100)],
                    piexif.GPSIFD.GPSAltitude: (0, 1),
                    piexif.GPSIFD.GPSTimeStamp: [(0, 1), (0, 1), (0, 1)],
                }
            }

            im = Image.open(BytesIO(image_data))
            exif_bytes = piexif.dump(exif_dict)
            img_byte_arr = BytesIO()

            im.save(img_byte_arr, format='JPEG', exif=exif_bytes)
            return img_byte_arr.getvalue()

        except Exception as e:
            self.logger.error(f"Failed to add GPS data to image: {e}")
            return image_data

    def _setup_routes(self):
        @self.app.route('/')
        def index():
            """Render the main dashboard page"""
            return render_template('base.html')

        @self.app.route('/video_feed')
        def video_feed():
            """Stream video feed"""
            return Response(
                self._generate_frames(),
                mimetype='multipart/x-mixed-replace; boundary=frame'
            )

        @self.app.route('/static/<path:filename>')
        def serve_static(filename):
            """Serve static files (CSS, JS, images)"""
            return send_from_directory(self.app.static_folder, filename)

        @self.app.route('/update_gps', methods=['POST'])
        def update_gps():
            try:
                data = request.json
                self.gps_data = {
                    'latitude': data['latitude'],
                    'longitude': data['longitude'],
                    'accuracy': data['accuracy'],
                    'timestamp': data['timestamp']
                }
                print(self.gps_data)
                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Failed to update GPS data: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/download_frame', methods=['POST'])
        def download_frame():
            try:
                try:
                    frame = self.frame_queue.get_nowait()
                except queue.Empty:
                    if self.latest_frame is not None:
                        frame = self.latest_frame
                    else:
                        return jsonify({'error': 'No frame available'}), 404

                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                success, buffer = cv2.imencode('.jpg', frame)
                if not success:
                    return jsonify({'error': 'Failed to encode image'}), 500

                # Add GPS data to image if available
                image_data = self._add_gps_to_image(buffer.tobytes())

                # Create filename with GPS data if available
                filename = f'owl_frame_{timestamp}'
                if self.gps_data:
                    filename += f"_lat{self.gps_data['latitude']:.6f}_lon{self.gps_data['longitude']:.6f}"
                filename += '.jpg'

                return Response(
                    image_data,
                    mimetype='image/jpeg',
                    headers={
                        'Content-Disposition': f'attachment; filename={filename}'
                    }
                )
            except Exception as e:
                self.logger.error(f"Failed to download frame: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/start_recording', methods=['POST'])
        def start_recording():
            """Start video recording"""
            self.recording_start_time = time.time()
            try:
                self.frame_buffer = []
                self.recording = True
                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Failed to start recording: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/stop_recording', methods=['POST'])
        def stop_recording():
            """Stop and save video recording"""
            try:
                if self.recording and self.frame_buffer:
                    self.recording = False
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                    height, width = self.frame_buffer[0].shape[:2]

                    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
                        out = cv2.VideoWriter(temp_file.name, fourcc, 30.0, (width, height))
                        CHUNK_SIZE = 50
                        for i in range(0, len(self.frame_buffer), CHUNK_SIZE):
                            chunk = self.frame_buffer[i:i + CHUNK_SIZE]
                            for frame in chunk:
                                out.write(frame)
                        out.release()

                        with open(temp_file.name, 'rb') as f:
                            video_data = f.read()

                    os.unlink(temp_file.name)
                    self.frame_buffer = []

                    return Response(
                        video_data,
                        mimetype='video/mp4',
                        headers={
                            'Content-Disposition': f'attachment; filename=owl_recording_{timestamp}.mp4'
                        }
                    )

                return jsonify({'error': 'No recording in progress'}), 400
            except Exception as e:
                self.logger.error(f"Failed to stop recording: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/system_stats')
        def system_stats():
            """Get system statistics"""
            try:
                stats = self.get_system_stats()
                return jsonify(stats)
            except Exception as e:
                self.logger.error(f"Error getting system stats: {str(e)}")
                return jsonify({
                    'cpu_percent': 0,
                    'cpu_temp': 0,
                    'status': 'Error',
                    'retry_count': 0,
                    'max_retries': self.MAX_RETRIES,
                    'timestamp': datetime.now().strftime('%H:%M:%S')
                }), 500

    def update_frame(self, frame) -> None:
        """Update the current frame with error handling."""
        current_time = time.time()
        if current_time - self.last_frame_push_time < (1.0 / self.target_fps):
            return

        self.last_frame_push_time = current_time

        try:
            if not isinstance(frame, np.ndarray):
                raise errors.StreamUpdateError(reason="Invalid frame format - must be numpy array")

            if self.recording:
                self.frame_buffer.append(frame)

            # Clear old frames from display queue
            while not self.frame_queue.empty():
                self.frame_queue.get_nowait()

            self.frame_queue.put(frame)
            self.last_frame_time = datetime.now()
            self.latest_frame = frame

        except queue.Full:
            self.logger.warning("Frame queue full - skipping frame")
        except Exception as e:
            raise errors.StreamUpdateError(
                frame_size=frame.shape if isinstance(frame, np.ndarray) else None,
                reason=str(e)
            )

    def get_system_stats(self):
        """Get all system statistics with caching."""
        try:
            current_time = time.time()

            # Update CPU usage if interval passed
            if current_time - self.last_cpu_check >= self.CPU_CHECK_INTERVAL:
                try:
                    self.cached_cpu_usage = psutil.cpu_percent(interval=None)
                    self.last_cpu_check = current_time
                except Exception as e:
                    self.logger.error(f"Error getting CPU usage: {str(e)}")
                    self.cached_cpu_usage = 0

            # Update temperature if interval passed
            if current_time - self.last_temp_check >= self.TEMP_CHECK_INTERVAL:
                try:
                    if not testing:  # Only try if not in testing mode
                        cpu = CPUTemperature()
                        self.cached_temp = cpu.temperature
                except Exception as e:
                    self.logger.error(f"Failed to read CPU temperature: {str(e)}")
                    self.cached_temp = 0
                self.last_temp_check = current_time

            # Check connection status
            if current_time - self.last_connection_check > 10:
                try:
                    if hasattr(self, 'last_frame_time') and self.last_frame_time:
                        time_diff = (datetime.now() - self.last_frame_time).total_seconds()
                        if time_diff > 5:
                            if self.connection_status == "Connected":
                                self.connection_status = "Retrying"
                                self.connection_retry_count = 0
                            elif self.connection_status == "Retrying":
                                self.connection_retry_count += 1
                                if self.connection_retry_count >= self.MAX_RETRIES:
                                    self.connection_status = "Disconnected"
                        else:
                            self.connection_status = "Connected"
                            self.connection_retry_count = 0
                except Exception as e:
                    self.logger.error(f"Error checking connection status: {str(e)}")
                self.last_connection_check = current_time

            return {
                'cpu_percent': round(self.cached_cpu_usage, 1),
                'cpu_temp': round(self.cached_temp, 1),
                'status': self.connection_status,
                'retry_count': self.connection_retry_count,
                'max_retries': self.MAX_RETRIES,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }
        except Exception as e:
            self.logger.error(f"Error in get_system_stats: {str(e)}")
            # Return safe default values
            return {
                'cpu_percent': 0,
                'cpu_temp': 0,
                'status': 'Error',
                'retry_count': 0,
                'max_retries': self.MAX_RETRIES,
                'timestamp': datetime.now().strftime('%H:%M:%S')
            }

    def _generate_frames(self):
        """Generate sequence of frames for streaming with error handling."""
        while True:
            try:
                frame = self.frame_queue.get(timeout=1.0)
                self.last_frame_time = datetime.now()

                success, buffer = cv2.imencode(
                    '.jpg',
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 50]
                )

                if not success:
                    raise errors.StreamUpdateError(
                        frame_size=frame.shape,
                        reason="Failed to encode frame"
                    )

                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

            except queue.Empty:
                self.logger.debug("No frame available")
                continue
            except Exception as e:
                self.logger.error(f"Frame generation error: {e}")
                continue

    def run(self):
        """Run the dashboard server with error handling."""
        try:
            self.app.run(host='localhost', port=self.port)
        except Exception as e:
            raise errors.StreamInitError(f"Failed to start server: {str(e)}")