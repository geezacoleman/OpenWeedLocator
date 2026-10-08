"""Last-resort error responder for OWL first-boot setup.

Started ONLY by systemd (OnFailure= on owl-firstboot.service) when
setup_app.py cannot start — broken venv, missing dependency, bad path.
Whatever broke the real service must not be able to break this one, so it
runs on the system python and uses the stdlib only.

Every request to the setup port gets a 503 JSON payload carrying the
crashed service's recent journal, so the phone app can show the actual
failure instead of "Failed to fetch". CORS mirrors setup_app.py's
allow-list (the app runs from file://, which sends Origin: null).
"""

import json
import os
import socket
import ssl
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SETUP_PORT = 8088
ALLOWED_ORIGINS = {'null', 'http://localhost:4180'}
TLS_CERT = '/etc/ssl/certs/owl.crt'
TLS_KEY = '/etc/ssl/private/owl.key'
FAILED_UNIT = 'owl-firstboot.service'


def journal_tail(lines=15):
    """Recent log of the crashed unit; [] when journalctl is unavailable."""
    try:
        result = subprocess.run(
            ['journalctl', '-u', FAILED_UNIT, '-n', str(lines),
             '--no-pager', '-o', 'cat'],
            capture_output=True, text=True, timeout=10)
        return result.stdout.strip().splitlines()
    except Exception:
        return []


class FallbackHandler(BaseHTTPRequestHandler):

    def _cors_headers(self):
        origin = self.headers.get('Origin')
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Setup-Session')
            self.send_header('Access-Control-Allow-Methods',
                             'GET, POST, OPTIONS')

    def _answer(self):
        detail = journal_tail()
        body = json.dumps({
            'success': False,
            'fallback': True,
            'error': 'The OWL setup service failed to start. '
                     'Power-cycle the OWL and retry; if it happens again, '
                     'the unit needs attention.',
            'detail': detail,
        }).encode('utf-8')
        self.send_response(503)
        self._cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._answer()

    def do_POST(self):
        self._answer()

    def do_OPTIONS(self):
        # Preflight must succeed or the browser swallows the real payload
        # and the app sees only a generic network error
        self.send_response(204)
        self._cors_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    # The TLS handshake runs inside accept(): bound it so one silent client
    # cannot freeze the responder (same reason as setup_app.main).
    socket.setdefaulttimeout(10)
    server = ThreadingHTTPServer(('0.0.0.0', SETUP_PORT), FallbackHandler)
    # HTTPS like setup_app.py (contract 5), so the app can read the error.
    if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(TLS_CERT, TLS_KEY)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
        except (OSError, ssl.SSLError):
            # Last-resort responder: plain HTTP beats no answer at all.
            pass
    server.serve_forever()


if __name__ == '__main__':
    main()
