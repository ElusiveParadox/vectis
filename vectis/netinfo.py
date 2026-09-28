from __future__ import annotations
import ipaddress
import re
import socket
import subprocess
import uuid
from dataclasses import dataclass

@dataclass
class NetInfo:
    interface: str
    ip: str
    mac: str

def is_valid_ipv4(value: str) -> bool | str:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return 'Enter a valid IPv4 address'

def _mac_from_uuid_getnode() -> str:
    node = uuid.getnode()
    return ':'.join((f'{node >> ele & 255:02x}' for ele in range(40, -1, -8)))

def _fallback_socket() -> NetInfo:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return NetInfo(interface='default', ip=ip, mac=_mac_from_uuid_getnode())

def _macos_route_default_iface() -> str | None:
    try:
        out = subprocess.run(['route', '-n', 'get', 'default'], capture_output=True, text=True, timeout=3).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    m = re.search('interface:\\s*(\\S+)', out)
    return m.group(1) if m else None

def _macos_ifconfig(iface: str) -> NetInfo | None:
    try:
        out = subprocess.run(['ifconfig', iface], capture_output=True, text=True, timeout=3).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    ip_m = re.search('inet (\\d+\\.\\d+\\.\\d+\\.\\d+)', out)
    mac_m = re.search('ether ([0-9a-f:]{17})', out)
    if not ip_m or not mac_m:
        return None
    return NetInfo(interface=iface, ip=ip_m.group(1), mac=mac_m.group(1))

def detect() -> NetInfo:
    iface = _macos_route_default_iface()
    if iface:
        info = _macos_ifconfig(iface)
        if info:
            return info
    return _fallback_socket()
