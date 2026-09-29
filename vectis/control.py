from __future__ import annotations
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
CONTROL_PORT = 9192

class _Handler(BaseHTTPRequestHandler):
    action_handler: Callable[[str], dict] = staticmethod(lambda action: {'ok': False, 'error': 'no handler registered'})

    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != '/control':
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
            result = self.action_handler(body['action'])
        except Exception as exc:
            result = {'ok': False, 'error': str(exc)}
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

def start_listener(bind_ip: str, action_handler: Callable[[str], dict]) -> ThreadingHTTPServer:
    bound = type('_BoundHandler', (_Handler,), {'action_handler': staticmethod(action_handler)})
    httpd = ThreadingHTTPServer((bind_ip, CONTROL_PORT), bound)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

def send(peer_ip: str, action: str) -> dict:
    body = json.dumps({'action': action}).encode()
    req = urllib.request.Request(f'http://{peer_ip}:{CONTROL_PORT}/control', data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except OSError as exc:
        return {'ok': False, 'error': str(exc)}
