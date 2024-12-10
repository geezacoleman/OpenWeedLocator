import logging
import queue
from datetime import datetime
import tempfile
import os

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


class OWLDashboard:
    """Manages secure dashboard interface for OWL."""

    def __init__(self, port: int = 5000):
        """Initialize dashboard with error handling.

        Args:
            port: Port number for Flask server (default: 5000)

        Raises:
            StreamInitError: If dashboard fails to initialize
            DependencyError: If required packages are missing
        """
        self.app = Flask(__name__)
        self.port = port
        self.frame_queue = queue.Queue(maxsize=1)
        self.latest_frame = None
        self.last_frame_time = None
        self.recording = False
        self.frame_buffer = []

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

                    # Get frame dimensions from first frame
                    height, width = self.frame_buffer[0].shape[:2]

                    # Create temporary file
                    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_file:
                        out = cv2.VideoWriter(temp_file.name, fourcc, 30.0, (width, height))
                        for frame in self.frame_buffer:
                            out.write(frame)
                        out.release()

                        # Read the temporary file
                        with open(temp_file.name, 'rb') as f:
                            video_data = f.read()

                    # Clean up
                    os.unlink(temp_file.name)
                    self.frame_buffer = []

                    # Send video file to browser
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

    def update_frame(self, frame) -> None:
        """Update the current frame with error handling."""
        try:
            if not isinstance(frame, np.ndarray):
                raise errors.StreamUpdateError(reason="Invalid frame format - must be numpy array")

            # Store frame if recording
            if self.recording:
                self.frame_buffer.append(frame.copy())

            # Clear old frames from display queue
            while not self.frame_queue.empty():
                self.frame_queue.get_nowait()

            self.frame_queue.put(frame)
            self.last_frame_time = datetime.now()
            self.latest_frame = frame.copy()

        except queue.Full:
            self.logger.warning("Frame queue full - skipping frame")
        except Exception as e:
            raise errors.StreamUpdateError(
                frame_size=frame.shape if isinstance(frame, np.ndarray) else None,
                reason=str(e)
            )

    def _generate_frames(self):
        """Generate sequence of frames for streaming with error handling."""
        while True:
            try:
                frame = self.frame_queue.get(timeout=1.0)
                self.last_frame_time = datetime.now().strftime('%H:%M:%S')

                success, buffer = cv2.imencode(
                    '.jpg',
                    frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 70]
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
            display: block
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
            <span class="status" id="status"></span>
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

                        // Download video file
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
            status.textContent = message;
            if (!isRecording) {
                setTimeout(() => {
                    status.textContent = '';
                }, 3000);
            }
        }

        // Update status periodically only when not recording
        setInterval(() => {
            const status = document.getElementById('status');
            if (status && !isRecording) {
                status.textContent = `Last update: ${new Date().toLocaleTimeString()}`;
            }
        }, 1000);
    </script>
</body>
</html>
"""