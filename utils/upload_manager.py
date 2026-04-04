#!/usr/bin/env python3
"""
Upload Manager for OWL Dashboard
"""

import threading
import time
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict
from datetime import datetime
import logging

try:
    import boto3
    from botocore.config import Config
    from boto3.s3.transfer import TransferConfig

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False


class UploadProgress:
    """Track upload progress across multiple files"""

    def __init__(self):
        self.total_files = 0
        self.completed_files = 0
        self.failed_files = 0
        self.total_bytes = 0
        self.uploaded_bytes = 0
        self.current_file = ""
        self.status = "idle"  # idle, running, completed, failed, cancelled
        self.error_message = ""
        self.start_time = None
        self.end_time = None
        self.lock = threading.RLock()

    def update_file_progress(self, bytes_amount):
        """Called during file upload progress"""
        with self.lock:
            self.uploaded_bytes += bytes_amount

    def complete_file(self, filename, success=True):
        """Mark a file as completed"""
        with self.lock:
            self.completed_files += 1
            if not success:
                self.failed_files += 1
            self.current_file = filename

    def get_progress_dict(self):
        """Get current progress as dictionary"""
        with self.lock:
            elapsed = 0
            speed_mbps = 0
            eta_seconds = 0

            if self.start_time:
                elapsed = time.time() - self.start_time
                if elapsed > 0:
                    speed_mbps = (self.uploaded_bytes / (1024 * 1024)) / elapsed
                    if speed_mbps > 0 and self.total_bytes > self.uploaded_bytes:
                        remaining_bytes = self.total_bytes - self.uploaded_bytes
                        eta_seconds = remaining_bytes / (speed_mbps * 1024 * 1024)

            return {
                'status': self.status,
                'total_files': self.total_files,
                'completed_files': self.completed_files,
                'failed_files': self.failed_files,
                'total_bytes': self.total_bytes,
                'uploaded_bytes': self.uploaded_bytes,
                'current_file': self.current_file,
                'progress_percent': (self.uploaded_bytes / self.total_bytes * 100) if self.total_bytes > 0 else 0,
                'speed_mbps': speed_mbps,
                'elapsed_seconds': elapsed,
                'eta_seconds': eta_seconds,
                'error_message': self.error_message
            }


class S3Uploader:
    """S3 uploader with progress tracking for dashboard"""

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.progress = UploadProgress()
        self.upload_thread = None
        self.stop_upload = threading.Event()

        if not BOTO3_AVAILABLE:
            raise ImportError("boto3 not available. Install with: pip install boto3")

    def check_ethernet_connection(self) -> Dict[str, any]:
        """Check if ethernet connection is available"""
        try:
            # Find the ip command location
            ip_command = None
            for path in ['/sbin/ip', '/usr/sbin/ip', '/bin/ip', '/usr/bin/ip']:
                if Path(path).exists():
                    ip_command = path
                    break

            if not ip_command:
                return {'connected': False, 'error': 'ip command not found'}

            # Check network interfaces
            result = subprocess.run([ip_command, 'route'], capture_output=True, text=True, timeout=5)
            if result.returncode != 0:
                return {'connected': False, 'error': 'Failed to check network routes'}

            has_default_route = 'default' in result.stdout

            ping_command = None
            for path in ['/bin/ping', '/usr/bin/ping', '/sbin/ping', '/usr/sbin/ping']:
                if Path(path).exists():
                    ping_command = path
                    break

            if not ping_command:
                return {
                    'connected': has_default_route,
                    'has_route': has_default_route,
                    'can_ping': False,
                    'error': 'ping command not found' if has_default_route else 'No default route and ping not found'
                }

            # Try to ping a reliable server
            ping_result = subprocess.run([ping_command, '-c', '1', '-W', '3', '8.8.8.8'],
                                         capture_output=True, timeout=10)
            can_ping = ping_result.returncode == 0

            return {
                'connected': has_default_route and can_ping,
                'has_route': has_default_route,
                'can_ping': can_ping,
                'error': None if (has_default_route and can_ping) else 'No internet connectivity'
            }

        except subprocess.TimeoutExpired:
            return {'connected': False, 'error': 'Network check timed out'}
        except Exception as e:
            return {'connected': False, 'error': str(e)}

    def test_s3_credentials(self, access_key: str, secret_key: str,
                            bucket_name: str, region: str = 'us-east-1',
                            endpoint_url: Optional[str] = None) -> Dict[str, any]:
        """Test S3 credentials and bucket access"""
        try:
            # Configure S3 client
            config = Config(
                signature_version='s3v4',
                s3={'addressing_style': 'path'} if endpoint_url else {}
            )

            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            )

            s3_client = session.client('s3', endpoint_url=endpoint_url, config=config)

            # Test bucket access
            s3_client.head_bucket(Bucket=bucket_name)

            # Try to list a few objects to verify permissions
            response = s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=1)

            return {
                'valid': True,
                'error': None,
                'bucket_exists': True,
                'can_list': True
            }

        except Exception as e:
            error_msg = str(e)
            if 'NoSuchBucket' in error_msg:
                return {'valid': False, 'error': 'Bucket does not exist', 'bucket_exists': False}
            elif 'InvalidAccessKeyId' in error_msg:
                return {'valid': False, 'error': 'Invalid access key', 'bucket_exists': None}
            elif 'SignatureDoesNotMatch' in error_msg:
                return {'valid': False, 'error': 'Invalid secret key', 'bucket_exists': None}
            elif 'AccessDenied' in error_msg:
                return {'valid': False, 'error': 'Access denied - check permissions', 'bucket_exists': True}
            else:
                return {'valid': False, 'error': f'Connection failed: {error_msg}', 'bucket_exists': None}

    def scan_directory(self, directory_path: str, preview_only: bool = False) -> Dict[str, any]:
        """Scan directory and return file information"""
        try:
            path = Path(directory_path)
            if not path.exists() or not path.is_dir():
                return {'success': False, 'error': f'Directory does not exist: {directory_path}'}

            files = []
            total_size = 0

            for file_path in path.rglob("*"):
                if file_path.is_file():
                    try:
                        size = file_path.stat().st_size
                        files.append({
                            'path': str(file_path),
                            'name': file_path.name,
                            'size': size,
                            'relative_path': str(file_path.relative_to(path))
                        })
                        total_size += size
                    except (OSError, IOError):
                        continue

            preview_files = files[:100] if preview_only else files

            return {
                'success': True,
                'file_count': len(files),  # Always show total count
                'total_size': total_size,
                'files': preview_files,  # Limited for preview, all for upload
                'total_size_formatted': self._format_size(total_size)
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    def start_upload(self, directory_path: str, access_key: str, secret_key: str,
                     bucket_name: str, s3_prefix: str = "", region: str = 'us-east-1',
                     endpoint_url: Optional[str] = None, max_workers: int = 4) -> bool:
        """Start upload in background thread"""

        if self.upload_thread and self.upload_thread.is_alive():
            return False  # Upload already in progress

        # Reset progress
        self.progress = UploadProgress()
        self.stop_upload.clear()

        # Start upload thread
        self.upload_thread = threading.Thread(
            target=self._upload_worker,
            args=(directory_path, access_key, secret_key, bucket_name, s3_prefix,
                  region, endpoint_url, max_workers),
            daemon=True
        )
        self.upload_thread.start()
        return True

    def find_key_files(self) -> Dict[str, any]:
        """Find credential files on USB drives"""
        try:
            key_files = []

            # Search for secret_key folders on USB drives
            media_path = Path('/media')
            if not media_path.exists():
                return {'success': False, 'error': 'No media directory found'}

            for user_dir in media_path.iterdir():
                if user_dir.is_dir():
                    for mount_dir in user_dir.iterdir():
                        if mount_dir.is_dir():
                            # Look for secret_key folder
                            secret_dir = mount_dir / 'secret_key'
                            if secret_dir.exists() and secret_dir.is_dir():
                                # Find credential files
                                for file_path in secret_dir.iterdir():
                                    if file_path.is_file() and file_path.suffix.lower() in ['.txt', '.env', '.key']:
                                        try:
                                            # Try to parse the file to see if it contains keys
                                            content = file_path.read_text().strip()
                                            if any(key in content.upper() for key in
                                                   ['ACCESS_KEY', 'SECRET_KEY', 'BUCKET']):
                                                key_files.append({
                                                    'name': file_path.name,
                                                    'path': str(file_path),
                                                    'size': file_path.stat().st_size,
                                                    'modified': datetime.fromtimestamp(
                                                        file_path.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                                                    'usb_device': str(mount_dir)
                                                })
                                        except Exception as e:
                                            self.logger.debug(f"Could not read {file_path}: {e}")
                                            continue

            return {
                'success': True,
                'key_files': key_files,
                'count': len(key_files)
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    def load_credentials_from_file(self, file_path: str) -> Dict[str, any]:
        """Load S3 credentials from a file"""
        try:
            if not file_path.startswith('/media'):
                return {'success': False, 'error': 'File access denied - must be on USB drive'}

            file_path_obj = Path(file_path)
            if not file_path_obj.exists():
                return {'success': False, 'error': 'File not found'}

            # Read and parse the file
            content = file_path_obj.read_text().strip()
            credentials = {}

            # Parse different formats
            for line in content.split('\n'):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                # Handle KEY=value format
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip().upper()
                    value = value.strip().strip('"\'')

                    if key in ['ACCESS_KEY', 'AWS_ACCESS_KEY_ID', 'S3_ACCESS_KEY']:
                        credentials['access_key'] = value
                    elif key in ['SECRET_KEY', 'AWS_SECRET_ACCESS_KEY', 'S3_SECRET_KEY']:
                        credentials['secret_key'] = value
                    elif key in ['BUCKET_NAME', 'BUCKET', 'S3_BUCKET']:
                        credentials['bucket_name'] = value
                    elif key in ['REGION', 'AWS_REGION', 'S3_REGION']:
                        credentials['region'] = value
                    elif key in ['ENDPOINT_URL', 'ENDPOINT', 'S3_ENDPOINT']:
                        credentials['endpoint_url'] = value

            # Validate required fields
            required_fields = ['access_key', 'secret_key', 'bucket_name']
            missing_fields = [field for field in required_fields if field not in credentials]

            if missing_fields:
                return {
                    'success': False,
                    'error': f'Missing required fields: {", ".join(missing_fields)}'
                }

            # Set defaults
            if 'region' not in credentials:
                credentials['region'] = 'us-east-1'

            return {
                'success': True,
                'credentials': credentials,
                'file_path': file_path
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    def create_metadata_file(self, metadata: Dict[str, str], upload_directory: str) -> Dict[str, any]:
        """Create a metadata file for the upload"""
        try:
            # Create metadata content
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            metadata_content = f"""# OWL Upload Metadata
    # Generated: {timestamp}

    [Upload Information]
    Name = {metadata.get('name', '')}
    Date = {metadata.get('date', '')}
    Location = {metadata.get('location', '')}
    Field = {metadata.get('field', '')}
    Weather = {metadata.get('weather', '')}
    Crop = {metadata.get('crop', '')}
    Expected Weeds = {metadata.get('expected_weeds', '')}
    Additional Notes = {metadata.get('notes', '')}

    [Technical Information]
    Upload Timestamp = {timestamp}
    Source Directory = {upload_directory}
    Generated By = OWL Dashboard
    """

            # Save metadata file in the upload directory
            metadata_path = Path(upload_directory) / 'upload_metadata.txt'
            metadata_path.write_text(metadata_content)

            return {
                'success': True,
                'metadata_file': str(metadata_path),
                'content': metadata_content
            }

        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _upload_worker(self, directory_path: str, access_key: str, secret_key: str,
                       bucket_name: str, s3_prefix: str, region: str,
                       endpoint_url: Optional[str], max_workers: int):
        """Background worker for upload"""
        try:
            self.progress.status = "running"
            self.progress.start_time = time.time()

            scan_result = self.scan_directory(directory_path, preview_only=False)
            if not scan_result['success']:
                raise Exception(scan_result['error'])

            files = scan_result['files']
            self.progress.total_files = len(files)
            self.progress.total_bytes = scan_result['total_size']

            self.logger.info(f"Starting upload of {len(files)} files ({self._format_size(scan_result['total_size'])})")

            if len(files) == 0:
                self.progress.status = "completed"
                self.progress.end_time = time.time()
                return

            # Configure S3 client
            config = Config(
                max_pool_connections=max_workers * 2,
                signature_version='s3v4',
                s3={'addressing_style': 'path'} if endpoint_url else {}
            )

            session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            )

            s3_client = session.client('s3', endpoint_url=endpoint_url, config=config)
            transfer_config = TransferConfig(use_threads=False)

            # Upload files with progress tracking
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_file = {}

                for file_info in files:
                    if self.stop_upload.is_set():
                        break

                    local_path = file_info['path']
                    s3_key = f"{s3_prefix.rstrip('/')}/{file_info['relative_path']}" if s3_prefix else file_info[
                        'relative_path']

                    future = executor.submit(
                        self._upload_file,
                        s3_client, local_path, bucket_name, s3_key, transfer_config
                    )
                    future_to_file[future] = file_info

                # Process completed uploads
                for future in as_completed(future_to_file):
                    if self.stop_upload.is_set():
                        break

                    file_info = future_to_file[future]
                    try:
                        future.result()
                        self.progress.complete_file(file_info['name'], success=True)
                        if self.progress.completed_files % 100 == 0:  # Log every 100 files
                            self.logger.info(
                                f"Uploaded {self.progress.completed_files}/{self.progress.total_files} files")
                    except Exception as e:
                        self.logger.error(f"Failed to upload {file_info['name']}: {e}")
                        self.progress.complete_file(file_info['name'], success=False)

            # Update final status
            if self.stop_upload.is_set():
                self.progress.status = "cancelled"
            elif self.progress.failed_files > 0:
                self.progress.status = "completed_with_errors"
            else:
                self.progress.status = "completed"

            self.progress.end_time = time.time()
            self.logger.info(
                f"Upload completed: {self.progress.completed_files - self.progress.failed_files} successful, {self.progress.failed_files} failed")

        except Exception as e:
            self.logger.error(f"Upload failed: {e}")
            self.progress.status = "failed"
            self.progress.error_message = str(e)
            self.progress.end_time = time.time()

    def _upload_file(self, s3_client, local_path: str, bucket_name: str,
                     s3_key: str, transfer_config):
        """Upload a single file"""

        def progress_callback(bytes_amount):
            self.progress.update_file_progress(bytes_amount)

        s3_client.upload_file(
            local_path, bucket_name, s3_key,
            Config=transfer_config,
            Callback=progress_callback
        )

    def stop_upload_process(self):
        """Stop the current upload"""
        self.stop_upload.set()

    def get_progress(self) -> Dict[str, any]:
        """Get current upload progress"""
        return self.progress.get_progress_dict()

    def _format_size(self, size_bytes: int) -> str:
        """Format file size in human readable format"""
        if size_bytes == 0:
            return "0 B"
        size_names = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size_bytes >= 1024.0 and i < len(size_names) - 1:
            size_bytes /= 1024.0
            i += 1
        return f"{size_bytes:.1f} {size_names[i]}"


# Global uploader instance for the dashboard
uploader_instance = None


def get_uploader():
    """Get or create the global uploader instance"""
    global uploader_instance
    if uploader_instance is None:
        uploader_instance = S3Uploader()
    return uploader_instance