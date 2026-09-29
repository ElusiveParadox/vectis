from __future__ import annotations
import os
import subprocess
from pathlib import Path
RESOLVER_DIR = Path(os.environ.get('VECTIS_RESOLVER_DIR', '/etc/resolver'))

def resolver_path(domain: str) -> Path:
    return RESOLVER_DIR / domain

def is_enabled(domain: str) -> bool:
    return resolver_path(domain).exists()

def enable(domain: str, dns_ip: str) -> tuple[bool, str]:
    path = resolver_path(domain)
    content = f'nameserver {dns_ip}\n'
    try:
        RESOLVER_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return (True, f'Browser lookups for *.{domain} now resolve via {dns_ip}.')
    except PermissionError:
        pass
    subprocess.run(['sudo', '-v'], check=True)
    script = f'mkdir -p {RESOLVER_DIR} && printf "nameserver {dns_ip}\\n" > {path}'
    result = subprocess.run(['sudo', 'sh', '-c', script], capture_output=True, text=True)
    if result.returncode != 0:
        return (False, result.stderr.strip() or 'sudo command failed')
    return (True, f'Browser lookups for *.{domain} now resolve via {dns_ip}.')

def disable(domain: str) -> tuple[bool, str]:
    path = resolver_path(domain)
    if not path.exists():
        return (True, f'No resolver entry for {domain} to remove.')
    try:
        path.unlink()
        return (True, f'Removed {path}.')
    except PermissionError:
        pass
    subprocess.run(['sudo', '-v'], check=True)
    result = subprocess.run(['sudo', 'rm', '-f', str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        return (False, result.stderr.strip() or 'sudo command failed')
    return (True, f'Removed {path}.')
