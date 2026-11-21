"""Port preflight: a stale vectis process still holding a port (a setup
that was Ctrl+C'd or got stuck, or an orphaned backend) causes confusing
failures -- worst case the smoke test passes against the OLD process.
Stale vectis processes are stopped; anything else is reported by name."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time

from . import state

AGENT_PORTS = {"control": 9192}
PORTS_BY_ROLE: dict[str, dict[int, str]] = {
    state.ROLE_DNS: {9191: "status dashboard", 9193: "log aggregator", 9192: "control"},
    state.ROLE_EDGE: {9190: "certificate server", 9192: "control"},
    state.ROLE_BACKEND_A: {3001: "backend A", 9192: "control"},
    state.ROLE_BACKEND_B: {3002: "backend B", 9192: "control"},
    state.ROLE_BACKENDS: {3001: "backend A", 3002: "backend B", 9192: "control"},
    state.ROLE_DNS_EDGE: {9190: "certificate server", 9191: "status dashboard", 9193: "log aggregator", 9192: "control"},
}


def _bind_fails(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _owners(port: int) -> list[tuple[int, str]]:
    if not shutil.which("lsof"):
        return []
    out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True).stdout
    result = []
    for tok in out.split():
        pid = int(tok)
        if pid == os.getpid():
            continue
        args = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        result.append((pid, args))
    return result


def _is_ours(args: str) -> bool:
    return "vectis" in args or "backend_app.py" in args


def _stop(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.2)


def ensure_free(role_id: str) -> tuple[list[str], list[str]]:
    """Returns (notes about stale vectis processes stopped, problems that
    need the user). Empty problems = safe to continue."""
    notes: list[str] = []
    problems: list[str] = []
    for port, what in PORTS_BY_ROLE.get(role_id, {}).items():
        if not _bind_fails(port):
            continue
        owners = _owners(port)
        if not owners:
            if shutil.which("lsof"):
                continue
            problems.append(f"port {port} ({what}) is in use and `lsof` is not available to say by what")
            continue
        for pid, args in owners:
            if _is_ours(args):
                try:
                    _stop(pid)
                    notes.append(f"stopped a previous vectis process (pid {pid}) that was holding port {port} ({what})")
                except (ProcessLookupError, PermissionError) as exc:
                    problems.append(f"port {port} ({what}): could not stop pid {pid}: {exc}")
            else:
                problems.append(f"port {port} ({what}) is used by pid {pid}: {args if len(args) <= 100 else '...' + args[-97:]} -- stop it, then re-run setup")
        if _bind_fails(port) and not any(f"port {port}" in p for p in problems):
            problems.append(f"port {port} ({what}) is still busy after stopping the old process")
    return notes, problems


SOCKET_CONNECT_TIMEOUT = 1.0
