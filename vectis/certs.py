from __future__ import annotations
import http.server
import shutil
import subprocess
import threading
from pathlib import Path
from . import state, ui
CERT_DIR = state.STATE_DIR / 'certs'
PULLED_DIR = state.STATE_DIR / 'certs' / 'pulled'

def pulled_path(domain: str) -> Path:
    return PULLED_DIR / f'{domain}.crt'

def is_pulled(domain: str) -> bool:
    return pulled_path(domain).exists()

def is_trusted(domain: str) -> bool:
    path = pulled_path(domain)
    if not path.exists():
        return False
    result = subprocess.run(['security', 'verify-cert', '-c', str(path)], capture_output=True, text=True)
    return result.returncode == 0

def generate(domain: str) -> tuple[Path, Path]:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    crt = CERT_DIR / f'{domain}.crt'
    key = CERT_DIR / f'{domain}.key'
    with ui.spinner(f'Generating certificate for {domain}...'):
        if shutil.which('mkcert'):
            subprocess.run(['mkcert', '-cert-file', str(crt), '-key-file', str(key), domain, f'api.{domain}'], check=True)
        else:
            subprocess.run(['openssl', 'req', '-x509', '-nodes', '-newkey', 'rsa:2048', '-days', '825', '-keyout', str(key), '-out', str(crt), '-subj', f'/CN={domain}', '-addext', f'subjectAltName=DNS:{domain},DNS:api.{domain}'], check=True)
    return (crt, key)

class CertServer:

    def __init__(self, httpd: http.server.ThreadingHTTPServer, thread: threading.Thread, ip: str, port: int, filename: str):
        self.httpd = httpd
        self.thread = thread
        self.pull_command = f'curl --create-dirs -o "$HOME/.config/vectis/certs/pulled/{filename}" http://{ip}:{port}/{filename}'

    def stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)

def publish(crt_path: Path, bind_ip: str, port: int=9190) -> CertServer:
    serve_dir = state.STATE_DIR / 'cert_publish'
    serve_dir.mkdir(parents=True, exist_ok=True)
    published = serve_dir / crt_path.name
    published.write_bytes(crt_path.read_bytes())

    def handler_factory(*a, **kw):
        return http.server.SimpleHTTPRequestHandler(*a, directory=str(serve_dir), **kw)
    httpd = http.server.ThreadingHTTPServer((bind_ip, port), handler_factory)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return CertServer(httpd, thread, bind_ip, port, crt_path.name)

def trust_locally(crt_path: Path) -> bool:
    with ui.spinner('Adding certificate to login keychain...'):
        result = subprocess.run(['security', 'add-trusted-cert', '-d', '-r', 'trustRoot', '-k', str(Path.home() / 'Library' / 'Keychains' / 'login.keychain-db'), str(crt_path)], capture_output=True, text=True)
    return result.returncode == 0

def trust(domain: str) -> tuple[bool, str]:
    path = pulled_path(domain)
    if not path.exists():
        return (False, f'No cert found at {path}. Pull it first (the edge machine prints the exact command).')
    if is_trusted(domain):
        return (True, f'{domain} is already trusted.')
    ok = trust_locally(path)
    return (ok, f'{domain} is now trusted.' if ok else 'security add-trusted-cert failed -- see stderr above.')
