#!/usr/bin/env python3
import argparse
import subprocess
import secrets
import logging
import json
import os
import getpass
import shutil
from pathlib import Path
from datetime import datetime

class OWLAuthSetup:
    def __init__(self, device_id: str, is_dashboard: bool = False, nginx_dir: str = "/etc/nginx", home_dir: str = "/home/owl"):
        self.device_id = device_id
        self.is_dashboard = is_dashboard
        self.nginx_dir = Path(os.getenv("NGINX_DIR", nginx_dir))
        self.home_dir = Path(os.getenv("HOME_DIR", home_dir))
        self.ssl_dir = self.nginx_dir / "ssl"
        self.auth_dir = self.nginx_dir / "auth"
        self.creds_file = self.home_dir / f".owl_credentials_{device_id}"
        self.default_url = "owl.local" if is_dashboard else device_id

        log_file = self.home_dir / f"owl_auth_setup_{device_id}_{datetime.now().strftime('%Y%m%d')}.log"
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger("OWL.Auth")
        os.chmod(log_file, 0o600)
        self.logger.info("Initializing OWL authentication setup")

    def _create_directories(self):
        self.logger.info("Creating directories")
        for dir_path in [self.ssl_dir, self.auth_dir, self.nginx_dir / "sites-available",
                         self.nginx_dir / "sites-enabled"]:
            if not dir_path.exists():
                self._run_command(["sudo", "mkdir", "-p", str(dir_path)])

            self._run_command(["sudo", "chmod", "750", str(dir_path)])

    def _run_command(self, command: list) -> bool:
        safe_command = []
        for item in command:
            if isinstance(item, str) and len(item) > 8 and any(
                    pwd_flag in str(command) for pwd_flag in ['password', 'psk', 'passwd']):
                safe_command.append('********')
            else:
                safe_command.append(item)

        self.logger.info(f"Running command: {' '.join(str(x) for x in safe_command)}")

        try:
            subprocess.run(command, check=True, text=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            safe_stderr = e.stderr.replace(str(command[-1]), "********") if any(
                pwd_flag in str(command) for pwd_flag in ['password', 'psk', 'passwd']) else e.stderr
            self.logger.error(f"Command failed: {' '.join(str(x) for x in safe_command)}\nError: {safe_stderr}")
            return False

    def _cleanup(self):
        self.logger.info("Cleaning up partial setup")
        for dir_path in [self.ssl_dir, self.auth_dir]:
            if dir_path.exists():
                shutil.rmtree(dir_path, ignore_errors=True)
        config_path = self.nginx_dir / f"sites-available/{self.device_id}"
        enabled_path = self.nginx_dir / f"sites-enabled/{self.device_id}"
        for path in [config_path, enabled_path]:
            if path.exists():
                path.unlink(missing_ok=True)

    def setup_hostname(self) -> bool:
        hostname = self.default_url
        self.logger.info(f"Setting up hostname: {hostname}")
        commands = [
            ["sudo", "tee", "/etc/hostname"],
            ["sudo", "sed", "-i", f"s/127.0.0.1.*/127.0.0.1 localhost {hostname}/", "/etc/hosts"],
            ["sudo", "sed", "-i", f"s/127.0.1.1.*/127.0.1.1 {hostname}/", "/etc/hosts"],  # Add for consistency
            ["sudo", "hostnamectl", "set-hostname", hostname]
        ]
        for cmd in commands:
            if cmd[1] == "tee":
                process = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True)
                process.communicate(input=hostname)
                if process.returncode != 0:
                    return False
            elif not self._run_command(cmd):
                return False
        if self.is_dashboard:
            self._run_command(["sudo", "systemctl", "enable", "avahi-daemon"])
            self._run_command(["sudo", "systemctl", "restart", "avahi-daemon"])
            self._run_command(["sudo", "systemctl", "restart", "NetworkManager"])
        return True

    def install_dependencies(self) -> bool:
        self.logger.info("Installing dependencies")
        pkgs = ["nginx", "apache2-utils", "ufw"]
        if self.is_dashboard:
            pkgs.append("avahi-daemon")
        return self._run_command(["sudo", "apt", "update"]) and \
            self._run_command(["sudo", "apt", "install", "-y"] + pkgs) and \
            self._run_command(["sudo", "ufw", "allow", "22/tcp"]) and \
            self._run_command(["sudo", "ufw", "allow", "80/tcp"]) and \
            self._run_command(["sudo", "ufw", "allow", "443/tcp"]) and \
            self._run_command(["sudo", "ufw", "--force", "enable"])

    def generate_ssl_cert(self) -> bool:
        self.logger.info("Generating SSL certificate")
        cmd = [
            "sudo", "openssl", "req", "-x509", "-nodes", "-days", "365", "-newkey", "rsa:4096",
            "-keyout", f"{self.ssl_dir}/nginx-{self.device_id}.key",
            "-out", f"{self.ssl_dir}/nginx-{self.device_id}.crt",
            "-subj", f"/CN={self.default_url}"
        ]
        success = self._run_command(cmd)
        if success:
            self._run_command(["sudo", "chmod", "600", f"{self.ssl_dir}/nginx-{self.device_id}.key"])
            self._run_command(["sudo", "chmod", "644", f"{self.ssl_dir}/nginx-{self.device_id}.crt"])
        return success

    def setup_auth(self) -> dict:
        self.logger.info("Setting up authentication")
        auth_file = self.auth_dir / f".htpasswd-{self.device_id}"
        auth_details = {"users": [], "auth_file": str(auth_file)}

        try:
            print("\nSet up authentication credentials (leave username blank to finish)")
            while True:
                username = input("Enter username (or press Enter to finish): ").strip()
                if not username:
                    if not auth_details["users"]:
                        print("At least one user is required")
                        continue
                    break
                password = getpass.getpass("Enter password (or press Enter for random): ")
                if not password:
                    password = secrets.token_urlsafe(12)
                    print(f"Generated password: {password}")
                while len(password) < 8:
                    print("Password must be at least 8 characters")
                    password = getpass.getpass("Enter password: ")
                confirm = getpass.getpass("Confirm password: ")
                if password != confirm:
                    print("Passwords do not match")
                    continue
                cmd = ["sudo", "htpasswd", "-bc" if not auth_details["users"] else "-b", str(auth_file), username, password]
                if self._run_command(cmd):
                    auth_details["users"].append({"username": username, "password": password})
                else:
                    raise Exception("Failed to add user to htpasswd")
            self._run_command(["sudo", "chmod", "640", str(auth_file)])
            return auth_details
        except KeyboardInterrupt:
            self.logger.error("Authentication setup cancelled")
            return {}
        except Exception as e:
            self.logger.error(f"Authentication setup failed: {e}")
            return {}

    def create_nginx_config(self, auth_details: dict) -> bool:
        self.logger.info("Creating nginx configuration")
        config = f"""
    limit_req_zone $binary_remote_addr zone=authlimit:10m rate=5r/m;

    server {{
        listen 443 ssl;
        server_name {self.default_url} 192.168.50.1;
        allow 192.168.50.0/24;
        deny all;

        ssl_certificate {self.ssl_dir}/nginx-{self.device_id}.crt;
        ssl_certificate_key {self.ssl_dir}/nginx-{self.device_id}.key;

        ssl_protocols TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers on;
        ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
        add_header Strict-Transport-Security "max-age=31536000" always;

        auth_basic "OWL Access";
        auth_basic_user_file {auth_details['auth_file']};

        location / {{
            limit_req zone=authlimit burst=10;
            autoindex off;
            proxy_pass http://localhost:5000;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 86400;
        }}

        location /video_feed {{
            proxy_pass http://localhost:5000/video_feed;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_buffering off;  # Critical for MJPEG streaming
            proxy_cache off;
            chunked_transfer_encoding off;
        }}
    }}

    server {{
        listen 80;
        server_name {self.default_url} 192.168.50.1;
        return 301 https://$server_name$request_uri;
    }}
    """
        try:
            config_path = self.nginx_dir / f"sites-available/{self.device_id}"
            enabled_path = self.nginx_dir / f"sites-enabled/{self.device_id}"
            with open(config_path, 'w') as f:
                f.write(config)
            if enabled_path.exists():
                self.logger.warning(f"Overwriting existing symlink at {enabled_path}")
                enabled_path.unlink()
            os.symlink(config_path, enabled_path)
            return self._run_command(["sudo", "nginx", "-t"]) and \
                self._run_command(["sudo", "systemctl", "reload", "nginx"])
        except Exception as e:
            self.logger.error(f"Failed to configure nginx: {e}")
            return False

    def save_credentials(self, auth_details: dict) -> bool:
        self.logger.info("Saving credentials")
        try:
            creds = {
                "device_id": self.device_id,
                "type": "dashboard" if self.is_dashboard else "owl",
                "url": f"https://{self.default_url}",
                "users": [{"username": user["username"]} for user in auth_details["users"]],
                "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            with open(self.creds_file, 'w') as f:
                json.dump(creds, f, indent=2)
            os.chmod(self.creds_file, 0o600)
            return True
        except Exception as e:
            self.logger.error(f"Failed to save credentials: {e}")
            return False

    def setup(self) -> dict:
        results = {
            "device_id": self.device_id,
            "type": "dashboard" if self.is_dashboard else "owl",
            "status": "failed",
            "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        self.logger.info(
            f"Setting up secure access for {'Dashboard' if self.is_dashboard else 'OWL'}: {self.device_id}")

        try:
            if not self.install_dependencies():
                raise Exception("Failed to install dependencies")
            if not self.setup_hostname():
                raise Exception("Failed to set up hostname")
            if not self.generate_ssl_cert():
                raise Exception("Failed to generate SSL certificate")
            auth_details = self.setup_auth()
            if not auth_details:
                raise Exception("Failed to set up authentication")
            if not self.create_nginx_config(auth_details):
                raise Exception("Failed to configure nginx")
            if not self.save_credentials(auth_details):
                raise Exception("Failed to save credentials")

            # Restart services to apply changes
            self._run_command(["sudo", "systemctl", "restart", "nginx"])
            self._run_command(["sudo", "systemctl", "restart", "avahi-daemon"])
            self._run_command(["sudo", "systemctl", "restart", "ssh"])

            results["status"] = "success"
            results["credentials"] = {
                "users": auth_details["users"],
                "auth_file": auth_details["auth_file"],
                "url": f"https://{self.default_url}"
            }
            self.logger.info("Setup completed successfully")
            return results

        except Exception as e:
            self.logger.error(f"Setup failed: {e}")
            self._cleanup()
            return results

def main():
    parser = argparse.ArgumentParser(description="OWL Authentication Setup")
    parser.add_argument("device_id", help="Device identifier (e.g., owl-1, dashboard)")
    parser.add_argument("--dashboard", action="store_true", help="Set up as dashboard with owl.local")
    parser.add_argument("--nginx-dir", default="/etc/nginx", help="nginx directory path")
    parser.add_argument("--home-dir", default="/home/owl", help="Home directory path")
    args = parser.parse_args()

    setup = OWLAuthSetup(
        device_id=args.device_id,
        is_dashboard=args.dashboard,
        nginx_dir=args.nginx_dir,
        home_dir=args.home_dir
    )
    results = setup.setup()

    if results["status"] == "success":
        print("\nAuthentication setup completed successfully!")
        print(f"Access your OWL at: {results['credentials']['url']}")
        print(f"Credentials saved to: {setup.creds_file}")
        print(f"To view usernames: cat {setup.creds_file}")
        print(f"To add/change users: sudo htpasswd {results['credentials']['auth_file']} <username>")
        print("Restart nginx if needed: sudo systemctl restart nginx")
        print("Note: On first visit, accept the self-signed certificate warning in your browser.")
    else:
        print("\nSetup failed. Check logs for details.")

if __name__ == "__main__":
    main()