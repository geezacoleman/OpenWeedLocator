#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OWL Dashboard Server
-------------------
Standalone web dashboard for the OpenWeedLocator (OWL) providing a WiFi-accessible interface
for monitoring and control using MQTT for inter-process communication.
"""

import os
import sys
import glob
import shutil
import threading
import logging
import subprocess
import configparser
from pathlib import Path
from datetime import datetime
import urllib.request
import urllib.error

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))
from utils.mqtt_manager import DashMQTTSubscriber
from utils.input_manager import get_rpi_version

try:
    from flask import Flask, Response, render_template, request, jsonify, send_from_directory, send_file
    import psutil
    import cv2
    import numpy as np
except ImportError as e:
    print(f"Error: Required modules not installed. Error: {e}")
    sys.exit(1)


class OWLDashboard:
    def __init__(self):
        self.config = None

        self.controller_type = None

        self.latest_frame = None
        self.frame_lock = threading.Lock()
        self.frame_interval = 0.1
        self.last_yield_time = 0
        self.disconnect_threshold = 2.0  # seconds without new frame => disconnected placeholder

        self.fan_state = 'auto'

        self.logger = logging.getLogger(__name__)
        self.load_config()
        self.setup_logging()

        self.app = Flask(__name__,
                         static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'),
                         template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

        self.mqtt_client = DashMQTTSubscriber(
            broker_host='localhost',
            broker_port=1883,
            client_id=f'owl_dashboard_{os.getpid()}')

        try:
            self.mqtt_client.start()
            self.logger.info("Connected to MQTT broker")
        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT: {e}")
            self.mqtt_client = None

        self.setup_routes()

        # Widget system (optional — does not block dashboard startup)
        widgets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'agent', 'widgets')
        try:
            from agent.widget_manager import WidgetManager
            self.widget_manager = WidgetManager(widgets_dir)
        except Exception as e:
            self.logger.warning(f"Widget system unavailable: {e}")
            self.widget_manager = None

        # Agent engine (optional — does not block dashboard startup)
        try:
            from agent import ToolRegistry, AgentEngine
            registry = ToolRegistry(developer_mode=False)
            registry.discover()
            agent_context = {
                'mqtt_client': self.mqtt_client,
                'config': self.config,
                'widget_manager': self.widget_manager,
            }
            sessions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'agent', 'sessions')
            self.agent_engine = AgentEngine(
                tool_registry=registry,
                context=agent_context,
                sessions_dir=sessions_dir,
            )
        except Exception as e:
            self.logger.warning(f"Agent engine unavailable: {e}")
            self.agent_engine = None

        self.logger.info("OWL Dashboard initialized")

    def get_owl_service_state(self):
        """Return the true systemd state of owl.service."""
        try:
            result = self._run_systemctl_command(["is-active", "owl.service"])
            is_active = result.stdout.strip() == "active"
            return {"active": is_active, "status": result.stdout.strip()}
        except Exception as e:
            self.logger.warning(f"Could not get owl.service state: {e}")
            return {"active": False, "status": "unknown"}

    def control_owl_service(self, action):
        """Start or stop the owl.service, returning a detailed result."""
        if action not in ("start", "stop"):
            return False, "Invalid action specified"
        try:
            self.logger.info(f"Attempting to run systemctl action: '{action}'")

            if action == "start":
                # Clear any failed state first (idempotent, harmless if not failed)
                self._run_systemctl_command(["reset-failed", "owl.service"], needs_sudo=True)

            result = self._run_systemctl_command([action, "owl.service"], needs_sudo=True)

            self.logger.info(f"systemctl command finished with return code: {result.returncode}")
            if result.stdout:
                self.logger.info(f"systemctl stdout: {result.stdout.strip()}")
            if result.stderr:
                self.logger.error(f"systemctl stderr: {result.stderr.strip()}")

            if result.returncode == 0:
                return True, f"OWL service '{action}' command issued successfully."
            else:
                error_message = result.stderr.strip() or f"Failed to {action} owl.service."
                return False, error_message
        except Exception as e:
            self.logger.error(f"An unexpected Python error occurred in control_owl_service: {e}", exc_info=True)
            return False, f"An unexpected error occurred: {str(e)}"

    def load_config(self):
        """Load CONTROLLER.ini for dashboard settings.

        Detection parameters (thresholds, algorithm, etc.) come from owl.py
        via MQTT — not from config files. The dashboard only needs its own
        infrastructure config (MQTT broker, port, GPS source).
        """
        self.config = configparser.ConfigParser()

        controller_ini = Path(__file__).parent.parent.parent / 'config' / 'CONTROLLER.ini'
        if controller_ini.exists():
            self.config.read(controller_ini)
            self.logger.info(f"Config loaded from {controller_ini}")
        else:
            self.logger.warning(f"CONTROLLER.ini not found at {controller_ini}")

        # controller_type is read on-demand via _get_controller_type()
        self.controller_type = 'none'

    def setup_logging(self):
        """Setup logging"""
        log_dir = Path('../../logs')
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

    def get_usb_storage_info(self):
        devices = []
        try:
            df_command = None
            for path in ['/bin/df', '/usr/bin/df']:
                if os.path.exists(path):
                    df_command = path
                    break

            if not df_command:
                self.logger.error("df command not found")
                return devices

            result = subprocess.run([df_command, '-h'],
                                    capture_output=True, text=True, timeout=10)

            if result.returncode != 0:
                self.logger.error(f"df command failed: {result.stderr}")
                return devices

            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 6:
                    device = parts[0]
                    mount_point = parts[5]

                    # Check if it's a USB device (starts with /dev/ and mounted in /media/)
                    if device.startswith('/dev/') and '/media/' in mount_point:
                        devices.append({
                            'device': device,
                            'size': parts[1],
                            'used': parts[2],
                            'available': parts[3],
                            'mount_point': mount_point
                        })
                        self.logger.info(f"Found USB device: {device} mounted at {mount_point}")

        except Exception as e:
            self.logger.error(f"Error retrieving USB storage info: {e}")

        return devices

    def browse_files(self, directory):
        """Browse files in a directory with directory support"""
        files = []
        try:
            directory_path = Path(directory)
            if not directory_path.exists() or not directory_path.is_dir():
                self.logger.warning(f"Directory does not exist or is not a directory: {directory}")
                return files

            # Security check - only allow browsing under /media
            if not str(directory_path).startswith('/media'):
                self.logger.warning(f"Directory access denied: {directory}")
                return files

            for entry in directory_path.iterdir():
                try:
                    stat = entry.stat()
                    files.append({
                        'name': entry.name,
                        'path': str(entry),
                        'size': stat.st_size,
                        'is_directory': entry.is_dir(),
                        'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                        'size_formatted': self.format_file_size(stat.st_size) if not entry.is_dir() else 'Directory'
                    })
                except Exception as e:
                    self.logger.warning(f"Error reading {entry}: {e}")
                    continue

            # Sort: directories first, then by name
            files.sort(key=lambda x: (not x['is_directory'], x['name'].lower()))

            self.logger.info(f"Found {len(files)} items in {directory}")

        except Exception as e:
            self.logger.error(f"Error browsing {directory}: {e}")

        return files

    def format_file_size(self, size_bytes):
        """Format file size in human readable format"""
        if size_bytes == 0:
            return "0 B"
        size_names = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size_bytes >= 1024.0 and i < len(size_names) - 1:
            size_bytes /= 1024.0
            i += 1
        return f"{size_bytes:.1f} {size_names[i]}"

    def setup_routes(self):
        """Setup Flask routes with MQTT"""

        @self.app.route('/')
        def index():
            """Main dashboard page"""
            return render_template('index.html')

        @self.app.route('/api/owl/start', methods=['POST'])
        def start_owl():
            """Starts the owl.service using the robust control method."""
            ok, message = self.control_owl_service('start')
            if ok:
                return jsonify({'success': True, 'message': message})
            else:
                return jsonify({'success': False, 'error': message}), 500

        @self.app.route('/api/owl/stop', methods=['POST'])
        def stop_owl():
            """Stops the owl.service using the robust control method."""
            ok, message = self.control_owl_service('stop')
            if ok:
                return jsonify({'success': True, 'message': message})
            else:
                return jsonify({'success': False, 'error': message}), 500

        @self.app.route('/api/detection/start', methods=['POST'])
        def start_detection():
            if self._is_hardware_locked('detection'):
                return jsonify({
                    'success': False,
                    'message': f'{self._get_controller_type().upper()} controller active - use physical switches'
                }), 423

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_enable(True)
            self.logger.info("Detection enabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/detection/stop', methods=['POST'])
        def stop_detection():
            if self._is_hardware_locked('detection'):
                return jsonify({
                    'success': False,
                    'message': f'{self._get_controller_type().upper()} controller active - use physical switches'
                }), 423

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_enable(False)
            self.logger.info("Detection disabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/recording/start', methods=['POST'])
        def start_recording():
            if self._is_hardware_locked('recording'):
                return jsonify({
                    'success': False,
                    'message': f'{self._get_controller_type().upper()} controller active - use physical switches'
                }), 423

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_image_sample_enable(True)
            self.logger.info("Recording enabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/recording/stop', methods=['POST'])
        def stop_recording():
            if self._is_hardware_locked('recording'):
                return jsonify({
                    'success': False,
                    'message': f'{self._get_controller_type().upper()} controller active - use physical switches'
                }), 423

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_image_sample_enable(False)
            self.logger.info("Recording disabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/nozzles/all-on', methods=['POST'])
        def nozzles_all_on():
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_mode(2)
            self.logger.info("All nozzles ON via dashboard (blanket mode)")
            return jsonify(result)

        @self.app.route('/api/nozzles/all-off', methods=['POST'])
        def nozzles_all_off():
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_mode(1)
            self.logger.info("All nozzles OFF via dashboard")
            return jsonify(result)

        @self.app.route('/api/tracking/set', methods=['POST'])
        def set_tracking():
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                data = request.get_json() or {}
                value = bool(data.get('value', False))
                result = self.mqtt_client._send_command('set_tracking', value=value)
                self._persist_config_change('Tracking', 'tracking_enabled', str(value))
                self.logger.info(f"Tracking {'enabled' if value else 'disabled'} via dashboard")
                return jsonify({'success': True, 'tracking_enabled': value})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/sensitivity/set', methods=['POST'])
        def set_sensitivity():
            """Set sensitivity to specific level"""
            if self._is_hardware_locked('sensitivity'):
                return jsonify({
                    'success': False,
                    'message': f'{self._get_controller_type().upper()} controller active - use physical switches'
                }), 423

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500

            try:
                data = request.get_json() or {}
                level = data.get('level', '').lower()

                if not level:
                    return jsonify({'success': False, 'error': 'Level parameter required'}), 400

                result = self.mqtt_client.set_sensitivity_level(level)

                if result.get('success'):
                    self.logger.info(f"Sensitivity set to {level} via dashboard")
                    return jsonify({'success': True, 'message': f'Sensitivity set to {level}'})
                else:
                    return jsonify({'success': False, 'error': result.get('error', 'Unknown error')}), 500

            except Exception as e:
                self.logger.error(f"Error setting sensitivity: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/sensitivity/presets', methods=['GET'])
        def get_sensitivity_presets():
            """Get available sensitivity presets from OWL state."""
            try:
                if not self.mqtt_client:
                    return jsonify({'success': False, 'error': 'MQTT not connected'}), 500

                state = self.mqtt_client.get_state()
                presets = state.get('sensitivity_presets', [])
                active = state.get('sensitivity_level', 'medium')

                return jsonify({
                    'success': True,
                    'presets': presets,
                    'active': active
                })
            except Exception as e:
                self.logger.error(f"Error getting sensitivity presets: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/sensitivity/save', methods=['POST'])
        def save_sensitivity_preset():
            """Save current slider values as a custom preset."""
            try:
                if not self.mqtt_client:
                    return jsonify({'success': False, 'error': 'MQTT not connected'}), 500

                data = request.get_json() or {}
                name = data.get('name', '').strip()
                if not name:
                    return jsonify({'success': False, 'error': 'Preset name required'}), 400

                result = self.mqtt_client._send_command('save_sensitivity_preset', name=name)
                return jsonify(result)

            except Exception as e:
                self.logger.error(f"Error saving sensitivity preset: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/sensitivity/delete', methods=['POST'])
        def delete_sensitivity_preset():
            """Delete a custom sensitivity preset."""
            try:
                if not self.mqtt_client:
                    return jsonify({'success': False, 'error': 'MQTT not connected'}), 500

                data = request.get_json() or {}
                name = data.get('name', '').strip()
                if not name:
                    return jsonify({'success': False, 'error': 'Preset name required'}), 400

                result = self.mqtt_client._send_command('delete_sensitivity_preset', name=name)
                return jsonify(result)

            except Exception as e:
                self.logger.error(f"Error deleting sensitivity preset: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/update_gps', methods=['POST'])
        def update_gps():
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                gps_data = request.get_json()
                lat = float(gps_data.get('latitude', 0.0))
                lon = float(gps_data.get('longitude', 0.0))
                accuracy = float(gps_data.get('accuracy', 0.0))
                result = self.mqtt_client.update_gps(lat, lon, accuracy)
                # self.logger.info(f"GPS updated: lat={lat}, lon={lon}")
                return jsonify(result)
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})

        @self.app.route('/api/system_stats')
        def system_stats():
            mqtt_state = {}
            if self.mqtt_client:
                mqtt_state = self.mqtt_client.get_state()

            stats = self.get_system_stats()

            # If systemd can't see owl.py but MQTT heartbeat confirms it's running, trust MQTT.
            # This handles manual launches (python owl.py) and non-Pi platforms.
            if not stats['owl_running'] and mqtt_state.get('owl_running', False):
                stats['owl_running'] = True

            stats.update({
                'detection_mode': mqtt_state.get('detection_mode', 1),  # 0=spot, 1=off, 2=blanket
                'algorithm': mqtt_state.get('algorithm', 'exhsv'),
                'model_available': mqtt_state.get('model_available', False),
                # AI tab data
                'available_models': mqtt_state.get('available_models', []),
                'current_model': mqtt_state.get('current_model', ''),
                'model_classes': mqtt_state.get('model_classes', {}),
                'detect_classes': mqtt_state.get('detect_classes', []),
                # Config slider params (GoB thresholds)
                'exg_min': mqtt_state.get('exg_min'),
                'exg_max': mqtt_state.get('exg_max'),
                'hue_min': mqtt_state.get('hue_min'),
                'hue_max': mqtt_state.get('hue_max'),
                'saturation_min': mqtt_state.get('saturation_min'),
                'saturation_max': mqtt_state.get('saturation_max'),
                'brightness_min': mqtt_state.get('brightness_min'),
                'brightness_max': mqtt_state.get('brightness_max'),
                'min_detection_area': mqtt_state.get('min_detection_area', 10),
                'confidence': mqtt_state.get('confidence', 0.5),
                'crop_buffer_px': mqtt_state.get('crop_buffer_px', 20),
                'algorithm_error': mqtt_state.get('algorithm_error'),
                # Tracking
                'tracking_enabled': mqtt_state.get('tracking_enabled', False),
                # Camera resolution
                'resolution_width': mqtt_state.get('resolution_width', 0),
                'resolution_height': mqtt_state.get('resolution_height', 0),
            })

            return jsonify(stats)

        @self.app.route('/api/algorithm/set', methods=['POST'])
        def set_algorithm():
            """Set detection algorithm via MQTT command."""
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                data = request.get_json() or {}
                algorithm = data.get('algorithm', '').lower()
                valid = {'exg', 'exgr', 'maxg', 'nexg', 'exhsv', 'hsv', 'gndvi', 'gog', 'gog-hybrid'}
                if algorithm not in valid:
                    return jsonify({'success': False, 'error': f'Invalid algorithm: {algorithm}'}), 400
                result = self.mqtt_client._send_command('set_algorithm', value=algorithm)
                self._persist_config_change('System', 'algorithm', algorithm)
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error setting algorithm: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/camera/set_max_resolution', methods=['POST'])
        def set_max_resolution():
            """Persist max camera resolution to config (copy-on-write safe).

            Used by the recording resolution warning to change resolution
            before restarting OWL. Does NOT update runtime state — caller
            must restart OWL for the change to take effect.
            """
            try:
                self._persist_config_change('Camera', 'resolution_width', '1456')
                self._persist_config_change('Camera', 'resolution_height', '1088')
                return jsonify({
                    'success': True,
                    'message': 'Resolution set to 1456x1088. Restart OWL to apply.'
                })
            except Exception as e:
                self.logger.error(f"Error setting max resolution: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/download_frame', methods=['POST'])
        def download_frame():
            # This endpoint now acts as a proxy to fetch the frame from owl.py's server
            stream_url = 'http://127.0.0.1:8001/latest_frame.jpg'
            try:
                with urllib.request.urlopen(stream_url, timeout=2) as response:
                    frame_data = response.read()

                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f'owl_frame_{timestamp}.jpg'

                return Response(
                    frame_data,
                    mimetype='image/jpeg',
                    headers={'Content-Disposition': f'attachment; filename={filename}'}
                )
            except urllib.error.URLError as e:
                self.logger.error(f"Error proxying frame download: {e}")
                return jsonify(
                    {'error': 'Failed to retrieve frame from OWL. Is it running?'}), 503  # Service Unavailable
            except Exception as e:
                self.logger.error(f"Unexpected error during frame download: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/video_feed')
        def video_feed():
            """Proxy MJPEG stream from owl.py's built-in server."""
            stream_url = 'http://127.0.0.1:8001/stream.mjpg'

            def generate():
                try:
                    resp = urllib.request.urlopen(stream_url, timeout=5)
                    while True:
                        chunk = resp.read(4096)
                        if not chunk:
                            break
                        yield chunk
                except (urllib.error.URLError, OSError):
                    # Stream unavailable — send a single-frame "no signal" JPEG
                    pass

            return Response(generate(),
                            mimetype='multipart/x-mixed-replace; boundary=frame')

        @self.app.route('/static/<path:filename>')
        def static_files(filename):
            return send_from_directory(self.app.static_folder, filename)

        shared_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'shared')

        @self.app.route('/shared/<path:filename>')
        def shared_static(filename):
            return send_from_directory(shared_dir, filename)

        @self.app.route('/api/usb_storage')
        def api_usb_storage():
            devices = self.get_usb_storage_info()
            return jsonify(devices)

        @self.app.route('/api/browse_files', methods=['POST'])
        def api_browse_files():
            data = request.get_json() or {}
            directory = data.get('directory', '/media')

            # Security: only allow browsing under /media
            if not directory.startswith('/media'):
                return jsonify({
                    'files': [],
                    'directory': directory,
                    'error': 'Directory access denied'
                })

            files = self.browse_files(directory)

            # Add parent directory option (except for /media root)
            if directory != '/media' and directory.startswith('/media'):
                parent_dir = str(Path(directory).parent)
                if parent_dir.startswith('/media'):
                    files.insert(0, {
                        'name': '.. (Parent Directory)',
                        'path': parent_dir,
                        'size': 0,
                        'is_directory': True,
                        'modified': '',
                        'size_formatted': 'Directory',
                        'is_parent': True
                    })

            return jsonify({
                'files': files,
                'directory': directory,
                'total_items': len(files)
            })

        @self.app.route('/api/navigate_directory', methods=['POST'])
        def api_navigate_directory():
            data = request.get_json() or {}
            directory = data.get('directory', '/media')

            # Security check
            if not directory.startswith('/media'):
                return jsonify({'error': 'Directory access denied'}), 403

            if not os.path.exists(directory) or not os.path.isdir(directory):
                return jsonify({'error': 'Directory not found'}), 404

            files = self.browse_files(directory)

            # Add breadcrumb navigation
            breadcrumbs = []
            current_path = '/media'
            breadcrumbs.append({'name': 'USB Storage', 'path': current_path})

            if directory != '/media':
                path_parts = Path(directory).relative_to('/media').parts
                for part in path_parts:
                    current_path = os.path.join(current_path, part)
                    breadcrumbs.append({'name': part, 'path': current_path})

            return jsonify({
                'files': files,
                'directory': directory,
                'breadcrumbs': breadcrumbs,
                'can_go_up': directory != '/media' and directory.startswith('/media')
            })

        @self.app.route('/api/download_file')
        def api_download_file():
            path = request.args.get('path', '')
            if not path.startswith('/media') or not os.path.isfile(path):
                return "File not found", 404
            return send_file(path, as_attachment=True)

        @self.app.route('/api/download_logs')
        def download_logs():
            try:
                import zipfile
                import tempfile

                # Create a temporary zip file
                temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')

                with zipfile.ZipFile(temp_zip.name, 'w') as zip_file:
                    log_dir = Path('../../logs')
                    if log_dir.exists():
                        for log_file in log_dir.glob('*.log*'):
                            if log_file.is_file():
                                zip_file.write(log_file, log_file.name)

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

        # ------------------------------------------------------------------
        # Data Downloads (separate page — local filesystem, no MQTT)
        # ------------------------------------------------------------------

        @self.app.route('/downloads')
        def downloads_page():
            return render_template('downloads.html')

        @self.app.route('/api/downloads/sessions')
        def api_downloads_sessions():
            """List local recording sessions (YYYYMMDD directories)."""
            import re

            save_dir = self._get_save_directory()
            if not save_dir or not os.path.isdir(save_dir):
                return jsonify({'sessions': [], 'storage': None,
                                'error': 'Save directory not configured or not found'})

            sessions = []
            date_pattern = re.compile(r'^\d{8}$')

            try:
                for entry in sorted(os.listdir(save_dir), reverse=True):
                    entry_path = os.path.join(save_dir, entry)
                    if os.path.isdir(entry_path) and date_pattern.match(entry):
                        image_files = [f for f in os.listdir(entry_path)
                                       if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
                        image_size = sum(
                            os.path.getsize(os.path.join(entry_path, f))
                            for f in image_files
                        )
                        sessions.append({
                            'date': entry,
                            'image_count': len(image_files),
                            'image_size': image_size,
                            'total_size': image_size,
                        })
            except OSError as e:
                self.logger.error(f"Error scanning sessions: {e}")
                return jsonify({'sessions': [], 'error': str(e)}), 500

            # Storage info
            storage = None
            try:
                usage = shutil.disk_usage(save_dir)
                storage = {
                    'used_mb': round(usage.used / 1048576),
                    'total_mb': round(usage.total / 1048576),
                    'free_mb': round(usage.free / 1048576),
                }
            except OSError:
                pass

            return jsonify({'sessions': sessions, 'storage': storage})

        @self.app.route('/api/downloads/session/<date>')
        def api_downloads_session_zip(date):
            """Serve a recording session as a ZIP file (created on-demand)."""
            import re
            import zipfile
            import tempfile

            if not re.match(r'^\d{8}$', date):
                return jsonify({'error': 'Invalid date format'}), 400

            save_dir = self._get_save_directory()
            if not save_dir:
                return jsonify({'error': 'Save directory not configured'}), 500

            session_path = os.path.join(save_dir, date)

            # Path traversal check
            if not os.path.realpath(session_path).startswith(os.path.realpath(save_dir)):
                return jsonify({'error': 'Invalid path'}), 400

            if not os.path.isdir(session_path):
                return jsonify({'error': 'Session not found'}), 404

            try:
                temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
                with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_STORED) as zf:
                    for f in sorted(os.listdir(session_path)):
                        fpath = os.path.join(session_path, f)
                        if os.path.isfile(fpath):
                            zf.write(fpath, f)

                filename = f'owl_{date}.zip'
                return send_file(
                    temp_zip.name,
                    as_attachment=True,
                    download_name=filename,
                    mimetype='application/zip'
                )
            except Exception as e:
                self.logger.error(f"Error creating session ZIP: {e}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/downloads/session/<date>/files')
        def api_downloads_session_files(date):
            """List images in a recording session."""
            import re

            if not re.match(r'^\d{8}$', date):
                return jsonify({'error': 'Invalid date format'}), 400

            save_dir = self._get_save_directory()
            if not save_dir:
                return jsonify({'error': 'Save directory not configured'}), 500

            session_path = os.path.join(save_dir, date)
            if not os.path.realpath(session_path).startswith(os.path.realpath(save_dir)):
                return jsonify({'error': 'Invalid path'}), 400

            if not os.path.isdir(session_path):
                return jsonify({'error': 'Session not found'}), 404

            files = []
            for f in sorted(os.listdir(session_path)):
                fpath = os.path.join(session_path, f)
                if os.path.isfile(fpath) and f.lower().endswith(('.jpg', '.jpeg', '.png')):
                    stat = os.stat(fpath)
                    files.append({
                        'filename': f,
                        'size': stat.st_size,
                        'modified': stat.st_mtime,
                    })

            return jsonify({'files': files, 'date': date})

        @self.app.route('/api/downloads/session/<date>', methods=['DELETE'])
        def api_downloads_delete_session(date):
            """Delete a recording session directory."""
            import re

            if not re.match(r'^\d{8}$', date):
                return jsonify({'error': 'Invalid date format'}), 400

            save_dir = self._get_save_directory()
            if not save_dir:
                return jsonify({'error': 'Save directory not configured'}), 500

            session_path = os.path.join(save_dir, date)
            if not os.path.realpath(session_path).startswith(os.path.realpath(save_dir)):
                return jsonify({'error': 'Invalid path'}), 400

            if not os.path.isdir(session_path):
                return jsonify({'error': 'Session not found'}), 404

            try:
                shutil.rmtree(session_path)
                self.logger.info(f"Deleted session: {session_path}")
                return jsonify({'success': True})
            except OSError as e:
                self.logger.error(f"Error deleting session: {e}")
                return jsonify({'error': str(e)}), 500

        # ------------------------------------------------------------------
        # End Downloads routes
        # ------------------------------------------------------------------

        @self.app.route('/api/controller_config', methods=['GET'])
        def get_controller_config():
            """Get controller config - reads fresh from active config file."""
            try:
                # Read fresh from the active config file (not cached self.config)
                active_config = self._get_active_config_path()
                config_path = self._resolve_config_path(active_config)

                if os.path.exists(config_path):
                    config = configparser.ConfigParser()
                    config.read(config_path)
                    controller_type = config.get('Controller', 'controller_type', fallback='none').strip("'\" ").lower()
                else:
                    controller_type = 'none'

                return jsonify({
                    'controller_type': controller_type,
                    'hardware_active': controller_type not in ('none', '')
                })
            except Exception as e:
                self.logger.error(f"Error reading controller config: {e}")
                return jsonify({
                    'controller_type': 'none',
                    'hardware_active': False,
                    'error': str(e)
                })

        @self.app.route('/api/fan/set', methods=['POST'])
        def set_fan_mode():
            """Toggle fan between auto and 100% modes"""
            if get_rpi_version() != 'rpi-5':
                return jsonify({'success': False, 'error': 'Not a Raspberry Pi 5'}), 403

            try:
                data = request.get_json() or {}
                new_mode = data.get('mode', 'auto').lower()

                if new_mode not in ['auto', '100']:
                    return jsonify({'success': False, 'error': 'Invalid fan mode specified'}), 400

                if new_mode == '100':
                    self._run_pinctrl('FAN_PWM', 'op', 'dl')
                    self.fan_state = '100'
                else:
                    self._run_pinctrl('FAN_PWM', 'a0')
                    self.fan_state = 'auto'

                self.logger.info(f"Fan mode toggled to: {new_mode}")
                return jsonify({'success': True, 'mode': new_mode, 'message': f'Fan set to {new_mode}'})

            except Exception as e:
                self.logger.error(f"Failed to toggle fan mode: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/get_errors', methods=['GET'])
        def get_errors():
            """Endpoint for the frontend to poll for new errors from owl.py."""
            if not self.mqtt_client:
                return jsonify([])

            errors = self.mqtt_client.get_and_clear_errors()
            return jsonify(errors)

        # =====================
        # AI Routes
        # =====================

        @self.app.route('/api/ai/set_model', methods=['POST'])
        def set_ai_model():
            """Set AI model via MQTT command."""
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                data = request.get_json() or {}
                model = data.get('model', '')
                if not model:
                    return jsonify({'success': False, 'error': 'Model parameter required'}), 400
                result = self.mqtt_client._send_command('set_model', value=model)
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error setting AI model: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/ai/set_detect_classes', methods=['POST'])
        def set_detect_classes():
            """Set detection classes via MQTT command."""
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                data = request.get_json() or {}
                classes = data.get('classes', [])
                result = self.mqtt_client._send_command('set_detect_classes', value=classes)
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error setting detect classes: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        # =====================
        # Config Slider Routes
        # =====================

        @self.app.route('/api/config/param', methods=['POST'])
        def set_config_param():
            """Set a GreenOnBrown parameter via MQTT command."""
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                data = request.get_json() or {}
                param = data.get('param', '')
                value = data.get('value')
                if not param or value is None:
                    return jsonify({'success': False, 'error': 'param and value required'}), 400
                result = self.mqtt_client._send_command('set_config', key=param, value=int(value))
                self._persist_config_change('GreenOnBrown', param, str(int(value)))
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error setting config param: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config/crop_buffer', methods=['POST'])
        def set_crop_buffer():
            """Set crop buffer via MQTT command."""
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                data = request.get_json() or {}
                value = data.get('value')
                if value is None:
                    return jsonify({'success': False, 'error': 'value required'}), 400
                result = self.mqtt_client._send_command('set_crop_buffer', value=int(value))
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error setting crop buffer: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config/confidence', methods=['POST'])
        def set_confidence():
            """Set AI confidence via MQTT command."""
            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            try:
                data = request.get_json() or {}
                value = data.get('value')
                if value is None:
                    return jsonify({'success': False, 'error': 'value required'}), 400
                result = self.mqtt_client._send_command('set_greenongreen_param', key='confidence', value=float(value))
                self._persist_config_change('GreenOnGreen', 'confidence', str(float(value)))
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Error setting confidence: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        # =====================
        # Config Editor Routes
        # =====================

        # Default config and active config pointer
        DEFAULT_CONFIG = 'config/GENERAL_CONFIG.ini'
        ACTIVE_CONFIG_POINTER = 'config/active_config.txt'

        @self.app.route('/api/config', methods=['GET'])
        def get_config():
            """Get the current configuration file contents."""
            try:
                config = configparser.ConfigParser()
                config.optionxform = str  # Preserve case

                # Get the active config path
                active_config = self._get_active_config_path()
                config_path = self._resolve_config_path(active_config)

                if not config_path or not os.path.exists(config_path):
                    return jsonify({'success': False, 'error': 'Config file not found'}), 404

                config.read(config_path)

                # Convert to nested dict for JSON
                config_dict = {}
                for section in config.sections():
                    config_dict[section] = {}
                    for key, value in config.items(section):
                        config_dict[section][key] = value

                # Get list of available configs
                available_configs = self._list_config_files()

                # Check if current is the default
                is_default = (active_config == DEFAULT_CONFIG)

                return jsonify({
                    'success': True,
                    'config': config_dict,
                    'config_path': config_path,
                    'config_name': os.path.basename(config_path),
                    'active_config': active_config,
                    'is_default': is_default,
                    'available_configs': available_configs,
                    'default_config': DEFAULT_CONFIG
                })

            except Exception as e:
                self.logger.error(f"Error reading config: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config', methods=['POST'])
        def save_config():
            """Save configuration as a new file (never overwrites defaults)."""
            try:
                data = request.get_json()
                if not data or 'config' not in data:
                    return jsonify({'success': False, 'error': 'No config data provided'}), 400

                # Generate new filename with timestamp
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                suggested_name = data.get('filename', f'config_{timestamp}.ini')

                # Ensure it's in the config directory and has .ini extension
                if not suggested_name.endswith('.ini'):
                    suggested_name += '.ini'

                # Sanitize filename
                safe_name = "".join(c for c in suggested_name if c.isalnum() or c in ('_', '-', '.')).strip()
                if not safe_name:
                    safe_name = f'config_{timestamp}.ini'

                config_dir = self._get_config_dir()
                new_config_path = os.path.join(config_dir, safe_name)

                # Don't allow overwriting default configs
                protected_configs = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']
                if safe_name in protected_configs:
                    return jsonify({
                        'success': False,
                        'error': f'Cannot overwrite default config "{safe_name}". Please choose a different name.'
                    }), 400

                # Build config from data
                config = configparser.ConfigParser()
                config.optionxform = str  # Preserve case

                new_config = data['config']
                for section, options in new_config.items():
                    if not config.has_section(section):
                        config.add_section(section)
                    for key, value in options.items():
                        config.set(section, key, str(value))

                # Write the new config file
                with open(new_config_path, 'w') as f:
                    config.write(f)

                self.logger.info(f"Config saved to new file: {new_config_path}")

                # Optionally set as active
                set_active = data.get('set_active', False)
                relative_path = f'config/{safe_name}'

                if set_active:
                    self._set_active_config(relative_path)
                    self._apply_config_to_owl()
                    self.logger.info(f"Set as active config: {relative_path}")

                return jsonify({
                    'success': True,
                    'message': f'Configuration saved as {safe_name}',
                    'filename': safe_name,
                    'config_path': new_config_path,
                    'relative_path': relative_path,
                    'is_active': set_active,
                    'restart_required': False
                })

            except Exception as e:
                self.logger.error(f"Error saving config: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config/set-active', methods=['POST'])
        def set_active_config():
            """Set a config file as the active boot config."""
            try:
                data = request.get_json()
                config_name = data.get('config')

                if not config_name:
                    return jsonify({'success': False, 'error': 'No config specified'}), 400

                # Handle both relative and just filename
                if not config_name.startswith('config/'):
                    config_name = f'config/{config_name}'

                # Verify the config exists using our resolver
                full_path = self._resolve_config_path(config_name)

                if not os.path.exists(full_path):
                    return jsonify({'success': False, 'error': f'Config file not found: {config_name}'}), 404

                self._set_active_config(config_name)
                self._apply_config_to_owl()

                return jsonify({
                    'success': True,
                    'message': f'Active config set to {config_name}',
                    'active_config': config_name,
                    'restart_required': False
                })

            except Exception as e:
                self.logger.error(f"Error setting active config: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config/reset-default', methods=['POST'])
        def reset_to_default():
            """Reset to the default configuration."""
            try:
                config_dir = self._get_config_dir()
                pointer_path = os.path.join(config_dir, 'active_config.txt')

                # Remove the active config pointer to revert to default
                if os.path.exists(pointer_path):
                    os.remove(pointer_path)
                    self.logger.info("Removed active config pointer - reverting to default")

                self._apply_config_to_owl()

                return jsonify({
                    'success': True,
                    'message': f'Reset to default config: {DEFAULT_CONFIG}',
                    'active_config': DEFAULT_CONFIG,
                    'restart_required': False
                })

            except Exception as e:
                self.logger.error(f"Error resetting to default: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config/delete', methods=['POST'])
        def delete_config():
            """Delete a custom config file (cannot delete defaults)."""
            try:
                data = request.get_json()
                config_name = data.get('config')

                if not config_name:
                    return jsonify({'success': False, 'error': 'No config specified'}), 400

                # Don't allow deleting default configs
                protected_configs = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']
                basename = os.path.basename(config_name)

                if basename in protected_configs:
                    return jsonify({
                        'success': False,
                        'error': 'Cannot delete default configuration files'
                    }), 400

                if not config_name.startswith('config/'):
                    config_name = f'config/{config_name}'

                full_path = self._resolve_config_path(config_name)

                if not os.path.exists(full_path):
                    return jsonify({'success': False, 'error': 'Config file not found'}), 404

                # Check if this is the active config
                active_config = self._get_active_config_path()
                if config_name == active_config:
                    # Reset to default first - remove pointer file
                    config_dir = self._get_config_dir()
                    pointer_path = os.path.join(config_dir, 'active_config.txt')
                    if os.path.exists(pointer_path):
                        os.remove(pointer_path)

                os.remove(full_path)
                self.logger.info(f"Deleted config file: {full_path}")

                return jsonify({
                    'success': True,
                    'message': f'Deleted {basename}',
                    'was_active': (config_name == active_config)
                })

            except Exception as e:
                self.logger.error(f"Error deleting config: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config/list', methods=['GET'])
        def list_configs():
            """List all available config files."""
            try:
                configs = self._list_config_files()
                active = self._get_active_config_path()

                return jsonify({
                    'success': True,
                    'configs': configs,
                    'active_config': active,
                    'default_config': DEFAULT_CONFIG
                })

            except Exception as e:
                self.logger.error(f"Error listing configs: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/config/directories', methods=['GET'])
        def list_directories():
            """List available directories for save_directory selection."""
            try:
                directories = []

                # Check common mount points
                mount_points = ['/media/owl', '/media', '/mnt', '/home/owl']
                for mount in mount_points:
                    if os.path.exists(mount):
                        try:
                            for item in os.listdir(mount):
                                full_path = os.path.join(mount, item)
                                if os.path.isdir(full_path):
                                    directories.append(full_path)
                        except PermissionError:
                            continue

                return jsonify({'success': True, 'directories': sorted(set(directories))})

            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        # =====================
        # Widget Routes
        # =====================

        @self.app.route('/api/widgets')
        def list_widgets():
            """List all installed widgets."""
            if not hasattr(self, 'widget_manager') or self.widget_manager is None:
                return jsonify([])
            return jsonify(self.widget_manager.scan())

        @self.app.route('/api/widgets/<widget_id>/template')
        def widget_template(widget_id):
            """Return rendered HTML for a widget."""
            if not hasattr(self, 'widget_manager') or self.widget_manager is None:
                return 'Widget system not available', 404
            html = self.widget_manager.render(widget_id)
            if html is None:
                return 'Widget not found', 404
            return html, 200, {'Content-Type': 'text/html'}

        @self.app.route('/api/widgets/<widget_id>/script')
        def widget_script(widget_id):
            """Return IIFE-wrapped JS for a custom widget."""
            if not hasattr(self, 'widget_manager') or self.widget_manager is None:
                return 'Widget system not available', 404
            widget = self.widget_manager.get(widget_id)
            if widget is None or widget.get('type') != 'custom':
                return 'Not found', 404
            script_path = self.widget_manager.widgets_dir / widget_id / 'script.js'
            if not script_path.exists():
                return '', 200, {'Content-Type': 'application/javascript'}
            js = script_path.read_text()
            wrapped = f'(function(OWLWidget) {{\n{js}\n}})(window.OWLWidget);'
            return wrapped, 200, {'Content-Type': 'application/javascript'}

        @self.app.route('/api/widgets/<widget_id>/style')
        def widget_style(widget_id):
            """Return scoped CSS for a custom widget."""
            if not hasattr(self, 'widget_manager') or self.widget_manager is None:
                return 'Widget system not available', 404
            widget = self.widget_manager.get(widget_id)
            if widget is None or widget.get('type') != 'custom':
                return 'Not found', 404
            style_path = self.widget_manager.widgets_dir / widget_id / 'style.css'
            if not style_path.exists():
                return '', 200, {'Content-Type': 'text/css'}
            css = style_path.read_text()
            scoped = f'.widget-container[data-widget-id="{widget_id}"] {{\n{css}\n}}'
            return scoped, 200, {'Content-Type': 'text/css'}

        @self.app.route('/api/widgets/<widget_id>', methods=['DELETE'])
        def delete_widget(widget_id):
            """Delete an installed widget."""
            if not hasattr(self, 'widget_manager') or self.widget_manager is None:
                return jsonify({'error': 'Widget system not available'}), 500
            success, error = self.widget_manager.remove(widget_id)
            if success:
                return jsonify({'status': 'ok'})
            return jsonify({'error': error}), 404

        # ---- Agent API routes ----

        @self.app.route('/api/agent/connect', methods=['POST'])
        def agent_connect():
            """Validate API key and configure agent provider."""
            if not hasattr(self, 'agent_engine') or self.agent_engine is None:
                return jsonify({'error': 'Agent engine not available'}), 500
            data = request.get_json()
            if not data:
                return jsonify({'error': 'No JSON body'}), 400
            api_key = data.get('api_key', '').strip()
            provider = data.get('provider', 'anthropic').strip()
            if not api_key:
                return jsonify({'error': 'API key is required'}), 400
            try:
                valid = self.agent_engine.set_provider(api_key, provider)
                if valid:
                    status = self.agent_engine.get_status()
                    return jsonify({'status': 'connected', 'model': status.get('model')})
                return jsonify({'error': 'Invalid API key'}), 401
            except Exception as e:
                return jsonify({'error': str(e)}), 400

        @self.app.route('/api/agent/chat', methods=['POST'])
        def agent_chat():
            """Stream agent responses via SSE."""
            if not hasattr(self, 'agent_engine') or self.agent_engine is None:
                return jsonify({'error': 'Agent engine not available'}), 500
            data = request.get_json()
            if not data:
                return jsonify({'error': 'No JSON body'}), 400
            message = data.get('message', '').strip()
            images = data.get('images', [])
            session_id = data.get('session_id', 'default')
            if not message and not images:
                return jsonify({'error': 'Message or image is required'}), 400

            # Validate images
            if len(images) > 4:
                return jsonify({'error': 'Maximum 4 images per message'}), 400
            for img in images:
                if not isinstance(img, str) or len(img) > 1_400_000:
                    return jsonify({'error': 'Invalid or oversized image (max ~1MB)'}), 400

            # Build content array if images present
            if images:
                content = []
                for img_data in images:
                    content.append({
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_data}
                    })
                if message:
                    content.append({'type': 'text', 'text': message})
                chat_input = content
            else:
                chat_input = message

            import json as json_mod

            def generate():
                try:
                    for chunk in self.agent_engine.chat(session_id, chat_input):
                        event_data = json_mod.dumps({
                            'type': chunk.type,
                            'data': chunk.data if not hasattr(chunk.data, '__dict__')
                                    else {'id': chunk.data.id, 'name': chunk.data.name,
                                          'arguments': chunk.data.arguments},
                        })
                        yield f"data: {event_data}\n\n"
                    # Send done event
                    session_info = self.agent_engine.get_session_info(session_id)
                    yield f"data: {json_mod.dumps({'type': 'done', 'data': session_info})}\n\n"
                except Exception as e:
                    self.logger.exception("Agent chat error")
                    yield f"data: {json_mod.dumps({'type': 'error', 'data': str(e)})}\n\n"

            return Response(generate(), mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

        @self.app.route('/api/agent/grab_frame')
        def agent_grab_frame():
            """Grab frame from local OWL, return as base64 for agent chat."""
            import base64 as b64mod
            try:
                with urllib.request.urlopen('http://127.0.0.1:8001/latest_frame.jpg', timeout=2) as resp:
                    b64 = b64mod.b64encode(resp.read()).decode('ascii')
                return jsonify({'image': b64})
            except Exception as e:
                self.logger.error(f"Grab frame error: {e}")
                return jsonify({'error': f'Could not grab frame: {e}'}), 502

        @self.app.route('/api/agent/status')
        def agent_status():
            """Return agent connection status and token usage."""
            if not hasattr(self, 'agent_engine') or self.agent_engine is None:
                return jsonify({'connected': False, 'provider': None, 'model': None})
            status = self.agent_engine.get_status()
            return jsonify(status)

        @self.app.route('/api/agent/sessions')
        def agent_sessions():
            """List saved agent sessions."""
            if not hasattr(self, 'agent_engine') or self.agent_engine is None:
                return jsonify([])
            return jsonify(self.agent_engine.list_sessions())

        @self.app.route('/api/agent/sessions/<session_id>')
        def agent_session_load(session_id):
            """Load a saved session with full message history."""
            if not hasattr(self, 'agent_engine') or self.agent_engine is None:
                return jsonify({'error': 'Agent engine not available'}), 500
            data = self.agent_engine.load_session(session_id)
            if data is None:
                return jsonify({'error': 'Session not found'}), 404
            return jsonify(data)

        @self.app.route('/api/agent/sessions/<session_id>', methods=['DELETE'])
        def agent_session_delete(session_id):
            """Delete a saved session."""
            if not hasattr(self, 'agent_engine') or self.agent_engine is None:
                return jsonify({'error': 'Agent engine not available'}), 500
            deleted = self.agent_engine.delete_session(session_id)
            if deleted:
                return jsonify({'status': 'deleted'})
            return jsonify({'error': 'Session not found'}), 404

    def _get_active_config_path(self):
        """Get the active config path from pointer file, or default."""
        # Try to find config directory relative to this file
        base_dir = os.path.dirname(os.path.abspath(__file__))

        # Check for pointer file in possible locations
        pointer_locations = [
            os.path.join(base_dir, 'config/active_config.txt'),
            os.path.join(base_dir, '../config/active_config.txt'),
            os.path.join(base_dir, '../../config/active_config.txt'),
        ]

        for pointer_path in pointer_locations:
            if os.path.exists(pointer_path):
                try:
                    with open(pointer_path, 'r') as f:
                        active = f.read().strip()
                        if active:
                            return active
                except Exception as e:
                    self.logger.warning(f"Could not read active config pointer: {e}")

        # Return default - will be resolved by _resolve_config_path
        return 'config/GENERAL_CONFIG.ini'

    def _get_controller_type(self):
        """Read controller_type fresh from the active config file."""
        try:
            active_config = self._get_active_config_path()
            config_path = self._resolve_config_path(active_config)

            if os.path.exists(config_path):
                config = configparser.ConfigParser()
                config.read(config_path)
                return config.get('Controller', 'controller_type', fallback='none').strip("'\" ").lower()
        except Exception as e:
            self.logger.warning(f"Could not read controller_type: {e}")

        return 'none'

    def _is_hardware_locked(self, control):
        """Check if a control is hardware-locked for the current controller type.

        UTE: only 'recording' is hardware-controlled.
        Advanced: 'recording', 'detection', 'sensitivity' are hardware-controlled.
        Nozzles, tracking, fan: never locked.
        """
        controller_type = self._get_controller_type()
        if controller_type in ('none', ''):
            return False

        if controller_type == 'ute':
            return control == 'recording'
        elif controller_type == 'advanced':
            return control in ('recording', 'detection', 'sensitivity')

        return False

    def _get_save_directory(self):
        """Read save_directory from the active OWL config."""
        try:
            active_config = self._get_active_config_path()
            config_path = self._resolve_config_path(active_config)

            if os.path.exists(config_path):
                config = configparser.ConfigParser()
                config.read(config_path)
                return config.get('DataCollection', 'save_directory', fallback=None)
        except Exception as e:
            self.logger.warning(f"Could not read save_directory: {e}")

        return None

    def _resolve_config_path(self, relative_path):
        """Resolve a relative config path to absolute."""
        base_dir = os.path.dirname(os.path.abspath(__file__))

        # Try multiple possible locations
        possible_paths = [
            os.path.join(base_dir, relative_path),
            os.path.join(base_dir, '..', relative_path),
            os.path.join(base_dir, '..', '..', relative_path),
            os.path.join(base_dir, relative_path.replace('config/', '../config/')),
        ]

        for path in possible_paths:
            normalized = os.path.normpath(path)
            if os.path.exists(normalized):
                return normalized

        # Return first option even if doesn't exist (for error reporting)
        return os.path.normpath(possible_paths[0])

    def _get_config_dir(self):
        """Find the config directory."""
        base_dir = os.path.dirname(os.path.abspath(__file__))

        possible_dirs = [
            os.path.join(base_dir, 'config'),
            os.path.join(base_dir, '..', 'config'),
            os.path.join(base_dir, '..', '..', 'config'),
        ]

        for config_dir in possible_dirs:
            normalized = os.path.normpath(config_dir)
            if os.path.exists(normalized) and os.path.isdir(normalized):
                return normalized

        return os.path.normpath(possible_dirs[0])

    def _set_active_config(self, config_path):
        """Set the active config by writing to pointer file."""
        config_dir = self._get_config_dir()
        pointer_path = os.path.join(config_dir, 'active_config.txt')

        with open(pointer_path, 'w') as f:
            f.write(config_path)

        self.logger.info(f"Active config set to: {config_path}")

    def _list_config_files(self):
        """List all .ini config files."""
        config_dir = self._get_config_dir()

        configs = []
        if os.path.exists(config_dir):
            for f in os.listdir(config_dir):
                if f.endswith('.ini'):
                    full_path = os.path.join(config_dir, f)
                    stat = os.stat(full_path)

                    # Determine if it's a default config
                    is_default = f in ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']

                    configs.append({
                        'name': f,
                        'path': f'config/{f}',
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        'is_default': is_default
                    })

        # Sort: defaults first, then by modified date descending
        configs.sort(key=lambda x: (not x['is_default'], x['modified']), reverse=True)
        configs.sort(key=lambda x: x['is_default'], reverse=True)

        return configs

    def _persist_config_change(self, section, key, value):
        """Persist a single config parameter change to the active config file.
        If the active config is a protected default, saves as a new file and sets it active.
        """
        try:
            active_config = self._get_active_config_path()
            config_path = self._resolve_config_path(active_config)

            if not os.path.exists(config_path):
                self.logger.warning(f"Config file not found for persistence: {config_path}")
                return

            config = configparser.ConfigParser()
            config.optionxform = str
            config.read(config_path)

            if not config.has_section(section):
                config.add_section(section)
            config.set(section, key, str(value))

            basename = os.path.basename(config_path)
            protected = ['GENERAL_CONFIG.ini', 'CONTROLLER.ini']

            if basename in protected:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                new_name = f'config_{timestamp}.ini'
                config_dir = self._get_config_dir()
                new_path = os.path.join(config_dir, new_name)

                with open(new_path, 'w') as f:
                    config.write(f)

                self._set_active_config(f'config/{new_name}')
                self.logger.info(f"Config saved to new file: {new_name} (was protected default)")
            else:
                with open(config_path, 'w') as f:
                    config.write(f)
                self.logger.info(f"Config updated: {basename} [{section}] {key}={value}")

        except Exception as e:
            self.logger.error(f"Error persisting config change [{section}].{key}: {e}")

    def _apply_config_to_owl(self):
        """Read the active config file and push detection parameters to owl.py via MQTT.

        Called after config switch, reset, or save-as-active so that owl.py's runtime
        state matches the new config without requiring a restart.
        """
        if not self.mqtt_client:
            return

        try:
            active_config = self._get_active_config_path()
            config_path = self._resolve_config_path(active_config)
            if not os.path.exists(config_path):
                self.logger.warning(f"Cannot apply config — file not found: {config_path}")
                return

            config = configparser.ConfigParser()
            config.read(config_path)

            # Push algorithm
            algorithm = config.get('System', 'algorithm', fallback=None)
            if algorithm:
                self.mqtt_client._send_command('set_algorithm', value=algorithm)

            # Push GreenOnBrown params
            gob_params = [
                'exg_min', 'exg_max', 'hue_min', 'hue_max',
                'saturation_min', 'saturation_max',
                'brightness_min', 'brightness_max',
                'min_detection_area'
            ]
            for param in gob_params:
                val = config.get('GreenOnBrown', param, fallback=None)
                if val is not None:
                    try:
                        self.mqtt_client._send_command('set_config', key=param, value=int(val))
                    except (ValueError, TypeError):
                        pass

            # Push GreenOnGreen confidence
            confidence = config.get('GreenOnGreen', 'confidence', fallback=None)
            if confidence is not None:
                try:
                    self.mqtt_client._send_command(
                        'set_greenongreen_param', key='confidence', value=float(confidence))
                except (ValueError, TypeError):
                    pass

            self.logger.info(f"Applied config to OWL: {os.path.basename(config_path)}")

        except Exception as e:
            self.logger.error(f"Error applying config to OWL: {e}")

    def _check_restart_required(self, changed_sections):
        """Check if changed sections require a service restart."""
        restart_sections = {'Controller'}
        return bool(set(changed_sections) & restart_sections)

    def get_system_stats(self):
        """
        Get system statistics with robust, component-level error handling.
        This function is designed to always return a complete dictionary, even if some
        underlying system commands fail.
        """
        # 1. Start with a default dictionary. This guarantees that the frontend
        #    will always receive all the keys it expects, preventing JavaScript errors.
        stats = {
            'cpu_percent': 0,
            'cpu_temp': 0,
            'memory_percent': 0,
            'memory_used': 0,
            'memory_total': 0,
            'disk_percent': 0,
            'disk_used': 0,
            'disk_total': 0,
            'usb_devices': [],
            'fan_status': {'is_rpi5': False, 'mode': 'unavailable', 'rpm': 0},
            'owl_running': False,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }

        try:
            service_state = self.get_owl_service_state()
            stats['owl_running'] = service_state['active']

            rpi_version = get_rpi_version()
            if rpi_version == 'rpi-5':
                stats['fan_status']['is_rpi5'] = True
                stats['fan_status']['mode'] = self.fan_state

                try:
                    rpm_files = glob.glob('/sys/devices/platform/cooling_fan/hwmon/*/fan1_input')

                    if rpm_files:
                        try:
                            with open(rpm_files[0], 'r') as f:
                                stats['fan_status']['rpm'] = int(f.read().strip())
                        except (IOError, ValueError) as e:
                            self.logger.warning(f"Could not read fan RPM: {e}")
                            stats['fan_status']['rpm'] = 0
                    else:
                        stats['fan_status']['rpm'] = 0

                except Exception as e:
                    self.logger.warning(f"Could not determine full fan state: {e}")

            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')

            stats.update({
                'cpu_percent': round(cpu_percent, 1),
                'memory_percent': round(memory.percent, 1),
                'memory_used': round(memory.used / (1024 ** 3), 1),
                'memory_total': round(memory.total / (1024 ** 3), 1),
                'disk_percent': round(disk.percent, 1),
                'disk_used': round(disk.used / (1024 ** 3), 1),
                'disk_total': round(disk.total / (1024 ** 3), 1),
            })

            try:
                result = subprocess.run(['/usr/bin/vcgencmd', 'measure_temp'], capture_output=True, text=True)
                if result.returncode == 0:
                    stats['cpu_temp'] = round(float(result.stdout.replace('temp=', '').replace("'C\n", '')), 1)

            except Exception as e:
                self.logger.warning(f"Temp. retrieval error: {e}")
                pass

            # USB devices
            usb_devices = []
            try:
                result = subprocess.run(['/usr/bin/lsusb'], capture_output=True, text=True)
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if 'Camera' in line or 'Webcam' in line:
                            usb_devices.append(line.strip())

            except Exception as e:
                self.logger.warning(f"USB devices retrieval error: {e}")
                pass

        except Exception as e:
            self.logger.error(f"A critical error occurred while getting system stats: {e}")
            return stats

        mqtt_state = self.mqtt_client.get_state() if self.mqtt_client else {}
        stats.update({
            'detection_enable': mqtt_state.get('detection_enable', False),
            'image_sample_enable': mqtt_state.get('image_sample_enable', False),
            'sensitivity_level': mqtt_state.get('sensitivity_level', 'high'),
            'stream_active': mqtt_state.get('stream_active', False),
            'weed_detect_indicator': self.mqtt_client.get_weed_detect_indicator() if self.mqtt_client else False,
            'image_write_indicator': self.mqtt_client.get_image_write_indicator() if self.mqtt_client else False
        })

        return stats

    def _run_pinctrl(self, *args):
        cmd = ['/usr/bin/sudo', '-n', '/usr/bin/pinctrl', *args]
        res = subprocess.run(cmd, capture_output=True, text=True)

        if res.returncode != 0:
            err = (res.stderr or res.stdout).strip()
            raise RuntimeError(f"pinctrl {' '.join(args)} failed: {err}")

        return res.stdout

    def _get_systemctl_path(self):
        """Find the absolute path to the systemctl executable for reliability."""
        path = shutil.which("systemctl")
        if path:
            return path
        if os.path.exists("/usr/bin/systemctl"):
            return "/usr/bin/systemctl"
        if os.path.exists("/bin/systemctl"):
            return "/bin/systemctl"
        return "systemctl"

    def _run_systemctl_command(self, args, needs_sudo=False):
        """A robust wrapper for running systemctl commands."""
        systemctl_path = self._get_systemctl_path()
        if not os.path.exists(systemctl_path):
            raise FileNotFoundError("systemctl executable not found")

        cmd = [systemctl_path] + args
        if needs_sudo:
            cmd.insert(0, 'sudo')

        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)

    def stop(self):
        """Stop the dashboard"""
        if self.mqtt_client:
            self.mqtt_client.stop()


# Create the dashboard instance
dashboard = OWLDashboard()
app = dashboard.app

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='OWL Dashboard Server')
    parser.add_argument('--port', type=int, default=8000, help='Port to run the server on')
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    args = parser.parse_args()

    app.run(host='0.0.0.0', port=args.port, debug=args.debug)