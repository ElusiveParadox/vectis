from __future__ import annotations
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from .. import state
from .base import Role, SetupResult
APP_FILE = Path(__file__).parent.parent / 'templates' / 'backend_app.py'
PORTS = {'A': 3001, 'B': 3002}

class BackendRole(Role):

    def __init__(self, backend_id: str):
        self.backend_id = backend_id
        self.name = f'backend_{backend_id.lower()}'
        self.port = PORTS[backend_id]

    def collect_peers(self) -> dict[str, str]:
        return {}

    def configure(self, peers: dict[str, str]) -> None:
        log_path = state.LOG_DIR / f'backend_{self.backend_id.lower()}.log'
        log_file = open(log_path, 'a')
        proc = subprocess.Popen([sys.executable, str(APP_FILE)], env={'BACKEND_ID': self.backend_id, 'PORT': str(self.port), 'PATH': '/usr/bin:/bin'}, stdout=log_file, stderr=subprocess.STDOUT)
        data = state.load()
        data['services'][self.name] = {'pid': proc.pid, 'started': time.time(), 'port': self.port}
        state.save(data)
        time.sleep(0.5)

    def smoke_test(self) -> SetupResult:
        checks: dict[str, bool] = {}
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port}/api/status', timeout=3) as r:
                checks['api_status_reachable'] = r.status == 200
                checks['x_backend_header_present'] = 'X-Backend' in r.headers
        except OSError:
            checks['api_status_reachable'] = False
            checks['x_backend_header_present'] = False
        return SetupResult(ok=all(checks.values()), checks=checks, messages=[])
