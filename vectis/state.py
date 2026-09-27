from __future__ import annotations
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
STATE_DIR = Path(os.environ.get('VECTIS_HOME', Path.home() / '.config' / 'vectis'))
STATE_FILE = STATE_DIR / 'state.json'
LOG_DIR = STATE_DIR / 'logs'
DEFAULT_STATE: dict[str, Any] = {'domain': None, 'role': None, 'self': {'ip': None, 'mac': None, 'iface': None}, 'peers': {}, 'services': {}, 'last_smoke_test': None, 'heartbeat': {}}
ROLE_DNS = 'dns'
ROLE_EDGE = 'edge'
ROLE_BACKEND_A = 'backend_a'
ROLE_BACKEND_B = 'backend_b'
ROLE_DNS_EDGE = 'dns_edge'
ROLE_BACKENDS = 'backends'
ALL_ROLES = [ROLE_DNS, ROLE_EDGE, ROLE_BACKEND_A, ROLE_BACKEND_B, ROLE_DNS_EDGE, ROLE_BACKENDS]
SERVER_ROLES = {ROLE_DNS, ROLE_EDGE, ROLE_DNS_EDGE}
DASHBOARD_ROLES = {ROLE_DNS, ROLE_DNS_EDGE}
_lock = threading.RLock()

def load() -> dict[str, Any]:
    with _lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not STATE_FILE.exists():
            save(dict(DEFAULT_STATE))
        with open(STATE_FILE) as f:
            data = json.load(f)
        for k, v in DEFAULT_STATE.items():
            data.setdefault(k, v)
        return data

def save(data: dict[str, Any]) -> None:
    with _lock:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(STATE_FILE)

def update(**kwargs: Any) -> dict[str, Any]:
    data = load()
    data.update(kwargs)
    save(data)
    return data

def set_peer(name: str, ip: str) -> None:
    data = load()
    data['peers'][name] = ip
    save(data)

def record_smoke_test(passed: bool, checks: dict[str, bool]) -> None:
    update(last_smoke_test={'passed': passed, 'checks': checks, 'ts': time.time()})
