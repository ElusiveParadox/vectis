import shutil
import socket
import subprocess
import sys
import time

import pytest

from vectis import ports, state

pytestmark = pytest.mark.skipif(not shutil.which("lsof"), reason="needs lsof")


def _listener(tmp_path, name, port):
    f = tmp_path / name
    f.write_text(f"import socket,time\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
                 f"s.bind(('0.0.0.0',{port}));s.listen();time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, str(f)])
    for _ in range(50):
        if ports._bind_fails(port):
            break
        time.sleep(0.1)
    return p


def test_stale_backend_is_stopped(tmp_path):
    p = _listener(tmp_path, "backend_app.py", 3001)
    try:
        notes, problems = ports.ensure_free(state.ROLE_BACKEND_A)
        assert problems == [] and any("3001" in n for n in notes)
        p.wait(timeout=5)
    finally:
        p.kill()


def test_foreign_process_is_reported_not_killed(tmp_path):
    p = _listener(tmp_path, "someone_else.py", 3002)
    try:
        notes, problems = ports.ensure_free(state.ROLE_BACKEND_B)
        assert notes == [] and any("3002" in x and "someone_else.py" in x for x in problems)
        assert p.poll() is None
    finally:
        p.kill()
