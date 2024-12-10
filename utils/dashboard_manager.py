import logging
import queue
import psutil
import time
import tempfile
import os
import platform
from typing import Optional
from datetime import datetime
from output_manager import get_platform_config
import utils.error_manager as errors

# Try importing required packages
try:
    from flask import Flask, Response, render_template_string, jsonify, request, send_from_directory
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
    """Manages secure dashboard interface for OWL."""

    def __init__(self, port: int = 5000, target_fps=30):
        """Initialize dashboard with error handling.

        Args:
            port: Port number for Flask server (default: 5000)

        Raises:
            StreamInitError: If dashboard fails to initialize
            DependencyError: If required packages are missing
        """
        # Core Flask and video streaming
        self.app = Flask(__name__)
        self.port = port
        self.frame_queue = queue.Queue(maxsize=1)
        self.latest_frame = None
        self.last_frame_time = None
        self.recording = False
        self.frame_buffer = []
        self.target_fps = target_fps
        self.last_frame_push_time = 0

        # System monitoring
        self.last_cpu_check = 0
        self.cached_cpu_usage = 0
        self.last_temp_check = 0
        self.cached_temp = 0
        self.last_connection_check = time.time()
        self.connection_status = "Connected"
        self.CPU_CHECK_INTERVAL = 2
        self.TEMP_CHECK_INTERVAL = 5

        # Setup logging
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger("OWL.Dashboard")

        try:
            self._setup_routes()
        except Exception as e:
            raise errors.StreamInitError(f"Failed to setup routes: {str(e)}")

        self.logger.info("Dashboard initialized successfully")

    def _setup_routes(self):
        """Setup Flask routes."""

        @self.app.route('/')
        def index():
            return render_template_string(HTML_TEMPLATE)

        @self.app.route('/video_feed')
        def video_feed():
            return Response(
                self._generate_frames(),
                mimetype='multipart/x-mixed-replace; boundary=frame'
            )

        @self.app.route('/images/<path:filename>')
        def serve_image(filename):
            return send_from_directory('../images', filename)

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

                return Response(
                    buffer.tobytes(),
                    mimetype='image/jpeg',
                    headers={
                        'Content-Disposition': f'attachment; filename=owl_frame_{timestamp}.jpg'
                    }
                )
            except Exception as e:
                self.logger.error(f"Failed to download frame: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/start_recording', methods=['POST'])
        def start_recording():
            try:
                self.frame_buffer = []  # Reset frame buffer
                self.recording = True
                return jsonify({'success': True})
            except Exception as e:
                self.logger.error(f"Failed to start recording: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/stop_recording', methods=['POST'])
        def stop_recording():
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
            return jsonify(self.get_system_stats())


    def update_frame(self, frame) -> None:
        """Update the current frame with error handling."""
        current_time = time.time()
        self.last_frame_push_time = current_time
        if current_time - self.last_frame_push_time < (1.0 / self.target_fps):
            return

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
        current_time = time.time()

        # Update CPU usage if interval passed
        if current_time - self.last_cpu_check >= self.CPU_CHECK_INTERVAL:
            self.cached_cpu_usage = psutil.cpu_percent(interval=None)
            self.last_cpu_check = current_time

        # Update temperature if interval passed
        if current_time - self.last_temp_check >= self.TEMP_CHECK_INTERVAL:
            try:
                cpu = CPUTemperature()
                self.cached_temp = cpu.temperature
            except Exception as e:
                self.logger.error(f"Failed to read CPU temperature: {e}")
                self.cached_temp = 0
            self.last_temp_check = current_time

        # Check connection status
        if current_time - self.last_connection_check > 10:  # Check every 10 seconds
            if hasattr(self, 'last_frame_time') and self.last_frame_time:
                time_diff = (datetime.now() - self.last_frame_time).total_seconds()
                if time_diff > 5:  # No frame for 5 seconds
                    self.connection_status = "Disconnected"
                else:
                    self.connection_status = "Connected"
            self.last_connection_check = current_time

        return {
            'cpu_percent': round(self.cached_cpu_usage, 1),
            'cpu_temp': round(self.cached_temp, 1),
            'status': self.connection_status,
            'timestamp': datetime.now().strftime('%H:%M:%S')}

    def _generate_frames(self):
        """Generate sequence of frames for streaming with error handling."""
        while True:
            try:
                frame = self.frame_queue.get(timeout=1.0)
                self.last_frame_time = datetime.now().strftime('%H:%M:%S')

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


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>OWL Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --owl-blue: #022775;
            --owl-grey: #808080;
            --recording-red: #ff4444;
        }

        body {
            margin: 0;
            padding: 0;
            font-family: Arial, sans-serif;
            background: #f5f5f5;
        }

        .header {
            background: var(--owl-blue);
            color: white;
            padding: 1rem;
            text-align: center;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            display: flex;
            align-items: center;
        }

        .header img {
            height: 40px;
            margin-right: 10px;
        }

        .header h1 {
            margin: 0;
            flex-grow: 1;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 1rem;
        }

        .stream-container {
            position: relative;
            background: white;
            padding: 1rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 1rem;
        }

        .stream {
            width: 100%;
            border-radius: 4px;
        }

        .controls {
            display: flex;
            gap: 1rem;
            margin-top: 1rem;
            justify-content: center;
        }

        button {
            background: var(--owl-blue);
            color: white;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            transition: opacity 0.2s;
        }

        button:hover {
            opacity: 0.9;
        }

        .recording {
            background: var(--recording-red);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { background-color: var(--recording-red); }
            50% { background-color: white; color: var(--recording-red); }
            100% { background-color: var(--recording-red); }
        }

        .status {
            display: block;
            color: var(--owl-grey);
            font-size: 0.9rem;
            margin-top: 0.5rem;
        }

        /* Zoom controls */
        .zoom-container {
            position: relative;
            overflow: hidden;
        }

        .zoom-image {
            width: 100%;
            transform-origin: 0 0;
            transition: transform 0.3s ease;
        }

        .zoom-controls {
            position: absolute;
            bottom: 1rem;
            right: 1rem;
            display: flex;
            gap: 0.5rem;
            background: rgba(255,255,255,0.9);
            padding: 0.5rem;
            border-radius: 4px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }

        /* Status Box Styles */
        .status-box {
            background: white;
            padding: 1rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-top: 1rem;
        }

        .status-box h2 {
            margin: 0 0 1rem 0;
            color: var(--owl-blue);
        }

        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-top: 1rem;
        }

        .status-item {
            padding: 0.5rem;
            border-radius: 4px;
            background: #f5f5f5;
        }

        .status-item h3 {
            margin: 0;
            font-size: 1rem;
            color: var(--owl-blue);
        }

        .status-value {
            font-size: 1.5rem;
            font-weight: bold;
            margin-top: 0.5rem;
        }

        .connection-status {
            display: inline-block;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            font-weight: bold;
        }

        .status-connected {
            background: #4CAF50;
            color: white;
        }

        .status-disconnected {
            background: #f44336;
            color: white;
        }

        .status-timestamp {
            margin-top: 0.5rem;
            font-size: 0.9rem;
            color: var(--owl-grey);
            text-align: right;
        }
    </style>
</head>
<body>
    <div class="header">
        <img src="{{ url_for('serve_image', filename='owl-logo-wht.png') }}" alt="OWL Logo">
        <h1>OWL Dashboard</h1>
    </div>

    <div class="container">
        <div class="stream-container">
            <div class="zoom-container">
                <img id="stream" class="stream zoom-image" src="{{ url_for('video_feed') }}">
                <div class="zoom-controls">
                    <button onclick="zoomIn()">+</button>
                    <button onclick="zoomOut()">-</button>
                    <button onclick="resetZoom()">Reset</button>
                </div>
            </div>

            <div class="controls">
                <button onclick="downloadFrame()">Download Frame</button>
                <button onclick="toggleRecording()" id="recordButton">Start Recording</button>
            </div>
        </div>

        <!-- Status Box -->
        <div class="status-box">
            <h2>OWL Status</h2>
            <div class="status-grid">
                <div class="status-item">
                    <h3>CPU Usage</h3>
                    <div class="status-value" id="cpuValue">0%</div>
                </div>
                <div class="status-item">
                    <h3>CPU Temperature</h3>
                    <div class="status-value" id="tempValue">0°C</div>
                </div>
                <div class="status-item">
                    <h3>Connection Status</h3>
                    <div class="status-value">
                        <span class="connection-status" id="connectionStatus">Connected</span>
                    </div>
                </div>
            </div>
            <div class="status-timestamp" id="statusTimestamp"></div>
        </div>
    </div>

    <script>
        let currentZoom = 1;
        const zoomStep = 0.2;
        const maxZoom = 3;
        const minZoom = 1;
        let isRecording = false;

        function zoomIn() {
            if (currentZoom < maxZoom) {
                currentZoom += zoomStep;
                updateZoom();
            }
        }

        function zoomOut() {
            if (currentZoom > minZoom) {
                currentZoom -= zoomStep;
                updateZoom();
            }
        }

        function resetZoom() {
            currentZoom = 1;
            updateZoom();
        }

        function updateZoom() {
            const img = document.querySelector('.zoom-image');
            img.style.transform = `scale(${currentZoom})`;
        }

        function downloadFrame() {
            fetch('/download_frame', { method: 'POST' })
                .then(response => {
                    if (!response.ok) {
                        throw new Error('Frame not available');
                    }
                    return response.blob();
                })
                .then(blob => {
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.style.display = 'none';
                    a.href = url;
                    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
                    a.download = `owl_frame_${timestamp}.jpg`;

                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    document.body.removeChild(a);

                    updateStatus('Frame downloaded successfully');
                })
                .catch(error => {
                    updateStatus(`Error: ${error.message}`);
                });
        }

        function toggleRecording() {
            const button = document.getElementById('recordButton');

            if (!isRecording) {
                // Start recording
                fetch('/start_recording', { method: 'POST' })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            isRecording = true;
                            button.textContent = 'Stop Recording';
                            button.classList.add('recording');
                            updateStatus('Recording started...');
                        }
                    })
                    .catch(error => updateStatus(`Error: ${error.message}`));
            } else {
                // Stop recording and download
                fetch('/stop_recording', { method: 'POST' })
                    .then(response => {
                        if (!response.ok) {
                            throw new Error('Recording failed');
                        }
                        return response.blob();
                    })
                    .then(blob => {
                        isRecording = false;
                        button.textContent = 'Start Recording';
                        button.classList.remove('recording');

                        const url = window.URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.style.display = 'none';
                        a.href = url;
                        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
                        a.download = `owl_recording_${timestamp}.mp4`;

                        document.body.appendChild(a);
                        a.click();
                        window.URL.revokeObjectURL(url);
                        document.body.removeChild(a);

                        updateStatus('Recording saved and downloaded');
                    })
                    .catch(error => {
                        isRecording = false;
                        button.textContent = 'Start Recording';
                        button.classList.remove('recording');
                        updateStatus(`Error: ${error.message}`);
                    });
            }
        }

        function updateStatus(message) {
            const status = document.getElementById('status');
            if (status) {
                status.textContent = message;
                if (!isRecording) {
                    setTimeout(() => {
                        status.textContent = '';
                    }, 3000);
                }
            }
        }

        // Function to get color based on percentage
        function getColorForPercentage(percent, max) {
            const normalized = percent / max;  // For temperature, max is 85
            const hue = ((1 - normalized) * 120).toFixed(0);  // 120 is green, 0 is red
            return `hsl(${hue}, 70%, 50%)`;
        }

        // Update system stats every 2 seconds
        function updateSystemStats() {
            fetch('/system_stats')
                .then(response => response.json())
                .then(data => {
                    // Update CPU Usage
                    const cpuElement = document.getElementById('cpuValue');
                    cpuElement.textContent = `${data.cpu_percent}%`;
                    cpuElement.style.color = getColorForPercentage(data.cpu_percent, 100);

                    // Update CPU Temperature
                    const tempElement = document.getElementById('tempValue');
                    tempElement.textContent = `${data.cpu_temp}°C`;
                    tempElement.style.color = getColorForPercentage(data.cpu_temp, 85);

                    // Update Connection Status
                    const statusElement = document.getElementById('connectionStatus');
                    statusElement.textContent = data.status;
                    statusElement.className = `connection-status status-${data.status.toLowerCase()}`;

                    // Update timestamp
                    document.getElementById('statusTimestamp').textContent = 
                        `Last updated: ${data.timestamp}`;
                })
                .catch(error => {
                    console.error('Error fetching system stats:', error);
                    document.getElementById('connectionStatus').textContent = 'Disconnected';
                    document.getElementById('connectionStatus').className = 
                        'connection-status status-disconnected';
                });
        }

        // Initial update and start interval
        updateSystemStats();
        setInterval(updateSystemStats, 2000);
    </script>
</body>
</html>
"""