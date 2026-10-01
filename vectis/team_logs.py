from __future__ import annotations
import json
import queue
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from . import live_logs, state
AGGREGATOR_PORT = 9193
TEAM_LOG = state.LOG_DIR / 'team.log'

def _append(ip: str, source: str, line: str) -> None:
    TEAM_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%H:%M:%S')
    with open(TEAM_LOG, 'a') as f:
        f.write(f'{ts}  {ip:>15}  {source:<14}| {line}\n')

class _Handler(BaseHTTPRequestHandler):

    def log_message(self, *a) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != '/log':
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
            _append(body['ip'], body['source'], body['line'])
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()

def start_aggregator(bind_ip: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((bind_ip, AGGREGATOR_PORT), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

def _push(dns_ip: str, ip: str, source: str, line: str) -> None:
    body = json.dumps({'ip': ip, 'source': source, 'line': line}).encode()
    req = urllib.request.Request(f'http://{dns_ip}:{AGGREGATOR_PORT}/log', data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=2):
            pass
    except OSError:
        pass

def start_forwarding(role: str, dns_ip: str, self_ip: str) -> threading.Event:
    stop = threading.Event()
    sources = live_logs.SOURCES_BY_ROLE.get(role, {})
    out: 'queue.Queue[tuple[str, str]]' = queue.Queue()
    for name, path in sources.items():
        threading.Thread(target=live_logs._follow, args=(name, path, out, stop), daemon=True).start()

    def consume() -> None:
        while not stop.is_set():
            try:
                name, line = out.get(timeout=1)
            except queue.Empty:
                continue
            _push(dns_ip, self_ip, name, line)
    threading.Thread(target=consume, daemon=True).start()
    return stop
