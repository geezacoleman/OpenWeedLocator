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
import threading
import logging
import subprocess
import configparser
from pathlib import Path
from datetime import datetime
import urllib.request
import urllib.error
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.mqtt_manager import MQTTClient
from utils.upload_manager import get_uploader

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

        self.logger = logging.getLogger(__name__)
        self.load_config()
        self.setup_logging()

        self.app = Flask(__name__,
                         static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static'),
                         template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

        self.mqtt_client = MQTTClient(
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

    def load_config(self):
        """Load configuration from config file"""
        config_path = Path(self.config_file)
        if not config_path.exists():
            # Try relative to script directory
            config_path = Path(__file__).parent / self.config_file

        self.config = configparser.ConfigParser()
        if config_path.exists():
            self.config.read(config_path)
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

        @self.app.route('/api/detection/start', methods=['POST'])
        def start_detection():
            if self.controller_type == 'advanced':
                return jsonify({
                    'success': False,
                    'message': f'{self.controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_enable(True)
            self.logger.info("Detection enabled via dashboard")
            return jsonify(result)

        # Replace the existing detection/stop route
        @self.app.route('/api/detection/stop', methods=['POST'])
        def stop_detection():
            if self.controller_type != 'none':
                return jsonify({
                    'success': False,
                    'message': f'{self.controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_detection_enable(False)
            self.logger.info("Detection disabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/recording/start', methods=['POST'])
        def start_recording():
            if self.controller_type != 'none':
                return jsonify({
                    'success': False,
                    'message': f'{self.controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_image_sample_enable(True)
            self.logger.info("Recording enabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/recording/stop', methods=['POST'])
        def stop_recording():
            if self.controller_type != 'none':
                return jsonify({
                    'success': False,
                    'message': f'{self.controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.set_image_sample_enable(False)
            self.logger.info("Recording disabled via dashboard")
            return jsonify(result)

        @self.app.route('/api/sensitivity/toggle', methods=['POST'])
        def toggle_sensitivity():
            if self.controller_type != 'none':
                return jsonify({
                    'success': False,
                    'message': f'{self.controller_type.upper()} controller active - use physical switches'
                }), 423  # HTTP 423 Locked

            if not self.mqtt_client:
                return jsonify({'success': False, 'error': 'MQTT not connected'}), 500
            result = self.mqtt_client.toggle_sensitivity()
            self.logger.info("Sensitivity toggled via dashboard")
            return jsonify(result)

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
                self.logger.info(f"GPS updated: lat={lat}, lon={lon}")
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
                'sensitivity_state': mqtt_state.get('sensitivity_state', False),
                'owl_running': mqtt_state.get('owl_running', False),
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
                    log_dir = Path('../logs')
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
            try:
                # Read controller type from the loaded config
                controller_type = self.config.get('Controller', 'controller_type', fallback='none').strip(
                    "'\" ").lower()

                return jsonify({
                    'controller_type': controller_type,
                    'hardware_active': controller_type != 'none'
                })
            except Exception as e:
                self.logger.error(f"Error reading controller config: {e}")
                return jsonify({
                    'controller_type': 'none',
                    'hardware_active': False,
                    'error': str(e)
                })

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
                'usb_devices': [],
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

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