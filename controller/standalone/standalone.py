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
from utils.upload_manager import get_uploader
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
    def __init__(self, config_file='../config/DAY_SENSITIVITY_2.ini'):
        self.config = None
        self.config_file = config_file

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
            client_id='owl_dashboard')

        try:
            self.mqtt_client.start()
            self.logger.info("Connected to MQTT broker")
        except Exception as e:
            self.logger.error(f"Failed to connect to MQTT: {e}")
            self.mqtt_client = None

        self.setup_routes()

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
        """Load configuration from config file"""
        config_path = Path(self.config_file)
        if not config_path.exists():
            # Try relative to script directory
            config_path = Path(__file__).parent / self.config_file

        self.config = configparser.ConfigParser()
        if config_path.exists():
            self.config.read(config_path)
            self.config.read(Path(__file__).parent.parent.parent / 'config' / 'CONTROLLER.ini')
            self.logger.info(f"Config loaded from {config_path}")
            self.controller_type = self.config.get('Controller', 'controller_type', fallback='none').strip(
                "'\" ").lower()

        else:
            self.logger.warning(f"Config file not found at {config_path}")
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
            # Read fresh controller type from config
            controller_type = self._get_controller_type()

            if controller_type not in ('none', ''):
                return jsonify({
                    'success': False,
                    'message': f'{controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_enable(True)
            self.logger.info("Detection enabled via dashboard")
            return jsonify(result)

        # Replace the existing detection/stop route
        @self.app.route('/api/detection/stop', methods=['POST'])
        def stop_detection():
            # Read fresh controller type from config
            controller_type = self._get_controller_type()

            if controller_type not in ('none', ''):
                return jsonify({
                    'success': False,
                    'message': f'{controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_enable(False)
            self.logger.info("Detection disabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/recording/start', methods=['POST'])
        def start_recording():
            # Read fresh controller type from config
            controller_type = self._get_controller_type()

            if controller_type not in ('none', ''):
                return jsonify({
                    'success': False,
                    'message': f'{controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_image_sample_enable(True)
            self.logger.info("Recording enabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/recording/stop', methods=['POST'])
        def stop_recording():
            # Read fresh controller type from config
            controller_type = self._get_controller_type()

            if controller_type not in ('none', ''):
                return jsonify({
                    'success': False,
                    'message': f'{controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_image_sample_enable(False)
            self.logger.info("Recording disabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/sensitivity/set', methods=['POST'])
        def set_sensitivity():
            """Set sensitivity to specific level"""
            # Read fresh controller type from config
            controller_type = self._get_controller_type()

            if controller_type not in ('none', ''):
                return jsonify({
                    'success': False,
                    'message': f'{controller_type.upper()} controller active - use physical switches'
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

            stats.update({
                'detection_enable': mqtt_state.get('detection_enable', False),
                'image_sample_enable': mqtt_state.get('image_sample_enable', False),
                'sensitivity_level': mqtt_state.get('sensitivity_level', 'high'),
                'detection_mode': mqtt_state.get('detection_mode', 1),  # 0=spot, 1=off, 2=blanket
                'owl_running': mqtt_state.get('owl_running', False),
                'stream_active': mqtt_state.get('stream_active', False),
                'weed_detect_indicator': self.mqtt_client.get_weed_detect_indicator() if self.mqtt_client else False,
                'image_write_indicator': self.mqtt_client.get_image_write_indicator() if self.mqtt_client else False
            })

            return jsonify(stats)

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

        @self.app.route('/api/upload/check_ethernet', methods=['POST'])
        def check_ethernet():
            try:
                uploader = get_uploader()
                result = uploader.check_ethernet_connection()
                return jsonify(result)
            except Exception as e:
                return jsonify({'connected': False, 'error': str(e)}), 500

        @self.app.route('/api/upload/test_credentials', methods=['POST'])
        def test_s3_credentials():
            try:
                data = request.get_json()
                access_key = data.get('access_key', '')
                secret_key = data.get('secret_key', '')
                bucket_name = data.get('bucket_name', '')
                region = data.get('region', 'us-east-1')
                endpoint_url = data.get('endpoint_url', None)

                if not all([access_key, secret_key, bucket_name]):
                    return jsonify({'valid': False, 'error': 'Missing required credentials'}), 400

                uploader = get_uploader()
                result = uploader.test_s3_credentials(
                    access_key, secret_key, bucket_name, region, endpoint_url
                )
                return jsonify(result)

            except Exception as e:
                return jsonify({'valid': False, 'error': str(e)}), 500

        @self.app.route('/api/upload/scan_directory', methods=['POST'])
        def scan_upload_directory():
            try:
                data = request.get_json()
                directory_path = data.get('directory_path', '')

                if not directory_path:
                    return jsonify({'success': False, 'error': 'Directory path required'}), 400

                if not directory_path.startswith('/media'):
                    return jsonify({'success': False, 'error': 'Directory access denied'}), 403

                uploader = get_uploader()
                result = uploader.scan_directory(directory_path, preview_only=True)
                if result['success']:
                    self.logger.info(f"Directory scan: {result['file_count']} files found in {directory_path}")

                return jsonify(result)

            except Exception as e:
                self.logger.error(f"Error scanning directory: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/upload/start', methods=['POST'])
        def start_upload():
            try:
                data = request.get_json()

                # Extract parameters
                directory_path = data.get('directory_path', '')
                access_key = data.get('access_key', '')
                secret_key = data.get('secret_key', '')
                bucket_name = data.get('bucket_name', '')
                s3_prefix = data.get('s3_prefix', '')
                region = data.get('region', 'us-east-1')
                endpoint_url = data.get('endpoint_url', None)
                max_workers = data.get('max_workers', 4)

                # Validate required fields
                if not all([directory_path, access_key, secret_key, bucket_name]):
                    return jsonify({'success': False, 'error': 'Missing required parameters'}), 400

                # Security check
                if not directory_path.startswith('/media'):
                    return jsonify({'success': False, 'error': 'Directory access denied'}), 403

                uploader = get_uploader()

                # Start upload
                success = uploader.start_upload(
                    directory_path, access_key, secret_key, bucket_name,
                    s3_prefix, region, endpoint_url, max_workers
                )

                if success:
                    return jsonify({'success': True, 'message': 'Upload started'})
                else:
                    return jsonify({'success': False, 'error': 'Upload already in progress'}), 409

            except Exception as e:
                self.logger.error(f"Error starting upload: {e}")
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/upload/progress', methods=['GET'])
        def get_upload_progress():
            try:
                uploader = get_uploader()
                progress = uploader.get_progress()
                return jsonify(progress)
            except Exception as e:
                return jsonify({'status': 'error', 'error_message': str(e)}), 500

        @self.app.route('/api/upload/stop', methods=['POST'])
        def stop_upload():
            try:
                uploader = get_uploader()
                uploader.stop_upload_process()
                return jsonify({'success': True, 'message': 'Upload stopped'})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/upload/browse_directories', methods=['POST'])
        def browse_upload_directories():
            """Browse directories for upload selection - only shows directories"""
            try:
                data = request.get_json() or {}
                directory = data.get('directory', '/media')

                # Security: only allow browsing under /media
                if not directory.startswith('/media'):
                    return jsonify({
                        'directories': [],
                        'directory': directory,
                        'error': 'Directory access denied'
                    }), 403

                directories = []
                try:
                    directory_path = Path(directory)
                    if not directory_path.exists() or not directory_path.is_dir():
                        return jsonify({
                            'directories': [],
                            'directory': directory,
                            'error': 'Directory not found'
                        }), 404

                    for entry in directory_path.iterdir():
                        if entry.is_dir():
                            try:
                                stat = entry.stat()
                                directories.append({
                                    'name': entry.name,
                                    'path': str(entry),
                                    'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
                                })
                            except Exception:
                                continue

                    # Sort directories by name
                    directories.sort(key=lambda x: x['name'].lower())

                    # Add parent directory option (except for /media root)
                    if directory != '/media' and directory.startswith('/media'):
                        parent_dir = str(Path(directory).parent)
                        if parent_dir.startswith('/media'):
                            directories.insert(0, {
                                'name': '.. (Parent Directory)',
                                'path': parent_dir,
                                'modified': '',
                                'is_parent': True
                            })

                except Exception as e:
                    return jsonify({
                        'directories': [],
                        'directory': directory,
                        'error': str(e)
                    }), 500

                return jsonify({
                    'directories': directories,
                    'directory': directory,
                    'total_items': len(directories)
                })

            except Exception as e:
                return jsonify({
                    'directories': [],
                    'directory': '/media',
                    'error': str(e)
                }), 500

        @self.app.route('/api/upload/find_key_files', methods=['GET'])
        def find_key_files():
            try:
                uploader = get_uploader()
                result = uploader.find_key_files()
                return jsonify(result)
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/upload/load_credentials', methods=['POST'])
        def load_credentials():
            try:
                data = request.get_json()
                file_path = data.get('file_path', '')

                if not file_path:
                    return jsonify({'success': False, 'error': 'File path required'}), 400

                uploader = get_uploader()
                result = uploader.load_credentials_from_file(file_path)
                return jsonify(result)

            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

        @self.app.route('/api/upload/create_metadata', methods=['POST'])
        def create_metadata():
            try:
                data = request.get_json()
                metadata = data.get('metadata', {})
                upload_directory = data.get('upload_directory', '')

                if not upload_directory:
                    return jsonify({'success': False, 'error': 'Upload directory required'}), 400

                # Security check
                if not upload_directory.startswith('/media'):
                    return jsonify({'success': False, 'error': 'Directory access denied'}), 403

                uploader = get_uploader()
                result = uploader.create_metadata_file(metadata, upload_directory)
                return jsonify(result)

            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500

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
        # Config Editor Routes
        # =====================

        # Default config and active config pointer
        DEFAULT_CONFIG = 'config/DAY_SENSITIVITY_2.ini'
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
                protected_configs = ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini']
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
                    self.logger.info(f"Set as active config: {relative_path}")

                return jsonify({
                    'success': True,
                    'message': f'Configuration saved as {safe_name}',
                    'filename': safe_name,
                    'config_path': new_config_path,
                    'relative_path': relative_path,
                    'is_active': set_active,
                    'restart_required': set_active
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

                return jsonify({
                    'success': True,
                    'message': f'Active config set to {config_name}',
                    'active_config': config_name,
                    'restart_required': True
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

                return jsonify({
                    'success': True,
                    'message': f'Reset to default config: {DEFAULT_CONFIG}',
                    'active_config': DEFAULT_CONFIG,
                    'restart_required': True
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
                protected_configs = ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini']
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
        return 'config/DAY_SENSITIVITY_2.ini'

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
                    is_default = f in ['DAY_SENSITIVITY_1.ini', 'DAY_SENSITIVITY_2.ini', 'DAY_SENSITIVITY_3.ini']

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
    parser.add_argument('--config', type=str, default='../config/DAY_SENSITIVITY_2.ini', help='Config file path')

    args = parser.parse_args()

    app.run(host='0.0.0.0', port=args.port, debug=args.debug)