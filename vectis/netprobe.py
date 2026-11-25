
from __future__ import annotations

import http.client
import random
import socket
import ssl
import struct
import time
from dataclasses import dataclass, field


@dataclass
class DnsResult:
    ok: bool
    addrs: list[str] = field(default_factory=list)
    ttl: int | None = None
    rcode: int | None = None
    rtt_ms: float = 0.0
    error: str | None = None


def dns_query_a(name: str, server: str, port: int = 53, timeout: float = 2.0) -> DnsResult:
    qid = random.randrange(0, 65536)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split(".")) + b"\x00"
    packet = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    t0 = time.perf_counter()
    try:
        s.sendto(packet, (server, port))
        while True:
            data, _ = s.recvfrom(4096)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid:
                break
    except OSError as exc:
        return DnsResult(False, error=f"no DNS answer from {server}:{port} ({exc})")
    finally:
        s.close()
    rtt = (time.perf_counter() - t0) * 1000
    try:
        _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
        rcode = flags & 0xF
        pos = 12
        for _ in range(qd):
            pos = _skip_name(data, pos) + 4
        addrs, ttl = [], None
        for _ in range(an):
            pos = _skip_name(data, pos)
            rtype, _, rttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
            pos += 10
            if rtype == 1 and rdlen == 4:
                addrs.append(socket.inet_ntoa(data[pos:pos + 4]))
                ttl = rttl if ttl is None else min(ttl, rttl)
            pos += rdlen
    except (struct.error, IndexError):
        return DnsResult(False, rtt_ms=rtt, error="malformed DNS response")
    if rcode != 0:
        return DnsResult(False, rcode=rcode, rtt_ms=rtt, error=f"DNS error rcode={rcode} (3=NXDOMAIN, 5=REFUSED)")
    if not addrs:
        return DnsResult(False, rcode=rcode, rtt_ms=rtt, error="DNS answered with no A record")
    return DnsResult(True, addrs, ttl, rcode, rtt)


def _skip_name(data: bytes, pos: int) -> int:
    while True:
        n = data[pos]
        if n == 0:
            return pos + 1
        if n & 0xC0 == 0xC0:
            return pos + 2
        pos += 1 + n


@dataclass
class HttpResult:
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    connect_ms: float = 0.0
    tls_ms: float | None = None
    ttfb_ms: float = 0.0
    total_ms: float = 0.0
    tls_version: str | None = None
    tls_cipher: str | None = None
    tls_verified: bool | None = None
    cert_sans: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400


def tls_context(verify: bool, cafile: str | None = None) -> ssl.SSLContext:
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    if cafile:
        ctx.load_verify_locations(cafile)
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


def http_fetch(ip: str, port: int, path: str = "/", *, host: str | None = None, tls: bool = False,
               verify: bool = False, cafile: str | None = None, timeout: float = 3.0,
               headers: dict[str, str] | None = None) -> HttpResult:
    """Connect to ip:port (NOT to `host` -- the caller decides which IP, so
    we can test one specific machine) and speak HTTP/1.1 with Host: host."""
    host = host or ip
    r = HttpResult()
    t0 = time.perf_counter()
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
        t1 = time.perf_counter()
        r.connect_ms = (t1 - t0) * 1000
        if tls:
            try:
                sock = tls_context(verify, cafile).wrap_socket(sock, server_hostname=host)
            except ssl.SSLCertVerificationError as exc:
                r.error = f"TLS verification failed: {exc.verify_message}"
                r.tls_verified = False
                return r
            t2 = time.perf_counter()
            r.tls_ms = (t2 - t1) * 1000
            r.tls_version = sock.version()
            r.tls_cipher = sock.cipher()[0] if sock.cipher() else None
            r.tls_verified = verify
            if verify:
                cert = sock.getpeercert() or {}
                r.cert_sans = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
        hdrs = {"Host": host, "Connection": "close", "User-Agent": "vectis", "Accept": "text/html,application/json"}
        hdrs.update(headers or {})
        req = f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n"
        t3 = time.perf_counter()
        sock.sendall(req.encode())
        resp = http.client.HTTPResponse(sock)
        resp.begin()
        r.ttfb_ms = (time.perf_counter() - t3) * 1000
        r.status = resp.status
        r.headers = {k.lower(): v for k, v in resp.getheaders()}
        r.body = resp.read(65536)
        r.total_ms = (time.perf_counter() - t0) * 1000
    except (OSError, http.client.HTTPException) as exc:
        r.error = _short(exc)
        r.total_ms = (time.perf_counter() - t0) * 1000
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    return r


def _short(exc: Exception) -> str:
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused (nothing listening)"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timed out"
    return str(exc) or exc.__class__.__name__
