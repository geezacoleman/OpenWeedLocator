#!/usr/bin/env python3
"""Quick diagnostic: listen on port 8500 and print raw Teltonika GPS data.

Usage:
    sudo systemctl stop owl-controller
    python3 tools/test_gps_port.py
    # Ctrl+C when done, then:
    sudo systemctl start owl-controller
"""
import socket
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8500

s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', PORT))
s.listen(1)
print(f'Listening on port {PORT}... waiting for Teltonika to connect')
print('(Ctrl+C to stop)\n')

try:
    conn, addr = s.accept()
    print(f'Connected from {addr}\n')
    while True:
        data = conn.recv(4096)
        if not data:
            print('\n-- Connection closed by remote --')
            break
        text = data.decode('ascii', errors='replace')
        for line in text.strip().splitlines():
            if line.startswith('$'):
                parts = line.split(',')
                print(f'  OK  {parts[0]:10s}  {line[:80]}')
            else:
                print(f'  ??  {repr(line[:80])}')
except KeyboardInterrupt:
    print('\nStopped.')
finally:
    s.close()
