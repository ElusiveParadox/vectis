from __future__ import annotations
import http.server
import os
import shlex
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from . import state, ui
STATIC_PORT = 8080
SERVICE_LOG = state.LOG_DIR / 'hosted_service.log'

def host_static(folder: str, port: int=STATIC_PORT) -> http.server.ThreadingHTTPServer:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f'{root} is not a directory')

    def handler_factory(*a, **kw):
        return http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **kw)
    httpd = http.server.ThreadingHTTPServer(('0.0.0.0', port), handler_factory)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state.update(services={**state.load()['services'], 'hosted_static': {'port': port, 'root': str(root)}})
    return httpd

def host_monorepo_service(cwd: str, command: str, port: int) -> int:
    workdir = Path(cwd).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f'{workdir} is not a directory')
    SERVICE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(SERVICE_LOG, 'a')
    proc = subprocess.Popen(shlex.split(command), cwd=str(workdir), env={**os.environ, 'PORT': str(port)}, stdout=log_file, stderr=subprocess.STDOUT)
    data = state.load()
    data['services']['hosted_service'] = {'pid': proc.pid, 'started': time.time(), 'port': port, 'cwd': str(workdir), 'command': command}
    state.save(data)
    return proc.pid

def check_reachable(port: int, path: str='/', timeout: float=3.0) -> bool:
    with ui.spinner(f'Checking http://127.0.0.1:{port}{path} ...'):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=timeout) as r:
                return r.status < 500
        except OSError:
            return False
