from __future__ import annotations
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from . import state
HEARTBEAT_PORT = 9191
HEARTBEAT_INTERVAL_S = 5
STALE_AFTER_S = HEARTBEAT_INTERVAL_S * 3

def push_once(dns_ip: str, role: str, payload: dict) -> bool:
    body = json.dumps({'role': role, 'ts': time.time(), **payload}).encode()
    req = urllib.request.Request(f'http://{dns_ip}:{HEARTBEAT_PORT}/report', data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=3):
            return True
    except OSError:
        return False

def start_heartbeat_loop(dns_ip: str, role: str, health_fn: Callable[[], dict]) -> threading.Thread:

    def loop():
        while True:
            push_once(dns_ip, role, health_fn())
            time.sleep(HEARTBEAT_INTERVAL_S)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t

class _Handler(BaseHTTPRequestHandler):

    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != '/report':
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        try:
            report = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        data = state.load()
        data['heartbeat'][report['role']] = report
        state.save(data)
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != '/status':
            self.send_response(404)
            self.end_headers()
            return
        data = state.load()
        now = time.time()
        rows = []
        for role, hb in sorted(data['heartbeat'].items()):
            stale = now - hb['ts'] > STALE_AFTER_S
            status = 'stale' if stale else 'ok' if hb.get('ok') else 'fault'
            seen = time.strftime('%H:%M:%S', time.localtime(hb['ts']))
            rows.append(f'<tr><td>{role}</td><td>{status}</td><td>{seen}</td></tr>')
        html = '<html><body><h3>vectis team status</h3><table border=1 cellpadding=6><tr><th>role</th><th>status</th><th>last seen</th></tr>' + ''.join(rows) + '</table></body></html>'
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

def start_aggregator(bind_ip: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind_ip, HEARTBEAT_PORT), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
