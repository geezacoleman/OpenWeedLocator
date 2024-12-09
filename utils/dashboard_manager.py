import logging
from pathlib import Path
import queue
from datetime import datetime
import utils.error_manager as errors

try:
    from flask import Flask, Response, render_template_string, jsonify, request
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
        self.last_frame_time = None

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

        @self.app.route('/save_frame', methods=['POST'])
        def save_frame():
            try:
                frame = self.frame_queue.get_nowait()
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                save_path = Path('saved_frames') / f'frame_{timestamp}.jpg'
                save_path.parent.mkdir(exist_ok=True)

                cv2.imwrite(str(save_path), frame)
                return jsonify({
                    'message': 'Frame saved successfully',
                    'path': str(save_path)
                })
            except queue.Empty:
                return jsonify({'error': 'No frame available'}), 404
            except Exception as e:
                self.logger.error(f"Failed to save frame: {e}")
                return jsonify({'error': str(e)}), 500

    def update_frame(self, frame) -> None:
        """Update the current frame with error handling.

        Args:
            frame: OpenCV/numpy array frame to be streamed

        Raises:
            StreamUpdateError: If frame update fails
        """
        try:
            if not isinstance(frame, np.ndarray):
                raise errors.StreamUpdateError(reason="Invalid frame format - must be numpy array")

            # Clear old frames
            while not self.frame_queue.empty():
                self.frame_queue.get_nowait()

            self.frame_queue.put(frame)
            self.last_frame_time = datetime.now()

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

                # Encode frame
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


# HTML Template with OWL styling
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

        .status {
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
        <h1>OpenWeedLocator Dashboard</h1>
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
                <button onclick="saveFrame()">Save Frame</button>
                <span class="status" id="status"></span>
            </div>
        </div>
    </div>

    <script>
        let currentZoom = 1;
        const zoomStep = 0.2;
        const maxZoom = 3;
        const minZoom = 1;

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

        function saveFrame() {
            fetch('/save_frame', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    const status = document.getElementById('status');
                    if (data.error) {
                        status.textContent = `Error: ${data.error}`;
                    } else {
                        status.textContent = 'Frame saved successfully';
                        setTimeout(() => {
                            status.textContent = '';
                        }, 3000);
                    }
                });
        }

        // Update status periodically
        setInterval(() => {
            const status = document.getElementById('status');
            if (status) {
                status.textContent = `Last update: ${new Date().toLocaleTimeString()}`;
            }
        }, 1000);
    </script>
</body>
</html>
"""